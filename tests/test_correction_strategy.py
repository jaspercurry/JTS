# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.correction import peq, strategy, target


def _log_freqs(n: int = 480) -> np.ndarray:
    return np.geomspace(20.0, 20000.0, n)


def test_balanced_strategy_matches_existing_peq_defaults():
    freqs = _log_freqs()
    measured = peq._bell_response_db(freqs, 80.0, 4.0, 6.0)
    target_db = target.flat_target(freqs)

    old = peq.design_peq(measured, target_db, freqs)
    design = strategy.design_correction(
        measured,
        freqs,
        target_choice="flat",
        strategy_choice="balanced",
    )

    assert design.strategy.strategy_id == "balanced"
    assert design.target_profile.target_id == "flat"
    assert [(p.freq, p.q, p.gain) for p in design.peqs] == [
        (p.freq, p.q, p.gain) for p in old
    ]
    assert design.report["predicted"]["filter_count"] == len(old)


def test_household_strategy_options_are_one_closed_allowlist():
    assert strategy.HOUSEHOLD_CORRECTION_STRATEGY_IDS == ("safe", "balanced")
    assert [
        option["strategy_id"]
        for option in strategy.household_correction_strategy_options()
    ] == list(strategy.HOUSEHOLD_CORRECTION_STRATEGY_IDS)
    assert "assertive" not in strategy.HOUSEHOLD_CORRECTION_STRATEGY_IDS


def test_design_report_labels_estimate_predicted_not_improvement():
    """The design report's before/after estimate is a MODEL prediction
    (measured + PEQ model, never re-measured). It must be exposed under
    `predicted`, and the old dishonest `improvement` key must be gone —
    no downstream surface should be able to call a prediction an
    improvement. The honest measured improvement only appears once a
    verify sweep lands (session.verify_before_after)."""
    freqs = _log_freqs()
    measured = peq._bell_response_db(freqs, 80.0, 4.0, 6.0)
    design = strategy.design_correction(measured, freqs, strategy_choice="balanced")

    assert "predicted" in design.report
    assert "improvement" not in design.report
    predicted = design.report["predicted"]
    # Same numeric fields as before, just honestly named.
    assert set(predicted) >= {
        "rms_db", "max_abs_db", "filter_count", "total_positive_boost_db",
    }
    # `before`/`after` here are the strategy-band estimate, distinct from
    # the verify path's 50-350 Hz measured band.
    assert design.report["band_hz"] == [
        design.strategy.f_low_hz, design.strategy.f_high_hz,
    ]


def test_safe_strategy_is_more_conservative_than_balanced():
    freqs = _log_freqs()
    measured = (
        peq._bell_response_db(freqs, 80.0, 4.0, 9.0)
        + peq._bell_response_db(freqs, 320.0, 4.0, 6.0)
    )

    safe = strategy.design_correction(
        measured,
        freqs,
        strategy_choice="safe",
    )
    balanced = strategy.design_correction(
        measured,
        freqs,
        strategy_choice="balanced",
    )

    assert safe.strategy.f_high_hz < balanced.strategy.f_high_hz
    assert all(p.freq <= safe.strategy.f_high_hz for p in safe.peqs)
    assert any(p.freq > safe.strategy.f_high_hz for p in balanced.peqs)
    assert all(p.gain >= safe.strategy.max_cut_db for p in safe.peqs)


def test_assertive_strategy_reports_boost_policy_and_headroom():
    freqs = _log_freqs()
    measured = -peq._bell_response_db(freqs, 120.0, 3.0, 6.0)

    design = strategy.design_correction(
        measured,
        freqs,
        strategy_choice="assertive",
    )

    assert design.peqs
    assert any(p.gain > 0 for p in design.peqs)
    assert 0 < design.report["predicted"]["total_positive_boost_db"] <= 3.0
    assert any(
        warning["code"] == "boosts_enabled"
        for warning in design.report["warnings"]
    )
    assert any(
        warning["code"] == "boosts_capped"
        for warning in design.report["warnings"]
    )


