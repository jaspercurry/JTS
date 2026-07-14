# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict Active-owned commissioning receipt for downstream Room gating.

This is an inert authority model.  A later Active integration shell will derive
the exact target plan from the current topology, mint applied-graph, read-back,
and protection proof, and persist the receipt.  Nothing here reads files,
mutates CamillaDSP, or changes the current Room gate.

The current Active measurement bundles are intentionally fail-soft historical
evidence and are **not** commissioning authority.  Wave 2 must create this
separate fail-closed authority chain for a fresh production commissioning
session. Historical bundles are permanently non-admitted: no migration,
backfill, marker copy, or synthesized admission artifact may promote them.
Merely deserializing a legacy bundle into one of these types is never
sufficient.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, TypeAlias, cast

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    EvidenceIdentityError,
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import ExcitationAdmission
from jasper.audio_measurement.excitation_artifacts import (
    GENERATION_PATH_PREFIX,
    PLAYBACK_PATH_PREFIX,
    admission_artifact_relative_path,
    canonical_admission_bytes,
)
from jasper.output_topology import OutputTopology, OutputTopologyError

from .bundles import BUNDLE_KIND
from .measurement import active_summed_targets

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
POST_APPLY_CONSUMER_ID = "active_crossover"
POST_APPLY_MEASUREMENT_KIND = "active_crossover_post_apply"
REFERENCE_AXIS_GEOMETRY_ID = "reference_axis"
POST_APPLY_REQUIRED_REPEATS = 3
POST_APPLY_VERIFICATION_ALGORITHM_ID = "jts_active_post_apply_target_verification"
POST_APPLY_VERIFICATION_ALGORITHM_VERSION = "1"
COMBINED_ACTIVE_GROUP_TARGET_PREFIX = "combined_active_group:"

MutationState: TypeAlias = Literal["not_attempted", "attempted", "applied", "unknown"]
RollbackStatus: TypeAlias = Literal[
    "not_applicable", "not_required", "restored", "failed", "unknown"
]
RollbackEvidenceKind: TypeAlias = Literal[
    "no_mutation",
    "retained_apply",
    "exact_restore",
    "uncertain_mutation",
]

ROLLBACK_FAILURE_CODES = frozenset(
    {
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

_PRE_MUTATION_ROLLBACK_FAILURE_CODES = frozenset(
    {
        "writer_lock_unavailable",
        "candidate_apply_failed_before_mutation",
    }
)
_POST_MUTATION_TRIGGER_FAILURE_CODES = frozenset(
    {
        "mutation_outcome_unknown",
        "fresh_readback_failed",
        "candidate_readback_mismatch",
        "protection_proof_failed",
        "post_apply_verification_failed",
    }
)
_ROLLBACK_OPERATION_FAILURE_CODES = frozenset(
    {
        "rollback_apply_failed",
        "rollback_readback_failed",
        "rollback_readback_mismatch",
    }
)
_FAILED_ROLLBACK_FAILURE_CODES = _ROLLBACK_OPERATION_FAILURE_CODES - {
    "rollback_readback_failed"
}
_UNKNOWN_ROLLBACK_FAILURE_CODES = _POST_MUTATION_TRIGGER_FAILURE_CODES | (
    _ROLLBACK_OPERATION_FAILURE_CODES - _FAILED_ROLLBACK_FAILURE_CODES
)


class CommissioningReceiptError(ValueError):
    """Commissioning authority is incomplete, stale, or contradictory."""


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CommissioningReceiptError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


def _optional_text(value: Any, *, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name=field_name)


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CommissioningReceiptError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _fingerprint(payload: Mapping[str, Any]) -> str:
    try:
        return json_fingerprint(payload, field_name="commissioning receipt payload")
    except EvidenceIdentityError as exc:
        raise CommissioningReceiptError(str(exc)) from exc


def _canonical_json_bytes(value: Any, *, field_name: str) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommissioningReceiptError(
            f"{field_name} must be canonical JSON: {exc}"
        ) from exc


def _strict_serialized_object(
    raw: Any,
    *,
    kind: str,
    fields: frozenset[str],
    schema_version: int = 1,
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise CommissioningReceiptError(f"{kind} must be an object")
    expected = fields | {"schema_version", "kind", "fingerprint"}
    if set(raw) != expected:
        raise CommissioningReceiptError(f"{kind} has unknown or missing fields")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != schema_version
    ):
        raise CommissioningReceiptError(f"unsupported {kind} schema")
    if raw["kind"] != kind:
        raise CommissioningReceiptError(f"unsupported {kind} kind")
    return raw


def _declared_fingerprint(raw: Mapping[str, Any], actual: str) -> None:
    if raw["fingerprint"] != actual:
        raise CommissioningReceiptError("declared fingerprint does not match payload")


def _raw(raw: Mapping[str, Any], name: str) -> Any:
    return raw[name]


def _combined_active_group_target_id(speaker_group_id: str) -> str:
    return f"{COMBINED_ACTIVE_GROUP_TARGET_PREFIX}{speaker_group_id}"


def _topology_authority_fingerprint(topology: OutputTopology) -> str:
    if not isinstance(topology, OutputTopology):
        raise CommissioningReceiptError("topology must be OutputTopology")
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_required_target_topology",
            "topology": topology.to_dict(),
        }
    )


def _admission_id_from_role_path(
    artifact: ArtifactIdentity,
    *,
    role_prefix: str,
    field_name: str,
) -> str:
    path = PurePosixPath(artifact.relative_path)
    role = "generation" if role_prefix == GENERATION_PATH_PREFIX else "playback"
    try:
        expected_path = admission_artifact_relative_path(role, path.stem)
    except ValueError as exc:
        raise CommissioningReceiptError(str(exc)) from exc
    if artifact.relative_path != expected_path:
        raise CommissioningReceiptError(
            f"{field_name} must use the canonical {PurePosixPath(role_prefix).name} path role"
        )
    return path.stem


