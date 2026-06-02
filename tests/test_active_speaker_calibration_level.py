from __future__ import annotations

from jasper.active_speaker.calibration_level import (
    DEFAULT_TEST_LEVEL_DBFS,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
    calibration_level_payload,
    clamp_test_level_dbfs,
    classify_mic_meter,
)


def test_calibration_level_defaults_to_floor() -> None:
    payload = calibration_level_payload()

    assert payload["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert payload["test_signal"]["default_level_dbfs"] == DEFAULT_TEST_LEVEL_DBFS
    assert payload["test_signal"]["normal_system_volume_untouched"] is True
    assert payload["safety"]["operator_controls_level"] is True
    assert payload["safety"]["jts_enforces_bounds"] is True
    assert payload["mic_meter"]["status"] == "unmeasured"


def test_clamp_test_level_dbfs_enforces_commissioning_bounds() -> None:
    assert clamp_test_level_dbfs(-100) == MIN_TEST_LEVEL_DBFS
    assert clamp_test_level_dbfs(-55) == -55
    assert clamp_test_level_dbfs(-10) == MAX_TEST_LEVEL_DBFS
    assert clamp_test_level_dbfs("not-a-number") == DEFAULT_TEST_LEVEL_DBFS


def test_classify_mic_meter_reports_usable_capture_window() -> None:
    assert classify_mic_meter(observed_dbfs=-70)["status"] == "too_quiet"
    assert classify_mic_meter(observed_dbfs=-50)["status"] == "low"
    assert classify_mic_meter(observed_dbfs=-30)["status"] == "usable"
    assert classify_mic_meter(observed_dbfs=-10)["status"] == "too_loud"


def test_classify_mic_meter_clipping_overrides_level() -> None:
    meter = classify_mic_meter(observed_dbfs=-30, clipping=True)

    assert meter["status"] == "clipping"
    assert meter["tone"] == "danger"
    assert meter["recommendation"] == "stop_or_lower"
