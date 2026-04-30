"""Strava bulk export ingest pipeline.

Parses the Strava archive (``activities.csv`` + activity files), merges with
existing Apple Health data (enrich if overlap, insert if new), and writes to
DuckDB.

Usage::

    python -m src.ingest.strava ~/fitness-data/raw/strava-export.zip
    python -m src.ingest.strava ~/fitness-data/raw/strava-export/ --db data/fitness.duckdb
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

from src.ingest.activity_types import normalize_strava_activity_type
from src.ingest.fit import FitSample, parse_fit
from src.ingest.gpx import RoutePoint, parse_gpx
from src.ingest.tcx import parse_tcx
from src.storage.ids import workout_id
from src.storage.schema import ensure_schema

DEFAULT_DB_PATH = Path("data/fitness.duckdb")

# How close start times must be (in minutes) to consider two workouts a match
_TIME_TOLERANCE_MIN = 5
# How close distances must be (as a fraction) to consider two workouts a match
_DISTANCE_TOLERANCE = 0.20


# -- CSV parsing ---------------------------------------------------------------


def _parse_strava_ts(ts_str: str) -> datetime:
    """Parse a Strava CSV timestamp into a UTC-aware datetime.

    Strava uses formats like ``"Jan 15, 2024, 6:05:00 AM"`` and sometimes ISO.
    The timestamps in the export are in UTC.
    """
    ts_str = ts_str.strip()

    # Try Strava's typical format: "Jan 15, 2024, 6:05:00 AM"
    for fmt in (
        "%b %d, %Y, %I:%M:%S %p",
        "%b %d, %Y, %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            continue

    raise ValueError(f"Cannot parse Strava timestamp: {ts_str!r}")


def parse_activities_csv(csv_path: str | Path) -> pl.DataFrame:
    """Parse a Strava ``activities.csv`` file into a Polars DataFrame.

    Reads all columns as strings to handle format variations, then casts
    the columns we care about.
    """
    df = pl.read_csv(str(csv_path), infer_schema_length=0, truncate_ragged_lines=True)
    return df


def _safe_float(val: str | None) -> float | None:
    """Parse a string to float, returning None on failure."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: str | None) -> int | None:
    """Parse a string to int, returning None on failure."""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


# -- Activity file parsing -----------------------------------------------------


def _parse_activity_file(
    path: Path,
) -> tuple[list[RoutePoint], list[FitSample]]:
    """Dispatch to the right parser based on file extension.

    Handles ``.fit``, ``.fit.gz``, ``.gpx``, ``.gpx.gz``, ``.tcx``, ``.tcx.gz``.
    """
    name = path.name.lower()

    if name.endswith(".fit.gz") or name.endswith(".fit"):
        return parse_fit(path)
    elif name.endswith(".tcx.gz") or name.endswith(".tcx"):
        return parse_tcx(path)
    elif name.endswith(".gpx.gz") or name.endswith(".gpx"):
        # GPX parser returns only route points; no sample data
        if name.endswith(".gz"):
            with gzip.open(path, "rb") as gz:
                import tempfile as tf

                with tf.NamedTemporaryFile(suffix=".gpx", delete=False) as tmp:
                    tmp.write(gz.read())
                    tmp_path = Path(tmp.name)
            try:
                points = parse_gpx(tmp_path)
            finally:
                tmp_path.unlink()
        else:
            points = parse_gpx(path)
        return points, []
    else:
        return [], []


# -- Match/enrich logic -------------------------------------------------------


def _find_matching_workout(
    con: duckdb.DuckDBPyConnection,
    activity_type: str,
    start_time: datetime,
    distance_m: float | None,
) -> str | None:
    """Find an existing workout that matches this Strava activity.

    Match criteria:
    - Same normalized activity type
    - Start time within 5 minutes
    - Distance within 20% (if both have distance)

    Returns the workout_id if found, None otherwise.
    """
    window_start = start_time - timedelta(minutes=_TIME_TOLERANCE_MIN)
    window_end = start_time + timedelta(minutes=_TIME_TOLERANCE_MIN)

    rows = con.execute(
        """
        SELECT workout_id, distance_m
        FROM workouts
        WHERE activity_type = ?
          AND start_time >= ?
          AND start_time <= ?
        """,
        [activity_type, window_start, window_end],
    ).fetchall()

    if not rows:
        return None

    for row_wid, row_dist in rows:
        # If both have distance, check distance tolerance
        if distance_m is not None and row_dist is not None and distance_m > 0 and row_dist > 0:
            ratio = abs(distance_m - row_dist) / max(distance_m, row_dist)
            if ratio > _DISTANCE_TOLERANCE:
                continue
        # Match found
        return row_wid

    return None


