"""GPX file parser.

Parses GPX route files (as exported by Apple Health) into a flat list of
track points with lat, lon, elevation, and timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gpxpy


@dataclass(frozen=True, slots=True)
class RoutePoint:
    timestamp: datetime
    lat: float
    lon: float
    elevation_m: float | None


def parse_gpx(path: str | Path) -> list[RoutePoint]:
    """Parse a GPX file and return a list of ``RoutePoint`` instances.

    Timestamps are converted to UTC.  Points without a timestamp are skipped.
    """
    path = Path(path)
    with path.open() as f:
        gpx = gpxpy.parse(f)

    points: list[RoutePoint] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                if pt.time is None:
                    continue
                ts = pt.time.astimezone(timezone.utc)
                points.append(
                    RoutePoint(
                        timestamp=ts,
                        lat=pt.latitude,
                        lon=pt.longitude,
                        elevation_m=pt.elevation,
                    )
                )
    return points
