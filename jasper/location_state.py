"""Shared default-location state for weather and transit.

Weather and transit intentionally keep separate wizard-owned env files,
but both store the same small coordinate scaffold: rounded latitude,
rounded longitude, and a display label. This module keeps the env-key
names and conversion helpers in one place so the two wizards can seed
each other without importing each other's HTTP handlers.
"""
from __future__ import annotations

from dataclasses import dataclass


TRANSIT_FILE = "/var/lib/jasper/transit.env"
TRANSIT_FILE_MODE = 0o640
TRANSIT_LAT_ENV = "JASPER_TRANSIT_LAT"
TRANSIT_LON_ENV = "JASPER_TRANSIT_LON"
TRANSIT_DISPLAY_NAME_ENV = "JASPER_TRANSIT_DISPLAY_NAME"

WEATHER_FILE = "/var/lib/jasper/weather.env"
WEATHER_FILE_MODE = 0o640
WEATHER_LAT_ENV = "JASPER_WEATHER_LAT"
WEATHER_LON_ENV = "JASPER_WEATHER_LON"
WEATHER_DISPLAY_NAME_ENV = "JASPER_WEATHER_DISPLAY_NAME"
WEATHER_DEFAULT_LOCATION_ENV = "JASPER_DEFAULT_LOCATION"
WEATHER_UNITS_ENV = "JASPER_WEATHER_UNITS"


@dataclass(frozen=True)
class SavedLocation:
    lat: float
    lon: float
    display_name: str = ""


def parse_location(
    state: dict[str, str],
    *,
    lat_key: str,
    lon_key: str,
    display_key: str,
) -> SavedLocation | None:
    """Return a parsed saved location, or None when coords are absent."""
    try:
        lat_raw = state.get(lat_key, "").strip()
        lon_raw = state.get(lon_key, "").strip()
        if not lat_raw or not lon_raw:
            return None
        lat = float(lat_raw)
        lon = float(lon_raw)
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return SavedLocation(
        lat=lat,
        lon=lon,
        display_name=state.get(display_key, "").strip(),
    )


def parse_weather_location(state: dict[str, str]) -> SavedLocation | None:
    return parse_location(
        state,
        lat_key=WEATHER_LAT_ENV,
        lon_key=WEATHER_LON_ENV,
        display_key=WEATHER_DISPLAY_NAME_ENV,
    )


def parse_transit_location(state: dict[str, str]) -> SavedLocation | None:
    return parse_location(
        state,
        lat_key=TRANSIT_LAT_ENV,
        lon_key=TRANSIT_LON_ENV,
        display_key=TRANSIT_DISPLAY_NAME_ENV,
    )


def weather_env_for_location(
    loc: SavedLocation,
    *,
    units: str | None = None,
) -> dict[str, str]:
    """Weather env entries for a saved coordinate location."""
    display = loc.display_name or f"{loc.lat:.3f}, {loc.lon:.3f}"
    out = {
        WEATHER_LAT_ENV: f"{loc.lat:.3f}",
        WEATHER_LON_ENV: f"{loc.lon:.3f}",
        WEATHER_DISPLAY_NAME_ENV: display,
        WEATHER_DEFAULT_LOCATION_ENV: display,
    }
    if units:
        out[WEATHER_UNITS_ENV] = units
    return out


def transit_env_for_location(loc: SavedLocation) -> dict[str, str]:
    """Transit env entries for a saved coordinate location."""
    display = loc.display_name or f"Manual: {loc.lat:.3f}, {loc.lon:.3f}"
    return {
        TRANSIT_LAT_ENV: f"{loc.lat:.3f}",
        TRANSIT_LON_ENV: f"{loc.lon:.3f}",
        TRANSIT_DISPLAY_NAME_ENV: display,
    }
