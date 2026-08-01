# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-position spread bounds designed room-correction depth (#1954).

Ground truth for every behavioural test here is a synthetic multi-position
fixture built from real bells run through the real designer: one mode every
seat agrees about, and one mode whose level swings across seats. The stable
mode must keep its correction; the unstable one must not get a deep one; and a
room where every seat agrees must design EXACTLY as it did before this cap
existed.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.correction import peq, spatial, strategy, target, variance_cap


STABLE_HZ = 45.0
UNSTABLE_HZ = 160.0


def _log_freqs(n: int = 480) -> np.ndarray:
    return np.geomspace(20.0, 20000.0, n)


def _fixture() -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
    """(freqs, spatial-mean, per-position curves).

    Every seat sees the same +8 dB mode at 45 Hz — sigma there is exactly zero.
    The 160 Hz mode is the textbook unstable one: strong at two seats (+20 and
    +15 dB) and cancelled at the third (-10 dB), so its cross-position sigma is
    ~13 dB — comfortably past the tolerance — while the spatial mean still
    shows a +8.4 dB peak the uncapped designer would invert in full.
    """
    freqs = _log_freqs()
    stable = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    unstable = peq._bell_response_db(freqs, UNSTABLE_HZ, 4.0, 10.0)
    positions = [stable + scale * unstable for scale in (2.0, 1.5, -1.0)]
    return freqs, np.mean(positions, axis=0), positions


def _deepest_cut_near(design: strategy.CorrectionDesign, freq_hz: float) -> float:
    """The deepest single cut within a third of an octave of ``freq_hz``."""
    lo, hi = freq_hz / 2.0 ** (1 / 3), freq_hz * 2.0 ** (1 / 3)
    gains = [p.gain for p in design.peqs if lo <= p.freq <= hi and p.gain < 0]
    return min(gains) if gains else 0.0


# --------------------------------------------------------------------------
# The curve itself.
# --------------------------------------------------------------------------


def test_tolerance_is_an_alias_of_the_shipped_confidence_vocabulary():
    """Not a new number: retuning the room layer's repeatability labels retunes
    the cap with them. The GENEROUS end of the 4-6 dB band is deliberate — see
    the module docstring and AcceptanceThresholds for why an automatic action
    seeded from the tight end would fire on ordinary rooms."""
    assert variance_cap.TOLERABLE_STD_DB == spatial.MEDIUM_CONFIDENCE_STD_DB
    assert variance_cap.TOLERABLE_STD_DB > spatial.HIGH_CONFIDENCE_STD_DB


def test_depth_fraction_saturates_below_tolerance_then_tapers_as_one_over_sigma():
    tol = variance_cap.TOLERABLE_STD_DB
    sigma = np.array([0.0, spatial.HIGH_CONFIDENCE_STD_DB, tol, 2 * tol, 4 * tol])
    assert variance_cap.depth_fraction(sigma) == pytest.approx(
        [1.0, 1.0, 1.0, 0.5, 0.25]
    )
    depth = variance_cap.allowed_depth_db(sigma, base_max_cut_db=-10.0)
    assert depth == pytest.approx([-10.0, -10.0, -10.0, -5.0, -2.5])
    # A bin inside the tolerance gets BIT-identical allowance to the strategy's
    # own scalar, which is what makes the neutrality pin below structural.
    assert depth[0] == -10.0 and depth[2] == -10.0
    # Monotone, and a noisier measurement never earns MORE depth.
    ramp = variance_cap.depth_fraction(np.linspace(0.0, 60.0, 400))
    assert np.all(np.diff(ramp) <= 0.0)
    # Never exactly zero — the taper has no cliff for the designer to stop on.
    assert float(variance_cap.depth_fraction(np.array([1e6]))[0]) > 0.0


def test_no_correction_mask_covers_shallow_allowances_not_just_zero():
    """design_peq STOPS (not skips) on a gain below min_filter_gain_db, so
    "undesignable" has to mean shallower-than-meaningful, not merely zero."""
    depth = np.array([-10.0, -0.6, -0.4, 0.0])
    mask = variance_cap.no_correction_mask(depth, min_filter_gain_db=0.5)
    assert mask.tolist() == [False, False, True, True]


# --------------------------------------------------------------------------
# The deliverable: capped where unstable, untouched where stable.
# --------------------------------------------------------------------------


