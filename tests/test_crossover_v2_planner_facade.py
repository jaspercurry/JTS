# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 1: the planner facade and the one commit seam it hangs off.

The parity assertions run a REAL conductor walk rather than a hand-built
proposal, because the claim under test is "the proposal describes what the
legacy path actually installed" — and a fixture assembled by the same author
who wrote the facade can agree with it while production does not.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.branch_chain import CrossoverSection, sections_by_role
from jasper.active_speaker.crossover_v2 import (
    PROPOSAL_CREATED_EVENT,
    PROPOSAL_REFUSED_EVENT,
    InterventionProposal,
    PlanRefusal,
    TrimStrategy,
    build_intervention_proposal,
    plan_intervention_proposal,
    trim_strategy_for_outcome,
)
from jasper.active_speaker.crossover_v2 import planner_facade
from jasper.active_speaker.fc_selector import FcCandidateEvaluation
from tests.test_crossover_v2_conductor import (
    FakeSeams,
    _cloud_conductor,
    _comb_cloud_analysis_factory,
    _eligible_measure_analysis,
    _walk_measure_cloud_to_close,
)


def _walked() -> tuple[flow.CrossoverV2Conductor, dict]:
    """One real MEASURE→cloud walk, closed to a committed candidate."""

    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    conductor = _cloud_conductor(fakes)
    verdict = _walk_measure_cloud_to_close(conductor)
    return conductor, verdict


# --------------------------------------------------------------------------
# parity with what the legacy path installed
# --------------------------------------------------------------------------


def test_a_committed_candidate_publishes_a_proposal_describing_it():
    conductor, verdict = _walked()
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)

    # The proposal names the candidate the walk actually published…
    assert proposal.candidate is conductor.candidate
    assert proposal.candidate_fingerprint == verdict["candidate_fingerprint"]
    # …and carries the same predicted sum / commanded delta the conductor
    # installed, rather than a re-derived copy of them.
    assert proposal.predicted_response_after is not None
    np.testing.assert_allclose(
        proposal.predicted_response_after.db, conductor.measure_predicted_sum[1]
    )
    assert proposal.commanded_delta is not None
    np.testing.assert_allclose(
        proposal.commanded_delta.db, conductor.measure_commanded_delta[1]
    )
    assert (
        proposal.predicted_spec_after == conductor.measure_predicted_spec_report
    )
    assert proposal.linearization_filters == dict(conductor.candidate.linearization)
    assert proposal.evidence_identities["session_id"] == conductor.session_id


def test_the_proposal_corner_comes_from_the_candidate_not_the_session():
    """The single-source-of-truth claim, checked against the candidate's preset.

    Every section in the context must name the same corner, and that corner
    must be the one the candidate's OWN preset declares — never a session
    field.
    """
    conductor, _ = _walked()
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)

    expected = sections_by_role(proposal.candidate.source_preset.crossover_regions)
    assert proposal.context.sections_by_role == expected
    corners = {
        float(section.fc_hz)
        for sections in expected.values()
        for section in sections
    }
    assert corners == {proposal.fc_hz}


def test_the_walk_path_records_the_realized_level_evidence_it_owns():
    conductor, _ = _walked()
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert proposal.realized_branch_level == (
        conductor._last_realized_level_match.to_dict()
    )


def test_the_predicted_spec_before_is_honestly_empty_until_entry_baseline():
    """No pre-apply summed baseline exists yet, so the field says so.

    ``STAGE1_INCLUDES_CLOUD_MEASURE`` is false by design and #2291 Phase 3 adds
    the ``entry_baseline`` capture. Until then the contract must expose the gap
    rather than quietly omitting the field.
    """
    conductor, _ = _walked()
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert proposal.predicted_spec_before == {}


# --------------------------------------------------------------------------
# one seam, both paths
# --------------------------------------------------------------------------


def test_the_walk_commits_through_the_named_seam(monkeypatch):
    seen: list[tuple] = []
    original = flow.CrossoverV2Conductor.commit_intervention_proposal

    def spy(self, candidate, **kwargs):
        seen.append((candidate, kwargs))
        return original(self, candidate, **kwargs)

    monkeypatch.setattr(
        flow.CrossoverV2Conductor, "commit_intervention_proposal", spy
    )
    conductor, _ = _walked()
    assert len(seen) == 1
    assert seen[0][0] is conductor.candidate


