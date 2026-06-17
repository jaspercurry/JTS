from __future__ import annotations

import numpy as np

from jasper.correction import confidence, spatial


def test_confidence_for_std_caps_high_below_three_positions():
    """A 2-seat std is too few samples to call repeatability 'high' — cap it
    to 'medium'. With >=3 positions (or an unknown count) the std-only
    classification stands; the cap only touches the 'high' band."""
    low = spatial.HIGH_CONFIDENCE_STD_DB - 1.0   # std that classifies "high"
    assert spatial.confidence_for_std(low, n_positions=2) == "medium"
    assert spatial.confidence_for_std(low, n_positions=3) == "high"
    assert spatial.confidence_for_std(low) == "high"  # legacy: no count given

    mid = spatial.MEDIUM_CONFIDENCE_STD_DB - 0.5
    assert spatial.confidence_for_std(mid, n_positions=2) == "medium"
    high = spatial.MEDIUM_CONFIDENCE_STD_DB + 1.0
    assert spatial.confidence_for_std(high, n_positions=2) == "low"


def test_two_position_low_variance_capped_to_medium_costs_score():
    """End-to-end ripple of the <3-position cap (the only case where it
    fires): two near-identical seats have a low per-frequency std that would
    classify 'high', but with only two positions it is capped to 'medium' —
    which costs the confidence score 10 points (moderate_position_variance)
    and so can tighten the score-gated strategies. Conservative, not neutral."""
    freqs = np.array([20.0, 40.0, 80.0, 160.0, 320.0])
    # Two near-identical seats → low std (would be "high" with 3+ positions).
    positions = [
        np.array([0.0, 1.0, 3.0, 2.0, 0.0]),
        np.array([0.1, 1.1, 3.1, 2.1, 0.1]),
    ]
    report = confidence.build_confidence_report(
        total_positions=2,
        completed_positions=2,
        has_mic_calibration=True,
        input_device={"label": "UMIK-2", "device_id_hash": "abc123"},
        capture_quality=[{"issues": []}, {"issues": []}],
        strategy_choice="balanced",
        repeatability_report={"available": True, "level": "high"},
        position_magnitudes=positions,
        freqs_hz=freqs,
    )

    assert report["position_variance"]["available"] is True
    # The cap fired: a low-std two-seat band reads "medium", not "high".
    assert report["position_variance"]["confidence_level"] == "medium"
    # ...which surfaces the score-penalty path (the gate-affecting effect).
    assert any(
        f["code"] == "moderate_position_variance" for f in report["findings"]
    )


def test_confidence_high_for_calibrated_multi_position_clean_capture():
    freqs = np.array([20.0, 40.0, 80.0, 160.0, 320.0])
    positions = [
        np.array([0.0, 1.0, 3.0, 2.0, 0.0]),
        np.array([0.5, 1.5, 2.5, 2.0, -0.5]),
        np.array([-0.5, 0.5, 3.2, 1.5, 0.2]),
    ]

    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "UMIK-2", "device_id_hash": "abc123"},
        capture_quality=[{"issues": []}, {"issues": []}, {"issues": []}],
        strategy_choice="balanced",
        repeatability_report={"available": True, "level": "high"},
        position_magnitudes=positions,
        freqs_hz=freqs,
    )

    assert report["level"] == "high"
    assert report["score"] >= 80
    assert report["position_variance"]["available"] is True
    assert report["position_variance"]["confidence_level"] == "high"
    assert {
        band["band_id"] for band in report["position_bands"]
    } >= {"sub_bass", "bass", "upper_bass", "correction_band"}
    assert report["strategy_gates"]["balanced"]["allowed"] is True
    assert report["strategy_gates"]["assertive"]["allowed"] is True


def test_confidence_downgrades_single_uncalibrated_processed_capture():
    report = confidence.build_confidence_report(
        total_positions=5,
        completed_positions=1,
        has_mic_calibration=False,
        input_device={"label": "iPhone Microphone"},
        capture_quality=[{
            "issues": [{
                "code": "browser_echo_cancellation",
                "severity": "warn",
                "message": "browser reported echo cancellation enabled",
            }],
        }],
        strategy_choice="assertive",
    )

    assert report["level"] == "low"
    codes = {finding["code"] for finding in report["findings"]}
    assert "single_position" in codes
    assert "uncalibrated_mic" in codes
    assert "browser_processing_reported" in codes
    assert report["strategy_gates"]["safe"]["allowed"] is True
    assert report["strategy_gates"]["balanced"]["allowed"] is False
    assert report["strategy_gates"]["assertive"]["allowed"] is False


def test_confidence_includes_browser_audio_path_failures():
    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[
            {"issues": [{
                "code": "browser_echo_cancellation",
                "severity": "warn",
                "message": "browser reported echo cancellation enabled",
            }]},
            {"issues": []},
            {"issues": []},
        ],
        strategy_choice="balanced",
        browser_audio_report={
            "level": "fail",
            "issues": [{
                "code": "browser_echo_cancellation",
                "severity": "fail",
                "message": "browser reported echo cancellation enabled",
            }],
        },
    )

    assert report["level"] == "low"
    assert report["evidence"]["browser_audio_issue_count"] == 1
    assert any(
        finding["code"] == "browser_audio_path_failed"
        for finding in report["findings"]
    )
    assert report["strategy_gates"]["safe"]["allowed"] is False


