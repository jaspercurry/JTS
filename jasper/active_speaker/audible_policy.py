"""Shared safety policy for the first active-speaker audible test slice."""

from __future__ import annotations

from typing import Any

AUDIBLE_TEST_POLICY_VERSION = "woofer_mid_low_level_v1"
AUDIBLE_TEST_ALLOWED_ROLES = frozenset({"mid", "subwoofer", "woofer"})


def normalise_driver_role(role: Any) -> str:
    return str(role or "").strip().lower()


def audible_role_allowed(role: Any) -> bool:
    return normalise_driver_role(role) in AUDIBLE_TEST_ALLOWED_ROLES


def audible_role_block_code(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "tweeter_audio_not_enabled"
    return "audible_role_not_enabled"


def audible_role_block_message(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "tweeter/compression-driver playback is disabled for this slice"
    return "audible tests are limited to woofer, mid, and subwoofer targets"


def audible_policy_payload(role: Any) -> dict[str, Any]:
    target_role = normalise_driver_role(role)
    return {
        "policy_version": AUDIBLE_TEST_POLICY_VERSION,
        "allowed_roles": sorted(AUDIBLE_TEST_ALLOWED_ROLES),
        "target_role": target_role or None,
        "target_role_allowed": audible_role_allowed(target_role),
    }
