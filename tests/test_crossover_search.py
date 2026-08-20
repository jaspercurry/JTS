# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The search walks a bounded grid, and knows what it is not allowed to claim.

The load-bearing test in this file is the armrun rung. The forward model has
been graded against banked hardware and came back PERFECTLY ANTI-CORRELATED with
the pooled measured referee, so the pre-registered bar for a ranking vote fails.
The tool has to agree with that measurement about its own blindness — a search
that quietly claimed ranking authority would be repeating the campaign's
original mistake, which was reading an ordering from an instrument that had
never been graded against the thing it was ordering.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.crossover_v2.candidate_space import (
    CandidateBounds, XoverCandidate, strictest_highpass_hz,
)
from jasper.active_speaker.crossover_v2.forward_model import DriverPlant
from jasper.active_speaker.crossover_v2.objective import GradingFrame
from jasper.active_speaker.crossover_v2.search import (
    DELAY_RANKING_AUTHORITY, DELAY_RANKING_BAR_RHO, DELAY_RANKING_MEASURED_RHO,
    FLAT_MINIMUM_EPSILON_DB, RANKING_AUTHORITY_BRACKET_ONLY,
    RANKING_AUTHORITY_RANK, SearchPlan, prediction_document, search_candidates,
    spearman_rho,
)

FLOOR_HZ = 357.14285714285717
ANGLES = (-22.0, -7.0, 0.0, 7.0, 22.0)
ROLE_BY_ANGLE = {
    -22.0: "offax", -7.0: "onax", 0.0: "onax", 7.0: "onax", 22.0: "offax",
}
# #2710's per-role integer-sample quantization on a 48 kHz chain.
LATTICE_US = 1e6 / 48_000.0


# --------------------------------------------------------------------------
# THE ARMRUN RUNG — the banked truth, and the tool agreeing with it
# --------------------------------------------------------------------------

#: The six banked arms, as published.
#:
#: Source: ``captures/xover-armrun-2026-08-18/analysis/README-delay-arm-regrade.md``
#: (2026-08-19), "The arms" table. ``predicted_db`` is the delay-SENSITIVE
#: model output graded at the mark; ``measured_pooled_db`` is the product's own
#: 5-position pooled grade — the campaign's measured referee. Both sides went
#: through one identical reduction, validated against the shipped
#: ``evaluate_flat_spec`` to 1.11e-16 dB. Lower is better on both.
#:
#: Transcribed here rather than read from ``captures/``, which is gitignored:
#: a test that needs an untracked directory is a test that does not run in CI,
#: and the numbers are the evidence, so they are the thing to bank.
BANKED_ARMS = (
    # (arm, delay_us, polarity, predicted_db, measured_pooled_db)
    ("control", 12.2, "keep", 1.3537, 0.7219),
    ("a250", -250.0, "invert", 1.3326, 0.7601),
    ("a350", -350.0, "invert", 1.2469, 0.7949),
    ("adopt350", -350.0, "invert", 1.2455, 0.8367),
    ("a450", -450.0, "invert", 1.1258, 0.9085),
    ("a550", -550.0, "keep", 1.0528, 1.0199),
)


def test_the_banked_armrun_reproduces_its_own_anti_correlation() -> None:
    """Rank the six banked arms; the model's order is the measurement reversed.

    This is the experiment the plan's rung 4 pre-registered, run on the banked
    data. It is not a test of the physics — it is a test that the tool's stated
    calibration constant is the number the evidence actually produces, so the
    constant cannot drift away from the artifact it cites.
    """
    predicted = [arm[3] for arm in BANKED_ARMS]
    measured = [arm[4] for arm in BANKED_ARMS]

    rho = spearman_rho(predicted, measured)

    assert rho == pytest.approx(-1.0, abs=1e-12)
    assert DELAY_RANKING_MEASURED_RHO == pytest.approx(rho, abs=1e-12)


def test_the_models_favourite_arm_measured_worst() -> None:
    """Strictly monotone, no ties — the shape that makes rho exactly -1."""
    best_predicted = min(BANKED_ARMS, key=lambda arm: arm[3])
    worst_measured = max(BANKED_ARMS, key=lambda arm: arm[4])
    worst_predicted = max(BANKED_ARMS, key=lambda arm: arm[3])
    best_measured = min(BANKED_ARMS, key=lambda arm: arm[4])

    assert best_predicted[0] == worst_measured[0] == "a550"
    assert worst_predicted[0] == best_measured[0] == "control"


