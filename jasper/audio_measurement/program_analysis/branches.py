# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read complete-tune branch diagnostics without fitting another crossover."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .check import _pilot_verdicts
from .drift import _estimate_drift
from .model import ProgramAnalysis
from .response import _deconvolve_window, _driver_response, _n_fft_for, _radiated_band_hz


def analyze_branches(program, capture, sample_rate, global_offset, locations, calibration, priors):
    drift = _estimate_drift(program, capture, sample_rate, locations)
    epsilon = drift.epsilon_ppm / 1e6
    segments = [program.segment(name) for name in ("sweep_w", "sweep_t", "sweep_verify")]
    impulses = [_deconvolve_window(capture, seg, global_offset + seg.start_sample,
                                  sample_rate, epsilon=epsilon) for seg in segments]
    n_fft = _n_fft_for(*(ir for ir, _ in impulses))
    responses = []
    records = []
    for seg, (ir, pre) in zip(segments, impulses):
        role = seg.role or "summed"
        shift = epsilon * seg.start_sample
        response = _driver_response(
            role, ir, sample_rate, calibration=calibration, ambient_report=None,
            fc_hz=priors.crossover_fc_hz, n_fft=n_fft,
            radiated_band_hz=_radiated_band_hz(seg), preserve_timing=True,
        )
        # Remove accumulated clock drift, retaining physical branch delay.
        response = replace(response, complex_tf=response.complex_tf * np.exp(
            2j * np.pi * response.freqs_hz * shift / sample_rate
        ))
        responses.append(response)
        records.append({
            "role": role, "segment_id": seg.segment_id,
            "scheduled_start_sample": seg.start_sample, "pre_guard_samples": pre,
            "clock_shift_samples": shift, "gate": response.gating,
            "band_hz": list(_radiated_band_hz(seg)),
            "impulse": ir[:pre + round(.1 * sample_rate)].tolist(),
        })
    pilots, linearity, channel_map, pilot_snr = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    return ProgramAnalysis(
        phase=program.phase, program_id=program.program_id, locations=tuple(locations),
        drift=drift, driver_responses=tuple(responses[:2]), summed_response=responses[2],
        pilots=pilots, linearity_ok=linearity, channel_map_ok=channel_map, pilot_snr_ok=pilot_snr,
        glitch_detected=drift.glitch_detected,
        mic_tier=priors.mic_tier, mic_calibrated=priors.mic_calibrated,
        measure_pair_not_evaluated="complete_tune_diagnostic",
        branch_diagnostic={
            "sample_rate_hz": sample_rate, "global_offset_samples": global_offset,
            "clock_epsilon_ppm": drift.epsilon_ppm,
            "timing_reference": "shared recording schedule; clock drift removed from complex phase",
            "delay_coordinates": "physical delay is already in the measured tune; prediction changes are residual additions",
            "normalization": "exact emitted stimulus; no branch level fitting",
            "responses": records,
            "limitation": "Each automatic gate has its own coverage. A null needs both crossover shoulders within the common valid band; use exact-recording windows to inspect a longer span, or move the mic away from the early reflection.",
        },
    )
