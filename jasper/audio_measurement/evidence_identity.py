# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict pure identities for measurement artifacts, captures, and replay.

Feature bundles retain ownership of paths, manifests, envelopes, and verdicts.
These small immutable values only bind exact feature-owned artifacts at a
shared measurement boundary; they perform no file I/O and do not reinterpret
feature evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

from .null_walk import DspPredecessor, NullWalkError

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
EXACT_DSP_STATE_DOMAIN = "camilladsp_exact_transaction_state"
ACTIVE_RAW_NORMALIZATION_DOMAIN = "camilladsp_active_raw"
ACTIVE_RAW_NORMALIZATION_ALGORITHM_ID = "jts_active_raw_canonical_json"
ACTIVE_RAW_NORMALIZATION_ALGORITHM_VERSION = "1"


class EvidenceIdentityError(ValueError):
    """Serialized evidence is malformed, ambiguous, or self-inconsistent."""


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceIdentityError(f"{field_name} must be a non-empty trimmed string")
    return value


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EvidenceIdentityError(
            f"{field_name} must be a lowercase SHA-256 fingerprint"
        )
    return value


def _freeze_json(value: Any, *, field_name: str, path: str = "$") -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise EvidenceIdentityError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, nested in value.items():
            if type(key) is not str:
                raise EvidenceIdentityError(
                    f"{field_name} contains a non-string key at {path}"
                )
            frozen[key] = _freeze_json(
                nested,
                field_name=field_name,
                path=f"{path}.{key}",
            )
        return frozen
    if type(value) is list:
        return [
            _freeze_json(nested, field_name=field_name, path=f"{path}[{index}]")
            for index, nested in enumerate(value)
        ]
    raise EvidenceIdentityError(f"{field_name} contains a non-JSON value at {path}")


