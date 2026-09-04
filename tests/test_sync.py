# tests/test_sync.py
from datetime import datetime, timezone

import httpx

from app import db, sync


def make_conn(dsn):
    conn = db.get_connection(dsn)
    db.init_db(conn)
    return conn


def test_sync_teams_inserts_rows(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    monkeypatch.setattr(
        sync.espn, "fetch_teams",
        lambda client: [{"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"}],
    )
    sync.sync_teams(conn, client=None)
    row = conn.execute("SELECT * FROM teams WHERE id = '26'").fetchone()
    assert row["abbreviation"] == "SEA"


def test_sync_historical_resolves_team_ids_and_stadium_coords(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    conn.commit()

    monkeypatch.setattr(
        sync.nflverse, "fetch_games_csv",
        lambda client, min_season: [{
            "id": "2024_01_NE_SEA", "season": 2024, "week": 1,
            "home_abbreviation": "SEA", "away_abbreviation": "NE",
            "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
            "status": "final", "home_score": 27, "away_score": 20,
        }],
    )
    sync.sync_historical(conn, client=None, min_season=2024)

    row = conn.execute("SELECT * FROM games WHERE id = '2024_01_NE_SEA'").fetchone()
    assert row["home_team_id"] == "26"
    assert row["lat"] == 47.5952


def test_sync_historical_max_season_excludes_rows_at_or_above_bound(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    conn.commit()

    monkeypatch.setattr(
        sync.nflverse, "fetch_games_csv",
        lambda client, min_season: [
            {
                "id": "2024_01_NE_SEA", "season": 2024, "week": 1,
                "home_abbreviation": "SEA", "away_abbreviation": "NE",
                "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
                "status": "final", "home_score": 27, "away_score": 20,
            },
            {
                "id": "2026_01_NE_SEA", "season": 2026, "week": 1,
                "home_abbreviation": "SEA", "away_abbreviation": "NE",
                "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
                "status": "final", "home_score": 30, "away_score": 24,
            },
        ],
    )
    sync.sync_historical(conn, client=None, min_season=2024, max_season=2026)

    old_row = conn.execute("SELECT * FROM games WHERE id = '2024_01_NE_SEA'").fetchone()
    assert old_row is not None
    current_row = conn.execute("SELECT * FROM games WHERE id = '2026_01_NE_SEA'").fetchone()
    assert current_row is None


def test_sync_historical_skips_unknown_team(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    monkeypatch.setattr(
        sync.nflverse, "fetch_games_csv",
        lambda client, min_season: [{
            "id": "2024_01_XXX_SEA", "season": 2024, "week": 1,
            "home_abbreviation": "SEA", "away_abbreviation": "XXX",
            "kickoff_at": None, "venue_name": "Lumen Field", "is_outdoor": True,
            "status": "final", "home_score": 27, "away_score": 20,
        }],
    )
    sync.sync_historical(conn, client=None, min_season=2024)
    row = conn.execute("SELECT * FROM games WHERE id = '2024_01_XXX_SEA'").fetchone()
    assert row is None


def test_sync_weather_for_upcoming_only_fetches_outdoor_scheduled_games(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "17", "away_team_id": "26",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Dome", "is_outdoor": 0,
        "lat": 1.0, "lon": 1.0, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    calls = []

    def fake_fetch_forecast(client, lat, lon, target_time):
        calls.append((lat, lon))
        return {"temp_f": 60, "wind_mph": 5, "precip_pct": 10}

    monkeypatch.setattr(sync.weather, "fetch_forecast", fake_fetch_forecast)
    sync.sync_weather_for_upcoming(conn, client=None)

    assert calls == [(47.5952, -122.3316)]
    row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g1'").fetchone()
    assert row["temp_f"] == 60


def test_sync_weather_for_upcoming_continues_past_one_failure(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "17", "away_team_id": "26",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Gillette Stadium", "is_outdoor": 1,
        "lat": 42.0909, "lon": -71.2643, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    def fake_fetch_forecast(client, lat, lon, target_time):
        if lat == 47.5952:
            raise httpx.HTTPError("boom")
        return {"temp_f": 55, "wind_mph": 8, "precip_pct": 20}

    monkeypatch.setattr(sync.weather, "fetch_forecast", fake_fetch_forecast)

    sync.sync_weather_for_upcoming(conn, client=None)

    failed_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g1'").fetchone()
    assert failed_row is None
    ok_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g2'").fetchone()
    assert ok_row["temp_f"] == 55


def test_sync_weather_for_upcoming_continues_past_missing_forecast_data(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 1, "home_team_id": "17", "away_team_id": "26",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Gillette Stadium", "is_outdoor": 1,
        "lat": 42.0909, "lon": -71.2643, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    def fake_fetch_forecast(client, lat, lon, target_time):
        if lat == 47.5952:
            raise ValueError("no hourly forecast data available")
        return {"temp_f": 55, "wind_mph": 5, "precip_pct": 10}

    monkeypatch.setattr(sync.weather, "fetch_forecast", fake_fetch_forecast)

    sync.sync_weather_for_upcoming(conn, client=None)

    failed_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g1'").fetchone()
    assert failed_row is None
    ok_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = 'g2'").fetchone()
    assert ok_row["temp_f"] == 55


def test_sync_predictions_saves_a_row_for_each_scheduled_game(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    sync.sync_predictions(conn, weights={"recent_games_window": 8})

    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g1'").fetchone()
    assert row is not None


def test_sync_predictions_skips_final_games(pg_url):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "final",
        "home_score": 24, "away_score": 17,
    })
    conn.commit()

    sync.sync_predictions(conn, weights={"recent_games_window": 8})

    row = conn.execute("SELECT * FROM predictions WHERE game_id = 'g1'").fetchone()
    assert row is None


def test_sync_injuries_for_upcoming_continues_past_one_failure(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    db.upsert_game(conn, {
        "id": "g2", "season": 2026, "week": 2, "home_team_id": "17", "away_team_id": "26",
        "kickoff_at": "2026-09-17T00:20Z", "venue_name": "Gillette Stadium", "is_outdoor": 1,
        "lat": 42.0909, "lon": -71.2643, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()

    def fake_fetch_game_summary(client, event_id):
        if event_id == "g1":
            raise httpx.HTTPError("boom")
        return {
            "injuries": [
                {
                    "team": {"abbreviation": "NE"},
                    "injuries": [
                        {
                            "athlete": {"displayName": "Test Player", "position": {"abbreviation": "WR"}},
                            "status": "Questionable",
                        },
                    ],
                },
            ],
        }

    monkeypatch.setattr(sync.espn, "fetch_game_summary", fake_fetch_game_summary)

    sync.sync_injuries_for_upcoming(conn, client=None)

    row = conn.execute("SELECT * FROM injuries WHERE team_id = '17'").fetchone()
    assert row["player_name"] == "Test Player"
    assert row["status"] == "Questionable"


def test_sync_injuries_for_upcoming_clears_team_with_no_current_injuries(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    db.upsert_game(conn, {
        "id": "g1", "season": 2026, "week": 1, "home_team_id": "26", "away_team_id": "17",
        "kickoff_at": "2026-09-10T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
        "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
        "home_score": None, "away_score": None,
    })
    conn.commit()
    # Pre-seed a stale injury row for SEA (team_id "26") from a prior sync.
    db.replace_team_injuries(
        conn, "26",
        [{"player_name": "Old Injury", "position": "WR", "status": "Out"}],
        "2026-08-01T00:00:00+00:00",
    )
    conn.commit()

    def fake_fetch_game_summary(client, event_id):
        # Both teams present in the summary's team blocks, but SEA has fully recovered.
        return {
            "injuries": [
                {"team": {"abbreviation": "SEA"}, "injuries": []},
                {
                    "team": {"abbreviation": "NE"},
                    "injuries": [
                        {
                            "athlete": {"displayName": "Test Player", "position": {"abbreviation": "WR"}},
                            "status": "Questionable",
                        },
                    ],
                },
            ],
        }

    monkeypatch.setattr(sync.espn, "fetch_game_summary", fake_fetch_game_summary)

    sync.sync_injuries_for_upcoming(conn, client=None)

    sea_rows = conn.execute("SELECT * FROM injuries WHERE team_id = '26'").fetchall()
    assert sea_rows == []
    ne_row = conn.execute("SELECT * FROM injuries WHERE team_id = '17'").fetchone()
    assert ne_row["player_name"] == "Test Player"


def test_sync_injuries_for_upcoming_stops_once_all_teams_seen(pg_url, monkeypatch):
    conn = make_conn(pg_url)
    db.upsert_team(conn, {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"})
    db.upsert_team(conn, {"id": "17", "name": "New England Patriots", "abbreviation": "NE"})
    for i, gid in enumerate(["g1", "g2", "g3"]):
        db.upsert_game(conn, {
            "id": gid, "season": 2026, "week": i + 1, "home_team_id": "26", "away_team_id": "17",
            "kickoff_at": f"2026-09-{10 + i}T00:20Z", "venue_name": "Lumen Field", "is_outdoor": 1,
            "lat": 47.5952, "lon": -122.3316, "status": "scheduled",
            "home_score": None, "away_score": None,
        })
    conn.commit()

    calls = []

    def fake_fetch_game_summary(client, event_id):
        calls.append(event_id)
        return {
            "injuries": [
                {"team": {"abbreviation": "SEA"}, "injuries": []},
                {"team": {"abbreviation": "NE"}, "injuries": []},
            ],
        }

    monkeypatch.setattr(sync.espn, "fetch_game_summary", fake_fetch_game_summary)

    sync.sync_injuries_for_upcoming(conn, client=None)

    # Only the total-team-count of games (2 total teams) is fully covered by g1;
    # g3 should never be fetched since both teams were already seen after g1.
    assert "g3" not in calls
    assert calls == ["g1"]
