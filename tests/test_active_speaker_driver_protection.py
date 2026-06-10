from __future__ import annotations

from jasper.active_speaker.calibration_level import (
    MIN_TEST_LEVEL_DBFS,
    calibration_level_payload,
)
from jasper.active_speaker.driver_protection import (
    AUTO_LEVEL_DECISION_KIND,
    DRIVER_PROTECTION_KIND,
    auto_level_decision,
    driver_protection_payload,
)


def test_low_frequency_auto_level_raises_one_step_when_mic_is_low() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS + 4,
        observed_mic_dbfs=-52,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-52,
        floor_audio_confirmed=True,
    )

    assert decision["kind"] == AUTO_LEVEL_DECISION_KIND
    assert decision["status"] == "raise"
    assert decision["action"] == "raise"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 5
    assert decision["applied_delta_db"] == 1
    assert decision["driver_protection"]["role_class"] == "low_frequency"


def test_auto_level_does_not_raise_above_floor_without_confirmation() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS + 1,
        observed_mic_dbfs=-58,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-58,
        floor_audio_confirmed=False,
    )

    assert decision["status"] == "waiting_for_floor_confirmation"
    assert decision["action"] == "hold_for_floor_confirmation"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 1
    assert decision["applied_delta_db"] == 0


def test_auto_level_holds_when_mic_is_usable() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-70,
        observed_mic_dbfs=-32,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-32,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "locked"
    assert decision["action"] == "hold"
    assert decision["next_level_dbfs"] == -70


def test_auto_level_resets_to_floor_on_clipping() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-70,
        observed_mic_dbfs=-18,
        mic_clipping=True,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        observed_mic_dbfs=-18,
        mic_clipping=True,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "reset"
    assert decision["action"] == "reset_to_floor"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert decision["mic_meter"]["status"] == "clipping"


def test_high_frequency_auto_level_waits_for_floor_confirmation() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS,
        observed_mic_dbfs=-60,
    )

    decision = auto_level_decision(
        current,
        role="tweeter",
        driver_style="dome_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 3000},
        observed_mic_dbfs=-60,
        floor_audio_confirmed=False,
    )

    assert decision["status"] == "waiting_for_floor_confirmation"
    assert decision["action"] == "hold_for_floor_confirmation"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert decision["driver_protection"]["role_class"] == "high_frequency"


def test_high_frequency_auto_level_uses_driver_specific_cap() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=-65,
        observed_mic_dbfs=-60,
    )

    decision = auto_level_decision(
        current,
        role="tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 5000},
        observed_mic_dbfs=-60,
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "maxed"
    assert decision["action"] == "hold_at_cap"
    assert decision["next_level_dbfs"] == -65
    assert decision["max_auto_level_dbfs"] == -65
    assert "auto_level_cap_reached" in {
        issue["code"] for issue in decision["issues"]
    }


def test_high_frequency_protection_requires_highpass_band_limit() -> None:
    missing = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
    )
    blocked = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 3000},
    )
    allowed = driver_protection_payload(
        "tweeter",
        driver_style="ribbon_tweeter",
        protection_status="software_guard_requested",
        band_limit={"type": "highpass", "highpass_hz": 5000},
    )

    assert missing["audio_allowed"] is False
    assert "high_frequency_highpass_missing" in {
        issue["code"] for issue in missing["issues"]
    }
    assert blocked["kind"] == DRIVER_PROTECTION_KIND
    assert blocked["audio_allowed"] is False
    assert "high_frequency_highpass_missing" in {
        issue["code"] for issue in blocked["issues"]
    }
    assert allowed["audio_allowed"] is True
