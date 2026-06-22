# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared active-speaker audible-test policy."""

from __future__ import annotations

from typing import Any

from .driver_protection import (
    DRIVER_PROTECTION_POLICY_VERSION,
    LOW_FREQUENCY_ROLES,
    driver_protection_payload,
    normalise_driver_role,
)

AUDIBLE_TEST_POLICY_VERSION = DRIVER_PROTECTION_POLICY_VERSION
AUDIBLE_TEST_ALLOWED_ROLES = LOW_FREQUENCY_ROLES


def audible_role_allowed(
    role: Any,
    *,
    driver_protection: dict[str, Any] | None = None,
) -> bool:
    target_role = normalise_driver_role(role)
    if target_role in AUDIBLE_TEST_ALLOWED_ROLES:
        return True
    if isinstance(driver_protection, dict):
        return bool(driver_protection.get("audio_allowed"))
    return False


def audible_role_block_code(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "high_frequency_protection_not_ready"
    return "audible_role_not_enabled"


def audible_role_block_message(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "high-frequency driver playback requires a valid protection profile"
    return "audible tests are limited to woofer, mid, and subwoofer targets"


def audible_policy_payload(
    role: Any,
    *,
    driver_protection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_role = normalise_driver_role(role)
    protection = (
        driver_protection
        if isinstance(driver_protection, dict)
        else driver_protection_payload(target_role)
    )
    return {
        "policy_version": AUDIBLE_TEST_POLICY_VERSION,
        "allowed_roles": sorted(AUDIBLE_TEST_ALLOWED_ROLES),
        "target_role": target_role or None,
        "target_role_allowed": audible_role_allowed(
            target_role,
            driver_protection=protection,
        ),
        "driver_role_class": protection.get("role_class"),
        "driver_style": protection.get("driver_style"),
        "driver_protection_audio_allowed": bool(protection.get("audio_allowed")),
    }
