"""Tests for src.ingest.fit — FIT file parser."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.fit import FitSample, parse_fit

FIXTURES = Path(__file__).parent.parent / "fixtures"


class TestParseFit:
    def test_returns_route_points_and_samples(self) -> None:
        points, samples = parse_fit(FIXTURES / "mini_activity.fit")
        assert len(points) == 3
        assert len(samples) == 3

    def test_route_point_coordinates(self) -> None:
        points, _ = parse_fit(FIXTURES / "mini_activity.fit")
        p = points[0]
        assert p.lat == pytest.approx(40.758, abs=0.001)
        assert p.lon == pytest.approx(-73.9855, abs=0.001)

    def test_route_point_elevation(self) -> None:
        points, _ = parse_fit(FIXTURES / "mini_activity.fit")
        assert points[0].elevation_m == pytest.approx(10.2, abs=0.1)

    def test_timestamps_are_utc(self) -> None:
        points, samples = parse_fit(FIXTURES / "mini_activity.fit")
        for p in points:
            assert p.timestamp.tzinfo == timezone.utc
        for s in samples:
            assert s.timestamp.tzinfo == timezone.utc

    def test_first_timestamp(self) -> None:
        points, _ = parse_fit(FIXTURES / "mini_activity.fit")
        assert points[0].timestamp == datetime(2024, 1, 15, 6, 5, 0, tzinfo=timezone.utc)

    def test_heart_rate_values(self) -> None:
        _, samples = parse_fit(FIXTURES / "mini_activity.fit")
        hrs = [s.heart_rate for s in samples]
        assert hrs == [142, 148, 155]

    def test_cadence_values(self) -> None:
        _, samples = parse_fit(FIXTURES / "mini_activity.fit")
        cads = [s.cadence for s in samples]
        assert cads == [85, 86, 88]

    def test_points_ordered_by_time(self) -> None:
        points, _ = parse_fit(FIXTURES / "mini_activity.fit")
        times = [p.timestamp for p in points]
        assert times == sorted(times)
