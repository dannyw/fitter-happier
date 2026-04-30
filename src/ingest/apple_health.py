"""Apple Health export ingest pipeline.

Stream-parses ``export.xml`` from an Apple Health iOS export, extracts
workouts, workout samples, health metrics, and GPS routes, then writes
everything to a DuckDB database.

Usage::

    python -m src.ingest.apple_health raw/
    python -m src.ingest.apple_health ~/fitness-data/raw/export.zip
    python -m src.ingest.apple_health raw/ --db data/fitness.duckdb
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl
from lxml import etree

from src.ingest.activity_types import normalize_activity_type
from src.ingest.gpx import parse_gpx
from src.ingest.timestamps import parse_apple_health_ts
from src.ingest.units import distance_to_meters, elevation_to_meters, energy_to_kcal
from src.storage.ids import workout_id
from src.storage.schema import ensure_schema

DEFAULT_DB_PATH = Path("data/fitness.duckdb")

# -- Record types we care about -----------------------------------------------

# Types that become workout_samples (matched to workouts by time overlap)
WORKOUT_SAMPLE_TYPES: dict[str, str] = {
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierRunningSpeed": "running_speed",
    "HKQuantityTypeIdentifierRunningPower": "running_power",
    "HKQuantityTypeIdentifierRunningStrideLength": "stride_length",
    "HKQuantityTypeIdentifierRunningVerticalOscillation": "vertical_oscillation",
    "HKQuantityTypeIdentifierRunningGroundContactTime": "ground_contact_time",
    "HKQuantityTypeIdentifierStepCadence": "cadence",
}

# Types that become health_metrics rows
HEALTH_METRIC_TYPES: dict[str, str] = {
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierBodyMass": "body_mass",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat",
    "HKQuantityTypeIdentifierOxygenSaturation": "oxygen_saturation",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
}

# Sleep sub-values: Apple Health value attribute → metric name suffix
_SLEEP_VALUES: dict[str, str] = {
    "HKCategoryValueSleepAnalysisAsleepCore": "sleep_core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "sleep_deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "sleep_rem",
    "HKCategoryValueSleepAnalysisAwake": "sleep_awake",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "sleep_unspecified",
    "HKCategoryValueSleepAnalysisInBed": "sleep_in_bed",
}

WRITE_CHUNK_SIZE = 500_000


# -- Intermediate dataclasses -------------------------------------------------


@dataclass
class WorkoutRecord:
    workout_id: str
    source: str
    source_id: str | None
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
    raw: str  # JSON string
    gpx_path: str | None  # relative path to GPX file


@dataclass
class SampleRecord:
    start_time: datetime
    metric: str
    value: float


@dataclass
class HealthMetricRecord:
    metric: str
    start_time: datetime
    end_time: datetime
    value: float
    unit: str
    source: str


@dataclass
class ParseResult:
    workouts: list[WorkoutRecord] = field(default_factory=list)
    samples: list[SampleRecord] = field(default_factory=list)
    health_metrics: list[HealthMetricRecord] = field(default_factory=list)


# -- Stream parser -------------------------------------------------------------


def _extract_device_name(device_str: str | None) -> str | None:
    """Extract a human-readable device name from the HK device string."""
    if not device_str:
        return None
    # Format: <<HKDevice: ..., name:Apple Watch, ...>>
    for part in device_str.split(","):
        part = part.strip()
        if part.startswith("name:"):
            name = part[5:].strip().rstrip(">")
            if name:
                return name
    return None


def _parse_workout(elem: etree._Element) -> WorkoutRecord:
    """Parse a <Workout> element into a WorkoutRecord."""
    attrib = elem.attrib
    raw_type = attrib.get("workoutActivityType", "Other")
    activity = normalize_activity_type(raw_type)

    start = parse_apple_health_ts(attrib["startDate"])
    end = parse_apple_health_ts(attrib["endDate"])
    duration = int((end - start).total_seconds())

    # Source info
    source_name = attrib.get("sourceName", "apple_health")
    device = _extract_device_name(attrib.get("device"))

    # Extract WorkoutStatistics
    distance_m: float | None = None
    energy_kcal_val: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None

    for stat in elem.findall("WorkoutStatistics"):
        stat_type = stat.get("type", "")
        if stat_type == "HKQuantityTypeIdentifierDistanceWalkingRunning" or \
           stat_type == "HKQuantityTypeIdentifierDistanceCycling" or \
           stat_type == "HKQuantityTypeIdentifierDistanceSwimming":
            val = stat.get("sum")
            unit = stat.get("unit", "km")
            if val:
                distance_m = distance_to_meters(float(val), unit)
        elif stat_type == "HKQuantityTypeIdentifierActiveEnergyBurned":
            val = stat.get("sum")
            unit = stat.get("unit", "kcal")
            if val:
                energy_kcal_val = energy_to_kcal(float(val), unit)
        elif stat_type == "HKQuantityTypeIdentifierHeartRate":
            avg_val = stat.get("average")
            max_val = stat.get("maximum")
            if avg_val:
                avg_hr = float(avg_val)
            if max_val:
                max_hr = float(max_val)

    # Extract metadata
    elevation_gain_m: float | None = None
    source_id: str | None = None
    metadata: dict[str, str] = {}

    for meta in elem.findall("MetadataEntry"):
        key = meta.get("key", "")
        val = meta.get("value", "")
        metadata[key] = val
        if key == "HKElevationAscended":
            try:
                elevation_gain_m = elevation_to_meters(float(val), "m")
            except (ValueError, TypeError):
                pass
        elif key == "HKExternalUUID":
            source_id = val

    # Extract GPX route path
    gpx_path: str | None = None
    for route in elem.findall("WorkoutRoute"):
        for fref in route.findall("FileReference"):
            path = fref.get("path")
            if path:
                gpx_path = path
                break
        if gpx_path:
            break

    # Build raw JSON
    raw_data = {k: v for k, v in attrib.items()}
    if metadata:
        raw_data["metadata"] = metadata

    wid = workout_id("apple_health", source_id, start.isoformat(), activity)

    return WorkoutRecord(
        workout_id=wid,
        source="apple_health",
        source_id=source_id,
        activity_type=activity,
        start_time=start,
        end_time=end,
        duration_sec=duration,
        distance_m=distance_m,
        energy_kcal=energy_kcal_val,
        avg_hr=avg_hr,
        max_hr=max_hr,
        elevation_gain_m=elevation_gain_m,
        device=device,
        raw=json.dumps(raw_data),
        gpx_path=gpx_path,
    )


def _parse_sample_record(elem: etree._Element) -> SampleRecord | None:
    """Parse a <Record> element that is a workout sample type."""
    rec_type = elem.get("type", "")
    metric = WORKOUT_SAMPLE_TYPES.get(rec_type)
    if metric is None:
        return None
    val = elem.get("value")
    start_date = elem.get("startDate")
    if val is None or start_date is None:
        return None
    try:
        return SampleRecord(
            start_time=parse_apple_health_ts(start_date),
            metric=metric,
            value=float(val),
        )
    except (ValueError, TypeError):
        return None


def _parse_health_record(elem: etree._Element) -> HealthMetricRecord | None:
    """Parse a <Record> element that is a health metric type."""
    rec_type = elem.get("type", "")
    metric = HEALTH_METRIC_TYPES.get(rec_type)
    if metric is None:
        return None

    start_date = elem.get("startDate")
    end_date = elem.get("endDate")
    source_name = elem.get("sourceName", "unknown")
    if start_date is None or end_date is None:
        return None

    start = parse_apple_health_ts(start_date)
    end = parse_apple_health_ts(end_date)

    # Sleep is special: value is a category, duration is the metric
    if metric == "sleep":
        raw_value = elem.get("value", "")
        sleep_metric = _SLEEP_VALUES.get(raw_value)
        if sleep_metric is None:
            return None
        duration_hours = (end - start).total_seconds() / 3600.0
        return HealthMetricRecord(
            metric=sleep_metric,
            start_time=start,
            end_time=end,
            value=round(duration_hours, 4),
            unit="hours",
            source=source_name,
        )

    val = elem.get("value")
    unit = elem.get("unit", "")
    if val is None:
        return None
    try:
        return HealthMetricRecord(
            metric=metric,
            start_time=start,
            end_time=end,
            value=float(val),
            unit=unit,
            source=source_name,
        )
    except (ValueError, TypeError):
        return None


def stream_parse(xml_path: str | Path) -> ParseResult:
    """Stream-parse an Apple Health export.xml file.

    Uses ``lxml.etree.iterparse`` to keep memory bounded.  Returns a
    ``ParseResult`` with all extracted records.
    """
    result = ParseResult()
    count = 0

    context = etree.iterparse(str(xml_path), events=("end",), tag=("Record", "Workout"))

    for _event, elem in context:
        tag = elem.tag

        if tag == "Record":
            rec_type = elem.get("type", "")
            if rec_type in WORKOUT_SAMPLE_TYPES:
                sample = _parse_sample_record(elem)
                if sample is not None:
                    result.samples.append(sample)
            elif rec_type in HEALTH_METRIC_TYPES:
                hm = _parse_health_record(elem)
                if hm is not None:
                    result.health_metrics.append(hm)

        elif tag == "Workout":
            workout = _parse_workout(elem)
            result.workouts.append(workout)

        # Free memory: clear element and remove from parent
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]

        count += 1
        if count % 500_000 == 0:
            print(f"  ... processed {count:,} elements", file=sys.stderr)

    print(
        f"  Parsed {len(result.workouts):,} workouts, "
        f"{len(result.samples):,} samples, "
        f"{len(result.health_metrics):,} health metrics",
        file=sys.stderr,
    )
    return result


# -- Sample-to-workout matching ------------------------------------------------


def match_samples_to_workouts(
    workouts: list[WorkoutRecord],
    samples: list[SampleRecord],
) -> dict[str, list[SampleRecord]]:
    """Match sample records to workouts by time overlap.

    Returns a mapping of workout_id → list of matching samples.
    Uses binary search for efficiency.
    """
    if not workouts or not samples:
        return {}

    # Sort workouts by start_time for binary search
    sorted_workouts = sorted(workouts, key=lambda w: w.start_time)
    workout_starts = [w.start_time for w in sorted_workouts]

    matched: dict[str, list[SampleRecord]] = {}

    for sample in samples:
        # Find workouts that could contain this sample:
        # workout.start_time <= sample.start_time < workout.end_time
        idx = bisect.bisect_right(workout_starts, sample.start_time)
        # Check the workout at idx-1 (last workout that started before/at sample time)
        for i in range(max(0, idx - 1), min(idx + 1, len(sorted_workouts))):
            w = sorted_workouts[i]
            if w.start_time <= sample.start_time <= w.end_time:
                matched.setdefault(w.workout_id, []).append(sample)
                break

    return matched


# -- DuckDB writers ------------------------------------------------------------


def _write_workouts(con: duckdb.DuckDBPyConnection, workouts: list[WorkoutRecord]) -> None:
    """Write workout records to DuckDB."""
    if not workouts:
        return
    for i in range(0, len(workouts), WRITE_CHUNK_SIZE):
        chunk = workouts[i : i + WRITE_CHUNK_SIZE]
        df = pl.DataFrame(
            {
                "workout_id": [w.workout_id for w in chunk],
                "source": [w.source for w in chunk],
                "source_id": [w.source_id for w in chunk],
                "activity_type": [w.activity_type for w in chunk],
                "start_time": [w.start_time for w in chunk],
                "end_time": [w.end_time for w in chunk],
                "duration_sec": [w.duration_sec for w in chunk],
                "distance_m": [w.distance_m for w in chunk],
                "energy_kcal": [w.energy_kcal for w in chunk],
                "avg_hr": [w.avg_hr for w in chunk],
                "max_hr": [w.max_hr for w in chunk],
                "elevation_gain_m": [w.elevation_gain_m for w in chunk],
                "device": [w.device for w in chunk],
                "raw": [w.raw for w in chunk],
            }
        )
        con.execute("INSERT OR REPLACE INTO workouts SELECT * FROM df")
    print(f"  Wrote {len(workouts):,} workouts", file=sys.stderr)


def _write_samples(
    con: duckdb.DuckDBPyConnection,
    matched: dict[str, list[SampleRecord]],
) -> None:
    """Write matched workout samples to DuckDB."""
    rows_workout_id: list[str] = []
    rows_timestamp: list[datetime] = []
    rows_metric: list[str] = []
    rows_value: list[float] = []

    for wid, samples in matched.items():
        for s in samples:
            rows_workout_id.append(wid)
            rows_timestamp.append(s.start_time)
            rows_metric.append(s.metric)
            rows_value.append(s.value)

    total = len(rows_workout_id)
    if total == 0:
        return

    for i in range(0, total, WRITE_CHUNK_SIZE):
        end = min(i + WRITE_CHUNK_SIZE, total)
        df = pl.DataFrame(
            {
                "workout_id": rows_workout_id[i:end],
                "timestamp": rows_timestamp[i:end],
                "metric": rows_metric[i:end],
                "value": rows_value[i:end],
            }
        )
        con.execute("INSERT OR REPLACE INTO workout_samples SELECT * FROM df")
    print(f"  Wrote {total:,} workout samples", file=sys.stderr)


def _write_health_metrics(
    con: duckdb.DuckDBPyConnection,
    metrics: list[HealthMetricRecord],
) -> None:
    """Write health metric records to DuckDB."""
    if not metrics:
        return
    for i in range(0, len(metrics), WRITE_CHUNK_SIZE):
        chunk = metrics[i : i + WRITE_CHUNK_SIZE]
        df = pl.DataFrame(
            {
                "metric": [m.metric for m in chunk],
                "start_time": [m.start_time for m in chunk],
                "end_time": [m.end_time for m in chunk],
                "value": [m.value for m in chunk],
                "unit": [m.unit for m in chunk],
                "source": [m.source for m in chunk],
            }
        )
        con.execute("INSERT OR REPLACE INTO health_metrics SELECT * FROM df")
    print(f"  Wrote {len(metrics):,} health metrics", file=sys.stderr)


def _write_routes(
    con: duckdb.DuckDBPyConnection,
    workouts: list[WorkoutRecord],
    export_dir: Path,
) -> None:
    """Parse GPX files and write route points to DuckDB."""
    total_points = 0
    for w in workouts:
        if not w.gpx_path:
            continue

        # GPX paths in the export are relative to the export directory
        # Try several resolution strategies
        gpx_path = _resolve_gpx_path(w.gpx_path, export_dir)
        if gpx_path is None:
            continue

        try:
            points = parse_gpx(gpx_path)
        except Exception as exc:
            print(f"  Warning: failed to parse {gpx_path}: {exc}", file=sys.stderr)
            continue

        if not points:
            continue

        df = pl.DataFrame(
            {
                "workout_id": [w.workout_id] * len(points),
                "timestamp": [p.timestamp for p in points],
                "lat": [p.lat for p in points],
                "lon": [p.lon for p in points],
                "elevation_m": [p.elevation_m for p in points],
            }
        )
        con.execute("INSERT OR REPLACE INTO routes SELECT * FROM df")
        total_points += len(points)

    print(f"  Wrote {total_points:,} route points", file=sys.stderr)


def _resolve_gpx_path(gpx_ref: str, export_dir: Path) -> Path | None:
    """Resolve a GPX file reference to an actual file path."""
    # Apple Health stores paths like "/workout-routes/route_2023-09-16_9.39pm.gpx"
    # Strip leading slash
    relative = gpx_ref.lstrip("/")

    # Try relative to export dir
    candidate = export_dir / relative
    if candidate.is_file():
        return candidate

    # Try under apple_health_export subdirectory
    candidate = export_dir / "apple_health_export" / relative
    if candidate.is_file():
        return candidate

    # Try just the filename in a workout-routes directory
    filename = Path(relative).name
    for routes_dir in [
        export_dir / "workout-routes",
        export_dir / "apple_health_export" / "workout-routes",
    ]:
        candidate = routes_dir / filename
        if candidate.is_file():
            return candidate

    return None


# -- Main pipeline -------------------------------------------------------------


def ingest(export_path: str | Path, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Run the full Apple Health ingest pipeline.

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


def _find_export_xml(export_dir: Path) -> Path | None:
    """Find export.xml inside an export directory."""
    # Direct
    candidate = export_dir / "export.xml"
    if candidate.is_file():
        return candidate
    # Inside apple_health_export subdirectory (common in zip exports)
    candidate = export_dir / "apple_health_export" / "export.xml"
    if candidate.is_file():
        return candidate
    return None


def _ingest_dir(export_dir: Path, db_path: Path) -> None:
    """Ingest from an unzipped export directory."""
    xml_path = _find_export_xml(export_dir)
    if xml_path is None:
        print(f"Error: no export.xml found in {export_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {xml_path} ...", file=sys.stderr)
    result = stream_parse(xml_path)

    print("Matching samples to workouts ...", file=sys.stderr)
    matched = match_samples_to_workouts(result.workouts, result.samples)
    matched_count = sum(len(v) for v in matched.values())
    print(f"  Matched {matched_count:,} samples to {len(matched):,} workouts", file=sys.stderr)

    print(f"Writing to {db_path} ...", file=sys.stderr)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        _write_workouts(con, result.workouts)
        _write_samples(con, matched)
        _write_health_metrics(con, result.health_metrics)
        _write_routes(con, result.workouts, export_dir)
    finally:
        con.close()

    print("Done.", file=sys.stderr)


# -- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest an Apple Health export into the fitness dashboard database."
    )
    parser.add_argument(
        "export_path",
        type=Path,
        help="Path to the export directory or .zip file",
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
