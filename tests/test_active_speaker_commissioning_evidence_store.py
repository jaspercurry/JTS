# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker.bundles import (
    BUNDLE_FILE_MODE,
    DEFAULT_SESSIONS_MAX_BYTES,
    open_bundle,
)
from jasper.active_speaker.commissioning_evidence import (
    STATIONARY_CAPTURE_COUNT,
    CompleteIsolatedDriverEvidence,
    CompleteCommissioningEvidence,
    derive_region_evidence_plan,
)
from jasper.active_speaker.commissioning_evidence_store import (
    MAX_CAPTURE_ARTIFACT_COUNT,
    MAX_COMMISSIONING_REGIONS,
    MAX_EVIDENCE_ARTIFACT_BYTES,
    MAX_ISOLATED_CAPTURE_ARTIFACT_COUNT,
    MAX_ISOLATED_DRIVER_TARGETS,
    MAX_SUMMED_CAPTURE_ARTIFACT_COUNT,
    MIN_FREE_SPACE_AFTER_PUBLISH_BYTES,
    MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES,
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
    attempt_capture_relative_path,
    complete_relative_path,
    isolated_driver_evidence_relative_path,
    plan_relative_path,
)
from jasper.active_speaker.commissioning_run import (
    CommissioningRunHandle,
    CommissioningRunStore,
)
from jasper.active_speaker.profile import (
    ADJACENT_PAIRS_BY_WAY,
    DRIVER_ROLES_BY_WAY,
    SIDES_BY_LAYOUT,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.excitation_artifacts import canonical_admission_bytes
from jasper.audio_measurement.null_walk import (
    MAX_SCHEDULED_CANDIDATES,
    MIN_CAPTURE_COUNT,
    BoundedNullWalkSchedule,
    NullWalkSpec,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_commissioning_evidence import (
    _Harness,
    _complete_isolated,
    _hash,
    _preset,
    _region,
)


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


def _harness_for_store(
    tmp_path: Path,
    store: CommissioningEvidenceStore,
) -> _Harness:
    session_fingerprint = _hash("strict-store-comparison")
    run_store = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="a" * 32,
    )
    run = run_store.start(
        session_id=store.session_id,
        session_fingerprint=session_fingerprint,
    )
    plan = derive_region_evidence_plan(
        _preset(),
        mono_output_topology(mode="active_3_way"),
        run=run,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=session_fingerprint,
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    return _Harness(store=run_store, plan=plan)


def _write_exact(bundle_dir: Path, identity: ArtifactIdentity, raw: bytes) -> None:
    assert hashlib.sha256(raw).hexdigest() == identity.sha256
    assert len(raw) == identity.byte_size
    path = bundle_dir / identity.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _all_captures(complete: CompleteCommissioningEvidence):
    for region in complete.regions:
        yield from region.normal.captures
        yield from region.reverse.captures
        for point in region.delay_walk.points:
            yield from point.captures


def _materialize_complete(
    store: CommissioningEvidenceStore,
    complete: CompleteCommissioningEvidence,
) -> CompleteCommissioningEvidence:
    def materialize_capture(capture):
        index = int(capture.capture.capture_id.rsplit("-", 1)[1])
        token_base = f"{capture.region_id}:{capture.evidence_kind}:{index}"
        raw_artifact = store.publish_raw_artifact(
            f"captures/{capture.capture.capture_id}/raw.wav",
            f"raw:{token_base}".encode(),
        )
        analysis_artifact = store.publish_raw_artifact(
            f"captures/{capture.capture.capture_id}/analysis.json",
            f"analysis:{token_base}".encode(),
        )
        quality_artifact = store.publish_raw_artifact(
            f"captures/{capture.capture.capture_id}/quality.json",
            f"quality:{token_base}".encode(),
        )
        stimulus_raw = f"stimulus:{token_base}".encode()
        stimulus_artifact = ArtifactIdentity(
            bundle_kind=capture.stimulus.artifact.bundle_kind,
            bundle_id=capture.stimulus.artifact.bundle_id,
            relative_path=f"stimuli/{capture.admission_id}.wav",
            sha256=hashlib.sha256(stimulus_raw).hexdigest(),
            byte_size=len(stimulus_raw),
        )
        _write_exact(
            store.bundle_dir,
            stimulus_artifact,
            stimulus_raw,
        )
        generation = canonical_admission_bytes(capture.generation_admission)
        playback = canonical_admission_bytes(capture.playback_admission)
        _write_exact(store.bundle_dir, capture.generation_artifact, generation)
        _write_exact(store.bundle_dir, capture.playback_artifact, playback)
        return replace(
            capture,
            capture=replace(
                capture.capture,
                raw_artifact=raw_artifact,
                analysis_input_artifact=analysis_artifact,
                quality_artifact=quality_artifact,
            ),
            stimulus=replace(capture.stimulus, artifact=stimulus_artifact),
        )

    regions = []
    for region in complete.regions:
        walk = region.delay_walk
        geometry_artifact = store.publish_raw_artifact(
            f"geometry/{walk.speaker_group_id}/{walk.region_id}.json",
            (
                f"geometry:{walk.region_id}:"
                f"{walk.geometry_attestation.signed_geometry_seed_us}"
            ).encode(),
        )
        repeatability_artifact = store.publish_raw_artifact(
            f"repeatability/{walk.speaker_group_id}/{walk.region_id}.json",
            f"repeatability:{walk.region_id}".encode(),
        )
        regions.append(
            replace(
                region,
                normal=replace(
                    region.normal,
                    captures=tuple(
                        materialize_capture(item) for item in region.normal.captures
                    ),
                ),
                reverse=replace(
                    region.reverse,
                    captures=tuple(
                        materialize_capture(item) for item in region.reverse.captures
                    ),
                ),
                delay_walk=replace(
                    walk,
                    geometry_attestation=replace(
                        walk.geometry_attestation,
                        attestation_artifact=geometry_artifact,
                    ),
                    repeatability_artifact=repeatability_artifact,
                    points=tuple(
                        replace(
                            point,
                            captures=tuple(
                                materialize_capture(item) for item in point.captures
                            ),
                        )
                        for point in walk.points
                    ),
                ),
            )
        )
    return replace(complete, regions=tuple(regions))


def _materialize_isolated(
    store: CommissioningEvidenceStore,
    complete: CompleteIsolatedDriverEvidence,
) -> CompleteIsolatedDriverEvidence:
    drivers = []
    for driver in complete.drivers:
        captures = []
        for capture in driver.captures:
            token = capture.capture.capture_id
            raw_artifact = store.publish_raw_artifact(
                f"isolated/{token}/raw.wav",
                f"raw:{token}".encode(),
            )
            analysis_artifact = store.publish_raw_artifact(
                f"isolated/{token}/analysis.json",
                f"analysis:{token}".encode(),
            )
            quality_artifact = store.publish_raw_artifact(
                f"isolated/{token}/quality.json",
                f"quality:{token}".encode(),
            )
            stimulus_raw = f"stimulus:{token}".encode()
            stimulus_artifact = ArtifactIdentity(
                bundle_kind=capture.stimulus.artifact.bundle_kind,
                bundle_id=capture.stimulus.artifact.bundle_id,
                relative_path=f"stimuli/{capture.admission_id}.wav",
                sha256=hashlib.sha256(stimulus_raw).hexdigest(),
                byte_size=len(stimulus_raw),
            )
            _write_exact(store.bundle_dir, stimulus_artifact, stimulus_raw)
            _write_exact(
                store.bundle_dir,
                capture.generation_artifact,
                canonical_admission_bytes(capture.generation_admission),
            )
            _write_exact(
                store.bundle_dir,
                capture.playback_artifact,
                canonical_admission_bytes(capture.playback_admission),
            )
            captures.append(
                replace(
                    capture,
                    capture=replace(
                        capture.capture,
                        raw_artifact=raw_artifact,
                        analysis_input_artifact=analysis_artifact,
                        quality_artifact=quality_artifact,
                    ),
                    stimulus=replace(
                        capture.stimulus,
                        artifact=stimulus_artifact,
                    ),
                )
            )
        repeatability = store.publish_json_artifact(
            f"isolated/{driver.speaker_group_id}/{driver.role}/repeatability.json",
            {
                "driver_target_fingerprint": driver.driver_target_fingerprint,
                "repeat_count": len(captures),
            },
        )
        drivers.append(
            replace(
                driver,
                captures=tuple(captures),
                repeatability_artifact=repeatability,
            )
        )
    return replace(complete, drivers=tuple(drivers))


def test_complete_isolated_driver_evidence_round_trips_at_run_scoped_path(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    complete = _materialize_isolated(store, _complete_isolated(harness))

    artifact = store.publish_complete_isolated_driver_evidence(complete)

    assert artifact.relative_path == isolated_driver_evidence_relative_path(
        complete.plan.authority.run.run_id
    )
    assert "/generations/" not in artifact.relative_path
    assert (store.bundle_dir / artifact.relative_path).stat().st_mode & 0o777 == (
        BUNDLE_FILE_MODE
    )
    assert (
        store.reopen_complete_isolated_driver_evidence(
            run_id=complete.plan.authority.run.run_id
        )
        == complete
    )
    assert store.verify_complete_isolated_driver_evidence(complete) == complete

    restarted = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="b" * 32,
    )
    claimed = restarted.claim_owner()
    assert claimed is not None and claimed.owner_generation == 2
    assert (
        store.reopen_complete_isolated_driver_evidence(run_id=claimed.run_id)
        == complete
    )


@pytest.mark.parametrize(
    "role",
    (
        "raw",
        "analysis",
        "quality",
        "stimulus",
        "generation",
        "playback",
        "repeatability",
    ),
)
def test_complete_isolated_reopen_detects_every_child_role_tamper(
    tmp_path: Path,
    role: str,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    complete = _materialize_isolated(store, _complete_isolated(harness))
    store.publish_complete_isolated_driver_evidence(complete)
    driver = complete.drivers[0]
    capture = driver.captures[0]
    artifact = {
        "raw": capture.capture.raw_artifact,
        "analysis": capture.capture.analysis_input_artifact,
        "quality": capture.capture.quality_artifact,
        "stimulus": capture.stimulus.artifact,
        "generation": capture.generation_artifact,
        "playback": capture.playback_artifact,
        "repeatability": driver.repeatability_artifact,
    }[role]
    (store.bundle_dir / artifact.relative_path).write_bytes(b"tampered")

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_complete_isolated_driver_evidence(
            run_id=complete.plan.authority.run.run_id
        )
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH


def test_complete_isolated_store_refuses_wrong_run_session_and_stimulus_path(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    complete = _materialize_isolated(store, _complete_isolated(harness))
    artifact = store.publish_complete_isolated_driver_evidence(complete)

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_complete_isolated_driver_evidence(
            run_id="f" * 32,
            artifact=artifact,
        )
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH

    other_store = _open_store(tmp_path / "other")
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        other_store.publish_complete_isolated_driver_evidence(complete)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY

    driver = complete.drivers[0]
    capture = driver.captures[0]
    wrong_stimulus = replace(
        capture.stimulus.artifact,
        relative_path="stimuli/wrong-admission.wav",
    )
    wrong_capture = replace(
        capture,
        stimulus=replace(capture.stimulus, artifact=wrong_stimulus),
    )
    wrong_driver = replace(
        driver,
        captures=(wrong_capture, *driver.captures[1:]),
    )
    wrong_complete = replace(
        complete,
        drivers=(wrong_driver, *complete.drivers[1:]),
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.verify_complete_isolated_driver_evidence(wrong_complete)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH


def test_complete_isolated_store_enforces_child_size_bound(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    complete = _materialize_isolated(store, _complete_isolated(harness))
    driver = complete.drivers[0]
    oversized = replace(
        driver.repeatability_artifact,
        byte_size=MAX_EVIDENCE_ARTIFACT_BYTES + 1,
    )
    oversized_complete = replace(
        complete,
        drivers=(
            replace(driver, repeatability_artifact=oversized),
            *complete.drivers[1:],
        ),
    )

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.verify_complete_isolated_driver_evidence(oversized_complete)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.TOO_LARGE


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


def test_plan_paths_are_owner_generation_scoped_and_typed_reopen_is_exact(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    original = harness.plan
    original_artifact = store.publish_region_evidence_plan(original)

    assert original_artifact.relative_path == plan_relative_path(original.authority.run)
    assert (
        store.reopen_region_evidence_plan(run=original.authority.run) == original
    )

    restarted = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="b" * 32,
    )
    claimed = restarted.claim_owner()
    assert claimed is not None
    replacement = derive_region_evidence_plan(
        _preset(),
        mono_output_topology(mode="active_3_way"),
        run=claimed,
        protected_safety_profile_fingerprint=_hash("profile"),
        comparison_set_fingerprint=claimed.session_fingerprint,
        threshold_profile_fingerprint=_hash("thresholds"),
        context_fingerprint=_hash("context"),
    )
    replacement_artifact = store.publish_region_evidence_plan(replacement)

    assert replacement_artifact.relative_path != original_artifact.relative_path
    assert store.reopen_region_evidence_plan(run=claimed) == replacement

    copied_path = "evidence/v1/copied-plan.json"
    copied_target = store.bundle_dir / copied_path
    copied_target.parent.mkdir(parents=True, exist_ok=True)
    copied_target.write_bytes(
        (store.bundle_dir / replacement_artifact.relative_path).read_bytes()
    )
    copied = store.identify_artifact(copied_path)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_region_evidence_plan(run=claimed, artifact=copied)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH


def test_attempt_capture_discovery_is_deterministic_and_contiguous(
    tmp_path: Path,
) -> None:
    assert attempt_capture_relative_path("attempt", 0).endswith("/0000.json")
    assert attempt_capture_relative_path("attempt", 12).endswith("/0012.json")
    with pytest.raises(CommissioningEvidenceStoreError):
        attempt_capture_relative_path("attempt", -1)


def test_attempt_discovery_ignores_only_regular_store_temp_residue(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    attempt_id = "attempt"
    collection = store.bundle_dir / Path(
        attempt_capture_relative_path(attempt_id, 0)
    ).parent
    collection.mkdir(parents=True)
    (collection / ".0000.json.crash123.tmp").write_bytes(b"residue")

    assert store.reopen_attempt_captures(attempt_id) == ()

    (collection / ".0001.json.crash123.tmp").symlink_to(
        collection / ".0000.json.crash123.tmp"
    )
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_attempt_captures(attempt_id)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.NOT_REGULAR
    (collection / ".0001.json.crash123.tmp").unlink()
    (collection / "unexpected.txt").write_bytes(b"unexpected")
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_attempt_captures(attempt_id)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.NOT_REGULAR


def test_schedule_reopen_rejects_a_foreign_session_handle(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "first")
    other = _open_store(tmp_path / "second")
    harness = _harness_for_store(tmp_path / "first", store)
    run = harness.plan.authority.run
    spec = NullWalkSpec(
        crossover_fc_hz=2_000.0,
        geometry_seed_us=0.0,
        positive_delay_target="tweeter",
        negative_delay_target="woofer",
    )
    schedule = BoundedNullWalkSchedule(spec, refinement_anchor_us=0.0)
    artifact = store.publish_bounded_null_walk_schedule(
        schedule,
        spec=spec,
        run=run,
        speaker_group_id="mono",
        region_id="woofer_tweeter",
    )
    foreign = CommissioningRunHandle(
        session_id=other.session_id,
        session_fingerprint=_hash("foreign-session"),
        run_id=run.run_id,
        owner_id=run.owner_id,
        owner_generation=run.owner_generation,
    )

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_bounded_null_walk_schedule(
            spec=spec,
            run=foreign,
            speaker_group_id="mono",
            region_id="woofer_tweeter",
            artifact=artifact,
        )
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY


def test_deep_complete_publish_reopen_and_missing_child_fail_closed(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    placement = _hash("placement")
    complete = CompleteCommissioningEvidence(
        plan=harness.plan,
        regions=tuple(
            _region(
                harness,
                target_index=index,
                placement_fingerprint=placement,
            )
            for index in range(len(harness.plan.targets))
        ),
    )
    complete = _materialize_complete(store, complete)

    plan_artifact = store.publish_region_evidence_plan(complete.plan)
    assert store.reopen_region_evidence_plan(
        run=complete.plan.authority.run,
        artifact=plan_artifact,
    ) == complete.plan
    first_region = complete.regions[0]
    for ordinal, capture in enumerate(first_region.normal.captures):
        store.publish_admitted_region_capture(capture, ordinal=ordinal)
    assert store.reopen_attempt_captures(first_region.normal.attempt.attempt_id) == (
        first_region.normal.captures
    )
    stationary_artifact = store.publish_stationary_region_evidence(
        first_region.normal
    )
    assert (
        store.reopen_stationary_region_evidence(stationary_artifact)
        == first_region.normal
    )
    first_point = first_region.delay_walk.points[0]
    point_artifact = store.publish_delay_point_evidence(first_point)
    assert store.reopen_delay_point_evidence(point_artifact) == first_point
    schedule_artifact = store.publish_bounded_null_walk_schedule(
        first_region.delay_walk.schedule,
        spec=first_region.delay_walk.spec,
        run=complete.plan.authority.run,
        speaker_group_id=first_region.target.speaker_group_id,
        region_id=first_region.target.region_id,
    )
    assert store.reopen_bounded_null_walk_schedule(
        spec=first_region.delay_walk.spec,
        run=complete.plan.authority.run,
        speaker_group_id=first_region.target.speaker_group_id,
        region_id=first_region.target.region_id,
        artifact=schedule_artifact,
    ) == first_region.delay_walk.schedule
    walk_artifact = store.publish_delay_walk_evidence(first_region.delay_walk)
    assert store.reopen_delay_walk_evidence(walk_artifact) == first_region.delay_walk
    region_artifact = store.publish_region_commissioning_evidence(first_region)
    assert (
        store.reopen_region_commissioning_evidence(region_artifact) == first_region
    )

    artifact = store.publish_complete_commissioning_evidence(complete)
    assert artifact.relative_path == complete_relative_path(
        complete.plan.authority.run.run_id
    )
    assert (
        store.reopen_complete_commissioning_evidence(
            run_id=complete.plan.authority.run.run_id
        )
        == complete
    )

    missing = next(_all_captures(complete)).capture.quality_artifact
    (store.bundle_dir / missing.relative_path).unlink()
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_complete_commissioning_evidence(
            run_id=complete.plan.authority.run.run_id
        )
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.MISSING


def test_deep_verification_requires_canonical_child_role_namespaces(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    harness = _harness_for_store(tmp_path, store)
    complete = CompleteCommissioningEvidence(
        plan=harness.plan,
        regions=tuple(
            _region(
                harness,
                target_index=index,
                placement_fingerprint=_hash("placement"),
            )
            for index in range(len(harness.plan.targets))
        ),
    )
    complete = _materialize_complete(store, complete)
    capture = next(_all_captures(complete))
    raw = store.reopen_artifact(capture.capture.raw_artifact)
    legacy = ArtifactIdentity(
        bundle_kind=capture.capture.raw_artifact.bundle_kind,
        bundle_id=capture.capture.raw_artifact.bundle_id,
        relative_path="legacy/raw.wav",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )
    _write_exact(store.bundle_dir, legacy, raw)
    copied = replace(
        capture,
        capture=replace(capture.capture, raw_artifact=legacy),
    )

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.publish_admitted_region_capture(copied, ordinal=0)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH


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


def test_attempt_collection_read_failure_is_not_reported_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    attempt_id = "attempt"
    collection = store.bundle_dir / Path(
        attempt_capture_relative_path(attempt_id, 0)
    ).parent
    collection.mkdir(parents=True)
    real_scandir = evidence_store.os.scandir

    def fail_collection(path: str | Path):
        if Path(path) == collection:
            raise OSError("simulated collection failure")
        return real_scandir(path)

    monkeypatch.setattr(evidence_store.os, "scandir", fail_collection)
    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        store.reopen_attempt_captures(attempt_id)
    assert raised.value.code is CommissioningEvidenceStoreErrorCode.INTEGRITY_MISMATCH


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
    real_fsync = evidence_store._fsync_directory
    artifact_dir_calls = 0

    def fail_final_artifact_dir_fsync(path: Path) -> None:
        nonlocal artifact_dir_calls
        if path.name == "artifacts":
            artifact_dir_calls += 1
            if artifact_dir_calls == 2:
                raise OSError("simulated directory fsync failure")
        real_fsync(path)

    monkeypatch.setattr(
        evidence_store,
        "_fsync_directory",
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
    real_fsync = evidence_store._fsync_directory

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
    monkeypatch.setattr(evidence_store, "_fsync_directory", record_fsync)

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jasper.active_speaker.commissioning_evidence_store as evidence_store

    store = _open_store(tmp_path)
    observed: list[int] = []
    real_fchown = evidence_store.os.fchown

    def record_fchown(descriptor: int, uid: int, gid: int) -> None:
        observed.append(gid)
        real_fchown(descriptor, uid, gid)

    monkeypatch.setattr(evidence_store.os, "fchown", record_fchown)
    artifact = store.publish_raw_artifact("group-owned.bin", b"owned")

    assert observed == [(store.bundle_dir / artifact.relative_path).parent.stat().st_gid]
    assert (store.bundle_dir / artifact.relative_path).stat().st_gid == observed[0]


def test_total_bound_covers_the_proven_max_capture_matrix() -> None:
    assert MAX_COMMISSIONING_REGIONS == max(
        len(sides) * len(regions)
        for sides in SIDES_BY_LAYOUT.values()
        for regions in ADJACENT_PAIRS_BY_WAY.values()
    )
    assert MAX_SUMMED_CAPTURE_ARTIFACT_COUNT == MAX_COMMISSIONING_REGIONS * (
        (2 * STATIONARY_CAPTURE_COUNT)
        + (MAX_SCHEDULED_CANDIDATES * MIN_CAPTURE_COUNT)
    )
    assert MAX_ISOLATED_DRIVER_TARGETS == max(
        len(sides) * len(roles)
        for sides in SIDES_BY_LAYOUT.values()
        for roles in DRIVER_ROLES_BY_WAY.values()
    )
    assert MAX_ISOLATED_CAPTURE_ARTIFACT_COUNT == (
        MAX_ISOLATED_DRIVER_TARGETS * STATIONARY_CAPTURE_COUNT
    )
    assert MAX_CAPTURE_ARTIFACT_COUNT == (
        MAX_SUMMED_CAPTURE_ARTIFACT_COUNT
        + MAX_ISOLATED_CAPTURE_ARTIFACT_COUNT
    )
    assert MAX_TOTAL_AUTHORITATIVE_EVIDENCE_BYTES >= (
        MAX_CAPTURE_ARTIFACT_COUNT * MAX_EVIDENCE_ARTIFACT_BYTES
    ) + (1024 * 1024 * 1024)
    assert MIN_FREE_SPACE_AFTER_PUBLISH_BYTES >= DEFAULT_SESSIONS_MAX_BYTES
