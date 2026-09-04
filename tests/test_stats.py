from app import db, stats


def seed_game(conn, game_id, home_id, away_id, home_score, away_score, kickoff_at, status="final"):
    db.upsert_game(conn, {
        "id": game_id, "season": 2026, "week": 1, "home_team_id": home_id, "away_team_id": away_id,
        "kickoff_at": kickoff_at, "venue_name": "X", "is_outdoor": True, "lat": 0, "lon": 0,
        "status": status, "home_score": home_score, "away_score": away_score,
    })


def make_conn(dsn):
    conn = db.get_connection(dsn)
    db.init_db(conn)
    db.upsert_team(conn, {"id": "A", "name": "Team A", "abbreviation": "A"})
    db.upsert_team(conn, {"id": "B", "name": "Team B", "abbreviation": "B"})
    db.upsert_team(conn, {"id": "C", "name": "Team C", "abbreviation": "C"})
    conn.commit()
    return conn


def test_recent_scoring_stats_averages_last_n_games(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 10, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "B", "A", 14, 30, "2026-09-08T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=8)
    assert result["avg_points_scored"] == (20 + 30) / 2
    assert result["avg_points_allowed"] == (10 + 14) / 2
    assert result["games_counted"] == 2


def test_recent_scoring_stats_respects_window(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 100, 0, "2026-08-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 10, 0, "2026-09-01T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=1)
    assert result["avg_points_scored"] == 10
    assert result["games_counted"] == 1


def test_recent_scoring_stats_window_zero_returns_no_games(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 10, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "B", "A", 14, 30, "2026-09-08T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=0)
    assert result["avg_points_scored"] is None
    assert result["avg_points_allowed"] is None
    assert result["games_counted"] == 0


def test_home_away_split(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 30, 10, "2026-09-01T00:00Z")  # A home
    seed_game(conn, "g2", "B", "A", 10, 10, "2026-09-08T00:00Z")  # A away
    conn.commit()

    result = stats.home_away_split(conn, "A")
    assert result["home_avg"] == 30
    assert result["away_avg"] == 10
    assert result["overall_avg"] == 20


def test_head_to_head_only_counts_matchups_between_the_two_teams(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 21, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "C", 40, 3, "2026-09-08T00:00Z")
    conn.commit()

    result = stats.head_to_head(conn, "A", "B")
    assert result["meetings"] == 1
    assert result["avg_points_scored"] == 21


def test_rest_days_computed_from_previous_game(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 21, 17, "2026-09-01T00:00Z")
    conn.commit()

    days = stats.rest_days(conn, "A", before="2026-09-08T00:00Z")
    assert days == 7


def test_recent_scoring_stats_ignores_games_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 10, 0, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 100, 0, "2026-10-01T00:00Z")
    conn.commit()

    result = stats.recent_scoring_stats(conn, "A", window=8, before="2026-09-15T00:00Z")
    assert result["avg_points_scored"] == 10
    assert result["games_counted"] == 1


def test_recency_weighted_scoring_ignores_games_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 10, 0, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 100, 0, "2026-10-01T00:00Z")
    conn.commit()

    result = stats.recency_weighted_scoring(conn, "A", window=8, before="2026-09-15T00:00Z")
    assert result["avg_points_scored"] == 10


def test_home_away_split_ignores_games_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 30, 10, "2026-09-01T00:00Z")  # A home
    seed_game(conn, "g2", "B", "A", 5, 5, "2026-10-01T00:00Z")  # after cutoff, A away

    conn.commit()

    result = stats.home_away_split(conn, "A", before="2026-09-15T00:00Z")
    assert result["home_avg"] == 30
    assert result["away_avg"] == 30  # no away games before cutoff -> falls back to overall_avg


def test_head_to_head_ignores_meetings_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 21, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 100, 0, "2026-10-01T00:00Z")
    conn.commit()

    result = stats.head_to_head(conn, "A", "B", before="2026-09-15T00:00Z")
    assert result["meetings"] == 1
    assert result["avg_points_scored"] == 21


