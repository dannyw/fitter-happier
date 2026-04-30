"""Weather enrichment for outdoor workouts.

Two data sources, in priority order:
1. Apple Health embedded weather metadata (already in ``raw`` JSON)
2. Open-Meteo historical weather API (free, no key, for workouts missing #1)

Usage::

    python -m src.enrich.weather
    python -m src.enrich.weather --db data/fitness.duckdb
    python -m src.enrich.weather --backfill   # also hit Open-Meteo for missing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb
import httpx

DEFAULT_DB_PATH = Path("data/fitness.duckdb")

# -- Apple Health weather condition codes → human-readable labels --------------
# These are HKWeatherCondition enum values from HealthKit.
_HK_WEATHER_CONDITIONS: dict[str, str] = {
    "1": "clear",
    "2": "fair",
    "3": "partly_cloudy",
    "4": "mostly_cloudy",
    "5": "cloudy",
    "6": "foggy",
    "7": "haze",
    "8": "windy",
    "9": "blustery",
    "10": "smoky",
    "11": "dust",
    "12": "snow",
    "13": "hail",
    "14": "sleet",
    "15": "freezing_drizzle",
    "16": "freezing_rain",
    "17": "mixed_rain_hail",
    "18": "mixed_rain_sleet",
    "19": "mixed_rain_snow",
    "20": "mixed_snow_sleet",
    "21": "drizzle",
    "22": "scattered_showers",
    "23": "showers",
    "24": "thunderstorms",
    "25": "tropical_storm",
    "26": "hurricane",
    "27": "tornado",
}


def _parse_apple_temp(val: str) -> float | None:
    """Parse ``"48 degF"`` → Celsius."""
    try:
        parts = val.strip().split()
        num = float(parts[0])
        if len(parts) > 1 and "F" in parts[1].upper():
            return round((num - 32) * 5 / 9, 1)
        return round(num, 1)  # assume Celsius
    except (ValueError, IndexError):
        return None


def _parse_apple_humidity(val: str) -> float | None:
    """Parse ``"3800 %"`` (basis points) → percentage 0-100."""
    try:
        num = float(val.strip().split()[0])
        # Apple stores humidity as basis points (3800 = 38%)
        if num > 100:
            return round(num / 100, 1)
        return round(num, 1)
    except (ValueError, IndexError):
        return None


def _parse_apple_condition(val: str | None) -> str | None:
    """Map HK condition code to label."""
    if not val:
        return None
    return _HK_WEATHER_CONDITIONS.get(val.strip())


# -- Extract Apple weather from raw JSON --------------------------------------


def _extract_apple_weather(con: duckdb.DuckDBPyConnection) -> int:
    """Extract weather from Apple Health metadata for outdoor workouts.

    Only processes workouts not already in the weather table.
    Returns the number of rows written.
    """
    rows = con.execute("""
        SELECT w.workout_id, w.raw
        FROM workouts w
        LEFT JOIN weather wx ON w.workout_id = wx.workout_id
        WHERE wx.workout_id IS NULL
    """).fetchall()

    written = 0
    for workout_id, raw_str in rows:
        meta = json.loads(raw_str).get("metadata", {})

        # Skip indoor workouts
        if meta.get("HKIndoorWorkout") == "1":
            continue

        temp_raw = meta.get("HKWeatherTemperature")
        if not temp_raw:
            continue  # No Apple weather data

        temp_c = _parse_apple_temp(temp_raw)
        humidity = _parse_apple_humidity(meta.get("HKWeatherHumidity", ""))
        conditions = _parse_apple_condition(meta.get("HKWeatherCondition"))

        if temp_c is None:
            continue

        con.execute(
            "INSERT OR REPLACE INTO weather (workout_id, temp_c, humidity_pct, conditions) "
            "VALUES (?, ?, ?, ?)",
            [workout_id, temp_c, humidity, conditions],
        )
        written += 1

    return written


# -- Open-Meteo fallback ------------------------------------------------------

_OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"


def _fetch_open_meteo(
    lat: float, lon: float, date: str, hour: int
) -> dict[str, float | str | None] | None:
    """Fetch historical weather from Open-Meteo for a single hour.

    *date* is ``"YYYY-MM-DD"``, *hour* is 0–23.
    Returns dict with temp_c, humidity_pct, wind_speed_mps, precipitation_mm, conditions.
    """
    params = {
        "latitude": round(lat, 2),
        "longitude": round(lon, 2),
        "start_date": date,
        "end_date": date,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
    }
    try:
        resp = httpx.get(_OPEN_METEO_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"    Open-Meteo error: {exc}", file=sys.stderr)
        return None

    hourly = data.get("hourly", {})
    temps = hourly.get("temperature_2m", [])
    humids = hourly.get("relative_humidity_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    precips = hourly.get("precipitation", [])
    codes = hourly.get("weather_code", [])

    if hour >= len(temps):
        return None

    # Wind speed from km/h to m/s
    wind_mps = round(winds[hour] / 3.6, 1) if hour < len(winds) and winds[hour] is not None else None

    return {
        "temp_c": temps[hour],
        "humidity_pct": humids[hour] if hour < len(humids) else None,
        "wind_speed_mps": wind_mps,
        "precipitation_mm": precips[hour] if hour < len(precips) else None,
        "conditions": _wmo_to_condition(codes[hour]) if hour < len(codes) else None,
    }


def _wmo_to_condition(code: int | None) -> str | None:
    """Map WMO weather code to a simple label."""
    if code is None:
        return None
    if code <= 1:
        return "clear"
    if code <= 3:
        return "partly_cloudy"
    if code <= 9:
        return "foggy"
    if code <= 19:
        return "drizzle"
    if code <= 29:
        return "rain"
    if code <= 39:
        return "snow"
    if code <= 49:
        return "foggy"
    if code <= 59:
        return "drizzle"
    if code <= 69:
        return "rain"
    if code <= 79:
        return "snow"
    if code <= 84:
        return "showers"
    if code <= 94:
        return "snow"
    return "thunderstorms"


def _backfill_open_meteo(con: duckdb.DuckDBPyConnection) -> int:
    """Backfill weather via Open-Meteo for outdoor workouts still missing data.

    Uses the first route point for lat/lon. Rate-limits to ~1 req/sec.
    Returns the number of rows written.
    """
    # Outdoor workouts missing weather that have route data
    rows = con.execute("""
        SELECT DISTINCT w.workout_id, w.start_time, r.lat, r.lon
        FROM workouts w
        JOIN routes r ON w.workout_id = r.workout_id
        LEFT JOIN weather wx ON w.workout_id = wx.workout_id
        WHERE wx.workout_id IS NULL
        AND json_extract_string(w.raw, '$.metadata.HKIndoorWorkout') != '1'
        ORDER BY w.start_time
    """).fetchall()

    if not rows:
        return 0

    # Deduplicate: use first route point per workout
    seen: set[str] = set()
    to_fetch: list[tuple[str, str, int, float, float]] = []
    for workout_id, start_time, lat, lon in rows:
        if workout_id in seen:
            continue
        seen.add(workout_id)
        date_str = start_time.strftime("%Y-%m-%d")
        hour = start_time.hour
        to_fetch.append((workout_id, date_str, hour, lat, lon))

    written = 0
    # Cache by (lat_rounded, lon_rounded, date, hour) to avoid duplicate API calls
    cache: dict[tuple[float, float, str, int], dict | None] = {}

    for workout_id, date_str, hour, lat, lon in to_fetch:
        cache_key = (round(lat, 2), round(lon, 2), date_str, hour)
        if cache_key not in cache:
            cache[cache_key] = _fetch_open_meteo(lat, lon, date_str, hour)
            time.sleep(0.5)  # rate limit

        weather = cache[cache_key]
        if weather is None:
            continue

        con.execute(
            "INSERT OR REPLACE INTO weather "
            "(workout_id, temp_c, humidity_pct, wind_speed_mps, precipitation_mm, conditions) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                workout_id,
                weather["temp_c"],
                weather["humidity_pct"],
                weather["wind_speed_mps"],
                weather["precipitation_mm"],
                weather["conditions"],
            ],
        )
        written += 1

    return written


# -- Main pipeline -------------------------------------------------------------


def enrich_weather(
    db_path: str | Path = DEFAULT_DB_PATH,
    backfill: bool = False,
) -> None:
    """Run weather enrichment."""
    con = duckdb.connect(str(db_path))
    try:
        # Step 1: Extract Apple Health embedded weather
        print("Extracting Apple Health weather metadata ...", file=sys.stderr)
        apple_count = _extract_apple_weather(con)
        print(f"  Wrote {apple_count} rows from Apple metadata", file=sys.stderr)

        # Step 2: Optionally backfill with Open-Meteo
        if backfill:
            remaining = con.execute("""
                SELECT count(DISTINCT w.workout_id)
                FROM workouts w
                JOIN routes r ON w.workout_id = r.workout_id
                LEFT JOIN weather wx ON w.workout_id = wx.workout_id
                WHERE wx.workout_id IS NULL
                AND json_extract_string(w.raw, '$.metadata.HKIndoorWorkout') != '1'
            """).fetchone()[0]
            print(
                f"Backfilling {remaining} workouts from Open-Meteo ...",
                file=sys.stderr,
            )
            meteo_count = _backfill_open_meteo(con)
            print(f"  Wrote {meteo_count} rows from Open-Meteo", file=sys.stderr)

        total = con.execute("SELECT count(*) FROM weather").fetchone()[0]
        print(f"Weather table: {total} total rows", file=sys.stderr)
    finally:
        con.close()

    print("Done.", file=sys.stderr)


# -- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Enrich workouts with weather data.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Also fetch from Open-Meteo for workouts missing Apple weather",
    )
    args = parser.parse_args(argv)
    enrich_weather(args.db, backfill=args.backfill)


if __name__ == "__main__":
    main()
