# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2609 — one measurement owns the level datum, and no threshold arbitrates it.

Replaces ``test_crossover_v2_level_frame_dispute.py``, whose subject was
deleted with the mechanism it pinned. That file pinned a three-case rule in
:func:`jasper.active_speaker.crossover_v2.intervention.anchor_trims` under which
two per-driver estimators voted and a 3.0 dB cliff decided whether their
reconciliation was admitted. This file pins that there is no vote left, that
the surviving check cannot move a number, and that the safety normalize the
migration ran past is intact.

**What went wrong, in one paragraph.** The anchor was
``base + giveback + level_frame_offset``, and the offset carried a
reconciliation between the trim solve's power-band average about Fc and the
fit's median over each driver's own radiating band. Those two routinely
disagree. When they disagreed past tolerance every offset was zeroed. On the
2026-08-16 shortfall round they disagreed by 3.326 dB against a 3.0 dB bar —
**a miss of 0.326 dB** — the zeroing fired, and the tweeter moved from a raw
measured trim of ``-10.835`` to a committed ``-7.043``, +3.79 dB hotter than
the measurement had asked for. The crossover band then realized +3.10 dB over
commanded, VERIFY's absolute check failed at -2.83 dB @ 1935 Hz, and the probe
rolled the round back.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.audio_measurement.program_analysis import (
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
)
# The planner harness, borrowed rather than forked: that module already owns
# the two stubs a `plan_linearization` run needs (the un-replayable fit and
# ripple seams) and the banked request they run against. A second copy here
# would be a second answer to "how do you drive the planner".
from tests.test_crossover_v2_intervention_dual_run import (
    _install_stubs,
    _planner_request,
    _sections_at,
)
from tests.test_crossover_v2_incident_replay import SELECTED_FC_HZ

# --------------------------------------------------------------------------- #
# the 2026-08-16 jts3 terms
# --------------------------------------------------------------------------- #

_ROLES = ("woofer", "tweeter")

#: The incident round's anchor terms, RECONSTRUCTED from the four numbers
#: #2609 states rather than copied from a bundle this repo does not hold.
#:
#: Stated on the issue: the raw measured tweeter trim ``-10.835`` and the
#: overlap-band estimate ``-6.893`` (the 12:27 comment), and the committed
#: ``-7.043`` with ``normalize_shift_db = 2.878`` (the conviction comment).
#: The woofer was the loud branch — it is what set the shift — so its
#: unnormalized term EQUALS the shift, and the rest follows::
#:
#:     giveback_t = committed + shift - base_used = -7.043 + 2.878 + 6.893
#:                = 2.728
#:     tba_w      = shift - giveback_t            = 0.150
#:     giveback_w = shift - tba_w                 = 2.728
#:
#: The reconstruction is checked rather than asserted: the first test below
#: replays the DELETED rule on these terms and gets the incident's own
#: ``-7.043`` back, and the move between the two rules comes out +3.792 dB
#: against the owner's stated +3.79. A fixture that reproduces the known bad
#: number under the old rule is the incident's; one that only produced the
#: good number under the new rule would prove nothing.
#:
#: ``scripts/derive-crossover-incident-fixture.py`` builds this shape from a
#: real bundle when one is on the laptop; these terms are the in-repo
#: equivalent, so the pin runs in CI with no gitignored evidence.
_RAW_TRIM_DB = {"woofer": 0.0, "tweeter": -10.835}
_TRIM_BAND_AVERAGE_DB = {"woofer": 0.150, "tweeter": -6.893}
_GIVEBACK_DB = {"woofer": 2.728, "tweeter": 2.728}

#: What the deleted rule committed, and what the measurement asked for.
_INCIDENT_COMMITTED_TWEETER_DB = -7.043
_MEASURED_TWEETER_DB = -10.8
#: The owner's conviction bar: the committed trim must land within 0.6 dB of
#: the measurement (the reigning tune's -10.214 sits inside the same window).
_INCIDENT_TOLERANCE_DB = 0.6


def _anchor(**overrides):
    kwargs = dict(
        roles=_ROLES,
        anchor_base_db=_RAW_TRIM_DB,
        giveback_db=_GIVEBACK_DB,
    )
    kwargs.update(overrides)
    return iv.anchor_trims(**kwargs)


# --------------------------------------------------------------------------- #
# (c) the contract: no branch keyed on a tolerance
# --------------------------------------------------------------------------- #


