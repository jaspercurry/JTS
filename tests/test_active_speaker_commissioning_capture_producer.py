# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import io
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml
import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

from jasper.active_speaker import commissioning_capture_producer as producer_module
from jasper.active_speaker import commissioning_runtime as runtime
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.camilla_yaml import emit_active_speaker_baseline_config
from jasper.active_speaker.commissioning_capture_producer import (
    CurrentCaptureAuthority,
    RawCaptureResult,
    SummedCaptureProducer,
    SummedCaptureProducerError,
)
from jasper.active_speaker.commissioning_evidence import (
    AdmittedRegionCapture,
    RegionEvidencePlan,
    active_region_threshold_profile_fingerprint,
    derive_region_evidence_plan,
    evidence_attempt_target_id,
)
from jasper.active_speaker.commissioning_evidence_store import (
    EVIDENCE_ROOT,
    CommissioningEvidenceStore,
)
from jasper.active_speaker.commissioning_host import RegionCaptureOperation
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.driver_acoustics import (
    SUMMED_BLEND_OK,
    SummedAcousticResult,
)
from jasper.active_speaker.driver_safety import (
    build_driver_safety_profile,
    evaluate_driver_safety_profile,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import admitted_playback
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.audio_measurement.playback import PlaybackResult
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_driver_safety import (
    _manual_settings,
    _stereo_manual_settings,
    _stereo_topology,
)
from tests.test_active_speaker_profile import _two_way_preset


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class _Harness:
    topology: OutputTopology
    safety_profile: dict
    calibration: CalibrationCurve
    store: CommissioningEvidenceStore
    plan: RegionEvidencePlan
    operation: RegionCaptureOperation
    baseline_raw: str
    guarded_raw: str

    def producer(self, raw_transport) -> SummedCaptureProducer:
        return SummedCaptureProducer(
            authority=self.plan.authority,
            plan_fingerprint=self.plan.fingerprint,
            topology=self.topology,
            evidence_store=self.store,
            load_current_authority=lambda: CurrentCaptureAuthority(
                safety_profile=self.safety_profile,
                calibration=self.calibration,
            ),
            raw_transport=raw_transport,
            alsa_device="test-output",
            playback_timeout_s=1.0,
        )


def _harness(tmp_path: Path, *, layout: str = "mono") -> _Harness:
    topology = (
        mono_output_topology(mode="active_2_way")
        if layout == "mono"
        else _stereo_topology()
    )
    preset_raw = deepcopy(_two_way_preset(layout=layout))
    preset_raw["crossover_regions"][0]["fc_hz"] = 6_000
    preset = ActiveSpeakerPreset.from_mapping(preset_raw)

    manual = deepcopy(
        _manual_settings() if layout == "mono" else _stereo_manual_settings()
    )
    for driver in manual["drivers"]:
        driver["hard_excitation_band_hz"] = [3_000, 12_000]
        driver["measurement_band_hz"] = [3_000, 12_000]
        driver["crossover_search_band_hz"] = [5_000, 7_000]
        driver["level_duration_limits"]["minimum_cooldown_s"] = 0
        if driver["role"] == "woofer":
            driver["required_protection_filters"][0]["cutoff_hz"] = 7_000
    safety_profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-14T12:00:00Z",
    )
    safety = evaluate_driver_safety_profile(safety_profile, topology)
    assert safety.confirmed_and_current
    assert safety.profile_fingerprint is not None

    info = open_bundle(
        topology,
        calibration_id="producer-test-calibration",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    run_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="1" * 32
    )
    session_fingerprint = _hash("producer-session")
    run = run_store.start(
        session_id=store.session_id,
        session_fingerprint=session_fingerprint,
    )
    plan = derive_region_evidence_plan(
        preset,
        topology,
        run=run,
        protected_safety_profile_fingerprint=safety.profile_fingerprint,
        comparison_set_fingerprint=session_fingerprint,
        threshold_profile_fingerprint=active_region_threshold_profile_fingerprint(),
        context_fingerprint=_hash("context"),
    )
    target = plan.targets[0]
    capture_target = target.target_fingerprint_for("normal")
    attempt = run_store.reserve_attempt(
        run,
        target_id=evidence_attempt_target_id("normal", capture_target),
        target_fingerprint=capture_target,
    )
    by_group_role = {
        (item["speaker_group_id"], item["role"]): item
        for item in active_driver_targets(topology)
    }
    lower = by_group_role[(target.speaker_group_id, target.lower_role)]
    upper = by_group_role[(target.speaker_group_id, target.upper_role)]
    operation = RegionCaptureOperation(
        plan_fingerprint=plan.fingerprint,
        target=target,
        attempt=attempt,
        evidence_kind="normal",
        placement_fingerprint=_hash("placement"),
        driver_target_fingerprints=(
            lower["target_fingerprint"],
            upper["target_fingerprint"],
        ),
        lower_channels=(lower["output_index"],),
        upper_channels=(upper["output_index"],),
        capture_ordinal=0,
        required_capture_count=3,
        issuance_id="2" * 32,
    )
    baseline_raw = emit_active_speaker_baseline_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        baseline_id="producer-test",
    )
    request = runtime.SummedGraphRequest(
        kind="normal",
        normal_active_raw=baseline_raw,
        lower_role=target.lower_role,
        upper_role=target.upper_role,
        lower_channels=operation.lower_channels,
        upper_channels=operation.upper_channels,
        listening_volume_db=-32.0,
        topology_id=topology.topology_id,
        topology_fingerprint=topology_config_fingerprint(topology),
    )
    binding = runtime._topology_binding(request, topology)
    normal = runtime._normal_graph(request, binding)
    guarded = runtime._stationary_candidate(request, normal, binding)
    guarded_raw = runtime._dump_graph(
        guarded, source_header=runtime._source_header(baseline_raw)
    )
    return _Harness(
        topology=topology,
        safety_profile=safety_profile,
        calibration=CalibrationCurve(
            freqs_hz=[20.0, 20_000.0], correction_db=[0.0, 0.0]
        ),
        store=store,
        plan=plan,
        operation=operation,
        baseline_raw=baseline_raw,
        guarded_raw=guarded_raw,
    )


