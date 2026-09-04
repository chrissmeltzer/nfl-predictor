from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import betting, db, predict, stats, sync
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
    "pass_protection": (
        "{team}'s offensive line has the edge in pass protection this week.",
        "{opponent}'s pass rush could give {team}'s offensive line trouble this week.",
    ),
}
_ANALYSIS_MIN_MAGNITUDE = 0.4
_ANALYSIS_MAX_ITEMS = 3

PICKER_COOKIE = "picker_id"
PICKER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _current_player(request: Request, conn) -> sqlite3.Row | None:
    raw = request.cookies.get(PICKER_COOKIE)
    if not raw or not raw.isdecimal() or len(raw) > 18:
        return None
    return db.get_player_by_id(conn, int(raw))


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


def _pick_locked(game) -> bool:
    if game["status"] != "scheduled":
        return True
    kickoff = game["kickoff_at"]
    if not kickoff:
        return False
    kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    return kickoff_dt <= datetime.now(timezone.utc)


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


def _weather_severity(weather_row) -> dict | None:
    if weather_row is None:
        return None
    wind = weather_row["wind_mph"] or 0
    precip = weather_row["precip_pct"] or 0
    temp = weather_row["temp_f"]

    if precip >= 60:
        icon = "🌧️"
    elif wind >= 20:
        icon = "💨"
    elif temp is not None and temp <= 32:
        icon = "❄️"
    elif temp is not None and temp >= 90:
        icon = "🔥"
    else:
        icon = "☀️"

    is_extreme = precip >= 60 or wind >= 20 or (temp is not None and (temp <= 20 or temp >= 95))
    is_notable = precip >= 30 or wind >= 12 or (temp is not None and (temp <= 35 or temp >= 88))
    if is_extreme:
        severity, label = "severe", "Severe conditions"
    elif is_notable:
        severity, label = "notable", "Challenging conditions"
    else:
        severity, label = "calm", "Favorable conditions"

    return {"icon": icon, "severity": severity, "label": label}


_INJURY_IMPACT = {
    "out": ("impact-out", "Out"),
    "ir": ("impact-out", "Out"),
    "injured reserve": ("impact-out", "Out"),
    "reserve/injured": ("impact-out", "Out"),
    "pup": ("impact-out", "Out"),
    "doubtful": ("impact-doubtful", "Doubtful"),
    "questionable": ("impact-questionable", "Questionable"),
}


def _injury_impact(status: str | None) -> dict:
    css_class, label = _INJURY_IMPACT.get((status or "").strip().lower(), ("impact-minor", status or "Active"))
    return {"class": css_class, "label": label}


def _injury_view(rows) -> list[dict]:
    return [{**dict(row), "impact": _injury_impact(row["status"])} for row in rows]


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


_REVEAL_HIT_MARGIN = 3.0


def _actual_winner_team_id(game) -> str | None:
    if game["status"] != "final" or game["home_score"] is None or game["away_score"] is None:
        return None
    if game["home_score"] > game["away_score"]:
        return game["home_team_id"]
    if game["away_score"] > game["home_score"]:
        return game["away_team_id"]
    return None


def _reveal(conn, game) -> dict | None:
    if game["status"] != "final":
        return None
    saved = predict.get_latest_prediction(conn, game["id"])
    if saved is None:
        return None
    predicted_home = saved["predicted_home_score"]
    predicted_away = saved["predicted_away_score"]
    margin_error = abs(
        (predicted_home - predicted_away) - (game["home_score"] - game["away_score"])
    )
    hit = margin_error <= _REVEAL_HIT_MARGIN
    return {
        "predicted_home_score": predicted_home,
        "predicted_away_score": predicted_away,
        "hit": hit,
        "label": "Nailed it" if hit else f"Missed by {margin_error:.0f}",
    }


def _game_view(conn, game, teams, result: dict, selected_team_id: str | None = None) -> dict:
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
        "reveal": _reveal(conn, game),
        "upset_alert": result.get("upset_alert", False) and game["status"] != "final",
        "actual_winner_team_id": _actual_winner_team_id(game),
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


def _template_context(request: Request, conn, **kwargs) -> dict:
    return {"request": request, "player": _current_player(request, conn), **kwargs}