def test_unstable_frequency_loses_depth_while_stable_one_keeps_it():
    freqs, measured, positions = _fixture()

    uncapped = strategy.design_correction(measured, freqs)
    capped = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )

    # Uncapped, the unstable mode gets the whole peak inverted (~8.4 dB, inside
    # the strategy's -10 dB floor).
    assert _deepest_cut_near(uncapped, UNSTABLE_HZ) < -8.0
    # Capped, it gets roughly the fraction the taper allows at sigma ~13 dB.
    assert -5.0 < _deepest_cut_near(capped, UNSTABLE_HZ) < -4.0
    # The stable mode is corrected in both, to within a small residual-path
    # difference (the capped filter near the unstable mode spills a little).
    assert _deepest_cut_near(uncapped, STABLE_HZ) < -7.0
    assert _deepest_cut_near(capped, STABLE_HZ) < -7.0
    # Fixture precondition: the UNSTABLE mode is the tallest in-band residual,
    # so this also proves the capped design did not simply stop at it.
    band = strategy._band_mask(freqs, strategy.CORRECTION_STRATEGIES["balanced"])
    before = measured - target.flat_target(freqs)
    assert freqs[band][int(np.argmax(before[band]))] == pytest.approx(
        UNSTABLE_HZ, rel=0.05
    )


def test_design_peq_stops_rather_than_skips_an_uncorrectable_peak():
    """Pins the engine behaviour `no_correction_mask` exists for.

    Offered zero depth at the tallest peak, the greedy loop clamps that filter
    to zero gain and BREAKS — it does not skip the peak and go on to the next
    one. So the room layer must withhold such a bin from the search rather than
    hand the designer an allowance it will stop on.
    """
    freqs = _log_freqs()
    measured = (
        peq._bell_response_db(freqs, UNSTABLE_HZ, 4.0, 12.0)
        + peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    )
    target_db = target.flat_target(freqs)
    # Full depth everywhere except a third of an octave around the tall peak.
    cap = np.full_like(freqs, -10.0)
    near = (freqs > UNSTABLE_HZ / 1.26) & (freqs < UNSTABLE_HZ * 1.26)
    cap[near] = 0.0

    assert peq.design_peq(measured, target_db, freqs, max_cut_db=cap) == []
    # And with the bin withheld instead, the stable peak IS reached.
    withheld = peq.design_peq(
        np.where(cap >= 0.0, target_db, measured), target_db, freqs,
        max_cut_db=cap,
    )
    assert withheld and any(
        abs(p.freq - STABLE_HZ) < STABLE_HZ * 0.1 for p in withheld
    )


def test_a_wrecked_capture_withholds_bins_without_forfeiting_the_stable_mode():
    """The tail `no_correction_mask` guards, driven end to end.

    Positions that disagree by tens of dB at one frequency — a capture where a
    seat sat at the noise floor — push the allowance there below a meaningful
    filter. The room design must withhold those bins, disclose it, and still
    correct the mode every seat agrees about.
    """
    freqs = _log_freqs()
    stable = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    unstable = peq._bell_response_db(freqs, UNSTABLE_HZ, 4.0, 10.0)
    positions = [stable + scale * unstable for scale in (30.0, 0.0, -30.0)]
    measured = np.mean(positions, axis=0) + peq._bell_response_db(
        freqs, UNSTABLE_HZ, 4.0, 9.0,
    )

    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )
    assert design.report["spatial_variance_cap"]["n_bins_no_cut"] > 0
    assert _deepest_cut_near(design, STABLE_HZ) < -7.0
    # Nothing meaningful is designed near the wrecked mode — only a sub-dB
    # filter out on the taper's skirt, where the spread has already fallen.
    assert _deepest_cut_near(design, UNSTABLE_HZ) > -1.0


