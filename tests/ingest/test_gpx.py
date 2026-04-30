"""Tests for src.ingest.gpx — GPX file parser."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.ingest.gpx import parse_gpx

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_parse_mini_route() -> None:
    points = parse_gpx(FIXTURES / "mini_route.gpx")
    assert len(points) == 5


def test_first_point_values() -> None:
    points = parse_gpx(FIXTURES / "mini_route.gpx")
    p = points[0]
    assert p.lat == 40.7580
    assert p.lon == -73.9855
    assert p.elevation_m == 10.2
    assert p.timestamp == datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)


def test_points_are_utc() -> None:
    points = parse_gpx(FIXTURES / "mini_route.gpx")
    for p in points:
        assert p.timestamp.tzinfo == timezone.utc


def test_points_ordered_by_time() -> None:
    points = parse_gpx(FIXTURES / "mini_route.gpx")
    times = [p.timestamp for p in points]
    assert times == sorted(times)
