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
