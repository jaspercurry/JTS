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
    class_prior_limit,
    compose_envelope,
    compute_sigma_curve,
    mic_trust_limit,
    position_stability_limit,
    repeatability_limit,
    spatial_exclusion_limit,
)
from jasper.active_speaker.linearization_fit import (
    PER_FILTER_CUT_CAP_DB,
    complex_correction_response,
    fit_driver_linearization,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.interference_nulls import identify_interference_nulls
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.audio_measurement.spatial_combine import (
    BandSpread,
    combine_positions,
    merged_true_intervals,
)
from tests._flat_lin_corpus import (
    S0_MAIN,
    requires_s0_curves,
    s0_position_captures,
    s0_position_driver_response,
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


def test_compose_envelope_zeroes_where_the_class_prior_reaches_exactly_zero():
    """The rule is about ANY term, not mic-trust specifically.

    ``class_prior_limit`` for the conservative ``unknown`` class tapers to
    exactly 0 at 12 kHz — below ``reference`` mic-trust's own 16 kHz zero — so
    this bin set is owned by a different term entirely.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    curve = compose_envelope(
        "tweeter", primary,
        excited_band_hz=(150.0, 20_000.0),
        mic_tier="reference",
        driver_class="unknown",
        grid_hz=grid,
    )
    class_prior = curve.terms[ReasonCode.LIMITED_BY_CLASS_PRIOR]
    zero_bins = class_prior <= 0.0
    assert zero_bins.any()
    # The class prior zeroes BELOW where reference mic-trust does, so these
    # bins prove the rule is term-agnostic.
    mic_trust = curve.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    assert (mic_trust[zero_bins] > 0.0).any()
    assert np.all(curve.allowed_depth_db[zero_bins] == 0.0)


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
    # The approach to the boundary is a taper, not a plunge from the ceiling:
    # the last bin that still has permission holds only a couple of dB.
    non_zero = np.flatnonzero(depth > 0.0)
    assert depth[non_zero[-1]] < 4.0
    assert depth[non_zero[-1]] > 0.0


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
    assert ReasonCode.LIMITED_BY_CLASS_PRIOR == "envelope_limited_by_class_prior"
    assert (
        ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION
        == "envelope_limited_by_spatial_exclusion"
    )
    assert (
        ReasonCode.LIMITED_BY_POSITION_STABILITY
        == "envelope_limited_by_position_stability"
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
    assert len(list(ReasonCode)) == 9


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
# position_stability_limit (PR-6a)
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


def test_position_stability_limit_empty_spread_is_all_sentinel():
    curve = position_stability_limit(
        DEFAULT_ENVELOPE_GRID_HZ, (), n_positions=10, tier="reference",
    )
    assert np.all(curve == ENVELOPE_CEILING_SENTINEL_DB)


def test_position_stability_limit_reads_the_standard_error_not_the_raw_spread():
    """Known-answer, and the whole design decision in two numbers.

    sigma = 2.0 dB in both clouds. At N=4 the standard error is 1.0 dB, twice
    the ``reference`` tier's 0.5 dB tolerable, so the limit is
    ``24 * 0.5/1.0 = 12.0`` dB. At N=16 the SAME 2.0 dB spread gives a 0.5 dB
    standard error and the term is back at the sentinel: dispersing more
    positions buys back depth, which is the ``1/sqrt(N)`` law the term
    exists to express. A raw-sigma reading would return 6.0 dB for both.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    spread = (_band(1000.0, 2.0),)
    inside = (grid >= 1000.0 / _SQRT2) & (grid <= 1000.0 * _SQRT2)

    four = position_stability_limit(grid, spread, n_positions=4, tier="reference")
    sixteen = position_stability_limit(grid, spread, n_positions=16, tier="reference")
    assert four[inside] == pytest.approx(12.0)
    assert sixteen[inside] == pytest.approx(ENVELOPE_CEILING_SENTINEL_DB)
    # A raw-sigma reading would have been this, at both N:
    assert ENVELOPE_CEILING_SENTINEL_DB * 0.5 / 2.0 == pytest.approx(6.0)


def test_position_stability_limit_is_monotone_non_increasing_in_sigma():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    previous = None
    for sigma_db in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0):
        curve = position_stability_limit(
            grid, (_band(1000.0, sigma_db),), n_positions=9, tier="reference",
        )
        if previous is not None:
            assert np.all(curve <= previous + 1e-12)
        previous = curve


def test_position_stability_limit_unreported_bands_stay_at_the_sentinel():
    """"No reading, no additional constraint" -- deliberately NOT the
    "no evidence, no permission" rule a missing repeat sigma gets. A cloud
    whose grid stopped short must not silently delete the envelope above it.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    curve = position_stability_limit(
        grid, (_band(1000.0, 4.0),), n_positions=4, tier="reference",
    )
    outside = (grid < 1000.0 / _SQRT2) | (grid > 1000.0 * _SQRT2)
    assert outside.any()
    assert np.all(curve[outside] == ENVELOPE_CEILING_SENTINEL_DB)
    assert np.all(curve[~outside] < ENVELOPE_CEILING_SENTINEL_DB)


def test_position_stability_limit_overlapping_bands_take_the_tighter_limit():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    quiet = _band(1000.0, 0.4, f_lo=800.0, f_hi=1600.0)
    noisy = _band(2000.0, 4.0, f_lo=1200.0, f_hi=3000.0)
    curve = position_stability_limit(
        grid, (quiet, noisy), n_positions=4, tier="reference",
    )
    overlap = (grid >= 1200.0) & (grid <= 1600.0)
    assert overlap.any()
    # sigma 4.0 / sqrt(4) = 2.0 -> 24 * 0.5/2.0 = 6.0 dB, the noisier band's.
    assert curve[overlap] == pytest.approx(6.0)


def test_position_stability_limit_shares_the_mapping_with_repeatability():
    """The two terms differ ONLY in which sigma they hand to the shared
    mapping -- pinned so a future edit to one cannot quietly fork the other.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    sigma_db, n_positions = 3.0, 9
    stability = position_stability_limit(
        grid, (_band(1000.0, sigma_db, f_lo=grid[0], f_hi=grid[-1]),),
        n_positions=n_positions, tier="consumer",
    )
    equivalent = repeatability_limit(
        np.full_like(grid, sigma_db / np.sqrt(n_positions)),
        tier="consumer", grid_hz=grid,
    )
    assert np.array_equal(stability, equivalent)


def test_position_stability_limit_rejects_bad_tier_and_a_thin_cloud():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    with pytest.raises(ValueError, match="unknown mic tier"):
        position_stability_limit(grid, (), n_positions=4, tier="studio")
    with pytest.raises(ValueError, match="n_positions must be >= 2"):
        position_stability_limit(
            grid, (_band(1000.0, 1.0),), n_positions=1, tier="reference",
        )


# The worst cross-position spread the S0 main leg produced: sigma 3.0957 dB
# in the 16 kHz band across N = 10 positions. Committed as literals so the
# test below is HARDWARE-FREE and runs in CI -- the corpus test that measured
# them (test_s0_position_stability_calibration_populations) is env-gated and
# skips there, which is exactly the gap this constant closes.
#
# RE-DERIVED 2026-08-02 (#2045), 3.0878 -> 3.0957, for PR #1991's prominence
# vote re-gating cloud_04 -- see tests._flat_lin_corpus "The 2026-08-02 re-pin
# era".
#
# **This literal is a FROZEN STAND-IN for an env-gated corpus measurement, and
# it must be re-derived whenever that corpus moves.** Nothing in CI can catch
# it going stale: the test below happily keeps passing against a number the
# hardware no longer produces, because the corpus test that would disagree is
# skipped there. That is the #1884 failure mode (corpus CI invisibility) in
# miniature -- and it is not hypothetical, it is how this constant survived
# #1991 while every env-gated reading around it moved. Re-derive it through
# position_stability_limit against the live corpus; do not transcribe it.
_S0_WORST_BAND_SIGMA_DB = 3.0957
_S0_WORST_BAND_N_POSITIONS = 10


def test_shared_sigma_tolerable_keeps_the_s0_worst_case_above_the_fit_cap():
    """Guards the 0.26 dB margin the whole position-stability design rests on.

    ``position_stability_limit`` is inert on a protocol-following cloud only
    because the S0 main leg's worst standard error (3.0957/sqrt(10) =
    0.979 dB) maps to **12.26 dB** at ``reference`` tier, which is above the
    fit's 12 dB ``PER_FILTER_CUT_CAP_DB`` -- so ``min(12, allowed_depth)``
    never moves and the emitted filters are unchanged.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote -- see
    ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era" and the
    frozen-stand-in warning on :data:`_S0_WORST_BAND_SIGMA_DB` above. The
    margin got THINNER, 0.29 -> 0.26 dB, which is the direction that matters
    for a guard whose whole job is that this limit stays above the cut cap.

    (The corpus's real bands produce 12.2579 dB. This test drives one
    synthetic band from the 4-dp rounded constant above and gets 12.2581 dB.
    Same measurement, two roundings, well inside this test's own 0.01 dB
    tolerance; both are stated so the pair cannot read as a contradiction.)

    **The hazard this exists for.** :data:`_SIGMA_TOLERABLE_DB` is now shared
    by two terms. A future retune motivated by *repeatability* -- tightening
    ``reference`` from 0.5 to 0.4 dB, a perfectly reasonable thing to want --
    drops that same limit to 9.81 dB, and the stability term starts binding
    the emitted fit on a cloud nobody thought had changed. The
    ``test_position_stability_limit_shares_the_mapping_with_repeatability``
    contract cannot catch it (both terms move together, so they stay in
    agreement while the answer goes wrong), and the corpus test that
    measured the margin skips in CI. This one does not.

    Regime: S0 corpus, JTS3 cdhorn, ten-position desk cloud, 16 kHz octave
    band, ``reference`` mic tier. The margin is class-independent -- this
    term reads only the cloud and the tier.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    worst = _band(16_000.0, _S0_WORST_BAND_SIGMA_DB)
    limit = position_stability_limit(
        grid, (worst,), n_positions=_S0_WORST_BAND_N_POSITIONS, tier="reference",
    )
    inside = limit < ENVELOPE_CEILING_SENTINEL_DB
    assert inside.any()
    shipped_limit_db = float(limit[inside].min())

    assert shipped_limit_db == pytest.approx(12.26, abs=0.01)
    assert shipped_limit_db >= PER_FILTER_CUT_CAP_DB

    # The counterfactual that makes this test load-bearing, from the same
    # documented mapping (ceiling * min(1, tolerable / sigma)), written out
    # here rather than imported so a change to the module's own helper cannot
    # move both sides at once.
    standard_error_db = _S0_WORST_BAND_SIGMA_DB / math.sqrt(
        _S0_WORST_BAND_N_POSITIONS
    )
    tightened_limit_db = ENVELOPE_CEILING_SENTINEL_DB * min(
        1.0, 0.4 / standard_error_db
    )
    assert tightened_limit_db == pytest.approx(9.81, abs=0.01)
    assert tightened_limit_db < PER_FILTER_CUT_CAP_DB


# --------------------------------------------------------------------------- #
# compose_envelope + the cloud terms (PR-6a)
# --------------------------------------------------------------------------- #

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
    ``terms`` mapping still holding exactly the three original keys."""
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
        ReasonCode.LIMITED_BY_CLASS_PRIOR,
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


def test_compose_envelope_position_stability_wins_where_it_binds():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    primary = _zero_sigma_primary("tweeter", freqs_hz=grid)
    # sigma 6 dB over 4 positions -> se 3.0 dB -> 24 * 0.5/3.0 = 4.0 dB, well
    # below mic-trust and class-prior at 3 kHz (both still at the sentinel).
    curve = _compose(primary, band_spread=(_band(3000.0, 6.0),), n_positions=4)
    idx = int(np.argmin(np.abs(grid - 3000.0)))
    assert curve.reason[idx] == ReasonCode.LIMITED_BY_POSITION_STABILITY
    assert curve.terms[ReasonCode.LIMITED_BY_POSITION_STABILITY][idx] == pytest.approx(
        4.0
    )


def test_compose_envelope_requires_band_spread_and_n_positions_together():
    primary = _zero_sigma_primary("tweeter", freqs_hz=DEFAULT_ENVELOPE_GRID_HZ)
    for kwargs in ({"band_spread": (_band(1000.0, 1.0),)}, {"n_positions": 8}):
        with pytest.raises(ValueError, match="must be supplied together"):
            _compose(primary, **kwargs)


def test_default_grid_step_pins_the_coarseness_the_edge_rule_is_about():
    """Pins the grid coarseness that makes a partial-coverage rule necessary."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    step_pct = float(grid[1] / grid[0] - 1.0) * 100.0
    assert step_pct == pytest.approx(2.8354, abs=0.0002)
    idx = int(np.argmin(np.abs(grid - 8700.0)))
    assert float(grid[idx]) == pytest.approx(8644.8, abs=0.1)
    assert float(grid[idx + 1] - grid[idx]) == pytest.approx(245.1, abs=0.1)


# --------------------------------------------------------------------------- #
# S0 corpus replay -- the PR-6a acceptance
#
# The envelope is what is under test here; the FIT is the observable, because
# "the correction spends no gain inside an identified null" is a claim about
# emitted filters, not about an array. Laptop-durable corpus, env-gated, skips
# cleanly in CI (see tests/_flat_lin_corpus.py).
# --------------------------------------------------------------------------- #

# The band the S0 report grades the 8-16 kHz family in (REPORT.md Q1/Q2), and
# the band tests/test_interference_nulls.py's own acceptance uses.
_S0_NULL_BAND_HZ = (5000.0, 19_000.0)
# JTS3's declared tweeter class. The corpus is a SUMMED system sweep; above
# the session's own crossover it is the tweeter, so the replay fits that half
# of it under the tweeter's declared class. Stated because the class is a
# declaration about the hardware, not a measurement this corpus contains.
_S0_TWEETER_CLASS = "compression_horn"


@pytest.fixture(scope="module")
def s0_main_captures():
    """The S0 main leg's ten positions, deconvolved once for the section."""
    return s0_position_captures(S0_MAIN)


@pytest.fixture(scope="module")
def s0_replay(s0_main_captures):
    """The S0 main-leg cloud, its merged honesty mask, and one position's
    two-occurrence driver response."""
    from types import SimpleNamespace

    combined = combine_positions(s0_main_captures)
    registry = identify_interference_nulls(combined, band_hz=_S0_NULL_BAND_HZ)
    primary, fc_hz = s0_position_driver_response(S0_MAIN, "cloud_01")
    return SimpleNamespace(
        combined=combined,
        registry=registry,
        # The plan's merged honesty mask: screen UNION identified nulls,
        # merged by the one owner upstream.
        merged_bands_hz=merged_true_intervals(
            combined.freqs_hz, combined.excluded | registry.excluded
        ),
        primary=primary,
        excited_band_hz=(fc_hz, 20_000.0),
    )


def _s0_envelope(s0_replay, driver_class: str, *, cloud: bool) -> EnvelopeCurve:
    cloud_kwargs = (
        {
            "excluded_bands_hz": s0_replay.merged_bands_hz,
            "band_spread": s0_replay.combined.band_spread,
            "n_positions": s0_replay.combined.n_positions,
        }
        if cloud
        else {}
    )
    return compose_envelope(
        "summed",
        s0_replay.primary,
        excited_band_hz=s0_replay.excited_band_hz,
        mic_tier="reference",
        driver_class=driver_class,
        grid_hz=DEFAULT_ENVELOPE_GRID_HZ,
        **cloud_kwargs,
    )


def _realized_correction_db(fit, grid_hz: np.ndarray) -> np.ndarray:
    if not fit.filters:
        return np.zeros_like(grid_hz)
    return 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(fit.filters, grid_hz)), 1e-12)
    )


