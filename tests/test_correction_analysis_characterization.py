# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Characterization guards for MeasurementSession's analysis helpers.

These exact outputs are the compatibility boundary for moving the acoustic
math out of the session state machine.  Keep the tests focused on observable
reports rather than the eventual module layout.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.audio_measurement import analysis, calibration, deconv, quality, sweep
from jasper.correction import acoustic_quality, envelope, status
from .correction_session_fixtures import make_measurement_session


def test_capture_band_snr_preserves_exact_report(tmp_path: Path):
    sess = make_measurement_session(tmp_path)
    sample_rate = sess.cfg.sample_rate
    time_s = np.arange(sample_rate, dtype=np.float64) / sample_rate
    captured = sum(
        0.08 * np.sin(2.0 * np.pi * frequency_hz * time_s)
        for frequency_hz in (60.0, 120.0, 240.0, 600.0)
    )
    capture_path = tmp_path / "capture.wav"
    sweep.write_sweep_wav(
        capture_path,
        captured.astype(np.float32),
        sample_rate,
    )
    noise_report = {
        "band_noise_dbfs": [
            {"band_id": "transition", "level_dbfs": -53.0},
            {"band_id": "outside_room_policy", "level_dbfs": -99.0},
            {"band_id": "sub_bass", "level_dbfs": -50.0},
            {"band_id": "bass", "level_dbfs": -51.0},
        ],
    }

    report = acoustic_quality.capture_band_snr(capture_path, noise_report)

    assert report == [
        {
            "band_id": "sub_bass",
            "band_hz": [20.0, 80.0],
            "capture_level_dbfs": -50.0,
            "noise_level_dbfs": -50.0,
            "estimated_snr_db": 0.0,
            "method": "fft_band_power_difference",
        },
        {
            "band_id": "bass",
            "band_hz": [80.0, 160.0],
            "capture_level_dbfs": -51.25,
            "noise_level_dbfs": -51.0,
            "estimated_snr_db": -0.25,
            "method": "fft_band_power_difference",
        },
        {
            "band_id": "transition",
            "band_hz": [350.0, 1000.0],
            "capture_level_dbfs": -60.35,
            "noise_level_dbfs": -53.0,
            "estimated_snr_db": -7.35,
            "method": "fft_band_power_difference",
        },
    ]
    assert sess._capture_band_snr(capture_path, noise_report) == report


def test_capture_band_snr_fails_closed_without_usable_evidence(tmp_path: Path):
    sess = make_measurement_session(tmp_path)

    assert sess._capture_band_snr(tmp_path / "missing.wav", None) == []
    assert sess._capture_band_snr(
        tmp_path / "missing.wav",
        {"band_noise_dbfs": []},
    ) == []


def test_direct_arrival_report_preserves_exact_boundaries():
    impulse_response = np.full(100, 0.01, dtype=np.float64)
    impulse_response[50] = 1.0

    report = acoustic_quality.direct_arrival_report(
        impulse_response,
        sample_rate=1000,
    )
    assert report == {
        "available": True,
        "direct_peak_index": 50,
        "direct_peak_dbfs": 0.0,
        "pre_arrival_floor_dbfs": -40.0,
        "direct_to_pre_arrival_db": 40.0,
        "pre_arrival_window_ms": [28.0, 48.0],
    }
    assert acoustic_quality.direct_arrival_report(
        np.ones((2, 8)),
        sample_rate=1000,
    ) == {
        "available": False,
        "reason": "impulse response unavailable",
    }
    assert acoustic_quality.direct_arrival_report(
        np.ones(7),
        sample_rate=1000,
    ) == {
        "available": False,
        "reason": "impulse response unavailable",
    }
    early_peak = np.zeros(100, dtype=np.float64)
    early_peak[9] = 1.0
    assert acoustic_quality.direct_arrival_report(
        early_peak,
        sample_rate=1000,
    ) == {
        "available": False,
        "reason": "not enough pre-arrival samples before direct peak",
        "direct_peak_index": 9,
    }
    boundary_peak = np.zeros(100, dtype=np.float64)
    boundary_peak[10] = 1.0
    assert acoustic_quality.direct_arrival_report(
        boundary_peak,
        sample_rate=1000,
    ) == {
        "available": True,
        "direct_peak_index": 10,
        "direct_peak_dbfs": 0.0,
        "pre_arrival_floor_dbfs": -120.0,
        "direct_to_pre_arrival_db": 120.0,
        "pre_arrival_window_ms": [0.0, 8.0],
    }


