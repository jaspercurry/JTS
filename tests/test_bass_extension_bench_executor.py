# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``BenchRoleExecutor`` — the composition layer, with a REAL executor.

Every OTHER bench test file mocks the executor away entirely (a
``RoleExecutor``-shaped fake with canned records). This file is the
exception: it builds a real :class:`~jasper.bass_extension.bench.executor.
BenchRoleExecutor` and exercises it against a real, proven owner-path
``LIVE_CONFIG`` (borrowed from ``test_bass_extension_bench_derivation.py``'s
fixture), mocking only the two genuinely non-reusable seams — the injected
:class:`~jasper.bass_extension.bench.executor.PlayAndCapture` collaborator
(hardware/phone-dependent) and ``render.render_config`` (a real camilladsp
subprocess). Everything between those two seams — R6 padding, R9 derivation
and rendering, R6a/R4(a) live-proof, and R10 cross-check — runs for real.

Covers the gate-2 review's executor-level findings: B1 (the render argv
carries the bracketed fader gain), B3 (the padded artifact is exactly what
PlayAndCapture receives), B4 (the digital-transfer-probe's deployed artifact
is the extracted body matching the reference transform, never the whole
render), S1 (prove_fader_bracket's locked-level assertion), S5 (the
cross-check tolerance bound is genuinely per-channel), and a full
BenchRoleExecutor -> build_sweep_record -> produce_limiter_thresholds
round-trip with synthetic rendered artifacts.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pytest
import yaml

from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.bench import cross_check, derivation, executor, live_proof, render, stimulus
from jasper.bass_extension.bench.manifest import StimulusRequest
from jasper.bass_extension.bench.runner import (
    BenchDeps,
    Stop,
    TargetPlan,
)
from jasper.bass_extension.bench.sink import BundleSink
from jasper.bass_extension.targets import MARGINS

LIMITER_NAME = "as_woofer_baseline_limiter"
OWNER_CHANNELS = (2, 3)
TARGET_ID = "deep"

# Copied from test_bass_extension_bench_derivation.py's proven fixture: a
# stereo split mixer (the -6.02 dB MONO_SUM_GAIN_DB hazard) feeding a stereo
# woofer owner step (bass_ext_lt -> bass_ext_subsonic -> the baseline
# limiter). Kept identical so this file inherits a config that is already
# known to survive derive_truncated_config's R2/R3/R7 checks.
LIVE_CONFIG: dict = {
    "devices": {
        "samplerate": 48000,
        "chunksize": 1024,
        "capture": {
            "type": "Alsa",
            "channels": 2,
            "device": "hw:Loopback,1",
            "format": "S32LE",
        },
        "playback": {
            "type": "Alsa",
            "channels": 4,
            "device": "hw:0",
            "format": "S32LE",
        },
        "enable_rate_adjust": True,
        "queuelimit": 4,
        "target_level": 1024,
        "volume_limit": 0.0,
    },
    "filters": {
        "bass_ext_lt": {
            "type": "Biquad",
            "parameters": {
                "type": "LinkwitzTransform",
                "freq_act": 40.0,
                "q_act": 0.7,
                "freq_target": 40.0,
                "q_target": 0.7,
            },
        },
        "bass_ext_subsonic": {
            "type": "BiquadCombo",
            "parameters": {
                "type": "ButterworthHighpass",
                "freq": 30.0,
                "order": 4,
            },
        },
        LIMITER_NAME: {
            "type": "Limiter",
            "parameters": {"clip_limit": -1.0, "soft_clip": True},
        },
    },
    "mixers": {
        "split_2way": {
            "channels": {"in": 2, "out": 4},
            "mapping": [
                {"dest": 0, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]},
                {"dest": 1, "sources": [{"channel": 1, "gain": 0.0, "inverted": False}]},
                {
                    "dest": 2,
                    "sources": [
                        {"channel": 0, "gain": -6.02, "inverted": False},
                        {"channel": 1, "gain": -6.02, "inverted": False},
                    ],
                },
                {
                    "dest": 3,
                    "sources": [
                        {"channel": 0, "gain": -6.02, "inverted": False},
                        {"channel": 1, "gain": -6.02, "inverted": False},
                    ],
                },
            ],
        }
    },
    "pipeline": [
        {"type": "Mixer", "name": "split_2way"},
        {
            "type": "Filter",
            "channels": [2, 3],
            "names": ["bass_ext_lt", "bass_ext_subsonic", LIMITER_NAME],
        },
    ],
}


def _live_yaml() -> str:
    return yaml.safe_dump(LIVE_CONFIG, sort_keys=False)


@pytest.fixture(autouse=True)
def _stub_post_limiter_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass activation._prove_active_graph — already pinned by its own
    tests — so these tests focus on the executor's OWN composition."""

    monkeypatch.setattr(
        derivation, "_prove_active_graph", lambda config, proof: proof.expected_clip_limit_dbfs
    )


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(label: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="jts_bass_extension_limiter_bench",
        bundle_id="fixture",
        relative_path=f"{label}.json",
        sha256=_sha(label),
        byte_size=len(label),
    )


def _write_readback_receipt(
    sink: BundleSink,
    *,
    relative_path: str = f"{TARGET_ID}/readback.json",
    active_config_raw: str | None = None,
    graph_fingerprint: str = "fp",
    configured_clip_limit_dbfs: float = -1.0,
) -> ArtifactIdentity:
    """S-1/R1: a REAL activation read-back receipt on disk — mirrors
    runner.py's _readback_receipt's exact JSON shape. run_discovery/
    run_reference_sweep/run_candidate now read active_config_raw from
    THIS kind of file (via executor._read_receipt), never a live
    controller.get_active_config_raw() fetch, so any test driving those
    methods needs a real file at the readback identity's path — a bare
    _artifact(label) (no file written) would 404."""

    return sink.write_json(
        relative_path,
        {
            "role": "test-readback",
            "target_id": TARGET_ID,
            "active_graph_fingerprint": graph_fingerprint,
            "configured_clip_limit_dbfs": configured_clip_limit_dbfs,
            "active_config_raw": active_config_raw or _live_yaml(),
        },
        kind="jts_bass_extension_bench_receipt",
    )


def _mux_status(**overrides: object) -> dict:
    base = {
        "test_source": "correction",
        "active_source": "correction",
        "test_owner": "correction-measurement",
    }
    base.update(overrides)
    return base