def test_design_report_explains_filter_choices():
    freqs = _log_freqs()
    measured = peq._bell_response_db(freqs, 80.0, 4.0, 6.0)

    design = strategy.design_correction(measured, freqs)

    report = design.report
    assert report["before"]["rms_db"] > report["after"]["rms_db"]
    assert report["dominant_residuals"]["peaks"][0]["freq_hz"] > 0
    first_filter = report["filters"][0]
    assert first_filter["action"] == "cut_peak"
    assert "Cut a +" in first_filter["rationale"]
    assert first_filter["local_predicted_delta_db"] > 0


def test_design_report_adds_spatial_confidence_when_positions_provided():
    freqs = _log_freqs()
    measured = peq._bell_response_db(freqs, 80.0, 4.0, 6.0)
    positions = [
        measured,
        measured + peq._bell_response_db(freqs, 80.0, 4.0, 0.5),
        measured - peq._bell_response_db(freqs, 80.0, 4.0, 0.5),
    ]

    design = strategy.design_correction(
        measured,
        freqs,
        position_magnitudes=positions,
    )

    first_filter = design.report["filters"][0]
    assert first_filter["spatial_confidence"]["available"] is True
    assert first_filter["spatial_confidence"]["confidence_level"] == "high"
    assert first_filter["spatial_confidence"]["range_db"] > 0


def test_unknown_choices_fall_back_to_safe_defaults():
    assert strategy.resolve_target_profile("bogus").target_id == "flat"
    assert (
        strategy.resolve_correction_strategy("bogus").strategy_id
        == "balanced"
    )


# --------------------------------------------------------------------------
# Crossover-region no-boost rule (revision plan §3.3 / P5). Ground-truth: a dip
# AT the bass-management corner gets no boost; the SAME dip an octave away may.
# Real-shape: synthesize a real measured curve with a real dip, run the real
# boost-capable designer with a real corner.
# --------------------------------------------------------------------------


def _boost_freqs(design: strategy.CorrectionDesign) -> list[float]:
    return [p.freq for p in design.peqs if p.gain > 0]


def test_crossover_region_dip_at_corner_gets_no_boost():
    freqs = _log_freqs()
    corner = 80.0
    # A real dip exactly AT the corner (negative bell) — a boost-capable
    # strategy would otherwise try to fill it.
    measured = peq._bell_response_db(freqs, corner, 4.0, -6.0)

    design = strategy.design_correction(
        measured,
        freqs,
        strategy_choice="assertive",  # boost-capable
        crossover_hz=corner,
    )
    # No boost lands inside the ±1/3-octave crossover band (63.5..100.8 Hz).
    lo, hi = strategy._crossover_no_boost_band_hz(corner)
    assert not [f for f in _boost_freqs(design) if lo <= f <= hi]
    # The report annotates the crossover region so the envelope can explain it.
    region = design.report.get("crossover_region")
    assert region is not None
    assert region["corner_hz"] == corner
    assert region["excluded_boosts"], "the corner dip must be recorded as excluded"
    assert any(
        w["code"] == "crossover_region_dip_not_boosted"
        for w in design.report["warnings"]
    )


def test_same_dip_an_octave_away_may_be_boosted():
    freqs = _log_freqs()
    corner = 80.0
    dip_freq = 160.0  # one octave above the corner — OUTSIDE the ±1/3-oct band
    measured = peq._bell_response_db(freqs, dip_freq, 4.0, -6.0)

    with_corner = strategy.design_correction(
        measured, freqs, strategy_choice="assertive", crossover_hz=corner,
    )
    # The dip is outside the crossover band, so the corner read does NOT strip
    # it: a boost near 160 Hz survives (identical to running with no corner).
    lo, hi = strategy._crossover_no_boost_band_hz(corner)
    boosts = _boost_freqs(with_corner)
    assert boosts, "a boost-capable strategy should place a boost for this dip"
    assert all(not (lo <= f <= hi) for f in boosts)
    # And the excluded set is empty — nothing was near the corner to exclude.
    region = with_corner.report.get("crossover_region")
    assert region is not None
    assert region["excluded_boosts"] == []

    no_corner = strategy.design_correction(
        measured, freqs, strategy_choice="assertive", crossover_hz=None,
    )
    # Corner=None is a no-op for the dip an octave away: same boosts either way.
    assert _boost_freqs(no_corner) == boosts


