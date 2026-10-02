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


def build_branch_program(summed: ExcitationProgram, branch_channels: Mapping[str, int],
                         alone_gains_db: Mapping[str, float] | None = None) -> ExcitationProgram:
    """Solo each branch, then sum them, on one clock. A branch plays alone at its
    ``alone_gains_db`` level where one is given, else at the sum's (ADR-0407).

    A branch identity is a measurement target id — the two drivers of a
    crossover take (``woofer``/``tweeter``) or the two woofers of a cardioid
    take (``woofer``/``woofer:rear``). Two of them, one per stereo channel.
    """
    if len(branch_channels) != 2 or any(not isinstance(role, str) or not role or role == "summed" for role in branch_channels) or any(
        type(channel) is not int for channel in branch_channels.values()
    ) or set(branch_channels.values()) != {0, 1}:
        raise ValueError("branch diagnostic requires two distinct branch identities on separate stereo input channels")
    # Retain existing program identities, including reversed legacy input routing.
    first, second = ("woofer", "tweeter") if set(branch_channels) == {"woofer", "tweeter"} else sorted(branch_channels, key=branch_channels.__getitem__)
    sweep = summed.segment("sweep_verify")
    tail = summed.segment("tail")
    segments = [replace(seg, role=first, channel=branch_channels[first],
                        segment_id=seg.segment_id.replace("summed", first))
                if seg.channel is not None else seg
                for seg in summed.segments if seg.start_sample < sweep.start_sample]
    cursor = sweep.start_sample
    # The two exact repeats let the existing drift reader fit the recording clock.
    for name, role in (("sweep_w", first), ("sweep_t", second),
                       ("sweep_w_rep", first), ("sweep_t_rep", second),
                       ("sweep_verify", "summed")):
        if role == "summed":
            segments.extend(replace(sweep, segment_id=name if channel == 0 else "sum_companion",
                                    start_sample=cursor, channel=channel, role=None)
                            for channel in (0, 1))
        else:
            segment = replace(sweep, segment_id=name, kind=KIND_SWEEP,
                              start_sample=cursor, channel=branch_channels[role], role=role)
            alone = (alone_gains_db or {}).get(role)
            if alone is not None:
                segment = replace(segment, gain_db=alone,
                                  effective_peak_dbfs=sweep.effective_peak_dbfs - sweep.gain_db + alone)
            segments.append(segment)
        cursor += sweep.n_samples
        segments.append(replace(tail, segment_id=f"tail_{name}", start_sample=cursor))
        cursor += tail.n_samples
    return _finalize(summed.phase, 2, segments, cursor)
