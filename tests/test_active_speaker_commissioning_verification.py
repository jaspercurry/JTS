# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from jasper.active_speaker import commissioning_capture_producer as capture_module
from jasper.active_speaker.commissioning_capture_producer import (
    SummedCaptureProducer,
)
from jasper.active_speaker.bundles import BUNDLE_KIND
from jasper.active_speaker.commissioning_receipt import AdmittedCaptureProof
from jasper.active_speaker.commissioning_run import (
    CommissioningRunConflict,
    CommissioningRunStore,
)
from jasper.active_speaker.commissioning_verification import (
    read_commissioning_room_authority,
)
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    CaptureIdentity,
)
from jasper.audio_measurement.excitation_artifacts import canonical_admission_bytes
from tests.test_active_speaker_commissioning_apply import _apply
from tests.test_active_speaker_commissioning_receipt import _admission


def _direct_artifact(
    bundle_dir: Path,
    *,
    bundle_kind: str,
    bundle_id: str,
    relative_path: str,
    payload: bytes,
) -> ArtifactIdentity:
    path = bundle_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactIdentity(
        bundle_kind=bundle_kind,
        bundle_id=bundle_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )


@pytest.mark.asyncio
async def test_three_current_graph_captures_issue_receipt_without_graph_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _candidate, _result, state, _previous, port, _load = await _apply(
        tmp_path, monkeypatch
    )
    initial_state = dict(state)
    store = harness.evidence_store
    counter = 0
    monkeypatch.setattr(
        capture_module,
        "active_region_threshold_profile_fingerprint",
        lambda: harness.plan.authority.threshold_profile_fingerprint,
    )

    async def capture_post_apply(self, operation, context):
        nonlocal counter
        counter += 1
        capture_id = f"post-apply-{counter}-{uuid.uuid4().hex}"
        admission = _admission(
            operation.target_fingerprint,
            safety_profile_fingerprint=(
                harness.plan.authority.protected_safety_profile_fingerprint
            ),
            evidence_fingerprint=f"{counter:x}" * 64,
        )
        generation = _direct_artifact(
            store.bundle_dir,
            bundle_kind=BUNDLE_KIND,
            bundle_id=store.session_id,
            relative_path=f"admission/v1/generation/{capture_id}.json",
            payload=canonical_admission_bytes(admission),
        )
        playback = _direct_artifact(
            store.bundle_dir,
            bundle_kind=BUNDLE_KIND,
            bundle_id=store.session_id,
            relative_path=f"admission/v1/playback/{capture_id}.json",
            payload=canonical_admission_bytes(admission),
        )
        raw = store.publish_raw_artifact(
            f"post-apply-test/{capture_id}/raw.wav", f"raw-{counter}".encode()
        )
        analysis = store.publish_raw_artifact(
            f"post-apply-test/{capture_id}/analysis.json",
            f"analysis-{counter}".encode(),
        )
        quality = store.publish_raw_artifact(
            f"post-apply-test/{capture_id}/quality.json",
            f"quality-{counter}".encode(),
        )
        capture = CaptureIdentity(
            consumer_id="active_crossover",
            measurement_kind="active_crossover_post_apply",
            capture_id=capture_id,
            raw_artifact=raw,
            analysis_input_artifact=analysis,
            target_fingerprint=operation.target_fingerprint,
            context_fingerprint=operation.commissioning_context_fingerprint,
            geometry_id="reference_axis",
            placement_fingerprint=operation.placement_fingerprint,
            quality_artifact=quality,
            admission_artifact=playback,
        )
        proof = AdmittedCaptureProof(
            capture=capture,
            commissioning_session_id=store.session_id,
            generation_admission=admission,
            admission=admission,
            generation_artifact=generation,
        )
        return type("CaptureResult", (), {"payload": proof})()

    monkeypatch.setattr(
        SummedCaptureProducer,
        "capture_post_apply",
        capture_post_apply,
    )

    results = []
    for _index in range(2):
        results.append(
            await harness.service.capture_post_apply(
                port,
                raw_capture_transport=lambda _play: None,
                config_dir=str(tmp_path),
            )
        )

    original_transition = harness.run_store.transition

    def competing_status_wins_transition(*args, **kwargs):
        assert original_transition(*args, **kwargs) is True
        raise CommissioningRunConflict("another status request finalized the receipt")

    monkeypatch.setattr(
        harness.run_store,
        "transition",
        competing_status_wins_transition,
    )
    results.append(
        await harness.service.capture_post_apply(
            port,
            raw_capture_transport=lambda _play: None,
            config_dir=str(tmp_path),
        )
    )

    assert [item["capture_ordinal"] for item in results] == [1, 2, 3]
    assert results[-1]["status"] == "verified"
    assert results[-1]["receipt"]["fingerprint"]
    assert harness.run_store.lifecycle_state(harness.plan.authority.run) == "verified"
    assert state == initial_state
    assert counter == 3

    room = read_commissioning_room_authority(
        harness.authority.topology,
        run_state_path=harness.run_store.path,
        sessions_root=tmp_path / "sessions",
    )
    assert room["allowed"] is True
    assert room["authority"] == "automatic_verified_receipt"
    assert room["receipt_fingerprint"] == results[-1]["receipt"]["fingerprint"]

    restarted_store = CommissioningRunStore(
        path=harness.run_store.path,
        owner_id="8" * 32,
    )
    claimed = restarted_store.claim_owner()
    assert claimed is not None
    assert claimed.owner_generation == harness.plan.authority.run.owner_generation + 1

    after_restart = read_commissioning_room_authority(
        harness.authority.topology,
        run_state_path=restarted_store.path,
        sessions_root=tmp_path / "sessions",
    )
    assert after_restart == room
