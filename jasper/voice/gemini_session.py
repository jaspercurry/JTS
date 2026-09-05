# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import AsyncIterator, Callable

from google import genai
from google.genai import types

from jasper.backoff import ReconnectNudge
from jasper.log_event import log_event

from ..tools import ToolRegistry, dispatch_tool
from ._supervisor import (
    DEFAULT_INITIAL_CONNECT_BUDGET_SEC,
    Deferred,
    OutageTracker,
    await_connected,
    http_status,
    request_planned_reopen,
    run_initial_connect,
    run_supervisor_loop,
    survive_terminal_initial_connect,
)
from .session import (
    CONNECTION_NOISY_TRANSITIONS,
    AudioOutChunk,
    ConnectionState,
    CuePlayer,
    LiveTurn,
)

logger = logging.getLogger(__name__)


# Planned session rotation. On gemini-3.1-flash-live-preview the server
# aborts an idle session at 150.1 s ±1 s with WebSocket close 1008 and no
# preceding GoAway (measured on jts4: n=516, floor 150.06 s; WS pings do
# not count as activity). Rotating at 135 s keeps ~15 s of headroom over
# that floor — comfortably more than the p99 connect (5.3 s) plus
# teardown. Raise only with a fresh lifetime measurement; the cap is
# per-model and undocumented.
SESSION_ROTATE_AFTER_SEC = 135.0

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
    status = http_status(exc)
    if status == 409:
        return True, status
    msg = str(exc)
    if "409" in msg or "Conflict" in msg:
        return True, status
    return False, status


def _close_code_and_reason(exc: Exception) -> tuple[int | None, str | None]:
    """Pull the WebSocket close code out of a receive-loop exception.

    Two shapes carry it. ``websockets`` raises with the frame itself on
    ``exc.rcvd``. The genai SDK consumes the frame and re-raises
    ``APIError``, which keeps the close code on ``.code`` and puts it in
    the message only as prose — so the server's 1008 idle abort was
    otherwise unattributable in the journal.
    """
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, int):
        return code, getattr(rcvd, "reason", None)
    api_code = getattr(exc, "code", None)
    # `APIError.code` is normally an HTTP status; only the 1000-4999
    # WebSocket close range is what this reports.
    if isinstance(api_code, int) and 1000 <= api_code <= 4999:
        return api_code, getattr(exc, "message", None)
    return None, None