def test_the_delay_lever_has_no_ranking_authority() -> None:
    """The bar fails, decisively and in the wrong direction."""
    assert DELAY_RANKING_MEASURED_RHO < DELAY_RANKING_BAR_RHO
    assert DELAY_RANKING_AUTHORITY.authority == RANKING_AUTHORITY_BRACKET_ONLY
    assert DELAY_RANKING_AUTHORITY.may_rank is False
    assert "armrun" in DELAY_RANKING_AUTHORITY.evidence


def test_the_authority_would_flip_only_on_a_measurement_that_clears_the_bar() -> None:
    """The derivation is a rule, not a hardcoded verdict — the mutation control.

    Without this the three assertions above pass against a constant string, and
    a future calibration that DID clear the bar would leave the stamp stale.
    """
    from jasper.active_speaker.crossover_v2.search import _authority_for

    assert _authority_for(0.95, DELAY_RANKING_BAR_RHO) == RANKING_AUTHORITY_RANK
    assert _authority_for(
        DELAY_RANKING_BAR_RHO, DELAY_RANKING_BAR_RHO,
    ) == RANKING_AUTHORITY_RANK
    assert _authority_for(0.59, DELAY_RANKING_BAR_RHO) == RANKING_AUTHORITY_BRACKET_ONLY
    # An unmeasured lever brackets: absence of evidence is not a passing grade.
    assert _authority_for(None, DELAY_RANKING_BAR_RHO) == RANKING_AUTHORITY_BRACKET_ONLY


