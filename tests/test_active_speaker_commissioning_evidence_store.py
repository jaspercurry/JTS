# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker.bundles import (
    BUNDLE_KIND,
    DEFAULT_SESSIONS_MAX_BYTES,
    open_bundle,
)
from jasper.active_speaker.commissioning_evidence_store import (
    MAX_EVIDENCE_ARTIFACT_BYTES,
    MAX_NON_ARTIFACT_READ_BYTES,
    MIN_FREE_SPACE_AFTER_PUBLISH_BYTES,
    MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES,
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from tests.active_speaker_fixtures import mono_output_topology


def _open_store(tmp_path: Path) -> CommissioningEvidenceStore:
    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    return CommissioningEvidenceStore.open(
        info["bundle_dir"],
        expected_session_id=info["session_id"],
    )


def test_raw_publish_is_write_once_idempotent_and_conflict_strict(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    first = store.publish_raw_artifact("captures/one.wav", b"one")
    second = store.publish_raw_artifact("captures/one.wav", b"one")

    assert second == first
    assert store.reopen_artifact(first) == b"one"
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("captures/one.wav", b"different")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.PATH_CONFLICT


def test_open_requires_the_exact_existing_session(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        CommissioningEvidenceStore.open(
            store.bundle_dir,
            expected_session_id="different-session",
        )
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY


def test_paths_reject_traversal_parent_symlinks_and_file_symlinks(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    for unsafe in ("../escape", "/absolute", "nested/../escape", "bad\\path"):
        with pytest.raises(CommissioningEvidenceStoreError) as raised:
            store.publish_raw_artifact(unsafe, b"blocked")
        assert raised.value.code is CommissioningEvidenceStoreErrorCode.INVALID_PATH

    store.publish_raw_artifact("seed.bin", b"seed")
    artifact_root = store.bundle_dir / "evidence/v1/artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifact_root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("escape/new.bin", b"blocked")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INVALID_PATH

    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    link = artifact_root / "link.bin"
    link.symlink_to(target)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.identify_artifact("evidence/v1/artifacts/link.bin")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.NOT_REGULAR


def test_reads_are_bounded_and_detect_tamper_truncation_and_missing(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    artifact = store.publish_raw_artifact("capture.wav", b"original")
    path = store.bundle_dir / artifact.relative_path

    path.write_bytes(b"tampered")
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_artifact(artifact)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH

    path.write_bytes(b"cut")
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_artifact(artifact)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH

    path.unlink()
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_artifact(artifact)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.MISSING

    oversized = ArtifactIdentity(
        bundle_kind=artifact.bundle_kind,
        bundle_id=artifact.bundle_id,
        relative_path="evidence/v1/artifacts/too-large.wav",
        sha256="0" * 64,
        byte_size=MAX_EVIDENCE_ARTIFACT_BYTES + 1,
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_artifact(oversized)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.TOO_LARGE


def test_non_artifact_reads_are_bounded_at_the_wider_ceiling(
    tmp_path: Path,
) -> None:
    """A read outside ``evidence/v1/artifacts/`` is capped at 32 MiB, not 5 MiB.

    ``crossover_v2/record_index.reopen_measurement_capture`` reopens take
    JSON and ``summed/*.wav`` this way -- neither lives under the artifact
    root, so both must clear the wider, non-artifact ceiling.
    """
    store = _open_store(tmp_path)
    relative_path = "summed/take.wav"
    payload = b"a" * (MAX_EVIDENCE_ARTIFACT_BYTES + 1)
    target = store.bundle_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    identity = ArtifactIdentity(
        bundle_kind=BUNDLE_KIND,
        bundle_id=store.session_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )
    assert store.reopen_artifact(identity) == payload

    oversized = ArtifactIdentity(
        bundle_kind=BUNDLE_KIND,
        bundle_id=store.session_id,
        relative_path=relative_path,
        sha256="0" * 64,
        byte_size=MAX_NON_ARTIFACT_READ_BYTES + 1,
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_artifact(oversized)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.TOO_LARGE


def test_json_publish_is_canonical_and_strictly_reopened(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    artifact = store.publish_json_artifact(
        "quality/capture.json",
        {"z": 1, "nested": {"ok": True}, "a": "value"},
    )

    assert store.reopen_json_artifact(artifact) == {
        "a": "value",
        "nested": {"ok": True},
        "z": 1,
    }
    assert (store.bundle_dir / artifact.relative_path).read_bytes() == (
        b'{"a":"value","nested":{"ok":true},"z":1}'
    )


def test_authoritative_total_counts_stimuli_and_admissions(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    before = store._authoritative_total()
    stimulus = store.bundle_dir / "stimuli/count-me.wav"
    admission = store.bundle_dir / "admission/count-me.json"
    stimulus.parent.mkdir(parents=True, exist_ok=True)
    admission.parent.mkdir(parents=True, exist_ok=True)
    stimulus.write_bytes(b"stimulus")
    admission.write_bytes(b"admission")

    assert store._authoritative_total() == before + len(b"stimulusadmission")


def test_disk_usage_failure_is_a_stable_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)

    def fail_disk_usage(_path: Path) -> None:
        raise OSError("simulated disk usage failure")

    monkeypatch.setattr(evidence_store.shutil, "disk_usage", fail_disk_usage)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("disk-usage.bin", b"value")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.PERSIST_FAILED


def test_filesystem_metadata_failures_use_the_store_error_taxonomy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    real_chmod = evidence_store.os.chmod

    def fail_artifact_chmod(path: str | Path, mode: int) -> None:
        if Path(path).name == "artifacts":
            raise OSError("simulated chmod failure")
        real_chmod(path, mode)

    monkeypatch.setattr(evidence_store.os, "chmod", fail_artifact_chmod)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("metadata.bin", b"value")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.PERSIST_FAILED


def test_directory_creation_failure_uses_the_store_error_taxonomy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store(tmp_path)
    real_mkdir = Path.mkdir

    def fail_artifact_mkdir(path: Path, *args, **kwargs) -> None:
        if path.name == "artifacts":
            raise OSError("simulated mkdir failure")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_artifact_mkdir)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("mkdir.bin", b"value")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.PERSIST_FAILED


def test_publish_refuses_per_artifact_total_and_free_space_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)

    monkeypatch.setattr(evidence_store, "MAX_EVIDENCE_ARTIFACT_BYTES", 2)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("oversized.bin", b"123")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.TOO_LARGE

    monkeypatch.setattr(
        evidence_store,
        "MAX_EVIDENCE_ARTIFACT_BYTES",
        MAX_EVIDENCE_ARTIFACT_BYTES,
    )
    current = store._authoritative_total()
    monkeypatch.setattr(
        evidence_store,
        "MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES",
        current + 2,
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("total.bin", b"123")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.TOTAL_TOO_LARGE

    monkeypatch.setattr(
        evidence_store,
        "MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES",
        MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES,
    )
    monkeypatch.setattr(
        evidence_store.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=MIN_FREE_SPACE_AFTER_PUBLISH_BYTES),
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("space.bin", b"1")
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INSUFFICIENT_SPACE


def test_directory_fsync_failure_after_link_is_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    store.publish_raw_artifact("first.bin", b"first")
    real_fsync = evidence_store.fsync_directory
    artifact_dir_calls = 0

    def fail_final_artifact_dir_fsync(path: Path) -> None:
        nonlocal artifact_dir_calls
        if path.name == "artifacts":
            artifact_dir_calls += 1
            if artifact_dir_calls == 2:
                raise OSError(errno.EIO, "directory fsync fault")
        real_fsync(path)

    monkeypatch.setattr(
        evidence_store,
        "fsync_directory",
        fail_final_artifact_dir_fsync,
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("second.bin", b"second")
    assert (
        raised.value.code
        is CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN
    )
    published = store.identify_artifact(
        "evidence/v1/artifacts/second.bin"
    )
    assert published.sha256 == hashlib.sha256(b"second").hexdigest()
    assert store.publish_raw_artifact("second.bin", b"second") == published
    assert artifact_dir_calls == 3


def test_identical_link_race_unlinks_temp_before_final_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    payload = b"raced"
    raced = False
    events: list[str] = []
    real_unlink = evidence_store.os.unlink
    real_fsync = evidence_store.fsync_directory

    def race_link(_source: str, target: Path) -> None:
        nonlocal raced
        Path(target).write_bytes(payload)
        raced = True
        raise FileExistsError

    def record_unlink(path: str | Path) -> None:
        if raced:
            events.append("unlink")
        real_unlink(path)

    def record_fsync(path: Path) -> None:
        if raced:
            events.append("fsync")
        real_fsync(path)

    monkeypatch.setattr(evidence_store.os, "link", race_link)
    monkeypatch.setattr(evidence_store.os, "unlink", record_unlink)
    monkeypatch.setattr(evidence_store, "fsync_directory", record_fsync)

    artifact = store.publish_raw_artifact("race.bin", payload)

    assert store.reopen_artifact(artifact) == payload
    assert events == ["unlink", "fsync"]
    assert not list((store.bundle_dir / artifact.relative_path).parent.glob("*.tmp"))


@pytest.mark.parametrize("link_race", [False, True])
def test_identical_success_paths_refuse_drift_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_race: bool,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    relative = "evidence/v1/artifacts/drift.bin"
    target = store.bundle_dir / relative
    payload = b"same"
    if not link_race:
        store.publish_raw_artifact("drift.bin", payload)
    else:
        def race_link(_source: str, destination: Path) -> None:
            Path(destination).write_bytes(payload)
            raise FileExistsError

        monkeypatch.setattr(evidence_store.os, "link", race_link)

    real_read = evidence_store.CommissioningEvidenceStore._read_path
    reads = 0

    def drift_after_equality_read(
        self: CommissioningEvidenceStore,
        relative_path: str,
    ) -> bytes:
        nonlocal reads
        raw = real_read(self, relative_path)
        if relative_path == relative:
            reads += 1
            if reads == 1:
                target.write_bytes(b"evil")
        return raw

    monkeypatch.setattr(
        evidence_store.CommissioningEvidenceStore,
        "_read_path",
        drift_after_equality_read,
    )

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_raw_artifact("drift.bin", payload)
    assert (
        raised.value.code
        is CommissioningEvidenceStoreErrorCode.PERSIST_OUTCOME_UNKNOWN
    )


def test_new_artifact_inherits_the_authority_directory_group(
    tmp_path: Path,
) -> None:
    shared_gid = next((gid for gid in os.getgroups() if gid != os.getegid()), os.getegid())
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    os.chown(sessions, -1, shared_gid)
    # Mirror the installer-owned parent (deploy/lib/install/env-migrations.sh
    # `d:2770`): the setgid bit is what confers the group on Linux.
    sessions.chmod(stat.S_ISGID | 0o770)
    store = _open_store(tmp_path)
    artifact = store.publish_raw_artifact("nested/group-owned.bin", b"owned")
    path = store.bundle_dir / artifact.relative_path
    assert store.reopen_artifact(artifact) == b"owned"
    assert path.stat().st_gid == path.parent.stat().st_gid
    assert path.stat().st_mode & 0o7777 == 0o640
    # The mode the store requests carries no SGID (a hardened unit refuses one). Whether
    # mkdir already lands on it under the setgid `sessions` parent is a
    # kernel/filesystem detail this test does not pin -- only that the
    # permission bits end up correct and the group still inherits.
    assert store.bundle_dir.stat().st_mode & 0o777 == 0o750
    assert store.bundle_dir.stat().st_gid == shared_gid
    parent = path.parent
    while parent != sessions:
        assert parent.stat().st_mode & 0o777 == 0o750
        parent = parent.parent


def test_publish_headroom_is_frozen_and_not_derived_from_retention() -> None:
    """The publish-time free-space floor answers "how much room must a publish
    leave behind"; ``DEFAULT_SESSIONS_MAX_BYTES`` answers "how much history do
    we keep". They were ONE constant until the crossover-v2 position-group
    choreography raised retention 256 MiB -> 1 GiB (flat-linearization PR-3b),
    at which point sharing them would have quadrupled the free space a Pi must
    have before it may publish ANY evidence — a new refusal on every SD card
    with under a gigabyte spare, for a change that was only ever about keeping
    more history. Frozen here at the pre-raise value, deliberately.
    """
    assert MIN_FREE_SPACE_AFTER_PUBLISH_BYTES == 256 * 1024 * 1024
    assert MIN_FREE_SPACE_AFTER_PUBLISH_BYTES != DEFAULT_SESSIONS_MAX_BYTES
    # Still comfortably above one crossover-v2 cloud session's own evidence
    # (15 accepted captures, each bounded by MAX_EVIDENCE_ARTIFACT_BYTES), which
    # is the run the raise was made for.
    assert MIN_FREE_SPACE_AFTER_PUBLISH_BYTES >= 15 * MAX_EVIDENCE_ARTIFACT_BYTES
