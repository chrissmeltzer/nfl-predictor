from __future__ import annotations

import csv
import io
import logging

import httpx

from app.reference import STADIUMS, canonical_abbreviation

logger = logging.getLogger(__name__)

GAMES_CSV_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
TEAM_STATS_CSV_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"

_ROOF_OUTDOOR_VALUES = {"outdoors", "open"}
_ROOF_INDOOR_VALUES = {"dome", "closed"}

# Offensive giveaways: interceptions thrown plus fumbles lost across rush/pass/receiving plays.
# Column names follow the documented nflverse load_team_stats() schema; verify against the live
# CSV header on first sync since this could not be confirmed against a live network response.
TURNOVER_COLUMNS = ["interceptions", "rushing_fumbles_lost", "sack_fumbles_lost", "receiving_fumbles_lost"]
EPA_COLUMNS = ["passing_epa", "rushing_epa"]


def parse_games_csv(csv_text: str, min_season: int) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    games = []
    for row in reader:
        season = int(row["season"])
        if season < min_season or row["game_type"] != "REG":
            continue
        if not row["home_score"] or not row["away_score"]:
            continue

        home_abbr = canonical_abbreviation(row["home_team"])
        away_abbr = canonical_abbreviation(row["away_team"])
        if home_abbr not in STADIUMS or away_abbr not in STADIUMS:
            logger.warning("Skipping %s: unknown team abbreviation", row["game_id"])
            continue

        roof = (row.get("roof") or "").lower()
        is_outdoor = None
        if roof in _ROOF_OUTDOOR_VALUES:
            is_outdoor = True
        elif roof in _ROOF_INDOOR_VALUES:
            is_outdoor = False

        games.append({
            "id": row["game_id"],
            "season": season,
            "week": int(row["week"]),
            "home_abbreviation": home_abbr,
            "away_abbreviation": away_abbr,
            "kickoff_at": None,
            "venue_name": row.get("stadium"),
            "is_outdoor": is_outdoor,
            "status": "final",
            "home_score": int(row["home_score"]),
            "away_score": int(row["away_score"]),
        })
    return games


def fetch_games_csv(client: httpx.Client, min_season: int) -> list[dict]:
    resp = client.get(GAMES_CSV_URL)
    resp.raise_for_status()
    return parse_games_csv(resp.text, min_season)


def _sum_columns(row: dict, columns: list[str]) -> float:
    total = 0.0
    for column in columns:
        value = row.get(column)
        if not value:
            continue
        try:
            total += float(value)
        except ValueError:
            continue
    return total


def parse_team_stats_csv(csv_text: str, min_season: int) -> list[dict]:
    """Parse nflverse's weekly team-stats release into per-team-per-week turnover and EPA rows.

    Rows with a season below ``min_season`` or outside the regular season are skipped. Turnover
    and EPA columns are summed defensively (missing columns contribute 0) since the exact column
    set can shift between nflverse releases.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    stats = []
    for row in reader:
        try:
            season = int(row["season"])
            week = int(row["week"])
        except (KeyError, ValueError):
            continue
        if season < min_season:
            continue
        if row.get("season_type") and row["season_type"] != "REG":
            continue

        abbr = canonical_abbreviation(row.get("team", ""))
        if abbr not in STADIUMS:
            continue

        stats.append({
            "team_abbreviation": abbr,
            "season": season,
            "week": week,
            "turnovers": int(round(_sum_columns(row, TURNOVER_COLUMNS))),
            "epa_offense": _sum_columns(row, EPA_COLUMNS),
        })
    return stats


def fetch_team_stats(client: httpx.Client, season: int) -> list[dict]:
    resp = client.get(TEAM_STATS_CSV_URL.format(season=season))
    resp.raise_for_status()
    return parse_team_stats_csv(resp.text, min_season=season)