def _context(raw: str) -> runtime.CommissioningLiveContext:
    graph = NormalizedActiveRawIdentity(yaml.safe_load(raw))

    async def fresh_readback() -> runtime.CommissioningFreshReadback:
        return runtime.CommissioningFreshReadback(
            graph=graph,
            active_raw=raw,
            config_path="/tmp/producer-test.yml",
            listening_volume_db=-32.0,
            delay_confirmation=None,
        )

    return runtime.CommissioningLiveContext(
        graph=graph,
        active_raw=raw,
        config_path="/tmp/producer-test.yml",
        listening_volume_db=-32.0,
        delay_confirmation=None,
        fresh_readback=fresh_readback,
    )


async def _fake_playback(source, *, alsa_device: str, timeout_s: float):
    return PlaybackResult(
        wav_path=source.path,
        alsa_device=alsa_device,
        returncode=0,
    )


def _refused_acoustic() -> SummedAcousticResult:
    return SummedAcousticResult(
        verdict=SUMMED_BLEND_OK,
        null_depth_db=2.0,
        crossover_fc_hz=6_000.0,
        observed_mic_dbfs=-30.0,
        mic_clipping=False,
        quality={"failed": True, "issues": ["synthetic_refusal"]},
        expect_null=False,
        calibrated=True,
        snr={"decision_class": "alignment", "verdict": "ok"},
        null_depth_capped=False,
        above_validity_floor=True,
        gating={"applied": True},
        capture_geometry="reference_axis",
    )


def _synthetic_reference_axis_wav(playback: PlaybackResult) -> bytes:
    sample_rate, raw_reference = wavfile.read(playback.wav_path)
    assert sample_rate == 48_000
    reference = raw_reference.astype(np.float64) / float(np.iinfo(raw_reference.dtype).max)
    impulse = np.zeros(64, dtype=np.float64)
    impulse[0] = 1.0
    impulse[4] = 0.2
    response = fftconvolve(reference, impulse)
    rng = np.random.default_rng(17)
    pre_s = 1.0
    ambient_s = 14.0
    tail_s = 0.6
    total = int(round((pre_s + ambient_s + tail_s) * sample_rate)) + len(response)
    captured = rng.normal(0.0, 0.00005, total)
    sweep_start = int(round((pre_s + ambient_s) * sample_rate))
    captured[sweep_start : sweep_start + len(response)] += 0.2 * response
    output = io.BytesIO()
    wavfile.write(output, sample_rate, captured.astype(np.float32))
    return output.getvalue()


