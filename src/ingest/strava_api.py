"""Strava REST API incremental sync.

Fetches activities newer than the stored cursor (``sync_state`` table), pulls
per-activity streams for routes/samples, and writes them through the *same*
match/enrich/insert logic as the bulk-export ingest — so an activity present in
both an export and the API resolves to one ``workout_id`` and never duplicates.

Prerequisites:
- Register a Strava API app at https://www.strava.com/settings/api
- Run ``python -m src.ingest.strava_auth`` once to authorize (see that module)

Usage::

    python -m src.ingest.strava_api            # sync new activities
    python -m src.ingest.strava_api --no-streams  # summaries only (faster)

Run Apple Health ingest before this when possible; otherwise
``src.enrich.reconcile_sources`` cleans up any resulting duplicates.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx

from src.ingest.activity_types import normalize_strava_activity_type
from src.ingest.fit import FitSample
from src.ingest.gpx import RoutePoint
from src.ingest.strava import (
    _backfill_route_samples,
    _enrich_existing_workout,
    _find_matching_workout,
    _write_route_points,
    _write_samples,
    _write_workout,
)
from src.ingest.strava_auth import DEFAULT_TOKENS_PATH, get_access_token
from src.storage.ids import workout_id
from src.storage.schema import ensure_schema
from src.storage.sync_state import get_cursor, set_cursor

DEFAULT_DB_PATH = Path("data/fitness.duckdb")
SOURCE = "strava"
API_BASE = "https://www.strava.com/api/v3"
_PER_PAGE = 200
_STREAM_KEYS = "time,latlng,altitude,heartrate,cadence,watts,velocity_smooth"


# -- Normalization (pure) ------------------------------------------------------


@dataclass
class NormalizedActivity:
    source_id: str
    activity_type: str
    start_time: datetime
    end_time: datetime
    duration_sec: int
    distance_m: float | None
    energy_kcal: float | None
    avg_hr: float | None
    max_hr: float | None
    elevation_gain_m: float | None
    device: str | None
    name: str | None


def _parse_iso(ts_str: str) -> datetime:
    """Parse a Strava API ISO-8601 UTC timestamp (``2026-03-01T14:00:00Z``)."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _f(val: object) -> float | None:
    """Coerce a JSON number to float, or None."""
    if val is None:
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _i(val: object) -> int | None:
    """Coerce a JSON number to int, or None."""
    if val is None:
        return None
    try:
        return int(val)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _s(val: object) -> str | None:
    """Return *val* if it is a string, else None."""
    return val if isinstance(val, str) else None


def normalize_activity(act: dict[str, object]) -> NormalizedActivity:
    """Map a Strava API activity object to a normalized workout record."""
    sport = act.get("sport_type") or act.get("type") or "Workout"
    activity_type = normalize_strava_activity_type(str(sport))
    start = _parse_iso(str(act["start_date"]))
    elapsed = int(_f(act.get("elapsed_time")) or 0)
    return NormalizedActivity(
        source_id=str(act["id"]),
        activity_type=activity_type,
        start_time=start,
        end_time=start + timedelta(seconds=elapsed),
        duration_sec=elapsed,
        distance_m=_f(act.get("distance")),
        energy_kcal=_f(act.get("calories")),  # present on detail, not summary
        avg_hr=_f(act.get("average_heartrate")),
        max_hr=_f(act.get("max_heartrate")),
        elevation_gain_m=_f(act.get("total_elevation_gain")),
        device=_s(act.get("device_name")),
        name=_s(act.get("name")),
    )


def streams_to_points_and_samples(
    streams: dict[str, object], start_time: datetime
) -> tuple[list[RoutePoint], list[FitSample]]:
    """Convert a key-by-type streams object into route points and samples.

    Strava streams are parallel arrays indexed by the ``time`` stream (seconds
    from activity start). Missing streams are simply absent.
    """

    def _data(key: str) -> list[object]:
        stream = streams.get(key)
        if isinstance(stream, dict) and isinstance(stream.get("data"), list):
            return stream["data"]
        return []

    times = _data("time")
    if not times:
        return [], []

    latlng = _data("latlng")
    altitude = _data("altitude")
    heartrate = _data("heartrate")
    cadence = _data("cadence")
    watts = _data("watts")
    velocity = _data("velocity_smooth")

    def _at(seq: list[object], i: int) -> object:
        return seq[i] if i < len(seq) else None

    points: list[RoutePoint] = []
    samples: list[FitSample] = []
    for i, offset in enumerate(times):
        ts = start_time + timedelta(seconds=float(offset))  # type: ignore[arg-type]

        pair = _at(latlng, i)
        if isinstance(pair, list) and len(pair) == 2:
            elev = _at(altitude, i)
            points.append(
                RoutePoint(
                    timestamp=ts,
                    lat=float(pair[0]),
                    lon=float(pair[1]),
                    elevation_m=_f(elev),
                )
            )

        hr, cad, pw, spd = _at(heartrate, i), _at(cadence, i), _at(watts, i), _at(velocity, i)
        if hr is not None or cad is not None or pw is not None or spd is not None:
            samples.append(
                FitSample(
                    timestamp=ts,
                    heart_rate=_i(hr),
                    cadence=_i(cad),
                    power=_i(pw),
                    speed=_f(spd),
                )
            )

    return points, samples


# -- Network -------------------------------------------------------------------