def test_confidence_promotes_low_snr_into_assertive_gate():
    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[
            {"issues": [{
                "code": "capture_snr_low",
                "severity": "warn",
                "message": "capture is less than 20 dB above noise",
            }]},
            {"issues": []},
            {"issues": []},
        ],
        strategy_choice="balanced",
    )

    assert report["evidence"]["snr_low_count"] == 1
    assert any(
        finding["code"] == "capture_snr_low"
        for finding in report["findings"]
    )
    assert report["strategy_gates"]["safe"]["allowed"] is True
    assert report["strategy_gates"]["assertive"]["allowed"] is False
    assert "capture SNR is low" in (
        report["strategy_gates"]["assertive"]["reasons"]
    )


def test_confidence_includes_runtime_integrity_failures():
    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[{"issues": []}, {"issues": []}, {"issues": []}],
        strategy_choice="balanced",
        runtime_integrity={
            "level": "fail",
            "issues": [{
                "code": "runtime_capture_too_short",
                "severity": "fail",
                "message": "uploaded capture is shorter than the played sweep",
            }],
        },
    )

    assert report["level"] == "low"
    assert report["evidence"]["runtime_integrity_failure_count"] == 1
    assert any(
        finding["code"] == "runtime_integrity_failed"
        for finding in report["findings"]
    )
    assert report["strategy_gates"]["safe"]["allowed"] is False


def test_confidence_reports_low_position_variance_level():
    freqs = np.array([40.0, 80.0, 160.0, 320.0])
    positions = [
        np.array([0.0, 8.0, -10.0, 1.0]),
        np.array([0.0, -2.0, 8.0, 1.0]),
        np.array([0.0, 5.0, -9.0, 1.0]),
    ]

    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[{"issues": []}, {"issues": []}, {"issues": []}],
        strategy_choice="balanced",
        position_magnitudes=positions,
        freqs_hz=freqs,
    )

    assert report["position_variance"]["available"] is True
    assert report["position_variance"]["confidence_level"] == "low"
    assert any(
        flag["kind"] == "high_position_variance"
        for flag in report["feature_flags"]
    )
    assert report["strategy_gates"]["assertive"]["allowed"] is False
    assert any(
        finding["code"] == "high_position_variance"
        for finding in report["findings"]
    )


def test_confidence_blocks_strategy_gates_without_completed_measurement():
    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=0,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[],
        strategy_choice="balanced",
    )

    assert report["level"] == "low"
    assert report["strategy_gates"]["safe"]["allowed"] is False
    assert report["strategy_gates"]["balanced"]["allowed"] is False
    assert report["strategy_gates"]["assertive"]["allowed"] is False
    assert (
        "no completed measurements are available"
        in report["strategy_gates"]["safe"]["reasons"]
    )


def test_confidence_variance_handles_mismatched_position_shapes():
    report = confidence.build_confidence_report(
        total_positions=3,
        completed_positions=3,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[],
        strategy_choice="assertive",
        position_magnitudes=[
            np.array([0.0, 1.0, 2.0]),
            np.array([0.0, 1.0]),
            np.array([0.0, 1.0, 2.0]),
        ],
        freqs_hz=np.array([20.0, 40.0, 80.0]),
    )

    assert report["position_variance"]["available"] is False
    assert report["position_variance"]["reason"] == "position curve shapes differ"
    assert report["strategy_gates"]["assertive"]["allowed"] is False


def test_confidence_variance_rejects_empty_frequency_grid():
    report = confidence.build_confidence_report(
        total_positions=2,
        completed_positions=2,
        has_mic_calibration=True,
        input_device={"label": "USB measurement mic"},
        capture_quality=[],
        strategy_choice="balanced",
        position_magnitudes=[np.array([]), np.array([])],
        freqs_hz=np.array([]),
    )

    assert report["position_variance"]["available"] is False
    assert report["position_variance"]["reason"] == "freqs must be non-empty 1-D"


def test_position_report_flags_deep_nulls_and_residual_bands():
    freqs = np.array([40.0, 80.0, 120.0, 160.0, 240.0])
    positions = [
        np.array([0.0, -7.0, 3.0, 2.0, 0.0]),
        np.array([0.5, -8.0, 2.0, 2.5, -0.5]),
        np.array([-0.5, -6.5, 3.2, 1.5, 0.2]),
    ]
    measured = np.mean(np.vstack(positions), axis=0)
    target = np.zeros_like(measured)

    report = confidence.build_position_report(
        position_magnitudes=positions,
        freqs_hz=freqs,
        measured_db=measured,
        target_db=target,
        correction_band_hz=(20.0, 250.0),
    )

    assert report["available"] is True
    correction_band = next(
        band for band in report["bands"]
        if band["band_id"] == "correction_band"
    )
    assert correction_band["residual"]["deepest_null_db"] < -6.0
    deep_nulls = [
        flag for flag in report["feature_flags"]
        if flag["kind"] == "deep_null"
    ]
    assert deep_nulls
    assert deep_nulls[0]["decision"] == "do_not_boost_by_default"
    assert deep_nulls[0]["region_hz"] == [80.0, 80.0]
