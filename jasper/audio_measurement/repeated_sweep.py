# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Identical summed passes and their sample-aligned quiet windows."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

import numpy as np

from .program import ExcitationProgram, KIND_SUMMED_SWEEP, _finalize, _silence


# In-band repeat RMS differed by 0.06–0.24 dB on the two-microphone corpus.
# Share its 0.3 dB budget with the maximum in-phase loss from arrival spread.
REPEAT_LEVEL_TOLERANCE_DB = 0.3


def summed_alignment_limit_samples(program: ExcitationProgram) -> int:
    """Keep each pass's in-phase loss within the repeat-level budget.

    cos(2*pi*f*dt) >= 10**(-budget/20); using the full arrival spread
    bounds loss relative to either extreme pass, without realigning audio.
    """
    ceiling = max(s.f2_hz for s in program.segments if s.kind == KIND_SUMMED_SWEEP)
    return math.floor(math.acos(10 ** (-REPEAT_LEVEL_TOLERANCE_DB / 20))
                      * program.sample_rate_hz / (2 * math.pi * ceiling))


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
    program: ExcitationProgram, capture: np.ndarray, offset: int, located_starts: Mapping[str, int],
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
    arrivals = []
    for sweep in sweeps:
        ambient = segments.get(sweep_ambient_id(sweep.segment_id))
        if (ambient is None or ambient.n_samples != quiet.n_samples
                or ambient.start_sample + ambient.n_samples != sweep.start_sample
                or (sweep.n_samples, sweep.f1_hz, sweep.f2_hz, sweep.gain_db) != (
                    first.n_samples, first.f1_hz, first.f2_hz, first.gain_db)):
            return "summed_pass_shape_mismatch"
        if offset + sweep.start_sample + sweep.n_samples + tail.n_samples > capture.size:
            return "summed_pass_capture_incomplete"
        if sweep.segment_id not in located_starts:
            return "summed_pass_location_missing"
        arrivals.append(located_starts[sweep.segment_id] - sweep.start_sample)
    if max(arrivals) - min(arrivals) > summed_alignment_limit_samples(program):
        return "summed_pass_arrival_drift"
    return None


def average_summed_capture(
    program: ExcitationProgram, capture: np.ndarray, offset: int, located_starts: Mapping[str, int],
) -> np.ndarray:
    """Average complete, identical passes only when their arrivals remain coherent.

    Slices use the shared capture anchor and emitted sample offsets. Located
    starts qualify coherence; they never move individual passes into alignment.
    """
    sweeps = [segment for segment in program.segments if segment.kind == KIND_SUMMED_SWEEP]
    if len(sweeps) < 2 or summed_pass_refusal(program, capture, offset, located_starts):
        return capture
    first = sweeps[0]
    quiet = program.segment(sweep_ambient_id(first.segment_id)).n_samples
    size = quiet + first.n_samples + program.segment("tail").n_samples
    mean = np.zeros(size, dtype=np.float64)
    for sweep in sweeps:
        start = offset + sweep.start_sample - quiet
        mean += capture[start:start + size]
    averaged = capture.copy()
    start = offset + first.start_sample - quiet
    averaged[start:start + size] = mean / len(sweeps)
    return averaged
