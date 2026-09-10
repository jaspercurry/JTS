# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Generated excitation WAV identities for stored commissioning evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    FingerprintedRecord,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_artifacts import GenerationAdmissionArtifact

GENERATED_EXCITATION_WAV_SCHEMA_VERSION = 1
_GENERATED_EXCITATION_WAV_KIND = "jts_generated_excitation_wav"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class GeneratedExcitationWav(FingerprintedRecord):
    """Exact persisted WAV identity bound to one generation and plan.

    The feature-owned deterministic generator issues this value and persists
    ``to_dict()`` with its own manifest/state. Shared verifies the exact artifact
    bytes and generation binding; it does not infer an opaque feature plan from
    PCM samples.
    """

    generation_artifact_fingerprint: str
    excitation_plan_fingerprint: str
    artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        generation = _sha256(
            self.generation_artifact_fingerprint,
            field_name="generation_artifact_fingerprint",
        )
        plan = _sha256(
            self.excitation_plan_fingerprint,
            field_name="excitation_plan_fingerprint",
        )
        if not isinstance(self.artifact, ArtifactIdentity):
            raise ValueError("artifact must be an ArtifactIdentity")
        object.__setattr__(self, "generation_artifact_fingerprint", generation)
        object.__setattr__(self, "excitation_plan_fingerprint", plan)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, object]:
        return {
            "schema_version": GENERATED_EXCITATION_WAV_SCHEMA_VERSION,
            "kind": _GENERATED_EXCITATION_WAV_KIND,
            "generation_artifact_fingerprint": (self.generation_artifact_fingerprint),
            "excitation_plan_fingerprint": self.excitation_plan_fingerprint,
            "artifact": self.artifact.to_dict(),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> GeneratedExcitationWav:
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "kind",
            "generation_artifact_fingerprint",
            "excitation_plan_fingerprint",
            "artifact",
            "fingerprint",
        }:
            raise ValueError("generated excitation WAV fields are invalid")
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != GENERATED_EXCITATION_WAV_SCHEMA_VERSION
        ):
            raise ValueError("generated excitation WAV schema is unsupported")
        if raw["kind"] != _GENERATED_EXCITATION_WAV_KIND:
            raise ValueError("generated excitation WAV kind is unsupported")
        result = cls(
            generation_artifact_fingerprint=raw["generation_artifact_fingerprint"],
            excitation_plan_fingerprint=raw["excitation_plan_fingerprint"],
            artifact=ArtifactIdentity.from_mapping(raw["artifact"]),
        )
        if raw["fingerprint"] != result.fingerprint:
            raise ValueError("generated excitation WAV fingerprint is invalid")
        return result


def bind_generated_excitation_wav(
    generation: GenerationAdmissionArtifact,
    artifact: ArtifactIdentity,
) -> GeneratedExcitationWav:
    """Bind a feature-generated WAV artifact to one exact generation decision."""

    if not isinstance(generation, GenerationAdmissionArtifact):
        raise ValueError("generation must be a GenerationAdmissionArtifact")
    plan = generation.admission.request.excitation_plan_fingerprint
    if plan is None:  # An allowed generation cannot reach this branch.
        raise ValueError("generation admission has no excitation plan identity")
    return GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.artifact.fingerprint,
        excitation_plan_fingerprint=plan,
        artifact=artifact,
    )
