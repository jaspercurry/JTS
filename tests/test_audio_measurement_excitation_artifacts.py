# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest

from jasper.audio_measurement.excitation_artifacts import (
    ADMISSION_AUTHORITY_MARKER,
    MAX_ADMISSION_ARTIFACT_BYTES,
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    create_admission_authority,
    open_admission_authority,
)

BUNDLE_KIND = "jts_active_speaker_commissioning_authority"
BUNDLE_ID = "authority-session-1"


def _authority(tmp_path: Path, *, bundle_id: str = BUNDLE_ID):
    return create_admission_authority(
        tmp_path / bundle_id,
        bundle_kind=BUNDLE_KIND,
        bundle_id=bundle_id,
    )


def test_authority_is_new_exclusive_canonical_and_private(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    marker_path = authority.directory / ADMISSION_AUTHORITY_MARKER

    assert authority.directory.stat().st_mode & 0o7777 == 0o750
    assert marker_path.stat().st_mode & 0o777 == 0o640
    assert marker_path.read_bytes() == canonical_marker_bytes(authority)
    assert (
        open_admission_authority(
            authority.directory,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )
        == authority
    )

    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_ALREADY_EXISTS


def test_authority_requires_a_feature_owned_existing_parent(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing"

    with pytest.raises(AdmissionArtifactError) as caught:
        create_admission_authority(
            missing_parent / BUNDLE_ID,
            bundle_kind=BUNDLE_KIND,
            bundle_id=BUNDLE_ID,
        )

    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_PARENT_INVALID
    assert not missing_parent.exists()


def test_authority_directory_creation_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    def fail_mkdir(_path, _mode) -> None:
        raise PermissionError("authority parent is read-only")

    monkeypatch.setattr(excitation_artifacts.os, "mkdir", fail_mkdir)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED


def test_authority_creation_never_requests_a_setgid_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    real_mkdir = excitation_artifacts.os.mkdir
    real_chmod = excitation_artifacts.os.chmod
    requested_modes: list[int] = []

    def record_mkdir(path: Path, mode: int) -> None:
        requested_modes.append(mode)
        real_mkdir(path, mode)

    def record_chmod(path: Path, mode: int) -> None:
        requested_modes.append(mode)
        real_chmod(path, mode)

    monkeypatch.setattr(excitation_artifacts.os, "mkdir", record_mkdir)
    monkeypatch.setattr(excitation_artifacts.os, "chmod", record_chmod)

    _authority(tmp_path)

    assert requested_modes
    assert not any(mode & stat.S_ISGID for mode in requested_modes)


def test_authority_directory_mode_under_strict_umask_has_no_setgid(
    tmp_path: Path,
) -> None:
    previous = os.umask(0o077)
    try:
        authority = _authority(tmp_path)
    finally:
        os.umask(previous)

    assert authority.directory.stat().st_mode & 0o7777 == 0o750


def test_authority_directory_chmod_is_skipped_when_mkdir_already_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    real_chmod = excitation_artifacts.os.chmod
    chmod_calls: list[int] = []

    def record_chmod(path: Path, mode: int) -> None:
        chmod_calls.append(mode)
        real_chmod(path, mode)

    monkeypatch.setattr(excitation_artifacts.os, "chmod", record_chmod)
    previous = os.umask(0)
    try:
        _authority(tmp_path)
    finally:
        os.umask(previous)

    # mkdir alone already lands on ADMISSION_DIRECTORY_MODE, so there is
    # nothing to correct -- skipping the chmod here is what lets an inherited
    # setgid bit from a real setgid parent survive untouched.
    assert chmod_calls == []


def test_authority_has_stable_modes_under_strict_umask(
    tmp_path: Path,
) -> None:
    shared_gid = next((gid for gid in os.getgroups() if gid != os.getegid()), os.getegid())
    os.chown(tmp_path, -1, shared_gid)
    tmp_path.chmod(0o2750)
    previous = os.umask(0o077)
    try:
        authority = _authority(tmp_path)
    finally:
        os.umask(previous)

    artifact_path = authority.directory / ADMISSION_AUTHORITY_MARKER
    assert authority.directory.stat().st_mode & 0o7777 == 0o750
    assert authority.directory.stat().st_gid == shared_gid
    assert artifact_path.stat().st_gid == artifact_path.parent.stat().st_gid
    assert artifact_path.stat().st_mode & 0o777 == 0o640


def test_persistence_accepts_a_resolved_alias_in_an_authority_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    (real_parent / "authority-root").mkdir()
    authority = create_admission_authority(
        alias_parent / "authority-root" / BUNDLE_ID,
        bundle_kind=BUNDLE_KIND,
        bundle_id=BUNDLE_ID,
    )

    assert (authority.directory / ADMISSION_AUTHORITY_MARKER).is_file()


def test_new_authority_fsyncs_its_parent_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    synced: list[Path] = []
    real_fsync_directory = excitation_artifacts.fsync_directory

    def record_fsync(path: Path) -> None:
        synced.append(Path(path))
        real_fsync_directory(path)

    monkeypatch.setattr(
        excitation_artifacts,
        "fsync_directory",
        record_fsync,
    )
    authority = _authority(tmp_path)

    assert tmp_path in synced
    assert authority.directory in synced


def test_marker_prepublish_failure_durably_removes_empty_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    synced: list[Path] = []
    real_fsync_directory = excitation_artifacts.fsync_directory

    def record_fsync(path: Path) -> None:
        synced.append(Path(path))
        real_fsync_directory(path)

    def fail_link(_source, _target) -> None:
        raise OSError("marker publish failed")

    monkeypatch.setattr(
        excitation_artifacts,
        "fsync_directory",
        record_fsync,
    )
    monkeypatch.setattr(excitation_artifacts.os, "link", fail_link)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED
    assert not (tmp_path / BUNDLE_ID).exists()
    assert synced.count(tmp_path) >= 2


def test_marker_cleanup_sync_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    real_fsync_directory = excitation_artifacts.fsync_directory
    parent_syncs = 0

    def fail_cleanup_parent_sync(path: Path) -> None:
        nonlocal parent_syncs
        if Path(path) == tmp_path:
            parent_syncs += 1
            if parent_syncs == 2:
                raise OSError("cleanup directory sync failed")
        real_fsync_directory(path)

    def fail_link(_source, _target) -> None:
        raise OSError("marker publish failed")

    monkeypatch.setattr(
        excitation_artifacts,
        "fsync_directory",
        fail_cleanup_parent_sync,
    )
    monkeypatch.setattr(excitation_artifacts.os, "link", fail_link)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )


def canonical_marker_bytes(authority) -> bytes:
    payload = json.loads(
        (authority.directory / ADMISSION_AUTHORITY_MARKER).read_text(encoding="utf-8")
    )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: json.dumps(json.loads(raw), indent=2).encode("utf-8"),
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"schema_version":1', b'"schema_version":2'),
        lambda raw: raw.replace(
            b'"kind":"jts_excitation_admission_authority"',
            b'"kind":"wrong"',
        ),
        lambda raw: raw.replace(
            b'"admission_artifact_contract_version":1',
            b'"admission_artifact_contract_version":2',
        ),
        lambda raw: raw.replace(b'"fingerprint":"', b'"fingerprint":"f', 1),
        lambda raw: raw[:-1] + b',"unexpected":true}',
        lambda raw: raw[:-1] + b',"schema_version":1}',
    ),
)
def test_authority_marker_rejects_noncanonical_tampered_and_duplicate_fields(
    tmp_path: Path, mutate
) -> None:
    authority = _authority(tmp_path)
    marker = authority.directory / ADMISSION_AUTHORITY_MARKER
    marker.write_bytes(mutate(marker.read_bytes()))

    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            authority.directory,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )

    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_INVALID


