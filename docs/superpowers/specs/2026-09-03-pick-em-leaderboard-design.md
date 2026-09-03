# Pick'em & Leaderboard — Design

## Purpose

Let multiple people pick straight-up winners for each week's games and
track a personal leaderboard of pick accuracy. This is independent of
the app's own model predictions — it's a lightweight, no-login pick'em
pool layered on top of the existing schedule data.

## Identity (no real auth)

- New `players` table: `id`, `name` (unique, case-insensitive), `created_at`.
- A browser cookie (`picker_id`, ~1 year max-age, httponly, samesite=lax)
  stores the current player's id on that device.
- `GET /join?next=<path>`: renders a small form asking for a display name.
- `POST /join`: creates the player if the name doesn't exist yet
  (case-insensitive match against existing names), otherwise reuses the
  existing player with that name. Sets the `picker_id` cookie and
  redirects to `next` (default `/`).
- Empty/whitespace-only names are rejected; the form re-renders with an
  error.
- The site header (`base.html`) shows "Picking as `<name>` · switch" when
  identified, linking to `/join?next=<current path>` so switching names
  is just re-submitting the form. When not identified, it shows a
  "Set your name to start picking" link to `/join`.
- A FastAPI dependency `get_current_player(request, conn)` reads the
  cookie and returns the player row or `None`. Routes that need a player
  use it directly; it never raises.

## Picking

- New `picks` table: `id`, `player_id` (FK `players`), `game_id` (FK
  `games`), `picked_team_id` (FK `teams`), `created_at`, unique on
  `(player_id, game_id)`.
- On the weekly schedule page (`index.html`), each game card gets two
  small pick buttons — one per team (logo + abbreviation), rendered as
  plain HTML forms (no JS required, consistent with the rest of the
  app). The currently-picked team (if any) is visually highlighted.
- `POST /games/{game_id}/pick` (form body: `team_id`, plus `week` to
  redirect back to the right page):
  - If no player is identified, redirect to
    `/join?next=/?week={week}`.
  - If `now >= game.kickoff_at` (or the game isn't `scheduled`), the
    pick is rejected and the redirect back carries `?pick_error=locked`,
    shown as a small dismissible banner (same pattern as the existing
    `team_not_found` banner on `/`).
  - Otherwise upsert the `(player_id, game_id)` row with the chosen
    `picked_team_id` — an existing pick for that game is simply
    overwritten (this is how "changing your pick before kickoff" works).
  - Redirect back to `/?week={week}`.
- Once a game's kickoff has passed, its pick buttons render as disabled
  / read-only (showing the locked-in pick, or "no pick" if none was
  made). Once the game is `final`, a ✓/✗ badge next to the player's pick
  shows whether the picked team actually won. A tie game shows neither
  (push — doesn't count for or against the player).

## Leaderboard

- New `GET /leaderboard` route and `leaderboard.html` template, styled
  consistent with `rankings.html`.
- For each player, only `final` games they picked are counted:
  - **Win** — picked team's score > opponent's score.
  - **Loss** — picked team's score < opponent's score.
  - **Push** — tie game; excluded from win/loss and from the win % calc.
- Table columns: rank, name, record (`W-L` or `W-L-T` when the player has
  any pushes, matching the existing `_team_record` formatting style),
  win %.
- Sorted by win % descending, then total wins descending.
- Below the main table, a per-week breakdown: for each `(season, week)`
  that has at least one final game, a small table of `correct / total`
  picks per player for that week.
- A player with zero decided picks yet still appears (0-0, "—" for win
  %) so newly-joined players are visible immediately.

## Navigation

- Add a "Leaderboard" link to the nav in `base.html` alongside the
  existing Schedule / Rankings / Accuracy links.

## Error handling

- Picking a locked/finished game: redirect with `?pick_error=locked`,
  rendered as a banner (mirrors the existing `team_not_found` pattern).
- Submitting an empty name to `/join`: re-render the form with a
  validation message; no player row is created.
- Picking without an identified player: redirect to `/join?next=...`
  rather than erroring, so the user can join and land back where they
  were.

## Testing

New `tests/test_picks.py`, following the style of `tests/test_main.py`
(FastAPI `TestClient`) and `tests/test_db.py` (direct db helper tests):

- `players` upsert-by-name is case-insensitive and idempotent.
- `picks` upsert overwrites an existing pick for the same
  `(player_id, game_id)`.
- Pick lock logic: rejected once `kickoff_at` has passed or status isn't
  `scheduled`.
- Leaderboard aggregation: win/loss/push counts and win % across a small
  fixture of final games with known outcomes, including a tie game
  (push) and a player with picks but no decided games yet.
- Route tests: `/join` (create + reuse + empty-name rejection),
  `/games/{id}/pick` (success, locked rejection, redirect-to-join when
  no cookie), `/leaderboard` (renders expected standings).