def test_anchor_trims_takes_no_tolerance_and_no_disagreement():
    """The three arbitration parameters are gone from the signature.

    A signature test rather than a behaviour one because the defect was
    structural: as long as ``anchor_trims`` can be TOLD a disagreement and a
    tolerance, some branch can compare them, and the cliff comes back. Its
    inputs are now exactly the two terms the anchor is made of.
    """
    params = set(inspect.signature(iv.anchor_trims).parameters)
    assert params == {"roles", "anchor_base_db", "giveback_db"}
    for gone in ("tolerance_db", "disagreement_db", "has_frame",
                 "level_frame_offset_db"):
        assert gone not in params


def test_anchor_trims_body_carries_no_threshold_comparison():
    """No surviving branch in the function compares against a tolerance.

    The signature test above closes the door the cliff came through; this one
    checks nobody re-opened it from inside by reaching for a module constant.
    Source inspection is the honest instrument here — the branch it forbids is
    one that fires on inputs a passing behaviour test would not supply.
    """
    body = inspect.getsource(iv.anchor_trims)
    _, _, code = body.partition('"""')
    _, _, code = code.partition('"""')
    for token in ("TOLERANCE", "tolerance", "disagreement"):
        assert token not in code, f"anchor_trims body still reaches for {token!r}"


