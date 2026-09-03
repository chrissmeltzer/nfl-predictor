from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, predict, stats, sync
from app.config import DB_PATH, STALENESS_HOURS, WEIGHTS_PATH
from app.sources import espn

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Plain-language sentence templates for the game-analysis box. Each factor maps to a
# (positive, negative) pair of templates; either side may be None to skip narrating that
# direction. These deliberately describe *what kind* of factor is in play (pass defense,
# travel, rest, etc.) without revealing exact point values or internal weights.
_FACTOR_TEMPLATES: dict[str, tuple[str | None, str | None]] = {
    "team_form": (
        "{team}'s passing efficiency and ball security have been a strength, which could be a deciding factor against {opponent}.",
        "{opponent}'s pass defense could be a tough matchup for {team}'s offense this week.",
    ),
    "elo": (
        "{team} enters as the stronger team by season-long form.",
        "{opponent} carries a clear power-rating edge into this matchup.",
    ),
    "weather": (
        None,
        "Game conditions could suppress scoring, especially for {team}'s passing attack.",
    ),
    "travel": (
        None,
        "{team}'s travel distance this week adds a fatigue factor worth watching.",
    ),
    "injuries": (
        None,
        "Injuries to key contributors are a concern for {team}.",
    ),
    "home_away_split": (
        "{team} has performed well in this game script this season.",
        "{team} has struggled in this game script (home/away) this season.",
    ),
    "head_to_head": (
        "{team} has had {opponent}'s number in recent head-to-head meetings.",
        "{opponent} has typically had the edge in recent meetings between these two.",
    ),
    "rest_days": (
        "{team} enters with a rest advantage this week.",
        "A short week could hamper {team}.",
    ),
    "recency_trend": (
        "{team} is trending upward compared to their season average.",
        "{team}'s recent form has dipped below their season norm.",
    ),
}
_ANALYSIS_MIN_MAGNITUDE = 0.4
_ANALYSIS_MAX_ITEMS = 3


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


def _logo_url(team) -> str:
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{team['abbreviation'].lower()}.png"


def _format_kickoff(value: str | None) -> str:
    if not value:
        return "Kickoff TBD"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%a, %b %-d · %-I:%M %p")
    except ValueError:
        return value


def _win_probability(team_score: float, opponent_score: float) -> int:
    margin = team_score - opponent_score
    return max(5, min(95, round(50 + margin * 4)))


def _factor_sentence(factor: str, team: str, opponent: str, value: float) -> str | None:
    templates_pair = _FACTOR_TEMPLATES.get(factor)
    if not templates_pair:
        return None
    positive_template, negative_template = templates_pair
    template = positive_template if value >= 0 else negative_template
    if template is None:
        return None
    return template.format(team=team, opponent=opponent)


def _build_analysis(breakdown: dict, home_name: str, away_name: str) -> list[str]:
    """Translate the model's internal per-factor adjustments into a short list of
    plain-language notes about what's swaying this game's projection, without exposing
    exact point values or the underlying weight configuration.
    """
    entries: list[tuple[str, float, str, str]] = []
    for factor, value in breakdown.get("home", {}).items():
        if factor == "baseline" or not isinstance(value, (int, float)):
            continue
        entries.append((factor, value, home_name, away_name))
    for factor, value in breakdown.get("away", {}).items():
        if factor == "baseline" or not isinstance(value, (int, float)):
            continue
        entries.append((factor, value, away_name, home_name))

    entries.sort(key=lambda entry: abs(entry[1]), reverse=True)

    sentences: list[str] = []
    seen: set[str] = set()
    for factor, value, team, opponent in entries:
        if abs(value) < _ANALYSIS_MIN_MAGNITUDE:
            continue
        sentence = _factor_sentence(factor, team, opponent, value)
        if not sentence or sentence in seen:
            continue
        sentences.append(sentence)
        seen.add(sentence)
        if len(sentences) >= _ANALYSIS_MAX_ITEMS:
            break
    return sentences


