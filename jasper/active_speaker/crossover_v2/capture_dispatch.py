# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-take recording integrity, capabilities and the next capture action."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, Mapping

from jasper.audio_measurement.program import KIND_PILOT, KIND_SWEEP, STIMULUS_KINDS
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    INTEGRITY_CHECK_SWEEP_HEARD,
)
from jasper.audio_measurement.program_analysis.check import alignment_snr_gain_adjustment
from jasper.audio_measurement.program_analysis.model import (
    DRIVER_SNR_ALIGNMENT_KEY, GAIN_MAX_DIGITAL_PEAK_DBFS, PILOT_MIN_SNR_DB,
    SWEEP_LOCATE_CONFIDENCE_FLOOR, SWEEP_SCHEDULE_RESIDUAL_CEILING_MS, MeasurementPriors, ProgramAnalysis,
)
from jasper.audio_measurement.program_analysis.summary import driver_alignment_snr_verdict, driver_snr_verdict
from jasper.active_speaker.profile import spl_raise_bound_db_spl
from jasper.active_speaker.program_failure import read_output_volume
from .sweep_spec import REQUIRED_SAMPLE_RATE_HZ
from jasper.json_fields import finite_float

from . import refusal_copy as reasons
from .refusal_copy import TakeCharge, TakeNext, TakeVerdict as TakeVerdict
from .programs import back_off_gain

if TYPE_CHECKING:
    from jasper.audio_measurement.program import ExcitationProgram

# Clip retries lower stimulus gain, never the admitted hardware ceiling.
SAME_POSE_DRIFT_DB = 2.0
ACROSS_POSE_DRIFT_DB = 6.0
CLIP_RETRY_BACKOFF_DB = 3.0
# dB, recorder transfer stability; see ADR-0182.
VERIFY_PILOT_TRANSFER_STEP_CEILING_DB = 0.35


def capped_gain_ceilings(
    caps_dbfs: Mapping[str, float], session_volume_db: float, ceilings: Mapping[str, float],
) -> dict[str, float]:
    return {role: back_off_gain(ceiling, session_volume_db, cap)
            for role, ceiling in ceilings.items() if (cap := caps_dbfs.get(role)) is not None}


def level_drift_verdict(
    *, loudest_half_second_db_spl: float | None, level_reference_db_spl: float | None, same_pose: bool,
) -> TakeVerdict:
    delta = (loudest_half_second_db_spl - level_reference_db_spl
             if loudest_half_second_db_spl is not None and level_reference_db_spl is not None else None)
    drifted = delta is not None and abs(delta) > (SAME_POSE_DRIFT_DB if same_pose else ACROSS_POSE_DRIFT_DB)
    return TakeVerdict(not drifted, fault=reasons.REASON_LEVEL_DRIFT_AT_SESSION_GAIN if drifted else None,
                       next="retake_same" if drifted else "accept", charge="none",
                       evidence={key: value for key, value in
                                 (("loudest_half_second_db_spl", loudest_half_second_db_spl), ("level_delta_db", delta))
                                 if value is not None})


def pilot_screens(analysis: ProgramAnalysis, *, program: ExcitationProgram | None = None) -> list[dict[str, Any]]:
    if analysis.pilot_snr_ok is not False:
        return []
    bands = {segment.role: [segment.f1_hz, segment.f2_hz] for segment in program.segments
             if segment.kind == KIND_PILOT} if program else {}
    return [{"code": reasons.REASON_PILOT_LEVEL_COLLAPSE, "blocking": True,
             "evidence": {"pilot_snr_ok": False, "pilot_ambient": analysis.pilot_ambient, "required_snr_db": PILOT_MIN_SNR_DB,
                          "ambient_report": analysis.ambient_report,
                          "pilots": [{**asdict(pilot), "snr_db": finite_float(pilot.snr_db),
                                      "band_hz": bands.get(pilot.role)} for pilot in analysis.pilots]}}]


