from __future__ import annotations

import sqlite3

from app import db

BASE_RATING = 1500.0
K_FACTOR = 20.0
HOME_FIELD_ADVANTAGE = 48.0
# FiveThirtyEight regresses each team one-third of the way back to the mean between seasons,
# i.e. retains two-thirds of its rating gap from 1500. This keeps early-season predictions
# from over-trusting a single prior season's sample.
SEASON_REGRESSION_FACTOR = 2.0 / 3.0


def _expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _mov_multiplier(margin: int, elo_diff: float) -> float:
    return ((abs(margin) + 3) ** 0.8) / (7.5 + 0.006 * abs(elo_diff))


def recompute_ratings(conn: sqlite3.Connection, before: tuple[int, int] | None = None) -> dict[str, float]:
    """Replay every finalized game in chronological order to build current Elo ratings.

    Pass `before=(season, week)` to stop the replay just short of that point, giving the
    ratings as they stood right before that game -- used to backtest a past game without
    leaking the results of games that hadn't happened yet.
    """
    ratings = {row["id"]: BASE_RATING for row in conn.execute("SELECT id FROM teams").fetchall()}
    query = "SELECT * FROM games WHERE status = 'final'"
    params: list = []
    if before is not None:
        season, week = before
        query += " AND (season < ? OR (season = ? AND week < ?))"
        params += [season, season, week]
    query += " ORDER BY season ASC, week ASC, kickoff_at ASC, id ASC"
    games = conn.execute(query, params).fetchall()

    current_season = None
    for game in games:
        if current_season is not None and game["season"] != current_season:
            for team_id in ratings:
                ratings[team_id] = BASE_RATING + (ratings[team_id] - BASE_RATING) * SEASON_REGRESSION_FACTOR
        current_season = game["season"]

        home_id, away_id = game["home_team_id"], game["away_team_id"]
        if home_id not in ratings or away_id not in ratings:
            continue

        home_rating = ratings[home_id]
        away_rating = ratings[away_id]
        elo_diff = (home_rating + HOME_FIELD_ADVANTAGE) - away_rating
        expected_home = _expected_score(home_rating + HOME_FIELD_ADVANTAGE, away_rating)

        margin = game["home_score"] - game["away_score"]
        actual_home = 1.0 if margin > 0 else (0.0 if margin < 0 else 0.5)

        change = K_FACTOR * _mov_multiplier(margin, elo_diff) * (actual_home - expected_home)
        ratings[home_id] = home_rating + change
        ratings[away_id] = away_rating - change

    return ratings


def sync_ratings(conn: sqlite3.Connection, updated_at: str) -> None:
    ratings = recompute_ratings(conn)
    for team_id, rating in ratings.items():
        db.upsert_team_rating(conn, team_id, rating, updated_at)
    conn.commit()
