# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.audio_measurement.mic_meter import classify_mic_meter


@pytest.mark.parametrize(("value", "status"), [
    (-70, "too_quiet"), (-50, "low"), (-30, "usable"), (-10, "too_loud"),
    ("-30", "unmeasured"), (True, "unmeasured"), (None, "unmeasured"),
    (float("nan"), "unmeasured"),
])
def test_classify_mic_meter_reports_usable_capture_window(value, status) -> None:
    assert classify_mic_meter(observed_dbfs=value)["status"] == status


def test_classify_mic_meter_clipping_overrides_level() -> None:
    meter = classify_mic_meter(observed_dbfs=-30, clipping=True)

    assert meter["status"] == "clipping"
    assert meter["tone"] == "danger"
    assert meter["recommendation"] == "stop_or_lower"
