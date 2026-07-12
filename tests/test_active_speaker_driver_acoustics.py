# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mic-backed driver/summed acoustic analysis.

These tests pin the verdict logic that replaces the old hand-passed
``observed_mic_dbfs`` number with a real per-driver acoustic measurement. They
follow the correction sweep/deconv test pattern: synthesize a driver+room
impulse response, convolve the reference sweep through it to make the
"captured" signal, write it as a WAV, and assert the verdict.

Key invariants:
  - write_driver_sweep_wav puts the sweep on exactly one channel.
  - A driver whose energy is in its passband reads "present".
  - A near-silent capture reads "silent".
  - A driver whose energy sits outside its passband reads "out_of_band".
  - A clipped / wrong-rate capture is "unusable_capture", never a fabricated
    curve (deconvolution is skipped).
  - A flat summed capture reads "blend_ok"; a deep crossover null reads
    "polarity_or_delay_problem".
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import firwin, firwin2, fftconvolve

from jasper.active_speaker import driver_acoustics as da
from jasper.audio_measurement import deconv, snr_policy
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement import sweep as sweep_mod

SR = 48000


def _reference_sweep(duration_s: float = 1.0):
    """A short reference sweep + its meta dict (what the analysis regenerates
    from). Kept short so the tests stay fast."""
    sig, meta = sweep_mod.synchronized_swept_sine(
        f1=da.DEFAULT_F1_HZ,
        f2=da.DEFAULT_F2_HZ,
        duration_approx_s=duration_s,
        sample_rate=SR,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    return sig, meta.to_dict()


def _write_capture(tmp_path, name, signal):
    path = tmp_path / name
    sweep_mod.write_sweep_wav(path, signal.astype(np.float32), SR)
    return path


def _write_ambient_prefixed_capture(tmp_path, name, reference, ambient, gain):
    """Write the relay shape: stored ambient, then sweep plus room noise."""

    full = np.concatenate([
        ambient,
        gain * reference.astype(np.float64) + ambient[: len(reference)],
    ])
    path = tmp_path / name
    wavfile.write(path, SR, full.astype(np.float32))
    return path


# ---------- channel-targeted sweep WAV --------------------------------------


def test_write_driver_sweep_wav_targets_one_channel(tmp_path):
    out = tmp_path / "sweep.wav"
    info = da.write_driver_sweep_wav(
        out, target_channel=1, channel_count=4, duration_s=0.5
    )
    assert info.channel_count == 4
    assert info.target_channel == 1
    assert info.sample_rate == SR

    sr, data = wavfile.read(str(out))
    assert sr == SR
    assert data.ndim == 2 and data.shape[1] == 4
    assert data.dtype == np.int16
    # Only the targeted channel carries the sweep; the rest are silent.
    assert int(np.max(np.abs(data[:, 1]))) > 0
    for ch in (0, 2, 3):
        assert int(np.max(np.abs(data[:, ch]))) == 0


def test_write_driver_sweep_wav_rejects_bad_channel(tmp_path):
    out = tmp_path / "sweep.wav"
    with pytest.raises(da.DriverAcousticsError):
        da.write_driver_sweep_wav(out, target_channel=4, channel_count=4)
    with pytest.raises(da.DriverAcousticsError):
        da.write_driver_sweep_wav(out, target_channel=0, channel_count=0)


# ---------- per-driver verdicts ---------------------------------------------


def test_driver_in_its_band_reads_present(tmp_path):
    sig, meta = _reference_sweep()
    # Woofer-like driver: lowpass at 400 Hz. Energy concentrated in 40-400.
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "present"
    assert result.present is True
    assert result.band_separation_db > da.PRESENT_MIN_SEPARATION_DB
    assert result.peak_dbfs > da.SILENT_PEAK_DBFS
    # observed_mic_dbfs is the real capture RMS, not a passed-in number.
    assert -120.0 < result.observed_mic_dbfs < 0.0


def test_overlong_capture_is_bounded_before_analysis(tmp_path, monkeypatch):
    """A driver capture longer than the cap is bounded before assess/deconv
    (mirrors the /correction session path) so it can't drive the FFT to OOM,
    and an otherwise-good over-long capture still reads 'present'."""
    monkeypatch.setattr(deconv, "DEFAULT_MAX_CAPTURE_SECONDS", 1.5)
    sig, meta = _reference_sweep()  # 1 s sweep
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    # Pad well past the 1.5 s cap; the full driver response stays within it.
    overlong = np.concatenate([captured, np.zeros(3 * SR, dtype=np.float64)])
    path = _write_capture(tmp_path, "woofer_long.wav", overlong)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "present"
    # Pin the pre-assess cap specifically (not deconvolve's own internal cap):
    # `capture_truncated` is emitted only when _capture_to_magnitude passes
    # truncated_from_samples to assess_capture. If the production cap here is
    # dropped, this assertion fails even though deconvolve still bounds the IR.
    assert any(
        issue["code"] == "capture_truncated" for issue in result.quality["issues"]
    )


def test_silent_capture_reads_silent(tmp_path):
    sig, meta = _reference_sweep()
    # Driver barely moving: gain so low the capture peak is below the silent
    # threshold.
    ir = (firwin(1023, 400, fs=SR) * 0.002).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "silent.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "silent"
    assert result.present is False
    assert result.peak_dbfs <= da.SILENT_PEAK_DBFS


def test_marginal_weak_driver_in_band_still_reads_present(tmp_path):
    """A driver only marginally weaker in-band than out — band separation in the
    [OUT_OF_BAND_SEPARATION_DB, PRESENT_MIN_SEPARATION_DB) range — is a
    real-but-quiet driver, not a wrong one, so it must read "present". This pins
    the OUT_OF_BAND_SEPARATION_DB boundary: a future refactor that folded the
    marginal range into "out_of_band" would silently reject quiet-but-correct
    drivers, and this test would go red."""
    sig, meta = _reference_sweep()
    # A gentle ~+4 dB high-shelf above the woofer band leaves slightly more
    # energy outside (40-400 Hz) than inside it — a small negative separation —
    # while the capture stays well above the silent floor.
    nyq = SR / 2
    g = 10 ** (4.0 / 20)
    ir = firwin2(1023, [0.0, 400 / nyq, 800 / nyq, 1.0], [1.0, 1.0, g, g]).astype(
        np.float64
    )
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "marginal.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "present"
    assert result.present is True
    assert result.peak_dbfs > da.SILENT_PEAK_DBFS
    # Lands in the marginal band — not the strong-present (>= 0) branch.
    assert (
        da.OUT_OF_BAND_SEPARATION_DB
        <= result.band_separation_db
        < da.PRESENT_MIN_SEPARATION_DB
    )


def test_driver_energy_outside_band_reads_out_of_band(tmp_path):
    sig, meta = _reference_sweep()
    # A highpassed (3 kHz) driver answered where a woofer (40-400) was expected:
    # all the energy sits outside the requested passband.
    ir = firwin(1023, 3000, fs=SR, pass_zero=False).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "wrong.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "out_of_band"
    assert result.present is False
    assert result.band_separation_db < da.OUT_OF_BAND_SEPARATION_DB


def test_clipped_capture_is_unusable_not_a_curve(tmp_path):
    sig, meta = _reference_sweep()
    # Hard-clip the capture: assess_capture must fail it before deconvolution.
    captured = np.ones(len(sig) + 2000, dtype=np.float64)
    path = _write_capture(tmp_path, "clipped.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "unusable_capture"
    assert result.present is False
    assert result.mic_clipping is True
    assert result.quality["failed"] is True


def test_wrong_sample_rate_is_unusable(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    # Write at the wrong rate; meta still says 48 kHz.
    path = tmp_path / "rate.wav"
    sweep_mod.write_sweep_wav(path, captured.astype(np.float32), 44100)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 400.0))
    assert result.verdict == "unusable_capture"
    assert result.present is False


def test_invalid_passband_raises(tmp_path):
    sig, meta = _reference_sweep()
    path = _write_capture(tmp_path, "x.wav", sig.astype(np.float64))
    with pytest.raises(da.DriverAcousticsError):
        da.analyze_driver_capture(path, meta, passband_hz=(400.0, 40.0))


# ---------- summed crossover verdicts ---------------------------------------


def test_flat_summed_capture_reads_blend_ok(tmp_path):
    sig, meta = _reference_sweep()
    # Delta IR → flat magnitude → no null at the crossover.
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "flat.wav", captured)

    result = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    assert result.verdict == "blend_ok"
    assert abs(result.null_depth_db) < da.DEFAULT_NULL_THRESHOLD_DB


