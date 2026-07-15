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
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.excitation_admission import ExcitationAdmission
from jasper.audio_measurement.excitation_artifacts import (
    admission_artifact_relative_path,
    canonical_admission_bytes,
)
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    MIN_CAPTURE_COUNT,
    NullWalkError,
    NullWalkSpec,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.output_topology import OutputTopology

from .baseline_profile import topology_config_fingerprint
from .bundles import BUNDLE_KIND
from .commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunError,
    CommissioningRunHandle,
)
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
DELAY_WALK_ALGORITHM_VERSION = "2"
ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID = "jts_active_summed_crossover_capture"
ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION = "1"

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


def measurement_kind_for_evidence(evidence_kind: EvidenceKind) -> str:
    """Return the canonical capture-identity kind for one region operation."""

    try:
        return _MEASUREMENT_KIND_BY_EVIDENCE[evidence_kind]
    except KeyError as exc:
        raise CommissioningEvidenceError(
            "capture evidence_kind is unsupported"
        ) from exc


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


def _finite_positive(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningEvidenceError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise CommissioningEvidenceError(f"{field_name} must be positive and finite")
    return result


def _finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningEvidenceError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise CommissioningEvidenceError(f"{field_name} must be finite")
    return result


def _fingerprint(payload: Mapping[str, Any]) -> str:
    try:
        return json_fingerprint(payload, field_name="commissioning evidence payload")
    except EvidenceIdentityError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


def active_region_threshold_profile_fingerprint() -> str:
    """Fingerprint the one code-owned quality model used by region capture."""

    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_region_threshold_profile",
            "quality_model": asdict(DRIVER),
            "summed_analyzer": {
                "policy_id": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
                "policy_version": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
            },
        }
    )


def active_region_context_fingerprint(
    *,
    baseline_active_raw_fingerprint: str,
    calibration_id: str,
    calibration: CalibrationCurve,
) -> str:
    """Bind one region program to its exact baseline and mic calibration."""

    baseline = _sha256(
        baseline_active_raw_fingerprint,
        field_name="baseline_active_raw_fingerprint",
    )
    calibration_name = _text(calibration_id, field_name="calibration_id")
    if not isinstance(calibration, CalibrationCurve):
        raise CommissioningEvidenceError("calibration must be CalibrationCurve")
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_region_evidence_context",
            "baseline_active_raw_fingerprint": baseline,
            "calibration_id": calibration_name,
            "calibration": calibration.to_dict(),
        }
    )


def _strict_object(
    raw: Any,
    *,
    kind: str,
    fields: frozenset[str],
    schema_version: int = 1,
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise CommissioningEvidenceError(f"{kind} must be an object")
    expected = fields | {"schema_version", "kind", "fingerprint"}
    if set(raw) != expected:
        raise CommissioningEvidenceError(f"{kind} has unknown or missing fields")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != schema_version
    ):
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


def _run_handle_to_dict(value: CommissioningRunHandle) -> dict[str, Any]:
    return {
        "session_id": value.session_id,
        "session_fingerprint": value.session_fingerprint,
        "run_id": value.run_id,
        "owner_id": value.owner_id,
        "owner_generation": value.owner_generation,
    }


