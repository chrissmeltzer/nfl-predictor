# Deployment Notes

This is a FastAPI + Postgres application. It requires a platform that runs a persistent Python
process -- it cannot be hosted on GitHub Pages, which only serves static files.

## Why the GitHub Pages build was failing

GitHub Pages defaults to building the repository with Jekyll, which tried to render every
Markdown file (including planning docs containing Jinja2 template syntax like `{% block %}`)
as a Liquid template and failed. The `.nojekyll` file in the repo root disables this Jekyll
build entirely. GitHub Pages is not an appropriate target for this app regardless; it's kept
only to stop the Pages build from erroring if Pages is enabled on this repository for any
other reason (e.g. documentation).

## Recommended hosting

Any platform that can run a Docker container or a standard Python/ASGI process works:

- Render
- Railway
- Fly.io
- A VPS running the container behind a reverse proxy

## Building and running with Docker

```bash
docker build -t nfl-predictor .
docker run -p 8000:8000 -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5432/nfl_predictor nfl-predictor
```

## Database

The app connects to Postgres via the `DATABASE_URL` environment variable (see
`app/config.py`). For local development, `docker compose up -d db` starts a Postgres
container matching the default `DATABASE_URL`. In production, `DATABASE_URL` should point
at a hosted Postgres instance rather than a container tied to the app's own lifecycle, so
data survives redeploys.

## Weight calibration is manual, not part of deployment

`scripts/calibrate_weights.py` (run via `make calibrate`) is intentionally **not** wired into
any build or deploy step, for two reasons:

1. It requires finalized games with real scores already in the database. On a fresh deploy
   the database is empty, so calibration has nothing to backtest against.
2. It never overwrites `weights.yaml` automatically -- it writes suggested values to
   `weights.suggested.yaml` for manual review, since automatically trusting a re-weighting
   without a human check could silently degrade prediction quality.

Run it manually and periodically once real season data has accumulated:

```bash
make calibrate
```

Then review `weights.suggested.yaml` and manually copy in any changes you're comfortable with.