def test_crossover_region_cut_at_corner_is_still_allowed():
    freqs = _log_freqs()
    corner = 80.0
    # A real PEAK at the corner (positive bell) — a genuine peak is still a peak,
    # so a CUT there must survive (only boosts are excluded near Fc).
    measured = peq._bell_response_db(freqs, corner, 4.0, 8.0)

    design = strategy.design_correction(
        measured, freqs, strategy_choice="assertive", crossover_hz=corner,
    )
    lo, hi = strategy._crossover_no_boost_band_hz(corner)
    cuts_in_band = [p for p in design.peqs if p.gain < 0 and lo <= p.freq <= hi]
    assert cuts_in_band, "a peak at the corner must still be cut"
    # No boost was excluded (there was no dip to boost), so no nudge.
    assert not any(
        w["code"] == "crossover_region_dip_not_boosted"
        for w in design.report["warnings"]
    )


def test_no_bass_management_leaves_report_without_crossover_annotation():
    freqs = _log_freqs()
    measured = peq._bell_response_db(freqs, 80.0, 4.0, -6.0)
    design = strategy.design_correction(
        measured, freqs, strategy_choice="assertive", crossover_hz=None,
    )
    # No corner -> no crossover_region key (additive; old consumers unaffected).
    assert "crossover_region" not in design.report


def test_boosts_capped_and_crossover_exclusion_each_own_their_reduction():
    """Reviewer repro (P5 should-fix 2): a -6 dB dip AT an 80 Hz corner under
    'assertive'. The greedy designer proposes 6.0 dB of raw boost; the headroom
    cap trims it to 3.0 dB; the near-Fc rule then removes that remaining 3.0 dB.
    Each warning must claim exactly its own reduction — boosts_capped reports
    the CAP's work (6.0 -> 3.0, computed pre-exclusion), never a false
    'capped ... to 0.0 dB to preserve headroom' that misattributes the
    crossover exclusion to headroom. This report is the assistant-readable
    audit (P6's tuning-LLM input), so a wrong reason would be narrated to the
    household.
    """
    freqs = _log_freqs()
    corner = 80.0
    measured = peq._bell_response_db(freqs, corner, 4.0, -6.0)

    design = strategy.design_correction(
        measured, freqs, strategy_choice="assertive", crossover_hz=corner,
    )

    by_code = {w["code"]: w for w in design.report["warnings"]}
    # The cap's own reduction: raw 6.0 dB -> capped 3.0 dB (max_total_boost_db).
    capped_msg = by_code["boosts_capped"]["message"]
    assert "6.0 dB" in capped_msg and "3.0 dB" in capped_msg
    assert "0.0 dB" not in capped_msg  # the exclusion's work is NOT the cap's
    # The crossover rule's own reduction: the surviving 3.0 dB of boosts,
    # recorded as excluded (2.0 + 1.0 at ~80 Hz), with its own warning.
    assert "crossover_region_dip_not_boosted" in by_code
    excluded = design.report["crossover_region"]["excluded_boosts"]
    assert sum(e["gain_db"] for e in excluded) == pytest.approx(3.0)
    # Nothing shipped: every proposed boost was either capped away or excluded.
    assert design.peqs == []
    assert design.report["predicted"]["total_positive_boost_db"] == 0.0
