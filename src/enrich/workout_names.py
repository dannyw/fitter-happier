"""Backfill workout names for rows where ``name`` is NULL.

For workouts with Strava metadata (``strava_activity_name`` in ``raw`` JSON),
uses the Strava name.  Otherwise generates a default from time-of-day +
activity type (e.g. "Morning Run").

Usage::

    python -m src.enrich.workout_names [--db path/to/fitness.duckdb]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from src.ingest.workout_names import generate_default_name
from src.storage.schema import ensure_schema

DEFAULT_DB = Path("data/fitness.duckdb")


def backfill_names(db_path: Path = DEFAULT_DB) -> None:
    """Fill in NULL workout names from Strava metadata or generated defaults."""
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)

        rows = con.execute(
            "SELECT workout_id, activity_type, start_time, raw "
            "FROM workouts WHERE name IS NULL"
        ).fetchall()

        if not rows:
            print("All workouts already have names.", file=sys.stderr)
            return

        print(f"Backfilling names for {len(rows):,} workouts ...", file=sys.stderr)

        for wid, activity_type, start_time, raw_json in rows:
            name: str | None = None

            # Try Strava name from raw JSON
            if raw_json:
                try:
                    raw_data = json.loads(raw_json)
                    name = raw_data.get("strava_activity_name")
                except (json.JSONDecodeError, TypeError):
                    pass

            # Fall back to generated default
            if not name:
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time)
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=UTC)
                name = generate_default_name(activity_type or "other", start_time)

            con.execute(
                "UPDATE workouts SET name = ? WHERE workout_id = ?",
                [name, wid],
            )

        print(f"  Updated {len(rows):,} workouts.", file=sys.stderr)
    finally:
        con.close()

    print("Done.", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill workout names.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB})",
    )
    args = parser.parse_args(argv)
    backfill_names(args.db)


if __name__ == "__main__":
    main()