def _enrich_existing_workout(
    con: duckdb.DuckDBPyConnection,
    existing_wid: str,
    strava_row: dict[str, str | None],
) -> None:
    """Enrich an existing workout with Strava metadata.

    Merges Strava-specific fields into the ``raw`` JSON column.
    """
    existing_raw = con.execute(
        "SELECT raw FROM workouts WHERE workout_id = ?", [existing_wid]
    ).fetchone()
    if existing_raw is None:
        return

    raw_json = existing_raw[0]
    try:
        raw_data = json.loads(raw_json) if raw_json else {}
    except (json.JSONDecodeError, TypeError):
        raw_data = {}

    # Add Strava-specific metadata
    raw_data["strava_activity_id"] = strava_row.get("Activity ID")
    raw_data["strava_activity_name"] = strava_row.get("Activity Name")
    raw_data["strava_description"] = strava_row.get("Activity Description")
    raw_data["strava_gear"] = strava_row.get("Activity Gear")

    con.execute(
        "UPDATE workouts SET raw = ? WHERE workout_id = ?",
        [json.dumps(raw_data), existing_wid],
    )


# -- DB writers ----------------------------------------------------------------


def _write_workout(
    con: duckdb.DuckDBPyConnection,
    wid: str,
    source_id: str,
    activity_type: str,
    start_time: datetime,
    end_time: datetime,
    duration_sec: int,
    distance_m: float | None,
    energy_kcal: float | None,
    avg_hr: float | None,
    max_hr: float | None,
    elevation_gain_m: float | None,
    device: str | None,
    raw: str,
) -> None:
    """Insert or replace a single workout."""
    df = pl.DataFrame(  # noqa: F841 — referenced by DuckDB SQL
        {
            "workout_id": [wid],
            "source": ["strava"],
            "source_id": [source_id],
            "activity_type": [activity_type],
            "start_time": [start_time],
            "end_time": [end_time],
            "duration_sec": [duration_sec],
            "distance_m": [distance_m],
            "energy_kcal": [energy_kcal],
            "avg_hr": [avg_hr],
            "max_hr": [max_hr],
            "elevation_gain_m": [elevation_gain_m],
            "device": [device],
            "raw": [raw],
        }
    )
    con.execute("INSERT OR REPLACE INTO workouts SELECT * FROM df")


def _write_route_points(
    con: duckdb.DuckDBPyConnection,
    wid: str,
    points: list[RoutePoint],
) -> None:
    """Write route points for a workout."""
    if not points:
        return
    df = pl.DataFrame(  # noqa: F841 — referenced by DuckDB SQL
        {
            "workout_id": [wid] * len(points),
            "timestamp": [p.timestamp for p in points],
            "lat": [p.lat for p in points],
            "lon": [p.lon for p in points],
            "elevation_m": [p.elevation_m for p in points],
        }
    )
    con.execute("INSERT OR REPLACE INTO routes SELECT * FROM df")


def _write_samples(
    con: duckdb.DuckDBPyConnection,
    wid: str,
    samples: list[FitSample],
) -> None:
    """Write HR/cadence/power samples for a workout."""
    rows_wid: list[str] = []
    rows_ts: list[datetime] = []
    rows_metric: list[str] = []
    rows_value: list[float] = []

    for s in samples:
        if s.heart_rate is not None:
            rows_wid.append(wid)
            rows_ts.append(s.timestamp)
            rows_metric.append("heart_rate")
            rows_value.append(float(s.heart_rate))
        if s.cadence is not None:
            rows_wid.append(wid)
            rows_ts.append(s.timestamp)
            rows_metric.append("cadence")
            rows_value.append(float(s.cadence))
        if s.power is not None:
            rows_wid.append(wid)
            rows_ts.append(s.timestamp)
            rows_metric.append("power")
            rows_value.append(float(s.power))
        if s.speed is not None:
            rows_wid.append(wid)
            rows_ts.append(s.timestamp)
            rows_metric.append("speed")
            rows_value.append(s.speed)

    if not rows_wid:
        return
    df = pl.DataFrame(  # noqa: F841 — referenced by DuckDB SQL
        {
            "workout_id": rows_wid,
            "timestamp": rows_ts,
            "metric": rows_metric,
            "value": rows_value,
        }
    )
    con.execute("INSERT OR REPLACE INTO workout_samples SELECT * FROM df")


def _backfill_route_samples(
    con: duckdb.DuckDBPyConnection,
    wid: str,
    route_points: list[RoutePoint],
    samples: list[FitSample],
) -> None:
    """Backfill route/sample data for an enriched workout if it has none."""
    if route_points:
        existing_routes = con.execute(
            "SELECT count(*) FROM routes WHERE workout_id = ?", [wid]
        ).fetchone()
        if existing_routes and existing_routes[0] == 0:
            _write_route_points(con, wid, route_points)

    if samples:
        existing_samples = con.execute(
            "SELECT count(*) FROM workout_samples WHERE workout_id = ?", [wid]
        ).fetchone()
        if existing_samples and existing_samples[0] == 0:
            _write_samples(con, wid, samples)


# -- Main pipeline -------------------------------------------------------------


