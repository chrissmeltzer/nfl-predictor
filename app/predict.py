from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import elo, stats
from app.reference import DEFAULT_POSITION_IMPORTANCE, POSITION_IMPORTANCE, STADIUMS, injury_counts_toward_model

LEAGUE_AVERAGE_SCORE = 21.0
LEAGUE_AVERAGE_TOTAL = 44.0
LEAGUE_AVERAGE_PLAYS = 64.0
ADVANCED_STATS_WINDOW = 8
TRAVEL_POINTS_PER_1000KM = 1.0
LEAGUE_AVERAGE_SACK_RATE = 0.065
LEAGUE_AVERAGE_DROPBACKS = 35.0
POINTS_PER_SACK = 1.7


def load_weights(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _average(a: float | None, b: float | None) -> float:
    values = [v for v in (a, b) if v is not None]
    return sum(values) / len(values) if values else LEAGUE_AVERAGE_SCORE


def _scaled_baseline(raw_baseline: float, weight: float) -> float:
    return LEAGUE_AVERAGE_SCORE + weight * (raw_baseline - LEAGUE_AVERAGE_SCORE)


def _home_away_adjustment(conn, team_id: str, is_home: bool, weight: float, before: str | None) -> float:
    split = stats.home_away_split(conn, team_id, before=before)
    if split["overall_avg"] is None:
        return 0.0
    side_avg = split["home_avg"] if is_home else split["away_avg"]
    return weight * (side_avg - split["overall_avg"])


def _head_to_head_adjustment(
    conn, team_id: str, opponent_id: str, overall_avg, weight: float, before: str | None
) -> float:
    h2h = stats.head_to_head(conn, team_id, opponent_id, before=before)
    if h2h["avg_points_scored"] is None or overall_avg is None:
        return 0.0
    delta = h2h["avg_points_scored"] - overall_avg
    return weight * _clamp(delta, -7, 7)


def _rest_days_adjustment(conn, team_id: str, kickoff_at: str, weight: float) -> float:
    days = stats.rest_days(conn, team_id, before=kickoff_at)
    if days is None:
        return 0.0
    return weight * _clamp((days - 7) * 0.5, -3, 3)


def _weather_adjustment(weather_row: sqlite3.Row | None, weight: float) -> float:
    if weather_row is None:
        return 0.0
    wind_penalty = -_clamp(((weather_row["wind_mph"] or 0) - 15) / 5, 0, 3)
    precip_penalty = -_clamp((weather_row["precip_pct"] or 0) / 25, 0, 2)
    return weight * (wind_penalty + precip_penalty)


def _injuries_adjustment(conn, team_id: str, weight: float) -> float:
    rows = conn.execute("SELECT position, status FROM injuries WHERE team_id = %s", (team_id,)).fetchall()
    total = 0.0
    for row in rows:
        if not injury_counts_toward_model(row["status"]):
            continue
        importance = POSITION_IMPORTANCE.get(row["position"], DEFAULT_POSITION_IMPORTANCE)
        total -= importance * 0.3
    return weight * _clamp(total, -10, 0)


def _recency_trend_adjustment(conn, team_id: str, window: int, weight: float, before: str | None) -> float:
    flat = stats.recent_scoring_stats(conn, team_id, window, before=before)
    weighted = stats.recency_weighted_scoring(conn, team_id, window, before=before)
    if flat["avg_points_scored"] is None or weighted is None:
        return 0.0
    delta = weighted["avg_points_scored"] - flat["avg_points_scored"]
    return weight * _clamp(delta, -4, 4)


def _team_form_adjustment(conn, team_id: str, weight: float, before: tuple[int, int] | None) -> float:
    """Combined turnover-margin and split passing/rushing EPA signal.

    Passing efficiency explains more modern NFL scoring variance than rushing efficiency,
    so the two EPA components are weighted 0.65/0.35 rather than treated equally. Turnovers
    and EPA are averaged together (not summed) since both largely measure the same
    underlying team quality.
    """
    committed = stats.turnover_form(conn, team_id, ADVANCED_STATS_WINDOW, before=before)["avg_turnovers_committed"]
    forced = stats.turnovers_forced(conn, team_id, ADVANCED_STATS_WINDOW, before=before)["avg_turnovers_forced"]
    turnover_component = (
        _clamp((forced - committed) * 4, -6, 6) if committed is not None and forced is not None else None
    )

    split = stats.epa_split_form(conn, team_id, ADVANCED_STATS_WINDOW, before=before)
    pass_net = (
        split["passing_epa_avg"] - split["passing_epa_allowed_avg"]
        if split["passing_epa_avg"] is not None and split["passing_epa_allowed_avg"] is not None else None
    )
    rush_net = (
        split["rushing_epa_avg"] - split["rushing_epa_allowed_avg"]
        if split["rushing_epa_avg"] is not None and split["rushing_epa_allowed_avg"] is not None else None
    )

    epa_parts, epa_weights = [], []
    if pass_net is not None:
        epa_parts.append(pass_net)
        epa_weights.append(0.65)
    if rush_net is not None:
        epa_parts.append(rush_net)
        epa_weights.append(0.35)
    epa_component = None
    if epa_parts:
        composite_net_epa = sum(p * w for p, w in zip(epa_parts, epa_weights)) / sum(epa_weights)
        epa_component = _clamp(composite_net_epa * 20, -8, 8)

    components = [c for c in (turnover_component, epa_component) if c is not None]
    if not components:
        return 0.0
    return weight * (sum(components) / len(components))


def _strength_of_schedule_adjustment(
    conn, team_id: str, baseline_delta: float, weight: float, before: tuple[int, int] | None
) -> float:
    sos = stats.strength_of_schedule(conn, team_id, ADVANCED_STATS_WINDOW, before=before)
    if sos["opponent_epa_avg"] is None:
        return 0.0
    difficulty_factor = _clamp(sos["opponent_epa_avg"] * 10, -1, 1)
    return weight * baseline_delta * difficulty_factor * 0.5


def _pass_protection_adjustment(
    conn, team_id: str, opponent_id: str, weight: float, before: tuple[int, int] | None
) -> float:
    """Blends the offense's own sack rate allowed with the opponent's sack rate forced, both
    relative to the league-average sack rate, into a point swing. A bottom-tier offensive
    line facing a top pass rush projects extra sacks -- and lost points -- beyond what either
    factor alone would suggest.
    """
    own = stats.pass_rush_form(conn, team_id, ADVANCED_STATS_WINDOW, before=before)
    opponent = stats.pass_rush_form(conn, opponent_id, ADVANCED_STATS_WINDOW, before=before)

    deltas = []
    if own["protection_rate"] is not None:
        deltas.append(own["protection_rate"] - LEAGUE_AVERAGE_SACK_RATE)
    if opponent["pressure_rate"] is not None:
        deltas.append(opponent["pressure_rate"] - LEAGUE_AVERAGE_SACK_RATE)
    if not deltas:
        return 0.0

    matchup_index = sum(deltas) / len(deltas)
    extra_sacks = matchup_index * LEAGUE_AVERAGE_DROPBACKS
    return -weight * _clamp(extra_sacks * POINTS_PER_SACK, -5, 5)


def _team_ratings(conn, game: sqlite3.Row, home_id: str, away_id: str) -> dict[str, float]:
    """Elo ratings for both teams, as they stood at kickoff of `game`.

    For an upcoming game, that's simply the persisted current ratings -- every finalized
    game in the database already happened before it. For a game that's itself final (i.e.
    we're backtesting/calibrating against it), the persisted ratings reflect *all* finalized
    games including this one and every one after it, so they're replayed fresh up to (but
    not including) this game instead, to avoid leaking future results into its own prediction.
    """
    if game["status"] == "final":
        computed = elo.recompute_ratings(conn, before=(game["season"], game["week"]))
        return {home_id: computed[home_id], away_id: computed[away_id]}
    return {home_id: stats.get_team_rating(conn, home_id), away_id: stats.get_team_rating(conn, away_id)}


def _effective_rating(rating: float, is_home: bool) -> float:
    """A team's Elo rating with home-field advantage folded in, if it's playing at home."""
    return rating + elo.HOME_FIELD_ADVANTAGE if is_home else rating


def _elo_adjustment(
    ratings: dict[str, float], team_id: str, opponent_id: str, is_home: bool, weight: float
) -> float:
    rating_diff = _effective_rating(ratings[team_id], is_home) - _effective_rating(ratings[opponent_id], not is_home)
    return weight * _clamp(rating_diff / 25.0, -7, 7)


def _elo_favorite(ratings: dict[str, float], home_id: str, away_id: str) -> str:
    """Which team Elo alone (rating + home field, no other factors) would favor -- used to
    flag when the full model's pick diverges from the raw power rating.
    """
    home_rating = _effective_rating(ratings[home_id], True)
    away_rating = _effective_rating(ratings[away_id], False)
    return home_id if home_rating >= away_rating else away_id


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _travel_adjustment(game: sqlite3.Row, away_abbr: str | None, is_home: bool, weight: float) -> float:
    if is_home or away_abbr is None:
        return 0.0
    if game["lat"] is None or game["lon"] is None:
        return 0.0
    away_stadium = STADIUMS.get(away_abbr)
    if not away_stadium:
        return 0.0
    distance_km = _haversine_km(game["lat"], game["lon"], away_stadium["lat"], away_stadium["lon"])
    penalty = -(distance_km / 1000.0) * TRAVEL_POINTS_PER_1000KM
    return weight * _clamp(penalty, -4, 0)


def _weather_total_shift(weather_row: sqlite3.Row | None) -> float:
    if weather_row is None:
        return 0.0
    wind_penalty = _clamp(((weather_row["wind_mph"] or 0) - 15) / 5, 0, 3)
    precip_penalty = _clamp((weather_row["precip_pct"] or 0) / 25, 0, 2)
    return -(wind_penalty + precip_penalty) * 2


def _pace_target_shift(conn, home_id: str, away_id: str, weight: float, before: tuple[int, int] | None) -> float:
    home_pace = stats.pace_form(conn, home_id, ADVANCED_STATS_WINDOW, before=before)
    away_pace = stats.pace_form(conn, away_id, ADVANCED_STATS_WINDOW, before=before)
    if home_pace is None or away_pace is None:
        return 0.0
    combined_avg_plays = (home_pace + away_pace) / 2
    return weight * _clamp((combined_avg_plays - LEAGUE_AVERAGE_PLAYS) * 0.8, -6, 6)


def _apply_total_anchor(
    conn, home_id: str, away_id: str, home_final: float, away_final: float,
    weather_row: sqlite3.Row | None, weights: dict, before: tuple[int, int] | None,
) -> tuple[float, float, float]:
    anchor_weight = weights.get("total_points_anchor", 0.5)
    pace_weight = weights.get("pace", 0.3)

    predicted_total = home_final + away_final
    predicted_margin = home_final - away_final

    target_total = LEAGUE_AVERAGE_TOTAL
    target_total += _weather_total_shift(weather_row)
    target_total += _pace_target_shift(conn, home_id, away_id, pace_weight, before)

    blended_total = predicted_total + anchor_weight * (target_total - predicted_total)
    new_home = (blended_total + predicted_margin) / 2
    new_away = (blended_total - predicted_margin) / 2
    return new_home, new_away, blended_total - predicted_total


def _prediction_confidence(home_season_games: int, away_season_games: int, window: int) -> tuple[int, str]:
    """Rough confidence score based on how much *current-season* game data underlies the
    prediction. Not a statistically calibrated probability -- just a UI-facing signal that
    early-season predictions (still leaning on last year's roster and form) should be
    trusted less than ones backed by a full season-to-date sample.
    """
    window = max(window, 1)
    combined = min(home_season_games, away_season_games)
    score = 30 + min(combined, window) / window * 60
    score = round(score)
    label = "Low" if score < 50 else ("Moderate" if score < 75 else "High")
    return score, label


def predict_game(conn: sqlite3.Connection, weights: dict, game: sqlite3.Row) -> dict:
    window = weights.get("recent_games_window", 8)
    home_id, away_id = game["home_team_id"], game["away_team_id"]
    # Every historical lookup below is cut off at this game's own kickoff, so backtesting an
    # already-final game (e.g. during calibration) can't leak the results of games that hadn't
    # happened yet. For a genuinely upcoming game this is a no-op -- only past games are ever
    # final, so they're already all before it.
    before_kickoff = game["kickoff_at"]
    before_season_week = (game["season"], game["week"])
    ratings = _team_ratings(conn, game, home_id, away_id)

    home_abbr_row = conn.execute("SELECT abbreviation FROM teams WHERE id = %s", (home_id,)).fetchone()
    away_abbr_row = conn.execute("SELECT abbreviation FROM teams WHERE id = %s", (away_id,)).fetchone()
    away_abbr = away_abbr_row["abbreviation"] if away_abbr_row else None

    home_recent = stats.recent_scoring_stats(conn, home_id, window, before=before_kickoff)
    away_recent = stats.recent_scoring_stats(conn, away_id, window, before=before_kickoff)
    trend_weight = weights.get("recent_scoring_trend", 1.0)

    home_baseline = _scaled_baseline(
        _average(home_recent["avg_points_scored"], away_recent["avg_points_allowed"]), trend_weight
    )
    away_baseline = _scaled_baseline(
        _average(away_recent["avg_points_scored"], home_recent["avg_points_allowed"]), trend_weight
    )

    home_split = stats.home_away_split(conn, home_id, before=before_kickoff)
    away_split = stats.home_away_split(conn, away_id, before=before_kickoff)

    weather_row = conn.execute(
        "SELECT * FROM weather_forecasts WHERE game_id = %s", (game["id"],)
    ).fetchone()

    breakdown = {"home": {}, "away": {}}

    def apply(side: str, team_id: str, baseline: float, is_home: bool, overall_avg):
        opponent_id = away_id if is_home else home_id
        adjustments = {
            "home_away_split": _home_away_adjustment(
                conn, team_id, is_home, weights.get("home_away_split", 1.0), before_kickoff
            ),
            "head_to_head": _head_to_head_adjustment(
                conn, team_id, opponent_id, overall_avg, weights.get("head_to_head", 0.5), before_kickoff
            ),
            "weather": _weather_adjustment(weather_row, weights.get("weather", 1.0)),
            "rest_days": (
                _rest_days_adjustment(conn, team_id, game["kickoff_at"], weights.get("rest_days", 0.5))
                if game["kickoff_at"] else 0.0
            ),
            "injuries": _injuries_adjustment(conn, team_id, weights.get("injuries", 1.0)),
            "team_form": _team_form_adjustment(conn, team_id, weights.get("team_form", 1.0), before_season_week),
            "strength_of_schedule": _strength_of_schedule_adjustment(
                conn, team_id, baseline - LEAGUE_AVERAGE_SCORE, weights.get("strength_of_schedule", 0.5),
                before_season_week,
            ),
            "elo": _elo_adjustment(ratings, team_id, opponent_id, is_home, weights.get("elo", 0.35)),
            "pass_protection": _pass_protection_adjustment(
                conn, team_id, opponent_id, weights.get("pass_protection", 0.5), before_season_week
            ),
            "recency_trend": _recency_trend_adjustment(
                conn, team_id, window, weights.get("recency_trend", 0.5), before_kickoff
            ),
            "travel": _travel_adjustment(game, away_abbr, is_home, weights.get("travel", 0.5)),
        }
        breakdown[side] = {"baseline": baseline, **adjustments}
        return baseline + sum(adjustments.values())

    home_final = apply("home", home_id, home_baseline, True, home_split["overall_avg"])
    away_final = apply("away", away_id, away_baseline, False, away_split["overall_avg"])

    home_final, away_final, anchor_delta = _apply_total_anchor(
        conn, home_id, away_id, home_final, away_final, weather_row, weights, before_season_week
    )
    breakdown["total_anchor_delta"] = anchor_delta

    home_season_games = stats.current_season_sample_size(
        conn, home_id, game["season"], window, before=before_kickoff
    )
    away_season_games = stats.current_season_sample_size(
        conn, away_id, game["season"], window, before=before_kickoff
    )
    confidence_score, confidence_label = _prediction_confidence(home_season_games, away_season_games, window)

    model_favorite = home_id if home_final >= away_final else away_id
    upset_alert = model_favorite != _elo_favorite(ratings, home_id, away_id)

    return {
        "predicted_home_score": round(max(home_final, 0), 1),
        "predicted_away_score": round(max(away_final, 0), 1),
        "confidence_score": confidence_score,
        "confidence_label": confidence_label,
        "breakdown": breakdown,
        "upset_alert": upset_alert,
    }


def get_latest_prediction(conn: sqlite3.Connection, game_id: str) -> dict | None:
    row = conn.execute(
        "SELECT predicted_home_score, predicted_away_score FROM predictions "
        "WHERE game_id = %s ORDER BY id DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return {"predicted_home_score": row["predicted_home_score"], "predicted_away_score": row["predicted_away_score"]}


def save_prediction(conn: sqlite3.Connection, game_id: str, result: dict, weights: dict) -> None:
    """Snapshot the current prediction for a game, replacing any prior snapshot for it --
    there's only ever one "latest" prediction per game, so this upserts on game_id rather
    than accumulating a new row every time a still-scheduled game is re-synced.
    """
    conn.execute(
        """
        INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score,
                                  factor_breakdown_json, weights_snapshot_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(game_id) DO UPDATE SET
            predicted_home_score = excluded.predicted_home_score,
            predicted_away_score = excluded.predicted_away_score,
            factor_breakdown_json = excluded.factor_breakdown_json,
            weights_snapshot_json = excluded.weights_snapshot_json,
            created_at = excluded.created_at
        """,
        (
            game_id,
            result["predicted_home_score"],
            result["predicted_away_score"],
            json.dumps(result["breakdown"]),
            json.dumps(weights),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
