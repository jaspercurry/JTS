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

import math
import re

import numpy as np
import pytest

from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    ReasonCode,
    compose_envelope,
)
from jasper.active_speaker._common import DRIVER_CLASSES
from jasper.active_speaker.linearization_fit import (
    CUT_ONLY_VOCABULARY,
    HF_CONTINUATION_POLICY,
    HF_REALIZATION_TOLERANCE_DB,
    HF_SINGLE_SHELF_SPEND_CAP_DB,
    HF_SUPPRESSION_REASONS,
    HF_TAPER_MAX_DB,
    LIFT_SUPPRESSION_REASONS,
    MAX_FILTERS_PER_DRIVER,
    MAX_NORMALIZATION_SPEND_DB,
    PER_FILTER_BOOST_CAP_DB,
    PER_FILTER_CUT_CAP_DB,
    _CUT_REDUCTION_EPS_DB,
    _ENVELOPE_NONZERO_EPS_DB,
    _MIN_FILTER_GAIN_DB,
    FitVocabulary,
    LinearizationFilter,
    LinearizationFit,
    _HF_MIN_OCCURRENCES,
    _HIGHSHELF_Q,
    _core_or_fallback_mask,
    _highshelf_response_db,
    _ladder_smooth,
    _power_band_average_db,
    _shelf_stage,
    complex_correction_response,
    core_level_band_hz,
    driver_core_level_db,
    fit_driver_linearization,
    linearization_filters_by_role,
    reduce_cuts_for_lift,
    solve_shared_level_frame,
)
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.correction.peq import PEQ, predicted_response

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
    not a monotonic ramp) is peaking-loop territory, not (rising) shelf
    territory: no single cut-only Highshelf can characterize a rise-then-fall.

    The excited band stops at 8 kHz (below the reference-tier confidence knee)
    on purpose, so this fixture isolates the RISING-slope ``_shelf_stage``
    behavior from the CD-horn continuation stage (#1668) — the latter only
    engages when the fit band reaches the confidence ceiling, and has its own
    dedicated tests below. Here, neither shelf mechanism should fire."""
    db = _bell(_NATIVE_FREQS_HZ, 2500.0, 6.0, 0.2) + _bell(_NATIVE_FREQS_HZ, 6000.0, 5.0, 0.2)
    resp = _driver_response("tweeter", db)
    envelope = _envelope("tweeter", resp, excited_band_hz=(2000.0, 8000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert len(fit.filters) >= 2
    assert all(f.biquad_type == "Peaking" for f in fit.filters)
    assert all(f.gain <= 0.0 for f in fit.filters)
    assert fit.hf_continuation_spend_db == 0.0  # CD-horn stage ineligible here
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
    with an EXPLICIT plateau_level_db lets the cases below assert exact
    numbers. Written symbolically in ``MAX_NORMALIZATION_SPEND_DB`` so it
    tracks the constant (raised 6 → 12 → 18 as the CD-horn work progressed)
    rather than a baked-in number: the plateau spends a chunk of the budget
    first, so the REMAINING budget — not the ramp's own 10 dB total_drop nor
    the 12 dB per-filter cap — is the binding ceiling on the shelf gain."""
    grid = np.geomspace(1000.0, 4000.0, 200)
    slope_db_per_oct = 5.0
    smoothed = slope_db_per_oct * np.log2(grid / 1000.0)  # 0 dB @1kHz, 10 dB @4kHz
    band_mask = np.ones_like(grid, dtype=bool)
    target = 0.0
    total_drop_db = 10.0  # the ramp's own value at the 4 kHz corner

    # Leave exactly 6 dB of budget (plateau spends the rest) -> the remaining
    # budget is below total_drop (10) and below the per-filter cap (12), so the
    # budget is the SOLE binding ceiling. Derived from the constant so the case
    # stays budget-bound whatever MAX_NORMALIZATION_SPEND_DB is raised to.
    remaining_target_db = 6.0
    plateau_spent = MAX_NORMALIZATION_SPEND_DB - remaining_target_db
    assert remaining_target_db < min(total_drop_db, PER_FILTER_CUT_CAP_DB)
    shelf_budget_bound = _shelf_stage(
        grid, smoothed, band_mask, 1000.0, 4000.0, target, plateau_spent,
    )
    assert shelf_budget_bound is not None
    assert shelf_budget_bound.gain == pytest.approx(-remaining_target_db)

    # Core spent more (plateau higher) -> even less remains; the shelf gain
    # shrinks proportionally, proving the clamp tracks the remaining budget.
    remaining_smaller_db = 2.0
    plateau_spent_more = MAX_NORMALIZATION_SPEND_DB - remaining_smaller_db
    shelf_partial = _shelf_stage(
        grid, smoothed, band_mask, 1000.0, 4000.0, target, plateau_spent_more,
    )
    assert shelf_partial is not None
    assert shelf_partial.gain == pytest.approx(-remaining_smaller_db)

    # Core already spent the WHOLE budget (plateau >= MAX above target) ->
    # nothing left; the shelf must not fire (an honest gap, not a filter with
    # sub-threshold gain).
    shelf_exhausted = _shelf_stage(
        grid, smoothed, band_mask, 1000.0, 4000.0, target,
        MAX_NORMALIZATION_SPEND_DB + 0.5,
    )
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
# complex_correction_response (#1667 — minimum-phase branch correction)
# --------------------------------------------------------------------------- #


def test_complex_correction_response_magnitude_matches_filter_response_db():
    """Magnitude-consistency parity: ``abs(complex_correction_response)`` equals
    ``10**(sum of jasper.sound.profile._filter_response_db over filters / 20)``
    bin-for-bin. The complex correction and the emitted graph's magnitude share
    the ``_biquad_coeffs`` SSOT, so the applied correction's magnitude can never
    silently drift from what CamillaDSP realizes -- only its phase is added."""
    from jasper.sound.profile import _filter_response_db

    q_shelf = 1.0 / np.sqrt(2.0)
    filters = (
        LinearizationFilter(biquad_type="Peaking", freq=803.0, q=1.35, gain=-5.57),
        LinearizationFilter(biquad_type="Peaking", freq=4064.0, q=2.0, gain=-2.92),
        LinearizationFilter(biquad_type="Highshelf", freq=6000.0, q=q_shelf, gain=-3.0),
    )
    freqs = np.geomspace(30.0, 20000.0, 512)
    H = complex_correction_response(filters, freqs)
    mag_db = np.zeros_like(freqs)
    for f in filters:
        mag_db = mag_db + np.asarray(_filter_response_db(f, freqs))
    np.testing.assert_allclose(np.abs(H), 10.0 ** (mag_db / 20.0), atol=1e-6, rtol=0)


def test_complex_correction_response_empty_filters_is_unity():
    """No filters -> unity in the LINEAR domain (the multiplicative identity a
    caller applies as ``W * H``), the complex analogue of the old
    magnitude-domain ``0 dB``."""
    freqs = np.array([100.0, 1000.0, 10000.0])
    H = complex_correction_response((), freqs)
    np.testing.assert_allclose(H, np.ones_like(freqs, dtype=np.complex128))


def test_complex_correction_response_carries_minimum_phase():
    """The load-bearing difference from a zero-phase magnitude scale: a peaking
    biquad's phase is non-zero off its centre. A zero-phase model would return
    a real (phase-0) response everywhere; this must not."""
    filters = (LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=1.0, gain=-6.0),)
    freqs = np.array([500.0, 1000.0, 2000.0])
    H = complex_correction_response(filters, freqs)
    # At the exact centre a peaking biquad is real (phase 0); on either skirt it
    # rotates. Assert a materially non-zero rotation off-centre.
    assert abs(np.angle(H[1])) < 1e-9  # centre: real
    assert abs(np.degrees(np.angle(H[0]))) > 5.0  # below centre: rotated
    assert abs(np.degrees(np.angle(H[2]))) > 5.0  # above centre: rotated


def test_complex_correction_response_phase_sensitivity_two_branch_sum():
    """#1667 regression — the exact failure shape. On an inverted-polarity LR4
    two-branch crossover (the JTS3 session's own topology: polarity=invert, so
    the branches null near Fc and the summation is phase-DOMINATED there),
    modeling a driver correction as a ZERO-PHASE magnitude scale mispredicts the
    summed response by >1 dB under the SAME notch-excluded comparator the VERIFY
    gate reads, while the COMPLEX minimum-phase correction (what the emitted
    graph actually realizes) matches the true summation exactly. This reproduces
    the mechanism behind the live failure: on session 2's captures the
    zero-phase model was ~2.0 dB off (WORSE than the ~1.7 dB of no correction at
    all), the complex model ~0.5 dB. If a future change reverts the branch
    correction to a magnitude scale (``W * 10**(db/20)``), this test fails.
    """
    from jasper.audio_measurement.analysis import notch_excluded_tracking_error_db
    from jasper.audio_measurement.program_analysis import (
        VERIFY_NOTCH_EXCLUSION_DB, predicted_branch_sum,
    )
    from jasper.sound.profile import _filter_response_db

    fc = 2000.0
    freqs = np.geomspace(500.0, 8000.0, 4096)
    # Minimum-phase LR4 branches (Butterworth-squared low/high pass):
    s = 1j * (freqs / fc)
    bw2 = 1.0 / (1.0 + np.sqrt(2.0) * s + s * s)
    w_branch = bw2 * bw2                                        # LR4 lowpass (woofer)
    t_branch = (s * s / (1.0 + np.sqrt(2.0) * s + s * s)) ** 2  # LR4 highpass (tweeter)
    sign = -1  # inverted polarity — the JTS3 session's own; branches null at Fc

    # A woofer correction whose skirt reaches the crossover region, so its
    # minimum-phase rotation shifts the (near-null) two-branch interference.
    corr = (LinearizationFilter(biquad_type="Peaking", freq=1600.0, q=1.5, gain=-6.0),)
    H_complex = complex_correction_response(corr, freqs)
    mag_db = np.asarray(_filter_response_db(corr[0], freqs))
    H_zero_phase = 10.0 ** (mag_db / 20.0)  # the OLD model: magnitude, no phase

    to_db = lambda z: 20.0 * np.log10(np.maximum(np.abs(z), 1e-12))
    # The emitted graph applies the COMPLEX correction; that summation is truth.
    true_db = to_db(predicted_branch_sum(w_branch * H_complex, t_branch, 0.0, 0.0, sign))
    zero_phase_db = to_db(predicted_branch_sum(w_branch * H_zero_phase, t_branch, 0.0, 0.0, sign))

    # Compare each model's predicted summation against truth the SAME way the
    # VERIFY gate does (notch-excluded max, so the deep-null bins — where any
    # model is hypersensitive — don't dominate; the divergence measured here is
    # the shoulder region, exactly what the live gate scored).
    band = (fc / 2.0, fc * 2.0)
    _, zero_phase_err = notch_excluded_tracking_error_db(
        freqs, zero_phase_db, true_db, band,
        notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB, notch_reference_db=true_db)
    assert zero_phase_err > 1.0, f"zero-phase should mispredict by >1 dB, got {zero_phase_err}"
    # The complex model's exactness against the emitted-biquad truth is pinned
    # independently by the profile-level magnitude/phase parity tests
    # (test_filter_response_complex_*); re-asserting truth-vs-truth here was
    # tautological (2026-07-24 review nit) — this test's load-bearing claim is
    # the zero-phase misprediction above.


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


