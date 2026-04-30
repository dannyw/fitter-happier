"""Tests for src.ingest.strava — Strava bulk export ingest."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from src.ingest.strava import (
    _find_matching_workout,
    _parse_strava_ts,
    ingest,
    parse_activities_csv,
)
from src.storage.schema import ensure_schema

FIXTURES = Path(__file__).parent.parent / "fixtures"


# -- Timestamp parsing ---------------------------------------------------------


class TestParseStravaTs:
    def test_standard_format(self) -> None:
        ts = _parse_strava_ts("Jan 15, 2024, 6:05:00 AM")
        assert ts == datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)

    def test_pm_format(self) -> None:
        ts = _parse_strava_ts("Dec 1, 2023, 8:30:00 PM")
        assert ts == datetime(2023, 12, 1, 20, 30, 0, tzinfo=timezone.utc)

    def test_iso_format(self) -> None:
        ts = _parse_strava_ts("2024-01-15 06:05:00")
        assert ts == datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)

    def test_result_is_utc(self) -> None:
        ts = _parse_strava_ts("Jan 15, 2024, 6:05:00 AM")
        assert ts.tzinfo == timezone.utc

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_strava_ts("not a date")


# -- CSV parsing ---------------------------------------------------------------


class TestParseActivitiesCsv:
    def test_reads_all_rows(self) -> None:
        df = parse_activities_csv(FIXTURES / "mini_strava_activities.csv")
        assert len(df) == 3

    def test_activity_ids(self) -> None:
        df = parse_activities_csv(FIXTURES / "mini_strava_activities.csv")
        ids = df["Activity ID"].to_list()
        assert "12345678" in ids
        assert "23456789" in ids
        assert "34567890" in ids

    def test_activity_types(self) -> None:
        df = parse_activities_csv(FIXTURES / "mini_strava_activities.csv")
        types = df["Activity Type"].to_list()
        assert "Run" in types
        assert "Yoga" in types

    def test_columns_are_strings(self) -> None:
        """All columns should be read as strings (infer_schema_length=0)."""
        df = parse_activities_csv(FIXTURES / "mini_strava_activities.csv")
        # Distance should be a string, not auto-parsed as float
        import polars as pl

        assert df["Distance"].dtype == pl.Utf8


# -- Matching logic ------------------------------------------------------------


class TestFindMatchingWorkout:
    @pytest.fixture()
    def con(self) -> duckdb.DuckDBPyConnection:
        c = duckdb.connect(":memory:")
        ensure_schema(c)
        yield c
        c.close()

    def _insert_workout(
        self,
        con: duckdb.DuckDBPyConnection,
        wid: str,
        activity_type: str,
        start_time: datetime,
        distance_m: float | None,
    ) -> None:
        con.execute(
            """INSERT INTO workouts (workout_id, source, activity_type, start_time,
               end_time, duration_sec, distance_m, raw)
               VALUES (?, 'apple_health', ?, ?, ?, 1800, ?, '{}')""",
            [wid, activity_type, start_time, start_time + timedelta(seconds=1800), distance_m],
        )

    def test_exact_match(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "running", ts, 5000.0)
        result = _find_matching_workout(con, "running", ts, 5200.0)
        assert result == "w1"

    def test_time_within_tolerance(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "running", ts, 5000.0)
        # 3 minutes later → should match
        result = _find_matching_workout(
            con, "running", ts + timedelta(minutes=3), 5200.0
        )
        assert result == "w1"

    def test_time_outside_tolerance(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "running", ts, 5000.0)
        # 10 minutes later → no match
        result = _find_matching_workout(
            con, "running", ts + timedelta(minutes=10), 5200.0
        )
        assert result is None

    def test_wrong_activity_type(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "running", ts, 5000.0)
        result = _find_matching_workout(con, "cycling", ts, 5200.0)
        assert result is None

    def test_distance_too_different(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "running", ts, 5000.0)
        # 50% difference → no match
        result = _find_matching_workout(con, "running", ts, 10000.0)
        assert result is None

    def test_no_distance_still_matches(self, con: duckdb.DuckDBPyConnection) -> None:
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        self._insert_workout(con, "w1", "yoga", ts, None)
        result = _find_matching_workout(con, "yoga", ts, None)
        assert result == "w1"


# -- Full pipeline (integration) -----------------------------------------------


class TestFullPipeline:
    @pytest.fixture()
    def strava_dir(self, tmp_path: Path) -> Path:
        """Create a Strava export directory with fixtures."""
        shutil.copy(FIXTURES / "mini_strava_activities.csv", tmp_path / "activities.csv")
        # Copy activity files referenced in CSV
        acts_dir = tmp_path / "activities"
        acts_dir.mkdir()
        # We don't have real .fit.gz or .tcx files with matching IDs,
        # so the parser will skip them gracefully
        return tmp_path

    def test_inserts_all_new_workouts(self, strava_dir: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.duckdb"
        ingest(strava_dir, db_path)

        con = duckdb.connect(str(db_path))
        try:
            count = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
            assert count == 3
        finally:
            con.close()

    def test_workout_fields(self, strava_dir: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.duckdb"
        ingest(strava_dir, db_path)

        con = duckdb.connect(str(db_path))
        try:
            rows = con.execute(
                "SELECT source, activity_type, distance_m, avg_hr FROM workouts ORDER BY start_time"
            ).fetchall()

            # Old Race (Dec 2023)
            assert rows[0][0] == "strava"
            assert rows[0][1] == "running"
            assert rows[0][2] == pytest.approx(21100.0)
            assert rows[0][3] == pytest.approx(165.0)

            # Morning Run (Jan 15)
            assert rows[1][1] == "running"
            assert rows[1][2] == pytest.approx(5200.0)
            assert rows[1][3] == pytest.approx(150.0)

            # Yoga Flow (Jan 16)
            assert rows[2][1] == "yoga"
        finally:
            con.close()

    def test_idempotent(self, strava_dir: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.duckdb"
        ingest(strava_dir, db_path)
        ingest(strava_dir, db_path)

        con = duckdb.connect(str(db_path))
        try:
            count = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
            assert count == 3
        finally:
            con.close()

    def test_enriches_existing_workout(self, tmp_path: Path) -> None:
        """When an Apple Health workout matches, Strava enriches instead of inserting."""
        db_path = tmp_path / "test.duckdb"

        # Pre-populate with an Apple Health workout matching first Strava row
        con = duckdb.connect(str(db_path))
        ensure_schema(con)
        ts = datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)
        con.execute(
            """INSERT INTO workouts (workout_id, source, activity_type, start_time,
               end_time, duration_sec, distance_m, raw)
               VALUES ('ah_w1', 'apple_health', 'running', ?, ?, 1800, 5200.0, '{}')""",
            [ts, ts + timedelta(seconds=1800)],
        )
        initial_count = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
        con.close()

        # Now ingest Strava
        strava_dir = tmp_path / "strava"
        strava_dir.mkdir()
        shutil.copy(FIXTURES / "mini_strava_activities.csv", strava_dir / "activities.csv")
        ingest(strava_dir, db_path)

        con = duckdb.connect(str(db_path))
        try:
            # Should have 3 total: 1 enriched (not duplicated) + 2 new from Strava
            count = con.execute("SELECT count(*) FROM workouts").fetchone()[0]
            assert count == initial_count + 2  # yoga + old race added, running enriched

            # Check the enriched workout has Strava metadata in raw
            raw = con.execute(
                "SELECT raw FROM workouts WHERE workout_id = 'ah_w1'"
            ).fetchone()[0]
            raw_data = json.loads(raw)
            assert raw_data["strava_activity_name"] == "Morning Run"
            assert raw_data["strava_gear"] == "Pegasus 40"
            assert raw_data["strava_activity_id"] == "12345678"
        finally:
            con.close()
