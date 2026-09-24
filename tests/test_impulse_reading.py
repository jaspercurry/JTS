# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What an impulse says, read the way REW reads it: the physics the readers pin."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import lfilter

from jasper.audio_measurement.excess_phase import add_delayed_copy, biquad_allpass
from jasper.audio_measurement.impulse_reading import impulse_shape, step_response, timing_by_frequency

RATE = 48_000
PEAK = 12_000
BAND = (300.0, 12_000.0)


def _arrival(amplitude: float = 1.0, *, noise: float = 0.0, length: int = 36_000) -> np.ndarray:
    ir = np.random.default_rng(0).normal(0.0, noise, length) if noise else np.zeros(length)
    ir[PEAK] += amplitude
    return ir


def _timing(ir: np.ndarray):
    return timing_by_frequency(ir, RATE, peak_index=int(np.argmax(np.abs(ir))), window_ms=8.0,
                               lead_ms=1.0, band_hz=BAND)


def _at(timing, hz: float, values) -> float:
    return float(np.interp(hz, timing.freqs_hz, values))


def test_onset_polarity_and_the_noise_before_it():
    shape = impulse_shape(_arrival(-1.0, noise=1e-4), RATE)

    assert (shape.peak_index, shape.onset_index, shape.polarity) == (PEAK, PEAK, -1)
    assert shape.peak_to_noise_db == pytest.approx(80.0, abs=1.0)


def test_the_energy_time_curve_falls_at_the_decay_rate():
    time_s = np.arange(36_000 - PEAK) / RATE
    decay = np.random.default_rng(1).normal(0.0, 1.0, time_s.size) * np.exp(-time_s / 0.020)
    ir = np.concatenate([np.zeros(PEAK), decay])
    ir[PEAK] = 4.0
    etc = dict(impulse_shape(ir, RATE, peak_index=PEAK).etc_db)

    # An amplitude envelope exp(-t/tau) loses 20*log10(e)/tau dB of energy per second.
    assert etc[50.0] - etc[20.0] == pytest.approx(-20 * np.log10(np.e) * 0.030 / 0.020, abs=1.5)


def test_a_step_is_the_running_sum_scaled_to_its_largest_excursion():
    np.testing.assert_allclose(step_response(np.array([0.0, 2.0, -1.0, 1.0])), [0.0, 1.0, 0.5, 1.0])


def test_a_pure_arrival_has_no_delay_left_once_time_starts_at_its_peak():
    timing = _timing(_arrival())

    assert np.nanmax(np.abs(timing.group_delay_ms)) < 0.01
    assert np.nanmax(np.abs(timing.excess_group_delay_ms)) < 0.01


def test_an_all_pass_shows_as_excess_group_delay_where_it_turns():
    b, a = biquad_allpass(2000.0, 2.0, RATE)
    timing = _timing(lfilter(b, a, _arrival()))
    theory_ms = 1000 * 4 * 2.0 / (2 * np.pi * 2000.0)  # an RBJ all-pass peaks at 4Q/w0

    assert _at(timing, 2000.0, timing.group_delay_ms) == pytest.approx(theory_ms, rel=0.15)
    assert _at(timing, 2000.0, timing.excess_group_delay_ms) > 0.5 * theory_ms
    assert abs(_at(timing, 400.0, timing.excess_group_delay_ms)) < 0.1 * theory_ms


def test_a_minimum_phase_echo_moves_group_delay_but_not_excess_group_delay():
    timing = _timing(add_delayed_copy(_arrival(), 0.5, 1.0, RATE))
    # Below the band the magnitude is held flat (excess_phase._hold_band_edges),
    # which a synthetic echo that is valid down to DC exposes at the lower edge.
    interior = (timing.freqs_hz >= 800.0) & (timing.freqs_hz <= 8000.0)

    assert np.nanmax(np.abs(timing.group_delay_ms[interior])) > 0.3
    assert np.nanmax(np.abs(timing.excess_group_delay_ms[interior])) < 0.03
