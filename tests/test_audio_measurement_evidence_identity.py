# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
    EvidenceIdentityError,
    ExactDspStateIdentity,
    NormalizedActiveRawIdentity,
    ReplayIdentity,
    json_fingerprint,
)


def _hash(char: str) -> str:
    return char * 64


def _artifact(path: str, char: str, *, bundle: str = "session-1") -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning_bundle",
        bundle_id=bundle,
        relative_path=path,
        sha256=_hash(char),
        byte_size=2048,
    )


def _capture(index: int = 1, *, bundle: str = "session-1") -> CaptureIdentity:
    return CaptureIdentity(
        consumer_id="active_crossover",
        measurement_kind="active_crossover_post_apply",
        capture_id=f"capture-{index}",
        raw_artifact=_artifact(f"repeat/{index}.wav", str(index), bundle=bundle),
        analysis_input_artifact=_artifact(
            f"repeat/{index}_input.json", "d", bundle=bundle
        ),
        target_fingerprint=_hash("e"),
        context_fingerprint=_hash("f"),
        geometry_id="reference_axis",
        placement_fingerprint=_hash("a"),
        quality_artifact=_artifact(f"repeat/{index}_quality.json", "b", bundle=bundle),
        admission_artifact=_artifact(
            f"repeat/{index}_admission.json", "c", bundle=bundle
        ),
    )


def _replay() -> ReplayIdentity:
    return ReplayIdentity(
        consumer_id="active_crossover",
        replay_kind="driver_response_splice",
        algorithm_id="driver_response_replay",
        algorithm_version="1",
        captures=(_capture(1), _capture(2)),
    )


def test_strict_evidence_authorities_round_trip_with_stable_fingerprints():
    replay = _replay()
    capture = replay.captures[0]

    assert ArtifactIdentity.from_mapping(capture.raw_artifact.to_dict()) == (
        capture.raw_artifact
    )
    assert CaptureIdentity.from_mapping(capture.to_dict()) == capture
    assert ReplayIdentity.from_mapping(replay.to_dict()) == replay


@pytest.mark.parametrize(
    ("payload", "parser"),
    [
        (lambda: _capture().raw_artifact.to_dict(), ArtifactIdentity.from_mapping),
        (lambda: _capture().to_dict(), CaptureIdentity.from_mapping),
        (lambda: _replay().to_dict(), ReplayIdentity.from_mapping),
    ],
)
def test_every_serialized_identity_rejects_unknown_fields_and_bool_schema(
    payload,
    parser,
):
    unknown = payload()
    unknown["future_guess"] = True
    with pytest.raises(EvidenceIdentityError, match="unknown or missing fields"):
        parser(unknown)

    boolean_schema = payload()
    boolean_schema["schema_version"] = True
    with pytest.raises(EvidenceIdentityError, match="unsupported"):
        parser(boolean_schema)


def test_capture_binds_raw_analysis_input_placement_quality_and_admission():
    capture = _capture()
    assert capture.raw_artifact.relative_path.endswith(".wav")
    assert capture.analysis_input_artifact.relative_path.endswith("_input.json")
    assert capture.geometry_id == "reference_axis"
    assert capture.quality_artifact.relative_path.endswith("_quality.json")
    assert capture.admission_artifact.relative_path.endswith("_admission.json")

    with pytest.raises(EvidenceIdentityError, match="one bundle"):
        CaptureIdentity(
            consumer_id=capture.consumer_id,
            measurement_kind=capture.measurement_kind,
            capture_id=capture.capture_id,
            raw_artifact=capture.raw_artifact,
            analysis_input_artifact=_artifact(
                "foreign/input.json", "9", bundle="another-session"
            ),
            target_fingerprint=capture.target_fingerprint,
            context_fingerprint=capture.context_fingerprint,
            geometry_id=capture.geometry_id,
            placement_fingerprint=capture.placement_fingerprint,
            quality_artifact=capture.quality_artifact,
            admission_artifact=capture.admission_artifact,
        )

    with pytest.raises(EvidenceIdentityError, match="distinct artifacts"):
        replace(capture, admission_artifact=capture.quality_artifact)


def test_nested_artifact_tamper_invalidates_capture_and_replay():
    payload = copy.deepcopy(_replay().to_dict())
    payload["captures"][0]["analysis_input_artifact"]["byte_size"] += 1

    with pytest.raises(EvidenceIdentityError, match="declared fingerprint"):
        ReplayIdentity.from_mapping(payload)


