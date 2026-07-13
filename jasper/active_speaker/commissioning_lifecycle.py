# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure Active-owned commissioning lifecycle states and guarded transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias, cast

from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)

CommissioningState: TypeAlias = Literal[
    "unconfigured",
    "protected",
    "measured",
    "candidate_ready",
    "applied_unverified",
    "verified",
    "blocked",
    "blocked_live_state_unknown",
    "rolled_back",
]
CommissioningEvidenceKind: TypeAlias = Literal[
    "protection_evidence",
    "admitted_measurement_set",
    "candidate_artifact",
    "applied_candidate_proof",
    "commissioning_eligibility_receipt",
    "failure_evidence",
    "uncertain_mutation_evidence",
    "exact_restore_evidence",
]

COMMISSIONING_STATES = frozenset(
    {
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
)

COMMISSIONING_EVIDENCE_KINDS = frozenset(
    {
        "protection_evidence",
        "admitted_measurement_set",
        "candidate_artifact",
        "applied_candidate_proof",
        "commissioning_eligibility_receipt",
        "failure_evidence",
        "uncertain_mutation_evidence",
        "exact_restore_evidence",
    }
)

COMMISSIONING_FAILURE_CODES = frozenset(
    {
        "protection_missing",
        "measurement_failed",
        "candidate_scoring_failed",
        "writer_lock_unavailable",
        "candidate_apply_failed_before_mutation",
        "mutation_outcome_unknown",
        "fresh_readback_failed",
        "candidate_readback_mismatch",
        "protection_proof_failed",
        "post_apply_verification_failed",
        "rollback_apply_failed",
        "rollback_readback_failed",
        "rollback_readback_mismatch",
    }
)

_ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "unconfigured": frozenset({"protected", "blocked"}),
    "protected": frozenset({"unconfigured", "measured", "blocked"}),
    "measured": frozenset({"protected", "candidate_ready", "blocked"}),
    "candidate_ready": frozenset({"measured", "applied_unverified", "blocked"}),
    # Once mutation begins, an uncertain failure gets a distinct durable state.
    # It cannot fall through the ordinary pre-mutation blocked recovery ladder.
    "applied_unverified": frozenset(
        {
            "verified",
            "blocked_live_state_unknown",
            "rolled_back",
        }
    ),
    "verified": frozenset(
        {
            "protected",
            "blocked_live_state_unknown",
            "rolled_back",
        }
    ),
    "blocked": frozenset({"unconfigured", "protected"}),
    "blocked_live_state_unknown": frozenset({"rolled_back"}),
    "rolled_back": frozenset({"protected", "candidate_ready", "blocked"}),
}

_EXPECTED_EVIDENCE_KIND: Mapping[str, CommissioningEvidenceKind] = {
    "protected": "protection_evidence",
    "measured": "admitted_measurement_set",
    "candidate_ready": "candidate_artifact",
    "applied_unverified": "applied_candidate_proof",
    "verified": "commissioning_eligibility_receipt",
    "blocked": "failure_evidence",
    "blocked_live_state_unknown": "uncertain_mutation_evidence",
    "rolled_back": "exact_restore_evidence",
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CommissioningLifecycleError(ValueError):
    """A commissioning state or transition violates the lifecycle contract."""


def _state(value: Any, *, field_name: str) -> CommissioningState:
    if not isinstance(value, str) or value not in COMMISSIONING_STATES:
        raise CommissioningLifecycleError(f"invalid {field_name}")
    return cast(CommissioningState, value)


def _optional_evidence_kind(value: Any) -> CommissioningEvidenceKind | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in COMMISSIONING_EVIDENCE_KINDS:
        raise CommissioningLifecycleError("invalid evidence_kind")
    return cast(CommissioningEvidenceKind, value)


def _optional_sha256(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CommissioningLifecycleError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _optional_failure(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in COMMISSIONING_FAILURE_CODES:
        raise CommissioningLifecycleError("invalid failure_code")
    return value


def _fingerprint(payload: Mapping[str, Any]) -> str:
    try:
        return json_fingerprint(payload, field_name="commissioning transition")
    except EvidenceIdentityError as exc:
        raise CommissioningLifecycleError(str(exc)) from exc


@dataclass(frozen=True)
class CommissioningTransition:
    """One legal, evidence-kind-bound Active commissioning transition."""

    from_state: CommissioningState
    to_state: CommissioningState
    evidence_kind: CommissioningEvidenceKind | None = None
    evidence_fingerprint: str | None = None
    failure_code: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        from_state = _state(self.from_state, field_name="from_state")
        to_state = _state(self.to_state, field_name="to_state")
        if to_state not in _ALLOWED_TRANSITIONS[from_state]:
            raise CommissioningLifecycleError(
                f"transition {from_state} -> {to_state} is not allowed"
            )
        evidence_kind = _optional_evidence_kind(self.evidence_kind)
        evidence = _optional_sha256(
            self.evidence_fingerprint,
            field_name="evidence_fingerprint",
        )
        failure = _optional_failure(self.failure_code)
        expected_kind = _EXPECTED_EVIDENCE_KIND.get(to_state)
        if expected_kind is None:
            if evidence_kind is not None or evidence is not None:
                raise CommissioningLifecycleError(
                    f"transition to {to_state} must not carry authority evidence"
                )
        elif evidence_kind != expected_kind or evidence is None:
            raise CommissioningLifecycleError(
                f"transition to {to_state} requires {expected_kind} evidence"
            )
        failure_state = to_state in {"blocked", "blocked_live_state_unknown"}
        if failure_state and failure is None:
            raise CommissioningLifecycleError(
                f"transition to {to_state} requires a failure code"
            )
        if not failure_state and failure is not None:
            raise CommissioningLifecycleError(
                "only a blocked transition may carry a failure code"
            )
        object.__setattr__(self, "from_state", from_state)
        object.__setattr__(self, "to_state", to_state)
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(self, "evidence_fingerprint", evidence)
        object.__setattr__(self, "failure_code", failure)
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_commissioning_transition",
            "from_state": self.from_state,
            "to_state": self.to_state,
            "evidence_kind": self.evidence_kind,
            "evidence_fingerprint": self.evidence_fingerprint,
            "failure_code": self.failure_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CommissioningTransition":
        if not isinstance(raw, Mapping):
            raise CommissioningLifecycleError(
                "commissioning transition must be an object"
            )
        expected = {
            "schema_version",
            "kind",
            "from_state",
            "to_state",
            "evidence_kind",
            "evidence_fingerprint",
            "failure_code",
            "fingerprint",
        }
        if set(raw) != expected:
            raise CommissioningLifecycleError(
                "commissioning transition has unknown or missing fields"
            )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise CommissioningLifecycleError(
                "unsupported commissioning transition schema"
            )
        if raw["kind"] != "jts_active_commissioning_transition":
            raise CommissioningLifecycleError(
                "unsupported commissioning transition kind"
            )
        result = cls(
            from_state=raw["from_state"],
            to_state=raw["to_state"],
            evidence_kind=raw["evidence_kind"],
            evidence_fingerprint=raw["evidence_fingerprint"],
            failure_code=raw["failure_code"],
        )
        if raw["fingerprint"] != result.fingerprint:
            raise CommissioningLifecycleError(
                "declared transition fingerprint does not match payload"
            )
        return result
