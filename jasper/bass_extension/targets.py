# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Target-family margin policy and level-anchor derivation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from jasper.bass_extension.adapters.base import TargetSpec
from jasper.volume_curve import percent_to_db


@dataclass(frozen=True)
class MarginPolicy:
    name: str
    boost_cap_db: float
    rung_step_db: float
    digital_margin_db: float
    compression_fail_db: float
    thd_fail_ratio: float
    sustain_duration_s: float
    sustain_sag_fail_db: float
    sustain_fc_shift_fail_pct: float
    subsonic_corner_ratio: float
    subsonic_order: int


MARGINS: dict[str, MarginPolicy] = {
    "conservative": MarginPolicy(
        name="conservative",
        boost_cap_db=6.0,
        rung_step_db=3.0,
        digital_margin_db=4.0,
        compression_fail_db=1.5,
        thd_fail_ratio=0.03,
        sustain_duration_s=90.0,
        sustain_sag_fail_db=1.5,
        sustain_fc_shift_fail_pct=5.0,
        subsonic_corner_ratio=0.75,
        subsonic_order=4,
    ),
    "normal": MarginPolicy(
        name="normal",
        boost_cap_db=9.0,
        rung_step_db=3.0,
        digital_margin_db=3.0,
        compression_fail_db=2.0,
        thd_fail_ratio=0.10,
        sustain_duration_s=60.0,
        sustain_sag_fail_db=1.5,
        sustain_fc_shift_fail_pct=5.0,
        subsonic_corner_ratio=0.70,
        subsonic_order=4,
    ),
    "aggressive": MarginPolicy(
        name="aggressive",
        boost_cap_db=12.0,
        rung_step_db=3.0,
        digital_margin_db=2.0,
        compression_fail_db=3.0,
        thd_fail_ratio=0.20,
        sustain_duration_s=30.0,
        sustain_sag_fail_db=1.5,
        sustain_fc_shift_fail_pct=5.0,
        subsonic_corner_ratio=0.65,
        subsonic_order=2,
    ),
}


def digital_anchor_level(
    boost_headroom_db: float,
    digital_margin_db: float,
    floor_db: float = -50.0,
) -> int:
    """Return the highest listening level satisfying the digital margin."""

    values = (boost_headroom_db, digital_margin_db, floor_db)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("anchor inputs must be finite")
    if boost_headroom_db <= 0.0:
        return 100
    ceiling_db = -float(digital_margin_db) - float(boost_headroom_db)
    for level in range(100, -1, -1):
        if percent_to_db(level, floor_db=float(floor_db)) <= ceiling_db:
            return level
    return 0


@dataclass(frozen=True)
class AnchorPoint:
    target_id: str
    max_listening_level: int
    evidence: str


def _level_at_or_below(gain_db: float, floor_db: float = -50.0) -> int:
    for level in range(100, -1, -1):
        if percent_to_db(level, floor_db=floor_db) <= gain_db:
            return level
    return 0


def interpolate_anchors(
    targets: tuple[TargetSpec, ...],
    measured: tuple[AnchorPoint, ...],
    margin: MarginPolicy,
) -> tuple[AnchorPoint, ...]:
    """Derive one digitally-clamped anchor per non-natural target."""

    if not measured:
        raise ValueError("at least one measured anchor is required")
    non_natural = targets[:-1]
    index = {target.target_id: i for i, target in enumerate(non_natural)}
    by_id: dict[str, AnchorPoint] = {}
    for point in measured:
        if point.target_id not in index or point.target_id in by_id:
            raise ValueError("measured anchor target is missing or duplicated")
        if not 0 <= point.max_listening_level <= 100:
            raise ValueError("measured anchor level must be between 0 and 100")
        by_id[point.target_id] = point

    ordered_measured = sorted(by_id.values(), key=lambda point: index[point.target_id])
    for deeper, shallower in zip(ordered_measured, ordered_measured[1:]):
        if deeper.max_listening_level > shallower.max_listening_level:
            raise ValueError("measured anchors violate monotonicity")

    boosts = {target.target_id: target.boost_headroom_db for target in non_natural}
    results: list[AnchorPoint] = []
    for target in non_natural:
        digital = digital_anchor_level(
            target.boost_headroom_db,
            margin.digital_margin_db,
        )
        if target.target_id in by_id:
            point = by_id[target.target_id]
            results.append(AnchorPoint(
                target_id=point.target_id,
                max_listening_level=min(point.max_listening_level, digital),
                evidence=point.evidence,
            ))
            continue

        target_i = index[target.target_id]
        lower = [point for point in ordered_measured if index[point.target_id] < target_i]
        upper = [point for point in ordered_measured if index[point.target_id] > target_i]
        if lower and upper:
            left = lower[-1]
            right = upper[0]
            left_boost = boosts[left.target_id]
            right_boost = boosts[right.target_id]
            left_db = percent_to_db(left.max_listening_level, floor_db=-50.0)
            right_db = percent_to_db(right.max_listening_level, floor_db=-50.0)
            if left_boost == right_boost:
                derived_db = min(left_db, right_db)
            else:
                fraction = (left_boost - target.boost_headroom_db) / (
                    left_boost - right_boost
                )
                derived_db = left_db + fraction * (right_db - left_db)
        else:
            reference = (lower[-1] if lower else upper[0])
            reference_db = percent_to_db(
                reference.max_listening_level,
                floor_db=-50.0,
            )
            derived_db = reference_db + (
                boosts[reference.target_id] - target.boost_headroom_db
            )
        results.append(AnchorPoint(
            target_id=target.target_id,
            max_listening_level=min(_level_at_or_below(derived_db), digital),
            evidence="derived",
        ))
    return tuple(results)
