# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared user-facing errors for Google-backed voice tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..google_creds import GoogleClients

logger = logging.getLogger(__name__)

# `label` -> what the user hears ('Gmail', 'Google Calendar').
_FRIENDLY = {"gmail": "Gmail", "calendar": "Google Calendar"}


def no_account_error(
    clients: "GoogleClients", attempted: str, setup_url: str,
) -> dict:
    available = clients.list_account_names()
    if not available:
        return {
            "ok": False,
            "error": (
                "No Google accounts linked to this speaker yet. "
                f"Visit {setup_url} to add one."
            ),
            "setup_url": setup_url,
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


def no_credentials_error(account_name: str, setup_url: str) -> dict:
    return {
        "ok": False,
        "error": (
            f"Google access for {account_name} can't be refreshed. "
            f"Re-link at {setup_url}."
        ),
        "setup_url": setup_url,
    }


def api_error(label: str, account_name: str, exc: Exception) -> dict:
    """Generic fallback for a googleapiclient HttpError or transport
    failure, shared by gmail.py and calendar.py. Logged with the full
    traceback for debugging; the model speaks the short version.
    `label` names the failing surface ('gmail', 'calendar') for both
    the log line and the `_FRIENDLY` lookup."""
    logger.warning(
        "%s API error for %s: %s", label, account_name, exc, exc_info=True,
    )
    return {
        "ok": False,
        "error": f"Couldn't reach {_FRIENDLY[label]} just now. Try again in a moment.",
    }