def test_replay_order_is_authoritative_and_captures_must_be_unique():
    replay = _replay()
    reordered = ReplayIdentity(
        consumer_id=replay.consumer_id,
        replay_kind=replay.replay_kind,
        algorithm_id=replay.algorithm_id,
        algorithm_version=replay.algorithm_version,
        captures=tuple(reversed(replay.captures)),
    )
    assert reordered.fingerprint != replay.fingerprint
    with pytest.raises(EvidenceIdentityError, match="unique"):
        ReplayIdentity(
            consumer_id=replay.consumer_id,
            replay_kind=replay.replay_kind,
            algorithm_id=replay.algorithm_id,
            algorithm_version=replay.algorithm_version,
            captures=(replay.captures[0], replay.captures[0]),
        )

    with pytest.raises(EvidenceIdentityError, match="capture ids"):
        ReplayIdentity(
            consumer_id=replay.consumer_id,
            replay_kind=replay.replay_kind,
            algorithm_id=replay.algorithm_id,
            algorithm_version=replay.algorithm_version,
            captures=(
                replay.captures[0],
                replace(replay.captures[1], capture_id="capture-1"),
            ),
        )
    with pytest.raises(EvidenceIdentityError, match="raw artifacts"):
        ReplayIdentity(
            consumer_id=replay.consumer_id,
            replay_kind=replay.replay_kind,
            algorithm_id=replay.algorithm_id,
            algorithm_version=replay.algorithm_version,
            captures=(
                replay.captures[0],
                replace(
                    replay.captures[1],
                    raw_artifact=replay.captures[0].raw_artifact,
                ),
            ),
        )
    with pytest.raises(EvidenceIdentityError, match="one commissioning session"):
        ReplayIdentity(
            consumer_id=replay.consumer_id,
            replay_kind=replay.replay_kind,
            algorithm_id=replay.algorithm_id,
            algorithm_version=replay.algorithm_version,
            captures=(replay.captures[0], _capture(2, bundle="session-2")),
        )


def test_exact_state_and_normalized_active_raw_are_typed_content_identities():
    active_raw = {
        "devices": {"volume_limit": -12.0},
        "pipeline": [{"type": "Filter", "channels": [0]}],
    }
    exact = ExactDspStateIdentity(
        {
            "config_path": "/etc/camilladsp/active.yml",
            "active_raw": active_raw,
        }
    )
    normalized = NormalizedActiveRawIdentity(active_raw)

    assert ExactDspStateIdentity.from_mapping(exact.to_dict()) == exact
    assert NormalizedActiveRawIdentity.from_mapping(normalized.to_dict()) == normalized
    assert exact.fingerprint != normalized.fingerprint
    assert normalized.normalization_domain == "camilladsp_active_raw"
    assert normalized.normalization_algorithm_version == "1"


def test_graph_identity_rejects_wrong_domain_algorithm_and_content_tamper():
    active_raw = {"devices": {"volume_limit": -12.0}}
    with pytest.raises(EvidenceIdentityError, match="normalization domain"):
        NormalizedActiveRawIdentity(active_raw, normalization_domain="yaml_file")
    with pytest.raises(EvidenceIdentityError, match="normalization algorithm"):
        NormalizedActiveRawIdentity(
            active_raw,
            normalization_algorithm_id="unspecified",
        )

    payload = NormalizedActiveRawIdentity(active_raw).to_dict()
    payload["normalized_active_raw"]["devices"]["volume_limit"] = -6.0
    with pytest.raises(EvidenceIdentityError, match="active_raw fingerprint"):
        NormalizedActiveRawIdentity.from_mapping(payload)


@pytest.mark.parametrize(
    "relative_path",
    ["/tmp/capture.wav", "../capture.wav", "captures/../capture.wav", "a\\b.wav"],
)
def test_artifact_identity_refuses_non_bundle_paths(relative_path: str):
    with pytest.raises(EvidenceIdentityError, match="bundle-relative POSIX"):
        _artifact(relative_path, "a")


def test_canonical_json_refuses_lossy_or_non_json_input():
    with pytest.raises(EvidenceIdentityError, match="non-JSON"):
        json_fingerprint({"analysis_input": (1, 2)})
    with pytest.raises(EvidenceIdentityError, match="non-string key"):
        json_fingerprint({"analysis_input": {1: "ambiguous"}})
    with pytest.raises(EvidenceIdentityError, match="non-finite"):
        json_fingerprint({"analysis_input": float("nan")})
