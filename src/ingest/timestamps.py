"""Apple Health timestamp parsing.

Apple Health exports timestamps like ``"2023-09-16 21:39:13 -0400"``.
We parse these into timezone-aware datetimes, then convert to UTC for storage.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_apple_health_ts(ts: str) -> datetime:
    """Parse an Apple Health timestamp string into a UTC-aware datetime.

    Input format: ``"2023-09-16 21:39:13 -0400"``
    Returns a ``datetime`` with ``tzinfo=timezone.utc``.
    """
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
    return dt.astimezone(timezone.utc)
