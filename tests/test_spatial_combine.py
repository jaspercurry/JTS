# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the spatial combiner + interference honesty screen.

Five layers, per the plan's fundamentals 1-2
(docs/flat-linearization-plan.md):

A. **Synthetic ground truth** — the primary, hardware-free validation. A
   known smooth "true" response is contaminated with a comb from a discrete
   echo at a per-position delay, then recovered through the combiner. This
   layer also carries the *estimator-discrimination* assertions: an RMS
   recovery metric with the common offset removed cannot tell the plan's
   chosen power mean from the plan-rejected max-hold, so absolute level is
   asserted separately.
B. **Power-domain arithmetic**, pinned to hand-computed literals. The
   power-vs-dB mean confusion has produced three separate wrong architect
   claims in this repo; this is the forever-pin.
C. **Echo detector** on synthetic impulse responses and negative controls,
   including the search window's *rejection* contract: a delay outside the
   caller's window is refused, never clamped onto the edge.
D. **Analysis-grid bounding** — the block-average decimation that keeps the
   combiner's cost bounded must not change the curves it produces.
E. **Real-data smoke** against the 2026-07-24/25 JTS3 corpus. Skipped when
   the (gitignored, laptop-durable) capture directory is absent, which is
   always the case in CI. The corpus lives beside the *main* checkout, so a
   worktree checkout must point at it with ``JTS_FLAT_LIN_CORPUS=<dir>``.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pytest