def _run_handle_from_mapping(raw: Any) -> CommissioningRunHandle:
    fields = {
        "session_id",
        "session_fingerprint",
        "run_id",
        "owner_id",
        "owner_generation",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise CommissioningEvidenceError("commissioning run handle fields are invalid")
    try:
        return CommissioningRunHandle(**{name: raw[name] for name in fields})
    except (CommissioningRunError, TypeError) as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


def _attempt_handle_to_dict(value: CommissioningAttemptHandle) -> dict[str, Any]:
    return {
        "run": _run_handle_to_dict(value.run),
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "target_id": value.target_id,
        "target_fingerprint": value.target_fingerprint,
    }


def _attempt_handle_from_mapping(raw: Any) -> CommissioningAttemptHandle:
    fields = {
        "run",
        "attempt_id",
        "attempt_number",
        "target_id",
        "target_fingerprint",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise CommissioningEvidenceError(
            "commissioning attempt handle fields are invalid"
        )
    try:
        return CommissioningAttemptHandle(
            run=_run_handle_from_mapping(raw["run"]),
            attempt_id=raw["attempt_id"],
            attempt_number=raw["attempt_number"],
            target_id=raw["target_id"],
            target_fingerprint=raw["target_fingerprint"],
        )
    except (CommissioningRunError, TypeError) as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class CommissioningEvidenceAuthority:
    """Exact run and immutable environment bound into every evidence value."""

    run: CommissioningRunHandle
    topology_id: str
    topology_fingerprint: str
    protected_safety_profile_fingerprint: str
    comparison_set_fingerprint: str
    threshold_profile_fingerprint: str
    context_fingerprint: str
    expected_geometry_id: str = REFERENCE_AXIS_GEOMETRY_ID
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run, CommissioningRunHandle):
            raise CommissioningEvidenceError("run must be CommissioningRunHandle")
        object.__setattr__(
            self, "topology_id", _identifier(self.topology_id, field_name="topology_id")
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
        if self.comparison_set_fingerprint != self.run.session_fingerprint:
            raise CommissioningEvidenceError(
                "comparison set must equal the durable run session fingerprint"
            )
        if self.expected_geometry_id != REFERENCE_AXIS_GEOMETRY_ID:
            raise CommissioningEvidenceError(
                "region evidence authority requires reference_axis geometry"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def owner_generation(self) -> int:
        return self.run.owner_generation

    @property
    def commissioning_session_id(self) -> str:
        return self.run.session_id

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_commissioning_evidence_authority",
            "run": _run_handle_to_dict(self.run),
            "topology_id": self.topology_id,
            "topology_fingerprint": self.topology_fingerprint,
            "protected_safety_profile_fingerprint": (
                self.protected_safety_profile_fingerprint
            ),
            "comparison_set_fingerprint": self.comparison_set_fingerprint,
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
                    "run",
                    "topology_id",
                    "topology_fingerprint",
                    "protected_safety_profile_fingerprint",
                    "comparison_set_fingerprint",
                    "threshold_profile_fingerprint",
                    "context_fingerprint",
                    "expected_geometry_id",
                }
            ),
        )
        result = cls(
            run=_run_handle_from_mapping(value["run"]),
            topology_id=value["topology_id"],
            topology_fingerprint=value["topology_fingerprint"],
            protected_safety_profile_fingerprint=value[
                "protected_safety_profile_fingerprint"
            ],
            comparison_set_fingerprint=value["comparison_set_fingerprint"],
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
        if evidence_kind == "delay_null":
            return self.delay_target_base_fingerprint
        raise CommissioningEvidenceError("target evidence_kind is unsupported")

    def context_base_fingerprint_for(self, evidence_kind: EvidenceKind) -> str:
        if evidence_kind == "normal":
            return self.normal_context_base_fingerprint
        if evidence_kind == "reverse":
            return self.reverse_context_base_fingerprint
        if evidence_kind == "delay_null":
            return self.delay_context_base_fingerprint
        raise CommissioningEvidenceError("context evidence_kind is unsupported")

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


def region_evidence_preset_fingerprint(preset: ActiveSpeakerPreset) -> str:
    """Fingerprint the exact typed preset bound into every region plan."""

    if not isinstance(preset, ActiveSpeakerPreset):
        raise CommissioningEvidenceError("preset must be ActiveSpeakerPreset")
    try:
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        raise CommissioningEvidenceError(f"capture preset is invalid: {exc}") from exc
    return _fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_region_evidence_preset",
            "preset": preset.to_dict(),
        }
    )


