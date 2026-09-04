import json
from datetime import datetime
from pathlib import Path

import pytest

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


def test_parse_forecast_skips_hours_with_null_data():
    # Open-Meteo returns null temperature/wind for the trailing hours of a forecast
    # run before that model data has been computed -- the closest hour by time can
    # still be one of these null hours, so parsing must fall back to the closest
    # hour that actually has data instead of crashing.
    raw = {
        "hourly": {
            "time": ["2026-09-10T00:00", "2026-09-10T01:00", "2026-09-10T02:00"],
            "temperature_2m": [18.0, None, None],
            "windspeed_10m": [10.0, None, None],
            "precipitation_probability": [5, None, None],
        }
    }
    result = weather.parse_forecast(raw, datetime(2026, 9, 10, 2, 0))
    assert result["temp_f"] == round(18.0 * 9 / 5 + 32, 1)
    assert result["wind_mph"] == round(10.0 * 0.621371, 1)
    assert result["precip_pct"] == 5


def test_parse_forecast_raises_when_no_hour_has_data():
    raw = {
        "hourly": {
            "time": ["2026-09-10T00:00", "2026-09-10T01:00"],
            "temperature_2m": [None, None],
            "windspeed_10m": [None, None],
            "precipitation_probability": [None, None],
        }
    }
    with pytest.raises(ValueError):
        weather.parse_forecast(raw, datetime(2026, 9, 10, 0, 0))
