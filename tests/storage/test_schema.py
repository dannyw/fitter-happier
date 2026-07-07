"""Tests for src.storage.schema — verifies DDL applies cleanly and is idempotent."""

from __future__ import annotations

import duckdb
import pytest

from src.storage.schema import ensure_schema


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection, closed after each test."""
    c = duckdb.connect(":memory:")
    yield c
    c.close()


EXPECTED_TABLES = {
    "workouts",
    "workout_samples",
    "health_metrics",
    "routes",
    "weather",
    "training_load",
    "sync_state",
}


def test_ensure_schema_creates_tables(con: duckdb.DuckDBPyConnection) -> None:
    ensure_schema(con)

    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert tables == EXPECTED_TABLES


def test_ensure_schema_is_idempotent(con: duckdb.DuckDBPyConnection) -> None:
    ensure_schema(con)
    ensure_schema(con)  # must not raise

    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert tables == EXPECTED_TABLES


def test_workouts_columns(con: duckdb.DuckDBPyConnection) -> None:
    ensure_schema(con)

    cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'workouts'"
        ).fetchall()
    }
    expected = {
        "workout_id",
        "source",
        "source_id",
        "activity_type",
        "name",
        "start_time",
        "end_time",
        "duration_sec",
        "distance_m",
        "energy_kcal",
        "avg_hr",
        "max_hr",
        "elevation_gain_m",
        "device",
        "raw",
    }
    assert cols == expected


def test_workouts_primary_key_enforced(con: duckdb.DuckDBPyConnection) -> None:
    ensure_schema(con)

    con.execute(
        "INSERT INTO workouts (workout_id, source, activity_type) "
        "VALUES ('w1', 'test', 'running')"
    )
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO workouts (workout_id, source, activity_type) "
            "VALUES ('w1', 'test', 'cycling')"
        )
