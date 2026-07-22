# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end MeasurementSession test on synthetic data.

Synthesizes a "room" with a known modal peak, runs the full session
flow (sweep → playback stub → upload → analyze → apply → reset),
and verifies the PEQ list contains a filter near the synthetic mode.

This is the integration-level confidence that all the pieces hook
up correctly. Unit tests for individual modules already pin the
math; this test pins the wiring.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.audio_measurement import deconv, quality, sweep
from jasper.audio_measurement.calibration import store_calibration
from jasper.correction import bundles, runtime_integrity, strategy
from jasper.correction.session import (
    AutolevelData,
    AutolevelStatus,
    DEFAULT_REPEAT_MAIN_POSITION,
    DEFAULT_ROOM_POSITION_COUNT,
    MeasurementSession,
    SessionBusyError,
    SessionState,
)
from .correction_session_fixtures import (
    make_measurement_session as _make_session,
)


def test_session_room_defaults_match_named_owners(tmp_path):
    fixture = _make_session(tmp_path)
    sess = MeasurementSession(fixture.cfg)

    assert sess.total_positions == DEFAULT_ROOM_POSITION_COUNT == 6
    assert sess.target_choice == strategy.DEFAULT_TARGET_PROFILE_ID == "flat"
    assert sess.strategy_choice == strategy.DEFAULT_CORRECTION_STRATEGY_ID == (
        "balanced"
    )
    assert sess.repeat_main_position is DEFAULT_REPEAT_MAIN_POSITION is True


@pytest.mark.asyncio
async def test_apply_forwards_exact_guard_bass_summary_before_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from jasper.correction import runtime_safety
    from jasper import dsp_apply
    from jasper.sound import graph_carrier, profile as sound_profile

    sess = _make_session(tmp_path)
    sess.state = SessionState.READY
    summary = MappingProxyType({
        "authority_valid": True,
        "runtime_block_required": False,
    })
    events: list[str] = []

    async def guard():
        events.append("guard")
        return summary

    async def get_current() -> str:
        return str(tmp_path / "active.yml")

    async def load(path: str) -> bool:
        events.append("load")
        return True

    async def apply_config(**kwargs):
        await kwargs["prepare"]()
        await kwargs["load_config"](str(kwargs["candidate_path"]))
        return SimpleNamespace()

    class Carrier:
        def reemit(self, *_args, **_kwargs):
            return SimpleNamespace(yaml="canonical candidate")

    def assert_safe(text, *, bass_profile_summary, **_kwargs):
        events.append("safety")
        assert text == "canonical candidate"
        assert bass_profile_summary is summary

    monkeypatch.setattr(dsp_apply, "apply_dsp_config", apply_config)
    monkeypatch.setattr(graph_carrier, "carrier_for_loaded_config", lambda *_a, **_k: Carrier())
    monkeypatch.setattr(sound_profile, "load_profile", lambda: object())
    monkeypatch.setattr(sound_profile, "build_sound_filters", lambda _profile: ())
    monkeypatch.setattr(runtime_safety, "assert_correction_graph_safe", assert_safe)

    await sess.apply(
        load,
        camilla_get_config=get_current,
        prepare_guard=guard,
    )

    assert events == ["guard", "safety", "load"]
    assert sess.state == SessionState.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard_value",
    ["missing", None, object(), [], "not-a-mapping"],
    ids=["missing", "none", "object", "list", "string"],
)
async def test_apply_refuses_invalid_bass_summary_before_load(
    tmp_path: Path,
    monkeypatch,
    guard_value,
) -> None:
    from jasper import dsp_apply

    sess = _make_session(tmp_path)
    sess.state = SessionState.READY
    loads: list[str] = []

    async def get_current() -> str:
        return str(tmp_path / "active.yml")

    async def load(path: str) -> bool:
        loads.append(path)
        return True

    async def guard():
        return guard_value

    async def apply_config(**kwargs):
        await kwargs["prepare"]()
        await kwargs["load_config"](str(kwargs["candidate_path"]))
        return SimpleNamespace()

    monkeypatch.setattr(dsp_apply, "apply_dsp_config", apply_config)

    with pytest.raises(RuntimeError, match="bass authority evidence"):
        await sess.apply(
            load,
            camilla_get_config=get_current,
            prepare_guard=None if guard_value == "missing" else guard,
        )

    assert loads == []


