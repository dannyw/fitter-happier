"""Training load enrichment: TRIMP per workout → daily CTL/ATL/TSB.

Computes Banister's TRIMP for each workout using heart rate data, then
rolls up into daily training load metrics (CTL, ATL, TSB) stored in
the ``training_load`` table.

Usage::

    python -m src.enrich.training_load [--db path/to/fitness.duckdb]
"""

from __future__ import annotations

import argparse
import math
from datetime import date, timedelta
from pathlib import Path

import duckdb

from src.storage.schema import ensure_schema

DEFAULT_DB = Path("data/fitness.duckdb")
DEFAULT_RESTING_HR = 60
DEFAULT_MAX_HR = 190
CTL_DAYS = 42
ATL_DAYS = 7


def compute_trimp(
    duration_sec: float,
    avg_hr: float,
    max_hr: float,
    resting_hr: float,
) -> float:
    """Compute Banister's TRIMP for a single workout.

    Returns 0 if inputs are invalid (e.g. avg_hr <= resting_hr).
    """
    if avg_hr <= resting_hr or max_hr <= resting_hr:
        return 0.0
    duration_min = duration_sec / 60.0
    hr_reserve = (avg_hr - resting_hr) / (max_hr - resting_hr)
    # Clamp HR reserve to [0, 1] — avg_hr can exceed estimated max_hr
    hr_reserve = max(0.0, min(1.0, hr_reserve))
    return duration_min * hr_reserve * 0.64 * math.exp(1.92 * hr_reserve)


def _get_resting_hr(con: duckdb.DuckDBPyConnection) -> float:
    """Median resting HR from health_metrics, or DEFAULT_RESTING_HR."""
    row = con.execute(
        "SELECT median(value) FROM health_metrics WHERE metric = 'resting_heart_rate'"
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return DEFAULT_RESTING_HR


def _get_max_hr(con: duckdb.DuckDBPyConnection) -> float:
    """User's observed max HR across all workouts, or DEFAULT_MAX_HR."""
    row = con.execute(
        "SELECT max(max_hr) FROM workouts WHERE max_hr IS NOT NULL"
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return DEFAULT_MAX_HR


def _compute_daily_trimp(
    con: duckdb.DuckDBPyConnection, resting_hr: float, max_hr: float
) -> dict[date, float]:
    """Compute TRIMP per workout, then sum by day.

    Uses the athlete's overall *max_hr* for HR reserve, not the
    per-workout peak.  Per-workout peaks inflate HR reserve for
    low-intensity sessions (e.g. pilates with peak 88 bpm).
    """
    rows = con.execute(
        """
        SELECT
            CAST(start_time AS DATE) AS workout_date,
            duration_sec,
            avg_hr
        FROM workouts
        WHERE avg_hr IS NOT NULL
          AND duration_sec IS NOT NULL
          AND duration_sec > 0
        ORDER BY start_time
        """
    ).fetchall()

    daily: dict[date, float] = {}
    for workout_date, duration_sec, avg_hr in rows:
        trimp = compute_trimp(duration_sec, avg_hr, max_hr, resting_hr)
        daily[workout_date] = daily.get(workout_date, 0.0) + trimp

    return daily


def _compute_ctl_atl_tsb(
    daily_trimp: dict[date, float],
) -> list[tuple[date, float, float, float, float]]:
    """Forward pass over a contiguous date range to compute CTL/ATL/TSB."""
    if not daily_trimp:
        return []

    start = min(daily_trimp)
    end = max(daily_trimp)

    alpha_ctl = 2.0 / (CTL_DAYS + 1)
    alpha_atl = 2.0 / (ATL_DAYS + 1)

    ctl = 0.0
    atl = 0.0
    results: list[tuple[date, float, float, float, float]] = []

    current = start
    while current <= end:
        trimp = daily_trimp.get(current, 0.0)
        ctl = ctl + alpha_ctl * (trimp - ctl)
        atl = atl + alpha_atl * (trimp - atl)
        tsb = ctl - atl
        results.append((current, trimp, ctl, atl, tsb))
        current += timedelta(days=1)

    return results


def enrich_training_load(db_path: Path = DEFAULT_DB) -> int:
    """Full recompute of training_load table. Returns row count written."""
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
        resting_hr = _get_resting_hr(con)
        max_hr = _get_max_hr(con)
        daily_trimp = _compute_daily_trimp(con, resting_hr, max_hr)
        rows = _compute_ctl_atl_tsb(daily_trimp)

        if not rows:
            return 0

        con.execute("DELETE FROM training_load")
        con.executemany(
            "INSERT INTO training_load (date, trimp, ctl, atl, tsb) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)
    finally:
        con.close()


def enrich_training_load_con(con: duckdb.DuckDBPyConnection) -> int:
    """Same as enrich_training_load but takes an open connection (for testing)."""
    ensure_schema(con)
    resting_hr = _get_resting_hr(con)
    max_hr = _get_max_hr(con)
    daily_trimp = _compute_daily_trimp(con, resting_hr, max_hr)
    rows = _compute_ctl_atl_tsb(daily_trimp)

    if not rows:
        return 0

    con.execute("DELETE FROM training_load")
    con.executemany(
        "INSERT INTO training_load (date, trimp, ctl, atl, tsb) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute training load metrics")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB path")
    args = parser.parse_args()

    count = enrich_training_load(args.db)
    print(f"training_load: wrote {count} daily rows")


if __name__ == "__main__":
    main()
