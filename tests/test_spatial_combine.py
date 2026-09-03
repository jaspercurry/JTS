# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the spatial combiner + interference honesty screen.

Layers: synthetic ground truth, power-domain arithmetic, the echo detector,
analysis-grid bounding, then two real-data layers that skip unless their
gitignored corpus roots are exported — ``JTS_FLAT_LIN_CORPUS`` (the
2026-07-24/25 JTS3 corpus) and ``JTS_FLAT_LIN_S0`` (the 2026-07-25 S0
session, a different capture protocol).
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


# --- Synthetic corpus construction ---


def _grid() -> np.ndarray:
    return np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)


def _true_response_db(freqs: np.ndarray) -> np.ndarray:
    """A smooth synthetic "true" response, rolled off above 8 kHz."""
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
    """One synthetic capture: truth + seeded noise, combed by one echo.

    Magnitude and IR are built from one complex spectrum, so the IR the
    detector sees is the cause of the comb the combiner sees.
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
    """Stratified delays spanning 150-490 us (~5-17 cm of path delta) with
    seeded jitter.
    """
    rng = np.random.default_rng(seed)
    return np.linspace(150e-6, 490e-6, n) + rng.uniform(-15e-6, 15e-6, n)


def _level_offset_db(
    estimate: np.ndarray, truth: np.ndarray, keep: np.ndarray
) -> float:
    """Mean (estimate - truth) over ``keep`` — the offset
    :func:`_relative_rms_error` removes.
    """
    return float((estimate - truth)[keep].mean())


def _relative_rms_error(
    estimate: np.ndarray, truth: np.ndarray, keep: np.ndarray
) -> float:
    """RMS of (estimate - truth) over ``keep``, common offset removed."""
    delta = (estimate - truth)[keep]
    return float(np.sqrt(np.mean((delta - delta.mean()) ** 2)))


# --- B. Power-domain arithmetic, pinned to hand-computed literals ---


def test_power_mean_is_the_energy_mean_not_the_db_mean():
    """The combiner averages in linear power, never in dB.

    Literals hand-computed as 10*log10((10**(a/10) + 10**(b/10)) / 2) per
    bin, which differs from the naive dB mean by up to 17 dB here.
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

    naive_db_mean = (loud + quiet) / 2.0
    assert not np.allclose(result.power_mean_db, naive_db_mean, atol=0.5)

    # With two positions the median IS the dB mean.
    np.testing.assert_allclose(result.median_db, naive_db_mean, atol=1e-12)
    assert bool(result.excluded[0]), "7.03 dB disagreement must be flagged"
    assert bool(result.excluded[3]), "16.99 dB disagreement must be flagged"
    assert not bool(result.excluded[2]), "identical positions cannot disagree"


def test_power_mean_matches_an_independent_scalar_computation():
    """The vectorised power mean matches plain-Python math on a random set."""
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


# --- A. Synthetic ground truth ---


def test_power_mean_recovers_truth_from_a_dispersed_cloud():
    """A1 — averaging a decorrelated cloud recovers truth, rolloff included."""
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

    top = (freqs >= 9000.0) & (freqs <= 16_000.0) & ~result.excluded
    assert _relative_rms_error(result.power_mean_spec_db,
                               smooth_fractional_octave(freqs, true_db, fraction=3),
                               top) < 1.0


def test_absolute_level_discriminates_the_power_mean_from_max_hold():
    """A1b — absolute level, which the offset-removed RMS metric cannot score.

    The power mean carries the echo's own energy, +10*log10(1 + r**2) =
    +0.529 dB at r=0.36 (measured +0.437 dB here; partial coherence over ten
    stratified delays pulls it under the analytic value). Max-hold, which the
    plan rejects as positively biased, lands +2.55 dB on the same cloud.
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

    assert _relative_rms_error(max_hold_db, true_db, keep) < _relative_rms_error(
        result.power_mean_db, true_db, keep
    )


def test_geometry_lock_is_false_when_delays_are_dispersed():
    """A2a — dispersed delays mean moving nulls, which averaging can fill."""
    _freqs, _true_db, captures = _cloud(_dispersed_taus())
    result = combine_positions(captures)

    assert result.geometry.locked is False
    assert result.geometry.reason == GEOMETRY_DISPERSED
    # 8 of the 10 stratified delays clear the resolution floor (3 * ~71.4 us);
    # the 150 and 185 us members are unresolvable, not undetected.
    assert result.geometry.n_confident == 8
    assert result.geometry.n_positions == 10
    assert result.geometry.clustered_fraction < 0.7


def test_geometry_lock_is_true_when_every_delay_is_identical():
    """A2b — a cloud that did not move has position-stable nulls."""
    _freqs, _true_db, captures = _cloud(np.full(10, 300e-6))
    result = combine_positions(captures)

    assert result.geometry.locked is True
    assert result.geometry.reason == GEOMETRY_LOCKED
    assert result.geometry.clustered_fraction == pytest.approx(1.0)
    assert result.geometry.median_tau_us == pytest.approx(300.0, rel=0.1)


def test_aligned_nulls_survive_the_average_which_is_why_the_flag_exists():
    """A3 — aligned nulls survive the average and the mean-vs-median screen
    is blind to them, so ``geometry.locked`` is the only warning.
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

    band = (freqs >= 300.0) & (freqs <= 16_000.0)
    keep = band & ~result.excluded
    assert _relative_rms_error(result.power_mean_db, true_db, keep) > 1.5

    assert result.geometry.locked is True


def test_exclusion_mask_and_merged_intervals_agree():
    """The exclusion mask and the reported (f_lo, f_hi) intervals are one fact."""
    _freqs, _true_db, captures = _cloud(_dispersed_taus())
    # A partially-aligned cloud is what trips the screen: half the positions
    # nulled at a bin, so the power mean and the median part company.
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
    """The two spread statistics answer two questions; only one discriminates.

    ``max_sigma_db`` (worst single bin) separates a moved cloud from a
    stationary one. ``sigma_db`` (per-position band level) collapses in the
    top octaves, where an octave spans many comb periods and every position
    lands near ``1 + r**2``, but not at 1-2 kHz, where tau of 150-490 us puts
    the comb period (2-6.7 kHz) wider than the band.
    """
    _f1, _t1, dispersed = _cloud(_dispersed_taus())
    _f2, _t2, tight = _cloud(np.full(10, 300e-6))

    bands_dispersed = {b.center_hz: b for b in combine_positions(dispersed).band_spread}
    bands_tight = {b.center_hz: b for b in combine_positions(tight).band_spread}
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
    exactly one :class:`BandSpread` is reported over seven bins.
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

    p1_band_level_db = 10.0 * math.log10((6.0 * 1.0 + 10.0 ** (-10.0 / 10.0)) / 7.0)
    expected_sigma_db = abs(0.0 - p1_band_level_db) / math.sqrt(2.0)
    assert band.sigma_db == pytest.approx(expected_sigma_db, abs=1e-12)
    assert band.sigma_db == pytest.approx(0.42262503, abs=1e-8)
    assert band.max_sigma_db == pytest.approx(10.0 / math.sqrt(2.0), abs=1e-12)


def test_single_capture_combines_to_itself_and_reports_no_spread():
    """N=1 is legal; spread is undefined, not zero."""
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


# --- A2. Retained per-position curves ---


def test_per_position_curves_are_retained_index_aligned_and_immutable():
    """Retained rows are index-aligned with ``position_ids``, on the result's
    own grid, and read-only.
    """
    _freqs, _true, captures = _cloud(_dispersed_taus(4))
    result = combine_positions(captures)

    assert result.per_position_db.shape == (4, result.freqs_hz.size)
    assert result.per_position_diag_db.shape == result.per_position_db.shape
    assert result.position_ids == tuple(c.position_id for c in captures)
    assert not result.per_position_db.flags.writeable
    assert not result.per_position_diag_db.flags.writeable

    # These captures share one grid, so the resample is the identity.
    for row, capture in zip(result.per_position_db, captures, strict=True):
        np.testing.assert_allclose(row, capture.magnitude_db, atol=1e-9)


def test_the_retained_curves_are_what_the_combined_ones_are_made_of():
    """The combined curves are recomputable from ``per_position_db``."""
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
    """Smoothing costs three combined passes plus one per position."""
    calls: list[int] = []
    real = spatial_combine.smooth_fractional_octave

    def counting(freqs, values, *, fraction):
        calls.append(fraction)
        return real(freqs, values, fraction=fraction)

    monkeypatch.setattr(spatial_combine, "smooth_fractional_octave", counting)
    _freqs, _true, captures = _cloud(_dispersed_taus(n_positions))
    result = combine_positions(captures)

    assert len(calls) == 3 + n_positions
    assert calls.count(result.spec_fraction) == 1
    assert calls.count(result.diag_fraction) == 2 + n_positions