@requires_s0_curves
def test_s0_position_stability_calibration_populations(s0_main_captures):
    """Re-derives ``position_stability_limit``'s whole calibration table.

    Measured 2026-07-26, five S0 cloud groupings at ``reference`` tier, over
    the seven octave bands inside ``DEFAULT_ENVELOPE_GRID_HZ``'s 150 Hz
    floor. Each row is (N, sigma range, standard-error range, limit range),
    and the assertions below pin the calibration to the measurement.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote, which
    re-gates ``cloud_04`` -- see ``tests._flat_lin_corpus`` "The 2026-08-02
    re-pin era". Exactly the two groupings that CONTAIN cloud_04 moved
    (``main_all_10`` and ``main_tweeter_height_6``); ``main_hand_width_low_4``
    (cloud_07-10), ``desk_front_edge_3`` and ``ground_plane_3`` were
    byte-identical THEN, which was the control on that change.

    RE-PINNED AGAIN 2026-09-10 (#2568) for commit ``4300a546d``, which
    changed ``deconv.regularized_deconvolution_full``'s Tikhonov floor from
    additive ``|X|**2 + eps`` to ``max(|X|**2, eps)``. Every S0 capture this
    table is built from is deconvolved through that function, so this time
    ALL FIVE groupings moved -- there is no unaffected control, because the
    change sits upstream of every one of them. ``main_tweeter_height_6``
    happens to land back inside the old ``abs=0.001``/``abs=0.01``
    tolerances (its numbers are unchanged below), which is coincidence, not
    immunity. ``ground_plane_3`` moved the most: sigma_min 0.362 -> 0.429,
    sigma_max 3.173 -> 3.190, limit_min 6.55 -> 6.52 dB. ``main_all_10``'s
    limit_min tightens again, 12.26 -> 12.24 dB, still 0.24 dB clear of
    ``PER_FILTER_CUT_CAP_DB``.
    """
    from tests._flat_lin_corpus import (
        S0_DESK_EDGE,
        S0_GROUND_PLANE,
        S0_MAIN_HAND_WIDTH_LOW,
        S0_MAIN_TWEETER_HEIGHT,
    )

    def _subset(only):
        return [c for c in s0_main_captures if c.position_id in only]

    groupings = {
        "main_all_10": combine_positions(s0_main_captures),
        "main_tweeter_height_6": combine_positions(_subset(S0_MAIN_TWEETER_HEIGHT)),
        "main_hand_width_low_4": combine_positions(_subset(S0_MAIN_HAND_WIDTH_LOW)),
        "desk_front_edge_3": combine_positions(s0_position_captures(S0_DESK_EDGE)),
        "ground_plane_3": combine_positions(s0_position_captures(S0_GROUND_PLANE)),
    }
    expected = {
        # name: (N, sigma_lo, sigma_hi, se_lo, se_hi, limit_lo, worst_band_hz)
        "main_all_10": (10, 0.921, 3.100, 0.291, 0.980, 12.24, 16_000.0),
        "main_tweeter_height_6": (6, 0.190, 1.222, 0.078, 0.499, 24.00, 16_000.0),
        "main_hand_width_low_4": (4, 0.962, 3.031, 0.481, 1.515, 7.92, 16_000.0),
        "desk_front_edge_3": (3, 0.306, 1.737, 0.177, 1.003, 11.97, 16_000.0),
        "ground_plane_3": (3, 0.429, 3.190, 0.248, 1.842, 6.52, 8000.0),
    }

    for name, combined in groupings.items():
        n, sig_lo, sig_hi, se_lo, se_hi, limit_lo, worst_hz = expected[name]
        assert combined.n_positions == n, name
        bands = [
            band for band in combined.band_spread
            if band.center_hz >= DEFAULT_ENVELOPE_GRID_HZ[0]
        ]
        assert [band.center_hz for band in bands] == [
            250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16_000.0,
        ], name

        sigmas = np.array([band.sigma_db for band in bands])
        errors = sigmas / np.sqrt(n)
        assert sigmas.min() == pytest.approx(sig_lo, abs=0.001), name
        assert sigmas.max() == pytest.approx(sig_hi, abs=0.001), name
        assert errors.min() == pytest.approx(se_lo, abs=0.001), name
        assert errors.max() == pytest.approx(se_hi, abs=0.001), name

        limit = position_stability_limit(
            DEFAULT_ENVELOPE_GRID_HZ, bands, n_positions=n, tier="reference",
        )
        assert float(limit.min()) == pytest.approx(limit_lo, abs=0.01), name
        assert float(limit.max()) == pytest.approx(
            ENVELOPE_CEILING_SENTINEL_DB
        ), name
        # The band that produced the tightest limit.
        assert bands[int(np.argmax(errors))].center_hz == worst_hz, name

    # The claim the term's shape rests on: the plan's own cloud shape is not
    # narrowed below the fit's per-filter cut cap; the thin ones are.
    assert expected["main_all_10"][5] > PER_FILTER_CUT_CAP_DB
    assert expected["main_hand_width_low_4"][5] < PER_FILTER_CUT_CAP_DB
    assert expected["ground_plane_3"][5] < PER_FILTER_CUT_CAP_DB


