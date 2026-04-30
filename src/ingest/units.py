"""Unit conversion helpers for Apple Health ingest."""

from __future__ import annotations

# Distance -------------------------------------------------------------------

_DISTANCE_TO_METERS: dict[str, float] = {
    "km": 1_000.0,
    "m": 1.0,
    "mi": 1_609.344,
    "ft": 0.3048,
    "yd": 0.9144,
}


def distance_to_meters(value: float, unit: str) -> float:
    """Convert a distance value to meters."""
    key = unit.lower().strip()
    factor = _DISTANCE_TO_METERS.get(key)
    if factor is None:
        raise ValueError(f"Unknown distance unit: {unit!r}")
    return value * factor


# Energy ---------------------------------------------------------------------

_ENERGY_TO_KCAL: dict[str, float] = {
    "kcal": 1.0,
    "cal": 1.0,  # Apple Health uses "Cal" meaning kcal
    "kj": 1.0 / 4.184,
}


def energy_to_kcal(value: float, unit: str) -> float:
    """Convert an energy value to kilocalories."""
    key = unit.lower().strip()
    factor = _ENERGY_TO_KCAL.get(key)
    if factor is None:
        raise ValueError(f"Unknown energy unit: {unit!r}")
    return value * factor


# Elevation ------------------------------------------------------------------

_ELEVATION_TO_METERS: dict[str, float] = {
    "m": 1.0,
    "cm": 0.01,
    "ft": 0.3048,
    "in": 0.0254,
}


def elevation_to_meters(value: float, unit: str) -> float:
    """Convert an elevation value to meters."""
    key = unit.lower().strip()
    factor = _ELEVATION_TO_METERS.get(key)
    if factor is None:
        raise ValueError(f"Unknown elevation unit: {unit!r}")
    return value * factor
