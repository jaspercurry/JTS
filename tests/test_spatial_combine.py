# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the spatial combiner + interference honesty screen.

Five layers, per the plan's fundamentals 1-2
(docs/historical/linearization-campaign-2026-07.md):

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
   covering all three of its refusal rules: a delay outside the caller's
   window is refused rather than clamped onto the edge; a delay *hugging
   the window's lower edge* is refused rather than believed — that is where
   a below-window echo aliases to, and believing it is how a dispersed
   cloud read as geometry-locked; and a candidate dominated by a stronger
   cepstral peak at lower quefrency is refused as a **rahmonic** of an echo
   the window excluded, which is the mechanism neither of the first two
   could reach. That third rule shipped after this file spent a revision
   pinning its absence as a deliberate known-wrong assertion; the inverted
   assertions now live in
   ``test_rahmonic_false_lock_under_a_raised_window_is_screened``, and the
   margin it turns on is bracketed from both sides by
   ``test_rahmonic_margin_is_load_bearing_in_both_directions``.
C2. **The three S0 hardenings on synthetic ground truth** — the
   signal-presence screen (``band_below_passband``), the
   earlier-dominant-arrival disclosure, the thin-evidence geometry
   qualifier, and the per-record ``effective_floor_us``. Each rule's
   *motivating* record is real and lives in layer F; these build the same
   shape from known truth so the rules are exercised in CI too, and each
   names the corpus record it stands in for.
D. **Analysis-grid bounding** — the block-average decimation that keeps the
   combiner's cost bounded must not change the curves it produces.
E. **Real-data smoke** against the 2026-07-24/25 JTS3 corpus. Skipped when
   the (gitignored, laptop-durable) capture directory is absent, which is
   always the case in CI. The corpus lives beside the *main* checkout, so a
   worktree checkout must point at it with ``JTS_FLAT_LIN_CORPUS=<dir>``.
F. **Real-data acceptance** against the 2026-07-25 S0 session — the
   electrical loopback that found a confident tau in filter stopband
   residue, and the ground-plane leg whose proud-capsule arrivals collapsed
   three records into an uninformative zero. Gated on a *second* root,
   ``JTS_FLAT_LIN_S0=<dir>``, because the S0 session is a different capture
   protocol from layer E's corpus and the two need not live together.
"""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, replace

import numpy as np
import pytest

import jasper.audio_measurement.spatial_combine as spatial_combine
from jasper.audio_measurement.analysis import smooth_fractional_octave
from tests._flat_lin_corpus import (
    CDHORN_ROOT,
    LOOPBACK_TWEETER_PASSBAND_HZ,
    LOOPBACK_WOOFER_PASSBAND_HZ,
    S0_GROUND_PLANE,
    S0_MAIN,
    S0_PROTOCOL_SEARCH_US,
    S0_SUMMED_PASSBAND_HZ,
    requires_cdhorn,
    requires_s0,
    s0_position_irs,
    sweep_anchor,
)
from tests._flat_lin_corpus import loopback_irs as _loopback_irs
from jasper.audio_measurement.spatial_combine import (
    BAND_BELOW_PASSBAND_MARGIN_DB,
    CORROBORATION_LOOSE,
    DEFAULT_ECHO_BAND_HZ,
    DEFAULT_ECHO_SEARCH_US,
    EARLIER_ARRIVAL_DOMINANCE_DB,
    ECHO_CONFIDENCE_FLOOR,
    GEOMETRY_DISPERSED,
    GEOMETRY_LOCKED,
    GEOMETRY_MIN_CONFIDENT,
    GEOMETRY_MIN_RESOLUTION_STEPS,
    GEOMETRY_UNKNOWN,
    MAX_ANALYSIS_BINS,
    RAHMONIC_MARGIN,
    REFUSAL_ALL_ZERO_IR,
    REFUSAL_BAD_BAND_HZ,
    REFUSAL_BAND_TOO_NARROW,
    REFUSAL_BAD_SIGNAL_BAND_HZ,
    REFUSAL_BAND_BELOW_PASSBAND,
    REFUSAL_EARLIER_DOMINANT_ARRIVAL,
    REFUSAL_LOW_ARRIVAL_CREST,
    REFUSAL_NO_IN_WINDOW_ECHO,
    REFUSAL_RAHMONIC_OF_LOWER_DELAY,
    REFUSAL_TAU_AT_WINDOW_LOWER_EDGE,
    REFUSAL_WINDOW_TOO_SHORT,
    STRENGTH_FLOOR_DB,
    WINDOW_EDGE_MARGIN_STEPS,
    CombinedResponse,
    EchoDiagnostic,
    EchoInputError,
    PositionCapture,
    assess_geometry,
    combine_positions,
    detect_echo,
    position_residuals,
    usable_echo_estimates,
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
# A2. Retained per-position curves
#
# The combiner is the single owner of the canonical grid, the decimation and
# the diagnostic smoothing, so a consumer that needs one position's curve
# must be able to read it here rather than rebuild that chain (plan PR-1's
# input contract). These pin that the retained arrays really are the ones the
# combined curves were reduced from.
# --------------------------------------------------------------------------- #


def test_per_position_curves_are_retained_index_aligned_and_immutable():
    """The two new arrays are row-for-row with ``position_ids``, live on the
    result's own grid, and are read-only like every other array here."""
    _freqs, _true, captures = _cloud(_dispersed_taus(4))
    result = combine_positions(captures)

    assert result.per_position_db.shape == (4, result.freqs_hz.size)
    assert result.per_position_diag_db.shape == result.per_position_db.shape
    assert result.position_ids == tuple(c.position_id for c in captures)
    assert not result.per_position_db.flags.writeable
    assert not result.per_position_diag_db.flags.writeable

    # Row i is capture i, unsmoothed, on the shared grid. These captures all
    # share one grid, so the resample is the identity and the comparison is
    # against the input itself.
    for row, capture in zip(result.per_position_db, captures, strict=True):
        np.testing.assert_allclose(row, capture.magnitude_db, atol=1e-9)


def test_the_retained_curves_are_what_the_combined_ones_are_made_of():
    """No second construction: the power mean, the median and the diagnostic
    smoothing are all recomputable from ``per_position_db``, and
    ``per_position_diag_db`` is that array through the *same* smoothing call
    the combined curves use."""
    _freqs, _true, captures = _cloud(_dispersed_taus(5))
    result = combine_positions(captures)
    stacked = result.per_position_db

    np.testing.assert_allclose(
        result.power_mean_db,
        10.0 * np.log10(np.mean(10.0 ** (stacked / 10.0), axis=0)),
        atol=1e-12,
    )
    np.testing.assert_allclose(result.median_db, np.median(stacked, axis=0), atol=1e-12)
    for row, diag in zip(stacked, result.per_position_diag_db, strict=True):
        np.testing.assert_allclose(
            diag,
            smooth_fractional_octave(
                result.freqs_hz, row, fraction=result.diag_fraction
            ),
            atol=1e-12,
        )


@pytest.mark.parametrize("n_positions", [1, 2, 5])
def test_the_combiner_makes_exactly_three_plus_n_smoothing_passes(
    monkeypatch, n_positions
):
    """The structural half of the per-position curves' cost, guarded.

    ``CombinedResponse.per_position_diag_db`` documents the cost it adds as a
    pass count — three combined curves plus one per position — and quotes a
    wall-clock share alongside it. The share is an observation about one
    laptop on one day and is left as that; the *count* is a property of the
    code, so it is asserted. A future change that smoothed per position twice,
    or that reintroduced the per-position pass ``_band_spread`` was rewritten
    to avoid, would multiply the dominant term in this function's runtime
    without failing anything else in this file.
    """
    calls: list[int] = []
    real = spatial_combine.smooth_fractional_octave

    def counting(freqs, values, *, fraction):
        calls.append(fraction)
        return real(freqs, values, fraction=fraction)

    monkeypatch.setattr(spatial_combine, "smooth_fractional_octave", counting)
    _freqs, _true, captures = _cloud(_dispersed_taus(n_positions))
    result = combine_positions(captures)

    assert len(calls) == 3 + n_positions
    # ...and they are the passes the docstrings name: the spec curve once, the
    # diagnostic fraction for the power mean, the median, and each position.
    assert calls.count(result.spec_fraction) == 1
    assert calls.count(result.diag_fraction) == 2 + n_positions


def test_the_retained_curves_follow_the_decimated_grid():
    """When the analysis grid is block-averaged down to ``MAX_ANALYSIS_BINS``,
    the retained per-position rows are decimated with it — they are the
    post-decimation array, not the captures' own."""
    captures = _large_grid_cloud()
    result = combine_positions(captures)

    assert captures[0].freqs_hz.size > MAX_ANALYSIS_BINS
    assert result.freqs_hz.size <= MAX_ANALYSIS_BINS
    assert result.per_position_db.shape == (len(captures), result.freqs_hz.size)
    assert result.per_position_diag_db.shape == result.per_position_db.shape


def test_the_per_position_fields_are_additive():
    """A :class:`CombinedResponse` built without them still constructs, which
    is what makes the extension additive: every existing positional or
    keyword construction keeps working, and the empty array is the honest
    "these were never retained" value the null gate refuses on."""
    from dataclasses import fields

    names = [field.name for field in fields(CombinedResponse)]
    assert names[-3:] == [
        "per_position_db", "per_position_diag_db", "position_roles",
    ]

    _freqs, _true, captures = _cloud(_dispersed_taus(3))
    result = combine_positions(captures)
    stripped = CombinedResponse(
        freqs_hz=result.freqs_hz,
        power_mean_db=result.power_mean_db,
        median_db=result.median_db,
        power_mean_diag_db=result.power_mean_diag_db,
        power_mean_spec_db=result.power_mean_spec_db,
        median_diag_db=result.median_diag_db,
        excluded=result.excluded,
        excluded_bands_hz=result.excluded_bands_hz,
        n_positions=result.n_positions,
        position_ids=result.position_ids,
        per_position_echo=result.per_position_echo,
        geometry=result.geometry,
        band_spread=result.band_spread,
        flag_threshold_db=result.flag_threshold_db,
        diag_fraction=result.diag_fraction,
        spec_fraction=result.spec_fraction,
        echo_band_hz=result.echo_band_hz,
        echo_search_us=result.echo_search_us,
    )
    assert stripped.per_position_db.size == 0
    assert stripped.per_position_diag_db.size == 0


def test_usable_echo_estimates_is_exactly_the_set_assess_geometry_clusters():
    """The admission rules have one implementation, and this is the pin.

    :func:`usable_echo_estimates` was extracted so the null gate and the
    geometry verdict could not drift about what counts as evidence. The
    contract is that its result size *is* ``GeometryLock.n_confident``, on
    every population this suite can build — including the three that each
    admission rule exists to exclude.
    """
    good = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE)
    refused = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE, search_us=(650.0, 1000.0))
    unconfident = replace(good, confidence=ECHO_CONFIDENCE_FLOOR - 0.01)
    unresolvable = replace(good, tau_us=2.0 * good.resolution_us)
    zero_resolution = replace(good, tau_us=0.0, resolution_us=0.0)

    populations = [
        [],
        [None, None],
        [good, good, good],
        [good, refused, unconfident, unresolvable, zero_resolution, None],
        [refused, unconfident, unresolvable],
    ]
    for echoes in populations:
        assert len(usable_echo_estimates(echoes)) == assess_geometry(echoes).n_confident
        # ...and the floor is honoured identically through both doors.
        raised = 0.99
        assert len(
            usable_echo_estimates(echoes, confidence_floor=raised)
        ) == assess_geometry(echoes, confidence_floor=raised).n_confident

    # The one that matters most: a refused record is not evidence, however
    # confident-looking its raw estimator fields are.
    assert refused.refusal != ""
    assert usable_echo_estimates([refused]) == ()


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


@pytest.mark.parametrize(
    "kwargs, match",
    [
        # Malformed by VALUE — the pair is well-shaped but wrong.
        ({"echo_band_hz": (19_000.0, 5000.0)}, "echo_band_hz"),
        ({"echo_band_hz": (0.0, 19_000.0)}, "echo_band_hz"),
        ({"echo_band_hz": (5000.0, float("inf"))}, "echo_band_hz"),
        ({"echo_search_us": (800.0, 120.0)}, "echo_search_us"),
        ({"echo_search_us": (-10.0, 800.0)}, "echo_search_us"),
        ({"echo_search_us": (120.0, float("nan"))}, "echo_search_us"),
        # Malformed by SHAPE — not a pair at all. Each of these used to
        # escape as something other than the documented ValueError.
        ({"echo_search_us": (120.0,)}, "echo_search_us"),  # was IndexError
        ({"echo_search_us": None}, "echo_search_us"),  # was TypeError
        ({"echo_search_us": (120.0, 800.0, 900.0)}, "echo_search_us"),  # was ACCEPTED
        ({"echo_band_hz": (5000.0,)}, "echo_band_hz"),
        ({"echo_band_hz": None}, "echo_band_hz"),
        ({"echo_band_hz": (5000.0, 19_000.0, 20_000.0)}, "echo_band_hz"),
        # Not a sequence, and not numeric.
        ({"echo_search_us": 120.0}, "echo_search_us"),
        ({"echo_search_us": ("lo", "hi")}, "echo_search_us"),
    ],
)
def test_malformed_echo_config_raises_rather_than_refusing_every_position(
    kwargs, match
):
    """N6 — malformed *config* fails loudly; malformed *data* refuses one
    position. The two must not be confused.

    Before this guard, a mistyped search window was handed straight to the
    detector, which raised, which ``_echo_for`` caught and turned into a
    refused diagnostic — once per position. A caller who inverted a tuple
    got back a clean :class:`CombinedResponse` reporting "no echo found at
    any position", which reads as a measurement result and is not one. The
    honest distinction is ownership: ``echo_search_us`` is the caller's own
    argument, wrong for every position simultaneously and un-diagnosable
    from the captures, so it fails the call the same way
    ``flag_threshold_db`` always has. An all-zero IR belongs to one capture.

    **Shape is checked before value, and this test discriminates on the
    exception type.** ``pytest.raises(ValueError)`` does not catch
    ``IndexError`` or ``TypeError`` — neither is a ``ValueError`` subclass —
    so the shape rows below fail against the previous implementation rather
    than passing incidentally. Three of them are the ways the old code went
    wrong: a 1-tuple raised ``IndexError`` off ``bounds[1]``, ``None``
    raised ``TypeError`` on subscripting, and a 3-tuple was *accepted
    outright*, silently discarding the third element and running a
    measurement the caller did not ask for. That last one is the worst of
    the three, because it returned a plausible :class:`CombinedResponse`.
    """
    _freqs, _true_db, captures = _cloud(_dispersed_taus(3))
    with pytest.raises(ValueError, match=match):
        combine_positions(captures, **kwargs)


def test_a_band_above_one_captures_nyquist_refuses_only_that_position():
    """N6's deliberate exception — the one case that stays on the data side.

    A well-formed band that exceeds *a particular capture's* Nyquist is not
    a property of the config; it is an interaction between the config and
    one capture's sample rate, and the other positions are unaffected. So it
    refuses that position rather than failing the combine — the same
    treatment a malformed IR gets, for the same reason.
    """
    freqs, _true_db, captures = _cloud(_dispersed_taus(3))
    narrow_rate = PositionCapture(
        position_id="lofi",
        freqs_hz=freqs,
        magnitude_db=captures[0].magnitude_db,
        sample_rate=8000,  # Nyquist 4 kHz, below the 5 kHz band floor
        ir=_impulse_with_echo(300e-6, ECHO_R),
    )

    result = combine_positions([*captures, narrow_rate])
    assert result.n_positions == 4
    refused = result.per_position_echo[-1]
    assert refused is not None
    assert refused.refusal == REFUSAL_BAD_BAND_HZ
    assert refused.tau_us == 0.0
    assert all(
        e is not None and e.refusal != REFUSAL_BAD_BAND_HZ
        for e in result.per_position_echo[:-1]
    )


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


def _impulse_with_two_echoes(
    early_s: float,
    early_r: float,
    late_s: float,
    late_r: float,
    seed: int = 0,
) -> np.ndarray:
    """``_impulse_with_echo`` with a second, later reflection.

    Two independent bounces off different boundaries — *not* a comb and its
    rahmonic. The distinction is the whole subject of
    :func:`test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one`.
    """
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    ir[1000 + int(round(early_s * SAMPLE_RATE))] += early_r
    ir[1000 + int(round(late_s * SAMPLE_RATE))] += late_r
    return ir + rng.normal(0.0, 1e-4, ir.size)


@pytest.mark.parametrize("tau_us", [240.0, 300.0, 450.0, 700.0])
@pytest.mark.parametrize("reflection", [0.15, 0.36, 0.6])
def test_detect_echo_recovers_synthetic_delay_and_strength(tau_us, reflection):
    """C — a known echo is found to within 10% in delay and 2 dB in level.

    The low anchor is 240 us, not the 200 us it used to be, because the
    default window's lower edge is now *refused* for one quefrency step
    (``WINDOW_EDGE_MARGIN_STEPS``) and the estimators' own near-floor
    under-read stretches that dead zone to ~1.3 steps for the weakest
    echoes: a true 200 us delay at r=0.15 is read as 191 us, i.e. 0.99
    steps above the 120 us edge, and refused. That boundary is not this
    test's subject — it is pinned directly, from both sides, by
    :func:`test_detect_echo_refuses_a_candidate_hugging_the_window_lower_edge`.
    This test covers what the default window can actually measure.
    """
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

    ``detect_echo``'s ``search_us`` docstring claims two things about the
    bottom of the window: that tau is recovered accurately once clear of it
    (asserted here at 3% over 240-500 us), and that the bottom
    ``WINDOW_EDGE_MARGIN_STEPS`` of whatever window is asked for does not
    measure at all. Both are real properties of the instrument, and both are
    the kind of claim that silently rots, so both are asserted here.

    The near-floor half used to assert an *under-read*: a 156.6 us echo was
    reported at ~9-14% low, confidently, and the honesty burden was pushed
    downstream onto :func:`assess_geometry`'s resolution rule. It is now
    refused at the source instead, because an estimate that close to the
    edge is indistinguishable from a below-window echo aliasing up — and the
    refusal still carries the raw estimates, so the reason is auditable
    rather than merely asserted.
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
    assert floored.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, floored
    assert floored.tau_us == 0.0
    assert floored.confidence == 0.0
    assert floored.strength_db == STRENGTH_FLOOR_DB

    # The refusal is evidenced, not bare: both raw estimates really did land
    # inside one quefrency step of the 120 us edge (measured: cepstral
    # 145.2 us = 0.35 steps, envelope 135.4 us = 0.22 steps), and the ripple
    # was *concentrated* — this is a confident-looking wrong answer, which is
    # exactly why confidence alone could not have caught it.
    edge_margin_us = WINDOW_EDGE_MARGIN_STEPS * floored.resolution_us
    lower_edge = DEFAULT_ECHO_SEARCH_US[0]
    assert floored.tau_cepstral_us - lower_edge < edge_margin_us
    assert floored.tau_envelope_us - lower_edge < edge_margin_us
    assert floored.concentration > 0.5, (
        "a low-concentration reading would mean the crest/concentration "
        "factors caught this, and the edge rule is not what is being pinned"
    )


