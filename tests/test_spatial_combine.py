# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the cross-position band statistics and the analysis-grid decimation."""
from __future__ import annotations

import math

import numpy as np
import pytest

from jasper.audio_measurement.spatial_combine import (
    MAX_ANALYSIS_BINS,
    _band_spread,
    decimate_curve_to_analysis_grid,
)
from tests._flat_lin_corpus import sweep_anchor

SAMPLE_RATE = 48_000
N_FFT = 16_384
ECHO_R = 0.36  # the corpus's measured reflection coefficient (-8.8 dB)


def _grid() -> np.ndarray:
    return np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)


def _true_response_db(freqs: np.ndarray) -> np.ndarray:
    """A smooth synthetic "true" response, rolled off above 8 kHz."""
    octaves = np.log2(np.maximum(freqs, 20.0) / 1000.0)
    shape = 1.5 * np.sin(octaves * 1.1) - 0.8 * np.cos(octaves * 2.3)
    rolloff = -3.0 * np.clip(np.log2(np.maximum(freqs, 8000.0) / 8000.0), 0.0, None)
    return shape + rolloff


def _cloud_db(taus_s: np.ndarray, seed: int = 20_260_725) -> np.ndarray:
    """One row per position: truth + seeded noise, combed by one echo."""
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(seed)
    rows = []
    for tau in taus_s:
        level = true_db + rng.normal(0.0, 0.15, freqs.size)
        comb = 1.0 + ECHO_R * np.exp(-2j * np.pi * freqs * float(tau))
        spectrum = 10.0 ** (level / 20.0) * np.exp(-2j * np.pi * freqs * 0.002) * comb
        rows.append(20.0 * np.log10(np.abs(spectrum)))
    return np.vstack(rows)


def _dispersed_taus(n: int = 10, seed: int = 20_260_725) -> np.ndarray:
    """Stratified delays spanning 150-490 us (~5-17 cm of path delta) with
    seeded jitter.
    """
    rng = np.random.default_rng(seed)
    return np.linspace(150e-6, 490e-6, n) + rng.uniform(-15e-6, 15e-6, n)


def test_band_spread_separates_a_tight_cloud_from_a_dispersed_one():
    """The two spread statistics answer two questions; only one discriminates.

    ``max_sigma_db`` (worst single bin) separates a moved cloud from a
    stationary one. ``sigma_db`` (per-position band level) collapses in the
    top octaves, where an octave spans many comb periods and every position
    lands near ``1 + r**2``, but not at 1-2 kHz, where tau of 150-490 us puts
    the comb period (2-6.7 kHz) wider than the band.
    """
    freqs = _grid()
    bands_dispersed = {b.center_hz: b for b in _band_spread(freqs, _cloud_db(_dispersed_taus()))}
    bands_tight = {b.center_hz: b for b in _band_spread(freqs, _cloud_db(np.full(10, 300e-6)))}
    assert bands_dispersed and bands_tight

    # A cloud that never moved disagrees with itself only by seeded noise.
    for band in bands_tight.values():
        assert band.sigma_db < 0.1, band
        assert band.max_sigma_db < 0.5, band

    for center in (2000.0, 4000.0, 8000.0):
        assert bands_dispersed[center].max_sigma_db > 2.0, center
        assert (
            bands_dispersed[center].max_sigma_db
            > 5 * bands_tight[center].max_sigma_db
        ), center

    for center in (8000.0, 16_000.0):
        band = bands_dispersed[center]
        assert band.sigma_db < 0.5, band
        assert band.max_sigma_db > 5 * band.sigma_db, band

    # ...but not at 1-2 kHz, where one octave is narrower than one period.
    for center in (1000.0, 2000.0):
        assert bands_dispersed[center].sigma_db > 1.0, center

    band = bands_dispersed[4000.0]
    assert band.max_sigma_db >= band.sigma_db
    assert band.n_bins > 0
    assert band.f_lo < 4000.0 < band.f_hi


