# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from jasper.active_speaker import bundles as active_bundles
from jasper.audio_measurement import bundles as shared_bundles


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


def test_active_speaker_uses_neutral_manifest_primitives() -> None:
    """Both halves of the contract: the writer AND the reader primitives.

    The reader half used to be pinned on ``active_speaker.legacy_replay``;
    that module was a dead island and was deleted, so the assertions moved
    onto ``active_speaker.bundles`` — the live active-speaker manifest
    module, which re-uses the same neutral reader primitives rather than
    hand-rolling a second copy. ``relative_artifact_path`` is not among the
    names this module re-exports; its guard is
    ``test_neutral_relative_artifact_path_is_the_public_reader_guard`` below.
    """

    assert active_bundles.BundleError is shared_bundles.BundleError
    assert active_bundles.record_artifact is shared_bundles.record_artifact
    assert active_bundles.write_json_artifact is shared_bundles.write_json_artifact
    assert active_bundles.read_artifact_manifest is (
        shared_bundles.read_artifact_manifest
    )
    assert active_bundles.sha256_file is shared_bundles.sha256_file


def test_neutral_relative_artifact_path_is_the_public_reader_guard(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    assert (
        shared_bundles.relative_artifact_path(bundle_dir, "captures/driver.wav")
        == "captures/driver.wav"
    )
    with pytest.raises(shared_bundles.BundleError, match="outside bundle"):
        shared_bundles.relative_artifact_path(bundle_dir, "../outside.wav")
    with pytest.raises(shared_bundles.BundleError, match="cannot list itself"):
        shared_bundles.relative_artifact_path(
            bundle_dir,
            shared_bundles.ARTIFACT_MANIFEST_NAME,
        )


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
            kind="jts_test_capture",
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