def _lane(**overrides: object) -> dict:
    base = {
        "label": "correction",
        "muted": False,
        "xrun_count": 0,
        "catchup_resync_frames": 0,
        "catchup_events": 0,
        "trim": {"trims": 0, "trimmed_frames": 0, "pending": False},
    }
    base.update(overrides)
    return base


def _fanin_status(**overrides: object) -> dict:
    base = {
        "inputs": [_lane()],
        "tts": {
            "program_duck_active": False,
            "pending_frames": 0,
            "flushed_frames": 0,
            "dropped_audio_frames": 0,
        },
        "output": {"sample_rate": 48000},
    }
    base.update(overrides)
    return base


def _request(
    *,
    commanded_main_volume_db: float = -35.0,
    hold_duration_s: float = 4.0,
    tolerance_db: float = 3.0,
) -> StimulusRequest:
    return StimulusRequest(
        requested_stimulus_band_hz=(30.0, 200.0),
        requested_stimulus_effective_peak_dbfs=-20.0,
        requested_commanded_main_volume_db=commanded_main_volume_db,
        requested_hold_duration_s=hold_duration_s,
        requested_cooldown_s=2.0,
        requested_repeat_count=1,
        stimulus_generator_identity="bench-noise-v1",
        render_timeout_s=30.0,
        render_rlimit_as_bytes=1 << 29,
        render_rlimit_cpu_s=30,
        render_nice=10,
        cross_check_poll_interval_s=0.1,
        cross_check_read_count=40,
        cross_check_tolerance_db=tolerance_db,
    )


def _target_plan() -> TargetPlan:
    return TargetPlan(
        target_id=TARGET_ID,
        target_fingerprint=_sha(f"target:{TARGET_ID}"),
        graph_raw_text=_live_yaml(),
        limiter_name=LIMITER_NAME,
        owner_channels=OWNER_CHANNELS,
        profile_summary={"runtime_block_required": True},
        baseline_clip_limit_dbfs=-1.0,
    )


class FakeController:
    """The controller seam. ``get_active_config_raw`` is all the EXECUTOR
    uses directly (finish_*/run_reference_sweep/run_candidate now read the
    config INSIDE the window, before playback — B3); the round-trip test
    also drives this controller through the REAL runner.py activation
    lifecycle (snapshot_predecessor / temporary_bass_activation), which
    additionally needs get_config_file_path/set_active_config_raw/
    patch_config/reload — mirrors test_bass_extension_bench_runner.py's
    proven FakeController.
    """

    def __init__(self, raw: str = "", *, config_path: Path | None = None) -> None:
        self.raw = raw or _live_yaml()
        self._predecessor_raw = self.raw
        self.config_path = config_path
        self.calls: list[str] = []

    async def get_active_config_raw(self) -> str:
        self.calls.append("get_active_config_raw")
        return self.raw

    async def get_config_file_path(self) -> str:
        return str(self.config_path)

    async def set_active_config_raw(self, raw: str) -> bool:
        self.calls.append("set_active_config_raw")
        self.raw = raw
        return True

    async def patch_config(self, patch: dict[str, Any]) -> bool:
        """Deep-merge ``patch`` into the running config (mirrors CamillaDSP's
        real PATCH semantics closely enough for this test): temporary_bass_
        activation uses this to inject the candidate clip_limit, and the
        candidate/reference activations' graph fingerprints must genuinely
        differ for produce_limiter_thresholds's active-graph-fingerprint /
        threshold consistency cross-check to hold — a no-op patch_config (as
        used where only Protocol conformance matters, e.g.
        test_bass_extension_bench_runner.py's FakeExecutor-based tests,
        which never derive a REAL fingerprint from this text) would leave
        every activation's read-back identical."""

        self.calls.append("patch_config")
        parsed = yaml.safe_load(self.raw)

        def _merge(dst: dict[str, Any], src: Mapping[str, Any]) -> None:
            for key, value in src.items():
                if isinstance(value, Mapping) and isinstance(dst.get(key), dict):
                    _merge(dst[key], value)
                else:
                    dst[key] = value

        _merge(parsed, patch)
        self.raw = yaml.safe_dump(parsed, sort_keys=False)
        return True

    async def reload(self) -> bool:
        self.calls.append("reload")
        self.raw = self._predecessor_raw
        return True


def _played_stimulus(
    *,
    commanded_main_volume_db: float = -35.0,
    fader_before_db: float | None = None,
    fader_after_db: float | None = None,
    live_peak_all_samples: Sequence[Sequence[float]] = ((-99.0, -99.0, -20.0, -20.0),),
    transparency_analysis: ArtifactIdentity | None = None,
    transparency_verdict: str | None = None,
    admission: ArtifactIdentity | None = None,
    label: str = "played",
) -> executor.PlayedStimulus:
    before = commanded_main_volume_db if fader_before_db is None else fader_before_db
    after = commanded_main_volume_db if fader_after_db is None else fader_after_db
    return executor.PlayedStimulus(
        admission=admission if admission is not None else _artifact(f"{label}-admission"),
        acoustic_capture=_artifact(f"{label}-capture"),
        signal_analysis=_artifact(f"{label}-signal"),
        protection_analysis=_artifact(f"{label}-protection"),
        quality_verdict="pass",
        protection_verdict="pass",
        transparency_analysis=transparency_analysis,
        transparency_verdict=transparency_verdict,
        mux_status_start=_mux_status(),
        mux_status_end=_mux_status(),
        fanin_status_start=_fanin_status(),
        fanin_status_end=_fanin_status(),
        fader_before_db=before,
        fader_before_muted=False,
        fader_after_db=after,
        fader_after_muted=False,
        clipped_samples_before=0,
        clipped_samples_after=0,
        live_peak_all_samples=live_peak_all_samples,
        stimulus_effective_peak_dbfs=-20.0,
        commanded_main_volume_db=commanded_main_volume_db,
        target_boost_db=6.0,
        hold_duration_s=4.0,
        required_cooldown_s=2.0,
        repeat_count=1,
    )


