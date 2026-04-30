"""Stable workout ID generation.

A workout_id is a deterministic hash so the same workout always gets the same
ID regardless of when or how many times it is ingested.
"""

from __future__ import annotations

import hashlib


def workout_id(source: str, source_id: str | None, start_time: str, activity_type: str) -> str:
    """Generate a stable workout ID.

    If *source_id* is available (e.g. an external UUID), hash ``(source, source_id)``.
    Otherwise fall back to ``(source, start_time, activity_type)``.

    Returns a 16-character hex string.
    """
    if source_id:
        key = f"{source}:{source_id}"
    else:
        key = f"{source}:{start_time}:{activity_type}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
