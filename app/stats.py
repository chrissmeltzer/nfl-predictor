from __future__ import annotations

import sqlite3
from datetime import datetime

MOV_DAMPENING_CAP = 21.0
# Heuristic exponential decay per game back in time (game 0 = most recent, weight 1.0;
# game 1 gets 0.85, game 2 gets 0.72, etc.), pending calibration against real outcomes.
RECENCY_DECAY = 0.85


def _team_games(conn: sqlite3.Connection, team_id: str, limit: int | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (home_team_id = ? OR away_team_id = ?)
        ORDER BY kickoff_at DESC, id DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, (team_id, team_id)).fetchall()


def _dampen_margin(scored: float, allowed: float, cap: float) -> tuple[float, float]:
    mean = (scored + allowed) / 2
    diff = scored - allowed
    if diff > cap:
        diff = cap
    elif diff < -cap:
        diff = -cap
    return mean + diff / 2, mean - diff / 2


def recent_scoring_stats(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    games = _team_games(conn, team_id, limit=window)
    if not games:
        return {"avg_points_scored": None, "avg_points_allowed": None, "games_counted": 0}

    scored, allowed = [], []
    for g in games:
        if g["home_team_id"] == team_id:
            game_scored, game_allowed = g["home_score"], g["away_score"]
        else:
            game_scored, game_allowed = g["away_score"], g["home_score"]
        damped_scored, damped_allowed = _dampen_margin(game_scored, game_allowed, MOV_DAMPENING_CAP)
        scored.append(damped_scored)
        allowed.append(damped_allowed)

    return {
        "avg_points_scored": sum(scored) / len(scored),
        "avg_points_allowed": sum(allowed) / len(allowed),
        "games_counted": len(games),
    }


def recency_weighted_scoring(conn: sqlite3.Connection, team_id: str, window: int) -> dict | None:
    """Exponentially recency-weighted scoring average, kept separate from
    recent_scoring_stats() so the flat baseline calculation is unaffected; used only to
    detect whether a team is trending above or below its own window average.
    """
    games = _team_games(conn, team_id, limit=window)
    if not games:
        return None

    weighted_scored = 0.0
    weight_sum = 0.0
    for i, g in enumerate(games):
        if g["home_team_id"] == team_id:
            game_scored, game_allowed = g["home_score"], g["away_score"]
        else:
            game_scored, game_allowed = g["away_score"], g["home_score"]
        damped_scored, _ = _dampen_margin(game_scored, game_allowed, MOV_DAMPENING_CAP)
        weight = RECENCY_DECAY ** i
        weighted_scored += damped_scored * weight
        weight_sum += weight

    return {"avg_points_scored": weighted_scored / weight_sum}


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


def _team_game_stats(conn: sqlite3.Connection, team_id: str, window: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM team_game_stats WHERE team_id = ? ORDER BY season DESC, week DESC LIMIT ?",
        (team_id, window),
    ).fetchall()


def turnover_form(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    rows = _team_game_stats(conn, team_id, window)
    if not rows:
        return {"avg_turnovers_committed": None, "games_counted": 0}
    committed = [row["turnovers"] for row in rows]
    return {"avg_turnovers_committed": sum(committed) / len(committed), "games_counted": len(rows)}


def turnovers_forced(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    rows = conn.execute(
        """
        SELECT tgs.turnovers AS opponent_turnovers
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = ? THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = ? OR g.away_team_id = ?)
        ORDER BY g.season DESC, g.week DESC
        LIMIT ?
        """,
        (team_id, team_id, team_id, window),
    ).fetchall()
    if not rows:
        return {"avg_turnovers_forced": None}
    values = [row["opponent_turnovers"] for row in rows]
    return {"avg_turnovers_forced": sum(values) / len(values)}


def epa_form(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    rows = _team_game_stats(conn, team_id, window)
    offense_values = [row["epa_offense"] for row in rows if row["epa_offense"] is not None]
    avg_offense = sum(offense_values) / len(offense_values) if offense_values else None

    opponent_rows = conn.execute(
        """
        SELECT tgs.epa_offense AS opponent_epa
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = ? THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = ? OR g.away_team_id = ?)
        ORDER BY g.season DESC, g.week DESC
        LIMIT ?
        """,
        (team_id, team_id, team_id, window),
    ).fetchall()
    allowed_values = [row["opponent_epa"] for row in opponent_rows if row["opponent_epa"] is not None]
    avg_allowed = sum(allowed_values) / len(allowed_values) if allowed_values else None

    return {"epa_offense_avg": avg_offense, "epa_allowed_avg": avg_allowed}


def epa_split_form(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    """Separate passing and rushing EPA, rather than the combined epa_offense figure.

    Passing efficiency explains more modern NFL scoring variance than rushing efficiency,
    so keeping them separate lets the prediction weight them differently instead of treating
    a pass-heavy explosive offense the same as a run-heavy grind-it-out one.
    """
    rows = _team_game_stats(conn, team_id, window)
    passing_values = [row["epa_passing"] for row in rows if row["epa_passing"] is not None]
    rushing_values = [row["epa_rushing"] for row in rows if row["epa_rushing"] is not None]
    avg_passing = sum(passing_values) / len(passing_values) if passing_values else None
    avg_rushing = sum(rushing_values) / len(rushing_values) if rushing_values else None

    opponent_rows = conn.execute(
        """
        SELECT tgs.epa_passing AS opponent_passing, tgs.epa_rushing AS opponent_rushing
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = ? THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = ? OR g.away_team_id = ?)
        ORDER BY g.season DESC, g.week DESC
        LIMIT ?
        """,
        (team_id, team_id, team_id, window),
    ).fetchall()
    passing_allowed = [row["opponent_passing"] for row in opponent_rows if row["opponent_passing"] is not None]
    rushing_allowed = [row["opponent_rushing"] for row in opponent_rows if row["opponent_rushing"] is not None]
    avg_passing_allowed = sum(passing_allowed) / len(passing_allowed) if passing_allowed else None
    avg_rushing_allowed = sum(rushing_allowed) / len(rushing_allowed) if rushing_allowed else None

    return {
        "passing_epa_avg": avg_passing,
        "passing_epa_allowed_avg": avg_passing_allowed,
        "rushing_epa_avg": avg_rushing,
        "rushing_epa_allowed_avg": avg_rushing_allowed,
    }


def strength_of_schedule(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    opponent_rows = conn.execute(
        """
        SELECT CASE WHEN g.home_team_id = ? THEN g.away_team_id ELSE g.home_team_id END AS opponent_id
        FROM games g
        WHERE g.status = 'final' AND (g.home_team_id = ? OR g.away_team_id = ?)
        ORDER BY g.season DESC, g.week DESC
        LIMIT ?
        """,
        (team_id, team_id, team_id, window),
    ).fetchall()
    if not opponent_rows:
        return {"opponent_epa_avg": None}

    epa_values = []
    for row in opponent_rows:
        opponent_form = epa_form(conn, row["opponent_id"], window)
        if opponent_form["epa_offense_avg"] is not None:
            epa_values.append(opponent_form["epa_offense_avg"])

    if not epa_values:
        return {"opponent_epa_avg": None}
    return {"opponent_epa_avg": sum(epa_values) / len(epa_values)}


def get_team_rating(conn: sqlite3.Connection, team_id: str, default: float = 1500.0) -> float:
    row = conn.execute("SELECT elo_rating FROM team_ratings WHERE team_id = ?", (team_id,)).fetchone()
    return row["elo_rating"] if row else default


def pace_form(conn: sqlite3.Connection, team_id: str, window: int) -> float | None:
    rows = _team_game_stats(conn, team_id, window)
    values = [row["plays"] for row in rows if row["plays"] is not None]
    if not values:
        return None
    return sum(values) / len(values)
