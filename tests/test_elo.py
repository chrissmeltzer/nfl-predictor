from app import db, elo


def seed_game(conn, game_id, season, week, home_id, away_id, home_score, away_score, kickoff_at):
    db.upsert_game(conn, {
        "id": game_id, "season": season, "week": week, "home_team_id": home_id, "away_team_id": away_id,
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": True, "lat": 0, "lon": 0,
        "status": "final", "home_score": home_score, "away_score": away_score,
    })


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


def test_rating_timeline_matches_recompute_ratings_at_every_cutoff(pg_db_factory):
    """rating_timeline() replays history once; ratings_before() must still agree with the
    (slower) full replay recompute_ratings(before=X) does for every possible cutoff -- including
    across the season boundary, where ratings regress toward the mean."""
    conn = make_conn(pg_db_factory())
    seed_game(conn, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", 2026, 2, "A", "B", 20, 20, "2026-09-08T00:00Z")
    seed_game(conn, "g3", 2027, 1, "A", "B", 10, 30, "2027-09-01T00:00Z")
    conn.commit()

    timeline = elo.rating_timeline(conn)

    for before in [(2025, 1), (2026, 1), (2026, 2), (2026, 3), (2027, 1), (2027, 2)]:
        assert elo.ratings_before(timeline, before) == elo.recompute_ratings(conn, before=before)


def test_rating_timeline_before_any_game_is_base_rating(pg_db_factory):
    conn = make_conn(pg_db_factory())
    seed_game(conn, "g1", 2026, 1, "A", "B", 24, 17, "2026-09-01T00:00Z")
    conn.commit()

    timeline = elo.rating_timeline(conn)

    assert elo.ratings_before(timeline, (2026, 1)) == {"A": elo.BASE_RATING, "B": elo.BASE_RATING}
