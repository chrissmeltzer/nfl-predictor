from __future__ import annotations

import sqlite3

from app.reference import parse_kickoff

MOV_DAMPENING_CAP = 21.0
# Heuristic exponential decay per game back in time (game 0 = most recent, weight 1.0;
# game 1 gets 0.85, game 2 gets 0.72, etc.), pending calibration against real outcomes.
RECENCY_DECAY = 0.85


def _team_games(
    conn: sqlite3.Connection, team_id: str, limit: int | None = None, before: str | None = None
) -> list[sqlite3.Row]:
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (home_team_id = %s OR away_team_id = %s)
    """
    params = [team_id, team_id]
    if before is not None:
        query += " AND kickoff_at < %s"
        params.append(before)
    query += " ORDER BY kickoff_at DESC, id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, params).fetchall()


def _dampen_margin(scored: float, allowed: float, cap: float) -> tuple[float, float]:
    mean = (scored + allowed) / 2
    diff = scored - allowed
    if diff > cap:
        diff = cap
    elif diff < -cap:
        diff = -cap
    return mean + diff / 2, mean - diff / 2


def recent_scoring_stats(conn: sqlite3.Connection, team_id: str, window: int, before: str | None = None) -> dict:
    games = _team_games(conn, team_id, limit=window, before=before)
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


def recency_weighted_scoring(
    conn: sqlite3.Connection, team_id: str, window: int, before: str | None = None
) -> dict | None:
    """Exponentially recency-weighted scoring average, kept separate from
    recent_scoring_stats() so the flat baseline calculation is unaffected; used only to
    detect whether a team is trending above or below its own window average.
    """
    games = _team_games(conn, team_id, limit=window, before=before)
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


def current_season_sample_size(
    conn: sqlite3.Connection, team_id: str, season: int, window: int, before: str | None = None
) -> int:
    """Count of the team's most recent `window` completed games that fall within `season`.

    Historical seasons are always fully backfilled, so "games played, ever" saturates at
    `window` before a new season even starts and can't signal early-season uncertainty.
    How many of those recent games are from the *current* season actually can.
    """
    games = _team_games(conn, team_id, limit=window, before=before)
    return sum(1 for g in games if g["season"] == season)


def home_away_split(conn: sqlite3.Connection, team_id: str, before: str | None = None) -> dict:
    games = _team_games(conn, team_id, before=before)
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


def head_to_head(conn: sqlite3.Connection, team_id: str, opponent_id: str, before: str | None = None) -> dict:
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (
            (home_team_id = %s AND away_team_id = %s) OR
            (home_team_id = %s AND away_team_id = %s)
        )
    """
    params = [team_id, opponent_id, opponent_id, team_id]
    if before is not None:
        query += " AND kickoff_at < %s"
        params.append(before)
    games = conn.execute(query, params).fetchall()

    if not games:
        return {"avg_points_scored": None, "meetings": 0}

    scored = [g["home_score"] if g["home_team_id"] == team_id else g["away_score"] for g in games]
    return {"avg_points_scored": sum(scored) / len(scored), "meetings": len(games)}


