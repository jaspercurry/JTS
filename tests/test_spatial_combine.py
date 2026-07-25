# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the spatial combiner + interference honesty screen.

Four layers, per the plan's fundamentals 1-2
(docs/flat-linearization-plan.md):

A. **Synthetic ground truth** — the primary, hardware-free validation. A
   known smooth "true" response is contaminated with a comb from a discrete
   echo at a per-position delay, then recovered through the combiner.
B. **Power-domain arithmetic**, pinned to hand-computed literals. The
   power-vs-dB mean confusion has produced three separate wrong architect
   claims in this repo; this is the forever-pin.
C. **Echo detector** on synthetic impulse responses and negative controls.
D. **Real-data smoke** against the 2026-07-24/25 JTS3 corpus. Skipped when
   the (gitignored, laptop-durable) capture directory is absent, which is
   always the case in CI.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.spatial_combine import (
    ECHO_CONFIDENCE_FLOOR,
    GEOMETRY_DISPERSED,
    GEOMETRY_LOCKED,
    GEOMETRY_UNKNOWN,
    STRENGTH_FLOOR_DB,
    CombinedResponse,
    PositionCapture,
    assess_geometry,
    combine_positions,
    detect_echo,
)

SAMPLE_RATE = 48_000
N_FFT = 16_384
ECHO_R = 0.36  # the corpus's measured reflection coefficient (-8.8 dB)


# --------------------------------------------------------------------------- #
# Synthetic corpus construction
# --------------------------------------------------------------------------- #


def _grid() -> np.ndarray:
    return np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)


def _true_response_db(freqs: np.ndarray) -> np.ndarray:
    """A smooth synthetic "true" speaker response, with a gentle top-octave
    rolloff above 8 kHz — the feature the plan says is *unknowable* from any
    single bounce-contaminated capture, so the combiner must recover it.
    """
    octaves = np.log2(np.maximum(freqs, 20.0) / 1000.0)
    shape = 1.5 * np.sin(octaves * 1.1) - 0.8 * np.cos(octaves * 2.3)
    rolloff = -3.0 * np.clip(np.log2(np.maximum(freqs, 8000.0) / 8000.0), 0.0, None)
    return shape + rolloff


def _position(
    position_id: str,
    freqs: np.ndarray,
    true_db: np.ndarray,
    tau_s: float,
    rng: np.random.Generator,
    *,
    noise_db: float = 0.15,
    reflection: float = ECHO_R,
    arrival_s: float = 0.002,
) -> PositionCapture:
    """One synthetic capture: truth + seeded noise, combed by one discrete
    echo, with a *consistent* impulse response.

    Magnitude and IR are built from one complex spectrum, so the IR the echo
    detector sees is exactly the physical cause of the comb in the magnitude
    the combiner sees — no chance of the two halves of the test disagreeing
    about what was simulated.
    """
    level = true_db + rng.normal(0.0, noise_db, freqs.size)
    comb = 1.0 + reflection * np.exp(-2j * np.pi * freqs * tau_s)
    spectrum = 10.0 ** (level / 20.0) * np.exp(-2j * np.pi * freqs * arrival_s) * comb
    return PositionCapture(
        position_id=position_id,
        freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.abs(spectrum)),
        sample_rate=SAMPLE_RATE,
        ir=np.fft.irfft(spectrum, N_FFT),
    )


def _cloud(taus_s: np.ndarray, seed: int = 20_260_725) -> tuple[
    np.ndarray, np.ndarray, list[PositionCapture]
]:
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(seed)
    captures = [
        _position(f"p{i}", freqs, true_db, float(tau), rng)
        for i, tau in enumerate(taus_s)
    ]
    return freqs, true_db, captures


