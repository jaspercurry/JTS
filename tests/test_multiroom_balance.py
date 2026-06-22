# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair-balance measurement core (jasper/multiroom/balance.py) —
equal-loudness ramp method.

The ramp-emission function is the timing contract between the played
WAV and the server-side lock math, so the tests pin both ends: the
pure function's shape, and that the rendered WAV's envelope actually
follows it.
"""

import numpy as np
import pytest

from jasper.multiroom.balance import (
    BURST_F_HI,
    BURST_F_LO,
    CHANNELS,
    MIN_LOCK_OFFSET_S,
    RAMP_CEIL_DBFS,
    RAMP_LEAD_IN_S,
    RAMP_RATE_DB_S,
    RAMP_START_DBFS,
    TrimRecommendation,
    drive_delta_db,
    ramp_duration_s,
    ramp_emission_dbfs,
    recommend_trims,
    write_ramp_wav,
)

SR = 48000


# ---------------------------------------------------------------------------
# Ramp emission function (the timing contract)


def test_emission_is_silent_during_lead_in():
    assert ramp_emission_dbfs(0.0) is None
    assert ramp_emission_dbfs(RAMP_LEAD_IN_S - 0.01) is None


def test_emission_ramps_linearly_then_holds_at_ceiling():
    t1 = RAMP_LEAD_IN_S + 4.0
    assert ramp_emission_dbfs(t1) == pytest.approx(
        RAMP_START_DBFS + 4.0 * RAMP_RATE_DB_S)
    # Deep into the hold: capped at the ceiling.
    assert ramp_emission_dbfs(ramp_duration_s() - 0.5) == RAMP_CEIL_DBFS


def test_emission_ends_after_wav():
    assert ramp_emission_dbfs(ramp_duration_s() + 0.1) is None


def test_min_lock_offset_sits_inside_the_ramp():
    assert ramp_emission_dbfs(MIN_LOCK_OFFSET_S) is not None
    assert MIN_LOCK_OFFSET_S > RAMP_LEAD_IN_S


def test_ceiling_never_exceeds_sweep_level():
    # Hearing-safety contract: the test signal never exceeds the
    # correction sweep's -12 dBFS program level.
    assert RAMP_CEIL_DBFS <= -12.0
    assert RAMP_START_DBFS <= RAMP_CEIL_DBFS - 25.0  # starts much quieter


# ---------------------------------------------------------------------------
# Ramp WAV rendering


@pytest.fixture(scope="module")
def left_wav(tmp_path_factory):
    from scipy.io import wavfile
    path = tmp_path_factory.mktemp("bal") / "left.wav"
    write_ramp_wav(path, "left", sample_rate=SR)
    sr, data = wavfile.read(str(path))
    return sr, data


def test_wav_shape_and_duration(left_wav):
    sr, data = left_wav
    assert sr == SR
    assert data.dtype == np.int16 and data.ndim == 2 and data.shape[1] == 2
    assert data.shape[0] == int(round(ramp_duration_s() * SR))


def test_wav_other_channel_is_silent(left_wav):
    _, data = left_wav
    assert np.max(np.abs(data[:, 1])) == 0  # right channel silent
    assert np.max(np.abs(data[:, 0])) > 1000


def test_wav_lead_in_is_silent(left_wav):
    _, data = left_wav
    lead = int(RAMP_LEAD_IN_S * SR)
    assert np.max(np.abs(data[:lead, 0])) == 0


def test_wav_envelope_tracks_emission_function(left_wav):
    """RMS of 1 s windows must follow ramp_emission_dbfs's RELATIVE
    law — the contract the server-side lock math depends on. (Only
    drive DIFFERENCES enter the trim delta, so the relative law is
    what must be honest; absolute crest offsets cancel between the
    two speakers' passes.)"""
    _, data = left_wav
    x = data[:, 0].astype(np.float64) / 32767.0

    def window_rms_db(probe_s: float) -> float:
        a = int((probe_s - 0.5) * SR)
        b = int((probe_s + 0.5) * SR)
        return 10 * np.log10(np.mean(x[a:b] ** 2) + 1e-24)

    ref_s = 3.0
    ref_db = window_rms_db(ref_s)
    ref_expected = ramp_emission_dbfs(ref_s)
    for probe_s in (6.0, 10.0, 14.0, ramp_duration_s() - 1.0):
        measured_rise = window_rms_db(probe_s) - ref_db
        expected_rise = ramp_emission_dbfs(probe_s) - ref_expected
        assert measured_rise == pytest.approx(expected_rise, abs=0.8)
    # And the hold really is flat at the ceiling.
    hold_a = window_rms_db(ramp_duration_s() - 2.5)
    hold_b = window_rms_db(ramp_duration_s() - 1.0)
    assert hold_a == pytest.approx(hold_b, abs=0.5)


def test_wav_energy_concentrated_in_band(left_wav):
    _, data = left_wav
    x = data[:, 0].astype(np.float64)
    spectrum = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(x.size, d=1.0 / SR)
    in_band = spectrum[(freqs >= BURST_F_LO - 50) & (freqs <= BURST_F_HI + 50)]
    assert in_band.sum() / spectrum.sum() > 0.95


def test_wav_is_deterministic(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    write_ramp_wav(a, "right", sample_rate=SR)
    write_ramp_wav(b, "right", sample_rate=SR)
    assert a.read_bytes() == b.read_bytes()


def test_wav_rejects_unknown_channel(tmp_path):
    with pytest.raises(ValueError):
        write_ramp_wav(tmp_path / "x.wav", "centre")


def test_channels_vocabulary():
    assert CHANNELS == ("left", "right")


# ---------------------------------------------------------------------------
# Drive delta


def test_drive_delta_sign_convention():
    # Left locked at -30 dBFS drive, right needed -24: left reached the
    # target with 6 dB less drive → left is the louder speaker.
    assert drive_delta_db(-30.0, -24.0) == pytest.approx(6.0)
    assert drive_delta_db(-24.0, -30.0) == pytest.approx(-6.0)
    assert drive_delta_db(-27.0, -27.0) == 0.0


# ---------------------------------------------------------------------------
# Trim recommendation (unchanged contract from v1)


def test_left_louder_trims_left():
    rec = recommend_trims(3.0)
    assert rec == TrimRecommendation(-3.0, 0.0, False)


def test_right_louder_trims_right():
    rec = recommend_trims(-2.0)
    assert rec == TrimRecommendation(0.0, -2.0, False)


def test_balanced_pair_renormalizes_wasted_attenuation():
    rec = recommend_trims(0.0, current_left_trim_db=-5.0,
                          current_right_trim_db=-2.0)
    assert rec == TrimRecommendation(-3.0, 0.0, False)


def test_residual_delta_composes_with_existing_trims():
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