def assess(
    analysis: ProgramAnalysis, *, level_verdict: TakeVerdict | None = None,
    prior_verdict: TakeVerdict | None = None, **kwargs: Any,
) -> TakeVerdict:
    verdict = prior_verdict if prior_verdict is not None else _assess_recording(analysis, **kwargs)
    # Removal condition: see the output mute guard in preflight.py.
    if prior_verdict is None and verdict.fault == reasons.REASON_LOCATE_FAILED:
        output_volume = read_output_volume()
        if output_volume.get("muted") is True:
            verdict = replace(verdict, fault=reasons.REASON_MEASUREMENT_OUTPUT_MUTED,
                              next="stop", charge="none", next_gain_db=None,
                              evidence={**verdict.evidence, **output_volume})
    verdict = replace(verdict, screens=[] if verdict.fault == reasons.REASON_MEASUREMENT_OUTPUT_MUTED
                      else pilot_screens(analysis, program=kwargs.get("program")))
    if level_verdict is None:
        return verdict
    verdict = replace(verdict, evidence={**verdict.evidence, **level_verdict.evidence})
    if verdict.ok and verdict.next == "accept" and not level_verdict.ok:
        return replace(verdict, ok=False, fault=level_verdict.fault, next=level_verdict.next,
                       charge=level_verdict.charge, capabilities={key: False for key in verdict.capabilities})
    return verdict