def _process_row(
    con: duckdb.DuckDBPyConnection,
    row: dict[str, str | None],
    activities_dir: Path | None,
    stats: dict[str, int],
) -> None:
    """Process a single row from activities.csv."""
    activity_id = row.get("Activity ID", "")
    if not activity_id:
        return

    raw_type = row.get("Activity Type", "Other") or "Other"
    activity_type = normalize_strava_activity_type(raw_type)

    ts_str = row.get("Activity Date", "")
    if not ts_str:
        return
    try:
        start_time = _parse_strava_ts(ts_str)
    except ValueError:
        print(f"  Warning: cannot parse timestamp for activity {activity_id}: {ts_str!r}",
              file=sys.stderr)
        return

    elapsed = _safe_int(row.get("Elapsed Time"))
    duration_sec = elapsed if elapsed is not None else 0
    end_time = start_time + timedelta(seconds=duration_sec)

    # Distance: Strava CSV "Distance" is in km (display column)
    # "Distance.1" is in meters. Prefer .1 if available.
    distance_m = _safe_float(row.get("Distance.1"))
    if distance_m is None:
        dist_km = _safe_float(row.get("Distance"))
        if dist_km is not None:
            distance_m = dist_km * 1000.0

    energy_kcal = _safe_float(row.get("Calories"))
    avg_hr = _safe_float(row.get("Average Heart Rate"))
    max_hr = _safe_float(row.get("Max Heart Rate"))
    elevation_gain_m = _safe_float(row.get("Elevation Gain"))

    # Parse activity file for route + samples
    route_points: list[RoutePoint] = []
    samples: list[FitSample] = []
    filename = row.get("Filename", "") or ""
    if filename and activities_dir is not None:
        activity_path = activities_dir / filename
        if activity_path.is_file():
            try:
                route_points, samples = _parse_activity_file(activity_path)
            except Exception as exc:
                print(f"  Warning: failed to parse {activity_path}: {exc}", file=sys.stderr)

    # Check for matching existing workout
    existing_wid = _find_matching_workout(con, activity_type, start_time, distance_m)

    if existing_wid is not None:
        # Enrich existing workout
        _enrich_existing_workout(con, existing_wid, row)
        _backfill_route_samples(con, existing_wid, route_points, samples)
        stats["enriched"] += 1
    else:
        # Insert new workout
        source_id = str(activity_id)
        wid = workout_id("strava", source_id, start_time.isoformat(), activity_type)

        raw_data = {k: v for k, v in row.items() if v is not None and v != ""}
        raw_json = json.dumps(raw_data)

        _write_workout(
            con, wid, source_id, activity_type, start_time, end_time,
            duration_sec, distance_m, energy_kcal, avg_hr, max_hr,
            elevation_gain_m, None, raw_json,
        )
        _write_route_points(con, wid, route_points)
        _write_samples(con, wid, samples)
        stats["inserted"] += 1


def ingest(export_path: str | Path, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Run the full Strava ingest pipeline.

    *export_path* can be a directory (already unzipped) or a ``.zip`` file.
    """
    export_path = Path(export_path)

    if export_path.suffix == ".zip":
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"Extracting {export_path} ...", file=sys.stderr)
            with zipfile.ZipFile(export_path) as zf:
                zf.extractall(tmpdir)
            _ingest_dir(Path(tmpdir), Path(db_path))
    elif export_path.is_dir():
        _ingest_dir(export_path, Path(db_path))
    else:
        print(f"Error: {export_path} is not a directory or .zip file", file=sys.stderr)
        sys.exit(1)


def _find_activities_csv(export_dir: Path) -> Path | None:
    """Find activities.csv inside an export directory."""
    candidate = export_dir / "activities.csv"
    if candidate.is_file():
        return candidate
    # Some exports have files directly in a subdirectory
    for child in export_dir.iterdir():
        if child.is_dir():
            candidate = child / "activities.csv"
            if candidate.is_file():
                return candidate
    return None


def _ingest_dir(export_dir: Path, db_path: Path) -> None:
    """Ingest from an unzipped Strava export directory."""
    csv_path = _find_activities_csv(export_dir)
    if csv_path is None:
        print(f"Error: no activities.csv found in {export_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {csv_path} ...", file=sys.stderr)
    df = parse_activities_csv(csv_path)
    print(f"  Found {len(df):,} activities", file=sys.stderr)

    # activities_dir is the parent of activities.csv
    activities_dir = csv_path.parent

    print(f"Writing to {db_path} ...", file=sys.stderr)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        stats: dict[str, int] = {"inserted": 0, "enriched": 0}

        for i in range(len(df)):
            row = df.row(i, named=True)
            _process_row(con, row, activities_dir, stats)

            if (i + 1) % 100 == 0:
                print(f"  ... processed {i + 1:,} activities", file=sys.stderr)

        print(
            f"  Inserted {stats['inserted']:,} new workouts, "
            f"enriched {stats['enriched']:,} existing workouts",
            file=sys.stderr,
        )
    finally:
        con.close()

    print("Done.", file=sys.stderr)


# -- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a Strava bulk export into the fitness dashboard database."
    )
    parser.add_argument(
        "export_path",
        type=Path,
        help="Path to the Strava export directory or .zip file",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)
    ingest(args.export_path, args.db)


if __name__ == "__main__":
    main()
