# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2392: the production constructor that assembles an ``InterventionProposal``.

:mod:`tests.test_crossover_v2_contracts` pins the proposal as a *type* — its
invariants and its fingerprint. This module pins the ASSEMBLER: which values it
reads, which strategy it can honestly claim from the artifact it has, and what
it does when a candidate cannot satisfy the contract.

The receipt end of the same wiring is
:mod:`tests.test_crossover_v2_round_wiring` section 3b, where it belongs,
because that is a fact about a durable write and is driven through the real
two-stage host.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.candidates import LinearizationState
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateFcDisagreementError,
    CrossoverV2ContractError,
    InterventionProposal,
    NoCrossoverSectionsError,
    PlanRefusal,
    TrimStrategy,
)
from jasper.active_speaker.crossover_v2.plan_assembly import LinearizationPlan
from jasper.active_speaker.crossover_v2.proposal import (
    PROPOSAL_CREATED_EVENT,
    PROPOSAL_REFUSED_EVENT,
    build_intervention_proposal,
    plan_intervention_proposal,
    trim_strategy_for_outcome,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset

from tests.test_active_speaker_profile import _two_way_preset

FC_HZ = 1648.7


def _preset(*, fc_hz: float = FC_HZ) -> ActiveSpeakerPreset:
    raw = copy.deepcopy(_two_way_preset())
    raw["crossover_regions"][0]["fc_hz"] = fc_hz
    return ActiveSpeakerPreset.from_mapping(raw)


def _candidate(**kwargs: object) -> MeasuredCrossoverCandidate:
    fields: dict = {
        "program_id": "prog-abc123",
        "analysis": {"drift_ppm": 12.5},
        "source_preset": _preset(),
        "role_attenuations_db": {"woofer": 0.0, "tweeter": -13.01298},
    }
    fields.update(kwargs)
    return MeasuredCrossoverCandidate(**fields)


# --------------------------------------------------------------------------- #
# 1. the trim strategy the artifact can honestly support
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _FakeMatch:
    """``decide_trim``'s graded pair, reduced to the field the policy reads."""

    difference_db: float
    matched: bool = True
    level_w_db: float = 0.0
    level_t_db: float = 0.0
    tolerance_db: float = 3.0
    woofer_band_hz: tuple[float, float] = (100.0, 1000.0)
    tweeter_band_hz: tuple[float, float] = (2000.0, 16000.0)


def _decision(*, drift_db: float, anchor_error: float, scan_error: float):
    anchored = {"woofer": -1.0, "tweeter": -6.0}
    return iv.decide_trim(
        anchored_db=anchored,
        resolved_db={"woofer": -1.0, "tweeter": -6.0 - drift_db},
        tweeter_role="tweeter",
        anchored_match=_FakeMatch(anchor_error),
        resolved_match=_FakeMatch(scan_error),
        ripple_db=0.4,
    )


def test_the_drift_outcome_string_and_the_drift_strategy_are_the_same_bit():
    """#2392's licence to derive a PRECISE member from the artifact string.

    :func:`trim_strategy_for_outcome` maps ``"trim_rejected"`` onto
    :attr:`TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT` rather than onto
    an "unrecorded" member, and that is only honest if the string and the
    strategy are the same fact. They are, by construction — ``outcome`` is
    ``beyond_sanity_margin`` and ``decide_trim`` takes the anchor branch on
    exactly that predicate — so this sweeps BOTH directions across the margin
    and both level orderings.

    Deleting ``COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT`` rests on this
    test; if the planner's policy ever decouples the two, this fails and the
    mapping has to go back to stating the gap.
    """
    margin = iv.LINEARIZATION_TRIM_SANITY_MARGIN_DB
    for drift_db in (0.0, 1.0, margin - 0.001, margin, margin + 0.001, 12.0, 40.0):
        for anchor_error, scan_error in ((0.1, 5.0), (5.0, 0.1), (1.0, 1.0)):
            decision = _decision(
                drift_db=drift_db, anchor_error=anchor_error, scan_error=scan_error
            )
            drifted = decision.outcome == "trim_rejected"
            assert drifted is decision.beyond_sanity_margin
            assert drifted is (
                decision.strategy is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
            ), "the outcome string and the drift strategy must not decouple"
            # …and the derived member agrees with the live one on that branch.
            derived, _why = trim_strategy_for_outcome(decision.outcome)
            if drifted:
                assert derived is decision.strategy


def test_a_fitted_artifact_cannot_say_which_pair_won_and_does_not_pretend_to():
    """The other branch is genuinely ambiguous, so it stays ambiguous.

    Within the margin the commit is decided by the realized-level grading,
    which the ``linearization_outcome`` string does not encode — the same
    string covers an anchored commit and a resolved one. Narrowing it to either
    precise member would be a guess printed as a record.
    """
    strategies = set()
    for anchor_error, scan_error in ((0.1, 5.0), (5.0, 0.1)):
        decision = _decision(
            drift_db=1.0, anchor_error=anchor_error, scan_error=scan_error
        )
        assert decision.outcome == "fitted"
        strategies.add(decision.strategy)
    assert strategies == {
        TrimStrategy.ANCHORED_COMMITTED,
        TrimStrategy.RESOLVED_COMMITTED,
    }, "one string really does cover two different commits"

    derived, why = trim_strategy_for_outcome("fitted")
    assert derived is TrimStrategy.COMMITTED_PAIR_UNRECORDED
    assert derived not in strategies
    assert "does not record which pair" in why


def _plan(decision) -> LinearizationPlan:
    """One plan around one real decision.

    Every field :meth:`LinearizationState.from_plan` does not read is left at a
    placeholder; the round-trip under test is the trim decision's.
    """
    return LinearizationPlan(
        fc_hz=FC_HZ,
        role_attenuations_db=dict(decision.committed_db),
        linearization={},
        trim=decision,
        core_level_evidence={},
        trim_band_estimate_db={},
        polish_delta_db={},
        level_consistency=None,
        linearized_predicted_sum=(np.array([FC_HZ]), np.array([0.0])),
        summation_frame=None,
        radiating_band_hz={},
    )


@pytest.mark.parametrize(
    ("anchor_error", "scan_error", "expected"),
    [
        (5.0, 0.1, TrimStrategy.RESOLVED_COMMITTED),
        (0.1, 5.0, TrimStrategy.ANCHORED_COMMITTED),
    ],
)
def test_the_committed_pair_and_its_drift_survive_the_commit_seam(
    anchor_error, scan_error, expected,
):
    """What the build decided reaches the proposal, so nothing is guessed.

    Both in-margin commits share one ``linearization_outcome`` string, so the
    string alone cannot tell them apart. The state carries the strategy and the
    drift it turned on; a state holding no such record still collapses to
    :attr:`TrimStrategy.COMMITTED_PAIR_UNRECORDED`.
    """
    decision = _decision(
        drift_db=1.0, anchor_error=anchor_error, scan_error=scan_error
    )
    assert decision.strategy is expected
    assert decision.outcome == "fitted"

    state = LinearizationState.from_plan(_plan(decision))
    assert state.trim_strategy is expected
    assert state.anchor_drift_db == pytest.approx(decision.anchor_drift_db)

    strategy, why = trim_strategy_for_outcome(state.outcome, linearization=state)
    assert strategy is expected
    assert f"{decision.anchor_drift_db:.3f}" in why, (
        "the drift the strategy turned on has to be recoverable from the "
        "proposal, which holds no other slot for it"
    )

    unrecorded = LinearizationState(outcome=state.outcome)
    assert trim_strategy_for_outcome(
        unrecorded.outcome, linearization=unrecorded,
    )[0] is TrimStrategy.COMMITTED_PAIR_UNRECORDED


@pytest.mark.parametrize("outcome", ["", "ineligible_mic_tier", "fit_failed", None])
def test_a_non_planning_outcome_is_not_fitted(outcome):
    """The three outcomes the conductor decides without planning at all."""
    strategy, why = trim_strategy_for_outcome(outcome)
    assert strategy is TrimStrategy.NOT_FITTED
    assert why


def test_the_unrecorded_drift_member_is_gone_and_referenced_nowhere():
    """#2392's half of the kept-types ledger, proven rather than asserted.

    ``COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT`` was kept by #2291 Phase
    5c-iii only until this issue decided whether an artifact-derived drift path
    came back. It did not — the drift case derives a PRECISE member (the test
    above) — so the member is dead vocabulary and is deleted. A name that
    survives its last producer is how a future reader concludes the pipeline
    can still say something it cannot.
    """
    assert not hasattr(TrimStrategy, "COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT")
    assert "committed_pair_unrecorded_after_sanity_drift" not in {
        member.value for member in TrimStrategy
    }

    root = Path(__file__).resolve().parent.parent
    stale = []
    for path in list((root / "jasper").rglob("*.py")) + list(
        (root / "tests").rglob("*.py")
    ):
        if path.resolve() == Path(__file__).resolve():
            continue  # this test names it in order to bury it
        if "COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT" in path.read_text(
            encoding="utf-8"
        ):
            stale.append(str(path.relative_to(root)))
    assert stale == [], f"deleted member still referenced in {stale}"


# --------------------------------------------------------------------------- #
# 2. the assembler
# --------------------------------------------------------------------------- #


def test_the_proposal_reads_its_corner_from_the_candidates_own_preset():
    """The 2026-08-10 defect's shape, refused by where the context comes from.

    The corner is derived from the candidate's own ``source_preset``, so there
    is no session field to disagree with it. A proposal built from a 1,648.7 Hz
    candidate names 1,648.7 Hz whatever else is going on.
    """
    proposal = build_intervention_proposal(_candidate())
    assert proposal.fc_hz == pytest.approx(FC_HZ)
    assert proposal.context.fc_hz == pytest.approx(FC_HZ)
    assert proposal.candidate_fingerprint == _candidate().fingerprint


def test_the_proposal_carries_the_evidence_the_commit_seam_holds():
    """Every value the seam has reaches the contract, and the digest covers it."""
    candidate = _candidate(
        linearization={"filters": [{"freq_hz": 900.0}]},
        linearization_outcome="fitted",
        exclusion_evidence={"bands": [[40.0, 60.0]]},
    )
    proposal = build_intervention_proposal(
        candidate,
        accountability={"finding": "level_frame_disagreement"},
        realized_branch_level={"difference_db": -1.6},
        evidence_identities={"session_id": "cap_1"},
    )
    assert proposal.trim_strategy is TrimStrategy.COMMITTED_PAIR_UNRECORDED
    assert proposal.linearization_filters == candidate.linearization
    assert proposal.excluded_regions == candidate.exclusion_evidence
    assert proposal.accountability["finding"] == "level_frame_disagreement"
    assert proposal.realized_branch_level["difference_db"] == pytest.approx(-1.6)
    assert proposal.evidence_identities["session_id"] == "cap_1"

    # Not a checksum of nothing: change one carried value, move the identity.
    other = build_intervention_proposal(
        candidate,
        accountability={"finding": "level_frame_disagreement"},
        realized_branch_level={"difference_db": -1.7},
        evidence_identities={"session_id": "cap_1"},
    )
    assert other.fingerprint != proposal.fingerprint


def test_the_realized_level_verdict_is_what_5c_iii_removed_and_this_restores():
    """``realized_branch_level`` is on the proposal, not merely accepted.

    #2291 Phase 5c-iii dropped the argument when the facade left. A parameter
    that is accepted and discarded looks identical to one that is wired, from
    the call site — so this asserts it reaches the digest.
    """
    without = build_intervention_proposal(_candidate())
    with_verdict = build_intervention_proposal(
        _candidate(), realized_branch_level={"difference_db": -1.6, "matched": True}
    )
    assert without.realized_branch_level == {}
    assert with_verdict.realized_branch_level["matched"] is True
    assert with_verdict.fingerprint != without.fingerprint


# --------------------------------------------------------------------------- #
# 3. the refusal arm — why PlanRefusal keeps its producer
# --------------------------------------------------------------------------- #


class _NoSections:
    """A candidate whose preset declares no crossover region at all."""

    fingerprint = "fp-no-sections"
    linearization_outcome = "fitted"
    source_preset = None


def test_a_candidate_with_no_crossover_has_no_context_and_says_which():
    """The raising form raises, with the reason attached to the exception."""
    with pytest.raises(NoCrossoverSectionsError) as excinfo:
        build_intervention_proposal(_NoSections())
    assert excinfo.value.refusal_reason == "no_crossover_sections"
    assert issubclass(NoCrossoverSectionsError, CrossoverV2ContractError)


def test_the_planning_form_refuses_instead_of_raising(caplog):
    """A commit mid-flight must not die because forensics could not assemble.

    :meth:`commit_intervention_proposal` performs irreversible acts for a
    candidate the household already confirmed. Assembly failure costs the round
    its proposal IDENTITY — the receipt then names the candidate under an
    explicit kind — and never the candidate itself.
    """
    with caplog.at_level(
        logging.WARNING, logger="jasper.active_speaker.crossover_v2.proposal"
    ):
        result = plan_intervention_proposal(_NoSections(), session_id="cap_1")

    assert isinstance(result, PlanRefusal)
    assert result.reason == "no_crossover_sections"
    assert result.detail
    messages = [record.getMessage() for record in caplog.records]
    assert any(f"event={PROPOSAL_REFUSED_EVENT}" in message for message in messages)


def test_the_refusal_reason_travels_by_TYPE_not_by_the_exceptions_prose():
    """#2307 gate note N5, kept alive now the classifier is back.

    Rewording a validator's message must not silently reclassify a household
    outcome, so the reason is read off the exception type's own attribute.
    """
    disagreement = CandidateFcDisagreementError("wording that no test owns")
    assert disagreement.refusal_reason == "candidate_fc_disagreement"
    assert CrossoverV2ContractError("anything").refusal_reason == "contract_invalid"


def test_a_missing_candidate_refuses_under_its_own_reason():
    with pytest.raises(CrossoverV2ContractError):
        build_intervention_proposal(None)
    result = plan_intervention_proposal(None)
    assert isinstance(result, PlanRefusal)
    assert result.reason == "no_candidate"


def test_a_successful_plan_says_what_it_proposed(caplog):
    """One stable event either way — the facade's write-only failure, undone.

    The Phase-1 facade computed a proposal, logged it, and dropped it. The
    fingerprint on this line is now the one the round receipt carries, so the
    journal and the artifact quote the same value.
    """
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.crossover_v2.proposal"
    ):
        proposal = plan_intervention_proposal(_candidate(), session_id="cap_1")

    assert isinstance(proposal, InterventionProposal)
    created = [
        record.getMessage()
        for record in caplog.records
        if f"event={PROPOSAL_CREATED_EVENT}" in record.getMessage()
    ]
    assert len(created) == 1
    assert f"proposal_fingerprint={proposal.fingerprint}" in created[0]


