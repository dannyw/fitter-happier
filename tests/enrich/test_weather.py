"""Tests for src.enrich.weather — weather enrichment."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.enrich.weather import (
    _extract_apple_weather,
    _parse_apple_humidity,
    _parse_apple_temp,
    _parse_apple_condition,
)
from src.storage.schema import ensure_schema


class TestParseAppleTemp:
    def test_fahrenheit(self) -> None:
        assert _parse_apple_temp("48 degF") == pytest.approx(8.9, abs=0.1)

    def test_freezing(self) -> None:
        assert _parse_apple_temp("32 degF") == pytest.approx(0.0, abs=0.1)

    def test_celsius_assumed(self) -> None:
        assert _parse_apple_temp("20") == pytest.approx(20.0)

    def test_invalid(self) -> None:
        assert _parse_apple_temp("") is None


class TestParseAppleHumidity:
    def test_basis_points(self) -> None:
        assert _parse_apple_humidity("3800 %") == pytest.approx(38.0)

    def test_full(self) -> None:
        assert _parse_apple_humidity("10000 %") == pytest.approx(100.0)

    def test_already_percentage(self) -> None:
        assert _parse_apple_humidity("45") == pytest.approx(45.0)


class TestParseAppleCondition:
    def test_clear(self) -> None:
        assert _parse_apple_condition("1") == "clear"

    def test_snow(self) -> None:
        assert _parse_apple_condition("12") == "snow"

    def test_unknown(self) -> None:
        assert _parse_apple_condition("999") is None

    def test_none(self) -> None:
        assert _parse_apple_condition(None) is None


class TestExtractAppleWeather:
    @pytest.fixture()
    def con(self) -> duckdb.DuckDBPyConnection:
        c = duckdb.connect(":memory:")
        ensure_schema(c)
        yield c
        c.close()

    def test_extracts_outdoor_weather(self, con: duckdb.DuckDBPyConnection) -> None:
        raw = json.dumps({
            "metadata": {
                "HKIndoorWorkout": "0",
                "HKWeatherTemperature": "68 degF",
                "HKWeatherHumidity": "5000 %",
                "HKWeatherCondition": "1",
            }
        })
        con.execute(
            "INSERT INTO workouts (workout_id, source, activity_type, raw) "
            "VALUES ('w1', 'apple_health', 'running', ?)",
            [raw],
        )

        count = _extract_apple_weather(con)
        assert count == 1

        row = con.execute("SELECT temp_c, humidity_pct, conditions FROM weather").fetchone()
        assert row[0] == pytest.approx(20.0, abs=0.1)
        assert row[1] == pytest.approx(50.0)
        assert row[2] == "clear"

    def test_skips_indoor(self, con: duckdb.DuckDBPyConnection) -> None:
        raw = json.dumps({
            "metadata": {
                "HKIndoorWorkout": "1",
                "HKWeatherTemperature": "68 degF",
            }
        })
        con.execute(
            "INSERT INTO workouts (workout_id, source, activity_type, raw) "
            "VALUES ('w1', 'apple_health', 'running', ?)",
            [raw],
        )

        count = _extract_apple_weather(con)
        assert count == 0

    def test_skips_already_enriched(self, con: duckdb.DuckDBPyConnection) -> None:
        raw = json.dumps({
            "metadata": {
                "HKIndoorWorkout": "0",
                "HKWeatherTemperature": "68 degF",
            }
        })
        con.execute(
            "INSERT INTO workouts (workout_id, source, activity_type, raw) "
            "VALUES ('w1', 'apple_health', 'running', ?)",
            [raw],
        )
        con.execute(
            "INSERT INTO weather (workout_id, temp_c) VALUES ('w1', 20.0)"
        )

        count = _extract_apple_weather(con)
        assert count == 0

    def test_idempotent(self, con: duckdb.DuckDBPyConnection) -> None:
        raw = json.dumps({
            "metadata": {
                "HKIndoorWorkout": "0",
                "HKWeatherTemperature": "68 degF",
            }
        })
        con.execute(
            "INSERT INTO workouts (workout_id, source, activity_type, raw) "
            "VALUES ('w1', 'apple_health', 'running', ?)",
            [raw],
        )

        _extract_apple_weather(con)
        _extract_apple_weather(con)
        assert con.execute("SELECT count(*) FROM weather").fetchone()[0] == 1
