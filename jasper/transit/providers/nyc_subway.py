"""NYC Subway transit provider — keyless, CSV-backed.

Loads `jasper/data/mta_stations.csv` (496 stations, ~32 KB) once at
module import. `find_stops_near` is an O(N) haversine sort — at this
scale that's sub-millisecond on a Pi 5, so a spatial index would be
premature complexity.

The CSV is also consumed by `jasper.subway` for voice-direction
labelling. Same file, same schema, different columns of interest:
this module needs lat/lon, that one needs north_label/south_label.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from importlib import resources

from ..base import BoundingBox, Stop, haversine_miles

logger = logging.getLogger(__name__)


# NYC envelope from Staten Island Ferry terminal up to Wakefield, plus
# a small margin. A coarse rectangle is fine because the wizard
# double-checks by dropping providers whose nearest stop is too far.
NYC_BBOX = BoundingBox(
    lat_min=40.49, lat_max=40.92,
    lon_min=-74.26, lon_max=-73.69,
)


_BOROUGH_DISPLAY = {
    "Bk": "Brooklyn",
    "Bx": "Bronx",
    "M": "Manhattan",
    "Q": "Queens",
    "SI": "Staten Island",
}


@dataclass(frozen=True)
class _SubwayStation:
    stop_id: str
    name: str
    borough: str
    lines: tuple[str, ...]
    lat: float
    lon: float


def _load_stations() -> tuple[_SubwayStation, ...]:
    path = resources.files("jasper.data").joinpath("mta_stations.csv")
    stations: list[_SubwayStation] = []
    with path.open("r", encoding="utf-8") as f:
        # Same comment-line strip pattern as jasper/subway.py.
        non_comment = (line for line in f if not line.lstrip().startswith("#"))
        for row in csv.DictReader(non_comment):
            sid = (row.get("stop_id") or "").strip()
            if not sid:
                continue
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, ValueError):
                # CSV row without lat/lon — predates the column. Skip
                # silently rather than refuse to import: the arrivals
                # tool still works for these via the existing CSV
                # path; only nearest-stop discovery is affected.
                continue
            lines = tuple(
                t for t in (row.get("lines") or "").replace(";", " ").split()
            )
            stations.append(_SubwayStation(
                stop_id=sid,
                name=(row.get("stop_name") or sid).strip(),
                borough=(row.get("borough") or "").strip(),
                lines=lines,
                lat=lat, lon=lon,
            ))
    return tuple(stations)


# Module-import IO — the file is bundled in the package, so this is
# effectively a constant from the caller's perspective.
#
# Exception safety: if the CSV is corrupt mid-deploy (partial rsync,
# bad refresh script run), don't take down `import jasper.transit`
# (and by cascade the whole jasper-web daemon — all wizards). Fall
# back to an empty tuple; the wizard card then renders "no stations
# nearby" instead of 500-ing every settings page.
try:
    _STATIONS: tuple[_SubwayStation, ...] = _load_stations()
except Exception:  # noqa: BLE001
    logger.exception(
        "mta_stations.csv unreadable; subway provider disabled. "
        "Re-run scripts/refresh-mta-stations.sh and redeploy."
    )
    _STATIONS = ()


def _format_display(s: _SubwayStation) -> str:
    """Human label shown on the wizard card. Includes line and borough
    so the user can disambiguate co-named stations (there are ~20
    pairs of stations sharing a name across boroughs)."""
    lines_label = "/".join(s.lines) if s.lines else "—"
    borough_label = _BOROUGH_DISPLAY.get(s.borough, s.borough)
    return f"{s.name} ({lines_label} — {borough_label})"


class _NycSubway:
    id = "nyc_subway"
    label = "NYC Subway"
    kind = "subway"
    help_url = (
        "https://data.ny.gov/Transportation/MTA-Subway-Stations/39hk-dx4f"
    )
    bbox = NYC_BBOX
    env_keys = (
        "JASPER_SUBWAY_STATION_ID",
        "JASPER_SUBWAY_DEFAULT_DIRECTION",
        "JASPER_SUBWAY_LINES",
    )
    credentials = ()  # keyless

    def find_stops_near(
        self,
        lat: float,
        lon: float,
        *,
        credentials: dict[str, str] | None = None,
        count: int = 5,
    ) -> list[Stop]:
        # Compute distances once, then sort+slice — half the haversine
        # calls of "sort by key=haversine".
        distances = [
            (haversine_miles(lat, lon, s.lat, s.lon), s)
            for s in _STATIONS
        ]
        distances.sort(key=lambda t: t[0])
        results: list[Stop] = []
        for d, s in distances[:count]:
            results.append(Stop(
                stop_id=s.stop_id,
                display_name=_format_display(s),
                lat=s.lat, lon=s.lon,
                distance_mi=d,
                lines=s.lines,
            ))
        return results

    def validate_credentials(
        self, credentials: dict[str, str],
    ) -> dict[str, str] | None:
        # Provider is keyless. Empty input → success (nothing to check).
        # Non-empty input is a programming error — the wizard shouldn't
        # be calling this on a keyless provider — but we don't raise,
        # just report each unknown key as rejected.
        return {k: "nyc_subway is keyless" for k in credentials} or None


PROVIDER = _NycSubway()
