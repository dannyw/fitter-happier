"""DuckDB schema definition for the fitness dashboard.

Defines the DDL for all tables and provides `ensure_schema()` to apply it
idempotently to a DuckDB connection.
"""

from __future__ import annotations

import duckdb

# -- DDL statements, one per table. Using TIMESTAMPTZ so values are stored as
# UTC internally (CLAUDE.md: "store as UTC; convert at the edges").

WORKOUTS_DDL = """\
CREATE TABLE IF NOT EXISTS workouts (
    workout_id       TEXT PRIMARY KEY,
    source           TEXT,
    source_id        TEXT,
    activity_type    TEXT,
    name             TEXT,
    start_time       TIMESTAMPTZ,
    end_time         TIMESTAMPTZ,
    duration_sec     INTEGER,
    distance_m       DOUBLE,
    energy_kcal      DOUBLE,
    avg_hr           DOUBLE,
    max_hr           DOUBLE,
    elevation_gain_m DOUBLE,
    device           TEXT,
    raw              JSON
);
"""

WORKOUT_SAMPLES_DDL = """\
CREATE TABLE IF NOT EXISTS workout_samples (
    workout_id  TEXT,
    timestamp   TIMESTAMPTZ,
    metric      TEXT,
    value       DOUBLE,
    PRIMARY KEY (workout_id, timestamp, metric)
);
"""

HEALTH_METRICS_DDL = """\
CREATE TABLE IF NOT EXISTS health_metrics (
    metric     TEXT,
    start_time TIMESTAMPTZ,
    end_time   TIMESTAMPTZ,
    value      DOUBLE,
    unit       TEXT,
    source     TEXT,
    PRIMARY KEY (metric, start_time, source)
);
"""

ROUTES_DDL = """\
CREATE TABLE IF NOT EXISTS routes (
    workout_id  TEXT,
    timestamp   TIMESTAMPTZ,
    lat         DOUBLE,
    lon         DOUBLE,
    elevation_m DOUBLE,
    PRIMARY KEY (workout_id, timestamp)
);
"""

WEATHER_DDL = """\
CREATE TABLE IF NOT EXISTS weather (
    workout_id       TEXT PRIMARY KEY,
    temp_c           DOUBLE,
    humidity_pct     DOUBLE,
    wind_speed_mps   DOUBLE,
    precipitation_mm DOUBLE,
    conditions       TEXT
);
"""

TRAINING_LOAD_DDL = """\
CREATE TABLE IF NOT EXISTS training_load (
    date    DATE PRIMARY KEY,
    trimp   DOUBLE,
    ctl     DOUBLE,
    atl     DOUBLE,
    tsb     DOUBLE
);
"""

ALL_DDL = [
    WORKOUTS_DDL,
    WORKOUT_SAMPLES_DDL,
    HEALTH_METRICS_DDL,
    ROUTES_DDL,
    WEATHER_DDL,
    TRAINING_LOAD_DDL,
]


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all tables if they don't already exist."""
    for ddl in ALL_DDL:
        con.execute(ddl)

    # -- Migrations ----------------------------------------------------------
    # Add 'name' column to workouts (for existing databases).
    con.execute("ALTER TABLE workouts ADD COLUMN IF NOT EXISTS name TEXT")
