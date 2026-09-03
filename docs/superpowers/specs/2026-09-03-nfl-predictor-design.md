# NFL Score Predictor — Design

## Purpose

A personal, locally-run web app to browse upcoming NFL games and predict
final scores using a transparent, weighted-factor model built on free data
sources — recent scoring trends, home/away performance, head-to-head
history, weather, rest days, and injuries. The app also tracks its own
prediction accuracy over time against actual results.

Single user (the app's owner), run locally. No auth, no multi-tenancy,
no deployment concerns for v1.

## Data sources

All free, no paid tier required:

- **ESPN's public (undocumented) API** — upcoming schedule, live/final
  scores, team info, venue. Example:
  `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`,
  `.../teams`. No API key.
- **nflverse** (github.com/nflverse) — historical game-by-game results as
  plain CSV releases: scores, stadium, roof type, surface, date. No key,
  no rate limit. This is the backbone for computing historical scoring
  trends and knowing which stadiums are outdoor vs. dome/retractable.
- **Open-Meteo** (open-meteo.com) — free, keyless weather API. Forecast
  endpoint for upcoming outdoor games (up to 16 days out) and historical
  endpoint for past games, queried by stadium lat/lon.
- **ESPN injuries endpoint** — per-team injury report (player, position,
  status: out/doubtful/questionable). No key.

No data source requires credentials, so there's no secrets management
concern for v1.

## Architecture

- **Backend**: Python + FastAPI, served with `uvicorn`.
- **Storage**: local SQLite file (`nfl.db`), accessed via stdlib `sqlite3`
  with a thin data-access layer — no ORM. The schema is small enough that
  an ORM would add indirection without real benefit.
- **Frontend**: server-rendered pages (Jinja2 templates) with light
  vanilla JS for interactivity (week selector, expandable factor
  breakdowns). No SPA build tooling.
- **Sync**: manual, not a background cron job. A "Refresh Data" action
  (nav button → `POST /sync`) pulls fresh data from all three sources
  into SQLite. Pages also trigger an automatic refresh if the relevant
  cached data is older than a configurable staleness threshold (default
  6 hours), so the app self-updates without you needing to remember to
  click refresh, but never spams the APIs on every page load.

## Data model (SQLite)

- **teams** — `id, name, abbreviation, conference, division`
- **games** — `id, season, week, home_team_id, away_team_id, kickoff_at,
  venue, roof_type, lat, lon, status, home_score, away_score`. Single
  source of truth for both past and upcoming games. Past games are
  populated from nflverse (bulk historical) plus ESPN (final scores for
  the current season, since nflverse's current-season data lags). Future
  games come from ESPN's schedule endpoint.
- **weather_forecasts** — `game_id, temp_f, wind_mph, precip_pct,
  condition, fetched_at`. Cached and refreshed as the forecast changes
  approaching kickoff; not populated for dome/retractable-roof games.
- **injuries** — `team_id, player_name, position, status, fetched_at`.
  Snapshot table, replaced on each sync.
- **predictions** — `id, game_id, predicted_home_score,
  predicted_away_score, factor_breakdown_json, weights_snapshot_json,
  created_at`. Written whenever a prediction is (re)computed for a game,
  so later we can compare against the final actual score without
  needing to reconstruct "what would the model have said."

Rolling stats (recent scoring average, home/away splits, head-to-head
record) are **computed on the fly via SQL queries over `games`**, not
stored in separate tables — keeps sync logic to one table per data
source, and these queries are cheap at this data volume (a few thousand
rows).

## Prediction model

A weighted heuristic. Weights live in `weights.yaml`, editable without
touching code:

```yaml
recent_scoring_trend: 1.0
home_away_split: 1.0
head_to_head: 0.5
weather: 1.0
rest_days: 0.5
injuries: 1.0
recent_games_window: 8   # how many past games count as "recent"
```

**Baseline expected score per team** — average of:
1. That team's average points scored over its last N games
   (`recent_games_window`).
2. The opponent's average points allowed over its last N games.

This is a standard simple "expected points" starting point: a team
playing a leaky defense should be expected to outscore its own recent
average; a team playing a stout defense should be expected to underscore
it.

**Adjustments** — each below computes a signed point delta, scaled by
its weight in `weights.yaml`, and added to the relevant team's baseline:

- **Home/away split**: difference between a team's home-game scoring
  average and its overall scoring average (and the inverse for the away
  team).
- **Head-to-head**: difference between each team's average score in
  their past meetings vs. their overall average, capped to avoid
  overweighting a small sample.
- **Weather**: wind speed and precipitation reduce the passing-driven
  scoring adjustment (e.g. above a wind-speed threshold, or any
  measurable precipitation probability, apply a negative delta to both
  teams' expected scores). Skipped entirely when `roof_type` is
  dome/retractable-closed.
- **Rest days**: extra rest above the standard week (e.g. off a bye)
  gives a small positive delta; short rest (e.g. Thursday game off a
  Sunday) gives a small negative delta.
- **Injuries**: each `out`/`doubtful` starter reduces the team's expected
  score, scaled by position importance (QB weighted far higher than a
  bench-depth position). Uses a small static position-importance table.

**Output**: predicted final score for both teams (rounded to whole
points), plus the full breakdown — each factor's raw value and its point
contribution — so a prediction is always explainable, not just a number.

## UI

Three views, navigable from a persistent header with a "Refresh Data"
button:

1. **Schedule / browse** (`/`) — upcoming games grouped by week (week
   selector), each game row showing matchup, kickoff time, venue, and
   the predicted score at a glance.
2. **Game detail** (`/games/{id}`) — full factor breakdown for the
   prediction, head-to-head history table, weather forecast detail,
   current injury report for both teams.
3. **Accuracy tracker** (`/accuracy`) — completed games where a
   prediction was logged, actual vs. predicted score, and summary error
   metrics (e.g. mean absolute error on margin and on total points)
   across recent weeks/season.

## Testing

- **pytest** unit tests for the prediction math: given fixed, known
  inputs (recent scores, splits, weather, etc.), assert the model
  produces the expected output. This is the highest-value test surface
  since the model is the core deliverable.
- **pytest** tests for each data source's parsing/normalization function,
  using saved fixture JSON/CSV samples committed to the repo — never
  hitting live network calls in tests.
- No end-to-end/browser tests planned for v1; manual verification in the
  browser is sufficient for a personal single-user tool.

## Out of scope for v1

- Betting odds / lines integration.
- Playoff seeding or standings implications.
- Multi-user accounts, auth, or public deployment.
- Coaching tendencies, referee tendencies, travel-distance/timezone
  effects — plausible future factors, not in the v1 weighted model.
