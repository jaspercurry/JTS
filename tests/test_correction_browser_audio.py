from __future__ import annotations

from jasper.correction import browser_audio


def test_browser_audio_path_ok_for_clean_calibrated_usb_mic():
    report = browser_audio.assess_browser_audio_path(
        input_device={
            "label": "USB measurement mic",
            "sample_rate": 48000,
            "channel_count": 1,
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
            "requested_device_id_hash": "abc",
            "actual_device_id_hash": "abc",
        },
        expected_sample_rate=48000,
        has_mic_calibration=True,
    )

    assert report.level == "ok"
    assert report.failed is False
    assert report.warning_count == 0
    assert report.to_dict()["summary"].endswith("measurement-ready.")


def test_browser_audio_path_flags_processing_and_sample_rate():
    report = browser_audio.assess_browser_audio_path(
        input_device={
            "label": "iPhone microphone",
            "sample_rate": 44100,
            "channel_count": 2,
            "echo_cancellation": True,
            "noise_suppression": True,
            "auto_gain_control": True,
        },
        expected_sample_rate=48000,
        has_mic_calibration=False,
    )

    codes = {issue.code for issue in report.issues}
    assert report.level == "fail"
    assert report.failed is True
    assert "sample_rate_mismatch" in codes
    assert "browser_echo_cancellation" in codes
    assert "browser_noise_suppression" in codes
    assert "browser_auto_gain_control" in codes
    assert "browser_channel_count" in codes
    assert "mic_uncalibrated" in codes


def test_browser_audio_path_warns_on_device_mismatch_without_raw_ids():
    report = browser_audio.assess_browser_audio_path(
        input_device={
            "sample_rate": 48000,
            "channel_count": 1,
            "requested_device_id_hash": "requested",
            "actual_device_id_hash": "actual",
        },
        expected_sample_rate=48000,
        has_mic_calibration=True,
    ).to_dict()

    assert report["level"] == "warn"
    assert any(
        issue["code"] == "browser_device_mismatch"
        for issue in report["issues"]
    )
    assert "requested-device-id" not in str(report)


def test_browser_audio_path_handles_missing_metadata_as_warning():
    report = browser_audio.assess_browser_audio_path(
        input_device=None,
        expected_sample_rate=48000,
        has_mic_calibration=False,
    )

    assert report.available is False
    assert report.level == "warn"
    assert report.warning_count == 1


def test_browser_audio_path_handles_malformed_browser_numbers():
    report = browser_audio.assess_browser_audio_path(
        input_device={
            "sample_rate": "not-a-number",
            "channel_count": "also-not-a-number",
            "echo_cancellation": False,
            "noise_suppression": False,
            "auto_gain_control": False,
        },
        expected_sample_rate=48000,
        has_mic_calibration=True,
    )

    codes = {issue.code for issue in report.issues}
    assert report.level == "warn"
    assert "browser_sample_rate_missing" in codes
    assert "browser_channel_count_missing" in codes