def _synthesize_room_capture(
    sweep_signal: np.ndarray,
    sample_rate: int,
    *,
    mode_freq_hz: float = 80.0,
    mode_q: float = 4.0,
    mode_gain_db: float = 6.0,
) -> np.ndarray:
    """Build a synthetic 'captured' signal: sweep convolved with an
    IR that has one modal peak at `mode_freq_hz`. The PEQ designer
    should pick a filter near this frequency.

    Approach: construct the room's magnitude response as a single
    bell-shaped peak, take ifft to get a symmetric IR, convolve
    with the sweep.
    """
    n_fft = 8192
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    # Magnitude response as 1 + bell-shaped peak in linear (not dB)
    omega = freqs / mode_freq_hz
    safe = np.where(omega > 0, omega, 1.0)
    delta_oct = np.log2(safe)
    bw = 1.0 / mode_q
    mag_db = mode_gain_db / (1.0 + (delta_oct / bw) ** 2)
    mag_db[omega <= 0] = 0
    H_lin = 10 ** (mag_db / 20.0)

    # Zero-phase IR: ifft of real magnitude. Symmetric, so we
    # circularly shift to make it causal-ish.
    h = np.fft.irfft(H_lin, n=n_fft)
    # Center the energy at the start of the buffer (causal-ish).
    h = np.fft.fftshift(h)
    # Trim to short IR for fast convolution.
    h = h[len(h) // 2 - 256: len(h) // 2 + 256].astype(np.float64)

    captured = fftconvolve(sweep_signal.astype(np.float64), h, mode="full")
    return captured


@pytest.mark.asyncio
async def test_local_capture_setup_binds_realized_input_before_first_upload(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    await sess.begin_noise_capture()
    record = store_calibration(
        text="20 0\n1000 0.5\n20000 1\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "calibrations",
    )
    device = {
        "browser_label": "USB measurement microphone",
        "sample_rate": 48000,
        "channel_count": 1,
        "echo_cancellation": False,
        "noise_suppression": False,
        "auto_gain_control": False,
    }

    report = await sess.bind_local_capture_setup(
        mic_calibration=record,
        input_device=device,
    )

    assert sess.input_device == device
    assert sess.mic_calibration is record
    assert sess.browser_audio_report == report
    assert sess.local_capture_setup_bound is True
    assert report["failed"] is False
    assert sess.snapshot()["input_device"] == device
    assert (sess.bundle_dir / "mic_calibration.json").is_file()
    assert (sess.bundle_dir / "mic_calibration.txt").is_file()
    retry_report = await sess.bind_local_capture_setup(
        mic_calibration=record,
        input_device=device,
    )
    assert retry_report == report
    assert len([
        event for event in sess._events if event.type == "local_capture_setup"
    ]) == 1

    with pytest.raises(RuntimeError, match="cannot change"):
        await sess.bind_local_capture_setup(
            mic_calibration=record,
            input_device={**device, "browser_label": "Different microphone"},
        )


@pytest.mark.asyncio
async def test_local_capture_setup_rejects_relay_or_started_measurement(
    tmp_path: Path,
):
    device = {
        "browser_label": "USB measurement microphone",
        "sample_rate": 48000,
        "channel_count": 1,
        "echo_cancellation": False,
        "noise_suppression": False,
        "auto_gain_control": False,
    }
    relay = _make_session(tmp_path / "relay")
    relay.capture_transport = "relay"
    await relay.begin_noise_capture()
    with pytest.raises(RuntimeError, match="unavailable"):
        await relay.bind_local_capture_setup(
            mic_calibration=None,
            input_device=device,
        )

    advanced = _make_session(tmp_path / "advanced")
    advanced.current_position = 1
    await advanced.begin_noise_capture()
    with pytest.raises(RuntimeError, match="cannot change"):
        await advanced.bind_local_capture_setup(
            mic_calibration=None,
            input_device=device,
        )

    unsafe = _make_session(tmp_path / "unsafe")
    await unsafe.begin_noise_capture()
    with pytest.raises(ValueError, match="audio path"):
        await unsafe.bind_local_capture_setup(
            mic_calibration=None,
            input_device={**device, "echo_cancellation": True},
        )
    assert unsafe.input_device is None
    assert unsafe.local_capture_setup_bound is False


@pytest.mark.asyncio
async def test_session_applies_mic_calibration_during_capture(
    tmp_path: Path, monkeypatch,
):
    """A selected mic calibration is applied inside _smooth_capture,
    before normalization / PEQ design, so result curves and bundles
    all inherit calibrated data."""
    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n20000 2\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "calibrations",
    )
    sess = _make_session(tmp_path)
    sess.mic_calibration = record
    called = {"value": False}

    from jasper.audio_measurement import calibration as cal_mod

    real_apply = cal_mod.apply_calibration_curve

    def wrapped_apply(freqs, magnitude, curve):
        called["value"] = True
        assert curve == record.curve
        return real_apply(freqs, magnitude, curve)

    monkeypatch.setattr(cal_mod, "apply_calibration_curve", wrapped_apply)

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)

    await sess.on_capture_uploaded(cap_path)

    assert called["value"] is True
    assert sess.state == SessionState.READY
    assert sess.capture_quality
    assert sess.capture_quality[-1]["failed"] is False
    assert sess.capture_quality[-1]["capture_kind"] == "measurement"
    assert sess.capture_quality[-1]["position_index"] == 0
    assert sess.capture_quality[-1]["artifact_path"] == "captures/p0.wav"
    replay = sess.capture_quality[-1]["replay_artifacts"]
    assert replay["impulse_response_path"] == "analysis/p0_ir.wav"
    assert replay["response_path"] == "analysis/p0_response.json"
    replay_payload = json.loads((sess.bundle_dir / replay["response_path"]).read_text())
    assert replay_payload["capture_kind"] == "measurement"
    assert replay_payload["source_capture_path"] == "captures/p0.wav"
    assert replay_payload["analysis_curve"]["calibration_applied"] is True
    assert replay_payload["analysis_curve"]["normalized_band_hz"] == [
        200.0,
        1000.0,
    ]
    assert (sess.bundle_dir / replay["impulse_response_path"]).exists()
    assert not any(
        issue["code"] == "mic_uncalibrated"
        for issue in sess.capture_quality[-1]["issues"]
    )
    assert sess.snapshot()["mic_calibration"]["calibration_id"] == (
        record.calibration_id
    )
    meta_path = sess.bundle_dir / "mic_calibration.json"
    raw_path = sess.bundle_dir / "mic_calibration.txt"
    assert meta_path.exists()
    assert raw_path.exists()
    assert "20 -1" in raw_path.read_text()
    assert record.calibration_id in meta_path.read_text()
    assert (meta_path.stat().st_mode & 0o777) == 0o600
    assert (raw_path.stat().st_mode & 0o777) == 0o600
    manifest_paths = {
        artifact["path"]
        for artifact in bundles.read_artifact_manifest(sess.bundle_dir)["artifacts"]
    }
    assert {
        "captures/p0.wav",
        "analysis/p0_ir.wav",
        "analysis/p0_response.json",
        "runtime_integrity.json",
        "mic_calibration.json",
        "mic_calibration.txt",
    }.issubset(manifest_paths)
    manifest = bundles.read_artifact_manifest(sess.bundle_dir)
    artifact_by_path = {
        artifact["path"]: artifact
        for artifact in manifest["artifacts"]
    }
    assert artifact_by_path["analysis/p0_ir.wav"]["dependencies"] == [
        "captures/p0.wav",
        "info.json",
    ]
    assert artifact_by_path["analysis/p0_response.json"]["dependencies"] == [
        "analysis/p0_ir.wav",
        "captures/p0.wav",
        "info.json",
        "mic_calibration.json",
    ]
    assert "analysis/p0_response.json" in artifact_by_path["result.json"][
        "dependencies"
    ]


@pytest.mark.asyncio
async def test_analysis_offloaded_to_worker_thread(
    tmp_path: Path, monkeypatch,
):
    """The multi-second deconv/smoothing and PEQ design run via
    asyncio.to_thread, not inline on the single-threaded wizard event
    loop — otherwise a long analysis freezes /status polls and every
    other wizard sharing the server."""
    sess = _make_session(tmp_path)
    main_ident = threading.get_ident()
    seen: dict[str, int] = {}

    real_smooth = sess._smooth_capture
    real_design = sess._run_design_from_positions

    def spy_smooth(*args, **kwargs):
        seen["smooth"] = threading.get_ident()
        return real_smooth(*args, **kwargs)

    def spy_design(*args, **kwargs):
        seen["design"] = threading.get_ident()
        return real_design(*args, **kwargs)

    monkeypatch.setattr(sess, "_smooth_capture", spy_smooth)
    monkeypatch.setattr(sess, "_run_design_from_positions", spy_design)

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)

    await sess.on_capture_uploaded(cap_path)

    assert sess.state == SessionState.READY
    # Both heavy steps executed on a worker thread, not the event loop.
    assert seen["smooth"] != main_ident
    assert seen["design"] != main_ident


