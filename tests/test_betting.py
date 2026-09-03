from app import betting


def _matchup(*, status="scheduled", home_score=24.0, away_score=17.0, home_probability=78, away_probability=22):
    return {
        "game": {"status": status},
        "home": {"name": "Home Team"},
        "away": {"name": "Away Team"},
        "home_score": home_score,
        "away_score": away_score,
        "home_probability": home_probability,
        "away_probability": away_probability,
        "confidence_label": "High",
    }


def test_american_odds_pick_em_is_even_both_ways():
    assert betting.american_odds(50) == -100


def test_american_odds_favorite_is_negative():
    assert betting.american_odds(65) == -186


def test_american_odds_underdog_is_positive():
    assert betting.american_odds(25) == 300


def test_build_betting_angles_returns_none_for_final_games():
    assert betting.build_betting_angles(_matchup(status="final"), []) is None


def test_build_betting_angles_favorite_is_home_when_home_score_is_higher():
    result = betting.build_betting_angles(_matchup(), [])

    assert result["moneyline"]["favorite"] == "Home Team"
    assert result["moneyline"]["underdog"] == "Away Team"
    assert result["moneyline"]["favorite_odds"] < 0
    assert result["moneyline"]["underdog_odds"] > 0
    assert result["spread"]["favorite"] == "Home Team"
    assert result["spread"]["margin"] == 7.0
    assert result["total"]["projected"] == 41.0
    assert result["confidence_label"] == "High"


def test_build_betting_angles_favorite_is_away_when_away_score_is_higher():
    matchup = _matchup(home_score=17.0, away_score=24.0, home_probability=22, away_probability=78)
    result = betting.build_betting_angles(matchup, [])

    assert result["moneyline"]["favorite"] == "Away Team"
    assert result["spread"]["favorite"] == "Away Team"


def test_build_betting_angles_history_avg_is_none_without_past_meetings():
    result = betting.build_betting_angles(_matchup(), [])
    assert result["total"]["history_avg"] is None


def test_build_betting_angles_history_avg_averages_combined_scores():
    history = [
        {"home_score": 20, "away_score": 10},
        {"home_score": 30, "away_score": 20},
    ]
    result = betting.build_betting_angles(_matchup(), history)
    assert result["total"]["history_avg"] == 40.0
