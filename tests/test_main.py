from datetime import datetime, timedelta, timezone

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


def test_schedule_page_lists_games_for_current_week(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert "Team A" in response.text
    assert "Team B" in response.text


def test_schedule_page_sorts_by_confidence_when_requested(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_team(conn, {"id": "C", "name": "Team C", "abbreviation": "C"})
    db.upsert_team(conn, {"id": "D", "name": "Team D", "abbreviation": "D"})
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "C", "away_team_id": "D",
        "kickoff_at": "2026-09-10T04:20Z", "venue_name": "Y", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })
    conn.commit()
    conn.close()

    def fake_predict(conn, weights, game):
        confidence = 90 if game["id"] == "g1" else 30
        return {
            "predicted_home_score": 24.0, "predicted_away_score": 17.0,
            "confidence_score": confidence, "confidence_label": "High" if confidence == 90 else "Low",
            "breakdown": {"home": {}, "away": {}}, "upset_alert": False,
        }

    monkeypatch.setattr(main.predict, "predict_game", fake_predict)

    response = client.get("/?sort=confidence")

    assert response.status_code == 200
    assert response.text.index('href="/games/g2"') < response.text.index('href="/games/g1"')


def test_game_detail_page_shows_breakdown_and_saves_prediction(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/games/g1")
    assert response.status_code == 200
    assert "Team A" in response.text

    conn = db.get_connection(tmp_path / "test.db")
    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g1'").fetchone()
    assert row is not None


def _canned_prediction(upset_alert: bool):
    return lambda conn, weights, game: {
        "predicted_home_score": 17.0, "predicted_away_score": 24.0,
        "confidence_score": 50, "confidence_label": "Moderate",
        "breakdown": {"home": {}, "away": {}}, "upset_alert": upset_alert,
    }


def test_schedule_page_shows_upset_alert_badge_when_flagged(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main.predict, "predict_game", _canned_prediction(True))

    response = client.get("/")

    assert response.status_code == 200
    assert "Upset Alert" in response.text


def test_schedule_page_hides_upset_alert_badge_when_not_flagged(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main.predict, "predict_game", _canned_prediction(False))

    response = client.get("/")

    assert response.status_code == 200
    assert "Upset Alert" not in response.text


def test_game_detail_final_game_shows_nailed_it_for_close_prediction(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.execute(
        "INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score, "
        "factor_breakdown_json, weights_snapshot_json, created_at) VALUES (?, ?, ?, '{}', '{}', ?)",
        ("g_final", 23, 16, "2026-09-09T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    response = client.get("/games/g_final")

    assert response.status_code == 200
    assert "Nailed it" in response.text


def test_game_detail_final_game_shows_missed_by_for_large_error(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.execute(
        "INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score, "
        "factor_breakdown_json, weights_snapshot_json, created_at) VALUES (?, ?, ?, '{}', '{}', ?)",
        ("g_final", 10, 40, "2026-09-09T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    response = client.get("/games/g_final")

    assert response.status_code == 200
    assert "Missed by" in response.text


def test_game_detail_final_game_without_saved_prediction_has_no_reveal_badge(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.commit()
    conn.close()

    response = client.get("/games/g_final")

    assert response.status_code == 200
    assert "Nailed it" not in response.text
    assert "Missed by" not in response.text


def test_game_detail_upcoming_game_shows_betting_angles(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/games/g1")

    assert response.status_code == 200
    assert "Betting angles" in response.text
    assert "Moneyline" in response.text


def test_game_detail_final_game_hides_betting_angles(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.commit()
    conn.close()

    response = client.get("/games/g_final")

    assert response.status_code == 200
    assert "Betting angles" not in response.text


def test_team_detail_shows_recent_pick_accuracy(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-03T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.execute(
        "INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score, "
        "factor_breakdown_json, weights_snapshot_json, created_at) VALUES (?, ?, ?, '{}', '{}', ?)",
        ("g_final", 21, 20, "2026-09-02T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    response = client.get("/teams/A")

    assert response.status_code == 200
    assert "1/1" in response.text


def test_team_detail_hides_accuracy_stat_with_no_final_predicted_games(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    response = client.get("/teams/A")

    assert response.status_code == 200
    assert "Model accuracy" not in response.text


def test_rankings_page_lists_teams_sorted_by_elo_desc(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_team_rating(conn, "A", 1600.0, "2026-08-01T00:00:00+00:00")
    db.upsert_team_rating(conn, "B", 1400.0, "2026-08-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    response = client.get("/rankings")

    assert response.status_code == 200
    assert response.text.index("Team A") < response.text.index("Team B")


def test_rankings_page_defaults_unrated_teams_to_base_rating(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    response = client.get("/rankings")

    assert response.status_code == 200
    assert "1500" in response.text


def test_accuracy_page_loads_with_no_predictions_yet(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/accuracy")
    assert response.status_code == 200


def test_game_detail_404_for_nonexistent_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    response = client.get("/games/nonexistent-id")
    assert response.status_code == 404


def test_game_detail_does_not_save_prediction_for_final_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_game(conn, {
        "id": "g_final", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 24, "away_score": 17,
    })
    conn.commit()
    conn.close()

    response = client.get("/games/g_final")
    assert response.status_code == 200

    conn = db.get_connection(tmp_path / "test.db")
    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g_final'").fetchone()
    conn.close()
    assert row is None


def test_accuracy_dedupes_multiple_predictions_for_same_game(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    # Two page views of the same scheduled game log two prediction rows.
    client.get("/games/g1")
    client.get("/games/g1")

    conn = db.get_connection(tmp_path / "test.db")
    pred_count = conn.execute(
        "SELECT COUNT(*) as c FROM predictions WHERE game_id = 'g1'"
    ).fetchone()["c"]
    assert pred_count == 2

    conn.execute("UPDATE games SET status = 'final', home_score = 24, away_score = 17 WHERE id = 'g1'")
    conn.commit()
    conn.close()

    response = client.get("/accuracy")
    assert response.status_code == 200
    # Exactly one row should be rendered for g1 despite the two saved predictions.
    assert response.text.count("B @ A") == 1


def test_accuracy_computes_mean_errors_correctly(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    conn = db.get_connection(tmp_path / "test.db")
    db.upsert_team(conn, {"id": "C", "name": "Team C", "abbreviation": "C"})
    db.upsert_team(conn, {"id": "D", "name": "Team D", "abbreviation": "D"})
    db.upsert_game(conn, {
        "id": "g_acc1", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 21, "away_score": 20,
    })
    db.upsert_game(conn, {
        "id": "g_acc2", "season": 2026, "week": 1, "home_team_id": "C", "away_team_id": "D",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Y", "is_outdoor": False,
        "lat": None, "lon": None, "status": "final", "home_score": 30, "away_score": 10,
    })
    # game 1: predicted home 24, away 17 -> predicted margin +7, actual margin +1 -> margin_error 6
    #         predicted total 41, actual total 41 -> total_error 0
    conn.execute(
        "INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score, "
        "factor_breakdown_json, weights_snapshot_json, created_at) VALUES (?, ?, ?, '{}', '{}', ?)",
        ("g_acc1", 24, 17, "2026-09-09T00:00:00+00:00"),
    )
    # game 2: predicted home 28, away 14 -> predicted margin +14, actual margin +20 -> margin_error 6
    #         predicted total 42, actual total 40 -> total_error 2
    conn.execute(
        "INSERT INTO predictions (game_id, predicted_home_score, predicted_away_score, "
        "factor_breakdown_json, weights_snapshot_json, created_at) VALUES (?, ?, ?, '{}', '{}', ?)",
        ("g_acc2", 28, 14, "2026-09-09T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    response = client.get("/accuracy")
    assert response.status_code == 200
    assert "<span>Mean margin error</span><strong>6.0</strong>" in response.text
    assert "<span>Mean total error</span><strong>1.0</strong>" in response.text


def test_is_stale_true_when_no_meta_row(tmp_path):
    conn = db.get_connection(tmp_path / "stale.db")
    db.init_db(conn)
    assert main._is_stale(conn) is True


def test_is_stale_false_when_recently_synced(tmp_path):
    conn = db.get_connection(tmp_path / "stale.db")
    db.init_db(conn)
    db.set_meta(conn, "last_synced_at", datetime.now(timezone.utc).isoformat())
    conn.commit()
    assert main._is_stale(conn) is False


def test_is_stale_true_when_synced_long_ago(tmp_path):
    conn = db.get_connection(tmp_path / "stale.db")
    db.init_db(conn)
    old = datetime.now(timezone.utc) - timedelta(hours=main.STALENESS_HOURS + 1)
    db.set_meta(conn, "last_synced_at", old.isoformat())
    conn.commit()
    assert main._is_stale(conn) is True