def _dispersed_taus(n: int = 10, seed: int = 20_260_725) -> np.ndarray:
    """Stratified delays spanning 150-490 us with seeded jitter.

    Stratified rather than i.i.d. because that is what a *guided* position
    cloud actually produces (the plan's "prompted moves between capture
    groups"), and because an unlucky i.i.d. draw of only 10 samples can
    accidentally cluster tightly enough to read as geometry-locked — a
    property of the draw, not of the estimator. The span corresponds to
    ~5-17 cm of path delta, a physically ordinary spread for a mic cloud
    near a boundary.
    """
    rng = np.random.default_rng(seed)
    return np.linspace(150e-6, 490e-6, n) + rng.uniform(-15e-6, 15e-6, n)


def _relative_rms_error(
    estimate: np.ndarray, truth: np.ndarray, keep: np.ndarray
) -> float:
    """RMS of (estimate - truth) over ``keep`` after removing the common
    offset.

    Why *relative* and why *RMS* — both are deliberate, and both follow the
    plan rather than softening it:

    * Relative, because the plan's spec is explicitly evaluated "relative to
      the power mean over 250 Hz-8 kHz", and because the power mean carries
      a known, irreducible ``+10*log10(1 + r**2)`` energy offset from the
      echo (+0.53 dB at r=0.36) that a relative comparison normalises out.
    * RMS, because that is the plan's own convergence metric (fundamental 5:
      "converge at <~1 dB RMS 300 Hz-8 kHz") and because the research's
      accuracy law is a ``1/sqrt(N)`` *standard error*, not a bound on the
      worst of several thousand bins. A max-over-bins assertion would be an
      extreme-value statistic, and would fail for reasons that have nothing
      to do with the estimator being correct.
    """
    delta = (estimate - truth)[keep]
    return float(np.sqrt(np.mean((delta - delta.mean()) ** 2)))


# --------------------------------------------------------------------------- #
# B. Power-domain arithmetic — pinned to hand-computed literals
# --------------------------------------------------------------------------- #


def test_power_mean_is_the_energy_mean_not_the_db_mean():
    """The combiner averages in linear power, never in dB.

    Hand-computed, two positions, four bins. For levels a and b in dB:

        power mean = 10*log10((10**(a/10) + 10**(b/10)) / 2)
        dB    mean = (a + b) / 2

    bin 0: (0, -20) -> 10*log10(1.01/2)    = -2.9670862188  (dB mean -10)
    bin 1: (0,  -6) -> 10*log10(1.251189/2)= -2.0370720196  (dB mean  -3)
    bin 2: (0,   0) ->                        0.0           (dB mean   0)
    bin 3: (0, -40) -> 10*log10(1.0001/2)  = -3.0098656839  (dB mean -20)

    Every value differs from the naive dB mean, by up to 17 dB. This trap has
    produced three separate wrong architect claims in this repo; the literals
    below are the forever-pin.
    """
    freqs = np.array([1000.0, 2000.0, 3000.0, 4000.0])
    quiet = np.array([-20.0, -6.0, 0.0, -40.0])
    loud = np.zeros(4)

    result = combine_positions(
        [
            PositionCapture("loud", freqs, loud, SAMPLE_RATE),
            PositionCapture("quiet", freqs, quiet, SAMPLE_RATE),
        ]
    )

    expected_power_mean = np.array(
        [-2.9670862188, -2.0370720196, 0.0, -3.0098656839]
    )
    np.testing.assert_allclose(result.power_mean_db, expected_power_mean, atol=1e-9)

    # The naive dB mean is a different number at every bin but one.
    naive_db_mean = (loud + quiet) / 2.0
    assert not np.allclose(result.power_mean_db, naive_db_mean, atol=0.5)

    # With two positions the median IS the dB mean, so the screen sees the
    # full power-vs-dB gap and flags every bin it exceeds the threshold at.
    np.testing.assert_allclose(result.median_db, naive_db_mean, atol=1e-12)
    assert bool(result.excluded[0]), "7.03 dB disagreement must be flagged"
    assert bool(result.excluded[3]), "16.99 dB disagreement must be flagged"
    assert not bool(result.excluded[2]), "identical positions cannot disagree"


