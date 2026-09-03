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

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_game_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL REFERENCES teams(id),
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    turnovers INTEGER NOT NULL DEFAULT 0,
    epa_offense REAL,
    epa_passing REAL,
    epa_rushing REAL,
    plays REAL,
    sacks_suffered INTEGER NOT NULL DEFAULT 0,
    pass_attempts REAL,
    def_sacks INTEGER NOT NULL DEFAULT 0,
    UNIQUE(team_id, season, week)
);
CREATE INDEX IF NOT EXISTS idx_team_game_stats_team ON team_game_stats(team_id, season, week);

CREATE TABLE IF NOT EXISTS team_ratings (
    team_id TEXT PRIMARY KEY REFERENCES teams(id),
    elo_rating REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    game_id TEXT NOT NULL REFERENCES games(id),
    picked_team_id TEXT NOT NULL REFERENCES teams(id),
    created_at TEXT NOT NULL,
    UNIQUE(player_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_picks_player ON picks(player_id);
CREATE INDEX IF NOT EXISTS idx_picks_game ON picks(game_id);
"""

# Columns that may be missing from a team_game_stats table created by an older version of
# this schema. init_db() adds any of these that don't already exist, so existing local
# databases don't need to be deleted every time a new column is introduced.
_TEAM_GAME_STATS_MIGRATIONS = {
    "epa_offense": "REAL",
    "epa_passing": "REAL",
    "epa_rushing": "REAL",
    "plays": "REAL",
    "sacks_suffered": "INTEGER NOT NULL DEFAULT 0",
    "pass_attempts": "REAL",
    "def_sacks": "INTEGER NOT NULL DEFAULT 0",
}


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, column_defs: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, ddl_type in column_defs.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _ensure_columns(conn, "team_game_stats", _TEAM_GAME_STATS_MIGRATIONS)
    conn.commit()


def upsert_team(conn, team: dict) -> None:
    conn.execute(
        "INSERT INTO teams (id, name, abbreviation) VALUES (:id, :name, :abbreviation) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, abbreviation=excluded.abbreviation",
        team,
    )


def upsert_game(conn, game: dict) -> None:
    conn.execute(
        """
        INSERT INTO games (id, season, week, home_team_id, away_team_id, kickoff_at,
                           venue_name, is_outdoor, lat, lon, status, home_score, away_score)
        VALUES (:id, :season, :week, :home_team_id, :away_team_id, :kickoff_at,
                :venue_name, :is_outdoor, :lat, :lon, :status, :home_score, :away_score)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, home_score=excluded.home_score,
            away_score=excluded.away_score, kickoff_at=excluded.kickoff_at,
            is_outdoor=excluded.is_outdoor
        """,
        game,
    )


def get_team_id_by_abbreviation(conn, abbreviation: str) -> str | None:
    row = conn.execute("SELECT id FROM teams WHERE abbreviation = ?", (abbreviation,)).fetchone()
    return row["id"] if row else None


def upsert_weather(conn, game_id: str, weather: dict, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO weather_forecasts (game_id, temp_f, wind_mph, precip_pct, fetched_at)
        VALUES (:game_id, :temp_f, :wind_mph, :precip_pct, :fetched_at)
        ON CONFLICT(game_id) DO UPDATE SET
            temp_f=excluded.temp_f, wind_mph=excluded.wind_mph,
            precip_pct=excluded.precip_pct, fetched_at=excluded.fetched_at
        """,
        {"game_id": game_id, "fetched_at": fetched_at, **weather},
    )


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def replace_team_injuries(conn, team_id: str, injuries: list[dict], fetched_at: str) -> None:
    conn.execute("DELETE FROM injuries WHERE team_id = ?", (team_id,))
    for injury in injuries:
        conn.execute(
            "INSERT INTO injuries (team_id, player_name, position, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (team_id, injury["player_name"], injury["position"], injury["status"], fetched_at),
        )


def upsert_team_game_stat(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO team_game_stats (team_id, season, week, turnovers, epa_offense, epa_passing, epa_rushing, plays,
                                      sacks_suffered, pass_attempts, def_sacks)
        VALUES (:team_id, :season, :week, :turnovers, :epa_offense, :epa_passing, :epa_rushing, :plays,
                :sacks_suffered, :pass_attempts, :def_sacks)
        ON CONFLICT(team_id, season, week) DO UPDATE SET
            turnovers=excluded.turnovers, epa_offense=excluded.epa_offense,
            epa_passing=excluded.epa_passing, epa_rushing=excluded.epa_rushing, plays=excluded.plays,
            sacks_suffered=excluded.sacks_suffered, pass_attempts=excluded.pass_attempts, def_sacks=excluded.def_sacks
        """,
        row,
    )


def upsert_team_rating(conn, team_id: str, elo_rating: float, updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO team_ratings (team_id, elo_rating, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            elo_rating=excluded.elo_rating, updated_at=excluded.updated_at
        """,
        (team_id, elo_rating, updated_at),
    )


def get_or_create_player(conn, name: str, created_at: str) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO players (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (name, created_at),
    )
    return conn.execute("SELECT * FROM players WHERE name = ?", (name,)).fetchone()


def get_player_by_id(conn, player_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()


def get_all_players(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM players ORDER BY name COLLATE NOCASE").fetchall()


def upsert_pick(conn, player_id: int, game_id: str, picked_team_id: str, created_at: str) -> None:
    conn.execute(
        """
        INSERT INTO picks (player_id, game_id, picked_team_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(player_id, game_id) DO UPDATE SET
            picked_team_id=excluded.picked_team_id, created_at=excluded.created_at
        """,
        (player_id, game_id, picked_team_id, created_at),
    )


def get_player_picks_for_games(conn, player_id: int, game_ids: list[str]) -> dict[str, str]:
    if not game_ids:
        return {}
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"SELECT game_id, picked_team_id FROM picks WHERE player_id = ? AND game_id IN ({placeholders})",
        (player_id, *game_ids),
    ).fetchall()
    return {row["game_id"]: row["picked_team_id"] for row in rows}


def get_decided_picks(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.player_id, pl.name AS player_name, g.season, g.week, p.picked_team_id,
               g.home_team_id, g.away_team_id, g.home_score, g.away_score
        FROM picks p
        JOIN games g ON g.id = p.game_id
        JOIN players pl ON pl.id = p.player_id
        WHERE g.status = 'final'
        """
    ).fetchall()
