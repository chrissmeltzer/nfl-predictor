from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    abbreviation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    home_team_id TEXT NOT NULL REFERENCES teams(id),
    away_team_id TEXT NOT NULL REFERENCES teams(id),
    kickoff_at TEXT,
    venue_name TEXT,
    is_outdoor INTEGER,
    lat REAL,
    lon REAL,
    status TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER
);
CREATE INDEX IF NOT EXISTS idx_games_season_week ON games(season, week);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    game_id TEXT PRIMARY KEY REFERENCES games(id),
    temp_f REAL,
    wind_mph REAL,
    precip_pct REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL REFERENCES teams(id),
    player_name TEXT NOT NULL,
    position TEXT,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_injuries_team ON injuries(team_id);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL REFERENCES games(id),
    predicted_home_score REAL NOT NULL,
    predicted_away_score REAL NOT NULL,
    factor_breakdown_json TEXT NOT NULL,
    weights_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_game ON predictions(game_id);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
