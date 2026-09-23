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

import math
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
    compose_envelope,
    compute_sigma_curve,
    mic_trust_limit,
    position_spread_db,
    repeatability_limit,
    spatial_exclusion_limit,
)
from jasper.active_speaker.linearization_fit import (
    fit_driver_linearization,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.audio_measurement.spatial_combine import (
    BandSpread,
)

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


def test_compute_sigma_curve_off_grid_valid_band_returns_none_no_warning():
    """valid_band_hz that does not overlap grid_hz at all (here: entirely
    below DEFAULT_ENVELOPE_GRID_HZ's 150 Hz floor) must return None -- the
    same 'no evidence, no guess' contract as the N<2 occurrence guard --
    and must NEVER silently produce an all-NaN curve via np.mean on an
    empty valid-mask slice (which would also emit a RuntimeWarning)."""
    primary = _with_occurrences("woofer", [_flat(0.0), _flat(0.2)])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sigma = compute_sigma_curve(primary, valid_band_hz=(20.0, 100.0))
    assert sigma is None


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
# mic_trust_limit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tier", MIC_TIERS)
def test_mic_trust_limit_taper_monotone_non_increasing(tier):
    curve = mic_trust_limit(DEFAULT_ENVELOPE_GRID_HZ, tier=tier)
    assert np.all(np.diff(curve) <= 1e-9)
    assert curve.max() == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)
    assert curve.min() == pytest.approx(0.0, abs=1e-9)


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


def test_mic_trust_limit_rejects_unknown_tier():
    with pytest.raises(ValueError):
        mic_trust_limit(DEFAULT_ENVELOPE_GRID_HZ, tier="iphone")


@pytest.mark.parametrize(
    "tier,full_to_hz,taper_zero_hz",
    [
        # 2026-08-29 horn-droop correction ruling: reference widens from
        # 8 k/16 k to 12 k/20 k so the allowance stays full through the
        # horn's measured droop and only backs off past the tweeter's own
        # beaming onset (docs/active-speaker-tuning-layers-design.md,
        # "Cold-start priors"). Consumer/phone are the unchanged pre-ruling
        # design-doc rows -- parametrized here too so a retune of either
        # cannot slip through unnoticed.
        ("reference", 12_000.0, 20_000.0),
        ("consumer", 6_000.0, 12_000.0),
        ("phone", 3_000.0, 8_000.0),
    ],
)
def test_mic_trust_limit_pins_full_to_and_taper_zero_hz_by_tier(
    tier, full_to_hz, taper_zero_hz
):
    """Pins ``_MIC_TRUST_TABLE_HZ``'s actual per-tier breakpoints in Hz.

    The shape tests above (monotone, sentinel max, zero min, phone most
    conservative) hold for ANY valid flat-then-taper table, so none of them
    would catch a silent retune of a tier's own numbers. This checks the
    three exact values :func:`_flat_then_taper`'s shape guarantees: full
    trust AT ``full_to``, zero AT ``taper_zero``, and the taper's log-domain
    midpoint (the geometric mean of the two), where the octave-linear ramp
    must cross exactly half the sentinel.
    """
    freqs_hz = np.array(
        [full_to_hz, math.sqrt(full_to_hz * taper_zero_hz), taper_zero_hz]
    )
    curve = mic_trust_limit(freqs_hz, tier=tier)
    assert curve[0] == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)
    assert curve[1] == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB / 2.0)
    assert curve[2] == pytest.approx(0.0, abs=1e-9)


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
        mic_tier="reference",  # mic_trust flat sentinel up to 12 kHz
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


