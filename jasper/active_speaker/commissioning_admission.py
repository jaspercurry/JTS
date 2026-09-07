# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stored capture admission and shared CamillaDSP graph identities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.evidence_identity import ArtifactIdentity, json_fingerprint
from jasper.audio_measurement.excitation_artifacts import (
    read_generation_admission,
    read_playback_admission,
)
from jasper.output_topology import OutputTopology

from .bundles import open_bundle_admission_authority, sessions_dir
from .capture_geometry import comparison_set_valid
from .measurement import active_driver_targets

ADMISSION_HANDOFF_SCHEMA_VERSION = 2
ADMISSION_HANDOFF_KIND = "jts_active_driver_capture_admission_handoff"
ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS = -12.0


class ActiveCommissioningAdmissionError(RuntimeError):
    """A capture or running graph cannot provide a usable identity."""

    def __init__(
        self, *args: Any, refusal_codes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(*args)
        self.refusal_codes = refusal_codes


@dataclass(frozen=True, slots=True)
class ActiveCaptureAdmissionHandoff:
    """Strict server-owned join from admitted playback to captured evidence."""

    session_id: str
    comparison_set_id: str
    comparison_set_fingerprint: str
    admission_id: str
    target_id: str
    target_fingerprint: str
    authority_fingerprint: str
    generation_artifact: ArtifactIdentity
    playback_artifact: ArtifactIdentity
    stimulus: GeneratedExcitationWav
    admission: Mapping[str, Any]
    graph_fingerprint: str
    graph_evidence_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "comparison_set_id",
            "admission_id",
            "target_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty trimmed text")
        for name in (
            "comparison_set_fingerprint",
            "target_fingerprint",
            "authority_fingerprint",
            "graph_fingerprint",
            "graph_evidence_fingerprint",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if not isinstance(self.generation_artifact, ArtifactIdentity):
            raise ValueError("generation_artifact must be an ArtifactIdentity")
        if not isinstance(self.playback_artifact, ArtifactIdentity):
            raise ValueError("playback_artifact must be an ArtifactIdentity")
        if not isinstance(self.stimulus, GeneratedExcitationWav):
            raise ValueError("stimulus must be a GeneratedExcitationWav")
        if not isinstance(self.admission, Mapping):
            raise ValueError("admission must be a mapping")
        object.__setattr__(self, "admission", dict(self.admission))
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": ADMISSION_HANDOFF_SCHEMA_VERSION,
            "kind": ADMISSION_HANDOFF_KIND,
            "session_id": self.session_id,
            "comparison_set_id": self.comparison_set_id,
            "comparison_set_fingerprint": self.comparison_set_fingerprint,
            "admission_id": self.admission_id,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "authority_fingerprint": self.authority_fingerprint,
            "generation_artifact": self.generation_artifact.to_dict(),
            "playback_artifact": self.playback_artifact.to_dict(),
            "stimulus": self.stimulus.to_dict(),
            "admission": dict(self.admission),
            "graph_fingerprint": self.graph_fingerprint,
            "graph_evidence_fingerprint": self.graph_evidence_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: object) -> "ActiveCaptureAdmissionHandoff":
        if not isinstance(raw, Mapping):
            raise ValueError("capture admission handoff must be an object")
        expected = {
            "schema_version",
            "kind",
            "session_id",
            "comparison_set_id",
            "comparison_set_fingerprint",
            "admission_id",
            "target_id",
            "target_fingerprint",
            "authority_fingerprint",
            "generation_artifact",
            "playback_artifact",
            "stimulus",
            "admission",
            "graph_fingerprint",
            "graph_evidence_fingerprint",
            "fingerprint",
        }
        if set(raw) != expected:
            raise ValueError("capture admission handoff fields are invalid")
        if (
            raw["schema_version"] != ADMISSION_HANDOFF_SCHEMA_VERSION
            or raw["kind"] != ADMISSION_HANDOFF_KIND
        ):
            raise ValueError("capture admission handoff schema is unsupported")
        result = cls(
            session_id=raw["session_id"],
            comparison_set_id=raw["comparison_set_id"],
            comparison_set_fingerprint=raw["comparison_set_fingerprint"],
            admission_id=raw["admission_id"],
            target_id=raw["target_id"],
            target_fingerprint=raw["target_fingerprint"],
            authority_fingerprint=raw["authority_fingerprint"],
            generation_artifact=ArtifactIdentity.from_mapping(
                raw["generation_artifact"]
            ),
            playback_artifact=ArtifactIdentity.from_mapping(raw["playback_artifact"]),
            stimulus=GeneratedExcitationWav.from_mapping(raw["stimulus"]),
            admission=raw["admission"],
            graph_fingerprint=raw["graph_fingerprint"],
            graph_evidence_fingerprint=raw["graph_evidence_fingerprint"],
        )
        if raw["fingerprint"] != result.fingerprint:
            raise ValueError("capture admission handoff fingerprint is invalid")
        return result


