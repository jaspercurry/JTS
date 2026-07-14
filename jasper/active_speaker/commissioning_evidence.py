# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Exact per-region evidence authority for Active crossover commissioning.

This module is deliberately pure.  It derives the immutable region plan and
validates newly admitted capture identities; it does not play audio, capture a
microphone, score a candidate, persist state, or mutate DSP.  Historical
fail-soft records cannot satisfy these types because every capture must belong
to the production commissioning bundle and retain canonical one-shot
generation and playback admission artifacts for one exact run attempt.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.excitation_admission import ExcitationAdmission
from jasper.audio_measurement.excitation_artifacts import (
    GENERATION_PATH_PREFIX,
    PLAYBACK_PATH_PREFIX,
    canonical_admission_bytes,
)
from jasper.audio_measurement.null_walk import (
    MIN_CAPTURE_COUNT,
    NullWalkError,
    NullWalkSpec,
)
from jasper.output_topology import OutputTopology

from .baseline_profile import topology_config_fingerprint
from .bundles import BUNDLE_KIND
from .profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    required_driver_roles,
)

REFERENCE_AXIS_GEOMETRY_ID = "reference_axis"
ACTIVE_REGION_EVIDENCE_CONSUMER_ID = "active_crossover"
ACTIVE_REGION_NORMAL_MEASUREMENT_KIND = "active_crossover_region_normal"
ACTIVE_REGION_REVERSE_MEASUREMENT_KIND = "active_crossover_region_reverse"
ACTIVE_REGION_DELAY_NULL_MEASUREMENT_KIND = "active_crossover_region_delay_null"
STATIONARY_CAPTURE_COUNT = 3
DELAY_WALK_ALGORITHM_ID = "jts_active_crossover_delay_null_walk"
DELAY_WALK_ALGORITHM_VERSION = "1"

