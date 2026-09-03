from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "nfl.db"
WEIGHTS_PATH = BASE_DIR / "weights.yaml"
STALENESS_HOURS = 6
SYNC_SEASONS_BACK = 2
