"""
Dedicated air quality (AQI) tool.

Real AQI numbers need to come from an actual air-quality API, not a
DuckDuckGo search snippet. A scraped snippet is often just a page's meta
description ("check real-time air quality...") rather than the live
number - which is exactly what went wrong before: the agent reported
"AQI 42, Good" while real sources at the same moment showed AQI 150+.
Search is fundamentally noisy for a single precise live number; a proper
API isn't. This mirrors weather_news.py's approach to temperature - a
dedicated tool with a real data source, not "hope search finds it."

Uses the World Air Quality Index project's free API.
Get a free token (instant, just an email) from:
https://aqicn.org/data-platform/token/

Set it as an environment variable (recommended - keeps it out of any file
you might paste or commit):
    setx AQICN_TOKEN "your-token-here"     (Windows, then restart terminal)
or just hardcode it below if you'd rather - your call.
"""

import os
import requests

AQICN_TOKEN = os.environ.get("AQICN_TOKEN", "")

_LEVELS = [
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (float("inf"), "Hazardous"),
]


def _aqi_level(aqi):
    try:
        aqi = int(aqi)
    except (ValueError, TypeError):
        return "unknown"
    for threshold, label in _LEVELS:
        if aqi <= threshold:
            return label
    return "unknown"


def _fetch_feed(query):
    try:
        resp = requests.get(
            f"https://api.waqi.info/feed/{query}/",
            params={"token": AQICN_TOKEN},
            timeout=10
        )
        return resp.json()
    except Exception:
        return None


def _search_station(location):
    """Fallback: if a direct city-name feed lookup fails, search for the
    nearest matching monitoring station and use its exact UID instead -
    more reliable than guessing at WAQI's expected name format."""
    try:
        resp = requests.get(
            "https://api.waqi.info/search/",
            params={"token": AQICN_TOKEN, "keyword": location},
            timeout=10
        )
        data = resp.json()
        results = data.get("data", [])
        if results:
            return results[0].get("uid")
    except Exception:
        pass
    return None


def get_air_quality(location):
    if not AQICN_TOKEN:
        return ("ERROR: AQICN_TOKEN not set. Get a free token from "
                "https://aqicn.org/data-platform/token/ and set it as an "
                "environment variable (AQICN_TOKEN) or directly in "
                "air_quality.py.")

    location = (location or "").strip()
    if not location:
        return "ERROR: air_quality needs a location, e.g. 'Shah Alam'."

    data = _fetch_feed(location)

    if not data or data.get("status") != "ok":
        uid = _search_station(location)
        if uid:
            data = _fetch_feed(f"@{uid}")

    if not data or data.get("status") != "ok":
        return f"ERROR: No air quality station found for '{location}'."

    d = data["data"]
    aqi = d.get("aqi", "unknown")
    city = d.get("city", {}).get("name", location)
    dominant = d.get("dominentpol", "")
    iaqi = d.get("iaqi", {})

    def _v(key):
        entry = iaqi.get(key)
        return entry.get("v") if entry else None

    level = _aqi_level(aqi)
    lines = [f"Air quality in {city}: AQI {aqi} ({level})"]
    if dominant:
        lines.append(f"Dominant pollutant: {dominant.upper()}")
    pm25 = _v("pm25")
    pm10 = _v("pm10")
    if pm25 is not None:
        lines.append(f"PM2.5: {pm25}")
    if pm10 is not None:
        lines.append(f"PM10: {pm10}")

    return "\n".join(lines)


if __name__ == "__main__":
    # Quick manual test: python air_quality.py
    print(get_air_quality("Shah Alam"))