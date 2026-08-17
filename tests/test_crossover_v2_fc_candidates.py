"""R17 PR-1 — the declared inputs the Fc selector adjudicates from (#1894).

Two halves, both hardware-free: the ka/beaming geometry (#1675) and the bounded
candidate set derived from declarations. No selector here — the evaluation loop,
scoring, and golden-equivalence proof are PR-2's.
"""

from __future__ import annotations

import math

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import BEAMING_KA, beaming_onset_hz
from jasper.active_speaker.crossover_v2_flow import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_BEAMING,
    FC_REJECT_OUTSIDE_SEARCH_BAND,
    fc_candidate_set,
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


# --- the candidate set --------------------------------------------------------


def test_the_declared_speaker_yields_a_downward_set_under_the_beaming_ceiling():
    """The owner's ruling in numbers: with the arbitrary [2000, 2500] search
    band withdrawn, the physics puts every proposal BELOW the configured Fc."""
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert JTS3_CONFIGURED_HZ in result.candidates
    assert result.alternatives, "the selector must have somewhere to move"
    ceiling = beaming_onset_hz(JTS3_DIAMETER_MM)
    for fc in result.alternatives:
        assert JTS3_HF_FLOOR_HZ <= fc <= ceiling
    assert result.limits["beaming_ceiling_hz"] == pytest.approx(1915.4, abs=0.05)
    assert len(result.alternatives) <= flow.MAX_PROPOSED_FC_CANDIDATES


def test_the_configured_candidate_survives_a_beaming_ceiling_below_it():
    """§9.8 versus #1675, at the exact point they collide on this speaker: the
    ka=2 ceiling is 1915.4 Hz and the configured crossover is 2000 Hz.

    #1675 defines ka as something to WARN on ("the fix is crossover placement
    ... never a filter"), not a fence, and §9.8 requires the configured path
    stay evaluable as the golden mode. So the ceiling bounds what may be
    PROPOSED and never evicts what is configured — otherwise this speaker
    would have no golden candidate and keep-configured could not be a verdict.
    """
    ceiling = beaming_onset_hz(JTS3_DIAMETER_MM)
    assert ceiling < JTS3_CONFIGURED_HZ, "the collision this test exists for"
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert JTS3_CONFIGURED_HZ in result.candidates
    assert all(fc <= ceiling for fc in result.alternatives)


def test_no_declared_diameter_means_no_beaming_ceiling_and_it_is_visible():
    """Undeclared is disclosed, not defaulted — the set simply widens, and the
    absence of the limit is what a consumer reads to say so."""
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        lower_driver_diameter_mm=None,
    )
    assert "beaming_ceiling_hz" not in result.limits
    assert max(result.alternatives) > beaming_onset_hz(JTS3_DIAMETER_MM)


def test_a_corner_exactly_at_the_declared_floor_is_offered_and_legal():
    """Owner ruling, 2026-08-17: "exact is legal — if the user/manufacturer says
    1600, we should be able to do it. no nannies."

    The manufacturer's minimum recommended crossover is a SANCTIONED operating
    point, so the sweep both OFFERS it and accepts it. #1654's earlier
    strictness cited the candidate's handoff landing on the evidence band's
    edge; that is a continuum (every Fc within an octave of the floor is
    clamped the same way, just less), not a degeneracy at equality — at
    ``fc == floor`` the scoring band is a full octave wide — so there was
    conservatism to drop and no math to repair.
    """
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    # Offered...
    assert JTS3_HF_FLOOR_HZ in result.alternatives
    # ...and legal, with no rejection reason of any kind.
    assert flow._fc_rejection(
        JTS3_HF_FLOOR_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None, None,
    ) is None
    # One epsilon below is still refused, and refused BY NAME.
    assert flow._fc_rejection(
        math.nextafter(JTS3_HF_FLOOR_HZ, 0.0),
        JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None, None,
    ) == FC_REJECT_BELOW_DECLARED_FLOOR
    # jts3's shipped corner was legal before this ruling and stays legal.
    assert JTS3_CONFIGURED_HZ > JTS3_HF_FLOOR_HZ
    assert flow._fc_rejection(
        JTS3_CONFIGURED_HZ, JTS3_HF_FLOOR_HZ, JTS3_WOOFER_CEILING_HZ, None, None,
    ) is None


@pytest.mark.parametrize("fc, floor, ceiling, search, beaming, expected", [
    (1500.0, 1600.0, 4000.0, None, None, FC_REJECT_BELOW_DECLARED_FLOOR),
    # Exact is legal (owner ruling 2026-08-17): AT the floor clears
    # every bound, so it produces no rejection reason at all.
    (1600.0, 1600.0, 4000.0, None, None, None),
    (4500.0, 1600.0, 4000.0, None, None, FC_REJECT_ABOVE_LOWER_DRIVER_BAND),
    (1800.0, 1600.0, 4000.0, (2000.0, 2500.0), None,
     FC_REJECT_OUTSIDE_SEARCH_BAND),
    (1950.0, 1600.0, 4000.0, None, 1915.4, FC_REJECT_BEAMING),
    (1800.0, 1600.0, 4000.0, None, 1915.4, None),
])
def test_every_bound_has_a_named_reason(fc, floor, ceiling, search, beaming, expected):
    """No bare numbers reach a household: each bound is a declaration someone
    confirmed, so each refusal names which one. Ordered hardest-first, so a
    value outside two bounds reports the safety one."""
    assert flow._fc_rejection(fc, floor, ceiling, search, beaming) == expected