# --------------------------------------------------------------------------- #
# A term's EXACT zero is a hard boundary, like OUT_OF_BAND (issue #1752)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tier", MIC_TIERS)
def test_compose_envelope_zeroes_where_mic_trust_reaches_exactly_zero(tier):
    """#1752: no allowed depth survives past a term's own exact zero.

    ``mic_trust_limit`` is exactly 0 from its tier's ``taper_zero`` up — the
    frequency above which the calibrated microphone resolves nothing. Before
    this rule the final ladder-smoothing pass blurred in-band depth back
    across that boundary, so the composed envelope granted correction
    permission at frequencies its own mic-trust term said were unmeasurable.
    Measured on the S0 replay at ``reference``: 1.4846 dB of allowed depth at
    16444.9 Hz, non-zero all the way to 18912.3 Hz.

    Asserted on EVERY tier, since the leak was a property of the smoother and
    not of one tier's breakpoints.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(150.0, 20_000.0),
        mic_tier=tier,
        driver_class="metal_dome",   # its own taper_zero is off-grid (32 kHz)
        grid_hz=grid,
    )
    mic_trust = curve.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    zero_bins = mic_trust <= 0.0
    assert zero_bins.any(), "fixture must reach the taper zero on this grid"
    # Exactly 0 — not "small", not "close to".
    assert np.all(curve.allowed_depth_db[zero_bins] == 0.0)
    # ...and there is still real permission just below the boundary, so this
    # is a boundary assertion rather than an all-zero envelope trivially
    # satisfying it.
    assert curve.allowed_depth_db[~zero_bins].max() > 1.0


def test_compose_envelope_still_smooths_inside_the_non_zero_region():
    """Hardening the boundary must not introduce a cliff INSIDE the band.

    The ladder pass is untouched wherever every term is non-zero: the
    composed curve stays smooth bin-to-bin across the interior, and the
    envelope's own taper (each term's explicit octave-linear ramp) is what
    brings it down to the boundary — the blur was never the soft handoff, it
    was permission extending past a term's zero.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(150.0, 20_000.0),
        mic_tier="reference",
        driver_class="compression_horn",
        grid_hz=grid,
    )
    depth = curve.allowed_depth_db
    interior = (grid > 200.0) & (grid < 15_000.0)
    steps = np.abs(np.diff(depth[interior]))
    assert steps.max() < 2.0, "interior must stay smooth, no new cliff"


def test_compose_envelope_out_of_band_treatment_is_unchanged_by_the_zero_rule():
    """OUT_OF_BAND keeps its own behaviour: zeroed on both sides of the
    smoothing pass, and still reported as ``OUT_OF_BAND`` rather than being
    relabelled by the term-zero rule that now sits beside it."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="reference",
        driver_class="metal_dome",
        grid_hz=grid,
    )
    below = grid < 2000.0
    assert below.any()
    assert np.all(curve.allowed_depth_db[below] == 0.0)
    assert all(curve.reason[i] == ReasonCode.OUT_OF_BAND
               for i in np.flatnonzero(below))


def test_compose_envelope_no_evidence_sigma_is_all_zero_as_before():
    """``sigma_db=None`` makes ``repeatability_limit`` an ALL-zero term, so
    every bin is now a hard zero — which is exactly the all-zero envelope
    that composition already produced (smoothing zeros yields zeros). Pinned
    because it is the one term that reaches zero everywhere rather than at a
    band edge, and its behaviour must not have moved."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(150.0, 20_000.0),
        mic_tier="reference",
        driver_class="metal_dome",
        grid_hz=grid,
        sigma_db=None,
    )
    assert np.all(curve.allowed_depth_db == 0.0)


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


# --------------------------------------------------------------------------- #
# compose_envelope -- sigma_db injection seam (review finding S1)
# --------------------------------------------------------------------------- #


