# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture integrity for the VERIFY phase."""

from __future__ import annotations

import logging
from typing import Sequence

from jasper.audio_measurement.frame_ledger import FrameLedger
from jasper.audio_measurement.program import (
    ExcitationProgram,
    KIND_SUMMED_SWEEP,
    STIMULUS_KINDS,
)
from jasper.log_event import log_event
from .model import (
    CaptureIntegrity,
    INTEGRITY_CHECK_CLIPPED_RUN,
    INTEGRITY_CHECK_DISCONTINUITY_STEP,
    INTEGRITY_CHECK_FRAME_LEDGER,
    INTEGRITY_CHECK_RENDER_GAP,
    INTEGRITY_CHECK_REPEAT_EPSILON,
    INTEGRITY_CHECK_REPEAT_LEVEL,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    INTEGRITY_FAIL,
    _INTEGRITY_NO_FRAME_COUNT,
    _INTEGRITY_NO_RENDER_REPORT,
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
    """The two frame-accounting checks, most fundamental first.

    ``capture_render_gap`` asks whether the browser's render graph handed the
    recorder every quantum; ``frame_ledger`` asks whether every frame the
    page declared reached this host (see
    :mod:`jasper.audio_measurement.frame_ledger`). A page that reported
    nothing leaves both ``not_evaluated``, never failed —
    ``verification.evaluate_capture_validity`` treats that as usable.
    """
    checks: list[IntegrityCheck] = []
    if not ledger.render_gap_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_RENDER_REPORT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP,
            INTEGRITY_FAIL if ledger.render_gap_frames else INTEGRITY_PASS,
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
        render_gaps=ledger.render_gaps,
        render_gap_frames=ledger.render_gap_frames,
        lost_at=",".join(lost),
    )


def _verify_capture_integrity(
    program: ExcitationProgram,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    frame_ledger: FrameLedger,
) -> CaptureIntegrity:
    """Capture-integrity evidence for a ONE-summed-sweep program.

    ``_estimate_drift`` cannot run here: its three glitch inputs all compare
    a role's repeated sweeps, and VERIFY plays one mono summed sweep. The
    honest record is "drift checks did not run, here is what did".

    What runs, in routing order: (0) frame accounting
    (:func:`_frame_accounting_checks`), ahead of every signal question — a
    capture missing a render quantum can locate its sweep perfectly and
    still be a splice; (1) heard — locate confidence against
    :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR`; (2) schedule — |residual| against
    :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`, only when (1) passed
    (otherwise ``not_evaluated`` with the residual still disclosed); (3)
    clipped run, independent of (1). Pilot segments are excluded from (1)
    and (2) (short/quiet windows locate coarsely) and included in (3).

    What (2) cannot see: a splice INSIDE the summed sweep (the residual is
    measured at the located START; needs more sweeps than VERIFY has — (0)
    only closes the browser-visible half of this class); a splice BEFORE
    the first stimulus (absorbed by the global offset, correctly, since a
    uniformly shifted capture is not corrupt); and anything on a
    pilot-less VERIFY program, where the summed sweep IS the anchor and its
    residual is structurally ~0 (every session-composed VERIFY carries a
    leading pilot pair instead).
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
    for name in (
        INTEGRITY_CHECK_REPEAT_EPSILON,
        INTEGRITY_CHECK_REPEAT_LEVEL,
        INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    ):
        checks.append(IntegrityCheck(
            name, INTEGRITY_NOT_EVALUATED, _INTEGRITY_NO_REPEAT_PAIR,
        ))
    checks.append(IntegrityCheck(
        INTEGRITY_CHECK_DISCONTINUITY_STEP, INTEGRITY_NOT_EVALUATED,
        _INTEGRITY_STEP_NEEDS_MORE_SWEEPS,
    ))

    integrity = CaptureIntegrity(
        checks=tuple(checks),
        locate_confidence_min=confidence_min,
        schedule_residual_ms_worst=residual_ms_worst,
        clipped_segments=clipped_segments,
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
