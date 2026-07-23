# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.active_speaker.linearization_envelope (#1668 PR-B).

**Corpus-anchor approach taken (see the two
``test_compute_sigma_corpus_regression_anchor_*`` cases below): "assert the
formula against hand-computed expected outputs," not literal corpus replay.**
Literally replaying
``captures/xover-e0-2026-07-21/sigma-seeding-20260723/compute_sigma.py``'s
real inputs through ``compute_sigma_curve`` is impractical here: its
``raw_samples.json`` (the per-occurrence curves compute_sigma.py actually
read) was intentionally not retained (~736 MB; see REPORT.md beside it), and
the retained ``sigma_curves.json`` keeps only AGGREGATE statistics
(``mean_absolute_db``, ``sigma_*_db``) — not the individual occurrence
curves a replay would need to feed back in. Separately,
``DEFAULT_ENVELOPE_GRID_HZ`` (150 Hz floor, 176 points) is a deliberately
different grid than compute_sigma.py's ``LOG_GRID_HZ`` (80 Hz floor, 185
points), so smoothing-window dilution at a nominal octave center would not
bit-match even given the real inputs. Given that, the anchor tests instead
construct synthetic fixtures whose UN-diluted per-occurrence spread equals a
REPORT.md-seeded octave-center value exactly (by construction — see
``_plateau`` below), then cross-check ``compute_sigma_curve``'s output
against ``_hand_compute_sigma``, an independent re-implementation of the same
documented formula written fresh in this file (not calling the module's own
private helpers) — the REPORT.md numbers anchor the test to a realistic
numeric SCALE; the tight assertion is the independent formula cross-check.
"""
from __future__ import annotations

import warnings

import numpy as np
import pytest

from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    DRIVER_CLASSES,
    ENVELOPE_CEILING_SENTINEL_DB,
    MIC_TIERS,
    EnvelopeCurve,
    EnvelopeTerm,
    ReasonCode,
    class_prior_limit,
    compose_envelope,
    compute_sigma_curve,
    invertibility_limit,
    linearity_limit,
    mic_trust_limit,
    repeatability_limit,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse

# A finer, non-grid frequency axis -- used by the basic sigma tests so they
# also exercise compute_sigma_curve's np.interp resample step, not just the
# smoothing/centering/std steps (the corpus-anchor / notch / offset tests
# build directly on DEFAULT_ENVELOPE_GRID_HZ instead, to keep their
# hand-computed expected values exact rather than interpolation-smeared).
_NATIVE_FREQS_HZ = np.linspace(20.0, 22_000.0, 4096)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


def _driver_response(
    role: str,
    magnitude_db: np.ndarray,
    *,
    freqs_hz: np.ndarray = _NATIVE_FREQS_HZ,
    repeat_responses: tuple[DriverResponse, ...] = (),
    validity_floor_hz: float | None = 150.0,
    repeat_index: int | None = None,
) -> DriverResponse:
    """Minimal, directly-constructed DriverResponse -- mirrors the
    direct-construction helper pattern in
    tests/test_crossover_v2_conductor.py (complex_tf/gating/snr are unused
    by this module; filled with innocuous placeholders)."""
    return DriverResponse(
        role=role,
        freqs_hz=freqs_hz,
        magnitude_db=magnitude_db,
        complex_tf=np.ones_like(freqs_hz, dtype=complex),
        gating={},
        snr=None,
        validity_floor_hz=validity_floor_hz,
        repeat_responses=repeat_responses,
        repeat_index=repeat_index,
    )


def _with_occurrences(
    role: str, curves_db: list[np.ndarray], **kwargs
) -> DriverResponse:
    """Build a primary DriverResponse with curves_db[1:] attached as
    repeat_responses, in order."""
    repeats = tuple(
        _driver_response(role, c, repeat_index=i + 1, **kwargs)
        for i, c in enumerate(curves_db[1:])
    )
    return _driver_response(role, curves_db[0], repeat_responses=repeats, **kwargs)


def _flat(value_db: float = 0.0, *, freqs_hz: np.ndarray = _NATIVE_FREQS_HZ) -> np.ndarray:
    return np.full_like(freqs_hz, value_db, dtype=np.float64)


def _plateau(
    grid_hz: np.ndarray, center_hz: float, height_db: float, half_width_oct: float
) -> np.ndarray:
    """A curve that is 0 dB everywhere except a constant `height_db`
    plateau spanning +/-half_width_oct around center_hz. Used to inject a
    KNOWN per-occurrence deviation that (a) survives ladder-smoothing
    almost exactly at its own center (the plateau is wide relative to the
    local smoothing window) and (b) is NOT removed by valid-band centering
    (unlike a flat whole-curve offset, which centering exists to cancel)."""
    lo = center_hz / (2.0**half_width_oct)
    hi = center_hz * (2.0**half_width_oct)
    out = np.zeros_like(grid_hz)
    out[(grid_hz >= lo) & (grid_hz <= hi)] = height_db
    return out


def _hand_ladder_smooth(freqs_hz: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    """Independent re-implementation of the design doc's smoothing ladder
    (1/6 oct <4 kHz, 1/3 oct 4-10 kHz, 1/2 oct >=10 kHz) -- written fresh
    here rather than imported from linearization_envelope, so the
    corpus-anchor cross-check below tests the module's COMPOSITION, not
    just that it calls itself twice."""
    fine = smooth_fractional_octave(freqs_hz, magnitude_db, fraction=6)
    mid = smooth_fractional_octave(freqs_hz, magnitude_db, fraction=3)
    coarse = smooth_fractional_octave(freqs_hz, magnitude_db, fraction=2)
    return np.where(freqs_hz < 4_000.0, fine, np.where(freqs_hz < 10_000.0, mid, coarse))


def _hand_compute_sigma(
    curves_db: list[np.ndarray],
    grid_hz: np.ndarray,
    valid_band_hz: tuple[float, float],
) -> np.ndarray:
    """Independent re-derivation of compute_sigma_curve's formula (smooth
    each occurrence individually, center each to its own valid-band mean,
    sample std ddof=1 across occurrences) for the corpus-anchor tests.
    Assumes curves_db are already on grid_hz (the resample step is
    exercised separately, by the tests using _NATIVE_FREQS_HZ)."""
    mask = (grid_hz >= valid_band_hz[0]) & (grid_hz <= valid_band_hz[1])
    centered = []
    for c in curves_db:
        smoothed = _hand_ladder_smooth(grid_hz, c)
        ref = float(np.mean(smoothed[mask]))
        centered.append(smoothed - ref)
    return np.std(np.stack(centered), axis=0, ddof=1)


# --------------------------------------------------------------------------- #
# compute_sigma_curve -- basic occurrence-count behavior
# --------------------------------------------------------------------------- #


def test_three_identical_repeats_sigma_is_at_the_floor():
    primary = _with_occurrences("woofer", [_flat(0.0)] * 3)
    sigma = compute_sigma_curve(primary, valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert not np.isnan(sigma).any()
    # Bit-identical inputs through a deterministic pipeline -> exactly 0,
    # modulo floating-point round-trip noise.
    assert np.abs(sigma).max() < 1e-9


def test_single_occurrence_returns_none_not_nan():
    """No repeats at all -- compute_sigma_curve must refuse (None), never
    silently compute a 1-sample 'spread'."""
    primary = _driver_response("woofer", _flat(0.0))
    assert primary.repeat_responses == ()
    sigma = compute_sigma_curve(primary, valid_band_hz=(150.0, 4000.0))
    assert sigma is None


def test_ddof1_at_n1_is_silently_nan_not_an_exception():
    """Documents WHY compute_sigma_curve's N<2 guard exists: np.std with
    ddof=1 on a SINGLE sample divides by (1 - 1) == 0 and returns NaN with
    a RuntimeWarning, not a raised exception. The len(occurrences) < 2
    check in compute_sigma_curve is the only thing standing between a
    real N=1 capture and a silently-NaN envelope term feeding
    min()/argmin() downstream — this is the single most important
    correctness assertion in the module."""
    single_row = np.array([[1.0, 2.0, 3.0]])
    with pytest.warns(RuntimeWarning):
        result = np.std(single_row, axis=0, ddof=1)
    assert np.isnan(result).all()


def test_two_occurrences_returns_defined_curve_no_nan_no_warning():
    primary = _with_occurrences("woofer", [_flat(0.0), _flat(0.2)])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sigma = compute_sigma_curve(primary, valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert not np.isnan(sigma).any()
    assert (sigma >= 0.0).all()


# --------------------------------------------------------------------------- #
# compute_sigma_curve -- formula correctness
# --------------------------------------------------------------------------- #


def test_injected_known_offset_pattern_recovered_within_smoothing_tolerance():
    """A deliberately simple plateau, injected with a KNOWN per-occurrence
    deviation -- a pure formula-correctness check, not tied to any
    REPORT.md number. sample_std([-d, 0, d], ddof=1) == d exactly is the
    un-diluted target; valid-band centering dilutes it down (never up,
    never past 0) because the plateau is a real, nonzero slice of the
    wide valid band -- the recovered value must land in (0, d]."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    center_hz = 1000.0
    idx = int(np.argmin(np.abs(grid - center_hz)))
    actual_hz = float(grid[idx])
    deviation_db = 0.5
    half_width_oct = 0.4  # wide vs. the ~0.08-oct 1/6-oct window reach here

    curves = [
        _plateau(grid, actual_hz, -deviation_db, half_width_oct),
        _plateau(grid, actual_hz, 0.0, half_width_oct),
        _plateau(grid, actual_hz, deviation_db, half_width_oct),
    ]
    primary = _with_occurrences("woofer", curves, freqs_hz=grid)
    sigma = compute_sigma_curve(primary, valid_band_hz=(150.0, 20000.0), grid_hz=grid)
    assert sigma is not None
    assert 0.0 < sigma[idx] <= deviation_db
    # Not a hard floor -- just proof the smoothing/centering dilution is a
    # modest fraction of the un-diluted value, not the whole thing.
    assert sigma[idx] >= 0.5 * deviation_db


