# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared user-facing errors for Google-backed voice tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..identity import resolve_hostname

if TYPE_CHECKING:
    from ..google_creds import GoogleClients


def _setup_url() -> str:
    """This speaker's own Google-linking wizard, resolved fresh per
    call — never a hardcoded hostname, which would misdirect a speaker
    that isn't named jts.local (e.g. jts3.local)."""
    return f"{resolve_hostname()}/google"


def no_account_error(clients: "GoogleClients", attempted: str) -> dict:
    available = clients.list_account_names()
    if not available:
        return {
            "ok": False,
            "error": (
                "No Google accounts linked to this speaker yet. "
                f"Visit {_setup_url()} to add one."
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
            f"Re-link at {_setup_url()}."
        ),
    }


def api_error(
    logger: logging.Logger,
    label: str,
    friendly_name: str,
    account_name: str,
    exc: Exception,
) -> dict:
    """Generic fallback for a googleapiclient HttpError or transport
    failure, shared by gmail.py and calendar.py. Logged with the full
    traceback for debugging; the model speaks the short version.
    `label` names the failing surface for the log line ('gmail',
    'calendar'); `friendly_name` is what the user hears ('Gmail',
    'Google Calendar')."""
    logger.warning(
        "%s API error for %s: %s", label, account_name, exc, exc_info=True,
    )
    return {
        "ok": False,
        "error": f"Couldn't reach {friendly_name} just now. Try again in a moment.",
    }
