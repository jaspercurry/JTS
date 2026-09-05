# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commissioning journal: the log fields, and the emitters that write them.

Read-only against a :class:`ProgramAnalysis`, a verdict and a handful of
session values the caller passes in. Nothing decides a verdict, retries a
capture or touches session state — these run AFTER the verdict exists,
which is what lets :func:`_safe_log_diag` degrade a bug in here to a WARN.
The ``logger`` every emitter takes is the CALLER's: an operator greps one
channel for a session, and moving these emitters out of
:mod:`jasper.active_speaker.crossover_v2_flow` must not split it.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Mapping, Sequence

from jasper.active_speaker.crossover_v2.admission import (
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
)
from jasper.active_speaker.crossover_v2.capture_dispatch import (
    _gate_floor_source,
    _gate_window_ms,
    _pilot_by_role,
    _pilot_diag_fields,
    _pilot_transfer_by_role,
    _sweep_schedule_diag_fields,
)
from jasper.active_speaker.crossover_v2.contracts import (
    REFERENCE_MARK_DESIGN_AXIS,
    VERIFY_TOLERANCE_DB,
    _rounded,
)
from jasper.active_speaker.crossover_v2.intervention import (
    driver_response_by_role as _driver_response_by_role,
    measure_validity_floor_hz as _measure_validity_floor_hz,
)
from jasper.active_speaker.crossover_v2.planning import (
    alignment_to_candidate_fields,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_VERIFY_LEVEL_SHIFT,
    PhaseVerdict,
)
from jasper.active_speaker.crossover_v2.verification import _band_edge
from jasper.audio_measurement.program import VERIFY_PILOT_ROLE
from jasper.audio_measurement.program_analysis import (
    CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB,
    CHANNEL_MAP_MIN_ISOLATION_DB,
    CaptureIntegrity,
    ProgramAnalysis,
    polarity_label,
)
from jasper.log_event import log_event


def _capture_integrity_log_field(integrity: CaptureIntegrity | None) -> str:
    """One logfmt token for a VERIFY capture's integrity verdict (#1971).

    Three values a reader must be able to tell apart, which is why this is not
    a bool: ``unavailable`` (no record), ``ok`` (every evaluated check passed),
    or the comma-joined names of the checks that FAILED. The companion
    ``integrity_not_evaluated`` field carries what could not be checked at all,
    so "ok" never has to stand in for "nobody looked".
    """
    if integrity is None:
        return "unavailable"
    return ",".join(integrity.failed) if integrity.failed else "ok"


CLAIM_NAMES = ("woofer_branch", "hf_branch", "integration", "absolute")


def _claims_log_field(claims: Mapping[str, Any]) -> str:
    """One logfmt token for the whole §7 claim record.

    ``name:state`` per claim, comma-joined, a not-evaluated one carrying its
    reason, so a corpus sweep can count what was actually judged instead of
    inferring it from a bare ``accepted=true``. ``""`` for an early refusal
    that graded nothing.
    """
    return ",".join(
        f"{name}:{claims[name].get('status')}"
        f"{'(%s)' % claims[name]['reason'] if claims[name].get('reason') else ''}"
        for name in CLAIM_NAMES
        if isinstance(claims.get(name), Mapping)
    )


def _driver_snr_fields(
    resp: Any | None,
) -> tuple[float | None, str | None, str | None]:
    """``(estimated_snr_db, verdict, band_id)`` from a driver's worst SNR band.

    The band identity travels because the number and the verdict alone cannot
    say WHICH band produced them (#2613). ``band_id`` stays ``None`` when
    ``worst_band_verdict`` selected a band carrying no id — it filters on
    overlap and verdict rank, never on identity — so a real band is never
    confused with an absent one.
    """
    if resp is None or resp.snr is None:
        return None, None, None
    worst = resp.snr.get("worst_relevant") or {}
    return (
        worst.get("estimated_snr_db"), worst.get("verdict"), worst.get("band_id"),
    )


# The finite stand-in logged for `_pilot_in_band_snr_db`'s ``-inf``: the
# estimate is unusable rather than merely low. JSON has no infinity, and
# DROPPING the value is worse than substituting one — a two-role capture
# with one buried pilot and one clean one would log the CLEAN pilot's SNR
# beside ``pilot_snr_ok=False`` (#1810). -120 dB mirrors
# `program_analysis.DBFS_FLOOR` and keeps the field monotone-comparable, so
# `min(...)` still selects the worst pilot.
PILOT_SNR_UNUSABLE_DB = -120.0