def test_spearman_matches_known_values() -> None:
    assert spearman_rho([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert spearman_rho([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    # Ties take average ranks, the standard definition and the banked one.
    assert spearman_rho([1, 1, 3], [1, 2, 3]) == pytest.approx(0.8660254, abs=1e-6)
    # A constant has no ranking; 0.0 would read as "measured, and unrelated".
    assert spearman_rho([1, 1, 1], [1, 2, 3]) is None
    assert spearman_rho([1.0], [2.0]) is None
    assert spearman_rho([1.0, 2.0], [1.0]) is None


# --------------------------------------------------------------------------
# Fixtures for the walk
# --------------------------------------------------------------------------


def _plants(n_bins: int = 1024):
    """A synthetic two-way: a woofer that rolls off and a tweeter that does not.

    Per angle the tweeter loses level, which is what makes the off-axis view a
    different measurement from the on-axis one rather than a copy of it.
    """
    freqs = np.linspace(20.0, 20000.0, n_bins)
    woofer = (1.0 / (1.0 + (freqs / 4000.0) ** 2)).astype(np.complex128)
    plants = []
    for angle_deg in ANGLES:
        taper = 1.0 - abs(angle_deg) / 90.0
        plants.append(DriverPlant(
            "woofer", angle_deg, freqs, woofer, (150.0, 4000.0),
            np.ones(n_bins, dtype=bool),
        ))
        plants.append(DriverPlant(
            "tweeter", angle_deg, freqs,
            np.full(n_bins, taper, dtype=np.complex128), (1200.0, 20000.0),
            np.ones(n_bins, dtype=bool),
        ))
    return tuple(plants), freqs


def _bounds(**overrides) -> CandidateBounds:
    defaults = dict(
        fc_band_hz=(1600.0, 2400.0),
        fc_lo_role="tweeter",
        fc_hi_role="woofer",
        declared_floor_hz_by_role={"tweeter": 1500.0},
        beaming_ceiling_hz=1915.4,
        legal_orders=frozenset({4, 8}),
        delay_window_us=(-125.0, 125.0),
        delay_step_us=LATTICE_US,
        gain_db_range=(-30.0, 0.0),
        undeclared_roles=(),
    )
    defaults.update(overrides)
    return CandidateBounds(**defaults)  # type: ignore[arg-type]


def _incumbent(fc_hz: float = 2000.0, order: int = 4) -> XoverCandidate:
    return XoverCandidate(
        sections_by_role={
            "tweeter": (CrossoverSection(fc_hz=fc_hz, order=order, highpass=True),),
            "woofer": (CrossoverSection(fc_hz=fc_hz, order=order, highpass=False),),
        },
        polarity_by_role={"tweeter": 1, "woofer": 1},
        delay_us_by_role={"tweeter": 0.0, "woofer": 0.0},
        gain_db_by_role={"tweeter": 0.0, "woofer": 0.0},
    )


def _search(**overrides):
    plants, _freqs = _plants()
    frames = {
        angle: GradingFrame.self_referenced(trusted_floor_hz=FLOOR_HZ)
        for angle in ANGLES
    }
    kwargs = dict(
        plan=SearchPlan(
            hf_role="tweeter", lf_role="woofer",
            fc_steps=3, delay_steps=3, gain_steps=3, refinement=1,
        ),
        role_by_angle=ROLE_BY_ANGLE,
        primary_role="onax",
        on_axis_deg=0.0,
        frame_by_angle=frames,
    )
    bounds = overrides.pop("bounds", None) or _bounds()
    incumbent = overrides.pop("incumbent", None) or _incumbent()
    kwargs.update(overrides)
    return search_candidates(plants, bounds, incumbent, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def shortlist():
    """One default walk, shared by every test that does not vary its inputs.

    Module-scoped because a walk is the expensive thing in this file and the
    default one is deterministic by construction — the determinism test below
    runs two walks of its own precisely so this shared object cannot be what
    makes them agree.
    """
    return _search()


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def test_the_same_inputs_produce_a_byte_identical_document() -> None:
    """Determinism is structural: no seed, because nothing draws."""
    first = json.dumps(prediction_document(_search()), sort_keys=True)
    second = json.dumps(prediction_document(_search()), sort_keys=True)

    assert first == second


def test_the_incumbent_is_always_evaluated_and_carried(shortlist) -> None:
    """``select_fc``'s S9.8 discipline: keeping configured is an honest verdict."""

    assert shortlist.incumbent.score.total is not None
    assert shortlist.incumbent.candidate.corner_hz == (2000.0,)
    assert shortlist.incumbent.score.primary is not None


def test_an_out_of_bounds_incumbent_is_still_graded_and_its_refusal_named() -> None:
    """The case a household most needs to see graded beside the alternatives."""
    shortlist = _search(incumbent=_incumbent(fc_hz=1000.0))

    assert shortlist.incumbent.score.total is not None
    assert any(what == "incumbent" for what, _why in shortlist.refusals)
    assert any(
        why == "below_declared_floor" for _what, why in shortlist.refusals
    )


def test_every_proposed_delay_lands_on_the_declared_lattice(shortlist) -> None:
    """A proposal the emitted graph rounds away is a proposal nobody ran."""

    proposed = [
        entry.candidate.delay_us_by_role["tweeter"]
        for entry in (shortlist.incumbent, *shortlist.ranked)
    ]
    assert proposed
    for delay_us in proposed:
        offset = abs(delay_us) % LATTICE_US
        assert min(offset, LATTICE_US - offset) < 0.05


def test_every_emitted_candidate_high_passes_the_floor_declared_role(shortlist) -> None:
    """Complete with respect to the declared floor, never silent about it.

    #2735's front door refuses a candidate that says NOTHING about a role a
    declaration protects — ``strictest_highpass_hz`` returns ``None`` for a role
    with no high-pass, and the shared predicate reads that as unsatisfied,
    because "a candidate cannot opt out of a wall by staying silent". So every
    candidate this search emits has to carry a READABLE corner on the role the
    plan high-passes, or the whole walk would refuse itself for silence rather
    than for any property of the crossover.
    """
    for entry in (shortlist.incumbent, *shortlist.ranked):
        assert strictest_highpass_hz(entry.candidate, "tweeter") is not None
    assert shortlist.ranked


def test_a_floor_nothing_in_the_chain_high_passes_refuses_everything() -> None:
    """Fail-closed and DISCLOSED, when the branch really is open below its floor.

    A two-way plan puts a high-pass on the HF role and a low-pass on the LF one,
    so neither a floor on the LF role nor one on a role the plan never names has
    a crossover corner to answer it. When the chain declares no standing
    high-pass there either — ``camilla_yaml._bass_management_active`` emits the
    lowest driver's only when the preset carries a local subwoofer — nothing
    honours that floor and every candidate is refused by name. That is the
    honest outcome: the search may not work around a wall by proposing an
    unprotected branch, and an empty shortlist saying ``below_declared_floor``
    on every point is legible where a silently-shortened one would not be.
    """
    lf_floor = _bounds(
        declared_floor_hz_by_role={"tweeter": 1500.0, "woofer": 60.0},
    )
    unnamed_role = _bounds(
        declared_floor_hz_by_role={"tweeter": 1500.0, "supertweeter": 8000.0},
    )

    for bounds in (lf_floor, unnamed_role):
        refused = _search(bounds=bounds)
        assert refused.ranked == ()
        assert refused.refusals
        assert {why for _what, why in refused.refusals} == {"below_declared_floor"}
        # The incumbent is graded anyway — a household running an unprotected
        # branch is exactly who needs to see it — and its refusal is named.
        assert refused.incumbent.score.total is not None
        assert ("incumbent", "below_declared_floor") in refused.refusals

    # The control: the same walk with only the HF role's floor declared does
    # produce a shortlist, so the two assertions above are about the LF floor
    # and not about the fixture refusing everything for some other reason.
    assert _search(bounds=_bounds()).ranked


def test_a_lower_driver_floor_the_chain_does_high_pass_leaves_the_space_open() -> None:
    """#2760: the crossover is not the only filter on the branch.

    The same LF floor as above, on a bass-managed chain. The woofer's low end is
    high-passed at the sub corner AHEAD of the split — a filter no candidate
    proposes and none can remove — so the floor is honoured and the whole space
    stays proposable. Read off the crossover corner alone it did not: the
    offline search refused 1681 of 1681 candidates, the incumbent included, the
    first time a confirmed driver-safety profile was supplied, and could only
    run by withholding the woofer's declared floor entirely.

    The HF floor is still compared against the crossover corner, so this is the
    LF wall standing down and not the safety wall going away.
    """
    bass_managed = _bounds(
        declared_floor_hz_by_role={"tweeter": 1500.0, "woofer": 60.0},
        standing_highpass_hz_by_role={"woofer": 80.0},
    )
    admitted = _search(bounds=bass_managed)

    assert admitted.ranked
    assert ("incumbent", "below_declared_floor") not in admitted.refusals

    # The same walk the fixture produces with no LF floor declared at all,
    # byte-for-byte everywhere EXCEPT the bounds echo: crediting the standing
    # filter changed which candidates are legal and nothing else about the
    # search. The one permitted difference is the declaration itself, and the
    # artifact publishes both maps precisely so a reader can see which of
    # ``below_declared_floor``'s two causes a walk met.
    admitted_doc = prediction_document(admitted)
    control_doc = prediction_document(_search())
    assert admitted_doc.pop("bounds") != control_doc.pop("bounds")
    assert json.dumps(admitted_doc, sort_keys=True) == (
        json.dumps(control_doc, sort_keys=True)
    )

    # And the wall is still a wall: raise the woofer's floor above what the
    # chain stands at and every candidate is refused again, by the same name.
    exposed = _search(bounds=_bounds(
        declared_floor_hz_by_role={"tweeter": 1500.0, "woofer": 100.0},
        standing_highpass_hz_by_role={"woofer": 80.0},
    ))
    assert exposed.ranked == ()
    assert {why for _what, why in exposed.refusals} == {"below_declared_floor"}


def test_every_proposed_trim_is_inside_the_declared_range() -> None:
    """The grid is generated inside the bound, not generated and then refused."""
    bounds = _bounds()
    shortlist = _search(bounds=bounds)

    for entry in shortlist.ranked:
        gain_db = entry.candidate.gain_db_by_role["tweeter"]
        assert bounds.gain_db_range[0] <= gain_db <= bounds.gain_db_range[1]


def test_a_refused_point_is_named_and_never_silently_dropped() -> None:
    """An empty shortlist and a fully-refused one must not look the same."""
    shortlist = _search(bounds=_bounds(declared_floor_hz_by_role={"tweeter": 9000.0}))

    assert shortlist.ranked == ()
    assert shortlist.refusals
    assert all(why for _what, why in shortlist.refusals)
    assert {why for _what, why in shortlist.refusals} == {"below_declared_floor"}

    # The document groups them by reason with a count, so a wall that refuses
    # hundreds of points does not bury the rest of the artifact — and nothing
    # goes silent: the reason and its count are both named.
    grouped = prediction_document(shortlist)["refusals"]
    assert [entry["reason"] for entry in grouped] == ["below_declared_floor"]
    assert grouped[0]["count"] == len(shortlist.refusals)
    assert grouped[0]["example"]


def test_no_proposal_is_legal_when_the_band_is_empty() -> None:
    shortlist = _search(bounds=_bounds(fc_band_hz=None))

    assert shortlist.ranked == ()
    assert shortlist.bounds.proposable is False
    # The incumbent is still graded — "keep configured" needs a number too.
    assert shortlist.incumbent.score.total is not None


def test_the_flat_minimum_resolves_toward_the_incumbent(shortlist) -> None:
    """Inside epsilon the answer is "these are the same", so take the smaller change.

    Every candidate here scores identically by construction — the plants are
    angle-independent in shape, so the graded curve does not move — which makes
    the whole grid one flat minimum and puts the tie-break entirely in charge.
    """
    totals = [entry.score.total for entry in shortlist.ranked]

    assert len(totals) >= 2
    # The PRECONDITION is asserted, not tested for. Guarding the real assertion
    # behind `if spread <= epsilon` would make this test go vacuous the moment
    # the fixture changed, and a test that can silently assert nothing guards
    # nothing.
    assert max(totals) - min(totals) <= FLAT_MINIMUM_EPSILON_DB

    head = shortlist.ranked[0].candidate
    # The incumbent's own order and polarity win the tie before its corner
    # does: a different filter SHAPE is a bigger ask than a nudged corner.
    assert head.polarity_by_role["tweeter"] == 1
    assert next(iter(head.sections_by_role["tweeter"])).order == 4


def test_ranking_is_bounded_by_the_declared_shortlist_size() -> None:
    shortlist = _search(
        plan=SearchPlan(
            hf_role="tweeter", lf_role="woofer",
            fc_steps=3, delay_steps=3, gain_steps=3, refinement=1,
            shortlist_size=2,
        ),
    )

    assert len(shortlist.ranked) <= 2
    assert shortlist.evaluated > len(shortlist.ranked)


def test_the_refinement_pass_visits_points_the_coarse_pass_did_not() -> None:
    """A refinement that re-scored the coarse grid would be decoration."""
    coarse_only = _search(
        plan=SearchPlan(
            hf_role="tweeter", lf_role="woofer",
            fc_steps=3, delay_steps=3, gain_steps=3, refinement=0,
        ),
    )
    refined = _search(
        plan=SearchPlan(
            hf_role="tweeter", lf_role="woofer",
            fc_steps=3, delay_steps=3, gain_steps=3, refinement=2,
        ),
    )

    assert refined.evaluated > coarse_only.evaluated
    assert refined.landscape["coarse_evaluated"] == coarse_only.evaluated


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def test_the_document_prints_the_ranking_authority(shortlist) -> None:
    """The H5 guard rides the ARTIFACT, not only the docs.

    A consumer that reads the shortlist and never opens this module still has to
    meet the stamp, because the campaign's original error was acting on an
    ordering whose caveat lived somewhere else.
    """
    document = prediction_document(shortlist)

    stamp = document["ranking_authority"]
    assert stamp["authority"] == RANKING_AUTHORITY_BRACKET_ONLY
    assert stamp["measured_rho"] == DELAY_RANKING_MEASURED_RHO
    assert stamp["bar_rho"] == DELAY_RANKING_BAR_RHO
    assert "README-delay-arm-regrade" in stamp["evidence"]


def test_the_document_declares_its_vertical_blindness(shortlist) -> None:
    """A crossover's primary artifact is vertical, and no pose here sees it."""
    document = prediction_document(shortlist)

    assert "NOT MEASURED" in document["vertical_polar"]
    for entry in document["ranked"]:
        assert entry["score"]["directivity"]["coverage"] == "horizontal_only"


def test_the_document_states_the_currency_it_grades_in(shortlist) -> None:
    document = prediction_document(shortlist)

    assert document["currency"]["residual"] == (
        "flat_spec.spec_convergence_residual.rms_db"
    )
    assert document["currency"]["units"] == "dB"
    assert document["currency"]["lower_is_better"] is True


def test_the_document_carries_the_primary_views_chunk_table(shortlist) -> None:
    """Acceptance reads the profile, so the profile has to be IN the artifact."""
    document = prediction_document(shortlist)

    assert document["ranked"]
    for entry in document["ranked"]:
        primary = entry["score"]["primary_view"]
        views = {view["view"]: view for view in entry["score"]["views"]}
        assert views[primary]["profile"]["chunks"]
        # Every seat is present, uncollapsed, carrying its own worst chunk.
        assert len(entry["score"]["per_seat"]) == len(ANGLES)
        for seat in entry["score"]["per_seat"]:
            assert "worst_chunk_db" in seat


def test_the_document_is_json_serialisable(shortlist) -> None:
    document = prediction_document(shortlist)

    assert json.loads(json.dumps(document)) == document