def test_reported_residual_grades_the_realized_biquad_not_the_lorentzian():
    """R10b (#1988): every REPORTED number grades the cascade the graph emits.

    The peaking stage folds itself into the working curve with
    :func:`jasper.correction.peq.predicted_response`, a Lorentzian in
    log-frequency whose HALF-WIDTH matches the RBJ peaking biquad but whose
    skirts do not ("the far skirts are still a Lorentzian approximation" —
    ``_bell_response_db``'s own docstring), and which knows nothing of the
    bilinear warping that reshapes a biquad near the top of the band. Using it
    to fit is fine — a search heuristic may be approximate. Using it to GRADE
    is the shelf-Q defect class of 2026-07-27 in peaking form: a fit auditing
    itself with an evaluator the hardware does not run
    (see :mod:`jasper.active_speaker.delta_probe`'s docstring).

    Under the default CUT-ONLY vocabulary the lift stage is inert, so before
    R10b the whole-cascade rebuild never fired and every claim below was the
    Lorentzian's opinion.

    The fixture is chosen for a KNOWN skirt gap — four HF peaks corrected by
    Peaking filters only — and the assertion is deliberately TWO-SIDED: pinning
    only "matches the exact evaluator" would pass vacuously on a fixture where
    the two evaluators happen to agree, so the gap is asserted to be real.
    """
    db = (
        _bell(_NATIVE_FREQS_HZ, 3000.0, 6.0, 0.12)
        + _bell(_NATIVE_FREQS_HZ, 6000.0, 6.0, 0.12)
        + _bell(_NATIVE_FREQS_HZ, 11000.0, 7.0, 0.12)
        + _bell(_NATIVE_FREQS_HZ, 17000.0, 8.0, 0.12)
    )
    resp = _driver_response("tweeter", db)
    envelope = _envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0), driver_class="unknown",
    )
    fit = fit_driver_linearization(resp, envelope)  # CUT_ONLY_VOCABULARY default

    assert fit.fit_band_hz != (0.0, 0.0)
    assert fit.filters, "fixture must place filters for the evaluators to differ"
    assert all(f.biquad_type == "Peaking" for f in fit.filters), (
        "this fixture is the peaking path; a shelf would fold in exactly and "
        "dilute what the test is pinning"
    )

    grid_hz = envelope.freqs_hz
    smoothed_db = _ladder_smooth(
        grid_hz, np.interp(grid_hz, resp.freqs_hz, resp.magnitude_db)
    )
    # The two evaluators, over the WHOLE cascade, on the fit's own grid.
    exact_db = smoothed_db + 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(fit.filters, grid_hz)), 1e-12)
    )
    lorentzian_db = smoothed_db + predicted_response(
        [PEQ(freq=f.freq, q=f.q, gain=f.gain) for f in fit.filters], grid_hz
    )
    # The gap is real and materially larger than the tolerances asserted below.
    assert np.max(np.abs(lorentzian_db - exact_db)) > 0.5

    fit_lo_hz, fit_hi_hz = fit.fit_band_hz
    band_mask = (
        (grid_hz >= fit_lo_hz)
        & (grid_hz <= fit_hi_hz)
        & (envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB)
    )
    verify_mask = (grid_hz >= fit.verify_band_hz[0]) & (
        grid_hz <= fit.verify_band_hz[1]
    )
    target_db = fit.target_level_db  # no BranchTarget supplied -> flat target

    def rms_max(curve, mask):
        r = (curve - target_db)[mask]
        return float(np.sqrt(np.mean(r ** 2))), float(np.max(np.abs(r)))

    exact_rms, exact_max = rms_max(exact_db, band_mask)
    lorentzian_rms, lorentzian_max = rms_max(lorentzian_db, band_mask)

    # 1. What is reported IS the realized cascade's residual.
    assert fit.residual_rms_db == pytest.approx(exact_rms, abs=1e-9)
    assert fit.residual_max_db == pytest.approx(exact_max, abs=1e-9)
    # 2. ...and is NOT the Lorentzian's, which pre-R10b it was.
    assert abs(lorentzian_rms - exact_rms) > 0.05
    assert abs(lorentzian_max - exact_max) > 0.05

    # Same rule for the VERIFY rung. Its gap is asserted on the MAX, not the
    # RMS: verify spans an octave past the fit band, so an RMS over ~83 bins
    # dilutes a localized skirt error down to ~0.02 dB here. The worst bin is
    # where the two evaluators actually disagree, and it is the number a
    # reader would act on.
    v_exact_rms, v_exact_max = rms_max(exact_db, verify_mask)
    _v_lor_rms, v_lor_max = rms_max(lorentzian_db, verify_mask)
    assert fit.verify_residual_rms_db == pytest.approx(v_exact_rms, abs=1e-9)
    assert fit.verify_residual_max_db == pytest.approx(v_exact_max, abs=1e-9)
    assert abs(v_lor_max - v_exact_max) > 0.05

    # ...and for the give-back, which is NOT report-only: it is the SSOT
    # crossover_v2_flow anchors each branch's linearized trim on, so grading it
    # with the wrong evaluator moved an emitted TRIM.
    level_mask = _core_or_fallback_mask(
        envelope, envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB,
    )
    exact_giveback = (
        _power_band_average_db(smoothed_db, level_mask)
        - _power_band_average_db(exact_db, level_mask)
    )
    lorentzian_giveback = (
        _power_band_average_db(smoothed_db, level_mask)
        - _power_band_average_db(lorentzian_db, level_mask)
    )
    assert fit.correction_giveback_db == pytest.approx(exact_giveback, abs=1e-9)
    assert abs(lorentzian_giveback - exact_giveback) > 0.05


def test_every_stage_combination_reports_the_realized_cascades_residual():
    """The R10b rule holds for every path through the fit, not one fixture.

    Eight runs — four shapes x both vocabularies — reaching the shelf, peaking,
    CD-horn and lift stages in their various combinations. In each, the reported
    ``residual_rms_db`` is recomputed from ``complex_correction_response`` over
    the fit's own band and give-back frame, and must match.

    **What this does NOT prove**, stated so the name is not read as more:
    that the fix leaves the emitted filters alone. A single-revision test cannot
    show that. The no-filter-moves claim rests on the seam's structure (the
    rebuild is the last write to ``working_db``; no filter-producing stage runs
    after it) and on the two-revision measurement in
    ``captures/r10b-alignment-20260801/lorentzian_gap_probe.py``, which compares
    the emitted sets across revisions on a real corpus. What this test adds is
    that no stage combination leaves an approximate claim behind.
    """
    fixtures = (
        ("woofer", (150.0, 4000.0), _bell(_NATIVE_FREQS_HZ, 1000.0, 8.0, 0.15)),
        ("woofer", (150.0, 4000.0),
         _bell(_NATIVE_FREQS_HZ, 400.0, 6.0, 0.2)
         + _bell(_NATIVE_FREQS_HZ, 1800.0, 7.0, 0.18)),
        ("tweeter", (2000.0, 20000.0),
         _bell(_NATIVE_FREQS_HZ, 3000.0, 6.0, 0.12)
         + _bell(_NATIVE_FREQS_HZ, 11000.0, 7.0, 0.12)),
        # A downward-sloping tweeter: reaches the CD-horn and lift stages.
        ("tweeter", (2000.0, 20000.0),
         -6.0 * np.log2(_NATIVE_FREQS_HZ / 2000.0)),
    )
    boost = FitVocabulary(allow_boost=True)
    for role, band, db in fixtures:
        resp = _driver_response(role, db)
        envelope = _envelope(role, resp, excited_band_hz=band, driver_class="unknown")
        for vocabulary in (CUT_ONLY_VOCABULARY, boost):
            fit = fit_driver_linearization(
                resp, envelope, vocabulary=vocabulary,
            )
            # Every emitted filter is a real biquad the emitter can realize,
            # and the reported residual is that cascade's own.
            for f in fit.filters:
                assert f.biquad_type in {"Peaking", "Highshelf", "Lowshelf"}
            if not fit.filters or fit.fit_band_hz == (0.0, 0.0):
                continue
            grid_hz = envelope.freqs_hz
            smoothed_db = _ladder_smooth(
                grid_hz, np.interp(grid_hz, resp.freqs_hz, resp.magnitude_db)
            )
            exact_db = smoothed_db + 20.0 * np.log10(
                np.maximum(
                    np.abs(complex_correction_response(fit.filters, grid_hz)), 1e-12,
                )
            )
            lo, hi = fit.fit_band_hz
            mask = (
                (grid_hz >= lo) & (grid_hz <= hi)
                & (envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB)
            )
            frame_db = fit.target_level_db - fit.hf_continuation_spend_db
            residual = (exact_db - frame_db)[mask]
            assert fit.residual_rms_db == pytest.approx(
                float(np.sqrt(np.mean(residual ** 2))), abs=1e-9,
            ), f"{role}/{band}/{vocabulary}"


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


def test_linearization_filters_by_role_on_already_reduced_shape_is_empty():
    """THE TRAP (#1668 PR-D review SF1): this helper expects the RICH
    ``{role: {filters: [...]}}`` shape, not its own already-reduced
    ``{role: [filter_dict, ...]}`` output. Calling it again on the reduced
    shape returns {} for every role (each value is a list, which fails the
    isinstance(fit, Mapping) check) -- exactly why
    baseline_profile.recompose_applied_baseline_yaml does NOT call this
    helper and instead re-validates the already-reduced snapshot inline."""
    already_reduced = {
        "woofer": [{"biquad_type": "Peaking", "freq": 900.0, "q": 3.0, "gain": -1.2}],
    }
    assert linearization_filters_by_role(already_reduced) == {}


def test_max_filters_per_driver_is_the_shelf_plus_peaking_cap():
    """Pins the value the camilla_yaml.py emitter's own
    MAX_LINEARIZATION_FILTERS_PER_DRIVER must equal (see that constant's
    own LOCKSTEP DUPLICATE comment) -- this test lives on THIS side of the
    pair; the emitter side has its own pinning test."""
    assert MAX_FILTERS_PER_DRIVER == 8


def test_hf_min_occurrences_matches_flow_paired_gate():
    """LOCKSTEP pin (mirrors test_max_linearization_filters_matches_fit_engine_cap):
    the CD-horn agreement gate's minimum-occurrence threshold must equal the flow
    layer's paired-gate constant, or the fit engine and the conductor would
    disagree about the N>=3 'paired gate.' Kept as a local constant in the fit
    module (not imported) because the flow imports THIS module -- this test is
    the guard that keeps the two numerically identical."""
    from jasper.active_speaker.crossover_v2_flow import (
        LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    )
    assert _HF_MIN_OCCURRENCES == LINEARIZATION_MIN_PAIRED_OCCURRENCES


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


# --------------------------------------------------------------------------- #
# CD-horn compensation stage (#1668)
# --------------------------------------------------------------------------- #

