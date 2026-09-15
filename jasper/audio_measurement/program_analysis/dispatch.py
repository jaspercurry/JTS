# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Phase dispatch behind :func:`analyze_program_capture`."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Any, Mapping, Sequence, TYPE_CHECKING

import numpy as np

from jasper.audio_measurement.mic_meter import classify_mic_meter
from jasper.audio_measurement.branch_program import is_branch_program
from jasper.audio_measurement.repeated_sweep import average_summed_capture
from .branches import analyze_branches
from .alignment_pairs import estimate_adjacent_alignment

from jasper.audio_measurement import analysis as analysis_mod, deconv
from jasper.audio_measurement.comparison_bands import (
    crossover_region_band_hz,
    overlap_band_hz,
    OVERLAP_OCTAVE_RATIO,
    verify_tracking_band_hz,
)
from jasper.audio_measurement.frame_fit import fit_frame, FrameComparison
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    ExcitationProgram,
    PROGRAM_PHASE_CHECK,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_PHASE_VERIFY,
)
from jasper.log_event import log_event
from .check import (
    _aggregate_linearity_ok,
    _aggregate_tri_state_ok,
    _ambient_from_capture,
    _pilot_observations,
    _pilot_verdicts,
    _solve_gain_plan,
)
from .drift import _estimate_drift, _sweep_occurrences_by_role
from .locate import _global_offset, _locate_segments
from .model import (
    ALIGNMENT_ESTIMATED_FLAT_SUM,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR, ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_SUMMED_FIT, ALIGNMENT_SAVED_TIMING,
    TIMING_AUTHORED, TIMING_ESTIMATE, TIMING_MEASURED, TIMING_NEEDS_MEASUREMENT, TIMING_SAVED,
    ALIGNMENT_OK,
    ALIGNMENT_SNR_REFUSAL_VERDICT,
    AlignmentEstimate,
    AppliedAlignment,
    CAPTURE_BOUND_MARGIN_S,
    CONFIGURED_PATH_PROTECTION_FLOOR_DB,
    ConfiguredPathConditioningError,
    CrossoverCandidate,
    DriverResponse,
    logger,
    MeasurementGeometry,
    MeasurementPriors,
    SummedAlignmentReference,
    ProgramAnalysis,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    SegmentLocation,
    VERIFY_NOTCH_EXCLUSION_DB,
    VERIFY_TRACKING_SMOOTHING_FRACTION,
)
from .response import (
    _aligned_branch_tf,
    branch_level_bands_hz,
    _deconvolve_window,
    _driver_response,
    _gate_floor_hz,
    _n_fft_for,
    polarity_label,
    polarity_sign_of,
    predicted_branch_sum,
    _radiated_band_hz,
    _raw_sweep_segment,
    _ripple_db,
    _select_alignment_pair,
    _select_summed_alignment_pair,
    solve_branch_trims,
    solve_ripple_optimal_trim,
    summed_model_residual_delay_us,
)
from .summary import driver_alignment_snr_verdict
from .verify_integrity import _log_frame_ledger, _verify_capture_integrity

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve


def analyze_program_capture(
    program: ExcitationProgram,
    samples: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None" = None,
    geometry: MeasurementGeometry | None = None,
    priors: MeasurementPriors | None = None,
    capture_report: Mapping[str, Any] | None = None,
) -> ProgramAnalysis:
    """Analyze a program capture into a :class:`ProgramAnalysis` (design §5.6).

    ``capture_report`` is the capture page's own per-take account
    (``CaptureResult.capture_integrity``), reconciled against the frames
    actually handed in; the resulting :class:`~.frame_ledger.FrameLedger`
    rides every returned analysis. ``None`` leaves page-side counts
    unreported, grading as not-evaluated rather than loss.
    """
    if sample_rate != program.sample_rate_hz:
        raise ValueError(
            f"capture rate {sample_rate} != program rate {program.sample_rate_hz}"
        )
    capture = np.asarray(samples, dtype=np.float64).ravel()
    # BEFORE the truncation below, a legitimate transform that must never
    # read as loss.
    frame_ledger = reconcile_capture_frames(
        capture_report, received_frames=int(capture.size),
    )
    _log_frame_ledger(program, frame_ledger)
    # Bound the capture BEFORE any full-rate FFT (1 GB Pi).
    capture = deconv.cap_capture_length(
        capture,
        sweep_len=program.total_samples,
        sample_rate=sample_rate,
        max_capture_seconds=(
            program.total_samples / sample_rate + CAPTURE_BOUND_MARGIN_S
        ),
    )
    geometry = geometry or MeasurementGeometry()
    priors = priors or MeasurementPriors()

    global_offset, _first, stimuli, anchor = _global_offset(
        program, capture, sample_rate
    )
    locations = _locate_segments(program, capture, sample_rate, global_offset, stimuli)

    if is_branch_program(program):
        analysis = analyze_branches(program, capture, sample_rate, global_offset, locations, calibration, priors)
    elif program.phase == PROGRAM_PHASE_CHECK:
        analysis = _analyze_check(
            program, capture, sample_rate, global_offset, locations, priors,
        )
    elif program.phase == PROGRAM_PHASE_MEASURE:
        analysis = _analyze_measure(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors,
        )
    elif program.phase == PROGRAM_PHASE_VERIFY:
        analysis = _analyze_verify(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors, frame_ledger,
        )
    else:
        raise ValueError(f"unknown phase: {program.phase!r}")
    pilots = tuple(
        replace(pilot, mic_meter_status=classify_mic_meter(
            observed_dbfs=pilot.peak_hi_dbfs,
        )["status"])
        for pilot in analysis.pilots
    )
    statuses = {pilot.mic_meter_status for pilot in pilots}
    # A loud pilot takes precedence over a quiet one; per-role grades stay
    # on the observations so the journal does not misattribute the aggregate.
    mic_meter_status = next(
        (status for status in ("too_loud", "too_quiet", "low", "usable", "unmeasured")
         if status in statuses), None,
    )
    discontinuity = analysis.drift.discontinuity_samples if analysis.drift else None
    return replace(
        analysis, frame_ledger=frame_ledger, anchor=anchor,
        anchor_ambiguous=anchor.ambiguous if anchor is not None else False,
        discontinuity_samples=(
            float(discontinuity) if isinstance(discontinuity, (int, float)) else None
        ),
        pilots=pilots, mic_meter_status=mic_meter_status,
    )