@pytest.mark.parametrize(
    "center_hz,target_sigma_db,label",
    [
        # REPORT.md "ACROSS-SESSION sigma (centered, smoothed)" octave
        # table -- see this file's module docstring for why these are
        # realistic-scale targets, not literal corpus-replay inputs.
        (250.0, 0.081, "umik_woofer_250hz"),
        (16000.0, 0.018, "imm_tweeter_16khz"),
    ],
)
def test_compute_sigma_corpus_regression_anchor(center_hz, target_sigma_db, label):
    grid = DEFAULT_ENVELOPE_GRID_HZ
    idx = int(np.argmin(np.abs(grid - center_hz)))
    actual_hz = float(grid[idx])
    half_width_oct = 0.3
    valid_band = (max(150.0, actual_hz / 8.0), min(20000.0, actual_hz * 8.0))

    # sample_std([-t, 0, t], ddof=1) == t exactly -- the un-diluted target.
    curves = [
        _plateau(grid, actual_hz, -target_sigma_db, half_width_oct),
        _plateau(grid, actual_hz, 0.0, half_width_oct),
        _plateau(grid, actual_hz, target_sigma_db, half_width_oct),
    ]
    primary = _with_occurrences("woofer", curves, freqs_hz=grid)

    got = compute_sigma_curve(primary, valid_band_hz=valid_band, grid_hz=grid)
    assert got is not None

    # Rigorous check: the module matches an independently-written
    # reimplementation of its own documented formula, everywhere.
    expected = _hand_compute_sigma(curves, grid, valid_band)
    np.testing.assert_allclose(got, expected, rtol=1e-9, atol=1e-12)

    # Loose check: the REPORT.md-seeded target is a realistic scale, not a
    # literal replay -- mean-centering dilution shrinks the recovered
    # value below the un-diluted target (never above), but not below half
    # of it on this construction.
    assert 0.0 < got[idx] <= target_sigma_db, (
        f"{label}: {got[idx]} outside (0, {target_sigma_db}]"
    )
    assert got[idx] >= 0.5 * target_sigma_db, (
        f"{label}: recovered sigma {got[idx]:.5f} implausibly far below the "
        f"REPORT.md-seeded target {target_sigma_db} -- construction or "
        f"formula regression, not expected dilution"
    )


