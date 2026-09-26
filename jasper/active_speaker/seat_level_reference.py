# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked session gain and its microphone identity."""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.audio_measurement.ramp import RAMP_MARGIN_DB
from jasper.bass_extension.dynamic import dynamic_bass_gain_reserve_db
from jasper.atomic_io import atomic_write_json
from jasper.json_fields import finite_float, utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.paths import resolve_state_path

from ._common import coerce_finite_float
from .profile import SPL_RAISE_MARGIN_DB, spl_raise_bound_db_spl
from .fader_hold import EMERGENCY_MEASUREMENT_VOLUME_DB

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SEAT_LEVEL_REFERENCE_KIND = "jts_active_speaker_seat_level_reference"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_seat_level_reference.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE"

DEFAULT_TARGET_DB_SPL = 75.0
DEFAULT_TOLERANCE_DB = 1.0


class SeatLevelTargetError(ValueError):
    """Invalid commissioning SPL target."""


def validate_commissioning_spl(level_db_spl: float, *, ceiling_db_spl: float, margin_db: float) -> None:
    if not all(math.isfinite(value) for value in (level_db_spl, ceiling_db_spl, margin_db)) or margin_db < 0:
        raise SeatLevelTargetError("measurement SPL, ceiling and nonnegative margin must be finite")
    bound = spl_raise_bound_db_spl(ceiling_db_spl, margin_db=margin_db)
    if level_db_spl > bound:
        raise SeatLevelTargetError(f"measurement SPL {level_db_spl:g} exceeds the commissioning bound of {bound:g} dB SPL")


def validate_ramp_target_spl(level_db_spl: float, *, ceiling_db_spl: float) -> None:
    validate_commissioning_spl(level_db_spl, ceiling_db_spl=ceiling_db_spl, margin_db=RAMP_MARGIN_DB)


def rung_lift_bound_db(candidate: Mapping[str, Any], applied: Mapping[str, Any]) -> float:
    return max(0.0, dynamic_bass_gain_reserve_db(candidate) - dynamic_bass_gain_reserve_db(applied))


def predicted_rung_admission(
    fader_db: float, anchor: ResolvedLevel, candidates: Mapping[str, Mapping[str, Any]], *,
    applied: Mapping[str, Any], ceiling_db_spl: float, tolerance_db: float,
) -> dict[str, Any]:
    lift, name = max((rung_lift_bound_db(descriptor, applied), name) for name, descriptor in candidates.items())
    margin = tolerance_db + lift
    bound = spl_raise_bound_db_spl(ceiling_db_spl, margin_db=margin)
    predicted = anchor.db_spl_at(fader_db)
    # 1e-9 dB clears db_spl_at's rounding; a one-ulp fader step can round back above the bound.
    level = fader_db - (predicted - bound) - 1e-9 if predicted > bound else fader_db
    return {"level_db": level, "admitted_db_spl": anchor.db_spl_at(level), "candidate_id": name,
            "anchor_tolerance_db": tolerance_db, "lift_bound_db": lift,
            "margin_db": margin, "margin_bound_db_spl": bound,
            **({"bound_by": "commissioning_margin"} if level < fader_db else {})}


class RungMeasurementUnavailable(SeatLevelTargetError):
    def __init__(self, fields: Sequence[str], observation_index: int | None = None,
                 previous_level_db: float | None = None) -> None:
        self.evidence: dict[str, Any] = {"unavailable": list(fields), "observation_index": observation_index,
                                         "previous_level_db": previous_level_db}
        super().__init__("The previous rung has no usable SPL window measurement")


def measured_rung_admission(
    fader_db: float, observations: Sequence[Mapping[str, Any]], *, ceiling_db_spl: float, tolerance_db: float,
) -> dict[str, Any]:
    bounds = []
    margin = max(tolerance_db, SPL_RAISE_MARGIN_DB)
    levels = [finite_float(observation.get("level_db")) for observation in observations]
    lowest = min((level for level in levels if level is not None), default=None)
    for index, (observation, previous) in enumerate(zip(observations, levels), 1):
        spl = observation.get("spl") or {}
        window = finite_float(spl.get("max_window_db_spl"))
        half_second = finite_float(spl.get("loudest_half_second_db_spl"))
        stop = finite_float(spl.get("ceiling_db_spl"))
        if previous is None or window is None or half_second is None or stop is None:
            raise RungMeasurementUnavailable([name for name, value in (
                ("level_db", previous), ("max_window_db_spl", window),
                ("loudest_half_second_db_spl", half_second), ("ceiling_db_spl", stop)) if value is None], index, lowest)
        bound = spl_raise_bound_db_spl(ceiling_db_spl, measured_stop_db_spl=stop, margin_db=margin)
        bounds.append((previous + (bound - window), previous, window, half_second, bound))
    if not bounds:
        raise RungMeasurementUnavailable(["previous_rung"])
    cap, previous, window, half_second, bound = min(bounds)
    admitted = min(fader_db, cap)
    return {"requested_level_db": fader_db, "level_db": admitted,
            "previous_level_db": previous, "max_window_db_spl": window,
            "loudest_half_second_db_spl": half_second, "window_crest_db": window - half_second,
            "predicted_max_window_db_spl": window + (admitted - previous),
            "bound_db_spl": bound, "anchor_tolerance_db": tolerance_db, "margin_db": margin,
            "bound_by": "measured_window_crest" if admitted < fader_db else None}


