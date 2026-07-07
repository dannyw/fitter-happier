"""Read/write the incremental-sync cursor stored in the ``sync_state`` table.

The cursor is the high-water mark for a source's API sync: the newest workout
start time seen so far. The next sync fetches activities *after* it.
"""

from __future__ import annotations

from datetime import datetime

import duckdb


def get_cursor(con: duckdb.DuckDBPyConnection, source: str) -> datetime | None:
    """Return the last-synced timestamp for *source*, or None if never synced."""
    row = con.execute(
        "SELECT last_synced FROM sync_state WHERE source = ?", [source]
    ).fetchone()
    return row[0] if row else None


def set_cursor(
    con: duckdb.DuckDBPyConnection, source: str, last_synced: datetime, *, now: datetime
) -> None:
    """Upsert the cursor for *source*.

    *now* is passed in (not read from the clock) so callers stay deterministic
    and testable.
    """
    con.execute(
        """
        INSERT OR REPLACE INTO sync_state (source, last_synced, updated_at)
        VALUES (?, ?, ?)
        """,
        [source, last_synced, now],
    )
