# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free percentile helpers for latency-critical daemons and CLIs."""
from __future__ import annotations

from collections.abc import Iterable
import math


def nearest_rank_percentile(
    samples: Iterable[float],
    percentile: float,
) -> float | None:
    """Return the nearest-rank percentile for measured samples."""

    values = sorted(float(sample) for sample in samples)
    if not values:
        return None
    p = float(percentile)
    if p > 1.0:
        p = p / 100.0
    if not 0.0 < p < 1.0:
        raise ValueError(f"percentile must be in (0, 1), got {percentile!r}")
    idx = max(0, min(len(values) - 1, int(math.ceil(p * len(values))) - 1))
    return values[idx]


__all__ = ["nearest_rank_percentile"]
