from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import stats
from app.reference import DEFAULT_POSITION_IMPORTANCE, POSITION_IMPORTANCE

INJURY_STATUSES_COUNTED = {"Out", "Doubtful", "Injured Reserve"}
LEAGUE_AVERAGE_SCORE = 21.0
ADVANCED_STATS_WINDOW = 8


def load_weights(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _average(a: float | None, b: float | None) -> float:
    values = [v for v in (a, b) if v is not None]
    return sum(values) / len(values) if values else LEAGUE_AVERAGE_SCORE


def _scaled_baseline(raw_baseline: float, weight: float) -> float:
    """Blend the recent-scoring-derived baseline with the league average.

    weight=1.0 (the default) uses the raw baseline unchanged; weight=0
    ignores recent form entirely and predicts the league average; values
    in between (or above 1) scale how strongly recent form is trusted.
    """
    return LEAGUE_AVERAGE_SCORE + weight * (raw_baseline - LEAGUE_AVERAGE_SCORE)


def _home_away_adjustment(conn, team_id: str, is_home: bool, weight: float) -> float:
    split = stats.home_away_split(conn, team_id)
    if split["overall_avg"] is None:
        return 0.0
    side_avg = split["home_avg"] if is_home else split["away_avg"]
    return weight * (side_avg - split["overall_avg"])


def _head_to_head_adjustment(conn, team_id: str, opponent_id: str, overall_avg, weight: float) -> float:
    h2h = stats.head_to_head(conn, team_id, opponent_id)
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
    wind_penalty = -_clamp((weather_row["wind_mph"] - 15) / 5, 0, 3)
    precip_penalty = -_clamp(weather_row["precip_pct"] / 25, 0, 2)
    return weight * (wind_penalty + precip_penalty)


def _injuries_adjustment(conn, team_id: str, weight: float) -> float:
    rows = conn.execute("SELECT position, status FROM injuries WHERE team_id = ?", (team_id,)).fetchall()
    total = 0.0
    for row in rows:
        if row["status"] not in INJURY_STATUSES_COUNTED:
            continue
        importance = POSITION_IMPORTANCE.get(row["position"], DEFAULT_POSITION_IMPORTANCE)
        total -= importance * 0.3
    return weight * _clamp(total, -10, 0)


def _turnover_adjustment(conn, team_id: str, weight: float) -> float:
    committed = stats.turnover_form(conn, team_id, ADVANCED_STATS_WINDOW)["avg_turnovers_committed"]
    forced = stats.turnovers_forced(conn, team_id, ADVANCED_STATS_WINDOW)["avg_turnovers_forced"]
    if committed is None or forced is None:
        return 0.0
    margin = forced - committed
    # Each net turnover is worth roughly 4 points of field position and possession value.
    return weight * _clamp(margin * 4, -6, 6)


def _epa_adjustment(conn, team_id: str, weight: float) -> float:
    form = stats.epa_form(conn, team_id, ADVANCED_STATS_WINDOW)
    if form["epa_offense_avg"] is None or form["epa_allowed_avg"] is None:
        return 0.0
    net_epa = form["epa_offense_avg"] - form["epa_allowed_avg"]
    # Team EPA/play typically ranges roughly -0.3 to +0.3; scale into point space.
    return weight * _clamp(net_epa * 20, -8, 8)


def _strength_of_schedule_adjustment(conn, team_id: str, baseline_delta: float, weight: float) -> float:
    sos = stats.strength_of_schedule(conn, team_id, ADVANCED_STATS_WINDOW)
    if sos["opponent_epa_avg"] is None:
        return 0.0
    # Scale recent-form trust up when opponents were strong, down when opponents were weak.
    difficulty_factor = _clamp(sos["opponent_epa_avg"] * 10, -1, 1)
    return weight * baseline_delta * difficulty_factor * 0.5


def predict_game(conn: sqlite3.Connection, weights: dict, game: sqlite3.Row) -> dict:
    window = weights.get("recent_games_window", 8)
    home_id, away_id = game["home_team_id"], game["away_team_id"]

    home_recent = stats.recent_scoring_stats(conn, home_id, window)
    away_recent = stats.recent_scoring_stats(conn, away_id, window)
    trend_weight = weights.get("recent_scoring_trend", 1.0)

    home_baseline = _scaled_baseline(
        _average(home_recent["avg_points_scored"], away_recent["avg_points_allowed"]), trend_weight
    )
    away_baseline = _scaled_baseline(
        _average(away_recent["avg_points_scored"], home_recent["avg_points_allowed"]), trend_weight
    )

    home_split = stats.home_away_split(conn, home_id)
    away_split = stats.home_away_split(conn, away_id)

    weather_row = conn.execute(
        "SELECT * FROM weather_forecasts WHERE game_id = ?", (game["id"],)
    ).fetchone()

    breakdown = {"home": {}, "away": {}}

    def apply(side: str, team_id: str, baseline: float, is_home: bool, overall_avg):
        opponent_id = away_id if is_home else home_id
        adjustments = {
            "home_away_split": _home_away_adjustment(conn, team_id, is_home, weights.get("home_away_split", 1.0)),
            "head_to_head": _head_to_head_adjustment(
                conn, team_id, opponent_id, overall_avg, weights.get("head_to_head", 0.5)
            ),
            "weather": _weather_adjustment(weather_row, weights.get("weather", 1.0)),
            "rest_days": (
                _rest_days_adjustment(conn, team_id, game["kickoff_at"], weights.get("rest_days", 0.5))
                if game["kickoff_at"] else 0.0
            ),
            "injuries": _injuries_adjustment(conn, team_id, weights.get("injuries", 1.0)),
            "turnovers": _turnover_adjustment(conn, team_id, weights.get("turnovers", 1.0)),
            "epa": _epa_adjustment(conn, team_id, weights.get("epa", 1.0)),
            "strength_of_schedule": _strength_of_schedule_adjustment(
                conn, team_id, baseline - LEAGUE_AVERAGE_SCORE, weights.get("strength_of_schedule", 0.5)
            ),
        }
        breakdown[side] = {"baseline": baseline, **adjustments}
        return baseline + sum(adjustments.values())

    home_final = apply("home", home_id, home_baseline, True, home_split["overall_avg"])
    away_final = apply("away", away_id, away_baseline, False, away_split["overall_avg"])

    return {
        "predicted_home_score": round(max(home_final, 0), 1),
        "predicted_away_score": round(max(away_final, 0), 1),
        "breakdown": breakdown,
    }


def save_prediction(conn: sqlite3.Connection, game_id: str, result: dict, weights: dict) -> None:
    conn.execute(
        """
        INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score,
                                  factor_breakdown_json, weights_snapshot_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
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
