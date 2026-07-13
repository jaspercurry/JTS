# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from jasper.active_speaker.commissioning_receipt import AdmittedCaptureProof
from jasper.audio_measurement.evidence_identity import ArtifactIdentity, CaptureIdentity
from jasper.audio_measurement.excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ExcitationRefusalReason,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    ADMISSION_AUTHORITY_MARKER,
    GENERATION_PATH_PREFIX,
    MAX_ADMISSION_ARTIFACT_BYTES,
    PLAYBACK_PATH_PREFIX,
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    HistoricalExcitationEvidence,
    canonical_admission_bytes,
    create_admission_authority,
    open_admission_authority,
    parse_canonical_admission_bytes,
    persist_generation_admission,
    read_generation_admission,
    read_playback_admission,
    readmit_and_persist_playback_admission,
    readmit_excitation_for_playback,
    refuse_historical_evidence,
)

TARGET = "1" * 64
PROFILE = "2" * 64
REQUIREMENT = "3" * 64
PLAN = "4" * 64
GENERATION_PROOF = "5" * 64
PLAYBACK_PROOF = "6" * 64
OTHER = "f" * 64
BUNDLE_KIND = "jts_active_speaker_commissioning_authority"
BUNDLE_ID = "authority-session-1"
ADMISSION_ID = "combined-main-repeat-1"


def _limits(**changes: object) -> ExcitationLimits:
    values: dict[str, object] = {
        "permitted_band": FrequencyBand(500, 10_000),
        "maximum_effective_peak_dbfs": -12,
        "maximum_duration_s": 8,
        "maximum_repeat_count": 3,
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
        "protection_requirement_fingerprint": REQUIREMENT,
        "excitation_plan_fingerprint": PLAN,
    }
    values.update(changes)
    return ExcitationLimits(**values)  # type: ignore[arg-type]


def _request(limits: ExcitationLimits) -> ExcitationRequest:
    return ExcitationRequest(
        band=FrequencyBand(1_000, 8_000),
        effective_peak_dbfs=-18,
        duration_s=4,
        repeat_count=3,
        target_fingerprint=limits.target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
    )


def _evidence(limits: ExcitationLimits, proof: str) -> ProtectionEvidence:
    return ProtectionEvidence(
        target_fingerprint=limits.target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        protection_requirement_fingerprint=(limits.protection_requirement_fingerprint),
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
        evidence_fingerprint=proof,
        current=True,
    )


def _admission(
    *,
    limits: ExcitationLimits | None = None,
    proof: str = GENERATION_PROOF,
    protection: bool = True,
) -> ExcitationAdmission:
    authority = limits or _limits()
    return admit_excitation(
        _request(authority),
        authority,
        protection_evidence=_evidence(authority, proof) if protection else None,
    )


def _authority(tmp_path: Path, *, bundle_id: str = BUNDLE_ID):
    return create_admission_authority(
        tmp_path / bundle_id,
        bundle_kind=BUNDLE_KIND,
        bundle_id=bundle_id,
    )


def _generation(tmp_path: Path):
    authority = _authority(tmp_path)
    generation = persist_generation_admission(
        authority,
        admission_id=ADMISSION_ID,
        admission=_admission(),
    )
    return authority, generation


