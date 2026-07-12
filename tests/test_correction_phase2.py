# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Phase 2 — multi-position averaging + verify pass.

Extends Phase 1 single-position session tests with:
  - analysis.spatial_average_db (power-mean of magnitude responses)
  - analysis.deviation_metrics (RMS / max deviation in band)
  - 5-position session flow: each capture transitions through the
    state machine; final design uses the averaged response.
  - Verify pass: post-Apply re-measurement → VERIFIED state with
    verify_curve + verify_metrics populated.
  - Target curve choice: 'flat' / 'warm' / 'bright' affects the
    target the PEQ designer fits against.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.audio_measurement import analysis, sweep
from jasper.correction.session import SessionState
from .correction_session_fixtures import (
    make_measurement_session as _make_session,
)


# ---------- Spatial averaging ----------------------------------------------


def test_spatial_average_single_input_returns_self():
    a = np.array([0.0, -3.0, 2.0, -1.0])
    out = analysis.spatial_average_db([a])
    np.testing.assert_allclose(out, a)


def test_spatial_average_power_mean():
    """For two responses with magnitudes [0 dB, 0 dB] and [6 dB, 6 dB]
    at every frequency, the power-mean is 3.99 dB (10*log10(0.5*1 +
    0.5*4) ≈ 3.99). Verifies we average linear power, not dB."""
    a = np.zeros(10)            # 0 dB everywhere
    b = np.full(10, 6.0)        # 6 dB everywhere
    avg = analysis.spatial_average_db([a, b])
    expected = 10 * np.log10(0.5 * 1.0 + 0.5 * 10 ** 0.6)
    np.testing.assert_allclose(avg, expected, atol=0.01)


def test_spatial_average_deep_null_one_position_only():
    """The whole point of power-mean: a single -30 dB null at ONE
    position shouldn't drag the averaged response down to -30 dB.
    With 5 positions where 4 are flat at 0 dB and one has -30 dB:
      mean_power = (4 * 1 + 1 * 0.001) / 5 = 0.8002
      → ≈ -0.97 dB (not -6 dB which dB-mean would give)."""
    flat = np.zeros(20)
    null_pos = flat.copy()
    null_pos[10] = -30.0
    arrays = [flat, flat, flat, flat, null_pos]
    avg = analysis.spatial_average_db(arrays)
    # The other bins are still 0 dB.
    assert avg[5] == 0.0
    # The null bin is much shallower than -30 dB.
    assert -2.0 < avg[10] < 0.0


def test_spatial_average_empty_raises():
    with pytest.raises(ValueError):
        analysis.spatial_average_db([])


def test_deviation_metrics_in_band():
    freqs = np.geomspace(20.0, 20000.0, 200)
    measured = np.zeros_like(freqs)
    measured[(freqs >= 80) & (freqs <= 100)] = 6.0  # +6 dB peak
    target = np.zeros_like(freqs)
    metrics = analysis.deviation_metrics(measured, target, freqs)
    assert metrics["max_db"] == pytest.approx(6.0, abs=0.01)
    assert metrics["rms_db"] > 0
    assert metrics["n_points"] > 0


def test_deviation_metrics_outside_band_zero():
    freqs = np.geomspace(20.0, 20000.0, 200)
    measured = np.zeros_like(freqs)
    measured[freqs > 1000] = 10.0  # peak above the design band
    target = np.zeros_like(freqs)
    metrics = analysis.deviation_metrics(
        measured, target, freqs, f_low=20, f_high=350,
    )
    # 20-350 Hz band; the 1 kHz+ peak is excluded.
    assert metrics["max_db"] == pytest.approx(0.0, abs=0.001)


