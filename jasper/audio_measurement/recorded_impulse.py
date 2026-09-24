# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The measured impulse one response was read from: what the program analysis
keeps and a round's take stores. NumPy only, so a reader of stored takes does
not load the analysis."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .program import DEFAULT_VERIFY_TAIL_S


@dataclass(frozen=True)
class RecordedImpulse:
    """``samples[origin_index]`` is the scheduled start of the sweep's segment.

    Every impulse of one recording shares that schedule, so
    ``(index - origin_index - clock_shift_samples) / sample_rate_hz`` is one
    time axis across a take's roles and repeats; across recordings the origin
    is each take's own anchor, so only a relative time compares. Raw
    deconvolution: no microphone correction and no configured-path composition.
    """

    samples: np.ndarray
    sample_rate_hz: int
    origin_index: int
    segment_id: str
    clock_shift_samples: float = 0.0


def kept_end(anchor_index: int, size: int, sample_rate_hz: int) -> int:
    """Where a kept impulse ends: :data:`DEFAULT_VERIFY_TAIL_S` past
    ``anchor_index``, its sweep's scheduled start, which the direct peak
    follows by milliseconds."""
    return min(size, anchor_index + round(DEFAULT_VERIFY_TAIL_S * sample_rate_hz) + 1)