def _worst_pilot_snr_db(analysis: ProgramAnalysis) -> float | None:
    """The lowest quiet-pilot in-band SNR across this capture's pilots.

    The number the ``pilot_snr_ok`` aggregate was thresholded from, so the diag
    line says HOW low. The two infinities `_pilot_in_band_snr_db` can return
    differ on purpose: ``+inf`` is "no ambient evidence to validate against",
    not a measurement, so it is EXCLUDED and an all-``+inf`` capture logs
    ``None``; ``-inf`` is "the pilot never exceeded the ambient", which IS a
    measurement, so it substitutes :data:`PILOT_SNR_UNUSABLE_DB`.
    """
    values = [
        PILOT_SNR_UNUSABLE_DB if p.snr_db == -math.inf else p.snr_db
        for p in analysis.pilots
        if p.snr_db != math.inf
    ]
    return round(min(values), 2) if values else None


def _emit_cloud_combine_diagnostics(
    logger: logging.Logger, diagnostics: Mapping[str, Any] | None,
) -> None:
    """Journal what the side-effect-free combiner could only hand back as data.

    :mod:`jasper.active_speaker.crossover_v2.spatial` owns the combine and is
    side-effect-free by charter, so it returns its log fields rather than
    writing them. The event NAME lives here, with the journal that carries it.
    """
    if diagnostics is None:
        return
    log_event(
        logger, "correction.crossover_v2_cloud_combine_failed",
        level=logging.WARNING, **diagnostics,
    )


def _safe_log_diag(
    logger: logging.Logger,
    log_fn: Callable[[ProgramAnalysis, PhaseVerdict], None],
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
) -> None:
    """Best-effort wrapper around one ``_log_*_diag`` call.

    A bug in diagnostic-field extraction (a malformed ``analysis``, an
    unexpected ``None``) must never crash the capture or change the verdict
    the flow already decided; it degrades to a WARN. The caught set matches
    the realistic failure modes of these read-only field-extraction calls
    (attribute/key/index access and numeric conversion) — never a bare
    ``except Exception``.
    """
    try:
        log_fn(analysis, verdict)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError):
        log_event(
            logger, "correction.crossover_v2_diag_log_failed",
            level=logging.WARNING, session_id=session_id,
            phase=analysis.phase, exc_info=True,
        )


def _log_slot_spent(
    logger: logging.Logger,
    phase: str,
    index: int,
    observed: str,
    outcome: str,
    *,
    session_id: str,
    diagnosis: str,
    pilot_heard: bool | None,
    reflection_measured: bool | None,
) -> None:
    log_event(
        logger, "correction.crossover_v2_position_attempts_spent",
        level=logging.WARNING,
        session_id=session_id, phase=phase, index=index,
        observed=observed, outcome=outcome,
        # A settled accepted result has ``code=None`` by protocol. The final rejected
        # capture's exact observation/evidence pairing is preserved here.
        diagnosis=diagnosis,
        pilot_heard=pilot_heard,
        reflection_measured=reflection_measured,
        extra_allowed=MAX_EXTRA_ATTEMPTS_PER_POSITION,
    )


def _log_condition_settled(
    logger: logging.Logger,
    phase: str,
    index: int,
    observed: str,
    outcome: str,
    diagnosis: str,
    *,
    session_id: str,
) -> None:
    """The journal line for a slot closed by its CONDITION, not its meter.

    Its own event rather than ``_log_slot_spent``: that line is named
    ``position_attempts_spent`` and carries ``extra_allowed``, and a rejection
    settled on the first take spent nothing.
    """
    log_event(
        logger, "correction.crossover_v2_position_not_retriable",
        level=logging.WARNING,
        session_id=session_id, phase=phase, index=index,
        observed=observed, outcome=outcome, diagnosis=diagnosis,
    )


