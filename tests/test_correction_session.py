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
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.correction import sweep
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
    cap_path = tmp_path / "capture.wav"
    sweep.write_sweep_wav(cap_path, sweep_signal.astype(np.float32), sr)

    await sess.on_capture_uploaded(cap_path)

    assert called["value"] is True
    assert sess.state == SessionState.READY
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
