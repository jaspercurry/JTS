# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-take recording integrity, capabilities and the next capture action."""

from __future__ import annotations

import logging
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple

from jasper.audio_measurement.program import (
    KIND_PILOT, KIND_SUMMED_SWEEP, KIND_SWEEP, STIMULUS_KINDS, is_level_probe,
)
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
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.level import LevelReading, solve_gain
from jasper.audio_measurement.ramp import MAX_STEP_DB
from jasper.audio_measurement.wired_capture import WIRED_POST_ROLL_S
from jasper.active_speaker.capture_provenance import stimulus_peak_dbfs
from jasper.active_speaker.profile import spl_raise_bound_db_spl
from jasper.platform.control_client import read_output_volume
from .sweep_spec import REQUIRED_SAMPLE_RATE_HZ
from jasper.platform.json_fields import finite_float
from jasper.platform.log_event import log_event

from . import refusal_copy as reasons
from .refusal_copy import TakeCharge, TakeNext, TakeVerdict as TakeVerdict
from .programs import back_off_gain

if TYPE_CHECKING:
    from jasper.active_speaker.measurement_programs import PoseLevel
    from jasper.audio_measurement.program import ExcitationProgram

# Clip retries lower stimulus gain, never the admitted hardware ceiling.
SAME_POSE_DRIFT_DB = 2.0
ACROSS_POSE_DRIFT_DB = 6.0
CLIP_RETRY_BACKOFF_DB = 3.0
#: dB a VERIFY sweep's impulse must clear its take's ambient floor by; real jts3 takes
#: read 9.1 dB and up (replay: #5672).
SWEEP_OVER_AMBIENT_MIN_DB = 6.0


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


class _LevelTarget(NamedTuple):
    """A play's highest step's reading in dB SPL and its loudest reading, against
    its pose's target, and the peak it played at."""

    reading: LevelReading
    loudest: LevelReading
    target_db_spl: float
    peak_dbfs: float
    rule: PoseLevel

    @property
    def gap_db(self) -> float:
        return self.target_db_spl - self.reading.level_db


def _stop_gain(analysis: ProgramAnalysis, program: ExcitationProgram) -> float | None:
    """The gain of the burst a stopped probe was playing as it stopped: the last to
    start before its capture's post-roll. ``None`` without a frame count (ADR-0411)."""
    if analysis.frame_ledger is None:
        return None
    stopped = analysis.frame_ledger.received_frames - WIRED_POST_ROLL_S * program.sample_rate_hz
    started = [(location.scheduled_start, program.segment(location.segment_id).gain_db)
               for location in analysis.locations
               if location.kind in (KIND_SWEEP, KIND_SUMMED_SWEEP) and location.scheduled_start <= stopped]
    return max(started)[1] if started else None


def _level_target(analysis: ProgramAnalysis, spl: Mapping[str, Any] | None,
                  program: ExcitationProgram | None, rule: PoseLevel) -> _LevelTarget | None:
    """The play's highest located sweep in dB SPL and its loudest one (ADR-0364,
    ADR-0411), held to its pose's target, never above the admission bound under its
    own stop (ADR-0361). A stopped probe leaves out the burst its stop cut short,
    unless it is the only one (ADR-0365, ADR-0411)."""
    spl = spl or {}
    sens_factor_db = finite_float(spl.get("sens_factor_db"))
    stop = finite_float(spl.get("ceiling_db_spl"))
    peak = stimulus_peak_dbfs(program) if program is not None else None
    if not analysis.stimulus_levels or sens_factor_db is None or stop is None or program is None or peak is None:
        return None
    to_spl = MicSensitivity(sens_factor_db).db_spl_from_dbfs
    readings = [replace(reading, level_db=to_spl(reading.level_db),
                        floor_db=None if reading.floor_db is None else to_spl(reading.floor_db))
                for reading in sorted(analysis.stimulus_levels, key=lambda reading: reading.gain_db)]
    if (spl.get("stopped_at_db_spl") is not None and len(readings) > 1
            and _stop_gain(analysis, program) in (None, readings[-1].gain_db)):
        readings.pop()
    return _LevelTarget(readings[-1], max(readings, key=lambda reading: reading.level_db),
                        min(rule.target_db_spl, spl_raise_bound_db_spl(stop) - rule.tolerance_db),
                        peak, rule)


def _level_retake(level: _LevelTarget, *, probe: bool) -> TakeVerdict:
    """A retake at the gain that lands ``level`` just under its target (ADR-0364),
    never more than one probe step over the gain its loudest reading solves; the
    evidence names that reading when it sets the gain (ADR-0411). A probe's evidence
    names how far its take's ceiling holds it under that gain (ADR-0365). A probe is
    the take's own level step, never a failure: it names no fault, and the play that
    follows it is free."""
    def solve(reading: LevelReading) -> float:
        return solve_gain(reading, target_db=level.target_db_spl,
                          tolerance_db=level.rule.tolerance_db, max_raise_db=level.rule.max_raise_db)
    solved = solve(level.reading)
    evidence: dict[str, float | bool | str] = {}
    if (bound := solve(level.loudest) + MAX_STEP_DB) < solved:
        solved = bound
        evidence.update(level_bound_gain_db=level.loudest.gain_db, level_bound_db_spl=level.loudest.level_db)
    if probe and (shortfall := solved - level.peak_dbfs) > 0:
        evidence["level_shortfall_db"] = shortfall
    return TakeVerdict(False, fault=None if probe else reasons.REASON_LEVEL_OFF_TARGET,
                       next="retake_louder" if level.gap_db > 0 else "retake_quieter",
                       charge="replay" if probe else "speaker", next_gain_db=solved, evidence=evidence)