def test_power_mean_matches_an_independent_scalar_computation():
    """Cross-check the vectorised implementation against plain-Python math
    on a randomised set, so the pin above cannot pass on a coincidence.
    """
    rng = np.random.default_rng(11)
    freqs = np.linspace(1000.0, 5000.0, 32)
    levels = rng.uniform(-45.0, 5.0, (6, freqs.size))
    result = combine_positions(
        [
            PositionCapture(f"p{i}", freqs, row, SAMPLE_RATE)
            for i, row in enumerate(levels)
        ]
    )
    for bin_index in range(freqs.size):
        column = levels[:, bin_index]
        expected = 10.0 * math.log10(
            sum(10.0 ** (v / 10.0) for v in column) / len(column)
        )
        assert result.power_mean_db[bin_index] == pytest.approx(expected, abs=1e-9)


# --------------------------------------------------------------------------- #
# A. Synthetic ground truth
# --------------------------------------------------------------------------- #


def test_power_mean_recovers_truth_from_a_dispersed_cloud():
    """A1 — the headline claim: spatial power averaging over a cloud whose
    echo delays are decorrelated recovers the true response, including the
    top-octave rolloff, from captures that are individually combed by a
    -8.8 dB echo.
    """
    freqs, true_db, captures = _cloud(_dispersed_taus())
    result = combine_positions(captures)

    assert result.n_positions == 10
    band = (freqs >= 300.0) & (freqs <= 16_000.0)
    keep = band & ~result.excluded
    assert keep.sum() > 0.9 * band.sum(), "a healthy cloud should flag few bins"

    raw_rms = _relative_rms_error(result.power_mean_db, true_db, keep)
    spec_rms = _relative_rms_error(
        result.power_mean_spec_db,
        smooth_fractional_octave(freqs, true_db, fraction=result.spec_fraction),
        keep,
    )
    # Measured on this construction: raw 0.588 dB, 1/3-oct 0.464 dB; across
    # 25 seeds the worst were 0.673 and 0.482.
    assert raw_rms < 1.0, f"raw power-mean recovery {raw_rms:.3f} dB"
    assert spec_rms < 0.75, f"1/3-oct spec recovery {spec_rms:.3f} dB"

    # The top-octave rolloff is recovered, not merely the mid-band: an
    # individual capture cannot be trusted there, the average can.
    top = (freqs >= 9000.0) & (freqs <= 16_000.0) & ~result.excluded
    assert _relative_rms_error(result.power_mean_spec_db,
                               smooth_fractional_octave(freqs, true_db, fraction=3),
                               top) < 1.0


def test_geometry_lock_is_false_when_delays_are_dispersed():
    """A2a — dispersed delays mean moving nulls, which averaging can fill."""
    _freqs, _true_db, captures = _cloud(_dispersed_taus())
    result = combine_positions(captures)

    assert result.geometry_locked is False
    assert result.geometry.reason == GEOMETRY_DISPERSED
    assert result.geometry.n_confident == 10, "every synthetic echo is credible"
    assert result.geometry.clustered_fraction < 0.7


def test_geometry_lock_is_true_when_every_delay_is_identical():
    """A2b — the flag's reason for existing: a cloud that did not actually
    move (or a speaker-fixed diffraction bounce) has position-stable nulls.
    """
    _freqs, _true_db, captures = _cloud(np.full(10, 300e-6))
    result = combine_positions(captures)

    assert result.geometry_locked is True
    assert result.geometry.reason == GEOMETRY_LOCKED
    assert result.geometry.clustered_fraction == pytest.approx(1.0)
    assert result.geometry.median_tau_us == pytest.approx(300.0, rel=0.1)


