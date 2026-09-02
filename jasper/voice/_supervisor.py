# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-agnostic helpers for the voice connection reconnect supervisor.

Each `LiveConnection` implementation runs its own reconnect supervisor
because the recovery details differ (Gemini drops a resumption handle on
1008; OpenAI just reopens the WebSocket). Voice-specific retry-loop
primitives — transient/terminal exception classification and the
once-per-outage escalation announcement — live here so behaviour stays
consistent across providers. The generic retry schedule lives in
:mod:`jasper.backoff` so non-voice subsystems do not depend on this
private module.

What's NOT here: the supervisor task itself, provider-specific close-code
handling (Gemini's 409 / 1008) and resumption-handle logic. Those stay in
`gemini_session.py` / `openai_session.py`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..log_event import log_event
from ..secret_redaction import redact_secrets
from .session import CuePlayer

logger = logging.getLogger(__name__)

ESCALATION_CUE_SLUG = "cant_reach_cloud"

# Cap on the cause string stored and logged. Comfortably fits a provider's
# JSON error body; short enough that an HTML error page cannot flood the
# journal or /state.
FAILURE_DETAIL_LIMIT = 300

# How much of a body is worth scanning. Nothing past this can reach the
# truncated output, so bounding the scan cannot hide a credential from
# redaction — it only stops a multi-megabyte error page from costing
# proportional work and memory on a 1 GB Pi.
_SCAN_LIMIT = FAILURE_DETAIL_LIMIT * 4


def failure_detail(exc: BaseException) -> str:
    """A one-line, secret-free cause for logs and ``/state``.

    ``str(exc)`` discards the part that matters: websockets renders a
    refused handshake as a bare "server rejected WebSocket connection:
    HTTP 403" while ``.response.body`` holds the reason a household can
    act on. Prefer the body; fall back to ``str(exc)`` otherwise.

    Redaction precedes truncation so a clipped tail cannot leave half a
    credential behind.
    """
    response = getattr(exc, "response", None)
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body[:_SCAN_LIMIT]).decode("utf-8", "replace")
    if isinstance(body, str) and body.strip():
        status = getattr(response, "status_code", None)
        text = f"HTTP {status}: {body}" if status else body
    else:
        text = str(exc)
    text = " ".join(redact_secrets(text[:_SCAN_LIMIT]).split())
    if len(text) > FAILURE_DETAIL_LIMIT:
        text = text[:FAILURE_DETAIL_LIMIT - 3] + "..."
    return text


def http_status(exc: BaseException) -> int | None:
    """The HTTP status a rejected handshake carries, if any.

    websockets puts it on ``.status_code``; httpx-style SDK errors on
    ``.response.status_code``."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


def is_transient(exc: BaseException) -> bool:
    """Whether retrying this failure can plausibly fix it.

    Transient: network errors, server 5xx, WebSocket resets, rate-limit
    bursts, 409 (race against a recently-closed prior session). Not
    transient means terminal — no amount of retrying helps and a human
    must act: a rejected key, an account out of credit, a missing model,
    a locally malformed config. See ADR-0215."""
    # Local-validation errors — never retry.
    if isinstance(exc, (TypeError, ValueError, ImportError, AttributeError)):
        return False
    status = http_status(exc)
    if status is not None:
        if status in (401, 403, 404):
            return False
        if 400 <= status < 500 and status != 429 and status != 409:
            return False
        return True
    # No status — treat as transient (network blip, WS reset, etc.).
    return True


class OutageTracker:
    """Track the current outage for a connection.

    Holds its redacted cause for logs and ``/state``, and whether it has
    been announced. The announcement is edge-triggered: the cue fires on
    the first terminal failure after the connection last worked, never on
    a timer, and a recovery re-arms it silently. One instance per
    connection. See ADR-0215."""

    def __init__(self) -> None:
        self.detail: str | None = None
        self._announced = False
        self._held = False
        self._cb: CuePlayer | None = None

    def set_callback(self, cb: CuePlayer | None) -> None:
        """None keeps the edge and its log line but plays nothing.

        The daemon can only wire this once the object that owns cue
        playback exists, which is after the connection's ``start()`` —
        so an outage that began on the very first connect has already
        claimed its edge. Play what it held back."""
        self._cb = cb
        if cb is not None and self._held:
            self._announce()

    def _announce(self) -> None:
        if self._cb is None:
            self._held = True
            return
        self._held = False
        # Fire-and-forget: cue playback must not stall the reconnect
        # cadence.
        asyncio.create_task(
            self._cb(ESCALATION_CUE_SLUG),
            name="jasper-supervisor-escalation-cue",
        )

    def on_failure(self, exc: BaseException) -> None:
        """Record a failed session open, announcing a terminal one once."""
        self.detail = failure_detail(exc)
        if is_transient(exc) or self._announced:
            return
        self._announced = True
        log_event(
            logger,
            "voice.connection.terminal_failure",
            cue=ESCALATION_CUE_SLUG,
            exc=type(exc).__name__,
            level=logging.WARNING,
        )
        self._announce()

    def on_recovery(self) -> None:
        """A session opened: clear the outage and re-arm, silently."""
        self.detail = None
        self._announced = False
        self._held = False


class DeferredReconnect:
    """Defer a mid-turn reconnect until the in-flight turn releases.

    Both real-time providers need to reconnect *during* a session but
    must not tear down the WebSocket while a turn is actively replying —
    doing so cuts the user off mid-sentence. The shared MECHANISM is a
    pending flag set by some trigger and fired from ``_on_turn_released``
    once the turn ends. Only the TRIGGER differs per provider:

      * OpenAI: the proactive pre-cap watchdog fires inside the 5-minute
        buffer before the 60-minute hard cap.
      * Gemini: a ``GoAway`` lands mid-turn with ample ``time_left``.

    This class owns the flag and its lifecycle so a future fourth
    provider reuses it instead of re-deriving the same three-state dance
    (request → defer → fire-on-release, plus clear-when-reconnect-starts
    so a later turn release can't fire a spurious second reconnect)."""

    def __init__(self) -> None:
        self._pending = False

    @property
    def pending(self) -> bool:
        """Whether a reconnect is currently deferred."""
        return self._pending

    def request(self) -> None:
        """A trigger fired mid-turn: mark a reconnect as deferred."""
        self._pending = True

    def clear(self) -> None:
        """A reconnect is now underway: drop any deferred request so a
        later turn release cannot fire a spurious second reconnect."""
        self._pending = False

    def fire_if_pending(self, fire: Callable[[], object]) -> bool:
        """Fire a deferred reconnect, if one is pending.

        Called from ``_on_turn_released``. If a reconnect was deferred,
        clear the flag, invoke ``fire`` (which sets the connection's
        reconnect event), and return ``True`` so the caller can log. The
        flag is cleared BEFORE ``fire`` so a re-entrant turn release
        can't double-fire. Returns ``False`` (and does nothing) when no
        reconnect is pending."""
        if not self._pending:
            return False
        self._pending = False
        fire()
        return True