def _identity(
    *,
    authority,
    relative_path: str,
    raw: bytes,
    bundle_id: str | None = None,
) -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind=authority.bundle_kind,
        bundle_id=bundle_id or authority.bundle_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def test_canonical_bytes_are_the_frozen_receipt_encoding() -> None:
    admission = _admission()
    expected = json.dumps(
        admission.to_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert canonical_admission_bytes(admission) == expected
    assert parse_canonical_admission_bytes(expected) == admission
    assert not expected.endswith(b"\n")


def test_authority_is_new_exclusive_canonical_and_private(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    marker_path = authority.directory / ADMISSION_AUTHORITY_MARKER

    assert authority.directory.stat().st_mode & 0o777 == 0o750
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


def test_one_session_authority_persists_multiple_unique_attempts(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    admission = _admission()
    admission_ids = ("combined-main-repeat-1", "combined-main-repeat-2")

    generations = tuple(
        persist_generation_admission(
            authority,
            admission_id=admission_id,
            admission=admission,
        )
        for admission_id in admission_ids
    )
    playbacks = tuple(
        readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=admission.limits,
            current_protection_evidence=_evidence(admission.limits, PLAYBACK_PROOF),
        )
        for generation in generations
    )

    assert {generation.authority for generation in generations} == {authority}
    assert {generation.admission_id for generation in generations} == set(
        admission_ids
    )
    for generation, playback in zip(generations, playbacks, strict=True):
        assert playback.artifact is not None
        assert playback.artifact.generation.authority == authority
        assert playback.artifact.generation.admission_id == generation.admission_id
        assert read_playback_admission(
            authority,
            generation,
            playback.artifact.artifact,
        ) == playback.artifact


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


def test_authority_and_role_directories_have_stable_modes_under_strict_umask(
    tmp_path: Path,
) -> None:
    previous = os.umask(0o077)
    try:
        authority, generation = _generation(tmp_path)
    finally:
        os.umask(previous)

    assert authority.directory.stat().st_mode & 0o777 == 0o750
    artifact_path = authority.directory / generation.artifact.relative_path
    for relative in ("admission", "admission/v1", GENERATION_PATH_PREFIX):
        assert (authority.directory / relative).stat().st_mode & 0o777 == 0o750
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

    generation = persist_generation_admission(
        authority,
        admission_id=ADMISSION_ID,
        admission=_admission(),
    )

    assert (authority.directory / generation.artifact.relative_path).is_file()


def test_new_authority_and_role_directories_fsync_their_parent_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    synced: list[Path] = []
    real_fsync_directory = excitation_artifacts._fsync_directory

    def record_fsync(path: Path) -> None:
        synced.append(Path(path))
        real_fsync_directory(path)

    monkeypatch.setattr(
        excitation_artifacts,
        "_fsync_directory",
        record_fsync,
    )
    authority, _generation_artifact = _generation(tmp_path)

    assert tmp_path in synced
    assert authority.directory in synced
    assert authority.directory / "admission" in synced
    assert authority.directory / "admission/v1" in synced
    assert authority.directory / GENERATION_PATH_PREFIX in synced


def test_marker_prepublish_failure_durably_removes_empty_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    synced: list[Path] = []
    real_fsync_directory = excitation_artifacts._fsync_directory

    def record_fsync(path: Path) -> None:
        synced.append(Path(path))
        real_fsync_directory(path)

    def fail_link(_source, _target) -> None:
        raise OSError("marker publish failed")

    monkeypatch.setattr(
        excitation_artifacts,
        "_fsync_directory",
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

    real_fsync_directory = excitation_artifacts._fsync_directory
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
        "_fsync_directory",
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
    late = legacy / f"{PLAYBACK_PATH_PREFIX}/{ADMISSION_ID}.json"
    late.parent.mkdir(parents=True)
    late.write_bytes(canonical_admission_bytes(_admission(proof=PLAYBACK_PROOF)))

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

    with pytest.raises(AdmissionArtifactError) as caught:
        refuse_historical_evidence(HistoricalExcitationEvidence("9" * 64))
    assert (
        caught.value.code is AdmissionArtifactErrorCode.HISTORICAL_EVIDENCE_NOT_ADMITTED
    )


def test_generation_path_role_is_persisted_and_exclusive(tmp_path: Path) -> None:
    authority, generation = _generation(tmp_path)
    path = authority.directory / generation.artifact.relative_path

    assert generation.artifact.relative_path == (
        f"{GENERATION_PATH_PREFIX}/{ADMISSION_ID}.json"
    )
    assert path.read_bytes() == canonical_admission_bytes(generation.admission)
    assert path.stat().st_mode & 0o777 == 0o640
    assert read_generation_admission(authority, generation.artifact) == generation

    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )
    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PATH_CONFLICT


def test_generation_artifact_cannot_be_read_as_playback(tmp_path: Path) -> None:
    authority, generation = _generation(tmp_path)

    with pytest.raises(AdmissionArtifactError) as caught:
        read_playback_admission(authority, generation, generation.artifact)

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PATH_INVALID


def test_playback_recheck_ignores_generation_evidence_and_persists_new_role(
    tmp_path: Path,
) -> None:
    authority, generation = _generation(tmp_path)
    limits = generation.admission.limits
    result = readmit_and_persist_playback_admission(
        authority,
        generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, PLAYBACK_PROOF),
    )

    assert result.allowed is True
    assert result.artifact is not None
    assert (
        result.decision.protection_evidence != generation.admission.protection_evidence
    )
    assert result.artifact.artifact.relative_path == (
        f"{PLAYBACK_PATH_PREFIX}/{ADMISSION_ID}.json"
    )
    assert result.artifact.artifact.fingerprint != generation.artifact.fingerprint
    assert (
        read_playback_admission(authority, generation, result.artifact.artifact)
        == result.artifact
    )


def test_pure_recheck_does_not_treat_hash_inequality_as_freshness() -> None:
    generation = _admission()
    limits = generation.limits

    same_content = readmit_excitation_for_playback(
        generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, GENERATION_PROOF),
    )
    different_observation = readmit_excitation_for_playback(
        generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, PLAYBACK_PROOF),
    )

    assert same_content.allowed is True
    assert different_observation.allowed is True
    assert same_content.protection_evidence != different_observation.protection_evidence


