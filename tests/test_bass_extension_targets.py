# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import asdict

from jasper.bass_extension.targets import (
    MARGINS,
    digital_anchor_level,
)
from jasper.volume_curve import percent_to_db


def test_digital_anchor_level_obeys_volume_curve_bound():
    level = digital_anchor_level(11.8, 3.0)
    assert percent_to_db(level, floor_db=-50.0) <= -14.8
    assert percent_to_db(level + 1, floor_db=-50.0) > -14.8
    assert digital_anchor_level(0.0, 3.0) == 100


def test_margin_policy_values_are_pinned():
    assert {name: asdict(policy) for name, policy in MARGINS.items()} == {
        "conservative": {
            "name": "conservative", "boost_cap_db": 6.0,
            "digital_margin_db": 4.0,
            "subsonic_corner_ratio": 0.75, "subsonic_order": 4,
        },
        "normal": {
            "name": "normal", "boost_cap_db": 9.0,
            "digital_margin_db": 3.0,
            "subsonic_corner_ratio": 0.70, "subsonic_order": 4,
        },
        "aggressive": {
            "name": "aggressive", "boost_cap_db": 12.0,
            "digital_margin_db": 2.0,
            "subsonic_corner_ratio": 0.65, "subsonic_order": 2,
        },
    }
