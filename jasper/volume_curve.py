# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared user-volume percent <-> Camilla dB curve.

JTS treats 0% as a true mute owned by ``VolumeCoordinator``. The audible
slider travel is therefore 1..100%, mapped over a calibrated dB range:

    1%   -> the quietest audible step, strictly above volume_floor_db
    100% -> 0 dB

Installations with low-sensitivity speakers can raise the floor from /sound/'s advanced
settings so the bottom of the slider becomes useful without allowing positive
digital gain.
"""
from __future__ import annotations

import logging
import threading
from typing import TypeGuard

from jasper.playback_state.music_sources import VolumeMode
from .sound import settings as sound_settings
from .volume_floor import (
    DEFAULT_VOLUME_FLOOR_DB,
    RECONCILE_DRIFT_DB,
    VOLUME_CEILING_DB,
    normalize_volume_floor_db,
)

logger = logging.getLogger(__name__)

MUTE_DB_EPSILON = 1e-6

_SETTINGS_FLOOR_LOCK = threading.Lock()
_SETTINGS_FLOOR_CACHE: tuple[str, int | None, int | None, float] | None = None
_SETTINGS_FLOOR_WARNING_LOGGED = False


def configured_volume_floor_db() -> float:
    """Return the wizard-configured floor, falling back to the shipped default.

    The sound-settings reader already logs corrupt-file details; this wrapper
    keeps volume changes fail-soft if that path is temporarily broken.
    """
    global _SETTINGS_FLOOR_CACHE, _SETTINGS_FLOOR_WARNING_LOGGED
    try:
        settings_path = sound_settings.resolve_settings_path(None)
        signature: tuple[str, int | None, int | None]
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

    0% returns the floor dB and is distinguished from anything audible by
    Camilla ``main_mute=true`` in ``VolumeCoordinator``. 1% is the quietest
    audible step, not mute, so it must sit strictly above the floor: a
    quarter of the normal per-percent step keeps it audibly distinct from
    0% while landing well below 2%, and round-trips back through
    ``db_to_percent`` as 1. 2..100% spans the remaining linear range.
    """
    p = max(0.0, min(100.0, float(percent)))
    floor = _floor(floor_db)
    span = VOLUME_CEILING_DB - floor
    if p <= 0.0:
        return floor
    if p <= 1.0:
        return floor + (span / 99.0) / 4.0
    return floor + span * ((p - 1.0) / 99.0)


def db_to_percent(db: float, *, floor_db: float | None = None) -> int:
    """Map Camilla dB back to the nearest user-facing percent.

    Exact/below-floor values return 0; 1% sits strictly above the floor.
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


def main_mute_for_level(level: int) -> bool:
    """0% asserts Camilla ``main_mute``; the dB floor alone is not silence."""
    return int(level) <= 0


def main_mute_for_db(db: float) -> bool:
    """The dB twin of :func:`main_mute_for_level`; they agree for every level."""
    return float(db) <= percent_to_db(0) + MUTE_DB_EPSILON


def guard_in_effect(db: float | None) -> TypeGuard[float]:
    """Whether a push-mode Camilla level is a deliberate guard.

    Push-mode pins Camilla at 0 dB; a level further below it than the drift
    dead band is a degraded-safe guard to keep, not jitter.
    """
    return db is not None and float(db) < -RECONCILE_DRIFT_DB


def canonical_target_db(
    effective_level: int, mode: VolumeMode, persisted_db: float | None,
) -> float:
    """The absolute Camilla ``main_volume`` the canonical intent asks for,
    ignoring any duck; a releasing duck lands against it (ADR-0004).

    A camilla-master source carries the level on Camilla. Push mode pins
    Camilla at 0 dB, except that a content mute keeps the mute floor and a
    failed push's guard (``persisted_db``) stays in place.
    """
    if mode == VolumeMode.CAMILLA_MASTER:
        return percent_to_db(effective_level)
    if main_mute_for_level(effective_level):
        return percent_to_db(0)
    if guard_in_effect(persisted_db):
        return persisted_db
    return 0.0
