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

What's NOT here: the supervisor task itself, provider-specific reconnect
handling (Gemini's 409 / 1008) and resumption-handle logic. Those stay in
`gemini_session.py` / `openai_session.py`.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import re
import socket
from typing import Callable

from ..log_event import log_event
from ..os_fault import root_os_error
from ..secret_redaction import redact_secrets
from .session import CuePlayer

logger = logging.getLogger(__name__)

OUT_OF_CREDIT_CUE_SLUG = "provider_out_of_credit"
NEEDS_ATTENTION_CUE_SLUG = "provider_needs_attention"
CANT_CONNECT_CUE_SLUG = "cant_connect"
NETWORK_DOWN_CUE_SLUG = "network_down"

# On the default 1/2/4/8 s ±25 % schedule (jasper/backoff.py) the fourth
# attempt lands ~15 s after the drop: past a Wi-Fi roam or a DHCP renew,
# still ahead of a rebooting router coming back.
NETWORK_DOWN_ATTEMPTS = 4

# The link itself is gone: name resolution failed, or no route to the
# provider. Everything else an unreachable host produces — refused,
# reset, timed out, TLS — is an ordinary blip and stays silent.
_NETWORK_DOWN_ERRNOS = frozenset({
    socket.EAI_AGAIN, socket.EAI_NONAME,
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
})

# asyncio folds a dual-stack connect that failed on every address into one
# errno-less OSError("Multiple exceptions: [Errno 101] ..., [Errno 101] ...").
_FOLDED_ERRNO = re.compile(r"\[Errno (-?\d+)\]")

# Markers that a rejection body blames the account's balance rather than its
# configuration: the xAI 403 reads "used all available credits or reached its
# monthly spending limit"; OpenAI sends `insufficient_quota`.
_OUT_OF_CREDIT_MARKERS = (
    "credit", "quota", "billing", "spending limit", "payment",
)

# RFC 6455 close codes that mean the server refused what we sent, not
# that the link failed: 1002 protocol error, 1003 unsupported data,
# 1007 invalid frame payload. A provider rejects a malformed `setup`
# message with one of these.
#
# 1008 (policy violation) is deliberately excluded: Gemini uses it for
# a benign, self-healing case, an expired session-resumption handle,
# which gemini_session.py already handles by dropping the handle and
# reconnecting.
_SETUP_REJECTION_CLOSE_CODES = frozenset({1002, 1003, 1007})

# Cap on the cause string stored and logged. Comfortably fits a provider's
# JSON error body; short enough that an HTML error page cannot flood the
# journal or /state.
FAILURE_DETAIL_LIMIT = 300

# How much of a body is worth scanning. Nothing past this can reach the
# truncated output, so bounding the scan cannot hide a credential from
# redaction — it only stops a multi-megabyte error page from costing
# proportional work and memory on a 1 GB Pi.
_SCAN_LIMIT = FAILURE_DETAIL_LIMIT * 4


def _rejection_text(exc: BaseException) -> str:
    """The provider's own reason, redacted and collapsed, untruncated.

    ``str(exc)`` discards the part that matters: websockets renders a
    refused handshake as a bare "server rejected WebSocket connection:
    HTTP 403" while ``.response.body`` holds the reason a household can
    act on. Prefer the body; fall back to ``str(exc)`` otherwise.

    Redaction precedes any truncation by a caller so a clipped tail
    cannot leave half a credential behind.
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
    return " ".join(redact_secrets(text[:_SCAN_LIMIT]).split())


def failure_detail(exc: BaseException) -> str:
    """A one-line, secret-free cause for logs and ``/state``."""
    text = _rejection_text(exc)
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


_NO_RCVD = object()


def provider_code(exc: BaseException) -> int | None:
    """The code a failure carries where ``http_status`` cannot see it.

    google-genai's ``APIError`` puts its code on ``.code`` (never
    ``.status_code``); a websockets ``ConnectionClosed`` puts the RFC 6455
    close code on ``.rcvd.code``. Its own ``.code`` is a deprecated
    backwards-compatibility property (websockets 13.1+): reading it emits
    a ``DeprecationWarning``, and when no close frame was received
    (``.rcvd`` is ``None``) it synthesizes 1006 instead of reporting
    absence. Never read it: any exception carrying an ``.rcvd`` attribute
    is treated as a websockets close, and its code comes from ``.rcvd``
    alone, even when that is ``None``."""
    rcvd = getattr(exc, "rcvd", _NO_RCVD)
    if rcvd is _NO_RCVD:
        candidate = getattr(exc, "code", None)
    else:
        candidate = getattr(rcvd, "code", None)
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return candidate
    return None


def is_transient(exc: BaseException) -> bool:
    """Whether retrying this failure can plausibly fix it.

    Transient: network errors, server 5xx, WebSocket resets, rate-limit
    bursts, 409 (race against a recently-closed prior session). Not
    transient means terminal — no amount of retrying helps and a human
    must act: a rejected key, an account out of credit, a missing model,
    a locally malformed config, a setup message the provider refuses.
    See ADR-0215."""
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
    code = provider_code(exc)
    if code is not None:
        if code in _SETUP_REJECTION_CLOSE_CODES:
            return False
        if 400 <= code < 500 and code != 429 and code != 409:
            return False
    # No status — treat as transient (network blip, WS reset, etc.).
    return True


def survive_terminal_initial_connect(
    exc: Exception, wake_supervisor: Callable[[], object],
) -> None:
    """Rule a failed first connect, from a provider's own except block.

    A terminal rejection (blocked account, revoked key) does NOT
    propagate: every provider's reconnect path already survives the same
    rejection indefinitely, and dying here instead crash-looped the unit
    into ``StartLimitAction=reboot``. Waking the supervisor leaves the
    daemon up — wake word, cues and local tools alive, ``/state``
    reporting the outage — until the provider accepts again.

    A budget-exhausted TRANSIENT failure re-raises: a fresh process gets
    a fresh budget, which is what that path is for."""
    if is_transient(exc):
        raise exc
    wake_supervisor()


def is_network_down(exc: BaseException) -> bool:
    """Whether this failure is the household's own link being down.

    A DNS or routing error carries no HTTP status, so nothing upstream
    can tell it apart from a provider blip — only its ``errno`` can. The
    remedy is the Wi-Fi rather than the account, so it earns a cue of
    its own even though retrying may still fix it. See ADR-0215."""
    fault = root_os_error(exc)
    if fault is None:
        return False
    if fault.errno is None:
        codes = {int(code) for code in _FOLDED_ERRNO.findall(str(fault))}
        return not codes.isdisjoint(_NETWORK_DOWN_ERRNOS)
    return fault.errno in _NETWORK_DOWN_ERRNOS


def outage_cue(exc: BaseException) -> str | None:
    """Which pre-baked cue names the remedy for this failure.

    ``None`` while retrying can still fix it, except for a network-down
    failure: that keeps retrying and still names a remedy. Otherwise the
    provider's own rejection text decides between "out of credit" and
    the catch-all "needs attention". See ADR-0215."""
    if is_network_down(exc):
        return NETWORK_DOWN_CUE_SLUG
    if is_transient(exc):
        return None
    lowered = _rejection_text(exc).lower()
    if any(marker in lowered for marker in _OUT_OF_CREDIT_MARKERS):
        return OUT_OF_CREDIT_CUE_SLUG
    return NEEDS_ATTENTION_CUE_SLUG


class OutageTracker:
    """Track the current outage for a connection.

    Holds its redacted cause for logs and ``/state``, and the cue that
    names the remedy. The announcement is edge-triggered: the cue fires
    on the first terminal failure after the connection last worked,
    never on a timer, a recovery re-arms it silently, and a remedy that
    changes mid-outage is announced again. The network-down cue alone
    waits for ``NETWORK_DOWN_ATTEMPTS`` consecutive failures, so a
    dropped link that comes back never speaks; ``cue`` is current from
    the first one either way, because the wake path reads it. One
    instance per connection. See ADR-0215."""

    def __init__(self) -> None:
        self.detail: str | None = None
        self.cue: str | None = None
        self._announced: str | None = None
        self._network_streak = 0
        self._held_cue = Deferred()
        self._cb: CuePlayer | None = None

    @property
    def wake_cue(self) -> str:
        """The cue to play for a wake while the connection is paused: the
        remedy if this outage is terminal, else the generic connection
        voice."""
        return self.cue or CANT_CONNECT_CUE_SLUG

    def set_callback(self, cb: CuePlayer | None) -> None:
        """None keeps the edge and its log line but plays nothing.

        The daemon can only wire this once the object that owns cue
        playback exists, which is after the connection's ``start()`` —
        so an outage that began on the very first connect has already
        claimed its edge. Play what it held back."""
        self._cb = cb
        if cb is not None:
            self._held_cue.fire_if_pending(self._announce)

    def _announce(self) -> None:
        cue = self.cue
        if cue is None:
            # The outage ended (or turned transient) before a player
            # existed: there is nothing left to announce.
            self._held_cue.clear()
            return
        if self._cb is None:
            self._held_cue.request()
            return
        self._held_cue.clear()
        # Fire-and-forget: cue playback must not stall the reconnect
        # cadence.
        asyncio.create_task(
            self._cb(cue),
            name="jasper-supervisor-escalation-cue",
        )

    def on_failure(self, exc: BaseException) -> None:
        """Record a failed session open, announcing a terminal one once."""
        self.detail = failure_detail(exc)
        # Latest failure wins, so a transient failure after a terminal
        # one reads as transient again.
        cue = outage_cue(exc)
        self.cue = cue
        if cue == NETWORK_DOWN_CUE_SLUG:
            self._network_streak += 1
        else:
            self._network_streak = 0
        if cue is None or cue == self._announced:
            return
        if self._network_streak and self._network_streak < NETWORK_DOWN_ATTEMPTS:
            return
        self._announced = cue
        log_event(
            logger,
            "voice.connection.outage",
            cue=cue,
            exc=type(exc).__name__,
            level=logging.WARNING,
        )
        self._announce()

    def on_recovery(self) -> None:
        """A session opened: clear the outage and re-arm, silently."""
        self.detail = None
        self.cue = None
        self._announced = None
        self._network_streak = 0
        self._held_cue.clear()


class Deferred:
    """One action held back until the moment it may happen.

    Three sites need the same dance — request → hold → fire-on-release,
    plus clear-when-it-no-longer-applies so a later release can't fire a
    spurious second time:

      * OpenAI: a mid-turn reconnect (the pre-cap watchdog fires inside
        the buffer before the 60-minute hard cap); tearing the socket
        down mid-reply would cut the user off mid-sentence.
      * Gemini: the same, triggered by a ``GoAway`` with ample
        ``time_left``.
      * ``OutageTracker``: an escalation cue raised before the daemon
        wired a cue player.

    The flag is cleared BEFORE ``fire`` runs, so a re-entrant release
    cannot double-fire."""

    def __init__(self) -> None:
        self._pending = False

    @property
    def pending(self) -> bool:
        """Whether an action is currently held."""
        return self._pending

    def request(self) -> None:
        """Hold the action until something releases it."""
        self._pending = True

    def clear(self) -> None:
        """Drop any held request without firing it."""
        self._pending = False

    def fire_if_pending(self, fire: Callable[[], object]) -> bool:
        """Fire the held action, if one is held. Returns whether it
        fired, so the caller can log."""
        if not self._pending:
            return False
        self._pending = False
        fire()
        return True