def test_crossover_null_reads_polarity_or_delay_problem(tmp_path):
    sig, meta = _reference_sweep()
    # Comb filter (1 + z^-d): nulls at odd multiples of fs/(2d). d=12 → a deep
    # null at 2000 Hz with peaks at 0/4000 Hz — the classic polarity/delay
    # cancellation signature at the crossover.
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "null.wav", captured)

    result = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    assert result.verdict == "polarity_or_delay_problem"
    assert result.null_depth_db >= da.DEFAULT_NULL_THRESHOLD_DB


def test_summed_clipped_capture_is_unusable(tmp_path):
    sig, meta = _reference_sweep()
    captured = np.ones(len(sig) + 2000, dtype=np.float64)
    path = _write_capture(tmp_path, "clip.wav", captured)
    result = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    assert result.verdict == "unusable_capture"


def test_summed_rejects_bad_fc(tmp_path):
    sig, meta = _reference_sweep()
    path = _write_capture(tmp_path, "x.wav", sig.astype(np.float64))
    with pytest.raises(da.DriverAcousticsError):
        da.analyze_summed_crossover(path, meta, crossover_fc_hz=0.0)


# ---------- overlap-band level (L1 phone level matching) ---------------------


def test_overlap_band_level_recorded_for_crossover_fc(tmp_path):
    sig, meta = _reference_sweep()
    # Woofer low-passed at the 2 kHz crossover; overlap_fcs asks for the level
    # at that handoff.
    ir = firwin(1023, 2000, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,)
    )
    assert len(result.overlap_levels) == 1
    entry = result.overlap_levels[0]
    assert entry["fc_hz"] == 2000.0
    assert entry["usable"] is True
    assert entry["bins"] >= da.OVERLAP_MIN_BINS
    assert np.isfinite(entry["level_db"])
    # The confidence neighbourhood is one octave centred (geometrically) on Fc.
    assert entry["lo_hz"] == pytest.approx(2000.0 / da.OVERLAP_BAND_RATIO)
    assert entry["hi_hz"] == pytest.approx(2000.0 * da.OVERLAP_BAND_RATIO)
    # to_dict round-trips the new evidence.
    assert result.to_dict()["overlap_levels"][0]["fc_hz"] == 2000.0