@requires_s0_curves
def test_s0_position_stability_narrows_the_envelope_but_not_the_fit(s0_replay):
    """What the stability term costs a protocol-following cloud: nothing.

    Measured 2026-07-26, the term supplied ALONE (no exclusion mask) against
    the same envelope without it, on the S0 ten-position cloud.

    RE-PINNED 2026-09-10 (#2568) for commit ``4300a546d`` (Tikhonov floor
    ``|X|**2 + eps`` -> ``max(|X|**2, eps)``, see the calibration-populations
    test above for the mechanism): the deconvolution feeds this table's
    sigma curve directly, so the narrowing widened on ``compression_horn`` --
    30 bins -> 43, span 5082.1-11433.5 Hz -> 5082.1-16444.9 Hz, max drop
    7.03 -> 7.08 dB -- and a second octave (12 kHz) now also crosses from
    ``envelope_limited_by_class_prior`` to
    ``envelope_limited_by_position_stability``. ``unknown``'s bin count and
    span happen to survive (18 bins, 5082.1-8174.7 Hz), only its max drop
    moves, 5.196 -> 5.24 dB.

    The **8 newest bins that changed, 13521.8-16444.9 Hz on
    ``compression_horn``, now cross the per-bin cut cap** (``min(12 dB,
    allowed_depth_db)`` moves there, where it did not before) -- that is
    checked explicitly below rather than folded into a blanket
    equal-everywhere claim, because it is no longer true everywhere. It
    stays true UP TO that point, and the fit is verified unaffected in
    either case: the CD-horn continuation stage suppresses itself at
    ``insufficient_repeats`` on this two-occurrence corpus (see
    ``test_s0_replay_fit_places_no_gain_inside_identified_nulls``), so no
    filter this corpus ever emits reaches within 10 kHz of the differing
    bins -- both fits' filters top out at 3533.4 Hz. The emitted fit stays
    byte-identical, which is the actual claim this test exists to make.

    What DOES change is the disclosure, which is the point of the term
    existing at all: the ``compression_horn`` fit's 8 kHz (and, newly,
    12 kHz) octave summaries move from ``envelope_fitted`` /
    ``envelope_limited_by_class_prior`` to
    ``envelope_limited_by_position_stability``, so the report can say which
    instrument is holding the ceiling even though nothing was given up. The
    ``unknown`` fit's summary does not move -- the class prior is already
    tighter than the stability term everywhere it is read.

    This is the claim ``position_stability_limit``'s docstring makes about
    its own regime, checked at the surface that matters rather than on the
    term's curve.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    for driver_class, n_changed, span_hz, max_drop_db, cap_diff_floor_hz, reason_delta in (
        (
            _S0_TWEETER_CLASS, 43, (5082.1, 16_444.9), 7.08, 13_521.8,
            {
                "8000": (
                    ReasonCode.FITTED.value,
                    ReasonCode.LIMITED_BY_POSITION_STABILITY.value,
                ),
                "12000": (
                    ReasonCode.LIMITED_BY_CLASS_PRIOR.value,
                    ReasonCode.LIMITED_BY_POSITION_STABILITY.value,
                ),
            },
        ),
        ("unknown", 18, (5082.1, 8174.7), 5.24, None, {}),
    ):
        common = dict(
            excited_band_hz=s0_replay.excited_band_hz,
            mic_tier="reference",
            driver_class=driver_class,
            grid_hz=grid,
        )
        bare = compose_envelope("summed", s0_replay.primary, **common)
        stable = compose_envelope(
            "summed", s0_replay.primary, **common,
            band_spread=s0_replay.combined.band_spread,
            n_positions=s0_replay.combined.n_positions,
        )
        drop = bare.allowed_depth_db - stable.allowed_depth_db
        changed = np.flatnonzero(np.abs(drop) > 1e-9)
        assert int(changed.size) == n_changed, driver_class
        assert float(grid[changed[0]]) == pytest.approx(span_hz[0], abs=0.1)
        assert float(grid[changed[-1]]) == pytest.approx(span_hz[1], abs=0.1)
        assert float(drop.max()) == pytest.approx(max_drop_db, abs=0.01)
        assert float(drop.min()) >= 0.0  # narrowing only

        # The surface the fit reads. Equal everywhere on `unknown`; on
        # `compression_horn` it differs at exactly 8 bins, all >= the named
        # floor and all beyond where either fit's HF-suppressed filters ever
        # reach (see docstring), so the fit itself stays unaffected either
        # way -- verified directly below via `bare_fit == stable_fit`.
        cap_bare = np.minimum(PER_FILTER_CUT_CAP_DB, bare.allowed_depth_db)
        cap_stable = np.minimum(PER_FILTER_CUT_CAP_DB, stable.allowed_depth_db)
        cap_diff = np.flatnonzero(cap_bare != cap_stable)
        if cap_diff_floor_hz is None:
            assert cap_diff.size == 0, driver_class
        else:
            assert cap_diff.size == 8, driver_class
            assert float(grid[cap_diff[0]]) == pytest.approx(
                cap_diff_floor_hz, abs=0.1
            ), driver_class
        bare_fit = fit_driver_linearization(s0_replay.primary, bare).to_dict()
        stable_fit = fit_driver_linearization(s0_replay.primary, stable).to_dict()
        bare_reasons = bare_fit.pop("reason_summary")
        stable_reasons = stable_fit.pop("reason_summary")
        assert bare_fit == stable_fit, driver_class
        assert {
            octave: (bare_reasons[octave], stable_reasons[octave])
            for octave in bare_reasons
            if bare_reasons[octave] != stable_reasons[octave]
        } == reason_delta, driver_class


@requires_s0_curves
def test_s0_replay_fit_places_no_gain_inside_identified_nulls(s0_replay):
    """THE acceptance, on the declared-``compression_horn`` regime.

    Measured 2026-07-26, INTERVALS RE-PINNED 2026-08-02 (#2045) for PR
    #1991's prominence vote, which re-gates ``cloud_04`` -- see
    ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era". The three
    intervals then moved by at most 28 Hz an edge (8016-9427 -> 8015-9428,
    10842-12348 -> 10841-12351, 14280-15679 -> 14276-15651).

    RE-PINNED AGAIN 2026-09-10 (#2568) for commit ``4300a546d`` (Tikhonov
    floor ``|X|**2 + eps`` -> ``max(|X|**2, eps)``, see the calibration
    tests above for the mechanism): every capture the registry reads is
    deconvolved through that function, so the three interval edges moved
    again, this time narrower and lower -- 8015-9428 -> 8010-9399,
    10841-12351 -> 10829-12339, 14276-15651 -> 14271-15612. That drops the
    point-contained bin count 14 -> 13 and, because the widened
    reference-tier mic-trust shelf (#3297) now covers the third rung
    (14271-15612 Hz sits inside 12-20 kHz), the BARE envelope's range inside
    the nulls moves from 2.82-22.80 dB to 10.13-24.00 dB (the ceiling
    sentinel). None of that touches the invariant this test exists to
    prove: the fit places **no filter** anywhere near the (now-shifted)
    nulls -- its two peaking cuts still sit at 2388.9 and 3533.4 Hz -- and
    every point-contained bin still reports ``LIMITED_BY_SPATIAL_EXCLUSION``.

    The registry identifies three rungs over 5-19 kHz --
    8010-9399, 10829-12339 and 14271-15612 Hz. With the merged mask composed
    in, the envelope allows **exactly 0.0 dB** at all thirteen envelope-grid
    bins inside them (against 10.13-24.00 dB without it), the fit places **no
    filter** anywhere near them (its two peaking cuts sit at 2388.9 and
    3533.4 Hz), and every one of those bins reports
    ``LIMITED_BY_SPATIAL_EXCLUSION``.

    **What is left inside a null is the analytic skirt of filters centred
    octaves below, and it is a CUT.** Signed realized correction inside the
    three intervals reads -0.0620 to -0.0068 dB: never positive, so the null
    is never *filled* -- which is the plan's actual non-goal, and is
    guaranteed absolutely by the pre-existing cut-only invariant rather than
    by anything this PR added. A minimum-phase biquad cascade has no compact
    support, so "exactly zero inside an interval" is not a claim any fit of
    this shape can make; the honest claims are the three above plus this
    bound.

    **What the fit band shows, stated to its evidence and no further.** The
    fit band is **identical** to the no-mask fit's -- 2020.0-19448.6 Hz as of
    this re-pin, wider than the 2020.0-15991.5 Hz measured before #3297
    widened the reference-tier mic-trust shelf, since that shelf is what the
    top of this band tracks. The exclusion still punches holes inside the
    band rather than truncating it at the first null, so the fit keeps its
    PERMISSION to correct above 8 kHz. That is not the same as demonstrating
    correction up there, and this corpus cannot demonstrate it: every filter
    either fit emits sits at 2388.9 or 3533.4 Hz, well below the first null
    at 8010 Hz, because the CD-horn continuation stage -- the stage that
    would place HF content -- suppresses itself at ``insufficient_repeats``
    on a corpus giving each position two occurrences
    (``LinearizationFit.hf_continuation_suppressed_reason``, asserted
    below). It suppresses identically with and without the mask, so the
    comparison stays clean; it also means "the surrounding envelope is
    corrected" is a claim about preserved permission here, and a session with
    >= 3 occurrences per position is what would exercise the rest of it.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    intervals = s0_replay.registry.excluded_bands_hz
    assert [(round(lo), round(hi)) for lo, hi in intervals] == [
        (8010, 9399), (10829, 12339), (14271, 15612),
    ]

    bare = _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=False)
    masked = _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=True)
    bare_fit = fit_driver_linearization(s0_replay.primary, bare)
    fit = fit_driver_linearization(s0_replay.primary, masked)
    realized_db = _realized_correction_db(fit, grid)

    # The two rasterization rules, on this registry: cell-overlap excludes
    # more envelope bins than point-containment. The 13 point-contained
    # bins are what every "inside a null" assertion below is measured over,
    # so the claims hold under the WEAKER rule too.
    inside = np.zeros_like(grid, dtype=bool)
    for f_lo, f_hi in intervals:
        inside |= (grid >= f_lo) & (grid <= f_hi)
    assert int(inside.sum()) == 13
    assert int((spatial_exclusion_limit(grid, intervals) <= 0.0).sum()) == 17
    # The grid-coarseness ratio that makes a partial-coverage rule necessary.
    assert float(
        s0_replay.combined.freqs_hz[1] - s0_replay.combined.freqs_hz[0]
    ) == pytest.approx(1.4648, abs=0.0002)

    assert np.all(masked.allowed_depth_db[inside] == 0.0)
    assert float(bare.allowed_depth_db[inside].min()) == pytest.approx(10.13, abs=0.02)
    assert float(bare.allowed_depth_db[inside].max()) == pytest.approx(24.00, abs=0.02)
    for i in np.flatnonzero(inside):
        assert masked.reason[i] == ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION

    assert [(f.biquad_type, round(f.freq, 1)) for f in fit.filters] == [
        ("Peaking", 2388.9), ("Peaking", 3533.4),
    ]
    for emitted in fit.filters:
        assert emitted.gain <= 0.0
        assert not any(f_lo <= emitted.freq <= f_hi for f_lo, f_hi in intervals)

    assert float(realized_db[inside].max()) == pytest.approx(-0.0068, abs=0.001)
    assert float(realized_db[inside].min()) == pytest.approx(-0.0620, abs=0.001)

    assert fit.fit_band_hz == bare_fit.fit_band_hz
    assert fit.fit_band_hz[0] == pytest.approx(2020.0, abs=1.0)
    assert fit.fit_band_hz[1] == pytest.approx(19_448.6, abs=1.0)

    # The reason no filter lands above 8 kHz in EITHER fit, named here so the
    # band assertion above cannot be read as more than preserved permission.
    for one_fit in (bare_fit, fit):
        assert one_fit.hf_continuation_suppressed_reason == "insufficient_repeats"
        assert one_fit.hf_continuation_spend_db == 0.0
        assert max(f.freq for f in one_fit.filters) < intervals[0][0]
    assert s0_replay.primary.repeat_responses  # 2 occurrences, 1 repeat
    assert len(s0_replay.primary.repeat_responses) == 1


