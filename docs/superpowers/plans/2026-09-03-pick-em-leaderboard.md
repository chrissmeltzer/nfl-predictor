# Pick'em & Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let multiple people pick straight-up game winners on the existing schedule page, with no login, and see a personal leaderboard of pick accuracy.

**Architecture:** Two new SQLite tables (`players`, `picks`) accessed through new functions in `app/db.py`, following the existing "db.py holds raw queries, main.py computes derived view data" split. Identity is a name typed once into a `/join` form, remembered via an unsigned cookie (`picker_id`) — no passwords, no sessions table. Pick buttons render inline on the existing weekly schedule cards; a new `/leaderboard` page aggregates picks against final game results in Python, the same way `accuracy.html` and `_team_record` already do.

**Tech Stack:** FastAPI, Jinja2 (`Jinja2Templates`), raw `sqlite3` (no ORM), pytest + FastAPI `TestClient`.

**Spec:** [docs/superpowers/specs/2026-09-03-pick-em-leaderboard-design.md](../specs/2026-09-03-pick-em-leaderboard-design.md)

## Global Constraints

- No authentication, passwords, or sessions table — identity is a name + an unsigned cookie (`picker_id`, ~1 year max-age, httponly, samesite=lax).
- A pick locks the instant `now >= game.kickoff_at`, or if the game's status is no longer `scheduled` — checked server-side on every `POST /games/{game_id}/pick`, not just in the UI.
- Tie games ("pushes") never count as a win or a loss for anyone.
- All new `db.py` functions take `conn` as the first argument and return `sqlite3.Row` / `list[sqlite3.Row]` / plain dicts, matching every existing function in that file — no new abstraction layer.
- Templates extend `base.html` and reuse existing CSS custom properties (`--cyan`, `--green`, `--rose`, `--amber`, `--muted`, `--text`, `--line`, `--shadow`) and existing classes (`.table-wrap`, `table`/`th`/`td`, `.notice`, `.badge-group`) rather than inventing a parallel style system.
- Tests live in `tests/test_picks.py` and reuse `make_test_client` from `tests/test_main.py` (import as `from tests.test_main import make_test_client` — `tests/` has no `__init__.py`, but `pythonpath = .` in `pytest.ini` makes it an importable namespace package; this already works, confirmed via `PYTHONPATH=. python3 -c "from tests.test_main import make_test_client"`).

---

## File Structure

- **Modify `app/db.py`** — add `players` + `picks` tables to `SCHEMA_SQL`, add 6 new functions.
- **Modify `app/main.py`** — add cookie constant, `_current_player`, `_pick_locked`, `_actual_winner_team_id`; update `_game_view` and `_template_context`; update the `/` route; add `GET/POST /join`, `POST /games/{game_id}/pick`, `GET /leaderboard`.
- **Modify `app/templates/base.html`** — add the "Picking as X" chip and the "Leaderboard" nav link.
- **Modify `app/templates/index.html`** — add a locked-pick error banner and a pick UI block on each matchup card.
- **Create `app/templates/join.html`** — the name-entry form.
- **Create `app/templates/leaderboard.html`** — standings + weekly breakdown tables.
- **Modify `app/static/style.css`** — append rules for the new UI pieces.
- **Create `tests/test_picks.py`** — all new tests (db-level and route-level).

---

### Task 1: Players & picks schema + db helpers

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_picks.py` (create)

**Interfaces:**
- Produces:
  - `get_or_create_player(conn, name: str, created_at: str) -> sqlite3.Row` (columns: `id`, `name`, `created_at`)
  - `get_player_by_id(conn, player_id: int) -> sqlite3.Row | None`
  - `get_all_players(conn) -> list[sqlite3.Row]`
  - `upsert_pick(conn, player_id: int, game_id: str, picked_team_id: str, created_at: str) -> None`
  - `get_player_picks_for_games(conn, player_id: int, game_ids: list[str]) -> dict[str, str]` (`game_id -> picked_team_id`)
  - `get_decided_picks(conn) -> list[sqlite3.Row]` (columns: `player_id`, `player_name`, `season`, `week`, `picked_team_id`, `home_team_id`, `away_team_id`, `home_score`, `away_score`; only rows whose game is `final`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_picks.py`:

```python
from datetime import datetime, timezone

from app import db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seed_teams_and_game(conn, game_id="g1", status="scheduled", home_score=None, away_score=None,
                          kickoff_at="2026-09-10T00:20Z", season=2026, week=1):
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    db.upsert_game(conn, {
        "id": game_id, "season": season, "week": week, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": status, "home_score": home_score, "away_score": away_score,
    })


def test_init_db_creates_players_and_picks_tables(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"players", "picks"} <= tables


def test_get_or_create_player_is_case_insensitive_and_idempotent(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)

    first = db.get_or_create_player(conn, "Chris", _now())
    conn.commit()
    second = db.get_or_create_player(conn, "chris", _now())
    conn.commit()

    assert first["id"] == second["id"]
    assert len(db.get_all_players(conn)) == 1


def test_upsert_pick_overwrites_existing_pick_for_same_game(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    _seed_teams_and_game(conn)
    player = db.get_or_create_player(conn, "Chris", _now())
    conn.commit()

    db.upsert_pick(conn, player["id"], "g1", "A", _now())
    db.upsert_pick(conn, player["id"], "g1", "B", _now())
    conn.commit()

    assert db.get_player_picks_for_games(conn, player["id"], ["g1"]) == {"g1": "B"}


def test_get_player_picks_for_games_returns_empty_dict_for_no_games(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    player = db.get_or_create_player(conn, "Chris", _now())
    conn.commit()

    assert db.get_player_picks_for_games(conn, player["id"], []) == {}


def test_get_decided_picks_only_returns_final_games(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    _seed_teams_and_game(conn, game_id="g_final", status="final", home_score=24, away_score=17)
    _seed_teams_and_game(conn, game_id="g_scheduled", status="scheduled", week=2)
    player = db.get_or_create_player(conn, "Chris", _now())
    conn.commit()
    db.upsert_pick(conn, player["id"], "g_final", "A", _now())
    db.upsert_pick(conn, player["id"], "g_scheduled", "A", _now())
    conn.commit()

    decided = db.get_decided_picks(conn)

    assert len(decided) == 1
    assert decided[0]["week"] == 1
    assert decided[0]["player_name"] == "Chris"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py -v`
Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'get_or_create_player'` (and similar for the other new functions).

- [ ] **Step 3: Add the schema and functions to `app/db.py`**

In `app/db.py`, add two new tables at the end of `SCHEMA_SQL` (right before the closing `"""` that currently ends after the `team_ratings` table):

```python
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
```

(i.e. this text goes right after the existing `team_ratings` table's closing `;` and before the final `"""` that terminates `SCHEMA_SQL`.)

Then append these functions at the end of `app/db.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_picks.py
git commit -m "$(cat <<'EOF'
feat: add players and picks tables with db helpers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Identity — cookie, `/join` routes, header chip

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Create: `app/templates/join.html`
- Modify: `app/static/style.css`
- Test: `tests/test_picks.py`