def test_cumulative_depth_at_one_centre_cannot_out_stack_the_allowance():
    """A per-filter floor does not bound a stack: the greedy designer re-picks
    a capped peak it failed to flatten. On this fixture the untrimmed set piles
    ~8.4 dB onto the 160 Hz centre where ~4.6 dB is allowed."""
    freqs, measured, positions = _fixture()
    correction = strategy.CORRECTION_STRATEGIES["balanced"]
    matrix, _ = spatial.build_spatial_matrix(positions, freqs)
    depth_cap_db, _ = variance_cap.plan_depth_cap(
        matrix,
        base_max_cut_db=correction.max_cut_db,
        min_filter_gain_db=correction.min_filter_gain_db,
        band_mask=strategy._band_mask(freqs, correction),
    )
    withheld = variance_cap.no_correction_mask(
        depth_cap_db, min_filter_gain_db=correction.min_filter_gain_db,
    )
    target_db = target.flat_target(freqs)
    untrimmed = peq.design_peq(
        np.where(withheld, target_db, measured), target_db, freqs,
        f_low=correction.f_low_hz, f_high=correction.f_high_hz,
        max_filters=correction.max_filters, max_cut_db=depth_cap_db,
        max_boost_db=correction.max_boost_db, cuts_only=correction.cuts_only,
        flatness_target_db=correction.flatness_target_db,
        q_min=correction.q_min, q_max=correction.q_max,
        min_filter_gain_db=correction.min_filter_gain_db,
    )
    hot_hz = min(
        (p.freq for p in untrimmed if p.gain < 0),
        key=lambda f: abs(f - UNSTABLE_HZ),
    )
    probe = np.asarray([hot_hz], dtype=np.float64)
    allowed = float(np.interp(hot_hz, freqs, depth_cap_db))
    stacked = float(peq.predicted_response(untrimmed, probe)[0])
    assert stacked < allowed - 1.0, "fixture precondition: the stack overshoots"

    trimmed, n_trimmed = strategy._enforce_variance_depth_cap(
        untrimmed, depth_cap_db, freqs, correction,
    )
    assert n_trimmed > 0
    assert len(trimmed) < len(untrimmed)
    assert float(peq.predicted_response(trimmed, probe)[0]) > stacked
    # And the shipped design carries the count so the trim is not silent.
    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )
    assert design.report["spatial_variance_cap"]["filters_depth_trimmed"] == n_trimmed


def test_the_trims_own_limitation_is_bounded_and_stated():
    """The trim binds each cut at its OWN centre, in order, so a later filter's
    skirt can still add a fraction of a dB over an earlier centre's allowance.
    `_enforce_variance_depth_cap` states that limitation and names 0.05 dB on
    this fixture; pin it so the stated number stays a measured one."""
    freqs, measured, positions = _fixture()
    correction = strategy.CORRECTION_STRATEGIES["balanced"]
    matrix, _ = spatial.build_spatial_matrix(positions, freqs)
    depth_cap_db, _ = variance_cap.plan_depth_cap(
        matrix,
        base_max_cut_db=correction.max_cut_db,
        min_filter_gain_db=correction.min_filter_gain_db,
        band_mask=strategy._band_mask(freqs, correction),
    )
    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )
    overshoot_db = max(
        float(np.interp(p.freq, freqs, depth_cap_db))
        - float(peq.predicted_response(design.peqs, np.asarray([p.freq]))[0])
        for p in design.peqs if p.gain < 0
    )
    assert overshoot_db == pytest.approx(0.05, abs=0.01)


def test_cumulative_trim_is_a_no_op_without_a_cap():
    freqs, measured, positions = _fixture()
    correction = strategy.CORRECTION_STRATEGIES["balanced"]
    filters = [peq.PEQ(freq=60.0, q=2.0, gain=-4.0), peq.PEQ(freq=61.0, q=2.0, gain=-4.0)]
    assert strategy._enforce_variance_depth_cap(
        filters, None, freqs, correction,
    ) == (filters, 0)


# --------------------------------------------------------------------------
# Honest neutrality: a stable room's design does not move.
# --------------------------------------------------------------------------


def test_stable_room_designs_byte_identically_to_the_uncapped_path():
    """The honest-neutrality pin. Every seat agrees, so the cap must take
    nothing — and the filters must match the pre-change (no-positions) design
    exactly, not merely closely."""
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    positions = [curve, curve + 0.5, curve - 0.5]  # sigma ~0.41 dB, well inside
    measured = np.mean(positions, axis=0)

    uncapped = strategy.design_correction(measured, freqs)
    capped = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )

    assert [(p.freq, p.q, p.gain) for p in capped.peqs] == [
        (p.freq, p.q, p.gain) for p in uncapped.peqs
    ]
    assert np.array_equal(capped.predicted_db, uncapped.predicted_db)
    block = capped.report["spatial_variance_cap"]
    assert block["available"] is True
    assert block["n_bins_capped"] == 0
    assert block["n_bins_no_cut"] == 0
    assert block["max_depth_forgone_db"] == 0.0
    assert block["worst_freq_hz"] is None
    assert block["filters_depth_trimmed"] == 0
    # Nothing happened, so nothing is claimed.
    assert not [
        w for w in capped.report["warnings"]
        if w["code"] == "spatial_variance_capped_depth"
    ]


