"""Tests for src.ingest.timestamps — Apple Health timestamp parsing."""

from __future__ import annotations

from datetime import datetime, timezone

from src.ingest.timestamps import parse_apple_health_ts


def test_negative_offset() -> None:
    dt = parse_apple_health_ts("2024-01-15 01:00:00 -0500")
    assert dt.tzinfo == timezone.utc
    assert dt == datetime(2024, 1, 15, 6, 0, 0, tzinfo=timezone.utc)


def test_positive_offset() -> None:
    dt = parse_apple_health_ts("2024-01-15 14:00:00 +0100")
    assert dt == datetime(2024, 1, 15, 13, 0, 0, tzinfo=timezone.utc)


def test_utc_offset() -> None:
    dt = parse_apple_health_ts("2024-01-15 12:00:00 +0000")
    assert dt == datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_large_positive_offset() -> None:
    dt = parse_apple_health_ts("2024-01-15 23:00:00 +0900")
    assert dt == datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