def _log_check_diag(
    logger: logging.Logger,
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
    woofer_role: str,
    tweeter_role: str | None,
) -> None:
    woofer = _pilot_diag_fields(_pilot_by_role(analysis, woofer_role))
    # A 1-way main declares no upper driver: ``tweeter_*`` publishes absent.
    tweeter = _pilot_diag_fields(
        _pilot_by_role(analysis, tweeter_role) if tweeter_role else None
    )
    log_event(
        logger, "correction.crossover_v2_check_diag",
        session_id=session_id, accepted=verdict.accepted, code=verdict.code or "",
        pilot_snr_ok=analysis.pilot_snr_ok,
        woofer_snr_db=woofer["snr_db"],
        woofer_captured_delta_db=woofer["captured_delta_db"],
        woofer_programmed_delta_db=woofer["programmed_delta_db"],
        woofer_channel_map_target_rise_db=woofer["channel_map_target_rise_db"],
        woofer_channel_map_cross_rise_db=woofer["channel_map_cross_rise_db"],
        woofer_channel_map_isolation_db=woofer["channel_map_isolation_db"],
        tweeter_snr_db=tweeter["snr_db"],
        tweeter_captured_delta_db=tweeter["captured_delta_db"],
        tweeter_programmed_delta_db=tweeter["programmed_delta_db"],
        tweeter_channel_map_target_rise_db=tweeter["channel_map_target_rise_db"],
        tweeter_channel_map_cross_rise_db=tweeter["channel_map_cross_rise_db"],
        tweeter_channel_map_isolation_db=tweeter["channel_map_isolation_db"],
        # The two constants the isolation figures above are GRADED against, on the
        # same line as the numbers. The bound is what the ratio had to clear; the
        # threshold is the target rise ABOVE WHICH the ratio was judged at all.
        # Below the threshold an isolation figure decided nothing, so the bound
        # alone would let a sub-bound number read as the cause of a refusal that
        # never happened.
        channel_map_min_isolation_db=CHANNEL_MAP_MIN_ISOLATION_DB,
        channel_map_isolation_judged_above_db=(
            CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
        ),
    )
    _log_measure_level_solve(logger, analysis, session_id=session_id)


def _log_measure_level_solve(
    logger: logging.Logger, analysis: ProgramAnalysis, *, session_id: str,
) -> None:
    """One event per driver disclosing its solved MEASURE level (#1825).

    A per-ROLE record with its own evidence — the ambient band it was solved
    against and the SNR it demanded there — which flattening two roles × six
    fields into the already-wide CHECK diag line would bury. Emitted from the
    diagnostic path so it lands on a REJECTED check too: what level the solve
    WOULD have chosen is what a ``snr_floor`` refusal needs read beside it.
    """
    gain_plan = analysis.gain_plan
    if gain_plan is None:
        return
    for role, solve in (gain_plan.role_solves or {}).items():
        band = solve.band_hz
        log_event(
            logger, "correction.crossover_v2_measure_level_solve",
            session_id=session_id,
            role=role,
            solved_gain_db=round(float(solve.gain_db), 3),
            flat_target_gain_db=round(float(solve.flat_target_gain_db), 3),
            reduction_db=round(float(solve.reduction_db), 3),
            bound_by=solve.bound_by,
            band_lo_hz=round(band[0], 1) if band else None,
            band_hi_hz=round(band[1], 1) if band else None,
            ambient_dbfs=(
                round(float(solve.ambient_dbfs), 2)
                if solve.ambient_dbfs is not None else None
            ),
            required_snr_db=(
                round(float(solve.required_snr_db), 2)
                if solve.required_snr_db is not None else None
            ),
            required_capture_dbfs=(
                round(float(solve.required_capture_dbfs), 2)
                if solve.required_capture_dbfs is not None else None
            ),
            # #1838: without this the disclosed triple no longer adds up —
            # `required_capture_dbfs` is `ambient + required_snr + crest`.
            crest_factor_db=(
                round(float(solve.crest_factor_db), 2)
                if solve.crest_factor_db is not None else None
            ),
        )


