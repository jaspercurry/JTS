from __future__ import annotations

import numpy as np

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
    assert design.report["improvement"]["filter_count"] == len(old)


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
    assert 0 < design.report["improvement"]["total_positive_boost_db"] <= 3.0
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
    assert first_filter["local_improvement_db"] > 0


def test_unknown_choices_fall_back_to_safe_defaults():
    assert strategy.resolve_target_profile("bogus").target_id == "flat"
    assert (
        strategy.resolve_correction_strategy("bogus").strategy_id
        == "balanced"
    )