def _analyze_check(
    program, capture, sample_rate, global_offset, locations, priors,
) -> ProgramAnalysis:
    ambient_seg = program.segment(AMBIENT_SEGMENT_ID)
    ambient_samples, ambient_report = _ambient_from_capture(
        capture, sample_rate, ambient_seg, global_offset
    )
    # CHECK's 12 s ambient window feeds both the level/SNR path and the
    # channel-map rise test (see `_pilot_observations`).
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=ambient_samples,
        channel_map_ambient_samples=ambient_samples,
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    # Any pilot's gap no wiring can produce (#2647) -- CHECK's ladder routes
    # this to mis-anchoring evidence ahead of `channel_map_mismatch`.
    delta_implausible = any(p.delta_implausible for p in pilots)
    gain_plan = _solve_gain_plan(program, pilots, ambient_report, priors)
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        ambient_report=ambient_report,
        pilots=tuple(pilots),
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        delta_implausible=delta_implausible,
        gain_plan=gain_plan,
    )


def _compose_configured_path_ir(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    radiated_band_hz: tuple[float, float] | None,
    priors: MeasurementPriors,
) -> np.ndarray:
    """Replace exact emitted protection ``P`` with configured crossover ``C``.

    ``S = M*C/P`` (design §4.2) as ONE filter across the whole rfft support,
    never spliced into a sub-band. The conditioning policy binds ONLY on
    §4.2's candidate-required bins (the DRIVEN band intersected with
    ``priors.candidate_required_band_hz_by_role``); outside them the same
    exact ratio applies, saturated at +12 dB. A segment declaring no sweep
    bounds is missing evidence, not ill-conditioning.
    """
    maps = (
        priors.measurement_protection_response_by_role,
        priors.configured_crossover_response_by_role,
        priors.configured_polarity_sign_by_role,
    )
    if all(value is None for value in maps):
        return full_ir
    if any(value is None for value in maps):
        raise ConfiguredPathConditioningError("incomplete priors")
    if radiated_band_hz is None:
        # Missing evidence, not ill-conditioning: no sweep bounds declared.
        raise ValueError(f"{role} sweep segment has no declared band")
    protection_by_role, configured_by_role, polarity_by_role = maps
    try:
        protection_response = protection_by_role[role]  # type: ignore[index]
        configured_response = configured_by_role[role]  # type: ignore[index]
        polarity_sign = polarity_by_role[role]  # type: ignore[index]
    except KeyError as exc:
        raise ConfiguredPathConditioningError(f"missing role {role}") from exc
    if type(polarity_sign) is not int or polarity_sign not in (-1, 1):
        raise ConfiguredPathConditioningError(f"invalid polarity for {role}")
    samples = np.asarray(full_ir, dtype=np.float64)
    measured = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    lo_hz, hi_hz = (float(value) for value in radiated_band_hz)
    band = (priors.candidate_required_band_hz_by_role or {}).get(role)
    if band is not None:
        lo_hz, hi_hz = max(lo_hz, float(band[0])), min(hi_hz, float(band[1]))
    required = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(required):
        raise ConfiguredPathConditioningError(f"no trusted bins for {role}")
    protection = np.asarray(protection_response(freqs), dtype=np.complex128)
    configured = np.asarray(configured_response(freqs), dtype=np.complex128)
    for label, response in (("P", protection), ("C", configured)):
        if not np.all(np.isfinite(response[required])):
            raise ConfiguredPathConditioningError(f"non-finite {label} for {role}")
    minimum = 10.0 ** (CONFIGURED_PATH_PROTECTION_FLOOR_DB / 20.0)
    if np.any(np.abs(protection[required]) < minimum):
        raise ConfiguredPathConditioningError(
            f"P below {CONFIGURED_PATH_PROTECTION_FLOOR_DB:g} dB for {role}",
            protection_floor=True,
        )
    ratio = np.divide(
        configured, protection,
        out=np.zeros_like(configured), where=protection != 0,
    )
    maximum = 10.0 ** (12.0 / 20.0)
    if not np.all(np.isfinite(ratio[required])) or np.any(
        np.abs(ratio[required]) > maximum
    ):
        raise ConfiguredPathConditioningError(f"C/P above +12 dB for {role}")
    saturated = ratio * (maximum / np.maximum(np.abs(ratio), maximum))
    spectrum = measured * polarity_sign * saturated
    if not np.all(np.isfinite(spectrum[required])):
        raise ConfiguredPathConditioningError(f"non-finite S for {role}")
    spectrum[~np.isfinite(spectrum)] = 0.0  # off-required, but rides the irfft
    return np.fft.irfft(spectrum, n=samples.size)