def test_threshold_profile_drift_refuses_at_construction(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    stale_authority = replace(
        harness.plan.authority,
        threshold_profile_fingerprint=_hash("stale-threshold-model"),
    )

    with pytest.raises(SummedCaptureProducerError) as raised:
        SummedCaptureProducer(
            authority=stale_authority,
            plan_fingerprint=harness.plan.fingerprint,
            topology=harness.topology,
            evidence_store=harness.store,
            load_current_authority=lambda: CurrentCaptureAuthority(
                harness.safety_profile, harness.calibration
            ),
            raw_transport=lambda play: None,
            alsa_device="test-output",
            playback_timeout_s=1.0,
        )

    assert raised.value.code == "threshold_profile_stale"


@pytest.mark.parametrize(
    ("wav_bytes", "expected_code"),
    [
        (b"", "raw_capture_invalid"),
        (
            b"x" * (producer_module.CROSSOVER_CAPTURE_MAX_WAV_BYTES + 1),
            "raw_capture_too_large",
        ),
    ],
)
def test_raw_capture_bytes_are_bounded(wav_bytes: bytes, expected_code: str) -> None:
    with pytest.raises(SummedCaptureProducerError) as raised:
        RawCaptureResult(wav_bytes, {})
    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_allowed_baseline_is_not_summed_protection(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    transport_called = False

    async def transport(play):
        nonlocal transport_called
        transport_called = True
        raise AssertionError("baseline must refuse before transport")

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation, _context(harness.baseline_raw)
        )

    assert raised.value.code == "generation_refused"
    assert transport_called is False


@pytest.mark.asyncio
async def test_summed_protection_refuses_a_ceiling_above_the_measurement_level(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    graph = yaml.safe_load(harness.guarded_raw)
    graph["devices"]["volume_limit"] = 0.0
    drifted_raw = runtime._dump_graph(
        graph,
        source_header=runtime._source_header(harness.guarded_raw),
    )
    transport_called = False

    async def transport(play):
        nonlocal transport_called
        transport_called = True
        raise AssertionError("an unbounded graph must refuse before transport")

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation,
            _context(drifted_raw),
        )

    assert raised.value.code == "generation_refused"
    assert transport_called is False


@pytest.mark.asyncio
async def test_guarded_graph_must_equal_operation_physical_outputs(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    transport_called = False
    drifted = replace(harness.operation, lower_channels=(2,))

    async def transport(play):
        nonlocal transport_called
        transport_called = True
        raise AssertionError("channel drift must refuse before transport")

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            drifted,
            _context(harness.guarded_raw),
        )

    assert raised.value.code == "generation_refused"
    assert transport_called is False


@pytest.mark.asyncio
async def test_canonical_stereo_grouped_role_filters_are_admitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, layout="stereo")
    monkeypatch.setattr(admitted_playback, "play_verified_wav", _fake_playback)

    async def transport(play):
        playback = await play()
        return RawCaptureResult(
            _synthetic_reference_axis_wav(playback),
            {"fixture": "deterministic_stereo_reference_axis"},
        )

    admitted = await harness.producer(transport).capture(
        harness.operation,
        _context(harness.guarded_raw),
    )

    assert isinstance(admitted.payload, AdmittedRegionCapture)
    assert admitted.payload.speaker_group_id == harness.operation.target.speaker_group_id


@pytest.mark.asyncio
async def test_stereo_cross_role_protection_group_is_refused_before_transport(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, layout="stereo")
    graph = yaml.safe_load(harness.guarded_raw)
    highpass_names = {
        name
        for name, definition in graph["filters"].items()
        if definition.get("type") == "BiquadCombo"
        and definition.get("parameters", {}).get("type")
        == "LinkwitzRileyHighpass"
    }
    upper_index = harness.operation.upper_channels[0]
    step = next(
        item
        for item in graph["pipeline"]
        if upper_index in item.get("channels", [])
        and highpass_names.intersection(item.get("names", []))
    )
    step["channels"] = sorted(
        {*step["channels"], harness.operation.lower_channels[0]}
    )
    drifted_raw = runtime._dump_graph(
        graph,
        source_header=runtime._source_header(harness.guarded_raw),
    )
    transport_called = False

    async def transport(play):
        nonlocal transport_called
        transport_called = True
        raise AssertionError("cross-role protection must refuse before transport")

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation,
            _context(drifted_raw),
        )

    assert raised.value.code == "generation_refused"
    assert transport_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("play_count", [0, 2])
async def test_transport_must_consume_play_exactly_once(
    tmp_path: Path, play_count: int
) -> None:
    harness = _harness(tmp_path)

    async def transport(play):
        if play_count == 0:
            return RawCaptureResult(b"synthetic", {})
        play()
        play()
        raise AssertionError("second play must refuse")

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation, _context(harness.guarded_raw)
        )

    assert raised.value.code == (
        "transport_play_missing" if play_count == 0 else "transport_play_reused"
    )