import jasper.audio_measurement.spatial_combine as spatial_combine
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.spatial_combine import (
    DEFAULT_ECHO_BAND_HZ,
    DEFAULT_ECHO_SEARCH_US,
    ECHO_CONFIDENCE_FLOOR,
    GEOMETRY_DISPERSED,
    GEOMETRY_LOCKED,
    GEOMETRY_MIN_RESOLUTION_STEPS,
    GEOMETRY_UNKNOWN,
    MAX_ANALYSIS_BINS,
    REFUSAL_ALL_ZERO_IR,
    REFUSAL_NO_IN_WINDOW_ECHO,
    STRENGTH_FLOOR_DB,
    CombinedResponse,
    EchoInputError,
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


def _level_offset_db(
    estimate: np.ndarray, truth: np.ndarray, keep: np.ndarray
) -> float:
    """The mean (estimate - truth) over ``keep`` — precisely the common
    offset that :func:`_relative_rms_error` removes.

    This is the discriminating statistic the RMS metric deliberately throws
    away, and the one the plan's estimator choice actually turns on: every
    candidate estimator can track the *shape* of a decorrelated cloud, but
    they land at very different absolute levels.
    """
    return float((estimate - truth)[keep].mean())


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


def test_absolute_level_discriminates_the_power_mean_from_max_hold():
    """A1b — the assertion the RMS metric structurally cannot make.

    ``_relative_rms_error`` removes the common offset, so it scores *shape*
    only. On a decorrelated cloud every plausible estimator tracks the shape
    well — max-hold, the estimator the plan explicitly rejects ("No max-hold
    estimator", non-goals), actually scores *better* on offset-removed RMS
    than the power mean does, because taking the per-bin maximum suppresses
    the seeded noise. Reading that as "max-hold is fine" is exactly the
    mistake, and it is invisible to a shape-only metric.

    The criterion the plan cares about is **level**: research artifact 01
    Question 2 rejects max-hold as *positively biased*. So this test asserts
    absolute level directly.

    Two numbers, on one shared cloud:

    * The power mean carries a known, derivable systematic — the echo's own
      energy, ``+10*log10(1 + r**2)`` = +0.529 dB at r=0.36. Measured here:
      +0.437 dB. The ~0.09 dB gap below the analytic value is expected and
      benign: the prediction assumes comb phase is uniformly distributed
      across positions, and ten stratified delays only approximate that
      (partial coherence pulls the realised offset toward the coherent-sum
      case), while the flagged-bin exclusion removes exactly the bins where
      the positions disagree most. The band is therefore asserted, not the
      point value — the claim is "small, positive, and near the analytic
      prediction", which is what makes the offset *normalisable* by the
      plan's band-relative reference.
    * Max-hold's offset is +2.55 dB on the same cloud — an order of
      magnitude larger, unrelated to r, and not removable by any band
      reference because it varies with N and with the local comb depth.
      That is the failure the plan's non-goal is about.
    """
    freqs, true_db, captures = _cloud(_dispersed_taus())
    result = combine_positions(captures)

    keep = (freqs >= 300.0) & (freqs <= 16_000.0) & ~result.excluded
    predicted_db = 10.0 * math.log10(1.0 + ECHO_R**2)
    assert predicted_db == pytest.approx(0.529, abs=0.001)

    power_mean_offset = _level_offset_db(result.power_mean_db, true_db, keep)
    assert 0.2 <= power_mean_offset <= 0.75, (
        f"power-mean level offset {power_mean_offset:.3f} dB left the band "
        f"consistent with the predicted +{predicted_db:.3f} dB echo energy"
    )

    max_hold_db = np.max(
        np.vstack([c.magnitude_db for c in captures]), axis=0
    )
    max_hold_offset = _level_offset_db(max_hold_db, true_db, keep)
    assert max_hold_offset > 2.0, (
        f"max-hold level offset {max_hold_offset:.3f} dB — the plan-rejected "
        "estimator must visibly fail the level criterion"
    )
    assert max_hold_offset > 4.0 * power_mean_offset

    # ...and the shape-only metric really does fail to separate them, which
    # is why the level assertions above have to exist at all.
    assert _relative_rms_error(max_hold_db, true_db, keep) < _relative_rms_error(
        result.power_mean_db, true_db, keep
    )


def test_geometry_lock_is_false_when_delays_are_dispersed():
    """A2a — dispersed delays mean moving nulls, which averaging can fill."""
    _freqs, _true_db, captures = _cloud(_dispersed_taus())
    result = combine_positions(captures)

    assert result.geometry.locked is False
    assert result.geometry.reason == GEOMETRY_DISPERSED
    # 8 of the 10 stratified delays clear the resolution floor
    # (3 * ~71.4 us = ~214 us); the 150 us and 185 us members sit inside it
    # and are not admitted as evidence — see assess_geometry's "what counts
    # as usable evidence". They are excluded because they are *unresolvable*,
    # not because they were absent: both were detected with confidence > 0.8.
    assert result.geometry.n_confident == 8
    assert result.geometry.n_positions == 10
    assert result.geometry.clustered_fraction < 0.7


def test_geometry_lock_is_true_when_every_delay_is_identical():
    """A2b — the flag's reason for existing: a cloud that did not actually
    move (or a speaker-fixed diffraction bounce) has position-stable nulls.
    """
    _freqs, _true_db, captures = _cloud(np.full(10, 300e-6))
    result = combine_positions(captures)

    assert result.geometry.locked is True
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
    interference pattern, and it is exactly why ``geometry.locked`` is a
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
            "and geometry.locked's role must be revisited"
        )

    # And the recovery metric that the dispersed cloud passes, this fails.
    band = (freqs >= 300.0) & (freqs <= 16_000.0)
    keep = band & ~result.excluded
    assert _relative_rms_error(result.power_mean_db, true_db, keep) > 1.5

    # The flag is the only warning the consumer gets.
    assert result.geometry.locked is True


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
    """The two spread numbers answer two questions, and only one of them
    separates a moved cloud from a stationary one.

    ``max_sigma_db`` (worst single bin, unsmoothed) is the *structure*
    diagnostic and is what discriminates: a cloud whose comb moves has
    positions that disagree violently bin-by-bin, a stationary one does not.

    ``sigma_db`` (per-position band-power level) is the *level* diagnostic
    and deliberately does not discriminate in the top octaves — an octave
    band up there spans many comb periods, so every position's band energy
    lands near ``1 + r**2`` whatever its delay. Its collapse toward zero
    while ``max_sigma_db`` stays large is precisely the "null-dominated, not
    broadly noisy" signature :class:`BandSpread` documents, and it is the
    1/sqrt(N) accuracy story working rather than a dead statistic.

    It does *not* collapse at 1-2 kHz, and that is physics rather than
    noise: with tau in 150-490 us the comb period is 2-6.7 kHz, wider than
    the whole octave band there, so band-power averaging has less than one
    period to work with and the per-position level genuinely moves.
    """
    _f1, _t1, dispersed = _cloud(_dispersed_taus())
    _f2, _t2, tight = _cloud(np.full(10, 300e-6))

    bands_dispersed = {b.center_hz: b for b in combine_positions(dispersed).band_spread}
    bands_tight = {b.center_hz: b for b in combine_positions(tight).band_spread}
    assert bands_dispersed and bands_tight

    # A cloud that never moved disagrees with itself only by seeded noise,
    # in both statistics, at every band it reports.
    for band in bands_tight.values():
        assert band.sigma_db < 0.1, band
        assert band.max_sigma_db < 0.5, band

    # Structure: the moved cloud's worst bin is an order of magnitude worse.
    for center in (2000.0, 4000.0, 8000.0):
        assert bands_dispersed[center].max_sigma_db > 2.0, center
        assert (
            bands_dispersed[center].max_sigma_db
            > 5 * bands_tight[center].max_sigma_db
        ), center

    # Level: near-total collapse where the octave spans many comb periods...
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
    """Two positions, seven band bins, one -10 dB notch — both statistics
    computable on paper.

    Grid 700-1400 Hz in 100 Hz steps. Only the 1 kHz octave band
    (707.1-1414.2 Hz, clipped to the grid) has the ``MIN_BAND_BINS`` = 4
    bins it needs, so exactly one :class:`BandSpread` is reported and its
    membership is unambiguous: 800...1400 Hz, seven bins.

    ``max_sigma_db`` is the per-bin cross-position sigma at the notch:
    ``ddof=1`` std of {0, -10} dB = ``10 / sqrt(2)`` = 7.0710678... dB,
    pinned as an exact literal below.

    ``sigma_db`` is the spread of the two per-position *band levels*, each
    the linear-power mean over the seven band bins:
    p0 = 10*log10(7/7) = 0 dB, p1 = 10*log10((6 + 0.1)/7) = -0.5977... dB,
    so sigma = 0.5977.../sqrt(2). Re-derived from scratch below rather than
    typed as a literal, so the assertion checks the module's formula rather
    than agreeing with a number copied out of its own output.
    """
    freqs = np.arange(700.0, 1401.0, 100.0)
    quiet = np.zeros(freqs.size)
    quiet[3] = -10.0  # 1000 Hz

    result = combine_positions(
        [
            PositionCapture("p0", freqs, np.zeros(freqs.size), SAMPLE_RATE),
            PositionCapture("p1", freqs, quiet, SAMPLE_RATE),
        ]
    )

    assert len(result.band_spread) == 1
    band = result.band_spread[0]
    assert band.center_hz == 1000.0
    assert (band.n_bins, band.f_lo, band.f_hi) == (7, 800.0, 1400.0)

    # Independent re-derivation, in plain Python.
    p1_band_level_db = 10.0 * math.log10((6.0 * 1.0 + 10.0 ** (-10.0 / 10.0)) / 7.0)
    expected_sigma_db = abs(0.0 - p1_band_level_db) / math.sqrt(2.0)
    assert band.sigma_db == pytest.approx(expected_sigma_db, abs=1e-12)
    assert band.sigma_db == pytest.approx(0.42262503, abs=1e-8)
    assert band.max_sigma_db == pytest.approx(10.0 / math.sqrt(2.0), abs=1e-12)


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
    assert result.geometry.locked is False


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