**Interfaces:**
- Consumes: `db.get_or_create_player`, `db.get_player_by_id` (Task 1)
- Produces:
  - `PICKER_COOKIE = "picker_id"`, `PICKER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365` (module-level constants in `main.py`)
  - `_current_player(request: Request, conn) -> sqlite3.Row | None`
  - `_template_context(request: Request, conn, **kwargs) -> dict` — **signature changes**, now requires `conn` as the second positional argument and always injects `"player"` into the returned context
  - Routes: `GET /join`, `POST /join`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_picks.py`:

Add this import at the top of `tests/test_picks.py`, alongside the existing `from app import db`:

```python
from tests.test_main import make_test_client
```

Then append the tests:

```python
def test_join_get_renders_name_form(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/join")
    assert response.status_code == 200
    assert 'name="name"' in response.text


def test_join_post_creates_player_and_sets_cookie(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.post("/join", data={"name": "Chris", "next": "/"}, follow_redirects=False)
    assert response.status_code == 303
    assert "picker_id" in response.cookies


def test_join_post_reuses_existing_player_case_insensitively(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    client.post("/join", data={"name": "chris", "next": "/"})

    conn = db.get_connection(tmp_path / "test.db")
    count = conn.execute("SELECT COUNT(*) as c FROM players").fetchone()["c"]
    conn.close()
    assert count == 1


def test_join_post_rejects_empty_name(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.post("/join", data={"name": "   ", "next": "/"})
    assert response.status_code == 422
    assert "Enter a name" in response.text


def test_base_header_shows_picking_as_name_after_join(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    response = client.get("/")
    assert "Picking as" in response.text
    assert "Chris" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py -v`
Expected: FAIL — `404` on `GET /join` (route doesn't exist yet).

- [ ] **Step 3: Implement**

In `app/main.py`, update the import line:

```python
from fastapi import Depends, FastAPI, HTTPException, Request
```
→
```python
from fastapi import Depends, FastAPI, Form, HTTPException, Request
```

Add near the top, after the `_ANALYSIS_MIN_MAGNITUDE` / `_ANALYSIS_MAX_ITEMS` constants:

```python
PICKER_COOKIE = "picker_id"
PICKER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _current_player(request: Request, conn) -> sqlite3.Row | None:
    raw = request.cookies.get(PICKER_COOKIE)
    if not raw or not raw.isdigit():
        return None
    return db.get_player_by_id(conn, int(raw))
```

Add `import sqlite3` to the top-level imports (needed for the `sqlite3.Row | None` annotation):

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
```
→
```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
```

Replace the existing `_template_context` function:

```python
def _template_context(request: Request, **kwargs) -> dict:
    return {"request": request, **kwargs}
```
→
```python
def _template_context(request: Request, conn, **kwargs) -> dict:
    return {"request": request, "player": _current_player(request, conn), **kwargs}
```

Update every existing call site to pass `conn` as the second positional argument:

1. In `schedule()`:
```python
        _template_context(
            request, matchups=matchups, teams=list(teams.values()), week=week, season=season, sort=sort
        ),
```
→
```python
        _template_context(
            request, conn, matchups=matchups, teams=list(teams.values()), week=week, season=season, sort=sort
        ),
```

2. In `team_detail()`:
```python
        _template_context(
            request,
            team=team,
```
→
```python
        _template_context(
            request,
            conn,
            team=team,
```

3. In `game_detail()`:
```python
        _template_context(
            request, game=game, result=result, matchup=matchup, home_team=home_team, away_team=away_team,
```
→
```python
        _template_context(
            request, conn, game=game, result=result, matchup=matchup, home_team=home_team, away_team=away_team,
```

4. In `rankings()`:
```python
        _template_context(request, rankings=ranked_teams, teams=list(teams), season=season),
```
→
```python
        _template_context(request, conn, rankings=ranked_teams, teams=list(teams), season=season),
```

5. In `accuracy()`:
```python
        _template_context(request, errors=errors, mean_margin_error=mean_margin_error, mean_total_error=mean_total_error, teams=teams),
```
→
```python
        _template_context(request, conn, errors=errors, mean_margin_error=mean_margin_error, mean_total_error=mean_total_error, teams=teams),
```

Add the two new routes at the end of `app/main.py`:

```python
@app.get("/join", response_class=HTMLResponse)
def join_form(request: Request, next: str = "/", error: str | None = None, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "join.html", _template_context(request, conn, next=next, error=error)
    )


@app.post("/join")
def join_submit(request: Request, name: str = Form(...), next: str = Form("/"), conn=Depends(get_db)):
    cleaned = name.strip()
    if not cleaned:
        return templates.TemplateResponse(
            request,
            "join.html",
            _template_context(request, conn, next=next, error="Enter a name to continue."),
            status_code=422,
        )
    player = db.get_or_create_player(conn, cleaned, datetime.now(timezone.utc).isoformat())
    conn.commit()
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        PICKER_COOKIE, str(player["id"]), max_age=PICKER_COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )
    return response
```

Create `app/templates/join.html`:

```html
{% extends 'base.html' %}
{% block title %}Set your name · Fourth Down Forecast{% endblock %}
{% block content %}
<section class="hero compact-hero">
  <div>
    <p class="eyebrow">Pick'em</p>
    <h1>What's your name?</h1>
    <p class="hero-copy">No password needed — just a name so your picks and spot on the leaderboard stick around on this device.</p>
  </div>
</section>

{% if error %}
<p class="notice">{{ error }}</p>
{% endif %}

<form class="join-form" method="post" action="/join">
  <label class="sr-only" for="player-name">Your name</label>
  <input id="player-name" name="name" placeholder="e.g. Chris" autocomplete="name" autofocus required>
  <input type="hidden" name="next" value="{{ next }}">
  <button type="submit">Start picking</button>
</form>
{% endblock %}
```

In `app/templates/base.html`, add the "Leaderboard" nav placeholder is done in Task 4 — for this task only add the picker chip. Replace:

```html
      <nav aria-label="Primary navigation">
        <a href="/">Predictions</a>
        <a href="/rankings">Rankings</a>
        <a href="/accuracy">Accuracy</a>
      </nav>
      <form class="team-search" action="/teams" method="get" role="search">
```
→
```html
      <nav aria-label="Primary navigation">
        <a href="/">Predictions</a>
        <a href="/rankings">Rankings</a>
        <a href="/accuracy">Accuracy</a>
      </nav>
      <a class="picker-chip" href="/join?next={{ (request.url.path ~ (('?' + request.url.query) if request.url.query else '')) | urlencode }}">
        {% if player %}Picking as <strong>{{ player.name }}</strong> · switch{% else %}Set your name to pick{% endif %}
      </a>
      <form class="team-search" action="/teams" method="get" role="search">
```

Append to `app/static/style.css`:

```css
.picker-chip { padding: .5rem .85rem; color: var(--muted); font-size: .78rem; font-weight: 700; white-space: nowrap; background: rgba(151, 166, 186, .1); border: 1px solid var(--line); border-radius: 99px; } .picker-chip strong { color: var(--cyan); }
.join-form { display: flex; gap: .6rem; max-width: 420px; } .join-form input { flex: 1; padding: .7rem .9rem; color: var(--text); background: rgba(17, 26, 46, .9); border: 1px solid var(--line); border-radius: 10px; outline: none; } .join-form input:focus { border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(34, 211, 238, .15); } .join-form button { padding: .7rem 1.1rem; color: #06111c; font-weight: 800; background: var(--cyan); border: 0; border-radius: 10px; cursor: pointer; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py tests/test_main.py -v`
Expected: PASS — all `test_picks.py` tests, and `test_main.py` still passes (its calls to `_template_context` all go through routes, which were all updated).

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/base.html app/templates/join.html app/static/style.css tests/test_picks.py
git commit -m "$(cat <<'EOF'
feat: add cookie-based player identity and /join flow

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Picking flow on the schedule page

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/index.html`
- Modify: `app/static/style.css`
- Test: `tests/test_picks.py`

**Interfaces:**
- Consumes: `_current_player` (Task 2), `PICKER_COOKIE` (Task 2), `db.upsert_pick`, `db.get_player_picks_for_games` (Task 1)
- Produces:
  - `_pick_locked(game) -> bool`
  - `_actual_winner_team_id(game) -> str | None`
  - `_game_view(...)` return dict gains key `"actual_winner_team_id"`
  - Each matchup dict built in `schedule()` gains keys `"player_pick_team_id"` (`str | None`) and `"pick_locked"` (`bool`)
  - Route: `POST /games/{game_id}/pick`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_picks.py`:

```python
def test_schedule_page_shows_pick_buttons_for_scheduled_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert 'action="/games/g1/pick"' in response.text


def test_submit_pick_without_player_redirects_to_join(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.post("/games/g1/pick", data={"team_id": "A", "week": 1}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/join")


def test_submit_pick_persists_and_highlights_active_pick(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})

    response = client.post("/games/g1/pick", data={"team_id": "A", "week": 1})

    assert response.status_code == 200
    assert "pick-btn-active" in response.text


def test_submit_pick_rejected_after_kickoff(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})

    conn = db.get_connection(tmp_path / "test.db")
    _seed_teams_and_game(conn, game_id="g_started", kickoff_at="2020-01-01T00:00:00Z")
    conn.commit()
    conn.close()

    response = client.post("/games/g_started/pick", data={"team_id": "A", "week": 1}, follow_redirects=False)

    assert response.status_code == 303
    assert "pick_error=locked" in response.headers["location"]

    check_conn = db.get_connection(tmp_path / "test.db")
    row = check_conn.execute("SELECT * FROM picks WHERE game_id = 'g_started'").fetchone()
    check_conn.close()
    assert row is None


def test_schedule_page_shows_correct_pick_badge_for_final_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    client.post("/games/g1/pick", data={"team_id": "A", "week": 1})

    conn = db.get_connection(tmp_path / "test.db")
    conn.execute("UPDATE games SET status = 'final', home_score = 24, away_score = 17 WHERE id = 'g1'")
    conn.commit()
    conn.close()

    response = client.get("/")
    assert "pick-correct" in response.text


def test_schedule_page_shows_push_badge_for_tied_final_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    client.post("/games/g1/pick", data={"team_id": "A", "week": 1})

    conn = db.get_connection(tmp_path / "test.db")
    conn.execute("UPDATE games SET status = 'final', home_score = 20, away_score = 20 WHERE id = 'g1'")
    conn.commit()
    conn.close()

    response = client.get("/")
    assert "pick-push" in response.text
```

(`_seed_teams_and_game` here is the helper added in Task 1 — reuse it; note `test_main.make_test_client`'s fixture already creates game `g1` with teams `A`/`B`, so calling `_seed_teams_and_game` again is only needed for the extra `g_started` game in the kickoff test — it will re-upsert teams A/B harmlessly since `upsert_team` is an upsert.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py -v`
Expected: FAIL — `404` on `POST /games/g1/pick` (route doesn't exist yet), and the schedule-page assertions fail because no pick UI is rendered.

- [ ] **Step 3: Implement**

Add to `urllib.parse` import in `app/main.py`. Update:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
```
→
```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
```

Add `_pick_locked` near `_is_stale`:

```python
def _pick_locked(game) -> bool:
    if game["status"] != "scheduled":
        return True
    kickoff = game["kickoff_at"]
    if not kickoff:
        return False
    kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    return kickoff_dt <= datetime.now(timezone.utc)
```

Add `_actual_winner_team_id` right before `_reveal`:

```python
def _actual_winner_team_id(game) -> str | None:
    if game["status"] != "final" or game["home_score"] is None or game["away_score"] is None:
        return None
    if game["home_score"] > game["away_score"]:
        return game["home_team_id"]
    if game["away_score"] > game["home_score"]:
        return game["away_team_id"]
    return None
```

In `_game_view`, add the new key to the `view` dict:

```python
        "reveal": _reveal(conn, game),
        "upset_alert": result.get("upset_alert", False) and game["status"] != "final",
    }
```
→
```python
        "reveal": _reveal(conn, game),
        "upset_alert": result.get("upset_alert", False) and game["status"] != "final",
        "actual_winner_team_id": _actual_winner_team_id(game),
    }
```

In `schedule()`, add the player/picks computation right before the `return templates.TemplateResponse(...)`:

```python
    matchups = [_game_view(conn, game, teams, predict.predict_game(conn, weights, game)) for game in games]
    if sort == "confidence":
        matchups.sort(key=lambda m: m["confidence_score"] if m["confidence_score"] is not None else 0)

    return templates.TemplateResponse(
```
→
```python
    matchups = [_game_view(conn, game, teams, predict.predict_game(conn, weights, game)) for game in games]
    if sort == "confidence":
        matchups.sort(key=lambda m: m["confidence_score"] if m["confidence_score"] is not None else 0)

    player = _current_player(request, conn)
    picks = db.get_player_picks_for_games(conn, player["id"], [m["game"]["id"] for m in matchups]) if player else {}
    for matchup in matchups:
        matchup["player_pick_team_id"] = picks.get(matchup["game"]["id"])
        matchup["pick_locked"] = _pick_locked(matchup["game"])

    return templates.TemplateResponse(
```

Add the pick submission route at the end of `app/main.py`:

```python
@app.post("/games/{game_id}/pick")
def submit_pick(request: Request, game_id: str, team_id: str = Form(...), week: int = Form(...), conn=Depends(get_db)):
    player = _current_player(request, conn)
    if player is None:
        return RedirectResponse(url=f"/join?next={quote(f'/?week={week}', safe='')}", status_code=303)

    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        raise HTTPException(status_code=404)

    if _pick_locked(game):
        return RedirectResponse(url=f"/?week={week}&pick_error=locked", status_code=303)

    db.upsert_pick(conn, player["id"], game_id, team_id, datetime.now(timezone.utc).isoformat())
    conn.commit()
    return RedirectResponse(url=f"/?week={week}", status_code=303)
```

In `app/templates/index.html`, add a locked-pick banner next to the existing `team_not_found` one:

```html
{% if request.query_params.get('team_not_found') %}
<p class="notice">No team matched that search. Try a city, team name, or abbreviation.</p>
{% endif %}
```
→
```html
{% if request.query_params.get('team_not_found') %}
<p class="notice">No team matched that search. Try a city, team name, or abbreviation.</p>
{% endif %}
{% if request.query_params.get('pick_error') == 'locked' %}
<p class="notice">That game has already started — picks are locked.</p>
{% endif %}
```

Then add the pick UI to each matchup card, right after the `card-footer-row` div and before the `analysis` block:

```html
    <div class="card-footer-row">
      <p class="pick-line"><span>Model edge</span><strong>{{ matchup.winner.name }}</strong></p>
      <div class="badge-group">
        {% if matchup.upset_alert %}
        <span class="upset-badge">⚡ Upset Alert</span>
        {% endif %}
        {% if matchup.reveal %}
        <span class="reveal-badge reveal-{{ 'hit' if matchup.reveal.hit else 'miss' }}">{{ matchup.reveal.label }}</span>
        {% else %}
        <span class="confidence-badge confidence-{{ matchup.confidence_label | lower }}">{{ matchup.confidence_label }} confidence</span>
        {% endif %}
      </div>
    </div>
    {% if matchup.analysis %}
```
→
```html
    <div class="card-footer-row">
      <p class="pick-line"><span>Model edge</span><strong>{{ matchup.winner.name }}</strong></p>
      <div class="badge-group">
        {% if matchup.upset_alert %}
        <span class="upset-badge">⚡ Upset Alert</span>
        {% endif %}
        {% if matchup.reveal %}
        <span class="reveal-badge reveal-{{ 'hit' if matchup.reveal.hit else 'miss' }}">{{ matchup.reveal.label }}</span>
        {% else %}
        <span class="confidence-badge confidence-{{ matchup.confidence_label | lower }}">{{ matchup.confidence_label }} confidence</span>
        {% endif %}
      </div>
    </div>
    <div class="pick-row">
      <span class="pick-row-label">Your pick</span>
      {% if matchup.pick_locked %}
      <span class="pick-locked">
        {% if matchup.player_pick_team_id == matchup.away.id %}{{ matchup.away.abbreviation }}
        {% elif matchup.player_pick_team_id == matchup.home.id %}{{ matchup.home.abbreviation }}
        {% else %}No pick{% endif %}
        {% if matchup.game.status == 'final' and matchup.player_pick_team_id %}
          {% if matchup.actual_winner_team_id is none %}
          <span class="pick-result pick-push">Push</span>
          {% else %}
          <span class="pick-result {{ 'pick-correct' if matchup.player_pick_team_id == matchup.actual_winner_team_id else 'pick-incorrect' }}">{{ '✓' if matchup.player_pick_team_id == matchup.actual_winner_team_id else '✗' }}</span>
          {% endif %}
        {% endif %}
      </span>
      {% else %}
      <div class="pick-buttons">
        <form method="post" action="/games/{{ matchup.game.id }}/pick">
          <input type="hidden" name="team_id" value="{{ matchup.away.id }}">
          <input type="hidden" name="week" value="{{ week }}">
          <button type="submit" class="pick-btn{{ ' pick-btn-active' if matchup.player_pick_team_id == matchup.away.id else '' }}">{{ matchup.away.abbreviation }}</button>
        </form>
        <form method="post" action="/games/{{ matchup.game.id }}/pick">
          <input type="hidden" name="team_id" value="{{ matchup.home.id }}">
          <input type="hidden" name="week" value="{{ week }}">
          <button type="submit" class="pick-btn{{ ' pick-btn-active' if matchup.player_pick_team_id == matchup.home.id else '' }}">{{ matchup.home.abbreviation }}</button>
        </form>
      </div>
      {% endif %}
    </div>
    {% if matchup.analysis %}
```

Append to `app/static/style.css`:

```css
.pick-row { display: flex; justify-content: space-between; align-items: center; gap: .7rem; padding-top: .8rem; margin-top: .6rem; border-top: 1px solid var(--line); } .pick-row-label { color: var(--muted); font-size: .78rem; font-weight: 700; }
.pick-buttons { display: flex; gap: .4rem; } .pick-btn { padding: .35rem .75rem; color: var(--text); font-size: .78rem; font-weight: 800; background: rgba(151, 166, 186, .1); border: 1px solid var(--line); border-radius: 99px; cursor: pointer; } .pick-btn-active { color: #06111c; background: var(--cyan); border-color: var(--cyan); }
.pick-locked { display: flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .82rem; font-weight: 800; }
.pick-result { display: inline-flex; font-weight: 800; } .pick-correct { color: var(--green); } .pick-incorrect { color: var(--rose); } .pick-push { color: var(--amber); }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py tests/test_main.py -v`
Expected: PASS — all tests in both files.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/index.html app/static/style.css tests/test_picks.py
git commit -m "$(cat <<'EOF'
feat: add pick submission and pick UI to the schedule page

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Leaderboard page

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Create: `app/templates/leaderboard.html`
- Modify: `app/static/style.css`
- Test: `tests/test_picks.py`

**Interfaces:**
- Consumes: `db.get_all_players`, `db.get_decided_picks` (Task 1)
- Produces:
  - `_pick_outcome(row) -> str` (`"win"` / `"loss"` / `"push"`)
  - `_build_standings(players, decided_picks) -> list[dict]` (each dict: `player`, `wins`, `losses`, `pushes`, `record` (`str`), `win_pct` (`float | None`), `win_pct_label` (`str`), `rank` (`int`))
  - `_build_weekly_breakdown(players, decided_picks) -> list[dict]` (each dict: `season`, `week`, `players` — a list of `{**player_stat, "player": row}` with `correct`/`total` ints)
  - Route: `GET /leaderboard`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_picks.py`:

```python
def test_leaderboard_shows_standings_and_weekly_breakdown(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    client.post("/games/g1/pick", data={"team_id": "A", "week": 1})

    conn = db.get_connection(tmp_path / "test.db")
    conn.execute("UPDATE games SET status = 'final', home_score = 24, away_score = 17 WHERE id = 'g1'")
    conn.commit()
    conn.close()

    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert "Chris" in response.text
    assert "1-0" in response.text
    assert "1/1" in response.text


def test_leaderboard_treats_tie_game_as_push(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})
    client.post("/games/g1/pick", data={"team_id": "A", "week": 1})

    conn = db.get_connection(tmp_path / "test.db")
    conn.execute("UPDATE games SET status = 'final', home_score = 20, away_score = 20 WHERE id = 'g1'")
    conn.commit()
    conn.close()

    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert "0-0-1" in response.text


def test_leaderboard_shows_joined_player_with_no_decided_picks(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    client.post("/join", data={"name": "Chris", "next": "/"})

    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert "Chris" in response.text
    assert "0-0" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_picks.py -v`
Expected: FAIL — `404` on `GET /leaderboard`.

- [ ] **Step 3: Implement**

Add these helpers and the route at the end of `app/main.py`:

```python
def _pick_outcome(row) -> str:
    picked_home = row["picked_team_id"] == row["home_team_id"]
    picked_score = row["home_score"] if picked_home else row["away_score"]
    opponent_score = row["away_score"] if picked_home else row["home_score"]
    if picked_score > opponent_score:
        return "win"
    if picked_score < opponent_score:
        return "loss"
    return "push"


def _build_standings(players, decided_picks) -> list[dict]:
    records = {p["id"]: {"player": p, "wins": 0, "losses": 0, "pushes": 0} for p in players}
    for row in decided_picks:
        outcome = _pick_outcome(row)
        record = records[row["player_id"]]
        if outcome == "win":
            record["wins"] += 1
        elif outcome == "loss":
            record["losses"] += 1
        else:
            record["pushes"] += 1

    standings = []
    for record in records.values():
        decided = record["wins"] + record["losses"]
        win_pct = record["wins"] / decided if decided else None
        standings.append({
            "player": record["player"],
            "wins": record["wins"],
            "losses": record["losses"],
            "pushes": record["pushes"],
            "record": (
                f"{record['wins']}-{record['losses']}-{record['pushes']}"
                if record["pushes"] else f"{record['wins']}-{record['losses']}"
            ),
            "win_pct": win_pct,
            "win_pct_label": f"{round(win_pct * 100)}%" if win_pct is not None else "—",
        })
    standings.sort(key=lambda s: (s["win_pct"] if s["win_pct"] is not None else -1, s["wins"]), reverse=True)
    for i, standing in enumerate(standings, start=1):
        standing["rank"] = i
    return standings


def _build_weekly_breakdown(players, decided_picks) -> list[dict]:
    weeks: dict[tuple[int, int], dict[int, dict]] = {}
    for row in decided_picks:
        key = (row["season"], row["week"])
        week_bucket = weeks.setdefault(key, {p["id"]: {"correct": 0, "total": 0} for p in players})
        stat = week_bucket[row["player_id"]]
        stat["total"] += 1
        if _pick_outcome(row) == "win":
            stat["correct"] += 1

    breakdown = []
    for (season, week), player_stats in sorted(weeks.items(), reverse=True):
        breakdown.append({
            "season": season,
            "week": week,
            "players": [{"player": p, **player_stats[p["id"]]} for p in players],
        })
    return breakdown


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request, conn=Depends(get_db)):
    players = db.get_all_players(conn)
    decided_picks = db.get_decided_picks(conn)
    standings = _build_standings(players, decided_picks)
    weekly = _build_weekly_breakdown(players, decided_picks)

    return templates.TemplateResponse(
        request, "leaderboard.html", _template_context(request, conn, standings=standings, weekly=weekly)
    )
```

Create `app/templates/leaderboard.html`:

```html
{% extends 'base.html' %}
{% block title %}Leaderboard · Fourth Down Forecast{% endblock %}
{% block content %}
<section class="hero compact-hero">
  <div>
    <p class="eyebrow">Pick'em</p>
    <h1>Leaderboard.</h1>
    <p class="hero-copy">Straight-up winner picks scored against final results. Picks lock at kickoff.</p>
  </div>
</section>

<section class="section-heading"><div><p class="eyebrow">Standings</p><h2>All-time record</h2></div><p>{{ standings | length }} players</p></section>

{% if standings %}
<div class="table-wrap">
  <table>
    <thead>
      <tr><th>Rank</th><th>Player</th><th>Record</th><th>Win %</th></tr>
    </thead>
    <tbody>
      {% for standing in standings %}
      <tr>
        <td>{{ standing.rank }}</td>
        <td>{{ standing.player.name }}</td>
        <td>{{ standing.record }}</td>
        <td>{{ standing.win_pct_label }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<section class="empty-state"><h2>No one has joined the pick'em pool yet.</h2><p>Set your name from any page and start picking games to appear here.</p></section>
{% endif %}

{% if weekly %}
<section class="section-heading"><div><p class="eyebrow">By week</p><h2>Weekly accuracy</h2></div></section>
{% for wk in weekly %}
<div class="table-wrap week-table">
  <table>
    <thead>
      <tr><th colspan="2">{{ wk.season }} · Week {{ wk.week }}</th></tr>
      <tr><th>Player</th><th>Correct</th></tr>
    </thead>
    <tbody>
      {% for entry in wk.players %}
      <tr>
        <td>{{ entry.player.name }}</td>
        <td>{{ entry.correct }}/{{ entry.total }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endfor %}
{% endif %}
{% endblock %}
```

In `app/templates/base.html`, add the "Leaderboard" nav link:

```html
      <nav aria-label="Primary navigation">
        <a href="/">Predictions</a>
        <a href="/rankings">Rankings</a>
        <a href="/accuracy">Accuracy</a>
      </nav>
```
→
```html
      <nav aria-label="Primary navigation">
        <a href="/">Predictions</a>
        <a href="/rankings">Rankings</a>
        <a href="/accuracy">Accuracy</a>
        <a href="/leaderboard">Leaderboard</a>
      </nav>
```

Append to `app/static/style.css`:

```css
.week-table { margin-top: 1rem; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest -v`
Expected: PASS — the entire suite, including every pre-existing test file, plus all of `tests/test_picks.py`.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/base.html app/templates/leaderboard.html app/static/style.css tests/test_picks.py
git commit -m "$(cat <<'EOF'
feat: add pick'em leaderboard with all-time standings and weekly breakdown

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
