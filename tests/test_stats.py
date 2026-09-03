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