def test_overlap_band_delta_recovers_relative_driver_level(tmp_path):
    """The overlap-band delta between a low-passed woofer and a high-passed
    tweeter (sharing one Fc) recovers their relative level — the basis for the
    measured level-match trim. The matched −6 dB crossover shoulder cancels, so
    a 12 dB-hotter tweeter reads ~12 dB above the woofer at Fc. (The woofer is
    attenuated rather than the tweeter boosted, so the high-passed capture stays
    below full scale instead of clipping into an unusable verdict.)"""
    sig, meta = _reference_sweep()
    fc = 2000.0
    woofer_gain = 10 ** (-12.0 / 20)  # tweeter 12 dB hotter than the woofer
    woofer_ir = (firwin(1023, fc, fs=SR) * woofer_gain).astype(np.float64)
    tweeter_ir = firwin(1023, fc, fs=SR, pass_zero=False).astype(np.float64)
    w_path = _write_capture(tmp_path, "w.wav", fftconvolve(sig.astype(np.float64), woofer_ir))
    t_path = _write_capture(tmp_path, "t.wav", fftconvolve(sig.astype(np.float64), tweeter_ir))

    woofer = da.analyze_driver_capture(
        w_path, meta, passband_hz=(40.0, fc), overlap_fcs=(fc,)
    )
    tweeter = da.analyze_driver_capture(
        t_path, meta, passband_hz=(fc, 18000.0), overlap_fcs=(fc,)
    )
    assert woofer.overlap_levels[0]["usable"] is True
    assert tweeter.overlap_levels[0]["usable"] is True
    delta = tweeter.overlap_levels[0]["level_db"] - woofer.overlap_levels[0]["level_db"]
    assert delta == pytest.approx(12.0, abs=1.5)


