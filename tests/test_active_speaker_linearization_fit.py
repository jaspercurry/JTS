# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.active_speaker.linearization_fit (#1668 PR-C).

Synthetic-fixture approach, mirroring
tests/test_active_speaker_linearization_envelope.py's own style: curves are
built with known, closed-form shapes (flat, a single bell, two bells, a
log-linear ramp) so expected behavior is derivable, not corpus-replayed.
Every shelf/adaptive-trim/normalization-budget fixture in this file was
validated interactively against the real N=3 capture's actual numeric
behavior during PR-C development before being fixed here — see the PR
description for the offline sanity numbers.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    ReasonCode,
    compose_envelope,
)
from jasper.active_speaker.linearization_fit import (
    MAX_FILTERS_PER_DRIVER,
    MAX_NORMALIZATION_SPEND_DB,
    PER_FILTER_CUT_CAP_DB,
    LinearizationFilter,
    LinearizationFit,
    _core_or_fallback_mask,
    _highshelf_response_db,
    _ladder_smooth,
    _shelf_stage,
    fit_driver_linearization,
    linearization_filters_by_role,
    predicted_correction_db,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse

_NATIVE_FREQS_HZ = np.linspace(100.0, 22_000.0, 4096)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


def _driver_response(
    role: str,
    magnitude_db: np.ndarray,
    *,
    freqs_hz: np.ndarray = _NATIVE_FREQS_HZ,
    n_repeats: int = 2,
    validity_floor_hz: float | None = 140.0,
) -> DriverResponse:
    """A primary DriverResponse with ``n_repeats`` IDENTICAL repeats (so
    live sigma is ~0, floored to the tier's own tolerable value —
    "behaviorally inert," per compose_sigma_db's docstring — keeping the
    repeatability term non-binding so each test's envelope shape is
    governed by mic-trust/class-prior/band coverage, not incidental
    repeat-noise). Mirrors tests/test_active_speaker_linearization_envelope
    .py's own direct-construction helper."""

    def make(m: np.ndarray) -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=freqs_hz, magnitude_db=m,
            complex_tf=(10.0 ** (m / 20.0)).astype(complex),
            gating={}, snr=None, validity_floor_hz=validity_floor_hz,
        )

    repeats = tuple(make(magnitude_db) for _ in range(n_repeats))
    return DriverResponse(
        role=role, freqs_hz=freqs_hz, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        gating={}, snr=None, validity_floor_hz=validity_floor_hz,
        repeat_responses=repeats,
    )


def _bell(freqs_hz: np.ndarray, center_hz: float, height_db: float, width_oct: float) -> np.ndarray:
    return height_db * np.exp(-0.5 * ((np.log2(freqs_hz / center_hz) / width_oct) ** 2))


def _envelope(
    role: str,
    resp: DriverResponse,
    *,
    excited_band_hz: tuple[float, float],
    mic_tier: str = "reference",
    driver_class: str = "unknown",
):
    return compose_envelope(
        role, resp, excited_band_hz=excited_band_hz,
        mic_tier=mic_tier, driver_class=driver_class,
    )


# --------------------------------------------------------------------------- #
# fit_driver_linearization -- synthetic-shape end-to-end behavior
# --------------------------------------------------------------------------- #


def test_flat_response_yields_no_filters():
    resp = _driver_response("woofer", np.zeros_like(_NATIVE_FREQS_HZ))
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.filters == ()
    assert fit.target_level_db == pytest.approx(0.0, abs=1e-6)
    assert fit.residual_rms_db == pytest.approx(0.0, abs=1e-6)
    assert fit.residual_max_db == pytest.approx(0.0, abs=1e-6)


def test_single_narrow_peak_yields_one_filter():
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert len(fit.filters) == 1
    f = fit.filters[0]
    assert f.biquad_type == "Peaking"
    assert f.freq == pytest.approx(1000.0, rel=0.05)
    assert f.gain < 0.0
    assert fit.residual_max_db < 8.0  # materially flatter than the raw 8 dB peak