def test_snapshot_returns_point_in_time_copies(tmp_path: Path):
    """snapshot() copies the mutable containers it exposes — capture_quality,
    noise_reports, design_report — so a concurrent .append / key-set from the
    loop or worker thread can't mutate (or tear) a snapshot mid-serialization."""
    sess = _make_session(tmp_path)
    sess.capture_quality.append({"k": 1})
    sess.noise_reports.append({"n": 1})
    sess.design_report = {"d": 1}

    snap = sess.snapshot()

    # Mutate the live session AFTER snapshotting.
    sess.capture_quality.append({"k": 2})
    sess.noise_reports.append({"n": 2})
    sess.design_report["d"] = 99

    assert snap["capture_quality"] == [{"k": 1}]
    assert snap["noise_reports"] == [{"n": 1}]
    assert snap["design_report"] == {"d": 1}

    # runtime_integrity issues reach the snapshot via summary() and must be a
    # point-in-time copy too (the same loop-thread-append tear class).
    sess.runtime_integrity.issues.append({"code": "a", "severity": "warn"})
    snap2 = sess.snapshot()
    sess.runtime_integrity.issues.append({"code": "b", "severity": "warn"})
    assert snap2["runtime_integrity"]["issues"] == [{"code": "a", "severity": "warn"}]


@pytest.mark.asyncio
async def test_overlong_capture_truncated_before_quality_assessment(
    tmp_path: Path, monkeypatch,
):
    """An over-long capture is bounded once at the session boundary, so the
    recorded capture quality and the deconvolved IR describe the SAME signal —
    not full-length quality stats against a truncated analysis."""
    monkeypatch.setattr(deconv, "DEFAULT_MAX_CAPTURE_SECONDS", 2.0)
    sess = _make_session(tmp_path)

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    # Real sweep response (passes quality) padded well past the 2 s cap.
    room = _synthesize_room_capture(sweep_signal, sr)
    overlong = np.concatenate([room, np.zeros(5 * sr, dtype=room.dtype)])
    assert len(overlong) > 2 * sr
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, overlong.astype(np.float32), sr)

    await sess.on_capture_uploaded(cap_path)

    assert sess.state == SessionState.READY
    # Duration reflects the 2 s cap, not the ~6 s captured length.
    assert sess.capture_quality[-1]["duration_s"] == pytest.approx(2.0, abs=0.05)
    # ...and the truncation is surfaced as an operator-visible quality warning
    # (→ /status / bundle / doctor), not just a journal line.
    assert any(
        i["code"] == "capture_truncated" and i["severity"] == "warn"
        for i in sess.capture_quality[-1]["issues"]
    )


def test_band_levels_dbfs_bounds_oversized_input(monkeypatch, caplog):
    """_band_levels_dbfs caps its FFT input (like deconvolve) so an oversized
    uploaded WAV — noise floor or capture band-SNR — can't drive the rfft to
    OOM on the 1 GB Pi."""
    from jasper.correction.session import _band_levels_dbfs

    monkeypatch.setattr(deconv, "DEFAULT_MAX_CAPTURE_SECONDS", 1.0)
    sr = 8000
    t = np.arange(5 * sr) / sr  # 5 s, well past the 1 s cap
    tone = (0.5 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float64)
    with caplog.at_level(logging.WARNING, logger="jasper.audio_measurement.deconv"):
        bands = _band_levels_dbfs(tone, sr)
    # Identical to feeding the pre-truncated 1 s slice → the cap was applied.
    assert bands == _band_levels_dbfs(tone[:sr], sr)
    assert any("truncating" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_overlong_noise_capture_is_bounded(tmp_path: Path, monkeypatch):
    """An over-long /upload-noise WAV is bounded before the rms/peak/abs/FFT
    math in _noise_report_dict, so it can't spike memory on the 1 GB Pi; the
    recorded noise report's duration reflects the cap."""
    monkeypatch.setattr(deconv, "DEFAULT_MAX_CAPTURE_SECONDS", 1.0)
    sess = _make_session(tmp_path)
    await sess.begin_noise_capture()
    noise_path = sess.noise_capture_path_for_position(0)
    noise_path.parent.mkdir(parents=True, exist_ok=True)
    sr = sess.cfg.sample_rate
    # 5 s of low-level noise, well past the 1 s cap.
    samples = (0.01 * np.ones(5 * sr, dtype=np.float64)).astype(np.float32)
    sweep.write_sweep_wav(noise_path, samples, sr)

    await sess.on_noise_capture_uploaded(noise_path)

    assert sess.noise_reports[0]["duration_s"] == pytest.approx(1.0, abs=0.05)


@pytest.mark.asyncio
async def test_repeat_and_verify_analysis_offloaded_to_worker_thread(
    tmp_path: Path, monkeypatch,
):
    """SF3: pin the off-loop execution of the repeat- and verify-capture
    analysis. The to_thread offload shipped in #755 covers these sites too,
    but only the measurement site was pinned — guard the symmetric sites
    against silently regressing to inline."""
    sess = _make_session(tmp_path)
    sess.repeat_main_position = True
    main_ident = threading.get_ident()
    smooth_idents: set[int] = set()
    design_idents: set[int] = set()

    real_smooth = sess._smooth_capture
    real_design = sess._run_design_from_positions

    def spy_smooth(*a, **k):
        smooth_idents.add(threading.get_ident())
        return real_smooth(*a, **k)

    def spy_design(*a, **k):
        design_idents.add(threading.get_ident())
        return real_design(*a, **k)

    monkeypatch.setattr(sess, "_smooth_capture", spy_smooth)
    monkeypatch.setattr(sess, "_run_design_from_positions", spy_design)

    async def fake_play(path, **kw):
        pass

    async def fake_camilla(path: str) -> bool:
        return True

    await sess.prepare_and_play_sweep(fake_play)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)
    await sess.on_capture_uploaded(cap_path)
    assert sess.state == SessionState.NEEDS_REPEAT_CAPTURE

    # Repeat path → on_repeat_capture_uploaded (to_thread smooth + design).
    await sess.prepare_and_play_repeat_sweep(fake_play)
    repeat_path = sess.repeat_capture_path_for_position(0)
    repeat_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(repeat_path, sweep_signal.astype(np.float32), sr)
    await sess.on_repeat_capture_uploaded(repeat_path)
    assert sess.state == SessionState.READY

    # Verify path → on_verify_capture_uploaded (to_thread smooth).
    await sess.apply(fake_camilla)
    await sess.start_verify_sweep(fake_play)
    verify_path = sess.verify_capture_path()
    verify_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(verify_path, sweep_signal.astype(np.float32), sr)
    await sess.on_verify_capture_uploaded(verify_path)
    assert sess.state == SessionState.VERIFIED

    # Every offloaded invocation ran off the event-loop thread.
    assert smooth_idents and main_ident not in smooth_idents
    assert design_idents and main_ident not in design_idents


