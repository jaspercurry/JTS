# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked session gain and its microphone identity."""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from jasper.audio_measurement.ramp import CEILING_MARGIN_DB, MAX_STEP_DB
from jasper.atomic_io import atomic_write_json
from jasper.json_fields import utc_now_iso as _utc_now

from ._common import finite_float
from .volume_latch import EMERGENCY_MEASUREMENT_VOLUME_DB

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity

SCHEMA_VERSION = 2
SEAT_LEVEL_REFERENCE_KIND = "jts_active_speaker_seat_level_reference"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_seat_level_reference.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE"

DEFAULT_TARGET_DB_SPL = 75.0
DEFAULT_TOLERANCE_DB = 1.0


class SeatLevelTargetError(ValueError):
    """The requested seat-SPL target is not a band this speaker may chase."""


@dataclass(frozen=True)
class StimulusProvenance:
    """The summed program and the statistic used for the session gain."""

    program_id: str
    phase: str
    wav_sha256: str
    peak_dbfs: float
    bundle_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "phase": self.phase,
            "wav_sha256": self.wav_sha256,
            "peak_dbfs": self.peak_dbfs,
            "statistic": "max_window_db_spl",
            "graph_scope": "candidate",
            "bundle_id": self.bundle_id,
        }


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
        if self.high_db_spl > ceiling_db_spl - MAX_STEP_DB - CEILING_MARGIN_DB:
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


def load_seat_level_reference(
    *, state_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Read the current schema; old sessions require a new leveling pass."""
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
    """Return a valid session gain; absent or old records require leveling."""
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
    stimulus: StimulusProvenance | None = None,
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
    leveled_at = _utc_now()
    payload = {
        "session_id": uuid.uuid4().hex, "leveled_at": leveled_at,
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": SEAT_LEVEL_REFERENCE_KIND,
        "updated_at": leveled_at,
        "state_path": str(path),
        "reference_volume_db": round(float(reference_volume_db), 3),
        "measured_db_spl": round(float(measured_db_spl), 2),
        "target": target.to_dict(),
        "mic_sensitivity": dict(sensitivity),
        "max_main_volume_db": round(float(max_main_volume_db), 3),
        # Always a key, ``None`` when the pass measured no stimulus: a consumer
        # must be able to tell "banked against a stimulus nobody recorded" from
        # "this build does not record stimuli", and a missing key cannot.
        "stimulus": None if stimulus is None else stimulus.to_dict(),
    }
    atomic_write_json(path, payload, mode=0o640)
    return payload


#: A measurement walk drives at the banked anchor's own SPL. These are the
#: reasons that level is not knowable, or not allowed — a closed vocabulary a
#: refusal's ``reason`` comes from. There is no relative fallback: a number
#: that looks absolute and was guessed is worse than no number.
ANCHOR_UNUSABLE = "seat_anchor_unusable"

#: Two sens factors this close are one number in two float reprs, not two
#: calibrations. A real recalibration moves the figure by whole tenths.
SENS_FACTOR_TOLERANCE_DB = 0.05


class LevelUnresolved(Exception):
    """The anchor's level is not usable, named by ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class ResolvedLevel:
    """The banked anchor's drive level, absolute, with the terms behind it.

    One level, not a target and an anchor: a walk drives AT the anchor, so a
    second field would be the same number under a name inviting the two to
    differ.
    """

    anchor_db_spl: float
    reference_volume_db: float
    mic_serial: str | None
    session_id: str = ""
    leveled_at: str = ""
    target_db_spl: float = DEFAULT_TARGET_DB_SPL

    def session(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "gain_db": self.reference_volume_db,
                "leveled_db_spl": self.anchor_db_spl, "target_db_spl": self.target_db_spl,
                "leveled_at": self.leveled_at}


@dataclass(frozen=True)
class AnchorFacts:
    record: Mapping[str, Any]
    sensitivity: MicSensitivity | None


def resolve_anchor_level(
    *,
    state_path: str | Path | None = None,
    calibration_file: str | Path | None = None,
    mic_serial: str | None = None,
    facts: AnchorFacts | None = None,
) -> ResolvedLevel:
    """Resolve supplied facts purely, or load the banked anchor for local callers.

    The anchor is already calibrated SPL; MicSensitivity.db_spl_from_dbfs
    owns the dBFS-to-SPL conversion, so this resolver does not repeat it.
    """
    record = facts.record if facts is not None else load_seat_level_reference(state_path=state_path) or {}
    anchor = finite_float(record.get("measured_db_spl"))
    reference_volume_db = finite_float(record.get("reference_volume_db"))
    target = finite_float((record.get("target") or {}).get("target_db_spl"))
    if (record.get("artifact_schema_version") != SCHEMA_VERSION
            or not record.get("session_id") or not record.get("leveled_at")
            or anchor is None or reference_volume_db is None or target is None
            or not EMERGENCY_MEASUREMENT_VOLUME_DB < reference_volume_db <= 0.0):
        raise LevelUnresolved(
            ANCHOR_UNUSABLE,
            "no seat-level reference is banked, so there is no anchor to "
            "drive at — run jasper-seat-level first",
        )

    banked_raw = record.get("mic_sensitivity")
    banked = banked_raw if isinstance(banked_raw, dict) else {}
    banked_serial = banked.get("serial")
    serial = mic_serial or (str(banked_serial) if banked_serial else None)
    if facts is None:
        from jasper.audio_measurement.calibration import resolve_mic_sensitivity  # lazy: numpy

        sensitivity = resolve_mic_sensitivity(calibration_file=calibration_file, mic_serial=serial)
    else:
        sensitivity = facts.sensitivity
    if sensitivity is None:
        raise LevelUnresolved(
            ANCHOR_UNUSABLE,
            "the seat-level reference banks no mic serial, so no stored "
            "calibration can be looked up for it — re-run jasper-seat-level "
            "with the mic you will measure with"
            if not serial
            else f"the anchor was measured with mic serial {serial} and no "
            "stored calibration resolves for it — store its vendor file in "
            "the calibration store, or re-run jasper-seat-level with the mic "
            "you will measure with",
        )
    if banked_serial and sensitivity.serial != banked_serial:
        raise LevelUnresolved(ANCHOR_UNUSABLE, "The anchor and current calibration name different microphones")
    banked_sens_factor_db = finite_float(banked.get("sens_factor_db"))
    if (
        banked_sens_factor_db is not None
        and abs(sensitivity.sens_factor_db - banked_sens_factor_db)
        > SENS_FACTOR_TOLERANCE_DB
    ):
        raise LevelUnresolved(
            ANCHOR_UNUSABLE,
            f"the anchor was measured with mic {serial or sensitivity.serial} "
            f"at a sens factor of {banked_sens_factor_db:g} dB, but that mic "
            f"resolves now at {sensitivity.sens_factor_db:g} dB — an anchor "
            "measured with one calibration cannot make a session measured "
            "with another absolute; re-run jasper-seat-level with the "
            "calibration you will measure with",
        )

    return ResolvedLevel(
        anchor_db_spl=anchor,
        reference_volume_db=reference_volume_db,
        mic_serial=sensitivity.serial, session_id=str(record["session_id"]),
        leveled_at=str(record["leveled_at"]), target_db_spl=target,
    )
