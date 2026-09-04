# NFL Score Predictor

A personal, locally-run app that browses upcoming NFL games and predicts
final scores from free data (ESPN, nflverse, Open-Meteo).

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

The app also needs a Postgres database, configured via the `DATABASE_URL`
environment variable (defaults to
`postgresql://postgres:postgres@localhost:5432/nfl_predictor`). Run
`docker compose up -d db` to start a local Postgres container matching that
default.

## Run

    uvicorn app.main:app --reload

Open http://127.0.0.1:8000 — the schedule page auto-syncs data on first
load (and whenever the cache is older than 6 hours). Use the "Refresh
Data" button to force a sync any time.

## Sample data (no network required)

    python scripts/seed_sample_data.py

Seeds a couple of sample games so you can click through the UI without
waiting on a live sync.

## Tests

    pytest

## Tuning the prediction model

Edit `weights.yaml` and reload the page — weights are read fresh on
every prediction, no restart needed.