@pytest.mark.parametrize(
    ("delta_db", "expected_level", "expected_issue_count"),
    [
        (1.5, "high", 0),
        (2.5, "medium", 0),
        (2.51, "low", 1),
    ],
)
def test_repeatability_thresholds_are_inclusive(
    tmp_path: Path,
    delta_db: float,
    expected_level: str,
    expected_issue_count: int,
):
    sess = make_measurement_session(tmp_path)
    freqs_hz = np.array([40.0, 50.0, 100.0, 200.0, 350.0, 400.0])
    first = np.zeros(freqs_hz.shape)
    repeat = np.full(freqs_hz.shape, delta_db)

    report = acoustic_quality.repeatability_from_arrays(
        first,
        repeat,
        freqs_hz,
        peq_f_high=sess.cfg.peq_f_high,
    )

    assert report == {
        "available": True,
        "level": expected_level,
        "band_hz": [50.0, 350.0],
        "metrics": {
            "rms_db": delta_db,
            "p95_abs_db": delta_db,
            "max_abs_db": delta_db,
        },
        "issues": (
            [{
                "code": "repeatability_low",
                "severity": "warn",
                "message": (
                    "same-position repeat capture differs enough to limit "
                    "assertive correction"
                ),
            }]
            if expected_issue_count
            else []
        ),
    }
    assert sess._repeatability_from_arrays(first, repeat, freqs_hz) == report


def test_repeatability_unavailable_reports_are_exact(tmp_path: Path):
    sess = make_measurement_session(tmp_path)

    assert sess._repeatability_from_arrays(
        np.zeros(3),
        np.zeros(4),
        np.zeros(3),
    ) == {
        "available": False,
        "level": "unavailable",
        "reason": "repeat and original curves use different shapes",
    }
    assert sess._repeatability_from_arrays(
        np.zeros(3),
        np.zeros(3),
        np.array([20.0, 40.0, 500.0]),
    ) == {
        "available": False,
        "level": "unavailable",
        "reason": "not enough points in the repeatability band",
    }


@pytest.mark.parametrize(
    ("outlier_db", "expected_level", "expected_p95_db"),
    [
        (3.0, "high", 3.0),
        (3.01, "medium", 3.01),
        (5.0, "medium", 5.0),
        (5.01, "low", 5.01),
    ],
)
def test_repeatability_p95_thresholds_are_independent(
    tmp_path: Path,
    outlier_db: float,
    expected_level: str,
    expected_p95_db: float,
):
    sess = make_measurement_session(tmp_path)
    freqs_hz = np.linspace(50.0, 350.0, 20)
    first = np.zeros(freqs_hz.shape)
    repeat = np.zeros(freqs_hz.shape)
    repeat[-2:] = outlier_db

    report = acoustic_quality.repeatability_from_arrays(
        first,
        repeat,
        freqs_hz,
        peq_f_high=sess.cfg.peq_f_high,
    )

    assert report["level"] == expected_level
    assert report["metrics"] == {
        "rms_db": round(outlier_db / np.sqrt(10.0), 2),
        "p95_abs_db": expected_p95_db,
        "max_abs_db": outlier_db,
    }
    assert bool(report["issues"]) is (expected_level == "low")
    assert sess._repeatability_from_arrays(first, repeat, freqs_hz) == report


def test_repeatability_uses_configured_upper_band(tmp_path: Path):
    sess = make_measurement_session(tmp_path)
    sess.cfg.peq_f_high = 200.0
    freqs_hz = np.array([50.0, 100.0, 200.0, 250.0])

    report = acoustic_quality.repeatability_from_arrays(
        np.zeros(freqs_hz.shape),
        np.ones(freqs_hz.shape),
        freqs_hz,
        peq_f_high=sess.cfg.peq_f_high,
    )

    assert report["available"] is True
    assert report["band_hz"] == [50.0, 200.0]
    assert report["metrics"] == {
        "rms_db": 1.0,
        "p95_abs_db": 1.0,
        "max_abs_db": 1.0,
    }
    assert sess._repeatability_from_arrays(
        np.zeros(freqs_hz.shape),
        np.ones(freqs_hz.shape),
        freqs_hz,
    ) == report


def test_smooth_capture_requires_prepared_sweep_metadata(tmp_path: Path):
    sess = make_measurement_session(tmp_path)

    with pytest.raises(
        RuntimeError,
        match="flow ordering bug .*_ensure_sweep_cache first",
    ):
        sess._smooth_capture(
            tmp_path / "capture.wav",
            capture_kind="measurement",
            position_index=0,
        )