@app.post("/sync")
def trigger_sync(conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, _ = espn.fetch_current_week(client)
        sync.sync_all(conn, client, current_season=season)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def schedule(request: Request, week: int | None = None, sort: str | None = None, conn=Depends(get_db)):
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
    matchups = [_game_view(conn, game, teams, predict.predict_game(conn, weights, game)) for game in games]
    if sort == "confidence":
        matchups.sort(key=lambda m: m["confidence_score"] if m["confidence_score"] is not None else 0)

    player = _current_player(request, conn)
    picks = db.get_player_picks_for_games(conn, player["id"], [m["game"]["id"] for m in matchups]) if player else {}
    for matchup in matchups:
        matchup["player_pick_team_id"] = picks.get(matchup["game"]["id"])
        matchup["pick_locked"] = _pick_locked(matchup["game"])

    return templates.TemplateResponse(
        request,
        "index.html",
        _template_context(
            request, conn, matchups=matchups, teams=list(teams.values()), week=week, season=season, sort=sort
        ),
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
    schedule_rows = [
        _game_view(conn, game, teams, predict.predict_game(conn, weights, game), team_id) for game in games
    ]
    upcoming = [row for row in schedule_rows if row["game"]["status"] != "final"]

    return templates.TemplateResponse(
        request,
        "team_detail.html",
        _template_context(
            request,
            conn,
            team=team,
            team_logo=_logo_url(team),
            season=current_season,
            schedule_rows=schedule_rows,
            upcoming_count=len(upcoming),
            streak=_recent_pick_accuracy(conn, team_id),
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
    head_to_head_games = stats.head_to_head_games(conn, game["home_team_id"], game["away_team_id"])
    matchup = _game_view(conn, game, {home_team["id"]: home_team, away_team["id"]: away_team}, result)

    matchup_history = [
        {
            "date_label": _format_kickoff(g["kickoff_at"]).split(" · ")[0]
            if g["kickoff_at"] else f"{g['season']} Wk {g['week']}",
            "season": g["season"],
            "week": g["week"],
            "home_score": g["team_score"],
            "away_score": g["opponent_score"],
            "winner": "home" if g["team_score"] > g["opponent_score"]
            else "away" if g["opponent_score"] > g["team_score"] else None,
        }
        for g in reversed(head_to_head_games)
    ]
    betting_angles = betting.build_betting_angles(matchup, matchup_history)

    return templates.TemplateResponse(
        request,
        "game_detail.html",
        _template_context(
            request, conn, game=game, result=result, matchup=matchup, home_team=home_team, away_team=away_team,
            home_logo=_logo_url(home_team), away_logo=_logo_url(away_team), weather=weather_row,
            weather_severity=_weather_severity(weather_row),
            injuries_home=_injury_view(injuries_home), injuries_away=_injury_view(injuries_away),
            head_to_head=head_to_head, matchup_history=matchup_history, betting=betting_angles,
            teams=[home_team, away_team],
        ),
    )


def _recent_pick_accuracy(conn, team_id: str, limit: int = 5) -> dict | None:
    rows = conn.execute(
        """
        SELECT g.home_team_id, g.home_score, g.away_score,
               p.predicted_home_score, p.predicted_away_score
        FROM games g
        JOIN predictions p ON p.game_id = g.id
        WHERE g.status = 'final' AND (g.home_team_id = ? OR g.away_team_id = ?)
          AND p.id IN (SELECT MAX(id) FROM predictions GROUP BY game_id)
        ORDER BY g.kickoff_at DESC, g.id DESC
        LIMIT ?
        """,
        (team_id, team_id, limit),
    ).fetchall()
    if not rows:
        return None
    correct = sum(
        1 for row in rows
        if (row["home_score"] >= row["away_score"]) == (row["predicted_home_score"] >= row["predicted_away_score"])
    )
    return {"correct": correct, "total": len(rows)}


def _team_record(conn, team_id: str, season: int) -> str:
    games = conn.execute(
        "SELECT home_team_id, home_score, away_score FROM games "
        "WHERE season = ? AND status = 'final' AND (home_team_id = ? OR away_team_id = ?)",
        (season, team_id, team_id),
    ).fetchall()
    wins = losses = ties = 0
    for g in games:
        team_score = g["home_score"] if g["home_team_id"] == team_id else g["away_score"]
        opponent_score = g["away_score"] if g["home_team_id"] == team_id else g["home_score"]
        if team_score > opponent_score:
            wins += 1
        elif team_score < opponent_score:
            losses += 1
        else:
            ties += 1
    return f"{wins}-{losses}-{ties}" if ties else f"{wins}-{losses}"


@app.get("/rankings", response_class=HTMLResponse)
def rankings(request: Request, conn=Depends(get_db)):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        season, _ = espn.fetch_current_week(client)

    rows = conn.execute(
        """
        SELECT t.*, COALESCE(r.elo_rating, 1500.0) AS elo_rating
        FROM teams t
        LEFT JOIN team_ratings r ON r.team_id = t.id
        ORDER BY elo_rating DESC
        """
    ).fetchall()
    ranked_teams = [
        {
            "rank": i,
            "team": row,
            "logo": _logo_url(row),
            "elo_rating": round(row["elo_rating"]),
            "record": _team_record(conn, row["id"], season),
        }
        for i, row in enumerate(rows, start=1)
    ]
    teams = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()

    return templates.TemplateResponse(
        request,
        "rankings.html",
        _template_context(request, conn, rankings=ranked_teams, teams=list(teams), season=season),
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
        _template_context(request, conn, errors=errors, mean_margin_error=mean_margin_error, mean_total_error=mean_total_error, teams=teams),
    )


@app.get("/join", response_class=HTMLResponse)
def join_form(request: Request, next: str = "/", error: str | None = None, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request, "join.html", _template_context(request, conn, next=next, error=error)
    )


@app.post("/join")
def join_submit(request: Request, name: str = Form(...), next: str = Form("/"), conn=Depends(get_db)):
    cleaned = name.strip()
    if not cleaned:
        return templates.TemplateResponse(
            request,
            "join.html",
            _template_context(request, conn, next=next, error="Enter a name to continue."),
            status_code=422,
        )
    player = db.get_or_create_player(conn, cleaned, datetime.now(timezone.utc).isoformat())
    conn.commit()
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        PICKER_COOKIE, str(player["id"]), max_age=PICKER_COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@app.post("/games/{game_id}/pick")
def submit_pick(request: Request, game_id: str, team_id: str = Form(...), week: int = Form(...), conn=Depends(get_db)):
    player = _current_player(request, conn)
    if player is None:
        return RedirectResponse(url=f"/join?next={quote(f'/?week={week}', safe='')}", status_code=303)

    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        raise HTTPException(status_code=404)

    if team_id not in (game["home_team_id"], game["away_team_id"]):
        raise HTTPException(status_code=400)

    if _pick_locked(game):
        return RedirectResponse(url=f"/?week={week}&pick_error=locked", status_code=303)

    db.upsert_pick(conn, player["id"], game_id, team_id, datetime.now(timezone.utc).isoformat())
    conn.commit()
    return RedirectResponse(url=f"/?week={week}", status_code=303)


def _pick_outcome(row) -> str:
    picked_home = row["picked_team_id"] == row["home_team_id"]
    picked_score = row["home_score"] if picked_home else row["away_score"]
    opponent_score = row["away_score"] if picked_home else row["home_score"]
    if picked_score > opponent_score:
        return "win"
    if picked_score < opponent_score:
        return "loss"
    return "push"


def _build_standings(players, decided_picks) -> list[dict]:
    records = {p["id"]: {"player": p, "wins": 0, "losses": 0, "pushes": 0} for p in players}
    for row in decided_picks:
        outcome = _pick_outcome(row)
        record = records[row["player_id"]]
        if outcome == "win":
            record["wins"] += 1
        elif outcome == "loss":
            record["losses"] += 1
        else:
            record["pushes"] += 1

    standings = []
    for record in records.values():
        decided = record["wins"] + record["losses"]
        win_pct = record["wins"] / decided if decided else None
        standings.append({
            "player": record["player"],
            "wins": record["wins"],
            "losses": record["losses"],
            "pushes": record["pushes"],
            "record": (
                f"{record['wins']}-{record['losses']}-{record['pushes']}"
                if record["pushes"] else f"{record['wins']}-{record['losses']}"
            ),
            "win_pct": win_pct,
            "win_pct_label": f"{round(win_pct * 100)}%" if win_pct is not None else "—",
        })
    standings.sort(key=lambda s: (s["win_pct"] if s["win_pct"] is not None else -1, s["wins"]), reverse=True)
    for i, standing in enumerate(standings, start=1):
        standing["rank"] = i
    return standings


def _build_weekly_breakdown(players, decided_picks) -> list[dict]:
    weeks: dict[tuple[int, int], dict[int, dict]] = {}
    for row in decided_picks:
        key = (row["season"], row["week"])
        week_bucket = weeks.setdefault(key, {p["id"]: {"correct": 0, "total": 0} for p in players})
        stat = week_bucket[row["player_id"]]
        stat["total"] += 1
        if _pick_outcome(row) == "win":
            stat["correct"] += 1

    breakdown = []
    for (season, week), player_stats in sorted(weeks.items(), reverse=True):
        breakdown.append({
            "season": season,
            "week": week,
            "players": [{"player": p, **player_stats[p["id"]]} for p in players],
        })
    return breakdown


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request, conn=Depends(get_db)):
    players = db.get_all_players(conn)
    decided_picks = db.get_decided_picks(conn)
    standings = _build_standings(players, decided_picks)
    weekly = _build_weekly_breakdown(players, decided_picks)

    return templates.TemplateResponse(
        request, "leaderboard.html", _template_context(request, conn, standings=standings, weekly=weekly)
    )
