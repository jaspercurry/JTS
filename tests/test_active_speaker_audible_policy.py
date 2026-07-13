# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.active_speaker.audible_policy import (
    AUDIBLE_TEST_ALLOWED_ROLES,
    audible_policy_payload,
    audible_role_allowed,
    audible_role_block_code,
    audible_role_block_message,
)
from jasper.active_speaker.driver_protection import driver_protection_payload


def test_tweeter_requires_explicit_driver_protection() -> None:
    assert audible_role_allowed(" Tweeter ") is False
    assert audible_role_block_code(" Tweeter ") == (
        "high_frequency_protection_not_ready"
    )
    assert "valid protection profile" in audible_role_block_message("tweeter")

    assert audible_role_allowed("tweeter", driver_protection={}) is False
    blocked = {"audio_allowed": False, "role_class": "high_frequency"}
    assert audible_role_allowed("tweeter", driver_protection=blocked) is False
    blocked_payload = audible_policy_payload(
        " Tweeter ",
        driver_protection=blocked,
    )
    assert blocked_payload["target_role"] == "tweeter"
    assert blocked_payload["target_role_allowed"] is False
    assert blocked_payload["driver_role_class"] == "high_frequency"
    assert blocked_payload["driver_protection_audio_allowed"] is False

    protected = driver_protection_payload(
        "tweeter",
        driver_style="dome_tweeter",
        protection_status="present",
        band_limit={"type": "highpass", "highpass_hz": 3000},
    )
    assert protected["audio_allowed"] is True
    assert audible_role_allowed("tweeter", driver_protection=protected) is True


def test_audible_policy_payload_exposes_low_frequency_default() -> None:
    payload = audible_policy_payload("woofer")

    assert payload["allowed_roles"] == sorted(AUDIBLE_TEST_ALLOWED_ROLES)
    assert payload["target_role"] == "woofer"
    assert payload["target_role_allowed"] is True
    assert payload["driver_role_class"] == "low_frequency"
    assert payload["driver_protection_audio_allowed"] is True


def test_unknown_role_uses_generic_block_reason() -> None:
    assert audible_role_allowed("full_range") is False
    assert audible_role_block_code("full_range") == "audible_role_not_enabled"
    assert "woofer, mid, and subwoofer" in audible_role_block_message("full_range")
