from app import db, elo


def seed_game(conn, game_id, season, week, home_id, away_id, home_score, away_score, kickoff_at):
    db.upsert_game(conn, {
        "id": game_id, "season": season, "week": week, "home_team_id": home_id, "away_team_id": away_id,
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": True, "lat": 0, "lon": 0,
        "status": "final", "home_score": home_score, "away_score": away_score,
    })


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
