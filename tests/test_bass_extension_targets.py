# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import asdict

import pytest

from jasper.bass_extension.adapters.base import TargetSpec
from jasper.bass_extension.targets import (
    AnchorPoint,
    MARGINS,
    digital_anchor_level,
    interpolate_anchors,
)
from jasper.volume_curve import percent_to_db


def _targets():
    return tuple(
        TargetSpec(f"t{int(boost)}", 30.0 + boost, 0.65, ({"type": "x"},), boost, {})
        for boost in (12.0, 9.0, 6.0, 3.0)
    ) + (TargetSpec("natural", 60.0, 0.7, (), 0.0, {}),)


def test_digital_anchor_level_obeys_volume_curve_bound():
    level = digital_anchor_level(11.8, 3.0)
    assert percent_to_db(level, floor_db=-50.0) <= -14.8
    assert percent_to_db(level + 1, floor_db=-50.0) > -14.8
    assert digital_anchor_level(0.0, 3.0) == 100


def test_single_measured_anchor_derives_linear_in_boost_and_clamps():
    anchors = interpolate_anchors(
        _targets(),
        (AnchorPoint("t12", 35, "measured"),),
        MARGINS["normal"],
    )
    assert anchors[0] == AnchorPoint("t12", 35, "measured")
    assert all(point.evidence == "derived" for point in anchors[1:])
    assert [point.max_listening_level for point in anchors] == sorted(
        point.max_listening_level for point in anchors
    )
    for point, target in zip(anchors, _targets()):
        assert point.max_listening_level <= digital_anchor_level(
            target.boost_headroom_db, MARGINS["normal"].digital_margin_db
        )


def test_measured_monotonicity_violation_raises():
    with pytest.raises(ValueError, match="monotonicity"):
        interpolate_anchors(
            _targets(),
            (
                AnchorPoint("t12", 60, "measured"),
                AnchorPoint("t6", 50, "measured"),
            ),
            MARGINS["normal"],
        )


def test_three_measured_points_pass_through_and_member_between_is_derived():
    measured = (
        AnchorPoint("t12", 30, "measured"),
        AnchorPoint("t6", 45, "spot_verified"),
        AnchorPoint("t3", 55, "measured"),
    )
    anchors = interpolate_anchors(_targets(), measured, MARGINS["normal"])
    by_id = {point.target_id: point for point in anchors}
    for point in measured:
        assert by_id[point.target_id] == point
    assert by_id["t9"].evidence == "derived"
    assert 30 <= by_id["t9"].max_listening_level <= 45


def test_equal_boost_plateau_uses_conservative_measured_anchor():
    targets = (
        TargetSpec("deep", 30.0, None, (), 0.0, {}),
        TargetSpec("middle", 40.0, None, (), 0.0, {}),
        TargetSpec("shallow", 50.0, None, (), 0.0, {}),
        TargetSpec("natural", 60.0, None, (), 0.0, {}),
    )
    anchors = interpolate_anchors(
        targets,
        (
            AnchorPoint("deep", 40, "measured"),
            AnchorPoint("shallow", 60, "measured"),
        ),
        MARGINS["normal"],
    )
    assert anchors[1] == AnchorPoint("middle", 40, "derived")


def test_margin_policy_values_are_pinned():
    assert {name: asdict(policy) for name, policy in MARGINS.items()} == {
        "conservative": {
            "name": "conservative", "boost_cap_db": 6.0, "rung_step_db": 3.0,
            "digital_margin_db": 4.0, "compression_fail_db": 1.5,
            "thd_fail_ratio": 0.03, "sustain_duration_s": 90.0,
            "sustain_sag_fail_db": 1.5, "sustain_fc_shift_fail_pct": 5.0,
            "subsonic_corner_ratio": 0.75, "subsonic_order": 4,
        },
        "normal": {
            "name": "normal", "boost_cap_db": 9.0, "rung_step_db": 3.0,
            "digital_margin_db": 3.0, "compression_fail_db": 2.0,
            "thd_fail_ratio": 0.10, "sustain_duration_s": 60.0,
            "sustain_sag_fail_db": 1.5, "sustain_fc_shift_fail_pct": 5.0,
            "subsonic_corner_ratio": 0.70, "subsonic_order": 4,
        },
        "aggressive": {
            "name": "aggressive", "boost_cap_db": 12.0, "rung_step_db": 3.0,
            "digital_margin_db": 2.0, "compression_fail_db": 3.0,
            "thd_fail_ratio": 0.20, "sustain_duration_s": 30.0,
            "sustain_sag_fail_db": 1.5, "sustain_fc_shift_fail_pct": 5.0,
            "subsonic_corner_ratio": 0.65, "subsonic_order": 2,
        },
    }
