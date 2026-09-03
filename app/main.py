from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, predict, stats, sync
from app.config import DB_PATH, STALENESS_HOURS, WEIGHTS_PATH
from app.sources import espn

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def get_db():
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _is_stale(conn) -> bool:
    latest = db.get_meta(conn, "last_synced_at")
    if not latest:
        return True
    latest_dt = datetime.fromisoformat(latest)
    return datetime.now(timezone.utc) - latest_dt > timedelta(hours=STALENESS_HOURS)


@app.post("/sync")
def trigger_sync(conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, _ = espn.fetch_current_week(client)
        sync.sync_all(conn, client, current_season=season)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def schedule(request: Request, week: int | None = None, conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, current_week = espn.fetch_current_week(client)
        if _is_stale(conn):
            sync.sync_all(conn, client, current_season=season)

    week = week or current_week
    games = conn.execute(
        "SELECT * FROM games WHERE season = ? AND week = ? ORDER BY kickoff_at",
        (season, week),
    ).fetchall()
    teams = {row["id"]: row for row in conn.execute("SELECT * FROM teams").fetchall()}

    weights = predict.load_weights(WEIGHTS_PATH)
    game_predictions = {game["id"]: predict.predict_game(conn, weights, game) for game in games}

    return templates.TemplateResponse(request, "index.html", {
        "games": games, "predictions": game_predictions,
        "teams": teams, "week": week, "season": season,
    })


@app.get("/games/{game_id}", response_class=HTMLResponse)
def game_detail(request: Request, game_id: str, conn=Depends(get_db)):
    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        raise HTTPException(status_code=404)
    weights = predict.load_weights(WEIGHTS_PATH)
    result = predict.predict_game(conn, weights, game)
    if game["status"] != "final":
        predict.save_prediction(conn, game_id, result, weights)

    home_team = conn.execute("SELECT * FROM teams WHERE id = ?", (game["home_team_id"],)).fetchone()
    away_team = conn.execute("SELECT * FROM teams WHERE id = ?", (game["away_team_id"],)).fetchone()
    weather_row = conn.execute("SELECT * FROM weather_forecasts WHERE game_id = ?", (game_id,)).fetchone()
    injuries_home = conn.execute("SELECT * FROM injuries WHERE team_id = ?", (game["home_team_id"],)).fetchall()
    injuries_away = conn.execute("SELECT * FROM injuries WHERE team_id = ?", (game["away_team_id"],)).fetchall()
    head_to_head = stats.head_to_head(conn, game["home_team_id"], game["away_team_id"])

    return templates.TemplateResponse(request, "game_detail.html", {
        "game": game, "result": result,
        "home_team": home_team, "away_team": away_team,
        "weather": weather_row, "injuries_home": injuries_home, "injuries_away": injuries_away,
        "head_to_head": head_to_head,
    })


@app.get("/accuracy", response_class=HTMLResponse)
def accuracy(request: Request, conn=Depends(get_db)):
    rows = conn.execute(
        """
        SELECT p.game_id, p.predicted_home_score, p.predicted_away_score, p.created_at,
               g.home_score, g.away_score,
               ht.abbreviation as home_abbr, at_.abbreviation as away_abbr
        FROM predictions p
        JOIN games g ON g.id = p.game_id
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at_ ON at_.id = g.away_team_id
        WHERE g.status = 'final'
          AND p.id IN (SELECT MAX(id) FROM predictions GROUP BY game_id)
        ORDER BY p.created_at DESC
        """
    ).fetchall()

    errors = []
    for row in rows:
        margin_error = abs(
            (row["predicted_home_score"] - row["predicted_away_score"])
            - (row["home_score"] - row["away_score"])
        )
        total_error = abs(
            (row["predicted_home_score"] + row["predicted_away_score"])
            - (row["home_score"] + row["away_score"])
        )
        errors.append({"row": row, "margin_error": margin_error, "total_error": total_error})

    mean_margin_error = sum(e["margin_error"] for e in errors) / len(errors) if errors else None
    mean_total_error = sum(e["total_error"] for e in errors) / len(errors) if errors else None

    return templates.TemplateResponse(request, "accuracy.html", {
        "errors": errors,
        "mean_margin_error": mean_margin_error, "mean_total_error": mean_total_error,
    })
