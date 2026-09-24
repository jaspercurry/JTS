# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decay times read off a measured impulse, octave by octave (ISO 3382-1)."""

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.decay import octave_decays

RATE = 48_000
START = 12_000


def _decay(rt60_s: float, noise_db: float, *, seed: int = 0, kept_s: float = 0.5) -> np.ndarray:
    """Exponentially decaying noise from ``START``, over a steady noise floor ``noise_db`` below it."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(kept_s * RATE)) / RATE
    x = np.concatenate([np.zeros(START), rng.normal(size=t.size) * 10 ** (-3 * t / rt60_s)])
    return x + rng.normal(size=x.size) * 10 ** (noise_db / 20)


def _band(x: np.ndarray, hz: float):
    band, = (one for one in octave_decays(x, RATE, start_index=START, band_hz=(20.0, 20_000.0))
             if one.centre_hz == hz)
    return band


@pytest.mark.parametrize("rt60_s", [0.3, 0.5])
@pytest.mark.parametrize("noise_db", [-80.0, -55.0])
def test_decay_times_read_a_known_decay_whatever_the_noise_floor(rt60_s, noise_db):
    band = _band(_decay(rt60_s, noise_db), 2000.0)

    assert band.t20_s == pytest.approx(rt60_s, rel=0.05)
    assert band.edt_s == pytest.approx(rt60_s, rel=0.15)


def test_a_figure_the_decay_range_cannot_carry_is_withheld():
    shallow = _band(_decay(0.3, -30.0), 2000.0)
    short = _band(_decay(0.8, -80.0), 2000.0)

    assert (shallow.edt_s is not None, shallow.t20_s, shallow.t30_s) == (True, None, None)
    # Half a second of a 0.8 s decay holds under 45 dB of it: T20 reads, T30 does not.
    assert short.t20_s == pytest.approx(0.8, rel=0.05)
    assert short.t30_s is None


def test_only_bands_inside_the_swept_band_are_read():
    bands = octave_decays(_decay(0.3, -80.0), RATE, start_index=START, band_hz=(1500.0, 20_000.0))

    assert [band.centre_hz for band in bands] == [2000.0, 4000.0, 8000.0, 16000.0]


def test_a_low_octave_reads_its_decay_from_the_onset():
    """The time-reversed filter's ringing before the onset is not decay."""
    edt = np.median([_band(_decay(0.3, -80.0, seed=seed), 63.0).edt_s for seed in range(8)])

    assert edt == pytest.approx(0.3, rel=0.15)