def _assess_recording(
    analysis: ProgramAnalysis, *, phase: str,
    priors: MeasurementPriors | None = None,
    program: ExcitationProgram | None = None,
    gain_db: Mapping[str, float] | None = None,
    gain_ceiling_db: Mapping[str, float] | None = None,
    caps_dbfs: Mapping[str, float] | None = None,
    session_volume_db: float = 0.0,
    spl_stop_db_spl: float | None = None,
    spl: Mapping[str, Any] | None = None,
    pilot_transfer_prior: Mapping[str, float] | None = None,
    measure_gate_window_ms: float | None = None,
) -> TakeVerdict:
    if phase not in {"check", "measure", "verify"}:
        raise ValueError(f"unsupported assessment phase: {phase}")
    priors = priors or MeasurementPriors()
    gains = dict(gain_db) if gain_db is not None else {
        seg.role or "summed": seg.gain_db for seg in program.segments
        if seg.kind in STIMULUS_KINDS
    } if program is not None else {}
    ceilings = gain_ceiling_db or {}
    if caps_dbfs is not None:
        ceilings = capped_gain_ceilings(caps_dbfs, session_volume_db, ceilings)
    anchor, drift, alignment = analysis.anchor, analysis.drift, analysis.alignment
    sample_rate = program.sample_rate_hz if program else REQUIRED_SAMPLE_RATE_HZ
    schedule_residual_ms, sweep_confidence_min = _sweep_schedule_diag_fields(analysis, sample_rate)
    stimuli = []
    locate_confidences: dict[str, float] = {}
    for loc in analysis.locations:
        if loc.kind in STIMULUS_KINDS:
            stimuli.append(loc)
        if loc.kind == KIND_SWEEP:
            key = f"locate_confidence.{loc.role or 'summed'}"
            locate_confidences[key] = min(locate_confidences.get(key, loc.confidence), loc.confidence)
    evidence: dict[str, float | bool | str] = {
        "mic_meter_status": analysis.mic_meter_status or "unmeasured",
        "pilot_ambient": analysis.pilot_ambient,
        "anchor_ambiguous": analysis.anchor_ambiguous or bool(anchor and anchor.ambiguous),
        "glitch_detected": bool(analysis.glitch_detected),
        "frame_loss": bool(analysis.frame_ledger and analysis.frame_ledger.lost_at),
        "glitch_inputs": ",".join(drift.glitch_inputs) if drift else "",
    }
    figures = {
        "anchor_presence": anchor.presence if anchor else None,
        "anchor_confidence": anchor.confidence if anchor else None,
        "anchor_runner_up_presence": anchor.runner_up_presence if anchor else None,
        "anchor_runner_up_confidence": anchor.runner_up_confidence if anchor else None,
        "anchor_witnesses_tried": anchor.witnesses_tried if anchor else None,
        "anchor_corroborated": anchor.corroborated if anchor else None,
        "anchor_shift_ms": anchor.shift_ms if anchor else None,
        "anchor_witness_residual_ms": anchor.witness_residual_ms if anchor else None,
        "epsilon_ppm": drift.epsilon_ppm if drift else None,
        "max_residual_samples": drift.max_residual_samples if drift else None,
        "schedule_residual_ms_worst": schedule_residual_ms,
        "repeat_level_delta_db": drift.repeat_level_delta_db if drift else None,
        "discontinuity_samples": analysis.discontinuity_samples,
        "peak_dbfs": max((loc.peak_dbfs for loc in stimuli), default=None),
        "locate_confidence_min": min((loc.confidence for loc in stimuli), default=None),
        **locate_confidences,
    }
    evidence.update({key: value if isinstance(value, bool) else float(value)
                     for key, value in figures.items() if value is not None})
    if anchor is not None and anchor.anchor is not None:
        evidence.update(anchor=anchor.anchor, anchor_witness=anchor.witness or "")
    responses = (*analysis.driver_responses, *((analysis.summed_response,) if analysis.summed_response else ()))
    capabilities = {
        "magnitude": bool(responses),
        "mic_level": analysis.mic_meter_status not in {None, "unmeasured"},
        "delay_estimate": alignment is not None and alignment.status == ALIGNMENT_OK,
        "level_solve": analysis.gain_plan is not None and analysis.gain_plan.snr_floor_ok,
    }
    for response in responses:
        for decision, snr_verdict in (("magnitude", driver_snr_verdict(response)),
                                      (DRIVER_SNR_ALIGNMENT_KEY, driver_alignment_snr_verdict(response))):
            block = (response.snr or {}) if decision == "magnitude" else (response.snr or {}).get(DRIVER_SNR_ALIGNMENT_KEY, {})
            worst = block.get("worst_relevant") or {}
            band: Mapping[str, Any] = next((row for row in block.get("bands", ())
                         if row.get("band_id") == worst.get("band_id")), {})
            prefix = f"snr.{response.role}.{decision}"
            if worst.get("band_id") is not None:
                evidence[f"{prefix}.band_id"] = str(worst["band_id"])
            for key in ("estimated_snr_db", "shortfall_db"):
                value = finite_float(band.get(key))
                if value is not None:
                    evidence[f"{prefix}.{key}"] = value
            evidence[f"{prefix}.verdict"] = snr_verdict or "unknown"
            if snr_verdict == "insufficient":
                capabilities["delay_estimate" if decision == "alignment" else "magnitude"] = False
    if alignment is not None:
        evidence.update(alignment_status=alignment.status, delay_us=float(alignment.delay_us))
        bounds = priors.alignment_delay_bounds_us
        plausible = bounds is None or bounds[0] <= abs(alignment.delay_us) <= bounds[1]
        evidence["delay_physically_plausible"] = plausible
        capabilities["delay_estimate"] &= plausible

    verdict = TakeVerdict(True, evidence=evidence, capabilities=capabilities)

    def program_peak(targets: Mapping[str, float]) -> float | None:
        return max({**gains, **targets}.values()) if targets else None

    def refuse(code: str, *, next: TakeNext = "fix_and_retake", charge: TakeCharge = "operator",
               targets: Mapping[str, float] | None = None, ok: bool = False) -> TakeVerdict:
        targets = targets or {}
        return replace(verdict, ok=ok, fault=code, next=next, charge=charge,
                       next_gain_db=program_peak(targets),
                       evidence={**evidence, **{f"next_gain_db.{role}": float(gain)
                                               for role, gain in targets.items()}},
                       capabilities=capabilities if ok else {key: False for key in capabilities})

    def quiet(code: str, *, charge: TakeCharge = "operator") -> TakeVerdict:
        adjusted, levels = alignment_snr_gain_adjustment(analysis.driver_responses, gains, ceilings)
        evidence.update(levels)
        return refuse(code, next="retake_louder" if adjusted else "fix_and_retake",
                      charge="speaker" if adjusted else charge, targets=adjusted)

    if analysis.frame_ledger and analysis.frame_ledger.capture_gap_frames:
        return refuse(reasons.REASON_CAPTURE_OVERRUN, next="retake_same", charge="speaker")
    if evidence["frame_loss"]:
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge="speaker")
    if not _stimulus_locate_ok(analysis):
        return quiet(reasons.REASON_LOCATE_FAILED)
    if evidence["anchor_ambiguous"]:
        return refuse(reasons.REASON_ANCHOR_AMBIGUOUS)
    if analysis.delta_implausible:
        return (refuse(reasons.REASON_PILOT_STEP_IMPLAUSIBLE) if analysis.pilot_snr_ok is True
                else quiet(reasons.REASON_SNR_FLOOR))
    if phase == "check" and analysis.channel_map_ok is False:
        return refuse(reasons.REASON_CHANNEL_MAP_MISMATCH, next="stop", charge="none", ok=True)
    if analysis.pilot_snr_ok is False:
        return quiet(reasons.REASON_SNR_FLOOR if phase == "check" else reasons.REASON_PILOT_LEVEL_COLLAPSE)
    # Retire when locate can resolve the timeline without a corroborating witness.
    if anchor is not None and anchor.corroborated is False:
        return quiet(reasons.REASON_ANCHOR_TOO_QUIET, charge="speaker" if gains else "operator")
    schedule_ok = _sweep_schedule_ok(analysis, sample_rate)
    if not schedule_ok and sweep_confidence_min is not None and sweep_confidence_min < SWEEP_LOCATE_CONFIDENCE_FLOOR:
        return quiet(reasons.REASON_LOCATE_FAILED)
    integrity = analysis.capture_integrity
    if integrity is not None and INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
        return quiet(reasons.REASON_LOCATE_FAILED)
    if _any_sweep_clipped(analysis):
        targets = {role: gain - CLIP_RETRY_BACKOFF_DB for role, gain in gains.items()}
        return refuse(reasons.REASON_CLIPPED, next="retake_quieter", charge="speaker", targets=targets)
    if analysis.glitch_detected or (analysis.discontinuity_samples or 0) != 0 or (integrity and integrity.failed):
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge="speaker")
    if not schedule_ok:
        evidence["guard"] = "sweep_schedule"
        return refuse(reasons.REASON_DRIFT_BASELINES_DISAGREE, next="retake_same", charge="speaker")
    if analysis.linearity_ok is False:
        code = (reasons.REASON_NOISY_ROOM_LINEARITY if phase == "check" and analysis.gain_plan
                and not analysis.gain_plan.snr_floor_ok else reasons.REASON_AGC_BEHAVIORAL_FAIL)
        return refuse(code)
    if phase == "check" and not capabilities["level_solve"]:
        return quiet(reasons.REASON_SNR_FLOOR)
    verify_gate = _gate_window_ms(analysis.summed_response)
    # A shorter VERIFY gate manufactures overlay differences (§5.2).
    if (phase == "verify" and measure_gate_window_ms is not None and verify_gate is not None
            and verify_gate + 1e-6 < measure_gate_window_ms):
        evidence.update(measure_gate_window_ms=measure_gate_window_ms, verify_gate_window_ms=verify_gate)
        return refuse(reasons.REASON_VERIFY_INCONCLUSIVE, ok=True)
    if phase == "verify" and pilot_transfer_prior:
        transfer = _pilot_transfer_by_role(analysis)
        step = max((abs(value - pilot_transfer_prior[role]) for role, value in transfer.items()
                    if role in pilot_transfer_prior), default=None)
        if step is not None:
            evidence["pilot_transfer_step_db"] = float(step)
            if step > VERIFY_PILOT_TRANSFER_STEP_CEILING_DB:
                return refuse(reasons.REASON_VERIFY_LEVEL_SHIFT, ok=True)
    spl = spl or {}
    peak_spl = finite_float(spl.get("max_window_db_spl"))
    stop = finite_float(spl.get("ceiling_db_spl"))
    headroom = (max(0.0, spl_raise_bound_db_spl(spl_stop_db_spl, measured_stop_db_spl=stop) - peak_spl)
                if peak_spl is not None and stop is not None and spl_stop_db_spl is not None else None)
    alignment_only = phase == "measure" and capabilities["magnitude"]
    alignment_ceiling_db = (capped_gain_ceilings(caps_dbfs, session_volume_db,
                            dict.fromkeys(caps_dbfs, GAIN_MAX_DIGITAL_PEAK_DBFS))
                            if alignment_only and headroom is not None and caps_dbfs is not None else None)
    adjusted, levels = alignment_snr_gain_adjustment(
        analysis.driver_responses, gains, ceilings,
        alignment_ceiling_db=alignment_ceiling_db,
        alignment_limit_reason=("spl_unobserved" if headroom is None else "ceiling_unavailable")
                               if alignment_only and alignment_ceiling_db is None else None,
        max_raise_db=headroom or 0.0,
    )
    evidence.update(levels)
    if adjusted:
        return replace(verdict, next="retake_louder", next_gain_db=program_peak(adjusted), charge="speaker",
                       evidence={**evidence, **{f"next_gain_db.{role}": gain for role, gain in adjusted.items()}})
    return verdict


