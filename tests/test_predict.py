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


def make_conn(dsn):
    conn = db.get_connection(dsn)
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


def test_predict_game_baseline_uses_recent_scoring_and_opponent_defense(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 30, 10, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)

    assert result["predicted_home_score"] > 0
    assert result["predicted_away_score"] > 0
    assert "baseline" in result["breakdown"]["home"]


def test_predict_game_skips_weather_when_no_forecast_row(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 20, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)
    assert result["breakdown"]["home"]["weather"] == 0.0


def test_predict_game_applies_negative_injury_adjustment(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 20, "2026-08-01T00:00Z")
    seed_upcoming(conn)
    conn.execute(
        "INSERT INTO injuries (team_id, player_name, position, status, fetched_at) VALUES (%s, %s, %s, %s, %s)",
        ("A", "Star QB", "QB", "Out", "2026-08-01T00:00Z"),
    )
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)
    assert result["breakdown"]["home"]["injuries"] < 0


def test_recent_scoring_trend_weight_scales_baseline_toward_league_average(pg_url):
    conn = make_conn(pg_url)
    # Team A scores well above the league average (21.0) in its recent games,
    # while Team B has no recorded games, so the home baseline for A is driven
    # purely by A's own scoring average (no opponent-defense averaging).
    db.upsert_team(conn, {"id": "X", "name": "Team X", "abbreviation": "X"})
    seed_game(conn, "h1", "A", "X", 40, 7, "2026-07-01T00:00Z")
    seed_game(conn, "h2", "A", "X", 40, 3, "2026-07-08T00:00Z")
    seed_game(conn, "h3", "A", "X", 40, 10, "2026-07-15T00:00Z")
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    weights_full_trend = {**WEIGHTS, "recent_scoring_trend": 1.0}
    weights_no_trend = {**WEIGHTS, "recent_scoring_trend": 0.0}

    result_full = predict.predict_game(conn, weights_full_trend, game)
    result_none = predict.predict_game(conn, weights_no_trend, game)

    baseline_full = result_full["breakdown"]["home"]["baseline"]
    baseline_none = result_none["breakdown"]["home"]["baseline"]

    # weight=0.0 must collapse the baseline to the league average...
    assert abs(baseline_none - 21.0) < 0.5
    # ...while weight=1.0 must preserve Team A's actual elevated scoring average.
    assert baseline_full > 25


def test_pass_protection_adjustment_penalizes_weak_line_against_strong_pass_rush(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-08-01T00:00Z")
    # Team A's offensive line allows sacks well above the league-average rate...
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 5, "pass_attempts": 25, "def_sacks": 0,
    })
    # ...and Team B's defense generates sacks well above the league-average rate.
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 25, "def_sacks": 5,
    })
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, {**WEIGHTS, "pass_protection": 1.0}, game)
    assert result["breakdown"]["home"]["pass_protection"] < 0


def test_pass_protection_adjustment_is_zero_without_stats_data(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, {**WEIGHTS, "pass_protection": 1.0}, game)
    assert result["breakdown"]["home"]["pass_protection"] == 0.0


def test_confidence_reflects_current_season_sample_not_lifetime_games(pg_url):
    conn = make_conn(pg_url)
    # A full season of history between these two teams, entirely in a prior season -- a
    # lifetime-games-ever-played count would already be saturated before the new season
    # kicks off, which is exactly the bug: confidence should be low in a season with zero
    # current-season games between these teams, regardless of how much old data exists.
    for i in range(8):
        db.upsert_game(conn, {
            "id": f"last{i}", "season": 2025, "week": i + 1, "home_team_id": "A", "away_team_id": "B",
            "kickoff_at": f"2025-09-{i + 1:02d}T00:00Z", "venue_name": "X", "is_outdoor": True,
            "lat": 0, "lon": 0, "status": "final", "home_score": 24, "away_score": 17,
        })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-09-07T00:00Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)

    assert result["confidence_label"] == "Low"