@requires_s0_curves
def test_s0_replay_unknown_class_pins_the_undeclared_regime(s0_replay):
    """The second acceptance case: with the class left undeclared, the
    EXISTING class prior already refuses the top octave and the new terms
    only narrow.

    ``unknown``'s ``full_to`` is 6 kHz with a taper to 12 kHz, so the
    15 kHz rung's interval is at 0.0 allowed depth **before** any mask
    exists, and the 12 k / 16 k octave summaries stay
    ``LIMITED_BY_CLASS_PRIOR`` even with the mask composed in. The 8.7 kHz
    rung is where the two regimes differ: ``compression_horn`` (``full_to``
    10 kHz) has ``ENVELOPE_CEILING_SENTINEL_DB`` (24.00 dB) of real authority
    there and the exclusion is what removes it, while ``unknown`` has 13.99 dB
    from the prior alone. This is why every 8-16 kHz statement about these
    terms has to name its class.

    Re-measured after #3297 widened the reference tier's mic-trust taper
    (``_MIC_TRUST_TABLE_HZ["reference"]``'s full-trust shelf, 8 kHz -> 12 kHz,
    owner ruling 2026-08-29): 8.7 kHz used to sit just past the old shelf, in
    the taper, at 22.80 dB; it now sits inside the widened shelf, where
    ``compression_horn``'s class prior no longer trims it either, so both
    terms read the flat sentinel. ``unknown``'s 13.99 dB is untouched -- its
    own class prior tapers well below 8.7 kHz regardless of the mic-trust
    table.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    first_interval = s0_replay.registry.excluded_bands_hz[0]
    top_interval = s0_replay.registry.excluded_bands_hz[-1]
    first = (grid >= first_interval[0]) & (grid <= first_interval[1])
    top = (grid >= top_interval[0]) & (grid <= top_interval[1])

    bare_unknown = _s0_envelope(s0_replay, "unknown", cloud=False)
    masked_unknown = _s0_envelope(s0_replay, "unknown", cloud=True)
    bare_horn = _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=False)
    masked_horn = _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=True)

    assert np.all(bare_unknown.allowed_depth_db[top] == 0.0)
    assert np.all(masked_unknown.allowed_depth_db <= bare_unknown.allowed_depth_db)

    # The 8.7 kHz rung, the two class regimes side by side.
    assert float(bare_horn.allowed_depth_db[first].max()) == pytest.approx(
        ENVELOPE_CEILING_SENTINEL_DB, abs=0.02
    )
    assert float(bare_unknown.allowed_depth_db[first].max()) == pytest.approx(
        13.99, abs=0.02
    )

    idx_12k = int(np.argmin(np.abs(grid - 12_000.0)))
    assert masked_unknown.reason[idx_12k] == ReasonCode.LIMITED_BY_CLASS_PRIOR
    assert masked_horn.reason[idx_12k] == ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION


@requires_s0_curves
def test_s0_replay_ripple_stays_within_bound_outside_excluded_bands(s0_replay):
    """Predicted-sum ripple, compared on the SAME bins with both fits.

    **The masked fit is slightly worse, not equal** -- +0.0670 dB on
    ``compression_horn`` -- so this test asserts the exact measured
    difference, which IS the bound, rather than the "no regression" the
    plan's acceptance line asks for in prose.

    The comparison window is the BARE fit's band minus every masked bin --
    one window for both fits, which is what makes the two RMS figures
    comparable -- 69 bins on the ``compression_horn`` regime, 56 on
    ``unknown``. The metric is the RMS deviation of the predicted sum about
    its own median. **Its absolute value is not a ripple figure**: it is
    dominated by the tweeter's own uncorrected top-octave rolloff, because
    this corpus gives each position two occurrences and the CD-horn
    continuation stage suppresses itself at ``insufficient_repeats``. Only
    the difference between the two fits, on identical bins, is being read
    here.

    Why the honesty mask now costs 0.2733 dB of level, not 0.0382
    ------------------------------------------------------------

    The masked arm's cost is set by how much of an identified null the BARE
    arm's core level mask reaches over: ``target_level_db`` is the median of
    ``smoothed_db`` on that mask, so dropping null bins can only raise it.
    #3297 widened reference-tier mic trust to 12k-20k, which extends the
    core level mask 2020-7949.3 Hz -> 2020-9941.9 Hz. The old mask poked ONE
    bin into the first identified null (7949.3 Hz, -28.68 dB, that
    interval's conservatively-rasterized outer edge) and the median moved
    0.0382 dB. The new mask contains all **seven** of that null's bins
    (7949.3-9401.2 Hz, -28.68 to -30.29 dB) and the median moves **0.2733
    dB**, both cuts shrinking with it.

    ``4300a546d``'s deconvolution floor (``|X|**2 + eps`` ->
    ``max(|X|**2, eps)``) stacks on top of that; it is not the cause.
    Measured on this corpus as a 2x2 (floor form x mic-trust table), on
    ``compression_horn``, as (bins, ripple delta, level shift)::

        add + old (the era these were pinned in)  61  +0.0059  -0.0382
        add + new                                 68  +0.0351  -0.1548
        max + old                                 62  +0.0050  -0.0425
        max + new (HEAD)                          69  +0.0670  -0.2733

    The floor change ALONE moves the level shift 0.0382 -> 0.0425 dB, inside
    the old budget. It is also a fix rather than drift: ``eps = 1e-3 *
    peak|X|**2`` against a log sweep whose ``|X(f)|**2`` falls as 1/f leaves
    ``|X|**2/eps`` at only ~6 by 19 kHz, so the additive form shrinks the
    recovered magnitude by ``|X|**2/(|X|**2 + eps)`` -- -0.045 dB at 500 Hz
    but -1.32 dB at 19 kHz. Deconvolving a synthetic known-flat system (a
    pure delay, driven by this corpus's own 6.0 s verify sweep) recovers
    0.7987 dB RMS error and a -1.2607 dB 500 Hz -> 19 kHz tilt under
    ``|X|**2 + eps``, against 0.0135 dB RMS and -0.0152 dB under
    ``max(|X|**2, eps)``. That tilt was reaching the emitted filters.

    The two 0.05 dB budgets this test used to carry are therefore gone
    rather than re-scaled: they were sized to an era when the two arms'
    level masks differed by one bin, and any replacement near 0.3 dB would
    be a number with no derivation. What replaces them is the DIRECTION the
    mechanism above guarantees -- dropping null bins raises a median, so on
    a cut-only arm the mask can only make a cut shallower, never deeper and
    never a boost -- carried alongside the exact pins.

    **The two fits no longer share a band on ``unknown``, and that is the
    hardening showing through.** ``class_prior_limit`` for ``unknown`` is
    exactly 0 from 12 kHz up, so the composed envelope now ends there
    instead of carrying blurred depth past it; the second identified null
    (10829-12339 Hz) then reaches that zero with nothing correctable left
    between them, and the masked band stops at 10513.6 Hz rather than
    punching a hole and continuing. On ``compression_horn`` -- whose class
    prior does not zero until 20 kHz -- the exclusion still punches holes
    inside a shared band, exactly as before. Filters are identical either
    way on ``unknown``, so there the change is in the band the fit REPORTS,
    not in what the speaker plays.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    measured_db = np.interp(
        grid, s0_replay.primary.freqs_hz, s0_replay.primary.magnitude_db
    )
    excluded = np.zeros_like(grid, dtype=bool)
    for f_lo, f_hi in s0_replay.merged_bands_hz:
        excluded |= (grid >= f_lo) & (grid <= f_hi)

    for driver_class, n_bins, expected_delta_db, bands_agree in (
        (_S0_TWEETER_CLASS, 69, 0.0670, True),
        ("unknown", 56, 0.0, False),
    ):
        bare_fit = fit_driver_linearization(
            s0_replay.primary, _s0_envelope(s0_replay, driver_class, cloud=False)
        )
        masked_fit = fit_driver_linearization(
            s0_replay.primary, _s0_envelope(s0_replay, driver_class, cloud=True)
        )
        assert (
            bare_fit.fit_band_hz == masked_fit.fit_band_hz
        ) is bands_agree, driver_class
        # One window for both fits (the bare band), so the two RMS figures
        # are read on identical bins even where the bands themselves differ.
        lo, hi = bare_fit.fit_band_hz
        window = (grid >= lo) & (grid <= hi) & ~excluded
        assert int(window.sum()) == n_bins, driver_class

        ripples = []
        for one_fit in (bare_fit, masked_fit):
            predicted = (measured_db + _realized_correction_db(one_fit, grid))[window]
            ripples.append(
                float(np.sqrt(np.mean((predicted - np.median(predicted)) ** 2)))
            )
        bare_rms, masked_rms = ripples
        assert masked_rms - bare_rms == pytest.approx(expected_delta_db, abs=0.002)
        assert masked_rms >= bare_rms

    # The seven-bin cause named above.
    horn_bare = fit_driver_linearization(
        s0_replay.primary, _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=False)
    )
    horn_masked = fit_driver_linearization(
        s0_replay.primary, _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=True)
    )
    assert horn_bare.target_level_db - horn_masked.target_level_db == pytest.approx(
        -0.2733, abs=0.002
    )
    assert [f.freq for f in horn_bare.filters] == [f.freq for f in horn_masked.filters]
    # Direction, not a budget: the mask drops null bins from the level mask,
    # which can only raise the median it is taken over, so every cut gets
    # shallower and none crosses into gain.
    assert all(
        b.gain >= a.gain and b.gain <= 0.0
        for a, b in zip(horn_bare.filters, horn_masked.filters)
    )
    assert max(
        abs(a.gain - b.gain) for a, b in zip(horn_bare.filters, horn_masked.filters)
    ) == pytest.approx(0.2734, abs=0.002)


