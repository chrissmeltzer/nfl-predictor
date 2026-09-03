"""Coordinate-descent calibration of prediction weights against finalized games.

Margin-affecting weights (recent scoring, home/away, turnovers/EPA-derived team form,
strength of schedule, Elo, etc.) are tuned against mean absolute margin error. The total-
points anchor and pace weights are tuned separately against mean absolute total error,
since they redistribute the predicted total without changing the predicted margin.

Usage:
    python scripts/calibrate_weights.py

The script never overwrites weights.yaml directly; it writes suggested values to
weights.suggested.yaml for manual review.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, predict
from app.config import DB_PATH, WEIGHTS_PATH

CANDIDATE_MULTIPLIERS = [0.0, 0.5, 1.0, 1.5, 2.0]
MARGIN_TUNABLE_KEYS = [
    "recent_scoring_trend", "home_away_split", "head_to_head", "weather",
    "rest_days", "injuries", "team_form", "strength_of_schedule", "elo",
]
TOTAL_TUNABLE_KEYS = ["total_points_anchor", "pace"]


def _finalized_games(conn):
    return conn.execute("SELECT * FROM games WHERE status = 'final'").fetchall()


def _mean_absolute_margin_error(conn, weights: dict, games) -> float | None:
    errors = []
    for game in games:
        result = predict.predict_game(conn, weights, game)
        predicted_margin = result["predicted_home_score"] - result["predicted_away_score"]
        actual_margin = game["home_score"] - game["away_score"]
        errors.append(abs(predicted_margin - actual_margin))
    return sum(errors) / len(errors) if errors else None


def _mean_absolute_total_error(conn, weights: dict, games) -> float | None:
    errors = []
    for game in games:
        result = predict.predict_game(conn, weights, game)
        predicted_total = result["predicted_home_score"] + result["predicted_away_score"]
        actual_total = game["home_score"] + game["away_score"]
        errors.append(abs(predicted_total - actual_total))
    return sum(errors) / len(errors) if errors else None


def _coordinate_descent(conn, base_weights: dict, games, keys: list[str], error_fn) -> dict:
    best_weights = dict(base_weights)
    best_error = error_fn(conn, best_weights, games)
    if best_error is None:
        return best_weights

    for key in keys:
        base_value = best_weights.get(key, 1.0)
        for multiplier in CANDIDATE_MULTIPLIERS:
            candidate = dict(best_weights)
            candidate[key] = round(base_value * multiplier, 3) if base_value else multiplier
            error = error_fn(conn, candidate, games)
            if error is not None and error < best_error:
                best_error = error
                best_weights = candidate

    return best_weights


def calibrate(conn, base_weights: dict, games) -> dict:
    weights = _coordinate_descent(conn, base_weights, games, MARGIN_TUNABLE_KEYS, _mean_absolute_margin_error)
    weights = _coordinate_descent(conn, weights, games, TOTAL_TUNABLE_KEYS, _mean_absolute_total_error)
    return weights


def main() -> None:
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    base_weights = predict.load_weights(WEIGHTS_PATH)
    games = _finalized_games(conn)

    if not games:
        print("No finalized games in the database yet; run a sync first.")
        return

    baseline_margin_error = _mean_absolute_margin_error(conn, base_weights, games)
    baseline_total_error = _mean_absolute_total_error(conn, base_weights, games)
    tuned_weights = calibrate(conn, base_weights, games)
    tuned_margin_error = _mean_absolute_margin_error(conn, tuned_weights, games)
    tuned_total_error = _mean_absolute_total_error(conn, tuned_weights, games)

    print(f"Finalized games evaluated: {len(games)}")
    print(f"Baseline mean absolute margin error: {baseline_margin_error:.2f}")
    print(f"Tuned mean absolute margin error:    {tuned_margin_error:.2f}")
    print(f"Baseline mean absolute total error:   {baseline_total_error:.2f}")
    print(f"Tuned mean absolute total error:      {tuned_total_error:.2f}")
    print("\nSuggested weights (not written to weights.yaml automatically):")
    for key in MARGIN_TUNABLE_KEYS + TOTAL_TUNABLE_KEYS:
        print(f"  {key}: {tuned_weights.get(key)}")

    suggested_path = WEIGHTS_PATH.parent / "weights.suggested.yaml"
    with open(suggested_path, "w") as f:
        for key, value in tuned_weights.items():
            f.write(f"{key}: {value}\n")
    print(f"\nWrote suggested weights to {suggested_path}. Review before replacing weights.yaml.")


if __name__ == "__main__":
    main()
