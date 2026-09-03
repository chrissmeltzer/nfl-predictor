import json
from datetime import datetime
from pathlib import Path

from app.sources import weather

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture():
    return json.loads((FIXTURES / "openmeteo_forecast.json").read_text())


def test_parse_forecast_picks_closest_hour():
    raw = load_fixture()
    result = weather.parse_forecast(raw, datetime(2026, 9, 10, 1, 10))
    assert result["wind_mph"] == round(25.0 * 0.621371, 1)
    assert result["precip_pct"] == 60


def test_parse_forecast_converts_celsius_to_fahrenheit():
    raw = load_fixture()
    result = weather.parse_forecast(raw, datetime(2026, 9, 10, 0, 0))
    assert result["temp_f"] == round(18.0 * 9 / 5 + 32, 1)
