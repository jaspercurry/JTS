# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared user-volume percent <-> Camilla dB curve.

JTS treats 0% as a true mute owned by ``VolumeCoordinator``. The audible
slider travel is therefore 1..100%, mapped over a calibrated dB range:

    1%  -> volume_floor_db
    100% -> 0 dB

The default floor preserves the original shipped curve. Installations with
low-sensitivity speakers can raise the floor from the /sound/ advanced
settings so the bottom of the slider becomes useful without allowing positive
digital gain.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VOLUME_FLOOR_DB = -50.0
VOLUME_CEILING_DB = 0.0

# UI/setting clamp. A floor above -10 dB makes 1% potentially loud; a floor
# below -60 dB is effectively silence for this product and wastes slider travel.
VOLUME_FLOOR_MIN_DB = -60.0
VOLUME_FLOOR_MAX_DB = -10.0

_SETTINGS_FLOOR_LOCK = threading.Lock()
_SETTINGS_FLOOR_CACHE: tuple[str, int | None, int | None, float] | None = None
_SETTINGS_FLOOR_WARNING_LOGGED = False


def normalize_volume_floor_db(value: Any) -> float:
    try:
        floor = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME_FLOOR_DB
    if floor != floor or floor in (float("inf"), float("-inf")):
        return DEFAULT_VOLUME_FLOOR_DB
    floor = min(VOLUME_FLOOR_MAX_DB, max(VOLUME_FLOOR_MIN_DB, floor))
    return round(floor, 3)


def configured_volume_floor_db() -> float:
    """Return the wizard-configured floor, falling back to the shipped default.

    Imported lazily to keep this small utility usable from ``sound.settings``
    itself. The sound-settings reader already logs corrupt-file details; this
    wrapper keeps volume changes fail-soft if that path is temporarily broken.
    """
    global _SETTINGS_FLOOR_CACHE, _SETTINGS_FLOOR_WARNING_LOGGED
    try:
        from .sound import settings as sound_settings

        settings_path = sound_settings._settings_path(None)
        try:
            stat = settings_path.stat()
            signature = (str(settings_path), stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = (str(settings_path), None, None)

        with _SETTINGS_FLOOR_LOCK:
            cached = _SETTINGS_FLOOR_CACHE
            if cached is not None and cached[:3] == signature:
                return cached[3]

        floor_db = sound_settings.load_sound_settings(settings_path).volume_floor_db
        with _SETTINGS_FLOOR_LOCK:
            _SETTINGS_FLOOR_CACHE = (*signature, floor_db)
            _SETTINGS_FLOOR_WARNING_LOGGED = False
        return floor_db
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
        with _SETTINGS_FLOOR_LOCK:
            should_log = not _SETTINGS_FLOOR_WARNING_LOGGED
            _SETTINGS_FLOOR_WARNING_LOGGED = True
        if should_log:
            logger.warning(
                "volume curve: using default floor %.1f dB after settings read "
                "failed: %s",
                DEFAULT_VOLUME_FLOOR_DB,
                e,
            )
        return DEFAULT_VOLUME_FLOOR_DB


def _floor(floor_db: float | None) -> float:
    if floor_db is None:
        return configured_volume_floor_db()
    return normalize_volume_floor_db(floor_db)


def percent_to_db(percent: float, *, floor_db: float | None = None) -> float:
    """Map user-facing volume percent to a Camilla main-volume dB value.

    0% and 1% both return the floor dB; 0% is distinguished by Camilla
    ``main_mute=true`` in ``VolumeCoordinator``. This keeps 1% as the audible
    calibration floor while preserving a real mute at 0%.
    """
    p = max(0.0, min(100.0, float(percent)))
    floor = _floor(floor_db)
    if p <= 1.0:
        return floor
    span = VOLUME_CEILING_DB - floor
    return floor + span * ((p - 1.0) / 99.0)


def db_to_percent(db: float, *, floor_db: float | None = None) -> int:
    """Map Camilla dB back to the nearest user-facing percent.

    The floor dB is ambiguous: it can mean muted 0% or audible 1%.
    Legacy dB-only callers expect the floor to mean 0%, so exact/below-floor
    values return 0; persisted ``listening_level`` disambiguates modern state.
    """
    floor = _floor(floor_db)
    try:
        value = float(db)
    except (TypeError, ValueError):
        return 0
    if value <= floor:
        return 0
    if value >= VOLUME_CEILING_DB:
        return 100
    span = VOLUME_CEILING_DB - floor
    return max(1, min(100, round(1.0 + (value - floor) / span * 99.0)))


def delta_db_to_delta_percent(
    delta_db: float, *, floor_db: float | None = None
) -> int:
    """Convert a dB delta into calibrated slider percentage points."""
    floor = _floor(floor_db)
    span = VOLUME_CEILING_DB - floor
    if span <= 0:
        return 0
    return round(float(delta_db) / span * 99.0)
