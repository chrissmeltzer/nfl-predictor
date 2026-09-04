# SQLite → Postgres Migration — Design

## Purpose

Replace the SQLite backend (`nfl.db`, `sqlite3` module) with Postgres so
the app can run against a hosted database (Neon, in production) instead
of a file on a persistent volume. This is a faithful lift of the
existing schema and data-access behavior onto a new engine — it does
not change the `players`/`picks` identity model (that's the job of the
follow-up auth sub-project) or add any new features.

## Driver: psycopg3 + connection pool

Considered three approaches:

1. **psycopg3 + connection pool, minimal-diff port (chosen)** — swap
   `sqlite3` for `psycopg` in `app/db.py`, keep every function's shape
   and every call site in `app/main.py` unchanged. Smallest, lowest-risk
   change: same architecture, new engine underneath.
2. **SQLAlchemy Core** — adds a query-building abstraction and
   theoretical multi-DB portability. Not useful here since SQLite isn't
   being kept around; skipped per YAGNI.
3. **asyncpg + full async rewrite** — fastest and most "idiomatic
   FastAPI," but requires converting every sync route handler to
   `async def`. Far more blast radius than this app's traffic
   justifies.

## Schema port

Straight port of the existing `SCHEMA_SQL` in `app/db.py`:

- `INTEGER PRIMARY KEY AUTOINCREMENT` → `INTEGER GENERATED ALWAYS AS
  IDENTITY PRIMARY KEY`.
- Drop the SQLite-only `PRAGMA foreign_keys = ON` call in
  `get_connection` — Postgres always enforces FKs.
- `players.name TEXT NOT NULL COLLATE NOCASE UNIQUE` → plain `TEXT NOT
  NULL`, with a `CREATE UNIQUE INDEX ON players (lower(name))` and
  lookups (`get_or_create_player`, any name match) adjusted to compare
  on `lower(name)`. (This table is superseded in the auth sub-project;
  this is a faithful port, not a redesign.)
- `?` positional placeholders → `%s`; `:name` named placeholders →
  `%(name)s`. The `INSERT ... ON CONFLICT (...) DO UPDATE SET
  col=excluded.col` upsert syntax used throughout is valid Postgres
  as-is and needs no rewrite beyond the placeholder syntax.
- Row access: use `psycopg.rows.dict_row` as the connection's row
  factory so `row["col"]` keeps working unchanged everywhere it's used
  today (route handlers and Jinja templates both index rows by key).
- `_ensure_columns` (reads `PRAGMA table_info`) and
  `_ensure_unique_predictions_per_game` (reads `sqlite_master`) are
  SQLite-specific migration helpers for evolving an existing on-disk
  db. Since this migration starts the Postgres schema fresh from
  `SCHEMA_SQL` (see Data below), these are replaced by an equivalent
  one-time `CREATE TABLE IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT
  EXISTS` — no need to port the "detect and backfill missing columns on
  an old db" logic since there's no old Postgres db to backfill.

## Connection handling

Today, `get_db` (`app/main.py`) opens a fresh SQLite file and calls
`init_db()` on *every* request — cheap for a local file, wasteful and
slow against a networked Postgres server (and Neon caps concurrent
connections).

- A `psycopg_pool.ConnectionPool` is created once in a FastAPI
  `lifespan` hook at startup and stored on `app.state`.
- `init_db()` runs once at startup (right after the pool is created),
  not per-request.
- The `get_db` dependency borrows a connection from the pool for the
  duration of a request (`with pool.connection() as conn: yield conn`)
  instead of opening/closing a SQLite connection each time.
- `app/config.py`: `DATABASE_URL` (a Postgres connection string)
  replaces `DB_PATH`.

## Local dev

- `docker-compose.yml` (new) adds a `postgres:16` service with a named
  volume for persistence across restarts, exposing `5432`.
- A local `.env` sets `DATABASE_URL` to point at that container (e.g.
  `postgresql://postgres:postgres@localhost:5432/nfl_predictor`).
- Production `DATABASE_URL` will point at Neon — wired up in the
  deployment sub-project, not this one.

## Data

No data migration. The current `nfl.db` only holds re-fetchable
synced data (games/teams/stats come from ESPN/nflverse) plus no real
picks worth preserving (confirmed with the user — start fresh). The
Postgres database starts empty and is populated by the existing sync
job the same way `nfl.db` is today.

## Testing

Tests currently get free isolation via a throwaway SQLite file per test
(`tmp_path` fixture — see `tests/test_picks.py`,
`tests/test_db.py`). Some tests open a *second*, independent connection
mid-test and commit into it (e.g.
`_push_g1_kickoff_into_future`), relying on real cross-connection
commit visibility rather than a single shared transaction.

To preserve that behavior on Postgres:

- A new `tests/conftest.py` fixture connects to the local
  docker-compose Postgres instance, creates a fresh, uniquely-named
  database per test (e.g. `test_<uuid>`), runs `init_db()` against it,
  and yields a connection string. Multiple connections opened against
  that string during a test behave exactly like today (independent,
  real commits, visible to each other).
- Teardown drops the per-test database.
- `db.get_connection` is updated to accept a Postgres connection
  string instead of a `Path`, so `tests/test_db.py` and
  `tests/test_picks.py` only need their fixture setup changed, not
  their assertions.

## Out of scope

- The `players`/`picks` identity model itself (handled by the auth
  sub-project).
- Deployment / Neon wiring (handled by the deployment sub-project).
- Any new features — this is a backend swap only.