def test_playback_artifact_preserves_active_receipt_encoding(tmp_path: Path) -> None:
    authority, generation = _generation(tmp_path)
    limits = generation.admission.limits
    result = readmit_and_persist_playback_admission(
        authority,
        generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, PLAYBACK_PROOF),
    )
    assert result.artifact is not None

    def artifact(path: str, marker: str) -> ArtifactIdentity:
        raw = marker.encode("ascii")
        return _identity(authority=authority, relative_path=path, raw=raw)

    capture = CaptureIdentity(
        consumer_id="active_crossover",
        measurement_kind="active_crossover_post_apply",
        capture_id="capture-1",
        raw_artifact=artifact("capture/raw.wav", "raw"),
        analysis_input_artifact=artifact("capture/input.json", "input"),
        target_fingerprint=TARGET,
        context_fingerprint="7" * 64,
        geometry_id="reference_axis",
        placement_fingerprint="8" * 64,
        quality_artifact=artifact("capture/quality.json", "quality"),
        admission_artifact=result.artifact.artifact,
    )

    proof = AdmittedCaptureProof(
        capture=capture,
        commissioning_session_id=BUNDLE_ID,
        admission=result.decision,
    )
    assert proof.admission_decision_fingerprint == result.decision.fingerprint


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: json.dumps(json.loads(raw), indent=2).encode("utf-8"),
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"allowed":true', b'"allowed":false'),
    ),
)
def test_reader_rejects_noncanonical_or_tampered_bytes(tmp_path: Path, mutate) -> None:
    authority, generation = _generation(tmp_path)
    path = authority.directory / generation.artifact.relative_path
    raw = mutate(path.read_bytes())
    path.unlink()
    path.write_bytes(raw)
    relabelled = _identity(
        authority=authority,
        relative_path=generation.artifact.relative_path,
        raw=raw,
    )

    with pytest.raises(AdmissionArtifactError) as caught:
        read_generation_admission(authority, relabelled)
    assert caught.value.code in {
        AdmissionArtifactErrorCode.ARTIFACT_NOT_CANONICAL,
        AdmissionArtifactErrorCode.ARTIFACT_MALFORMED,
    }