def test_the_retained_curves_follow_the_decimated_grid():
    """Retained rows follow the decimated grid, not the captures' own."""
    captures = _large_grid_cloud()
    result = combine_positions(captures)

    assert captures[0].freqs_hz.size > MAX_ANALYSIS_BINS
    assert result.freqs_hz.size <= MAX_ANALYSIS_BINS
    assert result.per_position_db.shape == (len(captures), result.freqs_hz.size)
    assert result.per_position_diag_db.shape == result.per_position_db.shape


def test_the_per_position_fields_are_additive():
    """``CombinedResponse`` still constructs without the per-position fields."""
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
    """``usable_echo_estimates``'s result size is ``GeometryLock.n_confident``,
    on every population this suite can build.
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
        raised = 0.99
        assert len(
            usable_echo_estimates(echoes, confidence_floor=raised)
        ) == assess_geometry(echoes, confidence_floor=raised).n_confident

    # A refused record is not evidence, however confident its raw fields.
    assert refused.refusal != ""
    assert usable_echo_estimates([refused]) == ()


# --- Canonical grid contract ---


def test_canonical_grid_is_the_identity_for_captures_sharing_one_grid():
    """The ordinary case — one program, one rfftfreq — must not resample."""
    freqs, _true_db, captures = _cloud(_dispersed_taus(4))
    result = combine_positions(captures)
    np.testing.assert_allclose(result.freqs_hz, freqs, rtol=0, atol=0)


def test_canonical_grid_takes_the_coarsest_spacing_over_common_support():
    """The canonical grid takes the coarsest spacing over common support."""
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
    """A log grid is rejected: the smoother binary-searches linear bins."""
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
        # Malformed by SHAPE — not a pair at all.
        ({"echo_search_us": (120.0,)}, "echo_search_us"),
        ({"echo_search_us": None}, "echo_search_us"),
        ({"echo_search_us": (120.0, 800.0, 900.0)}, "echo_search_us"),
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
    """N6 — malformed *config* raises; malformed *data* refuses one position.

    Shape is checked before value, and ``pytest.raises(ValueError)`` catches
    neither ``IndexError`` nor ``TypeError``, so the shape rows discriminate
    rather than passing incidentally.
    """
    _freqs, _true_db, captures = _cloud(_dispersed_taus(3))
    with pytest.raises(ValueError, match=match):
        combine_positions(captures, **kwargs)


def test_a_band_above_one_captures_nyquist_refuses_only_that_position():
    """N6's exception: a band above one capture's Nyquist refuses that
    position rather than failing the combine.
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


# --- C. Echo detector ---


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
    """``_impulse_with_echo`` with a second, later reflection — two
    independent bounces, not a comb and its rahmonic.
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

    The low anchor is 240 us: the window's bottom ``WINDOW_EDGE_MARGIN_STEPS``
    is refused, and the estimators' near-floor under-read stretches that dead
    zone to ~1.3 steps for the weakest echoes.
    """
    result = detect_echo(_impulse_with_echo(tau_us * 1e-6, reflection), SAMPLE_RATE)

    assert result.confidence > ECHO_CONFIDENCE_FLOOR, result
    assert result.tau_us == pytest.approx(tau_us, rel=0.10)
    assert result.strength_db == pytest.approx(20.0 * math.log10(reflection), abs=2.0)
    assert result.arrival_crest_db > 20.0


def test_detect_echo_on_a_band_limited_impulse_response():
    """The echo rides a shaped, rolled-off response rather than a delta pair."""
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
    """A real arrival with no secondary one is refused by concentration x
    corroboration, since the crest gate passes.
    """
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    ir += rng.normal(0.0, 0.02 if seed < 70 else 0.001, ir.size)

    result = detect_echo(ir, SAMPLE_RATE)
    assert result.arrival_crest_db > 20.0, "the arrival itself is real"
    assert result.confidence < ECHO_CONFIDENCE_FLOOR, result


def test_detect_echo_resolution_floor_is_where_the_docstring_says_it_is():
    """The documented accuracy floor, both directions: tau within 3% over
    240-500 us, and the bottom ``WINDOW_EDGE_MARGIN_STEPS`` refused rather
    than under-read.
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

    # The refusal is evidenced: both raw estimates landed within one quefrency
    # step of the 120 us edge (measured 145.2 us and 135.4 us) and the ripple
    # was concentrated — confidence alone could not have caught it.
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
    """The search window is a rejection contract, swept so a clamp fails.

    Eight true delays crossed with eight windows. Four properties: a reported
    tau is inside the requested window; it is never pinned exactly to an edge
    (the anti-clamp assertion); a delay above the window reports 0.0; a delay
    below it is never usable evidence — it aliases up rather than vanishing,
    and a window excluding tau can still contain a rahmonic at 2*tau, 3*tau.
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
    """B1(a) — a true 830 us echo searched to 800 us refuses rather than
    railing: both raw estimates stay unclamped outside the window.
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
    """SF1 — the window's lower edge is refused and the dead zone is one step.

    A below-window echo aliases onto the bottom of the window with both
    estimators agreeing, so refusing is the only honest answer; a genuine echo
    1.5 steps above the edge is still reported. Parametrised over four lower
    edges, so the rule is relative to the requested window.
    """
    resolution_us = 1e6 / 14_000.0  # the 5-19 kHz default band's step
    search_us = (search_lo_us, search_lo_us + 500.0)

    # Below the window entirely — half its lower edge, far enough down that
    # the aliasing is unambiguous.
    below = detect_echo(
        _impulse_with_echo((search_lo_us / 2.0) * 1e-6, ECHO_R),
        SAMPLE_RATE,
        search_us=search_us,
    )
    assert below.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, below
    assert below.tau_us == 0.0
    assert below.confidence == 0.0
    assert below.strength_db == STRENGTH_FLOOR_DB
    # The refusal is evidenced: a surviving candidate was inside the margin.
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

    ``_impulse_with_echo`` rounds a delay in seconds to a sample, which would
    hide the quantity under test; here the caller names the sample.
    """
    rng = np.random.default_rng(seed)
    ir = np.zeros(65_536)
    ir[1000] = 1.0
    for offset, reflection in spikes:
        ir[1000 + offset] += reflection
    return ir + rng.normal(0.0, 1e-4, ir.size)


def test_the_envelope_never_searches_below_the_requested_window():
    """``search_us[0]`` is the floor of the envelope's candidate range.

    Under ``ceil`` the envelope cannot select the sample below the window: at
    (150, 1000) us and 48 kHz it answers from sample 8 (156.25 us after the
    parabola's half-sample clamp), never sample 7 (145.833 us).
    """
    ir = _impulse_with_sample_spikes((7, 0.9))
    search_us = (150.0, 1000.0)
    assert search_us[0] * 1e-6 * SAMPLE_RATE == pytest.approx(7.2), (
        "this test is about a lower edge that lands between samples 7 and 8; "
        "if that is no longer true the case has stopped being the case"
    )

    found = detect_echo(ir, SAMPLE_RATE, search_us=search_us)

    # The floor is sample 8 less the parabola's clamp; below it means the
    # search reached outside the window.
    spike_us = 7.0 / SAMPLE_RATE * 1e6
    floor_us = 7.5 / SAMPLE_RATE * 1e6
    assert found.tau_envelope_us >= floor_us - 1e-9, found
    assert found.tau_envelope_us != pytest.approx(spike_us, abs=1.0), found

    # ...and the excluded sample is reported as a below-window arrival.
    assert found.earlier_arrival_us == pytest.approx(spike_us, abs=1e-9), found


