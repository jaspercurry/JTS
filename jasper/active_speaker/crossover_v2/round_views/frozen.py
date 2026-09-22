# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen-reference grading through the shipped grader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.crossover_v2.driver_prescription import (
    DECLARED_TILT_FIELD,
    EXPECTED_DELTA_FIELD,
)
from jasper.active_speaker.crossover_v2.evidence_packet import _mapping
from jasper.active_speaker.crossover_v2.round_inputs import RoundViewsError
from jasper.active_speaker.flat_spec import FlatSpecReport, evaluate_flat_spec
from jasper.active_speaker.flat_spec_views import (
    PositionCurve,
    _evaluate_position,
    _exclusion_mask,
    _pool,
)

from .banked import BankedRound, _round_candidate


def _own_reference_db(position: PositionCurve, report: FlatSpecReport) -> float:
    """The reference the SHIPPED path grades this position against — read
    back off the real evaluator, never recomputed here."""
    graded = evaluate_flat_spec(
        np.asarray(position.freqs_hz, dtype=float),
        np.asarray(position.magnitude_db, dtype=float),
        _exclusion_mask(np.asarray(position.freqs_hz, dtype=float), report.excluded_intervals),
        smoothing_fraction=position.smoothing_fraction,
        **report.frame_kwargs,
    )
    return float(graded.reference_db)


def _grade_positions(
    positions: tuple[PositionCurve, ...],
    report: FlatSpecReport,
    frozen_refs: Mapping[str, float] | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """One grading pass — shipped when ``frozen_refs`` is ``None``, frozen to
    the supplied per-position references otherwise.

    Returns ``(per_role_pooled, per_position_rms_db)``.
    """
    per_role: dict[str, list[tuple[float, float]]] = {}
    per_position: dict[str, float] = {}
    for position in positions:
        seat = position.position_id
        override = None if frozen_refs is None else frozen_refs[seat]
        flatness, _octaves = _evaluate_position(position, report, reference_db_override=override)
        if not flatness.evaluable or flatness.rms_db is None:
            raise RoundViewsError(f"{seat}: position not evaluable under this report's frame")
        per_position[seat] = float(flatness.rms_db)
        per_role.setdefault(position.role, []).append((float(flatness.n_bins), float(flatness.rms_db)))
    pooled = {role: value for role, pairs in per_role.items() if (value := _pool(pairs)) is not None}
    return pooled, per_position


def _banked_pre_registration(banked: BankedRound) -> dict[str, float]:
    """What the round's prescription pre-registered, off the candidate's
    ``prescribed_by`` stamp — every role carries the same pair, so the first wins."""
    for entry in _mapping(_round_candidate(banked).get("linearization")).values():
        stamp = _mapping(_mapping(entry).get("prescribed_by"))
        declared = {
            key: float(v) for key in (EXPECTED_DELTA_FIELD, DECLARED_TILT_FIELD)
            if isinstance(v := stamp.get(key), (int, float)) and type(v) is not bool
        }
        if declared:
            return declared
    return {}


@dataclass(frozen=True)
class FrozenReferenceResult:
    """One target round graded twice: as shipped, and frozen to the baseline's
    per-position reference levels.

    ``baseline``/``shipped``/``frozen`` are ``{role: pooled_rms_db}``. The freeze
    removes the one degree of freedom §8.9 found compensating a prescribed cut's
    level loss — grading each config against its OWN reference.
    ``target_own_refs`` / ``baseline_refs`` are the per-position levels each half
    actually used, so a caller can audit the freeze rather than trust it.

    ``measured_delta_db`` is ``frozen - baseline`` per role under the target's
    frame, ``{}`` when a baseline seat is not evaluable under it. It, the two
    declared fields and ``expected_minus_measured_db`` are DISCLOSURE only.
    """

    baseline_round_dir: str
    target_round_dir: str
    shipped: dict[str, float]
    frozen: dict[str, float]
    baseline: dict[str, float]
    measured_delta_db: dict[str, float]
    shipped_positions: dict[str, float]
    frozen_positions: dict[str, float]
    baseline_refs: dict[str, float]
    target_own_refs: dict[str, float]
    expected_delta_db: float | None = None
    declared_tilt_db_per_octave: float | None = None

    @property
    def expected_minus_measured_db(self) -> dict[str, float] | None:
        """Per role, or ``None`` when nothing was pre-registered."""
        expected = self.expected_delta_db
        if expected is None:
            return None
        return {r: expected - m for r, m in self.measured_delta_db.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_round_dir": self.baseline_round_dir,
            "target_round_dir": self.target_round_dir,
            "shipped": self.shipped,
            "frozen": self.frozen,
            "baseline": self.baseline,
            "measured_delta_db": self.measured_delta_db,
            "shipped_positions": self.shipped_positions,
            "frozen_positions": self.frozen_positions,
            "baseline_refs": self.baseline_refs,
            "target_own_refs": self.target_own_refs,
            EXPECTED_DELTA_FIELD: self.expected_delta_db,
            DECLARED_TILT_FIELD: self.declared_tilt_db_per_octave,
            "expected_minus_measured_db": self.expected_minus_measured_db,
        }


def frozen_reference_grade(baseline: BankedRound, target: BankedRound) -> FrozenReferenceResult:
    """Grade ``target`` twice: shipped, and frozen to ``baseline``'s per-position
    reference levels — then difference the frozen half against the baseline.

    ``target`` may be the same round as ``baseline`` (frozen == shipped by
    construction then). Raises :class:`RoundViewsError` when either round banked
    no cloud group, when a position in ``target`` has no ``position_id``
    counterpart in ``baseline``, or when a position is not evaluable under its
    own report's frame.
    """
    baseline_refs = {
        position.position_id: _own_reference_db(position, baseline.graded_report)
        for position in baseline.graded_positions
    }
    positions = target.graded_positions
    missing = [p.position_id for p in positions if p.position_id not in baseline_refs]
    if missing:
        raise RoundViewsError(
            f"target round has position(s) {missing} with no baseline counterpart "
            f"(baseline has {sorted(baseline_refs)})"
        )
    report = target.graded_report
    target_own_refs = {
        position.position_id: _own_reference_db(position, report)
        for position in positions
    }
    shipped_pooled, shipped_positions = _grade_positions(positions, report, None)
    frozen_pooled, frozen_positions = _grade_positions(positions, report, baseline_refs)
    # Target seats, target frame, same ``baseline_refs``: only curves differ.
    comparand = tuple(
        p for p in baseline.graded_positions if p.position_id in target_own_refs
    )
    try:
        baseline_pooled, _ = _grade_positions(comparand, report, baseline_refs)
    except RoundViewsError:
        baseline_pooled = {}  # A disclosure may not take the grade down.
    declared = _banked_pre_registration(target)
    return FrozenReferenceResult(
        baseline_round_dir=str(baseline.round_dir),
        target_round_dir=str(target.round_dir),
        shipped=shipped_pooled,
        frozen=frozen_pooled,
        baseline=baseline_pooled,
        measured_delta_db={
            role: value - baseline_pooled[role]
            for role, value in frozen_pooled.items() if role in baseline_pooled
        },
        shipped_positions=shipped_positions,
        frozen_positions=frozen_positions,
        baseline_refs=baseline_refs,
        target_own_refs=target_own_refs,
        expected_delta_db=declared.get(EXPECTED_DELTA_FIELD),
        declared_tilt_db_per_octave=declared.get(DECLARED_TILT_FIELD),
    )
