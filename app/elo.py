from __future__ import annotations

import sqlite3

from app import db

BASE_RATING = 1500.0
K_FACTOR = 20.0
# Matches FiveThirtyEight's historical NFL home-field Elo bonus (~48 rating points).
HOME_FIELD_ADVANTAGE = 48.0


def _expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _mov_multiplier(margin: int, elo_diff: float) -> float:
    """FiveThirtyEight's margin-of-victory multiplier.

    Rewards larger margins of victory while diminishing the impact of blowouts and of wins
    that were already heavily favored, so a 45-point win by a team already expected to win
    big doesn't swing ratings as much as a similar margin from an underdog would.
    """
    return ((abs(margin) + 3) ** 0.8) / (7.5 + 0.006 * abs(elo_diff))


def recompute_ratings(conn: sqlite3.Connection) -> dict[str, float]:
    """Rebuild every team's Elo rating from scratch using all finalized games in order.

    Ratings are recomputed fully each time (rather than updated incrementally) so newly
    synced historical games are always reflected consistently, regardless of sync order.
    """
    ratings = {row["id"]: BASE_RATING for row in conn.execute("SELECT id FROM teams").fetchall()}
    games = conn.execute(
        "SELECT * FROM games WHERE status = 'final' ORDER BY season ASC, week ASC, kickoff_at ASC, id ASC"
    ).fetchall()

    for game in games:
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
