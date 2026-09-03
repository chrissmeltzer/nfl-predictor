# tests/test_sync.py
from datetime import datetime, timezone

import httpx

from app import db, sync


def make_conn(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    db.init_db(conn)
    return conn


def test_sync_teams_inserts_rows(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        sync.espn, "fetch_teams",
        lambda client: [{"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"}],
    )
    sync.sync_teams(conn, client=None)
    row = conn.execute("SELECT * FROM teams WHERE id = '26'").fetchone()
    assert row["abbreviation"] == "SEA"


def test_sync_historical_resolves_team_ids_and_stadium_coords(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
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


def test_sync_historical_skips_unknown_team(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
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


def test_sync_weather_for_upcoming_only_fetches_outdoor_scheduled_games(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
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


def test_sync_weather_for_upcoming_continues_past_one_failure(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
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


def test_sync_injuries_for_upcoming_continues_past_one_failure(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
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
