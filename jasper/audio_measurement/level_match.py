# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-geometry level-match lock storage.

The lock is scoped **per mic-geometry step, not blanket per-session**
(near-field baffle vs listening position differ ~15-25 dB at the mic for the
same played level, so one lock reused across geometries blows past the window
or starves SNR). :class:`LevelLockStore` keys on the geometry;
:class:`MeasurementLevelLock` is the stored, wire-serializable result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jasper.audio_measurement.ramp import LEVEL_EVENT_SCHEMA_VERSION
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


class RampLockKind(str, Enum):
    """Why a terminal ``LOCKED`` result is usable.

    ``BOUNDED_LOW_LEVEL`` records that the hard/dynamic gain bound was honored
    and the live mic evidence was trustworthy and stable, but the measured level
    still fell short of the preferred window -- never a claim that the normal
    target was reached.
    """

    IN_WINDOW = "in_window"
    BOUNDED_LOW_LEVEL = "bounded_low_level"
    MANUAL = "manual"


@dataclass(frozen=True)
class MeasurementLevelLock:
    """A locked measurement level for ONE mic geometry.

    ``main_volume_db`` is the digital level the ramp settled on. ``gain_map_db``
    is the recovered chain gain ``G`` (``settled_mic_dbfs - main_volume_db``);
    together they say "at this geometry, this volume put the mic at
    ``main_volume_db + gain_map_db`` dBFS". ``noise_floor_dbfs`` is the phone's
    pre-ramp floor (context for the trust gate). ``lock_kind`` distinguishes an
    ordinary in-window lock, a manual lock, and the evidence-backed bounded-low
    cap policy. The settled SNR, preferred-window shortfall, and sample spread
    keep that degraded decision observable. ``agc_frozen`` records whether the
    reference is trustworthy (a ``False`` here means the lock came from the
    degraded manual-lock path and the drift rule is disabled for it).
    """

    geometry: str
    main_volume_db: float
    gain_map_db: float | None
    settled_mic_dbfs: float | None
    noise_floor_dbfs: float | None
    lock_kind: RampLockKind = RampLockKind.IN_WINDOW
    settled_snr_db: float | None = None
    window_shortfall_db: float | None = None
    settled_spread_db: float | None = None
    agc_frozen: bool = True
    schema_version: int = LEVEL_EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "geometry": self.geometry,
            "lock_kind": self.lock_kind.value,
            "main_volume_db": round(self.main_volume_db, 2),
            "gain_map_db": (
                round(self.gain_map_db, 2) if self.gain_map_db is not None else None
            ),
            "settled_mic_dbfs": (
                round(self.settled_mic_dbfs, 2)
                if self.settled_mic_dbfs is not None
                else None
            ),
            "noise_floor_dbfs": (
                round(self.noise_floor_dbfs, 2)
                if self.noise_floor_dbfs is not None
                else None
            ),
            "settled_snr_db": (
                round(self.settled_snr_db, 2)
                if self.settled_snr_db is not None
                else None
            ),
            "window_shortfall_db": (
                round(self.window_shortfall_db, 2)
                if self.window_shortfall_db is not None
                else None
            ),
            "settled_spread_db": (
                round(self.settled_spread_db, 2)
                if self.settled_spread_db is not None
                else None
            ),
            "agc_frozen": self.agc_frozen,
        }


class LevelLockStore:
    """Session-scoped store of the current lock per mic geometry.

    Not one value for the whole session — a dict keyed by geometry, so a
    near-field lock and a listening-position lock coexist and neither clobbers
    the other. In-memory; the correction session owns its lifetime.
    """

    def __init__(self) -> None:
        self._locks: dict[str, MeasurementLevelLock] = {}

    def put(self, lock: MeasurementLevelLock) -> None:
        self._locks[lock.geometry] = lock
        log_event(
            logger,
            "level_lock_stored",
            geometry=lock.geometry,
            main_volume_db=f"{lock.main_volume_db:.1f}",
            lock_kind=lock.lock_kind.value,
            gain_map_db=(
                f"{lock.gain_map_db:.1f}" if lock.gain_map_db is not None else ""
            ),
            agc_frozen=lock.agc_frozen,
        )

    def discard(self, geometry: str) -> None:
        """Forget one invalidated geometry without disturbing sibling locks."""

        self._locks.pop(str(geometry), None)

    def get(self, geometry: str) -> MeasurementLevelLock | None:
        return self._locks.get(geometry)

    def snapshot(self) -> dict[str, Any]:
        return {geo: lock.to_dict() for geo, lock in self._locks.items()}