# --------------------------------------------------------------------------- #
# 4. the commit seam — the promise the docstring makes, pinned
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _FakeRealizedMatch:
    """A ``RealizedLevelMatch`` reduced to what ``LinearizationState`` reads.

    That accessor is exactly ``self.realized_level_match.to_dict()``, so a
    stand-in with one method drives the real derivation without dragging a
    fit's worth of arrays into a wiring test.
    """

    payload: dict

    def to_dict(self) -> dict:
        return dict(self.payload)


def _session():
    """The smallest real session, with recorder seams."""
    from jasper.active_speaker import crossover_v2_flow as flow
    from tests.crossover_v2_fixtures import (
        CAPS,
        SESSION,
        SESSION_VOLUME_DB,
        FakeSeams,
    )
    from tests.crossover_v2_fixtures import FC_HZ as SESSION_FC_HZ
    from tests.crossover_v2_fixtures import _preset as _session_preset
    from tests.crossover_v2_fixtures import _roles

    seams = FakeSeams()
    session = flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_session_preset(),
        roles_bands=_roles(),
        fc_hz=SESSION_FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams.seams(),
        driver_spacing_m=0.15,
    )
    return session, seams


def test_the_commit_seam_stashes_the_proposal_and_its_durable_identity():
    """What the seam has to leave behind for the receipt to find it.

    The session-scoped payload and the durable fingerprint are separate on
    purpose: the fingerprint is what crosses ``verify_priors`` to the stage
    that writes the receipt, and it must be the one the proposal really had.
    """
    session, seams = _session()
    candidate = _candidate(linearization_outcome="fitted")

    session.commit_intervention_proposal(
        candidate,
        predicted_sum=None,
        commanded_delta=None,
        level_frame_finding=None,
        realized_branch_level={"difference_db": -1.6},
    )

    proposal = session.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert session.measure_proposal_fingerprint == proposal.fingerprint
    assert proposal.realized_branch_level["difference_db"] == pytest.approx(-1.6)
    assert proposal.candidate_fingerprint == candidate.fingerprint
    # It is the PROPOSAL's identity, not the candidate's, or the receipt would
    # be back where it started.
    assert session.measure_proposal_fingerprint != candidate.fingerprint
    assert seams.published_candidates == [candidate]


