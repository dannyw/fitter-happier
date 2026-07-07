"""Tests for src.enrich.reconcile_sources -- cross-source dedup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from src.enrich.reconcile_sources import _distance_matches, reconcile_con
from src.storage.schema import ensure_schema


def _insert_workout(
    con: duckdb.DuckDBPyConnection,
    *,
    wid: str,
    source: str,
    activity_type: str,
    start: datetime,
    distance_m: float | None,
    name: str | None = None,
    raw: str = "{}",
) -> None:
    con.execute(
        """
        INSERT INTO workouts
            (workout_id, source, source_id, activity_type, name,
             start_time, end_time, duration_sec, distance_m, energy_kcal,
             avg_hr, max_hr, elevation_gain_m, device, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            wid, source, wid, activity_type, name,
            start, start + timedelta(minutes=30), 1800, distance_m, None,
            None, None, None, None, raw,
        ],
    )


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    ensure_schema(c)
    return c


class TestDistanceMatches:
    def test_within_tolerance(self) -> None:
        assert _distance_matches(5000.0, 5500.0) is True  # 10% apart

    def test_outside_tolerance(self) -> None:
        assert _distance_matches(5000.0, 7000.0) is False  # 40% apart

    def test_missing_distance_matches(self) -> None:
        assert _distance_matches(None, 5000.0) is True
        assert _distance_matches(5000.0, None) is True


class TestReconcile:
    def test_merges_orphan_into_apple_health(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        start = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)

        # Apple Health row: no name, no routes/samples of its own.
        _insert_workout(
            con, wid="ah1", source="apple_health", activity_type="running",
            start=start, distance_m=5000.0,
        )
        # Strava orphan: 2 min later, distance within tolerance, has metadata.
        strava_raw = json.dumps({
            "Activity ID": "999",
            "Activity Name": "Morning Run",
            "Activity Description": "felt great",
            "Activity Gear": "Pegasus",
        })
        _insert_workout(
            con, wid="st1", source="strava", activity_type="running",
            start=start + timedelta(minutes=2), distance_m=5200.0,
            name="Morning Run", raw=strava_raw,
        )
        # Strava-only child data that should migrate to the AH row.
        con.execute(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            ["st1", start, 40.0, -74.0, 10.0],
        )
        con.execute(
            "INSERT INTO workout_samples VALUES (?, ?, ?, ?)",
            ["st1", start, "heart_rate", 150.0],
        )
        con.execute(
            "INSERT INTO weather VALUES (?, ?, ?, ?, ?, ?)",
            ["st1", 12.0, 60.0, 2.0, 0.0, "clear"],
        )

        assert reconcile_con(con) == 1

        # Orphan and its children are gone.
        assert con.execute(
            "SELECT count(*) FROM workouts WHERE source = 'strava'"
        ).fetchone()[0] == 0
        for table in ("routes", "workout_samples", "weather"):
            assert con.execute(
                f"SELECT count(*) FROM {table} WHERE workout_id = 'st1'"
            ).fetchone()[0] == 0

        # AH row survives, gained the Strava name + metadata.
        name, raw = con.execute(
            "SELECT name, raw FROM workouts WHERE workout_id = 'ah1'"
        ).fetchone()
        assert name == "Morning Run"
        merged = json.loads(raw)
        assert merged["strava_activity_id"] == "999"
        assert merged["strava_gear"] == "Pegasus"

        # Child rows were re-keyed to the AH workout.
        for table in ("routes", "workout_samples", "weather"):
            assert con.execute(
                f"SELECT count(*) FROM {table} WHERE workout_id = 'ah1'"
            ).fetchone()[0] == 1

    def test_does_not_clobber_existing_apple_health_children(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        start = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        _insert_workout(
            con, wid="ah1", source="apple_health", activity_type="running",
            start=start, distance_m=5000.0,
        )
        _insert_workout(
            con, wid="st1", source="strava", activity_type="running",
            start=start, distance_m=5000.0,
        )
        # Both have a HR sample; AH's must win.
        con.execute(
            "INSERT INTO workout_samples VALUES (?, ?, ?, ?)",
            ["ah1", start, "heart_rate", 140.0],
        )
        con.execute(
            "INSERT INTO workout_samples VALUES (?, ?, ?, ?)",
            ["st1", start, "heart_rate", 199.0],
        )

        reconcile_con(con)

        rows = con.execute(
            "SELECT value FROM workout_samples WHERE workout_id = 'ah1'"
        ).fetchall()
        assert rows == [(140.0,)]

    def test_leaves_unmatched_strava_alone(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        # AH run on day 1, Strava ride on day 2 -- different type + time.
        _insert_workout(
            con, wid="ah1", source="apple_health", activity_type="running",
            start=datetime(2026, 3, 1, 14, 0, tzinfo=UTC), distance_m=5000.0,
        )
        _insert_workout(
            con, wid="st1", source="strava", activity_type="cycling",
            start=datetime(2026, 3, 2, 9, 0, tzinfo=UTC), distance_m=20000.0,
        )

        assert reconcile_con(con) == 0
        assert con.execute(
            "SELECT count(*) FROM workouts WHERE source = 'strava'"
        ).fetchone()[0] == 1

    def test_idempotent(self, con: duckdb.DuckDBPyConnection) -> None:
        start = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        _insert_workout(
            con, wid="ah1", source="apple_health", activity_type="running",
            start=start, distance_m=5000.0,
        )
        _insert_workout(
            con, wid="st1", source="strava", activity_type="running",
            start=start, distance_m=5000.0,
        )
        assert reconcile_con(con) == 1
        assert reconcile_con(con) == 0  # nothing left to merge