def test_imm_3400hz_notch_uses_smoothed_sigma_never_raw_bin_spike():
    """Regression pin for REPORT.md finding 2: a narrow (~100 Hz),
    mic-intrinsic-shaped notch whose exact center wanders a few Hz between
    occurrences produces a RAW-bin sigma spike (REPORT.md observed
    0.74-1.19 dB there) -- but compute_sigma_curve must report the
    SMOOTHED value, which REPORT.md pins at <=0.485 dB ("compute
    repeatability limits from SMOOTHED sigma(f), never raw bins"). This
    test fails if that ordering regresses."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    # Literal, fixed wander centers (not derived from the grid) -- what
    # matters is that the grid's nearest bin to 3.4 kHz sits on each
    # notch's steep flank at a different offset per occurrence, which a
    # grid-derived center would not reliably reproduce.
    wander_centers_hz = [3390.0, 3397.0, 3405.0]

    def notch(center_hz: float, depth_db: float = 12.0, width_hz: float = 100.0):
        sigma_hz = width_hz / 2.355
        return -depth_db * np.exp(-0.5 * ((grid - center_hz) / sigma_hz) ** 2)

    curves = [notch(c) for c in wander_centers_hz]
    valid_band = (2000.0, 18000.0)
    primary = _with_occurrences(
        "tweeter", curves, freqs_hz=grid, validity_floor_hz=2000.0
    )

    got = compute_sigma_curve(primary, valid_band_hz=valid_band, grid_hz=grid)
    assert got is not None
    idx = int(np.argmin(np.abs(grid - 3400.0)))
    assert got[idx] <= 0.485

    # Prove this WOULD have failed the same bound on raw (unsmoothed)
    # bins -- the smoothing step is load-bearing for the assertion above,
    # not incidental.
    mask = (grid >= valid_band[0]) & (grid <= valid_band[1])
    raw_centered = [c - np.mean(c[mask]) for c in curves]
    raw_sigma = np.std(np.stack(raw_centered), axis=0, ddof=1)
    assert raw_sigma[idx] > 0.485


# --------------------------------------------------------------------------- #
# repeatability_limit
# --------------------------------------------------------------------------- #


def test_repeatability_limit_none_sigma_is_all_zero():
    """No evidence = no permission, never 'no constraint' -- a missing
    sigma (fewer than 2 occurrences) must never be treated as an
    unconstrained pass-through."""
    zero = repeatability_limit(None, tier="reference")
    assert np.all(zero == 0.0)
    assert zero.shape == DEFAULT_ENVELOPE_GRID_HZ.shape


def test_repeatability_limit_saturates_at_sentinel_for_tiny_sigma():
    tiny_sigma = np.full(DEFAULT_ENVELOPE_GRID_HZ.shape, 1e-8)
    curve = repeatability_limit(tiny_sigma, tier="reference")
    assert np.allclose(curve, ENVELOPE_CEILING_SENTINEL_DB)


def test_repeatability_limit_tapers_toward_zero_as_sigma_grows():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    small = repeatability_limit(np.full(grid.shape, 0.1), tier="reference")
    large = repeatability_limit(np.full(grid.shape, 50.0), tier="reference")
    assert np.all(large < small)
    assert np.all(large >= 0.0)


def test_repeatability_limit_rejects_unknown_tier():
    with pytest.raises(ValueError):
        repeatability_limit(None, tier="bogus")


# --------------------------------------------------------------------------- #
# mic_trust_limit / class_prior_limit -- shape + conservativeness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tier", MIC_TIERS)
def test_mic_trust_limit_taper_monotone_non_increasing(tier):
    curve = mic_trust_limit(DEFAULT_ENVELOPE_GRID_HZ, tier=tier)
    assert np.all(np.diff(curve) <= 1e-9)
    assert curve.max() == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)
    assert curve.min() == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("driver_class", DRIVER_CLASSES)
def test_class_prior_limit_taper_monotone_non_increasing(driver_class):
    curve = class_prior_limit(DEFAULT_ENVELOPE_GRID_HZ, driver_class=driver_class)
    assert np.all(np.diff(curve) <= 1e-9)
    assert curve.max() == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)


def test_mic_trust_limit_phone_is_conservative_vs_every_other_tier():
    """phone is the most conservative mic tier -- 'absent'/unknown mic
    information (mic_tier_for_model(None) -> 'phone') must never trust
    more than a tier we have real pedigree for."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    phone = mic_trust_limit(grid, tier="phone")
    for tier in MIC_TIERS:
        if tier == "phone":
            continue
        other = mic_trust_limit(grid, tier=tier)
        assert np.all(phone <= other + 1e-9), tier