# --------------------------------------------------------------------------
# Disclosure.
# --------------------------------------------------------------------------


def test_disclosure_and_nudge_state_the_ceiling_not_the_filters():
    freqs, measured, positions = _fixture()
    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )

    block = design.report["spatial_variance_cap"]
    assert block["available"] is True
    assert block["reason"] is None
    assert block["position_count"] == 3
    assert block["tolerable_std_db"] == variance_cap.TOLERABLE_STD_DB
    assert block["base_max_cut_db"] == -10.0
    assert 0 < block["n_bins_capped"] <= block["n_bins"]
    # This fixture's spread never reaches the undesignable tail.
    assert block["n_bins_no_cut"] == 0
    # The reduction in ALLOWED depth at the worst bin, never more than the
    # strategy's whole floor.
    assert 0.0 < block["max_depth_forgone_db"] < abs(block["base_max_cut_db"])
    assert block["worst_freq_hz"] == pytest.approx(UNSTABLE_HZ, rel=0.1)

    nudge = next(
        w for w in design.report["warnings"]
        if w["code"] == "spatial_variance_capped_depth"
    )
    assert nudge["severity"] == "info"
    # The claim is about the ceiling ("less correction depth was allowed"),
    # never about the shipped set — this record cannot know whether the
    # designer would have placed a filter there (see VarianceCapDisclosure).
    message = nudge["message"].lower()
    assert "depth was allowed" in message
    assert f"{block['n_bins_capped']} of {block['n_bins']}" in message
    assert not any(
        claim in message
        for claim in ("filters were", "filter was removed", "removed", "deleted")
    )


def test_advisor_packet_carries_the_cap_so_the_model_sees_the_refusal():
    """Without it the tuning model sees uncorrected residual and no reason for
    it, and would advise correcting harder — the advice the cap exists to
    refuse."""
    from jasper.calibration_agent import correction_advisor

    freqs, measured, positions = _fixture()
    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )

    class _Session:
        design_report = design.report
        peqs = design.peqs

    packet = correction_advisor.build_correction_advisor_context(_Session())
    summary = packet["correction"]["spatial_variance_cap"]
    assert summary["available"] is True
    assert summary["n_bins_capped"] == design.report[
        "spatial_variance_cap"]["n_bins_capped"]
    assert "Not a count of filters removed." in summary["note"]


# --------------------------------------------------------------------------
# Graceful degradation: never a crash, always a stated reason.
# --------------------------------------------------------------------------


def test_single_position_capture_leaves_the_design_alone_and_says_why():
    """One curve's std is zero by construction, not by stability. Reporting
    "nothing was capped" from it would be a confidence claim nobody measured."""
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)

    uncapped = strategy.design_correction(curve, freqs)
    single = strategy.design_correction(curve, freqs, position_magnitudes=[curve])

    assert [(p.freq, p.q, p.gain) for p in single.peqs] == [
        (p.freq, p.q, p.gain) for p in uncapped.peqs
    ]
    block = single.report["spatial_variance_cap"]
    assert block["available"] is False
    assert block["reason"] == spatial.TOO_FEW_POSITIONS_REASON
    assert block["position_count"] == 1
    # Absence, never a neutral-looking measurement.
    assert block["max_depth_forgone_db"] is None
    assert block["worst_freq_hz"] is None
    # The policy that WOULD have applied is still stated.
    assert block["base_max_cut_db"] == -10.0


def test_unstackable_positions_disclose_the_builders_reason_verbatim():
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    ragged = [curve, curve[:-1]]

    _, reason = spatial.build_spatial_matrix(ragged, freqs)
    design = strategy.design_correction(
        curve, freqs, position_magnitudes=ragged,
    )

    block = design.report["spatial_variance_cap"]
    assert block["available"] is False
    assert block["reason"] == reason
    assert block["position_count"] == 0
    assert design.peqs, "a broken spread must not cost the room its correction"