def test_the_below_window_scan_and_the_envelope_bound_share_one_definition():
    """No sample is both the first in-window candidate and a below-window
    arrival.

    Swept over every alignment of the lower edge within a sample, driven from
    a reflection on the boundary sample itself. The sweep sits around sample
    15 (~312 us) because the 5-19 kHz band resolves ~71 us, so a reflection
    one or two samples off the direct arrival raises no local maximum.
    """
    for numerator in range(0, 10):
        # Lower edges at 15.0, 15.1, ... 15.9 samples: every alignment, with
        # 15.0 the sample-aligned control where ``round`` and ``ceil`` agree.
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
        # The envelope's answer and the below-window arrival are not the same
        # sample: the arrival sits on whole sample ``first - 1`` and the
        # envelope's lowest reachable answer is ``first`` less the parabola's
        # clamp, so half a sample is the assertion rather than "one is bigger".
        half_sample_us = 0.5 / SAMPLE_RATE * 1e6
        assert below.tau_envelope_us - below.earlier_arrival_us >= (
            half_sample_us - 1e-9
        ), (where, below)

        # ...and the first sample INSIDE the window is searched.
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
    """``WINDOW_EDGE_SNAP_SAMPLES``: a sample-aligned edge written as a
    decimal still means that sample.

    Eight samples at 48 kHz is 166.6666...us, so a caller writes 166.6667 —
    8.0000016 samples, which a bare ``ceil`` would read as 9 and charge a
    whole sample of window for. The tolerance must stay far below a sample.
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

    # A genuinely fractional edge is NOT snapped: the snap is not a rounding
    # rule.
    assert spatial_combine._ceil_samples(150e-6, SAMPLE_RATE) == 8
    assert spatial_combine._ceil_samples(7.9 / SAMPLE_RATE, SAMPLE_RATE) == 8
    assert spatial_combine._floor_samples(7.9 / SAMPLE_RATE, SAMPLE_RATE) == 7

    # The tolerance is a thousandth of a sample, so a hundredth away from an
    # integer is still honoured as fractional.
    assert spatial_combine.WINDOW_EDGE_SNAP_SAMPLES == 1e-3
    assert spatial_combine._ceil_samples((8.0 + 0.01) / SAMPLE_RATE, SAMPLE_RATE) == 9
    assert spatial_combine._floor_samples((8.0 - 0.01) / SAMPLE_RATE, SAMPLE_RATE) == 7


def test_the_envelope_upper_bound_is_the_last_in_window_sample():
    """The envelope's upper bound is the last sample at or below ``search_hi``.

    (150, 850) us at 48 kHz is 40.8 samples, so a ``round`` bound searched
    sample 41 at 854.2 us, 4.2 us past the window the caller asked for.
    """
    # A lone reflection at sample 41 — outside a window that ends at 850 us.
    ir = _impulse_with_sample_spikes((41, 0.5))
    assert 850.0 * 1e-6 * SAMPLE_RATE == pytest.approx(40.8)

    found = detect_echo(ir, SAMPLE_RATE, search_us=(150.0, 850.0))
    assert spatial_combine._floor_samples(850e-6, SAMPLE_RATE) == 40

    # The ceiling is sample 40 plus the parabola's clamp, not "below sample
    # 41" — the old bound refined sample 41 to 854.15 us.
    assert found.tau_envelope_us <= 40.5 / SAMPLE_RATE * 1e6 + 1e-9, found

    # ...and it is found when the window is widened by one sample.
    widened = detect_echo(
        ir, SAMPLE_RATE, search_us=(150.0, 41.0 / SAMPLE_RATE * 1e6)
    )
    assert widened.tau_envelope_us == pytest.approx(
        41.0 / SAMPLE_RATE * 1e6, abs=0.5
    ), widened


_BELOW_WINDOW_CLOUD_US = (150.0, 400.0)

# The measured verdict for a 10-position cloud whose true delays span
# _BELOW_WINDOW_CLOUD_US, searched in windows at or above it. Rows are
# (search window, locked, reason, n_confident, rahmonic refusals).
#
# The first three rows are the edge rule; the last three are the regime it
# cannot reach — a cepstral rahmonic of the excluded echo landing mid-window.
# The last column pins WHICH rule declined, so a row asserts the screen
# rather than emptiness.
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
    """One below-window cloud swept across six raised windows: no row reaches
    a cluster, and rows 4-6 are the rahmonic regime the edge rule cannot
    reach.
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
    # GEOMETRY_UNKNOWN is only honest while the usable set cannot cluster.
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
    # Each refusal is recomputable from the two fields the record carries.
    for echo in rahmonic:
        assert echo.lower_peak_ratio > RAHMONIC_MARGIN, echo
        assert 0.0 < echo.lower_peak_us < echo.tau_cepstral_us, echo


def test_a_below_window_cloud_is_edge_refused_rather_than_clustered():
    """SF1 — a below-window cloud at (300, 800) is edge-refused, not clustered.

    A below-window echo is aliased rather than rejected, arriving as a
    plausible in-window estimate: 150 us and 178 us echoes were reported as
    318 us and 302 us. A resolution floor cannot catch that (318 us clears a
    214 us floor); only the distance to the edge distinguishes the two cases.
    """
    taus_s = np.linspace(150e-6, 400e-6, 10)
    # At (300, 800) exactly one position survives — the 400 us member, the
    # only one whose both estimates cleared the margin — and one estimate is
    # not a cluster.
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
    # Every refusal is a rescued false positive.
    for echo in edge_refused:
        assert 300.0 <= echo.tau_envelope_us <= 800.0 or (
            300.0 <= echo.tau_cepstral_us <= 800.0
        ), echo

    # The same cloud in the window it belongs in reads correctly.
    _freqs, _true_db, captures = _cloud(taus_s)
    honest = combine_positions(captures)
    assert honest.geometry.locked is False
    assert honest.geometry.reason == GEOMETRY_DISPERSED
    assert honest.geometry.n_confident >= 7


def test_rahmonic_false_lock_under_a_raised_window_is_screened():
    """A rahmonic false lock under a raised window is screened.

    A comb's cepstrum repeats at 2*tau, 3*tau..., so a window excluding the
    true delay can contain a rahmonic of it. Unlike aliasing it lands
    anywhere in the window, so ``WINDOW_EDGE_MARGIN_STEPS`` cannot catch it:
    the 150-400 us cloud searched at (700, 1000) used to lock at ~857 us.
    The mechanism is asserted alongside the verdict, since a verdict-only
    test would also pass if the detector had simply stopped working.
    """
    true_taus_us = np.linspace(150.0, 400.0, 10)
    _freqs, _true_db, captures = _cloud(true_taus_us * 1e-6)
    result = combine_positions(captures, echo_search_us=(700.0, 1000.0))

    assert result.geometry.locked is False, result.geometry
    assert result.geometry.reason == GEOMETRY_UNKNOWN
    assert result.geometry.n_confident == 0
    assert result.geometry.median_tau_us == 0.0
    assert not any(
        echo is not None and echo.refusal == "" and echo.confidence > 0.0
        for echo in result.per_position_echo
    ), [e.refusal for e in result.per_position_echo if e is not None]

    # --- The mechanism, so a regression is diagnosable rather than red. ---
    rahmonic = [
        (true_us, echo)
        for true_us, echo in zip(true_taus_us, result.per_position_echo, strict=True)
        if echo is not None and echo.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY
    ]
    # The two positions that used to be admitted are the two whose cepstral
    # candidate is the third rahmonic.
    third = [
        (true_us, echo)
        for true_us, echo in rahmonic
        if echo.tau_cepstral_us / true_us == pytest.approx(3.0, abs=0.05)
    ]
    assert len(third) == 2, [(t, e.tau_cepstral_us / t) for t, e in rahmonic]
    for true_us, echo in third:
        # The envelope still corroborates the rahmonic: the screen works by
        # noticing the fundamental, not by breaking corroboration.
        assert echo.corroboration < CORROBORATION_LOOSE, echo
        # The refusing peak is the excluded echo: within half a quefrency step
        # of this position's true delay.
        assert abs(echo.lower_peak_us - true_us) < 0.5 * echo.resolution_us, (
            true_us,
            echo,
        )
        assert echo.lower_peak_ratio > 20.0, echo
        assert echo.tau_us == 0.0
        assert echo.confidence == 0.0


def test_rahmonic_screen_refuses_a_below_window_echo_and_names_its_fundamental():
    """The rahmonic screen on one IR, both directions.

    Fires: a 300 us echo searched in (700, 1000) — before the screen the
    detector reported 876.1 us at confidence 1.000 — and the refusal names the
    fundamental at 285.6 us. Does not fire: the same echo in the default
    window, and a lone 850 us echo inside a raised (750, 1100) window, so the
    screen keys on "is something below stronger", not on "is the window high".
    """
    fired = detect_echo(
        _impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE, search_us=(700.0, 1000.0)
    )
    assert fired.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, fired
    assert fired.tau_us == 0.0
    assert fired.confidence == 0.0
    assert fired.strength_db == STRENGTH_FLOOR_DB

    # The candidate was the third rahmonic and the two estimators agreed.
    assert fired.tau_cepstral_us / 300.0 == pytest.approx(3.0, abs=0.15), fired
    assert fired.corroboration < CORROBORATION_LOOSE, fired
    # The evidence the refusal carries, so the verdict is recomputable.
    assert abs(fired.lower_peak_us - 300.0) < 0.5 * fired.resolution_us, fired
    assert fired.lower_peak_ratio > RAHMONIC_MARGIN, fired
    assert fired.lower_peak_ratio > 20.0, fired

    # Same echo, honest window: measured, and nowhere near the screen.
    honest = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE)
    assert honest.refusal == "", honest
    assert honest.tau_us == pytest.approx(300.0, rel=0.05)
    assert honest.lower_peak_ratio < RAHMONIC_MARGIN, honest
    # ...and the screen was awake while not firing: a raised
    # ``RAHMONIC_FLOOR_STEPS`` would leave no analyzable region below a
    # default-window candidate and degrade the rule to "no region, no opinion".
    assert honest.lower_peak_us > 0.0, honest
    assert honest.lower_peak_ratio > 0.0, honest

    # A genuinely late echo inside a raised window is still measured.
    late = detect_echo(
        _impulse_with_echo(850e-6, ECHO_R), SAMPLE_RATE, search_us=(750.0, 1100.0)
    )
    assert late.refusal == "", late
    assert late.confidence > ECHO_CONFIDENCE_FLOOR, late
    assert late.tau_us == pytest.approx(850.0, rel=0.05)
    assert late.lower_peak_ratio < RAHMONIC_MARGIN, late


