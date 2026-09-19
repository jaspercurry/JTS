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


@dataclass(frozen=True)
class SummedPassAlignment:
    method: str
    # Positive offsets mean later arrivals relative to pass 1.
    offsets_samples: dict[str, int]
    # Pass 1 defines the reference; its peak is None because it has no independent comparison.
    correlation_peaks: dict[str, float | None]
    residual_spread_samples: float
    edge_peaks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"pass_alignment": self.method, "pass_offsets_samples": self.offsets_samples,
                "pass_correlation_peaks": self.correlation_peaks,
                "pass_correlation_edge_peaks": list(self.edge_peaks),
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


def _summed_pass_windows(program: ExcitationProgram, offset: int) -> tuple[dict[str, int], int, int]:
    sweeps = [s for s in program.segments if s.kind == KIND_SUMMED_SWEEP]
    quiet = program.segment(sweep_ambient_id(sweeps[0].segment_id)).n_samples
    size = quiet + sweeps[0].n_samples + program.segment("tail").n_samples
    return {s.segment_id: offset + s.start_sample - quiet for s in sweeps}, quiet, size


def summed_pass_noise(program: ExcitationProgram, capture: np.ndarray, offset: int) -> list[dict[str, Any]]:
    starts, quiet, size = _summed_pass_windows(program, offset)
    sweep_stop = size - program.segment("tail").n_samples
    first_id, first = next(iter(starts.items()))
    reference = capture[first + quiet:first + sweep_stop]
    reference_power = float(np.mean(capture[first:first + quiet] ** 2))
    comparisons = []
    for segment_id, start in list(starts.items())[1:]:
        noise_power = float(np.mean(capture[start:start + quiet] ** 2))
        # Independent noise gives E[(n_i - n_0)^2] = P_i + P_0; no threshold is needed.
        expected = math.sqrt(reference_power + noise_power)
        observed = float(np.sqrt(np.mean((capture[start + quiet:start + sweep_stop] - reference) ** 2)))
        comparisons.append({"reference_segment_id": first_id, "segment_id": segment_id,
                            "expected_rms": expected, "observed_rms": observed,
                            "observed_to_expected_rms_ratio": observed / expected if expected > 0 else None})
    return comparisons


def align_summed_capture(
    program: ExcitationProgram, capture: np.ndarray, offset: int, *, search_samples: int,
) -> tuple[np.ndarray, SummedPassAlignment | None]:
    sweeps = [s for s in program.segments if s.kind == KIND_SUMMED_SWEEP]
    if len(sweeps) < 2 or summed_pass_refusal(program, capture, offset):
        return capture, None
    starts, _quiet, size = _summed_pass_windows(program, offset)
    length = size - program.segment("tail").n_samples
    first_id, start = next(iter(starts.items()))
    reference = capture[start:start + length]
    offsets, measured = {first_id: 0}, {first_id: 0.0}
    peaks: dict[str, float | None] = {first_id: None}
    edges = []
    for segment_id, start in list(starts.items())[1:]:
        lo, hi = max(-search_samples, -start), min(search_samples, capture.size - start - size)
        # Captured copies share the speaker/room response that a clean template lacks.
        corr = correlation(capture[start + lo:start + length + hi], reference,
                           sample_rate=program.sample_rate_hz,
                           max_capture_s=(length + hi - lo) / program.sample_rate_hz, matched_span=True)
        peak = int(np.argmax(corr))
        if peak in (0, corr.size - 1):
            edges.append(segment_id)
        offsets[segment_id] = lo + peak
        measured[segment_id] = lo + parabolic_peak(corr, peak)
        peaks[segment_id] = float(corr[peak])
    # With equal-power copies and independent noise, rho = shared/total; require shared > unshared.
    correlated = not edges and all(peak > 1 - peak for peak in peaks.values() if peak is not None)
    applied = offsets if correlated else dict.fromkeys(offsets, 0)
    residuals = [measured[key] - applied[key] for key in offsets]
    alignment = SummedPassAlignment("correlated" if correlated else "scheduled", applied, peaks,
                                    max(residuals) - min(residuals), tuple(edges))
    aligned = capture.copy() if correlated else capture
    if correlated:
        for segment_id, start in starts.items():
            shift = applied[segment_id]
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
    starts, _quiet, size = _summed_pass_windows(program, offset)
    mean = np.zeros(size, dtype=np.float64)
    for segment_id, start in starts.items():
        if alignment is not None:
            start += alignment.offsets_samples[segment_id]
        mean += capture[start:start + size]
    averaged = capture.copy()
    start = next(iter(starts.values()))
    averaged[start:start + size] = mean / len(sweeps)
    return averaged