def test_deviation_metrics_default_band_excludes_iphone_hpf_zone():
    """Real bug a user hit: a verify pass reported "max 56 dB
    deviation" when the chart visibly showed maybe 15 dB swings —
    iPhone built-in mic has a 24 dB/oct HPF starting around 250 Hz,
    so deconvolved magnitudes below ~50 Hz are dominated by the mic's
    rolloff, not the room. Including 20-50 Hz in the deviation summary
    produced absurd numbers that scared the user even though the
    correction was working fine. Default f_low is now 50 Hz so the
    summary number is honest."""
    freqs = np.geomspace(20.0, 20000.0, 200)
    # Synthetic: -50 dB at 20 Hz (mic HPF artifact), flat 0 dB
    # everywhere else.
    measured = np.where(freqs < 40, -50.0, 0.0)
    target = np.zeros_like(freqs)
    # With OLD default (f_low=20), would include the -50 dB artifact.
    old_metrics = analysis.deviation_metrics(
        measured, target, freqs, f_low=20,
    )
    assert old_metrics["max_db"] >= 40.0  # artifact dominates
    # With NEW default (f_low=50), the artifact is excluded.
    new_metrics = analysis.deviation_metrics(
        measured, target, freqs,  # use defaults
    )
    assert new_metrics["max_db"] == pytest.approx(0.0, abs=0.001)


def test_deviation_metrics_f_low_default_is_50():
    """Pin the default explicitly so a future refactor that "just
    bumps it back to 20 for symmetry with PEQ design" gets caught."""
    import inspect
    sig = inspect.signature(analysis.deviation_metrics)
    assert sig.parameters["f_low"].default == 50.0


# ---------- Honest measured before/after -----------------------------------


def test_before_after_delta_uses_one_consistent_band():
    """The band-mismatch trap: a naive before/after would take the
    measured "before" over the PEQ design band (down to 20 Hz) and the
    verify "after" over 50-350 Hz, so the delta would subtract two
    different bands. `before_after_delta` computes BOTH sides over the
    SAME band, so a huge sub-50 Hz artifact in the "before" cannot leak
    into the delta.

    Construct a before curve that is flat 0 dB in-band (50-350 Hz) but
    -50 dB below 50 Hz (the iPhone-HPF artifact zone), and an after
    curve identical in-band. A band-consistent delta is ~0; a
    band-mismatched one (before over 20+ Hz, after over 50+ Hz) would
    report a large spurious change.
    """
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    before = np.where(freqs < 50.0, -50.0, 0.0)  # artifact only below band
    after = np.zeros_like(freqs)  # identical inside the band
    ba = analysis.before_after_delta(freqs, before, after, target)
    # Both sides are flat 0 dB across 50-350 Hz → deviations ~0, delta ~0.
    assert ba["band_hz"] == [50.0, 350.0]
    assert ba["before"]["rms_db"] == pytest.approx(0.0, abs=1e-9)
    assert ba["after"]["rms_db"] == pytest.approx(0.0, abs=1e-9)
    assert ba["delta"]["rms_db"] == pytest.approx(0.0, abs=1e-9)
    # Sub-50 Hz artifact never entered either metric.
    assert ba["before"]["n_points"] == ba["after"]["n_points"]


def test_before_after_delta_positive_when_deviation_shrinks():
    """A real +6 dB modal peak flattened to +1 dB → positive delta."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    peak = (freqs >= 78) & (freqs <= 82)
    before = np.where(peak, 6.0, 0.0)
    after = np.where(peak, 1.0, 0.0)
    ba = analysis.before_after_delta(freqs, before, after, target)
    assert ba["before"]["rms_db"] > ba["after"]["rms_db"]
    assert ba["delta"]["rms_db"] > 0
    assert ba["delta"]["max_db"] == pytest.approx(5.0, abs=0.01)


def test_before_after_delta_negative_when_deviation_grows():
    """If the "correction" made the room worse in-band, the delta is
    negative — we never dress a regression up as an improvement."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    band = (freqs >= 100) & (freqs <= 120)
    before = np.where(band, 2.0, 0.0)
    after = np.where(band, 8.0, 0.0)  # worse
    ba = analysis.before_after_delta(freqs, before, after, target)
    assert ba["delta"]["rms_db"] < 0
    assert ba["delta"]["max_db"] < 0


