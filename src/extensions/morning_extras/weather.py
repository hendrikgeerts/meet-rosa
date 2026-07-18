"""Weer voor één lat/lon via Open-Meteo (gratis, geen API-key).

Returns een korte dict-representatie zodat de briefing-prompt het kan
inzetten. Geen state, geen cache — één HTTPS-call per ochtendbriefing.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# WMO weather code → korte NL-omschrijving. Bron: Open-Meteo docs.
_WMO = {
    0: "helder",
    1: "vrijwel helder", 2: "halfbewolkt", 3: "bewolkt",
    45: "mist", 48: "aanzettende mist",
    51: "lichte motregen", 53: "motregen", 55: "zware motregen",
    56: "lichte ijzelende motregen", 57: "zware ijzelende motregen",
    61: "lichte regen", 63: "regen", 65: "zware regen",
    66: "lichte ijzelregen", 67: "zware ijzelregen",
    71: "lichte sneeuw", 73: "sneeuw", 75: "zware sneeuw",
    77: "korrelsneeuw",
    80: "lichte regenbuien", 81: "regenbuien", 82: "hevige regenbuien",
    85: "lichte sneeuwbuien", 86: "zware sneeuwbuien",
    95: "onweer", 96: "onweer met lichte hagel", 99: "onweer met zware hagel",
}


@dataclass(frozen=True)
class WeatherToday:
    location: str
    description: str            # NL-tekst van WMO-code
    current_temp_c: float
    min_temp_c: float
    max_temp_c: float
    precipitation_probability_max: int   # %
    wind_speed_max_kmh: float
    raw: dict[str, Any]                  # full JSON, for advanced prompt use

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "description": self.description,
            "current_temp_c": self.current_temp_c,
            "min_temp_c": self.min_temp_c,
            "max_temp_c": self.max_temp_c,
            "precip_prob_max_pct": self.precipitation_probability_max,
            "wind_max_kmh": self.wind_speed_max_kmh,
        }


def fetch_weather(
    *, latitude: float, longitude: float, location: str = "",
    timezone: str = "Europe/Amsterdam", timeout: float = 10.0,
) -> WeatherToday | None:
    """Returns today's weather summary, or None on failure (briefing falls
    back to no weather-section)."""
    params = urllib.parse.urlencode({
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max",
        "timezone": timezone,
        "forecast_days": 1,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    from extensions.morning_extras._http import fetch_with_retry
    raw = fetch_with_retry(url, timeout=timeout)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("weather parse failed: %s", exc)
        return None

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    def _first(arr_key: str, default: Any = 0) -> Any:
        arr = daily.get(arr_key) or []
        return arr[0] if arr else default

    weather_code = int(_first("weather_code", current.get("weather_code", 0)))
    return WeatherToday(
        location=location,
        description=_WMO.get(weather_code, f"weercode {weather_code}"),
        current_temp_c=float(current.get("temperature_2m", _first("temperature_2m_max", 0))),
        min_temp_c=float(_first("temperature_2m_min", 0)),
        max_temp_c=float(_first("temperature_2m_max", 0)),
        precipitation_probability_max=int(_first("precipitation_probability_max", 0)),
        wind_speed_max_kmh=float(_first("wind_speed_10m_max", current.get("wind_speed_10m", 0))),
        raw=data,
    )
