"""Tests for src.ingest.units — unit conversion helpers."""

from __future__ import annotations

import pytest

from src.ingest.units import distance_to_meters, elevation_to_meters, energy_to_kcal


class TestDistanceToMeters:
    def test_km(self) -> None:
        assert distance_to_meters(5.0, "km") == 5000.0

    def test_miles(self) -> None:
        assert abs(distance_to_meters(1.0, "mi") - 1609.344) < 0.001

    def test_meters_passthrough(self) -> None:
        assert distance_to_meters(100.0, "m") == 100.0

    def test_case_insensitive(self) -> None:
        assert distance_to_meters(1.0, "KM") == 1000.0

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown distance unit"):
            distance_to_meters(1.0, "furlongs")

    def test_zero(self) -> None:
        assert distance_to_meters(0.0, "km") == 0.0


class TestEnergyToKcal:
    def test_kcal(self) -> None:
        assert energy_to_kcal(100.0, "kcal") == 100.0

    def test_cal_means_kcal(self) -> None:
        assert energy_to_kcal(100.0, "Cal") == 100.0

    def test_kj(self) -> None:
        assert abs(energy_to_kcal(4.184, "kJ") - 1.0) < 0.001

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown energy unit"):
            energy_to_kcal(1.0, "BTU")


class TestElevationToMeters:
    def test_meters(self) -> None:
        assert elevation_to_meters(10.0, "m") == 10.0

    def test_feet(self) -> None:
        assert abs(elevation_to_meters(1.0, "ft") - 0.3048) < 0.0001

    def test_cm(self) -> None:
        assert elevation_to_meters(100.0, "cm") == 1.0

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown elevation unit"):
            elevation_to_meters(1.0, "cubits")
