# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Repeat-study percentiles and the floor a repeat study measures."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# The frozen consecutive-pair study uses twice its p95; see ADR-0302.
CLAIM_FLOOR_P95_MULTIPLE = 2.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, NumPy's default method, spelled out (not imported) so
    the floor's provenance is auditable without pinning a NumPy version; pinned against
    the banked study's own summary by ``tests/test_active_speaker_attempts_loop.py``.
    """

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile of an empty sample")
    if len(ordered) == 1:
        return ordered[0]
    position = (q / 100.0) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(low)]
    weight = position - low
    return ordered[int(low)] + weight * (ordered[int(high)] - ordered[int(low)])


@dataclass(frozen=True)
class FloorStats:
    """A threshold measured by repeating one unchanged measurement, with its source."""

    metric: str
    claim_floor_db: float
    source: str
    median_db: float
    p95_db: float
    measured_at: str = ""

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("FloorStats.metric must be a non-empty name")
        if not (self.claim_floor_db > 0.0):
            raise ValueError("claim_floor_db must be positive")
        if not self.source:
            raise ValueError("FloorStats.source must say where this came from")

    @classmethod
    def from_repeat_study(
        cls,
        *,
        metric: str,
        median_db: float,
        p95_db: float,
        source: str,
        measured_at: str,
    ) -> "FloorStats":
        """``claim_floor_db`` is ``CLAIM_FLOOR_P95_MULTIPLE * p95_db``."""

        if not (p95_db > 0.0):
            raise ValueError("p95_db must be positive")
        return cls(
            metric=metric,
            claim_floor_db=CLAIM_FLOOR_P95_MULTIPLE * float(p95_db),
            source=source,
            median_db=float(median_db),
            p95_db=float(p95_db),
            measured_at=measured_at,
        )