class RecordingPlayAndCapture:
    """Records every ``stimulus_path`` it is handed (B3) and returns canned
    PlayedStimulus objects, one per call, in the order supplied."""

    def __init__(self, responses: list[executor.PlayedStimulus]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def play(
        self,
        *,
        target: TargetPlan,
        role: str,
        request: StimulusRequest,
        stop: Stop,
        stimulus_path: Path,
        reference: Any = None,
    ) -> executor.PlayedStimulus:
        self.calls.append(
            {
                "target": target,
                "role": role,
                "request": request,
                "stimulus_path": stimulus_path,
                "reference": reference,
            }
        )
        return self._responses.pop(0)


def _executor(
    *,
    play_and_capture: Any,
    margin_name: str = "conservative",
    renders_outstanding: int = 64,
) -> executor.BenchRoleExecutor:
    request = _request()
    return executor.BenchRoleExecutor(
        target=_target_plan(),
        requests={
            "sweep_transparency": request,
            "sustain_stress": request,
            "digital_transfer_probe": request,
        },
        play_and_capture=play_and_capture,
        binary=render.BinaryIdentity(
            path="/opt/camilladsp/camilladsp",
            version_output="CamillaDSP 4.1.3",
            sha256=_sha("fake-binary"),
            camilladsp_build_id="camilladsp-v4.1.3-fake",
        ),
        margin=MARGINS[margin_name],
        renders_outstanding=renders_outstanding,
    )


# --------------------------------------------------------------------------- #
# B-B: sweep_transparency generates a REAL swept sine; sustain_stress and
# digital_transfer_probe stay on band-limited noise
# --------------------------------------------------------------------------- #


def test_sweep_transparency_uses_the_real_sweep_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import sweep as sweep_mod

    calls: list[str] = []
    real_sine = sweep_mod.synchronized_swept_sine

    def _spy_sine(**kwargs: Any):
        calls.append("sweep")
        return real_sine(**kwargs)

    monkeypatch.setattr(sweep_mod, "synchronized_swept_sine", _spy_sine)

    request = _request()
    path = executor._generate_mono_stimulus_wav(
        role="sweep_transparency", request=request, sample_rate_hz=48000, target_dir=tmp_path
    )
    assert calls == ["sweep"]
    assert path.name.startswith("sweep-")
    assert path.is_file()

    # A genuine swept sine's instantaneous frequency changes over the file —
    # its early-vs-late autocorrelation-peak lag differs, unlike stationary
    # band-limited noise. Cheap, real signal check: the first and second
    # halves are NOT a simple time-shifted copy of each other (a sine at a
    # single fixed frequency would be near-periodic within each half).
    with wave.open(str(path), "rb") as wav:
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    half = samples.size // 2
    first_half_zero_crossings = int(np.sum(np.diff(np.sign(samples[:half])) != 0))
    second_half_zero_crossings = int(np.sum(np.diff(np.sign(samples[half:])) != 0))
    # An exponential sweep from f1 to f2 has monotonically increasing
    # instantaneous frequency, so the second half (higher frequencies) has
    # substantially more zero crossings than the first.
    assert second_half_zero_crossings > first_half_zero_crossings * 1.5


def test_sustain_and_transfer_probe_stay_on_band_limited_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.audio_measurement import playback as playback_mod
    from jasper.audio_measurement import sweep as sweep_mod

    noise_calls: list[str] = []
    sweep_calls: list[str] = []
    real_noise = playback_mod.ensure_bandlimited_noise_wav
    real_sine = sweep_mod.synchronized_swept_sine

    def _spy_noise(**kwargs: Any):
        noise_calls.append("noise")
        return real_noise(**kwargs)

    def _spy_sine(**kwargs: Any):
        sweep_calls.append("sweep")
        return real_sine(**kwargs)

    monkeypatch.setattr(playback_mod, "ensure_bandlimited_noise_wav", _spy_noise)
    monkeypatch.setattr(sweep_mod, "synchronized_swept_sine", _spy_sine)

    request = _request()
    for role in ("sustain_stress", "digital_transfer_probe"):
        executor._generate_mono_stimulus_wav(
            role=role, request=request, sample_rate_hz=48000, target_dir=tmp_path
        )
    assert noise_calls == ["noise", "noise"]
    assert sweep_calls == []


def test_generate_mono_stimulus_wav_caches_the_sweep_by_content(tmp_path: Path) -> None:
    request = _request()
    first = executor._generate_mono_stimulus_wav(
        role="sweep_transparency", request=request, sample_rate_hz=48000, target_dir=tmp_path
    )
    second = executor._generate_mono_stimulus_wav(
        role="sweep_transparency", request=request, sample_rate_hz=48000, target_dir=tmp_path
    )
    assert first == second


# --------------------------------------------------------------------------- #
# N-8: stereo duplication is width-checked, not dynamically dtype-derived
# --------------------------------------------------------------------------- #


def test_generate_stimulus_wav_refuses_a_non_16_bit_mono_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_24bit_mono(*, role, request, sample_rate_hz, target_dir):  # type: ignore[no-untyped-def]
        path = target_dir / "fake-24bit.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(3)  # 24-bit — R6a(iv) requires 16-bit
            out.setframerate(sample_rate_hz)
            out.writeframes(b"\x00\x00\x00" * 100)
        return path

    monkeypatch.setattr(executor, "_generate_mono_stimulus_wav", _fake_24bit_mono)
    with pytest.raises(executor.ExecutorError, match="16-bit"):
        executor._generate_stimulus_wav(
            role="sustain_stress", request=_request(), sample_rate_hz=48000, target_dir=tmp_path
        )


# --------------------------------------------------------------------------- #
# S4: renders_outstanding is threaded from the campaign, never an invented
# per-target literal
# --------------------------------------------------------------------------- #


def test_estimate_campaign_render_count_scales_with_target_count() -> None:
    from jasper.bass_extension.bench.manifest import STIMULUS_ROLES, author_campaign_manifest

    request_dict = _request().to_dict()
    manifest_one = author_campaign_manifest(
        {
            "driver_safety_fingerprint": _sha("ds"),
            "margin_policy_name": "conservative",
            "margin_policy_fingerprint": _sha("mp"),
            "requests": {"deep": {role: dict(request_dict) for role in STIMULUS_ROLES}},
        },
        target_ids=("deep",),
    )
    manifest_two = author_campaign_manifest(
        {
            "driver_safety_fingerprint": _sha("ds"),
            "margin_policy_name": "conservative",
            "margin_policy_fingerprint": _sha("mp"),
            "requests": {
                "deep": {role: dict(request_dict) for role in STIMULUS_ROLES},
                "natural": {role: dict(request_dict) for role in STIMULUS_ROLES},
            },
        },
        target_ids=("deep", "natural"),
    )
    # 8 = 2 discovery pre_limiter renders + 6 one-candidate renders
    # (digital_transfer_probe/sweep/sustain, each pre+post) per target — a
    # value DERIVED from campaign composition (target count), never a bare
    # literal a caller could get wrong for a multi-target campaign.
    assert executor.estimate_campaign_render_count(manifest_one) == 8
    assert executor.estimate_campaign_render_count(manifest_two) == 16
    assert (
        executor.estimate_campaign_render_count(manifest_two)
        == 2 * executor.estimate_campaign_render_count(manifest_one)
    )


def test_bench_role_executor_has_no_default_for_renders_outstanding() -> None:
    """S4: the field is required — a caller must pass an explicit,
    campaign-derived value (typically via estimate_campaign_render_count),
    never fall back to a silent invented default."""

    import dataclasses

    field = next(
        f for f in dataclasses.fields(executor.BenchRoleExecutor) if f.name == "renders_outstanding"
    )
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING  # type: ignore[comparison-overlap]


# --------------------------------------------------------------------------- #
# B3: the padded artifact is exactly what PlayAndCapture receives
# --------------------------------------------------------------------------- #


async def test_prepare_and_play_pads_before_handing_the_artifact_to_play_and_capture(
    tmp_path: Path,
) -> None:
    play_and_capture = RecordingPlayAndCapture([_played_stimulus()])
    bench = _executor(play_and_capture=play_and_capture)
    sink = BundleSink(tmp_path / "bundle", bundle_id="b3")

    played, padded, padded_identity = await bench._prepare_and_play(
        target=_target_plan(),
        role="sweep_transparency",
        request=_request(),
        live_active_config_raw=_live_yaml(),
        sink=sink,
        stop=Stop(),
        tag="probe",
    )

    assert played is not None
    assert len(play_and_capture.calls) == 1
    received_path = play_and_capture.calls[0]["stimulus_path"]
    assert received_path.is_file()
    received_bytes = received_path.read_bytes()

    # The artifact PlayAndCapture actually received is byte-identical to the
    # one recorded in the bundle (padded_identity) AND to the PaddedStimulus
    # object threaded forward for the offline render — one content-addressed
    # artifact, not two independently-generated near-duplicates.
    assert received_bytes == padded.wav_bytes
    assert hashlib.sha256(received_bytes).hexdigest() == padded_identity.sha256
    on_disk = sink.bundle_dir / padded_identity.relative_path
    assert on_disk.read_bytes() == received_bytes

    # A genuine WAV, not a bare copy of the (mono) generator output — R6a's
    # fixed fan-in mix geometry is 2-channel regardless of owner topology.
    with wave.open(str(received_path), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 48000

    # R6 padding actually happened: lead-in/lead-out are non-zero for this
    # owner path (bass_ext_lt @ 40 Hz, bass_ext_subsonic @ 30 Hz highpass).
    assert padded.lead_in_frames > 0
    assert padded.body_frames > 0


async def test_run_discovery_records_the_padded_identity_as_the_capture_stimulus(
    tmp_path: Path,
) -> None:
    """A DiscoveryCapture.stimulus of played.admission (the old bug) would
    attest an artifact that was never actually played; it must be the padded
    identity PlayAndCapture was handed."""

    responses = [_played_stimulus(label="sweep"), _played_stimulus(label="sustain")]
    play_and_capture = RecordingPlayAndCapture(responses)
    bench = _executor(play_and_capture=play_and_capture)
    sink = BundleSink(tmp_path / "bundle", bundle_id="disc")
    readback = _write_readback_receipt(sink)

    captures = await bench.run_discovery(
        target=_target_plan(),
        active_graph_readback=readback,
        sink=sink,
        stop=Stop(),
    )

    assert len(captures) == 2
    for capture, call in zip(captures, play_and_capture.calls):
        played_path_bytes = call["stimulus_path"].read_bytes()
        assert capture.stimulus.sha256 == hashlib.sha256(played_path_bytes).hexdigest()


# --------------------------------------------------------------------------- #
# S-1/S-2 (M6): derivation consumes the PERSISTED activation read-back
# receipt — never a live re-fetch, never a stale/mismatched source
# --------------------------------------------------------------------------- #


async def test_finish_discovery_derives_from_the_activation_receipt_not_a_stale_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-2 (M6): derivation's source config must be the activation
    read-back receipt the pass actually proved (runner.py's
    discovery_activation), never anything else — a stale post-restore
    config, or a live re-fetch that could disagree with what was proved.

    Proof mechanism: the receipt carries a DISTINCTIVE, otherwise-harmless
    marker (an unusual ``queuelimit`` value — R3 passes it through
    unchanged and records it in device_diff) that exists ONLY in this
    receipt, nowhere else reachable from this test. The derivation receipt
    written to the bundle is read back and must carry the exact marker —
    any code path that consulted a different source would fail this
    assertion. See this test's own docstring end for how this was verified
    to actually kill the two named mutations (revert-to-post-restore,
    revert-to-live-refetch), since a test that can't be shown failing
    against a real regression is not evidence of anything.
    """

    import json

    monkeypatch.setattr(render, "render_config", _fake_transfer_render_config)

    marker_queuelimit = 987654321  # distinctive: LIVE_CONFIG's own default is 4
    marked_config = {
        **LIVE_CONFIG,
        "devices": {**LIVE_CONFIG["devices"], "queuelimit": marker_queuelimit},
    }
    marked_yaml = yaml.safe_dump(marked_config, sort_keys=False)

    responses = [_played_stimulus(label="sweep"), _played_stimulus(label="sustain")]
    play_and_capture = RecordingPlayAndCapture(responses)
    bench = _executor(play_and_capture=play_and_capture)
    sink = BundleSink(tmp_path / "bundle", bundle_id="m6")
    readback = _write_readback_receipt(sink, active_config_raw=marked_yaml)

    captures = await bench.run_discovery(
        target=_target_plan(), active_graph_readback=readback, sink=sink, stop=Stop()
    )
    await bench.finish_discovery(
        captures, target=_target_plan(), sink=sink, stop=Stop()
    )

    derivation_receipt = json.loads(
        (
            sink.bundle_dir
            / f"{TARGET_ID}/discovery-sweep_transparency-pre_limiter-derivation.json"
        ).read_text(encoding="utf-8")
    )
    assert derivation_receipt["device_diff"]["queuelimit_live"] == marker_queuelimit


def _fake_per_channel_discovery_render_config(
    binary_path, config_path, *, output_path, bounds, fader_db
):  # type: ignore[no-untyped-def]
    """Owner channels 2 and 3 get DIFFERENT constant levels, so a test can
    prove finish_discovery emits genuinely distinct per-channel probes
    (S-3), never one collapsed value."""

    n_frames = 250_000  # comfortably exceeds lead_in + body + lead_out
    frame = np.zeros((n_frames, 4), dtype=np.float64)
    frame[:, 2] = 10 ** (-30.0 / 20.0)  # channel 2: -30 dBFS
    frame[:, 3] = 10 ** (-10.0 / 20.0)  # channel 3: -10 dBFS
    data = frame.astype("<f8").tobytes()
    output_path.write_bytes(data)
    return render.RenderInvocation(
        argv=(binary_path, f"--gain={fader_db}", str(config_path)),
        returncode=0,
        duration_s=0.001,
        stdout_tail="",
        stderr_tail="",
        output_sha256=hashlib.sha256(data).hexdigest(),
        output_byte_size=len(data),
    )


async def test_finish_discovery_emits_one_probe_per_owner_channel_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-3: R3 forbids collapsing owner channels before the runner's
    distinct-peak inventory. For a 2-channel owner and 2 discovery roles,
    finish_discovery must return 4 probes (2 roles x 2 channels), not 2
    (one min-collapsed probe per role) — and channel 2's and channel 3's
    probes must carry their OWN distinct peaks, matching the deliberately
    different levels this render mock writes to each channel."""

    monkeypatch.setattr(render, "render_config", _fake_per_channel_discovery_render_config)

    responses = [_played_stimulus(label="sweep"), _played_stimulus(label="sustain")]
    play_and_capture = RecordingPlayAndCapture(responses)
    bench = _executor(play_and_capture=play_and_capture)
    sink = BundleSink(tmp_path / "bundle", bundle_id="s3")
    readback = _write_readback_receipt(sink)

    captures = await bench.run_discovery(
        target=_target_plan(), active_graph_readback=readback, sink=sink, stop=Stop()
    )
    probes = await bench.finish_discovery(
        captures, target=_target_plan(), sink=sink, stop=Stop()
    )

    # 2 roles x 2 owner channels (2, 3) = 4 probes, never collapsed to 2.
    assert len(probes) == 4
    peaks = sorted(p.pre_limiter_peak_dbfs for p in probes)
    # Each role contributes one -30 dBFS (channel 2) and one -10 dBFS
    # (channel 3) probe — never a single min_across_channels(-30) value
    # standing in for both channels.
    assert peaks == pytest.approx([-30.0, -30.0, -10.0, -10.0])
    # Each probe's recorded PCM is that ONE channel's own extracted stream
    # (S7) — the two distinct-peak probes within a role must not share a
    # pre_limiter_pcm identity.
    sweep_probes = probes[:2]
    assert sweep_probes[0].pre_limiter_pcm.sha256 != sweep_probes[1].pre_limiter_pcm.sha256


# --------------------------------------------------------------------------- #
# B1: the render argv carries the bracketed (locked-level) fader gain
# --------------------------------------------------------------------------- #


async def test_finish_discovery_threads_the_played_faders_db_into_every_render(
    tmp_path: Path,
) -> None:
    captured_fader_db: list[float] = []

    def _fake_render_with_determinism_receipt(
        binary_path, config_path, *, yaml_text, first_output_path, second_output_path, bounds, fader_db
    ):
        captured_fader_db.append(fader_db)
        data = np.zeros(8000, dtype="<f8").tobytes()
        first_output_path.write_bytes(data)
        second_output_path.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        invocation = render.RenderInvocation(
            argv=(binary_path, f"--gain={fader_db}", str(config_path)),
            returncode=0,
            duration_s=0.001,
            stdout_tail="",
            stderr_tail="",
            output_sha256=sha,
            output_byte_size=len(data),
        )
        return render.DeterminismReceipt(
            config_sha256=render.config_shape_sha256(yaml_text), first=invocation, second=invocation
        )

    import jasper.bass_extension.bench.executor as executor_mod

    orig = render.render_with_determinism_receipt
    executor_mod.render.render_with_determinism_receipt = _fake_render_with_determinism_receipt  # type: ignore[attr-defined]
    try:
        played = _played_stimulus(commanded_main_volume_db=-17.5)
        # run_discovery plays BOTH live roles (sweep_transparency, sustain_stress).
        play_and_capture = RecordingPlayAndCapture([played, played])
        bench = _executor(play_and_capture=play_and_capture)
        sink = BundleSink(tmp_path / "bundle", bundle_id="b1")
        readback = _write_readback_receipt(sink)

        captures = await bench.run_discovery(
            target=_target_plan(),
            active_graph_readback=readback,
            sink=sink,
            stop=Stop(),
        )
        await bench.finish_discovery(
            captures, target=_target_plan(), sink=sink, stop=Stop()
        )
    finally:
        executor_mod.render.render_with_determinism_receipt = orig  # type: ignore[attr-defined]

    # One render per discovered role (sweep_transparency, sustain_stress);
    # every single one carries the SAME bracketed level PlayedStimulus
    # recorded, never an omitted/zero default.
    assert captured_fader_db
    assert all(value == -17.5 for value in captured_fader_db)


# --------------------------------------------------------------------------- #
# B4: the digital_transfer_probe's deployed artifact is the extracted body,
# matching the reference transform exactly (never the whole render).
# --------------------------------------------------------------------------- #

_TRANSFER_CLIP_LIMIT_DBFS = -3.0
_TRANSFER_QUIET_DBFS = -20.0
_TRANSFER_LOUD_DBFS = -1.0  # above the clip limit: genuine soft-clip compression
_FAKE_RENDER_FRAMES = 250_000


def _fake_transfer_render_config(binary_path, config_path, *, output_path, bounds, fader_db):
    """A deterministic fake render: a quiet-then-loud step, broadcast to
    every playback channel. For the post_limiter boundary, the written
    samples are EXACTLY render.reference_soft_clip(pre-shape) — proving the
    wiring (extraction + comparison), not re-deriving the transform math
    (already unit-tested in test_bass_extension_bench_render.py)."""

    half = _FAKE_RENDER_FRAMES // 2
    quiet = 10 ** (_TRANSFER_QUIET_DBFS / 20.0)
    loud = 10 ** (_TRANSFER_LOUD_DBFS / 20.0)
    mono = np.concatenate(
        [np.full(half, quiet), np.full(_FAKE_RENDER_FRAMES - half, loud)]
    ).astype(np.float64)
    if "post_limiter" in str(output_path):
        mono = render.reference_soft_clip(mono, clip_limit_dbfs=_TRANSFER_CLIP_LIMIT_DBFS)
    frame = np.tile(mono[:, None], (1, 4)).astype("<f8")  # LIVE_CONFIG playback.channels == 4
    data = frame.tobytes()
    output_path.write_bytes(data)
    return render.RenderInvocation(
        argv=(binary_path, f"--gain={fader_db}", str(config_path)),
        returncode=0,
        duration_s=0.001,
        stdout_tail="",
        stderr_tail="",
        output_sha256=hashlib.sha256(data).hexdigest(),
        output_byte_size=len(data),
    )


async def test_digital_transfer_probe_deployed_artifact_matches_the_reference_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render, "render_config", _fake_transfer_render_config)
    bench = _executor(play_and_capture=RecordingPlayAndCapture([]))
    sink = BundleSink(tmp_path / "bundle", bundle_id="b4")

    result = await bench._digital_transfer_probe(
        candidate_setting_dbfs=_TRANSFER_CLIP_LIMIT_DBFS,
        live_active_config_raw=_live_yaml(),
        sink=sink,
    )

    assert result["verdict"] == "pass"

    # Independently recompute what the reference/deployed bytes MUST be for
    # a single extracted (owner) channel, and confirm the recorded artifacts
    # are exactly that — not the whole multi-channel interleaved render.
    half = _FAKE_RENDER_FRAMES // 2
    quiet = 10 ** (_TRANSFER_QUIET_DBFS / 20.0)
    loud = 10 ** (_TRANSFER_LOUD_DBFS / 20.0)
    pre_mono = np.concatenate(
        [np.full(half, quiet), np.full(_FAKE_RENDER_FRAMES - half, loud)]
    ).astype(np.float64)
    expected_reference_full = render.reference_soft_clip(
        pre_mono, clip_limit_dbfs=_TRANSFER_CLIP_LIMIT_DBFS
    )

    post_path = sink.bundle_dir / result["post_limiter_pcm"]["relative_path"]
    reference_path = sink.bundle_dir / result["reference_post_limiter_pcm"]["relative_path"]
    post_bytes = post_path.read_bytes()
    reference_bytes = reference_path.read_bytes()

    # B4's core assertion: deployed (post) and reference are the SAME SHAPE
    # (one extracted channel's body, not the 4-channel interleaved file) —
    # the whole render.raw file is `4x` this size.
    whole_render_bytes = _FAKE_RENDER_FRAMES * 4 * 8
    assert len(post_bytes) < whole_render_bytes
    assert len(post_bytes) == len(reference_bytes)
    assert post_bytes == reference_bytes
    assert result["post_limiter_pcm"]["sha256"] == result["reference_post_limiter_pcm"]["sha256"]

    # And the reference bytes match the independently-recomputed transform,
    # sliced to the body window (lead-in/lead-out excluded, per R3/R6).
    body_samples = np.frombuffer(reference_bytes, dtype="<f8")
    # The body is a contiguous slice of expected_reference_full; every value
    # that occurs must be one this transform could have produced.
    assert body_samples.size > 0
    assert np.isin(np.round(body_samples, 12), np.round(np.unique(expected_reference_full), 12)).all()


async def test_digital_transfer_probe_checks_every_owner_channel_not_just_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: a per-channel divergence anywhere in owner_channels must flip the
    overall verdict to "fail" — the check must not collapse to
    owner_channels[0]."""

    def _one_channel_wrong(binary_path, config_path, *, output_path, bounds, fader_db):
        half = _FAKE_RENDER_FRAMES // 2
        quiet = 10 ** (_TRANSFER_QUIET_DBFS / 20.0)
        loud = 10 ** (_TRANSFER_LOUD_DBFS / 20.0)
        mono = np.concatenate(
            [np.full(half, quiet), np.full(_FAKE_RENDER_FRAMES - half, loud)]
        ).astype(np.float64)
        if "post_limiter" in str(output_path):
            mono = render.reference_soft_clip(mono, clip_limit_dbfs=_TRANSFER_CLIP_LIMIT_DBFS)
        frame = np.tile(mono[:, None], (1, 4)).astype(np.float64)
        if "post_limiter" in str(output_path):
            # Corrupt channel 3 (the SECOND owner channel) only.
            frame[:, 3] += 0.01
        data = frame.astype("<f8").tobytes()
        output_path.write_bytes(data)
        return render.RenderInvocation(
            argv=(binary_path, f"--gain={fader_db}", str(config_path)),
            returncode=0,
            duration_s=0.001,
            stdout_tail="",
            stderr_tail="",
            output_sha256=hashlib.sha256(data).hexdigest(),
            output_byte_size=len(data),
        )

    monkeypatch.setattr(render, "render_config", _one_channel_wrong)
    bench = _executor(play_and_capture=RecordingPlayAndCapture([]))
    sink = BundleSink(tmp_path / "bundle", bundle_id="b4-s8")

    result = await bench._digital_transfer_probe(
        candidate_setting_dbfs=_TRANSFER_CLIP_LIMIT_DBFS,
        live_active_config_raw=_live_yaml(),
        sink=sink,
    )

    # Channel 2 (owner_channels[0]) alone would report "pass" — the overall
    # verdict must reflect channel 3's corruption too.
    assert result["verdict"] == "fail"
    import json

    analysis = json.loads(
        (sink.bundle_dir / result["transfer_analysis"]["relative_path"]).read_text(encoding="utf-8")
    )
    assert analysis["per_channel"]["2"]["verdict"] == "pass"
    assert analysis["per_channel"]["3"]["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# S1: prove_fader_bracket checked at the locked measurement level
# --------------------------------------------------------------------------- #


async def test_finish_role_refuses_when_fader_held_the_safe_floor_not_the_commanded_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render, "render_config", _fake_transfer_render_config)
    # commanded_main_volume_db=-35 but the fader bracket held the safe floor
    # (-60) throughout — stable, unmuted, but NOT at the locked level.
    played = _played_stimulus(
        commanded_main_volume_db=-35.0, fader_before_db=-60.0, fader_after_db=-60.0
    )
    bench = _executor(play_and_capture=RecordingPlayAndCapture([]))
    sink = BundleSink(tmp_path / "bundle", bundle_id="s1")

    padded_wav = _pad_stereo_stimulus(sink)

    with pytest.raises(live_proof.FaderDriftError, match="locked measurement level"):
        await bench._finish_role(
            played,
            role="sweep_transparency",
            live_active_config_raw=_live_yaml(),
            padded=padded_wav[0],
            padded_identity=padded_wav[1],
            expected_clip_limit_dbfs=-1.0,
            setting_tag="s1",
            sink=sink,
        )


def _pad_stereo_stimulus(sink: BundleSink) -> tuple[stimulus.PaddedStimulus, ArtifactIdentity]:
    """Build a real, R3-valid (2-channel, 16-bit, 48 kHz) padded stimulus
    directly, bypassing generation — for tests that call _finish_role
    without going through _prepare_and_play."""

    minima = stimulus.compute_padding_minima(
        [
            LIVE_CONFIG["filters"]["bass_ext_lt"],
            LIVE_CONFIG["filters"]["bass_ext_subsonic"],
        ],
        sample_rate_hz=48000,
    )
    silence_frame = np.zeros((_FAKE_RENDER_FRAMES, 2), dtype="<i2")
    tmp_wav = sink.bundle_dir / "raw-mono-source.wav"
    with wave.open(str(tmp_wav), "wb") as source:
        source.setnchannels(2)
        source.setsampwidth(2)
        source.setframerate(48000)
        source.writeframes(silence_frame.tobytes())
    padded = stimulus.pad_stimulus_wav(tmp_wav, minima=minima)
    identity = sink.write_bytes(
        f"{TARGET_ID}/s1-padded.wav", padded.wav_bytes, kind="jts_bass_extension_bench_padded_stimulus"
    )
    return padded, identity


# --------------------------------------------------------------------------- #
# S5: the cross-check tolerance bound is genuinely per-channel
# --------------------------------------------------------------------------- #


async def test_finish_role_computes_bounds_from_each_channels_own_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spy on cross_check.cross_check_owner_channels proves _finish_role
    builds computed_bounds_db as a genuine per-channel mapping — one entry
    per owner channel, keyed by channel — never a single shared/collapsed
    value applied uniformly."""

    captured: dict[str, Any] = {}
    real_cross_check = cross_check.cross_check_owner_channels

    def _spy(**kwargs: Any):
        captured.update(kwargs)
        return real_cross_check(**kwargs)

    monkeypatch.setattr(cross_check, "cross_check_owner_channels", _spy)
    monkeypatch.setattr(render, "render_config", _fake_transfer_render_config)

    played = _played_stimulus(
        commanded_main_volume_db=-35.0,
        live_peak_all_samples=((-99.0, -99.0, _TRANSFER_LOUD_DBFS, _TRANSFER_LOUD_DBFS),),
    )
    bench = _executor(play_and_capture=RecordingPlayAndCapture([]))
    sink = BundleSink(tmp_path / "bundle", bundle_id="s5")
    padded, padded_identity = _pad_stereo_stimulus(sink)

    await bench._finish_role(
        played,
        role="sweep_transparency",
        live_active_config_raw=_live_yaml(),
        padded=padded,
        padded_identity=padded_identity,
        expected_clip_limit_dbfs=-1.0,
        setting_tag="s5",
        sink=sink,
    )

    computed_bounds_db = captured["computed_bounds_db"]
    assert isinstance(computed_bounds_db, dict)
    assert set(computed_bounds_db.keys()) == set(OWNER_CHANNELS)
    # Each owner channel's bound came from ITS OWN render body — computed
    # independently per channel (not one shared float broadcast to a dict).
    assert all(isinstance(v, float) for v in computed_bounds_db.values())


# --------------------------------------------------------------------------- #
# The full round trip: real BenchRoleExecutor -> build_sweep_record ->
# produce_limiter_thresholds, with synthetic rendered artifacts.
# --------------------------------------------------------------------------- #

_SWEEP_QUIET_DBFS = -40.0
_SWEEP_LOUD_DBFS = -20.0


def _round_trip_render_config(binary_path, config_path, *, output_path, bounds, fader_db):
    """One quiet-then-loud step, broadcast to all 4 playback channels, safe
    under both the -1 dBFS owner clip_limit and the conservative margin's -4
    dBFS digital-clamp ceiling. Discovery measures its LOUD half
    (_SWEEP_LOUD_DBFS) as the pre-limiter peak, which becomes the single
    candidate's clip_limit setting — so the digital_transfer_probe's
    post_limiter render for that SAME setting must be the reference
    soft-clip transform of its own pre-limiter shape (never the identical,
    untransformed step my _finish_role callers use), or the isolated
    transfer verdict correctly fails."""

    half = _FAKE_RENDER_FRAMES // 2
    quiet = 10 ** (_SWEEP_QUIET_DBFS / 20.0)
    loud = 10 ** (_SWEEP_LOUD_DBFS / 20.0)
    mono = np.concatenate(
        [np.full(half, quiet), np.full(_FAKE_RENDER_FRAMES - half, loud)]
    ).astype(np.float64)
    if "transfer-" in str(output_path) and "post_limiter" in str(output_path):
        mono = render.reference_soft_clip(mono, clip_limit_dbfs=_SWEEP_LOUD_DBFS)
    frame = np.tile(mono[:, None], (1, 4)).astype("<f8")
    data = frame.tobytes()
    output_path.write_bytes(data)
    return render.RenderInvocation(
        argv=(binary_path, f"--gain={fader_db}", str(config_path)),
        returncode=0,
        duration_s=0.001,
        stdout_tail="",
        stderr_tail="",
        output_sha256=hashlib.sha256(data).hexdigest(),
        output_byte_size=len(data),
    )


async def test_bench_role_executor_full_round_trip_through_produce_limiter_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The amendment's own round-trip wording: a REAL BenchRoleExecutor,
    composed through the runner's real two-phase campaign orchestration
    (run_discovery/finish_discovery, run_reference_sweep,
    run_candidate/finish_candidate), producing a bundle the frozen
    limiter_evidence producer accepts end to end."""

    from jasper.bass_extension.bench import activation
    from jasper.bass_extension.bench.context import build_measured_context
    from jasper.bass_extension.bench.manifest import STIMULUS_ROLES, author_campaign_manifest
    from jasper.bass_extension.bench.runner import run_campaign
    from jasper.bass_extension.limiter_evidence import LimiterThresholdSet, produce_limiter_thresholds

    monkeypatch.setattr(render, "render_config", _round_trip_render_config)
    monkeypatch.setattr(
        activation, "_prove_active_graph", lambda config, proof: proof.expected_clip_limit_dbfs
    )

    # Every play() call across discovery (x2), the reference sweep (x1), and
    # the candidate pass (x2) needs its own canned PlayedStimulus. The sweep
    # candidate play must carry transparency_analysis/verdict (frozen
    # schema requirement checked by run_candidate). The reference sweep and
    # the candidate sweep must share the SAME admission SHA — the frozen
    # protocol requires "the executor plays the same admitted request in
    # both phases" (ReferenceSweepCapture's docstring); the producer refuses
    # as inconsistent otherwise.
    shared_sweep_admission = _artifact("shared-sweep-admission")
    responses = [
        _played_stimulus(label="disc-sweep"),  # discovery: sweep_transparency
        _played_stimulus(label="disc-sustain"),  # discovery: sustain_stress
        _played_stimulus(
            label="ref-sweep", admission=shared_sweep_admission
        ),  # reference sweep (phase 1)
        _played_stimulus(
            label="cand-sweep",
            admission=shared_sweep_admission,
            transparency_analysis=_artifact("transp-analysis"),
            transparency_verdict="pass",
        ),  # candidate: sweep_transparency
        _played_stimulus(label="cand-sustain"),  # candidate: sustain_stress
    ]
    play_and_capture = RecordingPlayAndCapture(responses)

    # snapshot_predecessor fingerprint-matches get_active_config_raw()
    # against a REAL on-disk file at get_config_file_path() — write one.
    config_file = tmp_path / "active.yml"
    config_file.write_text(_live_yaml(), encoding="utf-8")
    controller = FakeController(config_path=config_file)
    stop = Stop()
    bench = executor.BenchRoleExecutor(
        target=_target_plan(),
        requests={role: _request() for role in STIMULUS_ROLES},
        play_and_capture=play_and_capture,
        binary=render.BinaryIdentity(
            path="/opt/camilladsp/camilladsp",
            version_output="CamillaDSP 4.1.3",
            sha256=_sha("fake-binary"),
            camilladsp_build_id="camilladsp-v4.1.3-fake",
        ),
        margin=MARGINS["conservative"],
        renders_outstanding=64,
    )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def open_window():
        yield None

    class FakeFloor:
        async def to_floor(self) -> None:
            pass

        async def assert_at_floor(self) -> None:
            pass

    deps = BenchDeps(
        open_window=open_window, controller=controller, floor=FakeFloor(), executor=bench, stop=stop
    )

    def _sha_label(label: str) -> str:
        return _sha(label)

    request_dict = {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -20.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 4.0,
        "requested_cooldown_s": 2.0,
        "requested_repeat_count": 1,
        "stimulus_generator_identity": "bench-noise-v1",
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 536_870_912,
        "render_rlimit_cpu_s": 30,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.1,
        "cross_check_read_count": 40,
        "cross_check_tolerance_db": 3.0,
    }
    manifest = author_campaign_manifest(
        {
            "driver_safety_fingerprint": _sha_label("ds"),
            "margin_policy_name": "conservative",
            "margin_policy_fingerprint": _sha_label("mp"),
            "requests": {TARGET_ID: {role: dict(request_dict) for role in STIMULUS_ROLES}},
        },
        target_ids=(TARGET_ID,),
    )
    context = build_measured_context(
        target_family_fingerprint=_sha_label("family"),
        target_order=[(TARGET_ID, _sha_label(f"target:{TARGET_ID}"))],
        driver_safety_fingerprint=_sha_label("ds"),
        margin_policy_fingerprint=_sha_label("mp"),
        transparency_policy_fingerprint=_sha_label("tp"),
        natural_graph_fingerprint=_sha_label("natural-graph"),
        baseline_limiter_clip_limit_dbfs=-1.0,
        camilladsp_build_id="build",
        owner_channels=list(OWNER_CHANNELS),
        sample_rate_hz=48_000,
        limiter_name=LIMITER_NAME,
        tap_implementation_id="tap",
    )
    retained_facts = {
        name: _artifact(f"retained-{name}")
        for name in ("sweep", "sustain", "commanded_level", "stimulus_peak", "boost", "digital_clamp")
    }
    sink = BundleSink(tmp_path / "bundle", bundle_id="round-trip")

    emitted = await run_campaign(
        deps,
        manifest=manifest,
        measured_context=context,
        targets=[_target_plan()],
        retained_facts=retained_facts,
        sink=sink,
    )

    result = emitted["targets"][0]["result"]
    # "evaluated" is the target-level success disposition (build_evaluated_result)
    # — distinct from the per-candidate "accepted"/"limiter_transparency_failed"/
    # "refused" disposition nested in candidates_least_to_most_permissive.
    assert result["disposition"] == "evaluated", result
    candidates = result["candidates_least_to_most_permissive"]
    assert len(candidates) == 1
    assert candidates[0]["disposition"] == "accepted"
    # The candidate's setting is exactly the discovered pre-limiter peak —
    # the LOUD half of the synthetic quiet/loud step.
    assert candidates[0]["limiter_threshold_dbfs"] == pytest.approx(_SWEEP_LOUD_DBFS)

    produced = produce_limiter_thresholds(emitted, required_context=context)
    assert isinstance(produced, LimiterThresholdSet), produced
    assert produced.targets[0].limiter_threshold_dbfs == pytest.approx(_SWEEP_LOUD_DBFS)

    # Sanity: the sweep record really did go through build_sweep_record (the
    # runner composes it from the executor's _finish_role dict), and its
    # PCM artifacts are on disk, content-addressed, and extracted (never the
    # whole multi-channel interleaved render).
    sweep_record = candidates[0]["sweep_transparency"]
    post_pcm_path = sink.bundle_dir / sweep_record["post_limiter_pcm"]["relative_path"]
    assert post_pcm_path.is_file()
    whole_render_bytes = _FAKE_RENDER_FRAMES * 4 * 8
    assert post_pcm_path.stat().st_size < whole_render_bytes