def test_an_unassemblable_candidate_costs_the_round_its_proposal_not_its_commit():
    """"Assembly cannot fail this commit" — the docstring's promise, tested.

    The seam runs on the capture thread for a candidate the household has already
    confirmed, and ``publish_candidate`` is irreversible. A contract violation
    while gathering forensics must not be the thing that stops a measurement
    that would otherwise have succeeded.

    ``_NoSections`` raises ``NoCrossoverSectionsError`` — a ``ValueError``, and
    so is every other assembly failure worth surviving: ``json_fingerprint``'s
    ``EvidenceIdentityError`` and the whole ``CrossoverV2ContractError`` family
    both subclass it, which is why the refusal arm's exception tuple really does
    cover the realistic set rather than one example of it.
    """
    session, seams = _session()
    candidate = _NoSections()

    session.commit_intervention_proposal(
        candidate,
        predicted_sum=None,
        commanded_delta=None,
        level_frame_finding=None,
    )

    # The commit completed, whole.
    assert seams.published_candidates == [candidate]
    assert session._candidate is candidate
    # The proposal did not, and the session says so rather than inventing one.
    assert isinstance(session.last_intervention_proposal, PlanRefusal)
    assert session.last_intervention_proposal.reason == "no_crossover_sections"
    assert session.measure_proposal_fingerprint == "", (
        "an empty identity is what routes the receipt to its candidate"
    )