def _fetch_activities_page(
    client: httpx.Client, token: str, after_epoch: int, page: int
) -> list[dict[str, object]]:
    resp = client.get(
        f"{API_BASE}/athlete/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"after": after_epoch, "page": page, "per_page": _PER_PAGE},
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_streams(
    client: httpx.Client, token: str, activity_id: str
) -> dict[str, object]:
    resp = client.get(
        f"{API_BASE}/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {token}"},
        params={"keys": _STREAM_KEYS, "key_by_type": "true"},
    )
    if resp.status_code == 404:  # manual entries have no streams
        return {}
    resp.raise_for_status()
    return resp.json()


# -- Persistence ---------------------------------------------------------------


def _persist_activity(
    con: duckdb.DuckDBPyConnection,
    act: dict[str, object],
    points: list[RoutePoint],
    samples: list[FitSample],
    stats: dict[str, int],
) -> None:
    """Match/enrich or insert a single normalized activity."""
    norm = normalize_activity(act)
    existing_wid = _find_matching_workout(
        con, norm.activity_type, norm.start_time, norm.distance_m
    )

    if existing_wid is not None:
        _enrich_existing_workout(con, existing_wid, act)  # act uses API-key shape
        _backfill_route_samples(con, existing_wid, points, samples)
        stats["enriched"] += 1
        return

    wid = workout_id(SOURCE, norm.source_id, norm.start_time.isoformat(), norm.activity_type)
    _write_workout(
        con, wid, norm.source_id, norm.activity_type, norm.start_time, norm.end_time,
        norm.duration_sec, norm.distance_m, norm.energy_kcal, norm.avg_hr, norm.max_hr,
        norm.elevation_gain_m, norm.device, json.dumps(act), name=norm.name,
    )
    _write_route_points(con, wid, points)
    _write_samples(con, wid, samples)
    stats["inserted"] += 1


# -- Pipeline ------------------------------------------------------------------


def sync(
    db_path: str | Path = DEFAULT_DB_PATH,
    tokens_path: str | Path = DEFAULT_TOKENS_PATH,
    *,
    fetch_streams: bool = True,
    limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Sync activities newer than the stored cursor. Returns ingest stats.

    *limit* caps how many activities are processed — a bounded, non-destructive
    probe for smoke tests. When set, the cursor is left unchanged so a later
    full sync still starts from where it was.
    """
    now_dt = now if now is not None else datetime.now(UTC)
    token = get_access_token(tokens_path, now=now_dt.timestamp())

    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        cursor = get_cursor(con, SOURCE)
        after_epoch = int(cursor.timestamp()) if cursor else 0
        if cursor:
            print(f"Syncing Strava activities after {cursor.isoformat()} ...", file=sys.stderr)
        else:
            print("No cursor found — syncing full activity history ...", file=sys.stderr)
        if limit is not None:
            print(f"  (limited to {limit} activities; cursor will not advance)", file=sys.stderr)

        stats = {"inserted": 0, "enriched": 0}
        max_start = cursor
        processed = 0
        with httpx.Client(timeout=30) as client:
            page = 1
            while limit is None or processed < limit:
                activities = _fetch_activities_page(client, token, after_epoch, page)
                if not activities:
                    break
                for act in activities:
                    start = _parse_iso(str(act["start_date"]))
                    points: list[RoutePoint] = []
                    samples: list[FitSample] = []
                    if fetch_streams:
                        streams = _fetch_streams(client, token, str(act["id"]))
                        points, samples = streams_to_points_and_samples(streams, start)
                    _persist_activity(con, act, points, samples, stats)

                    if max_start is None or start > max_start:
                        max_start = start
                    processed += 1
                    if limit is not None and processed >= limit:
                        break
                print(f"  ... page {page}: {len(activities)} activities", file=sys.stderr)
                page += 1

        # A limited probe must not move the cursor, or it would skip activities.
        if limit is None and max_start is not None and max_start != cursor:
            set_cursor(con, SOURCE, max_start, now=now_dt)

        print(
            f"  Inserted {stats['inserted']:,} new, enriched {stats['enriched']:,} existing.",
            file=sys.stderr,
        )
        return stats
    finally:
        con.close()


def _parse_cursor_arg(value: str) -> datetime:
    """Parse a ``--set-cursor`` value (``YYYY-MM-DD`` or ISO-8601) as UTC.

    A bare date is treated as midnight UTC.
    """
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def set_cursor_cli(
    db_path: str | Path, value: str, *, now: datetime | None = None
) -> datetime:
    """Seed the Strava sync cursor to *value* and return the parsed datetime.

    Use this to skip history already imported via the bulk export: point the
    cursor at the newest activity you already have so the API fetches only what
    is newer.
    """
    now_dt = now if now is not None else datetime.now(UTC)
    dt = _parse_cursor_arg(value)
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        set_cursor(con, SOURCE, dt, now=now_dt)
    finally:
        con.close()
    print(f"Cursor for '{SOURCE}' set to {dt.isoformat()}.")
    print("Next sync will fetch only activities after this time.")
    return dt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Incrementally sync Strava via the REST API.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help=f"Path to the DuckDB file (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--tokens", type=Path, default=DEFAULT_TOKENS_PATH,
                        help=f"Path to the tokens file (default: {DEFAULT_TOKENS_PATH})")
    parser.add_argument("--no-streams", action="store_true",
                        help="Skip per-activity stream fetches (no routes/samples).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap activities processed (probe mode; cursor stays put).")
    parser.add_argument("--set-cursor", type=str, default=None, metavar="DATE",
                        help="Seed the sync cursor (YYYY-MM-DD or ISO-8601, UTC) and exit "
                             "without syncing.")
    args = parser.parse_args(argv)
    if args.set_cursor is not None:
        set_cursor_cli(args.db, args.set_cursor)
        return
    sync(args.db, args.tokens, fetch_streams=not args.no_streams, limit=args.limit)


if __name__ == "__main__":
    main()
