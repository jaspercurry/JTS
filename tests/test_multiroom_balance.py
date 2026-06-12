"""Pair-balance measurement core (jasper/multiroom/balance.py).

The acoustic loop is closed synthetically: a fake room capture is
rendered from the same schedule the leader would play — each burst
scaled by an imposed per-speaker gain, plus a noise floor and a
random capture-start offset — and the pipeline must recover the
imposed left/right delta through alignment, gating, and band RMS.
"""

import numpy as np
import pytest

from jasper.multiroom import balance
from jasper.multiroom.balance import (
    BURST_F_HI,
    BURST_F_LO,
    BalanceSchedule,
    TrimRecommendation,
    band_rms_dbfs,
    build_balance_schedule,
    evaluate_capture,
    recommend_trims,
    synth_balance_burst,
    write_balance_wav,
)

SR = 48000


# ---------------------------------------------------------------------------
# Burst synthesis


def test_burst_is_deterministic():
    a = synth_balance_burst(SR)
    b = synth_balance_burst(SR)
    assert np.array_equal(a, b)


def test_burst_peak_at_amplitude():
    burst = synth_balance_burst(SR, amplitude_dbfs=-12.0)
    peak_db = 20 * np.log10(np.max(np.abs(burst)))
    # Fades can only reduce the peak; the pre-fade normalization pins it.
    assert -12.6 <= peak_db <= -11.9


def test_burst_energy_concentrated_in_band():
    burst = synth_balance_burst(SR).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(burst)) ** 2
    freqs = np.fft.rfftfreq(burst.size, d=1.0 / SR)
    in_band = spectrum[(freqs >= BURST_F_LO - 50) & (freqs <= BURST_F_HI + 50)]
    assert in_band.sum() / spectrum.sum() > 0.95


def test_burst_fades_to_silence_at_edges():
    burst = synth_balance_burst(SR)
    assert abs(burst[0]) < 1e-4 and abs(burst[-1]) < 1e-4


def test_burst_rejects_bad_band():
    with pytest.raises(ValueError):
        synth_balance_burst(SR, f_lo=2000, f_hi=500)
    with pytest.raises(ValueError):
        synth_balance_burst(SR, f_lo=500, f_hi=30000)


# ---------------------------------------------------------------------------
# Schedule + WAV


def test_schedule_is_left_right_left():
    sched = build_balance_schedule(SR)
    assert [b.channel for b in sched.bursts] == ["left", "right", "left"]
    starts = [b.start_s for b in sched.bursts]
    assert starts == sorted(starts)
    assert sched.total_s > sched.bursts[-1].end_s


def test_schedule_rejects_unknown_channel():
    with pytest.raises(ValueError):
        build_balance_schedule(SR, channel_order=("left", "centre"))


def test_wav_has_exclusive_channels(tmp_path):
    from scipy.io import wavfile

    sched = build_balance_schedule(SR)
    path = tmp_path / "balance.wav"
    write_balance_wav(path, sched)
    sr, data = wavfile.read(str(path))
    assert sr == SR
    assert data.dtype == np.int16 and data.ndim == 2 and data.shape[1] == 2
    assert data.shape[0] == int(round(sched.total_s * SR))
    for spec in sched.bursts:
        a, b = int(spec.start_s * SR), int(spec.end_s * SR)
        active = 0 if spec.channel == "left" else 1
        silent = 1 - active
        assert np.max(np.abs(data[a:b, active])) > 1000
        assert np.max(np.abs(data[a:b, silent])) == 0


def test_schedule_to_dict_roundtrips_keys():
    d = build_balance_schedule(SR).to_dict()
    assert set(d) == {"sample_rate", "total_s", "bursts"}
    assert all(set(b) == {"channel", "start_s", "end_s"} for b in d["bursts"])


# ---------------------------------------------------------------------------
# Synthetic room capture


def render_capture(
    sched: BalanceSchedule,
    left_gain_db: float,
    right_gain_db: float,
    *,
    noise_dbfs: float = -70.0,
    noise_in_band: bool = False,
    pre_pad_s: float = 0.7,
    post_pad_s: float = 0.4,
    second_left_extra_db: float = 0.0,
    drop_burst: int | None = None,
    seed: int = 7,
) -> np.ndarray:
    """What the phone would record: each scheduled burst scaled by its
    speaker's gain, noise floor underneath, unknown start offset."""
    sr = sched.sample_rate
    total = int(round((pre_pad_s + sched.total_s + post_pad_s) * sr))
    out = np.zeros(total)
    burst = synth_balance_burst(sr).astype(np.float64)
    left_seen = 0
    for i, spec in enumerate(sched.bursts):
        if drop_burst == i:
            continue
        gain_db = left_gain_db if spec.channel == "left" else right_gain_db
        if spec.channel == "left":
            left_seen += 1
            if left_seen == 2:
                gain_db += second_left_extra_db
        start = int(round((pre_pad_s + spec.start_s) * sr))
        out[start:start + burst.size] += burst * 10 ** (gain_db / 20.0)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(total)
    if noise_in_band:
        spectrum = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(total, d=1.0 / sr)
        spectrum[(freqs < BURST_F_LO) | (freqs > BURST_F_HI)] = 0.0
        noise = np.fft.irfft(spectrum, total)
    noise *= 10 ** (noise_dbfs / 20.0) / np.sqrt(np.mean(noise**2))
    return out + noise