# A compression-driver-on-a-horn: flat 2-8 kHz, then the horn's constant-
# directivity top-octave rolloff (~-2.5 @10k ... -11.5 @16k, relative to the
# trusted 4-8 kHz band). Anchored + log-interpolated so the shape is derivable.
_CD_HORN_ANCHOR_HZ = np.array(
    [2000.0, 4000.0, 8000.0, 10000.0, 12000.0, 14000.0, 16000.0, 18000.0, 20000.0]
)
_CD_HORN_ANCHOR_DB = np.array([0.0, 0.0, 0.0, -2.5, -5.8, -8.6, -11.5, -13.5, -15.0])
# The LIVE JTS3 rig's own shape (2026-07-24): a ~14.3 dB top-octave deficit at
# the reference-tier ceiling. Exceeds PER_FILTER_CUT_CAP_DB, so it exercises the
# Lowshelf clamp + peaking-residual absorption path.
_CD_HORN_LIVE_ANCHOR_DB = np.array([0.0, 0.0, 0.0, -3.2, -7.4, -11.0, -14.3, -16.0, -17.0])


def _cd_horn_db(freqs_hz: np.ndarray, anchor_db: np.ndarray = _CD_HORN_ANCHOR_DB) -> np.ndarray:
    return np.interp(np.log2(freqs_hz), np.log2(_CD_HORN_ANCHOR_HZ), anchor_db)


def _tweeter_response(magnitude_db, *, repeat_mags=None, role="tweeter", freqs_hz=_NATIVE_FREQS_HZ):
    """A tweeter primary whose repeats can be specified individually (for the
    agreement gate). ``repeat_mags=None`` gives two IDENTICAL repeats (agree)."""
    def mk(m):
        return DriverResponse(
            role=role, freqs_hz=freqs_hz, magnitude_db=m,
            complex_tf=(10.0 ** (m / 20.0)).astype(complex),
            gating={}, snr=None, validity_floor_hz=140.0,
        )

    if repeat_mags is None:
        repeat_mags = [magnitude_db, magnitude_db]
    return DriverResponse(
        role=role, freqs_hz=freqs_hz, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        gating={}, snr=None, validity_floor_hz=140.0,
        repeat_responses=tuple(mk(m) for m in repeat_mags),
    )


def _cd_horn_fit(driver_class="compression_horn", *, anchor_db=_CD_HORN_ANCHOR_DB, role="tweeter", repeat_mags=None):
    mag = _cd_horn_db(_NATIVE_FREQS_HZ, anchor_db)
    resp = _tweeter_response(mag, repeat_mags=repeat_mags, role=role)
    envelope = compose_envelope(
        role, resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class=driver_class,
    )
    return fit_driver_linearization(resp, envelope)


def test_cd_horn_falling_top_octave_fires_lowshelf_give_back():
    """The keystone: a reference-tier compression-horn tweeter with agreeing
    repeats FIRES the stage — a Lowshelf backbone at position 0, cut-only, and
    the realized cascade tracking cut_target (the stage only fires when the
    fit-quality gate passed, and the give-back-frame residual is small).

    This synthetic's measured deficit (~11.4 dB) sits just above the
    single-shelf realization cap, so spend lands ON that cap while
    ``measured_deficit_at_ceiling_db`` keeps reporting the uncapped measurement.
    """
    fit = _cd_horn_fit("compression_horn")
    assert fit.hf_continuation_spend_db > 0.0
    assert fit.hf_continuation_suppressed_reason == ""
    assert fit.hf_continuation_policy == "hold"
    assert fit.hf_continuation_ceiling_hz == pytest.approx(16444.9, rel=0.01)
    assert fit.filters[0].biquad_type == "Lowshelf"  # backbone at position 0
    assert fit.filters[0].gain == pytest.approx(-fit.hf_continuation_spend_db)
    assert all(f.gain <= 0.0 for f in fit.filters)  # cut-only invariant
    # Spend is bounded by the single-shelf realization cap here, and the
    # UNCAPPED measurement stays disclosed alongside it.
    assert fit.hf_continuation_spend_db == pytest.approx(HF_SINGLE_SHELF_SPEND_CAP_DB)
    assert 10.0 < fit.measured_deficit_at_ceiling_db < 12.0
    assert fit.measured_deficit_at_ceiling_db > fit.hf_continuation_spend_db
    assert fit.hf_continuation_spend_db <= MAX_NORMALIZATION_SPEND_DB
    # Realized cascade tracks cut_target -> the give-back frame is flat.
    assert fit.residual_max_db < HF_REALIZATION_TOLERANCE_DB


def test_cd_horn_realized_cascade_tracks_cut_target_within_tolerance():
    """Direct check that the EMITTED filters realize the intended cut-domain
    give-back: applied to the smoothed measured curve, the result is flat in
    the give-back frame (target - spend) across the trusted band, to within the
    realization tolerance — the same shape the fit-quality gate enforces."""
    mag = _cd_horn_db(_NATIVE_FREQS_HZ)
    resp = _tweeter_response(mag)
    envelope = compose_envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class="compression_horn",
    )
    fit = fit_driver_linearization(resp, envelope)
    assert fit.hf_continuation_spend_db > 0.0
    grid = envelope.freqs_hz
    smoothed = _ladder_smooth(grid, np.interp(grid, resp.freqs_hz, resp.magnitude_db))
    realized = 20.0 * np.log10(
        np.abs(complex_correction_response(fit.filters, grid))
    )
    working = smoothed + realized
    frame_target = fit.target_level_db - fit.hf_continuation_spend_db
    # Over the trusted band up to the ceiling, the corrected curve tracks the
    # give-back frame (flat) rather than falling ~spend below it.
    band = (grid >= 4000.0) & (grid <= fit.hf_continuation_ceiling_hz)
    assert float(np.max(np.abs(working - frame_target)[band])) < 2.0


def test_cd_horn_budget_binding_reports_uncapped_measured_deficit():
    """When the REMAINING normalization budget is smaller than the measured
    top-octave deficit, the spend is capped but measured_deficit_at_ceiling_db
    still reports the UNCAPPED measured value — so a partially-corrected top
    octave is visible, not silently rounded to the capped spend.

    The budget is bound here via the OTHER budget term — a narrow +9 dB core
    bump, which raises `plateau_level_db` well above the median target and so
    charges most of MAX_NORMALIZATION_SPEND_DB before the CD-horn stage runs.
    (Binding it with a deeper top-octave rolloff instead no longer works: at an
    18 dB budget a deficit that large makes the shape too steep for the
    realization to track, so the fit-quality gate — not the budget — becomes the
    binding constraint and the stage suppresses.)"""
    mag = _cd_horn_db(_NATIVE_FREQS_HZ) + _bell(_NATIVE_FREQS_HZ, 3000.0, 9.0, 0.12)
    resp = _tweeter_response(mag)
    envelope = compose_envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class="compression_horn",
    )
    fit = fit_driver_linearization(resp, envelope)
    assert fit.hf_continuation_spend_db > 0.0
    assert fit.hf_continuation_suppressed_reason == ""
    # Spend capped below the measured deficit by the remaining budget.
    assert fit.measured_deficit_at_ceiling_db > fit.hf_continuation_spend_db + 0.2
    assert fit.hf_continuation_spend_db < MAX_NORMALIZATION_SPEND_DB


def test_correction_giveback_approximates_spend_on_the_canonical_synthetic():
    """The give-back SSOT: ``correction_giveback_db`` is the MEASURED
    before-vs-after power-domain level delta of the driver's core (reference)
    band, reported POSITIVE. On the canonical fired synthetic — a flat core — it
    lands close to the CD-horn spend, which is what makes the flow's anchored
    trim restore the pre-correction level.

    ``spend`` is NOT the definition, only a sanity companion, so the tolerance is
    1.0 dB rather than 0.5. Measured deltas (giveback − spend) across the fixture
    family:

        canonical (11.4 dB deficit)   spend 11.000  giveback 11.775  +0.775
        live-rig  (14.3 dB deficit)   spend 11.000  giveback 11.046  +0.046
        budget-bound (+9 dB bump)     spend  9.880  giveback 12.773  +2.893
        woofer, flattening cuts only  spend  0.000  giveback  1.458  +1.458
        flat, no filters              spend  0.000  giveback  0.000   0.000

    (The two deficit cases now share a spend: both exceed the single-shelf
    realization cap, so both land on it.)

    The larger deltas are correct, not error: whenever the correction also cuts
    INSIDE the core band (the CD-horn residual peak near the onset, a flattening
    cut on a bumped core) it removes real level there, and the anchor restores
    exactly what was removed. Only the flat-core cases are expected near spend.
    """
    fit = _cd_horn_fit("compression_horn")
    assert fit.hf_continuation_spend_db > 0.0
    assert fit.correction_giveback_db == pytest.approx(
        fit.hf_continuation_spend_db, abs=1.0
    )
    # Reported positive (a cut cascade gives level BACK).
    assert fit.correction_giveback_db > 0.0


def test_correction_giveback_is_zero_without_filters_and_positive_with_them():
    """Era-tolerant default + the flattening-only case: a fit that emitted no
    filters reports 0.0 give-back; a woofer carrying only ordinary flattening
    cuts still reports a positive give-back, so the flow can anchor BOTH
    branches, not just the CD-horn one."""
    flat = _driver_response("woofer", np.zeros_like(_NATIVE_FREQS_HZ))
    flat_fit = fit_driver_linearization(
        flat, _envelope("woofer", flat, excited_band_hz=(150.0, 4000.0)),
    )
    assert flat_fit.filters == ()
    assert flat_fit.correction_giveback_db == 0.0

    bumped = _driver_response("woofer", _bell(_NATIVE_FREQS_HZ, 900.0, 8.0, 0.15))
    woofer_fit = fit_driver_linearization(
        bumped, _envelope("woofer", bumped, excited_band_hz=(150.0, 4000.0)),
    )
    assert woofer_fit.filters
    assert woofer_fit.hf_continuation_spend_db == 0.0  # CD-horn stage never ran
    assert woofer_fit.correction_giveback_db > 0.0


def test_cd_horn_deep_deficit_is_bounded_by_the_single_shelf_cap():
    """The live JTS3 run-6 shape (~14 dB deficit): the single-shelf realization
    cap — NOT the ledger budget and NOT the per-filter cut cap — is what bounds
    the spend, and the stage still FIRES rather than suppressing.

    This is the regression for run 6, which suppressed with ``fit_quality``
    because the raised ledger budget let spend reach 14.33 where a single
    clamped Lowshelf plus bell residuals cannot track a real curve. An offline
    ladder on that capture put the realization cliff at ~11.9 dB spend; the cap
    keeps spend below it, so a deep deficit yields a REALIZED partial correction
    instead of nothing at all. The uncapped measurement stays disclosed."""
    fit = _cd_horn_fit("compression_horn", anchor_db=_CD_HORN_LIVE_ANCHOR_DB)
    assert fit.hf_continuation_suppressed_reason == ""  # fires, unlike live run 6
    assert fit.hf_continuation_spend_db == pytest.approx(HF_SINGLE_SHELF_SPEND_CAP_DB)
    # The cap binds BELOW both other ceilings — that is the whole point.
    assert HF_SINGLE_SHELF_SPEND_CAP_DB < PER_FILTER_CUT_CAP_DB
    assert HF_SINGLE_SHELF_SPEND_CAP_DB < MAX_NORMALIZATION_SPEND_DB
    # Partial correction is visible: the measured deficit exceeds what was spent.
    assert fit.measured_deficit_at_ceiling_db > fit.hf_continuation_spend_db + 2.0
    # Hard per-filter invariant still holds for every emitted filter.
    assert all(f.gain >= -PER_FILTER_CUT_CAP_DB - 1e-6 for f in fit.filters)
    assert fit.filters[0].biquad_type == "Lowshelf"
    assert fit.filters[0].gain == pytest.approx(-HF_SINGLE_SHELF_SPEND_CAP_DB)
    assert all(f.gain <= 0.0 for f in fit.filters)  # cut-only
    assert len(fit.filters) <= MAX_FILTERS_PER_DRIVER
    assert fit.correction_giveback_db == pytest.approx(
        fit.hf_continuation_spend_db, abs=1.0
    )