def test_fill_segments_tags_improved_and_regressed():
    """A band that got closer to target reads improved; a band that
    moved away reads regressed. Both must appear when both happen."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    good = (freqs >= 70) & (freqs <= 90)     # 6 → 1 dB (toward target)
    bad = (freqs >= 200) & (freqs <= 260)    # 1 → 5 dB (away from target)
    before = np.zeros_like(freqs)
    after = np.zeros_like(freqs)
    before[good] = 6.0
    after[good] = 1.0
    before[bad] = 1.0
    after[bad] = 5.0
    segs = analysis.before_after_fill_segments(freqs, before, after, target)
    tones = {s["tone"] for s in segs}
    assert "improved" in tones
    assert "regressed" in tones
    # The improved segment must cover ~70-90 Hz; the regressed ~200-260.
    improved = [s for s in segs if s["tone"] == "improved"]
    regressed = [s for s in segs if s["tone"] == "regressed"]
    assert any(s["f_lo_hz"] <= 90 and s["f_hi_hz"] >= 70 for s in improved)
    assert any(s["f_lo_hz"] <= 260 and s["f_hi_hz"] >= 200 for s in regressed)


def test_fill_segments_are_contiguous_and_index_aligned():
    """Segments tile the band with no gaps/overlaps and their indices
    address the same arrays the caller holds (so the browser can slice
    the before/after curves it already has)."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    before = np.zeros_like(freqs)
    after = np.zeros_like(freqs)
    after[(freqs >= 80) & (freqs <= 120)] = -3.0  # a regressed island
    segs = analysis.before_after_fill_segments(freqs, before, after, target)
    band_idx = np.nonzero((freqs >= 50) & (freqs <= 350))[0]
    # Segments partition the in-band index range exactly once.
    covered: list[int] = []
    for s in segs:
        assert s["i_hi"] >= s["i_lo"]
        covered.extend(range(s["i_lo"], s["i_hi"] + 1))
    assert covered == list(band_idx)
    # Frequency bounds match the indexed grid points.
    for s in segs:
        assert s["f_lo_hz"] == pytest.approx(float(freqs[s["i_lo"]]))
        assert s["f_hi_hz"] == pytest.approx(float(freqs[s["i_hi"]]))


