# app/sync.py
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app import db
from app.config import SYNC_SEASONS_BACK
from app.reference import STADIUMS, canonical_abbreviation
from app.sources import espn, nflverse, weather

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_teams(conn, client: httpx.Client) -> None:
    for team in espn.fetch_teams(client):
        db.upsert_team(conn, team)
    conn.commit()


def sync_historical(conn, client: httpx.Client, min_season: int, max_season: int | None = None) -> None:
    for game in nflverse.fetch_games_csv(client, min_season):
        if max_season is not None and game["season"] >= max_season:
            continue
        home_id = db.get_team_id_by_abbreviation(conn, game["home_abbreviation"])
        away_id = db.get_team_id_by_abbreviation(conn, game["away_abbreviation"])
        if not home_id or not away_id:
            logger.warning("Skipping %s: team not found in teams table", game["id"])
            continue
        stadium = STADIUMS.get(game["home_abbreviation"], {})
        db.upsert_game(conn, {
            **game,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "lat": stadium.get("lat"),
            "lon": stadium.get("lon"),
        })
    conn.commit()


def sync_team_stats(conn, client: httpx.Client, current_season: int, seasons_back: int) -> None:
    """Sync per-team-per-week turnover and offensive EPA stats used by the prediction model."""
    for season in range(current_season - seasons_back, current_season + 1):
        try:
            rows = nflverse.fetch_team_stats(client, season)
        except httpx.HTTPError:
            logger.warning("Skipping team stats for season %s: fetch failed", season)
            continue
        for row in rows:
            team_id = db.get_team_id_by_abbreviation(conn, row["team_abbreviation"])
            if not team_id:
                continue
            db.upsert_team_game_stat(conn, {**row, "team_id": team_id})
        conn.commit()


def sync_schedule(conn, client: httpx.Client, season: int, week: int) -> list[dict]:
    games = espn.fetch_scoreboard(client, season, week)
    for game in games:
        home_row = conn.execute(
            "SELECT abbreviation FROM teams WHERE id = ?", (game["home_team_id"],)
        ).fetchone()
        stadium = STADIUMS.get(home_row["abbreviation"], {}) if home_row else {}
        db.upsert_game(conn, {**game, "lat": stadium.get("lat"), "lon": stadium.get("lon")})
    conn.commit()
    return games


def sync_weather_for_upcoming(conn, client: httpx.Client) -> None:
    rows = conn.execute(
        "SELECT id, kickoff_at, lat, lon FROM games "
        "WHERE status = 'scheduled' AND is_outdoor = 1 AND lat IS NOT NULL"
    ).fetchall()
    for row in rows:
        target_time = datetime.fromisoformat(row["kickoff_at"].replace("Z", "+00:00"))
        try:
            forecast = weather.fetch_forecast(client, row["lat"], row["lon"], target_time)
        except httpx.HTTPError:
            logger.warning("Skipping weather for game %s: fetch failed", row["id"])
            continue
        db.upsert_weather(conn, row["id"], forecast, _now_iso())
    conn.commit()


def sync_injuries_for_upcoming(conn, client: httpx.Client) -> None:
    game_ids = [
        row["id"] for row in conn.execute("SELECT id FROM games WHERE status = 'scheduled'").fetchall()
    ]
    total_teams = conn.execute("SELECT COUNT(*) as c FROM teams").fetchone()["c"]
    seen_teams: set[str] = set()
    for game_id in game_ids:
        if total_teams and len(seen_teams) >= total_teams:
            break
        try:
            summary = espn.fetch_game_summary(client, game_id)
        except httpx.HTTPError:
            logger.warning("Skipping injuries for game %s: summary fetch failed", game_id)
            continue
        by_team: dict[str, list[dict]] = {}
        for injury in espn.parse_injuries(summary):
            by_team.setdefault(injury["team_abbreviation"], []).append(injury)
        # Iterate the summary's team blocks directly (not just by_team, which is
        # built from parse_injuries and so omits teams with zero current injuries)
        # so a team that has fully recovered still gets its stale rows cleared.
        for team_block in summary.get("injuries", []):
            abbr = canonical_abbreviation(team_block["team"]["abbreviation"])
            team_id = db.get_team_id_by_abbreviation(conn, abbr)
            if not team_id or team_id in seen_teams:
                continue
            db.replace_team_injuries(conn, team_id, by_team.get(abbr, []), _now_iso())
            seen_teams.add(team_id)
    conn.commit()


def sync_all(conn, client: httpx.Client, current_season: int) -> None:
    sync_teams(conn, client)
    sync_historical(conn, client, min_season=current_season - SYNC_SEASONS_BACK, max_season=current_season)
    sync_team_stats(conn, client, current_season, SYNC_SEASONS_BACK)
    season, week = espn.fetch_current_week(client)
    for w in range(1, week + 3):
        sync_schedule(conn, client, season, w)
    sync_weather_for_upcoming(conn, client)
    sync_injuries_for_upcoming(conn, client)
    db.set_meta(conn, "last_synced_at", _now_iso())
    conn.commit()