class GeminiLiveTurn:
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
        self._interrupt_event = asyncio.Event()
        # Loop-time of the last audio chunk / tool_call / turn_complete.
        # Used by the daemon's idle watchdog and barge-in gate.
        self._last_activity_at: float = started_at
        self._last_chunk_at: float = 0.0
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
        # Gemini Live does not currently expose final text transcripts through
        # this adapter. Keep bounded metadata so conversation history can show
        # that an opt-in captured turn happened without storing tool args or
        # result payloads.
        self._tool_call_names: list[str] = []

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

    async def send_text_context(self, text: str) -> None:
        if self._released or self._turn_lost:
            return
        await self._conn._send_text_context(text)

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

    def conversation_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "kind": "voice_turn",
            "transcripts_available": False,
        }
        if self._tool_call_names:
            metadata["tools"] = list(self._tool_call_names)
        return metadata

    def turn_lost(self) -> bool:
        return self._turn_lost

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupt_event.clear()

    # ---- Barge-in capability seam (Gemini pack — final no-op) ----
    #
    # Reconciliation kind for Gemini is `server_self_truncates` (catalog):
    # START_OF_ACTIVITY_INTERRUPTS would drop the unspoken tail server-side,
    # and Gemini has no OpenAI-style per-response audio item id to truncate
    # against. Both methods are therefore genuine no-ops for Gemini, not
    # just deferred wiring (robust-barge-in PR-5 finalises them as no-ops) —
    # they exist so the daemon's barge-in path stays one code path across
    # providers. Note JTS runs Gemini with manual VAD + NO_INTERRUPTION (see
    # _build_config's barge-in resolution), so the server does not even
    # self-interrupt / self-truncate in normal operation: the daemon's local
    # gate (request_local_interrupt, below) is the sole interruption
    # authority and the local TTS flush happens at the daemon layer.

    async def cancel_response(self, reason: str) -> None:
        # No-op: Gemini interruption is provider-side generation state;
        # there is no client cancel call to synthesize. `reason` reserved
        # for a future structured-log line.
        return None

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        # No-op: no conversation.item.truncate equivalent. `provider_item_id`
        # is expected to be None for Gemini (no per-response audio item id);
        # arguments are accepted and ignored so callers need no provider
        # branch.
        return None

    def request_local_interrupt(self) -> None:
        # Local barge-in (PR-2 spine): flush playout without a provider-side
        # cancel. Mirrors the server-interrupt path's state writes (below) so
        # _play_responses' flush + clear_interrupted cycle is identical; the
        # only difference is the trigger source (daemon VAD vs server). The
        # cancel_response / truncate_assistant_audio seam above stays no-op.
        self._interrupt_event.set()

    def drop_pending_audio(self) -> int:
        # Distinct barge-in drain for the LOCAL gate: request_local_interrupt
        # above arms the flush but — unlike the server-interrupt path in
        # _on_response — does not drain queued audio, so the backlog would
        # replay. Drop queued chunks, PRESERVING any terminal sentinel.
        dropped = 0
        try:
            while True:
                item = self._audio_q.get_nowait()
                if item is None:
                    self._audio_q.put_nowait(None)
                    break
                dropped += 1
        except asyncio.QueueEmpty:
            pass
        return dropped

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

    def _record_tool_call_name(self, name: str | None) -> None:
        cleaned = str(name or "").strip()
        if cleaned:
            self._tool_call_names.append(cleaned)

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


