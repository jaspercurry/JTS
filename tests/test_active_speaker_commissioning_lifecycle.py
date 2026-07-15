# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.commissioning_lifecycle import (
    COMMISSIONING_EVIDENCE_KINDS,
    COMMISSIONING_STATES,
    CommissioningLifecycleError,
    CommissioningTransition,
)


def _hash(char: str) -> str:
    return char * 64


def _transition(
    from_state: str,
    to_state: str,
    evidence_kind: str,
    char: str,
    *,
    failure_code: str | None = None,
) -> CommissioningTransition:
    return CommissioningTransition(
        from_state=from_state,  # type: ignore[arg-type]
        to_state=to_state,  # type: ignore[arg-type]
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        evidence_fingerprint=_hash(char),
        failure_code=failure_code,
    )


def test_commissioning_happy_path_is_explicit_and_evidence_kind_bound():
    transitions = (
        _transition("unconfigured", "protected", "protection_evidence", "1"),
        _transition("protected", "measured", "admitted_measurement_set", "2"),
        _transition("measured", "candidate_ready", "candidate_artifact", "3"),
        _transition(
            "candidate_ready",
            "applied_unverified",
            "applied_candidate_proof",
            "4",
        ),
        _transition(
            "applied_unverified",
            "verified",
            "commissioning_eligibility_receipt",
            "5",
        ),
    )

    assert [transition.to_state for transition in transitions] == [
        "protected",
        "measured",
        "candidate_ready",
        "applied_unverified",
        "verified",
    ]
    assert all(
        CommissioningTransition.from_mapping(transition.to_dict()) == transition
        for transition in transitions
    )


def test_lifecycle_declares_closed_state_and_evidence_vocabularies():
    assert COMMISSIONING_STATES == {
        "unconfigured",
        "protected",
        "measured",
        "candidate_ready",
        "applied_unverified",
        "verified",
        "blocked",
        "blocked_live_state_unknown",
        "rolled_back",
    }
    assert COMMISSIONING_EVIDENCE_KINDS == {
        "protection_evidence",
        "admitted_measurement_set",
        "candidate_artifact",
        "applied_candidate_proof",
        "commissioning_eligibility_receipt",
        "failure_evidence",
        "uncertain_mutation_evidence",
        "exact_restore_evidence",
    }


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("unconfigured", "measured"),
        ("protected", "candidate_ready"),
        ("measured", "verified"),
        ("candidate_ready", "verified"),
        ("verified", "applied_unverified"),
        ("blocked", "verified"),
        ("applied_unverified", "blocked"),
        ("blocked_live_state_unknown", "protected"),
    ],
)
def test_lifecycle_refuses_skipped_or_uncertain_state_recovery(from_state, to_state):
    with pytest.raises(CommissioningLifecycleError, match="not allowed"):
        _transition(from_state, to_state, "failure_evidence", "a")


def test_verified_and_rolled_back_require_exact_evidence_kinds():
    with pytest.raises(
        CommissioningLifecycleError,
        match="commissioning_eligibility_receipt",
    ):
        _transition(
            "applied_unverified",
            "verified",
            "applied_candidate_proof",
            "a",
        )
    with pytest.raises(CommissioningLifecycleError, match="exact_restore_evidence"):
        _transition(
            "applied_unverified",
            "rolled_back",
            "failure_evidence",
            "b",
        )


def test_post_mutation_failure_retains_uncertain_live_state_until_exact_restore():
    blocked = _transition(
        "applied_unverified",
        "blocked_live_state_unknown",
        "uncertain_mutation_evidence",
        "b",
        failure_code="mutation_outcome_unknown",
    )
    restored = _transition(
        "blocked_live_state_unknown",
        "rolled_back",
        "exact_restore_evidence",
        "c",
    )

    assert blocked.to_state == "blocked_live_state_unknown"
    assert restored.to_state == "rolled_back"
    with pytest.raises(CommissioningLifecycleError, match="not allowed"):
        _transition(
            "blocked_live_state_unknown",
            "protected",
            "protection_evidence",
            "d",
        )


@pytest.mark.parametrize("from_state", ["unconfigured", "protected"])
def test_measurement_restore_failure_has_a_narrow_uncertain_state_path(
    from_state: str,
) -> None:
    transition = _transition(
        from_state,
        "blocked_live_state_unknown",
        "uncertain_mutation_evidence",
        "a",
        failure_code="measurement_restore_failed",
    )
    assert transition.to_state == "blocked_live_state_unknown"

    with pytest.raises(CommissioningLifecycleError, match="incompatible"):
        _transition(
            from_state,
            "blocked_live_state_unknown",
            "uncertain_mutation_evidence",
            "b",
            failure_code="mutation_outcome_unknown",
        )


def test_candidate_apply_uncertainty_cannot_use_recoverable_blocked_state():
    uncertain = _transition(
        "candidate_ready",
        "blocked_live_state_unknown",
        "uncertain_mutation_evidence",
        "a",
        failure_code="mutation_outcome_unknown",
    )
    assert uncertain.to_state == "blocked_live_state_unknown"

    with pytest.raises(
        CommissioningLifecycleError,
        match="post-mutation failure requires blocked_live_state_unknown",
    ):
        _transition(
            "candidate_ready",
            "blocked",
            "failure_evidence",
            "b",
            failure_code="mutation_outcome_unknown",
        )

    with pytest.raises(
        CommissioningLifecycleError,
        match="post-mutation failure code",
    ):
        _transition(
            "candidate_ready",
            "blocked_live_state_unknown",
            "uncertain_mutation_evidence",
            "c",
            failure_code="candidate_apply_failed_before_mutation",
        )


def test_pre_mutation_blocked_recovery_and_failure_codes_are_closed():
    blocked = _transition(
        "measured",
        "blocked",
        "failure_evidence",
        "b",
        failure_code="candidate_scoring_failed",
    )
    recovered = _transition(
        "blocked",
        "protected",
        "protection_evidence",
        "c",
    )
    assert blocked.to_state == "blocked"
    assert recovered.to_state == "protected"

    with pytest.raises(CommissioningLifecycleError, match="invalid failure_code"):
        _transition(
            "measured",
            "blocked",
            "failure_evidence",
            "d",
            failure_code="free_form_failure",
        )

    with pytest.raises(CommissioningLifecycleError, match="incompatible"):
        _transition(
            "unconfigured",
            "blocked",
            "failure_evidence",
            "e",
            failure_code="candidate_apply_failed_before_mutation",
        )


def test_transition_serialization_rejects_unknown_bool_schema_and_tamper():
    transition = _transition(
        "applied_unverified",
        "verified",
        "commissioning_eligibility_receipt",
        "d",
    )

    unknown = transition.to_dict()
    unknown["future_guess"] = True
    with pytest.raises(CommissioningLifecycleError, match="unknown or missing"):
        CommissioningTransition.from_mapping(unknown)

    boolean_schema = transition.to_dict()
    boolean_schema["schema_version"] = True
    with pytest.raises(CommissioningLifecycleError, match="unsupported"):
        CommissioningTransition.from_mapping(boolean_schema)

    tampered = transition.to_dict()
    tampered["evidence_fingerprint"] = _hash("e")
    with pytest.raises(CommissioningLifecycleError, match="does not match"):
        CommissioningTransition.from_mapping(tampered)
