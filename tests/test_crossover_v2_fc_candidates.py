"""The declarations that say WHERE this speaker may be crossed (#1894).

Hardware-free throughout, in three parts: the ka/beaming geometry (#1675), the
declared diameter's route from the draft to the conductor, and the two owners of
corner admissibility — ``_fc_rejection`` for "is this corner within every
declared bound" and ``resolve_fc_search_band`` for "which roles' declarations
bind the band".

The corner is executed, not hunted: a round crosses where the household declared
or where an operator pinned, so nothing here ranks one corner against another.
"""

from __future__ import annotations

import math

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import BEAMING_KA, beaming_onset_hz
from jasper.active_speaker.crossover_v2_flow import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_OUTSIDE_SEARCH_BAND,
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
        JTS3_HF_FLOOR_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None,
    ) is None
    # One epsilon below is still refused, and refused BY NAME.
    assert flow._fc_rejection(
        math.nextafter(JTS3_HF_FLOOR_HZ, 0.0),
        JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    # jts3's shipped corner was legal before this ruling and stays legal.
    assert JTS3_CONFIGURED_HZ > JTS3_HF_FLOOR_HZ
    assert flow._fc_rejection(
        JTS3_CONFIGURED_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None,
    ) is None


@pytest.mark.parametrize("fc, floor, ceiling, search, expected", [
    (1500.0, 1600.0, 4000.0, None, FC_REJECT_BELOW_DECLARED_FLOOR),
    # Exact is legal (owner ruling 2026-08-17): AT the floor clears
    # every bound, so it produces no rejection reason at all.
    (1600.0, 1600.0, 4000.0, None, None),
    (4500.0, 1600.0, 4000.0, None, FC_REJECT_ABOVE_LOWER_DRIVER_BAND),
    (1800.0, 1600.0, 4000.0, (2000.0, 2500.0), FC_REJECT_OUTSIDE_SEARCH_BAND),
])
def test_every_bound_has_a_named_reason(fc, floor, ceiling, search, expected):
    """No bare numbers reach a household: each bound is a declaration someone
    confirmed, so each refusal names which one. Ordered hardest-first, so a
    value outside two bounds reports the safety one."""
    assert flow._fc_rejection(fc, floor, ceiling, search) == expected


def test_the_band_edge_tolerance_absorbs_one_decimal_of_rounding():
    """A corner is spelled to one decimal everywhere it is declared, pinned or
    displayed, so the band check must not refuse a value rounding put ON the
    edge it is being checked against — while still refusing one genuinely
    outside.

    Both directions, because a tolerance is only correct if it has a far side:
    a one-sided pin would pass just as well against no tolerance at all, or
    against one wide enough to admit a corner the declaration excludes.
    """
    band = (2000.0, 2500.0)
    eps = flow._FC_GRID_EPS_HZ

    # Inside the tolerance at both edges — admitted.
    for fc in (band[0] - eps, band[1] + eps):
        assert flow._fc_rejection(fc, 1600.0, 4000.0, band) is None, fc
    # Past it at both edges — refused by name.
    for fc in (band[0] - eps * 2, band[1] + eps * 2):
        assert flow._fc_rejection(fc, 1600.0, 4000.0, band) == (
            FC_REJECT_OUTSIDE_SEARCH_BAND
        ), fc
    # …and the tolerance is half a display digit, not a licence: it may never
    # reach the 0.1 Hz the spelling itself can move a corner by.
    assert 0.0 < eps < 0.1


# --- the declared diameter reaches the conductor (#1675's four edits) ---------


def test_the_declared_diameter_resolves_off_the_draft_like_driver_class():
    from jasper.web.correction_crossover_v2 import (
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
    from jasper.web.correction_crossover_v2 import _resolve_radiating_diameter_by_role

    assert _resolve_radiating_diameter_by_role(
        {"manual_settings": {"drivers": drivers}}
    ) == {}


@pytest.mark.parametrize("draft", [
    {}, {"manual_settings": None}, {"manual_settings": {"drivers": None}},
    {"manual_settings": {"drivers": ["not-a-mapping"]}},
])
def test_a_draft_without_declarations_yields_no_priors(draft):
    from jasper.web.correction_crossover_v2 import _resolve_radiating_diameter_by_role

    assert _resolve_radiating_diameter_by_role(draft) == {}


# --- which roles' declarations bind the search band ---------------------------
#
# Two declaration SHAPES on this speaker's numbers, not two readings of the box.
# The pre-repair tweeter band [2000, 2500] is the withdrawn arbitrary-2000
# remnant; the repair moves its floor down to the tweeter's declared hard floor,
# so that edge is written as JTS3_HF_FLOOR_HZ — the governing term — rather than
# as a second free literal. Only the search bands are modelled here, because
# resolve_fc_search_band reads nothing else.
#
# #2191: the earlier version of this block called the pre-repair shape "the jts3
# declaration as it actually stands" and named its test after "the live
# declaration". Nothing here can read the box, so that claim could only decay —
# and it has: the owner's declaration has since been edited and re-confirmed,
# and its tweeter search band is [1600, 2500], the REPAIRED shape below. What
# these tests actually pin is the RULE — which role's declaration binds each
# edge — so they say that instead, and both shapes are fixtures rather than
# readings. Whether a repaired shape can be STORED and CONFIRMED is a different
# question with a real in-repo owner: driver_safety's validator, pinned by
# test_the_owner_ruled_search_repair_is_storable_and_offers_confirmation in
# tests/test_active_speaker_driver_safety.py.
PRE_REPAIR_SEARCH_BAND_BY_ROLE = {
    "woofer": (1200.0, 2500.0),
    "tweeter": (2000.0, 2500.0),
}
REPAIRED_SEARCH_BAND_BY_ROLE = dict(
    PRE_REPAIR_SEARCH_BAND_BY_ROLE,
    tweeter=(JTS3_HF_FLOOR_HZ, 2500.0),
)


def test_the_binding_band_is_the_intersection_and_names_who_set_each_edge():
    """Both drivers sit at Fc, so both declarations must admit it — the band is
    the intersection. On the pre-repair shape the tweeter owns the low edge,
    which is the fact an operator needs in order to act."""
    band = flow.resolve_fc_search_band(PRE_REPAIR_SEARCH_BAND_BY_ROLE)

    assert band.band_hz == (2000.0, 2500.0)
    # The tweeter's 2000 beats the woofer's 1200, so the tweeter owns the low
    # edge — the fact that explains why nothing downward can be proposed. Both
    # roles declare 2500, so the upper owner is a tie broken deterministically.
    assert band.lo_role == "tweeter"
    assert band.hi_role in ("tweeter", "woofer")
    assert band.undeclared_roles == ()


def test_repairing_the_stale_tweeter_declaration_widens_the_binding_band():
    """The rule's payoff, and the proof it is the DECLARATION that binds rather
    than anything hardcoded: widen the tweeter to its declared hard floor and
    the binding band widens with it, with no other input changed. The control is
    the sibling above, which pins the same speaker's pre-repair band at
    ``(2000.0, 2500.0)``.
    """
    band = flow.resolve_fc_search_band(REPAIRED_SEARCH_BAND_BY_ROLE)

    # The repaired low edge is the tweeter's declared hard floor exactly — the
    # search edge the repair moves, the value the owner's re-confirmed
    # declaration now carries, and the same number driver_safety accepts as a
    # stored search floor since #2191.
    assert band.band_hz == (JTS3_HF_FLOOR_HZ, 2500.0)
    assert band.lo_role == "tweeter"


def test_an_undeclared_role_proposes_nothing_rather_than_permitting_everything():
    """``crossover_search_band_hz`` is a REQUIRED declaration, so absence is an
    anomaly — and the safe reading of an anomaly is 'this role has told us
    nothing', never 'this role permits everything'. Fail-closed, and the role
    is named so the disclosure can say which declaration is missing."""
    band = flow.resolve_fc_search_band(
        {"woofer": (1200.0, 2500.0), "tweeter": None}
    )
    assert band.band_hz is None
    assert band.undeclared_roles == ("tweeter",)


def test_an_empty_intersection_still_names_both_edge_owners():
    """The actionable sentence is 'your woofer says at-or-below 1500 and your
    tweeter says at-or-above 2000' — losing the role names to a bare None would
    throw exactly that away."""
    band = flow.resolve_fc_search_band(
        {"woofer": (1200.0, 1500.0), "tweeter": (2000.0, 2500.0)}
    )
    assert band.band_hz is None
    assert (band.lo_role, band.hi_role) == ("tweeter", "woofer")


def test_one_role_can_own_both_edges():
    """A role declared strictly inside every other role's band owns both edges,
    and the disclosure then names one driver twice — which is the truth."""
    band = flow.resolve_fc_search_band(
        {"woofer": (1200.0, 4000.0), "tweeter": (1600.0, 2500.0)}
    )
    assert band.band_hz == (1600.0, 2500.0)
    assert band.lo_role == "tweeter" and band.hi_role == "tweeter"


def test_the_binding_band_never_widens_what_a_role_declared():
    """The invariant that makes this safe to put in front of the safety bounds:
    intersection only ever narrows, so no declared limit is escapable."""
    band = flow.resolve_fc_search_band(PRE_REPAIR_SEARCH_BAND_BY_ROLE)
    assert band.band_hz is not None
    for role, declared in PRE_REPAIR_SEARCH_BAND_BY_ROLE.items():
        assert band.band_hz[0] >= declared[0], role
        assert band.band_hz[1] <= declared[1], role


def test_the_search_band_resolver_reads_the_confirmed_profile_fail_soft():
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_crossover_search_band_hz as resolve,
    )

    profile = {
        "targets": [
            {"target_fingerprint": "fp-w", "crossover_search_band_hz": [200, 2500]},
            {"target_fingerprint": "fp-t", "crossover_search_band_hz": "nonsense"},
            {"target_fingerprint": "fp-x"},
        ],
    }
    assert resolve(profile, "fp-w") == (200.0, 2500.0)
    # Malformed, absent, and an unknown target all decline rather than raise:
    # this bounds where a corner may be OPENED, and refusing a measurement
    # session over it would be the wrong direction (its sibling
    # measurement-band resolver raises).
    assert resolve(profile, "fp-t") is None
    assert resolve(profile, "fp-x") is None
    assert resolve(profile, "fp-missing") is None