def test_class_prior_limit_unknown_is_conservative_vs_every_other_class():
    """'unknown' is a valid, closed-vocabulary driver class (not an error)
    representing 'we don't know this driver's class' -- it must never
    trust more than any class we actually have a researched prior for."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    unknown = class_prior_limit(grid, driver_class="unknown")
    for driver_class in DRIVER_CLASSES:
        if driver_class == "unknown":
            continue
        other = class_prior_limit(grid, driver_class=driver_class)
        assert np.all(unknown <= other + 1e-9), driver_class


def test_mic_trust_limit_rejects_unknown_tier():
    with pytest.raises(ValueError):
        mic_trust_limit(DEFAULT_ENVELOPE_GRID_HZ, tier="iphone")


def test_class_prior_limit_rejects_unknown_class():
    with pytest.raises(ValueError):
        class_prior_limit(DEFAULT_ENVELOPE_GRID_HZ, driver_class="tweeter")


# --------------------------------------------------------------------------- #
# linearity_limit / invertibility_limit -- stub contract
# --------------------------------------------------------------------------- #


def test_linearity_and_invertibility_stubs_return_finite_sentinel_everywhere():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    for curve in (linearity_limit(grid), invertibility_limit(grid)):
        assert np.all(np.isfinite(curve))
        assert np.all(curve == ENVELOPE_CEILING_SENTINEL_DB)


# --------------------------------------------------------------------------- #
# compose_envelope
# --------------------------------------------------------------------------- #


def _zero_sigma_primary(role: str, freqs_hz: np.ndarray = _NATIVE_FREQS_HZ) -> DriverResponse:
    """3 bit-identical occurrences -> sigma ~ 0 everywhere, so
    repeatability_limit saturates at the sentinel and doesn't mask the
    mic_trust / class_prior shapes under test."""
    flat = _flat(0.0, freqs_hz=freqs_hz)
    return _with_occurrences(role, [flat, flat.copy(), flat.copy()], freqs_hz=freqs_hz)


def test_compose_envelope_out_of_band_bins_are_zero_fixed_reason_and_win_over_argmin():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter")
    curve = compose_envelope(
        "tweeter",
        primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="reference",
        driver_class="unknown",
        grid_hz=grid,
    )
    below_excited = grid < 2000.0
    above_excited = grid > 18000.0
    out_of_band = below_excited | above_excited
    assert out_of_band.any()  # sanity: the fixture actually has OOB bins
    assert np.all(curve.allowed_depth_db[out_of_band] == 0.0)
    for i in np.where(out_of_band)[0]:
        assert curve.reason[i] == ReasonCode.OUT_OF_BAND


def test_compose_envelope_out_of_band_respects_conservative_validity_floor():
    """The in-band region also excludes bins below the WORST (highest)
    validity_floor_hz across primary + repeats, not the best."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    flat = _flat(0.0, freqs_hz=grid)
    lenient = _driver_response("woofer", flat, freqs_hz=grid, validity_floor_hz=150.0)
    strict = _driver_response("woofer", flat, freqs_hz=grid, validity_floor_hz=400.0)
    primary = _driver_response(
        "woofer", flat, freqs_hz=grid, validity_floor_hz=150.0,
        repeat_responses=(lenient, strict),
    )
    curve = compose_envelope(
        "woofer", primary,
        excited_band_hz=(150.0, 4000.0),
        mic_tier="reference",
        grid_hz=grid,
    )
    # Between the two floors (150-400 Hz): OUT_OF_BAND despite being
    # inside excited_band_hz, because the worst occurrence's floor (400)
    # governs.
    between = (grid >= 150.0) & (grid < 400.0)
    assert between.any()
    for i in np.where(between)[0]:
        assert curve.reason[i] == ReasonCode.OUT_OF_BAND
        assert curve.allowed_depth_db[i] == 0.0