def test_confidence_rises_as_current_season_sample_grows(pg_url):
    conn = make_conn(pg_url)
    for i in range(8):
        db.upsert_game(conn, {
            "id": f"cur{i}", "season": 2026, "week": i + 1, "home_team_id": "A", "away_team_id": "B",
            "kickoff_at": f"2026-09-{i + 1:02d}T00:00Z", "venue_name": "X", "is_outdoor": True,
            "lat": 0, "lon": 0, "status": "final", "home_score": 24, "away_score": 17,
        })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 9, "home_team_id": "A", "away_team_id": "B",
        "kickoff_at": "2026-11-02T00:00Z", "venue_name": "X", "is_outdoor": False,
        "lat": None, "lon": None, "status": "scheduled", "home_score": None, "away_score": None,
    })
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)

    assert result["confidence_label"] == "High"


def test_get_latest_prediction_returns_most_recent_row(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    conn.commit()
    predict.save_prediction(
        conn, "g2",
        {"predicted_home_score": 20.0, "predicted_away_score": 14.0, "breakdown": {"home": {}, "away": {}}},
        WEIGHTS,
    )
    predict.save_prediction(
        conn, "g2",
        {"predicted_home_score": 24.0, "predicted_away_score": 17.0, "breakdown": {"home": {}, "away": {}}},
        WEIGHTS,
    )

    result = predict.get_latest_prediction(conn, "g2")

    assert result["predicted_home_score"] == 24.0
    assert result["predicted_away_score"] == 17.0


def test_get_latest_prediction_returns_none_when_no_prediction_saved(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    conn.commit()

    assert predict.get_latest_prediction(conn, "g2") is None


def test_upset_alert_flags_when_model_favorite_differs_from_elo_favorite(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    # Team A has a big Elo edge, so Elo alone favors A. But seed enough lopsided recent
    # scoring for B (and none for A) that the full model flips to favor B instead.
    db.upsert_team_rating(conn, "A", 1700.0, "2026-08-01T00:00:00+00:00")
    db.upsert_team_rating(conn, "B", 1300.0, "2026-08-01T00:00:00+00:00")
    for i in range(3):
        seed_game(conn, f"b{i}", "B", "A", 45, 3, f"2026-07-0{i + 1}T00:00Z")
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, {**WEIGHTS, "elo": 0.1, "recent_scoring_trend": 2.0}, game)

    assert result["predicted_away_score"] > result["predicted_home_score"]
    assert result["upset_alert"] is True


def test_upset_alert_false_when_model_agrees_with_elo(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    db.upsert_team_rating(conn, "A", 1700.0, "2026-08-01T00:00:00+00:00")
    db.upsert_team_rating(conn, "B", 1300.0, "2026-08-01T00:00:00+00:00")
    conn.commit()
    game = conn.execute("SELECT * FROM games WHERE id = 'g2'").fetchone()

    result = predict.predict_game(conn, WEIGHTS, game)

    assert result["upset_alert"] is False


def test_predict_game_backtest_ignores_finalized_games_that_happened_later(pg_db_factory):
    conn_limited = make_conn(pg_db_factory())
    seed_game(conn_limited, "g0", "A", "B", 10, 24, "2026-08-25T00:00Z")
    seed_game(conn_limited, "target", "A", "B", 20, 17, "2026-09-01T00:00Z")
    conn_limited.commit()
    target_limited = conn_limited.execute("SELECT * FROM games WHERE id = 'target'").fetchone()
    result_limited = predict.predict_game(conn_limited, WEIGHTS, target_limited)

    conn_full = make_conn(pg_db_factory())
    seed_game(conn_full, "g0", "A", "B", 10, 24, "2026-08-25T00:00Z")
    seed_game(conn_full, "target", "A", "B", 20, 17, "2026-09-01T00:00Z")
    # A lopsided game that happens *after* the target game -- must not leak into its prediction.
    seed_game(conn_full, "g2", "A", "B", 55, 0, "2026-09-08T00:00Z")
    conn_full.commit()
    target_full = conn_full.execute("SELECT * FROM games WHERE id = 'target'").fetchone()
    result_full = predict.predict_game(conn_full, WEIGHTS, target_full)

    assert result_limited["predicted_home_score"] == result_full["predicted_home_score"]
    assert result_limited["predicted_away_score"] == result_full["predicted_away_score"]


def test_save_prediction_persists_row(pg_url):
    conn = make_conn(pg_url)
    seed_upcoming(conn)
    conn.commit()

    result = {"predicted_home_score": 24.0, "predicted_away_score": 17.0, "breakdown": {"home": {}, "away": {}}}
    predict.save_prediction(conn, "g2", result, WEIGHTS)

    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g2'").fetchone()
    assert row["predicted_home_score"] == 24.0
