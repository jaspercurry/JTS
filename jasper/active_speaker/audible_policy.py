# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared active-speaker audible-test policy."""

from __future__ import annotations

from typing import Any

from .driver_protection import (
    DRIVER_PROTECTION_POLICY_VERSION,
    FULL_RANGE_ROLES,
    LOW_FREQUENCY_ROLES,
    driver_protection_payload,
    normalise_driver_role,
)

AUDIBLE_TEST_POLICY_VERSION = DRIVER_PROTECTION_POLICY_VERSION
#: The role classes ``driver_protection_payload`` can admit with nothing staged
#: — DERIVED from that module's own classes rather than restated, because a set
#: here that disagreed with its ``audio_allowed`` answer would be two policies
#: for one question. ``HIGH_FREQUENCY_ROLES`` is absent because a tweeter is
#: admitted only against a protection status, which a role name cannot carry.
#: Published as ``allowed_roles``; it is a description of the classes, and
#: :func:`audible_role_allowed` is the answer for one target.
AUDIBLE_TEST_ALLOWED_ROLES = LOW_FREQUENCY_ROLES | FULL_RANGE_ROLES


def audible_role_allowed(
    role: Any,
    *,
    driver_protection: dict[str, Any] | None = None,
) -> bool:
    """Whether an audible test may target this driver at all.

    ONE owner: ``driver_protection_payload``'s ``audio_allowed``. A caller that
    already built the protection envelope hands it in — that envelope knows the
    staged band limit, the protection status and the declared floor this
    function has no way to reach — and a caller that has not gets the envelope
    the bare role produces, which admits the classes that need no evidence
    beyond their name and refuses the rest.
    """
    protection = (
        driver_protection
        if isinstance(driver_protection, dict)
        else driver_protection_payload(normalise_driver_role(role))
    )
    return bool(protection.get("audio_allowed"))


def audible_role_block_code(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "high_frequency_protection_not_ready"
    return "audible_role_not_enabled"


def audible_role_block_message(role: Any) -> str:
    if normalise_driver_role(role) == "tweeter":
        return "high-frequency driver playback requires a valid protection profile"
    return (
        "audible tests are limited to "
        + ", ".join(sorted(AUDIBLE_TEST_ALLOWED_ROLES))
        + " targets"
    )


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
