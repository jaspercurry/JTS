# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Value objects for canonical volume intent and source handoffs.

`jasper.volume_coordinator` is the sole mutator of these; this module holds
the frozen shapes so `jasper.control`, `jasper.mux`, and their tests can
read the same contract without importing the coordinator class itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .music_sources import Source, VolumeMode
from .volume_persistence import db_to_percent

if TYPE_CHECKING:
    from .volume_persistence import VolumeRecord


@dataclass
class _OutboundStamp:
    """Per-source last-outbound timestamp, for the same-source echo window."""
    at_mono: float


@dataclass(frozen=True)
class VolumeState:
    """One canonical interpretation of persisted speaker-volume intent.

    ``listening_level`` is the level to restore after a temporary mute.
    ``pre_mute_level`` being present is the temporary mute latch.  Every
    external surface should render ``effective_percent`` rather than
    interpreting those two persisted fields independently. ``mute_token`` is
    internal transition identity: it prevents a push renderer's stale
    pre-mute reading from being mistaken for a later user edit.
    """

    listening_level: int
    pre_mute_level: int | None = None
    mute_token: str | None = None

    @classmethod
    def from_record(
        cls,
        record: "VolumeRecord | None",
        *,
        default_level: int = 50,
    ) -> "VolumeState":
        """Project persistence through the one public volume-state contract."""
        if record is None:
            return cls(max(0, min(100, int(default_level))))
        level = (
            int(record.listening_level)
            if record.listening_level is not None
            else db_to_percent(record.main_volume_db)
        )
        return cls(
            listening_level=max(0, min(100, level)),
            pre_mute_level=record.pre_mute_level,
            mute_token=record.mute_token,
        )

    @property
    def effective_percent(self) -> int:
        return 0 if self.pre_mute_level is not None else self.listening_level

    @property
    def muted(self) -> bool:
        # Explicit 0% and temporary mute both assert the same final-output
        # silence contract. Only temporary mute has a restore target.
        return self.effective_percent == 0

    @property
    def restore_percent(self) -> int | None:
        return self.pre_mute_level


@dataclass(frozen=True)
class SourceHandoff:
    """Preparation result for a mux-owned source transition.

    ``level`` is the effective level captured during preparation, not the
    separately remembered post-unmute level.
    """
    prev_source: Source
    current_source: Source
    reason: str
    level: int
    prev_mode: VolumeMode
    current_mode: VolumeMode
    guard_db: float | None = None
    camilla_before_db: float | None = None
    push_ok: bool | None = None
    camilla_guarded: bool = False
    settled_ms: int = 0
    result: str = "ok"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result in {"ok", "degraded_safe", "noop"}