@pytest.mark.parametrize(
    "search_us",
    [
        (120.0, 800.0),
        (250.0, 400.0),
        (400.0, 800.0),
        (150.0, 300.0),
        (300.0, 600.0),
        (650.0, 1000.0),
        (700.0, 1000.0),
        (800.0, 1200.0),
    ],
)
@pytest.mark.parametrize(
    "true_tau_us", [95.0, 150.0, 180.0, 260.0, 350.0, 500.0, 700.0, 830.0]
)
def test_detect_echo_never_reports_a_delay_outside_the_search_window(
    search_us, true_tau_us
):
    """The window is a **rejection** contract, and this sweep is built so it
    can actually fail.

    Sixty-four combinations: eight true delays crossed with eight windows, so
    every window sees delays below it, inside it, and above it. The earlier
    single-delay version could only ever exercise the in-window case, which
    is precisely the case that cannot detect a clamp.

    Four properties:

    1. ``tau_us`` is either 0.0 (nothing reported) or inside the requested
       window, and a reported value is never a refusal.
    2. A reported value is never pinned *exactly* to a window edge. On its
       own, property 1 cannot catch a clamp — a clamped value satisfies it
       by construction — so this is the assertion that fails against the
       clamp the fix removed.
    3. When the true delay is **above** the window, ``tau_us`` is 0.0. This
       is the direct anti-clamp assertion: a clamped detector reports the
       upper edge instead.
    4. When the true delay is **below** the window, the diagnostic is never
       *usable evidence* — never simultaneously unrefused, at or above
       ``ECHO_CONFIDENCE_FLOOR``, and carrying a non-zero tau. This half of
       the sweep is what round 1 was missing, and it is the half the
       false-lock blocker lived in: a below-window echo does not vanish, it
       aliases up onto the bottom of the window, so property 1 alone is
       satisfied by a confidently wrong number.

    Property 4 is stated as "not usable" rather than "reports nothing"
    because the cepstrum of a comb has rahmonics at 2*tau, 3*tau..., so a
    window that excludes tau but contains a rahmonic can still find one.

    **Scope — the top three windows are the ones that used to fail.** For
    two revisions this parametrisation stopped at ``lo <= 400``, and that
    ceiling was load-bearing rather than incidental. An earlier version of
    this docstring claimed the rahmonic was harmless because, across a wider
    grid, "exactly one below-window case reported a value at all ... and it
    scored 0.39, below the floor, so it cannot enter a geometry verdict".
    That conclusion was true of the grid it was measured on and **false
    outside it**: that grid stopped at ``lo = 600``, one increment short of
    the regime where the rahmonic becomes credible. At ``lo >= 650`` a
    below-window echo was reported at up to confidence 1.000 and *did*
    enter geometry verdicts, so widening this list upward would have failed
    property 4 by design — the defect was pinned in its own test instead of
    being smuggled in here as a passing case.

    The rahmonic screen (``RAHMONIC_MARGIN``) closed that, so the three
    windows are now *in* this list rather than excluded from it, and
    property 4 is asserted across the regime that used to break it. That is
    the point of putting them here: the scope note was prose, and this is
    the same claim as an executable one.
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

    if true_tau_us < lo:
        usable = (
            found.refusal == ""
            and found.confidence >= ECHO_CONFIDENCE_FLOOR
            and found.tau_us > 0.0
        )
        assert not usable, (
            f"true tau {true_tau_us} us is below the requested window "
            f"{search_us}, so nothing here may count as evidence — got {found}"
        )


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


@pytest.mark.parametrize("search_lo_us", [120.0, 200.0, 300.0, 400.0])
def test_detect_echo_refuses_a_candidate_hugging_the_window_lower_edge(search_lo_us):
    """SF1 — the lower edge is refused, and the dead zone is one step wide.

    Both halves matter, and they pull against each other:

    * **Refused below the margin.** A true echo below the window aliases up
      onto the bottom of it — the envelope's search *starts* at
      ``search_us[0]``, so a below-window arrival's skirt puts the argmax a
      few microseconds above that floor, and the cepstral peak lands on the
      lowest search bin for the same reason. The two then agree, so
      corroboration and confidence are both high and the number is still
      wrong. Refusing is the only honest answer available: from inside the
      band, an in-window echo at the edge and a below-window echo aliasing
      to it are the same measurement.
    * **Not refused above it.** A margin is only defensible if it is narrow
      enough that the instrument still measures what it can. A genuine echo
      1.5 steps above the edge is reported normally at every window tested
      here, so this is a one-step rule and not a broad dead zone.

    Parametrised over four lower edges so the rule is pinned as *relative to
    the requested window*, not as a property of the 120 us default. That
    generality is the whole point of the fix: the round-1 guard was anchored
    at zero delay and therefore only ever screened a low window.
    """
    resolution_us = 1e6 / 14_000.0  # the 5-19 kHz default band's step
    search_us = (search_lo_us, search_lo_us + 500.0)

    # Below the window entirely — half its lower edge, far enough down that
    # the aliasing is unambiguous — and refused for it. (A delay only just
    # below the edge is a milder case: the aliased peak can itself land
    # below ``search_us[0]`` and be rejected by the plain window contract,
    # or corroborate poorly enough to score zero. Those are honest outcomes
    # too, but they are not the mechanism this test is pinning.)
    below = detect_echo(
        _impulse_with_echo((search_lo_us / 2.0) * 1e-6, ECHO_R),
        SAMPLE_RATE,
        search_us=search_us,
    )
    assert below.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, below
    assert below.tau_us == 0.0
    assert below.confidence == 0.0
    assert below.strength_db == STRENGTH_FLOOR_DB
    # The refusal is evidenced: at least one surviving candidate really was
    # inside the margin, which is the condition the rule fires on.
    margin_us = WINDOW_EDGE_MARGIN_STEPS * below.resolution_us
    assert margin_us == pytest.approx(resolution_us, rel=1e-9)
    hugging = [
        tau
        for tau in (below.tau_cepstral_us, below.tau_envelope_us)
        if search_us[0] <= tau <= search_us[1]
    ]
    assert hugging, below
    assert min(tau - search_us[0] for tau in hugging) <= margin_us

    # 1.5 steps above the edge: a real, resolvable delay, reported normally.
    clear_us = search_lo_us + 1.5 * resolution_us
    clear = detect_echo(
        _impulse_with_echo(clear_us * 1e-6, ECHO_R), SAMPLE_RATE, search_us=search_us
    )
    assert clear.refusal == "", clear
    assert clear.confidence > ECHO_CONFIDENCE_FLOOR, clear
    assert clear.tau_us == pytest.approx(clear_us, rel=0.05)
    assert clear.tau_us - search_us[0] > margin_us


def _impulse_with_sample_spikes(*spikes: tuple[int, float], seed: int = 0) -> np.ndarray:
    """An impulse at 1000 plus reflections at exact **sample** offsets.

    ``_impulse_with_echo`` takes a delay in seconds and rounds it to a
    sample, which is the wrong tool for testing where a sample boundary is:
    the quantity under test would be hidden inside the fixture. Here the
    caller names the sample.
    """
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    for offset, reflection in spikes:
        ir[1000 + offset] += reflection
    return ir + rng.normal(0.0, 1e-4, ir.size)


def test_the_envelope_never_searches_below_the_requested_window():
    """#1750 — ``search_us[0]`` is the floor of the envelope's candidate
    range, not a value to round to the nearest sample.

    The bounds used to be ``int(round(search_lo * rate))``, so a window whose
    lower edge fell in the first half of a sample searched from *below* it —
    at (150, 1000) us and 48 kHz, from sample 7 (145.833 us) rather than
    sample 8 (166.667 us), i.e. up to half a sample outside the caller's
    window. This is that boundary, pinned where it is visible: a dominant
    reflection at exactly sample 7 and a weaker one at sample 20.

    Under ``ceil`` the envelope cannot select sample 7 and answers from
    sample 8 (156.25 us after the parabola's half-sample clamp, which is
    refinement, not search). Under the old ``round`` it selected sample 7 and
    answered 145.8 us — a delay the caller's own window excludes — which is
    what makes this test fail if the bound is ever put back.
    """
    ir = _impulse_with_sample_spikes((7, 0.9))
    search_us = (150.0, 1000.0)
    assert search_us[0] * 1e-6 * SAMPLE_RATE == pytest.approx(7.2), (
        "this test is about a lower edge that lands between samples 7 and 8; "
        "if that is no longer true the case has stopped being the case"
    )

    found = detect_echo(ir, SAMPLE_RATE, search_us=search_us)

    # The excluded sample is not the answer. The floor is sample 8 less the
    # parabola's clamp; anything below it means the search itself reached
    # outside the window.
    spike_us = 7.0 / SAMPLE_RATE * 1e6
    floor_us = 7.5 / SAMPLE_RATE * 1e6
    assert found.tau_envelope_us >= floor_us - 1e-9, found
    assert found.tau_envelope_us != pytest.approx(spike_us, abs=1.0), found

    # ...and the excluded sample is reported as what it is — a below-window
    # arrival — rather than being silently dropped.
    assert found.earlier_arrival_us == pytest.approx(spike_us, abs=1e-9), found


def test_the_below_window_scan_and_the_envelope_bound_share_one_definition():
    """#1750 — no sample is both the first in-window candidate and a
    below-window arrival.

    Before the fix ``earlier_arrival_us`` used ``ceil(search_lo * rate)``
    while the envelope's candidate range used ``round(...)``, so at any
    window whose lower edge is not sample-aligned the sample between them
    was **both**: S0's ground_plane_01 reported an ``earlier_arrival_us`` of
    145.83 us and a ``tau_envelope_us`` of 137.5 us that were the same
    envelope sample, refined in opposite directions. They are one definition
    now, and this is the property that says so — swept over every alignment
    of the lower edge within a sample, because a single window would only
    pin one of them.

    Swept over every alignment of the lower edge within a sample, because a
    single window only pins one of them, and driven from a reflection placed
    on the boundary sample itself — which is the only place the two
    definitions could ever have disagreed.

    The sweep sits around sample 15 (~312 us) rather than S0's sample 7:
    the 5-19 kHz band resolves ~71 us, so a reflection one or two samples
    off the direct arrival is inside its main lobe and produces no local
    maximum for the below-window scan to find at all. That is a property of
    the band, not of the boundary, and putting the fixture where the
    instrument can resolve it is what keeps this a test of the boundary.
    """
    for numerator in range(0, 10):
        # Lower edges at 15.0, 15.1, ... 15.9 samples: every alignment.
        # 15.0 is the sample-aligned control — ``round`` and ``ceil`` agree
        # there, so that row passed before #1750 too and must still.
        search_lo_us = (15.0 + numerator / 10.0) / SAMPLE_RATE * 1e6
        first_index = spatial_combine._ceil_samples(search_lo_us * 1e-6, SAMPLE_RATE)
        where = (search_lo_us, first_index)

        # A lone dominant reflection on the last sample BELOW the window.
        below = detect_echo(
            _impulse_with_sample_spikes((first_index - 1, 0.9)),
            SAMPLE_RATE,
            search_us=(search_lo_us, 1000.0),
        )
        assert below.earlier_arrival_us == pytest.approx(
            (first_index - 1) / SAMPLE_RATE * 1e6, abs=1e-9
        ), where
        # The one invariant: the envelope's own answer and the below-window
        # arrival are not the same sample. The gap is at least half a sample
        # by construction — the arrival sits on whole sample ``first - 1``
        # and the envelope's lowest reachable answer is ``first`` less the
        # parabola's clamp — so half a sample is the assertion, not "one is
        # bigger". Under the old bounds the envelope selected the excluded
        # sample and the two readings were the same sample refined in
        # opposite directions, which a bare ``>`` accepts: measured at a
        # 15.1-sample edge, 312.5010 us against 312.5000 us. S0's
        # ground_plane_01 is the same shape at S0's scale (145.83 us
        # reported below the window, 137.5 us reported as the answer).
        half_sample_us = 0.5 / SAMPLE_RATE * 1e6
        assert below.tau_envelope_us - below.earlier_arrival_us >= (
            half_sample_us - 1e-9
        ), (where, below)

        # ...and the first sample INSIDE the window is searched, so the
        # boundary excludes exactly one sample and not two.
        inside = detect_echo(
            _impulse_with_sample_spikes((first_index, 0.9)),
            SAMPLE_RATE,
            search_us=(search_lo_us, 1000.0),
        )
        assert inside.tau_envelope_us == pytest.approx(
            first_index / SAMPLE_RATE * 1e6, abs=0.5 / SAMPLE_RATE * 1e6
        ), where
        assert inside.earlier_arrival_us < first_index / SAMPLE_RATE * 1e6, where


def test_window_edges_snap_to_a_sample_aligned_request():
    """#1750 — ``WINDOW_EDGE_SNAP_SAMPLES``: a sample-aligned edge written as
    a decimal still means that sample.

    Eight samples at 48 kHz is 166.6666...us, so a caller writes something
    like ``166.6667`` — which is 8.0000016 samples. A bare ``ceil`` reads
    that as 9 and silently costs the caller a whole sample (20.8 us) of
    window to honour a 0.000033 us overshoot; a bare ``floor`` has the
    mirror exposure at the upper edge. The snap is what stops both, and
    without it the sample-aligned control this file uses for
    ``earlier_dominant_arrival`` would not be sample-aligned at all.

    The tolerance is asserted to be small enough to be physically
    meaningless as well as large enough to do the job — a threshold that
    silently grew to a fraction of a sample would pass the first half of
    this test and defeat the window contract.
    """
    exact_lo = 8.0 / SAMPLE_RATE  # 166.6666...us
    exact_hi = 48.0 / SAMPLE_RATE  # 1000 us

    # Written to four decimals, high side, as a caller would: still sample 8.
    assert spatial_combine._ceil_samples(166.6667e-6, SAMPLE_RATE) == 8
    # ...and to four decimals, low side, at the upper edge: still sample 8.
    assert spatial_combine._floor_samples(166.6666e-6, SAMPLE_RATE) == 8
    # Exact values are unambiguous in both directions.
    assert spatial_combine._ceil_samples(exact_lo, SAMPLE_RATE) == 8
    assert spatial_combine._floor_samples(exact_hi, SAMPLE_RATE) == 48

    # A genuinely fractional edge is NOT snapped — the snap must not become a
    # rounding rule. 7.2 samples still ceils to 8, and 7.9 to 8, but 7.999
    # does too rather than being pulled down to 7.
    assert spatial_combine._ceil_samples(150e-6, SAMPLE_RATE) == 8
    assert spatial_combine._ceil_samples(7.9 / SAMPLE_RATE, SAMPLE_RATE) == 8
    assert spatial_combine._floor_samples(7.9 / SAMPLE_RATE, SAMPLE_RATE) == 7

    # The tolerance is a thousandth of a sample, so it cannot swallow a
    # window edge anyone can express: a hundredth of a sample away from an
    # integer is still honoured as fractional.
    assert spatial_combine.WINDOW_EDGE_SNAP_SAMPLES == 1e-3
    assert spatial_combine._ceil_samples((8.0 + 0.01) / SAMPLE_RATE, SAMPLE_RATE) == 9
    assert spatial_combine._floor_samples((8.0 - 0.01) / SAMPLE_RATE, SAMPLE_RATE) == 7


def test_the_envelope_upper_bound_is_the_last_in_window_sample():
    """#1750 — the upper edge got the symmetric treatment, for the symmetric
    reason.

    ``last`` was ``round(search_hi * rate)`` too, so a window whose upper
    edge fell in the second half of a sample searched *above* it. The
    contract is the same at both ends — ``search_us`` is a closed range of
    delays — so the bound is the last sample at or below ``search_hi``.

    (150, 850) us at 48 kHz is the ladder's clearest case: 40.8 samples, so
    ``round`` searched sample 41 at 854.2 us, 4.2 us past the window the
    caller asked for.
    """
    # A lone reflection at sample 41 — outside a window that ends at 850 us.
    ir = _impulse_with_sample_spikes((41, 0.5))
    assert 850.0 * 1e-6 * SAMPLE_RATE == pytest.approx(40.8)

    found = detect_echo(ir, SAMPLE_RATE, search_us=(150.0, 850.0))
    assert spatial_combine._floor_samples(850e-6, SAMPLE_RATE) == 40

    # The out-of-window reflection is not what the envelope answers with.
    # The ceiling is sample 40 plus the parabola's clamp, not "below sample
    # 41": with the old ``round`` bound the envelope selected sample 41 and
    # refinement nudged it to 854.15 us, which a bare "< 854.17" accepts.
    assert found.tau_envelope_us <= 40.5 / SAMPLE_RATE * 1e6 + 1e-9, found

    # ...and it is found when the window is widened by one sample, which is
    # what makes this a boundary test rather than a "does the detector see
    # anything" test.
    widened = detect_echo(
        ir, SAMPLE_RATE, search_us=(150.0, 41.0 / SAMPLE_RATE * 1e6)
    )
    assert widened.tau_envelope_us == pytest.approx(
        41.0 / SAMPLE_RATE * 1e6, abs=0.5
    ), widened


_BELOW_WINDOW_CLOUD_US = (150.0, 400.0)

# The measured verdict for a 10-position cloud whose true delays span
# _BELOW_WINDOW_CLOUD_US, searched in windows that all sit at or above it.
# Rows are (search window, locked, reason, n_confident, rahmonic refusals).
#
# The first three rows are the edge rule working: everything is refused or
# too thin to cluster. The last three are the regime the edge rule could not
# reach — a cepstral rahmonic of the excluded echo, landing mid-window. Until
# the rahmonic screen landed those three rows read ``geometry_locked`` at
# median tau 814.4 / 856.5 / 896.8 us (n_confident 3 / 2 / 2), and were
# pinned that way **as a bug, not a contract**, so the boundary was a
# measured fact instead of an assumption. The screen inverted them: no cloud
# member survives, so there is no cluster to lock. (800, 1200) was documented
# on ``DEFAULT_ECHO_SEARCH_US`` but never swept here; it is a row now, so all
# three false-lock windows are held by the same table that used to record
# their failure.
#
# The last column is the count of ``rahmonic_of_lower_delay`` refusals. It is
# there because "no usable estimates" is an outcome several different bugs
# could also produce; pinning *which* rule declined keeps each row an
# assertion about the screen rather than about emptiness.
#
# Two of those counts moved under #1750 (2 -> 0 at (300, 800), 5 -> 4 at
# (650, 1000)) and the verdict column did not move at all. Unifying the
# envelope's index bounds on ``ceil`` stopped those envelopes railing on an
# out-of-window sample; the estimate then lands inside the window but within
# the edge margin, and ``tau_at_window_lower_edge`` — which returns *before*
# the rahmonic screen — names it instead. The records were refused before and
# are refused now; only the more specific of two correct reasons changed.
#
# Which rows the boundary *can* reach is a property of their edges in
# samples at 48 kHz, and it is not the same set as the rows that moved:
# (300, 800) 14.4, (400, 900) 19.2, (650, 1000) 31.2 and (800, 1200)
# 38.4/57.6 all have at least one edge where ``round`` and ``ceil``/``floor``
# disagree, while (600, 1000) 28.8 and (700, 1000) 33.6 have neither and are
# bit-identical by construction. So two of the four reachable rows moved and
# two did not — (400, 900) has no rahmonic refusals to lose and (800, 1200)
# keeps all four. Reachable is the necessary condition, not the outcome.
_RAISED_WINDOW_SWEEP = [
    ((300.0, 800.0), False, GEOMETRY_UNKNOWN, 1, 0),
    ((400.0, 900.0), False, GEOMETRY_UNKNOWN, 0, 0),
    ((600.0, 1000.0), False, GEOMETRY_UNKNOWN, 0, 2),
    ((650.0, 1000.0), False, GEOMETRY_UNKNOWN, 0, 4),
    ((700.0, 1000.0), False, GEOMETRY_UNKNOWN, 0, 3),
    ((800.0, 1200.0), False, GEOMETRY_UNKNOWN, 0, 4),
]


@pytest.mark.parametrize(
    "search_us, expect_locked, expect_reason, expect_n_confident, expect_rahmonic",
    _RAISED_WINDOW_SWEEP,
)
def test_below_window_cloud_verdict_by_raised_search_window(
    search_us, expect_locked, expect_reason, expect_n_confident, expect_rahmonic
):
    """SF1 / round-3 blocker — the whole raised-window regime, measured.

    One cloud (ten positions, true delays stratified 150-400 us) swept
    across six windows that all sit at or above it. Round 2 ran this with
    the first three rows only and concluded the below-window class was
    closed; the grid simply stopped one increment short of the regime where
    it re-opened.

    Rows 1-3 are the edge rule doing its job. Rows 4-6 are the rahmonic
    regime it could not reach, and for one revision they were asserted **as
    the wrong behaviour they were** — the same dispersed cloud reading
    ``geometry_locked`` at a median tau of ~814 / ~857 / ~897 us, roughly 3x
    delays that never exceeded 400 us. Asserting the bug is what made the
    boundary a fact rather than an assumption, and it is what failed loudly
    when the rahmonic screen landed. All six rows now agree on the honest
    verdict: nothing in this cloud is measurable from a window above it.

    Rows 1 and 3 also carry rahmonic refusals, at windows that never
    false-locked. Nothing regressed there — the screen gives a more specific
    reason than "ran and found nothing credible" to records that were
    already not counting toward the verdict. Row 1's ``n_confident`` of 1 is
    unchanged: that position is the cloud's 400 us member, honestly measured
    inside a window it genuinely falls in.
    """
    taus_s = np.linspace(
        _BELOW_WINDOW_CLOUD_US[0] * 1e-6, _BELOW_WINDOW_CLOUD_US[1] * 1e-6, 10
    )
    _freqs, _true_db, captures = _cloud(taus_s)
    result = combine_positions(captures, echo_search_us=search_us)

    assert result.geometry.n_positions == 10
    assert result.geometry.locked is expect_locked, (search_us, result.geometry)
    assert result.geometry.reason == expect_reason, (search_us, result.geometry)
    assert result.geometry.n_confident == expect_n_confident, (
        search_us,
        result.geometry,
    )
    # No row may reach a cluster: GEOMETRY_UNKNOWN is only honest while the
    # usable set is too small to cluster at all.
    assert result.geometry.n_confident < GEOMETRY_MIN_CONFIDENT, (
        search_us,
        result.geometry,
    )

    rahmonic = [
        e
        for e in result.per_position_echo
        if e is not None and e.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY
    ]
    assert len(rahmonic) == expect_rahmonic, (
        search_us,
        [e.refusal for e in result.per_position_echo if e is not None],
    )
    # Each one is evidenced by the record itself — the screen's verdict is
    # recomputable from the two fields the refusal carries, rather than
    # being a slug the reader has to take on faith.
    for echo in rahmonic:
        assert echo.lower_peak_ratio > RAHMONIC_MARGIN, echo
        assert 0.0 < echo.lower_peak_us < echo.tau_cepstral_us, echo


def test_a_below_window_cloud_is_edge_refused_rather_than_clustered():
    """SF1 — the reviewer's reproduction at (300, 800), pinned in full.

    Round 1 fixed the *clamp*: an out-of-window candidate is rejected rather
    than pulled to the edge. That closed the mechanism at the top of the
    window and left the one at the bottom wide open, because a below-window
    echo is not rejected — it is *aliased*, arriving as a plausible
    in-window estimate a few microseconds above ``search_us[0]``.

    Ten positions, true delays stratified 150-400 us, searched in (300, 800).
    Before the edge rule this reported ``geometry_locked``: six positions
    were admitted, clustered at 0.83, with the two worst offenders — 150 us
    and 178 us echoes — reported as 318 us and 302 us at confidence 1.00 and
    0.68. A household would have been told to spread a mic cloud that was
    already spread, on evidence that was entirely manufactured by the
    window it was searched in.

    The floor-based guard could not catch this: ``150 us`` misreported as
    ``318 us`` clears a resolution floor of 214 us comfortably. Only the
    distance to the *edge* distinguishes the two cases.

    Scope note: this window is inside the regime where the edge rule holds.
    :func:`test_below_window_cloud_verdict_by_raised_search_window` maps
    where it stops.
    """
    taus_s = np.linspace(150e-6, 400e-6, 10)
    # At (300, 800) exactly one position survives —
    # the 400 us member, the only one whose *both* estimates cleared the
    # margin (the 372 us member's true delay is 1.01 steps clear, but its
    # cepstral peak reads 369 us, 0.97 steps, so it is refused too) — and
    # one estimate is not a cluster.
    _freqs, _true_db, captures = _cloud(taus_s)
    result = combine_positions(captures, echo_search_us=(300.0, 800.0))
    assert result.geometry.n_confident == 1
    assert result.geometry.clustered_fraction == 0.0

    edge_refused = [
        e
        for e in result.per_position_echo
        if e is not None and e.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE
    ]
    assert len(edge_refused) >= 6, [e.refusal for e in result.per_position_echo]
    # Every one of those refusals is a rescued false positive: an in-window
    # raw estimate that the old code would have reported and clustered.
    for echo in edge_refused:
        assert 300.0 <= echo.tau_envelope_us <= 800.0 or (
            300.0 <= echo.tau_cepstral_us <= 800.0
        ), echo

    # The same cloud in the window it actually belongs in reads correctly —
    # so the refusals above are the window being wrong, not the cloud.
    _freqs, _true_db, captures = _cloud(taus_s)
    honest = combine_positions(captures)
    assert honest.geometry.locked is False
    assert honest.geometry.reason == GEOMETRY_DISPERSED
    assert honest.geometry.n_confident >= 7


def test_rahmonic_false_lock_under_a_raised_window_is_screened():
    """The inverted tripwire. This test used to assert the bug on purpose.

    Its previous revision was named ``..._is_a_known_limitation`` and
    asserted the detector's *wrong* behaviour, with instructions to invert
    rather than relax it the day a rahmonic screen landed. The screen
    landed; these are the inverted assertions, and the history is kept
    because a test that once asserted the opposite is the strongest
    available evidence that the fix is real rather than definitional.

    **The defect it pinned.** A comb's cepstrum repeats at 2*tau, 3*tau,
    ..., so a search window that excludes the true delay can still contain
    a *rahmonic* of it. Unlike aliasing, a rahmonic lands anywhere in the
    window rather than at its bottom edge, so ``WINDOW_EDGE_MARGIN_STEPS``
    cannot catch it — it only did so incidentally, and only while the lower
    edge stayed under ~650 us. The same 150-400 us cloud that
    :func:`test_a_below_window_cloud_is_edge_refused_rather_than_clustered`
    correctly refuses at (300, 800), searched at (700, 1000), used to
    surface two positions with cepstral peaks at ~3x their true delays: the
    envelope agreed with each, corroboration was tight, and the verdict was
    a confident ``geometry_locked`` at a median tau of ~857 us — a delay
    more than double anything present in the cloud.

    **What is asserted now.** The verdict is not-locked with no usable
    estimates, *and* the mechanism is still visible in the record: those
    same two positions are refused as rahmonics, their would-have-been
    cepstral candidates still sit at ~3x their true delays, and the
    stronger peak that refused each one sits within half a quefrency step
    of the true delay. Keeping the mechanism assertions is deliberate — a
    verdict-only test would also pass if the detector had simply stopped
    working, which is a different thing from the screen working.

    Round 2 shipped the claim that this class was closed. It was closed
    only up to ``search_us[0] <= 600``; the sweep that established it
    stopped one increment short. That is why the screen is calibrated
    against a swept gap (``RAHMONIC_MARGIN``) rather than a single window.
    """
    true_taus_us = np.linspace(150.0, 400.0, 10)
    _freqs, _true_db, captures = _cloud(true_taus_us * 1e-6)
    result = combine_positions(captures, echo_search_us=(700.0, 1000.0))

    # --- The honest verdict. (This block is the inversion.) ---
    assert result.geometry.locked is False, result.geometry
    assert result.geometry.reason == GEOMETRY_UNKNOWN
    assert result.geometry.n_confident == 0
    assert result.geometry.median_tau_us == 0.0
    assert not any(
        echo is not None and echo.refusal == "" and echo.confidence > 0.0
        for echo in result.per_position_echo
    ), [e.refusal for e in result.per_position_echo if e is not None]

    # --- The mechanism, so a future regression is diagnosable, not just red. ---
    rahmonic = [
        (true_us, echo)
        for true_us, echo in zip(true_taus_us, result.per_position_echo, strict=True)
        if echo is not None and echo.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY
    ]
    # The two positions that used to be admitted are the two whose cepstral
    # candidate is the *third* rahmonic; a third position is refused here on
    # its second rahmonic, and was never admitted.
    third = [
        (true_us, echo)
        for true_us, echo in rahmonic
        if echo.tau_cepstral_us / true_us == pytest.approx(3.0, abs=0.05)
    ]
    assert len(third) == 2, [(t, e.tau_cepstral_us / t) for t, e in rahmonic]
    for true_us, echo in third:
        # The envelope still corroborates the rahmonic — that agreement is
        # what used to make it credible, and it is untouched. The screen
        # does not work by breaking corroboration; it works by noticing the
        # fundamental.
        assert echo.corroboration < CORROBORATION_LOOSE, echo
        # The peak that refused it is the echo the window excluded, not an
        # arbitrary lower bin: it lands within half a quefrency step of the
        # position's own true delay.
        assert abs(echo.lower_peak_us - true_us) < 0.5 * echo.resolution_us, (
            true_us,
            echo,
        )
        # And it wins by a margin the screen never had to strain for.
        assert echo.lower_peak_ratio > 20.0, echo
        assert echo.tau_us == 0.0
        assert echo.confidence == 0.0


def test_rahmonic_screen_refuses_a_below_window_echo_and_names_its_fundamental():
    """The screen in isolation, on one IR — both directions of the rule.

    The cloud tests show the screen changing a *verdict*; this one shows the
    rule itself, on a single clean impulse+echo pair, which is where a
    regression is diagnosable.

    * **Fires.** A 300 us echo searched in (700, 1000) is exactly the
      pathology: the true delay is below the window, its third rahmonic is
      not, and before the screen the detector reported **876.1 us at
      confidence 1.000** — a 2.9x-wrong answer at the top of the scale, from
      an IR with no noise problem at all. It is now refused, and the refusal
      names the fundamental: the peak that beat the candidate sits at 285.6
      us, 0.20 quefrency steps from the true 300 us.
    * **Does not fire.** The same echo in the default window is measured
      normally, and a *lone* 850 us echo inside a raised (750, 1100) window
      is measured normally too. The second case shows the screen keys on "is
      something below stronger", not on "is the window high".

    **Read the second case no wider than that.** It says a raised window
    whose excluded region is *quiet* still measures, not that a caller with
    a genuinely late bounce is never locked out. Put a stronger 300 us
    reflection into that same IR and the screen does refuse the honest 850
    us echo, because "something much stronger sits below" is necessary for a
    candidate to be a rahmonic but not sufficient — an unrelated earlier
    reflection presents the identical evidence. That limitation is measured
    and pinned by
    :func:`test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one`.
    """
    fired = detect_echo(
        _impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE, search_us=(700.0, 1000.0)
    )
    assert fired.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, fired
    assert fired.tau_us == 0.0
    assert fired.confidence == 0.0
    assert fired.strength_db == STRENGTH_FLOOR_DB

    # The candidate really was the third rahmonic, and really was credible:
    # the two estimators agreed, which is what made the old answer confident.
    assert fired.tau_cepstral_us / 300.0 == pytest.approx(3.0, abs=0.15), fired
    assert fired.corroboration < CORROBORATION_LOOSE, fired
    # The evidence the refusal carries: where the fundamental is, and by how
    # much it wins. Both are on the record, so the verdict is recomputable.
    assert abs(fired.lower_peak_us - 300.0) < 0.5 * fired.resolution_us, fired
    assert fired.lower_peak_ratio > RAHMONIC_MARGIN, fired
    assert fired.lower_peak_ratio > 20.0, fired

    # Same echo, honest window: measured, and nowhere near the screen.
    honest = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE)
    assert honest.refusal == "", honest
    assert honest.tau_us == pytest.approx(300.0, rel=0.05)
    assert honest.lower_peak_ratio < RAHMONIC_MARGIN, honest
    # ...and the screen was *awake* while not firing. A default-window
    # candidate sits only a few quefrency bins up, so if
    # ``RAHMONIC_FLOOR_STEPS`` were raised there would be no analyzable
    # region below it and the rule would silently degrade to "no region, no
    # opinion" for the most common window this detector is used with.
    assert honest.lower_peak_us > 0.0, honest
    assert honest.lower_peak_ratio > 0.0, honest

    # A genuinely late echo, measured *inside* a raised window. The screen
    # must not turn "the window is high" into "refuse".
    late = detect_echo(
        _impulse_with_echo(850e-6, ECHO_R), SAMPLE_RATE, search_us=(750.0, 1100.0)
    )
    assert late.refusal == "", late
    assert late.confidence > ECHO_CONFIDENCE_FLOOR, late
    assert late.tau_us == pytest.approx(850.0, rel=0.05)
    assert late.lower_peak_ratio < RAHMONIC_MARGIN, late


def test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one():
    """KNOWN LIMITATION — documented here, not endorsed.

    **The case.** An IR with two independent bounces: a strong 300 us
    reflection (r=0.5) and a genuine 850 us one (r=0.2). Searched in a
    raised (750, 1100) window, the detector locates the *real* 850 us echo
    with both estimators — 855.5 us cepstral, 854.1 us envelope, agreeing to
    0.16% — and then refuses it, because the 300 us reflection it excluded
    from the window puts a cepstral peak 2.45x stronger below the candidate.
    There is nothing wrong with that measurement; the screen simply cannot
    tell this apart from the false-lock case it exists to catch.

    **Why it is not fixable from one record.** "A much stronger peak sits
    below" is *necessary* for a candidate to be a rahmonic and not
    sufficient, and the two populations' ratios interleave rather than
    occupying separate bands: this honest case is refused at 2.448, while a
    true rahmonic — a lone 400 us echo seen through (650, 1000), whose
    cepstral candidate is its second rahmonic at 1.98x the truth — is
    refused at 2.337, *below* it. Both are asserted below, so the
    interleaving is executable rather than claimed (both figures survived
    #1750 unchanged — ``lower_peak_ratio`` is measured off the cepstrum,
    which the envelope's window bounds cannot reach). No threshold on
    ``lower_peak_ratio`` separates them, and the other single-record
    evidence does not either: both classes can show two estimators agreeing
    tightly on the in-window candidate. The plausible discriminator is
    multi-position context — a rahmonic is locked to its fundamental, so the
    pair moves together as the mic moves, while two independent bounces do
    not — but that is a hypothesis about a future capability, not something
    this module measures today.

    **Why the shipped behaviour is still right.** The failure direction is a
    refusal, never a wrong number, which is the module's standing trade, and
    it is reachable only from a raised window with something stronger below
    it. The shape of that boundary is not this test's job: it belongs to
    :func:`test_raised_window_two_echo_hazard_is_bounded_by_the_default_window`,
    which sweeps the same geometry family from committed grid literals (605
    of 720 raised-window cases refuse; the same geometries refuse 0 of 432 at
    the default window and single-echo raised-window cases 0 of 370). The
    remedy is in the last assertion here: the default window contains the
    earlier reflection instead of excluding it, so the same IR measures
    cleanly there. See ``DEFAULT_ECHO_SEARCH_US``.

    This test documents the limitation so a future reader meets it as a
    known cost rather than a surprise; if the screen is ever taught
    multi-position context, this test should be *inverted* (like
    :func:`test_rahmonic_false_lock_under_a_raised_window_is_screened` was),
    not deleted.
    """
    ir = _impulse_with_two_echoes(300e-6, 0.5, 850e-6, 0.2)
    refused = detect_echo(ir, SAMPLE_RATE, search_us=(750.0, 1100.0))

    assert refused.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, refused
    assert refused.tau_us == 0.0
    assert refused.confidence == 0.0
    assert refused.strength_db == STRENGTH_FLOOR_DB

    # The refused measurement was honest: both estimators found the real
    # 850 us echo and agreed on it. That is what makes this a cost rather
    # than the screen catching a bad reading.
    assert refused.tau_cepstral_us == pytest.approx(850.0, rel=0.02), refused
    assert refused.tau_envelope_us == pytest.approx(850.0, rel=0.02), refused
    assert refused.corroboration < 0.01, refused

    # And the peak that refused it is the *earlier real reflection*, not a
    # rahmonic of anything: it sits within half a quefrency step of 300 us.
    assert abs(refused.lower_peak_us - 300.0) < 0.5 * refused.resolution_us, refused
    assert refused.lower_peak_ratio > RAHMONIC_MARGIN, refused

    # The interleaving that makes this unfixable per-record: a genuine
    # rahmonic lands *below* this honest one on the very statistic the screen
    # uses, so no threshold on it could separate the two.
    #
    # Since #1750 this control is *reached* by the edge rule rather than by
    # the screen — (650, 1000) us has a 31.2-sample lower edge, so unifying
    # the envelope bounds on ``ceil`` stops its envelope railing below the
    # window, and the in-window estimate it finds instead hugs the edge. That
    # changes nothing about the ambiguity: ``lower_peak_ratio`` is a cepstral
    # statistic, both readings below are bit-identical to the pre-#1750 ones,
    # and a rule that happens to fire first at one window is not a way to tell
    # an honest late echo from a rahmonic.
    genuine = detect_echo(
        _impulse_with_echo(400e-6, 0.75), SAMPLE_RATE, search_us=(650.0, 1000.0)
    )
    assert genuine.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, genuine
    assert genuine.tau_cepstral_us / 400.0 == pytest.approx(2.0, abs=0.05), genuine
    assert genuine.lower_peak_ratio > RAHMONIC_MARGIN, genuine
    assert genuine.lower_peak_ratio < refused.lower_peak_ratio, (
        "the honest-late-echo refusal and a true rahmonic refusal no longer "
        "interleave on lower_peak_ratio; if they have separated, the "
        "'not separable from one record' claim in this docstring and in "
        "RAHMONIC_MARGIN needs re-deriving, not relaxing",
        genuine.lower_peak_ratio,
        refused.lower_peak_ratio,
    )

    # The remedy, on the same IR: the default window contains the earlier
    # reflection rather than excluding it, so there is no stronger peak
    # below the candidate and the detector measures normally.
    measured = detect_echo(ir, SAMPLE_RATE)
    assert measured.refusal == "", measured
    assert measured.confidence > ECHO_CONFIDENCE_FLOOR, measured
    assert measured.tau_us == pytest.approx(300.0, rel=0.05), measured
    assert measured.lower_peak_ratio < RAHMONIC_MARGIN, measured


def test_rahmonic_screen_catches_the_non_integer_ratio_the_submultiple_test_missed():
    """Why the rule is "anything stronger below" and not "tau/2 or tau/3".

    An earlier revision of ``DEFAULT_ECHO_SEARCH_US`` prescribed re-testing a
    survivor against exact submultiples. The module's own worst measured case
    is the counterexample, and it is pinned here so the design rationale is
    executable rather than a claim in a comment.

    That case is the 205.6 us member of the 150-400 us cloud, searched in
    (650, 1000). Its cepstral candidate lands at 749.8 us — **3.648x** the
    true delay, not 3x — and the detector used to report 814.4 us for it,
    3.96x the truth and the median that this file used to pin as a false
    lock. A submultiple test would have had to swallow that non-integer
    slop: dividing the reported 814.4 us by 3 gives 271.5 us against a true
    205.6 us, 32% high and 0.92 quefrency steps away, so the test could only
    have caught it by accepting a whole step of error — at which point it
    accepts nearly anything within its own resolution. The screen instead
    locates the fundamental directly, at 214.2 us: 4.2% high, **0.12
    quefrency steps**, nearly eight times closer.
    """
    true_taus_us = np.linspace(150.0, 400.0, 10)
    _freqs, _true_db, captures = _cloud(true_taus_us * 1e-6)
    result = combine_positions(captures, echo_search_us=(650.0, 1000.0))

    true_us = float(true_taus_us[2])
    assert true_us == pytest.approx(205.6, abs=0.1), "the cloud member moved"
    outlier = result.per_position_echo[2]
    assert outlier is not None
    assert outlier.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, outlier

    # The ratio is genuinely off-integer — that is the whole point.
    ratio = outlier.tau_cepstral_us / true_us
    assert ratio == pytest.approx(3.648, abs=0.02), outlier
    assert min(abs(ratio - 3.0), abs(ratio - 4.0)) > 0.3, (
        f"ratio {ratio:.3f} is close enough to an integer that a submultiple "
        "test would have worked, and this case no longer demonstrates why the "
        "screen is shaped the way it is"
    )

    # The screen localises the fundamental far better than either submultiple
    # of the cepstral candidate would have.
    screen_err = abs(outlier.lower_peak_us - true_us)
    assert screen_err < 0.2 * outlier.resolution_us, outlier
    for divisor in (2.0, 3.0):
        assert abs(outlier.tau_cepstral_us / divisor - true_us) > 4.0 * screen_err, (
            divisor,
            outlier,
        )
    # And it is the module's widest measured rejection margin.
    assert outlier.lower_peak_ratio > 70.0, outlier


def test_rahmonic_margin_is_load_bearing_in_both_directions():
    """``RAHMONIC_MARGIN`` sits in a gap, and both walls of it are real.

    A threshold nobody can move is a threshold nobody has checked. This
    mutates the constant and asserts that each direction breaks something
    specific:

    * **Too low (0.0)** — the screen swallows honest measurements. A clean
      300 us echo in the default window, the most ordinary reading this
      detector takes, is refused as a rahmonic of its own cepstral shoulder.
      That is the wall the calibration's *upper* headroom protects: across
      the sweep behind ``RAHMONIC_MARGIN``, true positives reach a
      lower/candidate ratio of 0.9955, so anything at or below 1.0 is
      shaving a real population.
    * **Too high (1e9)** — the screen stops screening, and the 150-400 us
      cloud goes back to reading ``geometry_locked`` at ~857 us through a
      (700, 1000) window. That is the *lower* wall: wrong readings bottom
      out at 2.7899, so a margin above that starts letting them through.

    The shipped value does neither, and the same two cases show it sitting
    between the populations rather than merely between the failures. Both
    population figures come from
    :func:`test_rahmonic_margin_calibration_populations_bracket_the_constant`,
    which regenerates them on demand (``JTS_RAHMONIC_CALIBRATION=1``) — this
    test asserts the constant is load-bearing, that one asserts it is in the
    right place.
    """
    clean_ir = _impulse_with_echo(300e-6, ECHO_R)
    taus_s = np.linspace(150e-6, 400e-6, 10)

    def raised_window_verdict():
        _freqs, _true_db, captures = _cloud(taus_s)
        return combine_positions(captures, echo_search_us=(700.0, 1000.0)).geometry

    # --- The shipped value: honest read measured, false lock refused. ---
    honest = detect_echo(clean_ir, SAMPLE_RATE)
    assert honest.refusal == ""
    assert honest.lower_peak_ratio < RAHMONIC_MARGIN, honest
    assert raised_window_verdict().locked is False

    # --- Mutated too low: the honest read is eaten. ---
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(spatial_combine, "RAHMONIC_MARGIN", 0.0)
        eaten = detect_echo(clean_ir, SAMPLE_RATE)
    assert eaten.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, (
        "a margin of 0 must refuse an ordinary in-window echo — if it no "
        "longer does, the screen has stopped being sensitive to the "
        "constant and this bracket is vacuous"
    )
    assert eaten.tau_us == 0.0

    # --- Mutated too high: the false lock comes back. ---
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(spatial_combine, "RAHMONIC_MARGIN", 1e9)
        unscreened = raised_window_verdict()
    assert unscreened.locked is True, (
        "with the screen effectively disabled the raised-window false lock "
        "must reproduce — if it does not, this test is no longer proving "
        "that the screen is what closed it"
    )
    assert unscreened.reason == GEOMETRY_LOCKED
    assert unscreened.median_tau_us == pytest.approx(856.5, rel=0.02)
    assert unscreened.median_tau_us > 2.0 * 400.0


# The two grids ``RAHMONIC_MARGIN``'s calibration comment describes, as
# literals, so the comment and the executable sweep cannot drift apart. The
# true-positive grid is windows that *contain* the echo; the wrong-reading
# grid is windows that exclude it, which is what makes a rahmonic reachable.
_CALIBRATION_REFLECTIONS = (
    0.10, 0.15, 0.20, 0.25, 0.30, 0.36, 0.42, 0.50, 0.60, 0.68, 0.75,
)
_CALIBRATION_TRUE_POSITIVE_TAUS_US = np.arange(200.0, 771.0, 30.0)
_CALIBRATION_TRUE_POSITIVE_WINDOWS = (
    (120.0, 800.0), (150.0, 850.0), (200.0, 900.0), (250.0, 950.0),
    (300.0, 800.0), (300.0, 1000.0), (400.0, 900.0), (400.0, 1000.0),
    (500.0, 1000.0), (600.0, 1000.0), (650.0, 1000.0), (700.0, 1100.0),
    (800.0, 1200.0),
)
_CALIBRATION_WRONG_READING_TAUS_US = np.arange(100.0, 456.0, 25.0)
_CALIBRATION_WRONG_READING_WINDOWS = (
    (400.0, 900.0), (500.0, 1000.0), (600.0, 1100.0), (650.0, 1000.0),
    (700.0, 1100.0), (750.0, 1200.0), (800.0, 1200.0), (850.0, 1400.0),
    (900.0, 1400.0), (950.0, 1500.0), (1000.0, 1600.0),
)

# Guards both reproducible sweeps below — the RAHMONIC_MARGIN calibration
# (~9 400 detector calls) and the raised-window two-echo hazard (~1 500).
requires_calibration_sweep = pytest.mark.skipif(
    os.environ.get("JTS_RAHMONIC_CALIBRATION", "").strip() != "1",
    reason=(
        "the reproducible rahmonic sweeps are ~11 000 detector calls (~38 s "
        "for both), too slow for the default lane: set "
        "JTS_RAHMONIC_CALIBRATION=1 to run them"
    ),
)


@requires_calibration_sweep
def test_rahmonic_margin_calibration_populations_bracket_the_constant():
    """The sweep behind ``RAHMONIC_MARGIN``, committed rather than quoted.

    :func:`test_rahmonic_margin_is_load_bearing_in_both_directions` shows the
    constant matters by mutating it; this shows *why it is 2.0*, by
    regenerating the two populations the calibration comment describes and
    re-deriving the gap between them. Before this test existed the comment
    cited 2925 true-positive readings with a ceiling of 0.9955 and 528 wrong
    readings with a floor of 3.531 — figures nobody could re-derive, because
    the grid steps behind them were never written down. A calibration nobody
    can reproduce is a claim, not a measurement.

    **Method.** Each IR is measured by the *shipped* detector with the screen
    disabled (``RAHMONIC_MARGIN`` patched to infinity), so this sweep reads
    ``lower_peak_ratio`` off real records rather than re-implementing the
    statistic, and classifies each by what the pre-screen detector did:

    * **True positives** — unrefused, at or above ``ECHO_CONFIDENCE_FLOOR``,
      within 15% of truth. These are readings the screen must never take
      away; their ratio *ceiling* is the wall below the margin.
    * **Wrong readings** — unrefused and confident, but more than 15% off
      truth. These are the false locks the screen exists to reject; their
      ratio *floor* is the wall above the margin.

    **Re-measured 2026-08-02** (24-28 s of sweep), on the unified window
    boundary (#1750): 2908 true positives, ceiling **0.9955** (0.995547);
    439 wrong readings, floor **2.7899** (2.789915). So 1.65 sits 1.66x above
    the ceiling and 1.69x below the floor. The four figures are printed on
    stdout under ``-s`` so a passing run re-derives the comment rather than
    only bracketing it.

    **What #1750 moved.** The true-positive population is bit-identical
    (2908 / 0.995547); the wrong-reading population grew 409 -> 439 and its
    floor fell 4.9192 -> 2.7899, which is the collision ``RAHMONIC_MARGIN``'s
    comment and the ``earlier_dominant_arrival`` rejected-alternative both
    predicted for exactly this change, to four decimal places. The constant
    moved 2.0 -> 1.65 to stay inside the narrower gap with the same 1.5x
    bracket, and the *verdicts* did not move with it: 439/439 wrong readings
    still refused, 0/2908 right ones. The three corpus IRs the original sweep
    folded into its true positives are deliberately not here: this test must
    run in CI, where the corpus is absent. Their headroom is pinned
    separately by :func:`test_detect_echo_finds_the_corpus_bounce`.

    The assertions are the bracket and its width, not the four figures. A
    grid this large is a *sample* of the two populations — pinning its exact
    output would make an honest re-sample look like a regression — but a
    bracket that closes, or a population that silently empties, is a real
    one.
    """
    freqs = _grid()
    true_db = _true_response_db(freqs)

    def irs_for(tau_us: float, reflection: float) -> list[np.ndarray]:
        """One bare impulse+echo IR and one shaped-response IR, same echo."""
        shaped = _position(
            "calibration",
            freqs,
            true_db,
            tau_us * 1e-6,
            np.random.default_rng(int(tau_us * 100) + int(reflection * 1000)),
            reflection=reflection,
        ).ir
        assert shaped is not None
        return [_impulse_with_echo(tau_us * 1e-6, reflection), shaped]

    def sweep(taus_us, windows, *, wrong: bool) -> list[float]:
        ratios: list[float] = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(spatial_combine, "RAHMONIC_MARGIN", math.inf)
            for raw_tau_us in taus_us:
                tau_us = float(raw_tau_us)
                for reflection in _CALIBRATION_REFLECTIONS:
                    for ir in irs_for(tau_us, reflection):
                        for window in windows:
                            echo = detect_echo(ir, SAMPLE_RATE, search_us=window)
                            if echo.refusal or echo.confidence < ECHO_CONFIDENCE_FLOOR:
                                continue
                            off_by = abs(echo.tau_us - tau_us) / tau_us
                            if (off_by > 0.15) == wrong:
                                ratios.append(echo.lower_peak_ratio)
        return ratios

    true_positive = sweep(
        _CALIBRATION_TRUE_POSITIVE_TAUS_US,
        _CALIBRATION_TRUE_POSITIVE_WINDOWS,
        wrong=False,
    )
    wrong_reading = sweep(
        _CALIBRATION_WRONG_READING_TAUS_US,
        _CALIBRATION_WRONG_READING_WINDOWS,
        wrong=True,
    )

    # Non-vacuity first: a sweep that stopped producing readings would pass
    # every bracket assertion below by having nothing to contradict it.
    assert len(true_positive) > 2000, len(true_positive)
    assert len(wrong_reading) > 250, len(wrong_reading)

    ceiling = max(true_positive)
    floor = min(wrong_reading)
    measured = (
        f"true positives n={len(true_positive)} ceiling={ceiling:.4f}; "
        f"wrong readings n={len(wrong_reading)} floor={floor:.4f}"
    )
    # The four figures ``RAHMONIC_MARGIN``'s comment quotes, on stdout as
    # well as in the assertion messages below: the assertions are brackets,
    # so a *passing* run would otherwise hand back no way to re-derive the
    # prose. Captured by pytest unless ``-s``, so this costs nothing in a
    # normal run and makes re-deriving the comment one command.
    print(
        f"RAHMONIC_MARGIN calibration: true positives n={len(true_positive)} "
        f"ceiling={ceiling:.6f}; wrong readings n={len(wrong_reading)} "
        f"floor={floor:.6f}"
    )

    # The gap exists, and the constant is inside it...
    assert ceiling < RAHMONIC_MARGIN < floor, measured
    # ...with room on both walls rather than grazing either. 1.5x is the
    # weakest bracket the measured gap comfortably supports (1.66x / 1.69x
    # since #1750, and the gap's own geometric centre is 1.667), so failing
    # this means the populations have genuinely closed in — which is exactly
    # what it did on #1750's first run, at the old 2.0.
    assert ceiling * 1.5 <= RAHMONIC_MARGIN, measured
    assert floor >= 1.5 * RAHMONIC_MARGIN, measured


# The two-echo hazard grid behind ``DEFAULT_ECHO_SEARCH_US``'s raised-window
# cost, as literals. Every figure that comment quotes is an attribute of
# :func:`_two_echo_hazard_sweep`'s return value, so a reader re-derives them by
# running this file rather than by reconstructing a grid from prose — a prose
# description of a grid is not a grid, and reconstructing one from an earlier
# revision of that comment landed three cases away from the real answer.
_HAZARD_EARLY_ECHOES = tuple(
    (tau_us, reflection)
    for tau_us in (250.0, 300.0, 350.0, 400.0)
    for reflection in (0.4, 0.5, 0.6)
)
_HAZARD_LATE_ECHOES = tuple(
    (tau_us, reflection)
    for tau_us in (850.0, 900.0, 950.0, 1000.0)
    for reflection in (0.15, 0.2, 0.25)
)
_HAZARD_RAISED_WINDOWS = (
    (700.0, 1100.0), (700.0, 1200.0), (750.0, 1100.0), (750.0, 1200.0),
    (800.0, 1200.0),
)
# Three noise seeds for the default-window leg: the hazard is a property of
# the geometry, so the leg that must show *nothing* is the one worth
# re-running against different noise.
_HAZARD_DEFAULT_WINDOW_SEEDS = (0, 1, 2)
_HAZARD_SINGLE_ECHO_REFLECTIONS = (0.15, 0.25, 0.36, 0.5, 0.6)


@dataclass(frozen=True)
class _HazardLeg:
    """One leg of the two-echo hazard sweep, in aggregate.

    ``ratio_lo``/``ratio_hi`` span *every* record in the leg;
    ``refused_ratio_lo``/``refused_ratio_hi`` span only the
    ``rahmonic_of_lower_delay`` refusals (0.0 when there are none). The two
    spans are separate because the interesting range differs by leg: for a
    leg that refuses nothing, "how far the whole leg stayed from the margin"
    is the point; for the raised-window leg it is "how hard the refusals
    fired".
    """

    total: int
    rahmonic_refusals: int
    measured_confident: int
    ratio_lo: float
    ratio_hi: float
    refused_ratio_lo: float
    refused_ratio_hi: float


@dataclass(frozen=True)
class _HazardSweep:
    """The three legs, plus the accuracy figures the prose quotes."""

    raised: _HazardLeg
    default_window: _HazardLeg
    single_echo: _HazardLeg
    # Worst |tau_envelope - true late echo| over the raised leg's refusals,
    # as a percent: how good the measurements the screen threw away were.
    raised_refused_envelope_error_pct: float
    # Worst corroboration over those same refusals: they were not refused for
    # disagreeing.
    raised_refused_corroboration_max: float
    # Worst |tau - true early echo| over the default-window leg, as a percent:
    # what the caller gets instead by not raising the window.
    default_window_early_echo_error_pct: float


def _summarise_leg(records: list[tuple[float, EchoDiagnostic]]) -> _HazardLeg:
    ratios = [echo.lower_peak_ratio for _target, echo in records]
    refused = [
        echo.lower_peak_ratio
        for _target, echo in records
        if echo.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY
    ]
    return _HazardLeg(
        total=len(records),
        rahmonic_refusals=len(refused),
        measured_confident=sum(
            1
            for _target, echo in records
            if echo.refusal == "" and echo.confidence >= ECHO_CONFIDENCE_FLOOR
        ),
        ratio_lo=min(ratios),
        ratio_hi=max(ratios),
        refused_ratio_lo=min(refused) if refused else 0.0,
        refused_ratio_hi=max(refused) if refused else 0.0,
    )


def _two_echo_hazard_sweep() -> _HazardSweep:
    """Sweep the raised-window two-echo hazard and its two boundaries.

    Three legs off one grid of geometries — an earlier, stronger reflection
    plus a later, weaker one:

    * **raised** — searched in windows that exclude the earlier reflection and
      contain the later one. This is where an honest late echo is refused as
      if it were a rahmonic.
    * **default_window** — the same geometries through ``(120, 800)``, which
      contains the earlier reflection, over three noise seeds. This is the
      remedy, and it must refuse nothing.
    * **single_echo** — one echo, genuinely inside each raised window, with
      nothing below it. This isolates the raised window itself as *not* the
      cause, and must refuse nothing either.
    """
    raised: list[tuple[float, EchoDiagnostic]] = []
    for window in _HAZARD_RAISED_WINDOWS:
        for early_us, early_r in _HAZARD_EARLY_ECHOES:
            for late_us, late_r in _HAZARD_LATE_ECHOES:
                if not window[0] < late_us < window[1] or late_r >= early_r:
                    continue
                ir = _impulse_with_two_echoes(
                    early_us * 1e-6, early_r, late_us * 1e-6, late_r
                )
                raised.append(
                    (late_us, detect_echo(ir, SAMPLE_RATE, search_us=window))
                )

    default_window: list[tuple[float, EchoDiagnostic]] = []
    for early_us, early_r in _HAZARD_EARLY_ECHOES:
        for late_us, late_r in _HAZARD_LATE_ECHOES:
            if late_r >= early_r:
                continue
            for seed in _HAZARD_DEFAULT_WINDOW_SEEDS:
                ir = _impulse_with_two_echoes(
                    early_us * 1e-6, early_r, late_us * 1e-6, late_r, seed=seed
                )
                default_window.append((early_us, detect_echo(ir, SAMPLE_RATE)))

    single_echo: list[tuple[float, EchoDiagnostic]] = []
    for window in _HAZARD_RAISED_WINDOWS:
        for tau_us in np.arange(window[0] + 90.0, window[1] - 40.0, 20.0):
            for reflection in _HAZARD_SINGLE_ECHO_REFLECTIONS:
                ir = _impulse_with_echo(float(tau_us) * 1e-6, reflection)
                single_echo.append(
                    (float(tau_us), detect_echo(ir, SAMPLE_RATE, search_us=window))
                )

    refusals = [
        (target, echo)
        for target, echo in raised
        if echo.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY
    ]
    return _HazardSweep(
        raised=_summarise_leg(raised),
        default_window=_summarise_leg(default_window),
        single_echo=_summarise_leg(single_echo),
        raised_refused_envelope_error_pct=max(
            100.0 * abs(echo.tau_envelope_us - target) / target
            for target, echo in refusals
        ),
        raised_refused_corroboration_max=max(
            echo.corroboration for _target, echo in refusals
        ),
        default_window_early_echo_error_pct=max(
            100.0 * abs(echo.tau_us - target) / target
            for target, echo in default_window
        ),
    )


@requires_calibration_sweep
def test_raised_window_two_echo_hazard_is_bounded_by_the_default_window():
    """The sweep behind the raised-window cost, committed rather than quoted.

    :func:`test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one`
    pins the hazard on one hand-picked IR; this establishes its *shape* — that
    it needs the stronger-earlier-echo geometry, that the default window does
    not have it, and that the measurements it throws away were good ones.
    ``DEFAULT_ECHO_SEARCH_US`` quotes these figures, and an earlier revision
    quoted them from an uncommitted script: a reader reconstructing that grid
    from its prose description landed on 479 refusals where the script had
    found 482. The grid is literals now
    (``_HAZARD_*``) and every quoted figure is an attribute of
    :func:`_two_echo_hazard_sweep`, so the comment and the code cannot drift.

    **Re-measured 2026-08-02** (~9 s): the raised leg refuses **605 of 720**
    at lower/candidate ratios **1.678-4.513**, with the discarded envelope
    estimates within **0.894%** of the true late echo and corroboration never
    worse than **0.266**; the default-window leg refuses **0 of 432** (ratios
    **0.166-0.945**) and reads the earlier echo to within **2.398%**; the
    single-echo leg refuses **0 of 370** (ratios **0.217-0.942**).

    Only the raised leg's refusal count moved under #1750, and not because
    the detector reads these IRs differently: every ratio in every leg is
    bit-identical (``lower_peak_ratio`` is cepstral, and the boundary that
    changed is the envelope's). What moved is ``RAHMONIC_MARGIN``, 2.0 ->
    1.65, so 123 raised-window records whose ratios always sat between the
    two values are now refused: 482 -> 605, and ``measured_confident``
    179 -> 56. That is the priced cost of the tighter screen, and it lands
    only where the module already says to prefer the default window — both
    boundary legs are untouched, ratio for ratio.

    The assertions are the walls, not those figures: exact grid sizes (so a
    changed literal fails loudly rather than silently re-scoping the prose),
    zero refusals on both boundary legs, refusals present on the raised leg,
    and the discarded envelope estimates staying within 1% of truth — which is
    what makes this a *cost* rather than the screen catching bad readings.
    """
    sweep = _two_echo_hazard_sweep()
    # Same reason as the rahmonic calibration's print: the assertions below
    # are walls, so a passing run would otherwise hand back no way to
    # re-derive the figures ``DEFAULT_ECHO_SEARCH_US`` quotes. Captured by
    # pytest unless ``-s``.
    print(f"two-echo hazard sweep: {sweep}")

    # Grid sizes, so "of 720 / of 432 / of 370" in the prose is executable.
    assert sweep.raised.total == 720, sweep.raised
    assert sweep.default_window.total == 432, sweep.default_window
    assert sweep.single_echo.total == 370, sweep.single_echo

    # Wall 1 — the remedy holds: the window that contains the earlier
    # reflection refuses none of these geometries, and measures all of them.
    assert sweep.default_window.rahmonic_refusals == 0, sweep.default_window
    assert sweep.default_window.measured_confident == 432, sweep.default_window
    assert sweep.default_window.ratio_hi < RAHMONIC_MARGIN, sweep.default_window

    # Wall 2 — a raised window alone is not the cause: with nothing below the
    # candidate, the same windows refuse nothing.
    assert sweep.single_echo.rahmonic_refusals == 0, sweep.single_echo
    assert sweep.single_echo.measured_confident == 370, sweep.single_echo
    assert sweep.single_echo.ratio_hi < RAHMONIC_MARGIN, sweep.single_echo

    # Wall 3 — the hazard is real and reachable, not a one-IR curiosity.
    assert sweep.raised.rahmonic_refusals > 0, sweep.raised
    assert sweep.raised.refused_ratio_lo > RAHMONIC_MARGIN, sweep.raised

    # Wall 4 — what was refused were good measurements. If this ever fails,
    # the refusals have started landing on records that were wrong anyway,
    # and the "known cost" framing in DEFAULT_ECHO_SEARCH_US is too harsh on
    # the screen rather than too kind.
    assert sweep.raised_refused_envelope_error_pct < 1.0, sweep
    assert sweep.raised_refused_corroboration_max < CORROBORATION_LOOSE, sweep


def test_an_edge_refusal_reports_the_corroboration_it_measured():
    """A refused record must not claim a number it did not measure.

    ``corroboration`` is 1.0 as a *marker* meaning "the two estimators could
    not be compared". Most refusal paths earn it: the detector gave up
    before both estimates existed, or one landed outside the window. The
    ``tau_at_window_lower_edge`` refusal does not — it fires only after both
    candidates were found in-window and compared, so a real reading exists.

    Reporting 1.0 there overwrote that reading with a fabricated one. This
    test uses a case where the pair agreed to within ~8%, which the old code
    recorded as maximal disagreement. **That agreement is not what makes it
    a refusal**, and this test must not be read as claiming edge refusals
    are always tight: the measured readings this path produces run from
    near-perfect agreement to gross disagreement, and readings above
    ``CORROBORATION_LOOSE`` are ordinary rather than exceptional. The
    refusal turns on distance to the window edge; the field's only job is to
    report what was measured. (No count is quoted for that population on
    purpose — it is a property of whichever tests happen to exist, so a
    census here would be stale the next time one is added.)
    """
    # A clean synthetic echo well below the default window's lower edge:
    # both estimators alias up onto the edge, agree closely, and are refused.
    refused = detect_echo(_impulse_with_echo(60e-6, ECHO_R), SAMPLE_RATE)
    assert refused.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, refused
    assert refused.corroboration < 0.1, refused

    # Not merely "small" — it is exactly the value recomputable from the raw
    # fields the same record carries. That is what makes it a measurement
    # rather than a second magic constant.
    both_in_window = [
        tau
        for tau in (refused.tau_cepstral_us, refused.tau_envelope_us)
        if DEFAULT_ECHO_SEARCH_US[0] <= tau <= DEFAULT_ECHO_SEARCH_US[1]
    ]
    assert len(both_in_window) == 2, refused
    assert refused.corroboration == pytest.approx(
        abs(refused.tau_envelope_us - refused.tau_cepstral_us)
        / refused.tau_cepstral_us,
        rel=1e-9,
    )

    # The contrast: a refusal where the two genuinely could not be compared
    # still reports the 1.0 marker. An 830 us echo puts BOTH estimates above
    # an 800 us ceiling, so neither corroborates anything.
    incomparable = detect_echo(
        _impulse_with_echo(830e-6, ECHO_R), SAMPLE_RATE, search_us=(120.0, 800.0)
    )
    assert incomparable.refusal == REFUSAL_NO_IN_WINDOW_ECHO, incomparable
    assert incomparable.corroboration == 1.0

    # A refusal taken before either estimate exists is the same marker: the
    # crest gate is an early return, so there is nothing to compare.
    no_arrival = detect_echo(
        np.random.default_rng(0).normal(0.0, 1.0, 65_536), SAMPLE_RATE
    )
    assert no_arrival.confidence == 0.0
    assert no_arrival.corroboration == 1.0
    assert no_arrival.tau_cepstral_us == 0.0


def test_reported_tau_is_always_the_envelope_estimate():
    """SF2 — the cepstrum corroborates; it never supplies the answer.

    :attr:`EchoDiagnostic.tau_us` used to be documented as "the envelope
    estimate, else the cepstral peak (quantised to ``resolution_us``)", and
    ``detect_echo`` carried the fallback branch to match. The branch could
    not execute: reaching the reporting return needs ``confidence > 0``,
    which needs ``corroboration < CORROBORATION_LOOSE``, which only the
    both-estimators-in-window path can produce — and on that path the
    envelope is in-window by construction.

    So the documented "coarse, quantised tau" was a value the field could
    never hold, and a reader budgeting for ~71 us of granularity was
    budgeting for a case that does not exist. The branch is gone; this
    pins the invariant that made it unreachable, so it cannot creep back.
    """
    seen = 0
    for tau_us in (240.0, 300.0, 450.0, 700.0):
        for search_us in ((120.0, 800.0), (200.0, 900.0), (150.0, 1000.0)):
            found = detect_echo(
                _impulse_with_echo(tau_us * 1e-6, ECHO_R),
                SAMPLE_RATE,
                search_us=search_us,
            )
            if found.confidence <= 0.0:
                continue
            seen += 1
            assert found.tau_us == found.tau_envelope_us, found
            assert found.corroboration < CORROBORATION_LOOSE, found
            assert search_us[0] <= found.tau_envelope_us <= search_us[1], found
    assert seen >= 8, "the invariant was never actually exercised"


def test_detect_echo_does_not_dress_up_a_sub_window_echo_as_an_in_window_one():
    """B1(b) — a true delay *below* the window's lower edge.

    An echo at 95-110 us cannot be resolved by a detector whose quefrency
    step is ~71 us, and it sits under the 120 us default search floor. Round
    1 shipped this as a *downstream* save: the detector reported ~135 us
    with high confidence — an in-window number that is simply wrong — and
    :func:`assess_geometry`'s resolution rule declined to cluster it.

    That was one guard too few. The resolution rule is anchored at zero
    delay, so it only screens a *low* window; raise the window to 300 us and
    the same aliased estimate lands at ~318 us, sails past a floor of 214
    us, and locks a cloud that never moved together. The detector now
    refuses at the source instead, on the property that actually
    generalises: proximity to the edge the aliasing lands on.
    """
    for true_tau_us in (95.0, 100.0, 104.0, 110.0):
        found = detect_echo(_impulse_with_echo(true_tau_us * 1e-6, ECHO_R), SAMPLE_RATE)
        assert found.resolution_us == pytest.approx(1e6 / 14_000.0, rel=1e-9)
        assert found.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, found
        assert found.tau_us == 0.0
        assert found.confidence == 0.0

        # Belt and braces: even had it been reported, the downstream
        # resolution floor still refuses to cluster it. Both guards are load
        # bearing — this one binds on a low window, the edge rule on any.
        usable = (
            found.refusal == ""
            and found.confidence >= ECHO_CONFIDENCE_FLOOR
            and found.tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us
        )
        assert not usable
        assert found.tau_envelope_us < GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us


def test_a_cloud_of_unresolvable_delays_does_not_read_as_geometry_locked():
    """B1(c) — the blocker's user-visible consequence.

    Ten positions whose true delays span 60-150 us: genuinely dispersed
    (2.5x spread), and every one of them below the detector's resolution
    floor. The estimates collapse onto ~115-152 us, and — before the fix —
    were reported with confidence >= 0.5 and clustered within +-15% of their
    median at a fraction of **1.0**, i.e. a confident ``geometry_locked``:
    "your mic cloud never moved, go spread it further", said about a cloud
    that moved plenty.

    Two independent guards now stop that, and the assertions below check
    both: the detector refuses these at the window's lower edge, and the
    evidence rule would refuse to cluster them even if it had not. The
    verdict is the honest one: not locked, insufficient evidence.
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
    assert all(
        e is not None and e.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE
        for e in result.per_position_echo
    ), [e.refusal for e in result.per_position_echo if e is not None]

    # The pathology itself, reconstructed from the refusals' own raw fields.
    # This is why a refused diagnostic keeps carrying them: the estimates
    # really do pile up (measured: 114-152 us, median 135.4), and really
    # would have satisfied the +-15% clustering test at a fraction of 0.9.
    # If this stops being true, the cloud this test is built on no longer
    # reproduces the bug and the regression guard has silently moved.
    railed = np.array([e.tau_envelope_us for e in result.per_position_echo])
    median = float(np.median(railed))
    clustered = float(np.mean(np.abs(railed - median) <= 0.15 * median))
    assert clustered >= 0.7, (
        f"the raw estimates no longer cluster ({clustered:.2f}) — this cloud "
        "has stopped reproducing the false-lock pathology"
    )
    assert float(railed.max() - railed.min()) < 40.0, (
        "genuinely dispersed 60-150 us delays must still collapse to one "
        "unresolvable value; that collapse IS the pathology"
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
        "ECHO_CONFIDENCE_FLOOR comment's measured 0.000-0.091 range is stale"
    )
    assert sum(c >= ECHO_CONFIDENCE_FLOOR for c in negatives) == 0

    positives = [
        detect_echo(_impulse_with_echo(tau_us * 1e-6, reflection), SAMPLE_RATE).confidence
        for tau_us in (240.0, 300.0, 450.0, 700.0)
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


def test_geometry_unknown_slug_is_pinned_to_its_literal_value():
    """The reason slug is a wire value, so pin the *string*, not the symbol.

    Every other assertion in this file compares against the imported
    ``GEOMETRY_UNKNOWN``, which is exactly what a rename would carry along
    silently — the suite stays green while a consumer matching on the old
    text breaks. One assertion against the literal is the only thing that
    can catch that, and it is deliberately spelled out rather than derived.

    The value is also the record of a deliberate wording choice: "usable",
    not "confident", because the count it describes survived all three
    admission rules (measured, confident, *and* resolvable), not just the
    confidence one.
    """
    assert GEOMETRY_UNKNOWN == "geometry_insufficient_usable_estimates"


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


def test_geometry_admission_rejects_a_zero_resolution_diagnostic():
    """N5 — the resolution rule must not be satisfiable by degeneracy.

    Rule 3 admits a diagnostic when ``tau_us >=
    GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. With ``resolution_us ==
    0`` that threshold collapses to zero, and a ``tau_us`` of zero clears
    it — so three maximally-confident records carrying no delay at all would
    be admitted, agree perfectly with their own median, and lock. That is
    the same false lock rule 1 exists to prevent, arriving through a
    different door.

    :func:`detect_echo` never emits ``resolution_us == 0`` (it is
    ``1 / band_width`` on a validated band), so this is a contract about
    what :func:`assess_geometry` accepts from *any* source — a hand-built
    record, a deserialised one, or a future second detector. The explicit
    ``tau_us > 0`` conjunct is what closes it.
    """
    degenerate = EchoDiagnostic(
        tau_us=0.0,
        strength_db=STRENGTH_FLOOR_DB,
        confidence=1.0,
        refusal="",
        resolution_us=0.0,
        tau_cepstral_us=0.0,
        tau_envelope_us=0.0,
        concentration=1.0,
        corroboration=0.0,
        arrival_crest_db=99.0,
    )

    verdict = assess_geometry([degenerate, degenerate, degenerate])
    assert verdict.locked is False
    assert verdict.reason == GEOMETRY_UNKNOWN
    assert verdict.n_confident == 0
    assert verdict.n_positions == 3
    assert verdict.median_tau_us == 0.0

    # Not a blanket ban on resolution_us == 0: a record carrying a real
    # delay is still admitted. It is the zero *delay* that is inadmissible.
    with_delay = replace(degenerate, tau_us=300.0, tau_envelope_us=300.0)
    admitted = assess_geometry([with_delay, with_delay, with_delay])
    assert admitted.n_confident == 3
    assert admitted.locked is True


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
    # No other position acquires the broken one's problem. Two of them carry
    # a refusal of their own — the 157 and 185 us members of this cloud sit
    # inside the default window's lower-edge margin — which is a fact about
    # those captures, present with or without the eleventh, and precisely
    # what "one position's problem" means.
    assert all(
        e is not None and e.refusal != REFUSAL_ALL_ZERO_IR
        for e in result.per_position_echo[:-1]
    )
    assert [e.refusal for e in result.per_position_echo[:-1]] == [
        e.refusal for e in good.per_position_echo
    ]

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
# C2. The three S0 hardenings, on synthetic ground truth
#
# Each rule's *motivating* record is real and lives in section F, which is
# env-gated and absent in CI. These are the CI-runnable half: they build the
# same shape from known truth so the rule is exercised everywhere, and they
# say which corpus record each one stands in for.
# --------------------------------------------------------------------------- #


def _lowpassed(ir: np.ndarray, fc_hz: float, order: int) -> np.ndarray:
    """Magnitude-only Butterworth-shaped lowpass, steep enough to turn the
    detector's 5-19 kHz default band into stopband.

    A stand-in for the electrical loopback's woofer branch, whose 2 kHz LR4
    lowpass is what put 40+ dB of rejection between its passband and the
    band ``detect_echo`` was pointed at (see ``BAND_BELOW_PASSBAND_MARGIN_DB``).
    Magnitude-only because the phase response is irrelevant here: the screen
    is a level comparison, and leaving phase alone keeps the direct arrival
    where the rest of the fixture put it.
    """
    n_fft = 1 << (ir.size - 1).bit_length()
    spectrum = np.fft.rfft(ir, n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SAMPLE_RATE)
    shape = 1.0 / np.sqrt(1.0 + (np.maximum(freqs, 1e-9) / fc_hz) ** (2 * order))
    return np.fft.irfft(spectrum * shape, n_fft)[: ir.size]


def test_band_below_passband_refuses_a_stopband_residue_signal():
    """S0-1 — the signal-presence screen, on a constructed stopband.

    An ordinary 320 us echo IR, lowpassed at 2 kHz hard enough that the
    5-19 kHz default band holds only residue: the declared 200-2000 Hz
    passband measures 48.6 dB above it, well past the 25.0 dB margin, and
    the detector refuses before either estimator runs.

    The *motivating* record is real and cannot be built here — the
    loopback's woofer branch returned a confident-looking ``tau = 323.3 us``
    from residue like this, and it is pinned by
    :func:`test_loopback_woofer_branch_is_refused_as_stopband_residue`. What
    this test owns is that the screen fires on the shape at all, in CI,
    without the corpus.
    """
    residue = _lowpassed(_impulse_with_echo(320e-6, ECHO_R), 2000.0, 10)

    screened = detect_echo(residue, SAMPLE_RATE, signal_band_hz=(200.0, 2000.0))
    assert screened.refusal == REFUSAL_BAND_BELOW_PASSBAND, screened
    assert screened.tau_us == 0.0
    assert screened.confidence == 0.0
    assert screened.strength_db == STRENGTH_FLOOR_DB
    # The refusal carries the number it turned on, so it is recomputable
    # from the record rather than asserted by the slug.
    assert screened.band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB, screened
    assert screened.band_deficit_db == pytest.approx(48.6, abs=1.0), screened
    # The estimators never ran: the screen returns before either exists.
    assert screened.tau_cepstral_us == 0.0
    assert screened.tau_envelope_us == 0.0

    # Declaring no passband is the documented default and leaves the
    # detector exactly as it was — the screen is opt-in, not a new floor.
    unscreened = detect_echo(residue, SAMPLE_RATE)
    assert unscreened.refusal != REFUSAL_BAND_BELOW_PASSBAND, unscreened
    assert unscreened.band_deficit_db == STRENGTH_FLOOR_DB, (
        "an undeclared passband must read as not-measured, not as 0 dB"
    )


def test_band_below_passband_stays_quiet_on_an_in_band_signal():
    """S0-1 — the screen's other half: it must not eat honest captures.

    The same 320 us echo, unfiltered, against two passbands a caller might
    plausibly declare. Both read within a fraction of a dB of zero — three
    orders of magnitude of margin below the 25.0 dB threshold — and the
    detection is untouched.
    """
    clean = _impulse_with_echo(320e-6, ECHO_R)
    reference = detect_echo(clean, SAMPLE_RATE)

    for passband in ((150.0, 20_000.0), DEFAULT_ECHO_BAND_HZ):
        measured = detect_echo(clean, SAMPLE_RATE, signal_band_hz=passband)
        assert measured.refusal == "", (passband, measured)
        assert abs(measured.band_deficit_db) < 1.0, (passband, measured)
        assert measured.confidence == pytest.approx(reference.confidence), passband
        assert measured.tau_us == pytest.approx(reference.tau_us), passband


def test_signal_band_is_validated_like_the_analysis_band():
    """S0-1 — malformed *config* raises, with a machine-readable slug.

    ``signal_band_hz`` is caller configuration in the same sense
    ``band_hz`` is: wrong for every capture at once and unfixable by looking
    at one. So it fails the same way, loudly, rather than becoming N
    identical refusals that would read as "no echo found anywhere".
    """
    ir = _impulse_with_echo(300e-6, ECHO_R)
    for bad in ((0.0, 2000.0), (2000.0, 200.0), (2000.0, 2000.0)):
        with pytest.raises(EchoInputError) as excinfo:
            detect_echo(ir, SAMPLE_RATE, signal_band_hz=bad)
        assert excinfo.value.slug == REFUSAL_BAD_SIGNAL_BAND_HZ, bad

    # Nyquist clipping matches band_hz's: an upper edge above Nyquist is
    # clipped rather than rejected, so a 20 kHz-declared passband works at
    # any sample rate that can carry the analysis band at all.
    clipped = detect_echo(ir, SAMPLE_RATE, signal_band_hz=(150.0, 96_000.0))
    assert clipped.refusal == "", clipped


def test_earlier_dominant_arrival_names_an_arrival_below_the_window():
    """S0-2 — the uninformative collapse, given a name.

    The S0 ground plane's geometry, reconstructed: a dominant arrival just
    *below* the search window (145 us at r=0.8) plus the real echo inside it
    (320 us at r=0.3). The envelope answers with the interloper, the window
    contract rejects that answer, corroboration is forced to the
    incomparable marker, and the score is zero **by construction** — the
    cepstrum's reading was never compared to anything.

    Before this rule the record said ``refusal == ""`` with
    ``confidence == 0.0``: "ran, found nothing credible", which is a
    different and much weaker statement than "the loudest thing here arrives
    before your window starts". The refusal now names it, and the real
    records it stands in for are pinned by
    :func:`test_ground_plane_positions_report_the_proud_capsule_arrival`.

    **The window is (166.6667, 1000) us — 8 samples at 48 kHz — since
    #1750**, not S0's (150, 1000). The refusal's first condition is that the
    envelope's own answer landed below ``search_us[0]``, and with the index
    bounds unified on ``ceil`` the envelope cannot *select* an out-of-window
    sample: it reaches below only by parabolic refinement, at most half a
    sample past ``first``. At a 7.2-sample lower edge that is not enough and
    ``tau_at_window_lower_edge`` names the record instead; at a sample-
    aligned one it is. That contrast is asserted below on this same IR, and
    on real captures in section F, so the alignment dependence is pinned
    rather than merely worked around.
    """
    ir = _impulse_with_two_echoes(145e-6, 0.8, 320e-6, 0.3)
    found = detect_echo(ir, SAMPLE_RATE, search_us=(166.6667, 1000.0))

    assert found.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, found
    assert found.tau_us == 0.0
    assert found.confidence == 0.0
    assert found.strength_db == STRENGTH_FLOOR_DB

    # The mechanism, readable off the record: the envelope's own answer is
    # below the window it was given, which is why nothing was comparable.
    assert 0.0 < found.tau_envelope_us < 166.6667, found
    assert found.corroboration == 1.0, found

    # The same IR through S0's own (150, 1000) protocol window, whose lower
    # edge is 7.2 samples: still a refusal, still zero, still disclosing the
    # interloper at the same delay and level — a different one of the two
    # correct reasons, because the envelope's answer now sits just inside
    # the window and inside the edge margin instead of just below it.
    unaligned = detect_echo(ir, SAMPLE_RATE, search_us=(150.0, 1000.0))
    assert unaligned.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, unaligned
    assert unaligned.tau_us == 0.0
    assert unaligned.confidence == 0.0
    assert unaligned.earlier_arrival_us == pytest.approx(
        found.earlier_arrival_us
    ), unaligned
    assert unaligned.earlier_arrival_db == pytest.approx(
        found.earlier_arrival_db
    ), unaligned

    # And the refusal names the interloper — delay and level, in the same
    # units strength_db uses.
    assert found.earlier_arrival_us == pytest.approx(145.0, abs=2.0), found
    assert found.earlier_arrival_db == pytest.approx(-1.9, abs=1.5), found

    # The same IR through the default window, which *contains* the
    # interloper: no below-window arrival, so nothing to name. This is the
    # remedy the refusal implies, and it is the same remedy the rest of the
    # module's window rules prescribe — widen the window.
    contained = detect_echo(ir, SAMPLE_RATE)
    assert contained.earlier_arrival_us == 0.0, contained
    assert contained.earlier_arrival_db == STRENGTH_FLOOR_DB, contained
    assert contained.refusal != REFUSAL_EARLIER_DOMINANT_ARRIVAL, contained


def test_found_nothing_credible_survives_the_earlier_arrival_refusal():
    """S0-2 — "refused" and "ran, found nothing" stay different outcomes.

    The module keeps an empty ``refusal`` with zero confidence as a distinct
    and deliberate state, and the new rule is gated narrowly enough not to
    swallow it: it needs the envelope's own answer to have landed below the
    window **and** a genuine arrival measured down there to name. An
    impulse-with-no-echo control has neither, so it still reports the honest
    nothing-found.

    Swept over the whole 60-member negative-control family
    ``ECHO_CONFIDENCE_FLOOR`` is calibrated against, at the default window,
    where the claim is absolute: the family has **no** below-window local
    maximum at all, so the refusal is unreachable by construction.

    Raised windows are where the danger is, and they are swept by
    :func:`test_earlier_arrival_dominance_floor_sits_in_the_measured_gap`
    against the committed ladder — including the mutation that proves the
    dominance floor, and not the accident of finding nothing, is what keeps
    the refusal off this family.
    """
    empty_refusals = 0
    for noise_sigma in (0.02, 0.001):
        for seed in range(30):
            rng = np.random.default_rng(seed)
            ir = np.zeros(65_536)
            ir[1000] = 1.0
            ir += rng.normal(0.0, noise_sigma, ir.size)
            found = detect_echo(ir, SAMPLE_RATE)
            where = (noise_sigma, seed)

            assert found.refusal != REFUSAL_EARLIER_DOMINANT_ARRIVAL, (where, found)
            assert found.earlier_arrival_us == 0.0, (where, found)
            assert found.earlier_arrival_db == STRENGTH_FLOOR_DB, where
            if found.refusal == "":
                # Not asserted as exactly zero: this family's documented
                # ceiling is 0.091 (see ``ECHO_CONFIDENCE_FLOOR``), and a
                # low-but-nonzero score with an empty refusal is the same
                # "ran, found nothing credible" outcome — it just found
                # slightly less nothing.
                assert found.confidence < ECHO_CONFIDENCE_FLOOR, (where, found)
                empty_refusals += 1

    # The state this test protects is actually reached — otherwise the
    # assertions above would be vacuously true.
    assert empty_refusals > 0


def test_earlier_arrival_dominance_floor_sits_in_the_measured_gap():
    """B1 — every figure ``EARLIER_ARRIVAL_DOMINANCE_DB`` quotes, re-derived
    from the one ladder it names.

    The constant separates two measured populations: the S0 ground plane's
    proud-capsule interlopers (which must refuse — real data, pinned in
    section F) from the band-limited envelope's own ringing on an echo-free
    signal (which must not). This is the ringing half, plus the mutation
    that shows the floor is load-bearing rather than decorative.

    **One ladder.** ``_CALIBRATION_WRONG_READING_WINDOWS`` — the eleven
    raised windows this file already commits for the rahmonic calibration —
    crossed with the 60-member negative-control family, 660 readings. It is
    reused rather than duplicated for the reason ``RAHMONIC_MARGIN`` gives:
    a described grid is not a grid, and a figure quoted from a mixture of
    ladders is reproducible from none of them.
    """
    controls = [
        (noise_sigma, seed)
        for noise_sigma in (0.02, 0.001)
        for seed in range(30)
    ]

    def echo_free_ir(noise_sigma: float, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        ir = np.zeros(65_536)
        ir[1000] = 1.0
        return ir + rng.normal(0.0, noise_sigma, ir.size)

    def sweep() -> tuple[list[float], int, dict[tuple[float, float], int]]:
        levels: list[float] = []
        flips = 0
        per_window: dict[tuple[float, float], int] = {}
        for search_us in _CALIBRATION_WRONG_READING_WINDOWS:
            in_window = 0
            for noise_sigma, seed in controls:
                found = detect_echo(
                    echo_free_ir(noise_sigma, seed),
                    SAMPLE_RATE,
                    search_us=search_us,
                )
                if found.earlier_arrival_us > 0.0:
                    levels.append(found.earlier_arrival_db)
                if found.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL:
                    flips += 1
                    in_window += 1
            per_window[search_us] = in_window
        return levels, flips, per_window

    readings = len(_CALIBRATION_WRONG_READING_WINDOWS) * len(controls)
    assert readings == 660

    levels, flips, _per_window = sweep()
    # The ringing population the constant is calibrated against.
    assert len(levels) == 658, len(levels)
    assert min(levels) == pytest.approx(-32.1297, abs=0.01), min(levels)
    assert max(levels) == pytest.approx(-17.1365, abs=0.01), max(levels)
    # ...and with the shipped floor, none of it is ever named as an arrival.
    assert flips == 0

    # Mutating the floor away must re-open the defect, or the constant is
    # decoration. These counts are the ones the comment quotes.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(spatial_combine, "EARLIER_ARRIVAL_DOMINANCE_DB", -120.0)
        unguarded_levels, unguarded_flips, unguarded_per_window = sweep()
    assert unguarded_levels == levels, (
        "the disclosure fields must not depend on the threshold — only the "
        "verdict does"
    )
    # Re-derived 2026-08-02: 21 -> 6 under #1750. The flips are records where
    # the envelope's answer landed below the window, and that is exactly what
    # the unified boundary stops the estimator doing on its own — what is
    # left reaches below only by parabolic refinement. The mutation still
    # re-opens the defect it is here to prove (6 > 0), on a population the
    # shipped floor still refuses 0 of.
    assert unguarded_flips == 6, unguarded_flips
    worst_window, worst_count = max(
        unguarded_per_window.items(), key=lambda item: item[1]
    )
    assert (worst_window, worst_count) == ((1000.0, 1600.0), 3), unguarded_per_window

    # The gap, in both directions, against the shipped value. The
    # ground-plane floor (-2.57 dB) is real data and lives in section F; the
    # ringing ceiling is the figure measured here.
    assert max(levels) < EARLIER_ARRIVAL_DOMINANCE_DB
    assert EARLIER_ARRIVAL_DOMINANCE_DB - max(levels) == pytest.approx(7.14, abs=0.05)

    # And a synthetic interloper at the ground plane's own level still
    # refuses, so the floor is not merely "quiet enough to never fire".
    # The window is the *sample-aligned* (166.6667, 1000) us — 8 samples at
    # 48 kHz — and not S0's (150, 1000) protocol window, because since #1750
    # the envelope can no longer select a sample below ``search_us[0]``: it
    # reaches below the window only through parabolic refinement, i.e. by at
    # most half a sample from ``first``. At a 7.2-sample lower edge that is
    # not enough and ``tau_at_window_lower_edge`` names the same record
    # instead (also a refusal, also carrying ``earlier_arrival_us`` /
    # ``_db``); at a sample-aligned edge it is. The real ground-plane
    # captures show both halves of that contrast, pinned in section F.
    loud = _impulse_with_two_echoes(145e-6, 0.8, 320e-6, 0.3)
    found = detect_echo(loud, SAMPLE_RATE, search_us=(166.6667, 1000.0))
    assert found.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, found
    assert found.earlier_arrival_db > EARLIER_ARRIVAL_DOMINANCE_DB, found


def test_a_below_dominance_interloper_falls_back_to_the_honest_zero():
    """B1 — the band the docstring owns: an interloper that takes the
    envelope's answer but is too quiet to be called dominant.

    The third condition is a threshold, so between it and the level at which
    an interloper stops winning the envelope at all there is a sliver where
    the record falls back to the silent empty-refusal outcome. That is by
    design — the alternative is naming ringing — but the docstring promises
    the band exists, so it is pinned rather than left as prose.

    Regime: one synthetic geometry — a 145 us interloper of varying strength
    against a real 320 us echo at r=0.3, searched (166.6667, 1000) us in the
    default band. Read the width as an order of magnitude, not a boundary.

    The window is the sample-aligned one for the reason
    :func:`test_earlier_dominant_arrival_names_an_arrival_below_the_window`
    gives: since #1750 the refusal's "envelope answered below the window"
    condition is only reachable within half a sample of the lower edge. The
    *levels* are unchanged by that — ``earlier_arrival_db`` is measured by a
    scan the fix did not touch, and reads -9.57 / -10.11 / -10.69 dB here
    exactly as it did at (150, 1000) before — so the band is the same band,
    read where the verdict it bounds can still be reached. It is one r step
    narrower: at r=0.30 the interloper no longer takes the envelope's answer
    at all, where at the old bounds it still did.
    """
    def probe(early_r: float):
        return detect_echo(
            _impulse_with_two_echoes(145e-6, early_r, 320e-6, 0.3),
            SAMPLE_RATE,
            search_us=(166.6667, 1000.0),
        )

    # Above the floor: named.
    dominant = probe(0.34)
    assert dominant.earlier_arrival_db == pytest.approx(-9.57, abs=0.2), dominant
    assert dominant.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, dominant

    # Inside the band: still steals the envelope's answer, still disclosed
    # on the record, but not named — the honest zero instead.
    fallen_back = probe(0.32)
    assert fallen_back.earlier_arrival_db == pytest.approx(-10.11, abs=0.2), fallen_back
    assert fallen_back.earlier_arrival_db < EARLIER_ARRIVAL_DOMINANCE_DB
    # It really did take the answer — that is what makes this a band
    # rather than just "quiet things are ignored".
    assert 0.0 < fallen_back.tau_envelope_us < 166.6667, fallen_back
    assert fallen_back.refusal == "", fallen_back
    assert fallen_back.confidence == 0.0, fallen_back
    # ...and the interloper is still disclosed on the fallen-back
    # record, which is what keeps the silence from being total.
    assert fallen_back.earlier_arrival_us == pytest.approx(145.8, abs=0.5)

    # Below the band: the interloper no longer wins the envelope at all, so
    # there is no mechanism left to name.
    weak = probe(0.30)
    assert weak.earlier_arrival_db == pytest.approx(-10.69, abs=0.2), weak
    assert not 0.0 < weak.tau_envelope_us < 166.6667, weak
    assert weak.refusal == "", weak


def test_effective_floor_is_reported_on_every_record():
    """S0-4 — the window's reporting floor, surfaced rather than raised.

    ``search_us[0]`` understates what a window can see: the bottom
    ``WINDOW_EDGE_MARGIN_STEPS`` of it is refused outright, so the default
    window's real floor is ~191.4 us and not its stated 120 us. S0's ground
    plane made that concrete — its 125-146 us arrivals are *structurally*
    unreportable there — and the plan's answer is disclosure, not a higher
    default (docs/historical/linearization-campaign-2026-07.md PR-2 item 4).

    Asserted on a measurement, on three different refusal paths, and on the
    ``combine_positions`` raise path, because a consumer disclosing "arrivals
    below ~X us are invisible" needs X most when the window found nothing.

    Two of the driving inputs changed under #1750, because they no longer
    reach the refusal they were chosen for: the ``rahmonic_of_lower_delay``
    case is now the honest-late-echo geometry through (750, 1100) us (a
    36-sample lower edge the boundary fix does not move), and the
    ``earlier_dominant_arrival`` case is read at the sample-aligned
    (166.6667, 1000) us. Neither substitution weakens the check — the field
    under test is ``effective_floor_us``, which is arithmetic on
    ``search_us[0]`` and the band — but the slug each case is *labelled*
    with has to be true, so the inputs move rather than the expectations.
    """
    def floor_for(search_us, band_hz=DEFAULT_ECHO_BAND_HZ):
        return search_us[0] + WINDOW_EDGE_MARGIN_STEPS * (
            1e6 / (band_hz[1] - band_hz[0])
        )

    measured = detect_echo(_impulse_with_echo(320e-6, ECHO_R), SAMPLE_RATE)
    assert measured.refusal == ""
    assert measured.effective_floor_us == pytest.approx(
        floor_for(DEFAULT_ECHO_SEARCH_US)
    )
    assert measured.effective_floor_us == pytest.approx(191.43, abs=0.01)

    # Every refusal ``detect_echo`` can be driven to from a constructed
    # input, including the three taken *before* the estimators run. "Every
    # record" is the promise; this is the check that it holds on the ones
    # where the field matters most.
    noise = np.random.default_rng(0)
    zero_confidence = np.zeros(65_536)
    zero_confidence[1000] = 1.0
    zero_confidence += np.random.default_rng(0).normal(0.0, 0.02, 65_536)
    cases = [
        # (slug, ir, kwargs, expected search window for the floor)
        (
            REFUSAL_LOW_ARRIVAL_CREST,
            noise.normal(0.0, 1.0, 8192),
            {},
            DEFAULT_ECHO_SEARCH_US,
        ),
        (REFUSAL_WINDOW_TOO_SHORT, np.array([0.0, 0.0, 0.0, 1.0]), {}, DEFAULT_ECHO_SEARCH_US),
        (
            REFUSAL_BAND_BELOW_PASSBAND,
            _lowpassed(_impulse_with_echo(320e-6, ECHO_R), 2000.0, 10),
            {"signal_band_hz": (200.0, 2000.0)},
            DEFAULT_ECHO_SEARCH_US,
        ),
        (
            REFUSAL_NO_IN_WINDOW_ECHO,
            _impulse_with_echo(830e-6, ECHO_R),
            {},
            DEFAULT_ECHO_SEARCH_US,
        ),
        (
            REFUSAL_TAU_AT_WINDOW_LOWER_EDGE,
            _impulse_with_echo(100e-6, ECHO_R),
            {},
            DEFAULT_ECHO_SEARCH_US,
        ),
        (
            REFUSAL_RAHMONIC_OF_LOWER_DELAY,
            _impulse_with_two_echoes(300e-6, 0.5, 850e-6, 0.2),
            {"search_us": (750.0, 1100.0)},
            (750.0, 1100.0),
        ),
        (
            REFUSAL_EARLIER_DOMINANT_ARRIVAL,
            _impulse_with_two_echoes(145e-6, 0.8, 320e-6, 0.3),
            {"search_us": (166.6667, 1000.0)},
            (166.6667, 1000.0),
        ),
        # ...and the non-refusal zero: "ran, found nothing credible".
        ("", zero_confidence, {}, DEFAULT_ECHO_SEARCH_US),
    ]
    for slug, ir, kwargs, search_us in cases:
        echo = detect_echo(ir, SAMPLE_RATE, **kwargs)
        assert echo.refusal == slug, echo
        assert echo.effective_floor_us == pytest.approx(floor_for(search_us)), slug
    # The band-too-narrow refusal has its own (much coarser) resolution, so
    # its floor is asserted against the band it actually used rather than
    # the default's — the field tracks the window *and* the band.
    narrow = detect_echo(
        _impulse_with_echo(320e-6, ECHO_R), SAMPLE_RATE, band_hz=(5000.0, 5100.0)
    )
    assert narrow.refusal == REFUSAL_BAND_TOO_NARROW, narrow
    assert narrow.effective_floor_us == pytest.approx(
        floor_for(DEFAULT_ECHO_SEARCH_US, (5000.0, 5100.0))
    )

    # A wider band buys a finer quefrency step, which lowers the floor —
    # the same lever the docstring points at for measuring below it.
    wide = detect_echo(
        _impulse_with_echo(320e-6, ECHO_R), SAMPLE_RATE, band_hz=(2000.0, 19_000.0)
    )
    assert wide.effective_floor_us == pytest.approx(floor_for(DEFAULT_ECHO_SEARCH_US, (2000.0, 19_000.0)))
    assert wide.effective_floor_us < measured.effective_floor_us

    # The one path that cannot compute it: combine_positions turning a
    # detector *raise* into a refused record. The band is unknown there, so
    # resolution_us is 0.0 and the floor is 0.0 with it — the two agree
    # about what was not measured.
    grid = _grid()
    capture = PositionCapture(
        position_id="zero",
        freqs_hz=grid,
        magnitude_db=np.zeros_like(grid),
        sample_rate=SAMPLE_RATE,
        ir=np.zeros(4096),
    )
    raised = combine_positions([capture]).per_position_echo[0]
    assert raised is not None
    assert raised.refusal == REFUSAL_ALL_ZERO_IR
    assert raised.resolution_us == 0.0
    assert raised.effective_floor_us == 0.0


def test_disclosed_floor_is_the_boundary_the_edge_check_applies():
    """S0-4 — the disclosed floor and the applied edge rule are one
    boundary, checked behaviourally rather than by reading the source.

    ``effective_floor_us`` exists so a consumer can say "arrivals below ~X
    are invisible to this window". That promise is only worth anything if X
    is the number the detector actually enforces. The two were briefly
    computed by separate expressions 230 lines apart, agreeing by
    coincidence; they now share one derivation, and this is the check that
    they keep agreeing.

    The contract asserted is exact and holds per record: **for any record
    that reached the edge check at all** (i.e. at least one estimator landed
    in the window — otherwise ``no_in_window_echo`` pre-empts it),
    ``tau_at_window_lower_edge`` fires if and only if the lowest in-window
    candidate is at or below that record's own ``effective_floor_us``. No
    estimator-bias caveat is needed because the comparison is made against
    the candidates the record reports, not against the synthetic truth.
    """
    reached = refused = accepted = 0
    for band_hz in ((5000.0, 19_000.0), (2000.0, 19_000.0), (6000.0, 18_000.0)):
        for search_us in ((120.0, 800.0), (150.0, 1000.0), (200.0, 900.0), (300.0, 800.0)):
            for tau_us in np.arange(80.0, 520.0, 20.0):
                for reflection in (0.15, 0.36, 0.6):
                    found = detect_echo(
                        _impulse_with_echo(float(tau_us) * 1e-6, reflection),
                        SAMPLE_RATE,
                        band_hz=band_hz,
                        search_us=search_us,
                    )
                    candidates = [
                        tau
                        for tau in (found.tau_cepstral_us, found.tau_envelope_us)
                        if search_us[0] <= tau <= search_us[1]
                    ]
                    if not candidates:
                        continue
                    reached += 1
                    at_or_below_floor = min(candidates) <= found.effective_floor_us
                    edge_refused = found.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE
                    assert at_or_below_floor == edge_refused, (
                        band_hz,
                        search_us,
                        tau_us,
                        reflection,
                        found,
                    )
                    refused += at_or_below_floor
                    accepted += not at_or_below_floor

    # Both sides of the boundary are exercised — an "iff" that only ever saw
    # one outcome would pass without testing anything.
    assert reached > 300, reached
    assert refused > 50 and accepted > 50, (refused, accepted)


def test_band_deficit_sentinel_covers_all_three_documented_causes():
    """S0-1 — ``band_deficit_db == STRENGTH_FLOOR_DB`` means "not measured",
    from any of the three causes its docstring lists. All three pinned.

    Cause 3 is the one that matters most downstream: a regression flipping
    the sub-bin passband from fail-**open** to fail-closed would make the
    PR-4 wiring refuse every position in a cloud on a declared passband
    narrower than one FFT bin, and no other test would notice.
    """
    ir = _impulse_with_echo(320e-6, ECHO_R)
    passband = (150.0, 20_000.0)

    # Cause 1 — no passband declared. (Also pinned by the combiner test;
    # repeated here so all three causes are visible in one place.)
    assert detect_echo(ir, SAMPLE_RATE).band_deficit_db == STRENGTH_FLOOR_DB

    # Cause 2 — the detector returned before the screen ran. All three
    # pre-screen refusals, each with a passband declared, so the sentinel is
    # "not measured" rather than "not asked for".
    pre_screen = {
        REFUSAL_LOW_ARRIVAL_CREST: (
            np.random.default_rng(0).normal(0.0, 1.0, 8192),
            {},
        ),
        REFUSAL_WINDOW_TOO_SHORT: (np.array([0.0, 0.0, 0.0, 1.0]), {}),
        REFUSAL_BAND_TOO_NARROW: (ir, {"band_hz": (5000.0, 5100.0)}),
    }
    for slug, (signal, kwargs) in pre_screen.items():
        found = detect_echo(
            signal, SAMPLE_RATE, signal_band_hz=passband, **kwargs
        )
        assert found.refusal == slug, found
        assert found.band_deficit_db == STRENGTH_FLOOR_DB, slug

    # Cause 3 — the screen ran, but the declared passband covers no bin of
    # the segment's spectrum. At n_fft >= 4096 and 48 kHz the bins are
    # ~11.7 Hz apart, so these are sub-bin passbands. Fail-**open**: the
    # verdict is identical to the undeclared call, and the deficit reads
    # not-measured rather than a fabricated number.
    ungated = detect_echo(ir, SAMPLE_RATE)
    for narrow in ((100.0, 105.0), (5.0, 8.0), (100.0, 103.0)):
        found = detect_echo(ir, SAMPLE_RATE, signal_band_hz=narrow)
        assert found.band_deficit_db == STRENGTH_FLOOR_DB, narrow
        assert found.refusal == ungated.refusal == "", narrow
        assert found.tau_us == pytest.approx(ungated.tau_us), narrow
        assert found.confidence == pytest.approx(ungated.confidence), narrow


def test_signal_band_is_plumbed_through_the_combiner_and_recorded():
    """S0-1 — the combiner passes the declared passband down and echoes it
    back, under the same "actually applied" convention as the echo band and
    search window. PR-4's wiring is then value-supply only.
    """
    freqs, _true_db, captures = _cloud(_dispersed_taus())

    default = combine_positions(captures)
    assert default.signal_band_hz is None
    assert all(
        e is not None and e.band_deficit_db == STRENGTH_FLOOR_DB
        for e in default.per_position_echo
    )

    declared = combine_positions(captures, signal_band_hz=[150.0, 20_000.0])
    # Coerced to a plain tuple of floats, like echo_band_hz / echo_search_us.
    assert declared.signal_band_hz == (150.0, 20_000.0)
    assert all(
        e is not None and e.band_deficit_db != STRENGTH_FLOOR_DB
        for e in declared.per_position_echo
    )
    # A passband that contains the analysis band changes no verdict.
    assert declared.geometry == default.geometry

    # Malformed config raises rather than refusing every position — the same
    # posture as the other two window arguments.
    for bad in ((0.0, 2000.0), (2000.0, 200.0), (1000.0,), 500.0):
        with pytest.raises(ValueError, match="signal_band_hz"):
            combine_positions(captures, signal_band_hz=bad)


def test_thin_evidence_qualifies_a_verdict_without_withholding_it():
    """S0-3 / issue #1742 item 2 — a ten-position cloud whose verdict rests
    on two usable estimates says so.

    The rule is exactly ``n_confident == GEOMETRY_MIN_CONFIDENT and
    n_positions >= 2 * GEOMETRY_MIN_CONFIDENT``, and it is *disclosure*: the
    verdict, its reason, and every supporting number are unchanged. The
    rejected alternative was scaling the clustering threshold with
    ``n_positions``, which turns a measured verdict into no verdict — a
    worse answer than a qualified one.
    """
    usable = EchoDiagnostic(
        tau_us=320.0,
        strength_db=-8.8,
        confidence=0.9,
        refusal="",
        resolution_us=1e6 / 14_000.0,
        tau_cepstral_us=318.0,
        tau_envelope_us=320.0,
        concentration=0.66,
        corroboration=0.006,
        arrival_crest_db=100.0,
    )
    unusable = replace(usable, refusal=REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, tau_us=0.0)

    thin = assess_geometry([usable, usable] + [unusable] * 8)
    assert thin.n_positions == 10
    assert thin.n_confident == GEOMETRY_MIN_CONFIDENT
    assert thin.thin_evidence is True
    # Disclosure only: the verdict is the same one the two estimates earn.
    assert thin.locked is True
    assert thin.reason == GEOMETRY_LOCKED
    assert thin.clustered_fraction == pytest.approx(1.0)

    # One more usable estimate and it is no longer the bare minimum.
    assert assess_geometry([usable] * 3 + [unusable] * 7).thin_evidence is False
    # A three-position cloud giving two usable estimates is not thin: two of
    # three is the evidence that cloud had, not a shortfall against it.
    assert assess_geometry([usable, usable, unusable]).thin_evidence is False
    # The flag is verdict-independent by construction, and structurally
    # unreachable on GEOMETRY_UNKNOWN — that reason fires exactly when
    # n_confident < GEOMETRY_MIN_CONFIDENT, which the equality excludes.
    unknown = assess_geometry([usable] + [unusable] * 9)
    assert unknown.reason == GEOMETRY_UNKNOWN
    assert unknown.thin_evidence is False
    # ...and it does qualify a dispersed verdict, not only a locked one.
    far = replace(usable, tau_us=900.0, tau_envelope_us=900.0)
    dispersed = assess_geometry([usable, far] + [unusable] * 8)
    assert dispersed.reason == GEOMETRY_DISPERSED
    assert dispersed.thin_evidence is True


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
    where the 1/N-octave window is a couple of bins wide).

    **Both raw curves are outside that bound, and both are named here.**
    Measured worst-bin: ``power_mean_db`` 0.224 dB and ``median_db`` 0.383
    dB. Earlier wording disclosed only the power mean, which left the
    median — the other half of the honesty screen's input pair, and the
    worse of the two — looking as though it had been checked when it had
    not. Neither is held to 0.1 dB on purpose: at 8x coarser spacing a raw
    per-bin curve genuinely cannot reproduce fine comb structure
    bin-for-bin, which is the resolution being traded away, and the median
    moves more than the mean because a per-bin median of three positions is
    an order statistic — decimation can change *which* position is the
    middle one, where the power mean only re-averages.

    **One consumer does read a raw curve, and it is why the loose bound is
    asserted rather than merely disclosed.** The exclusion screen compares
    ``power_mean_diag_db`` against ``median_diag_db`` and the spec is
    evaluated on ``power_mean_spec_db``, all three bounded above — but
    :mod:`jasper.audio_measurement.interference_nulls` reads
    ``power_mean_db`` **unsmoothed** at each located minimum: that is the
    ``null(unsmoothed)`` term of its depth statistic, and it is unsmoothed
    precisely because a fractional-octave window fills the null being
    measured. An earlier revision of this docstring said no consumer read a
    raw curve; PR-1 falsified it.

    That consumer is safe at this bound, but **not because the bound is
    tight** — a worst-bin maximum over 16k bins is simply not what a
    single-bin reading experiences, and the depth statistic's other end (the
    flank baseline) is read off a *smoothed* curve held to 0.1 dB, so only
    one of its two terms is exposed to this figure at all. The claim that
    matters is measured end to end rather than inferred: with the cap lifted
    on the S0 main-leg cloud, the identified rungs' depths move by at most
    **0.033 dB** and the depth-ceiling acquittal's margin by **0.026 dB**,
    against that rule's 0.98 dB of one-sided headroom. That measurement is
    corpus-gated and lives with the statistic it bounds —
    ``test_the_analysis_grid_cap_costs_hundredths_of_a_db_end_to_end`` in
    tests/test_interference_nulls.py asserts it, and ``NULL_DEPTH_STATISTIC``
    narrates it. Restated here only because this test's own bound is a
    rot-guard on that disclosed magnitude rather than a correctness threshold;
    both figures were re-derived 2026-08-22, having gone stale in both places
    at once while neither asserted them.

    ``per_position_db`` is the same kind of array (PR-1 retains the stack the
    combined curves are reduced from) and is held to the same loose bound,
    for the same reason: the null gate reads it per position when classifying
    a rung ``position_invariant`` or ``position_dependent``. Measured
    worst-bin 0.404 / 0.381 / 0.429 dB — larger than the power mean's 0.224,
    which is the expected direction, since averaging is what buys the mean
    its stability. Its smoothed sibling ``per_position_diag_db`` measures
    0.096 / 0.107 / 0.135 dB, above the combined smoothed curves' 0.074-0.085
    and well below the raw stack; it is bounded separately at 0.2 dB rather
    than folded into either group.
    """
    captures = _large_grid_cloud()
    assert captures[0].freqs_hz.size > 8 * MAX_ANALYSIS_BINS / 2

    decimated = combine_positions(captures)
    assert decimated.freqs_hz.size <= MAX_ANALYSIS_BINS
    assert decimated.freqs_hz.size == 16_384

    monkeypatch.setattr(spatial_combine, "MAX_ANALYSIS_BINS", 10**9)
    undecimated = combine_positions(captures)
    assert undecimated.freqs_hz.size == captures[0].freqs_hz.size

    def worst_bin_db(name: str) -> float:
        reference = np.interp(
            decimated.freqs_hz, undecimated.freqs_hz, getattr(undecimated, name)
        )
        return float(np.max(np.abs(np.asarray(getattr(decimated, name)) - reference)))

    for name in ("power_mean_spec_db", "power_mean_diag_db", "median_diag_db"):
        worst = worst_bin_db(name)
        assert worst < 0.1, f"{name} moved by {worst:.4f} dB under decimation"

    # The raw pair: disclosed, bounded loosely, and asserted to be the ones
    # that exceed 0.1 dB — so the disclosure above cannot rot in either
    # direction. If a future change brings them inside 0.1 dB, this fails
    # and the docstring is what needs updating.
    raw_worst = {name: worst_bin_db(name) for name in ("power_mean_db", "median_db")}
    for name, worst in raw_worst.items():
        assert 0.1 < worst < 0.6, f"{name} moved by {worst:.4f} dB under decimation"
    assert raw_worst["median_db"] > raw_worst["power_mean_db"], (
        "the median is documented as the worse of the two — an order "
        f"statistic, not an average: {raw_worst}"
    )

    # The retained per-position stack is raw in exactly the same sense and is
    # held to the same bound, row by row. Without this it would be the one
    # array in the result with no decimation contract at all — and the null
    # gate reads it per position to classify a rung.
    per_position_worst = 0.0
    for row, (decimated_row, undecimated_row) in enumerate(
        zip(decimated.per_position_db, undecimated.per_position_db, strict=True)
    ):
        reference = np.interp(
            decimated.freqs_hz, undecimated.freqs_hz, undecimated_row
        )
        worst = float(np.max(np.abs(decimated_row - reference)))
        assert worst < 0.6, f"per_position_db[{row}] moved by {worst:.4f} dB"
        per_position_worst = max(per_position_worst, worst)
    # It is a per-position curve, so it cannot be quieter than the power mean
    # of those same positions: averaging is what buys the mean its stability.
    assert per_position_worst > raw_worst["power_mean_db"]
    # The figure the docstring quotes, pinned so it cannot rot into prose.
    assert per_position_worst == pytest.approx(0.429, abs=0.01)

    # The smoothed per-position curves sit *between* the two bounds, and the
    # ordering is the point: measured 0.096-0.135 dB worst-bin against
    # 0.074-0.085 dB for the three combined smoothed curves. Smoothing one
    # position is not smoothing an average of three — the combined curve has
    # already lost the fine structure decimation would otherwise cost it — so
    # holding these to the combined curves' 0.1 dB would be asserting
    # something untrue rather than something strict.
    diag_worst = 0.0
    for row, (decimated_row, undecimated_row) in enumerate(
        zip(decimated.per_position_diag_db, undecimated.per_position_diag_db, strict=True)
    ):
        reference = np.interp(
            decimated.freqs_hz, undecimated.freqs_hz, undecimated_row
        )
        worst = float(np.max(np.abs(decimated_row - reference)))
        assert worst < 0.2, f"per_position_diag_db[{row}] moved by {worst:.4f} dB"
        diag_worst = max(diag_worst, worst)
    assert diag_worst > 0.085, (
        "the per-position smoothed curves are documented as moving more than "
        f"the combined ones, which top out at 0.085 dB here: {diag_worst:.4f}"
    )
    assert diag_worst == pytest.approx(0.135, abs=0.01)
    # ...and still far less than the raw stack they came from: smoothing is
    # what buys a curve its decimation stability, per position as much as
    # across the cloud.
    assert diag_worst < per_position_worst / 2.0


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

# The corpus is gitignored and laptop-durable: it lives under the checkout it
# was captured beside, and is simply absent in CI (where these tests skip).
# Its root and skip gate moved into tests/_flat_lin_corpus.py on 2026-07-27
# when PR-L3's level-match regression became the corpus's second reader —
# where the corpus IS and when to skip must be one answer for both. The
# loader below stays here: the two readers want different phases under
# different program parameters, and that is not shared reading.
CORPUS = CDHORN_ROOT
requires_corpus = requires_cdhorn


@pytest.fixture(scope="module")
def corpus_irs() -> dict[str, np.ndarray]:
    """Era-exact reconstruction of the run 5 / run 7 impulse responses.

    Program parameters are reused verbatim from the session's own forensics
    script (``captures/flat-linearization-20260725/comb_forensics2.py``),
    which is the authority on what DSP state those captures were taken
    under; re-deriving them here would risk silently analysing a different
    program than the one that was played.

    REGISTRATION FIXED 2026-07-29 (issue #1879). This loader used to place
    the deconvolution window at ``find_offset(capture, archived_program) +
    segment.start_sample`` — an offset measured against the 2026-07-24
    program WAV, plus a schedule position read off a *freshly composed* one.
    Those agreed until #1816 (beeps-first, 2026-07-28) inserted a 1.0 s
    pre-pilot ambient window ahead of the pilots, moving
    ``sweep_verify.start_sample`` 369324 -> 417324 while every archived
    capture still had its sweep at 369324. The window then landed 48000
    samples late in a 288091-sample (6.0 s) sweep, reading its last 5.0 s
    plus 1.0 s of tail, and two pinned readings moved with it.

    Anchoring on :func:`~tests._flat_lin_corpus.sweep_anchor` — the shared
    house registration, already used by every other reader of these corpora
    — locates the sweep by its own waveform, so where the composer puts it
    cannot matter. Note which reading did NOT move under the old scheme:
    ``run7_tweeter`` registered against a freshly *rendered* MEASURE program,
    so its reference and its ``start_sample`` shifted together and cancelled.
    Self-consistency is what saved it, not correctness, and it is on the
    shared anchor now for the same reason as the rest — leaving one loader on
    a second convention is precisely what made #1879 possible.

    One measured caveat on that, because it is not obvious: MEASURE ships
    **three** repeats of each sweep, so anchoring on ``sweep_t``'s waveform
    picks among three peaks that agree to 0.4% (553.02 / 551.05 / 551.88 on
    the run 7 capture). It lands on the first, but the margin is thin and
    should not be relied on. It does not need to be: the repeats ARE the same
    measurement, and reading each at its own peak gives band deficits of
    1.1130 / 1.1035 / 1.1077 dB and taus of 323.00 / 322.64 / 322.34 us —
    a 0.01 dB, 0.7 us spread against a +/-0.3 dB pin. Note also that the later
    repeats sit +16 and +32 samples off their scheduled positions (playback vs
    capture clock drift, ~31 ppm over the 43.8 s capture), which is an
    argument FOR locating each sweep by its own waveform rather than by
    schedule arithmetic off one global offset.
    """
    import glob
    import wave

    from jasper.audio_measurement import program_analysis as pa
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
        build_verify_program,
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

    def impulse_response(capture, program, segment_id) -> np.ndarray:
        segment = program.segment(segment_id)
        offset = sweep_anchor(capture, segment)
        return pa._deconvolve_window(capture, segment, offset, SAMPLE_RATE)[0]

    measure_capture = load(sorted(glob.glob(f"{CORPUS}/*run7_measure.wav"))[-1])

    return {
        "run7_verify": impulse_response(
            load(sorted(glob.glob(f"{CORPUS}/*run7_verify.wav"))[-1]),
            verify_program,
            "sweep_verify",
        ),
        "run5_verify": impulse_response(
            load(sorted(glob.glob(f"{CORPUS}/*run5_verify.wav"))[-1]),
            verify_program,
            "sweep_verify",
        ),
        "run7_tweeter": impulse_response(measure_capture, measure_program, "sweep_t"),
    }


# Deliberately NOT ``@requires_corpus``, unlike everything else in this
# section. Every corpus reading above rests on one property of
# ``sweep_anchor`` — that it finds the sweep by its own waveform and owes
# nothing to the composer's schedule — and that property is exactly what
# #1879 turned out to need. Pinning it on a SYNTHETIC capture is what makes
# it visible to CI, where the corpus is absent and every test that would
# otherwise notice simply skips.
def test_sweep_anchor_owes_nothing_to_the_composers_schedule():
    """The registration invariant the archived corpora rest on.

    #1879: this loader used to register an archived capture against the
    archived program but read ``start_sample`` off a freshly composed one.
    #1816 then inserted a 1.0 s pre-pilot ambient window ahead of the sweep,
    the two disagreed by 48000 samples, and two pinned readings moved.

    ``sweep_anchor`` is immune because it locates the stimulus by
    cross-correlating the stimulus itself. This test is the guard on that
    sentence: move the segment's declared schedule position and the answer
    must not budge, because a capture recorded years ago cannot know where
    today's composer would have put its sweep.
    """
    import dataclasses

    from jasper.audio_measurement.program import build_verify_program, segment_stimulus

    program = build_verify_program(
        2000.0, leading_pilot_gains_db=(-16.0006, -6.0005), courtesy_prelude=True
    )
    segment = program.segment("sweep_verify")
    stimulus = np.asarray(segment_stimulus(segment), dtype=np.float64)

    # A capture that knows nothing about the schedule: the sweep sits at a
    # position of our choosing, which is what an archived WAV amounts to.
    planted_at = 12_345
    capture = np.zeros(planted_at + stimulus.size + 8_000)
    capture[planted_at : planted_at + stimulus.size] = stimulus

    assert sweep_anchor(capture, segment) == planted_at

    # Now claim the composer moved it — by exactly the shift #1816 applied,
    # and by a shift in the other direction for good measure.
    for delta in (48_000, -7_777):
        moved = dataclasses.replace(segment, start_sample=segment.start_sample + delta)
        assert sweep_anchor(capture, moved) == planted_at, delta


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
        # The rahmonic screen's headroom on *real* data, which is the only
        # place the "low-quefrency leakage cannot auto-refuse an honest
        # reading" claim can actually be tested: a real IR carries a real
        # driver response, and the cubic detrend is what keeps it out of the
        # low quefrencies. Measured 0.329-0.387 against the shipped
        # ``RAHMONIC_MARGIN``, asserted against the symbol two lines down. A
        # future change to DETREND_ORDER or the analysis band would show up
        # here first. The bound is 0.45 rather than 0.5 so it is a genuine
        # tripwire on that 0.387 worst case (~16% of headroom) rather than a
        # number with room for real drift to hide in.
        assert result.lower_peak_ratio < 0.45, f"{name}: {result}"
        assert result.lower_peak_ratio < RAHMONIC_MARGIN, name


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


# --------------------------------------------------------------------------- #
# F. Real-data acceptance — the 2026-07-25 S0 session
#
# The records that motivated the three hardenings in section C2, gated on a
# second laptop-durable root so the two corpora can live in different places
# (the S0 session postdates the cdhorn one by a day and is a different
# capture protocol). Absent in CI, where these skip; a corpus-acceptance PR
# is not done until they have been seen to PASS, because a wrong-but-
# existing path skips silently.
# --------------------------------------------------------------------------- #

# Roots, skip gates, declared passbands and the era-exact deconvolution now
# live in tests/_flat_lin_corpus.py — promoted there when
# tests/test_interference_nulls.py needed the same session directories, so
# there is one loading chain rather than two to keep era-exact. Imported at
# module scope (see the import block at the top of this file).


@pytest.fixture(scope="module")
def ground_plane_irs() -> dict[str, np.ndarray]:
    return s0_position_irs(S0_GROUND_PLANE)


@pytest.fixture(scope="module")
def main_leg_irs() -> dict[str, np.ndarray]:
    """The ten-position desk cloud — leg A, same protocol, one changed mic
    mounting. It is the control every leg-B claim below is a contrast
    against, so it is loaded rather than quoted (~5 s, module-scoped).
    """
    return s0_position_irs(S0_MAIN)


@pytest.fixture(scope="module")
def loopback_irs() -> dict[str, np.ndarray]:
    """The electrical loopback's per-branch IRs, as the loopback run left
    them (``np.load``, no reconstruction — these are already impulse
    responses, one per stimulus x branch).
    """
    return _loopback_irs()


@requires_s0
def test_loopback_woofer_branch_is_refused_as_stopband_residue(loopback_irs):
    """S0-1 acceptance — the record the signal-presence screen exists for.

    2026-07-25, a file-in/file-out loopback of the live JTS3 CamillaDSP
    graph (s0-analysis/loopback/LOOPBACK-REPORT.md § 5a). Read in the
    5-19 kHz default band, the **woofer** branch — behind a 2 kHz LR4
    lowpass, so that band holds nothing but stopband residue and
    quantisation noise — returned ``tau = 323.3 us`` at ``confidence =
    0.275`` with an **empty refusal** on the sweep stimulus, and did not
    reproduce it on the impulse or MLS stimuli of the same graph.

    With its own passband declared, all three stimuli are refused, because
    what is wrong is the question rather than the stimulus.
    """
    for stimulus in ("impulse", "sweep", "mls"):
        ir = loopback_irs[f"{stimulus}_woofer"]
        screened = detect_echo(ir, SAMPLE_RATE, signal_band_hz=LOOPBACK_WOOFER_PASSBAND_HZ)
        assert screened.refusal == REFUSAL_BAND_BELOW_PASSBAND, (stimulus, screened)
        assert screened.tau_us == 0.0
        assert screened.band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB, stimulus
        assert screened.band_deficit_db == pytest.approx(41.0, abs=1.5), stimulus

    # The defect itself, at the leg-B protocol window the loopback used:
    # without a declared passband the sweep still reports its confident-
    # looking number, so this is a gate the caller must supply, not a
    # behaviour change that happened anyway.
    unscreened = detect_echo(
        loopback_irs["sweep_woofer"], SAMPLE_RATE, search_us=S0_PROTOCOL_SEARCH_US
    )
    assert unscreened.refusal == "", unscreened
    assert unscreened.tau_us == pytest.approx(323.3, abs=1.0), unscreened
    assert unscreened.confidence == pytest.approx(0.275, abs=0.01), unscreened


@requires_s0
def test_loopback_tweeter_branch_is_measured_in_its_own_passband(loopback_irs):
    """S0-1 — the in-band control, from the same loopback run.

    The *tweeter* branch's passband overlaps the analysis band, so the
    deficit is within a fifth of a dB of zero on all three stimuli and the
    screen stays out of the way. Without it, "an electrical IR is refused"
    would be indistinguishable from "the screen fires on electrical IRs".
    """
    for stimulus in ("impulse", "sweep", "mls"):
        ir = loopback_irs[f"{stimulus}_tweeter"]
        screened = detect_echo(ir, SAMPLE_RATE, signal_band_hz=LOOPBACK_TWEETER_PASSBAND_HZ)
        assert screened.refusal != REFUSAL_BAND_BELOW_PASSBAND, (stimulus, screened)
        assert abs(screened.band_deficit_db) < 0.5, (stimulus, screened)
        # Unchanged by declaring the passband — same verdict as without it.
        assert screened.refusal == detect_echo(ir, SAMPLE_RATE).refusal, stimulus


@requires_s0
def test_band_deficit_separation_depends_on_the_analysis_band(
    ground_plane_irs, main_leg_irs, loopback_irs
):
    """S0-1 — the margin's dependence on ``band_hz``, including where it
    stops working.

    PR-4 derives the analysis band from a driver contract rather than
    leaving it at the module default
    (``crossover_v2_flow._derive_cloud_echo_band_hz``, landed 2026-07-26 —
    clamped inside the declared passband, and since 2026-07-27 (issue #1763)
    clamped UP to ``ECHO_BAND_HF_REGIME_FLOOR_HZ`` as well, which is what
    actually keeps it clear of the crossover: JTS3's tweeter correctly
    declares a 2 kHz lower edge, so containment alone reached the dead row
    below), so "the separation was calibrated at one band" was a real
    limitation this test keeps honest. Swept over six bands, on the 13 S0 acoustic
    records (3 ground-plane, 10 main-leg, each against its own 150 Hz-20 kHz
    declared passband) and the 3 electrical-loopback woofer records
    (200-2000 Hz passband).

    The honest side is comfortable at every band. The **residue** side is
    not: at a band whose lower edge sits on this speaker's 2 kHz crossover,
    the woofer's own passband is inside the band being analysed, its deficit
    collapses to ~18 dB, and the screen misses the exact case it exists for.
    That is a false negative degrading silently to the pre-screen behaviour,
    not a narrowed gap — and it is why the constant's comment tells a caller
    to keep the analysis band clear of the crossover.

    An earlier revision of this test checked only that no *honest* record
    fires, which is one-sided: it would have passed at (2000, 19000) while
    the screen was not screening.

    What this does **not** cover is a different speaker: all 13 acoustic
    records are the same JTS3 cdhorn.
    """
    acoustic = [
        (ir, S0_SUMMED_PASSBAND_HZ)
        for ir in list(ground_plane_irs.values()) + list(main_leg_irs.values())
    ]
    residue = [
        (loopback_irs[f"{stimulus}_woofer"], LOOPBACK_WOOFER_PASSBAND_HZ)
        for stimulus in ("impulse", "sweep", "mls")
    ]
    assert len(acoustic) == 13

    def deficits(population, band_hz):
        return [
            detect_echo(
                ir, SAMPLE_RATE, band_hz=band_hz, signal_band_hz=passband
            ).band_deficit_db
            for ir, passband in population
        ]

    # (band, does the screen still catch stopband residue) — the table the
    # constant's comment prints, re-derived.
    expected_catches = {
        DEFAULT_ECHO_BAND_HZ: True,
        (10_000.0, 19_000.0): True,
        (8000.0, 16_000.0): True,
        (4000.0, 20_000.0): True,
        (3000.0, 19_000.0): True,
        (2000.0, 19_000.0): False,
    }
    honest_ceilings = []
    for band_hz, catches in expected_catches.items():
        honest = deficits(acoustic, band_hz)
        stopband = deficits(residue, band_hz)
        honest_ceilings.append(max(honest))

        # The honest side never fires, at any band tried.
        assert max(honest) < BAND_BELOW_PASSBAND_MARGIN_DB, (band_hz, max(honest))
        # The residue side is the one that moves.
        assert (min(stopband) > BAND_BELOW_PASSBAND_MARGIN_DB) is catches, (
            band_hz,
            min(stopband),
        )

    # The two rows the comment turns on.
    default_residue = deficits(residue, DEFAULT_ECHO_BAND_HZ)
    assert (min(default_residue), max(default_residue)) == pytest.approx(
        (40.43, 41.98), abs=0.3
    )
    crossover_residue = deficits(residue, (2000.0, 19_000.0))
    assert (min(crossover_residue), max(crossover_residue)) == pytest.approx(
        (18.21, 18.23), abs=0.3
    )
    # One octave up the margin is already thin — which is why the guidance
    # is "clear of the crossover", not "3 kHz is fine".
    thin_residue = deficits(residue, (3000.0, 19_000.0))
    assert min(thin_residue) - BAND_BELOW_PASSBAND_MARGIN_DB == pytest.approx(
        1.53, abs=0.3
    ), thin_residue

    # And the honest population's own spread across all six bands, which is
    # the number to watch when this ships against other hardware.
    assert max(honest_ceilings) == pytest.approx(17.50, abs=0.3), honest_ceilings
    assert BAND_BELOW_PASSBAND_MARGIN_DB - max(honest_ceilings) > 7.0


@requires_s0
def test_main_leg_is_unchanged_and_is_the_ground_plane_s_control(main_leg_irs):
    """S0-2 / S0-4 — nothing moved on the leg that was already working.

    The ten-position desk cloud, searched in the same leg-B protocol window
    that collapsed all three ground-plane records. Every position still
    measures the source-fixed ~320 us rim wave at the confidence the S0
    report tabulated, so the two new rules are additive rather than a
    behaviour change wearing a disclosure's clothes.

    It is also the contrast the ``earlier_arrival_*`` fields are documented
    against: same speaker, same program, same window — four of these ten
    carry a below-window arrival at 145.8 us, all of them 14.7-15.7 dB down,
    against the ground plane's 125-146 us at 0.6-2.6 dB down. One changed
    mic mounting is the whole difference, and it is the difference between a
    reading and a refusal.
    """
    # Per-position (tau, confidence) as s0-analysis/REPORT.md Q1, with the two
    # entries the 2026-07-27 alignment fix moved.
    #
    # RE-PINNED 2026-07-27 (flow-simplification PR-U1). This reader used to
    # locate an archived capture by cross-correlating the WHOLE composed
    # program against it, which made the deconvolution window depend on every
    # segment's placement rather than on the sweep it actually deconvolves —
    # see ``_flat_lin_corpus.sweep_anchor``. Anchoring on the sweep changed
    # exactly two of these ten positions, and BOTH moved toward a stronger
    # detection rather than a weaker one:
    #
    #   cloud_04  319.3 us @ 0.294  ->  315.7 us @ 0.949
    #   cloud_09  322.6 us @ 0.851  ->  333.4 us @ 0.852
    #
    # cloud_04 was the single weakest reading in the S0 report's own table —
    # low enough that ``test_interference_nulls`` pinned it as sitting BELOW
    # the corroboration confidence floor. Re-aligned it corroborates like its
    # neighbours. The eight unchanged positions are the control: a reader that
    # had simply started reading something else would not have left them
    # bit-identical.
    expected = {
        "cloud_01": (310.4, 0.919), "cloud_02": (327.1, 0.893),
        "cloud_03": (328.6, 0.877), "cloud_04": (315.7, 0.949),
        "cloud_05": (318.6, 0.956), "cloud_06": (321.9, 0.938),
        "cloud_07": (323.5, 0.926), "cloud_08": (317.8, 0.899),
        "cloud_09": (333.4, 0.852), "cloud_10": (321.0, 0.961),
    }
    assert set(main_leg_irs) == set(expected)

    with_earlier = {}
    for position, (tau_us, confidence) in expected.items():
        found = detect_echo(
            main_leg_irs[position], SAMPLE_RATE, search_us=S0_PROTOCOL_SEARCH_US
        )
        assert found.refusal == "", (position, found)
        assert found.tau_us == pytest.approx(tau_us, abs=0.5), position
        assert found.confidence == pytest.approx(confidence, abs=0.01), position
        assert found.effective_floor_us == pytest.approx(221.43, abs=0.01), position
        if found.earlier_arrival_us > 0.0:
            with_earlier[position] = (found.earlier_arrival_us, found.earlier_arrival_db)

    # RE-PINNED 2026-07-27 with the alignment fix above: three of these ten,
    # not four. The one that left the set is cloud_04 — the position whose
    # whole-program-aligned read was the table's weakest (confidence 0.294),
    # and whose sweep-aligned read no longer carries a below-window arrival at
    # all. The remaining three are unchanged to the sample.
    assert len(with_earlier) == 3, with_earlier
    assert all(us == pytest.approx(145.8, abs=0.5) for us, _db in with_earlier.values())
    levels = [db for _us, db in with_earlier.values()]
    assert min(levels) == pytest.approx(-15.71, abs=0.3), with_earlier
    assert max(levels) == pytest.approx(-14.75, abs=0.3), with_earlier
    # The contrast that matters: a desk-cloud interloper is ~12 dB quieter
    # than a proud-capsule one, and none of these ten is refused for it.
    assert max(levels) < -10.0, with_earlier


@requires_s0
def test_the_report_s_deficit_decomposes_into_the_shipped_statistic(loopback_irs):
    """S0-1 — 49.7 dB and 40.4 dB are the same signal, three metric changes
    apart, and the difference is exact rather than hand-waved.

    ``BAND_BELOW_PASSBAND_MARGIN_DB`` has to explain why it does not quote
    the number the loopback report quotes. Enumerating *most* of the reasons
    would leave a reader landing on an intermediate value and wondering what
    the remainder was, so the whole chain is re-derived here on the report's
    own IR.
    """
    ir = loopback_irs["sweep_woofer"]

    def level_db(spectrum, freqs, lo, hi, *, power):
        band = (freqs >= lo) & (freqs <= hi)
        if power:
            return 10.0 * np.log10(np.mean(np.abs(spectrum[band]) ** 2))
        return 20.0 * np.log10(np.mean(np.abs(spectrum[band])))

    # The report's frame: whole-IR spectrum, amplitude mean, 200-1500 Hz.
    n_fft = 1 << 16
    whole = np.abs(np.fft.rfft(ir, n_fft))
    whole_freqs = np.fft.rfftfreq(n_fft, 1.0 / SAMPLE_RATE)

    def whole_ir_deficit(lo, hi, *, power):
        return level_db(whole, whole_freqs, lo, hi, power=power) - level_db(
            whole, whole_freqs, 5000.0, 19_000.0, power=power
        )

    assert whole_ir_deficit(200.0, 1500.0, power=False) == pytest.approx(49.66, abs=0.05)
    # 1. amplitude mean -> power mean.
    assert whole_ir_deficit(200.0, 1500.0, power=True) == pytest.approx(42.97, abs=0.05)

    # 2. whole IR -> the early-arrival windowed segment the gate reads.
    peak = int(np.argmax(np.abs(ir)))
    pre = int(round(spatial_combine.ECHO_WINDOW_PRE_S * SAMPLE_RATE))
    span = int(
        round(
            spatial_combine.ECHO_WINDOW_SPAN_FACTOR
            * DEFAULT_ECHO_SEARCH_US[1]
            * 1e-6
            * SAMPLE_RATE
        )
    )
    segment = ir[max(0, peak - pre) : min(ir.size, peak + span)]
    seg_fft = spatial_combine._n_fft_for(segment.size)
    seg_spectrum = np.abs(np.fft.rfft(segment, seg_fft)) + 1e-12
    seg_freqs = np.fft.rfftfreq(seg_fft, 1.0 / SAMPLE_RATE)

    def segment_deficit(lo, hi):
        return level_db(seg_spectrum, seg_freqs, lo, hi, power=True) - level_db(
            seg_spectrum, seg_freqs, 5000.0, 19_000.0, power=True
        )

    assert segment_deficit(200.0, 1500.0) == pytest.approx(40.83, abs=0.05)
    # 3. the report's 200-1500 Hz passband -> the one the gate is given.
    assert segment_deficit(*LOOPBACK_WOOFER_PASSBAND_HZ) == pytest.approx(40.43, abs=0.05)

    # ...which is exactly what the shipped statistic reports.
    shipped = detect_echo(ir, SAMPLE_RATE, signal_band_hz=LOOPBACK_WOOFER_PASSBAND_HZ)
    assert shipped.band_deficit_db == pytest.approx(
        segment_deficit(*LOOPBACK_WOOFER_PASSBAND_HZ), abs=1e-9
    )


@requires_s0
@requires_corpus
def test_band_deficit_separates_honest_captures_from_stopband_residue(
    ground_plane_irs, main_leg_irs, corpus_irs, loopback_irs
):
    """S0-1 — the gap ``BAND_BELOW_PASSBAND_MARGIN_DB`` claims, re-derived.

    Three populations at the shipped defaults (5-19 kHz band, (120, 800) us
    window), each against its own declared passband. The constant's comment
    quotes this measurement; this test is where the quote comes from, so it
    cannot rot into prose nobody re-ran.

    The ground-plane leg is the honest ceiling on purpose: tipping the
    cabinet at the floor cost top-octave level, so those are the honest
    captures that come closest to looking like residue — and they are still
    13 dB clear of the threshold.

    **RE-DERIVATION PROCEDURE.** Every number below is a reading off a fixed,
    archived corpus through the shipped detector, so it moves for exactly two
    reasons and they are not treated alike. Written down 2026-07-29 (#1879)
    because it was not, and the first drift found no rule to follow.

    1. Reproduce. Export ``JTS_FLAT_LIN_S0`` / ``JTS_FLAT_LIN_CORPUS`` and run
       this file serially. Without them the whole lane SKIPS, which is why CI
       is not evidence about any pin here.
    2. Classify before touching anything, because only one of these two
       licenses a new number:

       * **The detector changed.** ``detect_echo``'s band screen, its window
         contract, or a shipped default moved, and the corpus is being read
         the same way it always was. The reading is honest and the pin is
         re-derived against the tree it ships on.
       * **The reading changed.** The IRs themselves moved — the loader,
         the program composer, the calibration parse, or the deconvolution.
         Then the pin is NOT the thing that is wrong. Find what moved the
         IR and fix that; the pin comes back on its own.

       The discriminator is cheap: the pins are readings off ONE detector
       over THREE independently-captured populations (S0 ground-plane, S0
       main leg, cdhorn). A detector change moves all three coherently. A
       reading change moves whichever populations share the broken input and
       leaves the others bit-identical, which is the signature to look for.
    3. Bisect to the commit, and state the mechanism in the commit message.
       "It drifted" is not a located cause; the released band edge here is
       ±0.3 dB and nothing should reach it by accident.
    4. Only then edit, and record the era: what moved the number, which
       commit/issue, and the previous value. Never widen a tolerance to
       admit a number you have not explained.

    Worked example, and the reason step 2 exists: on 2026-07-28 #1816's
    beeps-first reorder moved ``corpus_run5_verify`` 5.514 -> 5.133 and
    ``corpus_run7_verify`` 1.537 -> 1.384 while every S0 population stayed
    bit-identical. That split is case two — the corpus loader was reading a
    2026-07-24 capture through a 2026-07-28 program layout (see
    :func:`corpus_irs`). Repairing the registration restored both readings to
    ten significant figures, so **no pin below was adjusted**. Had the pins
    been nudged to 5.13/1.38 instead, the tree would have kept a green test
    over a corpus it was misreading by a full second.
    """
    honest = {
        **{f"gp_{k}": (ir, S0_SUMMED_PASSBAND_HZ) for k, ir in ground_plane_irs.items()},
        **{f"main_{k}": (ir, S0_SUMMED_PASSBAND_HZ) for k, ir in main_leg_irs.items()},
        "corpus_run5_verify": (corpus_irs["run5_verify"], S0_SUMMED_PASSBAND_HZ),
        "corpus_run7_verify": (corpus_irs["run7_verify"], S0_SUMMED_PASSBAND_HZ),
        "corpus_run7_tweeter": (corpus_irs["run7_tweeter"], (2000.0, 20_000.0)),
        **{
            f"loopback_{s}_tweeter": (
                loopback_irs[f"{s}_tweeter"],
                LOOPBACK_TWEETER_PASSBAND_HZ,
            )
            for s in ("impulse", "sweep", "mls")
        },
    }
    residue = {
        f"loopback_{s}_woofer": (
            loopback_irs[f"{s}_woofer"],
            LOOPBACK_WOOFER_PASSBAND_HZ,
        )
        for s in ("impulse", "sweep", "mls")
    }

    def deficits(population):
        return {
            name: detect_echo(ir, SAMPLE_RATE, signal_band_hz=band).band_deficit_db
            for name, (ir, band) in population.items()
        }

    honest_db = deficits(honest)
    residue_db = deficits(residue)
    ceiling, floor = max(honest_db.values()), min(residue_db.values())

    assert ceiling < BAND_BELOW_PASSBAND_MARGIN_DB < floor, (honest_db, residue_db)
    # The comment claims 12.07 dB and 40.43 dB, i.e. 12.9 dB of headroom
    # above and 15.4 dB below. Asserting 10 dB of clearance on each side
    # makes this a tripwire on the gap closing rather than a restatement.
    assert BAND_BELOW_PASSBAND_MARGIN_DB - ceiling > 10.0, honest_db
    assert floor - BAND_BELOW_PASSBAND_MARGIN_DB > 10.0, residue_db
    assert ceiling == pytest.approx(12.07, abs=1.0), honest_db
    assert floor == pytest.approx(40.43, abs=1.5), residue_db
    # The ground-plane leg really is the honest worst case, which is why it
    # is the population's ceiling rather than a bystander in it.
    assert max(honest_db, key=honest_db.get).startswith("gp_"), honest_db
    # The comment's per-population figures, so each is re-derived and not
    # just the aggregate gap.
    main_db = [v for k, v in honest_db.items() if k.startswith("main_")]
    assert (min(main_db), max(main_db)) == pytest.approx((1.04, 6.56), abs=0.3), main_db
    gp_db = [v for k, v in honest_db.items() if k.startswith("gp_")]
    assert (min(gp_db), max(gp_db)) == pytest.approx((8.28, 12.07), abs=0.3), gp_db
    assert (
        honest_db["corpus_run7_tweeter"],
        honest_db["corpus_run7_verify"],
        honest_db["corpus_run5_verify"],
    ) == pytest.approx((1.11, 1.54, 5.51), abs=0.3), honest_db

    # The **acoustic** subset is the 16 records ``band_deficit_db``'s own
    # docstring quotes a range for; the electrical in-band controls are a
    # separate population and read either side of zero, so they are excluded
    # here and asserted on their own below. Conflating the two is how a
    # quoted minimum drifts away from the population it describes.
    acoustic_db = {
        name: value
        for name, value in honest_db.items()
        if not name.startswith("loopback_")
    }
    assert len(acoustic_db) == 16, sorted(acoustic_db)
    assert min(acoustic_db, key=acoustic_db.get) == "main_cloud_06", acoustic_db
    assert min(acoustic_db.values()) == pytest.approx(1.04, abs=0.3), acoustic_db
    assert max(acoustic_db.values()) == pytest.approx(12.07, abs=0.3), acoustic_db

    control_db = [v for k, v in honest_db.items() if k.startswith("loopback_")]
    assert len(control_db) == 3
    assert (min(control_db), max(control_db)) == pytest.approx((-0.17, -0.05), abs=0.1)


@requires_s0
def test_ground_plane_positions_report_the_proud_capsule_arrival(ground_plane_irs):
    """S0-2 acceptance — the three records that used to collapse silently.

    S0's leg B put the speaker on a hard floor with the mic "lying on" it,
    which left the capsule centimetres proud of the boundary and
    manufactured a new dominant arrival at 125-146 us — 4.3-5.0 cm of path,
    r = 0.74-0.93 (s0-analysis/REPORT.md, "Where the extra energy comes
    from"). Searched in the leg-B protocol window (150, 1000) us, that
    arrival became the envelope's answer, the window contract rejected it,
    and all three positions reported ``confidence = 0.000`` with an **empty
    refusal** while the cepstrum was reading the real ~320 us comb at
    327.2 / 270.2 / 342.8 us.

    All three now refuse by name and carry the interloper. Note what has
    *not* changed: every estimator field below is exactly what the S0
    report tabulated, because the fix names the outcome rather than
    re-estimating anything.

    **Which name, since #1750, depends on the window's sample alignment, and
    both halves are asserted here.** Unifying the envelope's index bounds on
    ``ceil`` stopped the estimator selecting a sample below ``search_us[0]``,
    so it reaches below the window only by parabolic refinement — at most
    half a sample past ``first``.

    * Through the leg-B protocol window (150, 1000) us, whose lower edge is
      7.2 samples at 48 kHz, that is not enough: all three envelope answers
      land at 156.25 us, inside the window and inside its edge margin, and
      ``tau_at_window_lower_edge`` returns before the dominance rule is
      reached. Their pre-#1750 answers were 137.5 / 139.4 / 135.4 us, each
      the *same envelope sample* as the interloper the window excluded — the
      half-sample leak this fix closed.
    * Through the sample-aligned (166.6667, 1000) us — 8 samples, the
      control the module's ``earlier_arrival_us`` docstring names — it is,
      and all three records are **bit-identical to the pre-#1750 detector**,
      ``earlier_dominant_arrival`` included.

    What does not move either way, and is the acceptance that matters: no
    position reports a delay, none reports confidence, and every one carries
    the interloper at the delay and level the S0 report tabulated. A
    household is told the same thing about the same arrival in both cases.
    """
    expected = {
        # position -> (cepstral tau, interloper tau, its level)
        "ground_plane_01": (327.2, 145.8, -2.57),
        "ground_plane_02": (270.2, 145.8, -2.01),
        "ground_plane_03": (342.8, 125.0, -0.64),
    }
    assert set(ground_plane_irs) == set(expected)

    # The sample-aligned lower edge, in us: 8 samples at 48 kHz, written as
    # the literal four decimals a caller would — which is #1750's own case,
    # 8.0000016 samples. Deriving it as ``8.0 / SAMPLE_RATE * 1e6`` instead
    # gives 166.66666666666666, which round-trips to *exactly* 8.0 and makes
    # ``WINDOW_EDGE_SNAP_SAMPLES`` a no-op here — the assertions below would
    # then hold with the snap removed, which is not what this control is for.
    aligned_search_us = (166.6667, S0_PROTOCOL_SEARCH_US[1])

    for position, (ceps, early_us, early_db) in expected.items():
        protocol = detect_echo(
            ground_plane_irs[position],
            SAMPLE_RATE,
            search_us=S0_PROTOCOL_SEARCH_US,
        )
        aligned = detect_echo(
            ground_plane_irs[position], SAMPLE_RATE, search_us=aligned_search_us
        )

        # Both windows: a refusal, no delay, no confidence.
        assert protocol.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, (position, protocol)
        assert aligned.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, (position, aligned)
        for found in (protocol, aligned):
            assert found.tau_us == 0.0, position
            assert found.confidence == 0.0, position

            # The S0 report's own numbers, unchanged.
            assert found.tau_cepstral_us == pytest.approx(ceps, abs=0.5), position
            # The envelope now rails one sample up, at the first in-window
            # sample less the parabola's clamp, on both windows.
            assert found.tau_envelope_us == pytest.approx(156.25, abs=0.5), position

            # ...and the interloper the refusal is named for, which neither
            # the boundary fix nor the choice of window moved.
            assert found.earlier_arrival_us == pytest.approx(early_us, abs=0.5), position
            assert found.earlier_arrival_db == pytest.approx(early_db, abs=0.2), position
            # It is the loudest thing any of these records saw: louder than
            # anything the window contained, which is why it took the answer.
            assert found.earlier_arrival_db > -3.0, position

        # The dominance rule really is reached at the aligned window and
        # really is pre-empted at the protocol one — not merely a different
        # answer, but the documented refusal precedence doing its job.
        assert aligned.corroboration == 1.0, position
        assert protocol.corroboration < 1.0, position


@requires_s0
def test_ground_plane_arrivals_sit_under_the_default_window_floor(ground_plane_irs):
    """S0-4 acceptance — why the floor is *surfaced* rather than raised.

    Through the module's own default window the same three captures are
    edge-refused, which is honest but silent about the reason: the
    proud-capsule arrivals at 125-146 us sit under the default window's
    ~191.4 us effective floor, so that window structurally cannot report
    them. ``effective_floor_us`` is what lets a consumer say so.

    This is also the direct evidence for the plan's PR-2 item 4 decision —
    raising the default lower edge would have hidden these captures further,
    not helped: the offline envelope scan in the S0 report is what found the
    arrivals, and a higher floor moves *away* from that.

    The arrivals' own delays are **measured**, not asserted against
    literals: read them from the same IRs through the protocol window that
    can see them, then check they fall under the default window's floor.
    Comparing hardcoded 125/146 against an already-pinned 191.43 would have
    been arithmetic, not a test.
    """
    for position, ir in sorted(ground_plane_irs.items()):
        found = detect_echo(ir, SAMPLE_RATE)
        assert found.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, (position, found)
        assert found.effective_floor_us == pytest.approx(191.43, abs=0.01), position
        # Inside the default window the interloper is not "below" anything,
        # so there is nothing to name and the field says so.
        assert found.earlier_arrival_us == 0.0, position

        # The claim the field exists to let a consumer make, against this
        # capture's own measured arrival: it is real, and this window
        # structurally cannot report it.
        through_protocol_window = detect_echo(
            ir, SAMPLE_RATE, search_us=S0_PROTOCOL_SEARCH_US
        )
        arrival_us = through_protocol_window.earlier_arrival_us
        assert arrival_us > 0.0, position
        assert arrival_us < found.effective_floor_us, (position, arrival_us)


@requires_s0
def test_ground_plane_cloud_reads_as_thin_evidence_free(ground_plane_irs):
    """S0-3 — the three-position leg is *not* thin evidence, and the rule
    says why rather than the reader guessing.

    ``thin_evidence`` asks whether a cloud that supplied plenty of positions
    produced only the bare minimum of usable estimates. Leg B supplied
    three, all refused, so the verdict is ``GEOMETRY_UNKNOWN`` on zero
    usable estimates — a shortfall of evidence, but not the *disproportion*
    this flag reports. Pinning it here keeps the flag from being read as a
    general "not much evidence" indicator.
    """
    captures = []
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    for position, ir in sorted(ground_plane_irs.items()):
        spectrum = np.abs(np.fft.rfft(ir[:N_FFT], N_FFT)) + 1e-12
        captures.append(
            PositionCapture(
                position_id=position,
                freqs_hz=freqs,
                magnitude_db=20.0 * np.log10(spectrum),
                sample_rate=SAMPLE_RATE,
                ir=ir,
            )
        )

    combined = combine_positions(captures, echo_search_us=S0_PROTOCOL_SEARCH_US)
    assert combined.n_positions == 3
    assert combined.geometry.reason == GEOMETRY_UNKNOWN
    assert combined.geometry.n_confident == 0
    assert combined.geometry.locked is False
    assert combined.geometry.thin_evidence is False
    # Every position refused by name — the acceptance the plan asked for:
    # informative on all three, never a silent confidence collapse. Since
    # #1750 the name at this (not sample-aligned) window is the edge rule
    # rather than the dominance rule; which of the two it is belongs to
    # :func:`test_ground_plane_positions_report_the_proud_capsule_arrival`,
    # which asserts both windows. What this test needs is only that no
    # position fell back to the silent empty refusal.
    assert all(
        e is not None and e.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE
        for e in combined.per_position_echo
    ), [e.refusal for e in combined.per_position_echo if e is not None]


# --------------------------------------------------------------------------- #
# Per-position residual — the design brief's §4.2 trend surface
# --------------------------------------------------------------------------- #


def _roled(captures, roles):
    """The same captures, each carrying a role. One line, like the flow's."""
    return [
        dataclasses.replace(capture, role=role)
        for capture, role in zip(captures, roles, strict=True)
    ]


def test_the_role_rides_through_the_combination_without_touching_it():
    """Carried, never read: the reduction must be byte-identical either way.

    The one property that makes the role safe to add — it labels a number, it
    does not weight one. A combiner that started consulting roles would be a
    weighted combiner, which the brief's cut list forbids explicitly.
    """
    _freqs, _true, captures = _cloud(_dispersed_taus(4))

    plain = combine_positions(captures)
    roled = combine_positions(_roled(captures, ["onax", "offax", "xovr", ""]))

    assert roled.position_roles == ("onax", "offax", "xovr", "")
    assert plain.position_roles == ("", "", "", "")
    np.testing.assert_array_equal(roled.power_mean_db, plain.power_mean_db)
    np.testing.assert_array_equal(roled.median_db, plain.median_db)
    np.testing.assert_array_equal(roled.excluded, plain.excluded)


def test_a_position_that_sat_off_the_mean_reports_a_bigger_residual():
    """The whole point of the number, as an ordering rather than a constant.

    An absolute dB target here would pin the synthetic fixture rather than the
    estimator. What has to hold is the DISCRIMINATION: the position pushed
    away from the group reports a larger residual than the ones that were not,
    and the role travels with it so a reader can say WHICH position.
    """
    freqs, _true, captures = _cloud(_dispersed_taus(4))
    captures = _roled(captures, ["onax", "offax", "offax", "xovr"])
    # One position 4 dB hot across the whole band — a broadband level offset,
    # which is exactly the anchor-outlier signature this surface exists to see.
    captures[1] = dataclasses.replace(
        captures[1], magnitude_db=captures[1].magnitude_db + 4.0,
    )

    rows = position_residuals(combine_positions(captures))

    assert [row.position_id for row in rows] == ["p0", "p1", "p2", "p3"]
    assert [row.role for row in rows] == ["onax", "offax", "offax", "xovr"]
    offset = next(row for row in rows if row.position_id == "p1")
    others = [row for row in rows if row.position_id != "p1"]
    assert offset.rms_db is not None
    assert all(row.rms_db is not None for row in others)
    assert all(offset.rms_db > row.rms_db for row in others), (
        [(row.position_id, row.rms_db) for row in rows]
    )
    assert all(row.n_bins > 0 for row in rows)


def test_the_residual_band_is_the_callers_and_narrows_what_is_counted():
    """The trusted band is a session fact, so the module takes it rather than
    inventing one. A narrower band must grade fewer bins — otherwise the
    argument is decorative."""
    _freqs, _true, captures = _cloud(_dispersed_taus(3))
    combined = combine_positions(captures)

    whole = position_residuals(combined)
    narrow = position_residuals(combined, band_hz=(500.0, 4000.0))

    assert all(row.n_bins > 0 for row in narrow)
    assert all(
        n.n_bins < w.n_bins for n, w in zip(narrow, whole, strict=True)
    ), [(n.n_bins, w.n_bins) for n, w in zip(narrow, whole, strict=True)]


def test_a_band_that_selects_nothing_reports_absence_not_zero():
    """``0.0 dB`` would read as "this position agreed perfectly" — the exact
    over-claim every other honesty instrument in this module refuses."""
    _freqs, _true, captures = _cloud(_dispersed_taus(2))

    rows = position_residuals(
        combine_positions(captures), band_hz=(1.0e6, 2.0e6),
    )

    assert rows
    assert all(row.rms_db is None and row.n_bins == 0 for row in rows)


def test_a_record_with_no_retained_curves_reports_nothing():
    """A hand-built or deserialised ``CombinedResponse`` has no per-position
    array, and inventing residuals from the mean alone would be fabrication."""
    _freqs, _true, captures = _cloud(_dispersed_taus(3))
    result = combine_positions(captures)
    stripped = dataclasses.replace(
        result, per_position_db=np.empty((0, 0)), per_position_diag_db=np.empty((0, 0)),
    )

    assert position_residuals(stripped) == ()


def test_the_raw_curves_could_not_have_answered_this(monkeypatch):
    """Why the estimator reads the SMOOTHED pair, kept as a measurement.

    The design brief specified ``per_position_db - power_mean_db``. Run that
    way, a broadband +4 dB offset on one position of four does NOT make it the
    worst position: each position's own interference comb contributes ~2.5 dB
    and swamps the level term. This re-measures that on the same fixture the
    test above uses, so the choice stays a finding rather than a preference —
    and so a future edit that quietly swaps the operands back fails here.
    """
    freqs, _true, captures = _cloud(_dispersed_taus(4))
    captures = _roled(captures, ["onax", "offax", "offax", "xovr"])
    captures[1] = dataclasses.replace(
        captures[1], magnitude_db=captures[1].magnitude_db + 4.0,
    )
    combined = combine_positions(captures)

    def _raw_rms(index: int) -> float:
        deviation = (
            np.asarray(combined.per_position_db)[index]
            - np.asarray(combined.power_mean_db)
        )
        keep = ~np.asarray(combined.excluded, dtype=bool)
        return float(np.sqrt(np.mean(deviation[keep] ** 2)))

    raw = [_raw_rms(i) for i in range(combined.n_positions)]
    assert raw[1] < max(raw[0], raw[2], raw[3]), (
        "the raw form is comb-dominated; if this ever stops being true, "
        "revisit the smoothed choice rather than assuming it", raw,
    )
    # …and the shipped estimator, on the same input, does see it.
    rows = position_residuals(combined)
    assert rows[1].rms_db == max(row.rms_db for row in rows)
