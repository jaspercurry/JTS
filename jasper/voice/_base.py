# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The skeleton both live-voice adapters subclass.

`BaseLiveTurn` and `BaseLiveConnection` hold what the OpenAI Realtime and
Gemini Live adapters do identically: the per-turn playout queue and its
counters, the connection state machine, the initial-connect hand-off, one
pre-emptive reconnect watchdog, one receive-loop exit. A subclass adds
only wire logic — how its provider frames audio, tools and turn
boundaries.

Lines logged from here go to the subclass's own `_logger` and carry its
`_log_tag`, so a journal line still names the provider that produced it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from typing import Any, AsyncIterator, Awaitable, Callable

from jasper.backoff import ReconnectNudge
from jasper.log_event import log_event

from ..tools import ToolRegistry
from ._supervisor import (
    Deferred,
    OutageTracker,
    await_connected,
    hand_off_first_connect,
    provider_code,
    request_planned_reopen,
    request_unplanned_reopen,
    run_supervisor_loop,
)
from .session import (
    CONNECTION_NOISY_TRANSITIONS,
    AudioOutChunk,
    ConnectionState,
    CuePlayer,
)

logger = logging.getLogger(__name__)

# Ceiling on waiting out one cancelled background task. Long enough for
# an unwind that is mid-await, short enough that a task which swallows
# cancellation cannot hang the daemon's teardown.
TASK_CANCEL_TIMEOUT_SEC = 3.0

# Ceiling on one close handshake. Long enough for the server to finish
# tearing the session down before the next connect opens a socket
# against it (suspected in Gemini's 409s), short enough that a
# misbehaving close cannot hang the daemon. The whole teardown is
# bounded in turn by the unit's TimeoutStopSec.
SESSION_CLOSE_TIMEOUT_SEC = 3.0

# A watchdog that fires in one of these has nothing left to do: a
# reconnect is already under way, or the connection is going down.
_WATCHDOG_MOOT_STATES = frozenset({
    ConnectionState.RECONNECTING,
    ConnectionState.PAUSED_FOR_BACKOFF,
    ConnectionState.FAILED,
    ConnectionState.CLOSED,
})

# RFC 6455 close codes. A provider error can carry an HTTP status on the
# same attribute, so only this range reports as a close code.
_WS_CLOSE_CODES = range(1000, 5000)


def close_code_and_reason(exc: BaseException) -> tuple[int | None, str | None]:
    """The WebSocket close code and reason a receive-loop failure carries.

    Two shapes reach here: `websockets` raises with the frame on
    `.rcvd`, while the genai SDK consumes the frame and re-raises an
    `APIError` keeping the code on `.code` and the reason on `.message`.
    `_supervisor.provider_code` owns reading the code out of either —
    including why `.code` is never read off a websockets exception. This
    only separates a real close code from an HTTP status sharing the
    attribute, and pairs it with its reason.
    """
    code = provider_code(exc)
    if code is None or code not in _WS_CLOSE_CODES:
        return None, None
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        return code, getattr(rcvd, "reason", None)
    return code, getattr(exc, "message", None)


class BaseLiveTurn:
    """The per-turn state every provider's turn keeps identically.

    `jasper.voice.session.LiveTurn` documents what each member means to
    the daemon; this only implements the provider-independent half.
    """

    def __init__(self, conn: "BaseLiveConnection", started_at: float) -> None:
        self._conn = conn
        self._audio_q: asyncio.Queue[AudioOutChunk | None] = asyncio.Queue()
        self._interrupt_event = asyncio.Event()
        # Loop time (asyncio) of the last model activity of any kind, and
        # of the last audio chunk specifically.
        self._last_activity_at: float = started_at
        self._last_chunk_at: float = 0.0
        self._first_chunk_logged = False
        # Monotonic anchors for elapsed-ms lines. `acquire_turn`
        # overwrites the start so it lines up with the turn's first wire
        # send; 0.0 end-input means the model was never asked to respond.
        self._started_at_monotonic: float = _time.monotonic()
        self._end_input_at_monotonic: float = 0.0
        self._bytes_sent: int = 0
        self._chunks_received: int = 0
        self._released = False
        self._turn_lost = False
        self._server_turn_complete = False

    async def audio_out(self) -> AsyncIterator[bytes]:
        async for chunk in self.audio_out_chunks():
            yield chunk.pcm

    async def audio_out_chunks(self) -> AsyncIterator[AudioOutChunk]:
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                return
            if isinstance(chunk, bytes):
                chunk = AudioOutChunk(pcm=chunk)
            yield chunk

    def last_activity_at(self) -> float:
        return self._last_activity_at

    def last_chunk_at(self) -> float:
        return self._last_chunk_at

    def server_turn_complete(self) -> bool:
        return self._server_turn_complete

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def turn_lost(self) -> bool:
        return self._turn_lost

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupt_event.clear()

    def request_local_interrupt(self) -> None:
        self._interrupt_event.set()

    def drop_pending_audio(self) -> int:
        dropped = 0
        try:
            while True:
                item = self._audio_q.get_nowait()
                if item is None:
                    # Preserve the terminal sentinel so the consumer
                    # still ends the turn.
                    self._audio_q.put_nowait(None)
                    break
                dropped += 1
        except asyncio.QueueEmpty:
            pass
        return dropped

    def _note_activity(self) -> None:
        """Reset the pre-response idle anchor.

        Called on intermediate server events — a tool call arriving, one
        tool of a round finishing, the tool response going out — where
        the model is working but no audio has arrived yet. Without it the
        daemon's idle watchdog measures across the whole dispatch and
        fires mid-flight. The audio-chunk path does NOT call this: chunks
        are hot and already read the loop clock inline.
        """
        self._last_activity_at = asyncio.get_event_loop().time()

    def _on_connection_lost(self) -> None:
        """The WebSocket dropped while this turn was active."""
        if self._released or self._turn_lost:
            return
        self._turn_lost = True
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_q.put_nowait(None)