@pytest.mark.parametrize(
    "search_us",
    [(120.0, 800.0), (250.0, 400.0), (400.0, 800.0), (150.0, 300.0), (300.0, 600.0)],
)
@pytest.mark.parametrize("true_tau_us", [180.0, 260.0, 350.0, 500.0, 700.0, 830.0])
def test_detect_echo_never_reports_a_delay_outside_the_search_window(
    search_us, true_tau_us
):
    """The window is a **rejection** contract, and this sweep is built so it
    can actually fail.

    Thirty combinations: six true delays crossed with five windows, so every
    window sees delays below it, inside it, and above it. The earlier
    single-delay version could only ever exercise the in-window case, which
    is precisely the case that cannot detect a clamp.

    Three properties:

    1. ``tau_us`` is either 0.0 (nothing reported) or inside the requested
       window, and a reported value is never a refusal.
    2. A reported value is never pinned *exactly* to a window edge. On its
       own, property 1 cannot catch a clamp — a clamped value satisfies it
       by construction — so this is the assertion that fails against the
       clamp the fix removed.
    3. When the true delay is **above** the window, ``tau_us`` is 0.0 across
       all ten such combinations here. This is the direct anti-clamp
       assertion: a clamped detector reports the upper edge instead.

    Property 3 is deliberately *not* claimed for delays below the window,
    and that is an honest limitation rather than an oversight. The cepstrum
    of a comb has rahmonics at 2*tau, 3*tau..., so a window that excludes
    tau but contains 2*tau can find the rahmonic (measured here: a 260 us
    echo read as ~424 us at confidence 0.63 in a 400-800 us window). That is
    a property of the cepstral estimator, unaffected by the window contract,
    and would need a rahmonic screen to address — out of scope here, and
    recorded so it is not mistaken for a regression.
    """
    lo, hi = search_us
    found = detect_echo(
        _impulse_with_echo(true_tau_us * 1e-6, ECHO_R), SAMPLE_RATE, search_us=search_us
    )

    if found.tau_us != 0.0:
        assert lo <= found.tau_us <= hi, (search_us, true_tau_us, found)
        assert found.refusal == "", found
        assert abs(found.tau_us - lo) > 1e-9 and abs(found.tau_us - hi) > 1e-9, (
            f"tau railed onto a window edge exactly — {found}"
        )

    if true_tau_us > hi:
        assert found.tau_us == 0.0, (
            f"true tau {true_tau_us} us is above the requested window "
            f"{search_us}, so no delay may be reported — got {found}"
        )
        assert found.strength_db == STRENGTH_FLOOR_DB