@pytest.mark.asyncio
async def test_transport_cannot_retain_playback_capability_after_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    retained = []
    playback_calls = 0

    async def fake_playback(source, *, alsa_device: str, timeout_s: float):
        nonlocal playback_calls
        playback_calls += 1
        return PlaybackResult(
            wav_path=source.path,
            alsa_device=alsa_device,
            returncode=0,
        )

    monkeypatch.setattr(admitted_playback, "play_verified_wav", fake_playback)

    async def transport(play):
        retained.append(play)
        return RawCaptureResult(b"synthetic", {})

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation,
            _context(harness.guarded_raw),
        )
    assert raised.value.code == "transport_play_missing"

    with pytest.raises(SummedCaptureProducerError) as late:
        await retained[0]()
    assert late.value.code == "transport_play_expired"
    assert playback_calls == 0


@pytest.mark.asyncio
async def test_actual_analyzer_happy_path_reopens_complete_typed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    monkeypatch.setattr(admitted_playback, "play_verified_wav", _fake_playback)

    async def transport(play):
        playback = await play()
        return RawCaptureResult(
            _synthetic_reference_axis_wav(playback),
            {"fixture": "deterministic_reference_axis"},
        )

    admitted = await harness.producer(transport).capture(
        harness.operation, _context(harness.guarded_raw)
    )

    capture = admitted.payload
    assert isinstance(capture, AdmittedRegionCapture)
    assert capture.capture.raw_artifact.byte_size <= (
        producer_module.CROSSOVER_CAPTURE_MAX_WAV_BYTES
    )
    analysis = harness.store.reopen_json_artifact(
        capture.capture.analysis_input_artifact
    )
    quality = harness.store.reopen_json_artifact(capture.capture.quality_artifact)
    assert analysis["acoustic"]["verdict"] == SUMMED_BLEND_OK
    assert analysis["acoustic"]["gating"]["applied"] is True
    assert analysis["acoustic"]["snr"]["verdict"] == "ok"
    assert analysis["acoustic"]["calibrated"] is True
    assert quality["accepted"] is True
    assert quality["issues"] == []
    for artifact in (
        capture.capture.raw_artifact,
        capture.capture.analysis_input_artifact,
        capture.capture.quality_artifact,
        capture.generation_artifact,
        capture.playback_artifact,
        capture.stimulus.artifact,
    ):
        assert harness.store.reopen_artifact(artifact)
    typed_artifact = harness.store.publish_admitted_region_capture(capture, ordinal=0)
    assert harness.store.reopen_admitted_region_capture(typed_artifact) == capture


@pytest.mark.asyncio
async def test_quality_refusal_retains_replayable_analysis_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    monkeypatch.setattr(admitted_playback, "play_verified_wav", _fake_playback)
    observed_thresholds: list[float] = []

    def analyze(*args, **kwargs):
        observed_thresholds.append(kwargs["null_threshold_db"])
        return _refused_acoustic()

    monkeypatch.setattr(producer_module, "analyze_summed_crossover", analyze)

    async def transport(play):
        await play()
        return RawCaptureResult(b"synthetic-admitted-wav", {"fixture": "quality"})

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation, _context(harness.guarded_raw)
        )

    assert raised.value.code == "capture_quality_refused"
    assert observed_thresholds == [producer_module.DRIVER.null_threshold_db]
    prefix = (
        f"{EVIDENCE_ROOT}/artifacts/captures/"
        f"{harness.operation.attempt.attempt_id}/{harness.operation.issuance_id}/0000"
    )
    analysis = harness.store.reopen_json_artifact(
        harness.store.identify_artifact(f"{prefix}/analysis.json")
    )
    quality = harness.store.reopen_json_artifact(
        harness.store.identify_artifact(f"{prefix}/quality.json")
    )
    assert analysis["raw_artifact"]["relative_path"].endswith("/raw.wav")
    assert analysis["null_threshold_db"] == producer_module.DRIVER.null_threshold_db
    assert quality["analysis_artifact_fingerprint"] == harness.store.identify_artifact(
        f"{prefix}/analysis.json"
    ).fingerprint
    assert quality["accepted"] is False
    assert quality["issues"] == ["capture_quality_failed"]


@pytest.mark.asyncio
async def test_never_returning_transport_is_cancelled_at_code_owned_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    cancelled = False
    monkeypatch.setattr(
        producer_module, "CROSSOVER_CAPTURE_PLAY_DEADLINE_S", 0.01
    )

    async def transport(play):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    with pytest.raises(SummedCaptureProducerError) as raised:
        await harness.producer(transport).capture(
            harness.operation, _context(harness.guarded_raw)
        )

    assert raised.value.code == "capture_deadline_exceeded"
    assert cancelled is True
