# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from jasper.active_speaker import bundles as active_bundles
from jasper.audio_measurement import bundles as shared_bundles
from jasper.correction import bundles as correction_bundles


def _write_info(writer, bundle_dir: Path) -> None:
    writer(
        bundle_dir,
        "info.json",
        {"bundle_schema_version": 5, "session_id": "same"},
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="test",
        schema_version=5,
    )


def test_correction_compatibility_writer_preserves_bundle_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Room shim must retain the shipped JSON and manifest encoding."""

    monkeypatch.setattr(shared_bundles.time, "time", lambda: 123.25)
    through_room = tmp_path / "room"
    through_shared = tmp_path / "shared"

    _write_info(correction_bundles.write_json_artifact, through_room)
    _write_info(shared_bundles.write_json_artifact, through_shared)

    assert (through_room / "info.json").read_bytes() == (
        through_shared / "info.json"
    ).read_bytes()
    assert (through_room / shared_bundles.ARTIFACT_MANIFEST_NAME).read_bytes() == (
        through_shared / shared_bundles.ARTIFACT_MANIFEST_NAME
    ).read_bytes()
    assert (
        through_room / shared_bundles.ARTIFACT_MANIFEST_NAME
    ).read_text() == """{
  "manifest_schema_version": 1,
  "bundle_schema_version": 5,
  "generated_at": 123.25,
  "artifacts": [
    {
      "path": "info.json",
      "kind": "session_metadata",
      "sensitivity": "private_metadata",
      "recomputable": false,
      "sha256": "da7836507861faa955b9b06d141210f9050b5656fc35a3a9c8f5ebb894c7c8a6",
      "byte_size": 56,
      "recorded_at": 123.25,
      "generated_by": "test",
      "dependencies": [],
      "schema_version": 5
    }
  ]
}"""


def test_correction_compatibility_imports_reexport_shared_contract() -> None:
    assert correction_bundles.BundleError is shared_bundles.BundleError
    assert correction_bundles.ArtifactEntry is shared_bundles.ArtifactEntry
    assert (
        correction_bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
        == shared_bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
        == 1
    )
    assert correction_bundles.read_artifact_manifest is (
        shared_bundles.read_artifact_manifest
    )


def test_active_speaker_uses_neutral_manifest_primitives() -> None:
    assert active_bundles.BundleError is shared_bundles.BundleError
    assert active_bundles.record_artifact is shared_bundles.record_artifact
    assert active_bundles.write_json_artifact is shared_bundles.write_json_artifact


def test_neutral_writer_requires_feature_owned_schema_without_info(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "capture.wav"
    artifact.write_bytes(b"capture")

    with pytest.raises(
        shared_bundles.BundleError,
        match="bundle schema version is required",
    ):
        shared_bundles.record_artifact(
            tmp_path,
            artifact,
            kind="capture",
            sensitivity="private_raw_audio",
            recomputable=False,
            generated_by="test",
        )


def test_neutral_public_writers_do_not_expose_legacy_schema_defaults() -> None:
    for writer in (
        shared_bundles.record_artifact,
        shared_bundles.write_json_artifact,
    ):
        assert "default_bundle_schema_version" not in inspect.signature(
            writer
        ).parameters