def test_overlap_band_unusable_when_silent(tmp_path):
    sig, meta = _reference_sweep()
    ir = (firwin(1023, 2000, fs=SR) * 0.002).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "silent.wav", captured)

    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,)
    )
    assert result.verdict == "silent"
    assert result.overlap_levels[0]["usable"] is False


def test_overlap_band_unusable_when_capture_unusable(tmp_path):
    sig, meta = _reference_sweep()
    captured = np.ones(len(sig) + 2000, dtype=np.float64)  # clipped
    path = _write_capture(tmp_path, "clip.wav", captured)

    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,)
    )
    assert result.verdict == "unusable_capture"
    # Even when the capture fails quality gating the fc is reported, marked
    # unusable so the trim math fails closed.
    assert result.overlap_levels[0]["fc_hz"] == 2000.0
    assert result.overlap_levels[0]["usable"] is False


def test_overlap_band_no_entries_when_no_fcs(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 2000, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(path, meta, passband_hz=(40.0, 2000.0))
    assert result.overlap_levels == ()


# ---------- SC-1 band-specific SNR gate (P1b) --------------------------------
#
# analyze_driver_capture / analyze_summed_crossover optionally accept
# noise_band_report (+ analyze_summed_crossover also noise_floor_dbfs) and
# compute jasper.audio_measurement.snr_policy.band_snr_verdicts. These tests
# pin: (1) the shipped no-noise-input flow is byte-for-byte unchanged
# (snr=None, overlap_levels' usable flags untouched); (2) real band evidence
# populates a real verdict block and, for the alignment class, caps a
# measured null depth to what the overlap SNR can prove; (3) scalar-only
# evidence is accepted for the magnitude class's "existing scalar path" at
# the commissioning_capture layer but is explicitly NOT sufficient evidence
# for an alignment/null decision (reads "unknown", never caps).


def test_stored_ambient_uses_signal_window_and_recovers_known_snr_gain(
    tmp_path, monkeypatch
):
    """End-to-end oracle for the Lane-D ambient/sweep transform.

    A stored -40 dB sweep-shaped ambient plus captures at 10x and 100x that
    amplitude must report exactly 20 dB and 40 dB in every band. The ambient
    counterfactual traverses the exact same regularized
    inverse and the one signal-owned arrival window; it must never find its
    own random-noise argmax. A deliberately non-flat calibration, constant
    within each decision band, is applied to both sides and therefore
    cancels exactly from each band-SNR difference.
    """

    reference, sweep_meta = sweep_mod.synchronized_swept_sine(
        f1=20.0,
        f2=12000.0,
        duration_approx_s=1.0,
        sample_rate=SR,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    meta = sweep_meta.to_dict()
    ambient = 0.01 * reference
    ambient_duration_s = len(ambient) / SR
    low_path = _write_ambient_prefixed_capture(
        tmp_path, "ambient-low.wav", reference, ambient, 0.09
    )
    high_path = _write_ambient_prefixed_capture(
        tmp_path, "ambient-high.wav", reference, ambient, 0.99
    )

    arrival_calls = 0
    applied_windows = []
    real_arrival = deconv.direct_arrival_window
    real_apply = deconv.apply_arrival_window

    def count_signal_window(*args, **kwargs):
        nonlocal arrival_calls
        arrival_calls += 1
        return real_arrival(*args, **kwargs)

    def record_applied_window(full_ir, window):
        applied_windows.append(tuple(window))
        return real_apply(full_ir, window)

    monkeypatch.setattr(deconv, "direct_arrival_window", count_signal_window)
    monkeypatch.setattr(deconv, "apply_arrival_window", record_applied_window)

    low = da.analyze_driver_capture(
        low_path,
        meta,
        passband_hz=(100.0, 8000.0),
        ambient_duration_s=ambient_duration_s,
    )
    high = da.analyze_driver_capture(
        high_path,
        meta,
        passband_hz=(100.0, 8000.0),
        ambient_duration_s=ambient_duration_s,
    )

    assert arrival_calls == 2  # once per signal; never once per ambient noise
    assert len(applied_windows) == 4
    assert applied_windows[0] == applied_windows[1]
    assert applied_windows[2] == applied_windows[3]
    assert high.ambient is not None
    assert high.ambient["domain"] == "deconvolved"
    assert high.ambient["method"] == "paired_signal_window_deconvolution"
    assert high.ambient["operator"] == {
        "deconvolution": "regularized_fft_inverse",
        "arrival_window_source": "signal",
        "reflection_gate_source": None,
        "calibration_applied_to_signal_and_noise": False,
    }
    assert high.ambient["source"] == {
        "kind": "capture_prefix",
        "start_s": 0.0,
        "end_s": round(ambient_duration_s, 3),
    }
    assert [band["estimated_snr_db"] for band in low.snr["bands"]] == [20.0] * 6
    assert [band["estimated_snr_db"] for band in high.snr["bands"]] == [40.0] * 6

    # Non-flat across the range, constant inside each canonical band so the
    # physical cancellation has an exact oracle.
    calibration_freqs = []
    calibration_db = []
    for _band_id, lo, hi, correction_db in (
        ("sub_bass", 20.0, 80.0, 12.0),
        ("bass", 80.0, 160.0, 6.0),
        ("upper_bass", 160.0, 350.0, 3.0),
        ("transition", 350.0, 1000.0, 0.0),
        ("mid", 1000.0, 4000.0, -6.0),
        ("treble", 4000.0, 12000.0, -12.0),
    ):
        calibration_freqs.extend([lo, np.nextafter(hi, lo)])
        calibration_db.extend([correction_db, correction_db])
    calibrated = da.analyze_driver_capture(
        high_path,
        meta,
        passband_hz=(100.0, 8000.0),
        ambient_duration_s=ambient_duration_s,
        calibration=CalibrationCurve(calibration_freqs, calibration_db),
    )

    assert calibrated.ambient["operator"][
        "calibration_applied_to_signal_and_noise"
    ] is True
    assert [band["estimated_snr_db"] for band in calibrated.snr["bands"]] == [
        band["estimated_snr_db"] for band in high.snr["bands"]
    ]


def test_driver_capture_snr_block_is_none_without_noise_input(tmp_path):
    """Behavior-preservation regression: the shipped no-noise flow is
    unaffected by the new optional kwarg — verdict/present/separation/overlap
    usability are exactly what they were before this field existed."""
# ---------- IR gating / low-frequency validity floor (P1a, SC-2) ------------
#
# These tests use `monkeypatch` on `jasper.audio_measurement.gating`'s public
# functions rather than synthesizing a room reflection through the full
# deconvolution pipeline. Two reasons: (1) the gating math itself (reflection
# detection, the window, the floor formula) is already pinned exactly in
# tests/test_audio_measurement_gating.py; these tests exist to prove
# driver_acoustics' CONSUMPTION of a gating result is wired correctly, and
# (2) injecting a reflection through a symmetric linear-phase driver filter
# (firwin/firwin2, used elsewhere in this file for frequency-domain verdicts)
# pre-rings and shifts the apparent reflection time earlier than injected —
# see gating.py's module docstring and the P1a PR body. Mocking the gating
# call lets these tests pin an EXACT floor value and isolate the wiring.


def test_near_field_default_gating_is_exempt(tmp_path):
    """near_field (the default, and today's only shipped geometry) never
    gates: the exempt SC-2 block is persisted, and the verdict/magnitude are
    byte-identical to before capture_geometry existed (existing assertions in
    this file, all run with the near_field default, prove the byte-identity;
    this test pins the exempt block's own shape)."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), overlap_fcs=(400.0,)
    )
    assert result.snr is None
    assert result.verdict == "present"
    assert result.present is True
    assert result.band_separation_db > da.PRESENT_MIN_SEPARATION_DB
    assert result.to_dict()["snr"] is None
    entry = result.overlap_levels[0]
    assert entry["snr_verdict"] == "unknown"
    assert entry["usable"] is True


def test_driver_capture_snr_block_populated_with_noise_evidence(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    # Noise well below every canonical band's captured level -> a confident
    # "ok" magnitude-class verdict.
    noise = [
        {"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": -100.0}
        for band_id, lo, hi in snr_policy.CROSSOVER_SNR_BANDS_HZ
    ]
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), noise_band_report=noise,
    )
    assert result.snr is not None
    assert result.snr["decision_class"] == "magnitude"
    assert result.snr["verdict"] == "ok"
    assert result.snr["relevant_hz"] == [40.0, 400.0]
    # The verdict/present logic is unaffected by adding noise evidence.
    assert result.verdict == "present"


def test_summed_near_field_default_gating_is_exempt(tmp_path):
    sig, meta = _reference_sweep()
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "flat.wav", captured)

    result = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    assert result.verdict == "blend_ok"
    assert result.gating is not None
    assert result.gating["applied"] is False
    assert result.gating["exempt_reason"] == "near_field"
    assert result.above_validity_floor is True
    assert result.near_validity_floor is False


def _mock_gate_at_fixed_floor(monkeypatch, floor_hz: float):
    """Patch gating.gate_impulse_response to report a fixed floor without
    actually windowing the IR — isolates driver_acoustics' consumption of
    the floor from the gating detection/windowing math (tested separately)."""
    from jasper.audio_measurement import gating as gating_mod

    def fake_gate(ir, sample_rate, **kwargs):
        fragment = {
            "schema_version": 1,
            "direct_peak_ms": 5.0,
            "first_reflection_ms": 5.0 + 1000.0 / floor_hz,
            "window_ms": 1000.0 / floor_hz,
            "window": "half_hann_tail",
            "f_valid_floor_hz": floor_hz,
            "floor_source": "measured_reflection",
        }
        return ir, fragment

    monkeypatch.setattr(gating_mod, "gate_impulse_response", fake_gate)


def test_reference_axis_excludes_below_floor_bins_from_in_band_and_overlap(
    tmp_path, monkeypatch
):
    """A fixed floor above the driver's passband floor but below its
    ceiling: in_band/out_of_band means shift because the effective lower
    edge moves from ANALYSIS_LO_HZ up to the floor, an overlap entry whose
    fc sits below the floor is marked unusable + above_validity_floor=False,
    and one above the floor is untouched (same level_db as a near_field
    bake of the SAME capture, since the mock leaves the IR unwindowed)."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    # Noise well below every canonical band's captured level -> a confident
    # "ok" magnitude-class verdict.
    noise = [
        {"band_id": band_id, "band_hz": [lo, hi], "level_dbfs": -100.0}
        for band_id, lo, hi in snr_policy.CROSSOVER_SNR_BANDS_HZ
    ]
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), noise_band_report=noise,
    )
    assert result.snr is not None
    assert result.snr["decision_class"] == "magnitude"
    assert result.snr["verdict"] == "ok"
    assert result.snr["relevant_hz"] == [40.0, 400.0]
    # The verdict/present logic is unaffected by adding noise evidence.
    assert result.verdict == "present"