def test_compose_envelope_caller_supplied_sigma_db_used_verbatim():
    """sigma_db passed explicitly to compose_envelope must be used AS-IS
    for the repeatability term, bypassing compute_sigma_curve entirely --
    the seam PR-C's wiring layer uses to inject its own composed (floored,
    N-gated) sigma. Constructed so the caller-supplied value differs
    sharply from what compute_sigma_curve would have computed internally
    (near-zero, from 3 bit-identical occurrences), so the two envelopes
    provably differ rather than coincidentally matching."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)  # internal sigma ~ 0 everywhere
    idx = int(np.argmin(np.abs(grid - 3000.0)))  # inside every flat/unconstrained region

    default_curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="reference",
        driver_class="beryllium_diamond_dome",
        grid_hz=grid,
    )
    # Same fixture/bin as test_compose_envelope_fitted_reason_when_no_term_binds:
    # near-zero internal sigma means repeatability saturates, so nothing binds.
    assert default_curve.reason[idx] == ReasonCode.FITTED

    caller_sigma = np.full(grid.shape, 50.0)  # far past sigma_tolerable for every tier
    explicit_curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(2000.0, 18000.0),
        mic_tier="reference",
        driver_class="beryllium_diamond_dome",
        grid_hz=grid,
        sigma_db=caller_sigma,
    )
    # Recorded verbatim -- not the internally-computed near-zero value.
    assert explicit_curve.sigma_db is not None
    np.testing.assert_array_equal(explicit_curve.sigma_db, caller_sigma)
    assert not np.allclose(explicit_curve.sigma_db, default_curve.sigma_db)

    # The (now loose) repeatability term binds where nothing bound before,
    # so the two envelopes provably differ at this bin.
    assert explicit_curve.reason[idx] == ReasonCode.LIMITED_BY_REPEATABILITY
    assert explicit_curve.allowed_depth_db[idx] < default_curve.allowed_depth_db[idx]

    # n_repeats is unaffected by which sigma source was used -- it always
    # reports primary's own occurrence count.
    assert explicit_curve.n_repeats == default_curve.n_repeats == 2


def test_compose_envelope_explicit_none_sigma_forces_all_zero_repeatability():
    """Explicit sigma_db=None must force the SAME 'no evidence' contract as
    compute_sigma_curve's own <2-occurrence guard, even when primary DOES
    have real in-capture repeats -- e.g. PR-C's paired N>=3-for-both-
    drivers gate deciding this driver's repeats don't count yet because its
    partner hasn't repeated enough. n_repeats still reports the real
    occurrence count, distinguishing this from an actually-unrepeated
    capture (test_compose_envelope_no_repeats_sigma_none_but_still_composes,
    where n_repeats == 0)."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)  # 2 real in-capture repeats
    curve = compose_envelope(
        "woofer", primary,
        excited_band_hz=(150.0, 4000.0),
        mic_tier="reference",
        grid_hz=grid,
        sigma_db=None,
    )
    assert curve.sigma_db is None
    assert curve.n_repeats == 2
    in_band = (grid >= 150.0) & (grid <= 4000.0)
    assert np.all(curve.allowed_depth_db[in_band] == 0.0)
    for i in np.where(in_band)[0]:
        assert curve.reason[i] == ReasonCode.LIMITED_BY_REPEATABILITY


def test_compose_envelope_sigma_db_shape_mismatch_raises_value_error():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)
    wrong_shape = np.zeros(len(grid) - 1)
    with pytest.raises(ValueError):
        compose_envelope(
            "woofer", primary,
            excited_band_hz=(150.0, 4000.0),
            mic_tier="reference",
            grid_hz=grid,
            sigma_db=wrong_shape,
        )


