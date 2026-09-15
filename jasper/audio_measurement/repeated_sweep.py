# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Identical summed passes and their sample-aligned quiet windows."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np

from .program import ExcitationProgram, KIND_SUMMED_SWEEP, _finalize, _silence


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


def average_summed_capture(program: ExcitationProgram, capture: np.ndarray, offset: int) -> np.ndarray:
    """Replace the first pass, its quiet window and tail with their coherent means.

    All slices use the one capture anchor plus the emitted sample offsets.
    Local correlation peaks must not move individual passes into alignment.
    """
    sweeps = [segment for segment in program.segments if segment.kind == KIND_SUMMED_SWEEP]
    ids = {segment.segment_id for segment in program.segments}
    if not sweeps or sweep_ambient_id(sweeps[0].segment_id) not in ids:
        return capture
    first = sweeps[0]
    quiet = program.segment(sweep_ambient_id(first.segment_id)).n_samples
    tail = program.segment("tail").n_samples
    size = quiet + first.n_samples + tail
    mean = np.zeros(size, dtype=np.float64)
    for sweep in sweeps:
        ambient = program.segment(sweep_ambient_id(sweep.segment_id))
        if (sweep.n_samples, sweep.f1_hz, sweep.f2_hz, sweep.gain_db, ambient.n_samples) != (
            first.n_samples, first.f1_hz, first.f2_hz, first.gain_db, quiet,
        ) or ambient.start_sample + quiet != sweep.start_sample:
            raise ValueError("summed_pass_shape_mismatch")
        start = offset + sweep.start_sample - quiet
        if start < 0 or start + size > capture.size:
            raise ValueError("summed_pass_capture_incomplete")
        mean += capture[start:start + size]
    averaged = capture.copy()
    start = offset + first.start_sample - quiet
    averaged[start:start + size] = mean / len(sweeps)
    return averaged