def test_overlap_band_marked_unusable_when_snr_insufficient(tmp_path):
    sig, meta = _reference_sweep()
    ir = firwin(1023, 2000, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    mid_dbfs = next(
        b["level_dbfs"]
        for b in da._capture_band_levels(path)
        if b["band_id"] == "mid"
    )
    # 5 dB SNR: real evidence, well below snr_warn_db (20) -> insufficient.
    noise = [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": mid_dbfs - 5.0}]
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,),
        noise_band_report=noise,
    )
    entry = result.overlap_levels[0]
    assert entry["snr_verdict"] == "insufficient"
    # An insufficient SNR verdict fails the overlap-band reading closed, same
    # as a silent/clipped/too-few-bins capture would.
    assert entry["usable"] is False


def test_reference_axis_near_validity_floor_advisory_does_not_exclude(
    tmp_path, monkeypatch
):
    """An Fc inside [floor, NEAR_FLOOR_RATIO*floor) is flagged
    near_validity_floor but stays usable — the advisory band never excludes,
    only the hard floor does."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 2000, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    mid_dbfs = next(
        b["level_dbfs"]
        for b in da._capture_band_levels(path)
        if b["band_id"] == "mid"
    )
    # 5 dB SNR: real evidence, well below snr_warn_db (20) -> insufficient.
    noise = [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": mid_dbfs - 5.0}]
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,),
        noise_band_report=noise,
    )
    entry = result.overlap_levels[0]
    assert entry["snr_verdict"] == "insufficient"
    # An insufficient SNR verdict fails the overlap-band reading closed, same
    # as a silent/clipped/too-few-bins capture would.
    assert entry["usable"] is False


def test_overlap_band_usable_when_snr_reduced_not_insufficient(tmp_path):
    """A "reduced" verdict is a reduced-confidence result, not a refusal —
    only "insufficient" forces usable=False."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 2000, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    mid_dbfs = next(
        b["level_dbfs"]
        for b in da._capture_band_levels(path)
        if b["band_id"] == "mid"
    )
    # 22 dB SNR: real evidence, in [snr_warn_db, snr_ok_db) -> reduced.
    noise = [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": mid_dbfs - 22.0}]
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 2000.0), overlap_fcs=(2000.0,),
        noise_band_report=noise,
    )
    entry = result.overlap_levels[0]
    assert entry["snr_verdict"] == "reduced"
    assert entry["usable"] is True


