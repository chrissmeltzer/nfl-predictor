from pathlib import Path

from app.sources import nflverse

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_csv():
    return (FIXTURES / "nflverse_games_sample.csv").read_text()


def test_parse_games_csv_filters_by_min_season():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    seasons = {g["season"] for g in games}
    assert seasons == {2024}


def test_parse_games_csv_resolves_team_aliases():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    oak_game = next(g for g in games if g["id"] == "2024_02_OAK_KC")
    assert oak_game["away_abbreviation"] == "LV"


def test_parse_games_csv_reads_roof_and_scores():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    dome_game = next(g for g in games if g["id"] == "2024_01_ARI_MIN")
    assert dome_game["is_outdoor"] is False
    assert dome_game["home_score"] == 24
    assert dome_game["away_score"] == 20


def test_parse_games_csv_skips_games_without_final_scores():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    ids = {g["id"] for g in games}
    assert "2024_03_BUF_NE" not in ids