def derive_region_evidence_plan(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology,
    *,
    run: CommissioningRunHandle,
    protected_safety_profile_fingerprint: str,
    comparison_set_fingerprint: str,
    threshold_profile_fingerprint: str,
    context_fingerprint: str,
) -> RegionEvidencePlan:
    """Derive the exact group-by-region plan from current typed product state."""

    if not isinstance(preset, ActiveSpeakerPreset):
        raise CommissioningEvidenceError("preset must be ActiveSpeakerPreset")
    if not isinstance(topology, OutputTopology):
        raise CommissioningEvidenceError("topology must be OutputTopology")
    if not isinstance(run, CommissioningRunHandle):
        raise CommissioningEvidenceError("run must be CommissioningRunHandle")
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
        run=run,
        topology_id=topology.topology_id,
        topology_fingerprint=topology_fingerprint,
        protected_safety_profile_fingerprint=protected_safety_profile_fingerprint,
        comparison_set_fingerprint=comparison_set_fingerprint,
        threshold_profile_fingerprint=threshold_profile_fingerprint,
        context_fingerprint=context_fingerprint,
    )
    preset_fingerprint = region_evidence_preset_fingerprint(preset)
    expected_mode = f"active_{preset.way_count}_way"
    active_groups = [
        group for group in topology.speaker_groups if group.mode.startswith("active_")
    ]
    if any(group.mode != expected_mode for group in active_groups):
        raise CommissioningEvidenceError(
            "current topology active group modes do not match the capture preset"
        )
    if preset.channel_map.layout == "mono":
        if len(active_groups) != 1 or active_groups[0].kind != "mono":
            raise CommissioningEvidenceError(
                "mono capture preset requires exactly one mono active speaker group"
            )
        groups = active_groups
    elif preset.channel_map.layout == "stereo":
        by_kind = {group.kind: group for group in active_groups}
        if len(active_groups) != 2 or set(by_kind) != {"left", "right"}:
            raise CommissioningEvidenceError(
                "stereo capture preset requires exactly left and right active speaker groups"
            )
        groups = [by_kind["left"], by_kind["right"]]
    else:  # ActiveSpeakerPreset.validate() owns the supported layout vocabulary.
        raise CommissioningEvidenceError("capture preset layout is unsupported")

    expected_roles = set(required_driver_roles(preset.way_count))
    for group in groups:
        if {channel.role for channel in group.channels} != expected_roles:
            raise CommissioningEvidenceError(
                f"speaker group {group.id} does not match preset driver roles"
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
    attempt: CommissioningAttemptHandle,
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
    if not isinstance(attempt, CommissioningAttemptHandle):
        raise CommissioningEvidenceError("attempt must be CommissioningAttemptHandle")
    if attempt.run != authority.run:
        raise CommissioningEvidenceError(
            "attempt does not belong to the exact durable run authority"
        )
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
            "attempt": _attempt_handle_to_dict(attempt),
            "evidence_kind": evidence_kind,
            "target_fingerprint": target,
            "context_base_fingerprint": context_base,
            "graph_fingerprint": graph,
            "generation_protection_evidence_fingerprint": generation_proof,
            "playback_protection_evidence_fingerprint": playback_proof,
        }
    )


def evidence_attempt_target_id(
    evidence_kind: EvidenceKind,
    target_fingerprint: str,
) -> str:
    """Return the bounded durable reservation id for one evidence target."""

    if evidence_kind not in _MEASUREMENT_KIND_BY_EVIDENCE:
        raise CommissioningEvidenceError("attempt evidence_kind is unsupported")
    target = _sha256(target_fingerprint, field_name="target_fingerprint")
    return f"active:{evidence_kind}:{target}"