def test_the_anchor_is_base_plus_giveback_and_nothing_else():
    anchored, shift = _anchor()
    unnormalized = {
        role: _RAW_TRIM_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    expected_shift = max(0.0, max(unnormalized.values()))
    assert shift == pytest.approx(expected_shift)
    for role in _ROLES:
        assert anchored[role] == pytest.approx(unnormalized[role] - expected_shift)


# --------------------------------------------------------------------------- #
# (a) the incident replay
# --------------------------------------------------------------------------- #


def test_the_fixture_reproduces_the_incidents_own_committed_trim():
    """The deleted rule, replayed on these terms, gives back its own -7.043.

    This is what makes the fixture the INCIDENT's rather than three numbers
    chosen to make the next test pass. The deleted third case zeroed every
    per-role offset and anchored on the overlap-band estimate, which is
    exactly ``place(trim_band_average + giveback)`` — reproduced here by hand
    because the branch that did it no longer exists to call.
    """
    unnormalized = {
        role: _TRIM_BAND_AVERAGE_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    shift = max(0.0, max(unnormalized.values()))
    committed = {r: v - shift for r, v in unnormalized.items()}
    assert shift == pytest.approx(2.878, abs=1e-3)
    assert committed["tweeter"] == pytest.approx(
        _INCIDENT_COMMITTED_TWEETER_DB, abs=1e-3
    )


def test_the_incident_round_commits_the_measured_tweeter_trim():
    """The 2026-08-16 shortfall round, replayed: the anchor lands near -10.8.

    The whole claim of the migration in one assertion. Under the deleted rule
    this pair committed -7.043 while the raw measurement asked for -10.835 and
    the reigning tune sat at -10.214. With no branch left to take, the anchor
    commits what the measurement said.
    """
    anchored, _ = _anchor()
    assert anchored["tweeter"] == pytest.approx(
        _MEASURED_TWEETER_DB, abs=_INCIDENT_TOLERANCE_DB
    )
    # And explicitly NOT the value the exclusion branch shipped.
    assert abs(
        anchored["tweeter"] - _INCIDENT_COMMITTED_TWEETER_DB
    ) > _INCIDENT_TOLERANCE_DB


def test_the_move_the_migration_removes_is_the_owners_stated_plus_3_79_db():
    """Old rule minus new rule == +3.79 dB, the number the forensic named."""
    unnormalized = {
        role: _TRIM_BAND_AVERAGE_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    old_shift = max(0.0, max(unnormalized.values()))
    old_tweeter = unnormalized["tweeter"] - old_shift
    new_tweeter, _ = _anchor()
    assert old_tweeter - new_tweeter["tweeter"] == pytest.approx(3.79, abs=0.01)


def test_no_definition_gap_can_change_the_committed_pair():
    """A capture whose two definitions part company ships the same trims.

    The behavioural half of "the comparison cannot move a number". Two
    verdicts as far apart as the tolerance allows, one committed pair.
    """
    baseline, baseline_shift = _anchor()
    agreeing = iv.compare_level_definitions(
        trim_band_average_db=_RAW_TRIM_DB,
        core_proposal_db=_RAW_TRIM_DB,
    )
    wild = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -30.0},
        core_proposal_db={"woofer": 0.0, "tweeter": 12.0},
    )
    assert agreeing is not None and not agreeing.differs
    assert wild is not None and wild.differs
    # Same inputs to the anchor, same answer, whatever the check said.
    again, again_shift = _anchor()
    assert again == baseline and again_shift == baseline_shift


# --------------------------------------------------------------------------- #
# (a2) the WIRING — which of the request's two estimates reaches the anchor
# --------------------------------------------------------------------------- #


def _anchored_through_the_planner(monkeypatch, **request_overrides):
    """One full ``plan_linearization`` run's anchored pair.

    Through the whole planner rather than :func:`anchor_trims` directly,
    because the wiring is the fact under test: everything above pins what the
    anchor DOES with a base, and none of it can see which base the planner
    picks up.
    """

    _install_stubs(monkeypatch, scan_delta_db=0.0)
    request = _planner_request(_sections_at(SELECTED_FC_HZ))
    plan = iv.plan_linearization(replace(request, **request_overrides))
    return dict(plan.trim.anchored_db)


def test_the_planner_anchors_on_the_raw_trim_and_not_the_band_average(monkeypatch):
    """#2662 G4: the level datum's owner, pinned at the seam that chooses it.

    ``plan_linearization`` is handed BOTH per-driver level estimates on one
    request — ``raw_trim_db`` and the subordinate ``trim_band_average_db`` —
    and passes exactly one of them to the anchor. Which one is the fact this
    repo's prose got backwards at three sites in a single commit
    (``9b27f2716``, whose subject asserted the wrong half too), so an argument
    about it is worth nothing and a test is worth having.

    Stated as two runs rather than as arithmetic, so it cannot pass by
    re-deriving ``base + giveback`` the way production does: moving the
    SUBORDINATE estimate must change nothing, and moving the OWNER by the same
    amount must change the answer. A planner wired to the band average fails
    both halves at once.
    """

    on_owner = _anchored_through_the_planner(
        monkeypatch,
        raw_trim_db=_RAW_TRIM_DB,
        trim_band_average_db=_TRIM_BAND_AVERAGE_DB,
    )

    # (i) the subordinate estimate moves the full 3.942 dB; the datum does not.
    subordinate_moved = _anchored_through_the_planner(
        monkeypatch,
        raw_trim_db=_RAW_TRIM_DB,
        trim_band_average_db=_RAW_TRIM_DB,
    )
    assert subordinate_moved == pytest.approx(on_owner)

    # (ii) the owner moves by that same amount, and the anchor follows it.
    owner_moved = _anchored_through_the_planner(
        monkeypatch,
        raw_trim_db=_TRIM_BAND_AVERAGE_DB,
        trim_band_average_db=_TRIM_BAND_AVERAGE_DB,
    )
    assert owner_moved["tweeter"] != pytest.approx(on_owner["tweeter"])


# --------------------------------------------------------------------------- #
# the safety invariant the migration ran past
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "giveback",
    [
        {"woofer": 0.0, "tweeter": 0.0},
        {"woofer": 2.811, "tweeter": 6.717},
        # Give-back far exceeding the raw attenuation on BOTH roles: the case
        # the normalize exists for.
        {"woofer": 40.0, "tweeter": 40.0},
    ],
)
def test_no_committed_trim_is_ever_a_boost(giveback):
    """Every returned trim is <= 0. The hearing-safety invariant, unchanged.

    The migration moved where the anchor's base number comes from. It did not
    touch the clamp around it, and this is what says so: a branch whose own
    cuts give back more than its raw attenuation must still land non-positive,
    because the emitter refuses a positive trim and the hardware must never
    see one.
    """
    anchored, shift = _anchor(giveback_db=giveback)
    assert shift >= 0.0
    for role, value in anchored.items():
        assert value <= 0.0, f"{role} committed a boost: {value}"


def test_the_normalize_preserves_relative_placement_exactly():
    """The shift is common-mode, so it cannot change the inter-driver balance."""
    anchored, shift = _anchor()
    hot, hot_shift = _anchor(
        giveback_db={role: v + 12.0 for role, v in _GIVEBACK_DB.items()}
    )
    assert hot_shift == pytest.approx(shift + 12.0)
    spread = anchored["tweeter"] - anchored["woofer"]
    assert hot["tweeter"] - hot["woofer"] == pytest.approx(spread)