def test_single_shelf_cap_binds_in_the_spend_min_chain():
    """The cap is one of three independent ceilings in the spend min-chain
    (measured deficit, remaining ledger budget, single-shelf realization limit).
    A deficit far above all of them must land exactly on the smallest — the
    realization cap — proving it is wired into the chain rather than merely
    documented."""
    deep = np.array([0.0, 0.0, 0.0, -4.5, -10.5, -15.5, -20.0, -22.0, -23.0])
    fit = _cd_horn_fit("compression_horn", anchor_db=deep)
    assert fit.hf_continuation_spend_db == pytest.approx(HF_SINGLE_SHELF_SPEND_CAP_DB)
    assert fit.measured_deficit_at_ceiling_db > HF_SINGLE_SHELF_SPEND_CAP_DB


def test_cd_horn_disagreeing_repeats_suppress_the_stage():
    """The agreement gate: an occurrence (primary or a repeat) that disagrees
    from the others by more than the [10 kHz, ceiling] limit (2 dB) suppresses
    the stage entirely — no filters, a named reason, everything else zeroed.
    The spread is over (primary, *repeats), so a lone disagreeing repeat is
    caught even though the other two occurrences agree."""
    mag = _cd_horn_db(_NATIVE_FREQS_HZ)
    disagree = mag + 3.0 * _bell(_NATIVE_FREQS_HZ, 12000.0, 1.0, 0.15)  # +3 dB @12k on one repeat
    fit = _cd_horn_fit("compression_horn", repeat_mags=[mag, disagree])
    assert fit.filters == ()
    assert fit.hf_continuation_suppressed_reason == "repeat_disagreement"
    assert fit.hf_continuation_spend_db == 0.0
    assert fit.measured_deficit_at_ceiling_db == 0.0


def test_cd_horn_primary_outlier_is_caught_by_the_agreement_gate():
    """Review SF-2: the spread MUST include the PRIMARY, not just the repeats.
    A primary carrying a −12 dB top-octave artifact while two repeats agree at
    −5 must SUPPRESS — otherwise the stage would size a ~7 dB-too-hot lift from
    the one bad sweep the fit is built on. A repeats-only gate (spread over the
    two agreeing repeats = 0) would have let it through."""
    primary_mag = _cd_horn_db(
        _NATIVE_FREQS_HZ,
        np.array([0.0, 0.0, 0.0, -2.5, -6.0, -9.0, -12.0, -13.0, -13.5]),
    )
    repeat_mag = _cd_horn_db(
        _NATIVE_FREQS_HZ,
        np.array([0.0, 0.0, 0.0, -1.0, -2.5, -4.0, -5.0, -5.5, -6.0]),
    )
    resp = _tweeter_response(primary_mag, repeat_mags=[repeat_mag, repeat_mag])
    envelope = compose_envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class="compression_horn",
    )
    fit = fit_driver_linearization(resp, envelope)
    assert fit.filters == ()
    assert fit.hf_continuation_suppressed_reason == "repeat_disagreement"
    assert fit.hf_continuation_spend_db == 0.0


def test_cd_horn_insufficient_occurrences_suppress_the_stage():
    """Fewer than 3 total occurrences (primary + repeats) is no reproducibility
    evidence (the N>=3-total paired gate) — suppressed with its own reason,
    distinct from disagreement. One repeat = 2 occurrences < 3."""
    mag = _cd_horn_db(_NATIVE_FREQS_HZ)
    fit = _cd_horn_fit("compression_horn", repeat_mags=[mag])  # 1 repeat -> 2 occurrences
    assert fit.filters == ()
    assert fit.hf_continuation_suppressed_reason == "insufficient_repeats"


def test_cd_horn_no_filter_budget_suppression_when_slots_exhausted(monkeypatch):
    """Review N-2: an ELIGIBLE driver whose flattening loop already spent every
    filter slot gets a NAMED suppression ("no_filter_budget"), not a silent
    inert — the dropped CD-horn lift stays observable. Forced by making the
    (flattening) peaking loop fill all MAX_FILTERS_PER_DRIVER slots, so the
    stage arrives already at capacity."""
    import jasper.active_speaker.linearization_fit as fit_mod
    from jasper.correction.peq import PEQ
    eight = [
        PEQ(freq=1000.0 + 100.0 * i, q=2.0, gain=-1.0)
        for i in range(MAX_FILTERS_PER_DRIVER)
    ]
    monkeypatch.setattr(fit_mod, "design_peq", lambda *a, **k: eight)
    fit = _cd_horn_fit("compression_horn")
    assert len(fit.filters) == MAX_FILTERS_PER_DRIVER  # flattening filled every slot
    assert all(f.biquad_type == "Peaking" for f in fit.filters)  # no CD-horn shelf added
    assert fit.hf_continuation_suppressed_reason == "no_filter_budget"


def test_cd_horn_suppression_reason_vocabulary_is_closed():
    """Review N-2: pin the closed set of suppression reasons so a new
    suppression path can't ship an un-enumerated reason string."""
    assert HF_SUPPRESSION_REASONS == {
        "insufficient_repeats",
        "repeat_disagreement",
        "fit_quality",
        "no_filter_budget",
    }


def test_cd_horn_fit_quality_suppresses_when_realization_cannot_track(monkeypatch):
    """If the cut-domain realization cannot track cut_target within tolerance
    (here forced by starving the peaking residual fit, leaving the Lowshelf
    alone unable to match the corner), the WHOLE stage is suppressed rather
    than ship a mis-shaped correction."""
    import jasper.active_speaker.linearization_fit as fit_mod
    monkeypatch.setattr(fit_mod, "design_peq", lambda *a, **k: [])
    fit = _cd_horn_fit("compression_horn")
    assert fit.filters == ()
    assert fit.hf_continuation_suppressed_reason == "fit_quality"
    assert fit.hf_continuation_spend_db == 0.0


def test_cd_horn_sizing_is_class_blind():
    """Sizing comes from measurement, not the driver class: the same curve
    under two DIFFERENT hold-policy classes produces identical spend and
    identical filters (their continuation policy is the same, so nothing
    differs) — the class's only authority is the above-ceiling continuation."""
    a = _cd_horn_fit("compression_horn")
    b = _cd_horn_fit("soft_dome")
    assert a.hf_continuation_spend_db == pytest.approx(b.hf_continuation_spend_db)
    assert [f.to_dict() for f in a.filters] == [f.to_dict() for f in b.filters]


def test_cd_horn_taper_policy_appends_trailing_highshelf_cut():
    """metal_dome / unknown are "taper" classes: above the ceiling they append
    one TRAILING Highshelf CUT (freq = ceiling*1.25, gain = -min(spend/2,
    HF_TAPER_MAX_DB)) that walks the lift back down. compression_horn (hold)
    appends nothing — its filters end on a Peaking."""
    for taper_class in ("metal_dome", "unknown"):
        fit = _cd_horn_fit(taper_class)
        assert fit.hf_continuation_policy == "taper"
        assert fit.filters[0].biquad_type == "Lowshelf"
        taper = fit.filters[-1]
        assert taper.biquad_type == "Highshelf"
        assert taper.gain <= 0.0  # a CUT, not a boost
        assert taper.freq == pytest.approx(fit.hf_continuation_ceiling_hz * 1.25)
        assert taper.gain == pytest.approx(
            -min(fit.hf_continuation_spend_db / 2.0, HF_TAPER_MAX_DB)
        )

    hold_fit = _cd_horn_fit("compression_horn")
    assert hold_fit.hf_continuation_policy == "hold"
    assert all(f.biquad_type != "Highshelf" for f in hold_fit.filters)


def test_cd_horn_continuation_policy_covers_every_driver_class():
    """The policy table must key EXACTLY on DRIVER_CLASSES — no declared class
    can fall through to an undefined continuation policy."""
    assert set(HF_CONTINUATION_POLICY) == set(DRIVER_CLASSES)
    assert set(HF_CONTINUATION_POLICY.values()) == {"hold", "taper"}


def test_cd_horn_mutually_exclusive_with_rising_highshelf_shelf():
    """A monotonic RISING response fires the rising-slope Highshelf shelf and
    must NOT also fire the CD-horn stage — the two are mutually exclusive (a
    genuinely rising response has no falling top-octave deficit to compensate,
    and the eligibility check refuses to run alongside a rising Highshelf)."""
    rise_lo, rise_hi, rise_db = 2000.0, 16000.0, 12.0
    frac = np.log2(np.clip(_NATIVE_FREQS_HZ, rise_lo, rise_hi) / rise_lo) / np.log2(rise_hi / rise_lo)
    db = np.where(
        _NATIVE_FREQS_HZ < rise_lo, 0.0,
        np.where(_NATIVE_FREQS_HZ > rise_hi, rise_db, rise_db * frac),
    )
    resp = _tweeter_response(db)
    envelope = compose_envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class="compression_horn",
    )
    fit = fit_driver_linearization(resp, envelope)
    assert any(f.biquad_type == "Highshelf" for f in fit.filters)  # rising shelf fired
    assert fit.hf_continuation_spend_db == 0.0  # CD-horn stage stayed out
    assert fit.hf_continuation_suppressed_reason == ""


def test_cd_horn_woofer_shape_is_ineligible_and_byte_stable():
    """A woofer whose fit band never reaches the confidence ceiling is
    ineligible — the CD-horn fields stay at their zeroed defaults, so the
    result serializes exactly as it did before this stage existed."""
    db = _bell(_NATIVE_FREQS_HZ, 900.0, 8.0, 0.15)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    fit = fit_driver_linearization(resp, envelope)
    assert fit.hf_continuation_spend_db == 0.0
    assert fit.hf_continuation_ceiling_hz == 0.0
    assert fit.hf_continuation_policy == ""
    assert fit.hf_continuation_suppressed_reason == ""
    assert fit.measured_deficit_at_ceiling_db == 0.0
    # The woofer still gets its ordinary peaking correction, unaffected.
    assert fit.filters
    assert all(f.biquad_type == "Peaking" for f in fit.filters)


def test_cd_horn_residual_and_observe_are_in_the_give_back_frame():
    """When the stage fires, residual/verify/observe are computed against the
    give-back frame (target - spend), not the original median. Proof: the
    trusted-band octave summaries sit near 0 (flat in the give-back frame),
    not near -spend (which is what the ORIGINAL frame would report after the
    whole band was cut by spend)."""
    fit = _cd_horn_fit("compression_horn")
    assert fit.hf_continuation_spend_db > 5.0
    # In the give-back frame the trusted band reads ~flat; in the original
    # frame it would read ~-spend (< -5). This distinguishes the two frames.
    assert abs(fit.observe_octave_summary["4000"]) < 3.0
    assert fit.residual_max_db < 3.0
    # target_level_db FIELD still reports the ORIGINAL median (0 for a flat core).
    assert fit.target_level_db == pytest.approx(0.0, abs=0.5)


