# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact-recording window diagnostics over the gate-sweep numerical owner."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from jasper.active_speaker.frequency_view import FrequencyRun, FrequencySeries, build_frequency_view
from jasper.audio_measurement.gating import f_trusted_floor_hz, f_valid_floor_hz
from . import gate_sweep
from .round_captures import capture_row, select_capture


def window_view(round_dir: Path, *, capture_id: str, rungs_ms: Sequence[float], role: str = "summed") -> dict:
    capture = select_capture(round_dir, capture_id=capture_id, role=role)
    rungs, _ = gate_sweep._validated(rungs_ms, ())
    longest = max(*rungs, gate_sweep.REFERENCE_RUNG_MS)
    span = round(longest * capture.sample_rate / 1000)
    lead = round(gate_sweep.PHASE_GATE_LEAD_MS * capture.sample_rate / 1000)
    if span + lead + 1 > gate_sweep.N_FFT or capture.peak_idx + span >= len(capture.ir):
        raise ValueError("window exceeds the retained impulse or the gate-sweep FFT span")
    grid = gate_sweep.analysis_grid()
    read, = gate_sweep._read_curves((capture,), grid, rungs)
    series = tuple(FrequencySeries(
        id=f"{capture.capture_id}:{rung:g}ms", label=f"{rung:g} ms", kind="analysis",
        freqs_hz=tuple(grid), magnitude_db=tuple(read.curves[rung] + read.reference_const_db),
        reference_db=read.reference_const_db,
        smoothing_fractional_octave=gate_sweep.MAGNITUDE_SMOOTH_FRACTION,
        visible_by_default=True,
        details={
            **capture_row(capture), "window_ms": rung,
            "validity_floor_hz": f_valid_floor_hz(rung / 1000),
            "trusted_floor_hz": f_trusted_floor_hz(rung / 1000),
            "band_hz": [max(capture.radiated_band_hz[0], f_valid_floor_hz(rung / 1000)),
                        min(capture.radiated_band_hz[1], capture.sample_rate / 2, float(grid[-1]))],
        },
    ) for rung in rungs)
    start = max(0, capture.peak_idx - lead)
    end = capture.peak_idx + span + 1
    return build_frequency_view(FrequencyRun(
        id=capture.capture_id, label="Same recording · alternative windows",
        measurement_family="window_diagnostic", series=series,
        metadata={
            **capture_row(capture), "frame": gate_sweep.frame_descriptor(rungs, grid),
            "sample_rate_hz": capture.sample_rate,
            "direct_peak_sample": capture.peak_idx,
            "impulse": {
                "start_sample": start, "samples": capture.ir[start:end].tolist(),
                "time_reference_sample": capture.peak_idx,
            },
            "limitations": (
                "Raw program-bound impulse; no microphone correction. See preprocessing for clock correction. "
                "Timing retains the deconvolution sample axis; branch preprocessing binds it to the recording schedule. One reference for all windows. "
                "Usable bands describe window resolution, not freedom from reflections."
            ),
        },
    ))