def validate_capture_admission_handoff(
    raw: object,
    *,
    topology: OutputTopology,
    comparison_set: Mapping[str, Any],
    speaker_group_id: str,
    role: str,
) -> dict[str, Any]:
    """Re-verify a server-owned playback handoff before capture persistence."""

    handoff = ActiveCaptureAdmissionHandoff.from_mapping(raw)
    target = _target(topology, speaker_group_id=speaker_group_id, role=role)
    expected_session = comparison_set.get("bundle_session_id")
    if (
        not comparison_set_valid(comparison_set)
        or handoff.session_id != expected_session
        or handoff.comparison_set_id != comparison_set.get("comparison_set_id")
        or handoff.comparison_set_fingerprint != comparison_set.get("fingerprint")
        or handoff.target_id != target.get("target_id")
        or handoff.target_fingerprint != target.get("target_fingerprint")
    ):
        raise ActiveCommissioningAdmissionError(
            "capture admission handoff does not match the current target context"
        )
    authority = open_bundle_admission_authority(
        sessions_dir() / handoff.session_id,
        expected_session_id=handoff.session_id,
    )
    if authority.fingerprint != handoff.authority_fingerprint:
        raise ActiveCommissioningAdmissionError(
            "capture admission authority changed before persistence"
        )
    generation = read_generation_admission(authority, handoff.generation_artifact)
    playback = read_playback_admission(
        authority,
        generation,
        handoff.playback_artifact,
    )
    if (
        generation.admission_id != handoff.admission_id
        or playback.admission.to_dict() != dict(handoff.admission)
        or playback.admission.protection_evidence is None
        or playback.admission.protection_evidence.evidence_fingerprint
        != handoff.graph_evidence_fingerprint
        or handoff.stimulus.generation_artifact_fingerprint
        != generation.artifact.fingerprint
        or handoff.stimulus.artifact.bundle_kind != authority.bundle_kind
        or handoff.stimulus.artifact.bundle_id != authority.bundle_id
    ):
        raise ActiveCommissioningAdmissionError(
            "capture admission handoff artifacts are inconsistent"
        )
    stimulus_path = authority.directory.joinpath(
        *handoff.stimulus.artifact.relative_path.split("/")
    )
    if stimulus_path.is_symlink() or stimulus_path.parent.is_symlink():
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus path is not an immutable file"
        )
    try:
        stat = stimulus_path.stat()
        digest = hashlib.sha256()
        with stimulus_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus is no longer readable"
        ) from exc
    if (
        not stimulus_path.is_file()
        or stat.st_size != handoff.stimulus.artifact.byte_size
        or digest.hexdigest() != handoff.stimulus.artifact.sha256
    ):
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus identity changed before persistence"
        )
    return handoff.to_dict()


def _target(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
) -> Mapping[str, Any]:
    matches = [
        target
        for target in active_driver_targets(topology)
        if target.get("speaker_group_id") == speaker_group_id
        and target.get("role") == role
    ]
    if len(matches) != 1:
        raise ActiveCommissioningAdmissionError(
            "the requested driver target is not current"
        )
    return matches[0]


def parse_running_graph(running_config_raw: str | None) -> dict[str, Any]:
    """One parseable CamillaDSP graph as an object, unrepaired.

    Shared with callers that hash a SUBSET of the graph (see
    :func:`~jasper.active_speaker.crossover_v2.tuning_scope.tuning_scope_fingerprint`)
    so both refuse an unparseable readback identically.
    """

    try:
        parsed = yaml.safe_load(running_config_raw or "")
    except yaml.YAMLError as exc:
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not parseable"
        ) from exc
    if not isinstance(parsed, dict):
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not an object"
        )
    return parsed


def running_graph_fingerprint(running_config_raw: str | None) -> str:
    """Fingerprint one parseable fresh CamillaDSP readback without repairing it."""

    return json_fingerprint(parse_running_graph(running_config_raw))
