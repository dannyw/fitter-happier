"""Tests for src.ingest.strava_api and its auth/cursor helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from src.ingest.strava import _strava_metadata
from src.ingest.strava_api import (
    _parse_cursor_arg,
    normalize_activity,
    streams_to_points_and_samples,
)
from src.ingest.strava_auth import needs_refresh
from src.storage.schema import ensure_schema
from src.storage.sync_state import get_cursor, set_cursor


def _activity(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 12345,
        "name": "Morning Run",
        "sport_type": "Run",
        "start_date": "2026-03-01T14:00:00Z",
        "elapsed_time": 1800,
        "distance": 5000.0,
        "average_heartrate": 150.0,
        "max_heartrate": 172.0,
        "total_elevation_gain": 42.0,
        "device_name": "Garmin",
    }
    base.update(overrides)
    return base


class TestNormalizeActivity:
    def test_maps_core_fields(self) -> None:
        norm = normalize_activity(_activity())
        assert norm.source_id == "12345"
        assert norm.activity_type == "running"
        assert norm.start_time == datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        assert norm.end_time == datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
        assert norm.duration_sec == 1800
        assert norm.distance_m == 5000.0
        assert norm.avg_hr == 150.0
        assert norm.device == "Garmin"

    def test_falls_back_to_type_when_no_sport_type(self) -> None:
        act = _activity()
        del act["sport_type"]
        act["type"] = "Ride"
        assert normalize_activity(act).activity_type == "cycling"

    def test_missing_optional_fields_become_none(self) -> None:
        norm = normalize_activity(
            {"id": 1, "sport_type": "Run", "start_date": "2026-03-01T14:00:00Z"}
        )
        assert norm.distance_m is None
        assert norm.avg_hr is None
        assert norm.duration_sec == 0


class TestStreamsToPointsAndSamples:
    def test_builds_points_and_samples(self) -> None:
        start = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        streams = {
            "time": {"data": [0, 1, 2]},
            "latlng": {"data": [[40.0, -74.0], [40.1, -74.1], [40.2, -74.2]]},
            "altitude": {"data": [10.0, 11.0, 12.0]},
            "heartrate": {"data": [140, 145, 150]},
            "velocity_smooth": {"data": [2.5, 2.6, 2.7]},
        }
        points, samples = streams_to_points_and_samples(streams, start)
        assert len(points) == 3
        assert points[1].lat == 40.1
        assert points[1].elevation_m == 11.0
        assert points[2].timestamp == datetime(2026, 3, 1, 14, 0, 2, tzinfo=UTC)
        assert len(samples) == 3
        assert samples[0].heart_rate == 140
        assert samples[0].speed == 2.5

    def test_no_time_stream_returns_empty(self) -> None:
        points, samples = streams_to_points_and_samples(
            {"heartrate": {"data": [1, 2]}}, datetime(2026, 3, 1, tzinfo=UTC)
        )
        assert points == []
        assert samples == []

    def test_missing_gps_still_yields_samples(self) -> None:
        start = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        streams = {"time": {"data": [0, 1]}, "heartrate": {"data": [140, 141]}}
        points, samples = streams_to_points_and_samples(streams, start)
        assert points == []
        assert len(samples) == 2


class TestNeedsRefresh:
    def test_missing_expiry_needs_refresh(self) -> None:
        assert needs_refresh(None, now=1000.0) is True

    def test_expired_needs_refresh(self) -> None:
        assert needs_refresh(1000.0, now=2000.0) is True

    def test_within_skew_needs_refresh(self) -> None:
        assert needs_refresh(1050.0, now=1000.0, skew=120) is True

    def test_valid_token_no_refresh(self) -> None:
        assert needs_refresh(5000.0, now=1000.0, skew=120) is False


class TestStravaMetadata:
    def test_export_shape(self) -> None:
        meta = _strava_metadata(
            {"Activity ID": "9", "Activity Name": "Run", "Activity Gear": "Pegasus"}
        )
        assert meta["strava_activity_id"] == "9"
        assert meta["strava_activity_name"] == "Run"
        assert meta["strava_gear"] == "Pegasus"

    def test_api_shape(self) -> None:
        meta = _strava_metadata(
            {"id": 9, "name": "Run", "description": "nice", "gear_id": "g1"}
        )
        assert meta["strava_activity_id"] == "9"
        assert meta["strava_activity_name"] == "Run"
        assert meta["strava_description"] == "nice"
        assert meta["strava_gear"] == "g1"


class TestParseCursorArg:
    def test_bare_date_is_utc_midnight(self) -> None:
        assert _parse_cursor_arg("2026-04-26") == datetime(2026, 4, 26, 0, 0, tzinfo=UTC)

    def test_iso_with_zulu(self) -> None:
        assert _parse_cursor_arg("2026-04-26T10:30:00Z") == datetime(
            2026, 4, 26, 10, 30, tzinfo=UTC
        )

    def test_iso_with_offset_normalized_to_utc(self) -> None:
        assert _parse_cursor_arg("2026-04-26T06:30:00-04:00") == datetime(
            2026, 4, 26, 10, 30, tzinfo=UTC
        )


class TestSyncCursor:
    def test_roundtrip(self) -> None:
        con = duckdb.connect(":memory:")
        ensure_schema(con)
        assert get_cursor(con, "strava") is None

        ts = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        now = datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
        set_cursor(con, "strava", ts, now=now)
        assert get_cursor(con, "strava") == ts

        # Upsert moves the cursor forward.
        ts2 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
        set_cursor(con, "strava", ts2, now=now)
        assert get_cursor(con, "strava") == ts2