@dataclass(frozen=True)
class RequiredVerificationTarget:
    """One topology-derived fixed reference-axis verification target."""

    speaker_group_id: str
    target_id: str
    target_fingerprint: str
    geometry_id: str
    placement_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("speaker_group_id", "target_id", "geometry_id"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.geometry_id != REFERENCE_AXIS_GEOMETRY_ID:
            raise CommissioningReceiptError(
                "commissioning verification target must use reference_axis geometry"
            )
        expected_target_id = _combined_active_group_target_id(self.speaker_group_id)
        if self.target_id != expected_target_id:
            raise CommissioningReceiptError(
                "commissioning target must identify the combined active speaker group"
            )
        for name in ("target_fingerprint", "placement_fingerprint"):
            object.__setattr__(
                self, name, _sha256(getattr(self, name), field_name=name)
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def canonical_key(self) -> tuple[str, str]:
        return self.speaker_group_id, self.target_id

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_required_verification_target",
            "speaker_group_id": self.speaker_group_id,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "geometry_id": self.geometry_id,
            "placement_fingerprint": self.placement_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "RequiredVerificationTarget":
        value = _strict_serialized_object(
            raw,
            kind="jts_active_required_verification_target",
            fields=frozenset(
                {
                    "speaker_group_id",
                    "target_id",
                    "target_fingerprint",
                    "geometry_id",
                    "placement_fingerprint",
                }
            ),
        )
        result = cls(
            speaker_group_id=_raw(value, "speaker_group_id"),
            target_id=_raw(value, "target_id"),
            target_fingerprint=_raw(value, "target_fingerprint"),
            geometry_id=_raw(value, "geometry_id"),
            placement_fingerprint=_raw(value, "placement_fingerprint"),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True)
class RequiredTargetPlan:
    """Canonical exact target set derived from one immutable topology."""

    topology: OutputTopology
    topology_id: str
    topology_fingerprint: str
    targets: tuple[RequiredVerificationTarget, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.topology, OutputTopology):
            raise CommissioningReceiptError("topology must be OutputTopology")
        # Store the same canonical snapshot that is fingerprinted and serialized.
        # In-memory topology ``status`` may still be the operator's draft hint,
        # while ``to_dict`` derives the current evaluated status.  Round-tripping
        # once prevents that non-authoritative hint from making typed equality
        # differ after an otherwise byte-identical receipt reload.
        try:
            topology = OutputTopology.from_mapping(self.topology.to_dict())
        except OutputTopologyError as exc:  # pragma: no cover - typed input guards it
            raise CommissioningReceiptError(str(exc)) from exc
        object.__setattr__(self, "topology", topology)
        if topology.evaluation().get("status") != "verified":
            raise CommissioningReceiptError(
                "required target plan requires a verified output topology"
            )
        object.__setattr__(
            self, "topology_id", _text(self.topology_id, field_name="topology_id")
        )
        object.__setattr__(
            self,
            "topology_fingerprint",
            _sha256(self.topology_fingerprint, field_name="topology_fingerprint"),
        )
        if type(self.targets) is not tuple or not self.targets:
            raise CommissioningReceiptError(
                "required targets must be a non-empty tuple"
            )
        if any(
            not isinstance(item, RequiredVerificationTarget) for item in self.targets
        ):
            raise CommissioningReceiptError(
                "required targets must be RequiredVerificationTarget values"
            )
        keys = tuple(item.canonical_key for item in self.targets)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise CommissioningReceiptError(
                "required targets must be unique and in canonical order"
            )
        if self.topology_id != self.topology.topology_id:
            raise CommissioningReceiptError(
                "required target plan topology id does not match its topology snapshot"
            )
        expected_topology_fingerprint = _topology_authority_fingerprint(self.topology)
        if self.topology_fingerprint != expected_topology_fingerprint:
            raise CommissioningReceiptError(
                "required target plan topology fingerprint does not match its snapshot"
            )
        summed_targets = active_summed_targets(self.topology)
        expected_targets = {
            str(target["speaker_group_id"]): (
                _combined_active_group_target_id(str(target["speaker_group_id"])),
                _sha256(
                    target.get("group_fingerprint"),
                    field_name="active summed target fingerprint",
                ),
            )
            for target in summed_targets
        }
        observed_groups = {target.speaker_group_id for target in self.targets}
        if not expected_targets or observed_groups != set(expected_targets):
            raise CommissioningReceiptError(
                "required targets must exactly equal active summed topology targets"
            )
        for target in self.targets:
            expected_target_id, expected_target_fingerprint = expected_targets[
                target.speaker_group_id
            ]
            if (
                target.target_id != expected_target_id
                or target.target_fingerprint != expected_target_fingerprint
            ):
                raise CommissioningReceiptError(
                    "required target identity does not match the topology snapshot"
                )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_required_target_plan",
            "topology": self.topology.to_dict(),
            "topology_id": self.topology_id,
            "topology_fingerprint": self.topology_fingerprint,
            "targets": [target.to_dict() for target in self.targets],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_topology(
        cls,
        topology: OutputTopology,
        *,
        placement_fingerprints: Mapping[str, str],
    ) -> "RequiredTargetPlan":
        """Build the exact combined-group target set from current topology.

        Placement evidence is an explicit input because microphone placement is
        not topology data.  Its keys must exactly equal the active summed group
        endpoints; extras and omissions fail closed.
        """

        if not isinstance(topology, OutputTopology):
            raise CommissioningReceiptError("topology must be OutputTopology")
        if not isinstance(placement_fingerprints, Mapping) or any(
            not isinstance(key, str) for key in placement_fingerprints
        ):
            raise CommissioningReceiptError(
                "placement_fingerprints must map speaker group ids to fingerprints"
            )
        summed = active_summed_targets(topology)
        group_ids = {str(target["speaker_group_id"]) for target in summed}
        if set(placement_fingerprints) != group_ids:
            raise CommissioningReceiptError(
                "placement fingerprints must exactly equal active summed targets"
            )
        targets = tuple(
            sorted(
                (
                    RequiredVerificationTarget(
                        speaker_group_id=str(target["speaker_group_id"]),
                        target_id=_combined_active_group_target_id(
                            str(target["speaker_group_id"])
                        ),
                        target_fingerprint=_sha256(
                            target.get("group_fingerprint"),
                            field_name="active summed target fingerprint",
                        ),
                        geometry_id=REFERENCE_AXIS_GEOMETRY_ID,
                        placement_fingerprint=_sha256(
                            placement_fingerprints[str(target["speaker_group_id"])],
                            field_name="placement_fingerprint",
                        ),
                    )
                    for target in summed
                ),
                key=lambda item: item.canonical_key,
            )
        )
        if not targets:
            raise CommissioningReceiptError(
                "current topology has no combined active speaker group targets"
            )
        return cls(
            topology=topology,
            topology_id=topology.topology_id,
            topology_fingerprint=_topology_authority_fingerprint(topology),
            targets=targets,
        )

    def matches_current_topology(
        self,
        topology: OutputTopology,
        *,
        placement_fingerprints: Mapping[str, str],
    ) -> bool:
        """Return whether current topology yields this exact target authority."""

        try:
            current = type(self).from_topology(
                topology,
                placement_fingerprints=placement_fingerprints,
            )
        except CommissioningReceiptError:
            return False
        return current.fingerprint == self.fingerprint

    @classmethod
    def from_mapping(cls, raw: Any) -> "RequiredTargetPlan":
        value = _strict_serialized_object(
            raw,
            kind="jts_active_required_target_plan",
            fields=frozenset(
                {"topology", "topology_id", "topology_fingerprint", "targets"}
            ),
        )
        targets = value["targets"]
        if type(targets) is not list:
            raise CommissioningReceiptError("required targets must be a list")
        try:
            topology = OutputTopology.from_mapping(value["topology"])
        except OutputTopologyError as exc:
            raise CommissioningReceiptError(str(exc)) from exc
        if _canonical_json_bytes(
            value["topology"],
            field_name="required target plan topology snapshot",
        ) != _canonical_json_bytes(
            topology.to_dict(),
            field_name="required target plan canonical topology",
        ):
            raise CommissioningReceiptError(
                "required target plan topology snapshot is not canonical"
            )
        result = cls(
            topology=topology,
            topology_id=_raw(value, "topology_id"),
            topology_fingerprint=_raw(value, "topology_fingerprint"),
            targets=tuple(
                RequiredVerificationTarget.from_mapping(item) for item in targets
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True)
class AppliedCandidateProof:
    """Positive candidate→applied fresh-readback and protection authority.

    Expected and observed graphs are typed, versioned normalized ``active_raw``
    identities.  The predecessor is a separate exact transaction-state type;
    neither a candidate hash nor a same-looking graph hash may substitute for
    the state required by rollback.
    """

    operation_id: str
    target_plan_fingerprint: str
    safety_profile_fingerprint: str
    candidate_fingerprint: str
    predecessor_state: ExactDspStateIdentity
    expected_normalized_graph: NormalizedActiveRawIdentity
    observed_fresh_readback_graph: NormalizedActiveRawIdentity
    writer_lock_fingerprint: str
    mutation_fingerprint: str
    fresh_readback_fingerprint: str
    protection_proof_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _text(self.operation_id, field_name="operation_id")
        )
        for name in (
            "target_plan_fingerprint",
            "safety_profile_fingerprint",
            "candidate_fingerprint",
            "writer_lock_fingerprint",
            "mutation_fingerprint",
            "fresh_readback_fingerprint",
            "protection_proof_fingerprint",
        ):
            object.__setattr__(
                self, name, _sha256(getattr(self, name), field_name=name)
            )
        if not isinstance(self.predecessor_state, ExactDspStateIdentity):
            raise CommissioningReceiptError(
                "predecessor_state must be ExactDspStateIdentity"
            )
        if not isinstance(
            self.expected_normalized_graph, NormalizedActiveRawIdentity
        ) or not isinstance(
            self.observed_fresh_readback_graph, NormalizedActiveRawIdentity
        ):
            raise CommissioningReceiptError(
                "candidate and readback graphs must be NormalizedActiveRawIdentity"
            )
        if (
            self.observed_fresh_readback_graph.fingerprint
            != self.expected_normalized_graph.fingerprint
        ):
            raise CommissioningReceiptError(
                "fresh active_raw readback must equal the expected normalized graph"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_applied_candidate_proof",
            "operation_id": self.operation_id,
            "target_plan_fingerprint": self.target_plan_fingerprint,
            "safety_profile_fingerprint": self.safety_profile_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "predecessor_state": self.predecessor_state.to_dict(),
            "expected_normalized_graph": self.expected_normalized_graph.to_dict(),
            "observed_fresh_readback_graph": (
                self.observed_fresh_readback_graph.to_dict()
            ),
            "writer_lock_fingerprint": self.writer_lock_fingerprint,
            "mutation_fingerprint": self.mutation_fingerprint,
            "fresh_readback_fingerprint": self.fresh_readback_fingerprint,
            "protection_proof_fingerprint": self.protection_proof_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "AppliedCandidateProof":
        fields = frozenset(
            {
                "operation_id",
                "target_plan_fingerprint",
                "safety_profile_fingerprint",
                "candidate_fingerprint",
                "predecessor_state",
                "expected_normalized_graph",
                "observed_fresh_readback_graph",
                "writer_lock_fingerprint",
                "mutation_fingerprint",
                "fresh_readback_fingerprint",
                "protection_proof_fingerprint",
            }
        )
        value = _strict_serialized_object(
            raw, kind="jts_active_applied_candidate_proof", fields=fields
        )
        scalar_fields = fields - {
            "predecessor_state",
            "expected_normalized_graph",
            "observed_fresh_readback_graph",
        }
        try:
            result = cls(
                **{name: _raw(value, name) for name in scalar_fields},
                predecessor_state=ExactDspStateIdentity.from_mapping(
                    value["predecessor_state"]
                ),
                expected_normalized_graph=NormalizedActiveRawIdentity.from_mapping(
                    value["expected_normalized_graph"]
                ),
                observed_fresh_readback_graph=(
                    NormalizedActiveRawIdentity.from_mapping(
                        value["observed_fresh_readback_graph"]
                    )
                ),
            )
        except EvidenceIdentityError as exc:
            raise CommissioningReceiptError(str(exc)) from exc
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True)
class CommissioningRollbackEvidence:
    """Typed mutation outcome and exact-state rollback evidence.

    ``attempted`` means the mutation call began without a confirmed applied
    result; ``unknown`` means the host cannot establish what graph is live.
    Neither may be collapsed into ``not_attempted``.  Only typed exact-state
    equality can claim ``restored``.
    """

    mutation_state: MutationState
    status: RollbackStatus
    evidence_kind: RollbackEvidenceKind
    operation_id: str
    mutation_fingerprint: str | None = None
    observed_applied_graph_fingerprint: str | None = None
    predecessor_state: ExactDspStateIdentity | None = None
    restored_state: ExactDspStateIdentity | None = None
    failure_code: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.mutation_state not in {
            "not_attempted",
            "attempted",
            "applied",
            "unknown",
        }:
            raise CommissioningReceiptError("invalid rollback mutation_state")
        if self.status not in {
            "not_applicable",
            "not_required",
            "restored",
            "failed",
            "unknown",
        }:
            raise CommissioningReceiptError("invalid rollback status")
        if self.evidence_kind not in {
            "no_mutation",
            "retained_apply",
            "exact_restore",
            "uncertain_mutation",
        }:
            raise CommissioningReceiptError("invalid rollback evidence_kind")
        mutation_state = cast(MutationState, self.mutation_state)
        status = cast(RollbackStatus, self.status)
        evidence_kind = cast(RollbackEvidenceKind, self.evidence_kind)
        object.__setattr__(self, "mutation_state", mutation_state)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "evidence_kind", evidence_kind)
        operation_id = _text(self.operation_id, field_name="operation_id")
        mutation_fingerprint = (
            None
            if self.mutation_fingerprint is None
            else _sha256(
                self.mutation_fingerprint,
                field_name="mutation_fingerprint",
            )
        )
        observed_applied_graph_fingerprint = (
            None
            if self.observed_applied_graph_fingerprint is None
            else _sha256(
                self.observed_applied_graph_fingerprint,
                field_name="observed_applied_graph_fingerprint",
            )
        )
        predecessor = self.predecessor_state
        restored = self.restored_state
        if predecessor is not None and not isinstance(
            predecessor, ExactDspStateIdentity
        ):
            raise CommissioningReceiptError(
                "predecessor_state must be ExactDspStateIdentity or None"
            )
        if restored is not None and not isinstance(restored, ExactDspStateIdentity):
            raise CommissioningReceiptError(
                "restored_state must be ExactDspStateIdentity or None"
            )
        failure = _optional_text(self.failure_code, field_name="failure_code")
        if failure is not None and failure not in ROLLBACK_FAILURE_CODES:
            raise CommissioningReceiptError("unsupported rollback failure_code")
        if mutation_state == "not_attempted":
            if (
                status != "not_applicable"
                or evidence_kind != "no_mutation"
                or mutation_fingerprint is not None
                or observed_applied_graph_fingerprint is not None
                or any(item is not None for item in (predecessor, restored))
                or failure not in _PRE_MUTATION_ROLLBACK_FAILURE_CODES
            ):
                raise CommissioningReceiptError(
                    "failed-before-mutation requires a pre-mutation failure code"
                )
        elif status == "not_required":
            if (
                mutation_state != "applied"
                or evidence_kind != "retained_apply"
                or mutation_fingerprint is None
                or observed_applied_graph_fingerprint is None
                or predecessor is None
                or restored is not None
                or failure is not None
            ):
                raise CommissioningReceiptError(
                    "retained applied graph requires predecessor and no failure"
                )
        elif status == "restored":
            if (
                mutation_state not in {"attempted", "applied", "unknown"}
                or evidence_kind != "exact_restore"
                or mutation_fingerprint is None
                or predecessor is None
                or restored is None
                or restored.fingerprint != predecessor.fingerprint
                or failure not in _POST_MUTATION_TRIGGER_FAILURE_CODES
            ):
                raise CommissioningReceiptError(
                    "restored rollback requires exact predecessor and its mutation failure"
                )
        elif status == "failed":
            if (
                mutation_state not in {"attempted", "applied", "unknown"}
                or evidence_kind != "uncertain_mutation"
                or mutation_fingerprint is None
                or predecessor is None
                or (
                    restored is not None
                    and restored.fingerprint == predecessor.fingerprint
                )
                or failure not in _FAILED_ROLLBACK_FAILURE_CODES
            ):
                raise CommissioningReceiptError(
                    "failed rollback requires a rollback-operation failure code"
                )
        elif status == "unknown":
            if (
                mutation_state not in {"attempted", "applied", "unknown"}
                or evidence_kind != "uncertain_mutation"
                or mutation_fingerprint is None
                or predecessor is None
                or restored is not None
                or failure not in _UNKNOWN_ROLLBACK_FAILURE_CODES
            ):
                raise CommissioningReceiptError(
                    "unknown mutation outcome requires predecessor and failure evidence"
                )
        else:
            raise CommissioningReceiptError(
                "post-attempt mutation cannot use not_applicable rollback"
            )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "mutation_fingerprint", mutation_fingerprint)
        object.__setattr__(
            self,
            "observed_applied_graph_fingerprint",
            observed_applied_graph_fingerprint,
        )
        object.__setattr__(self, "failure_code", failure)
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_commissioning_rollback_evidence",
            "mutation_state": self.mutation_state,
            "status": self.status,
            "evidence_kind": self.evidence_kind,
            "operation_id": self.operation_id,
            "mutation_fingerprint": self.mutation_fingerprint,
            "observed_applied_graph_fingerprint": (
                self.observed_applied_graph_fingerprint
            ),
            "predecessor_state": (
                self.predecessor_state.to_dict()
                if self.predecessor_state is not None
                else None
            ),
            "restored_state": (
                self.restored_state.to_dict()
                if self.restored_state is not None
                else None
            ),
            "failure_code": self.failure_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CommissioningRollbackEvidence":
        fields = frozenset(
            {
                "mutation_state",
                "status",
                "evidence_kind",
                "operation_id",
                "mutation_fingerprint",
                "observed_applied_graph_fingerprint",
                "predecessor_state",
                "restored_state",
                "failure_code",
            }
        )
        value = _strict_serialized_object(
            raw, kind="jts_active_commissioning_rollback_evidence", fields=fields
        )
        try:
            predecessor = (
                None
                if value["predecessor_state"] is None
                else ExactDspStateIdentity.from_mapping(value["predecessor_state"])
            )
            restored = (
                None
                if value["restored_state"] is None
                else ExactDspStateIdentity.from_mapping(value["restored_state"])
            )
        except EvidenceIdentityError as exc:
            raise CommissioningReceiptError(str(exc)) from exc
        result = cls(
            mutation_state=_raw(value, "mutation_state"),
            status=_raw(value, "status"),
            evidence_kind=_raw(value, "evidence_kind"),
            operation_id=_raw(value, "operation_id"),
            mutation_fingerprint=_raw(value, "mutation_fingerprint"),
            observed_applied_graph_fingerprint=_raw(
                value, "observed_applied_graph_fingerprint"
            ),
            predecessor_state=predecessor,
            restored_state=restored,
            failure_code=_raw(value, "failure_code"),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def commissioning_context_fingerprint(
    *,
    target_plan: RequiredTargetPlan,
    applied_candidate: AppliedCandidateProof,
) -> str:
    if not isinstance(target_plan, RequiredTargetPlan):
        raise CommissioningReceiptError("target_plan must be RequiredTargetPlan")
    if not isinstance(applied_candidate, AppliedCandidateProof):
        raise CommissioningReceiptError(
            "applied_candidate must be AppliedCandidateProof"
        )
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_commissioning_context",
            "target_plan_fingerprint": target_plan.fingerprint,
            "safety_profile_fingerprint": applied_candidate.safety_profile_fingerprint,
            "candidate_fingerprint": applied_candidate.candidate_fingerprint,
            "observed_fresh_readback_graph_fingerprint": (
                applied_candidate.observed_fresh_readback_graph.fingerprint
            ),
            "applied_candidate_proof_fingerprint": applied_candidate.fingerprint,
        }
    )


