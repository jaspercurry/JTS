# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a measurement program drives at, in dB SPL at the microphone.

A program states its level RELATIVE to the anchor (``level_re_anchor_db``),
never as an absolute constant. This module turns that relative statement into
one absolute number, from the three facts that make it absolute:

* the **anchor** — the seat-level reference's ``measured_db_spl``, the
  calibrated SPL a closed-loop ramp converged to at the banked volume. The
  mic's sensitivity entered the anchor there, which is why nothing here
  re-derives ``dB SPL = dBFS - sens_factor + 94``
  (:meth:`~jasper.audio_measurement.calibration.MicSensitivity.db_spl_from_dbfs`
  owns that relation, at the one place it is applied);
* the **microphone**, resolved NOW through the path ``jasper-seat-level``
  uses, because the session about to run must be able to read its own captures
  absolutely. The banked record names WHICH mic the anchor was measured with
  and at WHAT sensitivity; whether that mic's calibration is still readable,
  and still the same one, are questions only a live lookup answers;
* the preset's ``max_commissioning_level_db_spl`` **ceiling**, which is a hard
  stop on the doctrine's closed list: a level above it refuses by name rather
  than being quietly shrunk.

Every failure is a refusal naming the input that is missing. Falling back to a
relative level, or to a guessed sensitivity, would hand a consumer a number
that looks absolute and is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jasper.audio_measurement.calibration import (
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
    resolve_mic_sensitivity,
)

from ._common import finite_float
from .measurement_programs import MeasurementProgram
from .seat_level_reference import load_seat_level_reference

SEAT_REFERENCE_MISSING = "seat_reference_missing"
SEAT_REFERENCE_MISSING_DETAIL = (
    "no seat-level reference is banked, so there is no anchor to drive "
    "relative to — run jasper-seat-level first"
)
MIC_CALIBRATION_CHANGED = "mic_calibration_changed"
LEVEL_OVER_CEILING = "level_over_ceiling"
PRESET_UNAVAILABLE = "preset_unavailable"

#: Two sens factors this close are one number in two float reprs, not two
#: calibrations. A real recalibration moves the figure by whole tenths.
SENS_FACTOR_TOLERANCE_DB = 0.05

#: The closed vocabulary a refusal's ``reason`` comes from.
LEVEL_REFUSAL_REASONS = (
    SEAT_REFERENCE_MISSING,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
    MIC_CALIBRATION_CHANGED,
    LEVEL_OVER_CEILING,
    PRESET_UNAVAILABLE,
)


class LevelUnresolved(Exception):
    """One input the absolute level needs is missing, named by ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class ResolvedLevel:
    """One program's drive level, absolute, with the terms it was built from."""

    target_db_spl: float
    anchor_db_spl: float
    level_re_anchor_db: float
    reference_volume_db: float
    mic_serial: str | None


def _mic_unavailable_detail(serial: str | None) -> str:
    """Why no absolute reference resolved, and the remedy THIS door can follow.

    ``jasper-angle-capture`` deliberately has no ``--calibration-file``: the
    mic it must resolve is the one the anchor was measured with, so the
    remedies are the wizard that stores that mic's vendor file and re-running
    the anchor with the mic actually in hand.
    """

    if not serial:
        return (
            "the seat-level reference banks no mic serial, so no stored "
            "calibration can be looked up for it — re-run jasper-seat-level "
            "with the mic you will measure with"
        )
    return (
        f"the anchor was measured with mic serial {serial} and no stored "
        "calibration resolves for it — store its vendor file through the "
        "calibration wizard (/correction/calibration/fetch), or re-run "
        "jasper-seat-level with the mic you will measure with"
    )


def _preset_ceiling_db_spl() -> float:
    """The commissioning SPL ceiling, resolved as ``jasper-seat-level`` does."""

    from jasper.output_topology import load_output_topology_strict

    from .commission_wiring import resolve_capture_preset

    try:
        preset = resolve_capture_preset(load_output_topology_strict())
        ceiling = finite_float(preset.safety.max_commissioning_level_db_spl)
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise LevelUnresolved(PRESET_UNAVAILABLE, str(exc)) from exc
    if ceiling is None:
        raise LevelUnresolved(
            PRESET_UNAVAILABLE,
            "the preset declares no finite max_commissioning_level_db_spl",
        )
    return ceiling


def resolve_program_level(
    program: MeasurementProgram | None,
    *,
    state_path: str | Path | None = None,
    ceiling_db_spl: float | None = None,
    calibration_file: str | Path | None = None,
    mic_serial: str | None = None,
) -> ResolvedLevel:
    """This program's absolute drive level, or :class:`LevelUnresolved`.

    ``program`` is ``None`` for a walk no program named: that walk drives at
    the anchor itself, so its level is anchor-relative zero like every shipped
    row. ``calibration_file``/``mic_serial`` mirror ``jasper-seat-level``'s own
    mic inputs; with neither, the mic banked with the anchor is looked up.
    """

    level_re_anchor_db = 0.0 if program is None else program.level_re_anchor_db
    record = load_seat_level_reference(state_path=state_path)
    anchor = finite_float((record or {}).get("measured_db_spl"))
    reference_volume_db = finite_float((record or {}).get("reference_volume_db"))
    if anchor is None or reference_volume_db is None:
        raise LevelUnresolved(SEAT_REFERENCE_MISSING, SEAT_REFERENCE_MISSING_DETAIL)

    banked_raw = (record or {}).get("mic_sensitivity")
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
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, _mic_unavailable_detail(serial)
        )
    banked_sens_factor_db = finite_float(banked.get("sens_factor_db"))
    if (
        banked_sens_factor_db is not None
        and abs(sensitivity.sens_factor_db - banked_sens_factor_db)
        > SENS_FACTOR_TOLERANCE_DB
    ):
        raise LevelUnresolved(
            MIC_CALIBRATION_CHANGED,
            "the anchor was measured with mic "
            f"{serial or sensitivity.serial} at a sens factor of "
            f"{banked_sens_factor_db:g} dB, but that mic resolves now at "
            f"{sensitivity.sens_factor_db:g} dB — an anchor measured with one "
            "calibration cannot make a session measured with another absolute; "
            "re-run jasper-seat-level with the calibration you will measure "
            "with",
        )

    target_db_spl = anchor + level_re_anchor_db
    ceiling = (
        _preset_ceiling_db_spl() if ceiling_db_spl is None else float(ceiling_db_spl)
    )
    if target_db_spl > ceiling:
        raise LevelUnresolved(
            LEVEL_OVER_CEILING,
            f"the program drives at {target_db_spl:g} dB SPL (anchor "
            f"{anchor:g} {level_re_anchor_db:+g}), above the preset's "
            f"commissioning ceiling of {ceiling:g} dB SPL",
        )
    return ResolvedLevel(
        target_db_spl=target_db_spl,
        anchor_db_spl=anchor,
        level_re_anchor_db=level_re_anchor_db,
        reference_volume_db=reference_volume_db,
        mic_serial=sensitivity.serial,
    )
