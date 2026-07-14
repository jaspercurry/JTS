# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import commissioning_delay_walk as delay_walk
from jasper.active_speaker.alignment_walk import driver_delay_walk_spec
from jasper.active_speaker.commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunHandle,
)
from jasper.audio_measurement.delay_graph import (
    DelayCandidateConfirmation,
    DelayGraphProofError,
    DelayGraphSnapshot,
    DelayLaneBinding,
)
from jasper.audio_measurement.null_walk import (
    DspPredecessor,
    DspRestoreConfirmation,
    DelayCandidate,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.dsp_apply import DspWriterLockTimeout, dsp_writer_lock

POSITIVE_DELAY = "active_upper_delay"
NEGATIVE_DELAY = "active_lower_delay"
POSITIVE_IDENTITY = "active_upper_crossover"
NEGATIVE_IDENTITY = "active_lower_crossover"
SESSION_ID = "session-1"
SESSION_FINGERPRINT = "a" * 64
RUN_ID = "1" * 32
OWNER_ID = "2" * 32
ATTEMPT_ID = "3" * 32
TARGET_ID = "group-a:upper-lower"
TARGET_FINGERPRINT = "b" * 64
SAFETY_FINGERPRINT = "c" * 64
THRESHOLD_FINGERPRINT = "d" * 64
PLACEMENT_FINGERPRINT = "e" * 64
PLAN_FINGERPRINT = "f" * 64
REQUIREMENT_FINGERPRINT = "9" * 64
EVIDENCE_FINGERPRINT = "8" * 64


def _attempt() -> CommissioningAttemptHandle:
    return CommissioningAttemptHandle(
        run=CommissioningRunHandle(
            session_id=SESSION_ID,
            session_fingerprint=SESSION_FINGERPRINT,
            run_id=RUN_ID,
            owner_id=OWNER_ID,
            owner_generation=1,
        ),
        attempt_id=ATTEMPT_ID,
        attempt_number=1,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )


def _context(
    *,
    region_id: str = "woofer-tweeter",
    crossover_fc_hz: float = 5000.0,
) -> delay_walk.ActiveDelayWalkContext:
    return delay_walk.ActiveDelayWalkContext(
        attempt=_attempt(),
        topology_id="active-topology",
        speaker_group_id="group-a",
        region_id=region_id,
        crossover_fc_hz=crossover_fc_hz,
        safety_profile_fingerprint=SAFETY_FINGERPRINT,
        threshold_profile_fingerprint=THRESHOLD_FINGERPRINT,
        placement_fingerprint=PLACEMENT_FINGERPRINT,
    )


def _typed_admission(
    context: delay_walk.ActiveDelayWalkContext,
    *,
    target_fingerprint: str | None = None,
    safety_profile_fingerprint: str | None = None,
    refused: bool = False,
) -> dict[str, object]:
    target = target_fingerprint or context.attempt.target_fingerprint
    safety = safety_profile_fingerprint or context.safety_profile_fingerprint
    limits = ExcitationLimits(
        permitted_band=FrequencyBand(100.0, 10_000.0),
        maximum_effective_peak_dbfs=-10.0,
        maximum_duration_s=2.0,
        maximum_repeat_count=1,
        target_fingerprint=target,
        safety_profile_fingerprint=safety,
        protection_requirement_fingerprint=REQUIREMENT_FINGERPRINT,
        excitation_plan_fingerprint=PLAN_FINGERPRINT,
    )
    request = ExcitationRequest(
        band=FrequencyBand(100.0, 10_000.0),
        effective_peak_dbfs=-5.0 if refused else -20.0,
        duration_s=1.0,
        repeat_count=1,
        target_fingerprint=target,
        safety_profile_fingerprint=safety,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=PLAN_FINGERPRINT,
    )
    evidence = ProtectionEvidence(
        target_fingerprint=target,
        safety_profile_fingerprint=safety,
        protection_requirement_fingerprint=REQUIREMENT_FINGERPRINT,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=PLAN_FINGERPRINT,
        evidence_fingerprint=EVIDENCE_FINGERPRINT,
        current=True,
    )
    return admit_excitation(
        request,
        limits,
        protection_evidence=evidence,
    ).to_dict()


def _graph() -> dict[str, Any]:
    return {
        "devices": {
            "samplerate": 48_000,
            "chunksize": 1024,
            "volume_limit": -12.0,
            "capture": {"type": "Alsa", "device": "capture"},
            "playback": {"type": "Alsa", "device": "playback"},
        },
        "filters": {
            POSITIVE_DELAY: {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            NEGATIVE_DELAY: {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            POSITIVE_IDENTITY: {
                "type": "BiquadCombo",
                "parameters": {"type": "LinkwitzRileyHighpass", "freq": 5000.0},
            },
            NEGATIVE_IDENTITY: {
                "type": "BiquadCombo",
                "parameters": {"type": "LinkwitzRileyLowpass", "freq": 5000.0},
            },
        },
        "mixers": {
            "route": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": 0,
                        "sources": [{"channel": 0, "gain": 0.0, "inverted": False}],
                    }
                ],
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "route"},
            {
                "type": "Filter",
                "channels": [0],
                "names": [POSITIVE_IDENTITY, POSITIVE_DELAY],
            },
            {
                "type": "Filter",
                "channels": [1],
                "names": [NEGATIVE_IDENTITY, NEGATIVE_DELAY],
            },
        ],
    }