def _log_measure_diag(
    logger: logging.Logger,
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
    roles: Sequence[str],
    sample_rate_hz: int,
    gate_window_ms: float | None,
    gate_floor_source: str | None,
    guard: str,
) -> None:
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    # A 1-way main declares no upper driver: every ``tweeter_*`` field below
    # publishes absent rather than repeating the woofer.
    woofer_role = roles[0]
    tweeter_role = roles[1] if len(roles) > 1 else None
    delay_us, delay_role, polarity = alignment_to_candidate_fields(
        analysis, roles=roles,
    )
    woofer_snr_db, woofer_snr_verdict, woofer_snr_band = _driver_snr_fields(
        _driver_response_by_role(analysis, woofer_role)
    )
    tweeter_snr_db, tweeter_snr_verdict, tweeter_snr_band = _driver_snr_fields(
        _driver_response_by_role(analysis, tweeter_role) if tweeter_role else None
    )
    sweep_residual_ms_worst, sweep_locate_confidence_min = _sweep_schedule_diag_fields(
        analysis, sample_rate_hz
    )
    # First-vs-last per-role epsilon (#1668) — diagnostic only, never gated
    # (DriftEstimate.per_role_epsilon_ppm). None-safe for a legacy construction
    # site predating the field, or a role with fewer than 2 located
    # occurrences.
    woofer_repeat_epsilon_ppm = (
        drift.per_role_epsilon_ppm.get(woofer_role) if drift else None
    )
    tweeter_repeat_epsilon_ppm = (
        drift.per_role_epsilon_ppm.get(tweeter_role) if drift and tweeter_role else None
    )
    log_event(
        logger, "correction.crossover_v2_measure_diag",
        session_id=session_id, accepted=verdict.accepted, code=verdict.code or "",
        alignment_confidence=round(float(align.confidence), 4) if align else None,
        alignment_confidence_source=(align.confidence_source if align else None),
        alignment_seed_delay_us=(
            round(float(align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        alignment_refinement_delta_us=(
            round(float(align.delay_us - align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        gate_window_ms=gate_window_ms,
        gate_floor_source=gate_floor_source,
        validity_floor_hz=_measure_validity_floor_hz(analysis),
        epsilon_ppm=round(float(drift.epsilon_ppm), 3) if drift else None,
        max_residual_samples=round(float(drift.max_residual_samples), 3) if drift else None,
        repeat_level_delta_db=(
            round(float(drift.repeat_level_delta_db), 3) if drift else None
        ),
        woofer_repeat_epsilon_ppm=(
            round(float(woofer_repeat_epsilon_ppm), 3)
            if woofer_repeat_epsilon_ppm is not None else None
        ),
        tweeter_repeat_epsilon_ppm=(
            round(float(tweeter_repeat_epsilon_ppm), 3)
            if tweeter_repeat_epsilon_ppm is not None else None
        ),
        delay_us=round(delay_us, 3) if delay_us is not None else None,
        delay_role=delay_role,
        polarity=polarity,
        # The (polarity, delay) pair is one selection on one objective (#2598).
        # ``polarity`` above is what shipped; these three say who chose it, what
        # the GCC correlation answered, and whether the two agreed. A disagreement
        # is ordinary operation, so this line is where it is legible rather than a
        # refusal.
        alignment_objective=(cand.alignment_objective if cand else None),
        seed_polarity=(
            None if cand is None or cand.seed_polarity_sign is None
            else polarity_label(int(cand.seed_polarity_sign))
        ),
        polarity_agrees_with_sum=(
            align.polarity_agrees_with_sum if align else None
        ),
        left_anchor_lobe=(bool(cand.left_anchor_lobe) if cand else None),
        predicted_ripple_db=(
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        # #1667: how far the RAW candidate's (ripple-optimal-where-trusted) tweeter
        # trim moved from solve_branch_trims's band-average seed — always the RAW
        # candidate's own recovery, even on a linearization-eligible attempt. The
        # sanity-guard fallback path reads exactly 0.0 (raw == seed); ``None`` only
        # when the candidate predates trim_band_average_db.
        trim_ripple_gain_db=(
            round(
                float(
                    cand.trim_db[tweeter_role]
                    - cand.trim_band_average_db[tweeter_role]
                ),
                4,
            )
            if cand and tweeter_role and cand.trim_band_average_db is not None else None
        ),
        # Disambiguates the 0.0 above, which is otherwise three rounds
        # wearing one face; see the field's own docstring.
        ripple_polish_rejected_delta_db=(
            round(float(cand.ripple_polish_rejected_delta_db), 4)
            if cand and cand.ripple_polish_rejected_delta_db is not None else None
        ),
        alignment_seed_ripple_db=(
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        flatness_improvement_db=(
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        anchor_delay_us=(
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        snap_delta_us=(
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        snap_found=(bool(cand.snap_found) if cand else None),
        woofer_snr_db=woofer_snr_db,
        woofer_snr_verdict=woofer_snr_verdict,
        woofer_snr_band=woofer_snr_band,
        tweeter_snr_db=tweeter_snr_db,
        tweeter_snr_verdict=tweeter_snr_verdict,
        tweeter_snr_band=tweeter_snr_band,
        sweep_residual_ms_worst=(
            round(sweep_residual_ms_worst, 3)
            if sweep_residual_ms_worst is not None else None
        ),
        sweep_locate_confidence_min=(
            round(sweep_locate_confidence_min, 4)
            if sweep_locate_confidence_min is not None else None
        ),
        # Which (if any) measurement-honesty gate fired this verdict — disambiguates
        # a G1/G2 fire from the pre-existing check that shares its reused reason
        # code.
        guard=guard,
        # The pilot SNR guard's own evidence (#1810). Live on this phase only since
        # the pre-pilot ambient window shipped, so a REASON_PILOT_LEVEL_COLLAPSE
        # line with numbers here is what distinguishes a real low-SNR capture from
        # the structurally-dead guard it replaced.
        pilot_snr_ok=analysis.pilot_snr_ok,
        pilot_snr_db=_worst_pilot_snr_db(analysis),
    )


def _log_verify_diag(
    logger: logging.Logger,
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
    verify_frame: Mapping[str, Any] | None,
    verify_claims: Mapping[str, Any] | None,
    verify_pilot_transfer_step_db: float | None,
    measure_gate_window_ms: float | None,
) -> None:
    integrity = analysis.capture_integrity
    tracking = analysis.verify_tracking or {}
    band = tracking.get("tracking_band_hz")
    tracking_band_lo_hz: float | None = None
    tracking_band_hi_hz: float | None = None
    if isinstance(band, (list, tuple)) and len(band) == 2:
        tracking_band_lo_hz, tracking_band_hi_hz = band[0], band[1]
    validity_floor_hz = (
        analysis.summed_response.validity_floor_hz
        if analysis.summed_response is not None else None
    )
    # Measurement-honesty gate diagnostics: the current
    # attempt's raw pilot transfer (re-derived fresh, read-only — never
    # the mutated session state) and the step vs baseline
    # ``_verify_verdict`` already computed and stashed transiently.
    pilot_transfer_db = _pilot_transfer_by_role(analysis).get(VERIFY_PILOT_ROLE)
    # Frame discipline (rung P1): the journal line an operator greps for "did
    # apply do what we predicted" is also where "84 % of that was the
    # instrument" has to be readable. Lifted, never recomputed.
    frame = verify_frame or {}
    claims = verify_claims or {}
    absolute = claims.get("absolute") or {}
    log_event(
        logger, "correction.crossover_v2_verify_diag",
        session_id=session_id, accepted=verdict.accepted, code=verdict.code or "",
        max_db_notch_excluded=tracking.get("max_db_notch_excluded"),
        verify_tolerance_db=VERIFY_TOLERANCE_DB,
        verify_gate_window_ms=_gate_window_ms(analysis.summed_response),
        verify_gate_floor_source=_gate_floor_source(analysis.summed_response),
        # No ``measure_gate_floor_source`` beside ``measure_gate_window_ms`` here on
        # purpose: that window is RESTORED from persisted state on a resumed
        # session and the floor source is not persisted, so the pair could only be
        # reported as a real window beside a null source. MEASURE's own source is
        # disclosed where it is computed.
        measure_gate_window_ms=measure_gate_window_ms,
        validity_floor_hz=validity_floor_hz,
        tracking_band_lo_hz=tracking_band_lo_hz,
        tracking_band_hi_hz=tracking_band_hi_hz,
        rms_db=tracking.get("rms_db"),
        # The frame those two numbers were measured ACROSS, and the same
        # two numbers with its tilt removed — beside, never instead of.
        frame_offset_db=_rounded(frame.get("offset_db"), 3),
        frame_tilt_db_per_octave=_rounded(frame.get("tilt_db_per_octave"), 3),
        rms_db_tilt_removed=_rounded(frame.get("rms_db_tilt_removed"), 4),
        max_db_tilt_removed=_rounded(frame.get("max_db_tilt_removed"), 4),
        # §7's claims, on the SAME line an operator already greps for a verify
        # outcome (R18, #1868) — including the two that are structurally
        # not-evaluated. The absolute scalars ride beside it because a band and a
        # verdict without the number are not a measurement. All lifted.
        claims=_claims_log_field(claims),
        absolute_worst_db=absolute.get("worst_db"),
        absolute_worst_hz=absolute.get("worst_hz"),
        absolute_max_db=absolute.get("max_db"),
        absolute_tolerance_db=absolute.get("tolerance_db"),
        absolute_band_lo_hz=_band_edge(absolute.get("band_hz"), 0),
        absolute_band_hi_hz=_band_edge(absolute.get("band_hz"), 1),
        pilot_transfer_db=(
            round(pilot_transfer_db, 3) if pilot_transfer_db is not None else None
        ),
        pilot_transfer_step_db=(
            round(verify_pilot_transfer_step_db, 3)
            if verify_pilot_transfer_step_db is not None else None
        ),
        # Issue #1810 — see ``_log_measure_diag``'s note. Read alongside
        # ``pilot_transfer_step_db``: a null step next to an agc_behavioral_fail is
        # the combination these two fields together make legible.
        pilot_snr_ok=analysis.pilot_snr_ok,
        pilot_snr_db=_worst_pilot_snr_db(analysis),
        # Capture integrity (#1971), disclosed on EVERY verify — pass or fail. On a
        # refusal it names which check fired, which is what tells telemetry a
        # ``locate_failed`` came from this gate rather than from
        # ``_stimulus_locate_ok``. The two scalars are the measured figures the
        # verdict was drawn from, and are reported even where the check they feed
        # was ``not_evaluated``.
        integrity=_capture_integrity_log_field(integrity),
        integrity_not_evaluated=(
            ",".join(integrity.not_evaluated) if integrity is not None else ""
        ),
        integrity_locate_confidence_min=_rounded(
            integrity.locate_confidence_min if integrity is not None else None, 4
        ),
        integrity_residual_ms_worst=_rounded(
            integrity.schedule_residual_ms_worst if integrity is not None else None,
            3,
        ),
        guard=(
            "pilot_level_shift" if verdict.code == REASON_VERIFY_LEVEL_SHIFT else ""
        ),
    )


def _log_cloud_diag(
    logger: logging.Logger,
    phase: str,
    index: int,
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
    positions_in: int,
) -> None:
    response = analysis.summed_response
    log_event(
        logger, "correction.crossover_v2_cloud_diag",
        session_id=session_id, phase=phase, index=index,
        accepted=verdict.accepted, code=verdict.code or "",
        positions_in=positions_in,
        gate_window_ms=_gate_window_ms(response),
        gate_floor_source=_gate_floor_source(response),
        validity_floor_hz=getattr(response, "validity_floor_hz", None),
        summed_ripple_db=analysis.summed_ripple_db,
        linearity_ok=analysis.linearity_ok,
        # Issue #1810 — see ``_log_measure_diag``'s note.
        pilot_snr_ok=analysis.pilot_snr_ok,
        pilot_snr_db=_worst_pilot_snr_db(analysis),
        glitch=analysis.glitch_detected,
    )


def _log_entry_baseline_diag(
    logger: logging.Logger,
    index: int,
    analysis: ProgramAnalysis,
    verdict: PhaseVerdict,
    *,
    session_id: str,
    baseline: Any | None,
) -> None:
    response = analysis.summed_response
    log_event(
        logger, "correction.crossover_v2_entry_baseline_diag",
        session_id=session_id, index=index,
        accepted=verdict.accepted, code=verdict.code or "",
        program_id=(baseline.program_id if baseline is not None else ""),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
        graph_fingerprint=(
            baseline.graph_fingerprint if baseline is not None else ""
        ),
        artifact_ref=(baseline.artifact_ref if baseline is not None else ""),
        gate_window_ms=_gate_window_ms(response),
        validity_floor_hz=getattr(response, "validity_floor_hz", None),
        summed_ripple_db=analysis.summed_ripple_db,
        linearity_ok=analysis.linearity_ok,
        pilot_snr_ok=analysis.pilot_snr_ok,
        glitch=analysis.glitch_detected,
    )
