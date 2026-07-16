# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MIN_TEST_LEVEL_DBFS,
    calibration_level_payload,
)
from jasper.active_speaker.driver_protection import (
    AUTO_LEVEL_DECISION_KIND,
    DRIVER_PROTECTION_KIND,
    auto_level_decision,
    driver_protection_payload,
    driver_protection_profile,
)


def test_low_frequency_auto_level_raises_one_bounded_step_when_mic_is_low() -> None:
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
    assert decision["next_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + 4 + AUDIBLE_RAMP_STEP_DB
    )
    assert decision["applied_delta_db"] == AUDIBLE_RAMP_STEP_DB
    assert decision["driver_protection"]["role_class"] == "low_frequency"


def test_auto_level_can_raise_after_floor_confirmation_without_mic_reading() -> None:
    current = calibration_level_payload(
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS,
    )

    decision = auto_level_decision(
        current,
        role="woofer",
        floor_audio_confirmed=True,
    )

    assert decision["status"] == "raise"
    assert decision["action"] == "raise"
    assert decision["next_level_dbfs"] == MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB
    assert decision["reason"] == "operator-controlled raise toward audible"


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


# Style -> protective high-pass floor, pinned per driver style. A compression
# driver (JTS3 hardware: B&C DE250-8, punch #14) floors at 2000 Hz; every
# other declared style floors higher; an undeclared/unrecognised style keeps
# today's conservative 5000 Hz default. This is the table a mis-declared or
# never-declared style silently falls back to, so it is pinned in full rather
# than spot-checked.
@pytest.mark.parametrize(
    ("driver_style", "expected_floor_hz"),
    (
        ("compression_driver", 2000.0),
        ("horn_compression_driver", 2000.0),
        ("dome_tweeter", 3000.0),
        ("amt_tweeter", 3000.0),
        ("planar_tweeter", 3500.0),
        ("ribbon_tweeter", 5000.0),
        ("supertweeter", 8000.0),
        (None, 5000.0),
        ("", 5000.0),
        ("some_future_style_not_in_the_table", 5000.0),
    ),
)
def test_tweeter_style_high_pass_floor_table(
    driver_style: str | None, expected_floor_hz: float
) -> None:
    profile = driver_protection_profile("tweeter", driver_style=driver_style)

    assert profile.min_highpass_hz == expected_floor_hz


def test_undeclared_tweeter_style_keeps_conservative_floor_when_hardware_ceiling_is_lower() -> None:
    # JTS3 shape: a woofer/tweeter pair with a 4000 Hz hard code-policy
    # ceiling and a compression tweeter meant to cross around 1.8-2.5 kHz.
    # Before a style is declared, the driver reads as "unknown" and the
    # conservative 5000 Hz floor exceeds the 4000 Hz ceiling, so no coherent
    # crossover exists -- this is the exact deadlock the gap produced.
    undeclared = driver_protection_profile("tweeter", driver_style=None)
    assert undeclared.min_highpass_hz == 5000.0
    assert undeclared.min_highpass_hz > 4000.0  # hard ceiling in the JTS3 shape

    declared = driver_protection_profile("tweeter", driver_style="compression_driver")
    assert declared.min_highpass_hz == 2000.0
    assert declared.min_highpass_hz <= 4000.0