def json_fingerprint(value: Mapping[str, Any], *, field_name: str = "payload") -> str:
    """Canonicalize one exact JSON object and return its SHA-256."""

    if not isinstance(value, Mapping) or not value:
        raise EvidenceIdentityError(f"{field_name} must be a non-empty mapping")
    canonical = json.dumps(
        _freeze_json(value, field_name=field_name),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return json_fingerprint(payload, field_name="identity payload")


def _strict_serialized_object(
    raw: Any,
    *,
    kind: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise EvidenceIdentityError(f"{kind} must be an object")
    expected = fields | {"schema_version", "kind", "fingerprint"}
    if set(raw) != expected:
        raise EvidenceIdentityError(f"{kind} has unknown or missing fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise EvidenceIdentityError(f"unsupported {kind} schema")
    if raw["kind"] != kind:
        raise EvidenceIdentityError(f"unsupported {kind} kind")
    return raw


def _declared_fingerprint(raw: Mapping[str, Any], actual: str) -> None:
    if raw["fingerprint"] != actual:
        raise EvidenceIdentityError("declared fingerprint does not match payload")


def _raw(raw: Mapping[str, Any], name: str) -> Any:
    return raw[name]


@dataclass(frozen=True)
class ArtifactIdentity:
    """Content-addressed identity for one feature-owned bundle artifact."""

    bundle_kind: str
    bundle_id: str
    relative_path: str
    sha256: str
    byte_size: int
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        bundle_kind = _text(self.bundle_kind, field_name="bundle_kind")
        bundle_id = _text(self.bundle_id, field_name="bundle_id")
        relative_path = _text(self.relative_path, field_name="relative_path")
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or relative_path != path.as_posix()
            or relative_path in {".", ".."}
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in relative_path
        ):
            raise EvidenceIdentityError(
                "relative_path must be a normalized bundle-relative POSIX path"
            )
        digest = _sha256(self.sha256, field_name="sha256")
        if type(self.byte_size) is not int or self.byte_size < 0:
            raise EvidenceIdentityError("byte_size must be a non-negative integer")
        object.__setattr__(self, "bundle_kind", bundle_kind)
        object.__setattr__(self, "bundle_id", bundle_id)
        object.__setattr__(self, "relative_path", relative_path)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_measurement_artifact_identity",
            "bundle_kind": self.bundle_kind,
            "bundle_id": self.bundle_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "ArtifactIdentity":
        value = _strict_serialized_object(
            raw,
            kind="jts_measurement_artifact_identity",
            fields=frozenset(
                {
                    "bundle_kind",
                    "bundle_id",
                    "relative_path",
                    "sha256",
                    "byte_size",
                }
            ),
        )
        identity = cls(
            bundle_kind=_raw(value, "bundle_kind"),
            bundle_id=_raw(value, "bundle_id"),
            relative_path=_raw(value, "relative_path"),
            sha256=_raw(value, "sha256"),
            byte_size=_raw(value, "byte_size"),
        )
        _declared_fingerprint(value, identity.fingerprint)
        return identity


@dataclass(frozen=True, init=False)
class ExactDspStateIdentity:
    """Content identity for the exact host-owned transaction/rollback state.

    This deliberately reuses :class:`DspPredecessor`'s strict JSON-domain
    canonicalization.  It is a different type and identity domain from a
    normalized ``active_raw`` graph: a graph equality proof must never stand in
    for the exact path/state payload needed by rollback.
    """

    state_domain: str
    _state_json: str = field(repr=False)
    state_fingerprint: str
    fingerprint: str

    def __init__(
        self,
        state: Mapping[str, Any],
        *,
        state_domain: str = EXACT_DSP_STATE_DOMAIN,
    ) -> None:
        if state_domain != EXACT_DSP_STATE_DOMAIN:
            raise EvidenceIdentityError("unsupported exact DSP state domain")
        try:
            predecessor = DspPredecessor(state)
        except NullWalkError as exc:
            raise EvidenceIdentityError(str(exc)) from exc
        frozen_state = predecessor.state
        state_json = json.dumps(
            frozen_state,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        object.__setattr__(self, "state_domain", state_domain)
        object.__setattr__(self, "_state_json", state_json)
        object.__setattr__(self, "state_fingerprint", predecessor.fingerprint)
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def state(self) -> dict[str, Any]:
        state = json.loads(self._state_json)
        assert isinstance(state, dict)
        return state

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_exact_dsp_state_identity",
            "state_domain": self.state_domain,
            "state": self.state,
            "state_fingerprint": self.state_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "ExactDspStateIdentity":
        value = _strict_serialized_object(
            raw,
            kind="jts_exact_dsp_state_identity",
            fields=frozenset({"state_domain", "state", "state_fingerprint"}),
        )
        if not isinstance(value["state"], Mapping):
            raise EvidenceIdentityError("exact DSP state must be an object")
        identity = cls(
            value["state"],
            state_domain=_raw(value, "state_domain"),
        )
        if value["state_fingerprint"] != identity.state_fingerprint:
            raise EvidenceIdentityError(
                "declared exact DSP state fingerprint does not match state"
            )
        _declared_fingerprint(value, identity.fingerprint)
        return identity


@dataclass(frozen=True, init=False)
class NormalizedActiveRawIdentity:
    """Typed/versioned identity for one normalized CamillaDSP ``active_raw``.

    The owning host performs normalization and supplies the resulting JSON
    graph.  This class freezes that graph with :class:`DspPredecessor`'s exact
    JSON rules and binds its content to one closed normalization domain,
    algorithm, and version.  It performs no CamillaDSP I/O or normalization.
    """

    normalization_domain: str
    normalization_algorithm_id: str
    normalization_algorithm_version: str
    _active_raw_json: str = field(repr=False)
    active_raw_fingerprint: str
    fingerprint: str

    def __init__(
        self,
        normalized_active_raw: Mapping[str, Any],
        *,
        normalization_domain: str = ACTIVE_RAW_NORMALIZATION_DOMAIN,
        normalization_algorithm_id: str = ACTIVE_RAW_NORMALIZATION_ALGORITHM_ID,
        normalization_algorithm_version: str = (
            ACTIVE_RAW_NORMALIZATION_ALGORITHM_VERSION
        ),
    ) -> None:
        if normalization_domain != ACTIVE_RAW_NORMALIZATION_DOMAIN:
            raise EvidenceIdentityError("unsupported active_raw normalization domain")
        if normalization_algorithm_id != ACTIVE_RAW_NORMALIZATION_ALGORITHM_ID:
            raise EvidenceIdentityError(
                "unsupported active_raw normalization algorithm"
            )
        if (
            normalization_algorithm_version
            != ACTIVE_RAW_NORMALIZATION_ALGORITHM_VERSION
        ):
            raise EvidenceIdentityError(
                "unsupported active_raw normalization algorithm version"
            )
        try:
            frozen = DspPredecessor(normalized_active_raw)
        except NullWalkError as exc:
            raise EvidenceIdentityError(str(exc)) from exc
        active_raw = frozen.state
        active_raw_json = json.dumps(
            active_raw,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        object.__setattr__(self, "normalization_domain", normalization_domain)
        object.__setattr__(
            self,
            "normalization_algorithm_id",
            normalization_algorithm_id,
        )
        object.__setattr__(
            self,
            "normalization_algorithm_version",
            normalization_algorithm_version,
        )
        object.__setattr__(self, "_active_raw_json", active_raw_json)
        object.__setattr__(self, "active_raw_fingerprint", frozen.fingerprint)
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    @property
    def normalized_active_raw(self) -> dict[str, Any]:
        active_raw = json.loads(self._active_raw_json)
        assert isinstance(active_raw, dict)
        return active_raw

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_normalized_active_raw_identity",
            "normalization_domain": self.normalization_domain,
            "normalization_algorithm_id": self.normalization_algorithm_id,
            "normalization_algorithm_version": (self.normalization_algorithm_version),
            "normalized_active_raw": self.normalized_active_raw,
            "active_raw_fingerprint": self.active_raw_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "NormalizedActiveRawIdentity":
        value = _strict_serialized_object(
            raw,
            kind="jts_normalized_active_raw_identity",
            fields=frozenset(
                {
                    "normalization_domain",
                    "normalization_algorithm_id",
                    "normalization_algorithm_version",
                    "normalized_active_raw",
                    "active_raw_fingerprint",
                }
            ),
        )
        active_raw = value["normalized_active_raw"]
        if not isinstance(active_raw, Mapping):
            raise EvidenceIdentityError("normalized active_raw must be an object")
        identity = cls(
            active_raw,
            normalization_domain=_raw(value, "normalization_domain"),
            normalization_algorithm_id=_raw(
                value,
                "normalization_algorithm_id",
            ),
            normalization_algorithm_version=_raw(
                value,
                "normalization_algorithm_version",
            ),
        )
        if value["active_raw_fingerprint"] != identity.active_raw_fingerprint:
            raise EvidenceIdentityError(
                "declared active_raw fingerprint does not match normalized content"
            )
        _declared_fingerprint(value, identity.fingerprint)
        return identity


@dataclass(frozen=True)
class CaptureIdentity:
    """Raw capture plus exact replay input and admitted evidence identities."""

    consumer_id: str
    measurement_kind: str
    capture_id: str
    raw_artifact: ArtifactIdentity
    analysis_input_artifact: ArtifactIdentity
    target_fingerprint: str
    context_fingerprint: str
    geometry_id: str
    placement_fingerprint: str
    quality_artifact: ArtifactIdentity
    admission_artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("consumer_id", "measurement_kind", "capture_id"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if not isinstance(self.raw_artifact, ArtifactIdentity):
            raise EvidenceIdentityError("raw_artifact must be an ArtifactIdentity")
        if not isinstance(self.analysis_input_artifact, ArtifactIdentity):
            raise EvidenceIdentityError(
                "analysis_input_artifact must be an ArtifactIdentity"
            )
        object.__setattr__(
            self, "geometry_id", _text(self.geometry_id, field_name="geometry_id")
        )
        object.__setattr__(
            self,
            "placement_fingerprint",
            _sha256(self.placement_fingerprint, field_name="placement_fingerprint"),
        )
        if not isinstance(self.quality_artifact, ArtifactIdentity):
            raise EvidenceIdentityError("quality_artifact must be an ArtifactIdentity")
        if not isinstance(self.admission_artifact, ArtifactIdentity):
            raise EvidenceIdentityError(
                "admission_artifact must be an ArtifactIdentity"
            )
        artifacts = (
            self.raw_artifact,
            self.analysis_input_artifact,
            self.quality_artifact,
            self.admission_artifact,
        )
        bundle_keys = {(item.bundle_kind, item.bundle_id) for item in artifacts}
        if len(bundle_keys) != 1:
            raise EvidenceIdentityError(
                "capture evidence artifacts must belong to one bundle"
            )
        artifact_fingerprints = {item.fingerprint for item in artifacts}
        artifact_paths = {item.relative_path for item in artifacts}
        if len(artifact_fingerprints) != len(artifacts) or len(artifact_paths) != len(
            artifacts
        ):
            raise EvidenceIdentityError(
                "raw, analysis-input, quality, and admission roles require distinct artifacts"
            )
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, field_name="target_fingerprint"),
        )
        object.__setattr__(
            self,
            "context_fingerprint",
            _sha256(self.context_fingerprint, field_name="context_fingerprint"),
        )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_measurement_capture_identity",
            "consumer_id": self.consumer_id,
            "measurement_kind": self.measurement_kind,
            "capture_id": self.capture_id,
            "raw_artifact": self.raw_artifact.to_dict(),
            "analysis_input_artifact": self.analysis_input_artifact.to_dict(),
            "target_fingerprint": self.target_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "geometry_id": self.geometry_id,
            "placement_fingerprint": self.placement_fingerprint,
            "quality_artifact": self.quality_artifact.to_dict(),
            "admission_artifact": self.admission_artifact.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "CaptureIdentity":
        value = _strict_serialized_object(
            raw,
            kind="jts_measurement_capture_identity",
            fields=frozenset(
                {
                    "consumer_id",
                    "measurement_kind",
                    "capture_id",
                    "raw_artifact",
                    "analysis_input_artifact",
                    "target_fingerprint",
                    "context_fingerprint",
                    "geometry_id",
                    "placement_fingerprint",
                    "quality_artifact",
                    "admission_artifact",
                }
            ),
        )
        identity = cls(
            consumer_id=_raw(value, "consumer_id"),
            measurement_kind=_raw(value, "measurement_kind"),
            capture_id=_raw(value, "capture_id"),
            raw_artifact=ArtifactIdentity.from_mapping(value["raw_artifact"]),
            analysis_input_artifact=ArtifactIdentity.from_mapping(
                value["analysis_input_artifact"]
            ),
            target_fingerprint=_raw(value, "target_fingerprint"),
            context_fingerprint=_raw(value, "context_fingerprint"),
            geometry_id=_raw(value, "geometry_id"),
            placement_fingerprint=_raw(value, "placement_fingerprint"),
            quality_artifact=ArtifactIdentity.from_mapping(value["quality_artifact"]),
            admission_artifact=ArtifactIdentity.from_mapping(
                value["admission_artifact"]
            ),
        )
        _declared_fingerprint(value, identity.fingerprint)
        return identity


@dataclass(frozen=True)
class ReplayIdentity:
    """Ordered exact capture set and algorithm identity for one replay."""

    consumer_id: str
    replay_kind: str
    algorithm_id: str
    algorithm_version: str
    captures: tuple[CaptureIdentity, ...]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "consumer_id",
            "replay_kind",
            "algorithm_id",
            "algorithm_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        if type(self.captures) is not tuple or not self.captures:
            raise EvidenceIdentityError("captures must be a non-empty tuple")
        if any(not isinstance(capture, CaptureIdentity) for capture in self.captures):
            raise EvidenceIdentityError("captures must contain CaptureIdentity values")
        fingerprints = [capture.fingerprint for capture in self.captures]
        if len(set(fingerprints)) != len(fingerprints):
            raise EvidenceIdentityError("replay captures must be unique")
        capture_ids = [capture.capture_id for capture in self.captures]
        raw_fingerprints = [
            capture.raw_artifact.fingerprint for capture in self.captures
        ]
        raw_content_hashes = [capture.raw_artifact.sha256 for capture in self.captures]
        if len(set(capture_ids)) != len(capture_ids):
            raise EvidenceIdentityError("replay capture ids must be unique")
        if len(set(raw_fingerprints)) != len(raw_fingerprints):
            raise EvidenceIdentityError("replay raw artifacts must be unique")
        if len(set(raw_content_hashes)) != len(raw_content_hashes):
            raise EvidenceIdentityError("replay raw capture content must be unique")
        if any(capture.consumer_id != self.consumer_id for capture in self.captures):
            raise EvidenceIdentityError(
                "replay captures must belong to the replay consumer"
            )
        bundle_keys = {
            (capture.raw_artifact.bundle_kind, capture.raw_artifact.bundle_id)
            for capture in self.captures
        }
        if len(bundle_keys) != 1:
            raise EvidenceIdentityError(
                "replay captures must belong to one commissioning session bundle"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_measurement_replay_identity",
            "consumer_id": self.consumer_id,
            "replay_kind": self.replay_kind,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "captures": [capture.to_dict() for capture in self.captures],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> "ReplayIdentity":
        value = _strict_serialized_object(
            raw,
            kind="jts_measurement_replay_identity",
            fields=frozenset(
                {
                    "consumer_id",
                    "replay_kind",
                    "algorithm_id",
                    "algorithm_version",
                    "captures",
                }
            ),
        )
        raw_captures = value["captures"]
        if type(raw_captures) is not list:
            raise EvidenceIdentityError("replay captures must be a list")
        identity = cls(
            consumer_id=_raw(value, "consumer_id"),
            replay_kind=_raw(value, "replay_kind"),
            algorithm_id=_raw(value, "algorithm_id"),
            algorithm_version=_raw(value, "algorithm_version"),
            captures=tuple(CaptureIdentity.from_mapping(item) for item in raw_captures),
        )
        _declared_fingerprint(value, identity.fingerprint)
        return identity