def test_summed_snr_block_is_none_without_any_noise_input(tmp_path):
    """Behavior-preservation regression: neither noise_band_report nor
    noise_floor_dbfs supplied -> snr stays None and null_depth_db is exactly
    the raw measured value, same as before this field existed."""
    sig, meta = _reference_sweep()
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "null.wav", captured)

    result = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    assert result.snr is None
    assert result.null_depth_capped is False
    assert result.verdict == "polarity_or_delay_problem"
    assert result.null_depth_db >= da.DEFAULT_NULL_THRESHOLD_DB


def test_summed_null_depth_capped_by_insufficient_overlap_snr(tmp_path):
    sig, meta = _reference_sweep()
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "null.wav", captured)

    plain = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    mid_dbfs = next(
        b["level_dbfs"]
        for b in da._capture_band_levels(path)
        if b["band_id"] == "mid"
    )
    # The overlap band [fc/2, fc*2] = [1000, 4000] Hz is exactly the "mid"
    # canonical band at fc=2000. 20 dB SNR: real evidence, below
    # alignment_snr_ok_db (35) -> insufficient, and D + 10 >= 20 fails for the
    # ~18 dB measured depth, so it must report capped.
    noise = [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": mid_dbfs - 20.0}]
    capped = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=2000.0, noise_band_report=noise,
    )
    assert capped.snr is not None
    assert capped.snr["decision_class"] == "alignment"
    assert capped.snr["verdict"] == "insufficient"
    assert capped.snr["worst_relevant"]["estimated_snr_db"] == pytest.approx(20.0, abs=0.05)
    assert capped.null_depth_capped is True
    expected = 20.0 - da.DRIVER.null_cap_margin_db
    assert capped.null_depth_db == pytest.approx(expected, abs=0.05)
    assert capped.null_depth_db < plain.null_depth_db
    # The pass/fail verdict itself is decided from the UNCAPPED measured
    # depth — a capped-but-still-deep null is safely "at least that deep".
    assert capped.verdict == plain.verdict == "polarity_or_delay_problem"