def test_detect_echo_refuses_rather_than_railing_on_an_echo_past_the_window():
    """B1(a) — the reviewer's exact reproduction.

    A true 830 us echo, searched in windows whose upper edge is 800 us. The
    cepstral peak refines to ~821 us and the envelope to ~802 us: *both* land
    outside, so both candidates are rejected and the diagnostic is a refusal,
    not a number. The raw per-estimator fields stay unclamped on the record —
    that is the evidence a reader needs to understand the refusal, and it is
    also what a clamp would have destroyed by rewriting them as exactly
    800.0 us.
    """
    for search_us in ((120.0, 800.0), (400.0, 800.0)):
        found = detect_echo(
            _impulse_with_echo(830e-6, ECHO_R), SAMPLE_RATE, search_us=search_us
        )
        assert found.refusal == REFUSAL_NO_IN_WINDOW_ECHO, (search_us, found)
        assert found.tau_us == 0.0
        assert found.confidence == 0.0
        assert found.strength_db == STRENGTH_FLOOR_DB
        # Both raw estimates are outside the window, and neither was pulled
        # back to the 800.0 us edge.
        assert found.tau_cepstral_us > search_us[1]
        assert found.tau_envelope_us > search_us[1]
        assert found.tau_cepstral_us != pytest.approx(search_us[1], abs=1.0)


