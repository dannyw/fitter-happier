"""Integration tests for src.ingest.apple_health.

Ingests the mini fixture into an in-memory DuckDB and verifies all tables.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import duckdb
import pytest

from src.ingest.apple_health import (
    SampleRecord,
    WorkoutRecord,
    match_samples_to_workouts,
    stream_parse,
)
from src.ingest.timestamps import parse_apple_health_ts
from src.storage.schema import ensure_schema

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture()
def export_dir(tmp_path: Path) -> Path:
    """Create a temporary export directory with fixtures."""
    # Copy export.xml
    shutil.copy(FIXTURES / "mini_export.xml", tmp_path / "export.xml")
    # Create workout-routes dir and copy GPX
    routes_dir = tmp_path / "workout-routes"
    routes_dir.mkdir()
    shutil.copy(FIXTURES / "mini_route.gpx", routes_dir / "route_2024-01-15.gpx")
    return tmp_path


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with schema applied."""
    c = duckdb.connect(":memory:")
    ensure_schema(c)
    yield c
    c.close()


class TestStreamParse:
    def test_parses_workouts(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        assert len(result.workouts) == 2

    def test_running_workout(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        running = [w for w in result.workouts if w.activity_type == "running"][0]
        assert running.source == "apple_health"
        assert running.source_id == "abc-123-def"
        assert running.distance_m == pytest.approx(5200.0)
        assert running.energy_kcal == pytest.approx(380.0)
        assert running.avg_hr == pytest.approx(150.0)
        assert running.max_hr == pytest.approx(172.0)
        assert running.elevation_gain_m == pytest.approx(42.5)
        assert running.gpx_path == "/workout-routes/route_2024-01-15.gpx"

    def test_yoga_workout(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        yoga = [w for w in result.workouts if w.activity_type == "yoga"][0]
        assert yoga.distance_m is None
        assert yoga.source_id is None

    def test_parses_hr_samples(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        hr_samples = [s for s in result.samples if s.metric == "heart_rate"]
        assert len(hr_samples) == 5

    def test_parses_health_metrics(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        # resting HR + HRV + sleep_core = 3
        assert len(result.health_metrics) == 3

    def test_resting_hr_metric(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        rhr = [m for m in result.health_metrics if m.metric == "resting_heart_rate"]
        assert len(rhr) == 1
        assert rhr[0].value == 52.0

    def test_sleep_metric(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        sleep = [m for m in result.health_metrics if m.metric == "sleep_core"]
        assert len(sleep) == 1
        assert sleep[0].value == pytest.approx(2.0)  # 2 hours
        assert sleep[0].unit == "hours"


class TestMatchSamples:
    def test_matches_samples_to_correct_workout(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        matched = match_samples_to_workouts(result.workouts, result.samples)

        running = [w for w in result.workouts if w.activity_type == "running"][0]
        yoga = [w for w in result.workouts if w.activity_type == "yoga"][0]

        # 3 HR samples fall within the running workout window
        assert len(matched.get(running.workout_id, [])) == 3
        # 2 HR samples fall within the yoga workout window
        assert len(matched.get(yoga.workout_id, [])) == 2

    def test_empty_samples(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        matched = match_samples_to_workouts(result.workouts, [])
        assert matched == {}

    def test_empty_workouts(self) -> None:
        result = stream_parse(FIXTURES / "mini_export.xml")
        matched = match_samples_to_workouts([], result.samples)
        assert matched == {}


class TestFullIngest:
    def test_ingest_writes_all_tables(self, export_dir: Path, con: duckdb.DuckDBPyConnection) -> None:
        """Full pipeline: parse → match → write to DuckDB."""
        from src.ingest.apple_health import (
            _write_health_metrics,
            _write_routes,
            _write_samples,
            _write_workouts,
        )

        result = stream_parse(export_dir / "export.xml")
        matched = match_samples_to_workouts(result.workouts, result.samples)

        _write_workouts(con, result.workouts)
        _write_samples(con, matched)
        _write_health_metrics(con, result.health_metrics)
        _write_routes(con, result.workouts, export_dir)

        # Check workouts
        workouts = con.execute("SELECT * FROM workouts ORDER BY start_time").fetchall()
        assert len(workouts) == 2

        # Check workout_samples
        samples = con.execute("SELECT * FROM workout_samples").fetchall()
        assert len(samples) == 5

        # Check health_metrics
        metrics = con.execute("SELECT * FROM health_metrics").fetchall()
        assert len(metrics) == 3

        # Check routes
        routes = con.execute("SELECT * FROM routes").fetchall()
        assert len(routes) == 5

    def test_ingest_is_idempotent(self, export_dir: Path, con: duckdb.DuckDBPyConnection) -> None:
        """Running ingest twice produces the same result (no duplicates)."""
        from src.ingest.apple_health import (
            _write_health_metrics,
            _write_routes,
            _write_samples,
            _write_workouts,
        )

        for _ in range(2):
            result = stream_parse(export_dir / "export.xml")
            matched = match_samples_to_workouts(result.workouts, result.samples)
            _write_workouts(con, result.workouts)
            _write_samples(con, matched)
            _write_health_metrics(con, result.health_metrics)
            _write_routes(con, result.workouts, export_dir)

        assert con.execute("SELECT count(*) FROM workouts").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM workout_samples").fetchone()[0] == 5
        assert con.execute("SELECT count(*) FROM health_metrics").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM routes").fetchone()[0] == 5
