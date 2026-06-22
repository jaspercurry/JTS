# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.correction import quality


def test_capture_quality_warns_without_calibration():
    captured = np.full(48000, 0.1, dtype=np.float32)
    report = quality.assess_capture(
        captured,
        sample_rate=48000,
        expected_sample_rate=48000,
        sweep_n_samples=24000,
        has_mic_calibration=False,
        input_device={
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
            "channel_count": 1,
        },
    )
    assert report.failed is False
    assert report.peak_dbfs == pytest.approx(-20.0)
    assert [i.code for i in report.issues] == ["mic_uncalibrated"]


def test_capture_quality_fails_on_clipping():
    captured = np.zeros(48000, dtype=np.float32)
    captured[:100] = 1.0
    report = quality.assess_capture(
        captured,
        sample_rate=48000,
        expected_sample_rate=48000,
        sweep_n_samples=24000,
        has_mic_calibration=True,
    )
    assert report.failed is True
    assert any(i.code == "capture_clipped" for i in report.issues)
    with pytest.raises(quality.CaptureQualityError, match="clipped"):
        raise quality.CaptureQualityError(report)


def test_capture_quality_surfaces_browser_processing_flags():
    captured = np.full(48000, 0.1, dtype=np.float32)
    report = quality.assess_capture(
        captured,
        sample_rate=48000,
        expected_sample_rate=48000,
        sweep_n_samples=24000,
        has_mic_calibration=True,
        input_device={
            "echo_cancellation": True,
            "noise_suppression": True,
            "auto_gain_control": True,
            "channel_count": 2,
        },
    )
    codes = {issue.code for issue in report.issues}
    assert "browser_echo_cancellation" in codes
    assert "browser_noise_suppression" in codes
    assert "browser_auto_gain_control" in codes
    assert "browser_channel_count" in codes
