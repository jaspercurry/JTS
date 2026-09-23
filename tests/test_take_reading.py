# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparing takes the way REW compares traces: one window, one smoothing, b minus a."""

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.round_captures import PoseCapture, RoundCapturesRefused
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_COMPARE_NO_COMMON_BAND, TakeRead, compare_preview_report, compare_report, read_preview,
)
from jasper.audio_measurement.impulse_reading import magnitude_db

RATE = 48_000
ORIGIN = 12_000


def _take(capture_id: str, *, role: str = "summed", delay: int = 100, gain: float = 1.0,
          gate_ms: float | None = 8.0, band: tuple[float, float] = (100.0, 20_000.0)) -> TakeRead:
    ir = np.zeros(36_000)
    ir[ORIGIN + delay] = gain
    return TakeRead(PoseCapture(
        capture_id=capture_id, phase=None, wav=None, program=None, program_sha256="",
        azimuth_deg=0.0, vertical_deg=0.0, mark_distance_m=1.0, radiated_band_hz=band,
        sample_rate=RATE, ir=ir, peak_idx=ORIGIN + delay,
        preprocessing={"impulse_source": "kept", "pre_guard_samples": ORIGIN, "clock_shift_samples": 0.0},
        curve={"gate_window_ms": gate_ms} if gate_ms else {},
    ), role)


def test_b_minus_a_reads_a_level_change_unless_the_level_is_removed():
    louder = compare_report(_take("t1"), _take("t2", gain=2.0))
    shape_only = compare_report(_take("t1"), _take("t2", gain=2.0), remove_level=True)

    assert [band["b_minus_a_db"] for band in louder["summary"]["bands"]] == pytest.approx(
        [6.02] * len(louder["summary"]["bands"]), abs=0.01)
    assert (shape_only["summary"]["level_offset_db"], shape_only["summary"]["rms_db"]) == (
        pytest.approx(6.02, abs=0.01), pytest.approx(0.0, abs=0.01))


def test_arrival_compares_only_within_one_recording():
    woofer, tweeter = _take("t1", role="woofer", delay=100), _take("t1", role="tweeter", delay=125)
    within = compare_report(woofer, tweeter)["summary"]
    across = compare_report(woofer, _take("t2", role="tweeter", delay=125))["summary"]

    assert (within["same_recording"], within["relative_arrival_ms"]) == (True, pytest.approx(25 / 48, abs=1e-3))
    assert (across["same_recording"], across["relative_arrival_ms"]) == (False, None)


def test_both_sides_are_read_through_the_shorter_take_window_unless_one_is_named():
    a, b = _take("t1", gate_ms=8.0), _take("t2", gate_ms=5.0)

    assert compare_report(a, b)["parameters"]["window_ms"] == 5.0
    assert compare_report(a, b, window_ms=20.0)["parameters"]["window_ms"] == 20.0


def test_sides_that_share_no_trusted_band_are_refused_by_name():
    with pytest.raises(RoundCapturesRefused) as refused:
        compare_report(_take("t1", band=(100.0, 300.0)), _take("t2", band=(2000.0, 8000.0)))
    assert refused.value.reason == REFUSE_COMPARE_NO_COMMON_BAND


def test_a_forecast_is_compared_through_its_own_window():
    measured = _take("t1", gate_ms=None)
    freqs = np.fft.rfftfreq(65_536, 1 / RATE)[1:]
    grid = freqs[(freqs >= 300.0) & (freqs <= 18_000.0)]
    forecast = magnitude_db(measured.capture.ir, RATE, peak_index=measured.capture.peak_idx,
                            window_ms=7.0, lead_ms=1.0, grid_hz=grid) - 20.0
    preview = read_preview({"section": "emitted_graph", "preview": {
        "kind": "jts_capture_prediction",
        "summary": {"window": {"window_ms": 7.0, "lead_ms": 1.0}, "candidate_id": "cand", "basis": {}},
        "prediction": {"freqs_hz": grid.tolist(), "predicted_db": forecast.tolist(),
                       "sum_band_hz": [300.0, 18_000.0]},
    }})
    report = compare_preview_report(preview, measured)

    assert report["parameters"]["window_ms"] == 7.0
    assert report["summary"]["level_offset_db"] == pytest.approx(20.0, abs=0.05)
    assert report["summary"]["rms_db"] < 0.05