def head_to_head_games(
    conn: sqlite3.Connection, team_id: str, opponent_id: str, before: str | None = None, limit: int = 8
) -> list[dict]:
    """Chronological list of past meetings between two teams, most recent `limit` kept."""
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (
            (home_team_id = %s AND away_team_id = %s) OR
            (home_team_id = %s AND away_team_id = %s)
        )
    """
    params = [team_id, opponent_id, opponent_id, team_id]
    if before is not None:
        query += " AND kickoff_at < %s"
        params.append(before)
    query += " ORDER BY kickoff_at DESC, id DESC LIMIT %s"
    params.append(limit)
    games = conn.execute(query, params).fetchall()

    meetings = []
    for g in reversed(games):
        team_score = g["home_score"] if g["home_team_id"] == team_id else g["away_score"]
        opponent_score = g["away_score"] if g["home_team_id"] == team_id else g["home_score"]
        meetings.append({
            "kickoff_at": g["kickoff_at"],
            "season": g["season"],
            "week": g["week"],
            "team_score": team_score,
            "opponent_score": opponent_score,
        })
    return meetings


def rest_days(conn: sqlite3.Connection, team_id: str, before: str) -> int | None:
    row = conn.execute(
        """
        SELECT kickoff_at FROM games
        WHERE status = 'final' AND (home_team_id = %s OR away_team_id = %s) AND kickoff_at < %s
        ORDER BY kickoff_at DESC LIMIT 1
        """,
        (team_id, team_id, before),
    ).fetchone()
    if not row or not row["kickoff_at"]:
        return None

    last = parse_kickoff(row["kickoff_at"])
    upcoming = parse_kickoff(before)
    return (upcoming - last).days


def _before_season_week_clause(alias: str, before: tuple[int, int] | None) -> tuple[str, list]:
    if before is None:
        return "", []
    season, week = before
    return f" AND ({alias}.season < %s OR ({alias}.season = %s AND {alias}.week < %s))", [season, season, week]


def _team_game_stats(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> list[sqlite3.Row]:
    clause, clause_params = _before_season_week_clause("team_game_stats", before)
    return conn.execute(
        f"SELECT * FROM team_game_stats WHERE team_id = %s{clause} ORDER BY season DESC, week DESC LIMIT %s",
        [team_id, *clause_params, window],
    ).fetchall()


def turnover_form(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> dict:
    rows = _team_game_stats(conn, team_id, window, before)
    if not rows:
        return {"avg_turnovers_committed": None, "games_counted": 0}
    committed = [row["turnovers"] for row in rows]
    return {"avg_turnovers_committed": sum(committed) / len(committed), "games_counted": len(rows)}


def turnovers_forced(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> dict:
    clause, clause_params = _before_season_week_clause("g", before)
    rows = conn.execute(
        f"""
        SELECT tgs.turnovers AS opponent_turnovers
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = %s THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = %s OR g.away_team_id = %s){clause}
        ORDER BY g.season DESC, g.week DESC
        LIMIT %s
        """,
        [team_id, team_id, team_id, *clause_params, window],
    ).fetchall()
    if not rows:
        return {"avg_turnovers_forced": None}
    values = [row["opponent_turnovers"] for row in rows]
    return {"avg_turnovers_forced": sum(values) / len(values)}


def epa_form(conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None) -> dict:
    rows = _team_game_stats(conn, team_id, window, before)
    offense_values = [row["epa_offense"] for row in rows if row["epa_offense"] is not None]
    avg_offense = sum(offense_values) / len(offense_values) if offense_values else None

    clause, clause_params = _before_season_week_clause("g", before)
    opponent_rows = conn.execute(
        f"""
        SELECT tgs.epa_offense AS opponent_epa
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = %s THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = %s OR g.away_team_id = %s){clause}
        ORDER BY g.season DESC, g.week DESC
        LIMIT %s
        """,
        [team_id, team_id, team_id, *clause_params, window],
    ).fetchall()
    allowed_values = [row["opponent_epa"] for row in opponent_rows if row["opponent_epa"] is not None]
    avg_allowed = sum(allowed_values) / len(allowed_values) if allowed_values else None

    return {"epa_offense_avg": avg_offense, "epa_allowed_avg": avg_allowed}


def epa_split_form(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> dict:
    """Separate passing and rushing EPA, rather than the combined epa_offense figure.

    Passing efficiency explains more modern NFL scoring variance than rushing efficiency,
    so keeping them separate lets the prediction weight them differently instead of treating
    a pass-heavy explosive offense the same as a run-heavy grind-it-out one.
    """
    rows = _team_game_stats(conn, team_id, window, before)
    passing_values = [row["epa_passing"] for row in rows if row["epa_passing"] is not None]
    rushing_values = [row["epa_rushing"] for row in rows if row["epa_rushing"] is not None]
    avg_passing = sum(passing_values) / len(passing_values) if passing_values else None
    avg_rushing = sum(rushing_values) / len(rushing_values) if rushing_values else None

    clause, clause_params = _before_season_week_clause("g", before)
    opponent_rows = conn.execute(
        f"""
        SELECT tgs.epa_passing AS opponent_passing, tgs.epa_rushing AS opponent_rushing
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week
         AND tgs.team_id = CASE WHEN g.home_team_id = %s THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = %s OR g.away_team_id = %s){clause}
        ORDER BY g.season DESC, g.week DESC
        LIMIT %s
        """,
        [team_id, team_id, team_id, *clause_params, window],
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


def strength_of_schedule(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> dict:
    clause, clause_params = _before_season_week_clause("g", before)
    opponent_rows = conn.execute(
        f"""
        SELECT CASE WHEN g.home_team_id = %s THEN g.away_team_id ELSE g.home_team_id END AS opponent_id
        FROM games g
        WHERE g.status = 'final' AND (g.home_team_id = %s OR g.away_team_id = %s){clause}
        ORDER BY g.season DESC, g.week DESC
        LIMIT %s
        """,
        [team_id, team_id, team_id, *clause_params, window],
    ).fetchall()
    if not opponent_rows:
        return {"opponent_epa_avg": None}

    epa_values = []
    for row in opponent_rows:
        opponent_form = epa_form(conn, row["opponent_id"], window, before)
        if opponent_form["epa_offense_avg"] is not None:
            epa_values.append(opponent_form["epa_offense_avg"])

    if not epa_values:
        return {"opponent_epa_avg": None}
    return {"opponent_epa_avg": sum(epa_values) / len(epa_values)}


def season_records(conn: sqlite3.Connection, season: int) -> dict[str, tuple[int, int, int]]:
    """Wins/losses/ties for every team with a finalized game this season, in one query --
    keyed by team_id, as (wins, losses, ties). A team with no finalized games is omitted.
    """
    games = conn.execute(
        "SELECT home_team_id, away_team_id, home_score, away_score FROM games "
        "WHERE season = %s AND status = 'final'",
        (season,),
    ).fetchall()
    records: dict[str, list[int]] = {}
    for g in games:
        for team_id, team_score, opponent_score in (
            (g["home_team_id"], g["home_score"], g["away_score"]),
            (g["away_team_id"], g["away_score"], g["home_score"]),
        ):
            wlt = records.setdefault(team_id, [0, 0, 0])
            if team_score > opponent_score:
                wlt[0] += 1
            elif team_score < opponent_score:
                wlt[1] += 1
            else:
                wlt[2] += 1
    return {team_id: tuple(wlt) for team_id, wlt in records.items()}


def _win_pct_from_record(record: tuple[int, int, int] | None) -> float | None:
    if record is None:
        return None
    wins, losses, ties = record
    total = wins + losses + ties
    return (wins + 0.5 * ties) / total if total else None


def remaining_strength_of_schedule(conn: sqlite3.Connection, season: int) -> dict[str, dict]:
    """Ranks every team by the average win % of its remaining (unplayed) opponents this
    season. Rank 1 is the easiest remaining schedule. Teams with no remaining games, or
    whose remaining opponents haven't played yet, are omitted."""
    team_ids = [row["id"] for row in conn.execute("SELECT id FROM teams").fetchall()]
    records = season_records(conn, season)
    win_pct = {team_id: _win_pct_from_record(records.get(team_id)) for team_id in team_ids}

    remaining_games = conn.execute(
        "SELECT home_team_id, away_team_id FROM games WHERE season = %s AND status != 'final'",
        (season,),
    ).fetchall()
    remaining_opponents: dict[str, list[str]] = {team_id: [] for team_id in team_ids}
    for g in remaining_games:
        if g["home_team_id"] in remaining_opponents:
            remaining_opponents[g["home_team_id"]].append(g["away_team_id"])
        if g["away_team_id"] in remaining_opponents:
            remaining_opponents[g["away_team_id"]].append(g["home_team_id"])

    averages = {}
    for team_id, opponent_ids in remaining_opponents.items():
        known_pcts = [win_pct[o] for o in opponent_ids if win_pct.get(o) is not None]
        if known_pcts:
            averages[team_id] = sum(known_pcts) / len(known_pcts)

    ranked = sorted(averages.items(), key=lambda kv: kv[1])
    teams_ranked = len(ranked)
    return {
        team_id: {"opponent_win_pct": pct, "rank": i, "teams_ranked": teams_ranked}
        for i, (team_id, pct) in enumerate(ranked, start=1)
    }


