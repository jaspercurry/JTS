# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    EvidenceIdentityError,
    NormalizedActiveRawIdentity,
    json_fingerprint,
)


def _hash(char: str) -> str:
    return char * 64


def _artifact(path: str, char: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning_bundle",
        bundle_id="session-1",
        relative_path=path,
        sha256=_hash(char),
        byte_size=2048,
    )


def test_normalized_active_raw_is_a_typed_content_identity():
    active_raw = {
        "devices": {"volume_limit": -12.0},
        "pipeline": [{"type": "Filter", "channels": [0]}],
    }
    normalized = NormalizedActiveRawIdentity(active_raw)

    assert normalized.normalization_domain == "camilladsp_active_raw"
    assert normalized.normalization_algorithm_version == "1"


def test_graph_identity_rejects_wrong_domain_and_algorithm():
    active_raw = {"devices": {"volume_limit": -12.0}}
    with pytest.raises(EvidenceIdentityError, match="normalization domain"):
        NormalizedActiveRawIdentity(active_raw, normalization_domain="yaml_file")
    with pytest.raises(EvidenceIdentityError, match="normalization algorithm"):
        NormalizedActiveRawIdentity(
            active_raw,
            normalization_algorithm_id="unspecified",
        )


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