# --------------------------------------------------------------------------- #
# the consistency check: symmetric, advisory, three-state
# --------------------------------------------------------------------------- #


def test_the_tolerance_is_the_realized_level_one_and_is_owned_once():
    """One DISCLOSURE TRIGGER, one owner — and it is the number it always was."""
    assert iv.LEVEL_ESTIMATOR_TOLERANCE_DB == REALIZED_LEVEL_MATCH_TOLERANCE_DB
    default = inspect.signature(
        iv.compare_level_definitions
    ).parameters["tolerance_db"].default
    assert default == iv.LEVEL_ESTIMATOR_TOLERANCE_DB


@pytest.mark.parametrize(
    "name",
    ["LevelFrameAdmission", "LEVEL_FRAME_DISPUTED_REASON"],
)
def test_the_arbitration_vocabulary_is_gone(name):
    assert not hasattr(iv, name)
    assert name not in iv.__all__


@pytest.mark.parametrize(
    "name", ["SharedLevelFrame", "solve_shared_level_frame"],
)
def test_the_frame_solver_is_gone(name):
    from jasper.active_speaker import linearization_fit

    assert not hasattr(linearization_fit, name)


# --------------------------------------------------------------------------- #
# the consistency check: estimator-vs-estimator, advisory, three-state
# --------------------------------------------------------------------------- #


def test_an_empty_estimate_means_no_verdict_not_an_agreement():
    """``None`` is a third state, not a quiet synonym for "they agreed"."""
    assert iv.compare_level_definitions(
        trim_band_average_db={}, core_proposal_db=_RAW_TRIM_DB,
    ) is None
    assert iv.compare_level_definitions(
        trim_band_average_db=_RAW_TRIM_DB, core_proposal_db={},
    ) is None


def test_neither_definition_is_preferred():
    """The ruling, as a test: the comparison reports a DISTANCE, not a winner.

    #2609 carries two owner comments pointing opposite ways on the same numeric
    pair, and ruling S8 explains why: each was right about a DIFFERENT quantity.
    The handover level is the level fact and the passband estimate is the
    starting estimate; neither places the pair, and swapping which one carries
    the offset must not change what is reported.
    """
    a = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -2.0},
        core_proposal_db={"woofer": 0.0, "tweeter": -10.0},
    )
    b = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -10.0},
        core_proposal_db={"woofer": 0.0, "tweeter": -2.0},
    )
    assert a is not None and b is not None
    assert a.differs is b.differs is True
    assert a.worst_delta_db == pytest.approx(b.worst_delta_db)
    assert a.estimator_delta_db == pytest.approx(b.estimator_delta_db)


def test_the_comparison_is_relative_not_absolute():
    """A common-mode offset between the two is not a difference worth reporting.

    The two definitions carry different references — a power-band average over
    the mirrored halves about Fc, and a median over the driver's own core band
    — so only their PLACEMENT of the pair is comparable, which is also the only
    thing the anchor commits.
    """
    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -10.0},
        core_proposal_db={"woofer": -25.0, "tweeter": -35.0},
    )
    assert verdict is not None
    assert verdict.worst_delta_db == pytest.approx(0.0)
    assert not verdict.differs


def test_a_role_only_one_definition_covered_is_not_a_difference():
    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -40.0},
        core_proposal_db={"woofer": 0.0},
    )
    assert verdict is not None
    assert not verdict.differs
    assert set(verdict.estimator_delta_db) == {"woofer"}


def test_a_verdict_exactly_at_the_bar_is_not_disclosed():
    """The boundary is `>`, so exactly-at-tolerance is not a finding."""
    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -10.0},
        core_proposal_db={
            "woofer": 0.0,
            "tweeter": -10.0 + iv.LEVEL_ESTIMATOR_TOLERANCE_DB,
        },
    )
    assert verdict is not None
    assert not verdict.differs
    assert verdict.reason == ""