def _game_view(game, teams, result: dict, selected_team_id: str | None = None) -> dict:
    home = teams[game["home_team_id"]]
    away = teams[game["away_team_id"]]
    home_score = result["predicted_home_score"]
    away_score = result["predicted_away_score"]
    winner = home if home_score >= away_score else away
    breakdown = result.get("breakdown", {})
    view = {
        "game": game,
        "home": home,
        "away": away,
        "home_logo": _logo_url(home),
        "away_logo": _logo_url(away),
        "home_score": home_score,
        "away_score": away_score,
        "winner": winner,
        "home_probability": _win_probability(home_score, away_score),
        "away_probability": _win_probability(away_score, home_score),
        "kickoff": _format_kickoff(game["kickoff_at"]),
        "confidence_score": result.get("confidence_score"),
        "confidence_label": result.get("confidence_label", "Moderate"),
        "analysis": _build_analysis(breakdown, home["name"], away["name"]),
    }
    if selected_team_id:
        is_home = game["home_team_id"] == selected_team_id
        team = home if is_home else away
        opponent = away if is_home else home
        team_score = home_score if is_home else away_score
        opponent_score = away_score if is_home else home_score
        view.update({
            "team": team,
            "opponent": opponent,
            "team_logo": _logo_url(team),
            "opponent_logo": _logo_url(opponent),
            "team_score": team_score,
            "opponent_score": opponent_score,
            "team_probability": _win_probability(team_score, opponent_score),
            "is_home": is_home,
            "location": "vs" if is_home else "at",
        })
    return view


def _template_context(request: Request, **kwargs) -> dict:
    return {"request": request, **kwargs}


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
    teams = {row["id"]: row for row in conn.execute("SELECT * FROM teams ORDER BY name").fetchall()}

    weights = predict.load_weights(WEIGHTS_PATH)
    matchups = [_game_view(game, teams, predict.predict_game(conn, weights, game)) for game in games]

    return templates.TemplateResponse(
        request,
        "index.html",
        _template_context(request, matchups=matchups, teams=list(teams.values()), week=week, season=season),
    )


@app.get("/teams")
def team_search(q: str = "", conn=Depends(get_db)):
    query = q.strip().lower()
    if not query:
        return RedirectResponse(url="/", status_code=303)
    teams = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
    match = next((team for team in teams if query in team["name"].lower() or query == team["abbreviation"].lower()), None)
    if not match:
        return RedirectResponse(url="/?team_not_found=1", status_code=303)
    return RedirectResponse(url=f"/teams/{match['id']}", status_code=303)


@app.get("/teams/{team_id}", response_class=HTMLResponse)
def team_detail(request: Request, team_id: str, conn=Depends(get_db)):
    team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        current_season, _ = espn.fetch_current_week(client)

    games = conn.execute(
        """
        SELECT * FROM games
        WHERE (home_team_id = ? OR away_team_id = ?) AND season = ?
        ORDER BY week ASC, kickoff_at ASC
        """,
        (team_id, team_id, current_season),
    ).fetchall()
    teams = {row["id"]: row for row in conn.execute("SELECT * FROM teams ORDER BY name").fetchall()}
    weights = predict.load_weights(WEIGHTS_PATH)
    schedule_rows = [_game_view(game, teams, predict.predict_game(conn, weights, game), team_id) for game in games]
    upcoming = [row for row in schedule_rows if row["game"]["status"] != "final"]

    return templates.TemplateResponse(
        request,
        "team_detail.html",
        _template_context(
            request,
            team=team,
            team_logo=_logo_url(team),
            season=current_season,
            schedule_rows=schedule_rows,
            upcoming_count=len(upcoming),
            teams=list(teams.values()),
        ),
    )


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
    matchup = _game_view(game, {home_team["id"]: home_team, away_team["id"]: away_team}, result)

    return templates.TemplateResponse(
        request,
        "game_detail.html",
        _template_context(
            request, game=game, result=result, matchup=matchup, home_team=home_team, away_team=away_team,
            home_logo=_logo_url(home_team), away_logo=_logo_url(away_team), weather=weather_row,
            injuries_home=injuries_home, injuries_away=injuries_away, head_to_head=head_to_head,
            teams=[home_team, away_team],
        ),
    )


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
        margin_error = abs((row["predicted_home_score"] - row["predicted_away_score"]) - (row["home_score"] - row["away_score"]))
        total_error = abs((row["predicted_home_score"] + row["predicted_away_score"]) - (row["home_score"] + row["away_score"]))
        errors.append({"row": row, "margin_error": margin_error, "total_error": total_error})
    mean_margin_error = sum(error["margin_error"] for error in errors) / len(errors) if errors else None
    mean_total_error = sum(error["total_error"] for error in errors) / len(errors) if errors else None
    teams = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()

    return templates.TemplateResponse(
        request,
        "accuracy.html",
        _template_context(request, errors=errors, mean_margin_error=mean_margin_error, mean_total_error=mean_total_error, teams=teams),
    )
