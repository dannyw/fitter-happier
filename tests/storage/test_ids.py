"""Tests for src.storage.ids — stable workout ID generation."""

from __future__ import annotations

from src.storage.ids import workout_id


def test_deterministic_with_source_id() -> None:
    """Same inputs always produce the same ID."""
    id1 = workout_id("apple_health", "abc-123", "2024-01-15T06:00:00+00:00", "running")
    id2 = workout_id("apple_health", "abc-123", "2024-01-15T06:00:00+00:00", "running")
    assert id1 == id2


def test_source_id_takes_precedence() -> None:
    """When source_id is present, start_time and activity_type don't affect the hash."""
    id1 = workout_id("apple_health", "abc-123", "2024-01-15T06:00:00+00:00", "running")
    id2 = workout_id("apple_health", "abc-123", "2024-01-15T07:00:00+00:00", "cycling")
    assert id1 == id2


def test_fallback_without_source_id() -> None:
    """Without source_id, hash uses source + start_time + activity_type."""
    id1 = workout_id("apple_health", None, "2024-01-15T06:00:00+00:00", "running")
    id2 = workout_id("apple_health", None, "2024-01-15T06:00:00+00:00", "running")
    assert id1 == id2


def test_different_without_source_id() -> None:
    """Different start_time or activity_type produces a different ID."""
    id1 = workout_id("apple_health", None, "2024-01-15T06:00:00+00:00", "running")
    id2 = workout_id("apple_health", None, "2024-01-15T07:00:00+00:00", "running")
    assert id1 != id2


def test_id_length() -> None:
    """IDs are 16 hex chars."""
    wid = workout_id("apple_health", "abc", "2024-01-15T06:00:00+00:00", "running")
    assert len(wid) == 16
    assert all(c in "0123456789abcdef" for c in wid)


def test_empty_source_id_treated_as_none() -> None:
    """Empty string source_id should use the fallback path."""
    id_none = workout_id("apple_health", None, "2024-01-15T06:00:00+00:00", "running")
    id_empty = workout_id("apple_health", "", "2024-01-15T06:00:00+00:00", "running")
    assert id_none == id_empty
