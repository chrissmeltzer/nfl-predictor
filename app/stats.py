from __future__ import annotations

import sqlite3
from datetime import datetime


def _team_games(conn: sqlite3.Connection, team_id: str, limit: int | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (home_team_id = ? OR away_team_id = ?)
        ORDER BY kickoff_at DESC, id DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, (team_id, team_id)).fetchall()


def recent_scoring_stats(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    games = _team_games(conn, team_id, limit=window)
    if not games:
        return {"avg_points_scored": None, "avg_points_allowed": None, "games_counted": 0}

    scored, allowed = [], []
    for g in games:
        if g["home_team_id"] == team_id:
            scored.append(g["home_score"])
            allowed.append(g["away_score"])
        else:
            scored.append(g["away_score"])
            allowed.append(g["home_score"])

    return {
        "avg_points_scored": sum(scored) / len(scored),
        "avg_points_allowed": sum(allowed) / len(allowed),
        "games_counted": len(games),
    }


def home_away_split(conn: sqlite3.Connection, team_id: str) -> dict:
    games = _team_games(conn, team_id)
    if not games:
        return {"home_avg": None, "away_avg": None, "overall_avg": None}

    all_scores, home_scores, away_scores = [], [], []
    for g in games:
        if g["home_team_id"] == team_id:
            all_scores.append(g["home_score"])
            home_scores.append(g["home_score"])
        else:
            all_scores.append(g["away_score"])
            away_scores.append(g["away_score"])

    overall_avg = sum(all_scores) / len(all_scores)
    return {
        "home_avg": sum(home_scores) / len(home_scores) if home_scores else overall_avg,
        "away_avg": sum(away_scores) / len(away_scores) if away_scores else overall_avg,
        "overall_avg": overall_avg,
    }


def head_to_head(conn: sqlite3.Connection, team_id: str, opponent_id: str) -> dict:
    games = conn.execute(
        """
        SELECT * FROM games
        WHERE status = 'final' AND (
            (home_team_id = ? AND away_team_id = ?) OR
            (home_team_id = ? AND away_team_id = ?)
        )
        """,
        (team_id, opponent_id, opponent_id, team_id),
    ).fetchall()

    if not games:
        return {"avg_points_scored": None, "meetings": 0}

    scored = [g["home_score"] if g["home_team_id"] == team_id else g["away_score"] for g in games]
    return {"avg_points_scored": sum(scored) / len(scored), "meetings": len(games)}


def rest_days(conn: sqlite3.Connection, team_id: str, before: str) -> int | None:
    row = conn.execute(
        """
        SELECT kickoff_at FROM games
        WHERE status = 'final' AND (home_team_id = ? OR away_team_id = ?) AND kickoff_at < ?
        ORDER BY kickoff_at DESC LIMIT 1
        """,
        (team_id, team_id, before),
    ).fetchone()
    if not row or not row["kickoff_at"]:
        return None

    last = datetime.fromisoformat(row["kickoff_at"].replace("Z", "+00:00"))
    upcoming = datetime.fromisoformat(before.replace("Z", "+00:00"))
    return (upcoming - last).days
