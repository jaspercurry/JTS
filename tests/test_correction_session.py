"""End-to-end MeasurementSession test on synthetic data.

Synthesizes a "room" with a known modal peak, runs the full session
flow (sweep → playback stub → upload → analyze → apply → reset),
and verifies the PEQ list contains a filter near the synthetic mode.

This is the integration-level confidence that all the pieces hook
up correctly. Unit tests for individual modules already pin the
math; this test pins the wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.correction import bundles, quality, runtime_integrity, sweep
from jasper.correction.calibration import store_calibration
from jasper.correction.session import MeasurementSession, SessionConfig, SessionState


def _make_session(tmp_path: Path) -> MeasurementSession:
    """Session pointed at tmp_path subdirs so the test doesn't write
    to /var/lib/jasper or /var/lib/camilladsp."""
    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        sessions_dir=tmp_path / "sessions",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        # Short sweep keeps tests fast.
        duration_s=1.0,
    )
    # Make a stub base config so /reset has a target.
    cfg.base_config_path.write_text("# stub base v1.yml for tests\n")
    return MeasurementSession(cfg)


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

    from jasper.correction import calibration as cal_mod

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
import asyncio  # noqa: E402


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


# --- reset() must not race an in-flight sweep/analysis task -----------------
@pytest.mark.asyncio
async def test_reset_rejected_while_a_sweep_or_analysis_is_in_flight(tmp_path: Path):
    # During preparing/sweeping/analyzing/verifying a fire-and-forget task is
    # running and will set the next state AFTER reset's IDLE. The server
    # rejects reset there (the wizard never offers Cancel/Reset from those
    # states), so it can't touch CamillaDSP mid-task.
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
        with pytest.raises(RuntimeError, match="in progress"):
            await sess.reset(fake_reset)


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


# --- Bug 2 regression: autolevel cap math --------------------------------
# The maxed_out UI previously hardcoded "(-6 dB)" instead of the real cap,
# misleading the user. The cap is computed here; pin it so the UI can read
# cap_db with confidence and the hardcoded-constant class of bug can't recur.
def test_compute_autolevel_cap_clamps():
    from jasper.correction.session import compute_autolevel_cap

    def cap(original):
        return compute_autolevel_cap(
            original, bump_db=6.0, floor_db=-20.0, ceil_db=-6.0
        )

    assert cap(-19.0) == -13.0   # +6 bump lands inside the band
    assert cap(-28.5) == -20.0   # very quiet listener floored UP to usable
    assert cap(-2.0) == -6.0     # loud listener clamped to the safety ceiling
    assert cap(-12.0) == -6.0    # -12+6 = -6 exactly at the ceiling