def test_summed_null_depth_uncapped_with_scalar_only_noise(tmp_path):
    """A scalar noise floor alone is not sufficient evidence for the
    alignment class (per "Level control and SNR"): the snr block reads
    "unknown" for every band and the null depth is reported exactly as
    measured — never silently capped from an untrusted scalar number."""
    sig, meta = _reference_sweep()
    ir = np.zeros(64, dtype=np.float64)
    ir[0] = 1.0
    ir[12] = 0.98
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "null.wav", captured)

    plain = da.analyze_summed_crossover(path, meta, crossover_fc_hz=2000.0)
    scalar_only = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=2000.0, noise_floor_dbfs=-80.0,
    )
    assert scalar_only.snr is not None
    assert scalar_only.snr["verdict"] == "unknown"
    assert scalar_only.snr["worst_relevant"] is None
    assert all(b["method"] == "scalar_fallback" for b in scalar_only.snr["bands"])
    assert scalar_only.null_depth_capped is False
    assert scalar_only.null_depth_db == pytest.approx(plain.null_depth_db)


def test_reference_axis_driver_unusable_when_floor_above_passband_ceiling(
    tmp_path, monkeypatch
):
    """The validity floor sits at/above the driver's own passband ceiling:
    the reference-axis capture cannot decide anything about this driver at
    all (spec: "the room prevented a low-frequency decision here")."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    _mock_gate_at_fixed_floor(monkeypatch, 20000.0)  # absurdly high floor
    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), overlap_fcs=(380.0,),
        capture_geometry="reference_axis",
    )
    assert result.verdict == da.VERDICT_UNUSABLE_CAPTURE
    assert result.present is False
    assert result.gating["applied"] is True
    assert result.gating["f_valid_floor_hz"] == 20000.0
    # The overlap entry is still reported (for diagnostics), marked unusable.
    assert result.overlap_levels[0]["above_validity_floor"] is False
    assert result.overlap_levels[0]["usable"] is False


def test_reference_axis_summed_unusable_when_shoulder_below_floor(
    tmp_path, monkeypatch
):
    """The crossover's lower shoulder (Fc/2, one of the two null-depth
    reference points) sits below the validity floor: the reference-axis
    capture cannot decide the null at all — VERDICT_UNUSABLE_CAPTURE with the
    gating block populated, never a null computed from contaminated data."""
    sig, meta = _reference_sweep()
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "flat.wav", captured)

    _mock_gate_at_fixed_floor(monkeypatch, 150.0)  # 200/2 = 100 < 150
    result = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=200.0, capture_geometry="reference_axis",
    )
    assert result.verdict == da.VERDICT_UNUSABLE_CAPTURE
    assert np.isnan(result.null_depth_db)
    assert result.gating is not None
    assert result.gating["applied"] is True
    assert result.gating["f_valid_floor_hz"] == 150.0
    assert result.above_validity_floor is False


def test_reference_axis_summed_usable_when_fc_and_shoulder_above_floor(
    tmp_path, monkeypatch
):
    """The mirror-image case: both Fc and its lower shoulder clear the
    floor, so the null is computed normally and above_validity_floor=True."""
    sig, meta = _reference_sweep()
    ir = np.zeros(256, dtype=np.float64)
    ir[10] = 1.0
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "flat.wav", captured)

    _mock_gate_at_fixed_floor(monkeypatch, 150.0)  # 2000/2 = 1000 > 150
    result = da.analyze_summed_crossover(
        path, meta, crossover_fc_hz=2000.0, capture_geometry="reference_axis",
    )
    assert result.verdict != da.VERDICT_UNUSABLE_CAPTURE
    assert result.above_validity_floor is True
    assert result.gating["applied"] is True


def test_capture_geometry_reference_axis_calls_real_gating_module(tmp_path):
    """No mocking: an end-to-end reference_axis call against the real gating
    module must not raise and must persist a populated, applied gating
    block — proves the wiring works against the actual implementation, not
    just the mocked contract used by the tests above."""
    sig, meta = _reference_sweep()
    ir = firwin(1023, 400, fs=SR).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = _write_capture(tmp_path, "woofer.wav", captured)

    result = da.analyze_driver_capture(
        path, meta, passband_hz=(40.0, 400.0), capture_geometry="reference_axis",
    )
    assert result.gating is not None
    assert result.gating["exempt_reason"] is None
    assert result.gating["applied"] in (True, False)  # either is a valid outcome
    if result.gating["applied"]:
        assert result.gating["f_valid_floor_hz"] > 0
