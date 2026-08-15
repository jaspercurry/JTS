# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 1: the crossover-v2 domain contracts and their invariants.

These are construction-time guards, so every one of them is asserted in BOTH
directions — a value that must be refused is refused, and the neighbouring
legal value still constructs. A guard pinned only on the failing side passes
just as happily when the guard is deleted and the type accepts everything.
"""

from __future__ import annotations

import dataclasses

import pytest

from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.crossover_v2 import (
    PLAN_REFUSAL_REASONS,
    PROPOSAL_FINGERPRINT_KINDS,
    AdoptionDecision,
    AdoptionOutcome,
    BenefitStatus,
    CandidateAcousticContext,
    CaptureValidity,
    CrossoverV2ContractError,
    InterventionProposal,
    PlanRefusal,
    RealizationStatus,
    ResponseCurve,
    RoundReceipt,
    SpecStatus,
    TrimStrategy,
    VerificationResult,
)

FC = 1648.7


def _sections(fc: float = FC) -> dict[str, tuple[CrossoverSection, ...]]:
    return {
        "woofer": (CrossoverSection(fc_hz=fc, order=4, highpass=False),),
        "tweeter": (CrossoverSection(fc_hz=fc, order=4, highpass=True),),
    }


def _context(fc: float = FC) -> CandidateAcousticContext:
    return CandidateAcousticContext(fc_hz=fc, sections_by_role=_sections(fc))


class _Candidate:
    """The narrow surface the proposal reads off a MeasuredCrossoverCandidate."""

    def __init__(self, fingerprint: str = "a" * 64, program_id: str = "prog-1") -> None:
        self.fingerprint = fingerprint
        self.program_id = program_id
        self.linearization: dict[str, object] = {}
        self.linearization_outcome = "fitted"
        self.exclusion_evidence: dict[str, object] = {}


def _proposal(**overrides: object) -> InterventionProposal:
    kwargs: dict[str, object] = {
        "candidate": _Candidate(),
        "context": _context(),
        "evidence_identities": {"session_id": "cap_1"},
        "predicted_response_before": ([100.0, 200.0], [-1.0, -2.0]),
        "predicted_response_after": ([100.0, 200.0], [-0.5, -1.5]),
        "predicted_spec_before": {"overall_passed": False},
        "predicted_spec_after": {"overall_passed": True},
        "commanded_delta": ([100.0, 200.0], [0.5, 0.5]),
        "trim_strategy": TrimStrategy.RESOLVED_COMMITTED_AFTER_SANITY_DRIFT,
        "trim_rationale": "drifted, committed anyway",
        "anchored_trim_db": {"tweeter": -6.713},
        "alternative_trim_db": {"tweeter": -13.01298},
        "realized_branch_level": {"difference_db": -1.6},
        "linearization_filters": {"tweeter": [{"gain_db": -2.0}]},
        "excluded_regions": {"bands_hz": [[4000.0, 5000.0]]},
        "accountability": {"banked": True},
        "diagnostic_findings": [{"mechanism": "baffle_step"}],
    }
    kwargs.update(overrides)
    return InterventionProposal(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# CandidateAcousticContext — the one Fc owner
# --------------------------------------------------------------------------


def test_a_context_whose_sections_all_name_its_corner_constructs():
    context = _context()
    assert context.fc_hz == FC
    assert context.roles == ("tweeter", "woofer")
    assert set(context.sections_by_role) == {"woofer", "tweeter"}


def test_a_section_cornered_away_from_the_declared_fc_fails_closed():
    """The 2026-08-10 defect, refused at construction.

    The configured session said 2,000 Hz and the candidate's sections said
    1,648.7 Hz. Whichever number the caller believes, a context cannot hold
    both.
    """
    sections = _sections(FC)
    sections["tweeter"] = (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),)
    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)


def test_a_declared_fc_away_from_agreeing_sections_fails_closed():
    """The same guard from the other side: the sections agree, the Fc does not."""

    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=2000.0, sections_by_role=_sections(FC))


def test_agreement_is_exact_and_not_toleranced():
    """A corner one float ulp away is a disagreement, not round-trip noise.

    ``REGION_FC_MATCH_TOLERANCE_HZ`` (1e-6) exists for a corner that has been
    through persisted JSON. These sections are built in-process from one float,
    so any inequality is real — and a tolerance here is precisely how a
    2,000 Hz session corner would eventually be allowed to stand in for a
    1,648.7 Hz candidate one.
    """
    sections = _sections(FC)
    sections["woofer"] = (
        CrossoverSection(fc_hz=FC + 1e-9, order=4, highpass=False),
    )
    with pytest.raises(CrossoverV2ContractError, match="disagrees with"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)


def test_from_sections_derives_the_corner_when_the_sections_are_unanimous():
    context = CandidateAcousticContext.from_sections(_sections())
    assert context.fc_hz == FC
    assert context == _context()


def test_from_sections_refuses_a_split_corner_set():
    sections = _sections(FC)
    sections["tweeter"] = (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),)
    with pytest.raises(CrossoverV2ContractError, match="different crossover corners"):
        CandidateAcousticContext.from_sections(sections)


def test_a_full_range_role_carries_no_sections_and_that_is_legal():
    """``sections_by_role`` gives a role with no crossover region no sections.

    That role runs full range in the emitted graph — the honest answer, and one
    the context must not turn into a refusal as long as some crossover exists.
    """
    sections = dict(_sections())
    sections["sub"] = ()
    context = CandidateAcousticContext(fc_hz=FC, sections_by_role=sections)
    assert context.sections_by_role["sub"] == ()
    assert context.fc_hz == FC


def test_a_context_with_no_crossover_section_anywhere_is_refused():
    with pytest.raises(CrossoverV2ContractError, match="at least one crossover"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role={"woofer": ()})
    with pytest.raises(CrossoverV2ContractError, match="must not be empty"):
        CandidateAcousticContext(fc_hz=FC, sections_by_role={})


def test_a_non_positive_or_non_finite_corner_is_refused():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(CrossoverV2ContractError):
            CandidateAcousticContext(fc_hz=bad, sections_by_role=_sections())


def test_the_context_is_immutable_including_its_section_map():
    context = _context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.fc_hz = 2000.0  # type: ignore[misc]
    # The accessor hands out a copy, so a caller cannot re-corner the context
    # it was given.
    context.sections_by_role["woofer"] = ()
    assert context.sections_by_role["woofer"] != ()


def test_the_context_fingerprint_ignores_role_insertion_order():
    forward = CandidateAcousticContext(
        fc_hz=FC,
        sections_by_role={
            "woofer": (CrossoverSection(fc_hz=FC, order=4, highpass=False),),
            "tweeter": (CrossoverSection(fc_hz=FC, order=4, highpass=True),),
        },
    )
    reversed_order = CandidateAcousticContext(
        fc_hz=FC,
        sections_by_role={
            "tweeter": (CrossoverSection(fc_hz=FC, order=4, highpass=True),),
            "woofer": (CrossoverSection(fc_hz=FC, order=4, highpass=False),),
        },
    )
    assert forward.fingerprint == reversed_order.fingerprint


def test_the_context_fingerprint_tracks_the_corner_and_the_section_shape():
    base = _context().fingerprint
    assert _context(fc=1700.0).fingerprint != base
    steeper = _sections()
    steeper["woofer"] = (CrossoverSection(fc_hz=FC, order=8, highpass=False),)
    assert (
        CandidateAcousticContext(fc_hz=FC, sections_by_role=steeper).fingerprint
        != base
    )


# --------------------------------------------------------------------------
# ResponseCurve
# --------------------------------------------------------------------------


def test_a_curve_refuses_non_finite_points_and_ragged_pairs():
    with pytest.raises(CrossoverV2ContractError, match="finite"):
        ResponseCurve([100.0, 200.0], [-1.0, float("nan")])
    with pytest.raises(CrossoverV2ContractError, match="one level per frequency"):
        ResponseCurve([100.0, 200.0], [-1.0])
    with pytest.raises(CrossoverV2ContractError, match="must have points"):
        ResponseCurve([], [])
    # …and the neighbouring legal curve still constructs.
    assert ResponseCurve([100.0, 200.0], [-1.0, -2.0]).db == (-1.0, -2.0)


def test_a_curve_pair_is_normalized_but_none_passes_through():
    assert ResponseCurve.from_pair(None, field_name="x") is None
    curve = ResponseCurve.from_pair(([1.0], [2.0]), field_name="x")
    assert curve is not None and curve.hz == (1.0,)
    with pytest.raises(CrossoverV2ContractError, match="freqs_hz, db"):
        ResponseCurve.from_pair(object(), field_name="x")


# --------------------------------------------------------------------------
# InterventionProposal
# --------------------------------------------------------------------------


def test_a_proposal_reads_its_corner_from_its_context():
    proposal = _proposal()
    assert proposal.fc_hz == FC == proposal.context.fc_hz
    assert proposal.candidate_fingerprint == "a" * 64


def test_a_proposal_needs_a_candidate_and_a_real_context():
    with pytest.raises(CrossoverV2ContractError, match="measured candidate"):
        _proposal(candidate=None)
    with pytest.raises(CrossoverV2ContractError, match="CandidateAcousticContext"):
        _proposal(context={"fc_hz": FC})
    with pytest.raises(CrossoverV2ContractError, match="TrimStrategy"):
        _proposal(trim_strategy="anchored_committed")


def test_a_proposal_is_immutable_and_copies_the_mappings_it_was_handed():
    filters = {"tweeter": [{"gain_db": -2.0}]}
    proposal = _proposal(linearization_filters=filters)
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.trim_rationale = "changed"  # type: ignore[misc]
    filters["woofer"] = []
    assert "woofer" not in proposal.linearization_filters


def test_a_proposal_detaches_the_nested_containers_it_was_handed_too():
    """The copy is DEEP — a shallow one leaves the fingerprint describing a
    different object than the report does (#2307 gate note N1).

    The top-level guard above passed while nested mutation still reached in:
    ``dict(value)`` copies the outer mapping and shares every inner one. Since
    :attr:`InterventionProposal.fingerprint` is taken once at construction, a
    caller mutating an inner dict afterwards produced a proposal whose digest
    no longer covered its own contents — the exact divergence these contracts
    exist to make impossible.
    """
    inner = {"gain_db": -2.0}
    band = [100.0, 200.0]
    filters = {"tweeter": {"filters": [inner], "band_hz": band}}
    proposal = _proposal(linearization_filters=filters)
    fingerprint = proposal.fingerprint

    inner["gain_db"] = -40.0
    band.append(300.0)
    filters["tweeter"]["reason"] = "tampered"

    held = proposal.linearization_filters["tweeter"]
    assert held["filters"][0]["gain_db"] == -2.0
    assert list(held["band_hz"]) == [100.0, 200.0]
    assert "reason" not in held
    # A list stays a list: the shared fingerprinter refuses a tuple, so
    # detaching must not change the type the digest sees.
    assert type(held["band_hz"]) is list
    # And the digest still describes what the object holds.
    assert proposal.fingerprint == fingerprint
    assert _proposal(linearization_filters=proposal.linearization_filters).fingerprint == (
        fingerprint
    )


def test_the_proposal_fingerprint_is_deterministic_across_key_ordering():
    forward = _proposal(
        evidence_identities={"session_id": "cap_1", "program_id": "p"},
        predicted_spec_after={"overall_passed": True, "reference_db": -31.367},
    )
    shuffled = _proposal(
        evidence_identities={"program_id": "p", "session_id": "cap_1"},
        predicted_spec_after={"reference_db": -31.367, "overall_passed": True},
    )
    assert forward.fingerprint == shuffled.fingerprint


# Every committed input/value, and the change that must move the digest.
_COMMITTED_FIELD_MUTATIONS: tuple[tuple[str, object], ...] = (
    ("candidate", _Candidate(fingerprint="b" * 64)),
    ("context", _context(fc=1700.0)),
    ("evidence_identities", {"session_id": "cap_2"}),
    ("predicted_response_before", ([100.0, 200.0], [-1.0, -2.5])),
    ("predicted_response_after", ([100.0, 200.0], [-0.5, -1.6])),
    ("predicted_spec_before", {"overall_passed": True}),
    ("predicted_spec_after", {"overall_passed": False}),
    ("commanded_delta", ([100.0, 200.0], [0.5, 0.6])),
    ("trim_strategy", TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT),
    ("trim_rationale", "a different rationale"),
    ("anchored_trim_db", {"tweeter": -6.714}),
    ("alternative_trim_db", {"tweeter": -13.013}),
    ("realized_branch_level", {"difference_db": -1.7}),
    ("linearization_filters", {"tweeter": [{"gain_db": -2.5}]}),
    ("excluded_regions", {"bands_hz": [[4000.0, 5100.0]]}),
    ("accountability", {"banked": False}),
    ("diagnostic_findings", [{"mechanism": "horn_cd"}]),
)


@pytest.mark.parametrize(
    "field_name,mutated",
    _COMMITTED_FIELD_MUTATIONS,
    ids=[name for name, _ in _COMMITTED_FIELD_MUTATIONS],
)
def test_changing_any_committed_value_changes_the_proposal_fingerprint(
    field_name, mutated
):
    """#2291: "a proposal fingerprint covering every committed input/value".

    Parametrized across the whole field list rather than spot-checked, because
    a digest that silently omits one field is indistinguishable from a correct
    one until the field that matters is the one that changed.
    """
    assert _proposal().fingerprint != _proposal(**{field_name: mutated}).fingerprint


def test_the_parametrized_mutation_set_covers_every_constructor_field():
    """The guard above is only as complete as its list — so pin the list.

    A field added to :class:`InterventionProposal` without a mutation case
    would otherwise join the contract untested.
    """
    import inspect

    covered = {name for name, _ in _COMMITTED_FIELD_MUTATIONS}
    declared = set(inspect.signature(InterventionProposal.__init__).parameters)
    declared -= {"self"}
    assert declared == covered


def test_a_proposal_round_trips_its_declared_fingerprint():
    proposal = _proposal()
    payload = proposal.to_dict()
    assert payload["fingerprint"] == proposal.fingerprint
    assert payload["kind"] == "jts_crossover_v2_intervention_proposal"
    assert payload["schema_version"] == 1


# --------------------------------------------------------------------------
# PlanRefusal
# --------------------------------------------------------------------------


def test_a_refusal_reason_comes_from_the_closed_set():
    refusal = PlanRefusal(reason="contract_invalid", detail="why")
    assert refusal.to_dict()["reason"] == "contract_invalid"
    assert "contract_invalid" in PLAN_REFUSAL_REASONS
    with pytest.raises(CrossoverV2ContractError, match="unknown plan refusal"):
        PlanRefusal(reason="something_went_wrong")


# --------------------------------------------------------------------------
# VerificationResult — four independent answers
# --------------------------------------------------------------------------


def _verification(**overrides) -> VerificationResult:
    kwargs = {
        "capture_validity": CaptureValidity.USABLE,
        "realization": RealizationStatus.MATCHED,
        "benefit": BenefitStatus.IMPROVED,
        "spec": SpecStatus.FAILED,
        "reason": "",
    }
    kwargs.update(overrides)
    return VerificationResult(**kwargs)  # type: ignore[arg-type]


def test_realized_and_improved_but_out_of_spec_is_a_legal_result():
    """#2291's named valid first pass. It must fit in the data model."""

    result = _verification()
    assert result.benefit is BenefitStatus.IMPROVED
    assert result.spec is SpecStatus.FAILED


def test_a_model_tracking_pass_can_coexist_with_a_measured_regression():
    """The 2026-08-10 shape: realization matched, the speaker got worse.

    The type must be able to say both at once, or the honest verdict has
    nowhere to live.
    """
    result = _verification(benefit=BenefitStatus.REGRESSED)
    assert result.realization is RealizationStatus.MATCHED
    assert result.benefit is BenefitStatus.REGRESSED


def test_an_unusable_capture_cannot_report_graded_answers():
    for field_name, value in (
        ("realization", RealizationStatus.MATCHED),
        ("benefit", BenefitStatus.IMPROVED),
        ("spec", SpecStatus.PASSED),
    ):
        kwargs = {
            "capture_validity": CaptureValidity.UNUSABLE,
            "realization": RealizationStatus.UNAVAILABLE,
            "benefit": BenefitStatus.INDETERMINATE,
            "spec": SpecStatus.UNEVALUABLE,
            "reason": "clipped",
            field_name: value,
        }
        with pytest.raises(CrossoverV2ContractError, match="unusable capture"):
            VerificationResult(**kwargs)  # type: ignore[arg-type]


def test_an_unusable_capture_must_say_why_but_otherwise_constructs():
    with pytest.raises(CrossoverV2ContractError, match="must state a reason"):
        _verification(
            capture_validity=CaptureValidity.UNUSABLE,
            realization=RealizationStatus.UNAVAILABLE,
            benefit=BenefitStatus.INDETERMINATE,
            spec=SpecStatus.UNEVALUABLE,
            reason="",
        )
    ok = _verification(
        capture_validity=CaptureValidity.UNUSABLE,
        realization=RealizationStatus.UNAVAILABLE,
        benefit=BenefitStatus.INDETERMINATE,
        spec=SpecStatus.UNEVALUABLE,
        reason="clipped",
    )
    assert ok.to_dict()["capture_validity"] == "unusable"


# --------------------------------------------------------------------------
# AdoptionDecision / RoundReceipt
# --------------------------------------------------------------------------


def test_keep_needs_no_reason_but_every_other_outcome_does():
    assert AdoptionDecision(outcome=AdoptionOutcome.KEEP).reason == ""
    for outcome in (
        AdoptionOutcome.RESTORE,
        AdoptionOutcome.KEEP_FOR_ITERATION,
        AdoptionOutcome.RECOVERY_REQUIRED,
    ):
        with pytest.raises(CrossoverV2ContractError, match="must state a reason"):
            AdoptionDecision(outcome=outcome, reason="   ")
        assert AdoptionDecision(outcome=outcome, reason="measured regression")


def _receipt(**overrides) -> RoundReceipt:
    kwargs = {
        "round_id": "round-1",
        "entry_graph_fingerprint": "c" * 64,
        "proposal_fingerprint": "d" * 64,
        "proposal_fingerprint_kind": "intervention_proposal",
        "verification": _verification(),
        "adoption": AdoptionDecision(outcome=AdoptionOutcome.KEEP),
        "created_at": "2026-08-10T00:00:00Z",
    }
    kwargs.update(overrides)
    return RoundReceipt(**kwargs)  # type: ignore[arg-type]


def test_a_receipt_must_say_what_its_proposal_fingerprint_identifies():
    """#2392's migration story, enforced where it can still be caught.

    ``proposal_fingerprint`` fed on a candidate identity before #2392 and on
    :attr:`InterventionProposal.fingerprint` after it, and the two are the same
    shape — a 64-hex SHA-256 — so nothing about the value distinguishes them.
    The receipt therefore has to SAY which, from a closed set, and a receipt
    that cannot must not be constructible: it is a write-once artifact, so this
    constructor is the last place a mislabel is still cheap.
    """
    for kind in sorted(PROPOSAL_FINGERPRINT_KINDS):
        assert _receipt(proposal_fingerprint_kind=kind).proposal_fingerprint_kind == kind
    with pytest.raises(CrossoverV2ContractError, match="proposal fingerprint kind"):
        _receipt(proposal_fingerprint_kind="proposal")
    with pytest.raises(CrossoverV2ContractError, match="proposal_fingerprint_kind"):
        _receipt(proposal_fingerprint_kind="  ")


def test_the_kind_reaches_the_payload_and_moves_the_receipt_fingerprint():
    """Two receipts identical but for the regime that wrote them differ.

    The whole point of the marker is that a later reader can tell them apart;
    if it rode outside the digest, a receipt could be relabelled after the fact
    without its fingerprint noticing.
    """
    proposal = _receipt(proposal_fingerprint_kind="intervention_proposal")
    candidate = _receipt(proposal_fingerprint_kind="candidate")
    assert proposal.to_dict()["proposal_fingerprint_kind"] == "intervention_proposal"
    assert candidate.to_dict()["proposal_fingerprint_kind"] == "candidate"
    assert proposal.fingerprint != candidate.fingerprint


def test_the_receipt_payload_covers_every_constructor_field():
    """A field added to :class:`RoundReceipt` without reaching the digest.

    The sibling guard :func:`test_the_parametrized_mutation_set_covers_every_
    constructor_field` does this for :class:`InterventionProposal`; the receipt
    had none, which is how #2392's new field could have been added as a value
    the fingerprint did not cover. ``self`` is not a field; ``fingerprint`` is
    the digest itself and is added by ``to_dict`` rather than ``_core``.
    """
    import inspect

    declared = set(inspect.signature(RoundReceipt.__init__).parameters) - {"self"}
    assert declared <= set(_receipt().to_dict())


def test_a_receipt_binds_its_round_and_fingerprints():
    receipt = _receipt()
    payload = receipt.to_dict()
    assert payload["kind"] == "jts_crossover_v2_round_receipt"
    assert payload["fingerprint"] == receipt.fingerprint
    assert payload["adoption"]["outcome"] == "keep"


def test_a_recovery_required_round_must_record_what_the_restore_did():
    """A recovery that cannot say what the restore did is not a receipt.

    ``recovery_required`` means the speaker is in neither the entry graph nor
    the intended one; that is the one state that must never be reported as a
    restore.
    """
    recovery = AdoptionDecision(
        outcome=AdoptionOutcome.RECOVERY_REQUIRED, reason="dsp_restore_incomplete"
    )
    with pytest.raises(CrossoverV2ContractError, match="restore result"):
        _receipt(adoption=recovery)
    assert _receipt(
        adoption=recovery, restore_result={"completed": False, "error": "timeout"}
    )


def test_a_receipt_fingerprint_tracks_its_adoption_and_verification():
    base = _receipt().fingerprint
    assert (
        _receipt(
            adoption=AdoptionDecision(
                outcome=AdoptionOutcome.RESTORE, reason="regressed"
            )
        ).fingerprint
        != base
    )
    assert (
        _receipt(verification=_verification(spec=SpecStatus.PASSED)).fingerprint != base
    )


# --------------------------------------------------------------------------
# the naming rule itself
# --------------------------------------------------------------------------


def test_no_trim_strategy_says_rejected_while_meaning_committed():
    """The contradiction that shipped on 2026-08-10, pinned as a rule.

    ``linearization_outcome="trim_rejected"`` recorded a −13.013 dB trim that
    was committed. Any member whose name claims a rejection must not also
    claim a commitment, and no member may use the word at all while a pair was
    in fact committed.
    """
    for member in TrimStrategy:
        if "committed" in member.value:
            assert "reject" not in member.value, member