def test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one():
    """KNOWN LIMITATION — an honest late echo under a stronger earlier
    reflection is refused as if it were a rahmonic.

    "A much stronger peak sits below" is necessary for a rahmonic and not
    sufficient, and the two populations' ``lower_peak_ratio`` interleave —
    this honest case refuses at 2.448, a true rahmonic at 2.337 — so no
    threshold on it separates them from one record. The failure direction is
    a refusal rather than a wrong number, and the default window, which
    contains the earlier reflection, measures the same IR cleanly.
    """
    ir = _impulse_with_two_echoes(300e-6, 0.5, 850e-6, 0.2)
    refused = detect_echo(ir, SAMPLE_RATE, search_us=(750.0, 1100.0))

    assert refused.refusal == REFUSAL_RAHMONIC_OF_LOWER_DELAY, refused
    assert refused.tau_us == 0.0
    assert refused.confidence == 0.0
    assert refused.strength_db == STRENGTH_FLOOR_DB

    # The refused measurement was honest: both estimators found the real echo.
    assert refused.tau_cepstral_us == pytest.approx(850.0, rel=0.02), refused
    assert refused.tau_envelope_us == pytest.approx(850.0, rel=0.02), refused
    assert refused.corroboration < 0.01, refused

    # The refusing peak is the earlier real reflection, within half a step.
    assert abs(refused.lower_peak_us - 300.0) < 0.5 * refused.resolution_us, refused
    assert refused.lower_peak_ratio > RAHMONIC_MARGIN, refused

    # The interleaving that makes this unfixable per-record: a genuine
    # rahmonic lands BELOW this honest one on the statistic the screen uses.
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
    # reflection, so no stronger peak sits below the candidate.
    measured = detect_echo(ir, SAMPLE_RATE)
    assert measured.refusal == "", measured
    assert measured.confidence > ECHO_CONFIDENCE_FLOOR, measured
    assert measured.tau_us == pytest.approx(300.0, rel=0.05), measured
    assert measured.lower_peak_ratio < RAHMONIC_MARGIN, measured


def test_rahmonic_screen_catches_the_non_integer_ratio_the_submultiple_test_missed():
    """The rule is "anything stronger below", not "an exact submultiple".

    The worst measured case is the 205.6 us cloud member searched in
    (650, 1000): its cepstral candidate lands at 749.8 us, 3.648x the truth,
    which a submultiple test could only catch by accepting a whole quefrency
    step of slop. The screen locates the fundamental at 214.2 us instead.
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

    # The screen localises the fundamental better than either submultiple.
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
    """``RAHMONIC_MARGIN`` is load-bearing in both directions.

    Mutated to 0.0 the screen eats an ordinary 300 us reading; mutated to 1e9
    the 150-400 us cloud locks again at ~857 us through (700, 1000). The
    populations either side are measured by
    :func:`test_rahmonic_margin_calibration_populations_bracket_the_constant`.
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


# The two grids ``RAHMONIC_MARGIN``'s calibration is measured over, as
# literals: windows that contain the echo, and windows that exclude it.
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
    """The two populations ``RAHMONIC_MARGIN`` sits between, re-derived.

    Each IR is measured by the shipped detector with the screen disabled
    (``RAHMONIC_MARGIN`` patched to infinity) and classified by what the
    pre-screen detector did: true positives (unrefused, confident, within 15%
    of truth) whose ratio ceiling is the wall below the margin, and wrong
    readings (unrefused, confident, >15% off) whose floor is the wall above.

    Measured 2026-08-02: 2908 true positives, ceiling 0.9955; 439 wrong
    readings, floor 2.7899. The assertions are the bracket and its width, not
    those four figures — a grid this large samples both populations.
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

    ``ratio_lo``/``ratio_hi`` span every record; ``refused_ratio_*`` span only
    the ``rahmonic_of_lower_delay`` refusals (0.0 when there are none).
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

    Three legs off one grid of an earlier, stronger reflection plus a later,
    weaker one: **raised** (windows excluding the earlier reflection, where an
    honest late echo is refused), **default_window** (the remedy, which must
    refuse nothing) and **single_echo** (nothing below the candidate, which
    isolates the raised window itself as not the cause).
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
    """The raised-window hazard's shape: it needs the stronger-earlier-echo
    geometry, the default window does not have it, and what it discards were
    good measurements.

    Measured 2026-08-02: the raised leg refuses 605 of 720 at ratios
    1.678-4.513, discarded envelope estimates within 0.894% of the true late
    echo; the default-window leg refuses 0 of 432 and the single-echo leg 0 of
    370. The assertions are those walls, not the counts.
    """
    sweep = _two_echo_hazard_sweep()
    # The assertions below are walls, so print the figures a reader would
    # otherwise have to re-derive. Captured by pytest unless ``-s``.
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

    # Wall 4 — what was refused were good measurements. If this fails, the
    # refusals have started landing on records that were wrong anyway.
    assert sweep.raised_refused_envelope_error_pct < 1.0, sweep
    assert sweep.raised_refused_corroboration_max < CORROBORATION_LOOSE, sweep


def test_an_edge_refusal_reports_the_corroboration_it_measured():
    """An edge refusal reports the corroboration it measured, not the 1.0
    "could not be compared" marker.

    ``tau_at_window_lower_edge`` fires only after both candidates were found
    in-window and compared, so a real reading exists. Readings on this path
    run from near-perfect agreement to gross disagreement; the refusal turns
    on distance to the edge, not on agreement.
    """
    # A clean synthetic echo well below the default window's lower edge:
    # both estimators alias up onto the edge, agree closely, and are refused.
    refused = detect_echo(_impulse_with_echo(60e-6, ECHO_R), SAMPLE_RATE)
    assert refused.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, refused
    assert refused.corroboration < 0.1, refused

    # Exactly the value recomputable from the raw fields the record carries.
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

    # The contrast: an 830 us echo puts BOTH estimates above an 800 us
    # ceiling, so neither corroborates anything and the 1.0 marker is honest.
    incomparable = detect_echo(
        _impulse_with_echo(830e-6, ECHO_R), SAMPLE_RATE, search_us=(120.0, 800.0)
    )
    assert incomparable.refusal == REFUSAL_NO_IN_WINDOW_ECHO, incomparable
    assert incomparable.corroboration == 1.0

    # A refusal taken before either estimate exists is the same marker.
    no_arrival = detect_echo(
        np.random.default_rng(0).normal(0.0, 1.0, 65_536), SAMPLE_RATE
    )
    assert no_arrival.confidence == 0.0
    assert no_arrival.corroboration == 1.0
    assert no_arrival.tau_cepstral_us == 0.0


def test_reported_tau_is_always_the_envelope_estimate():
    """The reported tau is always the envelope estimate; the cepstrum only
    corroborates.

    A cepstral fallback is unreachable: reporting needs ``confidence > 0``,
    which needs ``corroboration < CORROBORATION_LOOSE``, which only the
    both-estimators-in-window path produces.
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
    """B1(b) — a delay below the window's lower edge is refused, not dressed
    up as an in-window one.

    An echo at 95-110 us is under the 120 us search floor and below the ~71 us
    quefrency step. The downstream resolution rule is anchored at zero delay,
    so it screens only a low window; the edge rule generalises.
    """
    for true_tau_us in (95.0, 100.0, 104.0, 110.0):
        found = detect_echo(_impulse_with_echo(true_tau_us * 1e-6, ECHO_R), SAMPLE_RATE)
        assert found.resolution_us == pytest.approx(1e6 / 14_000.0, rel=1e-9)
        assert found.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, found
        assert found.tau_us == 0.0
        assert found.confidence == 0.0

        # Both guards are load bearing: the resolution floor binds on a low
        # window, the edge rule on any.
        usable = (
            found.refusal == ""
            and found.confidence >= ECHO_CONFIDENCE_FLOOR
            and found.tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us
        )
        assert not usable
        assert found.tau_envelope_us < GEOMETRY_MIN_RESOLUTION_STEPS * found.resolution_us