def test_reader_rejects_wrong_session_symlink_and_oversize(tmp_path: Path) -> None:
    authority, generation = _generation(tmp_path)
    wrong_session = ArtifactIdentity(
        bundle_kind=BUNDLE_KIND,
        bundle_id="other-session",
        relative_path=generation.artifact.relative_path,
        sha256=generation.artifact.sha256,
        byte_size=generation.artifact.byte_size,
    )
    with pytest.raises(AdmissionArtifactError) as caught:
        read_generation_admission(authority, wrong_session)
    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH

    path = authority.directory / generation.artifact.relative_path
    raw = path.read_bytes()
    actual = authority.directory / "actual.json"
    actual.write_bytes(raw)
    path.unlink()
    path.symlink_to(actual)
    with pytest.raises(AdmissionArtifactError) as caught:
        read_generation_admission(authority, generation.artifact)
    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_NOT_REGULAR

    path.unlink()
    oversized = b"x" * (MAX_ADMISSION_ARTIFACT_BYTES + 1)
    path.write_bytes(oversized)
    oversized_identity = _identity(
        authority=authority,
        relative_path=generation.artifact.relative_path,
        raw=oversized,
    )
    with pytest.raises(AdmissionArtifactError) as caught:
        read_generation_admission(authority, oversized_identity)
    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_TOO_LARGE


def test_reader_wraps_artifact_fstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority, generation = _generation(tmp_path)
    real_fstat = excitation_artifacts.os.fstat
    calls = 0

    def fail_artifact_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("artifact fstat failed")
        return real_fstat(descriptor)

    monkeypatch.setattr(excitation_artifacts.os, "fstat", fail_artifact_fstat)
    with pytest.raises(AdmissionArtifactError) as caught:
        read_generation_admission(authority, generation.artifact)

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_READ_FAILED


def test_playback_cannot_cross_authority_sessions(tmp_path: Path) -> None:
    authority, generation = _generation(tmp_path)
    other = _authority(tmp_path, bundle_id="other-session")
    limits = generation.admission.limits

    with pytest.raises(AdmissionArtifactError) as caught:
        readmit_and_persist_playback_admission(
            other,
            generation,
            current_limits=limits,
            current_protection_evidence=_evidence(limits, PLAYBACK_PROOF),
        )

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH
    assert not list((other.directory / PLAYBACK_PATH_PREFIX).glob("*.json"))


def test_playback_reader_rejects_unrelated_admission_with_same_id(
    tmp_path: Path,
) -> None:
    authority, generation = _generation(tmp_path)
    unrelated_limits = _limits(
        permitted_band=FrequencyBand(400, 11_000),
        maximum_effective_peak_dbfs=-10,
    )
    unrelated = _admission(limits=unrelated_limits, proof=PLAYBACK_PROOF)
    raw = canonical_admission_bytes(unrelated)
    relative_path = f"{PLAYBACK_PATH_PREFIX}/{ADMISSION_ID}.json"
    path = authority.directory / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    artifact = _identity(
        authority=authority,
        relative_path=relative_path,
        raw=raw,
    )

    with pytest.raises(AdmissionArtifactError) as caught:
        read_playback_admission(authority, generation, artifact)

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH


@pytest.mark.parametrize(
    ("limits", "expected"),
    (
        (
            _limits(target_fingerprint=OTHER),
            (
                ExcitationRefusalReason.TARGET_IDENTITY_MISMATCH,
                ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
            ),
        ),
        (
            _limits(safety_profile_fingerprint=OTHER),
            (
                ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISMATCH,
                ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
            ),
        ),
        (
            _limits(protection_requirement_fingerprint=OTHER),
            (ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,),
        ),
        (
            _limits(excitation_plan_fingerprint=OTHER),
            (
                ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
                ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISMATCH,
            ),
        ),
    ),
)
def test_playback_recheck_refuses_exact_binding_changes(
    tmp_path: Path,
    limits: ExcitationLimits,
    expected: tuple[ExcitationRefusalReason, ...],
) -> None:
    authority, generation = _generation(tmp_path)
    result = readmit_and_persist_playback_admission(
        authority,
        generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, PLAYBACK_PROOF),
    )

    assert result.allowed is False
    assert result.decision.refusal_reasons == expected
    assert not (authority.directory / PLAYBACK_PATH_PREFIX).exists()