#: The verdict both routes must carry from their own evidence onto the
#: proposal. Distinctive so an assertion cannot pass on a coincidence, and
#: non-empty so a severed accessor collapses it to ``{}`` and reddens.
_REALIZED = {"difference_db": -1.63, "matched": True}


def test_the_walk_route_carries_its_builds_own_realized_level_verdict():
    """WIRING, not contract: the configured-Fc route's accessor is real.

    ``test_the_realized_level_verdict_is_what_5c_iii_removed_and_this_restores``
    hand-supplies the value, so it pins the CONTRACT and would stay green if
    :meth:`_commit_measure_candidate` stopped reading
    ``built.linearization.realized_branch_level`` altogether. The value now
    sits inside a write-once digest, so a dropped accessor would silently
    change what ``proposal_fingerprint`` covers — the exact failure class this
    PR closes. This drives the real call site instead.
    """
    from jasper.active_speaker.crossover_v2.candidates import SpeculativeClose
    from types import SimpleNamespace

    session, seams = _session()
    candidate = _candidate(linearization_outcome="fitted")
    built = SpeculativeClose(
        candidate=candidate,
        predicted_sum=None,
        # The three fields the commit reads off an analysis. ``driver_responses``
        # and ``alignment`` joined ``predicted_sum`` with #2611's commanded axis,
        # which models the graph an apply replaces on this capture's own branch
        # pair; a real ``ProgramAnalysis`` always carries both (they are declared
        # fields with defaults), so naming them here keeps the stand-in the same
        # shape as the thing it stands in for.
        analysis=SimpleNamespace(
            predicted_sum=None, driver_responses=(), alignment=None,
        ),
        cloud=None,
        level_frame_finding=None,
        linearization=LinearizationState(
            outcome="fitted",
            realized_level_match=_FakeRealizedMatch(_REALIZED),
            trim_strategy=TrimStrategy.RESOLVED_COMMITTED,
            anchor_drift_db=1.25,
        ),
    )

    session._commit_measure_candidate(built)

    assert seams.published_candidates == [candidate], "the real commit ran"
    proposal = session.last_intervention_proposal
    assert isinstance(proposal, InterventionProposal)
    assert dict(proposal.realized_branch_level) == _REALIZED, (
        "the walk must read the verdict off its OWN build's linearization state"
    )
    assert proposal.trim_strategy is TrimStrategy.RESOLVED_COMMITTED, (
        "the committed pair travels the same seam; deriving it from the "
        "candidate's outcome string would say COMMITTED_PAIR_UNRECORDED"
    )


