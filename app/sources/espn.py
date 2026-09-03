from __future__ import annotations

import httpx

from app.reference import canonical_abbreviation

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def parse_teams(raw: dict) -> list[dict]:
    teams = []
    for entry in raw["sports"][0]["leagues"][0]["teams"]:
        team = entry["team"]
        teams.append({
            "id": team["id"],
            "name": team["displayName"],
            "abbreviation": team["abbreviation"],
        })
    return teams


def fetch_teams(client: httpx.Client) -> list[dict]:
    resp = client.get(f"{BASE_URL}/teams")
    resp.raise_for_status()
    return parse_teams(resp.json())


def parse_scoreboard(raw: dict) -> list[dict]:
    games = []
    for event in raw.get("events", []):
        competition = event["competitions"][0]
        home = next(c for c in competition["competitors"] if c["homeAway"] == "home")
        away = next(c for c in competition["competitors"] if c["homeAway"] == "away")
        venue = competition.get("venue", {})
        status_type = competition["status"]["type"]
        completed = bool(status_type.get("completed"))

        games.append({
            "id": event["id"],
            "season": event["season"]["year"],
            "week": event["week"]["number"],
            "home_team_id": home["team"]["id"],
            "away_team_id": away["team"]["id"],
            "kickoff_at": event["date"],
            "venue_name": venue.get("fullName"),
            "is_outdoor": not venue.get("indoor", False),
            "status": "final" if completed else "scheduled",
            "home_score": int(home["score"]) if completed else None,
            "away_score": int(away["score"]) if completed else None,
        })
    return games


def fetch_scoreboard(client: httpx.Client, season: int, week: int, season_type: int = 2) -> list[dict]:
    resp = client.get(
        f"{BASE_URL}/scoreboard",
        params={"dates": season, "seasontype": season_type, "week": week},
    )
    resp.raise_for_status()
    return parse_scoreboard(resp.json())


def fetch_current_week(client: httpx.Client) -> tuple[int, int]:
    resp = client.get(f"{BASE_URL}/scoreboard")
    resp.raise_for_status()
    data = resp.json()
    return data["season"]["year"], data["week"]["number"]


def parse_injuries(raw: dict) -> list[dict]:
    injuries = []
    for team_block in raw.get("injuries", []):
        team_abbr = canonical_abbreviation(team_block["team"]["abbreviation"])
        for item in team_block.get("injuries", []):
            athlete = item["athlete"]
            injuries.append({
                "team_abbreviation": team_abbr,
                "player_name": athlete["displayName"],
                "position": athlete.get("position", {}).get("abbreviation"),
                "status": item["status"],
            })
    return injuries


def fetch_game_summary(client: httpx.Client, event_id: str) -> dict:
    resp = client.get(f"{BASE_URL}/summary", params={"event": event_id})
    resp.raise_for_status()
    return resp.json()