def test_band_spread_numerics_are_pinned_on_a_hand_checkable_case():
    """Both spread statistics on a hand-checkable case.

    Two positions, one -10 dB notch, grid 700-1400 Hz in 100 Hz steps: only
    the 1 kHz octave band has the ``MIN_BAND_BINS`` = 4 bins it needs, so
    exactly one :class:`BandSpread` is reported over seven bins. One position
    has no spread at all: undefined, not zero.
    """
    freqs = np.arange(700.0, 1401.0, 100.0)
    quiet = np.zeros(freqs.size)
    quiet[3] = -10.0  # 1000 Hz

    band_spread = _band_spread(freqs, np.vstack([np.zeros(freqs.size), quiet]))

    assert len(band_spread) == 1
    band = band_spread[0]
    assert band.center_hz == 1000.0
    assert (band.n_bins, band.f_lo, band.f_hi) == (7, 800.0, 1400.0)

    p1_band_level_db = 10.0 * math.log10((6.0 * 1.0 + 10.0 ** (-10.0 / 10.0)) / 7.0)
    expected_sigma_db = abs(0.0 - p1_band_level_db) / math.sqrt(2.0)
    assert band.sigma_db == pytest.approx(expected_sigma_db, abs=1e-12)
    assert band.sigma_db == pytest.approx(0.42262503, abs=1e-8)
    assert band.max_sigma_db == pytest.approx(10.0 / math.sqrt(2.0), abs=1e-12)
    assert _band_spread(freqs, quiet.reshape(1, -1)) == ()


def test_decimation_preserves_the_linear_grid_contract_and_band_energy():
    """Decimation is block averaging, not subsampling, and the result is still
    a legal linear grid.

    Checked on a flat-plus-notch construction where the answer is computable:
    one -20 dB bin among zeros must survive as a shallow dip rather than
    vanishing or staying full-depth.
    """
    fine = np.fft.rfftfreq(2**18, 1.0 / SAMPLE_RATE)
    fine_step = float(fine[1] - fine[0])
    block = 8  # ceil(131073 / 16385)

    grid, _ = decimate_curve_to_analysis_grid(fine, np.zeros(fine.size))
    steps = np.diff(grid)
    assert np.allclose(steps, steps[0], rtol=1e-9), "decimated grid must stay linear"
    assert float(steps[0]) == pytest.approx(block * fine_step)
    # Each decimated bin sits at its block's CENTRE, which is what the block's
    # averaged power is the level of — not at the block's first bin.
    assert grid[0] == pytest.approx(fine_step * (block - 1) / 2.0)
    assert grid[0] != pytest.approx(float(fine[0]), abs=1e-6)
    # A trailing partial block is dropped.
    assert grid[-1] < fine[-1]
    assert float(fine[-1] - grid[-1]) < 2.0 * float(steps[0])

    # Energy, not samples: one deep notch on an otherwise flat curve.
    fine = np.linspace(0.0, 24_000.0, 4 * MAX_ANALYSIS_BINS)
    flat = np.zeros(fine.size)
    flat[1234] = -20.0
    coarse_grid, decimated = decimate_curve_to_analysis_grid(fine, flat)
    block = 4
    assert coarse_grid.size == fine.size // block
    dip_index = 1234 // block
    expected_db = 10.0 * math.log10(((block - 1) * 1.0 + 10.0 ** (-20.0 / 10.0)) / block)
    assert decimated[dip_index] == pytest.approx(expected_db, abs=1e-9)
    assert decimated[dip_index] == pytest.approx(-1.2, abs=0.05)
    # Neither lost (a subsample could have skipped it) nor still -20 dB.
    assert -2.0 < decimated[dip_index] < -0.5
    neighbours = np.delete(np.asarray(decimated), dip_index)
    assert np.allclose(neighbours, 0.0, atol=1e-9)


# Deliberately NOT ``@requires_corpus``: every corpus reading above rests on
# ``sweep_anchor`` finding the sweep by its own waveform, and pinning that on
# a synthetic capture is what makes it visible to CI.
def test_sweep_anchor_owes_nothing_to_the_composers_schedule():
    """``sweep_anchor`` locates the stimulus by cross-correlating the stimulus
    itself, so a declared schedule position cannot move the answer (#1879).
    """
    import dataclasses

    from jasper.audio_measurement.program import build_verify_program, segment_stimulus

    program = build_verify_program(
        2000.0, leading_pilot_gains_db=(-16.0006, -6.0005), courtesy_prelude=True
    )
    segment = program.segment("sweep_verify")
    stimulus = np.asarray(segment_stimulus(segment), dtype=np.float64)

    # A capture that knows nothing about the schedule, as an archived WAV is.
    planted_at = 12_345
    capture = np.zeros(planted_at + stimulus.size + 8_000)
    capture[planted_at : planted_at + stimulus.size] = stimulus

    assert sweep_anchor(capture, segment) == planted_at

    # Now claim the composer moved it, in both directions.
    for delta in (48_000, -7_777):
        moved = dataclasses.replace(segment, start_sample=segment.start_sample + delta)
        assert sweep_anchor(capture, moved) == planted_at, delta
