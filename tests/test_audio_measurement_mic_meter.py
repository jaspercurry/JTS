# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.audio_measurement.mic_meter import classify_mic_meter


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