def test_aligned_nulls_survive_the_average_which_is_why_the_flag_exists():
    """A3 — the documented limitation.

    When every position sees the same null, the power mean cannot fill it:
    the null passes straight through the average at close to full depth, and
    — critically — the mean-vs-median screen is *blind* to it, because all
    positions agree so mean and median agree too.

    This is not a defect to fix. It is the physics of a position-stable
    interference pattern, and it is exactly why ``geometry_locked`` is a
    separate signal from the exclusion mask. A consumer must treat the flag
    as actionable ("spread the mic further"), because neither the estimator
    nor the screen will save it.
    """
    freqs, true_db, captures = _cloud(np.full(10, 300e-6))
    result = combine_positions(captures)

    # tau = 300 us puts interference nulls at (n + 0.5) / tau.
    tau = 300e-6
    nulls = [(n + 0.5) / tau for n in range(5)]
    for null_hz in nulls:
        if not freqs[0] <= null_hz <= freqs[-1]:
            continue
        index = int(np.argmin(np.abs(freqs - null_hz)))
        depth = result.power_mean_db[index] - true_db[index]
        assert depth < -3.0, (
            f"aligned null at {null_hz:.0f} Hz should survive the average, "
            f"got {depth:+.2f} dB"
        )
        assert not bool(result.excluded[index]), (
            "the mean-vs-median screen is blind to fully-aligned nulls — if "
            "this ever starts flagging them, the screen's semantics changed "
            "and geometry_locked's role must be revisited"
        )

    # And the recovery metric that the dispersed cloud passes, this fails.
    band = (freqs >= 300.0) & (freqs <= 16_000.0)
    keep = band & ~result.excluded
    assert _relative_rms_error(result.power_mean_db, true_db, keep) > 1.5

    # The flag is the only warning the consumer gets.
    assert result.geometry_locked is True


def test_exclusion_mask_and_merged_intervals_agree():
    """The bool mask and the reported (f_lo, f_hi) intervals are two views of
    one fact, and must never drift apart.
    """
    _freqs, _true_db, captures = _cloud(_dispersed_taus())
    # A partially-aligned cloud is what actually trips the screen: half the
    # positions nulled at a bin, half not, so the null-filling power mean and
    # the outlier-rejecting median part company.
    partial = list(captures[:5])
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(5)
    partial += [
        _position(f"locked{i}", freqs, true_db, 300e-6, rng) for i in range(5)
    ]
    result = combine_positions(partial)

    rebuilt = np.zeros_like(result.excluded)
    for f_lo, f_hi in result.excluded_bands_hz:
        rebuilt |= (result.freqs_hz >= f_lo) & (result.freqs_hz <= f_hi)
    np.testing.assert_array_equal(rebuilt, result.excluded)
    assert result.excluded.any(), "partial alignment must trip the screen"


def test_band_spread_separates_a_tight_cloud_from_a_dispersed_one():
    """Cross-position sigma is the observable behind the 1/sqrt(N) accuracy
    story: a cloud whose captures disagree has a large sigma, one whose
    captures are near-identical has a small one.
    """
    _f1, _t1, dispersed = _cloud(_dispersed_taus())
    _f2, _t2, tight = _cloud(np.full(10, 300e-6))

    spread_dispersed = {b.center_hz: b.sigma_db for b in combine_positions(dispersed).band_spread}
    spread_tight = {b.center_hz: b.sigma_db for b in combine_positions(tight).band_spread}

    assert spread_dispersed and spread_tight
    for center in (2000.0, 4000.0, 8000.0):
        assert spread_dispersed[center] > 1.0, center
        assert spread_tight[center] < 0.2, center
        assert spread_dispersed[center] > 5 * spread_tight[center], center

    band = next(b for b in combine_positions(dispersed).band_spread if b.center_hz == 4000.0)
    assert band.max_sigma_db >= band.sigma_db
    assert band.n_bins > 0
    assert band.f_lo < 4000.0 < band.f_hi