def test_smooth_capture_preserves_pipeline_and_five_value_result(
    tmp_path: Path,
    monkeypatch,
):
    sess = make_measurement_session(tmp_path)
    sess.cfg.sample_rate = 1000
    sess.sweep_meta = SimpleNamespace(
        n_samples=3,
        f1=20.0,
        f2=400.0,
        duration_s=1.0,
        sample_rate=1000,
        amplitude_dbfs=-12.0,
    )
    sess.mic_calibration = SimpleNamespace(curve="calibration-curve")
    sess.input_device = {"label": "measurement mic"}
    capture_path = tmp_path / "capture.wav"
    captured = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    capped = captured[:3]
    capture_quality = quality.CaptureQuality(
        sample_rate=1000,
        duration_s=0.003,
        peak_dbfs=-8.0,
        rms_dbfs=-12.0,
        clipped_fraction=0.0,
        issues=(),
    )
    impulse_response = np.full(100, 0.01, dtype=np.float64)
    impulse_response[50] = 1.0
    raw_freqs = np.array([20.0, 100.0])
    raw_magnitude = np.array([1.0, 2.0])
    smoothed = np.array([3.0, 4.0])
    log_freqs = np.array([50.0, 100.0])
    resampled = np.array([5.0, 6.0])
    calibrated = np.array([7.0, 8.0])
    normalized = np.array([9.0, 10.0])
    order: list[str] = []

    def read_wav(path):
        order.append("read")
        assert path == capture_path
        return captured, 1000

    def cap_capture(values, *, sweep_len, sample_rate):
        order.append("cap")
        np.testing.assert_array_equal(values, captured)
        assert (sweep_len, sample_rate) == (3, 1000)
        return capped

    def assess(values, **kwargs):
        order.append("quality")
        np.testing.assert_array_equal(values, capped)
        assert kwargs == {
            "sample_rate": 1000,
            "expected_sample_rate": 1000,
            "sweep_n_samples": 3,
            "has_mic_calibration": True,
            "input_device": {"label": "measurement mic"},
            "truncated_from_samples": 4,
            "quality_model": quality.ROOM,
        }
        return capture_quality

    def synchronized_sweep(**kwargs):
        order.append("sweep")
        assert kwargs == {
            "f1": 20.0,
            "f2": 400.0,
            "duration_approx_s": 1.0,
            "sample_rate": 1000,
            "amplitude_dbfs": -12.0,
        }
        return np.array([0.25, 0.5]), object()

    def deconvolve(values, sweep_signal, *, sample_rate):
        order.append("deconvolve")
        np.testing.assert_array_equal(values, capped.astype(np.float64))
        np.testing.assert_array_equal(sweep_signal, np.array([0.25, 0.5]))
        assert sample_rate == 1000
        return impulse_response

    def magnitude_response(ir, sample_rate, *, normalize):
        order.append("magnitude")
        np.testing.assert_array_equal(ir, impulse_response)
        assert sample_rate == 1000
        assert normalize is False
        return raw_freqs, raw_magnitude

    def smooth_response(freqs, magnitude, *, fraction):
        order.append("smooth")
        np.testing.assert_array_equal(freqs, raw_freqs)
        np.testing.assert_array_equal(magnitude, raw_magnitude)
        assert fraction == 48
        return smoothed

    def resample_response(freqs, magnitude):
        order.append("resample")
        np.testing.assert_array_equal(freqs, raw_freqs)
        np.testing.assert_array_equal(magnitude, smoothed)
        return log_freqs, resampled

    def apply_calibration(freqs, magnitude, curve):
        order.append("calibrate")
        np.testing.assert_array_equal(freqs, log_freqs)
        np.testing.assert_array_equal(magnitude, resampled)
        assert curve == "calibration-curve"
        return calibrated

    def normalize_response(freqs, magnitude, *, f_low, f_high):
        order.append("normalize")
        np.testing.assert_array_equal(freqs, log_freqs)
        np.testing.assert_array_equal(magnitude, calibrated)
        assert (f_low, f_high) == (200.0, 1000.0)
        return normalized

    replay_call = {}

    def write_replay(path, **kwargs):
        order.append("replay")
        replay_call.update(path=path, **kwargs)
        return {"response_path": "analysis/p0_response.json"}

    monkeypatch.setattr(sweep, "read_wav_mono", read_wav)
    monkeypatch.setattr(deconv, "cap_capture_length", cap_capture)
    monkeypatch.setattr(quality, "assess_capture", assess)
    monkeypatch.setattr(sweep, "synchronized_swept_sine", synchronized_sweep)
    monkeypatch.setattr(deconv, "deconvolve", deconvolve)
    monkeypatch.setattr(deconv, "magnitude_response", magnitude_response)
    monkeypatch.setattr(analysis, "smooth_fractional_octave", smooth_response)
    monkeypatch.setattr(analysis, "resample_log", resample_response)
    monkeypatch.setattr(calibration, "apply_calibration_curve", apply_calibration)
    monkeypatch.setattr(analysis, "normalize_to_band", normalize_response)
    monkeypatch.setattr(sess, "_write_capture_replay_artifacts", write_replay)

    analysis_result = acoustic_quality.analyze_capture(
        capture_path,
        sweep_meta=sess.sweep_meta,
        expected_sample_rate=sess.cfg.sample_rate,
        mic_calibration=sess.mic_calibration,
        input_device=sess.input_device,
        normalize_band_hz=(200.0, 1000.0),
    )

    assert order == [
        "read",
        "cap",
        "quality",
        "sweep",
        "deconvolve",
        "magnitude",
        "smooth",
        "resample",
        "calibrate",
        "normalize",
    ]
    np.testing.assert_array_equal(analysis_result.log_freqs_hz, log_freqs)
    np.testing.assert_array_equal(
        analysis_result.log_magnitude_db,
        normalized,
    )
    assert analysis_result.capture_quality is capture_quality
    assert analysis_result.direct_arrival == {
        "available": True,
        "direct_peak_index": 50,
        "direct_peak_dbfs": 0.0,
        "pre_arrival_floor_dbfs": -40.0,
        "direct_to_pre_arrival_db": 40.0,
        "pre_arrival_window_ms": [28.0, 48.0],
    }

    order.clear()
    monkeypatch.setattr(
        acoustic_quality,
        "analyze_capture",
        lambda *_args, **_kwargs: analysis_result,
    )
    result = sess._smooth_capture(
        capture_path,
        capture_kind="measurement",
        position_index=0,
    )

    assert order == ["replay"]
    np.testing.assert_array_equal(result[0], log_freqs)
    np.testing.assert_array_equal(result[1], normalized)
    assert result[2] is capture_quality
    assert result[3] == {
        "available": True,
        "direct_peak_index": 50,
        "direct_peak_dbfs": 0.0,
        "pre_arrival_floor_dbfs": -40.0,
        "direct_to_pre_arrival_db": 40.0,
        "pre_arrival_window_ms": [28.0, 48.0],
    }
    assert result[4] == {"response_path": "analysis/p0_response.json"}
    assert replay_call["path"] == capture_path
    assert replay_call["capture_kind"] == "measurement"
    assert replay_call["position_index"] == 0
    np.testing.assert_array_equal(replay_call["ir"], impulse_response)
    np.testing.assert_array_equal(replay_call["raw_freqs_hz"], raw_freqs)
    np.testing.assert_array_equal(
        replay_call["raw_magnitude_db"],
        raw_magnitude,
    )
    np.testing.assert_array_equal(
        replay_call["smoothed_magnitude_db"],
        smoothed,
    )
    np.testing.assert_array_equal(replay_call["log_freqs_hz"], log_freqs)
    np.testing.assert_array_equal(replay_call["log_magnitude_db"], normalized)
    assert replay_call["direct_arrival"] == result[3]


