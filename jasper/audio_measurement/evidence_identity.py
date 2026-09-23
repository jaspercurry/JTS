# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict pure identities for measurement artifacts and DSP graphs.

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

from jasper.audio_measurement.fingerprinted_record import FingerprintedRecord
from jasper.audio_measurement.null_walk import DspPredecessor, NullWalkError

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
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


@dataclass(frozen=True)
class ArtifactIdentity(FingerprintedRecord):
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


@dataclass(frozen=True, init=False)
class NormalizedActiveRawIdentity(FingerprintedRecord):
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