def test_detect_echo_does_not_dress_up_a_sub_window_echo_as_an_in_window_one():
    """B1(b) — a true delay *below* the window's lower edge.

    An echo at 95-110 us cannot be resolved by a detector whose quefrency
    step is ~71 us, and it sits under the 120 us default search floor. What
    the detector actually does is report ~135 us with high confidence — an
    in-window number that is simply wrong.

    That is not fixable inside the estimator (the information is not in the
    band), so it is caught downstream instead: the reported delay is below
    ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``, so
    :func:`assess_geometry` will not admit it as evidence. This test pins
    that the *honesty* holds even though the number does not.
    """
    for true_tau_us in (95.0, 100.0, 104.0, 110.0):
        found = detect_echo(_impulse_with_echo(true_tau_us * 1e-6, ECHO_R), SAMPLE_RATE)
        assert found.resolution_us == pytest.approx(1e6 / 14_000.0, rel=1e-9)
        usable = (
            found.refusal == ""
            and found.confidence >= ECHO_CONFIDENCE_FLOOR
            and found.tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us
        )
        assert not usable, (
            f"a {true_tau_us} us echo read as usable evidence at "
            f"{found.tau_us:.1f} us — the resolution floor stopped working"
        )
        # And it is unusable for the honest reason: unresolvable, not absent.
        assert found.tau_us < GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us


