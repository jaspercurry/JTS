# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured seat-SPL reference volume — one writer, one reader.

:mod:`jasper.active_speaker.session_volume_plan` derives the crossover
session's fixed measurement volume as ``min(reference, max(driver caps))``.
The caps half is measured hardware truth; the reference half was a codified
guess (``MEASUREMENT_REFERENCE_VOLUME_DB = -20.0``, "PROVISIONAL pending W6
bench validation"). This module is where that guess becomes an observation.

Ownership, deliberately narrow:

* **one writer** — :mod:`jasper.active_speaker.seat_level_ramp`, after a
  closed-loop ramp measured a calibrated seat SPL inside the requested band;
* **one reader** — ``session_volume_plan.measurement_reference_volume_db``;
* **absent is normal.** A box that has never run the leveling step, or whose
  statefile is unreadable/implausible, resolves to the codified ``-20.0``
  default. Nothing regresses by not having run it.

The target BAND is not stored here and is not a property of the speaker: it is
what the operator wants tonight's session to sound like, passed per run and
bounded by the preset's ``max_commissioning_level_db_spl`` safety ceiling. Only
the *result* is durable.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_json

from .volume_latch import EMERGENCY_MEASUREMENT_VOLUME_DB

SCHEMA_VERSION = 1
SEAT_LEVEL_REFERENCE_KIND = "jts_active_speaker_seat_level_reference"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_seat_level_reference.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE"

# The operator's representative listening level for a measurement session:
# 75-80 dB SPL at the seat (owner ruling, 2026-08-19), expressed as a midpoint
# plus a symmetric tolerance because that is the shape a settle-based ramp
# window needs. Defaults only — every run may state its own, and the preset's
# ``max_commissioning_level_db_spl`` bounds whatever it states.
DEFAULT_TARGET_DB_SPL = 77.5
DEFAULT_TOLERANCE_DB = 2.5


class SeatLevelTargetError(ValueError):
    """The requested seat-SPL target is not a band this speaker may chase."""


@dataclass(frozen=True)
class SeatLevelTarget:
    """A seat-SPL band, validated against the profile's commissioning ceiling.

    The band is ``[target - tolerance, target + tolerance]``. Its TOP — not its
    midpoint — is what must clear the ceiling: a band whose upper edge sits
    above ``max_commissioning_level_db_spl`` is asking the ramp to aim at a
    level the profile forbids, and is refused at construction rather than
    silently clipped (a clipped band would converge somewhere the operator
    never asked for and record it as the reference).
    """

    target_db_spl: float
    tolerance_db: float

    @property
    def low_db_spl(self) -> float:
        return self.target_db_spl - self.tolerance_db

    @property
    def high_db_spl(self) -> float:
        return self.target_db_spl + self.tolerance_db

    def validate(self, *, ceiling_db_spl: float) -> None:
        if not math.isfinite(self.target_db_spl) or not math.isfinite(
            self.tolerance_db
        ):
            raise SeatLevelTargetError("seat-SPL target and tolerance must be finite")
        if self.tolerance_db <= 0.0:
            raise SeatLevelTargetError("seat-SPL tolerance must be positive")
        if not math.isfinite(ceiling_db_spl):
            raise SeatLevelTargetError("commissioning SPL ceiling must be finite")
        if self.high_db_spl > ceiling_db_spl:
            raise SeatLevelTargetError(
                f"seat-SPL band top {self.high_db_spl:g} dB SPL exceeds the "
                f"profile's commissioning ceiling {ceiling_db_spl:g} dB SPL"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_db_spl": self.target_db_spl,
            "tolerance_db": self.tolerance_db,
            "low_db_spl": self.low_db_spl,
            "high_db_spl": self.high_db_spl,
        }


def seat_level_reference_state_path(path: str | Path | None = None) -> Path:
    """Where the reference lives: an explicit path, the env override, or the
    default. One resolver, so the doctor probes the same file the reader reads."""
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


_state_path = seat_level_reference_state_path


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_seat_level_reference(
    *, state_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Return the persisted reference record, or ``None`` when there is none.

    Absent-tolerant and never raises: an unreadable, malformed, wrong-kind, or
    wrong-schema file is indistinguishable from no file at all, because the
    consumer's fallback (the codified default) is the conservative answer in
    every one of those cases.
    """
    path = _state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != SEAT_LEVEL_REFERENCE_KIND
        or raw.get("artifact_schema_version") != SCHEMA_VERSION
    ):
        return None
    return raw


def seat_level_reference_volume_db(
    *, state_path: str | Path | None = None
) -> float | None:
    """The measured reference volume in dB, or ``None`` to use the default.

    Fail-safe on every doubt. A value is returned only when it is finite,
    non-positive (main volume is attenuation), and strictly above the emergency
    attenuation floor — the same envelope
    ``session_measurement_volume_db`` already enforces on its own result. Any
    other stored value resolves to ``None``, so a corrupt or hostile statefile
    can only make the session QUIETER (back to the codified default), never
    louder.
    """
    record = load_seat_level_reference(state_path=state_path)
    if record is None:
        return None
    value = record.get("reference_volume_db")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    volume = float(value)
    if not math.isfinite(volume) or volume > 0.0:
        return None
    if not volume > EMERGENCY_MEASUREMENT_VOLUME_DB:
        return None
    return volume


def write_seat_level_reference(
    *,
    reference_volume_db: float,
    measured_db_spl: float,
    target: SeatLevelTarget,
    sensitivity: dict[str, Any],
    max_main_volume_db: float,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Publish one converged reference. Called ONLY on a converged ramp.

    Raises :class:`SeatLevelTargetError` if the volume is outside the envelope
    the reader accepts — writing a value the reader would reject is a silent
    no-op dressed up as success.
    """
    if (
        not math.isfinite(reference_volume_db)
        or reference_volume_db > 0.0
        or not reference_volume_db > EMERGENCY_MEASUREMENT_VOLUME_DB
    ):
        raise SeatLevelTargetError(
            f"reference volume {reference_volume_db!r} dB is outside the "
            f"({EMERGENCY_MEASUREMENT_VOLUME_DB:g}, 0.0] dB envelope"
        )
    path = _state_path(state_path)
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": SEAT_LEVEL_REFERENCE_KIND,
        "updated_at": _utc_now(),
        "state_path": str(path),
        "reference_volume_db": round(float(reference_volume_db), 3),
        "measured_db_spl": round(float(measured_db_spl), 2),
        "target": target.to_dict(),
        "mic_sensitivity": dict(sensitivity),
        "max_main_volume_db": round(float(max_main_volume_db), 3),
    }
    atomic_write_json(path, payload, mode=0o640, group_from_parent=True)
    return payload
