# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Cancel inter-sweep clock drift with adjacent forward and reverse pairs."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

import numpy as np

from jasper.audio_measurement.program import ExcitationProgram, KIND_SWEEP
from .model import AlignmentEstimate, MeasurementGeometry, MeasurementPriors
from .response import _estimate_alignment


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
) -> AlignmentEstimate:
    sweeps = sorted(
        (seg for seg in program.segments if seg.kind == KIND_SWEEP
         and seg.segment_id in sweep_irs),
        key=lambda seg: seg.start_sample,
    )
    woofer_role = program.segment("sweep_w").role
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

    delta = np.asarray(gaps, dtype=np.float64)
    weights = np.full(len(pairs), 1.0 / len(pairs))
    forward, reverse = delta > 0, delta < 0
    if np.any(forward) and np.any(reverse):
        # Unequal sweep durations and occurrence counts require sum(weight*gap)=0.
        f_gap, r_gap = float(np.mean(delta[forward])), -float(np.mean(delta[reverse]))
        weights[forward] = r_gap / (f_gap + r_gap) / np.count_nonzero(forward)
        weights[reverse] = f_gap / (f_gap + r_gap) / np.count_nonzero(reverse)

    def mean(field: str) -> float | None:
        values = [getattr(pair, field) for pair in pairs]
        return None if any(value is None for value in values) else float(np.dot(weights, values))

    snapped = mean("snapped_delay_us")
    anchor = mean("anchor_delay_us")
    spread_field = ("snapped_delay_us" if snapped is not None else
                    "anchor_delay_us" if anchor is not None else "delay_us")
    return replace(
        pairs[0],
        delay_us=float(np.dot(weights, [pair.delay_us for pair in pairs])),
        raw_delay_us=float(np.dot(weights, [pair.raw_delay_us for pair in pairs])),
        anchor_delay_us=anchor,
        snapped_delay_us=snapped,
        alignment_pair_count=len(pairs),
        alignment_pair_spread_us=float(np.ptp([getattr(pair, spread_field) for pair in pairs])),
        inter_sweep_drift_us=float(np.dot(weights, np.abs(epsilon * delta))) / sample_rate * 1e6,
    )
