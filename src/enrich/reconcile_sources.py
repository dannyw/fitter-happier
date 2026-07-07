"""Reconcile duplicate workouts across sources.

The Strava ingest only dedups against workouts that *already exist* when it
runs (see DESIGN.md: "Apple Health must run before Strava"). If Strava sync
runs first and inserts standalone ``source='strava'`` rows, a later Apple
Health ingest adds its own ``source='apple_health'`` row for the same physical
workout -- a duplicate that nothing cleans up.

This enricher closes that gap. It finds Strava/Apple-Health pairs that describe
the same workout (same normalized activity type, start within 5 min, distance
within 20% -- identical criteria to ingest-time matching), merges the Strava
metadata/routes/samples into the Apple Health row, and deletes the orphan
Strava row. Idempotent: once an orphan is merged away, later runs are no-ops.

Usage::

    python -m src.enrich.reconcile_sources
    python -m src.enrich.reconcile_sources --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

# Reuse ingest-time match criteria and the metadata-merge helper so the two
# code paths can never disagree about what "the same workout" means.
from src.ingest.strava import (
    _DISTANCE_TOLERANCE,
    _TIME_TOLERANCE_MIN,
    _enrich_existing_workout,
)

DEFAULT_DB_PATH = Path("data/fitness.duckdb")

# Child tables keyed by workout_id, copied from the orphan to the kept row.
_CHILD_TABLES = ("routes", "workout_samples", "weather")


def _distance_matches(a: float | None, b: float | None) -> bool:
    """Mirror _find_matching_workout: match if either lacks distance, else
    require the two distances within tolerance."""
    if a is None or b is None or a <= 0 or b <= 0:
        return True
    return abs(a - b) / max(a, b) <= _DISTANCE_TOLERANCE


def _find_pairs(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Return (strava_wid, apple_health_wid) pairs describing the same workout.

    Each Strava orphan is matched to its single nearest Apple Health workout
    (by start time) within the type/time/distance window.
    """
    strava = con.execute(
        """
        SELECT workout_id, activity_type, start_time, distance_m
        FROM workouts WHERE source = 'strava'
        ORDER BY start_time
        """
    ).fetchall()
    ah = con.execute(
        """
        SELECT workout_id, activity_type, start_time, distance_m
        FROM workouts WHERE source = 'apple_health'
        ORDER BY start_time
        """
    ).fetchall()

    # Bucket Apple Health rows by activity type for cheap lookup.
    ah_by_type: dict[str, list[tuple[str, datetime, float | None]]] = {}
    for wid, atype, start, dist in ah:
        ah_by_type.setdefault(atype, []).append((wid, start, dist))

    pairs: list[tuple[str, str]] = []
    window = timedelta(minutes=_TIME_TOLERANCE_MIN)
    for s_wid, s_type, s_start, s_dist in strava:
        best: tuple[str, timedelta] | None = None
        for a_wid, a_start, a_dist in ah_by_type.get(s_type, []):
            delta = abs(a_start - s_start)
            if (
                delta <= window
                and _distance_matches(s_dist, a_dist)
                and (best is None or delta < best[1])
            ):
                best = (a_wid, delta)
        if best is not None:
            pairs.append((s_wid, best[0]))
    return pairs


def _backfill_children(
    con: duckdb.DuckDBPyConnection, strava_wid: str, ah_wid: str
) -> None:
    """Copy route/sample/weather rows from the orphan to the kept row, but only
    for tables where the kept row currently has nothing."""
    for table in _CHILD_TABLES:
        kept_row = con.execute(
            f"SELECT count(*) FROM {table} WHERE workout_id = ?", [ah_wid]
        ).fetchone()
        if kept_row and kept_row[0]:
            continue  # Apple Health already has its own data -- don't clobber.
        # Re-key the orphan's rows to the kept workout_id. Every child table's
        # first column is workout_id; the rest carry over unchanged.
        con.execute(
            f"""
            INSERT OR REPLACE INTO {table}
            SELECT ? AS workout_id, * EXCLUDE (workout_id)
            FROM {table} WHERE workout_id = ?
            """,
            [ah_wid, strava_wid],
        )


def _delete_orphan(con: duckdb.DuckDBPyConnection, strava_wid: str) -> None:
    """Remove the orphan Strava workout and all its child rows."""
    for table in _CHILD_TABLES:
        con.execute(f"DELETE FROM {table} WHERE workout_id = ?", [strava_wid])
    con.execute("DELETE FROM workouts WHERE workout_id = ?", [strava_wid])


def _strava_raw(
    con: duckdb.DuckDBPyConnection, strava_wid: str
) -> dict[str, str | None]:
    """The Strava row's raw JSON is the original activities.csv row -- exactly
    the dict shape _enrich_existing_workout expects."""
    row = con.execute(
        "SELECT raw FROM workouts WHERE workout_id = ?", [strava_wid]
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return {}


def reconcile_con(con: duckdb.DuckDBPyConnection, *, dry_run: bool = False) -> int:
    """Merge Strava orphans into their Apple Health twins on an open connection.

    Returns the number of duplicate pairs found (dry run) or reconciled.
    """
    pairs = _find_pairs(con)
    if dry_run:
        for s_wid, a_wid in pairs:
            print(f"  would merge strava {s_wid} -> apple_health {a_wid}")
        print(f"{len(pairs)} duplicate pair(s) found (dry run, no changes)")
        return len(pairs)

    for s_wid, a_wid in pairs:
        con.execute("BEGIN")
        try:
            _enrich_existing_workout(con, a_wid, _strava_raw(con, s_wid))
            _backfill_children(con, s_wid, a_wid)
            _delete_orphan(con, s_wid)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    print(f"Reconciled {len(pairs)} duplicate pair(s).")
    return len(pairs)


def reconcile(
    db_path: str | Path = DEFAULT_DB_PATH, *, dry_run: bool = False
) -> int:
    """Open *db_path* and reconcile Strava orphans into Apple Health twins."""
    con = duckdb.connect(str(db_path))
    try:
        return reconcile_con(con, dry_run=dry_run)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile duplicate workouts across Apple Health and Strava."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates without modifying the database.",
    )
    args = parser.parse_args(argv)
    reconcile(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