@pytest.mark.asyncio
async def test_session_records_failed_capture_quality_in_bundle(
    tmp_path: Path, monkeypatch,
):
    # runtime_integrity.level reads os.getloadavg(); pin a low load so the
    # "level == ok" assertion below is deterministic instead of failing on a
    # busy dev machine (it's only ever a flake, never a real product signal).
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: 0.1)
    sess = _make_session(tmp_path)

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(
        cap_path,
        np.ones_like(sweep_signal, dtype=np.float32),
        sr,
    )

    with pytest.raises(quality.CaptureQualityError, match="clipped"):
        await sess.on_capture_uploaded(cap_path)

    assert sess.state == SessionState.FAILED
    assert sess.capture_quality[0]["failed"] is True
    assert sess.capture_quality[0]["capture_kind"] == "measurement"
    assert sess.capture_quality[0]["position_index"] == 0
    info = json.loads((sess.bundle_dir / "info.json").read_text())
    assert info["state"] == "failed"
    assert info["capture_quality"][0]["failed"] is True
    assert info["capture_quality"][0]["artifact_path"] == "captures/p0.wav"
    assert info["runtime_integrity"]["level"] == "ok"
    manifest_paths = {
        artifact["path"]
        for artifact in bundles.read_artifact_manifest(sess.bundle_dir)["artifacts"]
    }
    assert "captures/p0.wav" in manifest_paths
    assert "runtime_integrity.json" in manifest_paths


@pytest.mark.asyncio
async def test_session_records_noise_and_repeat_artifacts(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.repeat_main_position = True

    await sess.begin_noise_capture()
    assert sess.state == SessionState.NEEDS_NOISE_CAPTURE
    noise_path = sess.noise_capture_path_for_position(0)
    sweep.write_sweep_wav(
        noise_path,
        np.zeros(int(sess.cfg.sample_rate * 0.7), dtype=np.float32),
        sess.cfg.sample_rate,
    )
    await sess.on_noise_capture_uploaded(noise_path)
    assert sess.noise_reports[0]["artifact_path"] == "noise/p0_pre.wav"
    assert sess.acoustic_quality is not None
    assert sess.acoustic_quality["summary"]["noise_capture_count"] == 1

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)
    await sess.on_capture_uploaded(cap_path)
    assert sess.state == SessionState.NEEDS_REPEAT_CAPTURE

    await sess.prepare_and_play_repeat_sweep(fake_play_sweep)
    repeat_path = sess.repeat_capture_path_for_position(0)
    sweep.write_sweep_wav(repeat_path, sweep_signal.astype(np.float32), sr)
    await sess.on_repeat_capture_uploaded(repeat_path)

    assert sess.state == SessionState.READY
    assert sess.repeat_quality is not None
    assert sess.repeat_quality["artifact_path"] == "repeat_captures/p0_r1.wav"
    assert sess.repeat_quality["replay_artifacts"]["impulse_response_path"] == (
        "analysis/repeat_p0_ir.wav"
    )
    assert sess.repeatability_report is not None
    assert sess.repeatability_report["level"] == "high"
    assert sess.confidence_report is not None
    assert sess.confidence_report["repeatability"]["level"] == "high"

    manifest = bundles.read_artifact_manifest(sess.bundle_dir)
    artifact_by_path = {
        artifact["path"]: artifact
        for artifact in manifest["artifacts"]
    }
    assert artifact_by_path["noise/p0_pre.wav"]["kind"] == "noise_capture"
    assert (
        artifact_by_path["repeat_captures/p0_r1.wav"]["kind"]
        == "repeat_capture"
    )
    assert (
        artifact_by_path["analysis/p0_response.json"]["kind"]
        == "derived_frequency_response"
    )
    assert (
        artifact_by_path["analysis/repeat_p0_ir.wav"]["kind"]
        == "derived_impulse_response"
    )
    assert not any(
        issue.severity == "fail"
        for issue in bundles.validate_bundle(sess.bundle_dir)
    )


@pytest.mark.asyncio
async def test_session_full_flow_synthetic_room(tmp_path: Path):
    """Run session.prepare_and_play_sweep → on_capture_uploaded →
    apply → reset against a synthetic 80 Hz modal room. Verify state
    transitions and that the PEQ designer picks a filter near 80 Hz.

    The playback step is stubbed (no real ALSA needed) — we just
    note that play_sweep was called with the cached WAV path.
    """
    sess = _make_session(tmp_path)
    assert sess.state == SessionState.IDLE

    # Stub playback: don't actually play; just record the path so we
    # can synthesize a capture from the same sweep.
    captured_paths = []

    async def fake_play_sweep(path, **kwargs):
        captured_paths.append(path)

    await sess.prepare_and_play_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_CAPTURE
    assert sess.sweep_wav_path is not None
    assert sess.sweep_wav_path.exists()
    # Stub was called.
    assert captured_paths == [str(sess.sweep_wav_path)]

    # Synthesize a "captured" WAV by convolving the sweep with a
    # known room IR.
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    captured = _synthesize_room_capture(
        sweep_signal, sr, mode_freq_hz=80.0, mode_q=4.0, mode_gain_db=6.0,
    )
    # Normalize to avoid clipping in the int16 round-trip — the
    # convolution output amplitude is sweep_amp * H, which can hit
    # >1.0 at the peak.
    captured = captured / max(1.0, float(np.max(np.abs(captured))))
    captured_path = tmp_path / "fake_capture.wav"
    sweep.write_sweep_wav(captured_path, captured.astype(np.float32), sr)

    await sess.on_capture_uploaded(captured_path)
    assert sess.state == SessionState.READY
    assert sess.measured_curve is not None
    assert sess.target_curve is not None
    assert sess.predicted_curve is not None
    assert sess.capture_quality
    assert sess.capture_quality[-1]["capture_kind"] == "measurement"
    assert any(
        issue["code"] == "mic_uncalibrated"
        for issue in sess.capture_quality[-1]["issues"]
    )
    assert "replay_artifacts" not in sess.capture_quality[-1]

    # PEQ designer should have picked at least one filter near 80 Hz.
    assert len(sess.peqs) >= 1
    peq_freqs = [p.freq_hz for p in sess.peqs]
    # At least one of the designed peaks is within an octave of 80 Hz.
    assert any(abs(np.log2(f / 80.0)) < 1.0 for f in peq_freqs), (
        f"expected PEQ near 80 Hz, got freqs {peq_freqs}"
    )
    # All filters are cuts (default cuts_only=True).
    assert all(p.gain_db <= 0 for p in sess.peqs)

    # apply() with a stub camilla. Verifies state goes to APPLIED
    # and the YAML was written.
    apply_calls: list[str] = []

    async def fake_set_config(path: str) -> bool:
        apply_calls.append(path)
        return True

    await sess.apply(fake_set_config)
    assert sess.state == SessionState.APPLIED
    assert sess.config_path is not None
    assert sess.config_path.exists()
    assert apply_calls == [str(sess.config_path)]

    # reset() rolls back to base.
    reset_calls: list[str] = []

    async def fake_reset(path: str) -> bool:
        reset_calls.append(path)
        return True

    await sess.reset(fake_reset)
    assert sess.state == SessionState.IDLE
    assert reset_calls == [str(sess.cfg.base_config_path)]


@pytest.mark.asyncio
async def test_session_apply_failure_transitions_to_failed(tmp_path: Path):
    """If CamillaDSP rejects the config, the session moves to FAILED
    rather than silently leaving APPLIED in an inconsistent state."""
    sess = _make_session(tmp_path)

    # Force the session into READY without going through the real
    # capture flow — set the relevant state directly.
    sess.state = SessionState.READY

    async def reject_config(path: str) -> bool:
        return False

    await sess.apply(reject_config)
    assert sess.state == SessionState.FAILED
    assert sess.error is not None and "rejected" in sess.error.lower()


