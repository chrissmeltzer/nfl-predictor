import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nfl_predictor"
)
WEIGHTS_PATH = BASE_DIR / "weights.yaml"
STALENESS_HOURS = 6
SYNC_SEASONS_BACK = 2
