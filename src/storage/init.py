"""CLI entry point: python -m src.storage.init

Creates the DuckDB database file (default: data/fitness.duckdb) with the
full schema applied. Safe to re-run — all CREATE statements are IF NOT EXISTS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from src.storage.schema import ensure_schema

DEFAULT_DB_PATH = Path("data/fitness.duckdb")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the fitness dashboard DuckDB database."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the DuckDB file (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    db_path: Path = args.db
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)
    finally:
        con.close()

    print(f"Database ready at {db_path}")


if __name__ == "__main__":
    main()
