from __future__ import annotations

from datetime import datetime

import httpx

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _closest_hour_index(times: list[str], target: datetime) -> int:
    target_naive = target.replace(tzinfo=None)
    diffs = [abs((datetime.fromisoformat(t) - target_naive).total_seconds()) for t in times]
    return diffs.index(min(diffs))


def parse_forecast(raw: dict, target_time: datetime) -> dict:
    hourly = raw["hourly"]
    idx = _closest_hour_index(hourly["time"], target_time)
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
