"""FIT file parser.

Parses FIT activity files (as exported by Strava and other platforms) into
route points and sample records using ``fitdecode``.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import fitdecode

from src.ingest.gpx import RoutePoint


@dataclass(frozen=True, slots=True)
class FitSample:
    timestamp: datetime
    heart_rate: int | None
    cadence: int | None
    power: int | None
    speed: float | None  # m/s


# FIT stores coordinates as semicircles; convert to degrees.
_SEMICIRCLE_TO_DEG = 180.0 / (2**31)


def _to_degrees(semicircles: int) -> float:
    return semicircles * _SEMICIRCLE_TO_DEG


def parse_fit(path: str | Path) -> tuple[list[RoutePoint], list[FitSample]]:
    """Parse a FIT file and return (route_points, samples).

    Handles ``.fit.gz`` transparently.  Timestamps are converted to UTC.
    Points without a timestamp are skipped.
    """
    path = Path(path)

    opener = gzip.open if path.suffix == ".gz" else open
    route_points: list[RoutePoint] = []
    samples: list[FitSample] = []

    with opener(path, "rb") as f, fitdecode.FitReader(f) as reader:  # type: ignore[arg-type]
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name != "record":
                    continue

                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue

                # fitdecode returns naive datetimes in UTC
                ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)

                # GPS coordinates (may be absent for indoor workouts)
                lat_sc = frame.get_value("position_lat", fallback=None)
                lon_sc = frame.get_value("position_long", fallback=None)
                altitude = frame.get_value("altitude", fallback=None)
                # Some files use enhanced_altitude
                if altitude is None:
                    altitude = frame.get_value("enhanced_altitude", fallback=None)

                if lat_sc is not None and lon_sc is not None:
                    route_points.append(
                        RoutePoint(
                            timestamp=ts,
                            lat=_to_degrees(lat_sc),
                            lon=_to_degrees(lon_sc),
                            elevation_m=float(altitude) if altitude is not None else None,
                        )
                    )

                # Samples
                hr = frame.get_value("heart_rate", fallback=None)
                cadence = frame.get_value("cadence", fallback=None)
                power = frame.get_value("power", fallback=None)
                speed = frame.get_value("speed", fallback=None)
                if speed is None:
                    speed = frame.get_value("enhanced_speed", fallback=None)

                samples.append(
                    FitSample(
                        timestamp=ts,
                        heart_rate=int(hr) if hr is not None else None,
                        cadence=int(cadence) if cadence is not None else None,
                        power=int(power) if power is not None else None,
                        speed=float(speed) if speed is not None else None,
                    )
                )

    return route_points, samples
