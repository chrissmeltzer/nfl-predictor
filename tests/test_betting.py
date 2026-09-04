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


def _week_matchup(
    *, status="scheduled", game_id="g1", home_probability=80, away_probability=20,
    confidence_label="High", home_name="Home Team", away_name="Away Team",
):
    return {
        "game": {"status": status, "id": game_id, "week": 1},
        "home": {"name": home_name}, "away": {"name": away_name},
        "home_probability": home_probability, "away_probability": away_probability,
        "home_logo": "home.png", "away_logo": "away.png",
        "home_color": "#111", "away_color": "#222",
        "confidence_label": confidence_label,
        "kickoff": "Sun, Sep 6 · 1:00 PM",
    }


def test_build_safe_bets_includes_lopsided_high_confidence_game():
    bets = betting.build_safe_bets([_week_matchup()])

    assert len(bets) == 1
    assert bets[0]["team"]["name"] == "Home Team"
    assert bets[0]["opponent"]["name"] == "Away Team"
    assert bets[0]["win_probability"] == 80
    assert bets[0]["moneyline_odds"] < 0


def test_build_safe_bets_excludes_close_game():
    bets = betting.build_safe_bets([_week_matchup(home_probability=55, away_probability=45)])
    assert bets == []


def test_build_safe_bets_excludes_low_confidence_game_below_blowout_threshold():
    bets = betting.build_safe_bets(
        [_week_matchup(confidence_label="Low", home_probability=80, away_probability=20)]
    )
    assert bets == []


def test_build_safe_bets_includes_low_confidence_game_at_blowout_threshold():
    bets = betting.build_safe_bets(
        [_week_matchup(confidence_label="Low", home_probability=88, away_probability=12)]
    )
    assert len(bets) == 1
    assert bets[0]["team"]["name"] == "Home Team"


def test_build_safe_bets_excludes_moderate_confidence_game_below_its_threshold():
    bets = betting.build_safe_bets(
        [_week_matchup(confidence_label="Moderate", home_probability=70, away_probability=30)]
    )
    assert bets == []


def test_build_safe_bets_includes_moderate_confidence_game_at_its_threshold():
    bets = betting.build_safe_bets(
        [_week_matchup(confidence_label="Moderate", home_probability=76, away_probability=24)]
    )
    assert len(bets) == 1


def test_build_safe_bets_excludes_unrecognized_confidence_label():
    bets = betting.build_safe_bets(
        [_week_matchup(confidence_label="Unknown", home_probability=95, away_probability=5)]
    )
    assert bets == []


def test_build_safe_bets_excludes_final_games():
    bets = betting.build_safe_bets([_week_matchup(status="final")])
    assert bets == []


def test_build_safe_bets_picks_away_favorite_when_away_probability_higher():
    bets = betting.build_safe_bets([_week_matchup(home_probability=20, away_probability=80)])
    assert bets[0]["team"]["name"] == "Away Team"


def test_build_safe_bets_sorts_by_win_probability_descending():
    matchups = [
        _week_matchup(game_id="g1", home_probability=76, away_probability=24),
        _week_matchup(game_id="g2", home_probability=90, away_probability=10),
    ]
    bets = betting.build_safe_bets(matchups)
    assert [bet["win_probability"] for bet in bets] == [90, 76]