def test_a_proposal_fingerprint_and_a_candidate_fingerprint_are_the_same_shape():
    """The premise the whole #2392 migration story rests on, on real objects.

    Both identities come out of
    :func:`~jasper.audio_measurement.evidence_identity.json_fingerprint`, so
    they are the same 64-character lowercase SHA-256 hex and NOTHING about a
    banked value tells a later reader which regime wrote it. That is why the
    receipt carries an explicit ``proposal_fingerprint_kind`` rather than a
    claimed format difference — see
    ``tests/test_crossover_v2_round_wiring.py`` section 3b for the receipt end.
    """
    candidate = _candidate(linearization_outcome="fitted")
    proposal = build_intervention_proposal(candidate)

    for label, fingerprint in (
        ("candidate", candidate.fingerprint),
        ("proposal", proposal.fingerprint),
    ):
        assert len(fingerprint) == 64, label
        assert set(fingerprint) <= set("0123456789abcdef"), label
    # Same shape, different values — indistinguishable, and not the same thing.
    assert proposal.fingerprint != candidate.fingerprint


def test_every_assembly_failure_worth_surviving_is_in_the_refusal_arms_tuple():
    """The tuple is narrow (ruff BLE, the frozen broad-except budget), so the
    claim that it is WIDE ENOUGH has to be checked rather than assumed.

    Both error families a real assembly can raise are ``ValueError`` subclasses,
    and ``ValueError`` is in the tuple. A future error type outside it would
    escape into the commit path, which this catches.
    """
    from jasper.audio_measurement.evidence_identity import EvidenceIdentityError

    from jasper.active_speaker.crossover_v2.proposal import _ASSEMBLY_ERRORS

    for error in (CrossoverV2ContractError, NoCrossoverSectionsError,
                  CandidateFcDisagreementError, EvidenceIdentityError):
        assert issubclass(error, _ASSEMBLY_ERRORS), error.__name__
