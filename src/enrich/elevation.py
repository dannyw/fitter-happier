"""Backfill elevation gain for Apple Health workouts.

Apple Health stores ``HKElevationAscended`` as strings like ``"418.36 m"``
or ``"4731 cm"`` (value + unit).  Earlier versions of the ingest pipeline
tried ``float(val)`` with a hardcoded ``"m"`` unit, which silently failed
on any value containing a unit suffix.  This enricher parses the stored
``raw`` JSON and fills in ``elevation_gain_m`` for affected rows.

Usage::

    python -m src.enrich.elevation [--db path/to/fitness.duckdb]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from src.ingest.units import elevation_to_meters
from src.storage.schema import ensure_schema

DEFAULT_DB = Path("data/fitness.duckdb")


def backfill_elevation(db_path: Path = DEFAULT_DB) -> None:
    """Fill in NULL elevation_gain_m from raw JSON metadata."""
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)

        rows = con.execute(
            "SELECT workout_id, raw FROM workouts "
            "WHERE elevation_gain_m IS NULL AND raw IS NOT NULL"
        ).fetchall()

        if not rows:
            print("All workouts already have elevation data (or no raw JSON).", file=sys.stderr)
            return

        print(f"Checking {len(rows):,} workouts for elevation data ...", file=sys.stderr)

        updated = 0
        for wid, raw_json in rows:
            try:
                data = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue

            val = data.get("metadata", {}).get("HKElevationAscended", "")
            if not val:
                continue

            parts = val.strip().split()
            try:
                if len(parts) == 2:
                    elev_m = elevation_to_meters(float(parts[0]), parts[1])
                else:
                    elev_m = elevation_to_meters(float(val), "m")
            except (ValueError, TypeError):
                continue

            con.execute(
                "UPDATE workouts SET elevation_gain_m = ? WHERE workout_id = ?",
                [elev_m, wid],
            )
            updated += 1

        print(f"  Updated {updated:,} workouts with elevation data.", file=sys.stderr)
    finally:
        con.close()

    print("Done.", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill elevation gain from raw metadata.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB})",
    )
    args = parser.parse_args(argv)
    backfill_elevation(args.db)


if __name__ == "__main__":
    main()