def test_head_to_head_games_null_kickoff_does_not_crowd_out_dated_game(pg_url):
    # Regression test: Postgres sorts NULL kickoff_at FIRST on ORDER BY ... DESC (the
    # opposite of SQLite), so a naive "ORDER BY kickoff_at DESC ... LIMIT" would let the
    # NULL-kickoff backfilled games (see app/sources/nflverse.py) fill the limit window
    # and push out real, dated meetings entirely. With "NULLS LAST" the dated game must
    # always survive a small limit even when outnumbered by NULL-kickoff games.
    conn = make_conn(pg_url)
    seed_game(conn, "g_null1", "A", "B", 10, 7, None)
    seed_game(conn, "g_null2", "A", "B", 14, 21, None)
    seed_game(conn, "g_null3", "A", "B", 17, 20, None)
    seed_game(conn, "g_dated", "A", "B", 24, 23, "2026-09-01T00:00Z")
    conn.commit()

    meetings = stats.head_to_head_games(conn, "A", "B", limit=2)

    assert len(meetings) == 2
    kickoffs = [m["kickoff_at"] for m in meetings]
    assert "2026-09-01T00:00Z" in kickoffs
    # The dated game sorts before the NULL-kickoff games on the underlying
    # "ORDER BY kickoff_at DESC NULLS LAST" query, so after head_to_head_games()
    # reverses that window for chronological display, it lands last.
    assert meetings[-1]["kickoff_at"] == "2026-09-01T00:00Z"


def test_current_season_sample_size_ignores_games_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    for i in range(8):
        seed_game(conn, f"g{i}", "A", "B", 20, 10, f"2026-09-{i + 1:02d}T00:00Z")
    conn.commit()

    count = stats.current_season_sample_size(conn, "A", season=2026, window=8, before="2026-09-05T00:00Z")
    assert count == 4


def seed_team_game_stat(conn, team_id, season, week, sacks_suffered, pass_attempts, def_sacks):
    db.upsert_team_game_stat(conn, {
        "team_id": team_id, "season": season, "week": week, "turnovers": 0,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": sacks_suffered, "pass_attempts": pass_attempts, "def_sacks": def_sacks,
    })


def test_pass_rush_form_computes_protection_and_pressure_rates(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-09-01T00:00Z")
    seed_team_game_stat(conn, "A", 2026, 1, sacks_suffered=2, pass_attempts=30, def_sacks=1)
    seed_team_game_stat(conn, "B", 2026, 1, sacks_suffered=1, pass_attempts=28, def_sacks=3)
    conn.commit()

    result = stats.pass_rush_form(conn, "A", window=8)
    assert result["protection_rate"] == 2 / 32
    assert result["pressure_rate"] == 1 / 29


def test_pass_rush_form_returns_none_when_no_data(pg_url):
    conn = make_conn(pg_url)

    result = stats.pass_rush_form(conn, "A", window=8)
    assert result["protection_rate"] is None
    assert result["pressure_rate"] is None


def test_pass_rush_form_ignores_stats_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-09-01T00:00Z")
    seed_team_game_stat(conn, "A", 2026, 1, sacks_suffered=2, pass_attempts=30, def_sacks=1)
    seed_team_game_stat(conn, "B", 2026, 1, sacks_suffered=1, pass_attempts=28, def_sacks=3)
    # week 2 stats should be excluded when backtesting as of week 2
    seed_game(conn, "g2", "A", "B", 10, 10, "2026-09-08T00:00Z")
    seed_team_game_stat(conn, "A", 2026, 2, sacks_suffered=10, pass_attempts=10, def_sacks=0)
    seed_team_game_stat(conn, "B", 2026, 2, sacks_suffered=10, pass_attempts=10, def_sacks=0)
    conn.commit()

    result = stats.pass_rush_form(conn, "A", window=8, before=(2026, 2))
    assert result["protection_rate"] == 2 / 32
    assert result["pressure_rate"] == 1 / 29


def test_turnover_form_ignores_stats_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 1, "turnovers": 1,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 2, "turnovers": 5,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    conn.commit()

    result = stats.turnover_form(conn, "A", window=8, before=(2026, 2))
    assert result["avg_turnovers_committed"] == 1


def test_turnovers_forced_ignores_stats_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 10, 10, "2026-09-08T00:00Z")
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 1, "turnovers": 2,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 2, "turnovers": 9,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    conn.commit()

    result = stats.turnovers_forced(conn, "A", window=8, before=(2026, 2))
    assert result["avg_turnovers_forced"] == 2