def test_the_selected_fc_path_commits_through_the_same_seam(monkeypatch):
    """#2291 Phase 2's acceptance criterion, pinned one phase early.

    Configured-Fc and alternative-Fc candidates must traverse one code path.
    The evaluation is built from a REAL candidate produced by a real walk, so
    this exercises the production commit rather than a synthetic shape.
    """
    conductor, _ = _walked()
    real = conductor.candidate

    seen: list[tuple] = []
    original = flow.CrossoverV2Conductor.commit_intervention_proposal

    def spy(self, candidate, **kwargs):
        seen.append((candidate, kwargs))
        return original(self, candidate, **kwargs)

    monkeypatch.setattr(
        flow.CrossoverV2Conductor, "commit_intervention_proposal", spy
    )
    grid = np.array([100.0, 200.0])
    evaluation = FcCandidateEvaluation(
        fc_hz=float(conductor.last_intervention_proposal.fc_hz),
        freqs_hz=grid,
        branch_operator_by_role={},
        anchor_sum_db=np.zeros(2),
        scoring_band_hz=(100.0, 200.0),
        candidate=real.to_dict(),
        predicted_sum=(grid, np.array([-1.0, -2.0])),
        predicted_spec_report={"overall_passed": False},
        commanded_delta=(grid, np.array([0.5, 0.5])),
        level_frame_finding=None,
    )
    conductor._commit_fc_candidate(evaluation)

    assert len(seen) == 1
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert proposal.candidate_fingerprint == real.fingerprint
    assert conductor.measure_predicted_spec_report == {"overall_passed": False}


def test_the_selected_fc_path_records_no_realized_level_evidence():
    """It must not borrow the anchor's.

    ``_sweep_fc_candidates`` restores ``_FC_SWEEP_CONDUCTOR_FIELDS`` when it
    ends, so ``_last_realized_level_match`` belongs to the anchor by the time
    the selected candidate commits, and ``FcCandidateEvaluation`` carries none
    of its own. Reading it would be the same cross-context leak in a new
    place; the proposal reports the absence instead.
    """
    conductor, _ = _walked()
    real = conductor.candidate
    assert conductor._last_realized_level_match is not None  # the anchor's

    grid = np.array([100.0, 200.0])
    conductor._commit_fc_candidate(
        FcCandidateEvaluation(
            fc_hz=float(conductor.last_intervention_proposal.fc_hz),
            freqs_hz=grid,
            branch_operator_by_role={},
            anchor_sum_db=np.zeros(2),
            scoring_band_hz=(100.0, 200.0),
            candidate=real.to_dict(),
            predicted_sum=(grid, np.array([-1.0, -2.0])),
            commanded_delta=None,
            level_frame_finding=None,
        )
    )
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert proposal.realized_branch_level == {}


def test_no_proposal_exists_before_a_candidate_is_committed():
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    fakes.verify = _comb_cloud_analysis_factory()
    assert _cloud_conductor(fakes).last_intervention_proposal is None


# --------------------------------------------------------------------------
# trim vocabulary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("fitted", TrimStrategy.COMMITTED_PAIR_UNRECORDED),
        (
            "trim_rejected",
            TrimStrategy.COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT,
        ),
        ("fit_failed", TrimStrategy.NOT_FITTED),
        ("ineligible_mic_tier", TrimStrategy.NOT_FITTED),
        ("", TrimStrategy.NOT_FITTED),
        (None, TrimStrategy.NOT_FITTED),
    ],
)
def test_the_legacy_outcome_maps_onto_an_honest_strategy(outcome, expected):
    strategy, rationale = trim_strategy_for_outcome(outcome)
    assert strategy is expected
    assert rationale


def test_a_drifted_scan_is_never_reported_as_a_rejected_trim():
    """The 2026-08-10 contradiction, refused by vocabulary.

    ``trim_rejected`` recorded a drift observation while the −13.013 dB trim
    was committed. The mapped strategy must say a pair was committed, and its
    rationale must disclose that the artifact does not name which one.
    """
    strategy, rationale = trim_strategy_for_outcome("trim_rejected")
    assert "committed" in strategy.value and "reject" not in strategy.value
    assert "no trim was rejected" in rationale
    assert "Phase 2" in rationale


# --------------------------------------------------------------------------
# events and refusal
# --------------------------------------------------------------------------


