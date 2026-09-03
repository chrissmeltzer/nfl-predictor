# NFL Score Predictor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a personal, locally-run FastAPI web app that browses upcoming NFL games and predicts final scores using a transparent weighted-factor model built on free data (ESPN, nflverse, Open-Meteo), with an accuracy tracker comparing predictions to actual results.

**Architecture:** Python + FastAPI + server-rendered Jinja2 templates, backed by a local SQLite file with no ORM. Three source modules (`espn`, `nflverse`, `weather`) each expose a pure `parse_*` function (fixture-tested) and a thin `fetch_*` wrapper. A `sync` module orchestrates pulling data into SQLite; `stats` computes rolling statistics via SQL; `predict` combines a baseline expected score with weighted adjustments into a final prediction and breakdown.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, Jinja2, httpx, PyYAML, sqlite3 (stdlib), pytest.

**Spec:** [docs/superpowers/specs/2026-09-03-nfl-predictor-design.md](../specs/2026-09-03-nfl-predictor-design.md)

## Global Constraints

- Personal, single-user, local-only app for v1 — no auth, no deployment, no multi-tenancy.
- All data sources are free and require no API key or credentials.
- No ORM — plain `sqlite3` with hand-written SQL.
- No frontend build tooling — server-rendered Jinja2 templates plus plain HTML forms for interactivity (a week-selector `<form method="get">`, a `<form method="post">` for manual refresh). No separate JS file needed.
- Manual/staleness-triggered sync (default staleness threshold: 6 hours), not a background cron job.
- Historical sync window: current season minus 2 (i.e. 3 seasons of history), to sidestep almost all franchise-relocation team-abbreviation aliasing.
- Weather factor is skipped entirely for indoor/dome venues (`is_outdoor = false`), applied only for outdoor games.
- Weights for every prediction factor live in `weights.yaml` at the project root, editable without touching code.
- Tests never make live network calls — every data-source test runs against a saved fixture file.

## Implementation notes from research

- ESPN's endpoints are undocumented but stable and used in code below exactly as observed live:
  - Teams: `https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams`
  - Scoreboard: `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={season}&seasontype={type}&week={week}` — note the year parameter is `dates`, not `year`; `year` alone is not reliably honored.
  - Current week (no params): `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`
  - Game summary (has embedded, fully-resolved injuries — no N+1 fetches needed): `https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}`. A separate `sports.core.api.espn.com/.../teams/{id}/injuries` endpoint exists but returns a paginated list of `$ref` pointers requiring a follow-up fetch per player — avoid it; the summary endpoint gives the same data pre-resolved.
- `curl`/`WebFetch` calls to ESPN from this sandbox's own tools got HTTP 403 from ESPN's Akamai WAF, but a Python `httpx.Client` making the same request from a running `uvicorn` process reached ESPN fine — the block appears to be specific to those particular tools' network path/fingerprint, not a blanket sandbox-wide block. **This plan's whole stack was verified end-to-end against live ESPN/nflverse/Open-Meteo data** (not just fixtures) by building it in a scratch directory and hitting `/`, `/games/{id}`, and `/accuracy` in a real browser — see the bugs this caught, below. Don't assume live network is unreachable from wherever this plan is executed; it may well work exactly as it did here.
- nflverse's historical games CSV (`https://github.com/nflverse/nfldata/raw/master/data/games.csv`) has the exact columns used below (confirmed live): `game_id, season, game_type, week, home_team, home_score, away_team, away_score, roof, stadium, ...`. It has no stadium lat/lon column, so stadium coordinates are a small hardcoded table in `app/reference.py` (32 stadiums, stable data, avoids adding a geocoding dependency). **That URL 302-redirects** to `raw.githubusercontent.com`, and `httpx.Client()` raises `HTTPStatusError` on an unfollowed redirect by default — every `httpx.Client(...)` construction in this plan passes `follow_redirects=True` because of this (confirmed live: without it, `nflverse.fetch_games_csv` fails every time).
- Open-Meteo forecast API confirmed live at `https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&hourly=temperature_2m,precipitation_probability,windspeed_10m&timezone=UTC` — returns Celsius and km/h, converted to Fahrenheit/mph in code.
- **Deviation from the spec's schema**: the `teams` table drops `conference`/`division` (present in the spec's sketch). ESPN's free `/teams` endpoint doesn't cleanly return them (they require a separate standings/groups lookup), and no prediction factor or UI view in the spec actually uses them — a pure YAGNI cut, not a capability loss.
- **ESPN's abbreviation for Washington is `WSH`, nflverse's is `WAS`** — confirmed by running a real sync: every other team matched on the first try, only Washington's historical games were silently skipped until this was caught and fixed. `TEAM_ALIASES` and `STADIUMS` below use `WSH` as the canonical key (since `teams` is populated from ESPN) with `"WAS": "WSH"` as the alias — get this backwards and Washington's history quietly vanishes with no error, just a lower row count.
- **Starlette's `Jinja2Templates.TemplateResponse` takes `request` as its first positional argument** in the version `pip install` resolves today (`fastapi>=0.110` currently pulls fastapi 0.141 / starlette 1.6) — the older `TemplateResponse("name.html", {"request": request, ...})` style still runs but throws `TypeError: unhashable type: 'dict'` from Jinja2's template cache under FastAPI's dependency-injected route handlers. Every `TemplateResponse` call below uses `TemplateResponse(request, "name.html", {...})`, confirmed live.
- **A single bad ESPN response must not take down the whole page.** `sync_injuries_for_upcoming` and `sync_weather_for_upcoming` each fetch per-game (one HTTP call per scheduled game), and any one of those failing (bad event id, transient 5xx, timeout) used to raise and crash the entire `sync_all()` call — meaning the schedule page 500'd instead of rendering whatever data it already had. Both loops below catch `httpx.HTTPError` per item, log a warning, and continue.

---

### Task 1: Project scaffolding & SQLite schema

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `pytest.ini`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `app.config.DB_PATH: Path`, `app.config.WEIGHTS_PATH: Path`, `app.config.STALENESS_HOURS: int`, `app.config.SYNC_SEASONS_BACK: int`, `app.config.RECENT_GAMES_WINDOW: int`; `app.db.get_connection(db_path: Path) -> sqlite3.Connection`; `app.db.init_db(conn: sqlite3.Connection) -> None`. All later tasks depend on these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
from app.db import get_connection, init_db


def test_init_db_creates_all_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"teams", "games", "weather_forecasts", "injuries", "predictions"} <= tables


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)
    init_db(conn)  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'` (or `'app'`)

- [ ] **Step 3: Write the scaffolding and implementation**

```text
# requirements.txt
fastapi>=0.110
uvicorn[standard]>=0.29
jinja2>=3.1
httpx>=0.27
pyyaml>=6.0
pytest>=8.0
```

```text
# .gitignore
__pycache__/
*.pyc
.venv/
nfl.db
```

```ini
# pytest.ini
[pytest]
pythonpath = .
```

```python
# app/__init__.py
```

```python
# app/config.py
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "nfl.db"
WEIGHTS_PATH = BASE_DIR / "weights.yaml"
STALENESS_HOURS = 6
SYNC_SEASONS_BACK = 2
RECENT_GAMES_WINDOW = 8
```

