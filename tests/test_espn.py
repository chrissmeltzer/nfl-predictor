import json
from pathlib import Path

from app.sources import espn

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_teams():
    raw = load_fixture("espn_teams.json")
    teams = espn.parse_teams(raw)
    assert teams == [
        {"id": "22", "name": "Arizona Cardinals", "abbreviation": "ARI"},
        {"id": "26", "name": "Seattle Seahawks", "abbreviation": "SEA"},
        {"id": "17", "name": "New England Patriots", "abbreviation": "NE"},
    ]


def test_parse_scoreboard_final_game():
    raw = load_fixture("espn_scoreboard.json")
    games = espn.parse_scoreboard(raw)
    final_game = games[0]
    assert final_game["id"] == "401872001"
    assert final_game["status"] == "final"
    assert final_game["home_team_id"] == "26"
    assert final_game["away_team_id"] == "17"
    assert final_game["home_score"] == 27
    assert final_game["away_score"] == 20
    assert final_game["is_outdoor"] is True


def test_parse_scoreboard_scheduled_game_has_no_scores():
    raw = load_fixture("espn_scoreboard.json")
    games = espn.parse_scoreboard(raw)
    scheduled_game = games[1]
    assert scheduled_game["status"] == "scheduled"
    assert scheduled_game["home_score"] is None
    assert scheduled_game["is_outdoor"] is False


def test_parse_injuries():
    raw = load_fixture("espn_injuries_summary.json")
    injuries = espn.parse_injuries(raw)
    assert injuries == [
        {"team_abbreviation": "SEA", "player_name": "Zach Charbonnet", "position": "RB", "status": "Out"},
        {"team_abbreviation": "SEA", "player_name": "Amari Kight", "position": "OT", "status": "Questionable"},
    ]