def test_recovers_imposed_delta():
    sched = build_balance_schedule(SR)
    capture = render_capture(sched, left_gain_db=-6.0, right_gain_db=-9.0)
    result = evaluate_capture(capture, SR, sched)
    assert result.ok, result.reason
    assert result.delta_db == pytest.approx(3.0, abs=0.3)
    assert result.drift_db <= 0.2
    assert result.snr_db > balance.SNR_GATE_DB


def test_recovery_is_offset_invariant():
    sched = build_balance_schedule(SR)
    a = evaluate_capture(
        render_capture(sched, -6.0, -9.0, pre_pad_s=0.3), SR, sched)
    b = evaluate_capture(
        render_capture(sched, -6.0, -9.0, pre_pad_s=1.9, post_pad_s=1.0),
        SR, sched)
    assert a.ok and b.ok
    assert a.delta_db == pytest.approx(b.delta_db, abs=0.2)


def test_right_louder_gives_negative_delta():
    sched = build_balance_schedule(SR)
    result = evaluate_capture(
        render_capture(sched, -10.0, -6.0), SR, sched)
    assert result.ok
    assert result.delta_db == pytest.approx(-4.0, abs=0.3)


def test_drift_gate_rejects_moved_phone():
    sched = build_balance_schedule(SR)
    capture = render_capture(sched, -6.0, -6.0, second_left_extra_db=2.5)
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok
    assert result.reason == "drift"
    assert result.drift_db > balance.DRIFT_GATE_DB


def test_low_snr_rejected():
    sched = build_balance_schedule(SR)
    # In-band noise floor ~8 dB under the burst level: alignment can
    # still lock but the SNR gate must refuse to trust the numbers.
    capture = render_capture(
        sched, -6.0, -6.0, noise_dbfs=-18.0, noise_in_band=True)
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok
    assert result.reason in {"low_snr", "no_alignment"}


def test_silence_does_not_align():
    sched = build_balance_schedule(SR)
    rng = np.random.default_rng(3)
    capture = rng.standard_normal(int(sched.total_s * SR) + SR) * 1e-4
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok
    assert result.reason == "no_alignment"


def test_clipped_capture_rejected():
    sched = build_balance_schedule(SR)
    capture = render_capture(sched, 15.0, 15.0)  # bursts peak past 0 dBFS
    capture = np.clip(capture, -1.0, 1.0)
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok
    assert result.reason == "clipped"


def test_short_capture_rejected():
    sched = build_balance_schedule(SR)
    capture = render_capture(sched, -6.0, -6.0)[: int(sched.total_s * SR) - 200]
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok
    assert result.reason == "capture_short"


def test_missing_burst_rejected():
    sched = build_balance_schedule(SR)
    capture = render_capture(sched, -6.0, -6.0, drop_burst=1)
    result = evaluate_capture(capture, SR, sched)
    assert not result.ok  # right level collapses to floor → gated


def test_band_rms_ignores_out_of_band_energy():
    t = np.arange(SR) / SR
    hum = 0.5 * np.sin(2 * np.pi * 60 * t)  # loud mains hum, out of band
    quiet_tone = 0.01 * np.sin(2 * np.pi * 1000 * t)  # in band
    level_with_hum = band_rms_dbfs(hum + quiet_tone, SR)
    level_alone = band_rms_dbfs(quiet_tone, SR)
    assert level_with_hum == pytest.approx(level_alone, abs=0.5)


# ---------------------------------------------------------------------------
# Trim recommendation


def test_left_louder_trims_left():
    rec = recommend_trims(3.0)
    assert rec == TrimRecommendation(-3.0, 0.0, False)


def test_right_louder_trims_right():
    rec = recommend_trims(-2.0)
    assert rec == TrimRecommendation(0.0, -2.0, False)


def test_balanced_pair_renormalizes_wasted_attenuation():
    # Pair already balanced but both trimmed: lift together to 0.
    rec = recommend_trims(0.0, current_left_trim_db=-5.0,
                          current_right_trim_db=-2.0)
    assert rec == TrimRecommendation(-3.0, 0.0, False)


def test_residual_delta_composes_with_existing_trims():
    # Left still 1 dB louder despite -3 already applied.
    rec = recommend_trims(1.0, current_left_trim_db=-3.0,
                          current_right_trim_db=0.0)
    assert rec == TrimRecommendation(-4.0, 0.0, False)


def test_floor_clamps_and_reports():
    rec = recommend_trims(30.0)
    assert rec.left_trim_db == -24.0
    assert rec.right_trim_db == 0.0
    assert rec.clamped


def test_recommendation_is_stable_at_zero_delta():
    first = recommend_trims(3.4)
    again = recommend_trims(0.0, first.left_trim_db, first.right_trim_db)
    assert again == first


def test_result_to_dict_keys():
    sched = build_balance_schedule(SR)
    d = evaluate_capture(
        render_capture(sched, -6.0, -9.0), SR, sched).to_dict()
    assert set(d) == {"ok", "reason", "left_dbfs", "right_dbfs",
                      "delta_db", "drift_db", "snr_db", "noise_dbfs"}