def test_committing_a_candidate_emits_exactly_one_proposal_event(caplog):
    """The seam discloses once per commit — not zero times, and not twice.

    Asserted deliberately because it is otherwise invisible: whether this
    record exists depends on the effective capture level, so a test that only
    counts events around a later direct call silently passes whether or not
    the production seam logs at all.
    """
    with caplog.at_level(logging.INFO, logger=planner_facade.__name__):
        conductor, _ = _walked()
    created = [r for r in caplog.records if PROPOSAL_CREATED_EVENT in r.getMessage()]
    assert len(created) == 1
    proposal = conductor.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert f"proposal_fingerprint={proposal.fingerprint}" in created[0].getMessage()


def test_creating_a_proposal_emits_one_stable_event(caplog):
    conductor, _ = _walked()
    with caplog.at_level(logging.INFO, logger=planner_facade.__name__):
        # The walk above already emitted its own commit event, and whether
        # caplog kept it depends on the ambient level — so measure only this
        # call rather than however many records happen to be in the buffer.
        caplog.clear()
        proposal = plan_intervention_proposal(conductor.candidate, session_id="cap_x")
    assert isinstance(proposal, InterventionProposal)
    lines = [r for r in caplog.records if PROPOSAL_CREATED_EVENT in r.getMessage()]
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert f"proposal_fingerprint={proposal.fingerprint}" in message
    assert f"candidate_fc_hz={round(proposal.fc_hz, 6)}" in message
    assert f"trim_strategy={proposal.trim_strategy.value}" in message
    assert f"candidate_fingerprint={proposal.candidate_fingerprint}" in message


def test_the_committed_proposal_fingerprint_covers_the_curves_it_carries():
    """Two proposals over the same candidate, differing only in the predicted
    curves the conductor installed, must not share a digest.

    This is the same property the parametrized contract test asserts, checked
    once here against a REAL candidate — a fingerprint that quietly ignored the
    predicted response would pass the synthetic test if the synthetic curves
    were the part it ignored.
    """
    conductor, _ = _walked()
    committed = conductor.last_intervention_proposal
    assert isinstance(committed, InterventionProposal)
    bare = plan_intervention_proposal(conductor.candidate, session_id="cap_x")
    assert isinstance(bare, InterventionProposal)
    assert bare.candidate_fingerprint == committed.candidate_fingerprint
    assert bare.fingerprint != committed.fingerprint


def test_a_candidate_with_no_crossover_refuses_instead_of_raising(caplog):
    """Phase 1 is additive, so a contract failure must not fail the session.

    ``plan_intervention_proposal`` returns a named refusal and logs it; only
    ``build_intervention_proposal`` raises, and nothing in the live conductor
    calls that directly.
    """

    class _NoCrossover:
        fingerprint = "e" * 64
        program_id = "prog"
        linearization: dict = {}
        linearization_outcome = "fitted"
        exclusion_evidence: dict = {}
        source_preset = None

    with caplog.at_level(logging.WARNING, logger=planner_facade.__name__):
        refused = plan_intervention_proposal(_NoCrossover(), session_id="cap_y")
    assert isinstance(refused, PlanRefusal)
    assert refused.reason == "no_crossover_sections"
    assert PROPOSAL_REFUSED_EVENT in caplog.text
    assert "reason=no_crossover_sections" in caplog.text


def test_a_split_corner_candidate_refuses_by_its_own_named_reason():
    class _Preset:
        crossover_regions = ()

    class _Split(_Preset):
        pass

    candidate = _Split()
    candidate.fingerprint = "f" * 64  # type: ignore[attr-defined]
    candidate.program_id = "prog"  # type: ignore[attr-defined]
    candidate.linearization = {}  # type: ignore[attr-defined]
    candidate.linearization_outcome = "fitted"  # type: ignore[attr-defined]
    candidate.exclusion_evidence = {}  # type: ignore[attr-defined]
    candidate.source_preset = _Preset()  # type: ignore[attr-defined]

    # Two roles disagreeing about the corner is the mixed-Fc defect; the facade
    # must name it rather than collapsing it into a generic invalid.
    monkey = {
        "woofer": (CrossoverSection(fc_hz=1648.7, order=4, highpass=False),),
        "tweeter": (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),),
    }
    original = planner_facade._candidate_sections
    try:
        planner_facade._candidate_sections = lambda _c: monkey  # type: ignore[assignment]
        refused = plan_intervention_proposal(candidate)
    finally:
        planner_facade._candidate_sections = original  # type: ignore[assignment]
    assert isinstance(refused, PlanRefusal)
    assert refused.reason == "candidate_fc_disagreement"


def test_build_raises_where_plan_refuses():
    with pytest.raises(Exception) as excinfo:
        build_intervention_proposal(None)
    assert "measured candidate" in str(excinfo.value)
