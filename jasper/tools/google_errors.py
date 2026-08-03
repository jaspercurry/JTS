# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared user-facing errors for Google-backed voice tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..google_creds import GoogleClients


def no_account_error(clients: "GoogleClients", attempted: str) -> dict:
    available = clients.list_account_names()
    if not available:
        return {
            "ok": False,
            "error": (
                "No Google accounts linked to this speaker yet. "
                "Visit jts.local/google to add one."
            ),
        }
    name_list = ", ".join(available)
    if attempted:
        return {
            "ok": False,
            "error": (
                f"No Google account named '{attempted}' on this speaker. "
                f"Available: {name_list}."
            ),
        }
    return {
        "ok": False,
        "error": (
            "Could not pick a default Google account. "
            f"Try naming one: {name_list}."
        ),
    }


def no_credentials_error(account_name: str) -> dict:
    return {
        "ok": False,
        "error": (
            f"Google access for {account_name} can't be refreshed. "
            "Re-link at jts.local/google."
        ),
    }
