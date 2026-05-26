from __future__ import annotations

import numpy as np

from jasper.correction import confidence


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
        position_magnitudes=positions,
        freqs_hz=freqs,
    )

    assert report["level"] == "high"
    assert report["score"] >= 80
    assert report["position_variance"]["available"] is True
    assert report["position_variance"]["confidence_level"] == "high"
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