def _repeat_driver_responses(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    sweep_irs: Mapping[str, tuple[np.ndarray, int]],
    occurrences: Sequence[SegmentLocation],
    *,
    role: str,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
    alignment_band_hz: tuple[float, float] | None = None,
) -> tuple[DriverResponse, ...]:
    """Per-repeat responses for ``linearization_envelope.compute_sigma_curve``."""
    out: list[DriverResponse] = []
    for repeat_index, loc in enumerate(occurrences[1:], start=1):
        seg = program.segment(loc.segment_id)
        full_ir, _pre = sweep_irs[seg.segment_id]
        resp = _driver_response(
            role, full_ir, sample_rate,
            calibration=calibration, ambient_report=ambient_report,
            fc_hz=fc_hz, n_fft=n_fft,
            radiated_band_hz=_radiated_band_hz(seg),
            alignment_band_hz=alignment_band_hz,
            capture_segment=_raw_sweep_segment(
                capture, seg, global_offset + seg.start_sample,
            ),
        )
        out.append(replace(resp, repeat_index=repeat_index))
    return tuple(out)


#: :attr:`ProgramAnalysis.measure_pair_not_evaluated`'s only value today: one
#: driver, so no pair to align or to build a candidate across.
MEASURE_PAIR_SINGLE_DRIVER = "single_driver_no_pair"


def _analyze_measure(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors,
) -> ProgramAnalysis:
    # ``None`` is legal for exactly one shape: a 1-way passive main. On a
    # TWO-branch program it still raises below, where ``seg_t`` is known.
    fc_hz = None if priors.crossover_fc_hz is None else float(priors.crossover_fc_hz)
    drift = _estimate_drift(program, capture, sample_rate, locations)

    seg_w = program.segment("sweep_w")
    seg_t = next(
        (seg for seg in program.segments if seg.segment_id == "sweep_t"), None
    )
    if seg_t is not None and fc_hz is None:
        raise ValueError("MEASURE analysis requires priors.crossover_fc_hz")
    alignment_band_hz = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    ) if seg_t is not None and fc_hz is not None else None
    epsilon = drift.epsilon_ppm / 1e6
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    sweeps = {seg.segment_id: seg for seg in (seg_w, seg_t) if seg is not None}
    sweeps.update((loc.segment_id, program.segment(loc.segment_id))
                  for occurrences in occurrences_by_role.values() for loc in occurrences)
    sweep_irs = {}
    # Divide measured epsilon out of the reference so clock drift cannot smear the IR.
    for seg in sweeps.values():
        full_ir, pre = _deconvolve_window(
            capture, seg, global_offset + seg.start_sample, sample_rate, epsilon=epsilon,
        )
        sweep_irs[seg.segment_id] = (_compose_configured_path_ir(
            seg.role, full_ir, sample_rate, _radiated_band_hz(seg), priors,
        ), pre)
    woofer_full_ir = sweep_irs[seg_w.segment_id][0]
    tweeter_full_ir = sweep_irs[seg_t.segment_id][0] if seg_t is not None else None
    n_fft = _n_fft_for(*[ir for ir in (woofer_full_ir, tweeter_full_ir) if ir is not None])

    branches = [(seg_w, woofer_full_ir)]
    if seg_t is not None and tweeter_full_ir is not None:
        branches.append((seg_t, tweeter_full_ir))
    responses = tuple(
        replace(
            resp,
            repeat_responses=_repeat_driver_responses(
                program, capture, sample_rate, global_offset, sweep_irs,
                occurrences_by_role.get(resp.role, ()),
                role=resp.role,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                alignment_band_hz=alignment_band_hz,
            ),
        )
        for resp in (
            _driver_response(
                seg.role, full_ir, sample_rate,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                radiated_band_hz=_radiated_band_hz(seg),
                alignment_band_hz=alignment_band_hz,
                capture_segment=_raw_sweep_segment(
                    capture, seg, global_offset + seg.start_sample,
                ),
            )
            for seg, full_ir in branches
        )
    )

    # ONE branch: nothing to align across, so both come back absent WITH A REASON.
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    pair_not_evaluated: str | None = MEASURE_PAIR_SINGLE_DRIVER
    if seg_t is not None and tweeter_full_ir is not None and fc_hz is not None:
        pair_not_evaluated = None
        alignment = estimate_adjacent_alignment(
            capture, program, sample_rate, global_offset, drift.epsilon_ppm / 1e6,
            fc_hz, geometry, priors, sweep_irs,
            woofer_role=seg_w.role,
        )

        # Reads BOTH branches' ALIGNMENT-class verdict (the 35 dB law), not
        # the magnitude one every surface displays.
        alignment_roles = {seg_w.role, seg_t.role}
        branch_snr_insufficient = tuple(sorted({resp.role for resp in responses
            if resp.role in alignment_roles
            and driver_alignment_snr_verdict(resp) == ALIGNMENT_SNR_REFUSAL_VERDICT}))
        candidate, predicted_sum = _build_candidate(
            woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
            seg_w.role, seg_t.role, alignment, calibration,
            tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
            woofer_sweep_lo_hz=seg_w.f1_hz, tweeter_sweep_hi_hz=seg_t.f2_hz,
            alignment_delay_bounds_us=priors.alignment_delay_bounds_us,
            branch_snr_insufficient=branch_snr_insufficient,
            summed_alignment=priors.summed_alignment, geometry=geometry,
            applied_alignment=priors.applied_alignment,
            repeat_responses=responses,
            explicit_alignment_delay_us=priors.explicit_alignment_delay_us,
            explicit_alignment_polarity_sign=priors.explicit_alignment_polarity_sign,
        )
        alignment = replace(
            alignment, delay_us=candidate.delay_us,
            status=ALIGNMENT_OK if candidate.timing_verdict in (TIMING_SAVED, TIMING_AUTHORED) else alignment.status,
            raw_delay_us=candidate.delay_us + alignment.parallax_us,
            seed_delay_us=alignment.delay_us, polarity=candidate.polarity,
            polarity_sign=polarity_sign_of(candidate.polarity),
            polarity_agrees_with_sum=candidate.polarity_agrees_with_sum,
            confidence_source="gcc_phat_seed",
        )
    else:
        # One branch radiates the whole band, so the model IS that branch.
        predicted_sum = (responses[0].freqs_hz, responses[0].magnitude_db)
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        drift=drift,
        driver_responses=responses,
        alignment=alignment,
        candidate=candidate,
        measure_pair_not_evaluated=pair_not_evaluated,
        mic_tier=priors.mic_tier,
        mic_calibrated=priors.mic_calibrated,
        # Exact: composition returns its input untouched iff every prior map is None.
        configured_path_composed=(
            priors.configured_crossover_response_by_role is not None
        ),
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=predicted_sum,
        glitch_detected=drift.glitch_detected,
    )