def get_team_rating(conn: sqlite3.Connection, team_id: str, default: float = 1500.0) -> float:
    row = conn.execute("SELECT elo_rating FROM team_ratings WHERE team_id = %s", (team_id,)).fetchone()
    return row["elo_rating"] if row else default


def pass_rush_form(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> dict:
    """Sack rate the team's offense allows (protection) and its defense forces (pressure),
    both expressed as sacks per dropback (pass attempts + sacks) and summed over the window
    rather than averaged per game, so low-volume games don't count as much as high-volume ones.
    """
    rows = _team_game_stats(conn, team_id, window, before)
    sacks_allowed = dropbacks = 0.0
    for row in rows:
        if row["pass_attempts"] is None:
            continue
        sacks_allowed += row["sacks_suffered"]
        dropbacks += row["pass_attempts"] + row["sacks_suffered"]
    protection_rate = sacks_allowed / dropbacks if dropbacks else None

    clause, clause_params = _before_season_week_clause("g", before)
    opponent_rows = conn.execute(
        f"""
        SELECT tgs.def_sacks AS def_sacks,
               opp.pass_attempts AS opp_pass_attempts, opp.sacks_suffered AS opp_sacks_suffered
        FROM games g
        JOIN team_game_stats tgs
          ON tgs.season = g.season AND tgs.week = g.week AND tgs.team_id = %s
        JOIN team_game_stats opp
          ON opp.season = g.season AND opp.week = g.week
         AND opp.team_id = CASE WHEN g.home_team_id = %s THEN g.away_team_id ELSE g.home_team_id END
        WHERE g.status = 'final' AND (g.home_team_id = %s OR g.away_team_id = %s){clause}
        ORDER BY g.season DESC, g.week DESC
        LIMIT %s
        """,
        [team_id, team_id, team_id, team_id, *clause_params, window],
    ).fetchall()
    sacks_forced = opponent_dropbacks = 0.0
    for row in opponent_rows:
        if row["opp_pass_attempts"] is None:
            continue
        sacks_forced += row["def_sacks"]
        opponent_dropbacks += row["opp_pass_attempts"] + row["opp_sacks_suffered"]
    pressure_rate = sacks_forced / opponent_dropbacks if opponent_dropbacks else None

    return {"protection_rate": protection_rate, "pressure_rate": pressure_rate}


def pace_form(
    conn: sqlite3.Connection, team_id: str, window: int, before: tuple[int, int] | None = None
) -> float | None:
    rows = _team_game_stats(conn, team_id, window, before)
    values = [row["plays"] for row in rows if row["plays"] is not None]
    if not values:
        return None
    return sum(values) / len(values)
