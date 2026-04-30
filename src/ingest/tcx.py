"""TCX file parser.

Parses TCX (Training Center XML) activity files into route points and
sample records using ``lxml``.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree

from src.ingest.fit import FitSample
from src.ingest.gpx import RoutePoint

_NS = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}


def _parse_ts(text: str) -> datetime:
    """Parse an ISO 8601 timestamp to UTC-aware datetime."""
    # Handle both "2024-01-15T06:05:00Z" and "2024-01-15T06:05:00.000Z"
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC)


def _text(elem: etree._Element, xpath: str) -> str | None:
    """Get text content of a child element, or None."""
    child = elem.find(xpath, namespaces=_NS)
    if child is not None and child.text:
        return child.text.strip()
    return None


def parse_tcx(path: str | Path) -> tuple[list[RoutePoint], list[FitSample]]:
    """Parse a TCX file and return (route_points, samples).

    Handles ``.tcx.gz`` transparently.  Timestamps are converted to UTC.
    """
    path = Path(path)

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:  # type: ignore[arg-type]
        tree = etree.parse(f)

    root = tree.getroot()
    route_points: list[RoutePoint] = []
    samples: list[FitSample] = []

    for tp in root.iter("{http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2}Trackpoint"):
        time_text = _text(tp, "tcx:Time")
        if time_text is None:
            continue

        ts = _parse_ts(time_text)

        # Position
        lat_text = _text(tp, "tcx:Position/tcx:LatitudeDegrees")
        lon_text = _text(tp, "tcx:Position/tcx:LongitudeDegrees")
        alt_text = _text(tp, "tcx:AltitudeMeters")

        if lat_text is not None and lon_text is not None:
            route_points.append(
                RoutePoint(
                    timestamp=ts,
                    lat=float(lat_text),
                    lon=float(lon_text),
                    elevation_m=float(alt_text) if alt_text is not None else None,
                )
            )

        # Heart rate
        hr_text = _text(tp, "tcx:HeartRateBpm/tcx:Value")
        hr = int(hr_text) if hr_text is not None else None

        # Cadence
        cad_text = _text(tp, "tcx:Cadence")
        cad = int(cad_text) if cad_text is not None else None

        samples.append(
            FitSample(
                timestamp=ts,
                heart_rate=hr,
                cadence=cad,
                power=None,
                speed=None,
            )
        )

    return route_points, samples