def _build_candidate(
    woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
    woofer_role, tweeter_role, alignment, calibration,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
    woofer_sweep_lo_hz: float | None = None,
    tweeter_sweep_hi_hz: float | None = None,
    alignment_delay_bounds_us: tuple[float, float] | None = None,
    branch_snr_insufficient: tuple[str, ...] = (),
    applied_alignment: AppliedAlignment | None = None,
    explicit_alignment_delay_us: float | None = None,
    explicit_alignment_polarity_sign: int | None = None,
    summed_alignment: SummedAlignmentReference | None = None,
    repeat_responses: tuple[DriverResponse, ...] = (),
    geometry: MeasurementGeometry | None = None,
) -> tuple[CrossoverCandidate, tuple[np.ndarray, np.ndarray]]:
    freqs, W, gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=calibration)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=calibration)
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=tweeter_sweep_lo_hz, woofer_sweep_hi_hz=woofer_sweep_hi_hz,
    )
    # Gating-consistent prediction: a branch near a reflective mic position
    # can be valid only above a floor HIGHER than the nominal Fc+/-1-oct
    # band. Clamp every W/T-derived quantity to the worse of the two
    # branches' floors; if the floor consumes the whole band,
    # solve_branch_trims/_ripple_db raise into the internal_error seam.
    branch_floor_hz = max(
        (f for f in (_gate_floor_hz(gate_w), _gate_floor_hz(gate_t)) if f is not None),
        default=None,
    )
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )
    # The LEVEL MATCH reads a different span: each branch on its own side of
    # Fc, its own excited-and-gated span (see solve_branch_trims), floored
    # by the shared reflection floor. Missing sweep bound falls back to the
    # nominal Fc-octave edge.
    def _span(lo_hz: float | None, hi_hz: float | None) -> tuple[float, float]:
        lo = float(lo_hz) if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
        hi = float(hi_hz) if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
            lo = max(lo, branch_floor_hz)
        return lo, hi

    woofer_span = _span(woofer_sweep_lo_hz, woofer_sweep_hi_hz)
    tweeter_span = _span(tweeter_sweep_lo_hz, tweeter_sweep_hi_hz)
    trim_w, trim_t_band_average, level_w, level_t = solve_branch_trims(
        freqs, W, T, fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    (w_band_lo, w_band_hi), (t_band_lo, t_band_hi) = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    # The level match's own inputs. A large disagreement with the per-role
    # target_level_db in crossover_v2_linearization_giveback is the
    # signature of a level-frame defect.
    log_event(
        logger, "program_analysis.branch_level_match",
        woofer_role=woofer_role, tweeter_role=tweeter_role,
        fc_hz=round(float(fc_hz), 3),
        level_w_db=round(float(level_w), 3), level_t_db=round(float(level_t), 3),
        woofer_band_hz=(round(w_band_lo, 1), round(w_band_hi, 1)),
        tweeter_band_hz=(round(t_band_lo, 1), round(t_band_hi, 1)),
        trim_band_average_db=round(float(trim_t_band_average), 3),
    )
    # --- the (polarity, delay) pair, on one objective ---
    # Runs BEFORE the trim polish below (which takes the polarity as input):
    # scoring at the polished trim would be circular.
    seed_delay_us = alignment.delay_us
    anchor_delay_us = None
    snap_found = False
    if alignment.status == ALIGNMENT_OK and alignment.anchor_delay_us is not None:
        anchor_delay_us = float(alignment.anchor_delay_us)
    if anchor_delay_us is not None and alignment_delay_bounds_us is not None:
        if alignment.snapped_delay_us is not None:
            seed_delay_us = float(alignment.snapped_delay_us)
            snap_found = True
        else:
            seed_delay_us = anchor_delay_us
    selection = None
    verification = None
    delay_us, polarity_sign = seed_delay_us, alignment.polarity_sign
    alignment_objective = TIMING_NEEDS_MEASUREMENT if summed_alignment is not None else ALIGNMENT_ESTIMATED_FLAT_SUM
    if applied_alignment is not None:
        delay_us, polarity_sign = applied_alignment.delay_us, polarity_sign_of(applied_alignment.polarity)
        alignment_objective = ALIGNMENT_SAVED_TIMING
    elif explicit_alignment_delay_us is not None:
        delay_us = explicit_alignment_delay_us
        polarity_sign = explicit_alignment_polarity_sign if explicit_alignment_polarity_sign is not None else 1
        alignment_objective = ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR if branch_snr_insufficient else ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
    design_axis = (geometry is not None and (geometry.position_deg, geometry.vertical_deg) == (0, 0))
    if (summed_alignment is not None and design_axis and anchor_delay_us is not None
            and (summed_alignment.position_deg, summed_alignment.vertical_deg) == (0, 0)):
        by_role = {response.role: response for response in repeat_responses}
        repeats = tuple(zip(by_role[woofer_role].repeat_responses, by_role[tweeter_role].repeat_responses)) if by_role else ()
        selection = _select_summed_alignment_pair(
            freqs, W, T, reference=summed_alignment,
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            fc_hz=fc_hz, anchor_delay_us=anchor_delay_us,
            seed_delay_us=seed_delay_us, seed_polarity_sign=alignment.polarity_sign,
            delay_bounds_us=alignment_delay_bounds_us,
            repeats=tuple((w.complex_tf, t.complex_tf) for w, t in repeats), saved=applied_alignment,
        )
        if applied_alignment is not None:
            verification = {"residual_rms_db": selection.residual_rms_db, "repeat_noise_db": selection.repeat_noise_db}
        elif explicit_alignment_delay_us is None:
            alignment_objective = selection.objective
            if alignment_objective == ALIGNMENT_COMMITTED_SUMMED_FIT:
                delay_us, polarity_sign = selection.delay_us, selection.polarity_sign
    elif summed_alignment is None and applied_alignment is None and explicit_alignment_delay_us is None:
        selection = _select_alignment_pair(
            freqs, W, T, fc_hz=fc_hz, lo_hz=lo_clamped, hi_hz=hi,
            trim_w_db=trim_w, trim_t_db=trim_t_band_average,
            anchor_delay_us=anchor_delay_us, seed_delay_us=seed_delay_us,
            seed_polarity_sign=alignment.polarity_sign, delay_bounds_us=alignment_delay_bounds_us,
            branch_snr_insufficient=bool(branch_snr_insufficient),
        ) if alignment.status == ALIGNMENT_OK else None
        if selection is not None:
            delay_us, polarity_sign = selection.delay_us, selection.polarity_sign
    timing_verdict = {
        ALIGNMENT_COMMITTED_SUMMED_FIT: TIMING_MEASURED, ALIGNMENT_SAVED_TIMING: TIMING_SAVED,
        ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR: TIMING_AUTHORED,
        ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION: TIMING_AUTHORED,
        ALIGNMENT_ESTIMATED_FLAT_SUM: TIMING_ESTIMATE,
    }.get(alignment_objective, TIMING_NEEDS_MEASUREMENT)
    seed_ripple_db = selection.seed_ripple_db if selection else None
    flatness_improvement_db = selection.flatness_improvement_db if selection else None
    left_anchor_lobe = bool(selection and selection.left_anchor_lobe)
    log_event(logger, "program_analysis.alignment_selection",
              objective=alignment_objective, timing_verdict=timing_verdict,
              delay_us=delay_us, polarity=polarity_label(polarity_sign),
              prescribed_delay_us=explicit_alignment_delay_us,
              prescribed_polarity=(None if explicit_alignment_delay_us is None else "unpinned"
                                  if explicit_alignment_polarity_sign is None else polarity_label(explicit_alignment_polarity_sign)),
              residual_rms_db=selection.residual_rms_db if selection else None,
              margin_db=selection.margin_db if selection else None,
              repeat_spread_db=selection.repeat_spread_db if selection else None,
              repeat_spread_us=selection.repeat_spread_us if selection else None, repeat_count=selection.repeat_count if selection else None,
              polarity_agrees_with_sum=selection.polarity_agrees_with_sum if selection else None)
    snap_delta_us = None if anchor_delay_us is None else delay_us - anchor_delay_us
    # Polish the tweeter trim for minimum summed-response ripple, seeded by
    # the band-average match, guarded against a result further than
    # REALIZED_LEVEL_MATCH_TOLERANCE_DB from the seed.
    #
    # Run ONLY where summed ripple can express a level: on tweeter-sweep-
    # starts-at-Fc geometry the shared band is one-sided (woofer 20+ dB
    # down its skirt), so the objective moved a trim 7.9 dB on one archived
    # capture with only the sanity guard stopping it — a selector that
    # cannot see the woofer must not set its handoff level.
    ripple_band_straddles_fc = lo_clamped < fc_hz < hi
    ripple_polish_rejected_delta_db: float | None = None
    if ripple_band_straddles_fc:
        trim_t_ripple, _ripple_t_ripple, _seed = solve_ripple_optimal_trim(
            freqs, W, T, fc_hz,
            lo_hz=lo_clamped, hi_hz=hi,
            seed_trim_db=trim_t_band_average,
            trim_w_db=trim_w,
            sign=polarity_sign,
        )
        # Admitted only where the pair it produces can be GRADED as level matched.
        polish_delta_db = trim_t_ripple - trim_t_band_average
        if abs(polish_delta_db) > REALIZED_LEVEL_MATCH_TOLERANCE_DB:
            ripple_polish_rejected_delta_db = float(polish_delta_db)
            log_event(
                logger, "program_analysis.ripple_trim_rejected",
                level=logging.WARNING,
                woofer_role=woofer_role, tweeter_role=tweeter_role,
                band_average_trim_db=round(trim_t_band_average, 3),
                ripple_optimal_trim_db=round(trim_t_ripple, 3),
                rejected_delta_db=round(polish_delta_db, 3),
                tolerance_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB,
            )
            trim_t = trim_t_band_average
        else:
            trim_t = trim_t_ripple
    else:
        log_event(
            logger, "program_analysis.ripple_trim_skipped",
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            reason="ripple_band_one_sided",
            fc_hz=round(float(fc_hz), 3),
            ripple_band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
            band_average_trim_db=round(trim_t_band_average, 3),
        )
        trim_t = trim_t_band_average
    # TWO sums, two questions, two owners.
    #
    # `predicted_aligned` — the flattest-achievable, INDEPENDENTLY ALIGNED sum.
    # It answers "how coherently can this capture's two branches sum at all?",
    # a property of the measurement and not of the delay selection. It is the
    # ONLY input to `predicted_ripple_db`, and therefore to `crossover_v2_flow`'s
    # `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` threshold, which that constant
    # documents as calibrated against a fixed hardware corpus scored on THIS
    # metric — the zero-residual ripple, not the delay-carrying one. Crossing it
    # DISCLOSES rather than refuses.
    #
    # Why the disclosure keeps this frame: a candidate's own committed delay can
    # LOWER its ripple, so pointing it at a delay-carrying curve would let a
    # capture whose branches sum incoherently slip under the threshold on its
    # own alignment — and a reservation a capture can talk itself out of is
    # exactly as dishonest as a veto it could. Measured on a banked JTS3 capture
    # (`captures/r10b-alignment-20260801/ripple_vs_residual_sweep.py`): sweeping
    # the residual across the ±(period/6) snap radius, 32 of 84 sampled
    # residuals come in BELOW the zero-residual 14.8831 dB, bottoming at
    # 14.0744 dB — and that capture sits 0.12 dB under the 15.0 dB ceiling, so
    # the 0.81 dB an alignment could buy is not a hypothetical margin.
    predicted_aligned = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
    )
    ripple = _ripple_db(freqs, predicted_aligned, lo_clamped, hi)
    # `predicted_applied` — the same two branches under the delay this candidate
    # actually COMMITS. This is what gets persisted as
    # `ProgramAnalysis.predicted_sum` and becomes VERIFY's tracking reference,
    # so that comparison grades measured-vs-the-applied-model (model fidelity)
    # rather than measured against a target no realizable delay produces.
    #
    # The RESIDUAL relative to the argmax-referenced frame, never the
    # applied delay itself (see `summed_model_residual_delay_us`'s
    # double-count hazard). Gated on the aligner's own status rather than
    # the snap block's condition, so a hand-built refused estimate is never
    # modelled as though a delay ran. The SNR refusal also withdraws the
    # anchor: on that path `committed - anchor` measures disagreement
    # between an untrusted anchor and a trusted applied delay, not the
    # speaker, and this model is graded by VERIFY's tracking reference —
    # phasing it would let an untrusted number kill a correctly-aligned
    # speaker there. `snap_delta_us` still RECORDS the disagreement.
    _alignment_unmeasured = (
        alignment_objective == ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR
    )
    residual_delay_us = summed_model_residual_delay_us(
        alignment.anchor_delay_us
        if alignment.status == ALIGNMENT_OK and not _alignment_unmeasured
        else None,
        delay_us,
    )
    predicted_applied = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
        freqs_hz=freqs,
        residual_delay_us=residual_delay_us,
    )
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted_applied), 1e-12))
    candidate = CrossoverCandidate(
        trim_db={woofer_role: trim_w, tweeter_role: trim_t},
        polarity=polarity_label(polarity_sign),
        delay_us=delay_us,
        predicted_ripple_db=ripple,
        confidence=alignment.confidence,
        residual_rms_db=selection.residual_rms_db if selection else None,
        margin_db=selection.margin_db if selection else None,
        repeat_spread_db=selection.repeat_spread_db if selection else None,
        repeat_spread_us=selection.repeat_spread_us if selection else None, repeat_count=selection.repeat_count if selection else None,
        timing_verdict=timing_verdict, timing_saved=applied_alignment, timing_verification=verification,
        alignment_seed_ripple_db=seed_ripple_db,
        alignment_seed_delay_us=seed_delay_us,
        flatness_improvement_db=flatness_improvement_db,
        anchor_delay_us=anchor_delay_us,
        snap_delta_us=snap_delta_us,
        snap_found=snap_found,
        trim_band_average_db={woofer_role: trim_w, tweeter_role: trim_t_band_average},
        alignment_objective=alignment_objective,
        seed_polarity_sign=alignment.polarity_sign,
        left_anchor_lobe=left_anchor_lobe,
        # From the selection that MADE the comparison, never re-derived.
        polarity_agrees_with_sum=(
            None if selection is None else selection.polarity_agrees_with_sum
        ),
        polarity_pinned=explicit_alignment_polarity_sign is not None and applied_alignment is None,
        ripple_polish_rejected_delta_db=ripple_polish_rejected_delta_db,
    )
    return candidate, (freqs, predicted_db)


