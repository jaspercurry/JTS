# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured seat-SPL reference volume, and whether it is still usable.

:mod:`jasper.active_speaker.session_volume_plan` derives the crossover
session's fixed measurement volume as ``min(reference, max(driver caps))``.
The caps half is measured hardware truth; the reference half was a codified
guess (``MEASUREMENT_REFERENCE_VOLUME_DB = -20.0``). This module is where
that guess becomes an observation.

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
the *result* is durable — the volume, and the
:class:`StimulusProvenance` it was measured against, which is the other half of
what that volume means.
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

from ._common import finite_float
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
class StimulusProvenance:
    """WHICH signal a reference volume was measured against, and at what level.

    The other half of the reference's definition. A reference is the volume
    that produced a target SPL *for a given stimulus*, and
    ``dB SPL = stimulus dBFS + chain gain + volume`` — so with the stimulus
    term unrecorded, two references taken against different WAVs differ by a
    number no consumer can see, and every comparison of them (drift detection,
    doctor lines, an LLM reasoning about the rig) mis-attributes that
    difference to the hardware.

    ``sha256`` is the identity a path alone cannot carry: the same path can be
    a different file on the next session. ``band_hz`` is the DECLARED band a
    generated stimulus was synthesized over, and ``None`` for an
    operator-named WAV whose band nobody declared — never a measured estimate.
    """

    path: str
    sha256: str
    peak_dbfs: float
    rms_dbfs: float
    band_hz: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "peak_dbfs": round(float(self.peak_dbfs), 2),
            "rms_dbfs": round(float(self.rms_dbfs), 2),
            "band_hz": (
                None
                if self.band_hz is None
                else [round(float(edge), 1) for edge in self.band_hz]
            ),
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
LEVEL_OVER_CEILING = "level_over_ceiling"
PRESET_UNAVAILABLE = "preset_unavailable"

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


def _ceiling_db_spl() -> float:
    """The commissioning SPL ceiling, resolved as ``jasper-seat-level`` does."""

    from jasper.output_topology import load_output_topology_strict

    from .commission_wiring import commissioning_spl_ceiling_db

    try:
        return commissioning_spl_ceiling_db(load_output_topology_strict())
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise LevelUnresolved(PRESET_UNAVAILABLE, str(exc)) from exc


def resolve_anchor_level(
    *,
    state_path: str | Path | None = None,
    ceiling_db_spl: float | None = None,
    calibration_file: str | Path | None = None,
    mic_serial: str | None = None,
) -> ResolvedLevel:
    """The banked anchor as an absolute level, or :class:`LevelUnresolved`.

    The anchor's ``measured_db_spl`` is already calibrated SPL — the mic's
    sensitivity entered it at the ramp, which is why nothing here re-derives
    ``dB SPL = dBFS - sens_factor + 94``
    (:meth:`~jasper.audio_measurement.calibration.MicSensitivity.db_spl_from_dbfs`
    owns that relation). What is asked here is whether that number still means
    something for a session about to run: the mic it was measured with must
    still resolve, at the same sensitivity, and the level must sit under the
    preset's ``max_commissioning_level_db_spl``.

    ``calibration_file``/``mic_serial`` mirror ``jasper-seat-level``'s own mic
    inputs; with neither, the mic banked with the anchor is looked up.
    """

    # Function-local: importing ``jasper.audio_measurement`` costs numpy, and
    # this module's other readers (jasper-doctor, session_volume_plan) never
    # reach here. Pinned by ``test_seat_level_anchor.py``.
    from jasper.audio_measurement.calibration import resolve_mic_sensitivity

    record = load_seat_level_reference(state_path=state_path) or {}
    anchor = finite_float(record.get("measured_db_spl"))
    reference_volume_db = finite_float(record.get("reference_volume_db"))
    if anchor is None or reference_volume_db is None:
        raise LevelUnresolved(
            ANCHOR_UNUSABLE,
            "no seat-level reference is banked, so there is no anchor to "
            "drive at — run jasper-seat-level first",
        )

    banked_raw = record.get("mic_sensitivity")
    banked = banked_raw if isinstance(banked_raw, dict) else {}
    banked_serial = banked.get("serial")
    serial = mic_serial or (str(banked_serial) if banked_serial else None)
    # The banked block is ``MicSensitivity.to_dict()`` — sens factor, gain and
    # serial, no model — while a stored record is keyed provider/model/serial,
    # so the lookup runs under ``resolve_mic_sensitivity``'s default model.
    sensitivity = resolve_mic_sensitivity(
        calibration_file=calibration_file, mic_serial=serial
    )
    if sensitivity is None:
        raise LevelUnresolved(
            ANCHOR_UNUSABLE,
            "the seat-level reference banks no mic serial, so no stored "
            "calibration can be looked up for it — re-run jasper-seat-level "
            "with the mic you will measure with"
            if not serial
            else f"the anchor was measured with mic serial {serial} and no "
            "stored calibration resolves for it — store its vendor file "
            "through the calibration wizard (/correction/calibration/fetch), "
            "or re-run jasper-seat-level with the mic you will measure with",
        )
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

    ceiling = _ceiling_db_spl() if ceiling_db_spl is None else float(ceiling_db_spl)
    if anchor > ceiling:
        raise LevelUnresolved(
            LEVEL_OVER_CEILING,
            f"the banked anchor is {anchor:g} dB SPL, above the preset's "
            f"commissioning ceiling of {ceiling:g} dB SPL",
        )
    return ResolvedLevel(
        anchor_db_spl=anchor,
        reference_volume_db=reference_volume_db,
        mic_serial=sensitivity.serial,
    )