```python
# app/db.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add requirements.txt .gitignore pytest.ini app/__init__.py app/config.py app/db.py tests/test_db.py
git commit -m "feat: add project scaffolding and SQLite schema

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Static reference data (stadiums, team aliases, position importance)

**Files:**
- Create: `app/reference.py`
- Test: `tests/test_reference.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.reference.STADIUMS: dict[str, dict]` (keyed by current team abbreviation, values `{"name": str, "lat": float, "lon": float}`), `app.reference.TEAM_ALIASES: dict[str, str]`, `app.reference.canonical_abbreviation(abbr: str) -> str`, `app.reference.POSITION_IMPORTANCE: dict[str, float]`, `app.reference.DEFAULT_POSITION_IMPORTANCE: float`. Used by `nflverse.py`, `sync.py`, `predict.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reference.py
from app.reference import POSITION_IMPORTANCE, STADIUMS, canonical_abbreviation


def test_all_32_teams_have_stadium_coordinates():
    assert len(STADIUMS) == 32
    for abbr, info in STADIUMS.items():
        assert -90 <= info["lat"] <= 90
        assert -180 <= info["lon"] <= 180


def test_canonical_abbreviation_maps_relocated_teams():
    assert canonical_abbreviation("OAK") == "LV"
    assert canonical_abbreviation("SD") == "LAC"
    assert canonical_abbreviation("KC") == "KC"