def test_compose_envelope_sigma_db_wrong_type_raises_type_error():
    """Belt-and-braces for the sigma_db: np.ndarray | None | object
    signature: the `object` arm exists only so the module-private
    _COMPUTE sentinel type-checks as a default value, not to accept
    arbitrary types as real input -- anything that is neither the
    sentinel, None, nor an ndarray is a caller bug, not a fourth state."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("woofer", freqs_hz=grid)
    with pytest.raises(TypeError):
        compose_envelope(
            "woofer", primary,
            excited_band_hz=(150.0, 4000.0),
            mic_tier="reference",
            grid_hz=grid,
            sigma_db=[0.0] * len(grid),  # type: ignore[arg-type]
        )


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
    assert (
        ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION
        == "envelope_limited_by_spatial_exclusion"
    )
    assert (
        ReasonCode.LIMITED_BY_VERIFY_DIVERGENCE
        == "envelope_limited_by_verify_divergence"
    )
    assert (
        ReasonCode.BEYOND_MEASUREMENT_CONFIDENCE
        == "envelope_beyond_measurement_confidence"
    )
    assert ReasonCode.OUT_OF_BAND == "envelope_out_of_band"
    assert len(list(ReasonCode)) == 7


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


# --------------------------------------------------------------------------- #
# spatial_exclusion_limit -- interval rasterization (PR-6a)
# --------------------------------------------------------------------------- #

# An octave-spaced 3-bin grid, chosen so every cell edge is exactly a
# geometric midpoint this test can write down: cells are
#   1000 Hz -> [1000/sqrt2, 1414.214] = [707.107, 1414.214]
#   2000 Hz -> [1414.214, 2828.427]
#   4000 Hz -> [2828.427, 4000*sqrt2] = [2828.427, 5656.854]
_COARSE_GRID_HZ = np.array([1000.0, 2000.0, 4000.0])
_SQRT2 = float(np.sqrt(2.0))


def test_spatial_exclusion_limit_no_intervals_is_all_sentinel():
    for intervals in ((), []):
        curve = spatial_exclusion_limit(DEFAULT_ENVELOPE_GRID_HZ, intervals)
        assert np.all(curve == ENVELOPE_CEILING_SENTINEL_DB)


def test_spatial_exclusion_limit_any_overlap_excludes_a_partly_covered_bin():
    """The edge rule, on the case where the two candidate rules disagree.

    ``(1400, 1420)`` contains NO grid frequency -- a "the bin's own frequency
    is inside the interval" rule would exclude nothing at all -- but it
    straddles the 1414.214 Hz cell boundary, so it overlaps the cells of both
    the 1000 Hz and the 2000 Hz bin and both are excluded. ``(2900, 3000)``
    likewise contains no grid frequency and lies wholly inside the 4000 Hz
    bin's cell, which is therefore excluded on its own.
    """
    both = spatial_exclusion_limit(_COARSE_GRID_HZ, ((1400.0, 1420.0),))
    assert list(both) == [0.0, 0.0, ENVELOPE_CEILING_SENTINEL_DB]

    top_only = spatial_exclusion_limit(_COARSE_GRID_HZ, ((2900.0, 3000.0),))
    assert list(top_only) == [
        ENVELOPE_CEILING_SENTINEL_DB, ENVELOPE_CEILING_SENTINEL_DB, 0.0,
    ]

    # The point-in-interval alternative this rule was chosen over: neither
    # interval contains a single grid frequency.
    for interval in ((1400.0, 1420.0), (2900.0, 3000.0)):
        assert not ((_COARSE_GRID_HZ >= interval[0])
                    & (_COARSE_GRID_HZ <= interval[1])).any()


def test_spatial_exclusion_limit_outer_cell_edges_mirror_the_inner_half_step():
    """The first and last bins have no neighbour on one side, so their outer
    cell edge mirrors their own inner half-step: 1000/sqrt2 and 4000*sqrt2."""
    lo_edge = 1000.0 / _SQRT2
    hi_edge = 4000.0 * _SQRT2

    assert spatial_exclusion_limit(
        _COARSE_GRID_HZ, ((lo_edge - 5.0, lo_edge - 1.0),)
    )[0] == ENVELOPE_CEILING_SENTINEL_DB
    assert spatial_exclusion_limit(
        _COARSE_GRID_HZ, ((lo_edge - 5.0, lo_edge + 1.0),)
    )[0] == 0.0

    assert spatial_exclusion_limit(
        _COARSE_GRID_HZ, ((hi_edge + 1.0, hi_edge + 5.0),)
    )[2] == ENVELOPE_CEILING_SENTINEL_DB
    assert spatial_exclusion_limit(
        _COARSE_GRID_HZ, ((hi_edge - 1.0, hi_edge + 5.0),)
    )[2] == 0.0


def test_spatial_exclusion_limit_intervals_off_the_grid_exclude_nothing():
    curve = spatial_exclusion_limit(
        DEFAULT_ENVELOPE_GRID_HZ, ((1.0, 20.0), (30_000.0, 40_000.0)),
    )
    assert np.all(curve == ENVELOPE_CEILING_SENTINEL_DB)


def test_spatial_exclusion_limit_unions_overlapping_and_unsorted_intervals():
    """Two producers' intervals arrive already merged in production, but the
    function must not depend on that: overlapping and out-of-order pairs
    simply union."""
    unsorted_overlapping = spatial_exclusion_limit(
        _COARSE_GRID_HZ, ((2900.0, 3000.0), (1400.0, 1420.0), (2950.0, 3100.0)),
    )
    assert list(unsorted_overlapping) == [0.0, 0.0, 0.0]


def test_spatial_exclusion_limit_rejects_a_descending_interval():
    """A reversed pair would intersect nothing and silently UNDER-exclude --
    the unsafe direction -- so it raises instead of quietly passing."""
    with pytest.raises(ValueError, match="descending"):
        spatial_exclusion_limit(_COARSE_GRID_HZ, ((2000.0, 1000.0),))


def test_spatial_exclusion_limit_single_bin_grid_reduces_to_containment():
    one = np.array([1000.0])
    assert spatial_exclusion_limit(one, ((900.0, 1100.0),))[0] == 0.0
    assert spatial_exclusion_limit(one, ((1100.0, 1200.0),))[0] == (
        ENVELOPE_CEILING_SENTINEL_DB
    )


# --------------------------------------------------------------------------- #
# Position spread
# --------------------------------------------------------------------------- #


def _band(center_hz: float, sigma_db: float, *, f_lo=None, f_hi=None) -> BandSpread:
    return BandSpread(
        center_hz=center_hz,
        f_lo=center_hz / _SQRT2 if f_lo is None else f_lo,
        f_hi=center_hz * _SQRT2 if f_hi is None else f_hi,
        sigma_db=sigma_db,
        # Deliberately huge: this term must never read max_sigma_db (the
        # structure spread the null instruments own). If it ever did, every
        # assertion below would collapse toward zero.
        max_sigma_db=99.0,
        n_bins=100,
    )


_COMPOSE_BAND_HZ = (2000.0, 18_000.0)


def _compose(primary, **kwargs) -> EnvelopeCurve:
    return compose_envelope(
        "tweeter", primary,
        excited_band_hz=_COMPOSE_BAND_HZ,
        mic_tier="reference",
        driver_class="compression_horn",
        grid_hz=DEFAULT_ENVELOPE_GRID_HZ,
        **kwargs,
    )


def test_compose_envelope_absent_cloud_evidence_is_byte_identical():
    """The additivity contract: omitted, ``None``, and empty all compose to
    the pre-PR-6a curve exactly -- same numbers, same reasons, and a
    ``terms`` mapping still holding exactly the two depth terms."""
    primary = _zero_sigma_primary("tweeter", freqs_hz=DEFAULT_ENVELOPE_GRID_HZ)
    baseline = _compose(primary)
    variants = (
        _compose(primary, excluded_bands_hz=None, band_spread=None, n_positions=None),
        _compose(primary, excluded_bands_hz=()),
        _compose(primary, band_spread=(), n_positions=10),
        _compose(primary, excluded_bands_hz=(), band_spread=(), n_positions=10),
    )
    for variant in variants:
        assert np.array_equal(variant.allowed_depth_db, baseline.allowed_depth_db)
        assert variant.reason == baseline.reason
        assert set(variant.terms) == set(baseline.terms)
    assert set(baseline.terms) == {
        ReasonCode.LIMITED_BY_MIC_TIER,
        ReasonCode.LIMITED_BY_REPEATABILITY,
    }


def test_compose_envelope_spatial_exclusion_hard_zeroes_and_names_itself():
    """Excluded bins read EXACTLY 0.0 and report
    ``LIMITED_BY_SPATIAL_EXCLUSION``; every OTHER bin is untouched, bit for
    bit.

    The second half is the design decision under test. The exclusion is
    applied after the ladder-smoothing pass, so its zeros never enter the
    smoothing window -- an interval that removes a null must not also remove
    correction depth from the ordinary response beside it. Applied before,
    the half-octave window at these frequencies would have bled several dB
    outward.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = _compose(primary, excluded_bands_hz=((8000.0, 9500.0),))
    baseline = _compose(primary)

    excluded = curve.terms[ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION] <= 0.0
    assert excluded.sum() >= 4
    assert np.all(curve.allowed_depth_db[excluded] == 0.0)
    for i in np.flatnonzero(excluded):
        assert curve.reason[i] == ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION
        assert baseline.allowed_depth_db[i] > 1.0  # would have been correctable

    assert np.array_equal(
        curve.allowed_depth_db[~excluded], baseline.allowed_depth_db[~excluded]
    )
    assert tuple(np.array(curve.reason)[~excluded]) == tuple(
        np.array(baseline.reason)[~excluded]
    )


