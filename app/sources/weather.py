from __future__ import annotations

from datetime import datetime

import httpx

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _closest_hour_index(times: list[str], target: datetime, valid: list[bool]) -> int:
    candidates = [i for i, ok in enumerate(valid) if ok]
    if not candidates:
        raise ValueError("no hourly forecast data available")
    target_naive = target.replace(tzinfo=None)
    return min(
        candidates,
        key=lambda i: abs((datetime.fromisoformat(times[i]) - target_naive).total_seconds()),
    )


def parse_forecast(raw: dict, target_time: datetime) -> dict:
    # Open-Meteo returns null values for the trailing hours of a forecast run,
    # before that model data has been computed -- exclude those hours so the
    # closest-hour lookup doesn't land on missing data.
    hourly = raw["hourly"]
    valid = [t is not None for t in hourly["temperature_2m"]]
    idx = _closest_hour_index(hourly["time"], target_time, valid)
    temp_c = hourly["temperature_2m"][idx]
    wind_kmh = hourly["windspeed_10m"][idx]
    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "wind_mph": round(wind_kmh * 0.621371, 1),
        "precip_pct": hourly["precipitation_probability"][idx],
    }


def fetch_forecast(client: httpx.Client, lat: float, lon: float, target_time: datetime) -> dict:
    resp = client.get(BASE_URL, params={
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
        "timezone": "UTC",
        "forecast_days": 16,
    })
    resp.raise_for_status()
    return parse_forecast(resp.json(), target_time)
