# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the deterministic band-limited-noise WAV generator.

``ensure_bandlimited_noise_wav`` backs the bass-extension bench runner: it must
be byte-for-byte reproducible (same args + seed), produce a measurement-clean
mono 16-bit WAV whose energy sits inside ``[f_lo, f_hi]``, and validate its
inputs as loudly as the sine generator. All hardware-free and network-free.
"""
from __future__ import annotations

import hashlib
import math
import wave

import numpy as np
import pytest

from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav


def _read_wav(path):
    with wave.open(str(path), "rb") as reader:
        nchannels = reader.getnchannels()
        sampwidth = reader.getsampwidth()
        rate = reader.getframerate()
        nframes = reader.getnframes()
        frames = reader.readframes(nframes)
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    return samples, rate, nchannels, sampwidth, nframes


def test_determinism_is_byte_identical(tmp_path):
    kwargs = dict(
        f_lo_hz=40.0,
        f_hi_hz=120.0,
        duration_s=1.0,
        dbfs=-6.0,
        sample_rate=48000,
        seed=7,
    )
    a = ensure_bandlimited_noise_wav(cache_dir=tmp_path / "a", **kwargs)
    b = ensure_bandlimited_noise_wav(cache_dir=tmp_path / "b", **kwargs)
    assert (
        hashlib.sha256(a.read_bytes()).hexdigest()
        == hashlib.sha256(b.read_bytes()).hexdigest()
    )


def test_seed_changes_the_output(tmp_path):
    base = dict(
        f_lo_hz=40.0,
        f_hi_hz=120.0,
        duration_s=1.0,
        dbfs=-6.0,
        sample_rate=48000,
    )
    a = ensure_bandlimited_noise_wav(cache_dir=tmp_path / "a", seed=1, **base)
    b = ensure_bandlimited_noise_wav(cache_dir=tmp_path / "b", seed=2, **base)
    assert a.read_bytes() != b.read_bytes()


def test_cache_hit_reuses_the_existing_file(tmp_path):
    kwargs = dict(
        f_lo_hz=40.0,
        f_hi_hz=120.0,
        duration_s=0.5,
        dbfs=-6.0,
        sample_rate=48000,
        seed=0,
    )
    first = ensure_bandlimited_noise_wav(cache_dir=tmp_path, **kwargs)
    mtime = first.stat().st_mtime_ns
    second = ensure_bandlimited_noise_wav(cache_dir=tmp_path, **kwargs)
    assert first == second
    assert second.stat().st_mtime_ns == mtime  # not regenerated


def test_wav_header_is_mono_16bit_at_rate(tmp_path):
    rate = 48000
    duration_s = 0.5
    path = ensure_bandlimited_noise_wav(
        cache_dir=tmp_path,
        f_lo_hz=50.0,
        f_hi_hz=150.0,
        duration_s=duration_s,
        dbfs=-6.0,
        sample_rate=rate,
    )
    samples, got_rate, nchannels, sampwidth, nframes = _read_wav(path)
    assert nchannels == 1
    assert sampwidth == 2  # 16-bit
    assert got_rate == rate
    assert nframes == int(round(duration_s * rate))
    assert len(samples) == nframes


def test_energy_is_concentrated_in_the_passband(tmp_path):
    f_lo, f_hi, rate = 60.0, 200.0, 48000
    path = ensure_bandlimited_noise_wav(
        cache_dir=tmp_path,
        f_lo_hz=f_lo,
        f_hi_hz=f_hi,
        duration_s=2.0,
        dbfs=-6.0,
        sample_rate=rate,
        seed=3,
    )
    samples, rate, _, _, nframes = _read_wav(path)
    power = np.abs(np.fft.rfft(samples)) ** 2
    freqs = np.fft.rfftfreq(nframes, 1.0 / rate)

    in_band = power[(freqs >= f_lo) & (freqs <= f_hi)]
    # "well outside" the band: skip the transition skirts entirely (an octave
    # of guard on each side), which also keeps FFT leakage from the strong
    # in-band content out of the comparison.
    above = power[freqs >= 2.0 * f_hi]
    below = power[(freqs > 0.0) & (freqs <= 0.25 * f_lo)]

    # Nearly all the energy sits inside the passband.
    assert in_band.sum() / power.sum() > 0.9
    # Mean power density in-band is far above the out-of-band floor.
    assert in_band.mean() > above.mean() * 100.0
    assert in_band.mean() > below.mean() * 50.0


def test_five_ms_quadratic_fades_zero_the_endpoints(tmp_path):
    rate = 48000
    path = ensure_bandlimited_noise_wav(
        cache_dir=tmp_path,
        f_lo_hz=50.0,
        f_hi_hz=150.0,
        duration_s=1.0,
        dbfs=-6.0,
        sample_rate=rate,
        seed=5,
    )
    samples, *_ = _read_wav(path)
    # The quadratic fade endpoints are exactly 0 (linspace endpoints squared).
    assert samples[0] == 0.0
    assert samples[-1] == 0.0
    # A sample just inside the 5 ms fade is small relative to the signal peak.
    peak = float(np.max(np.abs(samples)))
    assert peak > 0.0
    fade = max(8, int(0.005 * rate))
    assert abs(samples[fade // 4]) < peak * 0.5


def test_peak_matches_target_dbfs(tmp_path):
    rate = 48000
    dbfs = -6.0
    path = ensure_bandlimited_noise_wav(
        cache_dir=tmp_path,
        f_lo_hz=50.0,
        f_hi_hz=200.0,
        duration_s=2.0,
        dbfs=dbfs,
        sample_rate=rate,
        seed=9,
    )
    samples, *_ = _read_wav(path)
    measured_amp = float(np.max(np.abs(samples))) / 32767.0
    measured_dbfs = 20.0 * math.log10(measured_amp)
    assert measured_dbfs == pytest.approx(dbfs, abs=0.3)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_seat_level_band_at_a_minus_one_peak_lands_in_the_hot_rms_window(
    tmp_path, seed
):
    """The crest factor a seat-SPL stimulus is actually reachable through.

    Field report: a 150-8000 Hz program generated here at the -20 dBFS peak
    (``..._200dbm_...``) measures ~-33.6 dBFS RMS, because band-limited noise in
    this band carries ~13.6 dB of crest. At a fader that cannot go above 0 dB
    that is 19 dB of seat SPL simply unreachable, and
    ``jasper-seat-level``'s ``spl_target_unreachable`` was the symptom.

    Asked for the hottest RMS that still keeps the true peak at or under
    -1 dBFS, the answer is the peak argument itself: this generator
    peak-normalizes, so ``dbfs=-1.0`` yields ~-14.6 dBFS RMS — not the -12 a
    crest-blind guess produces. Pinned as a WINDOW across seeds because the
    crest is a property of the draw, and at the field stimulus's own 20 s
    because it is also a property of the LENGTH — the peak is a max over
    samples, so a 2 s draw of the same band crests ~0.8 dB lower and lands
    hotter. Pinned at all because the reachability arithmetic in that refusal
    is only as good as this number.

    Nothing here changes a clamp: the ramp's per-sample SPL stop and its clip
    stop are what bound the level, and a hotter stimulus only moves the same
    target into reach at a LOWER fader.
    """
    path = ensure_bandlimited_noise_wav(
        cache_dir=tmp_path,
        f_lo_hz=150.0,
        f_hi_hz=8000.0,
        duration_s=20.0,
        dbfs=-1.0,
        sample_rate=48000,
        seed=seed,
    )
    samples, *_ = _read_wav(path)
    scaled = samples / 32767.0
    peak_dbfs = 20.0 * math.log10(float(np.max(np.abs(scaled))))
    rms_dbfs = 20.0 * math.log10(float(np.sqrt(np.mean(scaled**2))))

    assert peak_dbfs <= -1.0 + 0.05, "true peak must stay at or under -1 dBFS"
    assert -16.0 <= rms_dbfs <= -14.0
    # The level token the cache filename carries, on its own convention
    # (tenths of a dB, sign dropped) -- what an operator greps for.
    assert "_10dbm_" in path.name


@pytest.mark.parametrize(
    "bad",
    [
        dict(f_lo_hz=0.0, f_hi_hz=120.0, duration_s=1.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=120.0, f_hi_hz=120.0, duration_s=1.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=120.0, f_hi_hz=40.0, duration_s=1.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=40.0, f_hi_hz=30000.0, duration_s=1.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=40.0, f_hi_hz=120.0, duration_s=0.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=40.0, f_hi_hz=120.0, duration_s=-1.0, dbfs=-6.0, sample_rate=48000),
        dict(f_lo_hz=40.0, f_hi_hz=120.0, duration_s=1.0, dbfs=3.0, sample_rate=48000),
    ],
)
def test_input_validation_raises_value_error(tmp_path, bad):
    with pytest.raises(ValueError):
        ensure_bandlimited_noise_wav(cache_dir=tmp_path, **bad)


def test_negative_seed_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ensure_bandlimited_noise_wav(
            cache_dir=tmp_path,
            f_lo_hz=40.0,
            f_hi_hz=120.0,
            duration_s=1.0,
            dbfs=-6.0,
            sample_rate=48000,
            seed=-1,
        )