def _clipped_stimulus_peaks(analysis: ProgramAnalysis) -> list[float]:
    return [float(loc.peak_dbfs) for loc in analysis.locations
            if loc.kind in STIMULUS_KINDS and loc.clipped]


def _any_sweep_clipped(analysis: ProgramAnalysis) -> bool:
    return bool(_clipped_stimulus_peaks(analysis))


def _unanchored_sweep_roles(analysis: ProgramAnalysis) -> frozenset[str | None]:
    """Sweep roles a branch program's leading pilot pair does NOT anchor.

    Empty for every other program, and whenever no pilot named a role at all,
    which keeps both rungs below at their shared thresholds rather than
    inventing an exemption.
    """
    anchored = {pilot.role for pilot in analysis.pilots}
    if analysis.branch_diagnostic is None or not anchored:
        return frozenset()
    return frozenset(loc.role for loc in analysis.locations
                     if loc.kind == KIND_SWEEP and loc.role not in anchored)


def _sweep_schedule_ok(analysis: ProgramAnalysis, sample_rate_hz: int) -> bool:
    """Check sweep timing against the schedule, or the first sweep for an unanchored role."""
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return True
    unanchored = _unanchored_sweep_roles(analysis)
    reference: dict[str | None, float] = {}
    for loc in sweeps:
        if loc.role in unanchored:
            reference.setdefault(loc.role, loc.residual_samples)
    for loc in sweeps:
        residual_ms = abs(loc.residual_samples - reference.get(loc.role, 0.0)) / sample_rate_hz * 1000.0
        if residual_ms > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS:
            return False
    return True


