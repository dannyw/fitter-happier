"""Tests for src.ingest.activity_types — activity type normalization."""

from __future__ import annotations

from src.ingest.activity_types import normalize_activity_type, normalize_strava_activity_type


def test_full_hk_identifier() -> None:
    assert normalize_activity_type("HKWorkoutActivityTypeRunning") == "running"


def test_bare_suffix() -> None:
    assert normalize_activity_type("Cycling") == "cycling"


def test_already_lowercase() -> None:
    assert normalize_activity_type("yoga") == "yoga"


def test_strength_training_variants() -> None:
    assert normalize_activity_type("HKWorkoutActivityTypeFunctionalStrengthTraining") == "strength"
    assert normalize_activity_type("HKWorkoutActivityTypeTraditionalStrengthTraining") == "strength"


def test_hiit() -> None:
    assert normalize_activity_type("HKWorkoutActivityTypeHighIntensityIntervalTraining") == "hiit"


def test_unknown_type_returns_lowercase() -> None:
    result = normalize_activity_type("HKWorkoutActivityTypeUnderwaterBasketWeaving")
    assert result == "underwaterbasketweaving"


def test_whitespace_stripped() -> None:
    assert normalize_activity_type("  HKWorkoutActivityTypeRunning  ") == "running"


# -- Strava types -----------------------------------------------------------


def test_strava_run() -> None:
    assert normalize_strava_activity_type("Run") == "running"


def test_strava_trail_run() -> None:
    assert normalize_strava_activity_type("TrailRun") == "running"


def test_strava_ride() -> None:
    assert normalize_strava_activity_type("Ride") == "cycling"


def test_strava_mountain_bike() -> None:
    assert normalize_strava_activity_type("MountainBikeRide") == "cycling"


def test_strava_swim() -> None:
    assert normalize_strava_activity_type("Swim") == "swimming"


def test_strava_weight_training() -> None:
    assert normalize_strava_activity_type("WeightTraining") == "strength"


def test_strava_unknown_returns_lowercase() -> None:
    assert normalize_strava_activity_type("Paragliding") == "paragliding"


def test_strava_whitespace_stripped() -> None:
    assert normalize_strava_activity_type("  Hike  ") == "hiking"
