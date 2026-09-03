from __future__ import annotations

import csv
import io
import logging

import httpx

from app.reference import STADIUMS, canonical_abbreviation

logger = logging.getLogger(__name__)

GAMES_CSV_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

_ROOF_OUTDOOR_VALUES = {"outdoors", "open"}
_ROOF_INDOOR_VALUES = {"dome", "closed"}


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