def test_no_positions_leaves_the_key_absent():
    """Additive, exactly like `crossover_region`: a caller that never supplied
    positions sees the report it always saw."""
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    design = strategy.design_correction(curve, freqs)
    assert "spatial_variance_cap" not in design.report


@pytest.mark.parametrize("base_max_cut_db", [0.0, -0.4])
def test_a_strategy_with_no_usable_cut_budget_is_refused_not_silently_muted(
    base_max_cut_db,
):
    """A cut budget at or shallower than the smallest worthwhile filter would
    scale to an allowance that marks EVERY frequency undesignable, suppressing
    the whole correction. That is silent total failure, so the cap refuses and
    hands the design back untouched instead."""
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    matrix, _ = spatial.build_spatial_matrix([curve, curve + 1.0, curve - 1.0], freqs)

    depth_cap_db, disclosure = variance_cap.plan_depth_cap(
        matrix,
        base_max_cut_db=base_max_cut_db,
        min_filter_gain_db=0.5,
        band_mask=np.ones_like(freqs, dtype=bool),
    )
    assert depth_cap_db is None
    assert disclosure.available is False
    assert disclosure.reason == "strategy designs no cuts"


def test_every_shipped_strategy_has_a_cut_budget_the_cap_can_shape():
    """The guard above must never fire on a real strategy — if it did, that
    strategy's rooms would silently lose the cap with no household signal."""
    for correction in strategy.CORRECTION_STRATEGIES.values():
        assert correction.max_cut_db < -correction.min_filter_gain_db, (
            correction.strategy_id
        )


def test_a_grid_mismatch_is_loud_because_it_is_a_caller_bug():
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    matrix, _ = spatial.build_spatial_matrix([curve, curve + 1.0], freqs)
    with pytest.raises(ValueError, match="band_mask shape"):
        variance_cap.plan_depth_cap(
            matrix,
            base_max_cut_db=-10.0,
            min_filter_gain_db=0.5,
            band_mask=np.ones(len(freqs) - 1, dtype=bool),
        )


def test_empty_band_is_refused_with_the_spread_vocabulary():
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    matrix, _ = spatial.build_spatial_matrix([curve, curve + 1.0], freqs)
    depth_cap_db, disclosure = variance_cap.plan_depth_cap(
        matrix,
        base_max_cut_db=-10.0,
        min_filter_gain_db=0.5,
        band_mask=np.zeros_like(freqs, dtype=bool),
    )
    assert depth_cap_db is None
    assert disclosure.reason == "no points in band"


# --------------------------------------------------------------------------
# The audit that already existed keeps working off the same single stack.
# --------------------------------------------------------------------------


def test_build_spatial_matrix_always_returns_exactly_one_of_matrix_or_reason():
    """`_filter_audit`'s "were positions supplied at all?" discriminator is
    "either the matrix or the reason is set", which is only total because
    `build_spatial_matrix` never returns both as None."""
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    cases: list[tuple[list[np.ndarray], np.ndarray | None]] = [
        ([curve, curve + 1.0], freqs),      # ok
        ([], freqs),                        # no positions
        ([curve], None),                    # no freqs
        ([curve, curve[:-1]], freqs),       # ragged
        ([curve * np.nan], freqs),          # non-finite
        ([np.zeros((2, 3))], freqs),        # not 1-D
    ]
    for magnitudes, grid in cases:
        matrix, reason = spatial.build_spatial_matrix(magnitudes, grid)
        assert (matrix is None) != (reason is None), (magnitudes, grid)


def test_filter_audit_still_annotates_spread_from_the_shared_matrix():
    freqs, measured, positions = _fixture()
    design = strategy.design_correction(
        measured, freqs, position_magnitudes=positions,
    )
    assert design.report["filters"]
    for entry in design.report["filters"]:
        assert entry["spatial_confidence"]["available"] is True


def test_filter_audit_reports_an_unstackable_spread_as_unavailable():
    freqs = _log_freqs()
    curve = peq._bell_response_db(freqs, STABLE_HZ, 4.0, 8.0)
    design = strategy.design_correction(
        curve, freqs, position_magnitudes=[curve, curve[:-1]],
    )
    assert design.report["filters"]
    for entry in design.report["filters"]:
        assert entry["spatial_confidence"]["available"] is False
        assert entry["spatial_confidence"]["reason"]
