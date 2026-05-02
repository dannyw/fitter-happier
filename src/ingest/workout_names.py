"""Generate human-readable default names for workouts.

Used when a workout has no explicit name (e.g. Apple Watch workouts).
Strava names always take priority over generated defaults.
"""

from __future__ import annotations

from datetime import datetime

_ACTIVITY_LABELS: dict[str, str] = {
    "running": "Run",
    "hiking": "Hike",
    "cycling": "Ride",
    "swimming": "Swim",
    "strength": "Strength",
    "yoga": "Yoga",
    "pilates": "Pilates",
    "walking": "Walk",
    "hiit": "HIIT",
    "elliptical": "Elliptical",
    "rowing": "Row",
    "stair_climbing": "Stair Climb",
    "cross_training": "Cross Training",
    "core_training": "Core Training",
    "dance": "Dance",
    "cooldown": "Cooldown",
    "flexibility": "Flexibility",
}


def _time_of_day(hour: int) -> str:
    """Map hour (0-23) to a time-of-day label."""
    if 4 <= hour < 6:
        return "Early Morning"
    if 6 <= hour < 12:
        return "Morning"
    if 12 <= hour < 17:
        return "Afternoon"
    if 17 <= hour < 21:
        return "Evening"
    return "Night"


def generate_default_name(activity_type: str, start_time: datetime) -> str:
    """Generate a default workout name from activity type and start time.

    Returns strings like ``"Morning Run"`` or ``"Evening Hike"``.
    """
    label = _ACTIVITY_LABELS.get(
        activity_type,
        activity_type.replace("_", " ").title(),
    )
    tod = _time_of_day(start_time.hour)
    return f"{tod} {label}"