def _spec(*, step_us: float = 100.0):
    return driver_delay_walk_spec(
        crossover_fc_hz=5000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0,
        step_us=step_us,
    )


def _lanes() -> tuple[DelayLaneBinding, DelayLaneBinding]:
    return (
        DelayLaneBinding("upper", POSITIVE_DELAY, POSITIVE_IDENTITY, (0,)),
        DelayLaneBinding("lower", NEGATIVE_DELAY, NEGATIVE_IDENTITY, (1,)),
    )


class _Harness:
    def __init__(
        self,
        *,
        context: delay_walk.ActiveDelayWalkContext | None = None,
        locked=lambda: True,
    ) -> None:
        self.context = context or _context()
        self.entry_graph = _graph()
        self.current_graph = copy.deepcopy(self.entry_graph)
        self.locked = locked
        self.read_count = 0
        self.read_ids: list[str] = []
        self.applied: list[float] = []
        self.confirmations: list[DelayCandidateConfirmation] = []
        self.capture_ids: list[str] = []
        self.restored: list[DspPredecessor] = []
        self.candidate_read_topology = self.context.topology_id
        self.candidate_read_fc = self.context.crossover_fc_hz
        self.replay_read_id = False
        self.drift_graph = False
        self.capture_mode = "good"
        self.capture_overrides: dict[str, object] = {}
        self.current = True
        self.current_checks = 0
        self.stale_after_apply = False

    async def attempt_is_current(self, attempt: CommissioningAttemptHandle) -> bool:
        assert attempt == self.context.attempt
        self.current_checks += 1
        return self.current

    async def read_live_graph(
        self, candidate: DelayCandidate | None
    ) -> delay_walk.ActiveDelayLiveGraph:
        assert self.locked()
        self.read_count += 1
        readback_id = (
            "read-1"
            if self.replay_read_id and candidate is not None
            else f"read-{self.read_count}"
        )
        self.read_ids.append(readback_id)
        topology_id = (
            self.context.topology_id
            if candidate is None
            else self.candidate_read_topology
        )
        crossover_fc_hz = (
            self.context.crossover_fc_hz
            if candidate is None
            else self.candidate_read_fc
        )
        graph = copy.deepcopy(self.current_graph)
        if self.drift_graph and candidate is not None:
            graph["devices"]["chunksize"] = 2048
        return delay_walk.ActiveDelayLiveGraph(
            readback_id=readback_id,
            topology_id=topology_id,
            crossover_fc_hz=crossover_fc_hz,
            graph=graph,
        )

    async def build_and_load_candidate(
        self,
        snapshot: DelayGraphSnapshot,
        candidate: DelayCandidate,
    ) -> bool:
        assert self.locked()
        graph = snapshot.graph
        if candidate.delay_target == "upper":
            graph["filters"][POSITIVE_DELAY]["parameters"]["delay"] = (
                candidate.delay_us / 1000.0
            )
        elif candidate.delay_target == "lower":
            graph["filters"][NEGATIVE_DELAY]["parameters"]["delay"] = (
                candidate.delay_us / 1000.0
            )
        self.current_graph = graph
        self.applied.append(candidate.relative_delay_us)
        if self.stale_after_apply:
            self.current = False
        return True

    async def capture(
        self,
        candidate: DelayCandidate,
        index: int,
        confirmation: DelayCandidateConfirmation,
    ) -> dict[str, Any] | None:
        assert self.locked()
        self.confirmations.append(confirmation)
        token = f"{candidate.relative_delay_us:g}-{index}"
        capture_id = f"capture-{token}"
        admission_id = f"admission-{token}"
        null_id = f"null-{token}"
        if self.capture_mode == "none":
            return None
        if self.capture_mode == "duplicate_capture" and self.capture_ids:
            capture_id = self.capture_ids[0]
        if self.capture_mode == "duplicate_admission" and self.capture_ids:
            admission_id = "admission--100-0"
        if self.capture_mode == "duplicate_null" and self.capture_ids:
            null_id = "null--100-0"
        self.capture_ids.append(capture_id)
        capture = {
            "session_id": self.context.attempt.run.session_id,
            "run_id": self.context.attempt.run.run_id,
            "attempt_id": self.context.attempt.attempt_id,
            "target_id": self.context.attempt.target_id,
            "target_fingerprint": self.context.attempt.target_fingerprint,
            "context_fingerprint": self.context.fingerprint,
            "topology_id": self.context.topology_id,
            "speaker_group_id": self.context.speaker_group_id,
            "region_id": self.context.region_id,
            "crossover_fc_hz": self.context.crossover_fc_hz,
            "threshold_profile_fingerprint": (
                self.context.threshold_profile_fingerprint
            ),
            "safety_profile_fingerprint": self.context.safety_profile_fingerprint,
            "placement_fingerprint": self.context.placement_fingerprint,
            "capture_id": capture_id,
            "capture_admission": {
                "admission_id": admission_id,
                "admission": _typed_admission(self.context),
            },
            "quality": {"accepted": True},
            "null_identity": {
                "null_id": null_id,
                "candidate_fingerprint": confirmation.candidate_fingerprint,
                "snapshot_fingerprint": confirmation.snapshot_fingerprint,
                "relative_delay_us": candidate.relative_delay_us,
                "expect_null": True,
            },
            "acoustic": {
                "null_depth_db": (
                    20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
                ),
                "null_depth_capped": False,
                "mic_clipping": False,
                "calibrated": True,
                "expect_null": True,
                "crossover_fc_hz": 5000.0,
                "gating": {"applied": True},
                "above_validity_floor": True,
                "snr": {"decision_class": "alignment", "verdict": "ok"},
            },
        }
        capture.update(self.capture_overrides)
        if self.capture_mode == "admission":
            capture["capture_admission"]["admission"] = _typed_admission(
                self.context,
                refused=True,
            )
        elif self.capture_mode == "admission_target":
            capture["capture_admission"]["admission"] = _typed_admission(
                self.context,
                target_fingerprint="7" * 64,
            )
        elif self.capture_mode == "admission_safety":
            capture["capture_admission"]["admission"] = _typed_admission(
                self.context,
                safety_profile_fingerprint="6" * 64,
            )
        elif self.capture_mode == "quality":
            capture["quality"]["accepted"] = False
        elif self.capture_mode == "snr":
            capture["acoustic"]["snr"]["verdict"] = "reduced"
        elif self.capture_mode == "null_identity":
            capture["null_identity"]["expect_null"] = False
        return capture

    async def restore(self, predecessor: DspPredecessor) -> DspRestoreConfirmation:
        assert self.locked()
        self.restored.append(predecessor)
        self.current_graph = copy.deepcopy(predecessor.state["active_raw"])
        return DspRestoreConfirmation(predecessor.state)