def _pilots_heard(analysis: ProgramAnalysis) -> bool | None:
    """The pilots cleared the room: over their SNR floor, or with a step read
    inside tolerance, which ``linearity_ok`` judges only from
    ``PILOT_STEP_MIN_SNR_DB`` up (#6113). ``None`` without pilot SNR evidence."""
    if analysis.pilot_snr_ok is None:
        return None
    return analysis.pilot_snr_ok or analysis.linearity_ok is True


def _frames_lost(analysis: ProgramAnalysis) -> bool:
    return bool(analysis.frame_ledger and analysis.frame_ledger.lost_at)


def pilot_screens(analysis: ProgramAnalysis, *, program: ExcitationProgram | None = None) -> list[dict[str, Any]]:
    if _pilots_heard(analysis) is not False:
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
    pose_level: PoseLevel | None = None, level_asked_dbfs: float | None = None,
    prior_verdict: TakeVerdict | None = None, **kwargs: Any,
) -> TakeVerdict:
    program = kwargs.get("program")
    probe = program is not None and is_level_probe(program)
    level = _level_target(analysis, kwargs.get("spl"), program, pose_level) if pose_level is not None else None
    # A take the microphone heard is levelled before its recording is judged; one it did
    # not hear is judged, never levelled blind; one its ceiling held under the peak it
    # asked for is kept too quiet, since a louder retake would replay it (ADR-0361). A
    # level probe is never kept: with no reading it trusts, it asks for the microphone
    # again (ADR-0365), unless its SPL watch did not stop it: it then played every burst up
    # to its take's ceiling, so no more level is available and the run stops (ADR-0422).
    # A probe that lost frames is judged first: the loss can cut a burst's loudest period.
    capped = (level is not None and level.gap_db > 0 and level_asked_dbfs is not None
              and level.peak_dbfs < level_asked_dbfs)
    if prior_verdict is None and _stimulus_locate_ok(analysis, program) and not (probe and _frames_lost(analysis)):
        if probe and (level is None or not level.reading.trusted):
            prior_verdict = TakeVerdict(False, fault=reasons.REASON_SNR_FLOOR, next="fix_and_retake", charge="operator")
        elif level is not None and (probe or (abs(level.gap_db) > level.rule.tolerance_db and not capped)):
            prior_verdict = _level_retake(level, probe=probe)
    verdict = prior_verdict if prior_verdict is not None else _assess_recording(analysis, **kwargs)
    # Removal condition: see the output mute guard in preflight.py.
    if prior_verdict is None and verdict.fault == reasons.REASON_LOCATE_FAILED:
        output_volume = read_output_volume()
        if output_volume.get("muted") is True:
            log_event(logging.getLogger(__name__), "active_speaker.measurement_output_muted", fields=output_volume)
            verdict = replace(verdict, fault=reasons.REASON_MEASUREMENT_OUTPUT_MUTED,
                              next="stop", charge="none", next_gain_db=None,
                              evidence={**verdict.evidence, **output_volume})
    if (probe and verdict.fault in (reasons.REASON_SNR_FLOOR, reasons.REASON_LOCATE_FAILED)
            and (kwargs.get("spl") or {}).get("stopped_at_db_spl") is None):
        verdict = replace(verdict, fault="level_unreachable", next="stop", charge="none")
    verdict = replace(verdict, screens=[] if verdict.fault == reasons.REASON_MEASUREMENT_OUTPUT_MUTED
                      else pilot_screens(analysis, program=program))
    if level is not None:
        verdict = replace(verdict, evidence={**verdict.evidence, "level_db_spl": level.reading.level_db,
                                             **({"level_floor_db_spl": level.reading.floor_db}
                                                if level.reading.floor_db is not None else {}),
                                             "level_target_db_spl": level.target_db_spl,
                                             **({"level_capped": True} if capped else {})})
        if verdict.next == "retake_louder" and verdict.next_gain_db is not None:
            # No retake raises a take past its pose's target.
            verdict = replace(verdict, next_gain_db=min(verdict.next_gain_db, level.peak_dbfs + level.gap_db))
    if level_verdict is None:
        return verdict
    verdict = replace(verdict, evidence={**verdict.evidence, **level_verdict.evidence})
    if verdict.ok and verdict.next == "accept" and not level_verdict.ok:
        return replace(verdict, ok=False, fault=level_verdict.fault, next=level_verdict.next,
                       charge=level_verdict.charge, next_gain_db=level_verdict.next_gain_db,
                       capabilities={key: False for key in verdict.capabilities})
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
    raise_rides_next: bool = False,
    reads_timing: bool = True,
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
        "frame_loss": _frames_lost(analysis),
        "glitch_inputs": ",".join(drift.glitch_inputs) if drift else "",
    }
    figures = {
        "anchor_presence": anchor.presence if anchor else None,
        "anchor_confidence": anchor.confidence if anchor else None,
        "anchor_runner_up_presence": anchor.runner_up_presence if anchor else None,
        "anchor_runner_up_confidence": anchor.runner_up_confidence if anchor else None,
        "anchor_pair_presence": anchor.pair_presence if anchor else None,
        "anchor_pair_runner_up_presence": anchor.pair_runner_up_presence if anchor else None,
        "anchor_witnesses_tried": anchor.witnesses_tried if anchor else None,
        "anchor_corroborated": anchor.corroborated if anchor else None,
        "epsilon_ppm": drift.epsilon_ppm if drift else None,
        "max_residual_samples": drift.max_residual_samples if drift else None,
        "schedule_residual_ms_worst": schedule_residual_ms,
        "repeat_level_delta_db": drift.repeat_level_delta_db if drift else None,
        "discontinuity_samples": analysis.discontinuity_samples,
        "peak_dbfs": max((loc.peak_dbfs for loc in stimuli), default=None),
        "locate_confidence_min": min((loc.confidence for loc in stimuli), default=None),
        "sweep_over_ambient_db": analysis.sweep_over_ambient_db,
        **locate_confidences,
    }
    evidence.update({key: value if isinstance(value, bool) else float(value)
                     for key, value in figures.items() if value is not None})
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
    over_ambient = analysis.sweep_over_ambient_db
    if _pilots_heard(analysis) is True and over_ambient is not None and over_ambient < SWEEP_OVER_AMBIENT_MIN_DB:
        return refuse(reasons.REASON_SWEEP_MISSING, next="retake_same", charge="speaker")
    if not _stimulus_locate_ok(analysis, program):
        return quiet(reasons.REASON_LOCATE_FAILED)
    if evidence["anchor_ambiguous"]:
        return refuse(reasons.REASON_ANCHOR_AMBIGUOUS)
    if analysis.delta_implausible:
        return (refuse(reasons.REASON_PILOT_STEP_IMPLAUSIBLE) if analysis.pilot_snr_ok is True
                else quiet(reasons.REASON_SNR_FLOOR))
    if phase == "check" and analysis.channel_map_ok is False:
        evidence.update({f"{reasons.CHANNEL_MAP_FAILED_PREFIX}{pilot.role}": True
                         for pilot in analysis.pilots if pilot.channel_map_ok is False})
        return refuse(reasons.REASON_CHANNEL_MAP_MISMATCH, next="stop", charge="none", ok=True)
    if _pilots_heard(analysis) is False:
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

    def short_at_raise(role: str) -> bool:
        # In the same room, a replay reads a role's alignment SNR higher by its raise.
        shortfall = finite_float(levels.get(f"alignment.{role}.alignment_snr_shortfall_db")) or 0.0
        return shortfall > (adjusted[role] - gains[role] if role in adjusted else 0.0)

    if adjusted:
        if alignment_only and not reads_timing:
            # No decision reads the timing of a take off the mark, so its alignment asks no raise (ADR-0433).
            return verdict
        raised = {**evidence, **{f"next_gain_db.{role}": gain for role, gain in adjusted.items()}}
        if alignment_only and (raise_rides_next or any(short_at_raise(r.role) for r in analysis.driver_responses)):
            # The raise rides the run's later takes, or a replay at it could not reach the floor (ADR-0433).
            return replace(verdict, evidence=raised)
        return replace(verdict, next="retake_louder", next_gain_db=program_peak(adjusted), charge="speaker",
                       evidence=raised)
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


# --------------------------------------------------------------------------- #
# the locate-confidence screen
# --------------------------------------------------------------------------- #

# A located stimulus below this correlation confidence reads as "couldn't hear
# the speaker" (locate_failed).
LOCATE_MIN_CONFIDENCE = 0.1


def _stimulus_locate_ok(analysis: ProgramAnalysis, program: ExcitationProgram | None = None) -> bool:
    """False when any ROLE's stimuli all failed the locate-confidence floor.

    Per ROLE, not per SEGMENT, and not a max() over the whole capture (D8,
    #1838). A max() over every segment is effectively no floor at all on a
    multi-driver program: one clearly-located segment anywhere cleared the gate,
    so a capture in which an entire driver was inaudible passed. Per-SEGMENT
    would be too strict the other way — a two-level pilot pair's quiet side sits
    10 dB under its loud side and locates more coarsely. One confidently-located
    stimulus says "this driver was heard"; zero does not. Role-less stimuli (a
    summed sweep) group together under the same rule.

    A level probe reads exactly the bursts it heard at their anchors, so it was
    heard when it read one (ADR-0442).
    """
    if program is not None and is_level_probe(program):
        return bool(analysis.stimulus_levels)
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
