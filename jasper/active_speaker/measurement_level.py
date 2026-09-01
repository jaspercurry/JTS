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
  absolutely. The banked record names WHICH mic the anchor was measured with;
  whether that mic's calibration is still readable is a question only a live
  lookup answers;
* the preset's ``max_commissioning_level_db_spl`` **ceiling**, which is a hard
  stop on the doctrine's closed list: a level above it refuses by name rather
  than being quietly shrunk.

Every failure is a refusal naming the input that is missing. Falling back to a
relative level, or to a guessed sensitivity, would hand a consumer a number
that looks absolute and is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
    resolve_mic_sensitivity,
)

from .measurement_programs import MeasurementProgram
from .seat_level_reference import load_seat_level_reference

SEAT_REFERENCE_MISSING = "seat_reference_missing"
SEAT_REFERENCE_MISSING_DETAIL = (
    "no seat-level reference is banked, so there is no anchor to drive "
    "relative to — run jasper-seat-level first"
)
LEVEL_OVER_CEILING = "level_over_ceiling"
PRESET_UNAVAILABLE = "preset_unavailable"

#: The closed vocabulary a refusal's ``reason`` comes from.
LEVEL_REFUSAL_REASONS = (
    SEAT_REFERENCE_MISSING,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
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
    mic_sens_factor_db: float
    mic_serial: str | None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _preset_ceiling_db_spl() -> float:
    """The commissioning SPL ceiling, resolved as ``jasper-seat-level`` does."""

    from jasper.output_topology import load_output_topology_strict

    from .commission_wiring import resolve_capture_preset

    try:
        preset = resolve_capture_preset(load_output_topology_strict())
        ceiling = _finite(preset.safety.max_commissioning_level_db_spl)
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
    anchor = _finite((record or {}).get("measured_db_spl"))
    reference_volume_db = _finite((record or {}).get("reference_volume_db"))
    if anchor is None or reference_volume_db is None:
        raise LevelUnresolved(SEAT_REFERENCE_MISSING, SEAT_REFERENCE_MISSING_DETAIL)

    banked = (record or {}).get("mic_sensitivity")
    banked_serial = banked.get("serial") if isinstance(banked, dict) else None
    sensitivity = resolve_mic_sensitivity(
        calibration_file=calibration_file,
        mic_serial=mic_serial or (str(banked_serial) if banked_serial else None),
    )
    if sensitivity is None:
        raise LevelUnresolved(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL
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
        mic_sens_factor_db=sensitivity.sens_factor_db,
        mic_serial=sensitivity.serial,
    )