def test_compose_envelope_all_floors_none_is_entirely_out_of_band():
    """No occurrence has a validity floor at all -- no gating evidence
    anywhere means no in-band claim anywhere (same doctrine as
    sigma_db=None -> all-zero repeatability)."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    flat = _flat(0.0, freqs_hz=grid)
    repeat = _driver_response("woofer", flat, freqs_hz=grid, validity_floor_hz=None)
    primary = _driver_response(
        "woofer", flat, freqs_hz=grid, validity_floor_hz=None, repeat_responses=(repeat,)
    )
    curve = compose_envelope(
        "woofer", primary,
        excited_band_hz=(150.0, 4000.0),
        mic_tier="reference",
        grid_hz=grid,
    )
    assert np.all(curve.allowed_depth_db == 0.0)
    assert all(r == ReasonCode.OUT_OF_BAND for r in curve.reason)


def test_compose_envelope_fitted_reason_when_no_term_binds():
    """Deep inside every term's flat/unconstrained region, the winning
    value equals the sentinel -- the honest reason is FITTED, not
    whichever term happened to tie for the win (argmin's first-index
    tie-break would otherwise always blame mic-tier)."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="reference",  # mic_trust flat sentinel up to 8 kHz
        driver_class="beryllium_diamond_dome",  # class_prior flat to 17 kHz
        grid_hz=grid,
    )
    idx = int(np.argmin(np.abs(grid - 3000.0)))  # well inside every flat region
    assert curve.reason[idx] == ReasonCode.FITTED
    assert curve.allowed_depth_db[idx] == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)