def test_single_capture_combines_to_itself_and_reports_no_spread():
    """N=1 is degenerate but legal (the plan demotes single-point capture to
    a diagnostic, it does not forbid it). Spread is *undefined*, not zero.
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(3)
    capture = _position("solo", freqs, true_db, 300e-6, rng)
    result = combine_positions([capture])

    np.testing.assert_allclose(result.power_mean_db, capture.magnitude_db, atol=1e-9)
    np.testing.assert_allclose(result.median_db, capture.magnitude_db, atol=1e-9)
    assert result.band_spread == ()
    assert not result.excluded.any(), "one capture cannot disagree with itself"
    assert result.geometry.reason == GEOMETRY_UNKNOWN
    assert result.geometry_locked is False


# --------------------------------------------------------------------------- #
# Canonical grid contract
# --------------------------------------------------------------------------- #


def test_canonical_grid_is_the_identity_for_captures_sharing_one_grid():
    """The ordinary case — one program, one rfftfreq — must not resample."""
    freqs, _true_db, captures = _cloud(_dispersed_taus(4))
    result = combine_positions(captures)
    np.testing.assert_allclose(result.freqs_hz, freqs, rtol=0, atol=0)


def test_canonical_grid_takes_the_coarsest_spacing_over_common_support():
    """Never invent resolution the coarsest capture did not have, and never
    ask np.interp for a level outside a capture's measured span.
    """
    fine = np.arange(0.0, 20_001.0, 5.0)
    coarse = np.arange(100.0, 18_001.0, 20.0)
    result = combine_positions(
        [
            PositionCapture("fine", fine, np.zeros(fine.size), SAMPLE_RATE),
            PositionCapture("coarse", coarse, np.full(coarse.size, -6.0), SAMPLE_RATE),
        ]
    )
    grid = result.freqs_hz
    assert grid[0] == pytest.approx(100.0)
    assert grid[-1] <= 18_000.0 + 1e-9
    assert float(grid[1] - grid[0]) == pytest.approx(20.0)
    # Flat inputs -> the hand-checked two-position power mean everywhere.
    np.testing.assert_allclose(result.power_mean_db, -2.0370720196, atol=1e-9)


def test_log_spaced_grid_is_rejected():
    """smooth_fractional_octave binary-searches linear bins; a log grid would
    be silently mis-windowed at every frequency. Enforced, not trusted.
    """
    log_grid = np.geomspace(20.0, 20_000.0, 256)
    with pytest.raises(ValueError, match="linear-spaced"):
        combine_positions(
            [PositionCapture("log", log_grid, np.zeros(256), SAMPLE_RATE)]
        )


@pytest.mark.parametrize(
    "captures, match",
    [
        ([], "at least one capture"),
        (
            [PositionCapture("bad", np.array([1.0, 2.0]), np.array([0.0]), SAMPLE_RATE)],
            "length mismatch",
        ),
        (
            [PositionCapture("bad", np.array([2.0, 1.0]), np.zeros(2), SAMPLE_RATE)],
            "strictly increasing",
        ),
        (
            [PositionCapture("bad", np.array([1.0, 2.0]), np.zeros(2), 0)],
            "sample_rate must be positive",
        ),
        (
            [PositionCapture("bad", np.array([1.0, 2.0]), np.array([0.0, np.nan]), SAMPLE_RATE)],
            "must be finite",
        ),
    ],
)
def test_malformed_captures_are_rejected(captures, match):
    with pytest.raises(ValueError, match=match):
        combine_positions(captures)


def test_disjoint_frequency_support_is_rejected():
    with pytest.raises(ValueError, match="no frequency support"):
        combine_positions(
            [
                PositionCapture("low", np.arange(0.0, 1000.0, 10.0), np.zeros(100), SAMPLE_RATE),
                PositionCapture("high", np.arange(5000.0, 6000.0, 10.0), np.zeros(100), SAMPLE_RATE),
            ]
        )


# --------------------------------------------------------------------------- #
# C. Echo detector
# --------------------------------------------------------------------------- #


def _impulse_with_echo(tau_s: float, reflection: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    ir[1000 + int(round(tau_s * SAMPLE_RATE))] += reflection
    return ir + rng.normal(0.0, 1e-4, ir.size)


@pytest.mark.parametrize("tau_us", [200.0, 300.0, 450.0, 700.0])
@pytest.mark.parametrize("reflection", [0.15, 0.36, 0.6])
def test_detect_echo_recovers_synthetic_delay_and_strength(tau_us, reflection):
    """C — a known echo is found to within 10% in delay and 2 dB in level."""
    result = detect_echo(_impulse_with_echo(tau_us * 1e-6, reflection), SAMPLE_RATE)

    assert result.confidence > ECHO_CONFIDENCE_FLOOR, result
    assert result.tau_us == pytest.approx(tau_us, rel=0.10)
    assert result.strength_db == pytest.approx(20.0 * math.log10(reflection), abs=2.0)
    assert result.arrival_crest_db > 20.0


def test_detect_echo_on_a_band_limited_impulse_response():
    """The realistic shape: the echo rides a shaped, rolled-off response
    rather than a bare delta pair.
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(1)
    capture = _position("p", freqs, true_db, 300e-6, rng)
    assert capture.ir is not None

    result = detect_echo(capture.ir, SAMPLE_RATE)
    assert result.confidence > ECHO_CONFIDENCE_FLOOR
    assert result.tau_us == pytest.approx(300.0, rel=0.10)
    assert result.strength_db == pytest.approx(20.0 * math.log10(ECHO_R), abs=2.0)