def test_refused_generation_decision_cannot_be_persisted(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    refused = _admission(protection=False)

    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=refused,
        )

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_NOT_ALLOWED
    assert not (authority.directory / GENERATION_PATH_PREFIX).exists()


def test_publish_failure_is_typed_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    caplog.set_level(logging.WARNING)
    authority = _authority(tmp_path)

    def fail_link(_source, _target) -> None:
        raise OSError("read-only authority store")

    monkeypatch.setattr(excitation_artifacts.os, "link", fail_link)
    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_FAILED
    destination = authority.directory / GENERATION_PATH_PREFIX / f"{ADMISSION_ID}.json"
    assert not destination.exists()
    assert not list(authority.directory.rglob("*.tmp"))
    assert any(
        "result=failed failure_code=admission_artifact_persist_failed" in message
        for message in (record.getMessage() for record in caplog.records)
    )


def test_directory_fsync_failure_reports_unknown_published_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)
    real_fsync = excitation_artifacts.os.fsync
    published = authority.directory / GENERATION_PATH_PREFIX / f"{ADMISSION_ID}.json"

    def fail_directory_fsync(descriptor: int) -> None:
        if published.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(excitation_artifacts.os, "fsync", fail_directory_fsync)
    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_post_publish_unlink_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)
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
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    published = authority.directory / GENERATION_PATH_PREFIX / f"{ADMISSION_ID}.json"
    assert published.exists()


def test_post_publish_directory_open_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)
    target_parent = authority.directory / GENERATION_PATH_PREFIX
    published = target_parent / f"{ADMISSION_ID}.json"
    real_open = excitation_artifacts.os.open

    def fail_target_directory_open(path, flags, *args):
        if Path(path) == target_parent and published.exists():
            raise OSError("directory open failed")
        return real_open(path, flags, *args)

    monkeypatch.setattr(excitation_artifacts.os, "open", fail_target_directory_open)
    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_post_publish_directory_close_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)
    target_parent = authority.directory / GENERATION_PATH_PREFIX
    published = target_parent / f"{ADMISSION_ID}.json"
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
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert published.exists()


def test_post_publish_readback_failure_reports_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import excitation_artifacts

    authority = _authority(tmp_path)
    real_fstat = excitation_artifacts.os.fstat
    calls = 0

    def fail_artifact_readback_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("artifact readback failed")
        return real_fstat(descriptor)

    monkeypatch.setattr(
        excitation_artifacts.os,
        "fstat",
        fail_artifact_readback_fstat,
    )
    with pytest.raises(AdmissionArtifactError) as caught:
        persist_generation_admission(
            authority,
            admission_id=ADMISSION_ID,
            admission=_admission(),
        )

    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    published = authority.directory / GENERATION_PATH_PREFIX / f"{ADMISSION_ID}.json"
    assert published.exists()


def test_stable_events_cover_persist_refusal_and_historical(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    authority, generation = _generation(tmp_path)
    tightened = _limits(maximum_duration_s=3)
    readmit_and_persist_playback_admission(
        authority,
        generation,
        current_limits=tightened,
        current_protection_evidence=_evidence(tightened, PLAYBACK_PROOF),
    )
    with pytest.raises(AdmissionArtifactError):
        refuse_historical_evidence(HistoricalExcitationEvidence("9" * 64))

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=audio_measurement.excitation_admission boundary=generation "
        "result=persisted" in message
        for message in messages
    )
    assert any(
        "event=audio_measurement.excitation_admission boundary=playback "
        "result=refused" in message
        for message in messages
    )
    assert any(
        "result=historical_evidence_not_admitted" in message for message in messages
    )


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