def test_compose_envelope_reports_limiting_term_reason_outside_flat_region():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="phone",  # mic_trust tapers 3k->8k, well below class_prior
        driver_class="beryllium_diamond_dome",
        grid_hz=grid,
    )
    idx = int(np.argmin(np.abs(grid - 5000.0)))  # inside phone's taper region
    assert curve.reason[idx] == ReasonCode.LIMITED_BY_MIC_TIER
    assert curve.allowed_depth_db[idx] < ENVELOPE_CEILING_SENTINEL_DB


def test_compose_envelope_allowed_depth_bounded_by_every_smoothed_term():
    """allowed_depth_db <= every term, everywhere -- compared against each
    term's OWN independently-smoothed curve, not its raw curve.
    allowed_depth_db is itself ladder-smoothed once more after the min()
    (design doc: 'so term handoffs have no cliffs'); smoothing is a
    non-negative weighted average, so if min_curve(j) <= term_k(j) for
    every j (true by definition of min), then
    smooth(min_curve)(i) <= smooth(term_k)(i) for every i too (averaging
    preserves a pointwise <= ordering). Comparing against term_k's RAW
    (unsmoothed) curve instead does NOT hold in general -- smoothing can
    blend a nearby higher value into a point where term_k itself was
    momentarily near its own zero-taper kink -- so this test deliberately
    smooths each term the same way before comparing (verified upstream:
    an unsmoothed-vs-smoothed comparison is provably false near those
    kinks, not just untested)."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    for tier in MIC_TIERS:
        for driver_class in ("compression_horn", "unknown"):
            curve = compose_envelope(
                "tweeter", primary,
                excited_band_hz=(150.0, 20000.0),
                mic_tier=tier,
                driver_class=driver_class,
                grid_hz=grid,
            )
            for term_curve in curve.terms.values():
                smoothed_term = _hand_ladder_smooth(grid, term_curve)
                assert np.all(curve.allowed_depth_db <= smoothed_term + 1e-9)


def test_compose_envelope_n_repeats_and_sigma_reported():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)
    curve = compose_envelope(
        "woofer", primary,
        excited_band_hz=(150.0, 4000.0),
        mic_tier="consumer",
        grid_hz=grid,
    )
    assert curve.n_repeats == 2
    assert curve.sigma_db is not None
    assert curve.role == "woofer"
    assert curve.mic_tier == "consumer"
    assert curve.driver_class == "unknown"  # default


def test_compose_envelope_no_repeats_sigma_none_but_still_composes():
    """A driver that never repeated (old-shaped program, or a
    single-occurrence session) still gets a real envelope -- repeatability
    just contributes zero everywhere (no evidence, no permission), it
    does not crash composition."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _driver_response("woofer", _flat(0.0, freqs_hz=grid), freqs_hz=grid)
    curve = compose_envelope(
        "woofer", primary,
        excited_band_hz=(150.0, 4000.0),
        mic_tier="reference",
        grid_hz=grid,
    )
    assert curve.sigma_db is None
    assert curve.n_repeats == 0
    in_band = (grid >= 150.0) & (grid <= 4000.0)
    assert np.all(curve.allowed_depth_db[in_band] == 0.0)
    for i in np.where(in_band)[0]:
        assert curve.reason[i] == ReasonCode.LIMITED_BY_REPEATABILITY