@requires_s0_curves
def test_s0_pre_smoothing_exclusion_would_have_cost_the_comb_peaks_real_depth(
    s0_replay,
):
    """The counterfactual behind ``compose_envelope``'s smoothing-order
    decision: one rule mutated, nothing else.

    Rebuilds the envelope with the exclusion zeros pushed THROUGH the ladder
    smoother instead of applied after it, with every other input held fixed
    -- using this file's own ``_hand_ladder_smooth`` and the per-term curves
    the shipped ``EnvelopeCurve`` already carries, so no private module
    state is touched and the ONLY difference is the ordering. The hand-built
    counterfactual therefore also carries #1752's term-exact-zero rule: it is
    held FIXED so that smoothing ORDER stays the one mutated variable.

    Measured on the S0 main leg at ``compression_horn``: 18 in-band bins
    lose allowed depth, worst 5.47 dB. The comb peak at 12786.4 Hz, sitting
    *between* the second and third identified nulls and fully correctable,
    would have fallen from 13.14 to 7.67 dB; the one at 10223.7 Hz from
    16.21 to 12.00 dB.

    Re-measured after #3297 widened the reference tier's mic-trust taper
    (``_MIC_TRUST_TABLE_HZ["reference"]``'s exact-zero, 16 kHz -> 20 kHz,
    owner ruling 2026-08-29): only 1 of these bins now sits at mic-trust's
    own exact zero, versus 5 before, so the bin count no longer falls to 13
    -- it stays at 18, and the two named peaks lose more than previously
    pinned.

    Those peaks are what the registry sized its intervals to protect
    (``IdentifiedNull``: half-depth width, so the span's comb *peaks* stay
    correctable), which is why the shipped order applies the mask last.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    masked = _s0_envelope(s0_replay, _S0_TWEETER_CLASS, cloud=True)

    excluded = masked.terms[ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION] <= 0.0
    in_band = np.array(
        [reason is not ReasonCode.OUT_OF_BAND for reason in masked.reason]
    )
    smoothable = np.min(
        np.stack([
            curve for code, curve in masked.terms.items()
            if code is not ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION
        ]),
        axis=0,
    )
    # #1752's rule, held fixed across the mutation: a term at exactly 0 is a
    # hard boundary. Without it the counterfactual would differ from the
    # shipped curve in TWO rules at once and stop isolating the ordering.
    hard_zero = smoothable <= 0.0
    counterfactual = _hand_ladder_smooth(
        grid, np.where(in_band & ~excluded, smoothable, 0.0)
    )
    counterfactual = np.where(
        in_band & ~excluded & ~hard_zero, counterfactual, 0.0
    )

    # Sanity: the counterfactual still zeroes the nulls themselves -- the
    # mutation is about the neighbourhood, not about the doctrine.
    assert np.all(counterfactual[excluded] == 0.0)

    # Strictly a loss, never a gain: smoothing zeros in can only pull down.
    correctable = in_band & ~excluded
    assert np.all(counterfactual[correctable] <= masked.allowed_depth_db[correctable])
    losses = masked.allowed_depth_db[correctable] - counterfactual[correctable]
    assert int(np.count_nonzero(losses > 0.005)) == 18
    assert float(losses.max()) == pytest.approx(5.47, abs=0.02)

    for f_hz, shipped_db, pre_smoothing_db in (
        (10_223.7, 16.21, 12.00),
        (12_786.4, 13.14, 7.67),
    ):
        i = int(np.argmin(np.abs(grid - f_hz)))
        assert not excluded[i]
        assert float(masked.allowed_depth_db[i]) == pytest.approx(
            shipped_db, abs=0.02
        )
        assert float(counterfactual[i]) == pytest.approx(pre_smoothing_db, abs=0.02)
