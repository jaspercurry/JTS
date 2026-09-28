"""The declarations that say WHERE this speaker may be crossed (#1894).

Hardware-free throughout, in two parts: each declared driver fact resolved by
measurement target (ADR-0384), and the single owner of corner admissibility —
``_fc_rejection``, "is this corner within both drivers' declared hard
excitation bands".

The corner is executed, not hunted: a round crosses where the household declared
or where an operator pinned, so nothing here ranks one corner against another.
And only a damage stop refuses one: the invented ``crossover_search_band_hz``
that used to narrow the two hard bands was deleted by the 2026-08-22 owner
ruling (#2870), so the bounds pinned here are exactly the two that name a
component-damage mechanism.
"""

from __future__ import annotations

import math

import pytest

from jasper.active_speaker.crossover_v2.corner_admissibility import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    _fc_rejection,
)
from jasper.active_speaker.design_inputs import declared_by_target
from tests.test_rear_output_foundation import _rear_pair

# The JTS3 declaration, so the numbers below are the ones the owner's speaker
# actually produces rather than a synthetic shape.
JTS3_DIAMETER_MM = 114.0
JTS3_HF_FLOOR_HZ = 1600.0
JTS3_WOOFER_CEILING_HZ = 4000.0
JTS3_CONFIGURED_HZ = 2000.0


# --- corner admissibility -----------------------------------------------------


def test_a_corner_exactly_at_the_declared_floor_is_legal():
    """Owner ruling, 2026-08-17: "exact is legal — if the user/manufacturer says
    1600, we should be able to do it. no nannies."

    The manufacturer's minimum recommended crossover is a SANCTIONED operating
    point, so a round may be opened there. #1654's earlier strictness cited the
    candidate's handoff landing on the evidence band's edge; that is a continuum
    (every Fc within an octave of the floor is clamped the same way, just less),
    not a degeneracy at equality — at ``fc == floor`` the scoring band is a full
    octave wide — so there was conservatism to drop and no math to repair.

    Pinned at the BOUNDARY, which is what the table below cannot carry: one
    epsilon under the floor is still refused, and refused by name.
    """
    assert _fc_rejection(
        JTS3_HF_FLOOR_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) is None
    # One epsilon below is still refused, and refused BY NAME.
    assert _fc_rejection(
        math.nextafter(JTS3_HF_FLOOR_HZ, 0.0),
        JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    # jts3's shipped corner was legal before this ruling and stays legal.
    assert JTS3_CONFIGURED_HZ > JTS3_HF_FLOOR_HZ
    assert _fc_rejection(
        JTS3_CONFIGURED_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) is None


@pytest.mark.parametrize("fc, floor, ceiling, expected", [
    (1500.0, 1600.0, 4000.0, FC_REJECT_BELOW_DECLARED_FLOOR),
    # Exact is legal (owner ruling 2026-08-17): AT the floor clears
    # every bound, so it produces no rejection reason at all.
    (1600.0, 1600.0, 4000.0, None),
    (4500.0, 1600.0, 4000.0, FC_REJECT_ABOVE_LOWER_DRIVER_BAND),
    # #2870: 2600 Hz sits between jts3's declared bands and is now ADMITTED.
    # It was refused ``outside_declared_search_band`` until the search band was
    # deleted, purely by an invented 2500 Hz ceiling neither driver declared.
    (2600.0, 1600.0, 4000.0, None),
])
def test_every_bound_has_a_named_reason(fc, floor, ceiling, expected):
    """No bare numbers reach a household: each bound is a declaration someone
    confirmed, so each refusal names which one. Ordered hardest-first, so a
    value outside two bounds reports the safety one."""
    assert _fc_rejection(fc, floor, ceiling) == expected


def test_only_a_declared_hard_band_can_refuse_a_corner():
    """#2870's whole content, pinned as a property rather than a table.

    Every corner strictly inside both declared hard bands is admissible, with
    no third bound left that can narrow them. The sweep is what makes this more
    than a restatement of the two comparisons: before the ruling, a declared
    search band could refuse any of these, and half of jts3's own range was.
    """
    for fc in range(int(JTS3_HF_FLOOR_HZ), int(JTS3_WOOFER_CEILING_HZ) + 1, 50):
        assert _fc_rejection(
            float(fc), JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
        ) is None, fc
    # …and the two edges still bite, one step outside each.
    assert _fc_rejection(
        JTS3_HF_FLOOR_HZ - 0.1, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    assert _fc_rejection(
        JTS3_WOOFER_CEILING_HZ + 0.1, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_ABOVE_LOWER_DRIVER_BAND


def test_the_refusal_vocabulary_is_exactly_the_two_damage_stops():
    """The retired code is gone from the vocabulary, not merely unreachable.

    A constant left defined is a constant something can start returning again,
    and the ruling deleted the CONCEPT rather than one call site.
    """
    from jasper.active_speaker.crossover_v2 import corner_admissibility

    assert not hasattr(corner_admissibility, "FC_REJECT_OUTSIDE_SEARCH_BAND")
    assert not hasattr(corner_admissibility, "resolve_fc_search_band")
    assert not hasattr(corner_admissibility, "FcSearchBand")
    assert set(corner_admissibility.__all__) == {
        "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
        "FC_REJECT_BELOW_DECLARED_FLOOR",
        "fc_rejection_scenarios",
        "recornered_preset",
    }


# --- declared driver facts resolve by measurement target (ADR-0384) ----------

_MONO, _STEREO = (_rear_pair(layout)[1].to_dict() for layout in ("mono", "stereo"))


@pytest.mark.parametrize("topology,manual,research,key,expected", [
    (_MONO, {"mono:woofer": JTS3_DIAMETER_MM, "mono:woofer:rear": 100.0}, {}, "radiating_diameter_mm",
     {"woofer": JTS3_DIAMETER_MM, "woofer:rear": 100.0}),
    (_MONO, {"mono:woofer": JTS3_DIAMETER_MM}, {}, "radiating_diameter_mm",
     {"woofer": JTS3_DIAMETER_MM, "woofer:rear": JTS3_DIAMETER_MM}),
    (_MONO, {"mono:woofer": 120.0}, {"mono:woofer": JTS3_DIAMETER_MM, "mono:tweeter": 25.0}, "radiating_diameter_mm",
     {"woofer": 120.0, "woofer:rear": 120.0, "tweeter": 25.0}),
    (_STEREO, {"left:woofer": JTS3_DIAMETER_MM, "right:woofer": 165.0, "left:tweeter": 25.0}, {"right:tweeter": 25.0},
     "radiating_diameter_mm", {"tweeter": 25.0}),
    (_MONO, {"mono:tweeter": "compression_horn"}, {"mono:tweeter": "soft_dome", "mono:woofer": "unknown"},
     "driver_class", {"tweeter": "compression_horn", "woofer": "unknown", "woofer:rear": "unknown"}),
    (None, {"mono:woofer": JTS3_DIAMETER_MM}, {}, "radiating_diameter_mm", {}),
], ids=["rear_declares_its_own", "rear_takes_the_front", "manual_over_research", "disagreeing_outputs_declare_none",
        "driver_class", "no_topology"])
def test_a_declared_driver_fact_resolves_by_measurement_target(topology, manual, research, key, expected):
    def rows(values):
        return {"drivers": [{"target_id": target, key: value} for target, value in values.items()]}

    draft = {"topology": topology, "manual_settings": rows(manual), "driver_research": rows(research)}
    assert declared_by_target(draft, key) == expected