@pytest.mark.parametrize("seed", range(6))
def test_detect_echo_rejects_white_noise(seed):
    """No direct arrival at all -> no confidence, and no tau claimed."""
    noise = np.random.default_rng(seed).normal(0.0, 1.0, 65_536)
    result = detect_echo(noise, SAMPLE_RATE)

    assert result.confidence == 0.0
    assert result.tau_us == 0.0
    assert result.strength_db == STRENGTH_FLOOR_DB
    assert result.arrival_crest_db < 20.0


@pytest.mark.parametrize("seed", [50, 51, 52, 70, 71])
def test_detect_echo_rejects_an_impulse_with_no_echo(seed):
    """A real arrival but no secondary one: the crest gate passes, so the
    cepstral-concentration and corroboration factors must carry the refusal.
    """
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    ir += rng.normal(0.0, 0.02 if seed < 70 else 0.001, ir.size)

    result = detect_echo(ir, SAMPLE_RATE)
    assert result.arrival_crest_db > 20.0, "the arrival itself is real"
    assert result.confidence < ECHO_CONFIDENCE_FLOOR, result


def test_detect_echo_resolution_floor_is_where_the_docstring_says_it_is():
    """Pin the documented accuracy floor, in both directions.

    ``detect_echo``'s ``search_us`` docstring claims tau is recovered to
    ~1-3% above ~240 us but can read ~9-14% low near the bottom of the
    default window, because both estimators degrade as tau approaches
    ``1 / bandwidth`` (~71 us for the 5-19 kHz default). That is a real
    property of the instrument, not a bug — but it is also the kind of claim
    that silently rots, so it is asserted here.

    The upper assertion is the one that must never regress; the lower one
    documents the floor and would fail loudly if a future change made the
    bottom of the window *accurate*, at which point the docstring is what
    needs updating.
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(4)

    for tau_us in (240.0, 300.0, 400.0, 500.0):
        capture = _position("p", freqs, true_db, tau_us * 1e-6, rng)
        assert capture.ir is not None
        found = detect_echo(capture.ir, SAMPLE_RATE)
        assert found.confidence > ECHO_CONFIDENCE_FLOOR
        assert found.tau_us == pytest.approx(tau_us, rel=0.03), (
            f"mid-window accuracy regressed at {tau_us} us"
        )

    near_floor = _position("floor", freqs, true_db, 156.6e-6, rng)
    assert near_floor.ir is not None
    floored = detect_echo(near_floor.ir, SAMPLE_RATE)
    assert floored.confidence > ECHO_CONFIDENCE_FLOOR, "presence is still detected"
    assert floored.tau_us < 156.6 * 0.95, (
        "the documented near-floor under-read is gone — update the "
        "search_us docstring's resolution-floor paragraph"
    )


def test_detect_echo_never_reports_a_delay_outside_the_search_window():
    """Parabolic refinement runs against the full cepstrum so an edge peak
    keeps its neighbours, but the refined value is clamped: a caller that
    asked for a window must never be handed a delay outside it.
    """
    for lo, hi in ((120.0, 800.0), (250.0, 400.0), (400.0, 800.0)):
        found = detect_echo(
            _impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE, search_us=(lo, hi)
        )
        assert lo <= found.tau_cepstral_us <= hi, (lo, hi, found)
        if found.confidence > 0.0:
            assert lo <= found.tau_us <= hi, (lo, hi, found)


def test_detect_echo_validates_its_inputs():
    ir = _impulse_with_echo(300e-6, ECHO_R)
    with pytest.raises(ValueError, match="must not be empty"):
        detect_echo(np.array([]), SAMPLE_RATE)
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        detect_echo(ir, 0)
    with pytest.raises(ValueError, match="band_hz"):
        detect_echo(ir, SAMPLE_RATE, band_hz=(19_000.0, 5000.0))
    with pytest.raises(ValueError, match="search_us"):
        detect_echo(ir, SAMPLE_RATE, search_us=(800.0, 120.0))
    with pytest.raises(ValueError, match="all zeros"):
        detect_echo(np.zeros(4096), SAMPLE_RATE)


def test_geometry_verdict_needs_at_least_two_confident_estimates():
    """One estimate trivially clusters with itself; that must not read as a
    lock.
    """
    only_one = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE)
    verdict = assess_geometry([only_one, None, None])
    assert verdict.locked is False
    assert verdict.reason == GEOMETRY_UNKNOWN
    assert verdict.n_confident == 1
    assert verdict.n_positions == 3


def test_captures_without_an_ir_report_no_echo_diagnostic():
    """``None`` (not measured) is deliberately distinct from a
    zero-confidence diagnostic (measured, found nothing).
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(9)
    with_ir = _position("with", freqs, true_db, 300e-6, rng)
    without_ir = PositionCapture("without", freqs, with_ir.magnitude_db, SAMPLE_RATE)

    result = combine_positions([with_ir, without_ir])
    assert result.per_position_echo[0] is not None
    assert result.per_position_echo[1] is None
    assert result.position_ids == ("with", "without")