def test_a_search_band_that_excludes_everything_leaves_only_configured():
    """The pre-ruling [2000, 2500] declaration, kept as a case rather than a
    footnote: it admits no alternative at all once the beaming ceiling applies,
    which is exactly why the owner withdrew it. An empty ALTERNATIVE set is an
    honest keep-configured, not a crash."""
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        search_band_hz=(2000.0, 2500.0),
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert result.candidates == (JTS3_CONFIGURED_HZ,)
    assert result.alternatives == ()


def test_proposals_are_geometric_bounded_and_inside_the_open_interval():
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    alts = result.alternatives
    assert len(alts) == flow.MAX_PROPOSED_FC_CANDIDATES
    ratios = [alts[i + 1] / alts[i] for i in range(len(alts) - 1)]
    assert all(r == pytest.approx(ratios[0], rel=1e-3) for r in ratios), (
        "a crossover argument is per-octave; an arithmetic grid would crowd the top"
    )
    # Half-open [floor, ceiling): the declared floor IS proposed since the
    # 2026-08-17 ruling made it a legal operating point, while the ceiling
    # stays a bound rather than a recommendation.
    assert min(alts) == JTS3_HF_FLOOR_HZ
    assert max(alts) < beaming_onset_hz(JTS3_DIAMETER_MM)


def test_asking_for_no_proposals_still_evaluates_the_configured_path():
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        count=0,
    )
    assert result.candidates == (JTS3_CONFIGURED_HZ,)


def test_an_inverted_admissible_range_proposes_nothing_rather_than_guessing():
    """A declaration whose ceiling sits under its floor is a broken speaker
    profile, not an invitation to interpolate."""
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=3000.0,
        lower_driver_hard_ceiling_hz=1800.0,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert result.alternatives == ()
    assert result.candidates == (JTS3_CONFIGURED_HZ,)


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


# --- which roles' search bands bind the set (R17 PR-2 design point) ------------
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


def test_a_search_floor_above_the_ka_ceiling_proposes_nothing_and_that_is_a_verdict():
    """End to end on the pre-repair shape: the intersection starts at 2000 and
    the ka ceiling ends at 1915.4, so there is no room to propose into. The
    configured Fc survives anyway (§9.8), so the session has a golden candidate
    and 'keep configured' is reachable rather than an error."""
    band = flow.resolve_fc_search_band(PRE_REPAIR_SEARCH_BAND_BY_ROLE)
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        search_band_hz=band.band_hz,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert band.band_hz is not None
    # The verdict's governing relationship, named rather than assumed: the
    # search floor sits ABOVE the beaming ceiling, so the admissible interval
    # is empty. (That the floor is also 2000 Hz is a property of this shape,
    # not the reason.)
    assert band.band_hz[0] > result.limits["beaming_ceiling_hz"]
    assert result.candidates == (JTS3_CONFIGURED_HZ,)
    assert result.alternatives == ()
    # …and the limits carry every bound that produced that verdict, so the
    # disclosure is derivable without re-running the search.
    assert result.limits["search_lo_hz"] == PRE_REPAIR_SEARCH_BAND_BY_ROLE["tweeter"][0]
    assert result.limits["beaming_ceiling_hz"] == pytest.approx(1915.4, abs=0.05)


def test_repairing_the_stale_tweeter_declaration_reopens_the_downward_set():
    """The rule's payoff, and the proof it is the DECLARATION that binds rather
    than anything hardcoded: widen the tweeter to its declared hard floor and
    proposals appear, with no other input changed."""
    band = flow.resolve_fc_search_band(REPAIRED_SEARCH_BAND_BY_ROLE)

    # The repaired low edge is the tweeter's declared hard floor exactly — the
    # search edge the repair moves, the value the owner's re-confirmed
    # declaration now carries, and the same number driver_safety accepts as a
    # stored search floor since #2191.
    assert band.band_hz == (JTS3_HF_FLOOR_HZ, 2500.0)
    assert band.lo_role == "tweeter"
    result = fc_candidate_set(
        configured_hz=JTS3_CONFIGURED_HZ,
        hf_hard_floor_hz=JTS3_HF_FLOOR_HZ,
        lower_driver_hard_ceiling_hz=JTS3_WOOFER_CEILING_HZ,
        search_band_hz=band.band_hz,
        lower_driver_diameter_mm=JTS3_DIAMETER_MM,
    )
    assert result.alternatives, "a repaired declaration must open the search"
    for fc in result.alternatives:
        assert JTS3_HF_FLOOR_HZ <= fc <= result.limits["beaming_ceiling_hz"]


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