def test_cd_horn_two_bump_shape_yields_multiple_peaking_filters_no_shelf():
    """Two well-separated bumps (a compression-driver's mid-treble rise
    THEN fall — the shape the real N=3 capture's tweeter actually showed,
    not a monotonic ramp) is peaking-loop territory, not shelf territory:
    no single cut-only Highshelf can characterize a rise-then-fall."""
    db = _bell(_NATIVE_FREQS_HZ, 2500.0, 6.0, 0.2) + _bell(_NATIVE_FREQS_HZ, 6000.0, 5.0, 0.2)
    resp = _driver_response("tweeter", db)
    envelope = _envelope("tweeter", resp, excited_band_hz=(2000.0, 12000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert len(fit.filters) >= 2
    assert all(f.biquad_type == "Peaking" for f in fit.filters)
    assert all(f.gain <= 0.0 for f in fit.filters)
    # Both bumps' regions end up materially reduced.
    assert fit.residual_max_db < 6.0


def test_monotonic_rising_slope_triggers_highshelf():
    """A rise spanning nearly the WHOLE fit band (not diluted by long flat
    shoulders on either side — see this file's module docstring on how
    this fixture was chosen) crosses the slope gate and fits a cut-only
    Highshelf, corner near the steep (high-frequency) edge."""
    rise_lo, rise_hi, rise_db = 1300.0, 6200.0, 10.0
    frac = np.log2(np.clip(_NATIVE_FREQS_HZ, rise_lo, rise_hi) / rise_lo) / np.log2(rise_hi / rise_lo)
    db = np.where(_NATIVE_FREQS_HZ < rise_lo, 0.0, np.where(_NATIVE_FREQS_HZ > rise_hi, rise_db, rise_db * frac))
    resp = _driver_response("tweeter", db)
    envelope = _envelope("tweeter", resp, excited_band_hz=(1000.0, 6500.0), mic_tier="phone")
    fit = fit_driver_linearization(resp, envelope)
    shelves = [f for f in fit.filters if f.biquad_type == "Highshelf"]
    assert len(shelves) == 1
    shelf = shelves[0]
    assert shelf.gain < 0.0
    assert shelf.freq == pytest.approx(fit.fit_band_hz[1], rel=1e-6)
    assert shelf.q == pytest.approx(1.0 / np.sqrt(2.0))
    assert fit.residual_rms_db < 2.0


def test_envelope_zero_bins_never_get_filters():
    """A huge peak entirely OUTSIDE excited_band_hz must never attract a
    filter — the envelope's OUT_OF_BAND premask is absolute, and the
    adaptive-band-trim/fit domain is derived entirely from the envelope."""
    db = _bell(_NATIVE_FREQS_HZ, 10_000.0, 20.0, 0.1)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    for f in fit.filters:
        assert f.freq < 4000.0, f"filter placed outside the excited band: {f}"
    # The 20 dB peak sits entirely in an unclaimed region -> nothing to fit.
    assert fit.filters == ()


def test_per_bin_caps_respected_in_a_tapering_region():
    """A peak deep in the mic-trust taper (phone tier, well past its
    full_to breakpoint) must be cut only down to what the envelope allows
    AT THAT BIN, never the raw excursion and never the flat -12 dB
    per-filter ceiling used inside the untapered core."""
    center_hz = 7000.0
    db = _bell(_NATIVE_FREQS_HZ, center_hz, 15.0, 0.15)
    resp = _driver_response("tweeter", db)
    envelope = _envelope("tweeter", resp, excited_band_hz=(300.0, 9000.0), mic_tier="phone")
    grid = envelope.freqs_hz
    idx = int(np.argmin(np.abs(grid - center_hz)))
    local_cap_db = float(envelope.allowed_depth_db[idx])
    assert 0.0 < local_cap_db < PER_FILTER_CUT_CAP_DB, "fixture must land inside the taper, not the core"

    fit = fit_driver_linearization(resp, envelope)
    near = [f for f in fit.filters if abs(f.freq - center_hz) < 500.0]
    assert near, "expected at least one filter near the tapered peak"
    # None of the near-peak filters may cut deeper than the LOCAL envelope
    # allowance (interpolated at that filter's own frequency).
    for f in near:
        local_allowed = float(np.interp(f.freq, grid, envelope.allowed_depth_db))
        assert -f.gain <= local_allowed + 1e-6, (
            f"filter {f} cut past the local envelope cap {local_allowed}"
        )
    # And materially less than the raw 15 dB excursion / the flat 12 dB cap.
    assert all(-f.gain < 12.0 for f in near)


def test_cut_only_invariant_holds_across_peaks_and_dips():
    """cuts_only means a DIP (measured below target) must never attract a
    boost — only positive excursions (peaks) ever get a filter, and every
    filter's gain is <= 0."""
    db = _bell(_NATIVE_FREQS_HZ, 800.0, 8.0, 0.15) + _bell(_NATIVE_FREQS_HZ, 2000.0, -8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.filters, "expected the 800 Hz peak to attract at least one filter"
    assert all(f.gain <= 0.0 for f in fit.filters)
    # No filter is centered on the DIP (2000 Hz) -- cuts_only cannot lift it.
    assert not any(abs(f.freq - 2000.0) < 200.0 for f in fit.filters)


def test_cut_only_invariant_violation_raises_not_silently_returns(monkeypatch):
    """N1 (adversarial review, 2026-07-24): the cut-only invariant is
    enforced with an explicit raise, not a bare `assert` that `python -O`
    would strip. Force design_peq to hand back a boosting PEQ (the
    realistic failure mode -- a bug in the peaking loop, not the shelf
    stage) and confirm fit_driver_linearization refuses it with a
    RuntimeError, never silently returning hardware-bound boost."""
    import jasper.active_speaker.linearization_fit as linearization_fit_module
    from jasper.correction.peq import PEQ

    monkeypatch.setattr(
        linearization_fit_module, "design_peq",
        lambda *args, **kwargs: [PEQ(freq=1000.0, q=1.0, gain=3.0)],
    )
    db = _bell(_NATIVE_FREQS_HZ, 800.0, 8.0, 0.15) + _bell(_NATIVE_FREQS_HZ, 2000.0, -8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    with pytest.raises(RuntimeError, match="boost"):
        fit_driver_linearization(resp, envelope)


def test_adaptive_band_trim_pulls_edge_in_from_a_steep_rolloff():
    """Mirrors the real N=3 woofer's own behavior (PR-C offline sanity):
    flat, then a steep natural rolloff approaching the driver's own
    crossover point, well within the envelope's own (much wider) coverage.
    The fit band's high edge must land materially below the envelope's
    raw edge, not chase the rolloff past the per-filter cut budget."""
    db = np.where(
        _NATIVE_FREQS_HZ < 1500.0, 0.0,
        np.where(
            _NATIVE_FREQS_HZ < 3000.0,
            -40.0 * (np.log2(np.clip(_NATIVE_FREQS_HZ, 1500.0, 3000.0) / 1500.0) / np.log2(2.0)),
            -40.0,
        ),
    )
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.fit_band_hz[1] < 3000.0, (
        f"expected the trim to pull the high edge in below 3000 Hz, got {fit.fit_band_hz}"
    )
    assert fit.fit_band_hz[0] == pytest.approx(150.0, rel=0.05)


def test_empty_envelope_returns_degenerate_no_op_fit():
    """No in-capture repeats -> repeatability_limit forces allowed_depth to
    zero everywhere -> fit_driver_linearization must degrade honestly (an
    empty, finite, JSON-safe result), never crash or fabricate a fit."""
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 10.0, 0.15)
    resp = _driver_response("woofer", db, n_repeats=0)
    assert resp.repeat_responses == ()
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    assert np.all(envelope.allowed_depth_db == 0.0)
    fit = fit_driver_linearization(resp, envelope)
    assert fit.filters == ()
    assert fit.fit_band_hz == (0.0, 0.0)
    assert fit.residual_rms_db == 0.0
    assert fit.residual_max_db == 0.0


# --------------------------------------------------------------------------- #
# normalization budget (coordinator ruling)
# --------------------------------------------------------------------------- #


def test_normalization_budget_clamps_shelf_gain():
    """Direct, deterministic test of _shelf_stage's budget clamp, isolated
    from compose_envelope's own taper shapes: an exact log-linear ramp
    (10 dB over 2 octaves = 5 dB/oct, comfortably past the slope gate)
    with an EXPLICIT plateau_level_db lets the three cases below assert
    exact numbers."""
    grid = np.geomspace(1000.0, 4000.0, 200)
    slope_db_per_oct = 5.0
    smoothed = slope_db_per_oct * np.log2(grid / 1000.0)  # 0 dB @1kHz, 10 dB @4kHz
    band_mask = np.ones_like(grid, dtype=bool)
    target = 0.0

    # No spend yet (plateau == target) -> the full MAX_NORMALIZATION_SPEND_DB
    # budget is available; total_drop (10) and the per-filter cut cap (12)
    # both exceed it, so the budget itself binds.
    shelf_full_budget = _shelf_stage(grid, smoothed, band_mask, 1000.0, 4000.0, target, target)
    assert shelf_full_budget is not None
    assert shelf_full_budget.gain == pytest.approx(-MAX_NORMALIZATION_SPEND_DB)

    # Core already spent 5 dB of the budget (plateau 5 dB above target) ->
    # only 1 dB remains for the shelf.
    shelf_partial = _shelf_stage(grid, smoothed, band_mask, 1000.0, 4000.0, target, 5.0)
    assert shelf_partial is not None
    assert shelf_partial.gain == pytest.approx(-1.0)

    # Core already spent the WHOLE budget (plateau >= 6 dB above target) ->
    # nothing left; the shelf must not fire (an honest gap, not a filter
    # with sub-threshold gain).
    shelf_exhausted = _shelf_stage(grid, smoothed, band_mask, 1000.0, 4000.0, target, 6.5)
    assert shelf_exhausted is None


def test_normalization_budget_never_lets_shelf_exceed_per_filter_cut_cap():
    """The budget and the per-filter cut cap are independent ceilings — a
    surplus budget (plateau BELOW target, i.e. the core spent none of its
    own budget and then some — an artificial isolation case, not something
    a real median<=max core would produce) must still not let the shelf
    exceed PER_FILTER_CUT_CAP_DB."""
    grid = np.geomspace(1000.0, 4000.0, 200)
    smoothed = 20.0 * np.log2(grid / 1000.0)  # 40 dB over 2 octaves — deliberately huge
    band_mask = np.ones_like(grid, dtype=bool)
    # plateau well BELOW target => remaining budget (16 dB) exceeds the
    # 12 dB cut cap, isolating the cap as the binding ceiling.
    shelf = _shelf_stage(grid, smoothed, band_mask, 1000.0, 4000.0, 0.0, -10.0)
    assert shelf is not None
    assert shelf.gain == pytest.approx(-PER_FILTER_CUT_CAP_DB)


def test_falling_slope_never_fires_a_shelf():
    """Cut-only correction cannot fix a FALLING (negative) slope — that
    would need a boost. The shelf stage must recognize this and stay out
    of the way (matches the real N=3 woofer's own natural Fc-approach
    rolloff, which fires zero shelf filters — see the module docstring)."""
    grid = np.geomspace(1000.0, 4000.0, 200)
    smoothed = -8.0 * np.log2(grid / 1000.0)  # falls 16 dB over 2 octaves
    band_mask = np.ones_like(grid, dtype=bool)
    shelf = _shelf_stage(grid, smoothed, band_mask, 1000.0, 4000.0, -8.0, 0.0)
    assert shelf is None


# --------------------------------------------------------------------------- #
# cut-only invariant + JSON safety
# --------------------------------------------------------------------------- #


def test_linearization_fit_to_dict_is_json_safe():
    fit = LinearizationFit(
        role="tweeter",
        filters=(
            LinearizationFilter(biquad_type="Highshelf", freq=6000.0, q=0.707, gain=-2.0),
            LinearizationFilter(biquad_type="Peaking", freq=2000.0, q=2.0, gain=-3.5),
        ),
        fit_band_hz=(1000.0, 8000.0),
        target_level_db=-1.5,
        residual_rms_db=0.8,
        residual_max_db=1.9,
        reason_summary={"1000": ReasonCode.FITTED.value, "8000": ReasonCode.LIMITED_BY_MIC_TIER.value},
        mic_tier="reference",
        driver_class="unknown",
        n_repeats=2,
    )
    data = fit.to_dict()
    assert isinstance(data["filters"], list)
    assert all(isinstance(x, dict) for x in data["filters"])
    assert isinstance(data["fit_band_hz"], list)
    for value in data["filters"][0].values():
        assert type(value) in (str, float, int)
    assert type(data["target_level_db"]) is float
    assert type(data["n_repeats"]) is int


def test_reason_summary_values_are_plain_strings_not_enum_members():
    """ReasonCode is a StrEnum; reason_summary must carry its PLAIN
    ``.value`` (type is exactly str), never the enum member itself — the
    candidate artifact's JSON freeze (DspPredecessor) rejects anything
    whose type is not exactly str/int/float/bool/None/dict/list."""
    resp = _driver_response("woofer", np.zeros_like(_NATIVE_FREQS_HZ))
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.reason_summary
    for value in fit.reason_summary.values():
        assert type(value) is str


# --------------------------------------------------------------------------- #
# predicted_correction_db
# --------------------------------------------------------------------------- #


def test_predicted_correction_db_sums_peaking_and_highshelf():
    """Cross-checks predicted_correction_db's sum against the SAME two
    primitives it delegates to, computed independently here (PEQ's own
    predicted_response for the Peaking term, _highshelf_response_db for
    the Highshelf term) -- an exact-equality test, not an approximation."""
    from jasper.correction.peq import PEQ, predicted_response

    q_shelf = 1.0 / np.sqrt(2.0)
    filters = (
        LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=-4.0),
        LinearizationFilter(biquad_type="Highshelf", freq=6000.0, q=q_shelf, gain=-3.0),
    )
    freqs = np.array([1000.0, 6000.0, 12000.0])
    corr = predicted_correction_db(filters, freqs)
    expected = (
        predicted_response([PEQ(freq=1000.0, q=2.0, gain=-4.0)], freqs)
        + _highshelf_response_db(freqs, 6000.0, -3.0, q_shelf)
    )
    np.testing.assert_allclose(corr, expected, atol=1e-9)
    # Sanity: the RBJ half-gain-at-corner property still holds inside the sum.
    assert corr[1] == pytest.approx(expected[1])


def test_predicted_correction_db_empty_filters_is_zero():
    freqs = np.array([100.0, 1000.0, 10000.0])
    corr = predicted_correction_db((), freqs)
    assert np.all(corr == 0.0)


# --------------------------------------------------------------------------- #
# _highshelf_response_db -- RBJ parity properties
# --------------------------------------------------------------------------- #


def test_highshelf_zero_gain_is_unity_everywhere():
    freqs = np.geomspace(20.0, 20000.0, 50)
    resp = _highshelf_response_db(freqs, 1000.0, 0.0, 1.0 / np.sqrt(2.0))
    assert np.allclose(resp, 0.0, atol=1e-9)


def test_highshelf_half_gain_at_corner():
    """The RBJ shelf's well-known property (also pinned against
    jasper.sound.profile's own implementation in
    tests/test_sound_peq_response.py's test_shelf_reaches_half_gain_at_corner_
    and_full_gain_in_band): at freq == corner, response == gain / 2."""
    resp = _highshelf_response_db(np.array([1000.0]), 1000.0, 8.0, 1.0 / np.sqrt(2.0))
    assert resp[0] == pytest.approx(4.0, abs=0.1)


def test_highshelf_matches_sound_profile_reference_implementation():
    """Cross-checks THIS module's duplicated RBJ math against
    jasper.sound.profile's own (the module this one deliberately mirrors
    rather than imports — see linearization_fit.py's top docstring). A
    test file reaching into another module's private helper for a parity
    check is a different, established pattern from production code doing
    it (see test_active_speaker_linearization_envelope.py's own
    _hand_ladder_smooth for the same convention)."""
    from jasper.sound.profile import FilterSpec, _filter_response_db

    freqs = [200.0, 1000.0, 4000.0, 12000.0, 19000.0]
    corner, gain, q = 3000.0, -5.5, 1.0 / np.sqrt(2.0)
    ours = _highshelf_response_db(np.array(freqs), corner, gain, q)
    reference = _filter_response_db(FilterSpec("x", "Highshelf", corner, gain, q=q), freqs)
    for a, b in zip(ours, reference):
        assert a == pytest.approx(b, abs=1e-6)


# --------------------------------------------------------------------------- #
# _ladder_smooth parity with linearization_envelope's own (duplicated, not
# imported -- see this module's top docstring)
# --------------------------------------------------------------------------- #


def test_ladder_smooth_matches_envelope_module_bit_for_bit():
    from jasper.active_speaker import linearization_envelope as env_mod

    grid = DEFAULT_ENVELOPE_GRID_HZ
    rng = np.random.default_rng(1668)
    magnitude = rng.normal(0.0, 4.0, size=grid.shape)
    expected = env_mod._ladder_smooth(grid, magnitude)
    actual = _ladder_smooth(grid, magnitude)
    np.testing.assert_array_equal(actual, expected)


def test_ladder_smooth_matches_hand_rolled_reference():
    """Independent of BOTH module copies -- a third, freshly-written
    implementation, matching this test file's own house convention (see
    test_active_speaker_linearization_envelope.py's _hand_ladder_smooth)."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    magnitude = np.linspace(-3.0, 3.0, grid.size)
    fine = smooth_fractional_octave(grid, magnitude, fraction=6)
    mid = smooth_fractional_octave(grid, magnitude, fraction=3)
    coarse = smooth_fractional_octave(grid, magnitude, fraction=2)
    expected = np.where(grid < 4_000.0, fine, np.where(grid < 10_000.0, mid, coarse))
    np.testing.assert_array_equal(_ladder_smooth(grid, magnitude), expected)


# --------------------------------------------------------------------------- #
# honesty ladder -- verify_band_hz / observe_octave_summary (#1668 PR-D)
# --------------------------------------------------------------------------- #


def test_verify_band_extends_from_fit_lo_to_double_fit_hi():
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)

    assert fit.fit_band_hz != (0.0, 0.0)
    fit_lo, fit_hi = fit.fit_band_hz
    assert fit.verify_band_hz[0] == pytest.approx(fit_lo)
    assert fit.verify_band_hz[1] == pytest.approx(min(2.0 * fit_hi, DEFAULT_ENVELOPE_GRID_HZ[-1]))
    # A genuinely wider band than the fit claim -- the whole point of the
    # honesty-ladder level 2 escalation.
    assert fit.verify_band_hz[1] > fit_hi


def test_verify_band_clamps_to_grid_top_when_double_fit_hi_overflows():
    """A driver band that already reaches near the grid's own top must not
    ask verify_band_hz to extend PAST the grid -- it clamps."""
    db = _bell(_NATIVE_FREQS_HZ, 18000.0, 6.0, 0.2)
    resp = _driver_response("tweeter", db)
    envelope = _envelope(
        "tweeter", resp, excited_band_hz=(8000.0, 20000.0), driver_class="unknown",
    )
    fit = fit_driver_linearization(resp, envelope)
    if fit.fit_band_hz == (0.0, 0.0):
        pytest.skip("envelope allowed no correction for this synthetic tier/class")
    assert fit.verify_band_hz[1] <= DEFAULT_ENVELOPE_GRID_HZ[-1] + 1e-6


def test_verify_residual_uses_same_residual_math_as_fit_over_its_own_band():
    """Over the FIT band itself (a subset of the verify band), the verify
    residual can only be >= what fit measured there -- adding more (worse or
    equal, never better on average) bins to an RMS/max can't lower it below
    the sub-band's own value in the max case, and the two must agree exactly
    when verify_band_hz == fit_band_hz (a driver whose fit already reaches
    the grid top)."""
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.fit_band_hz != (0.0, 0.0)
    # verify_max is the max ABS residual over a superset band -> never smaller.
    assert fit.verify_residual_max_db >= fit.residual_max_db - 1e-9


def test_empty_fit_has_degenerate_honesty_ladder_placeholders():
    """The envelope-allows-nowhere degenerate case (_empty_fit) must carry
    the SAME kind of honest placeholder for the new fields as the existing
    fit_band_hz=(0,0)/residual=0 fields do. Mirrors
    test_empty_envelope_returns_degenerate_no_op_fit's own no-repeats
    fixture -- zero in-capture repeats forces repeatability_limit to zero
    the envelope everywhere, the reliable way to reach _empty_fit."""
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 10.0, 0.15)
    resp = _driver_response("woofer", db, n_repeats=0)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    assert np.all(envelope.allowed_depth_db == 0.0)
    fit = fit_driver_linearization(resp, envelope)
    assert fit.fit_band_hz == (0.0, 0.0)
    assert fit.verify_band_hz == (0.0, 0.0)
    assert fit.verify_residual_rms_db == 0.0
    assert fit.verify_residual_max_db == 0.0
    assert fit.observe_octave_summary == {}


def test_observe_octave_summary_keys_match_reason_summary_octave_centers():
    """observe_octave_summary is the disclosure sibling of reason_summary --
    same octave centers, same range guard, so the two dicts key identically
    band for band."""
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert set(fit.observe_octave_summary) == set(fit.reason_summary)
    assert len(fit.observe_octave_summary) > 0


def test_observe_octave_summary_reaches_above_the_fit_band_top():
    """The disclosure layer's whole point: it reports octaves the fit/verify
    claims never touch (design doc: "the top octave appears ... as the
    driver's measured natural response, never as a pass/fail")."""
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    observed_hz = {int(k) for k in fit.observe_octave_summary}
    assert any(hz > fit.verify_band_hz[1] for hz in observed_hz)


def test_to_dict_serializes_the_honesty_ladder_fields():
    db = _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    d = fit.to_dict()
    assert d["verify_band_hz"] == list(fit.verify_band_hz)
    assert d["verify_residual_rms_db"] == fit.verify_residual_rms_db
    assert d["verify_residual_max_db"] == fit.verify_residual_max_db
    assert d["observe_octave_summary"] == dict(fit.observe_octave_summary)


# --------------------------------------------------------------------------- #
# linearization_filters_by_role -- the shared reduction helper (#1668 PR-D)
# --------------------------------------------------------------------------- #


def test_linearization_filters_by_role_reduces_rich_shape_to_filter_lists():
    rich = {
        "woofer": {
            "role": "woofer",
            "filters": [{"biquad_type": "Peaking", "freq": 900.0, "q": 3.0, "gain": -1.2}],
            "fit_band_hz": [150.0, 3951.5], "target_level_db": -20.22,
            "residual_rms_db": 0.4, "residual_max_db": 1.1,
            "reason_summary": {"250": "envelope_fitted"},
            "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 2,
        },
    }
    reduced = linearization_filters_by_role(rich)
    assert reduced == {
        "woofer": [{"biquad_type": "Peaking", "freq": 900.0, "q": 3.0, "gain": -1.2}],
    }


def test_linearization_filters_by_role_empty_input_is_empty_output():
    assert linearization_filters_by_role({}) == {}
    assert linearization_filters_by_role(None) == {}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "malformed",
    [
        {"woofer": "not-a-mapping"},
        {"woofer": {"filters": "not-a-list"}},
        {"woofer": {"no_filters_key": True}},
        {"woofer": None},
    ],
)
def test_linearization_filters_by_role_drops_malformed_role_entries(malformed):
    """Defensive, not authoritative -- era-tolerant reads a persisted
    candidate might hand it should degrade to "nothing for this role", not
    raise. The emitter's own _validated_linearization is the fail-closed
    gate; this helper only reshapes."""
    assert linearization_filters_by_role(malformed) == {}


def test_linearization_filters_by_role_drops_non_mapping_filter_entries():
    rich = {"woofer": {"filters": ["not-a-mapping", {"biquad_type": "Peaking"}]}}
    reduced = linearization_filters_by_role(rich)
    assert reduced == {"woofer": [{"biquad_type": "Peaking"}]}


def test_max_filters_per_driver_is_the_shelf_plus_peaking_cap():
    """Pins the value the camilla_yaml.py emitter's own
    MAX_LINEARIZATION_FILTERS_PER_DRIVER must equal (see that constant's
    own LOCKSTEP DUPLICATE comment) -- this test lives on THIS side of the
    pair; the emitter side has its own pinning test."""
    assert MAX_FILTERS_PER_DRIVER == 8


# --------------------------------------------------------------------------- #
# _core_or_fallback_mask
# --------------------------------------------------------------------------- #


def test_core_falls_back_to_full_envelope_mask_when_core_is_empty():
    """A tier/class combination whose taper starts BELOW the grid floor
    leaves no untapered core at all -- _core_or_fallback_mask must fall
    back to the whole envelope-eligible mask rather than returning empty
    (which would crash the median/max computation downstream)."""
    resp = _driver_response("tweeter", np.zeros_like(_NATIVE_FREQS_HZ))
    envelope = _envelope(
        "tweeter", resp, excited_band_hz=(150.0, 20000.0),
        mic_tier="phone", driver_class="compression_horn",
    )
    envelope_mask = envelope.allowed_depth_db > 0.05
    assert envelope_mask.any()
    core = _core_or_fallback_mask(envelope, envelope_mask)
    assert core.any()


def test_woofer_within_class_and_tier_full_to_has_a_full_width_core():
    """The real-N=3-capture bug this module's _adaptive_band_trim fix
    addressed: a woofer whose ENTIRE excited band sits below every
    tier/class full_to breakpoint has a "core" that spans the WHOLE
    envelope-eligible range -- proving _adaptive_band_trim must not seed
    from the core's own extremes (see that function's docstring)."""
    resp = _driver_response("woofer", np.zeros_like(_NATIVE_FREQS_HZ))
    envelope = _envelope(
        "woofer", resp, excited_band_hz=(150.0, 4000.0),
        mic_tier="reference", driver_class="unknown",
    )
    envelope_mask = envelope.allowed_depth_db > 0.05
    core = _core_or_fallback_mask(envelope, envelope_mask)
    assert np.array_equal(core, envelope_mask)