def test_cd_horn_reason_override_above_ceiling_is_beyond_confidence():
    """Octave centers ABOVE the confidence ceiling are disclosed as
    beyond-measurement-confidence when the stage fired; centers at/below the
    ceiling keep their ordinary envelope reason."""
    fit = _cd_horn_fit("compression_horn")
    assert fit.hf_continuation_spend_db > 0.0
    # 20 kHz is above the ~16.4 kHz reference-tier ceiling.
    assert fit.reason_summary["20000"] == ReasonCode.BEYOND_MEASUREMENT_CONFIDENCE.value
    # 8 kHz is below the ceiling -> keeps its ordinary (non-override) reason.
    assert fit.reason_summary["8000"] != ReasonCode.BEYOND_MEASUREMENT_CONFIDENCE.value


def test_cd_horn_stage_is_role_agnostic():
    """The stage keys on the fit band reaching the ceiling, not on any role
    vocabulary — an arbitrary role string still fires it."""
    fit = _cd_horn_fit("compression_horn", role="supertweeter")
    assert fit.role == "supertweeter"
    assert fit.hf_continuation_spend_db > 0.0
    assert fit.filters[0].biquad_type == "Lowshelf"


# --------------------------------------------------------------------------- #
# PR-L5 — the allowed vocabulary, the shared level frame, and the lift stage
# --------------------------------------------------------------------------- #


def _dip_response(depth_db: float = 6.0, center_hz: float = 1500.0, width=0.4):
    """A driver with a real DIP — the shape a cut-only fit can never repair."""
    db = -_bell(_NATIVE_FREQS_HZ, center_hz, depth_db, width)
    resp = _driver_response("woofer", db)
    return resp, _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))


def test_the_default_vocabulary_is_cut_only():
    """Every caller that does not explicitly ask for boost gets the fit it got
    before boost existed. The default is the SAFE value, not the new one."""
    assert CUT_ONLY_VOCABULARY.allow_boost is False
    resp, envelope = _dip_response()
    default = fit_driver_linearization(resp, envelope)
    explicit = fit_driver_linearization(resp, envelope, vocabulary=CUT_ONLY_VOCABULARY)
    assert default.to_dict() == explicit.to_dict()
    assert all(f.gain <= 0.0 for f in default.filters)
    assert default.headroom_cost_db == 0.0


def test_a_cut_only_vocabulary_still_raises_on_a_boost():
    """The hardware-bound invariant survives PR-L5 unchanged for a cut-only
    vocabulary — an explicit raise, not a bare assert, so ``python -O`` cannot
    strip it."""
    import jasper.active_speaker.linearization_fit as fit_mod
    from jasper.correction.peq import PEQ

    resp, envelope = _dip_response()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            fit_mod, "design_peq",
            lambda *a, **k: [PEQ(freq=1000.0, q=2.0, gain=+3.0)],
        )
        with pytest.raises(RuntimeError, match="cut-only vocabulary"):
            fit_driver_linearization(resp, envelope)


def test_a_boost_vocabulary_fills_a_dip_a_cut_only_fit_cannot():
    """The doctrine amendment, end to end: "raise the tweeter" stops being
    unrepresentable."""
    resp, envelope = _dip_response(depth_db=6.0)
    cut_only = fit_driver_linearization(resp, envelope)
    boosted = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
    )
    assert cut_only.filters == ()
    assert any(f.gain > 0.0 for f in boosted.filters)
    # The dip is materially repaired, and the residual proves it.
    assert boosted.residual_max_db < cut_only.residual_max_db - 3.0


def test_total_boost_is_uncapped_but_one_filter_is_not():
    """The owner's ruling, precisely: arbitrary caps on the CORRECTION go, the
    per-filter realization bound stays. A gain past that bound raises rather
    than being silently clamped — same posture as the cut side."""
    import jasper.active_speaker.linearization_fit as fit_mod
    from jasper.correction.peq import PEQ

    resp, envelope = _dip_response()
    vocab = FitVocabulary(allow_boost=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            fit_mod, "design_peq",
            lambda *a, **k: [
                PEQ(freq=1500.0, q=2.0, gain=PER_FILTER_BOOST_CAP_DB + 1.0)
            ],
        )
        with pytest.raises(RuntimeError, match="per-filter boost cap"):
            fit_driver_linearization(resp, envelope, vocabulary=vocab)


def test_boost_cannot_fill_an_envelope_excluded_band():
    """Null-exclusion stays a measured fact. The lift is clamped per bin by the
    SAME ``allowed_depth_db`` the cut side honours, and that array is already
    zero wherever the interference-null registry or the position screen
    excluded a band — so this needs no separate null logic and cannot drift
    from the cut side's."""
    resp, _ = _dip_response(depth_db=8.0)
    blocked = compose_envelope(
        "woofer", resp, excited_band_hz=(150.0, 4000.0),
        mic_tier="reference", driver_class="unknown",
        excluded_bands_hz=((900.0, 2600.0),),
    )
    in_null = (blocked.freqs_hz >= 900.0) & (blocked.freqs_hz <= 2600.0)
    assert not np.any(blocked.allowed_depth_db[in_null] > 0.05)
    fit = fit_driver_linearization(
        resp, blocked, vocabulary=FitVocabulary(allow_boost=True),
    )
    assert all(f.gain <= 0.0 for f in fit.filters)
    assert fit.headroom_cost_db == 0.0


def test_reduce_cuts_for_lift_shrinks_a_cut_instead_of_stacking_a_boost():
    """The first-class operation. Given a wanted lift where one of our own
    filters cuts, the cut shrinks — no slot spent, no headroom spent."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    cut = LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=-6.0)
    wanted = np.full_like(grid, 2.0)
    reduced, delivered = reduce_cuts_for_lift(
        (cut,), wanted, np.full_like(grid, 2.0), grid,
    )
    assert len(reduced) == 1
    assert reduced[0].gain > cut.gain           # the cut shrank
    assert reduced[0].gain < 0.0                # …but is still a cut
    assert float(np.max(delivered)) == pytest.approx(2.0, abs=0.1)
    # Nowhere did it deliver more lift than was permitted (up to the
    # materiality slack the permitted test itself carries).
    assert float(np.max(delivered)) <= 2.0 + _CUT_REDUCTION_EPS_DB


def test_reduce_cuts_refuses_where_the_headroom_forbids_it():
    """The safety constraint, and the reason a cut placed to tame a peak is
    never unwound to fill an unrelated dip: raising that bin would simply bring
    the peak back."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    cut = LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=-6.0)
    reduced, delivered = reduce_cuts_for_lift(
        (cut,), np.full_like(grid, 4.0), np.zeros_like(grid), grid,
    )
    assert reduced == (cut,)
    assert float(np.max(delivered)) == 0.0


def test_reduce_cuts_leaves_non_cutting_filters_alone():
    grid = DEFAULT_ENVELOPE_GRID_HZ
    boost = LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=+3.0)
    reduced, delivered = reduce_cuts_for_lift(
        (boost,), np.full_like(grid, 2.0), np.full_like(grid, 2.0), grid,
    )
    assert reduced == (boost,)
    assert float(np.max(delivered)) == 0.0