#: ``verify_absolute["not_evaluated"]`` reasons: "nobody graded this" must
#: never render as "this passed".
ABSOLUTE_NO_FC = "no_crossover_fc"
ABSOLUTE_NO_TARGET = "no_candidate_crossover_target"
ABSOLUTE_NO_TRUSTED_BAND = "no_trusted_crossover_region"
#: The speaker HAS no crossover region — a 1-way main (#3480), distinct from
#: the three above, which say a round could not establish a region it does have.
ABSOLUTE_NO_CROSSOVER_TOPOLOGY = "no_crossover_topology"


def _verify_absolute_result(
    summed, segment, fc_hz, priors, measured_db=None,
) -> dict[str, Any]:
    """Measured summed response vs the CANDIDATE'S OWN crossover target across
    the crossover region — the absolute half of "did this crossover hand off
    as designed?".

    The target is ``20log10|sum_role sign_role*C_role(f)|``: the coherent
    sum of the committed crossover transfers. ``priors.predicted_sum`` is
    built from the MEASURED branches (null included); this curve is built
    from the crossover alone and says what the candidate is SUPPOSED to sum
    to. No level or trim enters it — the grader below is offset-invariant.

    Numbers only; tolerance and verdict belong to ``crossover_v2_flow``.
    ``worst_db`` is SIGNED with its frequency, since a dip and a peak are
    opposite defects. Measured is smoothed, the target is not (an analytic
    response with no noise to smooth).
    """
    transfers = priors.configured_crossover_response_by_role
    if transfers is not None and not transfers:
        # An EMPTY map, never a missing one — "has no crossover" vs "nobody
        # said where", both arriving with fc_hz is None.
        return {"not_evaluated": ABSOLUTE_NO_CROSSOVER_TOPOLOGY}
    if fc_hz is None:
        return {"not_evaluated": ABSOLUTE_NO_FC}
    if not transfers:
        return {"not_evaluated": ABSOLUTE_NO_TARGET}
    band = crossover_region_band_hz(
        fc_hz,
        validity_floor_hz=summed.validity_floor_hz,
        radiated_band_hz=_radiated_band_hz(segment),
    )
    if band is None:
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    mask = (summed.freqs_hz >= band[0]) & (summed.freqs_hz <= band[1])
    if not np.any(mask):
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    freqs = np.asarray(summed.freqs_hz, dtype=np.float64)
    total = np.zeros(freqs.shape, dtype=np.complex128)
    signs = priors.configured_polarity_sign_by_role or {}
    for role, response in transfers.items():
        sign = -1 if int(signs.get(role, 1)) < 0 else 1
        total = total + sign * np.asarray(response(freqs), dtype=np.complex128)
    target_db = 20.0 * np.log10(np.maximum(np.abs(total), 1e-12))
    if measured_db is None:
        measured_db = analysis_mod.smooth_fractional_octave(
            freqs, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
    # The SAME offset-invariant grader the tracking pair uses, so two numbers
    # on one screen mean the same kind of thing.
    rms_db, max_db = analysis_mod.tracking_error_db(
        freqs, measured_db, target_db, band,
    )
    deviation = measured_db[mask] - target_db[mask]
    deviation = deviation - float(np.mean(deviation))
    worst = int(np.argmax(np.abs(deviation)))
    return {
        "band_hz": [float(band[0]), float(band[1])],
        "rms_db": float(rms_db),
        "max_db": float(max_db),
        "worst_db": float(deviation[worst]),
        "worst_hz": float(freqs[mask][worst]),
        "n_bins": int(np.count_nonzero(mask)),
    }


def _analyze_verify(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors, frame_ledger,
) -> ProgramAnalysis:
    fc_hz = float(priors.crossover_fc_hz) if priors.crossover_fc_hz else None
    seg = program.segment("sweep_verify")
    full_ir, _pre = _deconvolve_window(
        average_summed_capture(program, capture, global_offset), seg, global_offset + seg.start_sample, sample_rate
    )
    n_fft = _n_fft_for(full_ir)
    summed = _driver_response(
        "summed", full_ir, sample_rate,
        calibration=calibration, ambient_report=None, fc_hz=fc_hz, n_fft=n_fft,
        radiated_band_hz=_radiated_band_hz(seg),
        gate_exempt_reason=geometry.gate_exempt_reason,
    )
    # The tracking comparator below is deliberately NOT re-based onto the
    # spatial cloud's shared spec curve: "did apply do what the model
    # predicted" is a single-position claim (both sides share that
    # geometry), while "is the speaker flat" is graded on the cloud.
    # Feeding the cloud's spatial average in here would read spatial
    # variation as a false tracking failure.
    ripple = None
    reverse_null_depth = None
    tracking = None
    tracking_curve = None
    measured_db = None
    band = verify_tracking_band_hz(
        fc_hz,
        radiated_band_hz=_radiated_band_hz(seg),
        measure_excited_band_hz=priors.measure_excited_band_hz,
    )
    # The ripple and the reverse null are statements about a handoff, so they
    # stay behind the corner rather than behind the band.
    if fc_hz is not None and band is not None:
        lo, hi = band
        ripple = _ripple_db(summed.freqs_hz, summed.complex_tf, lo, hi)
        # Guarded: depth is read at Fc/2 and 2*Fc, np.interp CLAMPS outside
        # the data, so a curve not reaching both shoulders (or below the
        # gate's validity floor) would return an edge-value number with
        # nothing saying so. No number is the honest answer there.
        from jasper.audio_measurement.analysis import crossover_null_depth_db

        lower_shoulder_hz = fc_hz / 2.0
        floor_hz = summed.validity_floor_hz
        spans_shoulders = (
            float(summed.freqs_hz[0]) <= lower_shoulder_hz
            and float(summed.freqs_hz[-1]) >= fc_hz * 2.0
        )
        above_floor = floor_hz is None or lower_shoulder_hz >= floor_hz
        if spans_shoulders and above_floor:
            reverse_null_depth = crossover_null_depth_db(
                summed.freqs_hz, summed.magnitude_db, fc_hz,
            )
    if band is not None and priors.predicted_sum is not None:
        lo, hi = band
        pred_freqs, pred_db = priors.predicted_sum
        measured_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION
        )
        predicted_db_interp = np.interp(summed.freqs_hz, pred_freqs, pred_db)
        predicted_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz,
            predicted_db_interp,
            VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
        # Validity-floor clamp: this capture's own reflection gate can be
        # tighter than the band above at a reflective mic position — bins
        # below it are a truncated-gate artifact, not a measurement, and
        # must not decide PASS/FAIL. Composes with notch exclusion (clamp
        # first, then still exclude a genuine deep predicted notch above it).
        floor_hz = summed.validity_floor_hz
        lo_clamped = (
            max(lo, floor_hz) if floor_hz is not None and math.isfinite(floor_hz) else lo
        )
        tracking_band = (lo_clamped, hi)
        rms, max_abs = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
        )
        # Notch-excluded: the actual gating comparator reads max_db_notch_excluded.
        rms_excl, max_excl = analysis_mod.notch_excluded_tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        # Raw full-band (pre-floor-clamp), DIAGNOSTIC only, never gated on.
        raw_rms, raw_max = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, (lo, hi),
        )
        # Hand the delta probe the same curves these scalars were reduced from.
        tracking_curve = (summed.freqs_hz, measured_db, predicted_db)
        tracking = {
            "rms_db": rms,
            "max_db": max_abs,
            "rms_db_notch_excluded": rms_excl,
            "max_db_notch_excluded": max_excl,
            "tracking_band_hz": [tracking_band[0], tracking_band[1]],
            "rms_db_full_band": raw_rms,
            "max_db_full_band": raw_max,
        }
        # FRAME DISCIPLINE. predicted_db (on-axis, MEASURE sitting) and
        # measured_db (in-room, VERIFY sitting) are not in the same frame —
        # on one corpus a -0.79 dB/octave tilt between them accounted for
        # 84% of a "predictions are optimistic" headline. So the frame is
        # fitted and the residual reported BOTH ways; max_db_notch_excluded
        # still gates, since a measured tilt is EVIDENCE, not permission to
        # re-grade without attribution.
        #
        # FITTED OVER TRUSTED BINS ONLY (validity-floor band minus
        # deep-predicted-notch bins): inside a modelled notch the depth is
        # hypersensitive, and a straight line through one lets the notch
        # lever the slope (measured: a 25 dB edge notch flips an injected
        # -0.800 dB/octave frame to +0.226). This reduces the lever without
        # removing it — a wide surviving skirt still biases the estimate,
        # so ``tilt_removed <= raw`` is not a theorem.
        frame_mask = analysis_mod.notch_excluded_band_mask(
            summed.freqs_hz, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        frame = fit_frame(
            summed.freqs_hz[frame_mask],
            measured_db[frame_mask],
            predicted_db[frame_mask],
        )
        tilt_removed_rms_db: float | None = None
        tilt_removed_max_db: float | None = None
        if frame.fitted:
            # One frame, removed from the measured curve, then each grade
            # re-taken by its own grader. Both graders mean-centre their
            # error, so the frame's OFFSET term cannot move either number;
            # only the TILT can — hence "tilt_removed".
            deframed_db = measured_db - frame.frame_db(summed.freqs_hz)
            tilt_removed_rms_db = analysis_mod.tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
            )[0]
            tilt_removed_max_db = analysis_mod.notch_excluded_tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
                notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
                notch_reference_db=predicted_db_interp,
            )[1]
        # raw_max_db is the NOTCH-EXCLUDED max (what the tolerance gates
        # on), not max_abs — pairing a tilt-removed excluded max against a
        # raw non-excluded one would be two bin sets under one label.
        tracking["frame"] = FrameComparison(
            fit=frame,
            raw_rms_db=rms,
            raw_max_db=max_excl,
            tilt_removed_rms_db=tilt_removed_rms_db,
            tilt_removed_max_db=tilt_removed_max_db,
            # The GRADED band's bin count beside the fit's TRUSTED one, so the
            # exclusion fraction is derivable from the record alone (#1990).
            # `notch_excluded_band_mask`'s own in-band term, not a second
            # opinion about which bins are in band.
            band_n_bins=int(np.count_nonzero(
                analysis_mod._band_mask(summed.freqs_hz, tracking_band)
            )),
        ).to_dict()
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    # Computed on EVERY verify-shaped analysis: the tracking comparison
    # above is exactly what a spliced/clipped recording invalidates.
    integrity = _verify_capture_integrity(
        program, sample_rate, locations, frame_ledger,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        summed_response=summed,
        summed_ripple_db=ripple,
        reverse_null_depth_db=reverse_null_depth,
        verify_tracking=tracking,
        verify_absolute=_verify_absolute_result(
            summed, seg, fc_hz, priors, measured_db=measured_db,
        ),
        verify_tracking_curve=tracking_curve,
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        capture_integrity=integrity,
        glitch_detected=integrity.glitched,
    )
