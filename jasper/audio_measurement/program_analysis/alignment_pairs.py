# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Cancel inter-sweep clock drift with adjacent forward and reverse pairs."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Mapping, cast

import numpy as np

from jasper.audio_measurement.alignment import (
    _bandlimit, _gcc_local_peak_snap, gcc_phat, parabolic_peak, GCC_UPSAMPLE,
)
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program import ExcitationProgram, ProgramSegment
from jasper.log_event import log_event
from .model import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW, ALIGNMENT_OK, AlignmentEstimate,
    GCC_SNAP_RADIUS_PERIODS, logger, MeasurementGeometry, MeasurementPriors,
)
from .response import polarity_label


def estimate_adjacent_alignment(
    capture: np.ndarray,
    program: ExcitationProgram,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    fc_hz: float,
    geometry: MeasurementGeometry,
    priors: MeasurementPriors,
    sweep_irs: Mapping[str, tuple[np.ndarray, int]],
    *,
    woofer_role: str,
) -> AlignmentEstimate:
    sweeps = sorted(
        (program.segment(sid) for sid in sweep_irs),
        key=lambda seg: seg.start_sample,
    )
    pairs = []
    gaps = []
    for first, second in zip(sweeps, sweeps[1:]):
        if first.role == second.role:
            continue
        seg_w, seg_t = (first, second) if first.role == woofer_role else (second, first)
        w_ir, w_pre = sweep_irs[seg_w.segment_id]
        t_ir, t_pre = sweep_irs[seg_t.segment_id]
        pairs.append(_estimate_alignment(
            capture, program, sample_rate, global_offset, epsilon, fc_hz, geometry, priors,
            woofer_full_ir=w_ir, tweeter_full_ir=t_ir, pre_samples=min(w_pre, t_pre),
            seg_w=seg_w, seg_t=seg_t,
        ))
        gaps.append(seg_t.start_sample - seg_w.start_sample)

    first_pair = pairs[0]
    survivors = [(pair, gap) for pair, gap in zip(pairs, gaps) if pair.status == ALIGNMENT_OK]
    if not (any(gap > 0 for _, gap in survivors) and any(gap < 0 for _, gap in survivors)):
        return replace(
            first_pair, alignment_pair_count=int(first_pair.status == ALIGNMENT_OK),
            alignment_pair_spread_us=0.0 if first_pair.status == ALIGNMENT_OK else None,
        )

    def combine(field: str) -> tuple[float | None, list[float], float | None]:
        orders = [
            [(value, gap) for pair, gap in survivors
             if (gap > 0) == forward and (value := getattr(pair, field)) is not None]
            for forward in (True, False)
        ]
        values = [value for order in orders for value, _ in order]
        medians = [float(np.median([value for value, _ in order])) if order else None
                   for order in orders]
        forward, reverse = medians
        if forward is None:
            return reverse, values, None
        if reverse is None:
            return forward, values, None
        # Unequal sweep durations and occurrence counts require sum(weight*gap)=0.
        f_gap = float(np.mean([gap for _, gap in orders[0]]))
        r_gap = -float(np.mean([gap for _, gap in orders[1]]))
        return (r_gap * forward + f_gap * reverse) / (f_gap + r_gap), values, forward - reverse

    snapped, snaps, residual = combine("snapped_delay_us")
    anchor, anchors, _ = combine("anchor_delay_us")
    participating = snaps or anchors
    return replace(
        survivors[0][0],
        delay_us=cast(float, combine("delay_us")[0]),
        raw_delay_us=cast(float, combine("raw_delay_us")[0]),
        confidence=min(pair.confidence for pair, _ in survivors),
        anchor_delay_us=anchor,
        snapped_delay_us=snapped,
        alignment_pair_count=len(participating),
        alignment_pair_spread_us=float(np.ptp(participating)),
        alignment_drift_residual_us=residual,
    )