def test_smooth_capture_stops_before_deconvolution_on_failed_quality(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    sess = make_measurement_session(tmp_path)
    sess.sweep_meta = SimpleNamespace(n_samples=3)
    failed = quality.CaptureQuality(
        sample_rate=sess.cfg.sample_rate,
        duration_s=0.0,
        peak_dbfs=-120.0,
        rms_dbfs=-120.0,
        clipped_fraction=0.0,
        issues=(quality.QualityIssue(
            code="capture_empty",
            severity="fail",
            message="capture is empty",
        ),),
    )
    monkeypatch.setattr(
        sweep,
        "read_wav_mono",
        lambda _path: (np.zeros(3, dtype=np.float32), sess.cfg.sample_rate),
    )
    monkeypatch.setattr(deconv, "cap_capture_length", lambda values, **_: values)
    monkeypatch.setattr(quality, "assess_capture", lambda *_args, **_kwargs: failed)
    monkeypatch.setattr(
        deconv,
        "deconvolve",
        lambda *_args, **_kwargs: pytest.fail("failed capture reached deconvolution"),
    )

    with caplog.at_level("WARNING", logger="jasper.correction.session"):
        with pytest.raises(quality.CaptureQualityError) as exc_info:
            sess._smooth_capture(
                tmp_path / "capture.wav",
                capture_kind="measurement",
                position_index=0,
            )

    assert exc_info.value.report is failed
    assert any(
        "capture_quality session=" in record.message
        and "code=capture_empty" in record.message
        for record in caplog.records
    )


def test_helper_reports_reach_status_and_homeowner_nudge(
    tmp_path: Path,
    monkeypatch,
):
    sess = make_measurement_session(tmp_path)
    sess.session_id = "analysis-characterization"
    sess.bundle_dir = tmp_path
    captured_path = tmp_path / "captures" / "p0.wav"
    capture_quality = quality.CaptureQuality(
        sample_rate=48000,
        duration_s=1.25,
        peak_dbfs=-3.0,
        rms_dbfs=-20.0,
        clipped_fraction=0.0,
        issues=(),
    )
    band_snr = [{
        "band_id": "bass",
        "band_hz": [80.0, 160.0],
        "capture_level_dbfs": -30.0,
        "noise_level_dbfs": -55.0,
        "estimated_snr_db": 25.0,
        "method": "fft_band_power_difference",
    }]
    direct_arrival = {
        "available": True,
        "direct_peak_index": 42,
        "direct_to_pre_arrival_db": 35.0,
    }
    monkeypatch.setattr(sess, "_capture_band_snr", lambda *_args: band_snr)

    status_report = sess._quality_report_dict(
        capture_quality,
        capture_kind="measurement",
        captured_wav_path=captured_path,
        position_index=0,
        noise_report={
            "rms_dbfs": -50.0,
            "method": "pre_sweep_silence_wav",
            "artifact_path": "captures/noise-p0.wav",
        },
        direct_arrival=direct_arrival,
        replay_artifacts={"response_path": "analysis/p0_response.json"},
    )
    expected_projection = {
        key: status_report[key]
        for key in (
            "artifact_path",
            "estimated_snr_db",
            "band_snr",
            "direct_arrival",
            "replay_artifacts",
        )
    }
    sess.capture_quality = [status_report]
    sess._refresh_acoustic_quality()
    expected_status_sha256 = (
        "14ec69fc13509525e49b35401c03622f17c7205b93507fc636a3a3b39c85bdb8"
    )
    for payload in (
        sess.snapshot(),
        status.info_json_payload(sess),
        status.result_json_payload(sess),
    ):
        report = payload["capture_quality"][0]
        assert hashlib.sha256(json.dumps(report).encode()).hexdigest() == (
            expected_status_sha256
        )
        assert {
            key: report[key]
            for key in expected_projection
        } == expected_projection
        assert payload["acoustic_quality"]["capture_count"] == 1
        assert payload["acoustic_quality"]["min_band_snr_db"] == 25.0

    sess._write_acoustic_quality_json()
    acoustic_path = tmp_path / "acoustic_quality.json"
    persisted_bytes = acoustic_path.read_bytes()
    assert hashlib.sha256(persisted_bytes).hexdigest() == (
        "261dd876ca504b900b016d34ba792a77f94eae65bd6d7f4f146e9ca8328ca298"
    )
    persisted = json.loads(persisted_bytes)
    persisted_capture = persisted["captures"][0]
    assert persisted_capture["snr"]["band_snr"] == band_snr
    assert persisted_capture["direct_arrival"] == direct_arrival

    freqs_hz = np.linspace(50.0, 350.0, 20)
    repeat = np.zeros(freqs_hz.shape)
    repeat[-2:] = 5.01
    sess.repeatability_report = sess._repeatability_from_arrays(
        np.zeros(freqs_hz.shape),
        repeat,
        freqs_hz,
    )
    sess.confidence_report = sess._build_confidence_report()

    assert [
        nudge for nudge in envelope.build_envelope(sess)["nudges"]
        if nudge["code"] == "repeatability_low"
    ] == [{
        "code": "repeatability_low",
        "severity": "warn",
        "text": (
            "The main-seat repeat differed more than expected. Keeping the "
            "microphone still and re-measuring may help, but you can continue."
        ),
    }]