@dataclass(frozen=True)
class AdmittedCaptureProof:
    """Typed positive admission proof for one exact captured artifact set.

    The trusted host issues this only after parsing the shared excitation
    admission result and observing ``allowed=true``.  The proof binds that
    generation decision to a separate generation-role artifact and one
    independently re-admitted playback decision to the final admission artifact
    retained by the capture.  Both bind exact canonical typed bytes, one shared
    admission ID, authority/plan, target context, and commissioning session.
    This pure type does not read either artifact from disk.
    """

    capture: CaptureIdentity
    commissioning_session_id: str
    generation_admission: ExcitationAdmission
    admission: ExcitationAdmission
    generation_artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capture, CaptureIdentity):
            raise CommissioningReceiptError("capture must be CaptureIdentity")
        object.__setattr__(
            self,
            "commissioning_session_id",
            _text(
                self.commissioning_session_id,
                field_name="commissioning_session_id",
            ),
        )
        if not isinstance(self.generation_admission, ExcitationAdmission):
            raise CommissioningReceiptError(
                "generation_admission must be a typed ExcitationAdmission"
            )
        if not isinstance(self.admission, ExcitationAdmission):
            raise CommissioningReceiptError(
                "admission must be a typed playback ExcitationAdmission"
            )
        if not isinstance(self.generation_artifact, ArtifactIdentity):
            raise CommissioningReceiptError(
                "generation_artifact must be an ArtifactIdentity"
            )
        if not self.generation_admission.allowed:
            raise CommissioningReceiptError(
                "admitted capture proof requires an allowed generation admission"
            )
        if not self.admission.allowed:
            raise CommissioningReceiptError(
                "admitted capture proof requires an allowed playback admission"
            )
        if (
            self.generation_admission.request.target_fingerprint
            != self.capture.target_fingerprint
            or self.admission.request.target_fingerprint
            != self.capture.target_fingerprint
        ):
            raise CommissioningReceiptError(
                "generation and playback admission targets must equal the capture target"
            )
        if self.commissioning_session_id != self.capture.raw_artifact.bundle_id:
            raise CommissioningReceiptError(
                "admitted capture proof must belong to its commissioning session"
            )
        capture_bundle = (
            self.capture.raw_artifact.bundle_kind,
            self.capture.raw_artifact.bundle_id,
        )
        if capture_bundle != (BUNDLE_KIND, self.commissioning_session_id):
            raise CommissioningReceiptError(
                "admitted capture proof requires the Active commissioning bundle kind"
            )
        if (
            self.generation_artifact.bundle_kind,
            self.generation_artifact.bundle_id,
        ) != capture_bundle:
            raise CommissioningReceiptError(
                "generation, playback, and capture artifacts must share one authority bundle"
            )
        capture_artifacts = (
            self.capture.raw_artifact,
            self.capture.analysis_input_artifact,
            self.capture.quality_artifact,
            self.capture.admission_artifact,
        )
        if self.generation_artifact.fingerprint in {
            artifact.fingerprint for artifact in capture_artifacts
        } or self.generation_artifact.relative_path in {
            artifact.relative_path for artifact in capture_artifacts
        }:
            raise CommissioningReceiptError(
                "generation admission requires a distinct capture artifact role"
            )
        generation_id = _admission_id_from_role_path(
            self.generation_artifact,
            role_prefix=GENERATION_PATH_PREFIX,
            field_name="generation admission artifact",
        )
        playback_id = _admission_id_from_role_path(
            self.capture.admission_artifact,
            role_prefix=PLAYBACK_PATH_PREFIX,
            field_name="capture admission artifact",
        )
        if playback_id != generation_id:
            raise CommissioningReceiptError(
                "generation and playback artifacts must share one admission ID"
            )
        if (
            self.admission.request != self.generation_admission.request
            or self.admission.limits != self.generation_admission.limits
        ):
            raise CommissioningReceiptError(
                "playback admission must retain its generation request and limits"
            )
        canonical_generation = canonical_admission_bytes(self.generation_admission)
        if self.generation_artifact.sha256 != hashlib.sha256(
            canonical_generation
        ).hexdigest() or self.generation_artifact.byte_size != len(
            canonical_generation
        ):
            raise CommissioningReceiptError(
                "generation admission artifact must equal canonical typed admission bytes"
            )
        canonical_playback = canonical_admission_bytes(self.admission)
        if self.capture.admission_artifact.sha256 != hashlib.sha256(
            canonical_playback
        ).hexdigest() or self.capture.admission_artifact.byte_size != len(
            canonical_playback
        ):
            raise CommissioningReceiptError(
                "playback admission artifact must equal canonical typed admission bytes"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def admission_decision_fingerprint(self) -> str:
        return self.admission.fingerprint

    @property
    def admission_id(self) -> str:
        """Shared canonical generation/playback attempt identity."""

        return PurePosixPath(self.capture.admission_artifact.relative_path).stem

    @property
    def playback_artifact(self) -> ArtifactIdentity:
        """Final playback decision retained by the canonical capture identity."""

        return self.capture.admission_artifact

    @property
    def authority_fingerprint(self) -> str:
        return self.admission.limits.fingerprint

    @property
    def excitation_plan_fingerprint(self) -> str:
        return self.admission.limits.excitation_plan_fingerprint

    @property
    def safety_profile_fingerprint(self) -> str:
        return self.admission.limits.safety_profile_fingerprint

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "kind": "jts_active_admitted_capture_proof",
            "capture": self.capture.to_dict(),
            "commissioning_session_id": self.commissioning_session_id,
            "generation_admission": self.generation_admission.to_dict(),
            "admission": self.admission.to_dict(),
            "generation_artifact": self.generation_artifact.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "AdmittedCaptureProof":
        value = _strict_serialized_object(
            raw,
            kind="jts_active_admitted_capture_proof",
            fields=frozenset(
                {
                    "capture",
                    "commissioning_session_id",
                    "generation_admission",
                    "admission",
                    "generation_artifact",
                }
            ),
            schema_version=2,
        )
        try:
            capture = CaptureIdentity.from_mapping(value["capture"])
            generation_admission = ExcitationAdmission.from_dict(
                value["generation_admission"]
            )
            admission = ExcitationAdmission.from_dict(value["admission"])
            generation_artifact = ArtifactIdentity.from_mapping(
                value["generation_artifact"]
            )
        except (EvidenceIdentityError, ValueError) as exc:
            raise CommissioningReceiptError(str(exc)) from exc
        result = cls(
            capture=capture,
            commissioning_session_id=_raw(value, "commissioning_session_id"),
            generation_admission=generation_admission,
            admission=admission,
            generation_artifact=generation_artifact,
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True)
class PostApplyTargetVerification:
    """Typed passing verdict over one exact three-capture target set."""

    speaker_group_id: str
    target_id: str
    target_fingerprint: str
    geometry_id: str
    placement_fingerprint: str
    commissioning_session_id: str
    commissioning_context_fingerprint: str
    verification_algorithm_id: str
    verification_algorithm_version: str
    threshold_profile_fingerprint: str
    verdict: Literal["passed"]
    admitted_captures: tuple[AdmittedCaptureProof, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("speaker_group_id", "target_id", "geometry_id"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if self.geometry_id != REFERENCE_AXIS_GEOMETRY_ID:
            raise CommissioningReceiptError(
                "post-apply verification must use reference_axis geometry"
            )
        for name in ("target_fingerprint", "placement_fingerprint"):
            object.__setattr__(
                self, name, _sha256(getattr(self, name), field_name=name)
            )
        object.__setattr__(
            self,
            "commissioning_session_id",
            _text(
                self.commissioning_session_id,
                field_name="commissioning_session_id",
            ),
        )
        object.__setattr__(
            self,
            "commissioning_context_fingerprint",
            _sha256(
                self.commissioning_context_fingerprint,
                field_name="commissioning_context_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "threshold_profile_fingerprint",
            _sha256(
                self.threshold_profile_fingerprint,
                field_name="threshold_profile_fingerprint",
            ),
        )
        if self.verification_algorithm_id != POST_APPLY_VERIFICATION_ALGORITHM_ID:
            raise CommissioningReceiptError(
                "unsupported post-apply verification algorithm"
            )
        if (
            self.verification_algorithm_version
            != POST_APPLY_VERIFICATION_ALGORITHM_VERSION
        ):
            raise CommissioningReceiptError(
                "unsupported post-apply verification algorithm version"
            )
        if self.verdict != "passed":
            raise CommissioningReceiptError(
                "post-apply target authority requires a passing verdict"
            )
        if (
            type(self.admitted_captures) is not tuple
            or len(self.admitted_captures) != POST_APPLY_REQUIRED_REPEATS
        ):
            raise CommissioningReceiptError(
                f"post-apply target requires exactly {POST_APPLY_REQUIRED_REPEATS} captures"
            )
        if any(
            not isinstance(proof, AdmittedCaptureProof)
            for proof in self.admitted_captures
        ):
            raise CommissioningReceiptError(
                "post-apply captures must be AdmittedCaptureProof values"
            )
        captures = tuple(proof.capture for proof in self.admitted_captures)
        if any(
            capture.consumer_id != POST_APPLY_CONSUMER_ID
            or capture.measurement_kind != POST_APPLY_MEASUREMENT_KIND
            or capture.target_fingerprint != self.target_fingerprint
            or capture.geometry_id != self.geometry_id
            or capture.placement_fingerprint != self.placement_fingerprint
            or capture.context_fingerprint != self.commissioning_context_fingerprint
            for capture in captures
        ):
            raise CommissioningReceiptError(
                "post-apply capture target, geometry, placement, or consumer is stale"
            )
        if any(
            proof.commissioning_session_id != self.commissioning_session_id
            for proof in self.admitted_captures
        ):
            raise CommissioningReceiptError(
                "post-apply captures must belong to one commissioning session"
            )
        admission_ids = [proof.admission_id for proof in self.admitted_captures]
        if len(set(admission_ids)) != POST_APPLY_REQUIRED_REPEATS:
            raise CommissioningReceiptError(
                "post-apply captures require distinct one-shot admission IDs"
            )
        if any(
            proof.generation_admission.request.repeat_count != 1
            or proof.admission.request.repeat_count != 1
            for proof in self.admitted_captures
        ):
            raise CommissioningReceiptError(
                "post-apply generation and playback admissions must authorize exactly one playback"
            )
        generation_artifacts = [
            proof.generation_artifact for proof in self.admitted_captures
        ]
        playback_artifacts = [
            proof.playback_artifact for proof in self.admitted_captures
        ]
        generation_identities = [
            artifact.fingerprint for artifact in generation_artifacts
        ]
        generation_paths = [artifact.relative_path for artifact in generation_artifacts]
        playback_identities = [artifact.fingerprint for artifact in playback_artifacts]
        playback_paths = [artifact.relative_path for artifact in playback_artifacts]
        if (
            len(set(generation_identities)) != POST_APPLY_REQUIRED_REPEATS
            or len(set(generation_paths)) != POST_APPLY_REQUIRED_REPEATS
        ):
            raise CommissioningReceiptError(
                "post-apply generation admission artifacts must be unique"
            )
        if (
            len(set(playback_identities)) != POST_APPLY_REQUIRED_REPEATS
            or len(set(playback_paths)) != POST_APPLY_REQUIRED_REPEATS
        ):
            raise CommissioningReceiptError(
                "post-apply playback admission artifacts must be unique"
            )
        capture_ids = [capture.capture_id for capture in captures]
        raw_artifacts = [capture.raw_artifact.fingerprint for capture in captures]
        raw_content_hashes = [capture.raw_artifact.sha256 for capture in captures]
        if (
            len(set(capture_ids)) != POST_APPLY_REQUIRED_REPEATS
            or len(set(raw_artifacts)) != POST_APPLY_REQUIRED_REPEATS
            or len(set(raw_content_hashes)) != POST_APPLY_REQUIRED_REPEATS
        ):
            raise CommissioningReceiptError(
                "post-apply captures and raw artifacts must be unique"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def captures(self) -> tuple[CaptureIdentity, ...]:
        return tuple(proof.capture for proof in self.admitted_captures)

    @property
    def required_target_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.speaker_group_id,
            self.target_id,
            self.target_fingerprint,
            self.geometry_id,
            self.placement_fingerprint,
        )

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "kind": "jts_active_post_apply_target_verification",
            "speaker_group_id": self.speaker_group_id,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "geometry_id": self.geometry_id,
            "placement_fingerprint": self.placement_fingerprint,
            "commissioning_session_id": self.commissioning_session_id,
            "commissioning_context_fingerprint": (
                self.commissioning_context_fingerprint
            ),
            "verification_algorithm_id": self.verification_algorithm_id,
            "verification_algorithm_version": self.verification_algorithm_version,
            "threshold_profile_fingerprint": self.threshold_profile_fingerprint,
            "verdict": self.verdict,
            "required_repeats": POST_APPLY_REQUIRED_REPEATS,
            "admitted_captures": [proof.to_dict() for proof in self.admitted_captures],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "PostApplyTargetVerification":
        value = _strict_serialized_object(
            raw,
            kind="jts_active_post_apply_target_verification",
            fields=frozenset(
                {
                    "speaker_group_id",
                    "target_id",
                    "target_fingerprint",
                    "geometry_id",
                    "placement_fingerprint",
                    "commissioning_session_id",
                    "commissioning_context_fingerprint",
                    "verification_algorithm_id",
                    "verification_algorithm_version",
                    "threshold_profile_fingerprint",
                    "verdict",
                    "required_repeats",
                    "admitted_captures",
                }
            ),
            schema_version=2,
        )
        if (
            type(value["required_repeats"]) is not int
            or value["required_repeats"] != POST_APPLY_REQUIRED_REPEATS
        ):
            raise CommissioningReceiptError("unsupported post-apply repeat count")
        admitted_captures = value["admitted_captures"]
        if type(admitted_captures) is not list:
            raise CommissioningReceiptError("post-apply captures must be a list")
        result = cls(
            speaker_group_id=_raw(value, "speaker_group_id"),
            target_id=_raw(value, "target_id"),
            target_fingerprint=_raw(value, "target_fingerprint"),
            geometry_id=_raw(value, "geometry_id"),
            placement_fingerprint=_raw(value, "placement_fingerprint"),
            commissioning_session_id=_raw(value, "commissioning_session_id"),
            commissioning_context_fingerprint=_raw(
                value,
                "commissioning_context_fingerprint",
            ),
            verification_algorithm_id=_raw(value, "verification_algorithm_id"),
            verification_algorithm_version=_raw(
                value,
                "verification_algorithm_version",
            ),
            threshold_profile_fingerprint=_raw(
                value,
                "threshold_profile_fingerprint",
            ),
            verdict=_raw(value, "verdict"),
            admitted_captures=tuple(
                AdmittedCaptureProof.from_mapping(item) for item in admitted_captures
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def _required_target_key(
    target: RequiredVerificationTarget,
) -> tuple[str, str, str, str, str]:
    return (
        target.speaker_group_id,
        target.target_id,
        target.target_fingerprint,
        target.geometry_id,
        target.placement_fingerprint,
    )


@dataclass(frozen=True)
class CommissioningEligibilityReceipt:
    """Positive authority: every exact topology target passed post-apply proof."""

    target_plan: RequiredTargetPlan
    applied_candidate: AppliedCandidateProof
    commissioning_context_fingerprint: str
    post_apply_targets: tuple[PostApplyTargetVerification, ...]
    rollback: CommissioningRollbackEvidence
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target_plan, RequiredTargetPlan):
            raise CommissioningReceiptError("target_plan must be RequiredTargetPlan")
        if not isinstance(self.applied_candidate, AppliedCandidateProof):
            raise CommissioningReceiptError(
                "applied_candidate must be AppliedCandidateProof"
            )
        context = _sha256(
            self.commissioning_context_fingerprint,
            field_name="commissioning_context_fingerprint",
        )
        object.__setattr__(self, "commissioning_context_fingerprint", context)
        if type(self.post_apply_targets) is not tuple or not self.post_apply_targets:
            raise CommissioningReceiptError(
                "post_apply_targets must be a non-empty tuple"
            )
        if any(
            not isinstance(item, PostApplyTargetVerification)
            for item in self.post_apply_targets
        ):
            raise CommissioningReceiptError(
                "post_apply_targets must contain PostApplyTargetVerification values"
            )
        keys = tuple(
            (item.speaker_group_id, item.target_id) for item in self.post_apply_targets
        )
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise CommissioningReceiptError(
                "post-apply targets must be unique and in canonical order"
            )
        if not isinstance(self.rollback, CommissioningRollbackEvidence):
            raise CommissioningReceiptError(
                "rollback must be CommissioningRollbackEvidence"
            )
        if (
            self.applied_candidate.target_plan_fingerprint
            != self.target_plan.fingerprint
        ):
            raise CommissioningReceiptError("candidate belongs to another target plan")
        expected_context = commissioning_context_fingerprint(
            target_plan=self.target_plan,
            applied_candidate=self.applied_candidate,
        )
        if self.commissioning_context_fingerprint != expected_context:
            raise CommissioningReceiptError(
                "post-apply verification belongs to a stale commissioning context"
            )
        if any(
            target.commissioning_context_fingerprint != expected_context
            or capture.context_fingerprint != expected_context
            for target in self.post_apply_targets
            for capture in target.captures
        ):
            raise CommissioningReceiptError(
                "post-apply capture belongs to another commissioning context"
            )
        sessions = {
            target.commissioning_session_id for target in self.post_apply_targets
        }
        if len(sessions) != 1:
            raise CommissioningReceiptError(
                "eligibility captures must belong to one commissioning session"
            )
        threshold_profiles = {
            target.threshold_profile_fingerprint for target in self.post_apply_targets
        }
        if len(threshold_profiles) != 1:
            raise CommissioningReceiptError(
                "post-apply targets must use one threshold profile identity"
            )
        all_captures = [
            capture for target in self.post_apply_targets for capture in target.captures
        ]
        all_admissions = [
            proof
            for target in self.post_apply_targets
            for proof in target.admitted_captures
        ]
        if any(
            proof.safety_profile_fingerprint
            != self.applied_candidate.safety_profile_fingerprint
            for proof in all_admissions
        ):
            raise CommissioningReceiptError(
                "capture admission safety profile must equal the applied candidate"
            )
        admission_ids = [proof.admission_id for proof in all_admissions]
        generation_artifacts = [proof.generation_artifact for proof in all_admissions]
        playback_artifacts = [proof.playback_artifact for proof in all_admissions]
        global_admission_roles = {
            "admission ids": admission_ids,
            "generation artifact identities": [
                artifact.fingerprint for artifact in generation_artifacts
            ],
            "generation artifact paths": [
                artifact.relative_path for artifact in generation_artifacts
            ],
            "playback artifact identities": [
                artifact.fingerprint for artifact in playback_artifacts
            ],
            "playback artifact paths": [
                artifact.relative_path for artifact in playback_artifacts
            ],
        }
        if any(
            len(set(values)) != len(values)
            for values in global_admission_roles.values()
        ):
            raise CommissioningReceiptError(
                "eligibility one-shot admission ids and role artifacts must be globally unique"
            )
        capture_ids = [capture.capture_id for capture in all_captures]
        artifact_roles = [
            artifact
            for proof in all_admissions
            for artifact in (
                proof.capture.raw_artifact,
                proof.capture.analysis_input_artifact,
                proof.capture.quality_artifact,
                proof.generation_artifact,
                proof.playback_artifact,
            )
        ]
        raw_content_hashes = [capture.raw_artifact.sha256 for capture in all_captures]
        if (
            len(set(capture_ids)) != len(capture_ids)
            or len(set(raw_content_hashes)) != len(raw_content_hashes)
            or len({artifact.fingerprint for artifact in artifact_roles})
            != len(artifact_roles)
            or len({artifact.relative_path for artifact in artifact_roles})
            != len(artifact_roles)
        ):
            raise CommissioningReceiptError(
                "eligibility capture ids, artifact roles and paths, and raw bytes must be globally unique"
            )
        bundle_keys = {
            (capture.raw_artifact.bundle_kind, capture.raw_artifact.bundle_id)
            for capture in all_captures
        }
        if len(bundle_keys) != 1:
            raise CommissioningReceiptError(
                "eligibility captures must belong to one commissioning session bundle"
            )
        required = tuple(
            _required_target_key(item) for item in self.target_plan.targets
        )
        observed = tuple(item.required_target_key for item in self.post_apply_targets)
        if observed != required:
            raise CommissioningReceiptError(
                "post-apply verification must exactly equal the required target plan"
            )
        if (
            self.rollback.mutation_state != "applied"
            or self.rollback.status != "not_required"
            or self.rollback.evidence_kind != "retained_apply"
            or self.rollback.operation_id != self.applied_candidate.operation_id
            or self.rollback.mutation_fingerprint
            != self.applied_candidate.mutation_fingerprint
            or self.rollback.observed_applied_graph_fingerprint
            != self.applied_candidate.observed_fresh_readback_graph.fingerprint
            or self.rollback.predecessor_state is None
            or self.rollback.predecessor_state.fingerprint
            != self.applied_candidate.predecessor_state.fingerprint
        ):
            raise CommissioningReceiptError(
                "eligibility requires retained verified apply with rollback not required"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "kind": "jts_active_commissioning_eligibility_receipt",
            "target_plan": self.target_plan.to_dict(),
            "applied_candidate": self.applied_candidate.to_dict(),
            "commissioning_context_fingerprint": self.commissioning_context_fingerprint,
            "post_apply_targets": [
                target.to_dict() for target in self.post_apply_targets
            ],
            "rollback": self.rollback.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CommissioningEligibilityReceipt":
        value = _strict_serialized_object(
            raw,
            kind="jts_active_commissioning_eligibility_receipt",
            fields=frozenset(
                {
                    "target_plan",
                    "applied_candidate",
                    "commissioning_context_fingerprint",
                    "post_apply_targets",
                    "rollback",
                }
            ),
            schema_version=2,
        )
        targets = value["post_apply_targets"]
        if type(targets) is not list:
            raise CommissioningReceiptError("post_apply_targets must be a list")
        result = cls(
            target_plan=RequiredTargetPlan.from_mapping(value["target_plan"]),
            applied_candidate=AppliedCandidateProof.from_mapping(
                value["applied_candidate"]
            ),
            commissioning_context_fingerprint=_raw(
                value, "commissioning_context_fingerprint"
            ),
            post_apply_targets=tuple(
                PostApplyTargetVerification.from_mapping(item) for item in targets
            ),
            rollback=CommissioningRollbackEvidence.from_mapping(value["rollback"]),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result
