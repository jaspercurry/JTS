# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The voice connection reconnect supervisor, shared by every provider.

Every `LiveConnection` implementation drives the same loop from here:
the supervisor task, its reconnect run with backoff, transient/terminal
exception classification and the once-per-outage escalation
announcement. The generic retry schedule lives in :mod:`jasper.backoff`
so non-voice subsystems do not depend on this private module.

Providers differ only in what a failed attempt costs them in session
state, and that difference goes through
`SupervisedConnection._on_reconnect_attempt_failed`.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import re
import socket
from typing import Any, Awaitable, Callable, Protocol

from ..backoff import reconnect_delay, sleep_or_nudge
from ..log_event import log_event
from ..os_fault import root_os_error
from ..secret_redaction import redact_secrets
from .session import ConnectionState, CuePlayer

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

# RFC 6455 close codes that mean the peer refused what we sent, not that
# the link failed: 1002 protocol error, 1003 unsupported data, 1007
# invalid frame payload. A provider rejects a malformed `setup` message
# with one of these — but `is_transient` also runs on every mid-session
# reconnect failure, so anything added here rules those terminal too.
#
# 1008 (policy violation) is deliberately excluded: Gemini uses it for
# a benign, self-healing case, an expired session-resumption handle,
# which gemini_session.py already handles by dropping the handle and
# reconnecting.
_PROVIDER_REJECTION_CLOSE_CODES = frozenset({1002, 1003, 1007})

# Cap on the cause string stored and logged. Comfortably fits a provider's
# JSON error body; short enough that an HTML error page cannot flood the
# journal or /state.
FAILURE_DETAIL_LIMIT = 300

# How much of a body is worth scanning. Nothing past this can reach the
# truncated output, so bounding the scan cannot hide a credential from
# redaction — it only stops a multi-megabyte error page from costing
# proportional work and memory on a 1 GB Pi.
_SCAN_LIMIT = FAILURE_DETAIL_LIMIT * 4


def _rejection_text(exc: BaseException, *, literals: tuple[str, ...] = ()) -> str:
    """The provider's own reason, redacted and collapsed, untruncated.

    ``str(exc)`` discards the part that matters: websockets renders a
    refused handshake as a bare "server rejected WebSocket connection:
    HTTP 403" while ``.response.body`` holds the reason a household can
    act on. Prefer the body; fall back to ``str(exc)`` otherwise.

    ``literals`` are secret values the caller holds (its own API key,
    say) that may not match `redact_secrets`'s generic prefix patterns —
    see ADR-0243.

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
    redacted = redact_secrets(text[:_SCAN_LIMIT], literals=literals)
    return " ".join(redacted.split())


def failure_detail(exc: BaseException, *, literals: tuple[str, ...] = ()) -> str:
    """A one-line, secret-free cause for logs and ``/state``."""
    text = _rejection_text(exc, literals=literals)
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


def peer_initiated_close(exc: BaseException) -> bool:
    """Whether the peer, not us, sent the close frame carrying the code.

    RFC 6455 makes the receiver echo a close code back, so a code our own
    client chose — 1007 for a truncated text frame from the provider's
    edge, say — arrives on ``.rcvd`` indistinguishable from a rejection.
    websockets records who moved first in ``.rcvd_then_sent``; ``False``
    means we did. Any other value, the flag being absent included,
    cannot rule the close ours."""
    return getattr(exc, "rcvd_then_sent", None) is not False


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
        if code in _PROVIDER_REJECTION_CLOSE_CODES and peer_initiated_close(exc):
            return False
        if 400 <= code < 500 and code != 429 and code != 409:
            return False
    # No status — treat as transient (network blip, WS reset, etc.).
    return True


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
        self._cb: CuePlayer | None = None
        # Strong refs for the fire-and-forget cue tasks `_announce` spawns:
        # asyncio only holds a weak reference, so an uncollected task could
        # otherwise be garbage-collected mid-flight.
        self._tasks: set[asyncio.Task] = set()

    @property
    def wake_cue(self) -> str:
        """The cue to play for a wake while the connection is paused: the
        remedy if this outage is terminal, else the generic connection
        voice."""
        return self.cue or CANT_CONNECT_CUE_SLUG

    def set_callback(self, cb: CuePlayer | None) -> None:
        """None keeps the edge and its log line but plays nothing."""
        self._cb = cb

    def _announce(self, cue: str) -> None:
        if self._cb is None:
            return
        # Fire-and-forget: cue playback must not stall the reconnect
        # cadence. Kept in `_tasks` (see __init__) and given a done
        # callback so a failure is observable instead of an "exception was
        # never retrieved" warning at GC time.
        task = asyncio.create_task(
            self._cb(cue),
            name="jasper-supervisor-escalation-cue",
        )
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._on_cue_task_done(done, cue))

    def _on_cue_task_done(self, task: asyncio.Task, cue: str) -> None:
        self._tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log_event(
                logger,
                "voice.supervisor.cue_failed",
                cue=cue,
                exc_type=type(exc).__name__,
                level=logging.WARNING,
            )

    def on_failure(
        self, exc: BaseException, *, literals: tuple[str, ...] = (),
    ) -> None:
        """Record a failed session open, announcing a terminal one once.

        ``literals`` are secret values the connection holds that
        `redact_secrets`'s generic patterns may not recognise — passed
        through to `failure_detail` before this reaches ``/state``.
        """
        self.detail = failure_detail(exc, literals=literals)
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
        self._announce(cue)

    def on_recovery(self) -> None:
        """A session opened: clear the outage and re-arm, silently."""
        self.detail = None
        self.cue = None
        self._announced = None
        self._network_streak = 0


class Deferred:
    """One action held back until the moment it may happen.

    Two sites need the same dance — request → hold → fire-on-release,
    plus clear-when-it-no-longer-applies so a later release can't fire a
    spurious second time:

      * OpenAI: a mid-turn reconnect (the pre-cap watchdog fires inside
        the buffer before the 60-minute hard cap); tearing the socket
        down mid-reply would cut the user off mid-sentence.
      * Gemini: the same, triggered either by a planned session
        rotation or by a ``GoAway`` with ample ``time_left``.

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


