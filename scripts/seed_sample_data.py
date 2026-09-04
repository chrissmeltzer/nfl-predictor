"""Seed the configured Postgres database (DATABASE_URL) with a small set of
sample data for local UI testing, without hitting any live APIs.
Run with: python scripts/seed_sample_data.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DATABASE_URL

conn = db.get_connection(DATABASE_URL)
db.init_db(conn)

db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
db.upsert_team(conn, {"id": "16", "name": "Minnesota Vikings", "abbreviation": "MIN"})
db.upsert_team(conn, {"id": "22", "name": "Arizona Cardinals", "abbreviation": "ARI"})

past_kickoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
db.upsert_game(conn, {
    "id": "sample_past_1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
    "kickoff_at": past_kickoff, "venue_name": "Lumen Field", "is_outdoor": True,
    "lat": 47.5952, "lon": -122.3316, "status": "final", "home_score": 27, "away_score": 20,
})

upcoming_kickoff = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
db.upsert_game(conn, {
    "id": "sample_upcoming_1", "season": 2026, "week": 1, "home_team_id": "16", "away_team_id": "22",
    "kickoff_at": upcoming_kickoff, "venue_name": "U.S. Bank Stadium", "is_outdoor": False,
    "lat": 44.9735, "lon": -93.2575, "status": "scheduled", "home_score": None, "away_score": None,
})

conn.commit()
_parsed = urlsplit(DATABASE_URL)
print("Seeded sample data into", f"{_parsed.hostname}{_parsed.path}")