def test_compose_envelope_cloud_terms_can_only_narrow():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    baseline = _compose(primary)
    narrowed = _compose(
        primary,
        excluded_bands_hz=((8000.0, 9500.0), (14_000.0, 15_500.0)),
        band_spread=(_band(4000.0, 3.0), _band(8000.0, 5.0)),
        n_positions=9,
    )
    assert np.all(narrowed.allowed_depth_db <= baseline.allowed_depth_db + 1e-12)
    assert np.any(narrowed.allowed_depth_db < baseline.allowed_depth_db)


@pytest.mark.parametrize("bands,positions", [(None, 2), ((), None)])
def test_compose_envelope_requires_band_spread_and_n_positions_together(bands, positions):
    primary = _zero_sigma_primary("tweeter", freqs_hz=DEFAULT_ENVELOPE_GRID_HZ)
    with pytest.raises(ValueError):
        _compose(primary, band_spread=bands, n_positions=positions)


@pytest.mark.parametrize("positions,spread", [(0, None), (1, None), (2, 6 / math.sqrt(2)), (4, 3.0)])
def test_position_spread_is_disclosed_without_limiting_depth(positions, spread):
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = _compose(primary, band_spread=(_band(3000.0, 6.0),), n_positions=positions)
    baseline = _compose(primary)
    idx = int(np.argmin(np.abs(grid - 3000.0)))
    assert curve.reason[idx] == ReasonCode.FITTED
    assert np.array_equal(curve.allowed_depth_db, baseline.allowed_depth_db)
    fit = fit_driver_linearization(primary, curve).to_dict()
    assert fit["reason_summary"]["4000"] == ReasonCode.FITTED
    assert fit["position_spread_db"] is None if spread is None else fit["position_spread_db"]["4000"] == pytest.approx(spread)
    if spread is None:
        assert curve.position_spread_db is None
    else:
        assert curve.position_spread_db[idx] == pytest.approx(spread)
        assert np.isnan(curve.position_spread_db[0])