def test_authority_rejects_marker_and_directory_symlinks(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    marker = authority.directory / ADMISSION_AUTHORITY_MARKER
    outside = tmp_path / "outside.json"
    outside.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(outside)

    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            authority.directory,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_INVALID

    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    alias = alias_parent / BUNDLE_ID
    alias.symlink_to(authority.directory, target_is_directory=True)
    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            alias,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_INVALID


def test_existing_legacy_directory_cannot_be_upgraded_or_backfilled(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / BUNDLE_ID
    legacy.mkdir()
    (legacy / "info.json").write_text(
        json.dumps({"kind": "jts_active_speaker_commissioning_bundle"}),
        encoding="utf-8",
    )

    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_ALREADY_EXISTS

    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            legacy,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_MISSING


def test_reader_rejects_oversize(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    path = authority.directory / ADMISSION_AUTHORITY_MARKER
    oversized = b"x" * (MAX_ADMISSION_ARTIFACT_BYTES + 1)
    path.write_bytes(oversized)
    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            authority.directory,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )
    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_INVALID


def test_reader_wraps_artifact_fstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)

    def fail_artifact_fstat(descriptor: int):
        raise OSError("artifact fstat failed")

    monkeypatch.setattr(excitation_artifacts.os, "fstat", fail_artifact_fstat)
    with pytest.raises(AdmissionArtifactError) as caught:
        open_admission_authority(
            authority.directory,
            expected_bundle_kind=BUNDLE_KIND,
            expected_bundle_id=BUNDLE_ID,
        )

    assert caught.value.code is AdmissionArtifactErrorCode.AUTHORITY_INVALID


def test_directory_fsync_failure_reports_unknown_published_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    real_fsync = excitation_artifacts.os.fsync
    published = tmp_path / BUNDLE_ID / ADMISSION_AUTHORITY_MARKER

    def fail_directory_fsync(descriptor: int) -> None:
        if published.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(excitation_artifacts.os, "fsync", fail_directory_fsync)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_post_publish_unlink_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    real_unlink = excitation_artifacts.os.unlink
    failed = False

    def fail_first_temporary_unlink(path) -> None:
        nonlocal failed
        if not failed and str(path).endswith(".tmp"):
            failed = True
            raise OSError("temporary unlink failed")
        real_unlink(path)

    monkeypatch.setattr(excitation_artifacts.os, "unlink", fail_first_temporary_unlink)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    published = tmp_path / BUNDLE_ID / ADMISSION_AUTHORITY_MARKER
    assert published.exists()


def test_post_publish_directory_open_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    target_parent = tmp_path / BUNDLE_ID
    published = target_parent / ADMISSION_AUTHORITY_MARKER
    real_open = excitation_artifacts.os.open

    def fail_target_directory_open(path, flags, *args):
        if Path(path) == target_parent and published.exists():
            raise OSError("directory open failed")
        return real_open(path, flags, *args)

    monkeypatch.setattr(excitation_artifacts.os, "open", fail_target_directory_open)
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_post_publish_directory_close_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    target_parent = tmp_path / BUNDLE_ID
    published = target_parent / ADMISSION_AUTHORITY_MARKER
    real_close = excitation_artifacts.os.close
    failed = False

    def fail_published_directory_close(descriptor: int) -> None:
        nonlocal failed
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        real_close(descriptor)
        if not failed and is_directory and published.exists():
            failed = True
            raise OSError("directory close failed")

    monkeypatch.setattr(
        excitation_artifacts.os, "close", fail_published_directory_close
    )
    with pytest.raises(AdmissionArtifactError) as caught:
        _authority(tmp_path)

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_module_has_no_powerful_feature_host_import() -> None:
    module_path = (
        Path(__file__).parents[1]
        / "jasper"
        / "audio_measurement"
        / "excitation_artifacts.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not any(
        name.startswith(
            (
                "jasper.active_speaker",
                "jasper.correction",
                "jasper.camilla",
                "jasper.dsp_apply",
            )
        )
        for name in imported_modules
    )