def stimulus_mismatch(anchor_program_id: str | None, program_id: str | None) -> bool | None:
    return anchor_program_id != program_id if anchor_program_id and program_id else None


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
            "peak_dbfs": round(float(self.peak_dbfs), 2),
            "statistic": "loudest_half_second_db_spl",
            "graph_scope": "candidate",
            "bundle_id": self.bundle_id,
        }


@dataclass(frozen=True)
class SeatLevelTarget:
    """A seat-SPL band whose upper edge must clear the commissioning bound."""

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
        validate_ramp_target_spl(self.high_db_spl, ceiling_db_spl=ceiling_db_spl)

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
    return resolve_state_path(path, STATE_PATH_ENV, DEFAULT_STATE_PATH)


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
    return _reference_volume_db(load_seat_level_reference(state_path=state_path))


def seat_level_reference_status() -> dict[str, Any] | None:
    record = load_seat_level_reference()
    return None if record is None else {
        "seat_level_reference_volume_db": _reference_volume_db(record),
        "leveled_db_spl": record.get("measured_db_spl"),
    }


def _reference_volume_db(record: Mapping[str, Any] | None) -> float | None:
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
    ambient_report: Mapping[str, Any] | None = None,
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
        "stimulus": None if stimulus is None else stimulus.to_dict(),
        "ambient_report": None if ambient_report is None else dict(ambient_report),
    }
    atomic_write_json(path, payload, mode=0o640)
    return payload


#: A measurement walk drives at the banked anchor's own SPL. These are the
#: reasons that level is not knowable, or not allowed — a closed vocabulary a
#: refusal's ``reason`` comes from. There is no relative fallback: a number
#: that looks absolute and was guessed is worse than no number.
ANCHOR_UNUSABLE = "seat_anchor_unusable"


class LevelUnresolved(Exception):
    """The anchor's level is not usable, named by ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class ResolvedLevel:
    """The banked session anchor and its microphone identity."""

    anchor_db_spl: float
    reference_volume_db: float
    mic_serial: str | None
    session_id: str = ""
    leveled_at: str = ""
    target_db_spl: float = DEFAULT_TARGET_DB_SPL

    def db_spl_at(self, fader_db: float) -> float:
        return self.anchor_db_spl + (fader_db - self.reference_volume_db)

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
) -> tuple[ResolvedLevel, dict[str, Any]]:
    """Resolve supplied facts purely, or load the banked anchor for local callers.

    The anchor is already calibrated SPL; MicSensitivity.db_spl_from_dbfs
    owns the dBFS-to-SPL conversion, so this resolver does not repeat it.
    The second value is rebase evidence for the rung admission: plan documents
    and their fingerprints serialize every ResolvedLevel field.
    """
    record = facts.record if facts is not None else load_seat_level_reference(state_path=state_path) or {}
    anchor = coerce_finite_float(record.get("measured_db_spl"))
    reference_volume_db = coerce_finite_float(record.get("reference_volume_db"))
    target = coerce_finite_float((record.get("target") or {}).get("target_db_spl"))
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
    banked_sens_factor_db = coerce_finite_float(banked.get("sens_factor_db"))
    identified = bool(banked_serial and sensitivity.serial)
    rebased = anchor
    if (banked_sens_factor_db is not None and sensitivity.sens_factor_db != banked_sens_factor_db
            and (not identified or sensitivity.serial == banked_serial)):
        old = replace(sensitivity, sens_factor_db=banked_sens_factor_db)
        rebased = sensitivity.db_spl_from_dbfs(old.dbfs_from_db_spl(anchor))
        if not identified:
            # Either mic may be the banked one; the higher anchor predicts more SPL, so it clamps lower.
            rebased = max(anchor, rebased)

    return ResolvedLevel(
        anchor_db_spl=rebased,
        reference_volume_db=reference_volume_db,
        mic_serial=sensitivity.serial, session_id=str(record["session_id"]),
        leveled_at=str(record["leveled_at"]), target_db_spl=target,
    ), {"anchor_mic_serial": str(banked_serial) if banked_serial else None, "anchor_rebased_db": rebased - anchor}


def check_target_capture_dbfs(sensitivity: Any, anchor_db_spl: float) -> float:
    """The CHECK solve's capture-peak target at the run's predicted SPL.

    The anchor is a loudest-half-second RMS; the solve compares peaks.
    """
    from jasper.audio_measurement.program_analysis.model import SWEEP_PEAK_TO_RMS_DB  # lazy: keeps this module numpy-free

    target = float(sensitivity.dbfs_from_db_spl(anchor_db_spl)) + SWEEP_PEAK_TO_RMS_DB
    log_event(logger, "active_speaker.check_level_target", anchor_db_spl=anchor_db_spl,
              target_capture_dbfs=target)
    return target
