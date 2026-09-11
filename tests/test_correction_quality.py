# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement import quality


def test_capture_quality_warns_without_calibration():
    captured = np.full(48000, 0.1, dtype=np.float32)
    report = quality.assess_capture(
        captured,
        sample_rate=48000,
        expected_sample_rate=48000,
        sweep_n_samples=24000,
        has_mic_calibration=False,
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