def _sweep_schedule_diag_fields(
    analysis: ProgramAnalysis, sample_rate_hz: int,
) -> tuple[float | None, float | None]:
    """``(sweep_residual_ms_worst, sweep_locate_confidence_min)`` — diagnostic
    only, over the ``KIND_SWEEP`` domain the schedule check uses, and never
    itself a verdict. ``sweep_residual_ms_worst`` is the SIGNED residual (not its
    magnitude) of whichever sweep has the largest absolute residual, so a
    reviewer sees which direction the schedule broke. ``(None, None)`` when there
    are no sweeps to judge.
    """
    sweeps = [loc for loc in analysis.locations if loc.kind == KIND_SWEEP]
    if not sweeps:
        return None, None
    worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
    residual_ms_worst = worst.residual_samples / sample_rate_hz * 1000.0
    confidence_min = min(loc.confidence for loc in sweeps)
    return residual_ms_worst, confidence_min


def _gate_window_ms(response: Any) -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def _pilot_transfer_by_role(analysis: ProgramAnalysis) -> dict[str, float]:
    """Per-role pilot transfer: captured hi level minus the programmed hi gain.

    The measurement-honesty raw material: VERIFY replays the identical
    program through the identical applied graph on every attempt, so this
    transfer should not move between attempts either. Excludes any pilot whose
    ``programmed_hi_gain_db`` is unset — there is nothing to compare it against.

    ``PilotObservation`` warns that ``level_hi_dbfs`` must never feed an
    ABSOLUTE-level consumer, because ambient subtraction shifts it. This use is
    safe for two independent reasons. (1) It is a RELATIVE cross-ATTEMPT
    comparison, never a true absolute-level read. (2) The confound is bounded far
    below the gate: ``_assess_recording`` refuses any attempt whose ``pilot_snr_ok``
    is False before reaching G3, so every attempt here cleared
    ``PILOT_MIN_SNR_DB`` (≈12.4 dB) on the QUIET pilot and the HI pilot sits a
    further ``PILOT_LEVEL_DELTA_DB`` (10 dB) above, i.e. ≥22.4 dB in-band SNR. At
    that SNR the subtraction moves ``level_hi_dbfs`` by at most
    ``10·log10(1 − 10**−2.24)`` ≈ 0.025 dB, so two admissible attempts differ by
    at most ~0.05 dB from this term — an order of magnitude under
    :data:`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` (0.35 dB). Lowering that
    ceiling toward ~0.1 dB, or trusting ``PILOT_AMBIENT_WINDOW_S`` without the
    SNR gate in front of it, is what would put this back in play.
    """
    return {
        pilot.role: pilot.level_hi_dbfs - pilot.programmed_hi_gain_db
        for pilot in analysis.pilots
        if pilot.programmed_hi_gain_db is not None
    }


