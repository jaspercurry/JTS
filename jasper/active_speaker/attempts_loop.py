# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Repeat-study percentiles and measured or declared floor values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

FLOOR_BASIS_MEASURED = "measured_repeat_study"
FLOOR_BASIS_POLICY = "declared_policy_bar"
FLOOR_BASES: frozenset[str] = frozenset({
    FLOOR_BASIS_MEASURED, FLOOR_BASIS_POLICY,
})


FLOOR_SCOPE_WITHIN_SITTING = "within_sitting"
FLOOR_SCOPE_ACROSS_SITTINGS = "across_sittings"
FLOOR_SCOPES: frozenset[str] = frozenset({
    FLOOR_SCOPE_WITHIN_SITTING, FLOOR_SCOPE_ACROSS_SITTINGS,
})


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
    """A measured or declared threshold, with its source and scope."""

    metric: str
    claim_floor_db: float
    basis: str
    source: str
    median_db: float | None = None
    p95_db: float | None = None
    measured_at: str = ""
    scope: str = FLOOR_SCOPE_WITHIN_SITTING

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("FloorStats.metric must be a non-empty name")
        if self.basis not in FLOOR_BASES:
            raise ValueError(f"unknown floor basis {self.basis!r}")
        if self.scope not in FLOOR_SCOPES:
            raise ValueError(f"unknown floor scope {self.scope!r}")
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
        scope: str = FLOOR_SCOPE_WITHIN_SITTING,
    ) -> "FloorStats":
        """A floor measured by repeating one unchanged measurement. ``claim_floor_db`` is
        ``CLAIM_FLOOR_P95_MULTIPLE * p95_db``. ``scope`` defaults to
        :data:`FLOOR_SCOPE_WITHIN_SITTING` (the only banked study held the mic fixed); a
        re-placement study passes :data:`FLOOR_SCOPE_ACROSS_SITTINGS`.
        """

        if not (p95_db > 0.0):
            raise ValueError("p95_db must be positive")
        return cls(
            metric=metric,
            claim_floor_db=CLAIM_FLOOR_P95_MULTIPLE * float(p95_db),
            basis=FLOOR_BASIS_MEASURED,
            source=source,
            median_db=float(median_db),
            p95_db=float(p95_db),
            measured_at=measured_at,
            scope=scope,
        )

    @classmethod
    def from_policy_bar(
        cls, *, metric: str, claim_floor_db: float, source: str, scope: str,
    ) -> "FloorStats":
        """A shipped threshold standing in where no repeat study exists.
        :attr:`median_db`/:attr:`p95_db` stay ``None`` rather than being back-solved.
        ``scope`` is required, with no default: a declared bar has no construction fact
        to infer it from.
        """

        return cls(
            metric=metric,
            claim_floor_db=float(claim_floor_db),
            basis=FLOOR_BASIS_POLICY,
            source=source,
            scope=scope,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "claim_floor_db": self.claim_floor_db,
            "basis": self.basis,
            "source": self.source,
            "median_db": self.median_db,
            "p95_db": self.p95_db,
            "measured_at": self.measured_at,
            "scope": self.scope,
        }
