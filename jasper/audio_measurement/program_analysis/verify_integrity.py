# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture integrity for the VERIFY phase."""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.program import (
    ExcitationProgram,
    KIND_SUMMED_SWEEP,
    STIMULUS_KINDS,
)
from jasper.audio_measurement.repeated_sweep import SummedPassAlignment, summed_pass_refusal
from jasper.log_event import log_event
from .drift import _estimate_drift
from .model import (
    CaptureIntegrity,
    INTEGRITY_CHECK_CLIPPED_RUN,
    INTEGRITY_CHECK_DISCONTINUITY_STEP,
    INTEGRITY_CHECK_FRAME_LEDGER,
    INTEGRITY_CHECK_CAPTURE_OVERRUN,
    INTEGRITY_CHECK_REPEAT_EPSILON,
    INTEGRITY_CHECK_REPEAT_LEVEL,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    INTEGRITY_FAIL,
    _INTEGRITY_NO_FRAME_COUNT,
    _INTEGRITY_NO_CAPTURE_REPORT,
    _INTEGRITY_NO_REPEAT_PAIR,
    _INTEGRITY_NO_STIMULUS,
    _INTEGRITY_NO_SUMMED_SWEEP,
    INTEGRITY_NOT_EVALUATED,
    INTEGRITY_PASS,
    _INTEGRITY_STEP_NEEDS_MORE_SWEEPS,
    _INTEGRITY_SWEEP_NOT_HEARD,
    IntegrityCheck,
    logger,
    SegmentLocation,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
)


def _frame_accounting_checks(ledger: FrameLedger) -> list[IntegrityCheck]:
    """Check microphone loss and frame transfer; absent counters stay unevaluated."""
    checks: list[IntegrityCheck] = []
    if not ledger.capture_gap_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CAPTURE_OVERRUN, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_CAPTURE_REPORT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CAPTURE_OVERRUN,
            INTEGRITY_FAIL if ledger.capture_gap_frames else INTEGRITY_PASS,
        ))
    if not ledger.balance_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_FRAME_COUNT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER,
            INTEGRITY_PASS if ledger.balanced else INTEGRITY_FAIL,
        ))
    return checks


def _log_frame_ledger(program: ExcitationProgram, ledger: FrameLedger) -> None:
    """One structured line per analyzed capture — the self-report itself.

    Emitted on every phase, including a clean capture (at INFO), so "no
    loss reported" and "no capture analysed" stay distinguishable. A short
    capture is the same line at WARNING.
    """
    lost = ledger.lost_at
    log_event(
        logger,
        "program_analysis.frame_ledger",
        level=logging.WARNING if lost else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        received_frames=ledger.received_frames,
        declared_frames=ledger.declared_frames,
        encoded_frames=ledger.encoded_frames,
        capture_gaps=ledger.capture_gaps,
        capture_gap_frames=ledger.capture_gap_frames,
        lost_at=",".join(lost),
    )


def _verify_capture_integrity(
    program: ExcitationProgram,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    frame_ledger: FrameLedger,
    *, capture: np.ndarray | None = None, offset: int = 0,
    repeat_locations: Sequence[SegmentLocation] | None = None,
    alignment: SummedPassAlignment | None = None,
) -> CaptureIntegrity:
    """Check frame accounting, located sweeps, clipping and available repeat pairs.

    Repeat checks use aligned individual passes, before their average.
    Single sweeps cannot answer repeat questions. The step fit additionally
    requires enough repeats to leave two residual degrees of freedom.
    """
    sweeps = [loc for loc in locations if loc.kind == KIND_SUMMED_SWEEP]
    stimuli = [loc for loc in locations if loc.kind in STIMULUS_KINDS]
    clipped_segments = tuple(loc.segment_id for loc in stimuli if loc.clipped)

    confidence_min: float | None = None
    residual_ms_worst: float | None = None
    if sweeps:
        confidence_min = min(float(loc.confidence) for loc in sweeps)
        worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
        residual_ms_worst = float(worst.residual_samples) / sample_rate * 1000.0

    checks: list[IntegrityCheck] = _frame_accounting_checks(frame_ledger)
    if confidence_min is None or residual_ms_worst is None:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
    elif confidence_min < SWEEP_LOCATE_CONFIDENCE_FLOOR:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_FAIL))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_SWEEP_NOT_HEARD,
        ))
    else:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_PASS))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE,
            INTEGRITY_FAIL
            if abs(residual_ms_worst) > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
            else INTEGRITY_PASS,
        ))
    if not stimuli:
        # No stimulus segment to inspect is not "nothing was clipped" — it is
        # the same "nobody looked" a bare False would have been.
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_STIMULUS,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN,
            INTEGRITY_FAIL if clipped_segments else INTEGRITY_PASS,
        ))
    refusal = None
    drift = None
    repeated = sum(segment.kind == KIND_SUMMED_SWEEP for segment in program.segments) > 1
    if repeated and capture is not None:
        repeat_locations = repeat_locations if repeat_locations is not None else locations
        repeat_confidence = min((loc.confidence for loc in repeat_locations if loc.kind == KIND_SUMMED_SWEEP), default=0.0)
        refusal = summed_pass_refusal(program, capture, offset)
        if refusal:
            checks.append(IntegrityCheck(refusal, INTEGRITY_FAIL))
        if refusal is None and repeat_confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR:
            drift = _estimate_drift(program, capture, sample_rate, repeat_locations)
    for name, input_code in (
        (INTEGRITY_CHECK_REPEAT_EPSILON, "epsilon_out_of_bound"),
        (INTEGRITY_CHECK_REPEAT_LEVEL, "repeat_level_disagree"),
        (INTEGRITY_CHECK_WITHIN_ROLE_DESYNC, "residual_desync"),
        (INTEGRITY_CHECK_DISCONTINUITY_STEP, "timeline_slip"),
    ):
        if drift is None:
            checks.append(IntegrityCheck(
                name, INTEGRITY_NOT_EVALUATED,
                refusal or (_INTEGRITY_SWEEP_NOT_HEARD if repeated else
                            _INTEGRITY_STEP_NEEDS_MORE_SWEEPS if name == INTEGRITY_CHECK_DISCONTINUITY_STEP
                            else _INTEGRITY_NO_REPEAT_PAIR),
            ))
        elif name == INTEGRITY_CHECK_DISCONTINUITY_STEP and not drift.discontinuity_resolvable:
            checks.append(IntegrityCheck(name, INTEGRITY_NOT_EVALUATED, _INTEGRITY_STEP_NEEDS_MORE_SWEEPS))
        else:
            checks.append(IntegrityCheck(name, INTEGRITY_FAIL if input_code in drift.glitch_inputs else INTEGRITY_PASS))

    integrity = CaptureIntegrity(
        checks=tuple(checks),
        locate_confidence_min=confidence_min,
        schedule_residual_ms_worst=residual_ms_worst,
        clipped_segments=clipped_segments,
        pass_alignment=alignment,
    )
    if integrity.glitched:
        # The VERIFY twin of ``program_analysis.glitch``, at the same level
        # and for the same reason: the capture this fired on is about to be
        # refused, and the journal should say which measurement said so.
        log_event(
            logger,
            "program_analysis.capture_integrity",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            failed=",".join(integrity.failed),
            not_evaluated=",".join(integrity.not_evaluated),
            locate_confidence_min=(
                round(confidence_min, 4) if confidence_min is not None else None
            ),
            schedule_residual_ms_worst=(
                round(residual_ms_worst, 3) if residual_ms_worst is not None else None
            ),
            clipped_segments=",".join(clipped_segments),
        )
    return integrity
