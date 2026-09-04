import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nfl_predictor"
)
WEIGHTS_PATH = BASE_DIR / "weights.yaml"
STALENESS_HOURS = 6
SYNC_SEASONS_BACK = 2