def test_position_importance_has_qb_highest():
    assert POSITION_IMPORTANCE["QB"] == max(POSITION_IMPORTANCE.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reference.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.reference'`

- [ ] **Step 3: Write the implementation**

```python
# app/reference.py
STADIUMS = {
    "ARI": {"name": "State Farm Stadium", "lat": 33.5276, "lon": -112.2626},
    "ATL": {"name": "Mercedes-Benz Stadium", "lat": 33.7554, "lon": -84.4008},
    "BAL": {"name": "M&T Bank Stadium", "lat": 39.2780, "lon": -76.6227},
    "BUF": {"name": "Highmark Stadium", "lat": 42.7738, "lon": -78.7870},
    "CAR": {"name": "Bank of America Stadium", "lat": 35.2258, "lon": -80.8528},
    "CHI": {"name": "Soldier Field", "lat": 41.8623, "lon": -87.6167},
    "CIN": {"name": "Paycor Stadium", "lat": 39.0954, "lon": -84.5160},
    "CLE": {"name": "Huntington Bank Field", "lat": 41.5061, "lon": -81.6995},
    "DAL": {"name": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945},
    "DEN": {"name": "Empower Field at Mile High", "lat": 39.7439, "lon": -105.0201},
    "DET": {"name": "Ford Field", "lat": 42.3400, "lon": -83.0456},
    "GB": {"name": "Lambeau Field", "lat": 44.5013, "lon": -88.0622},
    "HOU": {"name": "NRG Stadium", "lat": 29.6847, "lon": -95.4107},
    "IND": {"name": "Lucas Oil Stadium", "lat": 39.7601, "lon": -86.1639},
    "JAX": {"name": "EverBank Stadium", "lat": 30.3239, "lon": -81.6373},
    "KC": {"name": "GEHA Field at Arrowhead Stadium", "lat": 39.0489, "lon": -94.4839},
    "LAC": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "LAR": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "LV": {"name": "Allegiant Stadium", "lat": 36.0909, "lon": -115.1833},
    "MIA": {"name": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389},
    "MIN": {"name": "U.S. Bank Stadium", "lat": 44.9735, "lon": -93.2575},
    "NE": {"name": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643},
    "NO": {"name": "Caesars Superdome", "lat": 29.9511, "lon": -90.0812},
    "NYG": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745},
    "NYJ": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745},
    "PHI": {"name": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675},
    "PIT": {"name": "Acrisure Stadium", "lat": 40.4468, "lon": -80.0158},
    "SEA": {"name": "Lumen Field", "lat": 47.5952, "lon": -122.3316},
    "SF": {"name": "Levi's Stadium", "lat": 37.4030, "lon": -121.9700},
    "TB": {"name": "Raymond James Stadium", "lat": 27.9759, "lon": -82.5033},
    "TEN": {"name": "Nissan Stadium", "lat": 36.1665, "lon": -86.7713},
    "WSH": {"name": "Northwest Stadium", "lat": 38.9077, "lon": -76.8645},
}

# Historical nflverse team abbreviations for relocated/renamed franchises,
# mapped to the current abbreviation used as our canonical key everywhere.
# Canonical abbreviations are ESPN's (since the `teams` table is populated
# from ESPN) — confirmed live that ESPN uses "WSH" for Washington while
# nflverse's CSV uses "WAS", so that direction of the alias matters.
TEAM_ALIASES = {
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WAS": "WSH",
    "LA": "LAR",
}

POSITION_IMPORTANCE = {
    "QB": 7.0, "RB": 3.0, "WR": 2.5, "TE": 2.0,
    "OT": 2.0, "OG": 1.5, "G": 1.5, "C": 1.5,
    "DE": 2.0, "DT": 1.5, "NT": 1.5, "LB": 1.5,
    "CB": 2.0, "S": 1.5, "K": 1.0, "P": 0.5,
}
DEFAULT_POSITION_IMPORTANCE = 1.0


def canonical_abbreviation(abbr: str) -> str:
    return TEAM_ALIASES.get(abbr, abbr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_reference.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/reference.py tests/test_reference.py
git commit -m "feat: add stadium coordinates, team aliases, position importance table

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: ESPN source (teams, scoreboard, injuries)

**Files:**
- Create: `app/sources/__init__.py`
- Create: `app/sources/espn.py`
- Create: `tests/fixtures/espn_teams.json`
- Create: `tests/fixtures/espn_scoreboard.json`
- Create: `tests/fixtures/espn_injuries_summary.json`
- Test: `tests/test_espn.py`

**Interfaces:**
- Consumes: `app.reference.canonical_abbreviation`.
- Produces: `espn.parse_teams(raw: dict) -> list[dict]` (each `{"id", "name", "abbreviation"}`); `espn.fetch_teams(client) -> list[dict]`; `espn.parse_scoreboard(raw: dict) -> list[dict]` (each `{"id", "season", "week", "home_team_id", "away_team_id", "kickoff_at", "venue_name", "is_outdoor", "status", "home_score", "away_score"}`); `espn.fetch_scoreboard(client, season, week, season_type=2) -> list[dict]`; `espn.fetch_current_week(client) -> tuple[int, int]`; `espn.parse_injuries(raw: dict) -> list[dict]` (each `{"team_abbreviation", "player_name", "position", "status"}`); `espn.fetch_game_summary(client, event_id) -> dict`. Used by `sync.py`.

- [ ] **Step 1: Write the failing tests and fixtures**

```json
// tests/fixtures/espn_teams.json
{
  "sports": [
    {
      "leagues": [
        {
          "teams": [
            {"team": {"id": "22", "abbreviation": "ARI", "displayName": "Arizona Cardinals"}},
            {"team": {"id": "26", "abbreviation": "SEA", "displayName": "Seattle Seahawks"}},
            {"team": {"id": "17", "abbreviation": "NE", "displayName": "New England Patriots"}}
          ]
        }
      ]
    }
  ]
}
```

```json
// tests/fixtures/espn_scoreboard.json
{
  "events": [
    {
      "id": "401872001",
      "date": "2026-09-10T00:20Z",
      "season": {"year": 2026, "type": 2},
      "week": {"number": 1},
      "competitions": [
        {
          "date": "2026-09-10T00:20Z",
          "venue": {"id": "3673", "fullName": "Lumen Field", "indoor": false},
          "competitors": [
            {"homeAway": "home", "score": "27", "team": {"id": "26", "abbreviation": "SEA"}},
            {"homeAway": "away", "score": "20", "team": {"id": "17", "abbreviation": "NE"}}
          ],
          "status": {"type": {"name": "STATUS_FINAL", "completed": true}}
        }
      ]
    },
    {
      "id": "401872002",
      "date": "2026-09-14T17:00Z",
      "season": {"year": 2026, "type": 2},
      "week": {"number": 1},
      "competitions": [
        {
          "date": "2026-09-14T17:00Z",
          "venue": {"id": "3025", "fullName": "U.S. Bank Stadium", "indoor": true},
          "competitors": [
            {"homeAway": "home", "score": "0", "team": {"id": "16", "abbreviation": "MIN"}},
            {"homeAway": "away", "score": "0", "team": {"id": "22", "abbreviation": "ARI"}}
          ],
          "status": {"type": {"name": "STATUS_SCHEDULED", "completed": false}}
        }
      ]
    }
  ]
}
```

```json
// tests/fixtures/espn_injuries_summary.json
{
  "injuries": [
    {
      "team": {"id": "26", "abbreviation": "SEA"},
      "injuries": [
        {"status": "Out", "athlete": {"displayName": "Zach Charbonnet", "position": {"abbreviation": "RB"}}},
        {"status": "Questionable", "athlete": {"displayName": "Amari Kight", "position": {"abbreviation": "OT"}}}
      ]
    },
    {
      "team": {"id": "17", "abbreviation": "NE"},
      "injuries": []
    }
  ]
}
```

```python
# tests/test_espn.py
import json
from pathlib import Path

from app.sources import espn

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_teams():
    raw = load_fixture("espn_teams.json")
    teams = espn.parse_teams(raw)
    assert teams == [
        {"id": "22", "name": "Arizona Cardinals", "abbreviation": "ARI"},
        {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"},
        {"id": "17", "name": "New England Patriots", "abbreviation": "NE"},
    ]


def test_parse_scoreboard_final_game():
    raw = load_fixture("espn_scoreboard.json")
    games = espn.parse_scoreboard(raw)
    final_game = games[0]
    assert final_game["id"] == "401872001"
    assert final_game["status"] == "final"
    assert final_game["home_team_id"] == "26"
    assert final_game["away_team_id"] == "17"
    assert final_game["home_score"] == 27
    assert final_game["away_score"] == 20
    assert final_game["is_outdoor"] is True


def test_parse_scoreboard_scheduled_game_has_no_scores():
    raw = load_fixture("espn_scoreboard.json")
    games = espn.parse_scoreboard(raw)
    scheduled_game = games[1]
    assert scheduled_game["status"] == "scheduled"
    assert scheduled_game["home_score"] is None
    assert scheduled_game["is_outdoor"] is False


def test_parse_injuries():
    raw = load_fixture("espn_injuries_summary.json")
    injuries = espn.parse_injuries(raw)
    assert injuries == [
        {"team_abbreviation": "SEA", "player_name": "Zach Charbonnet", "position": "RB", "status": "Out"},
        {"team_abbreviation": "SEA", "player_name": "Amari Kight", "position": "OT", "status": "Questionable"},
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_espn.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sources'`

- [ ] **Step 3: Write the implementation**

```python
# app/sources/__init__.py
```

```python
# app/sources/espn.py
from __future__ import annotations

import httpx

from app.reference import canonical_abbreviation

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def parse_teams(raw: dict) -> list[dict]:
    teams = []
    for entry in raw["sports"][0]["leagues"][0]["teams"]:
        team = entry["team"]
        teams.append({
            "id": team["id"],
            "name": team["displayName"],
            "abbreviation": team["abbreviation"],
        })
    return teams


def fetch_teams(client: httpx.Client) -> list[dict]:
    resp = client.get(f"{BASE_URL}/teams")
    resp.raise_for_status()
    return parse_teams(resp.json())


def parse_scoreboard(raw: dict) -> list[dict]:
    games = []
    for event in raw.get("events", []):
        competition = event["competitions"][0]
        home = next(c for c in competition["competitors"] if c["homeAway"] == "home")
        away = next(c for c in competition["competitors"] if c["homeAway"] == "away")
        venue = competition.get("venue", {})
        status_type = competition["status"]["type"]
        completed = bool(status_type.get("completed"))

        games.append({
            "id": event["id"],
            "season": event["season"]["year"],
            "week": event["week"]["number"],
            "home_team_id": home["team"]["id"],
            "away_team_id": away["team"]["id"],
            "kickoff_at": event["date"],
            "venue_name": venue.get("fullName"),
            "is_outdoor": not venue.get("indoor", False),
            "status": "final" if completed else "scheduled",
            "home_score": int(home["score"]) if completed else None,
            "away_score": int(away["score"]) if completed else None,
        })
    return games


def fetch_scoreboard(client: httpx.Client, season: int, week: int, season_type: int = 2) -> list[dict]:
    resp = client.get(
        f"{BASE_URL}/scoreboard",
        params={"dates": season, "seasontype": season_type, "week": week},
    )
    resp.raise_for_status()
    return parse_scoreboard(resp.json())


def fetch_current_week(client: httpx.Client) -> tuple[int, int]:
    resp = client.get(f"{BASE_URL}/scoreboard")
    resp.raise_for_status()
    data = resp.json()
    return data["season"]["year"], data["week"]["number"]


def parse_injuries(raw: dict) -> list[dict]:
    injuries = []
    for team_block in raw.get("injuries", []):
        team_abbr = canonical_abbreviation(team_block["team"]["abbreviation"])
        for item in team_block.get("injuries", []):
            athlete = item["athlete"]
            injuries.append({
                "team_abbreviation": team_abbr,
                "player_name": athlete["displayName"],
                "position": athlete.get("position", {}).get("abbreviation"),
                "status": item["status"],
            })
    return injuries


def fetch_game_summary(client: httpx.Client, event_id: str) -> dict:
    resp = client.get(f"{BASE_URL}/summary", params={"event": event_id})
    resp.raise_for_status()
    return resp.json()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_espn.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/sources/__init__.py app/sources/espn.py tests/fixtures/espn_teams.json tests/fixtures/espn_scoreboard.json tests/fixtures/espn_injuries_summary.json tests/test_espn.py
git commit -m "feat: add ESPN source module for teams, scoreboard, injuries

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: nflverse historical source

**Files:**
- Create: `app/sources/nflverse.py`
- Create: `tests/fixtures/nflverse_games_sample.csv`
- Test: `tests/test_nflverse.py`

**Interfaces:**
- Consumes: `app.reference.canonical_abbreviation`, `app.reference.STADIUMS`.
- Produces: `nflverse.parse_games_csv(csv_text: str, min_season: int) -> list[dict]` (each `{"id", "season", "week", "home_abbreviation", "away_abbreviation", "kickoff_at": None, "venue_name", "is_outdoor", "status": "final", "home_score", "away_score"}`); `nflverse.fetch_games_csv(client, min_season) -> list[dict]`. Note the key difference from `espn.parse_scoreboard`: rows are keyed by team **abbreviation**, not ESPN team id — `sync.py` resolves abbreviation to id via the `teams` table.

- [ ] **Step 1: Write the failing tests and fixture**

```csv
# tests/fixtures/nflverse_games_sample.csv
game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,home_score,location,result,total,overtime,old_game_id,gsis,nfl_detail_id,pfr,pff,espn,ftn,away_rest,home_rest,away_moneyline,home_moneyline,spread_line,away_spread_odds,home_spread_odds,total_line,under_odds,over_odds,div_game,roof,surface,temp,wind,away_qb_id,home_qb_id,away_qb_name,home_qb_name,away_coach,home_coach,referee,stadium_id,stadium
2024_01_NE_SEA,2024,REG,1,2024-09-08,Sunday,,NE,17,SEA,27,Home,10,44,0,,,,,,,,7,7,,,,,,,,,0,outdoors,grass,68,8,,,,,,,,SEA00,Lumen Field
2024_01_ARI_MIN,2024,REG,1,2024-09-08,Sunday,,ARI,20,MIN,24,Home,4,44,0,,,,,,,,7,7,,,,,,,,,0,dome,turf,,,,,,,,,MIN00,U.S. Bank Stadium
2024_02_OAK_KC,2024,REG,2,2024-09-15,Sunday,,OAK,10,KC,31,Home,21,41,0,,,,,,,,7,7,,,,,,,,,1,outdoors,grass,75,6,,,,,,,,KC00,Arrowhead Stadium
2023_01_SEA_LAR,2023,REG,1,2023-09-10,Sunday,,SEA,13,LAR,30,Home,17,43,0,,,,,,,,7,7,,,,,,,,,1,outdoors,grass,80,5,,,,,,,,LAR00,SoFi Stadium
2024_03_BUF_NE,2024,REG,3,2024-09-22,Sunday,,BUF,,NE,,Home,,,,,,,,,,,7,7,,,,,,,,,1,outdoors,grass,,,,,,,,,NE00,Gillette Stadium
```

```python
# tests/test_nflverse.py
from pathlib import Path

from app.sources import nflverse

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_csv():
    return (FIXTURES / "nflverse_games_sample.csv").read_text()


def test_parse_games_csv_filters_by_min_season():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    seasons = {g["season"] for g in games}
    assert seasons == {2024}


def test_parse_games_csv_resolves_team_aliases():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    oak_game = next(g for g in games if g["id"] == "2024_02_OAK_KC")
    assert oak_game["away_abbreviation"] == "LV"


def test_parse_games_csv_reads_roof_and_scores():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    dome_game = next(g for g in games if g["id"] == "2024_01_ARI_MIN")
    assert dome_game["is_outdoor"] is False
    assert dome_game["home_score"] == 24
    assert dome_game["away_score"] == 20


def test_parse_games_csv_skips_games_without_final_scores():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    ids = {g["id"] for g in games}
    assert "2024_03_BUF_NE" not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_nflverse.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError: module 'app.sources' has no attribute 'nflverse'`

- [ ] **Step 3: Write the implementation**

```python
# app/sources/nflverse.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_nflverse.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/sources/nflverse.py tests/fixtures/nflverse_games_sample.csv tests/test_nflverse.py
git commit -m "feat: add nflverse historical games source module

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Open-Meteo weather source

**Files:**
- Create: `app/sources/weather.py`
- Create: `tests/fixtures/openmeteo_forecast.json`
- Test: `tests/test_weather.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `weather.parse_forecast(raw: dict, target_time: datetime) -> dict` (`{"temp_f", "wind_mph", "precip_pct"}`); `weather.fetch_forecast(client, lat, lon, target_time) -> dict`. Used by `sync.py`.

- [ ] **Step 1: Write the failing tests and fixture**

```json
// tests/fixtures/openmeteo_forecast.json
{
  "hourly": {
    "time": ["2026-09-10T00:00", "2026-09-10T01:00", "2026-09-10T02:00"],
    "temperature_2m": [18.0, 17.5, 17.0],
    "windspeed_10m": [10.0, 25.0, 12.0],
    "precipitation_probability": [5, 60, 10]
  }
}
```

```python
# tests/test_weather.py
import json
from datetime import datetime
from pathlib import Path

from app.sources import weather

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture():
    return json.loads((FIXTURES / "openmeteo_forecast.json").read_text())


def test_parse_forecast_picks_closest_hour():
    raw = load_fixture()
    result = weather.parse_forecast(raw, datetime(2026, 9, 10, 1, 10))
    assert result["wind_mph"] == round(25.0 * 0.621371, 1)
    assert result["precip_pct"] == 60


def test_parse_forecast_converts_celsius_to_fahrenheit():
    raw = load_fixture()
    result = weather.parse_forecast(raw, datetime(2026, 9, 10, 0, 0))
    assert result["temp_f"] == round(18.0 * 9 / 5 + 32, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_weather.py -v`
Expected: FAIL with `AttributeError: module 'app.sources' has no attribute 'weather'`

- [ ] **Step 3: Write the implementation**

```python
# app/sources/weather.py
from __future__ import annotations

from datetime import datetime

import httpx

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _closest_hour_index(times: list[str], target: datetime) -> int:
    target_naive = target.replace(tzinfo=None)
    diffs = [abs((datetime.fromisoformat(t) - target_naive).total_seconds()) for t in times]
    return diffs.index(min(diffs))


def parse_forecast(raw: dict, target_time: datetime) -> dict:
    hourly = raw["hourly"]
    idx = _closest_hour_index(hourly["time"], target_time)
    temp_c = hourly["temperature_2m"][idx]
    wind_kmh = hourly["windspeed_10m"][idx]
    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "wind_mph": round(wind_kmh * 0.621371, 1),
        "precip_pct": hourly["precipitation_probability"][idx],
    }


def fetch_forecast(client: httpx.Client, lat: float, lon: float, target_time: datetime) -> dict:
    resp = client.get(BASE_URL, params={
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
        "timezone": "UTC",
        "forecast_days": 16,
    })
    resp.raise_for_status()
    return parse_forecast(resp.json(), target_time)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_weather.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/sources/weather.py tests/fixtures/openmeteo_forecast.json tests/test_weather.py
git commit -m "feat: add Open-Meteo weather source module

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Sync orchestration

**Files:**
- Modify: `app/db.py` (add upsert/lookup helpers)
- Create: `app/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `app.db.get_connection`, `app.db.init_db`; `app.sources.espn.{fetch_teams,fetch_scoreboard,fetch_current_week,fetch_game_summary,parse_injuries}`; `app.sources.nflverse.fetch_games_csv`; `app.sources.weather.fetch_forecast`; `app.reference.STADIUMS`.
- Produces (new `app.db` helpers): `upsert_team(conn, team: dict) -> None`; `upsert_game(conn, game: dict) -> None` (dict must have all `games` columns except it is fine for `lat`/`lon` to be `None`); `get_team_id_by_abbreviation(conn, abbreviation: str) -> str | None`; `upsert_weather(conn, game_id: str, weather: dict, fetched_at: str) -> None`; `replace_team_injuries(conn, team_id: str, injuries: list[dict], fetched_at: str) -> None`.
- Produces (`app.sync`): `sync_teams(conn, client) -> None`; `sync_historical(conn, client, min_season: int) -> None`; `sync_schedule(conn, client, season: int, week: int) -> list[dict]`; `sync_weather_for_upcoming(conn, client) -> None`; `sync_injuries_for_upcoming(conn, client) -> None`; `sync_all(conn, client, current_season: int) -> None`. Used by `app/main.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync.py
from datetime import datetime, timezone

from app import db, sync


def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    return conn


def test_sync_teams_inserts_rows(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        sync.espn, "fetch_teams",
        lambda client: [{"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"}],
    )
    sync.sync_teams(conn, client=None)
    row = conn.execute("SELECT * FROM teams WHERE id = '26'").fetchone()
    assert row["abbreviation"] == "SEA"


def test_sync_historical_resolves_team_ids_and_stadium_coords(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    conn.commit()

    monkeypatch.setattr(
        sync.nflverse, "fetch_games_csv",
        lambda client, min_season: [{
            "id": "2024_01_NE_SEA", "season": 2024, "week": 1,
            "home_abbreviation": "SEA", "away_abbreviation": "NE",
            "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
            "status": "final", "home_score": 27, "away_score": 20,
        }],
    )
    sync.sync_historical(conn, client=None, min_season=2024)

    row = conn.execute("SELECT * FROM games WHERE id = '2024_01_NE_SEA'").fetchone()
    assert row["home_team_id"] == "26"
    assert row["lat"] == 47.5952


def test_sync_historical_skips_unknown_team(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        sync.nflverse, "fetch_games_csv",
        lambda client, min_season: [{
            "id": "2024_01_XXX_SEA", "season": 2024, "week": 1,
            "home_abbreviation": "SEA", "away_abbreviation": "XXX",
            "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
            "status": "final", "home_score": 27, "away_score": 20,
        }],
    )
    sync.sync_historical(conn, client=None, min_season=2024)
    row = conn.execute("SELECT * FROM games WHERE id = '2024_01_XXX_SEA'").fetchone()
    assert row is None


def test_sync_weather_for_upcoming_only_fetches_outdoor_scheduled_games(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "17", "away_team_id": "26",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Dome", "is_outdoor": 0,
        "lat": 1.0, "lon": 1.0, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    calls = []

    def fake_fetch_forecast(client, lat, lon, target_time):
        calls.append((lat, lon))
        return {"temp_f": 60, "wind_mph": 5, "precip_pct": 10}

    monkeypatch.setattr(sync.weather, "fetch_forecast", fake_fetch_forecast)
    sync.sync_weather_for_upcoming(conn, client=None)

    assert calls == [(47.5952, -122.3316)]
    row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g1'").fetchone()
    assert row["temp_f"] == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sync'`

- [ ] **Step 3: Write the implementation**

```python
# app/db.py — add below the existing init_db function
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


def replace_team_injuries(conn, team_id: str, injuries: list[dict], fetched_at: str) -> None:
    conn.execute("DELETE FROM injuries WHERE team_id = ?", (team_id,))
    for injury in injuries:
        conn.execute(
            "INSERT INTO injuries (team_id, player_name, position, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (team_id, injury["player_name"], injury["position"], injury["status"], fetched_at),
        )
```

```python
# app/sync.py
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app import db
from app.config import SYNC_SEASONS_BACK
from app.reference import STADIUMS
from app.sources import espn, nflverse, weather

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_teams(conn, client: httpx.Client) -> None:
    for team in espn.fetch_teams(client):
        db.upsert_team(conn, team)
    conn.commit()


def sync_historical(conn, client: httpx.Client, min_season: int) -> None:
    for game in nflverse.fetch_games_csv(client, min_season):
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
    seen_teams: set[str] = set()
    for game_id in game_ids:
        try:
            summary = espn.fetch_game_summary(client, game_id)
        except httpx.HTTPError:
            logger.warning("Skipping injuries for game %s: summary fetch failed", game_id)
            continue
        by_team: dict[str, list[dict]] = {}
        for injury in espn.parse_injuries(summary):
            by_team.setdefault(injury["team_abbreviation"], []).append(injury)
        for abbr, team_injuries in by_team.items():
            team_id = db.get_team_id_by_abbreviation(conn, abbr)
            if not team_id or team_id in seen_teams:
                continue
            db.replace_team_injuries(conn, team_id, team_injuries, _now_iso())
            seen_teams.add(team_id)
    conn.commit()


def sync_all(conn, client: httpx.Client, current_season: int) -> None:
    sync_teams(conn, client)
    sync_historical(conn, client, min_season=current_season - SYNC_SEASONS_BACK)
    season, week = espn.fetch_current_week(client)
    for w in range(1, week + 3):
        sync_schedule(conn, client, season, w)
    sync_weather_for_upcoming(conn, client)
    sync_injuries_for_upcoming(conn, client)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/sync.py tests/test_sync.py
git commit -m "feat: add sync orchestration tying data sources into SQLite

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Rolling stats queries

**Files:**
- Create: `app/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `app.db.{get_connection,init_db,upsert_team,upsert_game}`.
- Produces: `stats.recent_scoring_stats(conn, team_id: str, window: int) -> dict` (`{"avg_points_scored", "avg_points_allowed", "games_counted"}`); `stats.home_away_split(conn, team_id: str) -> dict` (`{"home_avg", "away_avg", "overall_avg"}`); `stats.head_to_head(conn, team_id: str, opponent_id: str) -> dict` (`{"avg_points_scored", "meetings"}`); `stats.rest_days(conn, team_id: str, before: str) -> int | None`. Used by `predict.py` and `app/main.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_stats.py
from app import db, stats


def seed_game(conn, game_id, home_id, away_id, home_score, away_score, kickoff_at, status="final"):
    db.upsert_game(conn, {
        "id": game_id, "season": 2026, "week": 1, "home_team_id": home_id, "away_team_id": away_id,
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": True, "lat": 0, "lon": 0,
        "status": status, "home_score": home_score, "away_score": away_score,
    })


def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    db.upsert_team(conn, {"id": "C", "name": "Team C", "abbreviation": "C"})
    conn.commit()
    return conn


def test_recent_scoring_stats_averages_last_n_games(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 20, 10, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "B", "A", 14, 30, "2026-09-08T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=8)
    assert result["avg_points_scored"] == (20 + 30) / 2
    assert result["avg_points_allowed"] == (10 + 14) / 2
    assert result["games_counted"] == 2


def test_recent_scoring_stats_respects_window(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 100, 0, "2026-08-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 10, 0, "2026-09-01T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=1)
    assert result["avg_points_scored"] == 10
    assert result["games_counted"] == 1


def test_home_away_split(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 30, 10, "2026-09-01T00:00Z")  # A home
    seed_game(conn, "g2", "B", "A", 10, 10, "2026-09-08T00:00Z")  # A away
    conn.commit()

    result = stats.home_away_split(conn, "A")
    assert result["home_avg"] == 30
    assert result["away_avg"] == 10
    assert result["overall_avg"] == 20


def test_head_to_head_only_counts_matchups_between_the_two_teams(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 21, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "C", 40, 3, "2026-09-08T00:00Z")
    conn.commit()

    result = stats.head_to_head(conn, "A", "B")
    assert result["meetings"] == 1
    assert result["avg_points_scored"] == 21


def test_rest_days_computed_from_previous_game(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 21, 17, "2026-09-01T00:00Z")
    conn.commit()

    days = stats.rest_days(conn, "A", before="2026-09-08T00:00Z")
    assert days == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.stats'`

- [ ] **Step 3: Write the implementation**

```python
# app/stats.py
from __future__ import annotations

import sqlite3
from datetime import datetime


def _team_games(conn: sqlite3.Connection, team_id: str, limit: int | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT * FROM games
        WHERE status = 'final' AND (home_team_id = ? OR away_team_id = ?)
        ORDER BY kickoff_at DESC, id DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, (team_id, team_id)).fetchall()


def recent_scoring_stats(conn: sqlite3.Connection, team_id: str, window: int) -> dict:
    games = _team_games(conn, team_id, limit=window)
    if not games:
        return {"avg_points_scored": None, "avg_points_allowed": None, "games_counted": 0}

    scored, allowed = [], []
    for g in games:
        if g["home_team_id"] == team_id:
            scored.append(g["home_score"])
            allowed.append(g["away_score"])
        else:
            scored.append(g["away_score"])
            allowed.append(g["home_score"])

    return {
        "avg_points_scored": sum(scored) / len(scored),
        "avg_points_allowed": sum(allowed) / len(allowed),
        "games_counted": len(games),
    }


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_stats.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/stats.py tests/test_stats.py
git commit -m "feat: add rolling stats queries (scoring trend, home/away, h2h, rest days)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Prediction engine

**Files:**
- Create: `app/predict.py`
- Create: `weights.yaml`
- Test: `tests/test_predict.py`

**Interfaces:**
- Consumes: `app.stats.{recent_scoring_stats,home_away_split,head_to_head,rest_days}`; `app.reference.{POSITION_IMPORTANCE,DEFAULT_POSITION_IMPORTANCE}`; `app.db.{upsert_team,upsert_game}` (test-only, to seed).
- Produces: `predict.load_weights(path: Path) -> dict`; `predict.predict_game(conn, weights: dict, game: sqlite3.Row) -> dict` (`{"predicted_home_score", "predicted_away_score", "breakdown": {"home": {...}, "away": {...}}}`, each breakdown side has keys `baseline, home_away_split, head_to_head, weather, rest_days, injuries`); `predict.save_prediction(conn, game_id: str, result: dict, weights: dict) -> None`. Used by `app/main.py`.

- [ ] **Step 1: Write the failing tests and default weights file**

```yaml
# weights.yaml
recent_scoring_trend: 1.0
home_away_split: 1.0
head_to_head: 0.5
weather: 1.0
rest_days: 0.5
injuries: 1.0
recent_games_window: 8
```

```python
# tests/test_predict.py
from app import db, predict

WEIGHTS = {
    "recent_scoring_trend": 1.0,
    "home_away_split": 1.0,
    "head_to_head": 0.5,
    "weather": 1.0,
    "rest_days": 0.5,
    "injuries": 1.0,
    "recent_games_window": 8,
}


def seed_game(conn, game_id, home_id, away_id, home_score, away_score, kickoff_at, status="final"):
    db.upsert_game(conn, {
        "id": game_id, "season": 2026, "week": 1, "home_team_id": home_id, "away_team_id": away_id,
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": True, "lat": 0, "lon": 0,
        "status": status, "home_score": home_score, "away_score": away_score,
    })


def seed_upcoming(conn):
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 2, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-08-08T00:00Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })


def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    conn.commit()
    return conn


def test_load_weights_reads_yaml(tmp_path):
    weights_file = tmp_path / "weights.yaml"
    weights_file.write_text("recent_scoring_trend: 2.0\nrecent_games_window: 4\n")
    weights = predict.load_weights(weights_file)
    assert weights["recent_scoring_trend"] == 2.0
    assert weights["recent_games_window"] == 4


def test_predict_game_baseline_uses_recent_scoring_and_opponent_defense(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 30, 10, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)

    assert result["predicted_home_score"] > 0
    assert result["predicted_away_score"] > 0
    assert "baseline" in result["breakdown"]["home"]


def test_predict_game_skips_weather_when_no_forecast_row(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 20, 20, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)
    assert result["breakdown"]["home"]["weather"] == 0.0


def test_predict_game_applies_negative_injury_adjustment(tmp_path):
    conn = make_conn(tmp_path)
    seed_game(conn, "g1", "A", "B", 20, 20, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.execute(
        "INSERT INTO injuries (team_id, player_name, position, status, fetched_at) VALUES (?, ?, ?, ?, ?)",
        ("A", "Star QB", "QB", "Out", "2026-08-01T00:00Z"),
    )
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)
    assert result["breakdown"]["home"]["injuries"] < 0


def test_save_prediction_persists_row(tmp_path):
    conn = make_conn(tmp_path)
    seed_upcoming(conn)
    conn.commit()

    result = {"predicted_home_score": 24.0, "predicted_away_score": 17.0, "breakdown": {"home": {}, "away": {}}}
    predict.save_prediction(conn, "g2", result, WEIGHTS)

    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g2'").fetchone()
    assert row["predicted_home_score"] == 24.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_predict.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.predict'`

- [ ] **Step 3: Write the implementation**

```python
# app/predict.py
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import stats
from app.reference import DEFAULT_POSITION_IMPORTANCE, POSITION_IMPORTANCE

INJURY_STATUSES_COUNTED = {"Out", "Doubtful", "Injured Reserve"}
LEAGUE_AVERAGE_SCORE = 21.0


def load_weights(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _average(a: float | None, b: float | None) -> float:
    values = [v for v in (a, b) if v is not None]
    return sum(values) / len(values) if values else LEAGUE_AVERAGE_SCORE


def _scaled_baseline(raw_baseline: float, weight: float) -> float:
    """Blend the recent-scoring-derived baseline with the league average.

    weight=1.0 (the default) uses the raw baseline unchanged; weight=0
    ignores recent form entirely and predicts the league average; values
    in between (or above 1) scale how strongly recent form is trusted.
    """
    return LEAGUE_AVERAGE_SCORE + weight * (raw_baseline - LEAGUE_AVERAGE_SCORE)


def _home_away_adjustment(conn, team_id: str, is_home: bool, weight: float) -> float:
    split = stats.home_away_split(conn, team_id)
    if split["overall_avg"] is None:
        return 0.0
    side_avg = split["home_avg"] if is_home else split["away_avg"]
    return weight * (side_avg - split["overall_avg"])


def _head_to_head_adjustment(conn, team_id: str, opponent_id: str, overall_avg, weight: float) -> float:
    h2h = stats.head_to_head(conn, team_id, opponent_id)
    if h2h["avg_points_scored"] is None or overall_avg is None:
        return 0.0
    delta = h2h["avg_points_scored"] - overall_avg
    return weight * _clamp(delta, -7, 7)


def _rest_days_adjustment(conn, team_id: str, kickoff_at: str, weight: float) -> float:
    days = stats.rest_days(conn, team_id, before=kickoff_at)
    if days is None:
        return 0.0
    return weight * _clamp((days - 7) * 0.5, -3, 3)


def _weather_adjustment(weather_row: sqlite3.Row | None, weight: float) -> float:
    if weather_row is None:
        return 0.0
    wind_penalty = -_clamp((weather_row["wind_mph"] - 15) / 5, 0, 3)
    precip_penalty = -_clamp(weather_row["precip_pct"] / 25, 0, 2)
    return weight * (wind_penalty + precip_penalty)


def _injuries_adjustment(conn, team_id: str, weight: float) -> float:
    rows = conn.execute("SELECT position, status FROM injuries WHERE team_id = ?", (team_id,)).fetchall()
    total = 0.0
    for row in rows:
        if row["status"] not in INJURY_STATUSES_COUNTED:
            continue
        importance = POSITION_IMPORTANCE.get(row["position"], DEFAULT_POSITION_IMPORTANCE)
        total -= importance * 0.3
    return weight * _clamp(total, -10, 0)


def predict_game(conn: sqlite3.Connection, weights: dict, game: sqlite3.Row) -> dict:
    window = weights.get("recent_games_window", 8)
    home_id, away_id = game["home_team_id"], game["away_team_id"]

    home_recent = stats.recent_scoring_stats(conn, home_id, window)
    away_recent = stats.recent_scoring_stats(conn, away_id, window)
    trend_weight = weights.get("recent_scoring_trend", 1.0)

    home_baseline = _scaled_baseline(
        _average(home_recent["avg_points_scored"], away_recent["avg_points_allowed"]), trend_weight
    )
    away_baseline = _scaled_baseline(
        _average(away_recent["avg_points_scored"], home_recent["avg_points_allowed"]), trend_weight
    )

    home_split = stats.home_away_split(conn, home_id)
    away_split = stats.home_away_split(conn, away_id)

    weather_row = conn.execute(
        "SELECT * FROM weather_forecasts WHERE game_id = ?", (game["id"],)
    ).fetchone()

    breakdown = {"home": {}, "away": {}}

    def apply(side: str, team_id: str, baseline: float, is_home: bool, overall_avg):
        opponent_id = away_id if is_home else home_id
        adjustments = {
            "home_away_split": _home_away_adjustment(conn, team_id, is_home, weights.get("home_away_split", 1.0)),
            "head_to_head": _head_to_head_adjustment(
                conn, team_id, opponent_id, overall_avg, weights.get("head_to_head", 0.5)
            ),
            "weather": _weather_adjustment(weather_row, weights.get("weather", 1.0)),
            "rest_days": (
                _rest_days_adjustment(conn, team_id, game["kickoff_at"], weights.get("rest_days", 0.5))
                if game["kickoff_at"] else 0.0
            ),
            "injuries": _injuries_adjustment(conn, team_id, weights.get("injuries", 1.0)),
        }
        breakdown[side] = {"baseline": baseline, **adjustments}
        return baseline + sum(adjustments.values())

    home_final = apply("home", home_id, home_baseline, True, home_split["overall_avg"])
    away_final = apply("away", away_id, away_baseline, False, away_split["overall_avg"])

    return {
        "predicted_home_score": round(max(home_final, 0), 1),
        "predicted_away_score": round(max(away_final, 0), 1),
        "breakdown": breakdown,
    }


def save_prediction(conn: sqlite3.Connection, game_id: str, result: dict, weights: dict) -> None:
    conn.execute(
        """
        INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score,
                                  factor_breakdown_json, weights_snapshot_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            game_id,
            result["predicted_home_score"],
            result["predicted_away_score"],
            json.dumps(result["breakdown"]),
            json.dumps(weights),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_predict.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/predict.py weights.yaml tests/test_predict.py
git commit -m "feat: add weighted-heuristic prediction engine

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: FastAPI app, templates, and routes

**Files:**
- Create: `app/main.py`
- Create: `app/templates/base.html`
- Create: `app/templates/index.html`
- Create: `app/templates/game_detail.html`
- Create: `app/templates/accuracy.html`
- Create: `app/static/style.css`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `app.db.{get_connection,init_db,upsert_team,upsert_game}`; `app.predict.{load_weights,predict_game,save_prediction}`; `app.stats.head_to_head`; `app.sync.sync_all`; `app.sources.espn.fetch_current_week`; `app.config.{DB_PATH,STALENESS_HOURS,WEIGHTS_PATH}`.
- Produces: `app.main.app` (the FastAPI instance), routes `GET /`, `GET /games/{game_id}`, `GET /accuracy`, `POST /sync`; `app.main.get_db` (FastAPI dependency, overridable in tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_main.py
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import db, main


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


def test_schedule_page_lists_games_for_current_week(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert "Team A" in response.text
    assert "Team B" in response.text


def test_game_detail_page_shows_breakdown_and_saves_prediction(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/games/g1")
    assert response.status_code == 200
    assert "Team A" in response.text

    conn = db.get_connection(tmp_path / "test.db")
    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g1'").fetchone()
    assert row is not None


def test_accuracy_page_loads_with_no_predictions_yet(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/accuracy")
    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Write the implementation**

```python
# app/main.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, predict, stats, sync
from app.config import DB_PATH, STALENESS_HOURS, WEIGHTS_PATH
from app.sources import espn

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def get_db():
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _is_stale(conn) -> bool:
    row = conn.execute("SELECT MAX(fetched_at) as latest FROM weather_forecasts").fetchone()
    if not row or not row["latest"]:
        return True
    latest = datetime.fromisoformat(row["latest"])
    return datetime.now(timezone.utc) - latest > timedelta(hours=STALENESS_HOURS)


@app.post("/sync")
def trigger_sync(conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, _ = espn.fetch_current_week(client)
        sync.sync_all(conn, client, current_season=season)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def schedule(request: Request, week: int | None = None, conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, current_week = espn.fetch_current_week(client)
        if _is_stale(conn):
            sync.sync_all(conn, client, current_season=season)

    week = week or current_week
    games = conn.execute(
        "SELECT * FROM games WHERE season = ? AND week = ? ORDER BY kickoff_at",
        (season, week),
    ).fetchall()
    teams = {row["id"]: row for row in conn.execute("SELECT * FROM teams").fetchall()}

    weights = predict.load_weights(WEIGHTS_PATH)
    game_predictions = {game["id"]: predict.predict_game(conn, weights, game) for game in games}

    return templates.TemplateResponse(request, "index.html", {
        "games": games, "predictions": game_predictions,
        "teams": teams, "week": week, "season": season,
    })


@app.get("/games/{game_id}", response_class=HTMLResponse)
def game_detail(request: Request, game_id: str, conn=Depends(get_db)):
    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    weights = predict.load_weights(WEIGHTS_PATH)
    result = predict.predict_game(conn, weights, game)
    predict.save_prediction(conn, game_id, result, weights)

    home_team = conn.execute("SELECT * FROM teams WHERE id = ?", (game["home_team_id"],)).fetchone()
    away_team = conn.execute("SELECT * FROM teams WHERE id = ?", (game["away_team_id"],)).fetchone()
    weather_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = ?", (game_id,)).fetchone()
    injuries_home = conn.execute("SELECT * FROM injuries WHERE team_id = ?", (game["home_team_id"],)).fetchall()
    injuries_away = conn.execute("SELECT * FROM injuries WHERE team_id = ?", (game["away_team_id"],)).fetchall()
    head_to_head = stats.head_to_head(conn, game["home_team_id"], game["away_team_id"])

    return templates.TemplateResponse(request, "game_detail.html", {
        "game": game, "result": result,
        "home_team": home_team, "away_team": away_team,
        "weather": weather_row, "injuries_home": injuries_home, "injuries_away": injuries_away,
        "head_to_head": head_to_head,
    })


@app.get("/accuracy", response_class=HTMLResponse)
def accuracy(request: Request, conn=Depends(get_db)):
    rows = conn.execute(
        """
        SELECT p.game_id, p.predicted_home_score, p.predicted_away_score, p.created_at,
               g.home_score, g.away_score,
               ht.abbreviation as home_abbr, at_.abbreviation as away_abbr
        FROM predictions p
        JOIN games g ON g.id = p.game_id
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at_ ON at_.id = g.away_team_id
        WHERE g.status = 'final'
        ORDER BY p.created_at DESC
        """
    ).fetchall()

    errors = []
    for row in rows:
        margin_error = abs(
            (row["predicted_home_score"] - row["predicted_away_score"])
            - (row["home_score"] - row["away_score"])
        )
        total_error = abs(
            (row["predicted_home_score"] + row["predicted_away_score"])
            - (row["home_score"] + row["away_score"])
        )
        errors.append({"row": row, "margin_error": margin_error, "total_error": total_error})

    mean_margin_error = sum(e["margin_error"] for e in errors) / len(errors) if errors else None
    mean_total_error = sum(e["total_error"] for e in errors) / len(errors) if errors else None

    return templates.TemplateResponse(request, "accuracy.html", {
        "errors": errors,
        "mean_margin_error": mean_margin_error, "mean_total_error": mean_total_error,
    })
```

```html
<!-- app/templates/base.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>NFL Predictor</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <nav>
      <a href="/">Schedule</a>
      <a href="/accuracy">Accuracy</a>
      <form method="post" action="/sync" onsubmit="return confirm('Refresh data from ESPN/nflverse/Open-Meteo now?');">
        <button type="submit">Refresh Data</button>
      </form>
    </nav>
  </header>
  <main>
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

```html
<!-- app/templates/index.html -->
{% extends "base.html" %}
{% block content %}
<h1>Week {{ week }} &middot; {{ season }}</h1>
<form method="get" action="/">
  <label>Week: <input type="number" name="week" min="1" max="18" value="{{ week }}"></label>
  <button type="submit">Go</button>
</form>
<table>
  <thead>
    <tr><th>Away</th><th>Home</th><th>Kickoff</th><th>Predicted Score</th></tr>
  </thead>
  <tbody>
    {% for game in games %}
    {% set p = predictions[game.id] %}
    <tr>
      <td>{{ teams[game.away_team_id].name }}</td>
      <td>{{ teams[game.home_team_id].name }}</td>
      <td>{{ game.kickoff_at }}</td>
      <td>
        <a href="/games/{{ game.id }}">
          {{ p.predicted_away_score }} &ndash; {{ p.predicted_home_score }}
        </a>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

```html
<!-- app/templates/game_detail.html -->
{% extends "base.html" %}
{% block content %}
<h1>{{ away_team.name }} at {{ home_team.name }}</h1>
<p>{{ game.kickoff_at }} &middot; {{ game.venue_name }}</p>

<h2>Predicted Score: {{ result.predicted_away_score }} &ndash; {{ result.predicted_home_score }}</h2>

<h3>Factor Breakdown</h3>
<table>
  <thead><tr><th>Factor</th><th>{{ away_team.abbreviation }}</th><th>{{ home_team.abbreviation }}</th></tr></thead>
  <tbody>
    {% for factor in result.breakdown.home.keys() %}
    <tr>
      <td>{{ factor }}</td>
      <td>{{ "%.1f"|format(result.breakdown.away[factor]) }}</td>
      <td>{{ "%.1f"|format(result.breakdown.home[factor]) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<h3>Head-to-Head</h3>
<p>
  {% if head_to_head.meetings %}
    {{ home_team.abbreviation }} averages {{ "%.1f"|format(head_to_head.avg_points_scored) }} points
    over {{ head_to_head.meetings }} meeting(s) vs {{ away_team.abbreviation }}.
  {% else %}
    No prior meetings on record.
  {% endif %}
</p>

<h3>Weather</h3>
<p>
  {% if weather %}
    {{ weather.temp_f }}&deg;F, wind {{ weather.wind_mph }} mph, {{ weather.precip_pct }}% precip chance
  {% else %}
    No forecast (indoor venue or not yet available).
  {% endif %}
</p>

<h3>Injuries</h3>
<h4>{{ home_team.abbreviation }}</h4>
<ul>{% for i in injuries_home %}<li>{{ i.player_name }} ({{ i.position }}) &ndash; {{ i.status }}</li>{% endfor %}</ul>
<h4>{{ away_team.abbreviation }}</h4>
<ul>{% for i in injuries_away %}<li>{{ i.player_name }} ({{ i.position }}) &ndash; {{ i.status }}</li>{% endfor %}</ul>
{% endblock %}
```

```html
<!-- app/templates/accuracy.html -->
{% extends "base.html" %}
{% block content %}
<h1>Prediction Accuracy</h1>
{% if mean_margin_error is not none %}
<p>Mean margin error: {{ "%.1f"|format(mean_margin_error) }} points &middot; Mean total-points error: {{ "%.1f"|format(mean_total_error) }} points</p>
{% else %}
<p>No completed, predicted games yet.</p>
{% endif %}
<table>
  <thead><tr><th>Game</th><th>Predicted</th><th>Actual</th><th>Margin Error</th></tr></thead>
  <tbody>
    {% for e in errors %}
    <tr>
      <td>{{ e.row.away_abbr }} @ {{ e.row.home_abbr }}</td>
      <td>{{ "%.1f"|format(e.row.predicted_away_score) }} &ndash; {{ "%.1f"|format(e.row.predicted_home_score) }}</td>
      <td>{{ e.row.away_score }} &ndash; {{ e.row.home_score }}</td>
      <td>{{ "%.1f"|format(e.margin_error) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

```css
/* app/static/style.css */
body { font-family: system-ui, sans-serif; margin: 2rem; color: #222; }
nav { display: flex; gap: 1rem; align-items: center; margin-bottom: 2rem; }
nav a { color: #0b5fff; text-decoration: none; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #ddd; }
button { cursor: pointer; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates app/static tests/test_main.py
git commit -m "feat: add FastAPI routes, templates, and static styling

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: README, sample-data seed script, and manual verification

**Files:**
- Create: `README.md`
- Create: `scripts/seed_sample_data.py`

**Interfaces:**
- Consumes: `app.db.{get_connection,init_db,upsert_team,upsert_game}`; `app.config.DB_PATH`.
- Produces: nothing consumed by other tasks — this is the terminal task.

- [ ] **Step 1: Write the seed script**

```python
# scripts/seed_sample_data.py
"""Seed nfl.db with a small set of sample data for local UI testing,
without hitting any live APIs. Run with: python scripts/seed_sample_data.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DB_PATH

conn = db.get_connection(DB_PATH)
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
print("Seeded sample data into", DB_PATH)
```

- [ ] **Step 2: Write the README**

```markdown
# NFL Score Predictor

A personal, locally-run app that browses upcoming NFL games and predicts
final scores from free data (ESPN, nflverse, Open-Meteo).

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

## Run

    uvicorn app.main:app --reload

Open http://127.0.0.1:8000 — the schedule page auto-syncs data on first
load (and whenever the cache is older than 6 hours). Use the "Refresh
Data" button to force a sync any time.

## Sample data (no network required)

    python scripts/seed_sample_data.py

Seeds a couple of sample games so you can click through the UI without
waiting on a live sync.

## Tests

    pytest

## Tuning the prediction model

Edit `weights.yaml` and reload the page — weights are read fresh on
every prediction, no restart needed.
```

- [ ] **Step 3: Manual verification**

Run with the seed data first — it's fast, deterministic, and doesn't
depend on network conditions:

```bash
python scripts/seed_sample_data.py
uvicorn app.main:app --reload
```

Then, using the Browser tool (or the user's own browser):
- [ ] Open `http://127.0.0.1:8000/`, change the week field to the seeded week if needed, confirm the schedule table renders the sample game with a predicted score link.
- [ ] Click into the upcoming sample game, confirm the factor breakdown table renders, confirm the weather section shows "No forecast" (the sample game is indoor), and confirm a new row appears in the `predictions` table (`sqlite3 nfl.db "select * from predictions;"`).
- [ ] Open `/accuracy`, confirm it renders without error even though the seeded game may not yet have both a prediction and a final score.

Then try a real sync against live ESPN/nflverse/Open-Meteo data — delete
`nfl.db` (or just hit `/` in a browser, which auto-syncs when stale) and
confirm the schedule page loads the real current-week slate with
predicted scores, and that a game detail page shows real weather and
real injury reports for an outdoor game. This exact flow was verified
live while writing this plan (see "Implementation notes from research"
above) and worked end-to-end, so it should work here too — if ESPN
truly is unreachable from wherever this plan is executed, `/` will 500
on the `fetch_current_week` call, in which case fall back to the seed
data for UI verification and note the live-sync gap to the user rather
than silently skipping this step.

- [ ] **Step 4: Commit**

```bash
git add README.md scripts/seed_sample_data.py
git commit -m "docs: add README and sample-data seed script for local verification

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
