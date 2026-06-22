# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared MTA-stations CSV loader for the subway runtime client + provider.

Both `jasper.subway` (the arrivals tool runtime) and
`jasper.transit.providers.nyc_subway` (the wizard's stop finder) need
parsed station rows. v1 had each of them re-implement the CSV parse,
which (1) doubled the import-time IO and (2) was a drift hazard if
the columns ever changed. This module is the single source of truth.

Each consumer cares about a subset of fields:
  - runtime client uses `stop_id`, `name`, `lines`, `north_label`, `south_label`
    (no need for lat/lon; arrivals come from Subway Now via the parent stop_id).
  - provider uses `stop_id`, `name`, `borough`, `lines`, `lat`, `lon`
    (for haversine ranking in `find_stops_near`).

The Station dataclass exposes all of them. Rows missing lat/lon are
still loaded (`lat=lon=None`) so the runtime client can use them; the
provider filters those out when building the haversine search.

Exception safety: if the CSV is missing / corrupt / partial-write
mid-deploy, `load_stations()` returns an empty tuple rather than
raising. Import of the runtime client is on jasper-voice's critical
path; a crashed import would mean no voice loop at all.
"""
from __future__ import annotations

import csv
import functools
import logging
from dataclasses import dataclass
from importlib import resources

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Station:
    stop_id: str
    name: str
    borough: str
    lines: tuple[str, ...]
    # WGS84 — may be None on legacy rows that predate the column.
    lat: float | None
    lon: float | None
    north_label: str
    south_label: str


@functools.lru_cache(maxsize=1)
def load_stations() -> tuple[Station, ...]:
    """Parse `jasper/data/mta_stations.csv` once. Memoised so the
    second caller reuses the tuple."""
    try:
        path = resources.files("jasper.data").joinpath("mta_stations.csv")
        with path.open("r", encoding="utf-8") as f:
            non_comment = (line for line in f if not line.lstrip().startswith("#"))
            stations: list[Station] = []
            for row in csv.DictReader(non_comment):
                sid = (row.get("stop_id") or "").strip()
                if not sid:
                    continue
                lines = tuple(
                    t for t in (row.get("lines") or "").replace(";", " ").split()
                )
                lat: float | None
                lon: float | None
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                except (KeyError, ValueError):
                    lat = lon = None
                stations.append(Station(
                    stop_id=sid,
                    name=(row.get("stop_name") or sid).strip(),
                    borough=(row.get("borough") or "").strip(),
                    lines=lines,
                    lat=lat, lon=lon,
                    north_label=(row.get("north_label") or "").strip(),
                    south_label=(row.get("south_label") or "").strip(),
                ))
            return tuple(stations)
    except Exception:  # noqa: BLE001
        logger.exception(
            "mta_stations.csv unreadable; subway data disabled. "
            "Re-run scripts/refresh-mta-stations.sh and redeploy."
        )
        return ()


def stations_by_id() -> dict[str, Station]:
    """Convenience for callers that need stop_id → Station lookup
    (the runtime arrivals client). Builds fresh each call but the
    underlying CSV parse is memoised."""
    return {s.stop_id: s for s in load_stations()}