class GeminiLiveConnection:
    """Long-lived Gemini Live connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and a state machine that survives the 15-min audio cap via
    `sessionResumption` and reconnects on GoAway / 1006 / 1011.

    Audio shape: input 16-bit PCM @ 16 kHz mono, output 16-bit PCM @ 24 kHz
    mono. Manual VAD: automatic_activity_detection.disabled = True; the
    daemon sends `activity_start` on wake and `activity_end` on idle.
    """

    PROVIDER_NAME = "gemini"
    # Prefix this module's own log lines already carry verbatim; the
    # shared supervisor reads it from here.
    _log_tag = "live connection:"

    INPUT_MIME = "audio/pcm;rate=16000"

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = "Aoede",
        context_reset_sec: float = 0.0,
        # 0 disables the planned rotation (tests, and any model whose
        # server does not abort idle sessions).
        rotate_after_sec: float = SESSION_ROTATE_AFTER_SEC,
        # Production: leave None → supervisor reconnects FOREVER with
        # `reconnect_delay()` (1, 2, 4, 8, 16, 32, 60, 60, …s with ±25%
        # jitter while the failure is transient; a fixed slow poll once
        # it is terminal). Tests pass a bounded tuple to make
        # exhaustion observable and runs fast.
        backoff_schedule: tuple[float, ...] | None = None,
        # Test seam: replace `client.aio.live.connect` so unit tests can
        # mock the SDK without touching the network.
        connect_factory=None,
        # Test seam: replace the backoff wait so unit tests observe the
        # schedule without sleeping it. Matches OpenAIRealtimeConnection.
        sleep=None,
    ) -> None:
        self._client = genai.Client(api_key=api_key) if connect_factory is None else None
        self._connect_factory = connect_factory
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._monotonic = _time.monotonic
        self._model = model
        self._voice = voice
        self._context_reset_sec = context_reset_sec
        self._rotate_after_sec = rotate_after_sec
        self._backoff_schedule = backoff_schedule

        self._registry: ToolRegistry | None = None
        # System-instruction provider. Called at every (re)connect so
        # time-injection ("right now it is Monday, May 4, 3:14 PM") stays
        # accurate across the daemon's lifetime — the connection lives
        # for hours but rerenders on reconnect or an opt-in context reset.
        self._system_instruction_provider: Callable[[], str] | None = None
        # Initial state set directly (no log) — _set_state requires
        # self._state to already exist. Subsequent transitions go
        # through _set_state for logging.
        self._state = ConnectionState.IDLE_INIT
        self._state_lock = asyncio.Lock()
        # Transitions log filter: WAKE/SESSION cycling produces
        # CONNECTED ↔ IN_TURN constantly and floods the journal at INFO;
        # everything else is rare and worth logging.
        self._noisy_transitions = CONNECTION_NOISY_TRANSITIONS

        # Active SDK session + context manager (cleared during reconnect).
        self._session = None
        self._session_cm = None

        # Latest session-resumption handle from the server. Used on
        # reconnect to resume the conversation. Cleared explicitly when
        # the idle-context-reset fires.
        self._resumption_handle: str | None = None
        # One-shot: the idle context reset raises it so `_teardown_session`
        # drops the handle once the old receive loop can no longer
        # repopulate it. A planned rotation deliberately keeps its handle
        # (ADR-0166), so nothing else may set this.
        self._drop_resumption_on_teardown = False
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

        # Background tasks: receive loop, rotate watchdog, reconnect
        # supervisor.
        self._receive_task: asyncio.Task | None = None
        # Fires at `rotate_after_sec` into each session; re-armed on every
        # successful open and cancelled by `_teardown_session`.
        self._rotate_task: asyncio.Task | None = None
        # Set only by the rotate watchdog: tells the shared reconnect
        # run this reconnect is ours, not a failure, so the first attempt
        # skips the backoff wait.
        self._planned_rotate = False
        # Triggered by the receive loop when it hits a drop / GoAway /
        # exception so the supervisor wakes up and reconnects.
        self._reconnect_event: asyncio.Event = asyncio.Event()
        # Separate from `_reconnect_event` (which means "the session
        # dropped"): this one only shortens an in-flight backoff wait.
        self._nudge_event: asyncio.Event = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Set when a reconnect comes due mid-turn — a GoAway with ample
        # time_left, or a planned rotation. The reconnect is deferred and
        # fired from `_on_turn_released` so the in-flight turn isn't torn
        # down mid-reply. Same shared mechanism OpenAI uses for its
        # proactive-watchdog deferral.
        self._deferred_reconnect = Deferred()
        # Pause turn acquisition while a reconnect is in progress so
        # the daemon doesn't try to send audio into a half-open WS.
        self._connected_event: asyncio.Event = asyncio.Event()

        self._outage = OutageTracker()
        # Rate gate for `request_reconnect_now`.
        self._reconnect_nudge = ReconnectNudge()

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

    def set_failure_escalation_cb(self, cb: CuePlayer | None) -> None:
        """Wire the cue player for a terminal connection failure. The
        daemon calls this once the ``WakeLoop`` exists."""
        self._outage.set_callback(cb)

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
        context-reset reopen.

        A terminal first connect leaves the connection FAILED and returns
        rather than raising — see ``survive_terminal_initial_connect``."""
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
        # Cancel background tasks first so they don't fight us during teardown.
        for task in (self._supervisor_task, self._rotate_task, self._receive_task):
            if task is not None:
                task.cancel()
        for task in (self._supervisor_task, self._rotate_task, self._receive_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._supervisor_task = None
        self._rotate_task = None
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
            raise RuntimeError(f"{self._log_tag} in FAILED state; daemon paused")
        if self._state is ConnectionState.CLOSED:
            raise RuntimeError(f"{self._log_tag} closed")

        await await_connected(self)
        await self._maybe_reset_context()

        async with self._turn_lock:
            if self._active_turn is not None:
                raise RuntimeError(f"{self._log_tag} a turn is already active")
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
            except BaseException:  # noqa: BLE001
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

    def last_failure_detail(self) -> str | None:
        return self._outage.detail

    def wake_cue(self) -> str:
        return self._outage.wake_cue

    def request_reconnect_now(self) -> bool:
        """Cut the current backoff wait short and retry at once.

        The daemon calls this when it refuses a wake because the
        connection is paused: during the 15-minute terminal poll the
        wake word is the household asking whether the outage is over,
        and they should not wait out the interval. Rate-gated, so
        repeated wakes cannot outpace the transient ramp."""
        if not self.is_paused():
            return False
        if not self._reconnect_nudge.allow():
            return False
        self._nudge_event.set()
        return True

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
                f"{self._log_tag} aged out %d un-ack activity_end(s) "
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
                f"{self._log_tag} _send_audio_blob called with self._session=None "
                "(state=%s, connected_event=%s, receive_task=%s)",
                self._state.value,
                self._connected_event.is_set(),
                "running" if self._receive_task and not self._receive_task.done() else "done/none",
            )
            raise RuntimeError(f"{self._log_tag} no active session")
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type=self.INPUT_MIME)
        )

    async def _send_text_context(self, text: str) -> None:
        if self._session is None:
            raise RuntimeError(f"{self._log_tag} no active session")
        await self._session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)],
            ),
            turn_complete=False,
        )

    async def _on_turn_released(self, turn: GeminiLiveTurn) -> None:
        async with self._turn_lock:
            if self._active_turn is turn:
                self._active_turn = None
                self._last_turn_end_at = asyncio.get_event_loop().time()
        async with self._state_lock:
            if self._state is ConnectionState.IN_TURN:
                self._set_state(ConnectionState.CONNECTED)
        # Fire any reconnect deferred for this turn — a mid-turn GoAway
        # or a planned rotation that came due while the user was talking.
        if self._deferred_reconnect.fire_if_pending(self._reconnect_event.set):
            logger.info(
                f"{self._log_tag} turn just ended, firing the deferred "
                "reconnect (planned=%s)", self._planned_rotate,
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
            #
            # Barge-in (flag JASPER_BARGE_IN_GEMINI, DEFAULT OFF) keeps THIS
            # config — manual VAD + NO_INTERRUPTION — even when enabled:
            # option (a). The
            # daemon's local Silero-on-AEC gate (request_local_interrupt) is
            # the sole interruption authority, so this connection never reads
            # the flag and the flag-OFF/-ON payloads are identical. We do NOT
            # enable server VAD (option b): it would re-open the
            # self-interrupt-on-bleed loop this line prevents. Pinned by
            # tests/test_gemini_barge_in.py.
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

    def _build_session_resumption(self) -> "types.SessionResumptionConfig":
        # Always send the field. The server only emits
        # `SessionResumptionUpdate` when the setup message asked for
        # resumption, so omitting it on the first connect meant we never
        # received a handle and every reconnect started cold
        # (https://ai.google.dev/api/live). `handle=None` is the
        # documented "start a new session but do send me handles" form.
        return types.SessionResumptionConfig(handle=self._resumption_handle)

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTING)
        try:
            await run_initial_connect(
                self, DEFAULT_INITIAL_CONNECT_BUDGET_SEC,
            )
        except Exception as e:  # noqa: BLE001
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            survive_terminal_initial_connect(e, self._trigger_reconnect)

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

    async def _open_session_attempt(self) -> None:
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
        except Exception:  # noqa: BLE001
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
        connect_ms = (_time.monotonic() - t0) * 1000
        handle_short = (self._resumption_handle or "")[:8] or "<new>"
        logger.info(
            f"{self._log_tag} connect ok in %.0fms (resumption=%s)",
            connect_ms, handle_short,
        )
        self._receive_task = asyncio.create_task(self._receive_loop())
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()
        self._start_rotate_watchdog()

    def _start_rotate_watchdog(self) -> None:
        """Arm the planned-rotation timer for the session just opened.

        No-op when `rotate_after_sec` is 0 — bare construction in tests
        doesn't spawn a surprise task."""
        if self._rotate_after_sec <= 0:
            return
        self._rotate_task = asyncio.create_task(
            self._rotate_watchdog(self._rotate_after_sec),
            name="jasper-gemini-rotate-watchdog",
        )

    async def _rotate_watchdog(self, delay_sec: float) -> None:
        """Sleep out the session's useful life, then roll it deliberately.

        The point is to replace a server-side 1008 abort — which lands as
        a WARNING, a lost turn and a ~1.4 s socket gap — with a quiet
        reconnect we choose the moment for. A turn in flight defers to
        `_on_turn_released` via the shared `Deferred`, exactly as the
        GoAway path does."""
        try:
            await asyncio.sleep(delay_sec)
        except asyncio.CancelledError:
            raise
        if self._state in (
            ConnectionState.RECONNECTING,
            ConnectionState.PAUSED_FOR_BACKOFF,
            ConnectionState.FAILED,
            ConnectionState.CLOSED,
        ):
            return
        if self._active_turn is not None:
            self._planned_rotate = True
            log_event(
                logger,
                "session.rotate",
                reason="planned",
                outcome="deferred",
                after_sec=round(delay_sec),
            )
            self._deferred_reconnect.request()
            return
        self._planned_rotate = True
        log_event(
            logger,
            "session.rotate",
            reason="planned",
            outcome="now",
            after_sec=round(delay_sec),
            resumption=(self._resumption_handle or "")[:8] or "<new>",
        )
        self._reconnect_event.set()

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
        # Cancel the rotate watchdog first — it only makes sense against a
        # live session, and we are about to drop this one.
        if self._rotate_task is not None:
            self._rotate_task.cancel()
            try:
                await asyncio.wait_for(self._rotate_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._rotate_task = None
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await asyncio.wait_for(self._receive_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        # Only now can the handle be dropped for good: until the receive
        # task above was cancelled it could still land a late
        # `session_resumption_update` and resurrect the old context.
        if self._drop_resumption_on_teardown:
            self._drop_resumption_on_teardown = False
            self._resumption_handle = None
        if self._session is not None:
            try:
                # Send close frame and wait for server ack so the
                # server-side session is actually torn down before
                # we (or anyone else) opens a new WS.
                await asyncio.wait_for(self._session.close(), timeout=3.0)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug(f"{self._log_tag} session.close() error (ignored): %s", e)
        if self._session_cm is not None:
            try:
                await asyncio.wait_for(
                    self._session_cm.__aexit__(None, None, None), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug(f"{self._log_tag} session __aexit__ error (ignored): %s", e)
        self._session_cm = None
        self._session = None
        self._connected_event.clear()
        teardown_ms = (_time.monotonic() - t0) * 1000
        logger.info(f"{self._log_tag} session torn down in %.0fms", teardown_ms)

    def _trigger_reconnect(self) -> None:
        """Wake the supervisor for an UNPLANNED reconnect.

        Clears any queued planned-rotation flag so a genuine failure can
        never inherit the rotation's zero-backoff first attempt."""
        self._planned_rotate = False
        self._reconnect_event.set()

    def _on_reconnect_attempt_failed(
        self, exc: Exception, attempt: int, transient: bool,
    ) -> None:
        is_409, status = _is_409_conflict(exc)
        handle_short = (
            (self._resumption_handle or "")[:8]
            if self._resumption_handle
            else "<none>"
        )
        if is_409:
            logger.warning(
                f"{self._log_tag} reconnect 409 Conflict on attempt "
                "%d (status=%s, exc=%s, handle=%s)",
                attempt, status, type(exc).__name__, handle_short,
            )
        else:
            logger.warning(
                f"{self._log_tag} reconnect attempt %d failed "
                "(%s: %s, handle=%s)",
                attempt, type(exc).__name__,
                self._outage.detail, handle_short,
            )
        # Drop the cached handle on the first failure of ANY kind, not
        # just a 409: a server-invalidated handle also surfaces as
        # WebSocket close 1008 "BidiGenerateContent session expired".
        # Keeping a stale one costs the whole session; dropping a good
        # one costs a turn of context. See ADR-0166.
        if self._resumption_handle is not None:
            logger.warning(
                f"{self._log_tag} reconnect dropping cached "
                "resumption handle (handle=%s) after first "
                "failure; next attempt will connect fresh",
                handle_short,
            )
            self._resumption_handle = None

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
        # Capture the session once, locally — if the connection is
        # torn down (and `self._session` is reassigned to None or to
        # a brand-new session), this loop stays bound to the session
        # it was started for, so cancellation can complete cleanly
        # without splicing two sessions' message streams together.
        session = self._session
        if session is None:
            logger.warning(
                f"{self._log_tag} receive_loop started with self._session=None; "
                "exiting (likely a stale cancelled task post-teardown)"
            )
            return
        try:
            while True:
                response = await session._receive()
                if response is None:
                    # Underlying connection closed cleanly — let the
                    # supervisor drive a reconnect.
                    logger.warning(
                        f"{self._log_tag} _receive returned None (clean close), reconnecting"
                    )
                    self._trigger_reconnect()
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
                            f"{self._log_tag} GoAway received mid-turn, "
                            "time_left=%s (%.0fs) ≥ %.0fs — deferring reconnect "
                            "until turn release",
                            time_left, secs, GOAWAY_DEFER_MIN_TIME_LEFT_SEC,
                        )
                        self._deferred_reconnect.request()
                        continue
                    logger.warning(
                        f"{self._log_tag} GoAway received, time_left=%s, will reconnect",
                        time_left,
                    )
                    self._trigger_reconnect()
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
                #     A belated turn_complete from turn N-1 typically
                #     arrives 30 ms after we send activity_start for
                #     turn N; routing it to turn N would set
                #     server_turn_complete=True and let the idle
                #     watchdog close turn N 1.5 s later — before
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
            close_code, close_reason = _close_code_and_reason(e)
            if close_code is not None:
                logger.warning(
                    f"{self._log_tag} disconnected (code=%s reason=%r), reconnecting",
                    close_code, close_reason,
                )
            else:
                logger.warning(
                    f"{self._log_tag} receive loop error (%s: %s), reconnecting",
                    type(e).__name__, e,
                )
            self._trigger_reconnect()

    async def _maybe_reset_context(self) -> None:
        """If the connection has been idle longer than the configured
        threshold AND we have at least one previous turn, drop the
        resumption handle and reopen with a fresh session.

        Disabled by default (`context_reset_sec=0`). Enable only if
        you actually observe stale-context glitches: each reset busts
        the resumption handle (so the next turn re-establishes session
        state at full cost) and blocks the wake event for the reopen.
        The terse-tool system prompt makes stale-context bleed a
        mostly-hypothetical concern in practice."""
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
        # Dropped in `_teardown_session`, not here: the old session's
        # receive loop runs until the supervisor cancels it and would
        # otherwise re-cache a handle for the context being discarded.
        self._drop_resumption_on_teardown = True
        request_planned_reopen(self)
        await await_connected(self)
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
        tool dispatches. Optional for back-compat — the
        caller in ``GeminiLiveTurn._on_response`` always passes it.
        """
        assert self._registry is not None
        responses = []
        t0 = _time.monotonic()
        for fc in tool_call.function_calls:
            if turn is not None:
                turn._record_tool_call_name(fc.name)
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