def test_fill_segments_tie_reads_regressed_not_improved():
    """Equal distance-from-target is NOT an improvement. A curve that
    didn't move (before == after) must read regressed, never improved —
    honest framing refuses to claim gains without evidence."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    before = np.full_like(freqs, 3.0)
    after = np.full_like(freqs, 3.0)  # identical → no improvement
    segs = analysis.before_after_fill_segments(freqs, before, after, target)
    assert segs  # band is non-empty
    assert all(s["tone"] == "regressed" for s in segs)


def test_fill_segments_out_of_band_excluded():
    """Only the 50-350 Hz band is tagged — a swing at 1 kHz is ignored."""
    freqs = np.geomspace(20.0, 20000.0, 480)
    target = np.zeros_like(freqs)
    before = np.zeros_like(freqs)
    after = np.zeros_like(freqs)
    after[(freqs >= 900) & (freqs <= 1100)] = -6.0  # out of band
    segs = analysis.before_after_fill_segments(freqs, before, after, target)
    for s in segs:
        assert 50.0 <= s["f_lo_hz"]
        assert s["f_hi_hz"] <= 350.0


def test_fill_segments_length_mismatch_raises():
    freqs = np.geomspace(20.0, 20000.0, 10)
    with pytest.raises(ValueError, match="length mismatch"):
        analysis.before_after_fill_segments(
            freqs, np.zeros(9), np.zeros(10), np.zeros(10),
        )


# ---------- Session flow ----------------------------------------------------


def _synth_capture(
    sweep_signal: np.ndarray,
    sample_rate: int,
    *,
    mode_freq_hz: float = 80.0,
    mode_q: float = 4.0,
    mode_gain_db: float = 6.0,
) -> np.ndarray:
    n_fft = 8192
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    omega = freqs / mode_freq_hz
    safe = np.where(omega > 0, omega, 1.0)
    delta_oct = np.log2(safe)
    bw = 1.0 / mode_q
    mag_db = mode_gain_db / (1.0 + (delta_oct / bw) ** 2)
    mag_db[omega <= 0] = 0
    H_lin = 10 ** (mag_db / 20.0)
    h = np.fft.irfft(H_lin, n=n_fft)
    h = np.fft.fftshift(h)
    h = h[len(h) // 2 - 256: len(h) // 2 + 256].astype(np.float64)
    captured = fftconvolve(sweep_signal.astype(np.float64), h, mode="full")
    return captured / max(1.0, float(np.max(np.abs(captured))))


@pytest.mark.asyncio
async def test_multi_position_flow_5_positions(tmp_path: Path):
    """Drive the full 5-position flow: each capture transitions to
    NEEDS_NEXT_POSITION until the last one, which transitions
    through ANALYZING → READY with the averaged design."""
    sess = _make_session(tmp_path, total_positions=5)

    async def fake_play(path, **kw):
        return None

    for i in range(5):
        # Plays the i-th sweep.
        await sess.prepare_and_play_sweep(fake_play)
        assert sess.state == SessionState.AWAITING_CAPTURE

        # Synthesize a capture at this position. We vary the
        # synthetic mode slightly per position to mimic real-room
        # variation; the spatial averaging should smooth them out.
        sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
        captured = _synth_capture(
            sweep_signal, sr,
            mode_freq_hz=80.0 + i * 5,  # mode wanders 80..100 Hz
            mode_q=4.0,
            mode_gain_db=6.0,
        )
        cap_path = tmp_path / f"cap_{i}.wav"
        sweep.write_sweep_wav(cap_path, captured.astype(np.float32), sr)
        await sess.on_capture_uploaded(cap_path)

        if i < 4:
            assert sess.state == SessionState.NEEDS_NEXT_POSITION, (
                f"position {i}: expected NEEDS_NEXT_POSITION, got {sess.state.value}"
            )
            assert sess.current_position == i + 1
        else:
            assert sess.state == SessionState.READY
            assert sess.current_position == 5

    # The averaged response should have a peak somewhere in 80-100 Hz
    # (the mean of the synthetic modes), and the designer should
    # have placed at least one PEQ in that range.
    assert len(sess.peqs) >= 1
    peq_freqs = [p.freq_hz for p in sess.peqs]
    assert any(70 < f < 110 for f in peq_freqs), (
        f"expected PEQ near 80-100 Hz, got {peq_freqs}"
    )

    # Five positions worth of magnitudes captured.
    assert len(sess.position_magnitudes) == 5


@pytest.mark.asyncio
async def test_target_choice_affects_design(tmp_path: Path):
    """With target='warm' (Harman-like), the designer's target curve
    should slope downward, so a measured curve that's actually
    natural-shaped requires fewer cuts than against flat target."""
    flat_sess = _make_session(tmp_path / "flat", target_choice="flat")
    warm_sess = _make_session(tmp_path / "warm", target_choice="warm")

    async def fake_play(path, **kw):
        return None

    for sess in (flat_sess, warm_sess):
        await sess.prepare_and_play_sweep(fake_play)
        sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
        captured = _synth_capture(sweep_signal, sr)
        cap_path = sess.cfg.capture_dir.parent / f"cap_{sess.session_id}.wav"
        sess.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        cap_path = sess.cfg.capture_dir / "cap.wav"
        sweep.write_sweep_wav(cap_path, captured.astype(np.float32), sr)
        await sess.on_capture_uploaded(cap_path)

    # The two designs should differ — the warm target has +4 dB at
    # the sub-bass shelf (60 Hz and below), which means a measured
    # +6 dB peak at 80 Hz is "less excessive" relative to the warm
    # target than to flat. Concretely: the flat design should
    # ALWAYS produce filters (the synthetic peak is +6 dB above
    # flat target), and the two PEQ lists shouldn't be identical
    # (different residuals → different greedy picks).
    assert len(flat_sess.peqs) >= 1
    flat_signature = [(p.freq_hz, p.gain_db) for p in flat_sess.peqs]
    warm_signature = [(p.freq_hz, p.gain_db) for p in warm_sess.peqs]
    # Different target → different residuals → different PEQ set.
    assert flat_signature != warm_signature
    # Confirm target_choice was honored.
    assert flat_sess.target_choice == "flat"
    assert warm_sess.target_choice == "warm"


@pytest.mark.asyncio
async def test_verify_pass_after_apply(tmp_path: Path):
    """Apply, then start verify, then upload a synthetic verify
    capture. Final state = VERIFIED, verify_curve + verify_metrics
    populated."""
    sess = _make_session(tmp_path)

    async def fake_play(path, **kw):
        return None

    async def fake_camilla(path: str) -> bool:
        return True

    # Run a single-position measurement.
    await sess.prepare_and_play_sweep(fake_play)
    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    captured = _synth_capture(sweep_signal, sr)
    cap_path = tmp_path / "cap.wav"
    sweep.write_sweep_wav(cap_path, captured.astype(np.float32), sr)
    await sess.on_capture_uploaded(cap_path)
    assert sess.state == SessionState.READY

    await sess.apply(fake_camilla)
    assert sess.state == SessionState.APPLIED

    # Now verify.
    await sess.start_verify_sweep(fake_play)
    assert sess.state == SessionState.AWAITING_VERIFY_CAPTURE

    # Use a flatter synthetic capture to simulate "correction worked
    # — the post-correction room is more flat now."
    flat_capture = _synth_capture(
        sweep_signal, sr,
        mode_freq_hz=80.0, mode_q=4.0, mode_gain_db=1.0,  # smaller residual peak
    )
    verify_path = tmp_path / "verify.wav"
    sweep.write_sweep_wav(verify_path, flat_capture.astype(np.float32), sr)
    await sess.on_verify_capture_uploaded(verify_path)

    assert sess.state == SessionState.VERIFIED
    assert sess.verify_curve is not None
    assert sess.verify_metrics is not None
    assert sess.verify_metrics["rms_db"] >= 0
    assert sess.verify_metrics["max_db"] >= 0
    assert sess.verify_metrics["n_points"] > 0

    # Honest MEASURED before/after must be populated once verify lands.
    ba = sess.verify_before_after
    assert ba is not None
    # The verify handler passes f_high=peq_f_high (350) and default
    # f_low=50, so the before/after band must match verify_metrics'
    # band — this is the guard against the band-mismatch trap.
    assert ba["band_hz"] == [50.0, sess.cfg.peq_f_high]
    # `after` is the SAME measured deviation verify_metrics already
    # reported (same curve, same band) — the two must agree.
    assert ba["after"]["rms_db"] == pytest.approx(
        sess.verify_metrics["rms_db"], abs=1e-9,
    )
    # The flatter verify capture (1 dB residual) is closer to target
    # than the design measurement (6 dB residual) → measured improvement.
    assert ba["before"]["rms_db"] > ba["after"]["rms_db"]
    assert ba["delta"]["rms_db"] > 0
    assert ba["fill_segments"]
    # `before` is the pre-correction measured curve's deviation over the
    # SAME band — recompute it directly and confirm it was not sourced
    # from the design report's (different-band) predicted "before".
    pre = np.asarray(sess.measured_curve.magnitude_db, dtype=float)
    pre_f = np.asarray(sess.measured_curve.freqs_hz, dtype=float)
    tgt = sess._design_target(pre_f)
    expected_before = analysis.deviation_metrics(
        pre, tgt, pre_f, f_high=sess.cfg.peq_f_high,
    )
    assert ba["before"]["rms_db"] == pytest.approx(
        expected_before["rms_db"], abs=1e-9,
    )


@pytest.mark.asyncio
async def test_verify_from_wrong_state_raises(tmp_path: Path):
    sess = _make_session(tmp_path)

    async def fake_play(path, **kw):
        return None

    # IDLE — verify should not be allowed.
    with pytest.raises(RuntimeError, match="cannot verify"):
        await sess.start_verify_sweep(fake_play)


def test_session_default_target_is_flat(tmp_path: Path):
    sess = _make_session(tmp_path)
    assert sess.target_choice == "flat"


def test_session_invalid_target_falls_back_to_flat(tmp_path: Path):
    sess = _make_session(tmp_path, target_choice="bogus")
    assert sess.target_choice == "flat"