class SupervisedConnection(Protocol):
    """What the shared reconnect supervisor reads from a connection.

    Every member already exists on each provider connection; naming
    them here is what lets the loop below be type-checked."""

    PROVIDER_NAME: str
    # Prefix for this provider's human-readable log lines, e.g.
    # "openai connection:".
    _log_tag: str

    _state: ConnectionState
    _state_lock: asyncio.Lock
    _turn_lock: asyncio.Lock
    _active_turn: Any
    _stopping: asyncio.Event
    _reconnect_event: asyncio.Event
    # Set on every successful open, cleared while a reopen is in flight.
    _connected_event: asyncio.Event
    _nudge_event: asyncio.Event
    _deferred_reconnect: Deferred
    # Raised by a provider that schedules its own session rotation: the
    # reconnect it asks for is not a failure. `run_reconnect_with_backoff`
    # spends the flag.
    _planned_rotate: bool
    # None in production (retry forever); a bounded tuple in tests, to
    # make schedule exhaustion observable.
    _backoff_schedule: tuple[float, ...] | None
    _sleep: Callable[[float], Awaitable[None]]

    def _set_state(self, new_state: ConnectionState) -> None: ...

    async def _teardown_session(self) -> None: ...

    async def _open_session(self) -> None: ...

    def _on_reconnect_attempt_failed(
        self, exc: Exception, attempt: int, transient: bool,
    ) -> None:
        """One failed reopen: log the provider's own diagnosis, and drop
        whatever session state the failure may have invalidated so the
        next attempt starts from something the provider will accept."""
        ...


