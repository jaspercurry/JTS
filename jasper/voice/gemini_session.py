from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from enum import Enum
from typing import Awaitable, AsyncIterator, Callable

from google import genai
from google.genai import types

from ..tools import ToolRegistry, dispatch_tool
from ._supervisor import (
    ESCALATION_CUE_SLUG,
    ESCALATION_RATE_LIMIT_SEC,
    ESCALATION_REPEAT_THRESHOLD,
    DeferredReconnect,
    FailureFingerprint,
    reconnect_backoff_delay,
)
from .session import AudioOutChunk, LiveConnection, LiveTurn

logger = logging.getLogger(__name__)


# Back-compat aliases for tests that import the underscore-prefixed
# names from this module. New code should import these directly from
# `jasper.voice._supervisor`.
_FailureFingerprint = FailureFingerprint
_reconnect_backoff_delay = reconnect_backoff_delay

# Keepalive period — Vertex Live API closes idle connections after 10
# min (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/troubleshooting).
# 4 min gives 6+ min headroom even if the keepalive task lags briefly.
KEEPALIVE_PERIOD_SEC = 240.0

# Age-out window for un-acked `activity_end`s. If the server hasn't
# returned a `turn_complete` within this many seconds of our send, we
# assume the server silently dropped the turn (a known Gemini Live
# behaviour — it accepts the audio, returns nothing, never finalises)
# and stop counting that activity_end as "still pending". Without
# this, silent-failure turns leak the un-ack counter forever, which
# eventually wedges the receive loop into dropping every legitimate
# response from subsequent turns as "stale from a prior turn".
# 30 s is a couple x the worst observed first-chunk latency.
UNACK_AGE_OUT_SEC = 30.0

# Connect retry schedule used for both the initial daemon-startup
# connect AND the post-context-reset reopen. Total wall-time on
# repeated failure is 15 s, which gives Google's session-release lag
# a generous window after a systemd restart hits the previous
# process's still-lingering WebSocket — empirically the prior 7 s
# budget (0+1+2+4) was occasionally too tight on busy regions.
INITIAL_CONNECT_BACKOFF_SCHEDULE = (0.0, 1.0, 2.0, 4.0, 8.0)

# GoAway deferral threshold. When the server sends a GoAway mid-turn
# (it fires near the ~15-min audio cap and can land while the user is
# still mid-reply), we don't want to tear the session down and lose the
# in-flight turn. If the GoAway's `time_left` is at least as long as the
# longest a turn can run, defer the reconnect until the turn is released
# (mirrors the OpenAI proactive-watchdog deferral idiom). A user turn is
# bounded by the daemon's hard recording cap (HARD_RECORDING_CAP_SEC =
# 30 s in voice_daemon) and usually ends sooner via the idle watchdog
# (JASPER_IDLE_TIMEOUT_SEC, default 20 s), so a 30 s threshold lets a turn
# run to completion inside the deferred window; a test pins
# `threshold >= HARD_RECORDING_CAP_SEC` so a future cap bump can't
# silently make deferral unsafe. Fail-safe either way: if `time_left` is
# below this (or unparseable, or no turn is active) we reconnect promptly,
# and if a deferred turn still overruns `time_left` the server just drops
# the WS and the supervisor reconnects — the same outcome as reconnecting
# now.
GOAWAY_DEFER_MIN_TIME_LEFT_SEC = 30.0


def _goaway_time_left_seconds(time_left) -> float | None:
    """Best-effort conversion of a GoAway `time_left` to seconds.

    The genai SDK surfaces `time_left` as a `datetime.timedelta` (it may
    also arrive as a protobuf Duration or a plain number depending on SDK
    version). Returns None when it can't be interpreted — callers treat
    None as "don't defer", which fails safe to the existing
    reconnect-immediately behaviour."""
    if time_left is None:
        return None
    total = getattr(time_left, "total_seconds", None)
    if callable(total):
        try:
            return float(total())
        except Exception:  # noqa: BLE001
            return None
    secs = getattr(time_left, "seconds", None)
    if secs is not None:
        try:
            nanos = getattr(time_left, "nanos", 0) or 0
            return float(secs) + float(nanos) / 1e9
        except Exception:  # noqa: BLE001
            return None
    try:
        return float(time_left)
    except (TypeError, ValueError):
        return None