async def _run(
    tmp_path: Path,
    harness: _Harness,
    *,
    step_us: float = 100.0,
    lock_timeout_s: float = 1.0,
):
    positive, negative = _lanes()
    return await delay_walk.run_active_delay_walk(
        _spec(step_us=step_us),
        config_dir=tmp_path,
        context=harness.context,
        positive_lane=positive,
        negative_lane=negative,
        attempt_is_current=harness.attempt_is_current,
        read_live_graph=harness.read_live_graph,
        build_and_load_candidate=harness.build_and_load_candidate,
        capture_admitted_null=harness.capture,
        restore_predecessor=harness.restore,
        lock_timeout_s=lock_timeout_s,
    )


def test_context_fingerprint_is_deterministic_and_binds_complete_attempt() -> None:
    first = _context()
    second = _context()

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.to_dict() == {
        "schema_version": 1,
        "kind": "jts_active_delay_walk_context",
        "session_id": SESSION_ID,
        "session_fingerprint": SESSION_FINGERPRINT,
        "run_id": RUN_ID,
        "owner_id": OWNER_ID,
        "owner_generation": 1,
        "attempt_id": ATTEMPT_ID,
        "attempt_number": 1,
        "target_id": TARGET_ID,
        "target_fingerprint": TARGET_FINGERPRINT,
        "topology_id": "active-topology",
        "speaker_group_id": "group-a",
        "region_id": "woofer-tweeter",
        "crossover_fc_hz": 5000.0,
        "safety_profile_fingerprint": SAFETY_FINGERPRINT,
        "threshold_profile_fingerprint": THRESHOLD_FINGERPRINT,
        "placement_fingerprint": PLACEMENT_FINGERPRINT,
        "fingerprint": first.fingerprint,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_us", "expected_candidates"),
    [(50.0, [-100.0, -50.0, 0.0, 50.0, 100.0]), (100.0, [-100.0, 0.0, 100.0])],
)
async def test_walk_holds_one_lock_across_fresh_reads_five_captures_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_us: float,
    expected_candidates: list[float],
) -> None:
    state = {"held": False, "entries": 0}

    @asynccontextmanager
    async def tracked_lock(_path, *, source, timeout_s):
        assert source == "active_speaker_delay_walk"
        assert timeout_s == 1.0
        assert state["held"] is False
        state["entries"] += 1
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    monkeypatch.setattr(delay_walk, "dsp_writer_lock", tracked_lock)
    harness = _Harness(locked=lambda: state["held"])

    result = await _run(tmp_path, harness, step_us=step_us)

    assert result["status"] == "selected"
    assert result["selected_relative_delay_us"] == 0.0
    assert harness.applied == expected_candidates
    assert harness.read_count == len(expected_candidates) + 1
    assert len(set(harness.read_ids)) == len(harness.read_ids)
    assert len(harness.capture_ids) == len(expected_candidates) * 5
    assert len(set(harness.capture_ids)) == len(harness.capture_ids)
    for candidate_index in range(len(expected_candidates)):
        confirmations = harness.confirmations[
            candidate_index * 5 : (candidate_index + 1) * 5
        ]
        assert len(confirmations) == 5
        assert all(item is confirmations[0] for item in confirmations)
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph
    assert state == {"held": False, "entries": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("none", "capture_invalid"),
        ("admission", "capture_admission_refused"),
        ("quality", "capture_quality_refused"),
        ("snr", "capture_snr_refused"),
        ("null_identity", "capture_identity_invalid"),
    ],
)
async def test_walk_fails_closed_on_missing_or_false_capture_authority(
    tmp_path: Path,
    mode: str,
    code: str,
) -> None:
    harness = _Harness()
    harness.capture_mode = mode

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == code
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("session_id", "stale-session"),
        ("run_id", "4" * 32),
        ("attempt_id", "5" * 32),
        ("target_id", "group-a:other"),
        ("target_fingerprint", "0" * 64),
        ("context_fingerprint", "1" * 64),
        ("topology_id", "stale-topology"),
        ("speaker_group_id", "group-b"),
        ("region_id", "mid-tweeter"),
        ("crossover_fc_hz", 4999.0),
        ("threshold_profile_fingerprint", "2" * 64),
        ("safety_profile_fingerprint", "3" * 64),
        ("placement_fingerprint", "4" * 64),
    ],
)
async def test_walk_refuses_every_stale_capture_correlation_dimension(
    tmp_path: Path,
    field: str,
    stale_value: object,
) -> None:
    harness = _Harness()
    harness.capture_overrides[field] = stale_value

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "capture_correlation_mismatch"
    assert harness.applied == [-100.0]
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["admission_target", "admission_safety"])
async def test_walk_refuses_typed_admission_for_other_target_or_safety_profile(
    tmp_path: Path,
    mode: str,
) -> None:
    harness = _Harness()
    harness.capture_mode = mode

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "capture_admission_refused"
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph


@pytest.mark.asyncio
async def test_two_regions_in_one_topology_cannot_alias_capture_authority(
    tmp_path: Path,
) -> None:
    lower = _context(region_id="woofer-mid", crossover_fc_hz=500.0)
    upper = _context(region_id="mid-tweeter", crossover_fc_hz=5000.0)
    assert lower.topology_id == upper.topology_id
    assert lower.speaker_group_id == upper.speaker_group_id
    assert lower.attempt == upper.attempt
    assert lower.fingerprint != upper.fingerprint

    harness = _Harness(context=upper)
    harness.capture_overrides.update(
        {
            "context_fingerprint": lower.fingerprint,
            "region_id": lower.region_id,
            "crossover_fc_hz": lower.crossover_fc_hz,
        }
    )
    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "capture_correlation_mismatch"
    assert len(harness.restored) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["duplicate_capture", "duplicate_admission", "duplicate_null"]
)
async def test_walk_rejects_duplicate_capture_authority(
    tmp_path: Path,
    mode: str,
) -> None:
    harness = _Harness()
    harness.capture_mode = mode

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "capture_duplicate"
    assert len(harness.restored) == 1


@pytest.mark.asyncio
async def test_walk_refuses_candidate_readback_drift_and_restores(
    tmp_path: Path,
) -> None:
    harness = _Harness()
    harness.drift_graph = True

    with pytest.raises(DelayGraphProofError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "graph_mismatch"
    assert harness.applied == [-100.0]
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph


@pytest.mark.asyncio
async def test_walk_refuses_replayed_live_readback_identity_and_restores(
    tmp_path: Path,
) -> None:
    harness = _Harness()
    harness.replay_read_id = True

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "live_readback_duplicate"
    assert len(harness.restored) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("topology", "replacement-topology"), ("fc", 4000.0)],
)
async def test_walk_refuses_stale_candidate_context_and_restores(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    harness = _Harness()
    if field == "topology":
        harness.candidate_read_topology = str(value)
    else:
        harness.candidate_read_fc = float(value)

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "stale_context"
    assert harness.applied == [-100.0]
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph


@pytest.mark.asyncio
async def test_stale_attempt_is_refused_before_writer_lock_or_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = False

    @asynccontextmanager
    async def observed_lock(_path, *, source, timeout_s):
        nonlocal entered
        entered = True
        yield

    monkeypatch.setattr(delay_walk, "dsp_writer_lock", observed_lock)
    harness = _Harness()
    harness.current = False

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "attempt_stale"
    assert entered is False
    assert harness.read_count == 0
    assert harness.applied == []
    assert harness.restored == []


@pytest.mark.asyncio
async def test_attempt_replaced_after_candidate_load_restores_before_refusal(
    tmp_path: Path,
) -> None:
    harness = _Harness()
    harness.stale_after_apply = True

    with pytest.raises(delay_walk.ActiveDelayWalkError) as caught:
        await _run(tmp_path, harness)

    assert caught.value.code == "attempt_stale"
    assert harness.applied == [-100.0]
    assert len(harness.restored) == 1
    assert harness.current_graph == harness.entry_graph
    assert harness.capture_ids == []


@pytest.mark.asyncio
async def test_cancellation_drains_late_candidate_apply_before_exact_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = False

    @asynccontextmanager
    async def tracked_lock(_path, *, source, timeout_s):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(delay_walk, "dsp_writer_lock", tracked_lock)
    harness = _Harness(locked=lambda: held)
    apply_started = asyncio.Event()
    release_apply = asyncio.Event()
    order: list[str] = []

    async def late_apply(snapshot, candidate):
        assert held
        apply_started.set()
        await release_apply.wait()
        await harness.build_and_load_candidate(snapshot, candidate)
        order.append("apply_finished")
        return True

    async def exact_restore(predecessor):
        assert held
        order.append("restored")
        return await harness.restore(predecessor)

    positive, negative = _lanes()
    task = asyncio.create_task(
        delay_walk.run_active_delay_walk(
            _spec(),
            config_dir=tmp_path,
            context=harness.context,
            positive_lane=positive,
            negative_lane=negative,
            attempt_is_current=harness.attempt_is_current,
            read_live_graph=harness.read_live_graph,
            build_and_load_candidate=late_apply,
            capture_admitted_null=harness.capture,
            restore_predecessor=exact_restore,
        )
    )
    await apply_started.wait()
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert order == []
    assert held is True

    release_apply.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["apply_finished", "restored"]
    assert harness.current_graph == harness.entry_graph
    assert held is False


@pytest.mark.asyncio
async def test_writer_timeout_mutates_nothing_and_emits_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="jasper.active_speaker.commissioning_delay_walk",
    )
    harness = _Harness()

    async with dsp_writer_lock(tmp_path, source="test_holder", timeout_s=1.0):
        with pytest.raises(DspWriterLockTimeout):
            await _run(tmp_path, harness, lock_timeout_s=0.01)

    assert harness.read_count == 0
    assert harness.applied == []
    assert harness.capture_ids == []
    assert harness.restored == []
    messages = [record.getMessage() for record in caplog.records]
    assert (
        sum("event=correction.crossover_delay_walk_started" in m for m in messages) == 1
    )
    assert (
        sum("event=correction.crossover_delay_walk_failed" in m for m in messages) == 1
    )
    assert any("failure_code=writer_lock_timeout" in m for m in messages)


@pytest.mark.asyncio
async def test_active_events_are_transition_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="jasper.active_speaker.commissioning_delay_walk",
    )

    await _run(tmp_path, _Harness())

    active_events = [
        record.getMessage()
        for record in caplog.records
        if "event=correction.crossover_delay_walk_" in record.getMessage()
    ]
    assert sum("_started" in event for event in active_events) == 1
    assert sum("_completed" in event for event in active_events) == 1
    assert all(f"session={SESSION_ID}" in event for event in active_events)
    assert all(f"run_id={RUN_ID}" in event for event in active_events)
    assert all(f"attempt_id={ATTEMPT_ID}" in event for event in active_events)
    assert all("group=group-a" in event for event in active_events)
    assert all("region=woofer-tweeter" in event for event in active_events)
    assert all("capture_id=" not in event for event in active_events)
    assert all("candidate_fingerprint=" not in event for event in active_events)