async def await_connected(conn: SupervisedConnection) -> None:
    """Wait for an open session so a turn never opens against a
    half-open WS.

    Bounded by one backoff window. The bound is a backstop rather than
    the normal user-facing wait: raising on it is what puts a
    connection that never comes back on the caller's failure path,
    where the daemon plays a cue for a paused connection, instead of
    hanging the wake."""
    if conn._connected_event.is_set():
        return
    schedule = conn._backoff_schedule
    timeout = (
        sum(schedule) + 5.0
        if schedule is not None
        else 15.0  # production: long enough for one full backoff cycle
    )
    try:
        await asyncio.wait_for(conn._connected_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{conn._log_tag} not connected after backoff window")


def request_planned_reopen(conn: SupervisedConnection) -> None:
    """Ask the supervisor for a fresh session on a healthy connection.

    One reopener per connection slot: the caller closes the connected
    gate and hands the reopen over rather than opening a session of its
    own beside the supervisor's. `_planned_rotate` is what spares the
    first attempt a backoff wait; `run_reconnect_with_backoff` spends
    it."""
    conn._connected_event.clear()
    conn._planned_rotate = True
    conn._reconnect_event.set()


def request_unplanned_reopen(conn: SupervisedConnection) -> None:
    """Ask the supervisor to reopen after a failure.

    Spends any queued planned-rotation flag, so a genuine failure never
    inherits the rotation's zero-backoff first attempt."""
    conn._planned_rotate = False
    conn._reconnect_event.set()


def hand_off_first_connect(
    conn: SupervisedConnection, exc: Exception, *, literals: tuple[str, ...] = (),
) -> None:
    """Give up a failed first connect to the reconnect supervisor.

    A first connect fails for the reasons a reconnect does and is
    retried the same way, so nothing here classifies it or exits: the
    daemon stays up — wake word, cues and local tools alive, ``/state``
    reporting the outage — until the provider answers. See ADR-0238.

    ``literals`` isn't on the ``SupervisedConnection`` Protocol — the
    caller passes its own ``_secret_literals()`` rather than this
    function reaching for it, so the Protocol stays narrow."""
    log_event(
        logger,
        "voice.initial_connect.failed",
        provider=conn.PROVIDER_NAME,
        exc=type(exc).__name__,
        reason=failure_detail(exc, literals=literals),
        level=logging.WARNING,
    )
    request_unplanned_reopen(conn)


async def run_supervisor_loop(conn: SupervisedConnection) -> None:
    """Reconnect for the connection's lifetime.

    Wakes on `_reconnect_event`, which the receive loop sets when it
    observes a drop, a GoAway or an unexpected exception, and which the
    proactive/rotate watchdogs set for a planned reopen."""
    while not conn._stopping.is_set():
        await conn._reconnect_event.wait()
        if conn._stopping.is_set():
            return
        # Cleared before the work so a signal during reopen survives. See #3915.
        conn._reconnect_event.clear()
        log_event(
            logger,
            "voice.supervisor.wake",
            provider=conn.PROVIDER_NAME,
            state=conn._state.value,
            # Read before the run below spends it: tells a reopen the
            # connection asked for (rotation, context reset) apart from
            # one a drop forced.
            planned=conn._planned_rotate,
        )
        await run_reconnect_with_backoff(conn)
        log_event(
            logger,
            "voice.supervisor.wait",
            provider=conn.PROVIDER_NAME,
            state=conn._state.value,
        )


async def run_reconnect_with_backoff(conn: SupervisedConnection) -> None:
    """Tear the session down, then reopen until it takes.

    A raised `_planned_rotate` is spent here: that reconnect is the
    connection's own scheduled rotation rather than a failure, so its
    first attempt waits for nothing. Every attempt after it backs off."""
    planned_rotate = conn._planned_rotate
    conn._planned_rotate = False
    async with conn._state_lock:
        conn._set_state(ConnectionState.RECONNECTING)
    # Tear down the old session before opening a new one so we don't
    # leak a half-open WS through the SDK.
    await conn._teardown_session()
    # This reconnect subsumes any deferred one; clear the flag so a
    # later turn release doesn't fire a spurious second reconnect.
    conn._deferred_reconnect.clear()
    # Mark the active turn (if any) as lost AND detach it. The daemon's
    # idle watchdog will pick up `turn_lost()` and call `release()`, but
    # in the meantime the connection's slot is free — clearing
    # `_active_turn` lets a wake event after reconnect acquire a fresh
    # turn rather than getting "a turn is already active" while the old
    # one is still being torn down.
    if conn._active_turn is not None:
        conn._active_turn._on_connection_lost()
        async with conn._turn_lock:
            conn._active_turn = None

    schedule = conn._backoff_schedule
    bounded = schedule is not None
    last_exc: Exception | None = None
    # Seeds the first delay; the previous failure's classification picks
    # every one after that.
    last_transient = True
    conn._nudge_event.clear()
    attempt = 0
    while not conn._stopping.is_set():
        attempt += 1
        if schedule is not None and attempt > len(schedule):
            break
        if attempt == 1 and planned_rotate:
            delay = 0.0
        elif schedule is not None:
            delay = schedule[attempt - 1]
        else:
            delay = reconnect_delay(attempt, transient=last_transient)
        async with conn._state_lock:
            conn._set_state(ConnectionState.PAUSED_FOR_BACKOFF)
        logger.info(
            "%s reconnect attempt %d after %.1fs backoff",
            conn._log_tag, attempt, delay,
        )
        # A bare sleep would be uninterruptible, so the 15-minute
        # terminal poll would ignore `request_reconnect_now` for up to
        # 15 minutes. `stop()` cancels the supervisor task, which
        # unwinds through here and cancels both waiters.
        await sleep_or_nudge(delay, conn._nudge_event, sleep=conn._sleep)
        if conn._stopping.is_set():
            return
        # This attempt answers every nudge raised so far, including any
        # raised during the previous attempt. Clearing here (not inside
        # the wait) is what keeps those from being discarded.
        conn._nudge_event.clear()
        try:
            await conn._open_session()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            transient = is_transient(e)
            conn._on_reconnect_attempt_failed(e, attempt, transient)
            if transient and not last_transient and not bounded:
                # The provider stopped rejecting us outright and is only
                # failing normally now, so it is recovering. Restart the
                # ramp at 1 s instead of resuming wherever the slow poll
                # left the counter. Bounded (test) schedules index by
                # `attempt`, so resetting one would replay the schedule
                # forever.
                attempt = 0
            last_transient = transient
            continue
        if attempt > 1:
            log_event(
                logger,
                "voice.supervisor.reconnected",
                provider=conn.PROVIDER_NAME,
                attempt=attempt,
            )
        return

    # Only reached when (a) a bounded test schedule was exhausted, or
    # (b) the daemon is stopping. Production never reaches this — the
    # loop iterates forever until success.
    if bounded and not conn._stopping.is_set():
        async with conn._state_lock:
            conn._set_state(ConnectionState.FAILED)
        logger.error(
            "%s bounded test schedule exhausted after %d retries. "
            "Last error: %s", conn._log_tag, attempt - 1, last_exc,
        )
