"""Normalize workout activity type identifiers from various sources.

Apple Health uses identifiers like ``HKWorkoutActivityTypeRunning``.
Strava uses PascalCase identifiers like ``Run``, ``TrailRun``, ``Ride``.
We map these to short lowercase names used throughout the dashboard.
"""

from __future__ import annotations

_HK_PREFIX = "HKWorkoutActivityType"

# Mapping of HK suffix → normalized name.  Extend as new types appear.
_ACTIVITY_MAP: dict[str, str] = {
    "Running": "running",
    "Walking": "walking",
    "Cycling": "cycling",
    "Swimming": "swimming",
    "Hiking": "hiking",
    "Yoga": "yoga",
    "Pilates": "pilates",
    "FunctionalStrengthTraining": "strength",
    "TraditionalStrengthTraining": "strength",
    "HighIntensityIntervalTraining": "hiit",
    "CrossTraining": "cross_training",
    "Elliptical": "elliptical",
    "Rowing": "rowing",
    "StairClimbing": "stair_climbing",
    "Tennis": "tennis",
    "TableTennis": "table_tennis",
    "Badminton": "badminton",
    "Soccer": "soccer",
    "Basketball": "basketball",
    "Golf": "golf",
    "Dance": "dance",
    "Cooldown": "cooldown",
    "CoreTraining": "core_training",
    "Flexibility": "flexibility",
    "MindAndBody": "mind_and_body",
    "Play": "play",
    "Other": "other",
}


def normalize_activity_type(raw: str) -> str:
    """Normalize an Apple Health activity type string.

    Handles full identifiers (``HKWorkoutActivityTypeRunning``), bare suffixes
    (``Running``), and already-lowercase names (``running``).
    """
    stripped = raw.strip()

    # Strip the HK prefix if present
    if stripped.startswith(_HK_PREFIX):
        suffix = stripped[len(_HK_PREFIX):]
    else:
        suffix = stripped

    # Try direct lookup (PascalCase suffix)
    if suffix in _ACTIVITY_MAP:
        return _ACTIVITY_MAP[suffix]

    # Try case-insensitive match
    lower = suffix.lower()
    for key, val in _ACTIVITY_MAP.items():
        if key.lower() == lower or val == lower:
            return val

    # Unknown type — return lowercase with underscores
    return lower.replace(" ", "_")


# -- Strava activity types ---------------------------------------------------

_STRAVA_TYPE_MAP: dict[str, str] = {
    "Run": "running",
    "TrailRun": "running",
    "VirtualRun": "running",
    "Ride": "cycling",
    "MountainBikeRide": "cycling",
    "GravelRide": "cycling",
    "VirtualRide": "cycling",
    "EBikeRide": "cycling",
    "Swim": "swimming",
    "Walk": "walking",
    "Hike": "hiking",
    "WeightTraining": "strength",
    "Yoga": "yoga",
    "Pilates": "pilates",
    "Rowing": "rowing",
    "Elliptical": "elliptical",
    "Tennis": "tennis",
    "Snowboard": "snowboarding",
    "AlpineSki": "alpine_ski",
    "NordicSki": "nordic_ski",
    "CrossCountrySkiing": "nordic_ski",
    "Crossfit": "cross_training",
    "RockClimbing": "rock_climbing",
    "StairStepper": "stair_climbing",
    "Soccer": "soccer",
    "Golf": "golf",
    "Surfing": "surfing",
    "Skateboard": "skateboarding",
    "InlineSkate": "inline_skating",
    "IceSkate": "ice_skating",
    "Kayaking": "kayaking",
    "Canoeing": "canoeing",
    "StandUpPaddling": "stand_up_paddling",
    "Handcycle": "handcycle",
    "Wheelchair": "wheelchair",
    "Velomobile": "velomobile",
    "Workout": "other",
}


def normalize_strava_activity_type(raw: str) -> str:
    """Normalize a Strava activity type string.

    Handles Strava PascalCase types (``Run``, ``TrailRun``, ``Ride``) and
    already-lowercase names.
    """
    stripped = raw.strip()

    # Direct lookup
    if stripped in _STRAVA_TYPE_MAP:
        return _STRAVA_TYPE_MAP[stripped]

    # Case-insensitive match
    lower = stripped.lower()
    for key, val in _STRAVA_TYPE_MAP.items():
        if key.lower() == lower or val == lower:
            return val

    # Unknown — return lowercase with underscores
    return lower.replace(" ", "_")
