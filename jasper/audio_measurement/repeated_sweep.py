# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Identical summed passes and their sample-aligned quiet windows."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .alignment import correlation, parabolic_peak
from .program import ExcitationProgram, KIND_SUMMED_SWEEP, _finalize, _silence


# In-band repeat RMS differed by 0.06–0.24 dB on the two-microphone corpus.
REPEAT_LEVEL_TOLERANCE_DB = 0.3


@dataclass(frozen=True)
class SummedPassAlignment:
    method: str
    # Positive offsets mean later arrivals relative to pass 1.
    offsets_samples: dict[str, int]
    correlation_peaks: dict[str, float]
    residual_spread_samples: float

    def to_dict(self) -> dict[str, Any]:
        return {"pass_alignment": self.method, "pass_offsets_samples": self.offsets_samples,
                "pass_correlation_peaks": self.correlation_peaks,
                "pass_alignment_residual_spread_samples": self.residual_spread_samples}


def sweep_ambient_id(segment_id: str) -> str:
    return f"ambient_{segment_id}"


def repeat_summed_program(
    program: ExcitationProgram, *, passes: int, quiet_samples: int, cooldown_s: float,
) -> ExcitationProgram:
    sweep, tail = program.segment("sweep_verify"), program.segment("tail")
    segments = [segment for segment in program.segments if segment.start_sample < sweep.start_sample]
    cursor = sweep.start_sample
    gap = max(quiet_samples, math.ceil(cooldown_s * program.sample_rate_hz) - tail.n_samples)
    for index in range(passes):
        name = sweep.segment_id if index == 0 else f"{sweep.segment_id}_repeat_{index}"
        if index and gap > quiet_samples:
            segments.append(_silence(f"cooldown_{index}", cursor, gap - quiet_samples))
            cursor += gap - quiet_samples
        segments.append(_silence(sweep_ambient_id(name), cursor, quiet_samples))
        cursor += quiet_samples
        segments.append(replace(sweep, segment_id=name, start_sample=cursor))
        cursor += sweep.n_samples
        segments.append(replace(tail, segment_id="tail" if index == 0 else f"tail_{index}", start_sample=cursor))
        cursor += tail.n_samples
    return _finalize(program.phase, program.channels, segments, cursor)


def summed_pass_refusal(
    program: ExcitationProgram, capture: np.ndarray, offset: int,
) -> str | None:
    sweeps = [s for s in program.segments if s.kind == KIND_SUMMED_SWEEP]
    if len(sweeps) < 2:
        return None
    segments = {s.segment_id: s for s in program.segments}
    first = sweeps[0]
    quiet = segments.get(sweep_ambient_id(first.segment_id))
    tail = segments.get("tail")
    if quiet is None or tail is None:
        return "summed_pass_shape_mismatch"
    if offset < 0 or offset + program.total_samples > capture.size:
        return "summed_pass_capture_incomplete"
    for sweep in sweeps:
        ambient = segments.get(sweep_ambient_id(sweep.segment_id))
        if (ambient is None or ambient.n_samples != quiet.n_samples
                or ambient.start_sample + ambient.n_samples != sweep.start_sample
                or (sweep.n_samples, sweep.f1_hz, sweep.f2_hz, sweep.gain_db) != (
                    first.n_samples, first.f1_hz, first.f2_hz, first.gain_db)):
            return "summed_pass_shape_mismatch"
        if offset + sweep.start_sample + sweep.n_samples + tail.n_samples > capture.size:
            return "summed_pass_capture_incomplete"
    return None


def align_summed_capture(
    program: ExcitationProgram, capture: np.ndarray, offset: int, *, search_samples: int,
) -> tuple[np.ndarray, SummedPassAlignment | None]:
    sweeps = [s for s in program.segments if s.kind == KIND_SUMMED_SWEEP]
    if len(sweeps) < 2 or summed_pass_refusal(program, capture, offset):
        return capture, None
    first = sweeps[0]
    quiet = program.segment(sweep_ambient_id(first.segment_id)).n_samples
    length = quiet + first.n_samples
    size = length + program.segment("tail").n_samples
    start = offset + first.start_sample - quiet
    reference = capture[start:start + length]
    offsets, peaks = {first.segment_id: 0}, {}
    measured = {first.segment_id: 0.0}
    for sweep in sweeps[1:]:
        start = offset + sweep.start_sample - quiet
        lo, hi = max(-search_samples, -start), min(search_samples, capture.size - start - size)
        # Captured copies share the speaker/room response that a clean template lacks.
        corr = correlation(capture[start + lo:start + length + hi], reference,
                           sample_rate=program.sample_rate_hz,
                           max_capture_s=(length + hi - lo) / program.sample_rate_hz)
        peak = int(np.argmax(corr))
        offsets[sweep.segment_id] = lo + peak
        measured[sweep.segment_id] = lo + parabolic_peak(corr, peak)
        peaks[sweep.segment_id] = float(corr[peak])
    # With equal-power copies and independent noise, rho = shared/total; require shared > unshared.
    correlated = all(peak > 1 - peak for peak in peaks.values())
    applied = offsets if correlated else dict.fromkeys(offsets, 0)
    residuals = [measured[key] - applied[key] for key in offsets]
    alignment = SummedPassAlignment("correlated" if correlated else "scheduled", applied, peaks,
                                    max(residuals) - min(residuals))
    aligned = capture.copy() if correlated else capture
    if correlated:
        for sweep in sweeps:
            start = offset + sweep.start_sample - quiet
            shift = applied[sweep.segment_id]
            aligned[start:start + size] = capture[start + shift:start + shift + size]
    return aligned, alignment


def average_summed_capture(
    program: ExcitationProgram, capture: np.ndarray, offset: int,
    alignment: SummedPassAlignment | None = None,
) -> np.ndarray:
    """Average complete, identical passes using their measured or scheduled offsets."""
    sweeps = [segment for segment in program.segments if segment.kind == KIND_SUMMED_SWEEP]
    if len(sweeps) < 2 or summed_pass_refusal(program, capture, offset):
        return capture
    first = sweeps[0]
    quiet = program.segment(sweep_ambient_id(first.segment_id)).n_samples
    size = quiet + first.n_samples + program.segment("tail").n_samples
    mean = np.zeros(size, dtype=np.float64)
    for sweep in sweeps:
        start = offset + sweep.start_sample - quiet
        if alignment is not None:
            start += alignment.offsets_samples[sweep.segment_id]
        mean += capture[start:start + size]
    averaged = capture.copy()
    start = offset + first.start_sample - quiet
    averaged[start:start + size] = mean / len(sweeps)
    return averaged