def test_a_cloud_of_unresolvable_delays_does_not_read_as_geometry_locked():
    """B1(c) — the blocker's user-visible consequence.

    Ten positions whose true delays span 60-150 us: genuinely dispersed
    (2.5x spread), and every one of them below the detector's resolution
    floor. The reported delays collapse onto ~135-152 us, six of them with
    confidence >= 0.5, and — before the fix — clustered within +-15% of
    their median at a fraction of **1.0**, i.e. a confident
    ``geometry_locked``: "your mic cloud never moved, go spread it further",
    said about a cloud that moved plenty.

    The evidence rule refuses to cluster unresolvable delays, so the verdict
    is now the honest one: not locked, insufficient evidence.
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(20_260_725)
    captures = [
        _position(f"p{i}", freqs, true_db, float(tau), rng)
        for i, tau in enumerate(np.linspace(60e-6, 150e-6, 10))
    ]
    result = combine_positions(captures)

    assert result.geometry.locked is False
    assert result.geometry.reason == GEOMETRY_UNKNOWN
    assert result.geometry.n_confident == 0
    assert result.geometry.n_positions == 10

    # The old confidence-only rule really would have locked here — this is
    # the pathology, reconstructed from the same diagnostics.
    railed = np.array(
        [
            e.tau_us
            for e in result.per_position_echo
            if e is not None and e.confidence >= ECHO_CONFIDENCE_FLOOR
        ]
    )
    assert railed.size >= 2, "the detector does still report these, confidently"
    median = float(np.median(railed))
    clustered = float(np.mean(np.abs(railed - median) <= 0.15 * median))
    assert clustered == pytest.approx(1.0), (
        "the confidence-only rule would have called this locked; if this "
        "stops being true the regression this test guards has moved"
    )


def test_echo_confidence_floor_sits_in_the_measured_gap():
    """N3 — re-measure both populations the ``ECHO_CONFIDENCE_FLOOR``
    comment cites, so the gap it claims cannot rot silently.

    Negative controls are the two impulse-with-no-echo families: they clear
    the arrival-crest gate, so they exercise the concentration x
    corroboration score rather than short-circuiting on the early return
    (white noise scores an uninformative 0.000 for the latter reason, and is
    covered separately).
    """
    negatives = []
    for noise_sigma in (0.02, 0.001):
        for seed in range(30):
            rng = np.random.default_rng(seed)
            ir = np.zeros(65_536)
            ir[1000] = 1.0
            ir += rng.normal(0.0, noise_sigma, ir.size)
            negatives.append(detect_echo(ir, SAMPLE_RATE).confidence)
    assert len(negatives) == 60
    assert min(negatives) >= 0.0
    assert max(negatives) < 0.15, (
        f"negative-control confidence reached {max(negatives):.3f}; the "
        "ECHO_CONFIDENCE_FLOOR comment's measured 0.000-0.098 range is stale"
    )
    assert sum(c >= ECHO_CONFIDENCE_FLOOR for c in negatives) == 0

    positives = [
        detect_echo(_impulse_with_echo(tau_us * 1e-6, reflection), SAMPLE_RATE).confidence
        for tau_us in (200.0, 300.0, 450.0, 700.0)
        for reflection in (0.15, 0.36, 0.6)
    ]
    assert min(positives) > 0.8, f"true-positive confidence fell to {min(positives):.3f}"
    # The gap the floor sits in is real, and the floor is inside it.
    assert max(negatives) < ECHO_CONFIDENCE_FLOOR < min(positives)


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


def test_detect_echo_input_errors_carry_a_machine_readable_slug():
    """``combine_positions`` turns a detector rejection into a refused
    diagnostic, and it must not do that by matching on message text.
    """
    with pytest.raises(EchoInputError) as excinfo:
        detect_echo(np.zeros(4096), SAMPLE_RATE)
    assert excinfo.value.slug == REFUSAL_ALL_ZERO_IR
    assert isinstance(excinfo.value, ValueError)


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


def test_one_malformed_ir_refuses_that_position_and_nothing_else():
    """S2 — a bad IR at one position is one position's problem.

    Ten good captures plus one whose IR is all zeros. Before this guard the
    detector's ``ValueError`` propagated out of ``combine_positions`` and
    threw away the entire combine: eleven captures' worth of work lost to
    one bad deconvolution, with no curve, no screen, and no diagnosis of
    *which* position was bad.

    The bad position now reports a refusal naming the cause; the curves,
    the screen, and the geometry verdict are computed from the rest exactly
    as if it had never been offered, and ``None`` keeps meaning strictly
    "no IR was supplied".
    """
    freqs, _true_db, captures = _cloud(_dispersed_taus())
    good = combine_positions(captures)

    broken = PositionCapture(
        position_id="broken",
        freqs_hz=freqs,
        magnitude_db=captures[0].magnitude_db,
        sample_rate=SAMPLE_RATE,
        ir=np.zeros(65_536),
    )
    result = combine_positions([*captures, broken])

    assert result.n_positions == 11
    assert result.position_ids[-1] == "broken"

    refused = result.per_position_echo[-1]
    assert refused is not None, "'no IR supplied' is the only meaning of None"
    assert refused.refusal == REFUSAL_ALL_ZERO_IR
    assert refused.confidence == 0.0
    assert refused.tau_us == 0.0
    assert all(e is not None and e.refusal == "" for e in result.per_position_echo[:-1])

    # The refusal contributes nothing to the verdict — same numbers as the
    # ten-position combine, not a diluted or shifted version of them.
    assert result.geometry.n_confident == good.geometry.n_confident
    assert result.geometry.reason == good.geometry.reason
    assert result.geometry.median_tau_us == pytest.approx(good.geometry.median_tau_us)
    assert result.geometry.n_positions == 11


def test_echo_detector_settings_are_plumbed_and_recorded():
    """N6 — a per-position tau is only interpretable against the window it
    was searched in, so the window travels with the result.
    """
    freqs, _true_db, captures = _cloud(_dispersed_taus(4))

    default = combine_positions(captures)
    assert default.echo_band_hz == DEFAULT_ECHO_BAND_HZ
    assert default.echo_search_us == DEFAULT_ECHO_SEARCH_US

    narrow = combine_positions(captures, echo_search_us=(400.0, 800.0))
    assert narrow.echo_search_us == (400.0, 800.0)
    assert narrow.echo_band_hz == DEFAULT_ECHO_BAND_HZ

    # Plumbed, not merely recorded: the delays in this cloud sit at
    # ~150-500 us, so a 400-800 us window changes what the detector reports.
    assert [e.tau_us for e in narrow.per_position_echo] != [
        e.tau_us for e in default.per_position_echo
    ]

    wide_band = combine_positions(captures, echo_band_hz=(2000.0, 19_000.0))
    assert wide_band.echo_band_hz == (2000.0, 19_000.0)
    # A wider band is a finer quefrency step — the whole point of widening.
    assert all(
        e is not None and e.resolution_us < 1e6 / 14_000.0
        for e in wide_band.per_position_echo
    )


# --------------------------------------------------------------------------- #
# D. Analysis-grid bounding
# --------------------------------------------------------------------------- #


def _large_grid_cloud() -> list[PositionCapture]:
    """Three combed captures on a 2**18-point rFFT grid — 131073 bins, 8x
    over ``MAX_ANALYSIS_BINS``.
    """
    freqs = np.fft.rfftfreq(2**18, 1.0 / SAMPLE_RATE)
    true_db = _true_response_db(freqs)
    rng = np.random.default_rng(11)
    captures = []
    for i, tau_s in enumerate((180e-6, 310e-6, 470e-6)):
        level = true_db + rng.normal(0.0, 0.15, freqs.size)
        comb = 1.0 + ECHO_R * np.exp(-2j * np.pi * freqs * tau_s)
        captures.append(
            PositionCapture(
                position_id=f"p{i}",
                freqs_hz=freqs,
                magnitude_db=20.0 * np.log10(np.abs(10.0 ** (level / 20.0) * comb)),
                sample_rate=SAMPLE_RATE,
            )
        )
    return captures


def test_decimated_and_undecimated_curves_agree(monkeypatch):
    """The cost bound must not change the answer.

    ``smooth_fractional_octave`` is an O(bins * window) Python loop whose
    window also grows with the grid, so a 131k-bin combine costs seconds —
    on a Pi 5, worse. Bounding the grid is only legitimate if the curves
    consumers actually read come out the same, so the same cloud is combined
    twice: once through the real cap, once with the cap lifted, and the
    results compared on the decimated grid.

    The three *smoothed* curves agree to well under 0.1 dB (measured: 0.074,
    0.075 and 0.085 dB worst-bin, the worst of it in the bottom few hertz
    where the 1/N-octave window is a couple of bins wide). The raw per-bin
    ``power_mean_db`` deliberately is **not** held to that: at 8x coarser
    spacing it genuinely cannot reproduce fine comb structure bin-for-bin,
    which is the resolution being traded away on purpose. Every downstream
    consumer — the exclusion screen and the spec curve — reads a smoothed
    curve.
    """
    captures = _large_grid_cloud()
    assert captures[0].freqs_hz.size > 8 * MAX_ANALYSIS_BINS / 2

    decimated = combine_positions(captures)
    assert decimated.freqs_hz.size <= MAX_ANALYSIS_BINS
    assert decimated.freqs_hz.size == 16_384

    monkeypatch.setattr(spatial_combine, "MAX_ANALYSIS_BINS", 10**9)
    undecimated = combine_positions(captures)
    assert undecimated.freqs_hz.size == captures[0].freqs_hz.size

    for name in ("power_mean_spec_db", "power_mean_diag_db", "median_diag_db"):
        reference = np.interp(
            decimated.freqs_hz, undecimated.freqs_hz, getattr(undecimated, name)
        )
        worst = float(np.max(np.abs(np.asarray(getattr(decimated, name)) - reference)))
        assert worst < 0.1, f"{name} moved by {worst:.4f} dB under decimation"


def test_decimation_preserves_the_linear_grid_contract_and_band_energy():
    """Block averaging, not subsampling — and the result is still a legal
    linear grid.

    Subsampling a combed curve would keep whichever bins happened to land on
    peaks or nulls and silently shift the level; averaging in linear power
    keeps the band energy the plan's estimator is built on. Both properties
    are checked here on a flat-plus-notch construction where the answer is
    computable: a single -20 dB bin among 2**17 otherwise-0 dB bins carries
    a known energy deficit, and it must survive decimation as a shallow dip
    rather than either vanishing or staying full-depth.
    """
    captures = _large_grid_cloud()
    result = combine_positions(captures)

    fine = captures[0].freqs_hz
    fine_step = float(fine[1] - fine[0])
    block = 8  # ceil(131073 / 16385)

    grid = result.freqs_hz
    steps = np.diff(grid)
    assert np.allclose(steps, steps[0], rtol=1e-9), "decimated grid must stay linear"
    assert float(steps[0]) == pytest.approx(block * fine_step)
    # Each decimated bin sits at its block's CENTRE, which is what the
    # block's averaged power is the level of — not at the block's first bin,
    # which is what a subsample would have kept.
    assert grid[0] == pytest.approx(fine_step * (block - 1) / 2.0)
    assert grid[0] != pytest.approx(float(fine[0]), abs=1e-6)
    # A trailing partial block is dropped, so the top edge slips by at most
    # one decimated step.
    assert grid[-1] < fine[-1]
    assert float(fine[-1] - grid[-1]) < 2.0 * float(steps[0])

    # Energy, not samples: one deep notch on an otherwise flat curve.
    fine = np.linspace(0.0, 24_000.0, 4 * MAX_ANALYSIS_BINS)
    flat = np.zeros(fine.size)
    flat[1234] = -20.0
    decimated = combine_positions(
        [PositionCapture("notched", fine, flat, SAMPLE_RATE)]
    )
    block = 4
    assert decimated.freqs_hz.size == fine.size // block
    dip_index = 1234 // block
    expected_db = 10.0 * math.log10(((block - 1) * 1.0 + 10.0 ** (-20.0 / 10.0)) / block)
    assert decimated.power_mean_db[dip_index] == pytest.approx(expected_db, abs=1e-9)
    assert decimated.power_mean_db[dip_index] == pytest.approx(-1.2, abs=0.05)
    # Neither lost (a subsample could have skipped it) nor still -20 dB.
    assert -2.0 < decimated.power_mean_db[dip_index] < -0.5
    neighbours = np.delete(np.asarray(decimated.power_mean_db), dip_index)
    assert np.allclose(neighbours, 0.0, atol=1e-9)


# --------------------------------------------------------------------------- #
# E. Real-data smoke — 2026-07-24/25 JTS3 corpus
# --------------------------------------------------------------------------- #

# The corpus is gitignored and laptop-durable: it lives under the checkout
# it was captured beside, and is simply absent in CI (where these three
# tests skip). Resolution is repo-root-relative by default so it works in a
# normal clone with no setup; a *worktree* checkout has no captures/ of its
# own and points at the main checkout's copy with
#   JTS_FLAT_LIN_CORPUS=/path/to/JTS/captures/.../cdhorn-live-session
# No absolute path is committed — one machine's home directory is not a
# contract, and a stale one silently skips instead of failing.
_CORPUS_ENV = os.environ.get("JTS_FLAT_LIN_CORPUS", "").strip()
CORPUS = (
    Path(_CORPUS_ENV)
    if _CORPUS_ENV
    else Path(__file__).resolve().parents[1]
    / "captures"
    / "flat-linearization-20260725"
    / "cdhorn-live-session"
)
requires_corpus = pytest.mark.skipif(
    not CORPUS.is_dir(),
    reason=(
        f"laptop-durable capture corpus absent: {CORPUS} "
        "(set JTS_FLAT_LIN_CORPUS to point at it)"
    ),
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

    ``geometry.locked`` firing here is the detector *working*, not failing:
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
    assert result.geometry.locked is True
    assert result.n_positions == 3
    assert all(e is not None for e in result.per_position_echo)
