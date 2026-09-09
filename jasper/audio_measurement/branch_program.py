# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A complete-tune diagnostic: solo branches and sum on one recording clock."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .program import ExcitationProgram, KIND_SWEEP, _finalize


def is_branch_program(program: ExcitationProgram) -> bool:
    return program.channels == 2 and {"sweep_w", "sweep_t", "sweep_verify"} <= {
        segment.segment_id for segment in program.segments
    }


def build_branch_program(summed: ExcitationProgram, role_channels: Mapping[str, int]) -> ExcitationProgram:
    if set(role_channels) != {"woofer", "tweeter"} or set(role_channels.values()) != {0, 1}:
        raise ValueError("branch diagnostic requires woofer and tweeter on separate stereo channels")
    sweep = summed.segment("sweep_verify")
    tail = summed.segment("tail")
    segments = [replace(seg, role="woofer", channel=role_channels["woofer"],
                        segment_id=seg.segment_id.replace("summed", "woofer"))
                if seg.channel is not None else seg
                for seg in summed.segments if seg.start_sample < sweep.start_sample]
    cursor = sweep.start_sample
    # The two exact repeats let the existing drift reader fit the recording clock.
    for name, role in (("sweep_w", "woofer"), ("sweep_t", "tweeter"),
                       ("sweep_w_rep", "woofer"), ("sweep_t_rep", "tweeter"),
                       ("sweep_verify", "summed")):
        if role == "summed":
            segments.extend(replace(sweep, segment_id=name if channel == 0 else "sum_companion",
                                    start_sample=cursor, channel=channel, role=None)
                            for channel in (0, 1))
        else:
            segments.append(replace(sweep, segment_id=name, kind=KIND_SWEEP,
                                    start_sample=cursor, channel=role_channels[role], role=role))
        cursor += sweep.n_samples
        segments.append(replace(tail, segment_id=f"tail_{name}", start_sample=cursor))
        cursor += tail.n_samples
    return _finalize(summed.phase, 2, segments, cursor)