def _is_409_conflict(exc: Exception) -> tuple[bool, int | None]:
    """Decide whether an exception from ``client.aio.live.connect`` /
    ``__aenter__`` represents an HTTP 409 Conflict from Google's edge.

    Returns ``(is_409, detected_status_code)``. The status is returned
    so callers can log it accurately — the existing log line was
    showing ``status=None`` for every real 409 because it only checked
    httpx-style ``e.response.status_code``, while the SDK actually
    raises ``websockets.legacy.exceptions.InvalidStatusCode`` with the
    code on ``e.status_code`` directly.

    Detection order, most to least specific:
      1. ``e.status_code`` — websockets ``InvalidStatusCode`` (the real
         path on google-genai 1.13.x).
      2. ``e.response.status_code`` — httpx-style errors (some SDK
         versions wrap edge errors this way).
      3. Substring scan of ``str(exc)`` for ``"409"`` or ``"Conflict"`` —
         forward-compat fallback if a future websockets / SDK release
         restructures the exception. Carries no detected status.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 409:
        return True, status
    msg = str(exc)
    if "409" in msg or "Conflict" in msg:
        return True, status
    return False, status


class ConnectionState(Enum):
    """States for the persistent Gemini Live connection state machine.

    Transitions:
        IDLE_INIT  -- start()         --> CONNECTING
        CONNECTING -- handshake ok    --> CONNECTED
        CONNECTING -- handshake fail  --> RECONNECTING (if retries remain)
                                       or FAILED (if exhausted)
        CONNECTED  -- acquire_turn()  --> IN_TURN
        IN_TURN    -- release()       --> CONNECTED
        CONNECTED  -- GoAway / drop   --> RECONNECTING
        IN_TURN    -- GoAway / drop   --> RECONNECTING (active turn marked turn_lost)
        RECONNECTING -- backoff_wait  --> PAUSED_FOR_BACKOFF (informational)
        PAUSED_FOR_BACKOFF -- timer   --> CONNECTING
        any state  -- stop()          --> CLOSED
    """
    IDLE_INIT = "idle_init"          # constructed, not yet started
    CONNECTING = "connecting"
    CONNECTED = "connected"
    IN_TURN = "in_turn"
    RECONNECTING = "reconnecting"
    PAUSED_FOR_BACKOFF = "paused_for_backoff"
    FAILED = "failed"
    CLOSED = "closed"


class GeminiLiveTurn(LiveTurn):
    """A single turn against an open `GeminiLiveConnection`.

    Owns the per-turn audio queue and per-turn counters. The connection's
    receive loop routes incoming server messages here while a turn is
    active. After `release()`, the connection's `_active_turn` slot is
    cleared and the next `acquire_turn()` returns a fresh turn.
    """

    def __init__(
        self,
        conn: "GeminiLiveConnection",
        started_at: float,
        usage_baseline: dict[str, int] | None = None,
    ) -> None:
        self._conn = conn
        self._audio_q: asyncio.Queue[AudioOutChunk | None] = asyncio.Queue()
        # Gemini Live reports usage_metadata as a counter cumulative for
        # the WebSocket's lifetime, not per-turn. We capture the
        # connection's cumulative at turn start as a baseline and report
        # this turn's DELTA from it (see usage_tokens), so per-turn usage
        # rows hold per-turn counts and SUM() across rows doesn't
        # multi-count. `_usage` tracks the latest observed cumulative; it
        # starts at the baseline so a turn that observes no usage_metadata
        # reports a zero delta rather than a negative one.
        self._usage_baseline = dict(
            usage_baseline or {"input_tokens": 0, "output_tokens": 0}
        )
        self._usage = dict(self._usage_baseline)
        self._turn_count = 0
        self._interrupted = False
        self._interrupt_event = asyncio.Event()
        # Loop-time of the last audio chunk / tool_call / turn_complete.
        # Used by the daemon's idle watchdog and barge-in gate.
        self._last_activity_at: float = started_at
        self._last_chunk_at: float = 0.0
        # Updated by audio_out() each time the consumer dequeues a
        # chunk. The idle watchdog uses this for the tail wait so we
        # don't end the turn while audio is still queued waiting to
        # play through ALSA. Gemini paces audio chunks closer to
        # real-time than OpenAI does, so this anchor matters less for
        # this provider — but we track it anyway for protocol parity
        # so daemon code stays single-path.
        self._last_chunk_dequeued_at: float = 0.0
        self._first_chunk_logged = False
        self._started_at = started_at
        # Monotonic clock anchor for elapsed-ms log lines. The connection
        # overrides this in acquire_turn() right after construction so the
        # value lines up with the actual activity_start send.
        self._started_at_monotonic: float = _time.monotonic()
        # Counters per turn — silent-failure detection lives at this
        # granularity now (was per-session pre-rework). With the
        # persistent connection, "session" no longer maps cleanly to one
        # user query.
        self._bytes_sent: int = 0
        self._chunks_received: int = 0
        self._activity_end_sent = False
        self._released = False
        self._turn_lost = False
        # Set when the server emits server_content.turn_complete — the
        # explicit "model is done speaking" signal. Used by the daemon's
        # idle watchdog to close the turn promptly without racing
        # mid-response chunk gaps.
        self._server_turn_complete = False

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._released or self._turn_lost:
            return
        try:
            await self._conn._send_audio_blob(pcm_16khz_int16)
            self._bytes_sent += len(pcm_16khz_int16)
        except Exception as e:  # noqa: BLE001
            # The connection's reconnect supervisor will pick up the WS
            # drop. Mark the turn as lost so the daemon stops trying.
            logger.warning(
                "live turn: send_audio failed (%s: %s); turn lost",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

    async def end_input(self) -> None:
        """Send `activity_end` to the server. Idempotent."""
        if self._activity_end_sent or self._released or self._turn_lost:
            return
        self._activity_end_sent = True
        try:
            await self._conn._send_activity_end()
        except Exception as e:  # noqa: BLE001
            logger.debug("live turn: end_input ignored (%s: %s)", type(e).__name__, e)
            self._turn_lost = True
            await self._audio_q.put(None)

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
            self._last_chunk_dequeued_at = asyncio.get_event_loop().time()
            yield chunk

    async def release(self) -> None:
        """Release the turn. Idempotent. Sends `activity_end` if not
        already sent, then closes the audio iterator (sentinel None)
        and detaches from the connection."""
        if self._released:
            return
        self._released = True
        elapsed_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
        # Drain pending playback queue so any in-flight `audio_out()`
        # iterator wakes up promptly.
        await self._audio_q.put(None)
        # Best-effort: tell the server the turn is over so it doesn't
        # keep waiting for more user audio.
        if not self._activity_end_sent and not self._turn_lost:
            try:
                await self._conn._send_activity_end()
                self._activity_end_sent = True
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "live turn: release activity_end ignored (%s: %s)",
                    type(e).__name__, e,
                )
        await self._conn._on_turn_released(self)
        logger.info(
            "live turn: ended in %.0fms, %d chunks received (sent=%dB)",
            elapsed_ms, self._chunks_received, self._bytes_sent,
        )

    def last_activity_at(self) -> float:
        return self._last_activity_at

    def last_chunk_at(self) -> float:
        return self._last_chunk_at

    def last_chunk_played_at(self) -> float:
        return self._last_chunk_dequeued_at

    def server_turn_complete(self) -> bool:
        """True once the server has emitted server_content.turn_complete
        — the canonical 'model is done speaking' signal. The daemon's
        idle watchdog uses this to close out a turn promptly without
        racing mid-response chunk gaps that look like idleness."""
        return self._server_turn_complete

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def usage_tokens(self) -> dict[str, int]:
        """This turn's token usage — the delta of Gemini's cumulative
        counter since the baseline captured at turn start, so callers
        may SUM across turns without multi-counting. See __init__."""
        return {
            "input_tokens": self._turn_delta("input_tokens"),
            "output_tokens": self._turn_delta("output_tokens"),
        }

    def _turn_delta(self, key: str) -> int:
        observed = int(self._usage.get(key, 0))
        baseline = int(self._usage_baseline.get(key, 0))
        delta = observed - baseline
        # A negative delta means the server-side counter reset under us
        # (a fresh session after a reconnect restarts it); the observed
        # value is then already the post-reset, this-session total.
        return delta if delta >= 0 else observed

    def usage_breakdown(self) -> dict | None:
        # Gemini Live's usage_metadata only carries
        # `prompt_token_count` and `response_token_count` — there's no
        # audio/text/cached split exposed today. Returning None makes
        # the spend cap fall back to the scalar all-audio estimate,
        # which is what we've always done for Gemini.
        return None

    def turn_lost(self) -> bool:
        return self._turn_lost

    def interrupted(self) -> bool:
        return self._interrupted

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    # Internal — called by the connection's receive loop when it routes
    # an incoming server message to this active turn.
    async def _on_response(self, response) -> None:
        # Audio frames live on response.data (raw 24 kHz int16 PCM).
        data = getattr(response, "data", None)
        if data:
            now = asyncio.get_event_loop().time()
            self._last_activity_at = now
            self._last_chunk_at = now
            self._chunks_received += 1
            if not self._first_chunk_logged:
                self._first_chunk_logged = True
                first_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
                logger.info(
                    "first audio chunk from Gemini in %.0fms (turn start→1st chunk)",
                    first_ms,
                )
            await self._audio_q.put(AudioOutChunk(pcm=data))

        # Tool calls. The connection's dispatcher resets the idle anchor
        # inside its loop too — covers slow / chained dispatches the
        # initial reset here can't see.
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            self._note_activity()
            await self._conn._handle_tool_call(tool_call, self)

        # Server content: turn_complete + interrupted.
        turn_just_completed = False
        sc = getattr(response, "server_content", None)
        if sc is not None:
            if getattr(sc, "turn_complete", False):
                self._turn_count += 1
                self._note_activity()
                self._server_turn_complete = True
                turn_just_completed = True
            if getattr(sc, "interrupted", False):
                # Drop any audio chunks queued ahead of this point — they
                # are pre-interrupt and should NOT be played to the user.
                while True:
                    try:
                        self._audio_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                self._interrupted = True
                self._interrupt_event.set()
                logger.info("model interrupted by user")

        # Usage metadata: guarded since field names can shift on Preview.
        # The counter is cumulative for the WebSocket's lifetime, so we
        # store the latest observed value here AND advance the
        # connection's running cumulative (the baseline for the NEXT
        # turn). usage_tokens() reports this turn's delta from its
        # captured baseline.
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            in_tok = getattr(usage, "prompt_token_count", None)
            out_tok = getattr(usage, "response_token_count", None)
            if in_tok is not None:
                self._usage["input_tokens"] = int(in_tok)
            if out_tok is not None:
                self._usage["output_tokens"] = int(out_tok)
            # Advance the connection's running cumulative (the baseline
            # for the NEXT turn). Goes through a connection method rather
            # than poking its dict, matching the turn→connection call
            # pattern used elsewhere (_handle_tool_call, _on_turn_released).
            self._conn._note_cumulative_usage(
                self._usage["input_tokens"], self._usage["output_tokens"],
            )

        # Per-turn diagnostic line, parity with the OpenAI adapter's
        # `openai response.done:` log. We surface both this turn's delta
        # (what gets billed to the usage row) and the cumulative counter
        # (for debugging the delta math). Same shape lets
        # `grep "response.done\|turn complete"` work across providers.
        if turn_just_completed:
            td = self.usage_tokens()
            logger.info(
                "gemini turn complete: in=%d out=%d (turn) "
                "in=%d out=%d (cumulative) chunks=%d",
                td["input_tokens"], td["output_tokens"],
                int(self._usage.get("input_tokens") or 0),
                int(self._usage.get("output_tokens") or 0),
                self._chunks_received,
            )

    def _note_activity(self) -> None:
        """Reset the pre-response idle anchor.

        Called by the connection's receive loop and tool dispatcher
        whenever something happens that means "model is still working"
        — tool_call arrival, an individual tool completing inside a
        multi-call round, the post-dispatch send_tool_response.

        Mirrors ``OpenAIRealtimeTurn._note_activity()`` so the daemon's
        protocol-agnostic ``_idle_watchdog`` behaves uniformly across
        adapters. ``_on_response``'s audio-delta path does NOT call
        this (chunks arrive on a hot path and read the loop clock
        once inline for the ``_last_chunk_at`` companion update)."""
        self._last_activity_at = asyncio.get_event_loop().time()

    def _on_connection_lost(self) -> None:
        """Called by the connection when the underlying WS dropped while
        this turn was active. The turn is marked as lost; the daemon
        should treat it like "turn ended" but log the loss."""
        if self._released or self._turn_lost:
            return
        self._turn_lost = True
        # Wake any playback iterator.
        try:
            self._audio_q.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            pass


class GeminiLiveConnection(LiveConnection):
    """Long-lived Gemini Live connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and a state machine that survives the 15-min audio cap via
    `sessionResumption` and reconnects on GoAway / 1006 / 1011.

    Audio shape: input 16-bit PCM @ 16 kHz mono, output 16-bit PCM @ 24 kHz
    mono. Manual VAD: automatic_activity_detection.disabled = True; the
    daemon sends `activity_start` on wake and `activity_end` on idle.
    """

    INPUT_MIME = "audio/pcm;rate=16000"

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = "Aoede",
        context_reset_sec: float = 0.0,
        keepalive_period_sec: float = KEEPALIVE_PERIOD_SEC,
        # Production: leave None → supervisor reconnects FOREVER with
        # `_reconnect_backoff_delay(attempt)` (1, 2, 4, 8, 16, 32, 60,
        # 60, …s with ±25% jitter). Tests pass a bounded tuple to make
        # exhaustion observable and runs fast.
        backoff_schedule: tuple[float, ...] | None = None,
        # Test seam: replace `client.aio.live.connect` so unit tests can
        # mock the SDK without touching the network.
        connect_factory=None,
    ) -> None:
        self._client = genai.Client(api_key=api_key) if connect_factory is None else None
        self._connect_factory = connect_factory
        self._model = model
        self._voice = voice
        self._context_reset_sec = context_reset_sec
        self._keepalive_period_sec = keepalive_period_sec
        self._backoff_schedule = backoff_schedule

        self._registry: ToolRegistry | None = None
        # System-instruction provider. Called at every (re)connect so
        # time-injection ("right now it is Monday, May 4, 3:14 PM") stays
        # accurate across the daemon's lifetime — the connection lives
        # for hours but reopens on every context-reset (default 5 min idle).
        self._system_instruction_provider: Callable[[], str] | None = None
        # Initial state set directly (no log) — _set_state requires
        # self._state to already exist. Subsequent transitions go
        # through _set_state for logging.
        self._state = ConnectionState.IDLE_INIT
        self._state_lock = asyncio.Lock()
        # Transitions log filter: WAKE/SESSION cycling produces
        # CONNECTED ↔ IN_TURN constantly and floods the journal at INFO;
        # everything else is rare and worth logging.
        self._noisy_transitions = frozenset({
            (ConnectionState.CONNECTED, ConnectionState.IN_TURN),
            (ConnectionState.IN_TURN, ConnectionState.CONNECTED),
        })

        # Active SDK session + context manager (cleared during reconnect).
        self._session = None
        self._session_cm = None

        # Latest session-resumption handle from the server. Used on
        # reconnect to resume the conversation. Cleared explicitly when
        # the idle-context-reset fires.
        self._resumption_handle: str | None = None
        # Loop-time of the last completed turn (for idle-context-reset).
        self._last_turn_end_at: float = 0.0

        # The slot for the currently active turn, if any. Only one turn
        # may be in flight at a time — wake events are serialised by the
        # daemon's WakeLoop.
        self._active_turn: GeminiLiveTurn | None = None
        self._turn_lock = asyncio.Lock()

        # Running cumulative of Gemini's session usage counter (which is
        # cumulative for the WebSocket's lifetime). Each turn captures
        # this at start as its baseline and reports its own delta, so
        # per-turn usage rows don't multi-count. Each turn advances it as
        # it observes usage_metadata. NOT reset on reconnect — a counter
        # reset on a fresh session is handled by the delta's reset-guard
        # (GeminiLiveTurn._turn_delta).
        self._cumulative_usage = {"input_tokens": 0, "output_tokens": 0}

        # Timestamps of `activity_end`s sent to the server that haven't
        # yet been matched by a server-side `turn_complete`. See the
        # docstring on _prune_unack_activity_ends for the design.
        self._unack_activity_end_times: list[float] = []

        # Background tasks: receive loop, keepalive, reconnect supervisor.
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Triggered by the receive loop when it hits a drop / GoAway /
        # exception so the supervisor wakes up and reconnects.
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Set when a GoAway lands mid-turn with ample time_left: the
        # reconnect is deferred and fired from `_on_turn_released` so the
        # in-flight turn isn't torn down mid-reply. Same shared mechanism
        # OpenAI uses for its proactive-watchdog deferral — here the
        # trigger is the GoAway branch in _receive_loop (see _supervisor).
        self._deferred_reconnect = DeferredReconnect()
        # Pause turn acquisition while a reconnect is in progress so
        # the daemon doesn't try to send audio into a half-open WS.
        self._connected_event: asyncio.Event = asyncio.Event()

        # Tight-retry-loop detection. See module-level constants and
        # _FailureFingerprint. Cleared on successful reconnect so the
        # "consecutive failures" count resets after a recovery.
        self._recent_failure_fingerprints: deque[_FailureFingerprint] = deque(
            maxlen=ESCALATION_REPEAT_THRESHOLD,
        )
        # Sentinel: -inf means "never fired", so the rate-limit window
        # check passes the first time. Using 0.0 would falsely block the
        # first fire whenever asyncio.get_event_loop().time() < the
        # rate-limit (1 hour) — which is the entire common case on a
        # freshly-started daemon.
        self._last_escalation_at: float = float("-inf")
        # Async callback invoked when the supervisor detects a tight
        # retry loop. Wired by the daemon to WakeLoop.play_supervisor_cue
        # after both the connection and wake loop are constructed.
        # Signature: (slug: str) -> Awaitable[Any]. None disables
        # escalation (used by tests + minimal harnesses).
        self._failure_escalation_cb: Callable[[str], Awaitable[object]] | None = None

    def _set_state(self, new_state: "ConnectionState") -> None:
        """Update connection state with structured logging.

        Caller is expected to already hold _state_lock when ordering
        of state changes matters. This helper *only* updates the
        state field and logs the transition — it must NOT touch any
        other instance attributes (an earlier refactor accidentally
        re-initialised the entire connection on every transition,
        causing self._session=None and a wedged daemon)."""
        old = self._state
        if old is new_state:
            return
        self._state = new_state
        if (old, new_state) not in self._noisy_transitions:
            logger.info(
                "live connection state: %s → %s",
                old.value, new_state.value,
            )

    def set_failure_escalation_cb(
        self, cb: Callable[[str], Awaitable[object]] | None,
    ) -> None:
        """Wire the supervisor's tight-retry-loop escalation cue.

        Called by the voice daemon after both the connection and the
        WakeLoop are constructed (chicken-and-egg: connection comes
        first, but the cue manager + WakeLoop come later). `cb` should
        be `WakeLoop.play_supervisor_cue` in production — it takes a
        cue slug, ducks music, plays the WAV, and skips if a
        user-driven turn is already active.

        Pass None to disable. Tests do this to keep the supervisor
        observable without a cue manager."""
        self._failure_escalation_cb = cb

    def _maybe_fire_escalation_cue(self) -> None:
        """Inspect the recent-failure ring buffer; fire the escalation
        cue if the last N failures are all the same shape AND the
        rate-limit window has elapsed.

        Called from `_reconnect_with_backoff` after each failure is
        logged. Synchronous (the actual cue play happens in a fire-
        and-forget background task so the supervisor's reconnect
        cadence isn't blocked by audio playback)."""
        if len(self._recent_failure_fingerprints) < ESCALATION_REPEAT_THRESHOLD:
            return
        first = self._recent_failure_fingerprints[0]
        if not all(fp == first for fp in self._recent_failure_fingerprints):
            return
        now = asyncio.get_event_loop().time()
        if now - self._last_escalation_at < ESCALATION_RATE_LIMIT_SEC:
            return
        if self._failure_escalation_cb is None:
            return
        self._last_escalation_at = now
        logger.warning(
            "live connection: %d consecutive identical reconnect failures "
            "(%s, code=%s, %r) — firing %s cue",
            ESCALATION_REPEAT_THRESHOLD,
            first.exc_type, first.close_code, first.reason[:60],
            ESCALATION_CUE_SLUG,
        )
        # Fire-and-forget so the audio playback doesn't block the
        # supervisor's reconnect cadence. The callback (WakeLoop.
        # play_supervisor_cue) handles its own errors and returns a
        # status string; we don't need the result.
        asyncio.create_task(
            self._failure_escalation_cb(ESCALATION_CUE_SLUG),
            name="jasper-supervisor-escalation-cue",
        )

    # ------------------------------------------------------------------
    # LiveConnection protocol
    # ------------------------------------------------------------------

    async def start(
        self,
        registry: ToolRegistry,
        system_instruction: "str | Callable[[], str]",
    ) -> None:
        """Start the persistent connection.

        `system_instruction` may be either a fixed string OR a callable
        that returns a fresh string on each call. The callable form is
        what voice_daemon.py uses so the time-injection stays accurate
        across the connection's hours-long lifetime — the callable is
        invoked on initial connect, every reconnect, and every
        context-reset reopen."""
        self._registry = registry
        if callable(system_instruction):
            self._system_instruction_provider = system_instruction
        else:
            instruction = system_instruction or ""
            self._system_instruction_provider = lambda: instruction
        await self._do_initial_connect()
        # Once initial connect succeeds, supervisor + keepalive run for
        # the daemon's lifetime.
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop(self) -> None:
        if self._state is ConnectionState.CLOSED:
            return
        self._stopping.set()
        # Cancel background tasks first so they don't fight us during teardown.
        for task in (self._supervisor_task, self._keepalive_task, self._receive_task):
            if task is not None:
                task.cancel()
        for task in (self._supervisor_task, self._keepalive_task, self._receive_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._supervisor_task = None
        self._keepalive_task = None
        self._receive_task = None
        # Best-effort close of the SDK session.
        await self._teardown_session()
        # If a turn was active, mark it lost so any iterators wake up.
        if self._active_turn is not None:
            self._active_turn._on_connection_lost()
            self._active_turn = None
        async with self._state_lock:
            self._set_state(ConnectionState.CLOSED)

    async def acquire_turn(self) -> LiveTurn:
        if self._state is ConnectionState.FAILED:
            raise RuntimeError("live connection: in FAILED state; daemon paused")
        if self._state is ConnectionState.CLOSED:
            raise RuntimeError("live connection: closed")

        # If we're mid-reconnect, wait for the connected event so the
        # turn doesn't open against a half-open WS. Bounded so we don't
        # block the wake handler forever if the connection is in a
        # protracted outage. The daemon's wake path checks is_paused()
        # before reaching here, so this timeout is a defensive
        # backstop, not the normal user-facing wait.
        if not self._connected_event.is_set():
            timeout = (
                sum(self._backoff_schedule) + 5.0
                if self._backoff_schedule is not None
                else 15.0  # production: long enough for one full backoff cycle
            )
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "live connection: not connected after backoff window"
                )

        # Idle-context-reset: if the connection is healthy but has been
        # idle too long, drop the resumption handle and reopen with a
        # fresh session so stale conversational state doesn't leak in.
        await self._maybe_reset_context()

        async with self._turn_lock:
            if self._active_turn is not None:
                raise RuntimeError("live connection: a turn is already active")
            now_loop = asyncio.get_event_loop().time()
            # Snapshot the cumulative usage as this turn's baseline so it
            # reports only its own token delta (see GeminiLiveTurn).
            turn = GeminiLiveTurn(
                self, started_at=now_loop,
                usage_baseline=self._cumulative_usage,
            )
            # Used by GeminiLiveTurn for elapsed-ms logging.
            turn._started_at_monotonic = _time.monotonic()
            self._active_turn = turn
            try:
                await self._send_activity_start()
            except BaseException:
                # The turn never started — roll the slot back, or every
                # later acquire_turn() gets "a turn is already active"
                # until a reconnect happens to clear it (observed on the
                # 2026-06-11 eval runs: one ConnectionClosed here wedged
                # the whole suite).
                self._active_turn = None
                raise
            async with self._state_lock:
                if self._state is ConnectionState.CONNECTED:
                    self._set_state(ConnectionState.IN_TURN)
            logger.info("live turn: started (activity_start sent)")
            return turn

    def is_paused(self) -> bool:
        return self._state in (
            ConnectionState.RECONNECTING,
            ConnectionState.PAUSED_FOR_BACKOFF,
            ConnectionState.FAILED,
        )

    def supports_server_vad(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Internal — turn-side helpers
    # ------------------------------------------------------------------

    async def _send_activity_start(self) -> None:
        # Manual VAD requires the client to bracket each turn with
        # activity_start / activity_end markers. acquire_turn() calls
        # this on every wake.
        if self._session is None:
            return
        # Prune any aged-out un-ack entries before reporting.
        self._prune_unack_activity_ends()
        await self._session.send_realtime_input(activity_start=types.ActivityStart())
        logger.info(
            "activity_start sent (unack_activity_ends=%d before send)",
            len(self._unack_activity_end_times),
        )

    def _prune_unack_activity_ends(self) -> None:
        """Drop un-ack timestamps older than UNACK_AGE_OUT_SEC.

        Server silent-failure mode: the server accepts our audio +
        activity_end but never sends turn_complete. Without aging the
        un-ack list, those silent-fail turns leak entries forever and
        eventually wedge the stale-response drop logic into discarding
        every subsequent turn's response as 'belongs to a prior turn'."""
        if not self._unack_activity_end_times:
            return
        cutoff = asyncio.get_event_loop().time() - UNACK_AGE_OUT_SEC
        before = len(self._unack_activity_end_times)
        self._unack_activity_end_times = [
            t for t in self._unack_activity_end_times if t >= cutoff
        ]
        dropped = before - len(self._unack_activity_end_times)
        if dropped > 0:
            logger.warning(
                "live connection: aged out %d un-ack activity_end(s) "
                "(server silent-failure on prior turn); unack now=%d",
                dropped, len(self._unack_activity_end_times),
            )

    async def _send_activity_end(self) -> None:
        # Sent the moment the daemon's Silero user-silence detector
        # sees ~1.2 s of silence after the user has spoken. The server
        # uses this marker (not audio energy) to know the user's
        # utterance is complete and it can begin generating a response.
        # Required for multi-turn: each turn ends with this marker;
        # the next turn opens with a fresh activity_start.
        if self._session is None:
            return
        await self._session.send_realtime_input(activity_end=types.ActivityEnd())
        self._unack_activity_end_times.append(asyncio.get_event_loop().time())
        logger.info(
            "activity_end sent (unack_activity_ends=%d)",
            len(self._unack_activity_end_times),
        )

    async def _send_audio_blob(self, pcm: bytes) -> None:
        if self._session is None:
            logger.warning(
                "live connection: _send_audio_blob called with self._session=None "
                "(state=%s, connected_event=%s, receive_task=%s)",
                self._state.value,
                self._connected_event.is_set(),
                "running" if self._receive_task and not self._receive_task.done() else "done/none",
            )
            raise RuntimeError("live connection: no active session")
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type=self.INPUT_MIME)
        )

    async def _on_turn_released(self, turn: GeminiLiveTurn) -> None:
        async with self._turn_lock:
            if self._active_turn is turn:
                self._active_turn = None
                self._last_turn_end_at = asyncio.get_event_loop().time()
        async with self._state_lock:
            if self._state is ConnectionState.IN_TURN:
                self._set_state(ConnectionState.CONNECTED)
        # Fire any reconnect a mid-turn GoAway deferred for this turn.
        if self._deferred_reconnect.fire_if_pending(self._reconnect_event.set):
            logger.info(
                "live connection: GoAway reconnect — turn just ended, "
                "firing the deferred reconnect",
            )

    def _note_cumulative_usage(
        self, input_tokens: int, output_tokens: int,
    ) -> None:
        """Advance the running cumulative usage counter.

        Gemini reports usage_metadata as a counter cumulative for the
        WebSocket's lifetime; the active turn calls this as it observes
        new values. The next turn captures this in ``acquire_turn`` as
        its baseline and reports its own delta, so per-turn usage rows
        don't multi-count."""
        self._cumulative_usage["input_tokens"] = int(input_tokens)
        self._cumulative_usage["output_tokens"] = int(output_tokens)

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    def _build_config(self) -> "types.LiveConnectConfig":
        """Build LiveConnectConfig with current resumption handle and a
        freshly-rendered system instruction."""
        decls = self._registry.function_declarations() if self._registry else []
        instruction = (
            self._system_instruction_provider()
            if self._system_instruction_provider is not None
            else ""
        )
        # Brevity levers. The system instruction does the heavy lifting
        # ("answer in 1-2 sentences, never ask follow-ups", with
        # few-shot examples). These two config knobs shape the model's
        # tendencies without imposing a hard length cap that could
        # truncate mid-sentence:
        #   - temperature 0.3: low enough to suppress creative tangents,
        #     high enough that responses don't feel robotic.
        #   - thinking_config low: minimal hidden reasoning. The default
        #     for Gemini 3.x is reasoning-leaning; for our use case
        #     (smart-speaker, low-latency, simple intents) we want the
        #     fast path.
        # Deliberately NOT setting max_output_tokens — let the model
        # finish its sentence cleanly. If the system instruction is
        # well-tuned, runaway responses shouldn't happen; if they do,
        # they're a signal the prompt needs work, not that we should
        # mid-sentence-chop.
        # Built defensively: SDK 1.13.0 rejects unknown fields outright
        # (pydantic extra_forbidden), so optional ones go through a
        # construct-then-add try block.
        gen_kwargs: dict = {}
        try:
            gen_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        except Exception:  # noqa: BLE001
            pass
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=instruction or None,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            temperature=0.3,
            **gen_kwargs,
            # Pin the prebuilt voice so it's consistent across sessions
            # (without this the server picks a different voice each time).
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice,
                    ),
                ),
            ),
            # Manual VAD + activity markers. The daemon's wake-word
            # detector already gates "is the user talking right now",
            # so server-side automatic VAD adds nothing useful and
            # makes ambient/music handling fiddly. With manual VAD we
            # ONLY stream mic frames between activity_start/activity_end,
            # so the server doesn't see music or background noise at
            # all between turns.
            #
            # NO_INTERRUPTION: server doesn't let user activity interrupt
            # the model mid-turn. Necessary because we have no working
            # bleed-vs-real-speech distinguisher in software — Silero VAD
            # treats TTS bleed as "speech" (which it is — TTS is by design
            # speech-shaped), so the server-side VAD AND any local VAD
            # will both fire on the model's own bleed-through. With
            # NO_INTERRUPTION the server ignores user activity until
            # turn_complete, so the model always finishes its sentence.
            # Trade-off: real barge-in is disabled. Fix path is hardware
            # AEC — XVF3800 USB-IN as AEC reference, requires CamillaDSP-
            # routed playback architecture (TODO: future work).
            # Manual VAD: client owns turn boundaries via activity_start
            # / activity_end markers. This is the canonical multi-turn
            # pattern on a persistent connection — each pair is one
            # turn, and the server uses them as the unambiguous turn
            # signal. Auto VAD with pause-resume (stop streaming
            # between turns) silently breaks on turn 2: the server
            # never sees a clean turn boundary so it drops turn-2's
            # audio entirely (0 input_tokens, 0 chunks back).
            # Sending audio_stream_end instead of activity_end is also
            # wrong here — that's auto-VAD's "stream paused" signal,
            # observed to also leave turn 2 silently failing.
            # The user-silence detector in voice_daemon.py
            # (END_OF_UTTERANCE_SILENCE_SEC) calls turn.end_input()
            # the moment Silero sees ~1.2 s of silence after the user
            # has spoken; that fires the activity_end marker so the
            # server can process the utterance and begin generating.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,
                ),
                activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            ),
            session_resumption=self._build_session_resumption(),
        )

    def _build_session_resumption(self) -> "types.SessionResumptionConfig | None":
        # Only include sessionResumption when we actually have a cached
        # handle (i.e. on a reconnect after the server gave us a
        # new_handle). On the first connect, NONE of Google's reference
        # demos send this field — sending `SessionResumptionConfig(
        # handle=None)` on a fresh connect is semantically odd ("resume
        # session None") and may put the server into a state where
        # subsequent turns silently fail. Verified against the four
        # GoogleCloudPlatform/generative-ai live-API demos: zero of
        # them set this field at all.
        if self._resumption_handle is None:
            return None
        return types.SessionResumptionConfig(handle=self._resumption_handle)

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open_session_with_409_retry(
                INITIAL_CONNECT_BACKOFF_SCHEDULE,
                phase="initial-connect",
            )
        except Exception:
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            raise

    async def _open_session_with_409_retry(
        self,
        schedule: tuple[float, ...],
        *,
        phase: str,
    ) -> None:
        """Run ``_open_session`` with a 409-aware retry loop.

        Used by both ``_do_initial_connect`` (daemon startup) and
        ``_maybe_reset_context`` (post-idle context reset). The
        supervisor's reconnect path uses its own loop because it
        also needs to coordinate with the state machine (PAUSED_FOR
        _BACKOFF transitions, stop-event checks); the 409 detection
        and handle-drop logic there is duplicated rather than shared
        to avoid coupling state-machine code into this helper.

        Behaviour:
          * Each attempt calls ``_open_session()``; on success returns.
          * On 409: log the status code accurately (read from
            ``e.status_code`` first, then ``e.response.status_code``),
            then — if a resumption handle is currently cached AND
            we haven't already dropped it within this retry loop —
            drop the handle so the NEXT attempt connects as a fresh
            session. A stale / invalidated resumption handle is the
            single most common cause of 409 here (next is
            concurrent-session-limit), and dropping the handle is
            both the recommended Live-API recovery and harmless
            otherwise (we lose conversational context, not the
            connection).
          * On non-409: re-raise immediately (auth errors / malformed
            config don't fix themselves with a wait).
          * After exhausting the schedule: raise ``RuntimeError``.

        ``phase`` is included verbatim in log lines so journalctl
        searches for "409" can tell whether the conflict happened on
        startup or on a context reset.
        """
        last_exc: Exception | None = None
        handle_dropped = False
        for attempt, delay in enumerate(schedule):
            if delay > 0:
                logger.warning(
                    "live connection: %s retry %d after %.1fs (last: %s: %s)",
                    phase, attempt, delay,
                    type(last_exc).__name__ if last_exc else "?",
                    last_exc,
                )
                await asyncio.sleep(delay)
            try:
                await self._open_session()
                if handle_dropped:
                    logger.info(
                        "live connection: %s recovered after dropping stale "
                        "resumption handle on attempt %d",
                        phase, attempt + 1,
                    )
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                is_409, status = _is_409_conflict(e)
                if not is_409:
                    raise
                # Visible, structured 409 log so journalctl filtering
                # for "409 Conflict" surfaces every occurrence, with
                # enough context (attempt, status, exc type, partial
                # handle) to attribute the cause.
                handle_short = (
                    (self._resumption_handle or "")[:8]
                    if self._resumption_handle
                    else "<none>"
                )
                logger.warning(
                    "live connection: %s 409 Conflict on attempt %d/%d "
                    "(status=%s, exc=%s, handle=%s)",
                    phase, attempt + 1, len(schedule),
                    status, type(e).__name__, handle_short,
                )
                # First 409 with a cached resumption handle: drop it.
                # Stale / server-invalidated handles are the single
                # most common 409 source on reconnect — the bare
                # concurrent-session-limit case is much rarer, and
                # dropping the handle doesn't hurt that case (the
                # next attempt just connects fresh once Google's
                # release lag passes).
                if not handle_dropped and self._resumption_handle is not None:
                    logger.warning(
                        "live connection: %s dropping cached resumption "
                        "handle (handle=%s) and will retry as fresh session",
                        phase, handle_short,
                    )
                    self._resumption_handle = None
                    handle_dropped = True
        raise RuntimeError(
            f"live connection: {phase} failed after {len(schedule)} retries; "
            f"last error: {last_exc}"
        )

    async def _open_session(self) -> None:
        """Open a fresh SDK session against the current config and start
        the receive loop. Raises if the connect fails."""
        # Reset the stale-response counter — server-side state is fresh
        # on a new session, so any prior pending turn_completes from
        # the old session are no longer relevant.
        self._unack_activity_end_times = []
        config = self._build_config()
        connect_call = (
            self._connect_factory
            if self._connect_factory is not None
            else self._client.aio.live.connect
        )
        t0 = _time.monotonic()
        cm = connect_call(model=self._model, config=config)
        try:
            session = await cm.__aenter__()
        except Exception:
            # __aenter__ failed (e.g. 409, network error). The CM is in
            # an indeterminate state; don't leak the reference. Don't
            # set self._session_cm at all so the supervisor's next
            # retry / shutdown's teardown sees no stale handle.
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            raise
        self._session_cm = cm
        self._session = session
        logger.info(
            "live connection: self._session SET (id=%s) by _open_session",
            id(session),
        )
        connect_ms = (_time.monotonic() - t0) * 1000
        handle_short = (self._resumption_handle or "")[:8] or "<new>"
        logger.info(
            "live connection: connect ok in %.0fms (resumption=%s)",
            connect_ms, handle_short,
        )
        self._reconnect_event.clear()
        # Verify self._session is still what we set right before
        # creating the receive task — instrumentation to chase a
        # bug where receive_loop sees None at start.
        logger.info(
            "live connection: pre-create_task check — self._session id=%s, target id=%s",
            id(self._session) if self._session is not None else None,
            id(session),
        )
        self._receive_task = asyncio.create_task(self._receive_loop())
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()

    async def _teardown_session(self) -> None:
        """Tear down whatever's currently open — session + receive task —
        without affecting the supervisor. Used both on normal close and
        as a step in reconnect.

        Bounded awaits everywhere: we WANT to give the WS close
        handshake time to complete server-side (so the next connect
        doesn't conflict with a session that's still cleaning up —
        this is suspected to contribute to 409s in Cloud Logging),
        but we don't want a misbehaving close to hang the daemon.
        Each step gets a 3 s ceiling, with the entire teardown
        bounded by the daemon's systemd TimeoutStopSec (90 s default)
        on shutdown."""
        t0 = _time.monotonic()
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await asyncio.wait_for(self._receive_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        if self._session is not None:
            try:
                # Send close frame and wait for server ack so the
                # server-side session is actually torn down before
                # we (or anyone else) opens a new WS.
                await asyncio.wait_for(self._session.close(), timeout=3.0)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("live connection: session.close() error (ignored): %s", e)
        if self._session_cm is not None:
            try:
                await asyncio.wait_for(
                    self._session_cm.__aexit__(None, None, None), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("live connection: session __aexit__ error (ignored): %s", e)
        self._session_cm = None
        prior_session_id = id(self._session) if self._session is not None else None
        self._session = None
        self._connected_event.clear()
        teardown_ms = (_time.monotonic() - t0) * 1000
        logger.info(
            "live connection: session torn down in %.0fms (cleared session id=%s)",
            teardown_ms, prior_session_id,
        )

    async def _supervisor_loop(self) -> None:
        """Run for the connection's lifetime. Wakes on `_reconnect_event`,
        runs through the bounded backoff schedule, and surfaces FAILED
        if exhausted. Triggered by the receive loop when it observes a
        drop, GoAway, or unexpected exception."""
        try:
            while not self._stopping.is_set():
                await self._reconnect_event.wait()
                if self._stopping.is_set():
                    return
                await self._reconnect_with_backoff()
                self._reconnect_event.clear()
        except asyncio.CancelledError:
            raise

    async def _reconnect_with_backoff(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.RECONNECTING)
        # Tear down the old session before opening a new one so we don't
        # leak a half-open WS through the SDK.
        await self._teardown_session()
        # A reconnect is now underway — any GoAway-deferred reconnect is
        # subsumed by this one; clear the flag so a later turn release
        # doesn't fire a spurious second reconnect.
        self._deferred_reconnect.clear()
        # Mark the active turn (if any) as lost AND detach it. The
        # daemon's idle watchdog will pick up `turn_lost()` and call
        # `release()`, but in the meantime the connection's slot is free
        # — clearing `_active_turn` lets a wake event after reconnect
        # acquire a fresh turn rather than getting "a turn is already
        # active" while the old one is still being torn down.
        if self._active_turn is not None:
            self._active_turn._on_connection_lost()
            async with self._turn_lock:
                self._active_turn = None

        last_exc: Exception | None = None
        handle_dropped = False
        attempt = 0
        # Production: `self._backoff_schedule is None` → infinite loop.
        # Tests pass a bounded tuple to make exhaustion observable.
        bounded = self._backoff_schedule is not None
        max_attempts = len(self._backoff_schedule) if bounded else None
        while not self._stopping.is_set():
            attempt += 1
            if bounded and attempt > max_attempts:
                break
            delay = (
                self._backoff_schedule[attempt - 1]
                if bounded
                else _reconnect_backoff_delay(attempt)
            )
            async with self._state_lock:
                self._set_state(ConnectionState.PAUSED_FOR_BACKOFF)
            logger.info(
                "live connection: reconnect attempt %d after %.1fs backoff",
                attempt, delay,
            )
            await asyncio.sleep(delay)
            if self._stopping.is_set():
                return
            try:
                await self._open_session()
                if handle_dropped:
                    logger.info(
                        "live connection: reconnect recovered on attempt %d "
                        "after dropping stale resumption handle",
                        attempt,
                    )
                # Successful reconnect resets the consecutive-identical-
                # failure detector. Without this clear, a future tight
                # loop could fire the escalation cue prematurely by
                # combining new failures with stale ones from before
                # the recovery.
                self._recent_failure_fingerprints.clear()
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                is_409, status = _is_409_conflict(e)
                handle_short = (
                    (self._resumption_handle or "")[:8]
                    if self._resumption_handle
                    else "<none>"
                )
                if is_409:
                    logger.warning(
                        "live connection: reconnect 409 Conflict on attempt "
                        "%d (status=%s, exc=%s, handle=%s)",
                        attempt, status, type(e).__name__, handle_short,
                    )
                else:
                    logger.warning(
                        "live connection: reconnect attempt %d failed "
                        "(%s: %s, handle=%s)",
                        attempt, type(e).__name__, e, handle_short,
                    )
                # Tight-retry-loop detection: append the failure shape
                # to the ring buffer and check for sustained identical
                # failures. The cue (if it fires) is rate-limited to
                # once per hour to avoid spamming during long outages.
                self._recent_failure_fingerprints.append(
                    _FailureFingerprint.from_exception(e),
                )
                self._maybe_fire_escalation_cue()
                # Drop the cached resumption handle on the first failure
                # of ANY kind. A server-invalidated handle that surfaces
                # as anything other than a 409 (the killer: WebSocket
                # close 1008 with reason "BidiGenerateContent session
                # expired") used to lock the supervisor into an
                # indefinite same-error retry loop because the drop was
                # gated on 409 detection. The cost of dropping a handle
                # we didn't strictly need to is one turn of context
                # continuity; the cost of keeping a stale one is the
                # entire session. The asymmetry justifies the broader
                # drop.
                if not handle_dropped and self._resumption_handle is not None:
                    logger.warning(
                        "live connection: reconnect dropping cached "
                        "resumption handle (handle=%s) after first "
                        "failure; next attempt will connect fresh",
                        handle_short,
                    )
                    self._resumption_handle = None
                    handle_dropped = True

        # Only reached when (a) the test override exhausted its bounded
        # schedule, or (b) the daemon is stopping. Production never
        # reaches this — the loop iterates forever until success.
        if bounded and not self._stopping.is_set():
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            logger.error(
                "live connection: bounded test schedule exhausted after %d "
                "retries. Last error: %s", attempt - 1, last_exc,
            )

    async def _receive_loop(self) -> None:
        """Iterate the SDK's lower-level `session._receive()` and route
        messages.

        We deliberately avoid the public `session.receive()` async
        generator: it `break`s out of its loop the moment the first
        `turn_complete` arrives (the SDK's `live.py` around line 455
        does `if result.server_content.turn_complete: yield result;
        break` — verify against your installed SDK version).
        On a persistent multi-turn connection that means everything
        from turn 2 onward is silently dropped: zero audio chunks
        delivered, zero input/output tokens, no exception. Calling
        `_receive()` directly in a `while` loop bypasses the
        early-break and gives us every message for the connection's
        lifetime, exactly what we need.

        Audio chunks / tool calls / turn_complete / interrupted go to
        the active turn (if any). Connection-level messages
        (`session_resumption_update`, `go_away`) update connection
        state directly. On any exception the receive loop wakes the
        supervisor to drive a reconnect."""
        logger.info(
            "live connection: receive_loop ENTERED — self._session id=%s, conn id=%s",
            id(self._session) if self._session is not None else None,
            id(self),
        )
        # Capture the session once, locally — if the connection is
        # torn down (and `self._session` is reassigned to None or to
        # a brand-new session), this loop stays bound to the session
        # it was started for, so cancellation can complete cleanly
        # without splicing two sessions' message streams together.
        session = self._session
        if session is None:
            logger.warning(
                "live connection: receive_loop started with self._session=None; "
                "exiting (likely a stale cancelled task post-teardown)"
            )
            return
        logger.info(
            "live connection: receive_loop bound to session id=%s",
            id(session),
        )
        try:
            while True:
                response = await session._receive()
                if response is None:
                    # Underlying connection closed cleanly — let the
                    # supervisor drive a reconnect.
                    logger.warning(
                        "live connection: _receive returned None (clean close), reconnecting"
                    )
                    self._reconnect_event.set()
                    return
                # Connection-level: session resumption handle.
                sru = getattr(response, "session_resumption_update", None)
                if sru is not None:
                    new_handle = getattr(sru, "new_handle", None)
                    if new_handle:
                        self._resumption_handle = new_handle
                # Connection-level: server-initiated GoAway. Fired when
                # the 15-min audio cap or other server-side limit is
                # about to disconnect us. Trigger reconnect proactively
                # so the user doesn't see a gap mid-conversation.
                go_away = getattr(response, "go_away", None)
                if go_away is not None:
                    time_left = getattr(go_away, "time_left", None)
                    secs = _goaway_time_left_seconds(time_left)
                    # Defer the reconnect when a turn is in flight AND the
                    # server gave us comfortably more time than a turn
                    # takes — otherwise tearing down now marks the
                    # in-flight turn lost and cuts off the user mid-reply.
                    # Fire the deferred reconnect from `_on_turn_released`.
                    if (
                        self._active_turn is not None
                        and secs is not None
                        and secs >= GOAWAY_DEFER_MIN_TIME_LEFT_SEC
                    ):
                        logger.warning(
                            "live connection: GoAway received mid-turn, "
                            "time_left=%s (%.0fs) ≥ %.0fs — deferring reconnect "
                            "until turn release",
                            time_left, secs, GOAWAY_DEFER_MIN_TIME_LEFT_SEC,
                        )
                        self._deferred_reconnect.request()
                        continue
                    logger.warning(
                        "live connection: GoAway received, time_left=%s, will reconnect",
                        time_left,
                    )
                    self._reconnect_event.set()
                    continue
                # Per-turn routing — but first check whether this
                # response is "stale" from a prior turn we already
                # moved past locally (e.g. via the no-speech abort
                # path) before the server's response landed.
                #
                # Bookkeeping (after pruning aged-out entries):
                #   unack == 0  → no turn-ends are pending an ack from
                #     the server. Audio/tool_call/etc. for the active
                #     turn flows freely.
                #   unack == 1  AND active turn HAS sent activity_end
                #     → the one pending entry IS this turn's. Route.
                #   unack == 1  AND active turn has NOT sent
                #     activity_end → the pending entry must be from
                #     an EARLIER turn (the server can't be turn-
                #     completing the active turn before we tell it
                #     the user is done). Any turn_complete arriving
                #     here is the prior turn's belated ack — pop it
                #     but DO NOT mark the active turn as completed.
                #     This is the bugfix: previously a belated
                #     turn_complete from turn N-1 (typically arriving
                #     30 ms after we sent activity_start for turn N)
                #     was routed to turn N, setting
                #     server_turn_complete=True and causing the idle
                #     watchdog to close turn N 1.5 s later — before
                #     turn N's real response could land.
                #   unack >  1  → multiple turns are pending. Same
                #     stale treatment as the unack==1+!ended case.
                self._prune_unack_activity_ends()
                sc = getattr(response, "server_content", None)
                turn_complete_in_msg = bool(
                    sc is not None and getattr(sc, "turn_complete", False)
                )
                turn = self._active_turn
                active_has_ended_input = (
                    turn is not None and turn._activity_end_sent
                )
                is_stale = (
                    len(self._unack_activity_end_times) > 1
                    or (
                        len(self._unack_activity_end_times) >= 1
                        and not active_has_ended_input
                    )
                )
                if is_stale:
                    if turn_complete_in_msg and self._unack_activity_end_times:
                        # Pop oldest pending entry — this turn_complete
                        # belongs to the earliest-still-pending turn.
                        self._unack_activity_end_times.pop(0)
                        logger.info(
                            "dropped stale turn_complete from prior turn "
                            "(unack_activity_ends=%d remaining)",
                            len(self._unack_activity_end_times),
                        )
                    continue
                if turn_complete_in_msg and self._unack_activity_end_times:
                    self._unack_activity_end_times.pop(0)
                if turn is not None:
                    await turn._on_response(response)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            close_code = getattr(getattr(e, "rcvd", None), "code", None)
            close_reason = getattr(getattr(e, "rcvd", None), "reason", None)
            if close_code is not None:
                logger.warning(
                    "live connection: disconnected (code=%s reason=%r), reconnecting",
                    close_code, close_reason,
                )
            else:
                logger.warning(
                    "live connection: receive loop error (%s: %s), reconnecting",
                    type(e).__name__, e,
                )
            self._reconnect_event.set()

    async def _keepalive_loop(self) -> None:
        """Periodically poke the WebSocket so Vertex doesn't close it
        for the 10-min idle timeout
        (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/troubleshooting).
        Sending a no-op realtime input with no payload counts as activity
        on the SDK's side without consuming Live tokens."""
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(self._keepalive_period_sec)
                if self._stopping.is_set():
                    return
                if self._session is None or self._state in (
                    ConnectionState.RECONNECTING,
                    ConnectionState.PAUSED_FOR_BACKOFF,
                    ConnectionState.FAILED,
                ):
                    continue
                # No-op: with manual VAD enabled (the only mode we run
                # in), sending audio outside an active turn (i.e. without
                # being bracketed by activity_start / activity_end) is
                # protocol-invalid and at best silently ignored, at worst
                # logged server-side as a conflict / state-machine
                # violation — strongly suspected as a contributor to
                # 409 conflict entries in Cloud Logging despite our
                # daemon's WebSocket staying up. The websockets library
                # under genai already sends WS-level PING frames every
                # ~20 s by default, which keeps the underlying TCP
                # connection healthy. If Vertex's *application*-level
                # 10-min idle timeout ever fires while we're in this
                # state, the receive loop will see the close and the
                # supervisor will reconnect cleanly. For our smart-
                # speaker use case (frequent wakes), the 10-min timeout
                # is unlikely to ever hit.
                logger.debug(
                    "live connection: keepalive tick (no-op; rely on WS-level pings)"
                )
        except asyncio.CancelledError:
            raise

    async def _maybe_reset_context(self) -> None:
        """If the connection has been idle longer than the configured
        threshold AND we have at least one previous turn, drop the
        resumption handle and reopen with a fresh session.

        Disabled by default (`context_reset_sec=0`). Enable only if
        you actually observe stale-context glitches: each reset busts
        the resumption handle (so the next turn re-establishes session
        state at full cost) and blocks the wake event for 1-6 s while
        the reconnect happens. The terse-tool system prompt makes
        stale-context bleed a mostly-hypothetical concern in practice."""
        if self._context_reset_sec <= 0:
            return
        if self._last_turn_end_at <= 0.0:
            return
        idle_for = asyncio.get_event_loop().time() - self._last_turn_end_at
        if idle_for < self._context_reset_sec:
            return
        logger.info(
            "live context reset: idle for %.0fs > threshold (%.0fs); "
            "reopening with no resumption handle",
            idle_for, self._context_reset_sec,
        )
        # Drop the handle and roll the session.
        self._resumption_handle = None
        await self._teardown_session()
        # Use the same 409-aware retry wrapper as initial-connect. The
        # bare `_open_session()` here was the single most common source
        # of acquire_turn() failures: server-side session release lags
        # client-side close, and the immediate post-teardown reopen
        # would race that release and 409. Wrapping in the retry loop
        # both spaces the attempts AND drops the (already-cleared, but
        # re-set on error if a server message snuck in) handle.
        try:
            await self._open_session_with_409_retry(
                INITIAL_CONNECT_BACKOFF_SCHEDULE,
                phase="context-reset-reopen",
            )
        except Exception as e:  # noqa: BLE001
            # Hard failure during context-reset reopen: the connection
            # is now in an indeterminate state (no session, no
            # supervisor reconnect triggered, _connected_event clear).
            # Wake the supervisor so it can drive recovery from a clean
            # state instead of leaving the daemon stuck waiting on a
            # connect that nobody will retry.
            logger.error(
                "live connection: context-reset reopen failed (%s: %s); "
                "triggering supervisor reconnect",
                type(e).__name__, e,
            )
            self._reconnect_event.set()
            raise
        # Reset the idle marker so we don't immediately re-trigger.
        self._last_turn_end_at = asyncio.get_event_loop().time()

    async def _handle_tool_call(
        self, tool_call, turn: "GeminiLiveTurn | None" = None,
    ) -> None:
        """Dispatch tool calls from the model with structured timing logs.

        Log format per call:
          tool {name} start args={...}                      [t=0.000s]
          tool {name} fn done in 412ms ok payload={...}     [HTTP + parsing]
          tool {name} response sent to Gemini in 614ms      [total round-trip]
        Failure paths log `timed out` or `raised:` with the same elapsed.

        ``turn`` is the active turn whose idle anchor we reset between
        tool dispatches (see docs/HANDOFF-voice-providers.md
        "Idle anchor + tool rounds"). Optional for back-compat — the
        caller in ``GeminiLiveTurn._on_response`` always passes it.
        """
        assert self._registry is not None
        responses = []
        t0 = _time.monotonic()
        for fc in tool_call.function_calls:
            payload = await dispatch_tool(
                self._registry, fc.name, dict(fc.args or {}),
            )
            responses.append(
                types.FunctionResponse(
                    id=fc.id, name=fc.name, response=payload
                )
            )
            # Per-tool reset so a slow first tool doesn't burn the
            # idle budget of the next one in the same round.
            if turn is not None:
                turn._note_activity()
        if self._session is not None:
            t_send = _time.monotonic()
            await self._session.send_tool_response(function_responses=responses)
            send_ms = (_time.monotonic() - t_send) * 1000
            total_ms = (_time.monotonic() - t0) * 1000
            logger.info(
                "tool response sent to Gemini in %.0fms (total dispatch %.0fms, %d call%s)",
                send_ms, total_ms, len(responses),
                "" if len(responses) == 1 else "s",
            )
            # Final reset after the response item lands — wait for
            # the next audio chunk starts now.
            if turn is not None:
                turn._note_activity()
