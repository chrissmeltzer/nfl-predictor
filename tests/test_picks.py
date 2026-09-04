from datetime import datetime, timezone

from app import db
from tests.test_main import make_test_client


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
