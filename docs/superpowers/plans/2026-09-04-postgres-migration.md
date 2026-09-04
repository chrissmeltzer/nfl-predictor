# SQLite to Postgres Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SQLite backend (`nfl.db`, raw `sqlite3` module) with Postgres, as a faithful port of the existing schema and data-access behavior — no new features, no change to the `players`/`picks` identity model.

**Architecture:** `app/db.py` moves from `sqlite3` to `psycopg` (v3), using a `psycopg_pool.ConnectionPool` created once at FastAPI startup instead of opening a fresh connection per request. Local dev and tests run against a `postgres:16` container started via `docker-compose.yml`. Every test that currently gets isolation from a throwaway SQLite file (`tmp_path`) instead gets a freshly created, uniquely-named Postgres database (via a new `tests/conftest.py` fixture), dropped after the test.

**Tech Stack:** FastAPI, psycopg 3, psycopg_pool, Postgres 16 (local via Docker Compose), pytest.

**Spec:** [docs/superpowers/specs/2026-09-04-postgres-migration-design.md](../specs/2026-09-04-postgres-migration-design.md)

## Global Constraints

- No data migration — the Postgres database starts empty; games/teams/stats repopulate via the existing sync job, and there's no existing picks data worth preserving.
- `players`/`picks` schema and behavior are ported as-is; do not redesign them here (that's a separate sub-project).
- Row access must keep working as `row["col"]` everywhere it's used today (route handlers and Jinja templates both index rows by key) — use `psycopg.rows.dict_row`.
- Every existing test's assertions stay unchanged; only how each test acquires its database connection changes.

---

### Task 1: Local Postgres via Docker Compose + psycopg dependencies

**Files:**
- Create: `docker-compose.yml`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: a `db` service reachable at `postgresql://postgres:postgres@localhost:5432/nfl_predictor`, and the `psycopg` / `psycopg_pool` packages available for later tasks to import.

- [ ] **Step 1: Add `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: nfl_predictor
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

- [ ] **Step 2: Add Postgres driver dependencies**

Append to `requirements.txt`:

```
psycopg[binary]>=3.1
psycopg-pool>=3.2
```

- [ ] **Step 3: Start the container and verify it accepts connections**

```bash
docker compose up -d db
docker compose exec db pg_isready -U postgres
```

Expected: `/var/run/postgresql:5432 - accepting connections`

- [ ] **Step 4: Install the new Python dependencies**

```bash
pip install -r requirements.txt
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml requirements.txt
git commit -m "chore: add local Postgres via Docker Compose and psycopg dependencies"
```

---

### Task 2: Port `app/db.py` to psycopg3, add per-test-database fixtures, update `tests/test_db.py`

**Files:**
- Modify: `app/db.py` (full rewrite of the SQLite-specific parts)
- Create: `tests/conftest.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Consumes: the `db` service from Task 1 (`postgresql://postgres:postgres@localhost:5432/postgres` as the admin/maintenance connection).
- Produces:
  - `db.get_connection(dsn: str) -> psycopg.Connection` (was `get_connection(db_path: Path)`).
  - `db.init_db(conn) -> None` — unchanged signature.
  - Every other `db.py` function keeps its existing name and signature; only SQL syntax changes internally.
  - `tests/conftest.py` fixtures `pg_db_factory` (callable, returns a fresh DSN string each call) and `pg_url` (a single DSN string, `= pg_db_factory()`), usable by any test in the suite via pytest fixture injection.

- [ ] **Step 1: Write `tests/conftest.py`**

```python
import os
import uuid

import psycopg
import pytest

ADMIN_DSN = os.environ.get(
    "TEST_POSTGRES_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)


def _dsn_for(database: str) -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + f"/{database}"


@pytest.fixture
def pg_db_factory():
    """Yields a callable that creates a fresh, uniquely-named Postgres database on each
    call and returns its connection string. All databases created during the test are
    dropped afterward. Use this directly (instead of `pg_url`) when a single test needs
    more than one independent database.
    """
    created = []

    def factory() -> str:
        name = f"test_{uuid.uuid4().hex}"
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
            admin_conn.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        return _dsn_for(name)

    yield factory

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        for name in created:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pg_url(pg_db_factory):
    """A single fresh database's connection string — the common case of one database
    per test."""
    return pg_db_factory()
```

- [ ] **Step 2: Run it against nothing yet — just confirm imports resolve**

Run: `python3 -c "import tests.conftest"`
Expected: no output, exit code 0 (module imports cleanly; the fixtures themselves only run under pytest).

- [ ] **Step 3: Rewrite `app/db.py`**

Replace the entire file with:

```python
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        abbreviation TEXT NOT NULL
    )
    """,
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_games_season_week ON games(season, week)",
    """
    CREATE TABLE IF NOT EXISTS weather_forecasts (
        game_id TEXT PRIMARY KEY REFERENCES games(id),
        temp_f REAL,
        wind_mph REAL,
        precip_pct REAL,
        fetched_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS injuries (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        team_id TEXT NOT NULL REFERENCES teams(id),
        player_name TEXT NOT NULL,
        position TEXT,
        status TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_injuries_team ON injuries(team_id)",
    """
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        game_id TEXT NOT NULL REFERENCES games(id),
        predicted_home_score REAL NOT NULL,
        predicted_away_score REAL NOT NULL,
        factor_breakdown_json TEXT NOT NULL,
        weights_snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_game ON predictions(game_id)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_game_stats (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_game_stats_team ON team_game_stats(team_id, season, week)",
    """
    CREATE TABLE IF NOT EXISTS team_ratings (
        team_id TEXT PRIMARY KEY REFERENCES teams(id),
        elo_rating REAL NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_players_name_lower ON players (lower(name))",
    """
    CREATE TABLE IF NOT EXISTS picks (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id),
        game_id TEXT NOT NULL REFERENCES games(id),
        picked_team_id TEXT NOT NULL REFERENCES teams(id),
        created_at TEXT NOT NULL,
        UNIQUE(player_id, game_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_picks_player ON picks(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_picks_game ON picks(game_id)",
]


def get_connection(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


def init_db(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


def get_all_teams(conn) -> list[dict]:
    return conn.execute("SELECT * FROM teams ORDER BY name").fetchall()


def upsert_team(conn, team: dict) -> None:
    conn.execute(
        "INSERT INTO teams (id, name, abbreviation) VALUES (%(id)s, %(name)s, %(abbreviation)s) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, abbreviation=excluded.abbreviation",
        team,
    )


def upsert_game(conn, game: dict) -> None:
    conn.execute(
        """
        INSERT INTO games (id, season, week, home_team_id, away_team_id, kickoff_at,
                           venue_name, is_outdoor, lat, lon, status, home_score, away_score)
        VALUES (%(id)s, %(season)s, %(week)s, %(home_team_id)s, %(away_team_id)s, %(kickoff_at)s,
                %(venue_name)s, %(is_outdoor)s, %(lat)s, %(lon)s, %(status)s, %(home_score)s, %(away_score)s)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, home_score=excluded.home_score,
            away_score=excluded.away_score, kickoff_at=excluded.kickoff_at,
            is_outdoor=excluded.is_outdoor
        """,
        game,
    )


def get_team_id_by_abbreviation(conn, abbreviation: str) -> str | None:
    row = conn.execute("SELECT id FROM teams WHERE abbreviation = %s", (abbreviation,)).fetchone()
    return row["id"] if row else None


def upsert_weather(conn, game_id: str, weather: dict, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO weather_forecasts (game_id, temp_f, wind_mph, precip_pct, fetched_at)
        VALUES (%(game_id)s, %(temp_f)s, %(wind_mph)s, %(precip_pct)s, %(fetched_at)s)
        ON CONFLICT(game_id) DO UPDATE SET
            temp_f=excluded.temp_f, wind_mph=excluded.wind_mph,
            precip_pct=excluded.precip_pct, fetched_at=excluded.fetched_at
        """,
        {"game_id": game_id, "fetched_at": fetched_at, **weather},
    )


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def replace_team_injuries(conn, team_id: str, injuries: list[dict], fetched_at: str) -> None:
    conn.execute("DELETE FROM injuries WHERE team_id = %s", (team_id,))
    for injury in injuries:
        conn.execute(
            "INSERT INTO injuries (team_id, player_name, position, status, fetched_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (team_id, injury["player_name"], injury["position"], injury["status"], fetched_at),
        )


def upsert_team_game_stat(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO team_game_stats (team_id, season, week, turnovers, epa_offense, epa_passing, epa_rushing, plays,
                                      sacks_suffered, pass_attempts, def_sacks)
        VALUES (%(team_id)s, %(season)s, %(week)s, %(turnovers)s, %(epa_offense)s, %(epa_passing)s, %(epa_rushing)s, %(plays)s,
                %(sacks_suffered)s, %(pass_attempts)s, %(def_sacks)s)
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
        VALUES (%s, %s, %s)
        ON CONFLICT(team_id) DO UPDATE SET
            elo_rating=excluded.elo_rating, updated_at=excluded.updated_at
        """,
        (team_id, elo_rating, updated_at),
    )


def get_or_create_player(conn, name: str, created_at: str) -> dict:
    conn.execute(
        "INSERT INTO players (name, created_at) VALUES (%s, %s) ON CONFLICT (lower(name)) DO NOTHING",
        (name, created_at),
    )
    return conn.execute("SELECT * FROM players WHERE lower(name) = lower(%s)", (name,)).fetchone()


def get_player_by_id(conn, player_id: int) -> dict | None:
    return conn.execute("SELECT * FROM players WHERE id = %s", (player_id,)).fetchone()


def get_all_players(conn) -> list[dict]:
    return conn.execute("SELECT * FROM players ORDER BY lower(name)").fetchall()


def upsert_pick(conn, player_id: int, game_id: str, picked_team_id: str, created_at: str) -> None:
    conn.execute(
        """
        INSERT INTO picks (player_id, game_id, picked_team_id, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(player_id, game_id) DO UPDATE SET
            picked_team_id=excluded.picked_team_id, created_at=excluded.created_at
        """,
        (player_id, game_id, picked_team_id, created_at),
    )


def get_player_picks_for_games(conn, player_id: int, game_ids: list[str]) -> dict[str, str]:
    if not game_ids:
        return {}
    placeholders = ",".join("%s" for _ in game_ids)
    rows = conn.execute(
        f"SELECT game_id, picked_team_id FROM picks WHERE player_id = %s AND game_id IN ({placeholders})",
        (player_id, *game_ids),
    ).fetchall()
    return {row["game_id"]: row["picked_team_id"] for row in rows}


def get_decided_picks(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT p.player_id, pl.name AS player_name, g.season, g.week, p.picked_team_id,
               g.home_team_id, g.away_team_id, g.home_score, g.away_score
        FROM picks p
        JOIN games g ON g.id = p.game_id
        JOIN players pl ON pl.id = p.player_id
        WHERE g.status = 'final' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        """
    ).fetchall()
```

Notes on what changed from the SQLite version:
- `_ensure_columns` / `_TEAM_GAME_STATS_MIGRATIONS` / `_ensure_unique_predictions_per_game` are gone — those existed only to backfill columns/indexes onto an *existing* on-disk SQLite db from older schema versions. Since every test and every environment starts from an empty Postgres database, `CREATE TABLE IF NOT EXISTS` with the full column list already covers it.
- `players.name` dropped its inline `UNIQUE COLLATE NOCASE` in favor of a `lower(name)` unique index, with lookups (`get_or_create_player`) and ordering (`get_all_players`) matched to it via `lower(...)`.
- No `PRAGMA foreign_keys` — Postgres always enforces foreign keys.

- [ ] **Step 4: Rewrite `tests/test_db.py`**

```python
from app.db import get_connection, init_db


def test_init_db_creates_all_tables(pg_url):
    conn = get_connection(pg_url)
    init_db(conn)

    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"teams", "games", "weather_forecasts", "injuries", "predictions"} <= tables


def test_init_db_is_idempotent(pg_url):
    conn = get_connection(pg_url)
    init_db(conn)
    init_db(conn)  # should not raise
```

- [ ] **Step 5: Run the test file**

Run: `pytest tests/test_db.py -v`
Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: port db.py to psycopg3, add per-test Postgres database fixtures"
```

---

### Task 3: Wire `app/config.py` + `app/main.py` to a connection pool, update `tests/test_main.py`

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py:1-91` (imports, `_current_player`, `get_db`, `app` construction)
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `db.get_connection(dsn: str)`, `db.init_db(conn)` from Task 2.
- Produces: `app.state.db_pool` (a live `psycopg_pool.ConnectionPool`), `main.get_db` — a FastAPI dependency taking `request: Request` and yielding a pooled connection (same external behavior as before: routes still do `conn=Depends(get_db)`).

- [ ] **Step 1: Update `app/config.py`**

```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nfl_predictor"
)
WEIGHTS_PATH = BASE_DIR / "weights.yaml"
STALENESS_HOURS = 6
SYNC_SEASONS_BACK = 2
```

- [ ] **Step 2: Update `app/main.py` imports and app construction**

Replace:

```python
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import betting, db, predict, stats, sync
from app.config import DB_PATH, STALENESS_HOURS, WEIGHTS_PATH
from app.reference import DEFAULT_TEAM_COLOR, TEAM_COLORS, injury_impact, parse_kickoff
from app.sources import espn

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
```

with:

```python
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import betting, db, predict, stats, sync
from app.config import DATABASE_URL, STALENESS_HOURS, WEIGHTS_PATH
from app.reference import DEFAULT_TEAM_COLOR, TEAM_COLORS, injury_impact, parse_kickoff
from app.sources import espn


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = ConnectionPool(DATABASE_URL, open=True, kwargs={"row_factory": dict_row})
    with pool.connection() as conn:
        db.init_db(conn)
    app.state.db_pool = pool
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
```

- [ ] **Step 3: Update `_current_player` and `get_db`**

Replace:

```python
def _current_player(request: Request, conn) -> sqlite3.Row | None:
    raw = request.cookies.get(PICKER_COOKIE)
    if not raw or not raw.isdecimal() or len(raw) > 18:
        return None
    return db.get_player_by_id(conn, int(raw))


def get_db():
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()
```

with:

```python
def _current_player(request: Request, conn) -> dict | None:
    raw = request.cookies.get(PICKER_COOKIE)
    if not raw or not raw.isdecimal() or len(raw) > 18:
        return None
    return db.get_player_by_id(conn, int(raw))


def get_db(request: Request):
    with request.app.state.db_pool.connection() as conn:
        yield conn
```

- [ ] **Step 4: Update `tests/test_main.py`'s `make_test_client` and every test's fixture parameter**

Replace the helper:

```python
def make_test_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })
    conn.execute(
        "INSERT INTO weather_forecasts (game_id, temp_f, wind_mph, precip_pct, fetched_at) VALUES (?, ?, ?, ?, ?)",
        ("g1", 60, 5, 10, datetime.now(timezone.utc).isoformat()),
    )
    # Mark the DB as freshly synced so route tests don't trigger a real sync_all.
    db.set_meta(conn, "last_synced_at", datetime.now(timezone.utc).isoformat())
    conn.commit()
    conn.close()

    def override_get_db():
        c = db.get_connection(db_path)
        try:
            yield c
        finally:
            c.close()

    monkeypatch.setattr(main.espn, "fetch_current_week", lambda client: (2026, 1))
    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)
```

with:

```python
def make_test_client(pg_url, monkeypatch):
    conn = db.get_connection(pg_url)
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })
    conn.execute(
        "INSERT INTO weather_forecasts (game_id, temp_f, wind_mph, precip_pct, fetched_at) VALUES (%s, %s, %s, %s, %s)",
        ("g1", 60, 5, 10, datetime.now(timezone.utc).isoformat()),
    )
    # Mark the DB as freshly synced so route tests don't trigger a real sync_all.
    db.set_meta(conn, "last_synced_at", datetime.now(timezone.utc).isoformat())
    conn.commit()
    conn.close()

    def override_get_db():
        c = db.get_connection(pg_url)
        try:
            yield c
        finally:
            c.close()

    monkeypatch.setattr(main.espn, "fetch_current_week", lambda client: (2026, 1))
    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)
```

Then, in every test function in `tests/test_main.py`, rename the `tmp_path` parameter to `pg_url` and change every remaining `db.get_connection(tmp_path / "test.db")` (and the two `tmp_path / "stale.db"` occurrences in the `_is_stale` tests) to `db.get_connection(pg_url)`. Every test function in this file uses `tmp_path` exclusively for the database (there's no unrelated filesystem use of `tmp_path` in this file), so this is a blanket rename — every occurrence of the bare word `tmp_path` in the file becomes `pg_url`. The full list of function names to touch:

`test_schedule_page_lists_games_for_current_week`, `test_game_detail_page_shows_breakdown_and_saves_prediction`, `test_schedule_page_shows_upset_alert_badge_when_flagged`, `test_schedule_page_hides_upset_alert_badge_when_not_flagged`, `test_game_detail_final_game_shows_nailed_it_for_close_prediction`, `test_game_detail_final_game_shows_missed_by_for_large_error`, `test_game_detail_final_game_without_saved_prediction_has_no_reveal_badge`, `test_game_detail_upcoming_game_shows_betting_angles`, `test_game_detail_final_game_hides_betting_angles`, `test_team_detail_shows_recent_pick_accuracy`, `test_team_detail_hides_accuracy_stat_with_no_final_predicted_games`, `test_team_detail_shows_remaining_strength_of_schedule`, `test_team_detail_hides_remaining_sos_when_opponent_data_unavailable`, `test_rankings_page_lists_teams_sorted_by_elo_desc`, `test_rankings_page_defaults_unrated_teams_to_base_rating`, `test_accuracy_page_loads_with_no_predictions_yet`, `test_game_detail_404_for_nonexistent_game`, `test_game_detail_does_not_save_prediction_for_final_game`, `test_accuracy_dedupes_multiple_predictions_for_same_game`, `test_accuracy_computes_mean_errors_correctly`, `test_bets_page_shows_safe_bet_for_lopsided_high_confidence_game`, `test_bets_page_hides_close_game`, `test_bets_page_hides_low_confidence_game_below_blowout_threshold`, `test_bets_page_shows_low_confidence_blowout_game`, `test_is_stale_true_when_no_meta_row`, `test_is_stale_false_when_recently_synced`, `test_is_stale_true_when_synced_long_ago`.

Also fix the raw-SQL `?` placeholders used directly in a few test bodies (not through `db.py` helpers) — change every `?` to `%s` in this file's inline `conn.execute(...)` calls (e.g. the `INSERT INTO predictions (...)` calls used in several `test_game_detail_*` and `test_accuracy_*` tests).

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_main.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/main.py tests/test_main.py
git commit -m "feat: wire app to a psycopg connection pool instead of per-request SQLite"
```

---

### Task 4: Migrate `tests/test_stats.py`, `tests/test_predict.py`, `tests/test_sync.py` to `pg_url`

**Files:**
- Modify: `tests/test_stats.py`
- Modify: `tests/test_predict.py`
- Modify: `tests/test_sync.py`

**Interfaces:**
- Consumes: `pg_url` fixture from Task 2's `tests/conftest.py`; `db.get_connection(dsn: str)` from Task 2.

Each of these three files defines its own local `make_conn(tmp_path)` helper that does `db.get_connection(tmp_path / "test.db")`. The fix is identical in each: change the helper to take a DSN directly, and rename every test function's `tmp_path` parameter to `pg_url` (with one exception, noted below).

- [ ] **Step 1: `tests/test_stats.py` — update the helper**

Replace:

```python
def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
```

with:

```python
def make_conn(dsn):
    conn = db.get_connection(dsn)
```

Then rename the `tmp_path` parameter to `pg_url` (and every `make_conn(tmp_path)` call to `make_conn(pg_url)`) in all 21 test functions: `test_recent_scoring_stats_averages_last_n_games`, `test_recent_scoring_stats_respects_window`, `test_recent_scoring_stats_window_zero_returns_no_games`, `test_home_away_split`, `test_head_to_head_only_counts_matchups_between_the_two_teams`, `test_rest_days_computed_from_previous_game`, `test_recent_scoring_stats_ignores_games_after_cutoff`, `test_recency_weighted_scoring_ignores_games_after_cutoff`, `test_home_away_split_ignores_games_after_cutoff`, `test_head_to_head_ignores_meetings_after_cutoff`, `test_current_season_sample_size_ignores_games_after_cutoff`, `test_pass_rush_form_computes_protection_and_pressure_rates`, `test_pass_rush_form_returns_none_when_no_data`, `test_pass_rush_form_ignores_stats_after_cutoff`, `test_turnover_form_ignores_stats_after_cutoff`, `test_turnovers_forced_ignores_stats_after_cutoff`, `test_epa_form_ignores_stats_after_cutoff`, `test_pace_form_ignores_stats_after_cutoff`, `test_strength_of_schedule_ignores_opponent_games_after_cutoff`, `test_remaining_strength_of_schedule_ranks_easiest_first`, `test_remaining_strength_of_schedule_excludes_team_with_no_opponent_data`.

Example of the resulting shape (`test_recent_scoring_stats_averages_last_n_games`, unchanged body below the signature):

```python
def test_recent_scoring_stats_averages_last_n_games(pg_url):
    conn = make_conn(pg_url)
    ...
```

- [ ] **Step 2: Run `test_stats.py`**

Run: `pytest tests/test_stats.py -v`
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_stats.py
git commit -m "test: migrate test_stats.py to per-test Postgres database"
```

- [ ] **Step 4: `tests/test_predict.py` — update the helper**

Replace:

```python
def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
```

with:

```python
def make_conn(dsn):
    conn = db.get_connection(dsn)
```

Rename the `tmp_path` parameter to `pg_url` (and `make_conn(tmp_path)` calls to `make_conn(pg_url)`) in every function **except** `test_load_weights_reads_yaml`, which uses `tmp_path` to write a real `weights.yaml` file to disk and has nothing to do with the database — leave it exactly as-is. Functions to rename: `test_predict_game_baseline_uses_recent_scoring_and_opponent_defense`, `test_predict_game_skips_weather_when_no_forecast_row`, `test_predict_game_applies_negative_injury_adjustment`, `test_recent_scoring_trend_weight_scales_baseline_toward_league_average`, `test_pass_protection_adjustment_penalizes_weak_line_against_strong_pass_rush`, `test_pass_protection_adjustment_is_zero_without_stats_data`, `test_confidence_reflects_current_season_sample_not_lifetime_games`, `test_confidence_rises_as_current_season_sample_grows`, `test_get_latest_prediction_returns_most_recent_row`, `test_get_latest_prediction_returns_none_when_no_prediction_saved`, `test_upset_alert_flags_when_model_favorite_differs_from_elo_favorite`, `test_upset_alert_false_when_model_agrees_with_elo`, `test_predict_game_backtest_ignores_finalized_games_that_happened_later`, `test_save_prediction_persists_row`.

- [ ] **Step 5: Run `test_predict.py`**

Run: `pytest tests/test_predict.py -v`
Expected: all tests PASS, including `test_load_weights_reads_yaml` (untouched, still uses `tmp_path`).

- [ ] **Step 6: Commit**

```bash
git add tests/test_predict.py
git commit -m "test: migrate test_predict.py to per-test Postgres database"
```

- [ ] **Step 7: `tests/test_sync.py` — update the helper**

Replace:

```python
def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    return conn
```

with:

```python
def make_conn(dsn):
    conn = db.get_connection(dsn)
    db.init_db(conn)
    return conn
```

Rename the `tmp_path` parameter to `pg_url` (and `make_conn(tmp_path)` calls to `make_conn(pg_url)`) in all 12 test functions: `test_sync_teams_inserts_rows`, `test_sync_historical_resolves_team_ids_and_stadium_coords`, `test_sync_historical_max_season_excludes_rows_at_or_above_bound`, `test_sync_historical_skips_unknown_team`, `test_sync_weather_for_upcoming_only_fetches_outdoor_scheduled_games`, `test_sync_weather_for_upcoming_continues_past_one_failure`, `test_sync_weather_for_upcoming_continues_past_missing_forecast_data`, `test_sync_predictions_saves_a_row_for_each_scheduled_game`, `test_sync_predictions_skips_final_games`, `test_sync_injuries_for_upcoming_continues_past_one_failure`, `test_sync_injuries_for_upcoming_clears_team_with_no_current_injuries`, `test_sync_injuries_for_upcoming_stops_once_all_teams_seen`. Several of these also take `monkeypatch` — keep it, e.g. `def test_sync_teams_inserts_rows(pg_url, monkeypatch):`.

- [ ] **Step 8: Run `test_sync.py`**

Run: `pytest tests/test_sync.py -v`
Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add tests/test_sync.py
git commit -m "test: migrate test_sync.py to per-test Postgres database"
```

---

### Task 5: Migrate `tests/test_elo.py` to `pg_db_factory`

**Files:**
- Modify: `tests/test_elo.py`

**Interfaces:**
- Consumes: `pg_db_factory` fixture from Task 2's `tests/conftest.py` (this file needs two independent databases in one test, unlike the others).

This file's single test creates two *separate* SQLite files (`week1_only.db`, `both_weeks.db`) to represent two independent database states. The Postgres equivalent is two independent databases from `pg_db_factory`.

- [ ] **Step 1: Update the helper and test**

Replace:

```python
def make_conn(db_path):
    conn = db.get_connection(db_path)
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    conn.commit()
    return conn


def test_recompute_ratings_ignores_games_after_cutoff(tmp_path):
    conn_only_week1 = make_conn(tmp_path / "week1_only.db")
    seed_game(conn_only_week1, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    conn_only_week1.commit()
    expected_after_week1 = elo.recompute_ratings(conn_only_week1)["A"]

    conn = make_conn(tmp_path / "both_weeks.db")
    seed_game(conn, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    # A big blowout in week 2 should not move a rating computed as of before week 2.
    seed_game(conn, "g2", 2026, 2, "A", "B", 50, 0, "2026-09-08T00:00Z")
    conn.commit()

    ratings_before_week2 = elo.recompute_ratings(conn, before=(2026, 2))
    ratings_all = elo.recompute_ratings(conn)

    assert ratings_before_week2["A"] == expected_after_week1
    assert ratings_before_week2["A"] != ratings_all["A"]
```

with:

```python
def make_conn(dsn):
    conn = db.get_connection(dsn)
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    conn.commit()
    return conn


def test_recompute_ratings_ignores_games_after_cutoff(pg_db_factory):
    conn_only_week1 = make_conn(pg_db_factory())
    seed_game(conn_only_week1, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    conn_only_week1.commit()
    expected_after_week1 = elo.recompute_ratings(conn_only_week1)["A"]

    conn = make_conn(pg_db_factory())
    seed_game(conn, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    # A big blowout in week 2 should not move a rating computed as of before week 2.
    seed_game(conn, "g2", 2026, 2, "A", "B", 50, 0, "2026-09-08T00:00Z")
    conn.commit()

    ratings_before_week2 = elo.recompute_ratings(conn, before=(2026, 2))
    ratings_all = elo.recompute_ratings(conn)

    assert ratings_before_week2["A"] == expected_after_week1
    assert ratings_before_week2["A"] != ratings_all["A"]
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_elo.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_elo.py
git commit -m "test: migrate test_elo.py to per-test Postgres databases"
```

---

### Task 6: Migrate `tests/test_picks.py` to `pg_url`

**Files:**
- Modify: `tests/test_picks.py`

**Interfaces:**
- Consumes: `pg_url` fixture from Task 2; `make_test_client(pg_url, monkeypatch)` from Task 3 (this file imports it: `from tests.test_main import make_test_client`).

Every test function in this file uses `tmp_path` exclusively for the database, so this is a blanket rename like Task 4, plus one query fix.

- [ ] **Step 1: Update `_push_g1_kickoff_into_future`**

Replace:

```python
def _push_g1_kickoff_into_future(tmp_path):
    """g1 is seeded by make_test_client with a hardcoded kickoff_at that will eventually
    be in the past (see tests/test_main.py). Tests that need g1 to still be pickable
    (i.e. not yet locked) push its kickoff relative to wall-clock time instead of relying
    on the fixture's hardcoded date.
    """
    conn = db.get_connection(tmp_path / "test.db")
    conn.execute(
        "UPDATE games SET kickoff_at = ? WHERE id = 'g1'",
        ((datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z"),),
    )
    conn.commit()
    conn.close()
```

with:

```python
def _push_g1_kickoff_into_future(pg_url):
    """g1 is seeded by make_test_client with a hardcoded kickoff_at that will eventually
    be in the past (see tests/test_main.py). Tests that need g1 to still be pickable
    (i.e. not yet locked) push its kickoff relative to wall-clock time instead of relying
    on the fixture's hardcoded date.
    """
    conn = db.get_connection(pg_url)
    conn.execute(
        "UPDATE games SET kickoff_at = %s WHERE id = 'g1'",
        ((datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z"),),
    )
    conn.commit()
    conn.close()
```

Every call site `_push_g1_kickoff_into_future(tmp_path)` becomes `_push_g1_kickoff_into_future(pg_url)`.

- [ ] **Step 2: Update `test_init_db_creates_players_and_picks_tables`**

Replace:

```python
def test_init_db_creates_players_and_picks_tables(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"players", "picks"} <= tables
```

with:

```python
def test_init_db_creates_players_and_picks_tables(pg_url):
    conn = db.get_connection(pg_url)
    db.init_db(conn)
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"players", "picks"} <= tables
```

- [ ] **Step 3: Rename `tmp_path` to `pg_url` in every remaining function**

For every other test function in the file, rename the `tmp_path` parameter to `pg_url` and change every `db.get_connection(tmp_path / "test.db")` to `db.get_connection(pg_url)`, and every `make_test_client(tmp_path, monkeypatch)` to `make_test_client(pg_url, monkeypatch)`. The functions: `test_get_or_create_player_is_case_insensitive_and_idempotent`, `test_upsert_pick_overwrites_existing_pick_for_same_game`, `test_get_player_picks_for_games_returns_empty_dict_for_no_games`, `test_get_decided_picks_only_returns_final_games`, `test_join_get_renders_name_form`, `test_join_post_creates_player_and_sets_cookie`, `test_join_post_reuses_existing_player_case_insensitively`, `test_join_post_rejects_empty_name`, `test_base_header_shows_picking_as_name_after_join`, `test_submit_pick_without_player_redirects_to_join`, `test_submit_pick_rejected_after_kickoff`, `test_pickem_page_shows_standings_and_weekly_breakdown`, `test_pickem_page_treats_tie_game_as_push`, `test_pickem_page_shows_joined_player_with_no_decided_picks`, `test_submit_pick_rejects_team_not_in_game`, `test_pickem_page_does_not_crash_on_final_game_with_null_scores`, `test_schedule_page_shows_pick_mode_toggle_and_pick_buttons`, `test_submit_pick_from_index_redirects_back_to_index`, `test_submit_pick_from_index_persists_and_highlights_active_pick`, `test_submit_pick_from_index_rejected_after_kickoff_redirects_with_error`, `test_index_shows_correct_pick_badge_for_final_game`, `test_index_shows_push_badge_for_tied_final_game`, `test_current_player_ignores_tampered_cookie_instead_of_crashing`.

- [ ] **Step 4: Run the full test suite**

Run: `pytest -v`
Expected: every test across the whole suite PASSES (this is the first point every migrated file runs together).

- [ ] **Step 5: Commit**

```bash
git add tests/test_picks.py
git commit -m "test: migrate test_picks.py to per-test Postgres database"
```

---

### Task 7: Update scripts, Dockerfile, and deployment docs for `DATABASE_URL`

**Files:**
- Modify: `scripts/seed_sample_data.py`
- Modify: `scripts/calibrate_weights.py`
- Modify: `Dockerfile:9-10`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `app.config.DATABASE_URL`, `db.get_connection(dsn: str)` from Tasks 2-3.

- [ ] **Step 1: Update `scripts/seed_sample_data.py`**

Replace:

```python
from app import db
from app.config import DB_PATH

conn = db.get_connection(DB_PATH)
db.init_db(conn)
```

with:

```python
from app import db
from app.config import DATABASE_URL

conn = db.get_connection(DATABASE_URL)
db.init_db(conn)
```

And at the bottom, replace:

```python
print("Seeded sample data into", DB_PATH)
```

with:

```python
print("Seeded sample data into", DATABASE_URL)
```

- [ ] **Step 2: Update `scripts/calibrate_weights.py`**

Change `from app.config import DB_PATH, WEIGHTS_PATH` to `from app.config import DATABASE_URL, WEIGHTS_PATH`, and `conn = db.get_connection(DB_PATH)` to `conn = db.get_connection(DATABASE_URL)`.

- [ ] **Step 3: Update `Dockerfile`**

Replace:

```dockerfile
# nfl.db should live on a mounted persistent volume in production so synced data
# survives redeploys -- see docs/DEPLOYMENT.md.
EXPOSE 8000
```

with:

```dockerfile
# The app connects to Postgres via the DATABASE_URL environment variable at runtime --
# see docs/DEPLOYMENT.md.
EXPOSE 8000
```

- [ ] **Step 4: Update `docs/DEPLOYMENT.md`**

Replace the "This is a FastAPI + SQLite application" opening line with "This is a FastAPI + Postgres application", and replace the whole "Persistent storage" section:

```markdown
## Persistent storage

`nfl.db` (see `app/config.py`) must live on a persistent volume/disk in production. Without
one, every redeploy wipes synced schedule, stats, and Elo rating data, and the app has to
resync from scratch. Most platforms (Render, Fly.io) offer a small free persistent disk you
can mount at the database's configured path.
```

with:

```markdown
## Database

The app connects to Postgres via the `DATABASE_URL` environment variable (see
`app/config.py`). For local development, `docker compose up -d db` starts a Postgres
container matching the default `DATABASE_URL`. In production, `DATABASE_URL` should point
at a hosted Postgres instance rather than a container tied to the app's own lifecycle, so
data survives redeploys.
```

Also update the `docker run` example, replacing:

```bash
docker run -p 8000:8000 -v $(pwd)/data:/app/data nfl-predictor
```

with:

```bash
docker run -p 8000:8000 -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5432/nfl_predictor nfl-predictor
```

- [ ] **Step 5: Update `.gitignore`**

Remove the `nfl.db` line (no longer produced by anything).

- [ ] **Step 6: Run the full test suite one more time**

Run: `pytest -v`
Expected: every test PASSES.

- [ ] **Step 7: Manual smoke test**

```bash
docker compose up -d db
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nfl_predictor python3 scripts/seed_sample_data.py
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nfl_predictor uvicorn app.main:app --reload
```

Visit `http://localhost:8000/` in a browser and confirm the seeded sample games render.

- [ ] **Step 8: Commit**

```bash
git add scripts/seed_sample_data.py scripts/calibrate_weights.py Dockerfile docs/DEPLOYMENT.md .gitignore
git commit -m "docs: update deployment notes and scripts for Postgres"
```