def test_reduce_cuts_uses_the_real_evaluator_not_a_linear_gain_model():
    """A shelf's delivered lift is NOT linear in its gain, and assuming it is
    errs in the unsafe direction. Pinned by comparing the reported delivery
    against the same complex evaluator the emitter's graph realizes."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    shelf = LinearizationFilter(
        biquad_type="Lowshelf", freq=4000.0, q=_HIGHSHELF_Q, gain=-9.0,
    )
    reduced, delivered = reduce_cuts_for_lift(
        (shelf,), np.full_like(grid, 3.0), np.full_like(grid, 3.0), grid,
    )
    before = 20.0 * np.log10(np.abs(complex_correction_response((shelf,), grid)))
    after = 20.0 * np.log10(np.abs(complex_correction_response(reduced, grid)))
    assert np.allclose(delivered, np.maximum(after - before, 0.0), atol=1e-9)


def test_the_lift_stage_is_inert_under_a_cut_only_vocabulary():
    """Not a formality: a cuts-only flattening loop leaves the whole curve at
    or BELOW its target, so every dip is a "deficit" by this stage's
    arithmetic. Chasing it under a vocabulary that never intended to add level
    would silently change what every pre-PR-L5 caller gets."""
    resp, envelope = _dip_response()
    fit = fit_driver_linearization(resp, envelope)
    assert fit.lift_requested_db == 0.0
    assert fit.lift_from_reduced_cuts_db == 0.0
    assert fit.lift_from_boost_db == 0.0
    assert fit.lift_suppressed_reason == ""


def test_every_lift_suppression_reason_the_stage_returns_is_enumerated():
    """The HF stage's own contract, applied to the lift stage: a new
    suppression path cannot ship an un-enumerated reason string."""
    import inspect
    import jasper.active_speaker.linearization_fit as fit_mod

    source = inspect.getsource(fit_mod._lift_stage)
    # The reason is the LAST argument of every ``_Lift(...)`` the stage
    # returns, so read those calls rather than every string in the function.
    calls = re.findall(r"_Lift\((?:[^()]|\([^()]*\))*\)", source)
    assert calls, "the lift stage must return _Lift results"
    emitted = set()
    for call in calls:
        literals = re.findall(r'"([a-z_]*)"', call)
        if literals:
            emitted.add(literals[-1])
    assert emitted - {""} <= LIFT_SUPPRESSION_REASONS
    # …and every enumerated reason is actually reachable from the stage.
    assert LIFT_SUPPRESSION_REASONS <= emitted


def test_the_lift_stage_does_not_unwind_the_cd_horn_give_back():
    """The CD-horn stage's Lowshelf is a DELIBERATE level move whose give-back
    the trim returns — not a cut the lift stage may reclaim. The permitted-
    headroom constraint protects it with no special case: after that stage the
    sub-onset band already sits at the give-back frame's target, so there is no
    headroom there to spend."""
    mag = _cd_horn_db(_NATIVE_FREQS_HZ, _CD_HORN_ANCHOR_DB)
    resp = _tweeter_response(mag, role="tweeter")
    envelope = compose_envelope(
        "tweeter", resp, excited_band_hz=(2000.0, 20000.0),
        mic_tier="reference", driver_class="compression_horn",
    )
    cut_only = fit_driver_linearization(resp, envelope)
    boosted = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
    )
    assert cut_only.filters == boosted.filters
    assert boosted.hf_continuation_spend_db == cut_only.hf_continuation_spend_db
    assert boosted.correction_giveback_db == pytest.approx(
        cut_only.correction_giveback_db
    )


# --- the shared level frame ------------------------------------------------


def test_the_shared_frame_puts_every_role_at_one_system_level():
    """The structural close of PR-L4 item 1: ``target + trim`` is the SAME
    number for every role, by construction rather than by later comparison."""
    trims = {"woofer": 0.0, "tweeter": -24.74}
    frame = solve_shared_level_frame({"woofer": -21.13, "tweeter": -7.24}, trims)
    for role in ("woofer", "tweeter"):
        assert frame.target_level_db[role] + trims[role] == pytest.approx(
            frame.system_level_db
        )


def test_the_shared_frame_reproduces_the_2026_07_27_frame_error():
    """Built from the incident's own numbers — the tweeter fitted at −7.24 dB
    behind a −24.74 dB trim, against a woofer at −21.13 dB with no trim. The
    frame reports 10.85 dB of disagreement, which is the 10.9 dB PR-L3
    independently measured as the trim's own frame error on the same capture.
    Two instruments, one number: that is what makes the offset trustworthy
    enough to gate on."""
    frame = solve_shared_level_frame(
        {"woofer": -21.13, "tweeter": -7.24},
        {"woofer": 0.0, "tweeter": -24.74},
    )
    assert frame.reference_role == "woofer"
    assert frame.offset_db["woofer"] == pytest.approx(0.0)
    assert frame.offset_db["tweeter"] == pytest.approx(10.85, abs=0.01)


def test_the_shared_frame_never_cuts_a_driver_merely_to_match_another():
    """The loudest proposal is the reference, so the offset is never negative:
    only the driver that is actually out of place moves, and it moves UP. The
    quietest reference would spend real max SPL to reach a level nothing
    wanted."""
    frame = solve_shared_level_frame({"a": -10.0, "b": -20.0, "c": -14.0}, {})
    assert frame.reference_role == "a"
    assert all(v >= 0.0 for v in frame.offset_db.values())
    assert frame.offset_db["b"] == pytest.approx(10.0)


def test_the_shared_frame_is_topology_agnostic():
    """One role (a passive box's summed chain) is the trivial frame; three is
    the same code. No way-count, no crossover, no role vocabulary."""
    one = solve_shared_level_frame({"summed": -12.0}, {})
    assert one.system_level_db == pytest.approx(-12.0)
    assert one.offset_db["summed"] == pytest.approx(0.0)
    three = solve_shared_level_frame(
        {"woofer": -20.0, "midrange": -18.0, "tweeter": -22.0}, {},
    )
    assert set(three.offset_db) == {"woofer", "midrange", "tweeter"}
    assert three.reference_role == "midrange"


def test_an_empty_frame_raises_rather_than_being_invented():
    with pytest.raises(ValueError, match="at least one role"):
        solve_shared_level_frame({}, {})


def test_driver_core_level_matches_the_fits_own_target_exactly():
    """Not a second estimator: with no radiating band supplied, the frame's
    level and the fit's target are the same resample → smooth → core-mask →
    median chain, to the bit.

    #1929 gave the frame a band the fit's target deliberately does not take,
    so the two coincide only on this call shape — which is every caller that
    does not pass one, and is what makes the default provably non-breaking.
    The divergence when a band IS passed is pinned below.
    """
    resp, envelope = _dip_response()
    fit = fit_driver_linearization(resp, envelope)
    assert driver_core_level_db(resp, envelope) == pytest.approx(
        fit.target_level_db
    )


def test_an_unmeasurable_driver_is_left_out_of_the_frame_not_defaulted():
    """``None``, never 0.0. A driver whose envelope allows correction nowhere
    has an UNKNOWN level, and a placeholder would let one unmeasurable driver
    move every other one."""
    resp, envelope = _dip_response()
    empty = compose_envelope(
        "woofer", resp, excited_band_hz=(150.0, 4000.0),
        mic_tier="reference", driver_class="unknown",
        excluded_bands_hz=((0.0, 30000.0),),
    )
    assert driver_core_level_db(resp, empty) is None
    assert driver_core_level_db(resp, envelope) is not None


def test_the_frame_offset_is_reported_per_role_on_the_fit():
    resp, envelope = _dip_response()
    frame = solve_shared_level_frame({"woofer": -3.0, "tweeter": 0.0}, {})
    fit = fit_driver_linearization(resp, envelope, level_frame=frame)
    assert fit.level_frame_offset_db == pytest.approx(3.0)
    assert fit.to_dict()["level_frame_offset_db"] == pytest.approx(3.0)
    # No frame supplied -> 0.0, and the fit is otherwise unchanged.
    assert fit_driver_linearization(resp, envelope).level_frame_offset_db == 0.0


def test_reduce_cuts_survives_a_production_shaped_headroom_array():
    """**N2 regression.** The permitted-headroom test is per-bin over the WHOLE
    grid, and a biquad's response never reaches exactly zero — a cut at 1 kHz
    still leaks thousandths of a dB at 20 kHz. At a float-noise epsilon a
    single such bin, with fractionally negative permitted headroom, vetoed the
    entire shrink: measured 0.00 of a wanted 4.00 dB. With a materiality
    epsilon the operation delivers.

    The headroom array here is production-shaped — mostly positive, with a
    sliver of NEGATIVE residual at the extremes, which is exactly what
    ``target − working`` looks like after a real flattening loop.
    """
    grid = DEFAULT_ENVELOPE_GRID_HZ
    cut = LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=-6.0)
    headroom = np.full_like(grid, 4.0)
    headroom[grid > 15_000.0] = -0.0034   # the measured leakage-scale residual
    reduced, delivered = reduce_cuts_for_lift(
        (cut,), np.full_like(grid, 4.0), headroom, grid,
    )
    assert float(np.max(delivered)) > 1.0, "a sliver of residual must not veto"
    assert reduced[0].gain > cut.gain


def test_reduce_cuts_still_refuses_a_material_overshoot():
    """The materiality epsilon must not become a licence: a genuinely negative
    headroom (an order above the epsilon) still forbids the shrink."""
    grid = DEFAULT_ENVELOPE_GRID_HZ
    cut = LinearizationFilter(biquad_type="Peaking", freq=1000.0, q=2.0, gain=-6.0)
    headroom = np.full_like(grid, 4.0)
    headroom[np.argmin(np.abs(grid - 1000.0))] = -1.0
    reduced, delivered = reduce_cuts_for_lift(
        (cut,), np.full_like(grid, 4.0), headroom, grid,
    )
    assert reduced == (cut,)
    assert float(np.max(delivered)) == 0.0


def test_the_cut_reduction_epsilon_is_the_modules_own_materiality_floor():
    """One definition of "this per-bin dB figure is noise, not an allowance"
    across the envelope and the lift stage."""
    import jasper.active_speaker.linearization_fit as fit_mod

    assert _CUT_REDUCTION_EPS_DB == fit_mod._ENVELOPE_NONZERO_EPS_DB
    # Comfortably above real biquad leakage, comfortably below the smallest
    # gain this module will emit — so it can mask neither a real overshoot nor
    # a real filter.
    assert 0.0034 < _CUT_REDUCTION_EPS_DB < _MIN_FILTER_GAIN_DB


def _two_dip_response(depth_db: float = 3.0):
    """A driver with TWO separated dips — the shape whose fit makes the two
    candidate headroom contracts diverge (two bells at different centres never
    reach their combined height anywhere). Measured on this fixture: two boosts
    of +2.90 and +2.68 dB, sum 5.58, realized cascade peak 2.91."""
    db = -(
        depth_db * np.exp(-0.5 * ((np.log2(_NATIVE_FREQS_HZ / 700.0) / 0.25) ** 2))
        + depth_db * np.exp(-0.5 * ((np.log2(_NATIVE_FREQS_HZ / 5000.0) / 0.25) ** 2))
    )
    resp = _driver_response("woofer", db)
    return resp, _envelope("woofer", resp, excited_band_hz=(150.0, 8000.0))


def test_headroom_cost_is_the_realized_peak_the_emitter_charges_not_the_sum():
    """**S2, re-pointed by #1808.** The disclosed number and the charged
    number are one number — and that number is now the branch chain's
    REALIZED PEAK, not the sum of the fit's positive gains.

    Still pinned on the two-separated-boosts fixture, because that is where
    the two candidate contracts DIVERGE (a single-boost fit makes sum and peak
    trivially equal, so it would pass against either implementation and
    regress nothing — adversarial review SF2). What changed is which side the
    contract is on.

    The sum was defended here as the conservative bound, since overlapping
    boosts at one frequency DO add. They still do — and a bin-by-bin cascade
    evaluation contains that case exactly, which is why the peak is not a
    weakening. What the sum ALSO charged for was boosts that never meet, and
    boosts the branch's own crossover deletes: on the 2026-07-28 JTS3 profile
    that was 22.458 dB charged against a +4.00 dB realized branch peak, and a
    speaker 8.3 dB below its household's listening level at full volume
    (#1808).

    The fit core no longer computes the field at all — a correction's cost
    depends on the crossover and trim it is emitted into, which the
    topology-agnostic core does not know — so this asserts the contract at the
    seam that DOES know: ``branch_chain``, which the composer stamps with and
    the emitter charges with.
    """
    from jasper.active_speaker.branch_chain import (
        HEADROOM_MARGIN_DB, CrossoverSection, branch_headroom_db,
    )
    from jasper.active_speaker.camilla_yaml import linearization_headroom_db

    resp, envelope = _two_dip_response()
    fit = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
    )
    emitted = [f.to_dict() for f in fit.filters]
    boosts = [f.gain for f in fit.filters if f.gain > 0.0]
    assert len(boosts) >= 2, "the contracts only diverge with separated boosts"
    cascade_peak_db = float(np.max(20.0 * np.log10(
        np.abs(complex_correction_response(fit.filters, envelope.freqs_hz))
    )))
    # The fixture really does separate the two contracts…
    assert sum(boosts) > cascade_peak_db + 1.0, (sum(boosts), cascade_peak_db)

    # …and the charge follows the PEAK (plus the named margin), not the sum.
    # (The divergence guard above is what makes this an assertion about WHICH
    # contract holds, so no separate `charge < sum` restatement is needed.)
    charge_db = branch_headroom_db(emitted)
    assert charge_db == pytest.approx(cascade_peak_db + HEADROOM_MARGIN_DB, abs=0.05)

    # It is literally the emitter's own charge for this fit — one function,
    # two readers — and it survives the JSON round-trip a candidate takes.
    # (A branch with no crossover and no trim: this fit was not composed into
    # one, so the honest context for it is empty.)
    assert charge_db == pytest.approx(
        linearization_headroom_db({fit.role: emitted}, branch_context={})
    )
    assert fit.to_dict()["headroom_cost_db"] == fit.headroom_cost_db

    # The fit core leaves the field alone: no branch, no charge. The composer
    # stamps it (crossover_v2_flow._fit_linearization), which is what makes
    # the crossover and trim terms below expressible at all.
    assert fit.headroom_cost_db == 0.0

    # And the crossover the branch runs through is part of the charge: put
    # this fit behind a low-pass an octave under its boosts and the same
    # filters cost the speaker nothing at all.
    lowest_boost_hz = min(f.freq for f in fit.filters if f.gain > 0.0)
    assert branch_headroom_db(
        emitted,
        sections=(
            CrossoverSection(
                fc_hz=lowest_boost_hz / 4.0, order=4, highpass=False,
            ),
        ),
    ) == 0.0


def test_headroom_cost_is_zero_for_every_cut_only_fit():
    resp, envelope = _dip_response()
    assert fit_driver_linearization(resp, envelope).headroom_cost_db == 0.0


# --------------------------------------------------------------------------- #
# the fit band vs the driver's own crossover (#1809)
# --------------------------------------------------------------------------- #


def _crossed_over_woofer(fc_hz: float = 2000.0, order: int = 4):
    """The 2026-07-28 JTS3 woofer shape, reconstructed: a flat driver measured
    THROUGH its own LR4 low-pass, so its curve falls away above Fc for a reason
    that has nothing to do with the driver.

    That is the whole defect. The fit flattens toward a target read off the
    trusted core band, the crossover's rolloff reads as a driver deficit, and
    a boost-capable vocabulary "corrects" it — on the real profile with
    +11.6155 dB (Q 8.0) at 2747 Hz and +5.9619 dB at 2196 Hz, both inside the
    stopband, for a net acoustic contribution of +1.06 and -1.60 dB.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    sections = (CrossoverSection(fc_hz=fc_hz, order=order, highpass=False),)
    db = crossover_response_db(_NATIVE_FREQS_HZ, sections)
    resp = _driver_response("woofer", db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 8000.0))
    return resp, envelope, sections


def test_the_fit_places_no_boost_in_the_drivers_own_stopband():
    """**The #1809 keystone.** Reconstruct the shape that produced the
    2026-07-28 JTS3 profile — a woofer whose measured curve extends well above
    its own crossover — and the bounded fit must spend no gain up there.

    Asserted against the UNBOUNDED fit on the same response, so this cannot
    pass by the fixture simply having nothing to boost.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    resp, envelope, sections = _crossed_over_woofer()
    band = radiating_band_hz(sections)
    vocabulary = FitVocabulary(allow_boost=True)

    unbounded = fit_driver_linearization(resp, envelope, vocabulary=vocabulary)
    bounded = fit_driver_linearization(
        resp, envelope, vocabulary=vocabulary, radiating_band_hz=band,
    )

    # The defect reproduces: without the bound the fit boosts above Fc.
    stopband_boosts = [
        f for f in unbounded.filters if f.gain > 0.0 and f.freq > band[1]
    ]
    assert stopband_boosts, "the fixture must reproduce the defect to pin the fix"
    assert max(f.gain for f in stopband_boosts) > 4.0

    # …and the bound removes exactly that: no boost outside the radiating band.
    assert [f for f in bounded.filters if f.gain > 0.0 and f.freq > band[1]] == []


def test_the_bound_is_boost_only_cuts_still_reach_out_of_band_leakage():
    """The asymmetry, pinned. A cut past the handoff is ordinary useful work —
    whatever leaks through the crossover still reaches the summed response, and
    removing it spends no headroom (the charge is a peak, and a cut cannot
    raise one) — so the bound must not take the cut stages with it. (The
    conductor fixture proved the cost of getting this wrong: its tweeter's real
    +6 dB bump sits 100 Hz BELOW Fc, and refusing to cut it turned a passing
    correction into a refused one.)"""
    from jasper.active_speaker.branch_chain import radiating_band_hz

    fc_hz = 2000.0
    resp, envelope, sections = _crossed_over_woofer(fc_hz=fc_hz)
    sections_band = radiating_band_hz(sections)
    # A woofer with a real resonance just past its own handoff (1800 Hz, above
    # the 1602.9 Hz radiating edge), riding the same crossover-shaped rolloff.
    peak_db = resp.magnitude_db + _bell(_NATIVE_FREQS_HZ, 1800.0, 8.0, 0.2)
    resp = _driver_response("woofer", peak_db)
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 8000.0))

    fit = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
        radiating_band_hz=sections_band,
    )
    out_of_band = [f for f in fit.filters if f.freq > sections_band[1]]
    assert [f for f in out_of_band if f.gain > 0.0] == []
    assert [f for f in out_of_band if f.gain < 0.0], (
        "a resonance past the handoff must still be cut"
    )


def test_an_unbounded_fit_is_byte_identical_to_before_the_bound_existed():
    """Every caller that declares no crossover — a one-way box's summed chain,
    and every pre-#1809 caller — gets exactly the fit it got before."""
    resp, envelope, _sections = _crossed_over_woofer()
    vocabulary = FitVocabulary(allow_boost=True)
    assert fit_driver_linearization(
        resp, envelope, vocabulary=vocabulary,
    ).to_dict() == fit_driver_linearization(
        resp, envelope, vocabulary=vocabulary,
        radiating_band_hz=(0.0, float("inf")),
    ).to_dict()


def test_the_bound_does_not_move_the_target_level_or_the_give_back():
    """Scoped to the SHAPE question. ``target_level_db`` is the flat line every
    stage in this fit grades against, so it is read over the whole region the
    fit may place a filter on; ``correction_giveback_db`` anchors the trim and
    is a power-domain average over the same region. #1809's bound must move
    neither, and #1929 did not change that — it gave the FRAME's own median a
    band (:func:`driver_core_level_db`), which is a different question.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    resp, envelope, sections = _crossed_over_woofer()
    unbounded = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
    )
    bounded = fit_driver_linearization(
        resp, envelope, vocabulary=FitVocabulary(allow_boost=True),
        radiating_band_hz=radiating_band_hz(sections),
    )
    assert bounded.target_level_db == unbounded.target_level_db
    assert driver_core_level_db(resp, envelope) == unbounded.target_level_db


# --------------------------------------------------------------------------- #
# #1929 — the level frame's median runs over the RADIATING band
# --------------------------------------------------------------------------- #


def test_the_core_level_median_excludes_the_drivers_own_crossover_stopband():
    """**The #1929 keystone, woofer side.**

    A driver is measured THROUGH its own crossover, and the band it is swept
    over is a CAPTURE-COVERAGE declaration (``measurement_band_hz``) that
    routinely reaches past Fc — this fixture's woofer is declared to 8000 Hz
    against a 2000 Hz LR4, which puts 36% of its core-mask bins in its own
    stopband. The core level is a MEDIAN, in which a −40 dB stopband bin
    counts exactly as much as a passband one, so those bins push the median
    down the driver's own passband and the number stops describing where the
    driver sits.

    **The size of the error is the passband's own spread, not the skirt's
    depth**, which is why it is pinned here on two shapes rather than one. A
    perfectly flat driver barely moves (its passband has nothing to slide
    down); give it the gentle fall a real cone has toward its crossover and
    the same declaration costs 1.80 dB — the class of number #1809's comment
    measured at 1.66 dB on the conductor fixture, the JTS3 corpus pays at
    2.09 dB (``test_audio_measurement_program_analysis``), and #1870's field
    session was refused 3.395 dB of frame disagreement over.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    resp, envelope, sections = _crossed_over_woofer()
    band = radiating_band_hz(sections)

    # Flat driver: the mechanism is present and signed, and small.
    flat_wide = driver_core_level_db(resp, envelope)
    flat_radiating = driver_core_level_db(resp, envelope, radiating_band_hz=band)
    assert flat_radiating == pytest.approx(0.0, abs=0.1)
    assert 0.0 < flat_radiating - flat_wide < 0.6

    # ...and the same declaration on a driver with a real passband.
    tilt_db = np.clip(-1.5 * np.log2(_NATIVE_FREQS_HZ / 300.0), -8.0, 4.0)
    real = _driver_response("woofer", resp.magnitude_db + tilt_db)
    real_envelope = _envelope("woofer", real, excited_band_hz=(150.0, 8000.0))
    real_wide = driver_core_level_db(real, real_envelope)
    real_radiating = driver_core_level_db(
        real, real_envelope, radiating_band_hz=band,
    )
    assert real_radiating - real_wide == pytest.approx(1.80, abs=0.15)


def test_the_core_level_band_mirrors_onto_the_tweeter():
    """**The mirror case.** Nothing about #1929 is woofer-specific: a
    silk-dome declared from well below its crossover — the overwhelmingly
    common tweeter declaration — feeds its own HIGH-pass skirt into its median
    by exactly the same mechanism, reflected about Fc. The tweeter side is if
    anything worse, because a declaration reaching an octave below Fc puts
    over half the core mask in the stopband: 2.70 dB here on a driver that is
    otherwise perfectly flat.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db, radiating_band_hz,
    )

    sections = (CrossoverSection(fc_hz=2000.0, order=4, highpass=True),)
    resp = _driver_response(
        "tweeter", crossover_response_db(_NATIVE_FREQS_HZ, sections),
    )
    # Declared from an octave below Fc — the shape a real tweeter declaration
    # has, and the reason the mirror is not a hypothetical.
    envelope = _envelope("tweeter", resp, excited_band_hz=(1000.0, 20000.0))
    band = radiating_band_hz(sections)
    assert band[0] > 2000.0  # radiates ABOVE Fc, so the declared bottom is skirt

    whole_band = driver_core_level_db(resp, envelope)
    radiating = driver_core_level_db(resp, envelope, radiating_band_hz=band)
    assert radiating - whole_band == pytest.approx(2.70, abs=0.15)
    # The residue on the radiating side is the LR4 knee itself: the band's
    # own edge is 3 dB down by definition, so a flat tweeter reads a little
    # under unity there. That is the threshold's cost, not contamination.
    assert -1.0 < radiating < 0.0


def test_a_declared_band_inside_the_radiating_band_reads_bit_identical():
    """**The no-crossing regression.** #1929 must be invisible to a driver
    whose declared span never reaches its own handoff — the same float, not
    merely a close one, so a future change cannot quietly re-band a session
    the defect never touched.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz

    resp, envelope, sections = _crossed_over_woofer()
    band = radiating_band_hz(sections)
    inside = _envelope("woofer", resp, excited_band_hz=(150.0, band[1] * 0.75))

    assert driver_core_level_db(
        resp, inside, radiating_band_hz=band,
    ) == driver_core_level_db(resp, inside)


def test_an_inverted_radiating_band_falls_back_to_the_core_mask():
    """A three-way mid squeezed between two crossovers closer together than
    their own edges honestly has NO radiating band (``radiating_band_hz``
    returns ``lo > hi``). Its level is still measured — over the whole core
    mask, as before #1929 — rather than the role dropping out of the frame,
    because a frame with one role missing stops grading every other one.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db, radiating_band_hz,
    )

    squeezed = (
        CrossoverSection(fc_hz=2000.0, order=4, highpass=True),
        CrossoverSection(fc_hz=2200.0, order=4, highpass=False),
    )
    band = radiating_band_hz(squeezed)
    assert band[0] > band[1], "the fixture must have an empty band to mean anything"

    resp = _driver_response(
        "midrange", crossover_response_db(_NATIVE_FREQS_HZ, squeezed),
    )
    envelope = _envelope("midrange", resp, excited_band_hz=(500.0, 8000.0))
    level = driver_core_level_db(resp, envelope, radiating_band_hz=band)
    assert level is not None
    assert level == driver_core_level_db(resp, envelope)
    assert core_level_band_hz(envelope, radiating_band_hz=band) == core_level_band_hz(
        envelope
    )


def _sub_floor_tweeter(fc_hz: float, order: int = 2):
    """A flat tweeter behind a HIGH crossover, declared 300 Hz-20 kHz at
    reference tier with the production-default ``driver_class`` — the shape
    whose radiating band lands at or past its own core mask's top edge.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db, radiating_band_hz,
    )

    sections = (CrossoverSection(fc_hz=fc_hz, order=order, highpass=True),)
    resp = _driver_response(
        "tweeter", crossover_response_db(_NATIVE_FREQS_HZ, sections),
    )
    envelope = _envelope("tweeter", resp, excited_band_hz=(300.0, 20000.0))
    return resp, envelope, radiating_band_hz(sections)