def test_epa_form_ignores_stats_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 10, 10, "2026-09-08T00:00Z")
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": 0.1, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 2, "turnovers": 0,
        "epa_offense": 0.9, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": -0.2, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 2, "turnovers": 0,
        "epa_offense": -0.8, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    conn.commit()

    result = stats.epa_form(conn, "A", window=8, before=(2026, 2))
    assert result["epa_offense_avg"] == 0.1
    assert result["epa_allowed_avg"] == -0.2


def test_pace_form_ignores_stats_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": 60,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "A", "season": 2026, "week": 2, "turnovers": 0,
        "epa_offense": None, "epa_passing": None, "epa_rushing": None, "plays": 100,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    conn.commit()

    result = stats.pace_form(conn, "A", window=8, before=(2026, 2))
    assert result == 60


def test_strength_of_schedule_ignores_opponent_games_after_cutoff(pg_url):
    conn = make_conn(pg_url)
    seed_game(conn, "g1", "A", "B", 20, 17, "2026-09-01T00:00Z")
    seed_game(conn, "g2", "A", "B", 10, 10, "2026-09-08T00:00Z")
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 1, "turnovers": 0,
        "epa_offense": 0.2, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    db.upsert_team_game_stat(conn, {
        "team_id": "B", "season": 2026, "week": 2, "turnovers": 0,
        "epa_offense": 0.9, "epa_passing": None, "epa_rushing": None, "plays": None,
        "sacks_suffered": 0, "pass_attempts": 0, "def_sacks": 0,
    })
    conn.commit()

    result = stats.strength_of_schedule(conn, "A", window=8, before=(2026, 2))
    assert result["opponent_epa_avg"] == 0.2


def test_remaining_strength_of_schedule_ranks_easiest_first(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "D", "name": "Team D", "abbreviation": "D"})
    db.upsert_team(conn, {"id": "E", "name": "Team E", "abbreviation": "E"})
    db.upsert_team(conn, {"id": "F", "name": "Team F", "abbreviation": "F"})

    # A goes 1-0 (win_pct 1.0), B goes 0-1 (win_pct 0.0), C ties (win_pct 0.5).
    seed_game(conn, "g_a", "A", "F", 20, 10, "2026-09-01T00:00Z")
    seed_game(conn, "g_b", "B", "F", 10, 20, "2026-09-01T00:00Z")
    seed_game(conn, "g_c", "C", "F", 15, 15, "2026-09-01T00:00Z")

    # D's remaining schedule is A and B (avg opponent win_pct 0.5) — easier than E's.
    seed_game(conn, "g_d1", "D", "A", None, None, "2026-09-15T00:00Z", status="scheduled")
    seed_game(conn, "g_d2", "D", "B", None, None, "2026-09-22T00:00Z", status="scheduled")
    # E's remaining schedule is just A (avg opponent win_pct 1.0) — the toughest.
    seed_game(conn, "g_e1", "E", "A", None, None, "2026-09-15T00:00Z", status="scheduled")
    conn.commit()

    result = stats.remaining_strength_of_schedule(conn, season=2026)

    assert result["D"] == {"opponent_win_pct": 0.5, "rank": 1, "teams_ranked": 2}
    assert result["E"] == {"opponent_win_pct": 1.0, "rank": 2, "teams_ranked": 2}
    assert "A" not in result  # A has no remaining games of its own this season


def test_remaining_strength_of_schedule_excludes_team_with_no_opponent_data(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "D", "name": "Team D", "abbreviation": "D"})
    db.upsert_team(conn, {"id": "E", "name": "Team E", "abbreviation": "E"})
    seed_game(conn, "g_de", "D", "E", None, None, "2026-09-15T00:00Z", status="scheduled")
    conn.commit()

    result = stats.remaining_strength_of_schedule(conn, season=2026)

    assert result == {}