def _estimate_alignment(
    capture: np.ndarray,
    program: ExcitationProgram,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    fc_hz: float,
    geometry: MeasurementGeometry,
    priors: MeasurementPriors,
    *,
    woofer_full_ir: np.ndarray,
    tweeter_full_ir: np.ndarray,
    pre_samples: int,
    seg_w: ProgramSegment,
    seg_t: ProgramSegment,
) -> AlignmentEstimate:
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    )

    max_lag = priors.align_search_ms * 1e-3 * sample_rate
    # Both IRs share the pre-guard + global offset time base, so each direct
    # peak sits at pre_samples +/- the relative delay. Slice the same
    # [pre-H, pre+H] region from both, band-limit to the overlap, GCC-PHAT.
    half = int(round(0.010 * sample_rate)) + int(math.ceil(max_lag)) + 1
    a = max(0, pre_samples - half)
    b_w = min(woofer_full_ir.size, pre_samples + half)
    b_t = min(tweeter_full_ir.size, pre_samples + half)
    b = min(b_w, b_t)
    ir_w = _bandlimit(np.asarray(woofer_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    ir_t = _bandlimit(np.asarray(tweeter_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    length = min(ir_w.size, ir_t.size)
    ir_w, ir_t = ir_w[:length], ir_t[:length]

    lag_samples, polarity_sign, confidence, at_edge = gcc_phat(
        ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, max_lag_samples=max_lag,
    )
    # epsilon-correct: the tweeter's schedule offset is stretched by epsilon.
    delta_start = seg_t.start_sample - seg_w.start_sample
    tau_samples = lag_samples - epsilon * delta_start
    # delay_us = (D_woofer - D_tweeter) = -tau (tau = D_tweeter - D_woofer).
    raw_delay_us = -tau_samples / sample_rate * 1e6
    parallax_us = geometry.parallax_us()
    delay_us = raw_delay_us - parallax_us

    polarity = polarity_label(polarity_sign)

    status = ALIGNMENT_OK
    if at_edge:
        # A peak clamped at the search bound likely exceeds the geometry
        # prior; fail explicitly rather than return a wrong value.
        status = ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
        confidence = 0.0
        log_event(
            logger,
            "program_analysis.alignment_edge",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            woofer_segment_id=seg_w.segment_id,
            tweeter_segment_id=seg_t.segment_id,
            lag_samples=round(lag_samples, 3),
            search_window_ms=priors.align_search_ms,
        )

    # Fine stage (methodology §10). The aligner OWNS the physical peak-gap
    # anchor (full-IR peak gap, drift+parallax-corrected); the peak is never
    # recomputed downstream so the snap center and the reported anchor cannot
    # desync. The snap moves the anchor to the nearest local maximum of the
    # same correlation within +/-(period/6) at Fc; ``None`` leaves
    # ``_build_candidate`` on the bare anchor. Snaps applied delay only — GCC
    # polarity/confidence is untouched.
    snapped_delay_us: float | None = None
    anchor_delay_us: float | None = None
    if status == ALIGNMENT_OK:
        anchor_lag_samples = (
            _rectified_peak_sample(tweeter_full_ir)
            - _rectified_peak_sample(woofer_full_ir)
        )
        # Peak gap - inter-sweep drift, plus parallax, negated into the signed frame.
        inter_sweep_drift_us = epsilon * delta_start / sample_rate * 1e6
        drift_corrected_peak_gap_us = (
            anchor_lag_samples / sample_rate * 1e6 - inter_sweep_drift_us
        )
        anchor_delay_us = -(drift_corrected_peak_gap_us + parallax_us)
        if fc_hz > 0.0:
            radius_samples = sample_rate / fc_hz * GCC_SNAP_RADIUS_PERIODS
            # Recomputes the correlation rather than threading the seed's
            # array: one extra small FFT per pair, no big-array coupling.
            snapped_lag = _gcc_local_peak_snap(
                ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
                upsample=GCC_UPSAMPLE, anchor_lag_samples=anchor_lag_samples,
                radius_samples=radius_samples,
            )
            if snapped_lag is not None:
                snapped_tau = snapped_lag - epsilon * delta_start
                snapped_delay_us = -snapped_tau / sample_rate * 1e6 - parallax_us

    # The flat-sum cross-check belongs to `_select_alignment_pair`; this
    # estimate is the correlation SEED, and `polarity_agrees_with_sum`
    # stays None until the selection answers it.
    return AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=raw_delay_us,
        parallax_us=parallax_us,
        polarity=polarity,
        polarity_sign=polarity_sign,
        confidence=confidence,
        status=status,
        anchor_delay_us=anchor_delay_us,
        snapped_delay_us=snapped_delay_us,
    )


def _rectified_peak_sample(ir: np.ndarray) -> float:
    """Sub-sample position of an IR's rectified peak, the parabolic estimator.

    ``np.abs`` then a 3-point parabola over the argmax bin — a rectified peak,
    not a Hilbert envelope. A bare ``argmax`` quantises the inter-driver
    anchor to one sample — 20.8 us at 48 kHz, +/-15 deg at a 2 kHz Fc — which
    is large enough to flip ``delay_role`` between sessions on the same
    hardware (#1869; measured +/-2 samples of argmax jitter between
    bit-identical sweep repeats). The same parabolic estimator three siblings
    in this package already use, refining within the same bin.
    """
    magnitude = np.abs(np.asarray(ir, dtype=np.float64))
    return parabolic_peak(magnitude, int(np.argmax(magnitude)))
