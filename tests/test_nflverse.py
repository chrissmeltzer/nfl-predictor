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


def test_parse_games_csv_skips_games_with_unresolvable_team():
    games = nflverse.parse_games_csv(load_fixture_csv(), min_season=2024)
    ids = {g["id"] for g in games}
    assert "2024_04_ZZZ_LAR" not in ids


def load_fixture_team_stats_csv():
    return (FIXTURES / "nflverse_team_stats_sample.csv").read_text()


def test_parse_team_stats_csv_filters_by_min_season():
    rows = nflverse.parse_team_stats_csv(load_fixture_team_stats_csv(), min_season=2024)
    seasons = {row["season"] for row in rows}
    assert seasons == {2024}


def test_parse_team_stats_csv_skips_unresolvable_team():
    rows = nflverse.parse_team_stats_csv(load_fixture_team_stats_csv(), min_season=2024)
    abbrs = {row["team_abbreviation"] for row in rows}
    assert "ZZZ" not in abbrs


def test_parse_team_stats_csv_reads_pass_protection_fields():
    rows = nflverse.parse_team_stats_csv(load_fixture_team_stats_csv(), min_season=2024)
    kc_row = next(row for row in rows if row["team_abbreviation"] == "KC")
    assert kc_row["sacks_suffered"] == 2
    assert kc_row["pass_attempts"] == 35
    assert kc_row["def_sacks"] == 3