# --------------------------------------------------------------------------- #
# D. Real-data smoke — 2026-07-24/25 JTS3 corpus
# --------------------------------------------------------------------------- #

CORPUS = Path(
    "/Users/jaspercurry/Code/JTS/captures/flat-linearization-20260725/cdhorn-live-session"
)
requires_corpus = pytest.mark.skipif(
    not CORPUS.is_dir(),
    reason=f"laptop-durable capture corpus absent: {CORPUS}",
)


@pytest.fixture(scope="module")
def corpus_irs() -> dict[str, np.ndarray]:
    """Era-exact reconstruction of the run 5 / run 7 impulse responses.

    Program parameters are reused verbatim from the session's own forensics
    script (``captures/flat-linearization-20260725/comb_forensics2.py``),
    which is the authority on what DSP state those captures were taken
    under; re-deriving them here would risk silently analysing a different
    program than the one that was played.
    """
    import glob
    import wave

    from jasper.audio_measurement import program_analysis as pa
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
        build_verify_program,
        render_program_pcm,
    )

    def load(path: str) -> np.ndarray:
        with wave.open(path) as handle:
            raw = handle.readframes(handle.getnframes())
            channels = handle.getnchannels()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        return samples[::2] if channels == 2 else samples

    verify_program = build_verify_program(
        2000.0, leading_pilot_gains_db=(-16.0006, -6.0005), courtesy_prelude=True
    )
    measure_program = build_measure_program(
        {"woofer": -6.0005, "tweeter": -15.0105},
        [
            RoleBand("woofer", 0, FrequencyBand(150.0, 4000.0)),
            RoleBand("tweeter", 1, FrequencyBand(2000.0, 20000.0)),
        ],
        leading_pilot_gains_db=(-16.0006, -6.0005),
        leading_pilot_role="woofer",
        courtesy_prelude=True,
    )

    def render_mono(program) -> np.ndarray:
        pcm = np.asarray(render_program_pcm(program), dtype=np.float64)
        if pcm.ndim == 2:
            if pcm.shape[0] < pcm.shape[1]:
                pcm = pcm.T
            pcm = pcm.sum(axis=1)
        return pcm

    def find_offset(capture: np.ndarray, reference: np.ndarray) -> int:
        n_fft = 1 << (capture.size + reference.size - 1).bit_length()
        cross = np.fft.rfft(capture, n_fft) * np.conj(np.fft.rfft(reference, n_fft))
        window = max(1, capture.size - reference.size // 2)
        return int(np.argmax(np.abs(np.fft.irfft(cross, n_fft)[:window])))

    def impulse_response(capture, program, reference, segment_id) -> np.ndarray:
        segment = program.segment(segment_id)
        offset = find_offset(capture, reference) + segment.start_sample
        return pa._deconvolve_window(capture, segment, offset, SAMPLE_RATE)[0]

    verify_reference = load(str(CORPUS / "run5_verify_program.wav"))
    measure_capture = load(sorted(glob.glob(f"{CORPUS}/*run7_measure.wav"))[-1])

    return {
        "run7_verify": impulse_response(
            load(sorted(glob.glob(f"{CORPUS}/*run7_verify.wav"))[-1]),
            verify_program,
            verify_reference,
            "sweep_verify",
        ),
        "run5_verify": impulse_response(
            load(sorted(glob.glob(f"{CORPUS}/*run5_verify.wav"))[-1]),
            verify_program,
            verify_reference,
            "sweep_verify",
        ),
        "run7_tweeter": impulse_response(
            measure_capture, measure_program, render_mono(measure_program), "sweep_t"
        ),
    }


@requires_corpus
def test_detect_echo_finds_the_corpus_bounce(corpus_irs):
    """D — the detector reproduces the offline forensics finding that
    motivated the whole plan: a discrete echo at ~0.31 ms, ~-8.8 dB
    (r ~= 0.36), present in the summed VERIFY frame and in the
    tweeter-alone MEASURE frame, unchanged between run 5 and run 7 (~1.6 h
    apart, entirely different DSP).
    """
    for name, ir in corpus_irs.items():
        result = detect_echo(ir, SAMPLE_RATE)
        assert 250.0 <= result.tau_us <= 400.0, f"{name}: tau {result.tau_us:.1f} us"
        assert result.confidence > ECHO_CONFIDENCE_FLOOR, f"{name}: {result}"
        # The plan records -8.8 dB / r ~= 0.36 for this bounce.
        assert result.strength_db == pytest.approx(-8.8, abs=1.5), name


@requires_corpus
def test_corpus_frames_read_as_geometry_locked(corpus_irs):
    """D — every existing corpus capture was taken from essentially one
    place, so the delays do not move between frames.

    ``geometry_locked`` firing here is the detector *working*, not failing:
    it is precisely the signal that this corpus cannot be spatially averaged
    into a bounce-free answer, which is why the plan's S0 session is
    mic-move-only.
    """
    echoes = [detect_echo(ir, SAMPLE_RATE) for ir in corpus_irs.values()]
    verdict = assess_geometry(echoes)

    assert verdict.n_confident == 3
    assert verdict.locked is True
    assert verdict.reason == GEOMETRY_LOCKED
    assert verdict.clustered_fraction == pytest.approx(1.0)
    assert 250.0 <= verdict.median_tau_us <= 400.0


@requires_corpus
def test_combining_the_corpus_frames_flags_the_locked_geometry(corpus_irs):
    """End-to-end on real data: the combiner surfaces the lock, and the
    screen stays quiet — the pair of signals a consumer must read together.
    """
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    captures = []
    for name, ir in corpus_irs.items():
        spectrum = np.abs(np.fft.rfft(ir[:N_FFT], N_FFT)) + 1e-12
        captures.append(
            PositionCapture(
                position_id=name,
                freqs_hz=freqs,
                magnitude_db=20.0 * np.log10(spectrum),
                sample_rate=SAMPLE_RATE,
                ir=ir,
            )
        )

    result = combine_positions(captures)
    assert isinstance(result, CombinedResponse)
    assert result.geometry_locked is True
    assert result.n_positions == 3
    assert all(e is not None for e in result.per_position_echo)