def test_the_incident_gap_discloses_and_ships_anyway():
    """3.326 dB against a 3.0 dB bar: disclosed, and the pair ships unchanged.

    The same magnitude that took the deleted third branch. The verdict still
    fires — S8 did not widen the bar, it corrected what the number MEANS — and
    what it produces is a disclosure. The anchor is the raw measured trim
    either way.
    """
    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -10.835},
        core_proposal_db={"woofer": 0.0, "tweeter": -10.835 + 3.326},
    )
    assert verdict is not None
    assert verdict.differs
    assert verdict.reason == iv.LEVEL_DEFINITIONS_DIFFER_REASON
    assert verdict.worst_delta_db == pytest.approx(3.326, abs=1e-6)
    anchored, _ = _anchor()
    assert anchored["tweeter"] == pytest.approx(
        _MEASURED_TWEETER_DB, abs=_INCIDENT_TOLERANCE_DB
    )


def test_the_verdict_states_which_axis_the_levels_were_read_on():
    """Toole's constraint, as a pin: a level without its axis is unplaceable.

    Where woofer beaming and horn directivity mismatch, the on-axis,
    listening-window and power-response ratios differ and there is NO single
    correct level — so a tool that matched one and did not say which published
    a number whose meaning the reader cannot recover.

    The value is derived from where the capture was taken, not chosen: the
    level fact is computed from the MEASURE capture, and MEASURE is a capture
    with no prompted move of its own, which ``spatial._DESIGN_AXIS_GEOMETRY``
    names a design-axis capture at ``degrees=0``. This asserts the axis the
    verdict reports IS that geometry's bearing, so a future regime that moves
    the capture off axis cannot leave this label behind saying otherwise.
    """
    from jasper.active_speaker.crossover_v2 import spatial

    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -2.0},
        core_proposal_db={"woofer": 0.0, "tweeter": -10.0},
    )
    assert verdict is not None
    assert verdict.matched_axis == iv.LEVEL_MATCH_AXIS
    assert verdict.to_dict()["matched_axis"] == iv.LEVEL_MATCH_AXIS
    assert str(spatial._DESIGN_AXIS_GEOMETRY.degrees) in iv.LEVEL_MATCH_AXIS


def test_the_two_tolerances_answer_different_questions_and_both_stay():
    """The disclosure trigger is not the setting precision, and cannot become it.

    Two numbers, two jobs, and someone will try to collapse them. **3.0 dB is
    when to TELL someone** the two level definitions parted company.
    **<=0.5 dB is how precisely the trim must LAND** once the level fact is
    known — and it is already met by construction, because the trim search
    steps five times finer than that.

    Pinned as an inequality rather than two equalities so it says the RELATION
    that matters: the disclosure trigger must stay well above the setting
    precision. A future edit that narrows 3.0 toward 0.5 turns this red, which
    is exactly the collapse the plan warns about.
    """
    from jasper.audio_measurement.program_analysis import RIPPLE_TRIM_SEARCH_STEP_DB

    setting_precision_db = 0.5
    assert RIPPLE_TRIM_SEARCH_STEP_DB <= setting_precision_db
    assert iv.LEVEL_ESTIMATOR_TOLERANCE_DB > setting_precision_db


def test_the_verdict_serializes_flat_for_the_journal():
    verdict = iv.compare_level_definitions(
        trim_band_average_db={"woofer": 0.0, "tweeter": -2.0},
        core_proposal_db={"woofer": 0.0, "tweeter": -10.0},
    )
    assert verdict is not None
    payload = verdict.to_dict()
    assert set(payload) == {
        "differs", "reason", "matched_axis", "tolerance_db", "worst_delta_db",
        "estimator_delta_db",
    }
    assert payload["differs"] is True
    assert payload["worst_delta_db"] == pytest.approx(8.0)


def test_the_summed_owner_plumbing_is_gone_not_dormant():
    """The deletion test, as a test.

    An earlier cut of this migration made the summed at-the-mark capture the
    level owner. It is unreachable in production — the session that holds the
    baseline never plans (the VERIFY map has no MEASURE), the session that
    plans never holds one (stage 1 captures the baseline AFTER the fit, and
    hydrate passes none) — and the two captures are in different frames anyway:
    the per-branch sweeps ride the protected-NEUTRAL graph while the baseline
    rides the applied incumbent, so combining them double-counts the
    incumbent's trims. Fixture-only code is not kept as aspiration.
    """
    from dataclasses import fields

    from jasper.audio_measurement import program_analysis

    assert not hasattr(program_analysis, "summed_level_reference_db")
    names = {f.name for f in fields(iv.LinearizationRequest)}
    assert "summed_level_reference_db" not in names
    assert "level_frame_tolerance_db" not in names
    assert "summed_level_reference_db" not in {
        f.name for f in fields(iv.LinearizationPlan)
    }