class BaseLiveConnection:
    """The connection lifecycle every provider drives identically.

    `jasper.voice.session.LiveConnection` documents the daemon-facing
    surface; `_supervisor.SupervisedConnection` names what the shared
    reconnect loop reads off the members below.
    """

    PROVIDER_NAME: str = ""
    # Prefix for this provider's human-readable log lines, e.g. "openai
    # connection:".
    _log_tag: str = ""
    # The subclass's module logger, so a line raised from a shared method
    # still lands under the provider's own logger name.
    _logger: logging.Logger = logger
    # Raised by a provider that schedules its own session rotation: the
    # reconnect it asks for is not a failure and skips the first backoff
    # wait. `run_reconnect_with_backoff` spends the flag.
    _planned_rotate: bool = False
    # Whether this provider's watchdog reconnect is a rotation it chose
    # (Gemini) rather than a server cap it is pre-empting (OpenAI).
    _watchdog_is_planned: bool = False

    def __init__(
        self,
        *,
        model: str,
        voice: str,
        context_reset_sec: float = 0.0,
        backoff_schedule: tuple[float, ...] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        nudge_clock: Callable[[], float] | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._context_reset_sec = context_reset_sec
        # None in production (retry forever); a bounded tuple in tests,
        # to make schedule exhaustion observable.
        self._backoff_schedule = backoff_schedule
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )

        self._registry: ToolRegistry | None = None
        # Called on every (re)connect so dynamic content — the current
        # local time — stays fresh across a connection that lives hours.
        self._system_instruction_provider: Callable[[], str] | None = None

        # Set directly rather than through `_set_state`, which needs the
        # field to exist already.
        self._state = ConnectionState.IDLE_INIT
        self._state_lock = asyncio.Lock()
        # CONNECTED ↔ IN_TURN cycles on every wake and floods the journal
        # at INFO; everything else is rare and worth a line.
        self._noisy_transitions = CONNECTION_NOISY_TRANSITIONS

        # One turn in flight at a time — the daemon's WakeLoop serialises
        # wakes.
        self._active_turn: Any = None
        self._turn_lock = asyncio.Lock()
        # Loop time of the last completed turn, for the idle context reset.
        self._last_turn_end_at: float = 0.0

        self._receive_task: asyncio.Task | None = None
        self._proactive_watchdog_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # "The session dropped" (wakes the supervisor) vs "shorten the
        # in-flight backoff wait".
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._nudge_event: asyncio.Event = asyncio.Event()
        # Set on every successful open, cleared while a reopen is in
        # flight; pauses turn acquisition against a half-open socket.
        self._connected_event: asyncio.Event = asyncio.Event()
        # A reconnect that came due mid-turn, fired from
        # `_on_turn_released` so an in-flight reply is not cut off.
        self._deferred_reconnect = Deferred()

        self._outage = OutageTracker()
        # Rate gate for `request_reconnect_now`.
        self._reconnect_nudge = ReconnectNudge(
            clock=nudge_clock if nudge_clock is not None else _time.monotonic,
        )

    # ------------------------------------------------------------------
    # LiveConnection protocol
    # ------------------------------------------------------------------

    async def start(
        self,
        registry: ToolRegistry,
        system_instruction: "str | Callable[[], str]",
    ) -> None:
        self._registry = registry
        if callable(system_instruction):
            self._system_instruction_provider = system_instruction
        else:
            instruction = system_instruction or ""
            self._system_instruction_provider = lambda: instruction
        await self._do_initial_connect()
        self._supervisor_task = asyncio.create_task(run_supervisor_loop(self))

    async def stop(self) -> None:
        if self._state is ConnectionState.CLOSED:
            return
        self._stopping.set()
        # Cancel every background task first so none of them fights the
        # teardown, then collect them.
        tasks = (
            self._supervisor_task,
            self._proactive_watchdog_task,
            self._receive_task,
        )
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            await self._cancel_task(task)
        self._supervisor_task = None
        self._proactive_watchdog_task = None
        self._receive_task = None
        await self._teardown_session()
        if self._active_turn is not None:
            self._active_turn._on_connection_lost()
            self._active_turn = None
        async with self._state_lock:
            self._set_state(ConnectionState.CLOSED)

    def is_paused(self) -> bool:
        return self._state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.PAUSED_FOR_BACKOFF,
            ConnectionState.FAILED,
        )

    def last_failure_detail(self) -> str | None:
        return self._outage.detail

    def wake_cue(self) -> str:
        return self._outage.wake_cue

    def request_reconnect_now(self) -> bool:
        if not self.is_paused():
            return False
        if not self._reconnect_nudge.allow():
            return False
        self._nudge_event.set()
        return True

    def set_failure_escalation_cb(self, cb: CuePlayer | None) -> None:
        self._outage.set_callback(cb)

    # ------------------------------------------------------------------
    # Internal — state and lifecycle
    # ------------------------------------------------------------------

    def _set_state(self, new_state: ConnectionState) -> None:
        """Update the state field and log the transition — nothing else.

        The connection is never re-initialised from here.
        """
        old = self._state
        if old is new_state:
            return
        self._state = new_state
        if (old, new_state) not in self._noisy_transitions:
            self._logger.info(
                "%s state %s → %s", self._log_tag, old.value, new_state.value,
            )

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open_session()
        except Exception as e:  # noqa: BLE001
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            hand_off_first_connect(self, e)

    async def _open_session(self) -> None:
        """Open a session, recording the outcome on the outage tracker.

        Every session open funnels through here, so the tracker follows
        the live connection by construction."""
        try:
            await self._open_session_attempt()
        except Exception as e:  # noqa: BLE001
            self._outage.on_failure(e)
            raise
        self._outage.on_recovery()

    async def _maybe_reset_context(self) -> None:
        """Reopen when the connection has been idle past the threshold.

        Opt-in (`context_reset_sec=0` disables it) and deliberately
        costly: the reopen discards the provider's warm session state and
        blocks the wake event that triggered it, so only a threshold in
        hours makes sense. Skipped until at least one turn has ended.
        """
        if self._context_reset_sec <= 0:
            return
        if self._last_turn_end_at <= 0.0:
            return
        idle_for = asyncio.get_event_loop().time() - self._last_turn_end_at
        if idle_for < self._context_reset_sec:
            return
        log_event(
            self._logger,
            "voice.context_reset",
            provider=self.PROVIDER_NAME,
            idle_sec=round(idle_for),
            threshold_sec=round(self._context_reset_sec),
        )
        self._on_context_reset()
        request_planned_reopen(self)
        await await_connected(self)
        # Move the marker so the next acquire does not re-trigger.
        self._last_turn_end_at = asyncio.get_event_loop().time()

    def _on_context_reset(self) -> None:
        """Drop any provider state that must not survive a context reset."""

    async def _on_turn_released(self, turn: Any) -> None:
        async with self._turn_lock:
            if self._active_turn is turn:
                self._active_turn = None
                self._last_turn_end_at = asyncio.get_event_loop().time()
        async with self._state_lock:
            if self._state is ConnectionState.IN_TURN:
                self._set_state(ConnectionState.CONNECTED)
        # Fire any reconnect held back for this turn — a mid-turn GoAway,
        # a rotation, or a pre-cap watchdog that came due while the user
        # was talking.
        if self._deferred_reconnect.fire_if_pending(self._reconnect_event.set):
            self._logger.info(
                "%s turn just ended, firing the deferred reconnect "
                "(planned=%s)", self._log_tag, self._planned_rotate,
            )

    # ------------------------------------------------------------------
    # Internal — the pre-emptive reconnect watchdog
    # ------------------------------------------------------------------

    def _start_proactive_watchdog(self) -> None:
        """Arm the pre-emptive reconnect for the session just opened.

        A non-positive delay disables it, so bare construction in tests
        does not spawn a surprise task.
        """
        delay = self._watchdog_delay_sec()
        if delay <= 0:
            return
        self._proactive_watchdog_task = asyncio.create_task(
            self._scheduled_reconnect_watchdog(
                delay, planned=self._watchdog_is_planned,
            ),
            name=f"jasper-{self.PROVIDER_NAME}-proactive-watchdog",
        )

    def _watchdog_delay_sec(self) -> float:
        """How far into a session the watchdog fires. ≤ 0 disables it."""
        return 0.0

    async def _scheduled_reconnect_watchdog(
        self, delay_sec: float, *, planned: bool,
    ) -> None:
        """Sleep out the session's useful life, then reconnect on our terms.

        Both providers face a server-forced disconnect the supervisor
        would otherwise meet reactively, costing a lost turn and a cue:
        OpenAI's hard session cap, Gemini's idle abort. Firing a little
        early, in an idle window, replaces that with a reconnect we chose
        the moment for. A turn in flight defers to `_on_turn_released`.
        """
        await asyncio.sleep(delay_sec)
        if self._state in _WATCHDOG_MOOT_STATES:
            # Already reconnecting or closing for another reason; the
            # teardown path is about to cancel this task anyway.
            return
        deferred = self._active_turn is not None
        log_event(
            self._logger,
            "session.rotate",
            provider=self.PROVIDER_NAME,
            reason="planned" if planned else "cap_preempt",
            outcome="deferred" if deferred else "now",
            after_sec=round(delay_sec),
        )
        # The flag `run_reconnect_with_backoff` spends: a rotation the
        # connection scheduled skips the first backoff wait, a cap
        # pre-empt takes the ordinary ramp.
        self._planned_rotate = planned
        if deferred:
            self._deferred_reconnect.request()
        else:
            self._reconnect_event.set()

    # ------------------------------------------------------------------
    # Internal — receive loop and teardown helpers
    # ------------------------------------------------------------------

    def _on_receive_loop_error(self, exc: BaseException) -> None:
        """Report why the receive loop exited and ask for a reopen.

        Always an UNPLANNED reopen, even with a rotation queued: a real
        failure that inherited the rotation's zero-backoff first attempt
        would retry a dead link immediately, and `request_unplanned_reopen`
        is what spends that flag.
        """
        close_code, close_reason = close_code_and_reason(exc)
        if close_code is not None:
            self._logger.warning(
                "%s disconnected (code=%s reason=%r), reconnecting",
                self._log_tag, close_code, close_reason,
            )
        else:
            self._logger.warning(
                "%s receive loop error (%s: %s), reconnecting",
                self._log_tag, type(exc).__name__, exc,
            )
        request_unplanned_reopen(self)

    async def _mark_connected(self, receive_task: asyncio.Task) -> None:
        """Publish the session the caller just opened as the live one."""
        self._receive_task = receive_task
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()
        self._start_proactive_watchdog()

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        """Cancel one background task and wait out its unwind."""
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=TASK_CANCEL_TIMEOUT_SEC)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    async def _close_with_timeout(self, obj: Any) -> None:
        """Send the close frame and wait out the server's ack."""
        if obj is None:
            return
        try:
            await asyncio.wait_for(obj.close(), timeout=SESSION_CLOSE_TIMEOUT_SEC)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            self._logger.debug("%s close error (ignored): %s", self._log_tag, e)

    async def _close_cm_with_timeout(self, cm: Any) -> None:
        """Unwind the SDK's connect context manager, bounded."""
        if cm is None:
            return
        try:
            await asyncio.wait_for(
                cm.__aexit__(None, None, None), timeout=SESSION_CLOSE_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            self._logger.debug("%s __aexit__ error (ignored): %s", self._log_tag, e)

    def _log_teardown(self, elapsed_sec: float) -> None:
        self._logger.info(
            "%s session torn down in %.0fms", self._log_tag, elapsed_sec * 1000,
        )

    # ------------------------------------------------------------------
    # Provider hooks
    # ------------------------------------------------------------------

    async def _open_session_attempt(self) -> None:
        """See `_supervisor.SupervisedConnection._open_session`."""
        raise NotImplementedError

    async def _teardown_session(self) -> None:
        """See `_supervisor.SupervisedConnection`."""
        raise NotImplementedError

    def _on_reconnect_attempt_failed(
        self, exc: Exception, attempt: int, transient: bool,
    ) -> None:
        """See `_supervisor.SupervisedConnection`."""
        self._logger.warning(
            "%s reconnect attempt %d failed (%s: %s, transient=%s)",
            self._log_tag, attempt, type(exc).__name__,
            self._outage.detail, transient,
        )
