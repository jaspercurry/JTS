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

import logging
from collections.abc import Mapping

from .._mta_stations import Station, load_stations
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


# The provider needs lat/lon for haversine ranking; legacy CSV rows
# missing those columns are filtered out at module import. The CSV
# parse itself is memoised in `_mta_stations.load_stations()`, shared
# with the runtime subway client.
_STATIONS: tuple[Station, ...] = tuple(
    s for s in load_stations() if s.lat is not None and s.lon is not None
)


def _format_display(s: Station) -> str:
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

    def build_client(self, env: Mapping[str, str]) -> object | None:
        # Parse our own keys (mirrors Config.subway_*): an empty station id
        # disables the tool. Raw (unstripped) values, matching Config._env.
        station_id = env.get("JASPER_SUBWAY_STATION_ID", "")
        if not station_id:
            return None
        from ...subway import SubwayClient  # lazy: keep the wizard light

        return SubwayClient(
            station_id, env.get("JASPER_SUBWAY_DEFAULT_DIRECTION", ""),
        )

    def make_tools(self, client: object):
        from ...tools.subway import make_subway_tools  # lazy

        return make_subway_tools(client)


PROVIDER = _NycSubway()