@pytest.mark.parametrize("driver_class,full_to", [
    ("unknown", 6000), ("compression_horn", 10000), ("soft_dome", 14000),
    ("metal_dome", 16000), ("beryllium_diamond_dome", 17000), ("ribbon_amt", 17000),
])
def test_class_prior_is_disclosed_without_limiting_depth(driver_class, full_to):
    primary = _zero_sigma_primary("tweeter")
    curve = compose_envelope("tweeter", primary, excited_band_hz=(2000, 20000),
                             mic_tier="reference", driver_class=driver_class)
    baseline = compose_envelope("tweeter", primary, excited_band_hz=(2000, 20000), mic_tier="reference")
    assert np.array_equal(curve.allowed_depth_db, baseline.allowed_depth_db)
    assert curve.reason == baseline.reason
    assert curve.class_prior_hz == {"full_to_hz": full_to, "taper_zero_hz": full_to * 2}


def test_position_spread_overlapping_bands_report_the_larger_error():
    result = position_spread_db(np.array([2000, 3000, 4000]),
                                (_band(3000, 2, f_lo=2000, f_hi=4000), _band(3000, 6, f_lo=2500, f_hi=3500)), n_positions=4)
    assert result == pytest.approx([1, 3, 1])


def test_default_grid_step_pins_the_coarseness_the_edge_rule_is_about():
    """Pins the grid coarseness that makes a partial-coverage rule necessary."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    step_pct = float(grid[1] / grid[0] - 1.0) * 100.0
    assert step_pct == pytest.approx(2.8354, abs=0.0002)
    idx = int(np.argmin(np.abs(grid - 8700.0)))
    assert float(grid[idx]) == pytest.approx(8644.8, abs=0.1)
    assert float(grid[idx + 1] - grid[idx]) == pytest.approx(245.1, abs=0.1)