def test_a_cloud_of_unresolvable_delays_does_not_read_as_geometry_locked():
    """B1(c) — a cloud of unresolvable delays does not read as geometry-locked.

    Ten positions spanning 60-150 us, all below the resolution floor: the
    estimates collapse onto ~115-152 us and used to cluster at a fraction of
    1.0. Both the edge refusal and the evidence rule now stop it.
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

    # The pathology, reconstructed from the refusals' own raw fields: the
    # estimates pile up (measured 114-152 us, median 135.4) and would have
    # satisfied the +-15% clustering test at a fraction of 0.9.
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
    """``ECHO_CONFIDENCE_FLOOR`` sits in the gap between the two measured
    populations.

    The negative controls are the impulse-with-no-echo families: they clear
    the arrival-crest gate, so they exercise concentration x corroboration
    rather than the early return.
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
    """Detector input errors carry a machine-readable slug, so the combiner
    need not match on message text.
    """
    with pytest.raises(EchoInputError) as excinfo:
        detect_echo(np.zeros(4096), SAMPLE_RATE)
    assert excinfo.value.slug == REFUSAL_ALL_ZERO_IR
    assert isinstance(excinfo.value, ValueError)


def test_geometry_unknown_slug_is_pinned_to_its_literal_value():
    """The reason slug is a wire value, so one assertion pins the literal a
    consumer matches on rather than the imported symbol.
    """
    assert GEOMETRY_UNKNOWN == "geometry_insufficient_usable_estimates"


def test_geometry_verdict_needs_at_least_two_confident_estimates():
    """One estimate clusters with itself; that must not read as a lock."""
    only_one = detect_echo(_impulse_with_echo(300e-6, ECHO_R), SAMPLE_RATE)
    verdict = assess_geometry([only_one, None, None])
    assert verdict.locked is False
    assert verdict.reason == GEOMETRY_UNKNOWN
    assert verdict.n_confident == 1
    assert verdict.n_positions == 3


def test_geometry_admission_rejects_a_zero_resolution_diagnostic():
    """N5 — a zero-resolution, zero-delay diagnostic is inadmissible evidence.

    With ``resolution_us == 0`` rule 3's threshold collapses to zero and a
    ``tau_us`` of zero clears it. :func:`detect_echo` never emits that, so
    this is a contract about what :func:`assess_geometry` accepts from any
    source — a hand-built record, a deserialised one, a future detector.
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

    # Not a ban on resolution_us == 0: it is the zero delay that is refused.
    with_delay = replace(degenerate, tau_us=300.0, tau_envelope_us=300.0)
    admitted = assess_geometry([with_delay, with_delay, with_delay])
    assert admitted.n_confident == 3
    assert admitted.locked is True


def test_captures_without_an_ir_report_no_echo_diagnostic():
    """``None`` (not measured) stays distinct from a zero-confidence
    diagnostic (measured, found nothing).
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
    """S2 — one malformed IR refuses that position and nothing else.

    The curves, the screen and the geometry verdict are computed from the
    rest, and ``None`` keeps meaning strictly "no IR was supplied".
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
    # Two other positions carry a refusal of their own — the 157 and 185 us
    # members sit inside the default window's lower-edge margin — which is a
    # fact about those captures, present with or without the eleventh.
    assert all(
        e is not None and e.refusal != REFUSAL_ALL_ZERO_IR
        for e in result.per_position_echo[:-1]
    )
    assert [e.refusal for e in result.per_position_echo[:-1]] == [
        e.refusal for e in good.per_position_echo
    ]

    # The refusal contributes nothing: same numbers as the ten-position run.
    assert result.geometry.n_confident == good.geometry.n_confident
    assert result.geometry.reason == good.geometry.reason
    assert result.geometry.median_tau_us == pytest.approx(good.geometry.median_tau_us)
    assert result.geometry.n_positions == 11


def test_echo_detector_settings_are_plumbed_and_recorded():
    """N6 — the echo window travels with the result, since a per-position tau
    is only interpretable against the window it was searched in.
    """
    freqs, _true_db, captures = _cloud(_dispersed_taus(4))

    default = combine_positions(captures)
    assert default.echo_band_hz == DEFAULT_ECHO_BAND_HZ
    assert default.echo_search_us == DEFAULT_ECHO_SEARCH_US

    narrow = combine_positions(captures, echo_search_us=(400.0, 800.0))
    assert narrow.echo_search_us == (400.0, 800.0)
    assert narrow.echo_band_hz == DEFAULT_ECHO_BAND_HZ

    # Plumbed, not merely recorded: this cloud sits at ~150-500 us, so a
    # 400-800 us window changes what the detector reports.
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


# --- C2. The three S0 hardenings, on synthetic ground truth ---


def _lowpassed(ir: np.ndarray, fc_hz: float, order: int) -> np.ndarray:
    """Magnitude-only Butterworth-shaped lowpass, steep enough to turn the
    detector's 5-19 kHz default band into stopband.

    Magnitude-only because the screen is a level comparison, and leaving phase
    alone keeps the direct arrival where the rest of the fixture put it.
    """
    n_fft = 1 << (ir.size - 1).bit_length()
    spectrum = np.fft.rfft(ir, n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SAMPLE_RATE)
    shape = 1.0 / np.sqrt(1.0 + (np.maximum(freqs, 1e-9) / fc_hz) ** (2 * order))
    return np.fft.irfft(spectrum * shape, n_fft)[: ir.size]


def test_band_below_passband_refuses_a_stopband_residue_signal():
    """S0-1 — a stopband residue signal is refused before either estimator runs.

    A 320 us echo IR lowpassed at 2 kHz: the declared 200-2000 Hz passband
    measures 48.6 dB above the analysis band, past the 25.0 dB margin.
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

    # Declaring no passband leaves the detector as it was: the screen is
    # opt-in, not a new floor.
    unscreened = detect_echo(residue, SAMPLE_RATE)
    assert unscreened.refusal != REFUSAL_BAND_BELOW_PASSBAND, unscreened
    assert unscreened.band_deficit_db == STRENGTH_FLOOR_DB, (
        "an undeclared passband must read as not-measured, not as 0 dB"
    )


def test_band_below_passband_stays_quiet_on_an_in_band_signal():
    """S0-1 — the screen stays quiet on an in-band signal, at two passbands a
    caller might plausibly declare.
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
    """S0-1 — a malformed ``signal_band_hz`` raises with a machine-readable
    slug, like ``band_hz``: it is wrong for every capture at once.
    """
    ir = _impulse_with_echo(300e-6, ECHO_R)
    for bad in ((0.0, 2000.0), (2000.0, 200.0), (2000.0, 2000.0)):
        with pytest.raises(EchoInputError) as excinfo:
            detect_echo(ir, SAMPLE_RATE, signal_band_hz=bad)
        assert excinfo.value.slug == REFUSAL_BAD_SIGNAL_BAND_HZ, bad

    # Nyquist clipping matches band_hz's: an upper edge above Nyquist is
    # clipped rather than rejected.
    clipped = detect_echo(ir, SAMPLE_RATE, signal_band_hz=(150.0, 96_000.0))
    assert clipped.refusal == "", clipped


def test_earlier_dominant_arrival_names_an_arrival_below_the_window():
    """S0-2 — an arrival below the window is named rather than collapsing to
    "ran, found nothing credible".

    The S0 ground plane's geometry: a dominant arrival just below the window
    (145 us at r=0.8) plus the real echo inside it (320 us at r=0.3). The
    window is the sample-aligned (166.6667, 1000) us — the refusal needs the
    envelope's own answer below ``search_us[0]``, which under ``ceil`` bounds
    is reachable only within half a sample of the edge; at S0's own
    (150, 1000) the edge rule names the same record instead.
    """
    ir = _impulse_with_two_echoes(145e-6, 0.8, 320e-6, 0.3)
    found = detect_echo(ir, SAMPLE_RATE, search_us=(166.6667, 1000.0))

    assert found.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, found
    assert found.tau_us == 0.0
    assert found.confidence == 0.0
    assert found.strength_db == STRENGTH_FLOOR_DB

    # The mechanism: the envelope's answer is below the window it was given.
    assert 0.0 < found.tau_envelope_us < 166.6667, found
    assert found.corroboration == 1.0, found

    # The same IR through S0's (150, 1000) protocol window, a 7.2-sample lower
    # edge: still a refusal, still disclosing the interloper, under the other
    # of the two correct reasons.
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

    # The same IR through the default window, which contains the interloper:
    # nothing below to name. That is the remedy the refusal implies.
    contained = detect_echo(ir, SAMPLE_RATE)
    assert contained.earlier_arrival_us == 0.0, contained
    assert contained.earlier_arrival_db == STRENGTH_FLOOR_DB, contained
    assert contained.refusal != REFUSAL_EARLIER_DOMINANT_ARRIVAL, contained


def test_found_nothing_credible_survives_the_earlier_arrival_refusal():
    """S0-2 — "refused" and "ran, found nothing" stay different outcomes.

    The rule needs the envelope's answer below the window AND a genuine
    arrival down there to name. Swept over the whole 60-member negative
    control family at the default window, where the family has no
    below-window local maximum at all.
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
                # Not exactly zero: this family's ceiling is 0.091 (see
                # ``ECHO_CONFIDENCE_FLOOR``), and a low-but-nonzero score with
                # an empty refusal is the same "found nothing credible".
                assert found.confidence < ECHO_CONFIDENCE_FLOOR, (where, found)
                empty_refusals += 1

    # The state this test protects is actually reached.
    assert empty_refusals > 0