def test_compose_envelope_rejects_unknown_tier_and_class():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)
    with pytest.raises(ValueError):
        compose_envelope(
            "woofer", primary, excited_band_hz=(150.0, 4000.0),
            mic_tier="bogus", grid_hz=grid,
        )
    with pytest.raises(ValueError):
        compose_envelope(
            "woofer", primary, excited_band_hz=(150.0, 4000.0),
            mic_tier="reference", driver_class="bogus", grid_hz=grid,
        )


# --------------------------------------------------------------------------- #
# vocabulary pins
# --------------------------------------------------------------------------- #


def test_reason_code_vocabulary_is_stable():
    """Values are persisted/serialized surfaces once wired to UI (design
    doc: 'every band emits a reason code'). Pin the exact strings so a
    rename doesn't silently change what's on disk / in the API."""
    assert ReasonCode.FITTED == "envelope_fitted"
    assert ReasonCode.LIMITED_BY_MIC_TIER == "envelope_limited_by_mic_tier"
    assert ReasonCode.LIMITED_BY_REPEATABILITY == "envelope_limited_by_repeatability"
    assert ReasonCode.LIMITED_BY_NONLINEARITY == "envelope_limited_by_nonlinearity"
    assert ReasonCode.LIMITED_BY_EXCESS_PHASE == "envelope_limited_by_excess_phase"
    assert ReasonCode.LIMITED_BY_CLASS_PRIOR == "envelope_limited_by_class_prior"
    assert (
        ReasonCode.LIMITED_BY_VERIFY_DIVERGENCE
        == "envelope_limited_by_verify_divergence"
    )
    assert ReasonCode.OUT_OF_BAND == "envelope_out_of_band"
    assert len(list(ReasonCode)) == 8


def test_mic_tiers_and_driver_classes_vocabulary_is_stable():
    assert MIC_TIERS == ("reference", "consumer", "phone")
    assert DRIVER_CLASSES == (
        "compression_horn",
        "soft_dome",
        "metal_dome",
        "beryllium_diamond_dome",
        "ribbon_amt",
        "unknown",
    )


def test_default_envelope_grid_shape_and_range():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    assert grid.shape == (176,)
    assert grid[0] == pytest.approx(150.0)
    assert grid[-1] == pytest.approx(20_000.0)
    assert np.all(np.diff(grid) > 0)  # strictly increasing


def test_envelope_term_and_curve_are_frozen_dataclasses():
    term = EnvelopeTerm(ReasonCode.FITTED, np.zeros(3))
    with pytest.raises(Exception):
        term.code = ReasonCode.OUT_OF_BAND  # type: ignore[misc]

    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)
    curve = compose_envelope(
        "woofer", primary, excited_band_hz=(150.0, 4000.0),
        mic_tier="reference", grid_hz=grid,
    )
    assert isinstance(curve, EnvelopeCurve)
    with pytest.raises(Exception):
        curve.role = "tweeter"  # type: ignore[misc]