def _sub_floor_woofer(fc_hz: float, validity_floor_hz: float, order: int = 4):
    """A flat woofer behind a LOW crossover whose response the room gate
    trusts only above ``validity_floor_hz`` — the shape whose radiating band
    lands at or below its own core mask's BOTTOM edge.

    The mirror of :func:`_sub_floor_tweeter`, and the reason the width floor
    has to be two-sided: a low-pass edge sits at ~0.80*Fc and slides DOWN as
    Fc falls, while a room gate raises the trusted floor to meet it, so the
    intersection runs out of room at the opposite end from a tweeter's.
    Reachable whenever ``Fc <= 1.57 * validity_floor_hz`` — an ordinary
    horn-in-a-room shape, not a contrived one.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db, radiating_band_hz,
    )

    sections = (CrossoverSection(fc_hz=fc_hz, order=order, highpass=False),)
    resp = _driver_response(
        "woofer", crossover_response_db(_NATIVE_FREQS_HZ, sections),
        validity_floor_hz=validity_floor_hz,
    )
    envelope = _envelope("woofer", resp, excited_band_hz=(150.0, 4000.0))
    return resp, envelope, radiating_band_hz(sections)


def _core_level_sweep(make, fcs) -> list[float]:
    """``driver_core_level_db`` across a sweep of crossover frequencies, on
    the production defaults, so a test can grade CONTINUITY rather than pick
    two points and hope."""
    levels = []
    for fc_hz in fcs:
        resp, envelope, band = make(float(fc_hz))
        level = driver_core_level_db(resp, envelope, radiating_band_hz=band)
        assert level is not None
        levels.append(level)
    return levels


def _worst_adjacent_step_db(levels: list[float]) -> float:
    return max(abs(b - a) for a, b in zip(levels, levels[1:]))


@pytest.mark.parametrize(
    "validity_floor_hz, fcs",
    [
        # Fc step is 2.5 Hz, not the tweeter's 20-25: a woofer's widened band
        # sits on its own low-pass shoulder, where the curve genuinely moves
        # ~0.015 dB per Hz of Fc, so the sampling interval has to be fine
        # enough to separate that SLOPE from a STEP. It is not a looser test —
        # the ceiling is the same 0.10 dB, and the refinement below proves the
        # variation is slope.
        (600.0, np.arange(800.0, 1121.0, 2.5)),
        (400.0, np.arange(540.0, 761.0, 2.5)),
    ],
)
def test_the_woofer_core_level_is_continuous_across_the_width_floor(
    validity_floor_hz, fcs,
):
    """**The floor is TWO-SIDED, and the woofer is why.**

    Widening downward is the tweeter's fix: its high-pass edge slides UP into
    the core mask's top, so there is always passband below to widen into. A
    woofer runs out at the other end — its low-pass edge sits at ~0.80*Fc and
    slides DOWN, while a room gate raises the trusted floor to meet it — so a
    downward-only widen is clamped by ``core &`` and silently returns the
    still-sub-floor intersection. The floor then never enforces at all in the
    one region it was written for. Measured with the downward-only version:
    a 600 Hz gate at Fc 760 and a 400 Hz gate at Fc 520 both read ONE-BIN
    medians, with 21-30 dB steps in the neighbourhood.

    So the deficit is made up UPWARD from the core's own bottom bin instead,
    spending at most a floor width of the woofer's own low-pass skirt — the
    same trade the tweeter already makes into its high-pass knee.

    This sweep runs entirely ABOVE the empty-intersection transition (which
    for these gates sits at Fc ≈ 757 and ≈ 512), so what it grades is the
    FLOOR boundary specifically: the point where the intersection stops being
    sub-floor and the widened band hands back over to it. Worst adjacent step
    measured 0.073 dB (600 Hz gate) and 0.066 dB (400 Hz gate).
    """
    levels = _core_level_sweep(
        lambda fc: _sub_floor_woofer(fc, validity_floor_hz), fcs,
    )
    worst = _worst_adjacent_step_db(levels)
    assert worst < 0.10, f"worst adjacent step {worst:.4f} dB"
    # The estimate stays on the radiating side throughout — never the
    # whole-mask number, which for these shapes is 25-35 dB down.
    assert all(level > -6.0 for level in levels)


def test_the_woofer_floor_variation_is_slope_not_a_step():
    """The discriminator behind the sweep above, made explicit: refine the Fc
    increment and a genuine discontinuity does NOT shrink, while smooth
    variation does, proportionally.

    Measured on the 600 Hz-gate sweep — worst adjacent step 0.292 dB at 20 Hz,
    0.149 at 10 Hz, 0.076 at 5 Hz — i.e. ~0.015 dB per Hz of Fc at every
    resolution. That is the low-pass shoulder moving under a band that is
    tracking it, not a band being swapped for another one.
    """
    coarse = _worst_adjacent_step_db(
        _core_level_sweep(
            lambda fc: _sub_floor_woofer(fc, 600.0), np.arange(800.0, 1121.0, 20.0),
        )
    )
    fine = _worst_adjacent_step_db(
        _core_level_sweep(
            lambda fc: _sub_floor_woofer(fc, 600.0), np.arange(800.0, 1121.0, 5.0),
        )
    )
    assert coarse == pytest.approx(0.292, abs=0.02)
    # Quartering the Fc step quarters the observed jump, within the bin
    # quantisation that eventually floors it. A step would not move at all.
    assert fine < coarse / 3.0


@pytest.mark.parametrize(
    "order, fcs, ceiling_db",
    [
        (2, np.arange(2700.0, 3101.0, 20.0), 0.10),
        (4, np.arange(3450.0, 3801.0, 25.0), 0.10),
    ],
)
def test_the_core_level_is_continuous_across_the_width_floor(order, fcs, ceiling_db):
    """**The floor WIDENS a too-narrow bound; it does not discard it — and this
    is the test that proves the difference.**

    As Fc rises, a tweeter's radiating band eats into its own core mask from
    below and the intersection narrows toward nothing. Somewhere in there it
    crosses :data:`_MIN_LEVEL_BAND_OCTAVES`. The first cut of #1929's floor
    treated that crossing as "unusable" and fell back to the whole core mask —
    which does not remove a discontinuity, it MOVES one, from the 0↔1-bin edge
    down to the 9↔10-bin edge at lower and much more ordinary Fc. Measured on
    exactly this fixture with that version: **LR2 2900→2920 Hz stepped 13.14 dB
    and LR4 3625→3650 stepped 33.84 dB**, into a gate whose whole tolerance is
    3.0 dB. A two-point test at 3750/3775 called that "the cliff is gone",
    because 3750/3775 is the one place the cliff had moved away FROM.

    Widening instead — anchor on the intersection's own top edge, extend down
    to exactly the floor width, stay inside the core mask — is continuous by
    construction: an exact no-op at the boundary width, a constant band below
    it, the shrinking intersection above it. Measured here, the worst adjacent
    step across the whole boundary region is **0.033 dB (LR2)** and
    **0.052 dB (LR4)**, so the ceiling asserted is 0.10 dB: comfortably above
    the measurement, two orders below the tolerance it feeds, and nowhere near
    loose enough to let either 13 dB or 34 dB back in.

    Sweeping is the point. A pair of points can only ever prove the property at
    the pair; the defect this replaces was invisible to exactly such a pair.
    """
    levels = _core_level_sweep(lambda fc: _sub_floor_tweeter(fc, order=order), fcs)
    worst = _worst_adjacent_step_db(levels)
    assert worst < ceiling_db, f"worst adjacent step {worst:.4f} dB"
    # ...and the level stays where the driver radiates rather than collapsing
    # to the contaminated whole-mask number (about −19 dB on this fixture).
    assert all(level > -6.0 for level in levels)


@pytest.mark.parametrize(
    "order, fcs, expected_step_db",
    [
        (2, np.arange(3650.0, 3901.0, 25.0), 15.82),
        (4, np.arange(4500.0, 4801.0, 25.0), 40.42),
    ],
)
def test_the_empty_intersection_step_is_the_one_residual_and_it_is_disclosed(
    order, fcs, expected_step_db,
):
    """**The discontinuity that remains, measured and owned rather than hidden.**

    Widening needs an intersection to anchor on. When the radiating band clears
    the core mask's top entirely there is none at all — not "too narrow" but
    *none* — and the level falls back to the whole core mask, which is the
    pre-#1929 answer. That transition really is a step: **15.82 dB at LR2 and
    40.42 dB at LR4** on this fixture. (Both are ~0.1-0.2 dB smaller than the
    downward-only version of the widen produced, because the two-sided rule
    snaps each widened edge OUTWARD to the first bin that truly reaches the
    floor, so the band it steps away from is one bin wider.)

    It is kept, deliberately, because the alternative is worse. There is no
    honest radiating-band estimate to widen toward — the driver's trusted
    region and the band it radiates in do not overlap — so any number produced
    there would be an invention. What #1929 owes such a session is not a
    fabricated level but the truth about which band produced the one it has,
    and that is what :func:`core_level_band_hz` reports and the refusal journal
    now carries.

    These configurations refused before #1929 and still refuse; the estimate is
    ~19 dB (LR2) from where the driver sits either way. Closing THAT is the
    comparator family's work, not this issue's.
    """
    levels = _core_level_sweep(lambda fc: _sub_floor_tweeter(fc, order=order), fcs)
    assert _worst_adjacent_step_db(levels) == pytest.approx(
        expected_step_db, abs=0.05
    )


def test_the_disclosed_band_is_the_one_the_median_actually_used():
    """#1929 observability, at the seam. Two things must be disclosable, and
    :func:`core_level_band_hz` is the one place that decides both because it
    shares :func:`_core_level_mask` with the level itself:

    * a sub-floor bound that was WIDENED — the span used is wider than the
      bound and still sits on the radiating side;
    * an EMPTY intersection that fell back — the span used is the whole core
      mask, and saying so is the difference between a reader reaching for the
      right lever and the wrong one.
    """
    # Widened: the bound was too narrow, so the span used starts BELOW it —
    # but nowhere near the bottom of the core mask.
    resp, envelope, band = _sub_floor_tweeter(3750.0)
    widened = core_level_band_hz(envelope, radiating_band_hz=band)
    whole_mask = core_level_band_hz(envelope)
    assert widened is not None and whole_mask is not None
    assert widened[0] < band[0]
    assert widened != whole_mask
    assert widened[0] > whole_mask[0] * 4.0
    assert math.log2(widened[1] / widened[0]) == pytest.approx(1 / 3, abs=0.05)

    # Empty: nothing to widen toward, so the whole mask is used — and reported.
    _r, empty_env, empty_band = _sub_floor_tweeter(3775.0)
    assert core_level_band_hz(
        empty_env, radiating_band_hz=empty_band,
    ) == core_level_band_hz(empty_env)

    # Ordinary tweeter: the bound is used as-is, and that is what is disclosed.
    ordinary_resp, ordinary_env, ordinary_band = _sub_floor_tweeter(2000.0, order=4)
    ordinary = core_level_band_hz(ordinary_env, radiating_band_hz=ordinary_band)
    assert ordinary is not None and ordinary[0] >= ordinary_band[0]
    assert ordinary != core_level_band_hz(ordinary_env)
    assert driver_core_level_db(
        ordinary_resp, ordinary_env, radiating_band_hz=ordinary_band,
    ) == pytest.approx(-0.585, abs=0.01)