EvidenceKind: TypeAlias = Literal["normal", "reverse", "delay_null"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MEASUREMENT_KIND_BY_EVIDENCE: dict[str, str] = {
    "normal": ACTIVE_REGION_NORMAL_MEASUREMENT_KIND,
    "reverse": ACTIVE_REGION_REVERSE_MEASUREMENT_KIND,
    "delay_null": ACTIVE_REGION_DELAY_NULL_MEASUREMENT_KIND,
}


class CommissioningEvidenceError(ValueError):
    """Commissioning evidence is malformed, stale, or self-contradictory."""


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CommissioningEvidenceError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


def _identifier(value: Any, *, field_name: str) -> str:
    value = _text(value, field_name=field_name)
    if _ID_RE.fullmatch(value) is None:
        raise CommissioningEvidenceError(
            f"{field_name} must be a safe identifier of at most 128 characters"
        )
    return value


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CommissioningEvidenceError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _positive_int(value: Any, *, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise CommissioningEvidenceError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: Any, *, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise CommissioningEvidenceError(f"{field_name} must be a non-negative integer")
    return value


def _finite_positive(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningEvidenceError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise CommissioningEvidenceError(f"{field_name} must be positive and finite")
    return result


def _fingerprint(payload: Mapping[str, Any]) -> str:
    try:
        return json_fingerprint(payload, field_name="commissioning evidence payload")
    except EvidenceIdentityError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


def _strict_object(
    raw: Any,
    *,
    kind: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise CommissioningEvidenceError(f"{kind} must be an object")
    expected = fields | {"schema_version", "kind", "fingerprint"}
    if set(raw) != expected:
        raise CommissioningEvidenceError(f"{kind} has unknown or missing fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise CommissioningEvidenceError(f"unsupported {kind} schema")
    if raw["kind"] != kind:
        raise CommissioningEvidenceError(f"unsupported {kind} kind")
    return raw


def _declared_fingerprint(raw: Mapping[str, Any], actual: str) -> None:
    if raw["fingerprint"] != actual:
        raise CommissioningEvidenceError(
            "declared fingerprint does not match canonical content"
        )


def _artifact_from_mapping(raw: Any) -> ArtifactIdentity:
    try:
        return ArtifactIdentity.from_mapping(raw)
    except EvidenceIdentityError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


def _capture_from_mapping(raw: Any) -> CaptureIdentity:
    try:
        return CaptureIdentity.from_mapping(raw)
    except EvidenceIdentityError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class CommissioningEvidenceAuthority:
    """Exact run and immutable environment bound into every evidence value."""

    run_id: str
    owner_generation: int
    topology_id: str
    topology_fingerprint: str
    protected_safety_profile_fingerprint: str
    comparison_set_fingerprint: str
    commissioning_session_id: str
    threshold_profile_fingerprint: str
    context_fingerprint: str
    expected_geometry_id: str = REFERENCE_AXIS_GEOMETRY_ID
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("run_id", "topology_id", "commissioning_session_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "owner_generation",
            _positive_int(self.owner_generation, field_name="owner_generation"),
        )
        for name in (
            "topology_fingerprint",
            "protected_safety_profile_fingerprint",
            "comparison_set_fingerprint",
            "threshold_profile_fingerprint",
            "context_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        if self.expected_geometry_id != REFERENCE_AXIS_GEOMETRY_ID:
            raise CommissioningEvidenceError(
                "region evidence authority requires reference_axis geometry"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_commissioning_evidence_authority",
            "run_id": self.run_id,
            "owner_generation": self.owner_generation,
            "topology_id": self.topology_id,
            "topology_fingerprint": self.topology_fingerprint,
            "protected_safety_profile_fingerprint": (
                self.protected_safety_profile_fingerprint
            ),
            "comparison_set_fingerprint": self.comparison_set_fingerprint,
            "commissioning_session_id": self.commissioning_session_id,
            "threshold_profile_fingerprint": self.threshold_profile_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "expected_geometry_id": self.expected_geometry_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CommissioningEvidenceAuthority":
        value = _strict_object(
            raw,
            kind="jts_active_commissioning_evidence_authority",
            fields=frozenset(
                {
                    "run_id",
                    "owner_generation",
                    "topology_id",
                    "topology_fingerprint",
                    "protected_safety_profile_fingerprint",
                    "comparison_set_fingerprint",
                    "commissioning_session_id",
                    "threshold_profile_fingerprint",
                    "context_fingerprint",
                    "expected_geometry_id",
                }
            ),
        )
        result = cls(
            run_id=value["run_id"],
            owner_generation=value["owner_generation"],
            topology_id=value["topology_id"],
            topology_fingerprint=value["topology_fingerprint"],
            protected_safety_profile_fingerprint=value[
                "protected_safety_profile_fingerprint"
            ],
            comparison_set_fingerprint=value["comparison_set_fingerprint"],
            commissioning_session_id=value["commissioning_session_id"],
            threshold_profile_fingerprint=value["threshold_profile_fingerprint"],
            context_fingerprint=value["context_fingerprint"],
            expected_geometry_id=value["expected_geometry_id"],
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True, slots=True)
class RegionEvidenceTarget:
    """One exact group/region and its phase-distinct measurement identities."""

    speaker_group_id: str
    region_id: str
    region_fingerprint: str
    lower_role: str
    upper_role: str
    electrical_fc_hz: float
    electrical_family: str
    electrical_order: int
    normal_target_fingerprint: str
    normal_context_base_fingerprint: str
    reverse_target_fingerprint: str
    reverse_context_base_fingerprint: str
    delay_target_base_fingerprint: str
    delay_context_base_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("speaker_group_id", "region_id", "lower_role", "upper_role"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        if self.lower_role == self.upper_role:
            raise CommissioningEvidenceError("region roles must be distinct")
        object.__setattr__(
            self,
            "region_fingerprint",
            _sha256(self.region_fingerprint, field_name="region_fingerprint"),
        )
        object.__setattr__(
            self,
            "electrical_fc_hz",
            _finite_positive(self.electrical_fc_hz, field_name="electrical_fc_hz"),
        )
        object.__setattr__(
            self,
            "electrical_family",
            _text(self.electrical_family, field_name="electrical_family"),
        )
        object.__setattr__(
            self,
            "electrical_order",
            _positive_int(self.electrical_order, field_name="electrical_order"),
        )
        for name in (
            "normal_target_fingerprint",
            "normal_context_base_fingerprint",
            "reverse_target_fingerprint",
            "reverse_context_base_fingerprint",
            "delay_target_base_fingerprint",
            "delay_context_base_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        if (
            len(
                {
                    self.normal_target_fingerprint,
                    self.reverse_target_fingerprint,
                    self.delay_target_base_fingerprint,
                }
            )
            != 3
            or len(
                {
                    self.normal_context_base_fingerprint,
                    self.reverse_context_base_fingerprint,
                    self.delay_context_base_fingerprint,
                }
            )
            != 3
        ):
            raise CommissioningEvidenceError(
                "normal, reverse, and delay identities must be semantically distinct"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def canonical_key(self) -> tuple[str, float, str]:
        return self.speaker_group_id, self.electrical_fc_hz, self.region_id

    def target_fingerprint_for(self, evidence_kind: EvidenceKind) -> str:
        if evidence_kind == "normal":
            return self.normal_target_fingerprint
        if evidence_kind == "reverse":
            return self.reverse_target_fingerprint
        return self.delay_target_base_fingerprint

    def context_base_fingerprint_for(self, evidence_kind: EvidenceKind) -> str:
        if evidence_kind == "normal":
            return self.normal_context_base_fingerprint
        if evidence_kind == "reverse":
            return self.reverse_context_base_fingerprint
        return self.delay_context_base_fingerprint

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_region_evidence_target",
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "region_fingerprint": self.region_fingerprint,
            "lower_role": self.lower_role,
            "upper_role": self.upper_role,
            "electrical_fc_hz": self.electrical_fc_hz,
            "electrical_family": self.electrical_family,
            "electrical_order": self.electrical_order,
            "normal_target_fingerprint": self.normal_target_fingerprint,
            "normal_context_base_fingerprint": self.normal_context_base_fingerprint,
            "reverse_target_fingerprint": self.reverse_target_fingerprint,
            "reverse_context_base_fingerprint": self.reverse_context_base_fingerprint,
            "delay_target_base_fingerprint": self.delay_target_base_fingerprint,
            "delay_context_base_fingerprint": self.delay_context_base_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "RegionEvidenceTarget":
        fields = frozenset(
            {
                "speaker_group_id",
                "region_id",
                "region_fingerprint",
                "lower_role",
                "upper_role",
                "electrical_fc_hz",
                "electrical_family",
                "electrical_order",
                "normal_target_fingerprint",
                "normal_context_base_fingerprint",
                "reverse_target_fingerprint",
                "reverse_context_base_fingerprint",
                "delay_target_base_fingerprint",
                "delay_context_base_fingerprint",
            }
        )
        value = _strict_object(
            raw,
            kind="jts_active_region_evidence_target",
            fields=fields,
        )
        result = cls(**{name: value[name] for name in fields})
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True, slots=True)
class RegionEvidencePlan:
    """Canonical region target plan for one exact commissioning run."""

    authority: CommissioningEvidenceAuthority
    preset_id: str
    preset_fingerprint: str
    targets: tuple[RegionEvidenceTarget, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authority, CommissioningEvidenceAuthority):
            raise CommissioningEvidenceError(
                "authority must be CommissioningEvidenceAuthority"
            )
        object.__setattr__(
            self,
            "preset_id",
            _identifier(self.preset_id, field_name="preset_id"),
        )
        object.__setattr__(
            self,
            "preset_fingerprint",
            _sha256(self.preset_fingerprint, field_name="preset_fingerprint"),
        )
        if type(self.targets) is not tuple or not self.targets:
            raise CommissioningEvidenceError("region plan targets must be non-empty")
        if any(not isinstance(item, RegionEvidenceTarget) for item in self.targets):
            raise CommissioningEvidenceError(
                "region plan targets must be RegionEvidenceTarget values"
            )
        keys = tuple(item.canonical_key for item in self.targets)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise CommissioningEvidenceError(
                "region plan targets must be unique and canonically ordered"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_region_evidence_plan",
            "authority": self.authority.to_dict(),
            "preset_id": self.preset_id,
            "preset_fingerprint": self.preset_fingerprint,
            "targets": [target.to_dict() for target in self.targets],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "RegionEvidencePlan":
        value = _strict_object(
            raw,
            kind="jts_active_region_evidence_plan",
            fields=frozenset(
                {"authority", "preset_id", "preset_fingerprint", "targets"}
            ),
        )
        raw_targets = value["targets"]
        if type(raw_targets) is not list:
            raise CommissioningEvidenceError("region plan targets must be a list")
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            preset_id=value["preset_id"],
            preset_fingerprint=value["preset_fingerprint"],
            targets=tuple(
                RegionEvidenceTarget.from_mapping(item) for item in raw_targets
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def _mode_identity(
    *,
    identity_kind: str,
    evidence_kind: EvidenceKind,
    authority: CommissioningEvidenceAuthority,
    preset_fingerprint: str,
    group_payload: Mapping[str, Any],
    region_payload: Mapping[str, Any],
) -> str:
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": identity_kind,
            "evidence_kind": evidence_kind,
            "authority_fingerprint": authority.fingerprint,
            "preset_fingerprint": preset_fingerprint,
            "speaker_group": dict(group_payload),
            "region": dict(region_payload),
        }
    )


def derive_region_evidence_plan(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology,
    *,
    run_id: str,
    owner_generation: int,
    protected_safety_profile_fingerprint: str,
    comparison_set_fingerprint: str,
    commissioning_session_id: str,
    threshold_profile_fingerprint: str,
    context_fingerprint: str,
) -> RegionEvidencePlan:
    """Derive the exact group-by-region plan from current typed product state."""

    if not isinstance(preset, ActiveSpeakerPreset):
        raise CommissioningEvidenceError("preset must be ActiveSpeakerPreset")
    if not isinstance(topology, OutputTopology):
        raise CommissioningEvidenceError("topology must be OutputTopology")
    try:
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        raise CommissioningEvidenceError(f"capture preset is invalid: {exc}") from exc
    if topology.evaluation().get("status") != "verified":
        raise CommissioningEvidenceError(
            "region evidence planning requires a verified current topology"
        )
    topology_fingerprint = topology_config_fingerprint(topology)
    authority = CommissioningEvidenceAuthority(
        run_id=run_id,
        owner_generation=owner_generation,
        topology_id=topology.topology_id,
        topology_fingerprint=topology_fingerprint,
        protected_safety_profile_fingerprint=protected_safety_profile_fingerprint,
        comparison_set_fingerprint=comparison_set_fingerprint,
        commissioning_session_id=commissioning_session_id,
        threshold_profile_fingerprint=threshold_profile_fingerprint,
        context_fingerprint=context_fingerprint,
    )
    preset_fingerprint = _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_region_evidence_preset",
            "preset": preset.to_dict(),
        }
    )
    expected_mode = f"active_{preset.way_count}_way"
    expected_roles = set(required_driver_roles(preset.way_count))
    groups = []
    for group in topology.speaker_groups:
        if group.mode != expected_mode:
            continue
        if {channel.role for channel in group.channels} != expected_roles:
            raise CommissioningEvidenceError(
                f"speaker group {group.id} does not match preset driver roles"
            )
        groups.append(group)
    if not groups:
        raise CommissioningEvidenceError(
            "current topology has no active group matching the capture preset"
        )

    targets: list[RegionEvidenceTarget] = []
    for group in groups:
        group_payload = group.to_dict()
        for region in preset.crossover_regions:
            region_payload = region.to_dict()
            region_fingerprint = _fingerprint(
                {
                    "schema_version": 1,
                    "kind": "jts_active_preset_crossover_region",
                    "preset_id": preset.preset_id,
                    "preset_fingerprint": preset_fingerprint,
                    "region": region_payload,
                }
            )
            identities: dict[str, str] = {}
            for evidence_kind in ("normal", "reverse", "delay_null"):
                identities[f"{evidence_kind}_target"] = _mode_identity(
                    identity_kind="jts_active_region_measurement_target",
                    evidence_kind=evidence_kind,
                    authority=authority,
                    preset_fingerprint=preset_fingerprint,
                    group_payload=group_payload,
                    region_payload=region_payload,
                )
                identities[f"{evidence_kind}_context"] = _mode_identity(
                    identity_kind="jts_active_region_measurement_context",
                    evidence_kind=evidence_kind,
                    authority=authority,
                    preset_fingerprint=preset_fingerprint,
                    group_payload=group_payload,
                    region_payload=region_payload,
                )
            targets.append(
                RegionEvidenceTarget(
                    speaker_group_id=group.id,
                    region_id=region.id,
                    region_fingerprint=region_fingerprint,
                    lower_role=region.lower_driver,
                    upper_role=region.upper_driver,
                    electrical_fc_hz=region.fc_hz,
                    electrical_family=region.target_type,
                    electrical_order=region.order,
                    normal_target_fingerprint=identities["normal_target"],
                    normal_context_base_fingerprint=identities["normal_context"],
                    reverse_target_fingerprint=identities["reverse_target"],
                    reverse_context_base_fingerprint=identities["reverse_context"],
                    delay_target_base_fingerprint=identities["delay_null_target"],
                    delay_context_base_fingerprint=identities["delay_null_context"],
                )
            )
    return RegionEvidencePlan(
        authority=authority,
        preset_id=preset.preset_id,
        preset_fingerprint=preset_fingerprint,
        targets=tuple(sorted(targets, key=lambda item: item.canonical_key)),
    )


def _artifact_matches_canonical_admission(
    artifact: ArtifactIdentity,
    admission: ExcitationAdmission,
) -> bool:
    canonical = canonical_admission_bytes(admission)
    return artifact.sha256 == hashlib.sha256(
        canonical
    ).hexdigest() and artifact.byte_size == len(canonical)


def _assert_authority_artifact(
    artifact: ArtifactIdentity,
    authority: CommissioningEvidenceAuthority,
    *,
    field_name: str,
) -> None:
    if not isinstance(artifact, ArtifactIdentity):
        raise CommissioningEvidenceError(f"{field_name} must be ArtifactIdentity")
    if (
        artifact.bundle_kind != BUNDLE_KIND
        or artifact.bundle_id != authority.commissioning_session_id
    ):
        raise CommissioningEvidenceError(
            f"{field_name} must belong to the exact commissioning authority bundle"
        )


def capture_attempt_context_fingerprint(
    authority: CommissioningEvidenceAuthority,
    *,
    attempt_id: str,
    evidence_kind: EvidenceKind,
    target_fingerprint: str,
    context_base_fingerprint: str,
    graph_fingerprint: str,
    generation_protection_evidence_fingerprint: str,
    playback_protection_evidence_fingerprint: str,
) -> str:
    """Bind a semantic capture context to one durable run attempt."""

    if not isinstance(authority, CommissioningEvidenceAuthority):
        raise CommissioningEvidenceError(
            "authority must be CommissioningEvidenceAuthority"
        )
    attempt = _identifier(attempt_id, field_name="attempt_id")
    if evidence_kind not in _MEASUREMENT_KIND_BY_EVIDENCE:
        raise CommissioningEvidenceError("capture evidence_kind is unsupported")
    target = _sha256(target_fingerprint, field_name="target_fingerprint")
    context_base = _sha256(
        context_base_fingerprint,
        field_name="context_base_fingerprint",
    )
    graph = _sha256(graph_fingerprint, field_name="graph_fingerprint")
    generation_proof = _sha256(
        generation_protection_evidence_fingerprint,
        field_name="generation_protection_evidence_fingerprint",
    )
    playback_proof = _sha256(
        playback_protection_evidence_fingerprint,
        field_name="playback_protection_evidence_fingerprint",
    )
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_capture_attempt_context",
            "authority_fingerprint": authority.fingerprint,
            "attempt_id": attempt,
            "evidence_kind": evidence_kind,
            "target_fingerprint": target,
            "context_base_fingerprint": context_base,
            "graph_fingerprint": graph,
            "generation_protection_evidence_fingerprint": generation_proof,
            "playback_protection_evidence_fingerprint": playback_proof,
        }
    )


@dataclass(frozen=True, slots=True)
class AdmittedRegionCapture:
    """One fresh, one-shot, graph-confirmed region capture."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    attempt_id: str
    speaker_group_id: str
    region_id: str
    evidence_kind: EvidenceKind
    target_fingerprint: str
    context_base_fingerprint: str
    context_fingerprint: str
    placement_fingerprint: str
    graph_fingerprint: str
    generation_protection_evidence_fingerprint: str
    playback_protection_evidence_fingerprint: str
    admission_id: str
    capture: CaptureIdentity
    stimulus: GeneratedExcitationWav
    generation_artifact: ArtifactIdentity
    playback_artifact: ArtifactIdentity
    generation_admission: ExcitationAdmission
    playback_admission: ExcitationAdmission
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authority, CommissioningEvidenceAuthority):
            raise CommissioningEvidenceError(
                "capture authority must be CommissioningEvidenceAuthority"
            )
        object.__setattr__(
            self,
            "plan_fingerprint",
            _sha256(self.plan_fingerprint, field_name="plan_fingerprint"),
        )
        for name in ("speaker_group_id", "region_id", "admission_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "attempt_id",
            _identifier(self.attempt_id, field_name="attempt_id"),
        )
        if self.evidence_kind not in _MEASUREMENT_KIND_BY_EVIDENCE:
            raise CommissioningEvidenceError("capture evidence_kind is unsupported")
        for name in (
            "target_fingerprint",
            "context_base_fingerprint",
            "context_fingerprint",
            "placement_fingerprint",
            "graph_fingerprint",
            "generation_protection_evidence_fingerprint",
            "playback_protection_evidence_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        expected_context = capture_attempt_context_fingerprint(
            self.authority,
            attempt_id=self.attempt_id,
            evidence_kind=self.evidence_kind,
            target_fingerprint=self.target_fingerprint,
            context_base_fingerprint=self.context_base_fingerprint,
            graph_fingerprint=self.graph_fingerprint,
            generation_protection_evidence_fingerprint=(
                self.generation_protection_evidence_fingerprint
            ),
            playback_protection_evidence_fingerprint=(
                self.playback_protection_evidence_fingerprint
            ),
        )
        if self.context_fingerprint != expected_context:
            raise CommissioningEvidenceError(
                "capture context is not bound to its exact run attempt"
            )
        if not isinstance(self.capture, CaptureIdentity):
            raise CommissioningEvidenceError("capture must be CaptureIdentity")
        if not isinstance(self.stimulus, GeneratedExcitationWav):
            raise CommissioningEvidenceError("stimulus must be GeneratedExcitationWav")
        for name in ("generation_artifact", "playback_artifact"):
            _assert_authority_artifact(
                getattr(self, name),
                self.authority,
                field_name=name,
            )
        _assert_authority_artifact(
            self.stimulus.artifact,
            self.authority,
            field_name="stimulus.artifact",
        )
        for artifact_name, artifact in (
            ("raw_artifact", self.capture.raw_artifact),
            ("analysis_input_artifact", self.capture.analysis_input_artifact),
            ("quality_artifact", self.capture.quality_artifact),
            ("capture.admission_artifact", self.capture.admission_artifact),
        ):
            _assert_authority_artifact(
                artifact,
                self.authority,
                field_name=artifact_name,
            )
        if (
            self.capture.consumer_id != ACTIVE_REGION_EVIDENCE_CONSUMER_ID
            or self.capture.measurement_kind
            != _MEASUREMENT_KIND_BY_EVIDENCE[self.evidence_kind]
            or self.capture.target_fingerprint != self.target_fingerprint
            or self.capture.context_fingerprint != self.context_fingerprint
            or self.capture.geometry_id != self.authority.expected_geometry_id
            or self.capture.placement_fingerprint != self.placement_fingerprint
        ):
            raise CommissioningEvidenceError(
                "capture consumer, target, context, geometry, or placement is stale"
            )
        expected_generation_path = f"{GENERATION_PATH_PREFIX}/{self.admission_id}.json"
        expected_playback_path = f"{PLAYBACK_PATH_PREFIX}/{self.admission_id}.json"
        if self.generation_artifact.relative_path != expected_generation_path:
            raise CommissioningEvidenceError(
                "generation artifact does not occupy its canonical admission role"
            )
        if self.playback_artifact.relative_path != expected_playback_path:
            raise CommissioningEvidenceError(
                "playback artifact does not occupy its canonical admission role"
            )
        if self.capture.admission_artifact != self.playback_artifact:
            raise CommissioningEvidenceError(
                "capture admission artifact must be the exact playback decision"
            )
        if not isinstance(
            self.generation_admission, ExcitationAdmission
        ) or not isinstance(self.playback_admission, ExcitationAdmission):
            raise CommissioningEvidenceError(
                "generation and playback admissions must be typed decisions"
            )
        if not self.generation_admission.allowed or not self.playback_admission.allowed:
            raise CommissioningEvidenceError(
                "region capture requires allowed generation and playback admissions"
            )
        for admission in (self.generation_admission, self.playback_admission):
            if (
                admission.request.repeat_count != 1
                or admission.request.target_fingerprint != self.target_fingerprint
                or admission.limits.target_fingerprint != self.target_fingerprint
                or admission.limits.safety_profile_fingerprint
                != self.authority.protected_safety_profile_fingerprint
            ):
                raise CommissioningEvidenceError(
                    "region capture admission is not a one-shot for this target/profile"
                )
        generation_proof = self.generation_admission.protection_evidence
        playback_proof = self.playback_admission.protection_evidence
        if (
            generation_proof is None
            or playback_proof is None
            or generation_proof.evidence_fingerprint
            != self.generation_protection_evidence_fingerprint
            or playback_proof.evidence_fingerprint
            != self.playback_protection_evidence_fingerprint
        ):
            raise CommissioningEvidenceError(
                "capture protection identities must equal both admitted proofs"
            )
        if (
            self.generation_admission.request != self.playback_admission.request
            or self.generation_admission.limits != self.playback_admission.limits
        ):
            raise CommissioningEvidenceError(
                "playback admission must retain the generation request and limits"
            )
        if not _artifact_matches_canonical_admission(
            self.generation_artifact, self.generation_admission
        ) or not _artifact_matches_canonical_admission(
            self.playback_artifact, self.playback_admission
        ):
            raise CommissioningEvidenceError(
                "admission artifacts must equal canonical typed admission bytes"
            )
        if (
            self.stimulus.generation_artifact_fingerprint
            != self.generation_artifact.fingerprint
            or self.stimulus.excitation_plan_fingerprint
            != self.generation_admission.limits.excitation_plan_fingerprint
            or self.stimulus.excitation_plan_fingerprint
            != self.playback_admission.limits.excitation_plan_fingerprint
        ):
            raise CommissioningEvidenceError(
                "generated stimulus is not bound to the exact admissions"
            )
        artifacts = (
            self.capture.raw_artifact,
            self.capture.analysis_input_artifact,
            self.capture.quality_artifact,
            self.playback_artifact,
            self.stimulus.artifact,
            self.generation_artifact,
        )
        if len({item.fingerprint for item in artifacts}) != len(artifacts) or len(
            {item.relative_path for item in artifacts}
        ) != len(artifacts):
            raise CommissioningEvidenceError(
                "capture, stimulus, generation, and playback roles must be distinct"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def canonical_key(self) -> tuple[str, str]:
        return self.capture.capture_id, self.admission_id

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_admitted_region_capture",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt_id": self.attempt_id,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "evidence_kind": self.evidence_kind,
            "target_fingerprint": self.target_fingerprint,
            "context_base_fingerprint": self.context_base_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "placement_fingerprint": self.placement_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "generation_protection_evidence_fingerprint": (
                self.generation_protection_evidence_fingerprint
            ),
            "playback_protection_evidence_fingerprint": (
                self.playback_protection_evidence_fingerprint
            ),
            "admission_id": self.admission_id,
            "capture": self.capture.to_dict(),
            "stimulus": self.stimulus.to_dict(),
            "generation_artifact": self.generation_artifact.to_dict(),
            "playback_artifact": self.playback_artifact.to_dict(),
            "generation_admission": self.generation_admission.to_dict(),
            "playback_admission": self.playback_admission.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "AdmittedRegionCapture":
        fields = frozenset(
            {
                "authority",
                "plan_fingerprint",
                "attempt_id",
                "speaker_group_id",
                "region_id",
                "evidence_kind",
                "target_fingerprint",
                "context_base_fingerprint",
                "context_fingerprint",
                "placement_fingerprint",
                "graph_fingerprint",
                "generation_protection_evidence_fingerprint",
                "playback_protection_evidence_fingerprint",
                "admission_id",
                "capture",
                "stimulus",
                "generation_artifact",
                "playback_artifact",
                "generation_admission",
                "playback_admission",
            }
        )
        value = _strict_object(
            raw,
            kind="jts_active_admitted_region_capture",
            fields=fields,
        )
        try:
            generation_admission = ExcitationAdmission.from_dict(
                value["generation_admission"]
            )
            playback_admission = ExcitationAdmission.from_dict(
                value["playback_admission"]
            )
        except ValueError as exc:
            raise CommissioningEvidenceError(str(exc)) from exc
        try:
            stimulus = GeneratedExcitationWav.from_mapping(value["stimulus"])
        except (EvidenceIdentityError, ValueError) as exc:
            raise CommissioningEvidenceError(str(exc)) from exc
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            plan_fingerprint=value["plan_fingerprint"],
            attempt_id=value["attempt_id"],
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            evidence_kind=value["evidence_kind"],
            target_fingerprint=value["target_fingerprint"],
            context_base_fingerprint=value["context_base_fingerprint"],
            context_fingerprint=value["context_fingerprint"],
            placement_fingerprint=value["placement_fingerprint"],
            graph_fingerprint=value["graph_fingerprint"],
            generation_protection_evidence_fingerprint=value[
                "generation_protection_evidence_fingerprint"
            ],
            playback_protection_evidence_fingerprint=value[
                "playback_protection_evidence_fingerprint"
            ],
            admission_id=value["admission_id"],
            capture=_capture_from_mapping(value["capture"]),
            stimulus=stimulus,
            generation_artifact=_artifact_from_mapping(value["generation_artifact"]),
            playback_artifact=_artifact_from_mapping(value["playback_artifact"]),
            generation_admission=generation_admission,
            playback_admission=playback_admission,
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def _assert_fresh_capture_set(
    captures: tuple[AdmittedRegionCapture, ...],
    *,
    expected_count: int,
) -> None:
    if type(captures) is not tuple or len(captures) != expected_count:
        raise CommissioningEvidenceError(
            f"evidence set requires exactly {expected_count} captures"
        )
    if any(not isinstance(item, AdmittedRegionCapture) for item in captures):
        raise CommissioningEvidenceError(
            "evidence set captures must be AdmittedRegionCapture values"
        )
    keys = tuple(item.canonical_key for item in captures)
    if keys != tuple(sorted(keys)):
        raise CommissioningEvidenceError("captures must be in canonical order")
    unique_fields = {
        "capture ids": [item.capture.capture_id for item in captures],
        "capture identities": [item.capture.fingerprint for item in captures],
        "raw artifacts": [item.capture.raw_artifact.fingerprint for item in captures],
        "raw bytes": [item.capture.raw_artifact.sha256 for item in captures],
        "admission ids": [item.admission_id for item in captures],
        "generation artifacts": [
            item.generation_artifact.fingerprint for item in captures
        ],
        "generation paths": [
            item.generation_artifact.relative_path for item in captures
        ],
        "playback artifacts": [item.playback_artifact.fingerprint for item in captures],
        "playback paths": [item.playback_artifact.relative_path for item in captures],
        "stimulus artifacts": [item.stimulus.artifact.fingerprint for item in captures],
        "stimulus paths": [item.stimulus.artifact.relative_path for item in captures],
    }
    for label, values in unique_fields.items():
        if len(set(values)) != expected_count:
            raise CommissioningEvidenceError(
                f"fresh one-shot evidence requires unique {label}"
            )


@dataclass(frozen=True, slots=True)
class StationaryRegionEvidence:
    """Exactly three fresh captures for one normal or reverse stationary target."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    attempt_id: str
    speaker_group_id: str
    region_id: str
    evidence_kind: Literal["normal", "reverse"]
    target_fingerprint: str
    context_base_fingerprint: str
    placement_fingerprint: str
    graph_fingerprint: str
    captures: tuple[AdmittedRegionCapture, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authority, CommissioningEvidenceAuthority):
            raise CommissioningEvidenceError(
                "stationary authority must be CommissioningEvidenceAuthority"
            )
        for name in (
            "plan_fingerprint",
            "target_fingerprint",
            "context_base_fingerprint",
            "placement_fingerprint",
            "graph_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        for name in ("speaker_group_id", "region_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "attempt_id",
            _identifier(self.attempt_id, field_name="attempt_id"),
        )
        if self.evidence_kind not in {"normal", "reverse"}:
            raise CommissioningEvidenceError(
                "stationary evidence must be normal or reverse"
            )
        _assert_fresh_capture_set(
            self.captures,
            expected_count=STATIONARY_CAPTURE_COUNT,
        )
        if any(
            capture.authority != self.authority
            or capture.plan_fingerprint != self.plan_fingerprint
            or capture.attempt_id != self.attempt_id
            or capture.speaker_group_id != self.speaker_group_id
            or capture.region_id != self.region_id
            or capture.evidence_kind != self.evidence_kind
            or capture.target_fingerprint != self.target_fingerprint
            or capture.context_base_fingerprint != self.context_base_fingerprint
            or capture.placement_fingerprint != self.placement_fingerprint
            or capture.graph_fingerprint != self.graph_fingerprint
            for capture in self.captures
        ):
            raise CommissioningEvidenceError(
                "stationary captures do not share one exact target, placement, and graph"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_stationary_region_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt_id": self.attempt_id,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "evidence_kind": self.evidence_kind,
            "target_fingerprint": self.target_fingerprint,
            "context_base_fingerprint": self.context_base_fingerprint,
            "placement_fingerprint": self.placement_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "required_capture_count": STATIONARY_CAPTURE_COUNT,
            "captures": [capture.to_dict() for capture in self.captures],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "StationaryRegionEvidence":
        fields = frozenset(
            {
                "plan_fingerprint",
                "authority",
                "attempt_id",
                "speaker_group_id",
                "region_id",
                "evidence_kind",
                "target_fingerprint",
                "context_base_fingerprint",
                "placement_fingerprint",
                "graph_fingerprint",
                "required_capture_count",
                "captures",
            }
        )
        value = _strict_object(
            raw,
            kind="jts_active_stationary_region_evidence",
            fields=fields,
        )
        if (
            type(value["required_capture_count"]) is not int
            or value["required_capture_count"] != STATIONARY_CAPTURE_COUNT
        ):
            raise CommissioningEvidenceError(
                "stationary evidence required capture count is invalid"
            )
        raw_captures = value["captures"]
        if type(raw_captures) is not list:
            raise CommissioningEvidenceError("stationary captures must be a list")
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            plan_fingerprint=value["plan_fingerprint"],
            attempt_id=value["attempt_id"],
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            evidence_kind=value["evidence_kind"],
            target_fingerprint=value["target_fingerprint"],
            context_base_fingerprint=value["context_base_fingerprint"],
            placement_fingerprint=value["placement_fingerprint"],
            graph_fingerprint=value["graph_fingerprint"],
            captures=tuple(
                AdmittedRegionCapture.from_mapping(item) for item in raw_captures
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def delay_point_target_fingerprint(
    target: RegionEvidenceTarget,
    spec: NullWalkSpec,
    relative_delay_us: float,
) -> str:
    """Return the target identity for one exact shared null-walk coordinate."""

    if not isinstance(target, RegionEvidenceTarget):
        raise CommissioningEvidenceError("target must be RegionEvidenceTarget")
    if not isinstance(spec, NullWalkSpec):
        raise CommissioningEvidenceError("spec must be NullWalkSpec")
    try:
        candidate = spec.dsp_candidate(relative_delay_us)
    except NullWalkError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_delay_point_target",
            "region_target_fingerprint": target.fingerprint,
            "delay_target_base_fingerprint": target.delay_target_base_fingerprint,
            "walk_spec": spec.to_dict(),
            "candidate": candidate.to_dict(),
        }
    )


def delay_point_context_base_fingerprint(
    target: RegionEvidenceTarget,
    spec: NullWalkSpec,
    relative_delay_us: float,
    graph_fingerprint: str,
) -> str:
    """Return the graph-bound capture context for one null-walk coordinate."""

    graph = _sha256(graph_fingerprint, field_name="graph_fingerprint")
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_delay_point_context",
            "region_target_fingerprint": target.fingerprint,
            "delay_context_base_fingerprint": target.delay_context_base_fingerprint,
            "point_target_fingerprint": delay_point_target_fingerprint(
                target, spec, relative_delay_us
            ),
            "graph_fingerprint": graph,
        }
    )


@dataclass(frozen=True, slots=True)
class DelayPointEvidence:
    """Exactly five one-shot null captures at one graph-confirmed delay."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    attempt_id: str
    speaker_group_id: str
    region_id: str
    relative_delay_us: float
    target_fingerprint: str
    context_base_fingerprint: str
    placement_fingerprint: str
    graph_fingerprint: str
    captures: tuple[AdmittedRegionCapture, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authority, CommissioningEvidenceAuthority):
            raise CommissioningEvidenceError(
                "delay point authority must be CommissioningEvidenceAuthority"
            )
        for name in (
            "plan_fingerprint",
            "target_fingerprint",
            "context_base_fingerprint",
            "placement_fingerprint",
            "graph_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        for name in ("speaker_group_id", "region_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "attempt_id",
            _identifier(self.attempt_id, field_name="attempt_id"),
        )
        if isinstance(self.relative_delay_us, bool) or not isinstance(
            self.relative_delay_us, (int, float)
        ):
            raise CommissioningEvidenceError("relative_delay_us must be finite")
        relative_delay = float(self.relative_delay_us)
        if not math.isfinite(relative_delay):
            raise CommissioningEvidenceError("relative_delay_us must be finite")
        object.__setattr__(self, "relative_delay_us", relative_delay)
        _assert_fresh_capture_set(self.captures, expected_count=MIN_CAPTURE_COUNT)
        if any(
            capture.authority != self.authority
            or capture.plan_fingerprint != self.plan_fingerprint
            or capture.attempt_id != self.attempt_id
            or capture.speaker_group_id != self.speaker_group_id
            or capture.region_id != self.region_id
            or capture.evidence_kind != "delay_null"
            or capture.target_fingerprint != self.target_fingerprint
            or capture.context_base_fingerprint != self.context_base_fingerprint
            or capture.placement_fingerprint != self.placement_fingerprint
            or capture.graph_fingerprint != self.graph_fingerprint
            for capture in self.captures
        ):
            raise CommissioningEvidenceError(
                "delay point captures do not share one exact target, placement, and graph"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def canonical_key(self) -> float:
        return self.relative_delay_us

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_delay_point_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt_id": self.attempt_id,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "relative_delay_us": self.relative_delay_us,
            "target_fingerprint": self.target_fingerprint,
            "context_base_fingerprint": self.context_base_fingerprint,
            "placement_fingerprint": self.placement_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "required_capture_count": MIN_CAPTURE_COUNT,
            "captures": [capture.to_dict() for capture in self.captures],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "DelayPointEvidence":
        fields = frozenset(
            {
                "plan_fingerprint",
                "authority",
                "attempt_id",
                "speaker_group_id",
                "region_id",
                "relative_delay_us",
                "target_fingerprint",
                "context_base_fingerprint",
                "placement_fingerprint",
                "graph_fingerprint",
                "required_capture_count",
                "captures",
            }
        )
        value = _strict_object(
            raw,
            kind="jts_active_delay_point_evidence",
            fields=fields,
        )
        if (
            type(value["required_capture_count"]) is not int
            or value["required_capture_count"] != MIN_CAPTURE_COUNT
        ):
            raise CommissioningEvidenceError(
                "delay point required capture count is invalid"
            )
        raw_captures = value["captures"]
        if type(raw_captures) is not list:
            raise CommissioningEvidenceError("delay point captures must be a list")
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            plan_fingerprint=value["plan_fingerprint"],
            attempt_id=value["attempt_id"],
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            relative_delay_us=value["relative_delay_us"],
            target_fingerprint=value["target_fingerprint"],
            context_base_fingerprint=value["context_base_fingerprint"],
            placement_fingerprint=value["placement_fingerprint"],
            graph_fingerprint=value["graph_fingerprint"],
            captures=tuple(
                AdmittedRegionCapture.from_mapping(item) for item in raw_captures
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def _spec_from_mapping(raw: Any) -> NullWalkSpec:
    expected = {
        "schema_version",
        "crossover_fc_hz",
        "geometry_seed_us",
        "positive_delay_target",
        "negative_delay_target",
        "half_period_us",
        "lower_bound_us",
        "upper_bound_us",
        "step_us",
        "candidate_count",
        "candidate_delays_us",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CommissioningEvidenceError("delay walk spec fields are invalid")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise CommissioningEvidenceError("delay walk spec schema is unsupported")
    try:
        result = NullWalkSpec(
            crossover_fc_hz=raw["crossover_fc_hz"],
            geometry_seed_us=raw["geometry_seed_us"],
            positive_delay_target=raw["positive_delay_target"],
            negative_delay_target=raw["negative_delay_target"],
            step_us=raw["step_us"],
        )
        expected_payload = result.to_dict()
    except NullWalkError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc
    if dict(raw) != expected_payload:
        raise CommissioningEvidenceError(
            "delay walk spec is not the exact canonical shared grid"
        )
    return result


@dataclass(frozen=True, slots=True)
class DelayWalkEvidence:
    """A complete shared null-walk grid; this type makes no winning-delay claim."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    speaker_group_id: str
    region_id: str
    algorithm_id: str
    algorithm_version: str
    spec: NullWalkSpec
    placement_fingerprint: str
    points: tuple[DelayPointEvidence, ...]
    repeatability_artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authority, CommissioningEvidenceAuthority):
            raise CommissioningEvidenceError(
                "delay walk authority must be CommissioningEvidenceAuthority"
            )
        object.__setattr__(
            self,
            "plan_fingerprint",
            _sha256(self.plan_fingerprint, field_name="plan_fingerprint"),
        )
        for name in ("speaker_group_id", "region_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        if self.algorithm_id != DELAY_WALK_ALGORITHM_ID:
            raise CommissioningEvidenceError("delay walk algorithm is unsupported")
        if self.algorithm_version != DELAY_WALK_ALGORITHM_VERSION:
            raise CommissioningEvidenceError(
                "delay walk algorithm version is unsupported"
            )
        if not isinstance(self.spec, NullWalkSpec):
            raise CommissioningEvidenceError("delay walk spec must be NullWalkSpec")
        try:
            candidates = self.spec.candidate_delays_us()
        except NullWalkError as exc:
            raise CommissioningEvidenceError(str(exc)) from exc
        object.__setattr__(
            self,
            "placement_fingerprint",
            _sha256(self.placement_fingerprint, field_name="placement_fingerprint"),
        )
        if type(self.points) is not tuple or not self.points:
            raise CommissioningEvidenceError("delay walk points must be non-empty")
        if any(not isinstance(item, DelayPointEvidence) for item in self.points):
            raise CommissioningEvidenceError(
                "delay walk points must be DelayPointEvidence values"
            )
        coordinates = tuple(item.relative_delay_us for item in self.points)
        if coordinates != candidates:
            raise CommissioningEvidenceError(
                "delay walk points must cover the exact canonical shared grid"
            )
        if any(
            point.authority != self.authority
            or point.plan_fingerprint != self.plan_fingerprint
            or point.speaker_group_id != self.speaker_group_id
            or point.region_id != self.region_id
            or point.placement_fingerprint != self.placement_fingerprint
            for point in self.points
        ):
            raise CommissioningEvidenceError(
                "delay walk points do not share one exact plan and placement"
            )
        attempts = [point.attempt_id for point in self.points]
        if len(set(attempts)) != len(attempts):
            raise CommissioningEvidenceError(
                "every delay coordinate requires a distinct durable attempt"
            )
        _assert_authority_artifact(
            self.repeatability_artifact,
            self.authority,
            field_name="repeatability_artifact",
        )
        capture_artifact_fingerprints = {
            artifact.fingerprint
            for point in self.points
            for capture in point.captures
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.capture.admission_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        }
        capture_artifact_paths = {
            artifact.relative_path
            for point in self.points
            for capture in point.captures
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.capture.admission_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        }
        if (
            self.repeatability_artifact.fingerprint in capture_artifact_fingerprints
            or self.repeatability_artifact.relative_path in capture_artifact_paths
        ):
            raise CommissioningEvidenceError(
                "delay repeatability artifact must have a distinct artifact role"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_delay_walk_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "spec": self.spec.to_dict(),
            "placement_fingerprint": self.placement_fingerprint,
            "points": [point.to_dict() for point in self.points],
            "repeatability_artifact": self.repeatability_artifact.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "DelayWalkEvidence":
        value = _strict_object(
            raw,
            kind="jts_active_delay_walk_evidence",
            fields=frozenset(
                {
                    "authority",
                    "plan_fingerprint",
                    "speaker_group_id",
                    "region_id",
                    "algorithm_id",
                    "algorithm_version",
                    "spec",
                    "placement_fingerprint",
                    "points",
                    "repeatability_artifact",
                }
            ),
        )
        raw_points = value["points"]
        if type(raw_points) is not list:
            raise CommissioningEvidenceError("delay walk points must be a list")
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            plan_fingerprint=value["plan_fingerprint"],
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            algorithm_id=value["algorithm_id"],
            algorithm_version=value["algorithm_version"],
            spec=_spec_from_mapping(value["spec"]),
            placement_fingerprint=value["placement_fingerprint"],
            points=tuple(DelayPointEvidence.from_mapping(item) for item in raw_points),
            repeatability_artifact=_artifact_from_mapping(
                value["repeatability_artifact"]
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


def _capture_role_identities(
    captures: tuple[AdmittedRegionCapture, ...],
) -> dict[str, set[str]]:
    return {
        "capture_ids": {capture.capture.capture_id for capture in captures},
        "raw_artifacts": {
            capture.capture.raw_artifact.fingerprint for capture in captures
        },
        "raw_bytes": {capture.capture.raw_artifact.sha256 for capture in captures},
        "generation_artifacts": {
            capture.generation_artifact.fingerprint for capture in captures
        },
        "generation_paths": {
            capture.generation_artifact.relative_path for capture in captures
        },
        "playback_artifacts": {
            capture.playback_artifact.fingerprint for capture in captures
        },
        "playback_paths": {
            capture.playback_artifact.relative_path for capture in captures
        },
        "stimulus_artifacts": {
            capture.stimulus.artifact.fingerprint for capture in captures
        },
        "stimulus_paths": {
            capture.stimulus.artifact.relative_path for capture in captures
        },
    }


@dataclass(frozen=True, slots=True)
class RegionCommissioningEvidence:
    """Complete normal, reverse, and delay evidence for one exact region."""

    plan: RegionEvidencePlan
    target: RegionEvidenceTarget
    normal: StationaryRegionEvidence
    reverse: StationaryRegionEvidence
    delay_walk: DelayWalkEvidence
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RegionEvidencePlan):
            raise CommissioningEvidenceError("plan must be RegionEvidencePlan")
        if (
            not isinstance(self.target, RegionEvidenceTarget)
            or self.target not in self.plan.targets
        ):
            raise CommissioningEvidenceError(
                "target must belong to the exact region plan"
            )
        if not isinstance(self.normal, StationaryRegionEvidence) or not isinstance(
            self.reverse, StationaryRegionEvidence
        ):
            raise CommissioningEvidenceError(
                "normal and reverse must be StationaryRegionEvidence"
            )
        if not isinstance(self.delay_walk, DelayWalkEvidence):
            raise CommissioningEvidenceError("delay_walk must be DelayWalkEvidence")
        common = (
            self.target.speaker_group_id,
            self.target.region_id,
            self.plan.fingerprint,
        )
        if (
            (
                self.normal.speaker_group_id,
                self.normal.region_id,
                self.normal.plan_fingerprint,
            )
            != common
            or (
                self.reverse.speaker_group_id,
                self.reverse.region_id,
                self.reverse.plan_fingerprint,
            )
            != common
            or (
                self.delay_walk.speaker_group_id,
                self.delay_walk.region_id,
                self.delay_walk.plan_fingerprint,
            )
            != common
        ):
            raise CommissioningEvidenceError(
                "region evidence does not belong to one exact plan target"
            )
        if (
            self.normal.authority != self.plan.authority
            or self.reverse.authority != self.plan.authority
            or self.delay_walk.authority != self.plan.authority
        ):
            raise CommissioningEvidenceError(
                "region evidence authorities do not match the exact run plan"
            )
        if (
            self.normal.evidence_kind != "normal"
            or self.normal.target_fingerprint != self.target.normal_target_fingerprint
            or self.normal.context_base_fingerprint
            != self.target.normal_context_base_fingerprint
            or self.reverse.evidence_kind != "reverse"
            or self.reverse.target_fingerprint != self.target.reverse_target_fingerprint
            or self.reverse.context_base_fingerprint
            != self.target.reverse_context_base_fingerprint
        ):
            raise CommissioningEvidenceError(
                "normal and reverse evidence identities do not match their phase targets"
            )
        if self.normal.placement_fingerprint != self.reverse.placement_fingerprint:
            raise CommissioningEvidenceError(
                "normal and reverse evidence must retain one stationary placement"
            )
        if self.delay_walk.placement_fingerprint != self.normal.placement_fingerprint:
            raise CommissioningEvidenceError(
                "delay walk must retain the normal/reverse stationary placement"
            )
        attempts = [
            self.normal.attempt_id,
            self.reverse.attempt_id,
            *(point.attempt_id for point in self.delay_walk.points),
        ]
        if len(set(attempts)) != len(attempts):
            raise CommissioningEvidenceError(
                "normal, reverse, and every delay point require distinct attempts"
            )
        if not math.isclose(
            self.delay_walk.spec.crossover_fc_hz,
            self.target.electrical_fc_hz,
            rel_tol=0.0,
            abs_tol=1e-9,
        ) or (
            self.delay_walk.spec.positive_delay_target != self.target.upper_role
            or self.delay_walk.spec.negative_delay_target != self.target.lower_role
        ):
            raise CommissioningEvidenceError(
                "delay walk spec does not match the region electrical crossover"
            )
        for point in self.delay_walk.points:
            expected_target = delay_point_target_fingerprint(
                self.target,
                self.delay_walk.spec,
                point.relative_delay_us,
            )
            expected_context_base = delay_point_context_base_fingerprint(
                self.target,
                self.delay_walk.spec,
                point.relative_delay_us,
                point.graph_fingerprint,
            )
            if (
                point.target_fingerprint != expected_target
                or point.context_base_fingerprint != expected_context_base
            ):
                raise CommissioningEvidenceError(
                    "delay point target or graph-bound context is stale"
                )
        normal_ids = _capture_role_identities(self.normal.captures)
        reverse_ids = _capture_role_identities(self.reverse.captures)
        if any(normal_ids[key] & reverse_ids[key] for key in normal_ids):
            raise CommissioningEvidenceError(
                "normal captures or artifacts cannot be replayed as reverse evidence"
            )
        accumulated = {key: normal_ids[key] | reverse_ids[key] for key in normal_ids}
        for point in self.delay_walk.points:
            point_ids = _capture_role_identities(point.captures)
            if any(accumulated[key] & point_ids[key] for key in accumulated):
                raise CommissioningEvidenceError(
                    "delay captures must be fresh from normal and reverse captures"
                )
            for key in accumulated:
                accumulated[key].update(point_ids[key])
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_region_commissioning_evidence",
            "plan": self.plan.to_dict(),
            "target": self.target.to_dict(),
            "normal": self.normal.to_dict(),
            "reverse": self.reverse.to_dict(),
            "delay_walk": self.delay_walk.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "RegionCommissioningEvidence":
        value = _strict_object(
            raw,
            kind="jts_active_region_commissioning_evidence",
            fields=frozenset({"plan", "target", "normal", "reverse", "delay_walk"}),
        )
        result = cls(
            plan=RegionEvidencePlan.from_mapping(value["plan"]),
            target=RegionEvidenceTarget.from_mapping(value["target"]),
            normal=StationaryRegionEvidence.from_mapping(value["normal"]),
            reverse=StationaryRegionEvidence.from_mapping(value["reverse"]),
            delay_walk=DelayWalkEvidence.from_mapping(value["delay_walk"]),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result