@pytest.mark.asyncio
async def test_session_apply_from_wrong_state_raises(tmp_path: Path):
    sess = _make_session(tmp_path)
    # IDLE → apply should raise (not silently wreck CamillaDSP).
    async def stub(path: str) -> bool:
        return True

    with pytest.raises(RuntimeError, match="cannot apply"):
        await sess.apply(stub)


@pytest.mark.asyncio
async def test_session_snapshot_shape(tmp_path: Path):
    sess = _make_session(tmp_path)
    snap = sess.snapshot()
    assert snap["session_id"] == sess.session_id
    assert snap["state"] == "idle"
    assert snap["error"] is None
    assert snap["peqs"] == []
    assert snap["sweep"] is None
    assert snap["config_path"] is None


# --- Bug 3 regression: stranded-capture watchdog ---------------------------
# A sweep leaves the session in awaiting_capture waiting for the browser to
# upload. If that upload never arrives the session wedged forever and blocked
# every future /start (observed on hardware 2026-06-04, session 07c57fbe8d12).


@pytest.mark.asyncio
async def test_awaiting_capture_times_out_to_failed(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_CAPTURE
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED
    assert "capture" in (sess.error or "").lower()


@pytest.mark.asyncio
async def test_capture_upload_cancels_the_timeout(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_sweep(fake_play_sweep)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)
    await sess.on_capture_uploaded(cap_path)
    # Past the timeout window: the upload must have cancelled the watchdog.
    await asyncio.sleep(0.2)
    assert sess.state != SessionState.FAILED


@pytest.mark.asyncio
async def test_needs_noise_capture_times_out_to_failed(tmp_path: Path):
    # needs_noise_capture is an automatic browser step (record pre-sweep room
    # noise, then upload). A denied mic / backgrounded tab strands it, blocking
    # every future /start — the same wedge class as awaiting_capture. The
    # watchdog must abandon it too. No measurement window is open here, so the
    # wedge never mutes the speaker; it just blocks /start.
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05
    await sess.begin_noise_capture()
    assert sess.state == SessionState.NEEDS_NOISE_CAPTURE
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED
    assert "capture" in (sess.error or "").lower()


@pytest.mark.asyncio
async def test_human_setup_can_suspend_and_resume_capture_watchdog_on_loop(
    tmp_path: Path,
):
    # Relay level setup is deliberately human-paced (permission,
    # mic/calibration, placement, then auto-level). Pause the local upload
    # watchdog for that sub-flow, then restore a fresh bound so an abandoned
    # room capture still self-recovers.
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05

    await sess.begin_noise_capture()
    assert sess.state == SessionState.NEEDS_NOISE_CAPTURE
    sess.suspend_capture_timeout()
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.NEEDS_NOISE_CAPTURE
    assert sess.error is None

    await sess.resume_capture_timeout_on_loop()
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED
    assert "capture" in (sess.error or "").lower()


@pytest.mark.asyncio
async def test_needs_noise_capture_times_out_on_later_positions_too(tmp_path: Path):
    # The watchdog arms via _set_state on BOTH entries to needs_noise_capture:
    # the first from IDLE (above) and later positions from needs_next_position.
    # Pin the second path so the guard can't silently regress for positions 2+.
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05
    sess.state = SessionState.NEEDS_NEXT_POSITION
    sess.current_position = 1
    await sess.begin_noise_capture()
    assert sess.state == SessionState.NEEDS_NOISE_CAPTURE
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED


@pytest.mark.asyncio
async def test_repeat_capture_times_out_to_failed(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05
    sess.state = SessionState.NEEDS_REPEAT_CAPTURE

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.prepare_and_play_repeat_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_REPEAT_CAPTURE
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED
    assert "capture" in (sess.error or "").lower()


@pytest.mark.asyncio
async def test_verify_capture_times_out_to_failed(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05
    sess.state = SessionState.APPLIED

    async def fake_play_sweep(path, **kwargs):
        pass

    await sess.start_verify_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_VERIFY_CAPTURE
    await asyncio.sleep(0.25)
    assert sess.state == SessionState.FAILED
    assert "capture" in (sess.error or "").lower()


# --- reset() must not race an in-flight sweep/analysis task -----------------
@pytest.mark.asyncio
async def test_reset_rejected_while_a_sweep_or_analysis_is_in_flight(tmp_path: Path):
    # During preparing/sweeping/analyzing/verifying a fire-and-forget task is
    # running and will set the next state AFTER reset's IDLE. The server
    # rejects a direct reset there, so it can't touch CamillaDSP mid-task. The
    # web emergency Stop uses the separately tested cancel/reap seam first.
    sess = _make_session(tmp_path)

    async def fake_reset(path: str) -> bool:
        raise AssertionError("reset must not touch CamillaDSP while busy")

    for busy in (
        SessionState.PREPARING,
        SessionState.SWEEPING,
        SessionState.ANALYZING,
        SessionState.VERIFYING,
    ):
        sess.state = busy
        # SessionBusyError (a RuntimeError subclass) so the web layer can
        # map it to 409, not 500.
        with pytest.raises(SessionBusyError, match="in progress"):
            await sess.reset(fake_reset)


@pytest.mark.asyncio
async def test_emergency_stop_reaps_audio_task_before_reset(tmp_path: Path):
    sess = _make_session(tmp_path)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def audio_operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    running = asyncio.create_task(
        sess.run_background_audio_operation(audio_operation)
    )
    await started.wait()
    sess.state = SessionState.SWEEPING

    assert await sess.stop_background_audio_for_reset() is True
    assert running.done()
    assert cleaned.is_set()
    assert sess.state == SessionState.FAILED

    reset_calls: list[str] = []

    async def fake_reset(path: str) -> bool:
        reset_calls.append(path)
        return True

    await sess.reset(fake_reset)
    assert sess.state == SessionState.IDLE
    assert reset_calls == [str(sess.cfg.base_config_path)]


@pytest.mark.asyncio
async def test_reset_intent_atomically_blocks_autolevel_admission(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.state = SessionState.NEEDS_NOISE_CAPTURE
    sess._local_capture_setup_bound = True

    intent = await sess.begin_autolevel_reset()
    assert await sess.reserve_autolevel_run() is None
    assert await sess.end_autolevel_reset(intent) is True

    token = await sess.reserve_autolevel_run()
    assert token is not None
    assert await sess.release_autolevel_run_reservation(token) is True


@pytest.mark.asyncio
async def test_reset_intent_atomically_blocks_sweep_and_level_admission(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    called = False

    async def audio_operation() -> None:
        nonlocal called
        called = True

    async def get_volume() -> float:
        return -20.0

    async def set_volume(_value: float) -> bool:
        return True

    async def play_tone() -> None:
        raise AssertionError("reset-gated level match must not play audio")

    intent = await sess.begin_autolevel_reset()
    with pytest.raises(SessionBusyError, match="reset is in progress"):
        await sess.run_background_audio_operation(audio_operation)
    with pytest.raises(SessionBusyError, match="reset is in progress"):
        await sess.run_level_match(
            "listening_position",
            get_main_volume_db=get_volume,
            set_main_volume_db=set_volume,
            play_continuous_tone=play_tone,
            cancel_tone=lambda: None,
            read_status=lambda: {},
            wait_for_armed=False,
        )
    assert called is False
    assert await sess.end_autolevel_reset(intent) is True


@pytest.mark.asyncio
async def test_begin_reset_releases_its_intent_when_quiescence_fails(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    sess.state = SessionState.NEEDS_NOISE_CAPTURE
    sess._local_capture_setup_bound = True
    token = await sess.reserve_autolevel_run()
    assert token is not None

    async def fail_quiescence(*, timeout_s: float = 5.0) -> bool:
        del timeout_s
        raise asyncio.TimeoutError("cleanup stalled")

    sess.cancel_autolevel_and_wait = fail_quiescence  # type: ignore[method-assign]
    with pytest.raises(asyncio.TimeoutError, match="cleanup stalled"):
        await sess.begin_autolevel_reset()
    assert sess._autolevel_reset_intent is None
    assert await sess.release_autolevel_run_reservation(token) is True


@pytest.mark.asyncio
async def test_emergency_stop_gracefully_reaps_active_relay_level_ramp(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    tone_started = asyncio.Event()
    tone_stopped = asyncio.Event()
    writes: list[float] = []

    async def get_volume() -> float:
        return -24.0

    async def set_volume(value: float) -> bool:
        writes.append(value)
        return True

    async def play_tone() -> None:
        tone_started.set()
        await tone_stopped.wait()

    async def fast_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    running = asyncio.create_task(
        sess.run_level_match(
            "listening_position",
            get_main_volume_db=get_volume,
            set_main_volume_db=set_volume,
            play_continuous_tone=play_tone,
            cancel_tone=tone_stopped.set,
            read_status=lambda: {},
            sleep=fast_sleep,
            wait_for_armed=False,
        )
    )
    await tone_started.wait()

    intent = await sess.begin_autolevel_reset()
    assert await sess.stop_background_audio_for_reset() is True
    outcome = await running
    assert outcome.ramp.state.value == "cancelled"
    assert tone_stopped.is_set()
    assert writes[-1] == -24.0
    assert await sess.end_autolevel_reset(intent) is True


@pytest.mark.asyncio
async def test_emergency_stop_reaps_level_match_before_phone_arms(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    writes: list[float] = []
    tone_started = False
    phone_armed = False
    poll_sleeping = asyncio.Event()
    release_poll = asyncio.Event()
    first_poll = True
    now = 0.0

    async def get_volume() -> float:
        return -24.0

    async def set_volume(value: float) -> bool:
        writes.append(value)
        return True

    async def play_tone() -> None:
        nonlocal tone_started
        tone_started = True

    def read_status() -> dict:
        return {"event": {"armed": True}} if phone_armed else {}

    def clock() -> float:
        return now

    async def controlled_sleep(delay: float) -> None:
        nonlocal first_poll, now
        now += max(delay, 0.01)
        if first_poll:
            first_poll = False
            poll_sleeping.set()
            await release_poll.wait()
        await asyncio.sleep(0)

    running = asyncio.create_task(
        sess.run_level_match(
            "listening_position",
            get_main_volume_db=get_volume,
            set_main_volume_db=set_volume,
            play_continuous_tone=play_tone,
            cancel_tone=lambda: None,
            read_status=read_status,
            clock=clock,
            sleep=controlled_sleep,
            wait_for_armed=True,
        )
    )
    await poll_sleeping.wait()

    intent = await sess.begin_autolevel_reset()
    stopping = asyncio.create_task(sess.stop_background_audio_for_reset())
    await asyncio.sleep(0)
    assert not stopping.done()
    # Reproduce the exact race: the phone's armed update lands after Stop was
    # accepted but before the retained pre-arm poll task resumes.
    phone_armed = True
    release_poll.set()

    assert await stopping is True
    outcome = await running
    assert outcome.ramp.state.value == "cancelled"
    assert writes == []
    assert tone_started is False
    assert await sess.end_autolevel_reset(intent) is True


@pytest.mark.asyncio
async def test_reset_still_recovers_from_a_wedged_state(tmp_path: Path):
    # The escape hatch must keep working from settled/wedged states — the
    # whole point of reset is recovery.
    sess = _make_session(tmp_path)
    sess.state = SessionState.FAILED
    reset_calls: list[str] = []

    async def fake_reset(path: str) -> bool:
        reset_calls.append(path)
        return True

    await sess.reset(fake_reset)
    assert sess.state == SessionState.IDLE
    assert reset_calls == [str(sess.cfg.base_config_path)]


# --- Audio-safety: a measurement that ends via FAIL/VERIFY (not apply/reset)
# must still return main_volume to the listening level. Autolevel ramps it up
# and leaves it LOCKED for the whole measurement; the web apply/reset handlers
# own the success-path restore, so the session has to cover the endings they
# never see — else a failed measurement strands the speaker loud until /reset.
@pytest.mark.asyncio
async def test_failed_measurement_restores_autolevel_volume(tmp_path: Path):
    sess = _make_session(tmp_path)
    sess.capture_timeout_sec = 0.05
    restored: list[float] = []

    async def fake_set_vol(db):
        restored.append(db)

    sess._main_volume_setter = fake_set_vol
    sess.autolevel = AutolevelData(
        status=AutolevelStatus.LOCKED, original_main_volume_db=-20.0
    )
    await sess.begin_noise_capture()  # arms the watchdog
    await asyncio.sleep(0.25)  # let it fire → _fail → restore
    assert sess.state == SessionState.FAILED
    assert restored == [-20.0]
    assert sess.autolevel.restored is True


@pytest.mark.asyncio
async def test_autolevel_restore_idempotent_and_skips_when_not_ramped(
    tmp_path: Path,
):
    sess = _make_session(tmp_path)
    restored: list[float] = []

    async def fake_set_vol(db):
        restored.append(db)

    sess._main_volume_setter = fake_set_vol

    # No autolevel ramp (default IDLE status) → nothing to restore.
    await sess._restore_listening_volume_if_ramped()
    assert restored == []

    # Ramped + LOCKED → restore exactly once, even if called again.
    sess.autolevel = AutolevelData(
        status=AutolevelStatus.LOCKED, original_main_volume_db=-18.0
    )
    await sess._restore_listening_volume_if_ramped()
    await sess._restore_listening_volume_if_ramped()
    assert restored == [-18.0]


# --- Bug 2 regression: autolevel cap math --------------------------------
# The maxed_out UI previously hardcoded "(-6 dB)" instead of the real cap,
# misleading the user. The cap is computed here; pin it so the UI can read
# cap_db with confidence and the hardcoded-constant class of bug can't recur.
def test_compute_autolevel_cap_clamps():
    from jasper.correction.session import compute_autolevel_cap

    def cap(original):
        return compute_autolevel_cap(original, bump_db=6.0, ceil_db=-6.0)

    assert cap(-19.0) == -13.0   # +6 bump lands inside the band
    assert cap(-28.5) == -22.5   # quiet listener never rises by more than +6
    assert cap(-45.0) == -39.0   # regression: floor must not create a +25 jump
    assert cap(-2.0) == -6.0     # loud listener clamped to the safety ceiling
    assert cap(-12.0) == -6.0    # -12+6 = -6 exactly at the ceiling


# --- P4 acceptance verdict — session integration through the real verify path
#
# These drive the REAL on_verify_capture_uploaded (real deconv, real analysis,
# real AcceptanceEvaluator) with synthetic captures whose ground-truth verdict
# is known, stubbing only the CamillaDSP I/O boundary. They prove: the verdict
# lands on session.acceptance; the position-1 curve is the matched basis; the
# confirmatory-re-measure concordance advances across two verifies; and a
# confirmed regression auto-reverts through the existing reset() path.


async def _measure_positions(sess, room_gains_db: list[float]) -> None:
    """Run the flow measure→READY through the real pipeline, one synthetic
    room per position (an 80 Hz mode of the given gain per seat)."""

    async def fake_play(path, **kw):
        pass

    sess.total_positions = len(room_gains_db)
    sess.repeat_main_position = False
    for gain in room_gains_db:
        await sess.prepare_and_play_sweep(fake_play)
        sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
        captured = _synthesize_room_capture(
            sweep_signal, sr, mode_freq_hz=80.0, mode_gain_db=gain,
        )
        cap_path = sess.capture_path_for_position(sess.current_position)
        cap_path.parent.mkdir(parents=True, exist_ok=True)
        sweep.write_sweep_wav(cap_path, captured.astype(np.float32), sr)
        await sess.on_capture_uploaded(cap_path)


async def _measure_one_position(sess, room_gain_db: float) -> None:
    """Single-position variant of :func:`_measure_positions`."""
    await _measure_positions(sess, [room_gain_db])


async def _run_verify(sess, verify_room_gain_db: float) -> None:
    """Play + upload one verify sweep whose captured room has an 80 Hz mode of
    the given gain, through the real verify path."""

    async def fake_play(path, **kw):
        pass

    async def fake_camilla(path: str) -> bool:
        return True

    await sess.start_verify_sweep(fake_play)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    captured = _synthesize_room_capture(
        sweep_signal, sr, mode_freq_hz=80.0, mode_gain_db=verify_room_gain_db,
    )
    verify_path = sess.verify_capture_path()
    verify_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(verify_path, captured.astype(np.float32), sr)
    await sess.on_verify_capture_uploaded(verify_path)


@pytest.mark.asyncio
async def test_verify_populates_acceptance_verdict_and_position1_basis(
    tmp_path: Path,
):
    """A real measure→apply→verify run lands a verdict on session.acceptance,
    computed against the retained position-1 curve."""
    sess = _make_session(tmp_path)

    async def fake_camilla(path: str) -> bool:
        return True

    await _measure_one_position(sess, room_gain_db=8.0)
    assert sess.state == SessionState.READY
    # Position-1 curve is retained as the matched basis.
    assert sess.position1_curve is not None

    await sess.apply(fake_camilla)
    # A near-flat verify (mode corrected) → the verdict should be present and
    # is not a revert.
    await _run_verify(sess, verify_room_gain_db=0.5)
    assert sess.state == SessionState.VERIFIED
    assert isinstance(sess.acceptance, dict)
    assert sess.acceptance["verdict"] in ("accept", "surface")
    assert sess.acceptance["basis"] == "position_1"
    assert sess.acceptance["verify_index"] == 1
    assert sess.acceptance_verdict == sess.acceptance["verdict"]


@pytest.mark.asyncio
async def test_confirmatory_remeasure_concordance_and_auto_revert(
    tmp_path: Path,
):
    """A clear regression on the first verify is revert_pending_confirm (not
    reverted); a second concordant clear regression escalates to revert and
    auto_revert() rolls back through the existing reset() path."""
    sess = _make_session(tmp_path)

    reset_targets: list[str] = []

    async def fake_camilla(path: str) -> bool:
        reset_targets.append(path)
        return True

    # Measure a nearly-flat room, then a verify that measures a big NEW mode
    # (a genuinely-regressed result). Because the before is flat-ish and the
    # verify has a strong 80 Hz mode, this is a clear regression.
    await _measure_one_position(sess, room_gain_db=0.5)
    # Compatibility apply path (no config getter) — the topology-aware carrier
    # rejects tmp stub graphs; this test targets the verdict/auto-revert loop,
    # not the carrier. auto_revert is driven with an explicit target below.
    await sess.apply(fake_camilla)

    await _run_verify(sess, verify_room_gain_db=20.0)
    assert sess.state == SessionState.VERIFIED
    assert isinstance(sess.acceptance, dict)
    # First verify with a clear regression → pending confirmation, NOT reverted.
    assert sess.acceptance["verdict"] == "revert_pending_confirm"
    assert sess.acceptance["confirmed"] is False
    # auto_revert is a no-op on a pending (unconfirmed) verdict.
    reverted = await sess.auto_revert(fake_camilla, target_config_path="/x.yml")
    assert reverted is False
    assert sess.state == SessionState.VERIFIED
    assert sess.auto_revert_outcome is None  # nothing ran, nothing recorded

    # Second, concordant verify (same regression) → confirmed → revert verdict.
    await _run_verify(sess, verify_room_gain_db=20.0)
    assert sess.acceptance["verdict"] == "revert"
    assert sess.acceptance["confirmed"] is True
    assert sess.acceptance["verify_index"] == 2

    # Now auto_revert rolls back through reset() with the given target.
    reset_targets.clear()
    reverted = await sess.auto_revert(fake_camilla, target_config_path="/restore.yml")
    assert reverted is True
    assert sess.state == SessionState.IDLE
    assert reset_targets == ["/restore.yml"]
    # The completed rollback is recorded as fact...
    assert sess.auto_revert_outcome is not None
    assert sess.auto_revert_outcome["result"] == "ok"
    # ...and the REAL envelope tells the household what happened — the IDLE
    # screen carries the reverted copy, not the silent default (blocker pin
    # through the real session, not a fake).
    from jasper.correction.envelope import build_envelope

    env = build_envelope(sess)
    assert env["screen"] == "idle"
    assert "Reverted" in env["verdict_text"]
    assert "removed the correction" in env["verdict_text"]


@pytest.mark.asyncio
async def test_clean_confirmatory_verify_clears_the_concordance_flag(
    tmp_path: Path,
):
    """Adjudication (A) pin — STRICT ADJACENCY: regress → clean → regress
    yields REVERT_PENDING_CONFIRM on the third verify, never an instant
    REVERT off the stale first flag. The clean confirmatory sweep answered
    the pending question (first read = noise); a later regression must earn
    its own confirmatory re-measure."""
    sess = _make_session(tmp_path)

    async def fake_camilla(path: str) -> bool:
        return True

    await _measure_one_position(sess, room_gain_db=0.5)
    await sess.apply(fake_camilla)

    # Verify 1: clear regression → pending confirmation.
    await _run_verify(sess, verify_room_gain_db=20.0)
    assert sess.acceptance["verdict"] == "revert_pending_confirm"

    # Verify 2 (the confirmatory one): clean — the regression was noise.
    await _run_verify(sess, verify_room_gain_db=0.5)
    assert sess.acceptance["verdict"] in ("accept", "surface")

    # Verify 3: a regression again — a FRESH pending cycle, not a revert.
    await _run_verify(sess, verify_room_gain_db=20.0)
    assert sess.acceptance["verdict"] == "revert_pending_confirm"
    assert sess.acceptance["confirmed"] is False
    # No rollback ran at any point.
    assert sess.auto_revert_outcome is None
    assert sess.state == SessionState.VERIFIED


@pytest.mark.asyncio
async def test_auto_revert_camilla_reject_records_failed_outcome(
    tmp_path: Path,
):
    """A rollback whose config load is rejected records outcome=failed (and
    reset() fails the session loudly) — auto_revert never claims success."""
    sess = _make_session(tmp_path)

    async def fake_camilla_ok(path: str) -> bool:
        return True

    async def fake_camilla_reject(path: str) -> bool:
        return False

    await _measure_one_position(sess, room_gain_db=0.5)
    await sess.apply(fake_camilla_ok)
    await _run_verify(sess, verify_room_gain_db=20.0)  # pending
    await _run_verify(sess, verify_room_gain_db=20.0)  # confirmed → revert
    assert sess.acceptance_verdict == "revert"

    reverted = await sess.auto_revert(
        fake_camilla_reject, target_config_path="/restore.yml",
    )
    assert reverted is False
    assert sess.auto_revert_outcome is not None
    assert sess.auto_revert_outcome["result"] == "failed"
    # reset() failed the session loudly (no silent revert failure).
    assert sess.state == SessionState.FAILED


@pytest.mark.asyncio
async def test_auto_revert_falls_back_to_pre_apply_config(tmp_path: Path):
    """With no explicit target, auto_revert restores the pre-apply config the
    apply() path captured."""
    sess = _make_session(tmp_path)

    prior = "/etc/camilladsp/prior-graph.yml"

    async def fake_set(path: str) -> bool:
        fake_set.calls.append(path)
        return True

    fake_set.calls = []  # type: ignore[attr-defined]

    await _measure_one_position(sess, room_gain_db=0.5)
    await sess.apply(fake_set)  # compatibility path (no topology-aware carrier)
    # Simulate what the getter-driven apply() records on the real web path: the
    # pre-swap graph. (Driving the getter path here would exercise the
    # topology-aware carrier, which rejects tmp stub graphs — a separate,
    # already-tested concern; this test is about the auto-revert FALLBACK.)
    sess.pre_apply_config_path = prior

    await _run_verify(sess, verify_room_gain_db=20.0)  # regressed
    await _run_verify(sess, verify_room_gain_db=20.0)  # confirmed
    assert sess.acceptance_verdict == "revert"

    fake_set.calls.clear()
    reverted = await sess.auto_revert(fake_set)  # no explicit target
    assert reverted is True
    assert fake_set.calls == [prior]  # restored the captured pre-apply graph


@pytest.mark.asyncio
async def test_acceptance_verdict_lands_in_result_json(tmp_path: Path):
    """The verdict is recorded in the evidence bundle's result.json."""
    sess = _make_session(tmp_path)
    sess.save_bundles = True

    async def fake_camilla(path: str) -> bool:
        return True

    await _measure_one_position(sess, room_gain_db=8.0)
    await sess.apply(fake_camilla)
    await _run_verify(sess, verify_room_gain_db=0.5)

    result_path = sess.bundle_dir / "result.json"
    assert result_path.exists()
    data = json.loads(result_path.read_text())
    assert data["acceptance"] is not None
    assert data["acceptance"]["verdict"] == sess.acceptance["verdict"]
    assert data["position1"] is not None  # matched-basis curve recorded


@pytest.mark.asyncio
async def test_multi_position_verify_judges_against_position1_not_average(
    tmp_path: Path,
):
    """SF pin — the MATCHED basis is real, not a label: with divergent seats
    the position-1 ground truth and the spatial average yield DIFFERENT
    verdicts, and the session must follow position 1 (plan §4 P4 point 3).

    Seat 1 has a mild 80 Hz mode; seats 2-3 have big ones, so the spatial
    average is much worse than seat 1. The verify (captured at seat 1, same
    room as before) is a wash against the position-1 basis — but reads as a
    big "improvement" against the average. A mutation that stores the
    averaged curve in position1_curve, or an evaluator preferring
    measured_curve, produces "accept" here and fails."""
    sess = _make_session(tmp_path)

    async def fake_camilla(path: str) -> bool:
        return True

    await _measure_positions(sess, [2.0, 14.0, 14.0])
    assert sess.state == SessionState.READY
    assert sess.position1_curve is not None

    # position1_curve is the FIRST seat's curve, not the spatial average.
    pos1 = np.asarray(sess.position1_curve.magnitude_db)
    avg = np.asarray(sess.measured_curve.magnitude_db)
    assert np.allclose(pos1, sess.position_magnitudes[0])
    assert not np.allclose(pos1, avg)

    await sess.apply(fake_camilla)
    await _run_verify(sess, verify_room_gain_db=2.0)  # seat-1 room, unchanged

    assert isinstance(sess.acceptance, dict)
    assert sess.acceptance["basis"] == "position_1"
    # Ground truth at the matched seat: nothing changed → a wash → surface.
    assert sess.acceptance["verdict"] == "surface"

    # Discriminator: the SAME verify judged against the spatial average would
    # have read "accept" (a large apparent improvement) — proving this test
    # fails if the average sneaks in as the basis.
    from jasper.correction.acceptance import Verdict, evaluate_acceptance

    freqs = np.asarray(sess.verify_curve.freqs_hz)
    verify = np.asarray(sess.verify_curve.magnitude_db)
    target = np.asarray(sess.target_curve.magnitude_db)
    avg_result = evaluate_acceptance(
        freqs=freqs,
        before_db=np.interp(
            freqs, np.asarray(sess.measured_curve.freqs_hz), avg,
        ),
        verify_db=verify,
        target_db=target,
        f_high=sess.cfg.peq_f_high,
        basis="spatial_average",
    )
    assert avg_result.verdict is Verdict.ACCEPT
    assert avg_result.verdict.value != sess.acceptance["verdict"]