def test_earlier_arrival_dominance_floor_sits_in_the_measured_gap():
    """``EARLIER_ARRIVAL_DOMINANCE_DB`` sits above the band-limited envelope's
    own ringing, re-derived from one committed ladder.

    ``_CALIBRATION_WRONG_READING_WINDOWS`` crossed with the 60-member negative
    control family, 660 readings, plus the mutation that shows the floor is
    load-bearing. The other population — S0's proud-capsule interlopers — is
    real data and lives in section F.
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
    # Re-derived 2026-08-02: 6 flips. The mutation still re-opens the defect
    # on a population the shipped floor refuses 0 of.
    assert unguarded_flips == 6, unguarded_flips
    worst_window, worst_count = max(
        unguarded_per_window.items(), key=lambda item: item[1]
    )
    assert (worst_window, worst_count) == ((1000.0, 1600.0), 3), unguarded_per_window

    # The gap in both directions: the ground-plane floor (-2.57 dB) is real
    # data in section F; the ringing ceiling is measured here.
    assert max(levels) < EARLIER_ARRIVAL_DOMINANCE_DB
    assert EARLIER_ARRIVAL_DOMINANCE_DB - max(levels) == pytest.approx(7.14, abs=0.05)

    # A synthetic interloper at the ground plane's own level still refuses, so
    # the floor is not merely "quiet enough to never fire". The window is the
    # sample-aligned (166.6667, 1000) us, where the dominance rule is
    # reachable; at a 7.2-sample edge the edge rule names the record instead.
    loud = _impulse_with_two_echoes(145e-6, 0.8, 320e-6, 0.3)
    found = detect_echo(loud, SAMPLE_RATE, search_us=(166.6667, 1000.0))
    assert found.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, found
    assert found.earlier_arrival_db > EARLIER_ARRIVAL_DOMINANCE_DB, found


def test_a_below_dominance_interloper_falls_back_to_the_honest_zero():
    """An interloper that takes the envelope's answer but is too quiet to be
    called dominant falls back to the honest empty refusal.

    One geometry: a 145 us interloper of varying strength against a real
    320 us echo at r=0.3, searched (166.6667, 1000) us — the sample-aligned
    window, where the refusal is reachable. Read the band's width as an order
    of magnitude, not a boundary.
    """
    def probe(early_r: float):
        return detect_echo(
            _impulse_with_two_echoes(145e-6, early_r, 320e-6, 0.3),
            SAMPLE_RATE,
            search_us=(166.6667, 1000.0),
        )

    dominant = probe(0.34)
    assert dominant.earlier_arrival_db == pytest.approx(-9.57, abs=0.2), dominant
    assert dominant.refusal == REFUSAL_EARLIER_DOMINANT_ARRIVAL, dominant

    # Inside the band: disclosed on the record, but not named.
    fallen_back = probe(0.32)
    assert fallen_back.earlier_arrival_db == pytest.approx(-10.11, abs=0.2), fallen_back
    assert fallen_back.earlier_arrival_db < EARLIER_ARRIVAL_DOMINANCE_DB
    # It really did take the answer — that is what makes this a band.
    assert 0.0 < fallen_back.tau_envelope_us < 166.6667, fallen_back
    assert fallen_back.refusal == "", fallen_back
    assert fallen_back.confidence == 0.0, fallen_back
    # ...and the interloper is still disclosed on the fallen-back record.
    assert fallen_back.earlier_arrival_us == pytest.approx(145.8, abs=0.5)

    # Below the band: the interloper no longer wins the envelope at all.
    weak = probe(0.30)
    assert weak.earlier_arrival_db == pytest.approx(-10.69, abs=0.2), weak
    assert not 0.0 < weak.tau_envelope_us < 166.6667, weak
    assert weak.refusal == "", weak


def test_effective_floor_is_reported_on_every_record():
    """``effective_floor_us`` is reported on every record, including refusals.

    ``search_us[0]`` understates what a window can see — the bottom
    ``WINDOW_EDGE_MARGIN_STEPS`` is refused outright, so the default window's
    real floor is ~191.4 us, not 120 us — and a consumer needs it most when
    the window found nothing.
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

    # Every refusal ``detect_echo`` can be driven to from a constructed input,
    # including the three taken before the estimators run.
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
    # The band-too-narrow refusal has its own coarser resolution: the field
    # tracks the window AND the band.
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
    # detector raise into a refused record. The band is unknown there, so
    # resolution_us and the floor are both 0.0.
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
    """The disclosed floor and the applied edge rule are one boundary.

    For any record that reached the edge check (at least one estimator landed
    in the window), ``tau_at_window_lower_edge`` fires iff the lowest
    in-window candidate is at or below that record's ``effective_floor_us``.
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

    # Both sides of the boundary are exercised, so the "iff" is not vacuous.
    assert reached > 300, reached
    assert refused > 50 and accepted > 50, (refused, accepted)


def test_band_deficit_sentinel_covers_all_three_documented_causes():
    """``band_deficit_db == STRENGTH_FLOOR_DB`` means "not measured", from all
    three documented causes.

    Cause 3 matters most downstream: a sub-bin passband must fail OPEN, or a
    declared passband narrower than one FFT bin would refuse every position.
    """
    ir = _impulse_with_echo(320e-6, ECHO_R)
    passband = (150.0, 20_000.0)

    # Cause 1 — no passband declared. (Also pinned by the combiner test;
    # repeated here so all three causes are visible in one place.)
    assert detect_echo(ir, SAMPLE_RATE).band_deficit_db == STRENGTH_FLOOR_DB

    # Cause 2 — the detector returned before the screen ran. All three
    # pre-screen refusals, each with a passband declared.
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

    # Cause 3 — the screen ran but the passband covers no bin: at n_fft >=
    # 4096 and 48 kHz the bins are ~11.7 Hz apart, so these are sub-bin
    # passbands. Fail-open, and the deficit reads not-measured.
    ungated = detect_echo(ir, SAMPLE_RATE)
    for narrow in ((100.0, 105.0), (5.0, 8.0), (100.0, 103.0)):
        found = detect_echo(ir, SAMPLE_RATE, signal_band_hz=narrow)
        assert found.band_deficit_db == STRENGTH_FLOOR_DB, narrow
        assert found.refusal == ungated.refusal == "", narrow
        assert found.tau_us == pytest.approx(ungated.tau_us), narrow
        assert found.confidence == pytest.approx(ungated.confidence), narrow


def test_signal_band_is_plumbed_through_the_combiner_and_recorded():
    """S0-1 — the combiner passes the declared passband down and echoes back
    the one actually applied.
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

    # Malformed config raises rather than refusing every position.
    for bad in ((0.0, 2000.0), (2000.0, 200.0), (1000.0,), 500.0):
        with pytest.raises(ValueError, match="signal_band_hz"):
            combine_positions(captures, signal_band_hz=bad)