# --------------------------------------------------------------------------- #
# the locate-confidence screen
# --------------------------------------------------------------------------- #

# A located stimulus below this correlation confidence reads as "couldn't hear
# the speaker" (locate_failed).
LOCATE_MIN_CONFIDENCE = 0.1


def _stimulus_locate_ok(analysis: ProgramAnalysis) -> bool:
    """False when any ROLE's stimuli all failed the locate-confidence floor.

    Per ROLE, not per SEGMENT, and not a max() over the whole capture (D8,
    #1838). A max() over every segment is effectively no floor at all on a
    multi-driver program: one clearly-located segment anywhere cleared the gate,
    so a capture in which an entire driver was inaudible passed. Per-SEGMENT
    would be too strict the other way — a two-level pilot pair's quiet side sits
    10 dB under its loud side and locates more coarsely. One confidently-located
    stimulus says "this driver was heard"; zero does not. Role-less stimuli (a
    summed sweep) group together under the same rule.
    """
    by_role: dict[str | None, float] = {}
    for loc in analysis.locations:
        if loc.kind not in STIMULUS_KINDS:
            continue
        best = by_role.get(loc.role)
        if best is None or loc.confidence > best:
            by_role[loc.role] = loc.confidence
    if not by_role:
        return False
    return all(best >= LOCATE_MIN_CONFIDENCE for best in by_role.values())