@dataclass(frozen=True, slots=True)
class AdmittedRegionCapture:
    """One fresh, one-shot, graph-confirmed region capture."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    attempt: CommissioningAttemptHandle
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
        for name in ("speaker_group_id", "region_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        try:
            expected_generation_path = admission_artifact_relative_path(
                "generation", self.admission_id
            )
            expected_playback_path = admission_artifact_relative_path(
                "playback", self.admission_id
            )
        except ValueError as exc:
            raise CommissioningEvidenceError(str(exc)) from exc
        if not isinstance(self.attempt, CommissioningAttemptHandle):
            raise CommissioningEvidenceError(
                "capture attempt must be CommissioningAttemptHandle"
            )
        if self.attempt.run != self.authority.run:
            raise CommissioningEvidenceError(
                "capture attempt does not belong to the exact durable run authority"
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
        if (
            self.attempt.target_fingerprint != self.target_fingerprint
            or self.attempt.target_id
            != evidence_attempt_target_id(
                self.evidence_kind,
                self.target_fingerprint,
            )
        ):
            raise CommissioningEvidenceError(
                "capture does not equal its reserved attempt target"
            )
        expected_context = capture_attempt_context_fingerprint(
            self.authority,
            attempt=self.attempt,
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

    @property
    def attempt_id(self) -> str:
        return self.attempt.attempt_id

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_admitted_region_capture",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt": _attempt_handle_to_dict(self.attempt),
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
                "attempt",
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
            attempt=_attempt_handle_from_mapping(value["attempt"]),
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
        "raw paths": [item.capture.raw_artifact.relative_path for item in captures],
        "raw bytes": [item.capture.raw_artifact.sha256 for item in captures],
        "analysis-input artifacts": [
            item.capture.analysis_input_artifact.fingerprint for item in captures
        ],
        "analysis-input paths": [
            item.capture.analysis_input_artifact.relative_path for item in captures
        ],
        "quality artifacts": [
            item.capture.quality_artifact.fingerprint for item in captures
        ],
        "quality paths": [
            item.capture.quality_artifact.relative_path for item in captures
        ],
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
        "artifact roles": [
            artifact.fingerprint
            for item in captures
            for artifact in (
                item.capture.raw_artifact,
                item.capture.analysis_input_artifact,
                item.capture.quality_artifact,
                item.playback_artifact,
                item.stimulus.artifact,
                item.generation_artifact,
            )
        ],
        "artifact role paths": [
            artifact.relative_path
            for item in captures
            for artifact in (
                item.capture.raw_artifact,
                item.capture.analysis_input_artifact,
                item.capture.quality_artifact,
                item.playback_artifact,
                item.stimulus.artifact,
                item.generation_artifact,
            )
        ],
    }
    for label, values in unique_fields.items():
        if len(set(values)) != len(values):
            raise CommissioningEvidenceError(
                f"fresh one-shot evidence requires unique {label}"
            )


@dataclass(frozen=True, slots=True)
class StationaryRegionEvidence:
    """Exactly three fresh captures for one normal or reverse stationary target."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    attempt: CommissioningAttemptHandle
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
        if not isinstance(self.attempt, CommissioningAttemptHandle):
            raise CommissioningEvidenceError(
                "stationary attempt must be CommissioningAttemptHandle"
            )
        if self.attempt.run != self.authority.run:
            raise CommissioningEvidenceError(
                "stationary attempt does not belong to the exact durable run authority"
            )
        if self.evidence_kind not in {"normal", "reverse"}:
            raise CommissioningEvidenceError(
                "stationary evidence must be normal or reverse"
            )
        expected_attempt_target_id = evidence_attempt_target_id(
            self.evidence_kind,
            self.target_fingerprint,
        )
        if (
            self.attempt.target_id != expected_attempt_target_id
            or self.attempt.target_fingerprint != self.target_fingerprint
        ):
            raise CommissioningEvidenceError(
                "stationary evidence does not equal its reserved attempt target"
            )
        _assert_fresh_capture_set(
            self.captures,
            expected_count=STATIONARY_CAPTURE_COUNT,
        )
        if any(
            capture.authority != self.authority
            or capture.plan_fingerprint != self.plan_fingerprint
            or capture.attempt != self.attempt
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

    @property
    def attempt_id(self) -> str:
        return self.attempt.attempt_id

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_stationary_region_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt": _attempt_handle_to_dict(self.attempt),
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
                "attempt",
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
            attempt=_attempt_handle_from_mapping(value["attempt"]),
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
    attempt: CommissioningAttemptHandle
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
        if not isinstance(self.attempt, CommissioningAttemptHandle):
            raise CommissioningEvidenceError(
                "delay point attempt must be CommissioningAttemptHandle"
            )
        if self.attempt.run != self.authority.run:
            raise CommissioningEvidenceError(
                "delay point attempt does not belong to the exact durable run authority"
            )
        if isinstance(self.relative_delay_us, bool) or not isinstance(
            self.relative_delay_us, (int, float)
        ):
            raise CommissioningEvidenceError("relative_delay_us must be finite")
        relative_delay = float(self.relative_delay_us)
        if not math.isfinite(relative_delay):
            raise CommissioningEvidenceError("relative_delay_us must be finite")
        object.__setattr__(self, "relative_delay_us", relative_delay)
        expected_attempt_target_id = evidence_attempt_target_id(
            "delay_null",
            self.target_fingerprint,
        )
        if (
            self.attempt.target_id != expected_attempt_target_id
            or self.attempt.target_fingerprint != self.target_fingerprint
        ):
            raise CommissioningEvidenceError(
                "delay point evidence does not equal its reserved attempt target"
            )
        _assert_fresh_capture_set(self.captures, expected_count=MIN_CAPTURE_COUNT)
        if any(
            capture.authority != self.authority
            or capture.plan_fingerprint != self.plan_fingerprint
            or capture.attempt != self.attempt
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
    def attempt_id(self) -> str:
        return self.attempt.attempt_id

    @property
    def canonical_key(self) -> float:
        return self.relative_delay_us

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_delay_point_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "attempt": _attempt_handle_to_dict(self.attempt),
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
                "attempt",
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
            attempt=_attempt_handle_from_mapping(value["attempt"]),
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
    try:
        return NullWalkSpec.from_mapping(raw)
    except NullWalkError as exc:
        raise CommissioningEvidenceError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RegionGeometryAttestation:
    """Explicit signed acoustic-center provenance for one exact region target."""

    speaker_group_id: str
    region_id: str
    region_target_fingerprint: str
    signed_geometry_seed_us: float
    provenance_kind: Literal["operator_attested"]
    provenance_id: str
    attestation_artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("speaker_group_id", "region_id", "provenance_id"):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "region_target_fingerprint",
            _sha256(
                self.region_target_fingerprint,
                field_name="region_target_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "signed_geometry_seed_us",
            _finite(
                self.signed_geometry_seed_us,
                field_name="signed_geometry_seed_us",
            ),
        )
        if self.provenance_kind != "operator_attested":
            raise CommissioningEvidenceError(
                "region geometry requires explicit operator attestation"
            )
        if not isinstance(self.attestation_artifact, ArtifactIdentity):
            raise CommissioningEvidenceError(
                "geometry attestation artifact must be ArtifactIdentity"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_region_geometry_attestation",
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "region_target_fingerprint": self.region_target_fingerprint,
            "signed_geometry_seed_us": self.signed_geometry_seed_us,
            "provenance_kind": self.provenance_kind,
            "provenance_id": self.provenance_id,
            "attestation_artifact": self.attestation_artifact.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "RegionGeometryAttestation":
        value = _strict_object(
            raw,
            kind="jts_active_region_geometry_attestation",
            fields=frozenset(
                {
                    "speaker_group_id",
                    "region_id",
                    "region_target_fingerprint",
                    "signed_geometry_seed_us",
                    "provenance_kind",
                    "provenance_id",
                    "attestation_artifact",
                }
            ),
        )
        result = cls(
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            region_target_fingerprint=value["region_target_fingerprint"],
            signed_geometry_seed_us=value["signed_geometry_seed_us"],
            provenance_kind=value["provenance_kind"],
            provenance_id=value["provenance_id"],
            attestation_artifact=_artifact_from_mapping(value["attestation_artifact"]),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result


@dataclass(frozen=True, slots=True)
class DelayWalkEvidence:
    """A complete bounded null-walk schedule; this makes no winning-delay claim."""

    authority: CommissioningEvidenceAuthority
    plan_fingerprint: str
    speaker_group_id: str
    region_id: str
    algorithm_id: str
    algorithm_version: str
    geometry_attestation: RegionGeometryAttestation
    spec: NullWalkSpec
    schedule: BoundedNullWalkSchedule
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
        if not isinstance(self.geometry_attestation, RegionGeometryAttestation):
            raise CommissioningEvidenceError(
                "delay walk requires explicit region geometry attestation"
            )
        if (
            self.geometry_attestation.speaker_group_id != self.speaker_group_id
            or self.geometry_attestation.region_id != self.region_id
        ):
            raise CommissioningEvidenceError(
                "geometry attestation does not belong to this delay-walk region"
            )
        _assert_authority_artifact(
            self.geometry_attestation.attestation_artifact,
            self.authority,
            field_name="geometry_attestation.attestation_artifact",
        )
        if not isinstance(self.spec, NullWalkSpec):
            raise CommissioningEvidenceError("delay walk spec must be NullWalkSpec")
        if not isinstance(self.schedule, BoundedNullWalkSchedule):
            raise CommissioningEvidenceError(
                "delay walk schedule must be BoundedNullWalkSchedule"
            )
        if self.schedule.spec_fingerprint != self.spec.fingerprint:
            raise CommissioningEvidenceError(
                "delay walk schedule does not belong to the exact shared spec"
            )
        if not math.isclose(
            self.spec.geometry_seed_us,
            self.geometry_attestation.signed_geometry_seed_us,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise CommissioningEvidenceError(
                "delay walk spec is not bound to its signed geometry attestation"
            )
        candidates = self.schedule.scheduled_delays_us
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
                "delay walk points must cover the exact bounded shared schedule"
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
        attempt_numbers = [point.attempt.attempt_number for point in self.points]
        if len(set(attempts)) != len(attempts) or len(set(attempt_numbers)) != len(
            attempt_numbers
        ):
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
            or self.geometry_attestation.attestation_artifact.fingerprint
            in capture_artifact_fingerprints
            or self.geometry_attestation.attestation_artifact.relative_path
            in capture_artifact_paths
            or self.repeatability_artifact.fingerprint
            == self.geometry_attestation.attestation_artifact.fingerprint
            or self.repeatability_artifact.relative_path
            == self.geometry_attestation.attestation_artifact.relative_path
        ):
            raise CommissioningEvidenceError(
                "delay metadata artifacts must have distinct artifact roles"
            )
        accumulated: dict[str, set[str]] | None = None
        for point in self.points:
            point_ids = _capture_role_identities(point.captures)
            if accumulated is not None and any(
                accumulated[key] & point_ids[key] for key in accumulated
            ):
                raise CommissioningEvidenceError(
                    "every delay point requires globally fresh capture roles"
                )
            if accumulated is None:
                accumulated = point_ids
            else:
                for key in accumulated:
                    accumulated[key].update(point_ids[key])
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "kind": "jts_active_delay_walk_evidence",
            "authority": self.authority.to_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "geometry_attestation": self.geometry_attestation.to_dict(),
            "spec": self.spec.to_dict(),
            "schedule": self.schedule.to_dict(),
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
                    "geometry_attestation",
                    "spec",
                    "schedule",
                    "placement_fingerprint",
                    "points",
                    "repeatability_artifact",
                }
            ),
            schema_version=2,
        )
        raw_points = value["points"]
        if type(raw_points) is not list:
            raise CommissioningEvidenceError("delay walk points must be a list")
        spec = _spec_from_mapping(value["spec"])
        try:
            schedule = BoundedNullWalkSchedule.from_mapping(
                value["schedule"],
                spec=spec,
            )
        except NullWalkError as exc:
            raise CommissioningEvidenceError(str(exc)) from exc
        result = cls(
            authority=CommissioningEvidenceAuthority.from_mapping(value["authority"]),
            plan_fingerprint=value["plan_fingerprint"],
            speaker_group_id=value["speaker_group_id"],
            region_id=value["region_id"],
            algorithm_id=value["algorithm_id"],
            algorithm_version=value["algorithm_version"],
            geometry_attestation=RegionGeometryAttestation.from_mapping(
                value["geometry_attestation"]
            ),
            spec=spec,
            schedule=schedule,
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
        "capture_identities": {capture.capture.fingerprint for capture in captures},
        "raw_artifacts": {
            capture.capture.raw_artifact.fingerprint for capture in captures
        },
        "raw_paths": {
            capture.capture.raw_artifact.relative_path for capture in captures
        },
        "raw_bytes": {capture.capture.raw_artifact.sha256 for capture in captures},
        "analysis_input_artifacts": {
            capture.capture.analysis_input_artifact.fingerprint for capture in captures
        },
        "analysis_input_paths": {
            capture.capture.analysis_input_artifact.relative_path
            for capture in captures
        },
        "quality_artifacts": {
            capture.capture.quality_artifact.fingerprint for capture in captures
        },
        "quality_paths": {
            capture.capture.quality_artifact.relative_path for capture in captures
        },
        "admission_ids": {capture.admission_id for capture in captures},
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
        "all_artifact_roles": {
            artifact.fingerprint
            for capture in captures
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.playback_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        },
        "all_artifact_paths": {
            artifact.relative_path
            for capture in captures
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.playback_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
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
        attempt_numbers = [
            self.normal.attempt.attempt_number,
            self.reverse.attempt.attempt_number,
            *(point.attempt.attempt_number for point in self.delay_walk.points),
        ]
        if len(set(attempts)) != len(attempts) or len(set(attempt_numbers)) != len(
            attempt_numbers
        ):
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
        if (
            self.delay_walk.geometry_attestation.region_target_fingerprint
            != self.target.fingerprint
        ):
            raise CommissioningEvidenceError(
                "geometry attestation does not belong to the exact region target"
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
        all_capture_artifacts = {
            artifact.fingerprint
            for capture in (
                *self.normal.captures,
                *self.reverse.captures,
                *(
                    capture
                    for point in self.delay_walk.points
                    for capture in point.captures
                ),
            )
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.playback_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        }
        all_capture_paths = {
            artifact.relative_path
            for capture in (
                *self.normal.captures,
                *self.reverse.captures,
                *(
                    capture
                    for point in self.delay_walk.points
                    for capture in point.captures
                ),
            )
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.playback_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        }
        for artifact in (
            self.delay_walk.repeatability_artifact,
            self.delay_walk.geometry_attestation.attestation_artifact,
        ):
            if (
                artifact.fingerprint in all_capture_artifacts
                or artifact.relative_path in all_capture_paths
            ):
                raise CommissioningEvidenceError(
                    "region metadata and capture artifact roles must be distinct"
                )
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


def _region_captures(
    region: RegionCommissioningEvidence,
) -> tuple[AdmittedRegionCapture, ...]:
    return (
        *region.normal.captures,
        *region.reverse.captures,
        *(capture for point in region.delay_walk.points for capture in point.captures),
    )


@dataclass(frozen=True, slots=True)
class CompleteCommissioningEvidence:
    """Exactly one fresh evidence value for every target in an immutable plan."""

    plan: RegionEvidencePlan
    regions: tuple[RegionCommissioningEvidence, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RegionEvidencePlan):
            raise CommissioningEvidenceError("complete evidence plan is invalid")
        if type(self.regions) is not tuple or any(
            not isinstance(item, RegionCommissioningEvidence) for item in self.regions
        ):
            raise CommissioningEvidenceError(
                "complete evidence regions must be a tuple of region evidence"
            )
        if tuple(region.target for region in self.regions) != self.plan.targets:
            raise CommissioningEvidenceError(
                "complete evidence requires exactly one canonically ordered region per plan target"
            )
        if any(region.plan != self.plan for region in self.regions):
            raise CommissioningEvidenceError(
                "complete region evidence must retain the exact shared plan"
            )

        captures = tuple(
            capture for region in self.regions for capture in _region_captures(region)
        )
        role_artifacts = [
            artifact
            for capture in captures
            for artifact in (
                capture.capture.raw_artifact,
                capture.capture.analysis_input_artifact,
                capture.capture.quality_artifact,
                capture.playback_artifact,
                capture.stimulus.artifact,
                capture.generation_artifact,
            )
        ]
        role_artifacts.extend(
            artifact
            for region in self.regions
            for artifact in (
                region.delay_walk.repeatability_artifact,
                region.delay_walk.geometry_attestation.attestation_artifact,
            )
        )
        if len({artifact.fingerprint for artifact in role_artifacts}) != len(
            role_artifacts
        ) or len({artifact.relative_path for artifact in role_artifacts}) != len(
            role_artifacts
        ):
            raise CommissioningEvidenceError(
                "complete evidence requires globally unique artifact roles and paths"
            )
        for label, values in (
            ("capture ids", [capture.capture.capture_id for capture in captures]),
            ("admission ids", [capture.admission_id for capture in captures]),
            (
                "raw bytes",
                [capture.capture.raw_artifact.sha256 for capture in captures],
            ),
        ):
            if len(set(values)) != len(values):
                raise CommissioningEvidenceError(
                    f"complete evidence requires globally unique {label}"
                )

        attempts = [
            attempt
            for region in self.regions
            for attempt in (
                region.normal.attempt,
                region.reverse.attempt,
                *(point.attempt for point in region.delay_walk.points),
            )
        ]
        if len({attempt.attempt_id for attempt in attempts}) != len(attempts) or len(
            {attempt.attempt_number for attempt in attempts}
        ) != len(attempts):
            raise CommissioningEvidenceError(
                "complete evidence requires globally distinct durable attempts"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_complete_commissioning_evidence",
            "plan": self.plan.to_dict(),
            "regions": [region.to_dict() for region in self.regions],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CompleteCommissioningEvidence":
        value = _strict_object(
            raw,
            kind="jts_active_complete_commissioning_evidence",
            fields=frozenset({"plan", "regions"}),
        )
        raw_regions = value["regions"]
        if type(raw_regions) is not list:
            raise CommissioningEvidenceError("complete evidence regions must be a list")
        result = cls(
            plan=RegionEvidencePlan.from_mapping(value["plan"]),
            regions=tuple(
                RegionCommissioningEvidence.from_mapping(item) for item in raw_regions
            ),
        )
        _declared_fingerprint(value, result.fingerprint)
        return result