def test_thin_evidence_qualifies_a_verdict_without_withholding_it():
    """S0-3 — a verdict resting on the bare minimum of usable estimates is
    qualified, not withheld.

    The rule is ``n_confident == GEOMETRY_MIN_CONFIDENT and n_positions >=
    2 * GEOMETRY_MIN_CONFIDENT``, and it is disclosure: the verdict, its
    reason and every supporting number are unchanged.
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
    # Two of three is the evidence that cloud had, not a shortfall.
    assert assess_geometry([usable, usable, unusable]).thin_evidence is False
    # Structurally unreachable on GEOMETRY_UNKNOWN, which fires exactly when
    # n_confident < GEOMETRY_MIN_CONFIDENT.
    unknown = assess_geometry([usable] + [unusable] * 9)
    assert unknown.reason == GEOMETRY_UNKNOWN
    assert unknown.thin_evidence is False
    # ...and it does qualify a dispersed verdict, not only a locked one.
    far = replace(usable, tau_us=900.0, tau_envelope_us=900.0)
    dispersed = assess_geometry([usable, far] + [unusable] * 8)
    assert dispersed.reason == GEOMETRY_DISPERSED
    assert dispersed.thin_evidence is True


# --- D. Analysis-grid bounding ---


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
    """The analysis-grid cap must not change the curves consumers read.

    The same cloud combined twice, once through the cap and once with it
    lifted. Measured worst-bin agreement: the three smoothed curves 0.074,
    0.075 and 0.085 dB; the raw pair ``power_mean_db`` 0.224 dB and
    ``median_db`` 0.383 dB, both deliberately outside the 0.1 dB bound
    because a raw per-bin curve cannot reproduce fine comb structure at 8x
    coarser spacing; the retained per-position stack 0.404-0.429 dB and its
    smoothed sibling 0.096-0.135 dB.

    ``interference_nulls`` reads ``power_mean_db`` unsmoothed at each located
    minimum, so the loose bound is asserted rather than merely disclosed; the
    end-to-end cost of the cap on that statistic (0.033 dB on rung depths,
    re-derived 2026-08-22) is asserted in tests/test_interference_nulls.py.
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

    # The raw pair: bounded loosely, and asserted to be the ones that exceed
    # 0.1 dB, so the docstring cannot rot in either direction.
    raw_worst = {name: worst_bin_db(name) for name in ("power_mean_db", "median_db")}
    for name, worst in raw_worst.items():
        assert 0.1 < worst < 0.6, f"{name} moved by {worst:.4f} dB under decimation"
    assert raw_worst["median_db"] > raw_worst["power_mean_db"], (
        "the median is documented as the worse of the two — an order "
        f"statistic, not an average: {raw_worst}"
    )

    # The retained per-position stack is raw in the same sense and is held to
    # the same bound, row by row; the null gate reads it per position.
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
    # A per-position curve cannot be quieter than the power mean of those
    # same positions: averaging is what buys the mean its stability.
    assert per_position_worst > raw_worst["power_mean_db"]
    # The figure the docstring quotes, pinned so it cannot rot into prose.
    assert per_position_worst == pytest.approx(0.429, abs=0.01)

    # The smoothed per-position curves sit between the two bounds: smoothing
    # one position is not smoothing an average of three, so holding these to
    # the combined curves' 0.1 dB would assert something untrue.
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
    # ...and still far less than the raw stack they came from.
    assert diag_worst < per_position_worst / 2.0


