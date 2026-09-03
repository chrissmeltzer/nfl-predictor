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
