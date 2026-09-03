from __future__ import annotations


def american_odds(win_probability: float) -> int:
    """Convert a win-probability percentage (5-95) into fair American odds."""
    prob = win_probability / 100
    if prob >= 0.5:
        return -round(100 * prob / (1 - prob))
    return round(100 * (1 - prob) / prob)


def build_betting_angles(matchup: dict, matchup_history: list[dict]) -> dict | None:
    """Derive implied moneyline/spread/total numbers from the model's own projection.

    Self-contained: no odds data is ingested. The numbers are meant to be compared
    against whatever line a sportsbook actually offers, so only upcoming games get
    them -- a final game has no bet left to make.
    """
    if matchup["game"]["status"] == "final":
        return None

    home, away = matchup["home"], matchup["away"]
    home_score, away_score = matchup["home_score"], matchup["away_score"]
    margin = home_score - away_score
    favorite, underdog = (home, away) if margin >= 0 else (away, home)
    favorite_probability = matchup["home_probability"] if margin >= 0 else matchup["away_probability"]
    underdog_probability = 100 - favorite_probability

    history_avg = None
    if matchup_history:
        history_avg = sum(m["home_score"] + m["away_score"] for m in matchup_history) / len(matchup_history)

    return {
        "confidence_label": matchup["confidence_label"],
        "moneyline": {
            "favorite": favorite["name"],
            "favorite_odds": american_odds(favorite_probability),
            "underdog": underdog["name"],
            "underdog_odds": american_odds(underdog_probability),
        },
        "spread": {
            "favorite": favorite["name"],
            "underdog": underdog["name"],
            "margin": round(abs(margin), 1),
        },
        "total": {
            "projected": round(home_score + away_score, 1),
            "history_avg": round(history_avg, 1) if history_avg is not None else None,
        },
    }
