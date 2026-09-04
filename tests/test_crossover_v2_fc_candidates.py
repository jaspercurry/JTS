"""The declarations that say WHERE this speaker may be crossed (#1894).

Hardware-free throughout, in three parts: the ka/beaming geometry (#1675), the
declared diameter's route from the draft to the conductor, and the single owner
of corner admissibility — ``_fc_rejection``, "is this corner within both
drivers' declared hard excitation bands".

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

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import BEAMING_KA, beaming_onset_hz
from jasper.active_speaker.crossover_v2.fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
)

# The JTS3 declaration, so the numbers below are the ones the owner's speaker
# actually produces rather than a synthetic shape.
JTS3_DIAMETER_MM = 114.0
JTS3_HF_FLOOR_HZ = 1600.0
JTS3_WOOFER_CEILING_HZ = 4000.0
JTS3_CONFIGURED_HZ = 2000.0


# --- ka geometry (#1675) ------------------------------------------------------


def test_beaming_onset_is_the_ka_closed_form_at_the_declared_diameter():
    """ka = 2*pi*f*a/c, so f = ka*c/(2*pi*a). The two numbers the owner ruling
    quotes for the 114 mm declaration, re-derived rather than restated."""
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=1.0) == pytest.approx(957.7, abs=0.05)
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=2.0) == pytest.approx(1915.4, abs=0.05)
    # ka is linear in f, so doubling ka doubles the frequency exactly.
    assert beaming_onset_hz(JTS3_DIAMETER_MM, ka=2.0) == pytest.approx(
        2.0 * beaming_onset_hz(JTS3_DIAMETER_MM, ka=1.0)
    )
    # …and inversely proportional to the diameter: a cone twice as wide beams
    # an octave lower. This is the whole content of the prior.
    assert beaming_onset_hz(2.0 * JTS3_DIAMETER_MM) == pytest.approx(
        0.5 * beaming_onset_hz(JTS3_DIAMETER_MM)
    )
    assert BEAMING_KA == 2.0


def test_beaming_onset_agrees_with_the_browsers_component_entry_hint():
    """The browser hint (kaBeamingOnsetHz) rounds ka=1 to a whole Hz so its
    displayed "2x" is exact. This value is unrounded and must round TO it, so
    the two surfaces cannot quote different geometry for one declaration."""
    for diameter_mm in (25.0, 114.0, 140.0, 165.0, 200.0):
        js_ka1 = round(343.0 / (2.0 * math.pi * (diameter_mm / 2000.0)))
        assert round(beaming_onset_hz(diameter_mm, ka=1.0)) == js_ka1


def test_beaming_onset_refuses_a_dimension_nobody_declared():
    """No conservative default: inventing a diameter would manufacture a
    beaming ceiling out of nothing, and #1675 derives this FROM a declaration."""
    for bad in (0.0, -114.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            beaming_onset_hz(bad)
    with pytest.raises(ValueError):
        beaming_onset_hz(JTS3_DIAMETER_MM, ka=0.0)


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
    assert flow._fc_rejection(
        JTS3_HF_FLOOR_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) is None
    # One epsilon below is still refused, and refused BY NAME.
    assert flow._fc_rejection(
        math.nextafter(JTS3_HF_FLOOR_HZ, 0.0),
        JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    # jts3's shipped corner was legal before this ruling and stays legal.
    assert JTS3_CONFIGURED_HZ > JTS3_HF_FLOOR_HZ
    assert flow._fc_rejection(
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
    assert flow._fc_rejection(fc, floor, ceiling) == expected


def test_only_a_declared_hard_band_can_refuse_a_corner():
    """#2870's whole content, pinned as a property rather than a table.

    Every corner strictly inside both declared hard bands is admissible, with
    no third bound left that can narrow them. The sweep is what makes this more
    than a restatement of the two comparisons: before the ruling, a declared
    search band could refuse any of these, and half of jts3's own range was.
    """
    for fc in range(int(JTS3_HF_FLOOR_HZ), int(JTS3_WOOFER_CEILING_HZ) + 1, 50):
        assert flow._fc_rejection(
            float(fc), JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
        ) is None, fc
    # …and the two edges still bite, one step outside each.
    assert flow._fc_rejection(
        JTS3_HF_FLOOR_HZ - 0.1, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    assert flow._fc_rejection(
        JTS3_WOOFER_CEILING_HZ + 0.1, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ,
    ) == FC_REJECT_ABOVE_LOWER_DRIVER_BAND


def test_the_refusal_vocabulary_is_exactly_the_two_damage_stops():
    """The retired code is gone from the vocabulary, not merely unreachable.

    A constant left defined is a constant something can start returning again,
    and the ruling deleted the CONCEPT rather than one call site.
    """
    from jasper.active_speaker.crossover_v2 import fc_sweep

    assert not hasattr(fc_sweep, "FC_REJECT_OUTSIDE_SEARCH_BAND")
    assert not hasattr(fc_sweep, "resolve_fc_search_band")
    assert not hasattr(fc_sweep, "FcSearchBand")
    assert set(fc_sweep.__all__) == {
        "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
        "FC_REJECT_BELOW_DECLARED_FLOOR",
        "recornered_preset",
    }


# --- the declared diameter reaches the conductor (#1675's four edits) ---------


def test_the_declared_diameter_resolves_off_the_draft_like_driver_class():
    from jasper.active_speaker.crossover_v2.conductor_context import (
        _resolve_driver_class_by_role,
        _resolve_radiating_diameter_by_role,
    )

    draft = {"manual_settings": {"drivers": [
        {"role": "woofer", "driver_class": "unknown",
         "radiating_diameter_mm": JTS3_DIAMETER_MM},
        {"role": "tweeter", "driver_class": "compression_horn"},
    ]}}
    assert _resolve_radiating_diameter_by_role(draft) == {"woofer": JTS3_DIAMETER_MM}
    # Same draft path, same role keying as the field it mirrors.
    assert set(_resolve_driver_class_by_role(draft)) >= {"tweeter"}


@pytest.mark.parametrize("drivers", [
    [{"role": "woofer", "radiating_diameter_mm": 114.0},
     {"role": "woofer", "radiating_diameter_mm": 165.0}],   # disagreeing
    [{"role": "woofer", "radiating_diameter_mm": 0.0}],     # non-physical
    [{"role": "woofer", "radiating_diameter_mm": -114.0}],
    [{"role": "woofer", "radiating_diameter_mm": "114"}],   # not a number
    [{"role": "woofer", "radiating_diameter_mm": True}],    # bool is not a size
    [{"role": "", "radiating_diameter_mm": 114.0}],         # no role
])
def test_a_malformed_diameter_costs_that_role_its_prior_not_the_session(drivers):
    """Fail-soft, exactly like the class resolver it mirrors: a beaming prior is
    guidance, so a bad declaration must never abort a measurement."""
    from jasper.active_speaker.crossover_v2.conductor_context import (
        _resolve_radiating_diameter_by_role,
    )

    assert _resolve_radiating_diameter_by_role(
        {"manual_settings": {"drivers": drivers}}
    ) == {}


@pytest.mark.parametrize("draft", [
    {}, {"manual_settings": None}, {"manual_settings": {"drivers": None}},
    {"manual_settings": {"drivers": ["not-a-mapping"]}},
])
def test_a_draft_without_declarations_yields_no_priors(draft):
    from jasper.active_speaker.crossover_v2.conductor_context import (
        _resolve_radiating_diameter_by_role,
    )

    assert _resolve_radiating_diameter_by_role(draft) == {}