def test_decimation_preserves_the_linear_grid_contract_and_band_energy():
    """Decimation is block averaging, not subsampling, and the result is still
    a legal linear grid.

    Checked on a flat-plus-notch construction where the answer is computable:
    one -20 dB bin among 2**17 zeros must survive as a shallow dip rather than
    vanishing or staying full-depth.
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


# --- E. Real-data smoke — 2026-07-24/25 JTS3 corpus ---

# The corpus is gitignored and laptop-durable, and simply absent in CI. Its
# root and skip gate live in tests/_flat_lin_corpus.py; the loader below stays
# here because the two readers want different phases and program parameters.
CORPUS = CDHORN_ROOT
requires_corpus = requires_cdhorn


@pytest.fixture(scope="module")
def corpus_irs() -> dict[str, np.ndarray]:
    """Era-exact reconstruction of the run 5 / run 7 impulse responses.

    Program parameters are reused verbatim from the session's own forensics
    script (``captures/flat-linearization-20260725/comb_forensics2.py``),
    which is the authority on the DSP state those captures were taken under.
    Registration is :func:`~tests._flat_lin_corpus.sweep_anchor`, which
    locates the sweep by its own waveform, so where a composer puts it cannot
    matter (#1879). MEASURE ships three repeats of each sweep, agreeing to
    0.4%; the later repeats sit +16 and +32 samples off their scheduled
    positions (~31 ppm of clock drift over the 43.8 s capture).
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


@requires_corpus
def test_detect_echo_finds_the_corpus_bounce(corpus_irs):
    """D — the detector reproduces the offline forensics finding: a discrete
    echo at ~0.31 ms, ~-8.8 dB (r ~= 0.36), in both run 5 and run 7.
    """
    for name, ir in corpus_irs.items():
        result = detect_echo(ir, SAMPLE_RATE)
        assert 250.0 <= result.tau_us <= 400.0, f"{name}: tau {result.tau_us:.1f} us"
        assert result.confidence > ECHO_CONFIDENCE_FLOOR, f"{name}: {result}"
        # The plan records -8.8 dB / r ~= 0.36 for this bounce.
        assert result.strength_db == pytest.approx(-8.8, abs=1.5), name
        # The rahmonic screen's headroom on real data, which is where the
        # "low-quefrency leakage cannot auto-refuse an honest reading" claim
        # can be tested: measured 0.329-0.387. The bound is 0.45 rather than
        # 0.5 so it is a tripwire on that 0.387 worst case.
        assert result.lower_peak_ratio < 0.45, f"{name}: {result}"
        assert result.lower_peak_ratio < RAHMONIC_MARGIN, name


@requires_corpus
def test_corpus_frames_read_as_geometry_locked(corpus_irs):
    """D — corpus frames read as geometry-locked, which is the detector
    working: this corpus cannot be spatially averaged bounce-free.
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
    """End-to-end on real data: the combiner surfaces the lock and the screen
    stays quiet.
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


# --- F. Real-data acceptance — the 2026-07-25 S0 session ---
#
# Gated on a second laptop-durable root and absent in CI, so a corpus
# acceptance PR is not done until these have been seen to PASS.

# Roots, skip gates, declared passbands and the era-exact deconvolution live
# in tests/_flat_lin_corpus.py.


@pytest.fixture(scope="module")
def ground_plane_irs() -> dict[str, np.ndarray]:
    return s0_position_irs(S0_GROUND_PLANE)


@pytest.fixture(scope="module")
def main_leg_irs() -> dict[str, np.ndarray]:
    """The ten-position desk cloud — leg A, the control every leg-B claim is a
    contrast against (~5 s, module-scoped).
    """
    return s0_position_irs(S0_MAIN)


@pytest.fixture(scope="module")
def loopback_irs() -> dict[str, np.ndarray]:
    """The electrical loopback's per-branch IRs, as the run left them."""
    return _loopback_irs()


@requires_s0
def test_loopback_woofer_branch_is_refused_as_stopband_residue(loopback_irs):
    """S0-1 acceptance — the loopback's woofer branch is refused as stopband
    residue.

    Behind a 2 kHz LR4 lowpass the 5-19 kHz band holds only residue, and the
    branch returned ``tau = 323.3 us`` at confidence 0.275 with an empty
    refusal on the sweep stimulus (2026-07-25, s0-analysis/loopback).
    """
    for stimulus in ("impulse", "sweep", "mls"):
        ir = loopback_irs[f"{stimulus}_woofer"]
        screened = detect_echo(ir, SAMPLE_RATE, signal_band_hz=LOOPBACK_WOOFER_PASSBAND_HZ)
        assert screened.refusal == REFUSAL_BAND_BELOW_PASSBAND, (stimulus, screened)
        assert screened.tau_us == 0.0
        assert screened.band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB, stimulus
        assert screened.band_deficit_db == pytest.approx(41.0, abs=1.5), stimulus

    # The defect itself, at the leg-B protocol window the loopback used:
    # Without a declared passband the sweep still reports its confident-
    # looking number, so this is a gate the caller supplies.
    unscreened = detect_echo(
        loopback_irs["sweep_woofer"], SAMPLE_RATE, search_us=S0_PROTOCOL_SEARCH_US
    )
    assert unscreened.refusal == "", unscreened
    assert unscreened.tau_us == pytest.approx(323.3, abs=1.0), unscreened
    assert unscreened.confidence == pytest.approx(0.275, abs=0.01), unscreened


@requires_s0
def test_loopback_tweeter_branch_is_measured_in_its_own_passband(loopback_irs):
    """S0-1 — the tweeter branch, whose passband overlaps the analysis band,
    is measured on all three stimuli.
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
    """S0-1 — the deficit's separation depends on ``band_hz``, including where
    it stops working.

    Swept over six bands on the 13 S0 acoustic records and the 3 electrical
    loopback woofer records. At a band whose lower edge sits on this speaker's
    2 kHz crossover the woofer's own passband is inside the analysed band, the
    deficit collapses to ~18 dB and the screen misses the case it exists for —
    which is why the analysis band must stay clear of the crossover. All 13
    acoustic records are the same JTS3 cdhorn.
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

    # (band, does the screen still catch stopband residue)
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
    # One octave up the margin is already thin.
    thin_residue = deficits(residue, (3000.0, 19_000.0))
    assert min(thin_residue) - BAND_BELOW_PASSBAND_MARGIN_DB == pytest.approx(
        1.53, abs=0.3
    ), thin_residue

    # The honest population's spread across all six bands.
    assert max(honest_ceilings) == pytest.approx(17.50, abs=0.3), honest_ceilings
    assert BAND_BELOW_PASSBAND_MARGIN_DB - max(honest_ceilings) > 7.0


@requires_s0
def test_main_leg_is_unchanged_and_is_the_ground_plane_s_control(main_leg_irs):
    """S0-2 / S0-4 — the main leg is unchanged and is the ground plane's
    control.

    Same speaker, program and window: four of these ten carry a below-window
    arrival at 145.8 us, all 14.7-15.7 dB down, against the ground plane's
    125-146 us at 0.6-2.6 dB down. One changed mic mounting is the whole
    difference between a reading and a refusal.
    """
    # Per-position (tau, confidence) as s0-analysis/REPORT.md Q1, re-pinned
    # 2026-07-27 when the reader moved onto ``_flat_lin_corpus.sweep_anchor``:
    # that moved exactly two of the ten, both toward a stronger detection.
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

    # Three of these ten, not four: cloud_04's sweep-aligned read no longer
    # carries a below-window arrival at all.
    assert len(with_earlier) == 3, with_earlier
    assert all(us == pytest.approx(145.8, abs=0.5) for us, _db in with_earlier.values())
    levels = [db for _us, db in with_earlier.values()]
    assert min(levels) == pytest.approx(-15.71, abs=0.3), with_earlier
    assert max(levels) == pytest.approx(-14.75, abs=0.3), with_earlier
    # A desk-cloud interloper is ~12 dB quieter than a proud-capsule one.
    assert max(levels) < -10.0, with_earlier


@requires_s0
def test_the_report_s_deficit_decomposes_into_the_shipped_statistic(loopback_irs):
    """S0-1 — the loopback report's 49.7 dB and the shipped statistic's
    40.4 dB are the same signal, three metric changes apart.
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
    window), each against its own declared passband. The ground-plane leg is
    the honest ceiling: tipping the cabinet at the floor cost top-octave
    level, and it is still 13 dB clear of the threshold.

    Every number here is a reading off a fixed archived corpus, so a moved pin
    is classified before it is edited: a detector change moves all three
    independently-captured populations coherently, while a reading change (the
    loader, the composer, the calibration parse, the deconvolution) moves only
    the populations sharing the broken input and is a bug in that input, not
    in the pin. Never widen a tolerance to admit an unexplained number.
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
    # Asserting 10 dB of clearance either side makes this a tripwire on the
    # gap closing rather than a restatement of it.
    assert BAND_BELOW_PASSBAND_MARGIN_DB - ceiling > 10.0, honest_db
    assert floor - BAND_BELOW_PASSBAND_MARGIN_DB > 10.0, residue_db
    assert ceiling == pytest.approx(12.07, abs=1.0), honest_db
    assert floor == pytest.approx(40.43, abs=1.5), residue_db
    # The ground-plane leg really is the honest worst case, which is why it
    # is the population's ceiling rather than a bystander in it.
    assert max(honest_db, key=honest_db.get).startswith("gp_"), honest_db
    # The per-population figures, so each is re-derived, not just the gap.
    main_db = [v for k, v in honest_db.items() if k.startswith("main_")]
    assert (min(main_db), max(main_db)) == pytest.approx((1.04, 6.56), abs=0.3), main_db
    gp_db = [v for k, v in honest_db.items() if k.startswith("gp_")]
    assert (min(gp_db), max(gp_db)) == pytest.approx((8.28, 12.07), abs=0.3), gp_db
    assert (
        honest_db["corpus_run7_tweeter"],
        honest_db["corpus_run7_verify"],
        honest_db["corpus_run5_verify"],
    ) == pytest.approx((1.11, 1.54, 5.51), abs=0.3), honest_db

    # The acoustic subset is the 16 records ``band_deficit_db`` quotes a range
    # for; the electrical in-band controls read either side of zero and are
    # asserted separately below.
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
    """S0-2 acceptance — the three ground-plane records refuse by name and
    carry the interloper.

    Leg B left the capsule centimetres proud of a hard floor, manufacturing a
    dominant arrival at 125-146 us (4.3-5.0 cm of path, r = 0.74-0.93). All
    three used to report ``confidence = 0.000`` with an empty refusal. Which
    refusal names them depends on the window's sample alignment: through the
    leg-B protocol window (150, 1000) us — a 7.2-sample lower edge — the
    envelope answers 156.25 us inside the edge margin and
    ``tau_at_window_lower_edge`` returns first; through the sample-aligned
    (166.6667, 1000) us it is ``earlier_dominant_arrival``. Both windows are
    asserted: no delay, no confidence, and the interloper at the delay and
    level the S0 report tabulated.
    """
    expected = {
        # position -> (cepstral tau, interloper tau, its level)
        "ground_plane_01": (327.2, 145.8, -2.57),
        "ground_plane_02": (270.2, 145.8, -2.01),
        "ground_plane_03": (342.8, 125.0, -0.64),
    }
    assert set(ground_plane_irs) == set(expected)

    # 8 samples at 48 kHz written as the four decimals a caller would use —
    # 8.0000016 samples. Deriving it as ``8.0 / SAMPLE_RATE * 1e6`` gives a
    # value that round-trips to exactly 8.0 and makes the snap a no-op here.
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
            # The envelope rails one sample up, at the first in-window sample
            # less the parabola's clamp, on both windows.
            assert found.tau_envelope_us == pytest.approx(156.25, abs=0.5), position

            # ...and the interloper the refusal is named for.
            assert found.earlier_arrival_us == pytest.approx(early_us, abs=0.5), position
            assert found.earlier_arrival_db == pytest.approx(early_db, abs=0.2), position
            # Louder than anything the window contained, which is why it took
            # the answer.
            assert found.earlier_arrival_db > -3.0, position

        # The dominance rule is reached at the aligned window and pre-empted at
        # the protocol one: the documented refusal precedence.
        assert aligned.corroboration == 1.0, position
        assert protocol.corroboration < 1.0, position


@requires_s0
def test_ground_plane_arrivals_sit_under_the_default_window_floor(ground_plane_irs):
    """S0-4 acceptance — the ground-plane arrivals sit under the default
    window's ~191.4 us effective floor, so that window structurally cannot
    report them.

    The arrivals' delays are measured through the protocol window that can see
    them rather than asserted against literals.
    """
    for position, ir in sorted(ground_plane_irs.items()):
        found = detect_echo(ir, SAMPLE_RATE)
        assert found.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE, (position, found)
        assert found.effective_floor_us == pytest.approx(191.43, abs=0.01), position
        # Inside the default window there is nothing below to name.
        assert found.earlier_arrival_us == 0.0, position

        # The claim the field lets a consumer make, against this capture's own
        # measured arrival.
        through_protocol_window = detect_echo(
            ir, SAMPLE_RATE, search_us=S0_PROTOCOL_SEARCH_US
        )
        arrival_us = through_protocol_window.earlier_arrival_us
        assert arrival_us > 0.0, position
        assert arrival_us < found.effective_floor_us, (position, arrival_us)


@requires_s0
def test_ground_plane_cloud_reads_as_thin_evidence_free(ground_plane_irs):
    """S0-3 — the three-position leg is not thin evidence: zero usable
    estimates is a shortfall, not the disproportion the flag reports.
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
    # Every position refused by name, never a silent confidence collapse.
    # Which of the two refusals it is belongs to
    # :func:`test_ground_plane_positions_report_the_proud_capsule_arrival`.
    assert all(
        e is not None and e.refusal == REFUSAL_TAU_AT_WINDOW_LOWER_EDGE
        for e in combined.per_position_echo
    ), [e.refusal for e in combined.per_position_echo if e is not None]


# --- Per-position residual — the design brief's §4.2 trend surface ---


def _roled(captures, roles):
    """The same captures, each carrying a role. One line, like the flow's."""
    return [
        dataclasses.replace(capture, role=role)
        for capture, role in zip(captures, roles, strict=True)
    ]


def test_the_role_rides_through_the_combination_without_touching_it():
    """The role is carried, never read: the reduction is byte-identical
    either way, so the combiner never becomes a weighted one.
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
    """A position that sat off the mean reports a bigger residual.

    An absolute dB target would pin the synthetic fixture; the discrimination
    and the role travelling with it are what must hold.
    """
    freqs, _true, captures = _cloud(_dispersed_taus(4))
    captures = _roled(captures, ["onax", "offax", "offax", "xovr"])
    # One position 4 dB hot across the whole band — the anchor-outlier
    # signature this surface exists to see.
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
    """The trusted band is the caller's, and a narrower one grades fewer bins."""
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
    """The estimator reads the SMOOTHED pair, kept as a measurement.

    Run on the raw curves, a broadband +4 dB offset on one position of four
    does NOT make it the worst position: each position's own comb contributes
    ~2.5 dB and swamps the level term.
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
    rows = position_residuals(combined)
    assert rows[1].rms_db == max(row.rms_db for row in rows)
