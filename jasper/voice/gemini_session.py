# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time

from google import genai
from google.genai import types
from google.genai.live import AsyncSession

from ..log_event import log_event
from ..secret_redaction import redact_secrets
from ._base import BaseLiveConnection, BaseLiveTurn, ToolCall
from ._supervisor import (
    await_connected,
    http_status,
    request_planned_reopen,
    request_unplanned_reopen,
)
from .session import AudioOutChunk, ConnectionState, LiveTurn, TurnCapture

logger = logging.getLogger(__name__)


# Planned session rotation. On gemini-3.1-flash-live-preview the server
# aborts an idle session at 150.1 s ±1 s with WebSocket close 1008 and no
# preceding GoAway (measured on jts4: n=516, floor 150.06 s; WS pings do
# not count as activity). Rotating at 135 s keeps ~15 s of headroom over
# that floor — comfortably more than the p99 connect (5.3 s) plus
# teardown. Raise only with a fresh lifetime measurement; the cap is
# per-model and undocumented.
SESSION_ROTATE_AFTER_SEC = 135.0

# GoAway deferral threshold. When the server sends a GoAway mid-turn
# (it fires near the ~15-min audio cap and can land while the user is
# still mid-reply), we don't want to tear the session down and lose the
# in-flight turn. If the GoAway's `time_left` is at least as long as the
# longest a turn can run, defer the reconnect until the turn is
# released. A user turn is bounded by the daemon's hard recording cap
# (HARD_RECORDING_CAP_SEC = 30 s in voice_daemon) and usually ends
# sooner via the idle watchdog (JASPER_IDLE_TIMEOUT_SEC, default 20 s),
# so a 30 s threshold lets a turn run to completion inside the deferred
# window; a test pins `threshold >= HARD_RECORDING_CAP_SEC` so a future
# cap bump can't silently make deferral unsafe. Fail-safe either way:
# if `time_left` is below this (or unparseable, or no turn is active) we
# reconnect promptly, and if a deferred turn still overruns `time_left`
# the server just drops the WS and the supervisor reconnects — the same
# outcome as reconnecting now.
GOAWAY_DEFER_MIN_TIME_LEFT_SEC = 30.0


def _is_progress_response(response) -> bool:
    """True when this message proves the turn is making progress, so it
    advances the pre-response idle anchor: audio, a tool call,
    transcript text in either direction, turn_complete, or
    generation_complete.

    Connection bookkeeping — `session_resumption_update`, `go_away`, a
    bare usage snapshot — proves only that the socket is open and must
    not reset the anchor. See issue #4532.
    """
    if getattr(response, "data", None) or getattr(response, "tool_call", None) is not None:
        return True
    sc = getattr(response, "server_content", None)
    if sc is None:
        return False
    return bool(
        getattr(sc, "turn_complete", False)
        or getattr(sc, "generation_complete", False)
        or getattr(getattr(sc, "input_transcription", None), "text", None)
        or getattr(getattr(sc, "output_transcription", None), "text", None)
    )


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


class GeminiLiveTurn(BaseLiveTurn):
    """A single turn against an open `GeminiLiveConnection`."""

    def __init__(
        self,
        conn: "GeminiLiveConnection",
        started_at: float,
    ) -> None:
        super().__init__(conn, started_at)
        self._conn: GeminiLiveConnection = conn
        self._session = getattr(conn, "_session", None)
        self._activity_end_sent = False
        self._tool_responses: list[types.FunctionResponse] = []

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._released or self._turn_lost or self._activity_end_sent:
            return
        try:
            if await self._conn._send_realtime_input(self, audio=types.Blob(
                data=pcm_16khz_int16, mime_type=self._conn.INPUT_MIME,
            )):
                self._bytes_sent += len(pcm_16khz_int16)
        except Exception as e:  # noqa: BLE001
            # The connection's reconnect supervisor will pick up the WS
            # drop. Mark the turn as lost so the daemon stops trying.
            log_event(
                self._conn._logger, "provider.send_failed", provider=self._conn.PROVIDER_NAME,
                exc_type=type(e).__name__,
                detail=self._conn._redacted(e),
                outcome="turn_lost", level=logging.WARNING,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

    async def send_text_context(self, text: str) -> None:
        if self._released or self._turn_lost:
            return
        await self._conn._send_text_context(self, text)

    async def end_input(self) -> None:
        """Send `activity_end` to the server. Idempotent."""
        if self._activity_end_sent or self._released or self._turn_lost:
            return
        self._activity_end_sent = True
        self._end_input_at_monotonic = _time.monotonic()
        try:
            await self._conn._send_realtime_input(self, activity_end=types.ActivityEnd())
        except Exception as e:  # noqa: BLE001
            log_event(
                self._conn._logger, "provider.end_input_failed", provider=self._conn.PROVIDER_NAME,
                exc_type=type(e).__name__,
                detail=self._conn._redacted(e),
                outcome="turn_lost", level=logging.DEBUG,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._cancel_tools()
        self.drop_pending_audio()
        self._audio_q.put_nowait(None)
        self._log_release()
        await self._conn._on_turn_released(self)

    def capture(self) -> TurnCapture | None:
        user = self.user_transcript().strip() or None
        assistant = self.assistant_transcript().strip() or None
        data: dict[str, object] = {
            "kind": "voice_turn",
            "transcripts_available": user is not None or assistant is not None,
        }
        if self._dispatched_tools:
            data["tools"] = list(self._dispatched_tools)
        return TurnCapture(user_text=user, assistant_text=assistant, data=data)

    async def cancel_response(self, reason: str) -> None:
        if (self._released or self._turn_lost or not self._activity_end_sent
                or self._cancel_requested):
            return
        self._cancel_requested = True
        self._cancel_tools()
        self.drop_pending_audio()
        if not self._server_turn_complete:
            try:
                await self._conn._send_realtime_input(self, activity_start=types.ActivityStart())
            except Exception as e:  # noqa: BLE001
                log_event(
                    self._conn._logger, "barge.cancel_failed", provider=self._conn.PROVIDER_NAME,
                    exc_type=type(e).__name__,
                    detail=self._conn._redacted(e),
                    level=logging.WARNING,
                )
                self._on_connection_lost()
        log_event(
            self._conn._logger, "barge.cancel", reason=reason,
            provider=self._conn.PROVIDER_NAME,
        )

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        # No-op: no conversation.item.truncate equivalent. `provider_item_id`
        # is expected to be None for Gemini (no per-response audio item id);
        # arguments are accepted and ignored so callers need no provider
        # branch.
        return None

    # Internal — called by the connection's receive loop when it routes
    # an incoming server message to this active turn.
    async def _on_response(self, response) -> None:
        if self._server_turn_complete:
            return
        # Audio frames live on response.data (raw 24 kHz int16 PCM).
        data = getattr(response, "data", None)
        if data and not self._cancel_requested and not self._server_turn_complete:
            self._note_audio_chunk(_time.monotonic())
            self._enqueue_audio(AudioOutChunk(pcm=data))

        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            self._tool_round_pending = True
            self._start_tool_calls([
                ToolCall(id=fc.id, name=fc.name, args=dict(fc.args or {}))
                for fc in tool_call.function_calls
            ])
            self._tool_responses = []

        # Server content: turn_complete + interrupted.
        sc = getattr(response, "server_content", None)
        if sc is not None:
            if not self._cancel_requested:
                text = getattr(getattr(sc, "output_transcription", None), "text", None)
                if isinstance(text, str) and text:
                    self.add_transcript(assistant=text)
            if getattr(sc, "turn_complete", False) and not self._server_turn_complete:
                self._cancel_tools()
                self._note_activity()
                self._server_turn_complete = True
                self._audio_q.put_nowait(None)
            if getattr(sc, "interrupted", False):
                # Drop any audio chunks queued ahead of this point — they
                # are pre-interrupt and should NOT be played to the user.
                self._cancel_requested = True
                self._cancel_tools()
                self.drop_pending_audio()
                self._interrupt_event.set()
                log_event(
                    self._conn._logger, "turn.interrupted", provider=self._conn.PROVIDER_NAME,
                    reason="user", level=logging.INFO,
                )

        # Retained context is billed anew each turn; snapshots replace, not add:
        # https://ai.google.dev/gemini-api/docs/live-api/best-practices#pricing-and-billing
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            self.set_usage(
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "response_token_count", None),
            )

    def _tools_may_run(self) -> bool:
        return (
            self._conn._owns_turn(self)
            and not self._cancel_requested and not self._server_turn_complete
        )

    async def _send_tool_result(self, call: ToolCall, payload: dict) -> bool:
        """Gemini takes the whole round in one `send_tool_response`."""
        self._tool_responses.append(
            types.FunctionResponse(id=call.id, name=call.name, response=payload)
        )
        self._note_activity()
        return True

    async def _finish_tool_round(self) -> bool:
        async with self._conn._send_lock:
            if not self._tools_may_run():
                return False
            assert self._session is not None
            await self._session.send_tool_response(function_responses=self._tool_responses)
            self._tool_round_pending = False
        if self._conn._owns_turn(self):
            self._note_activity()
        return True


class GeminiLiveConnection(BaseLiveConnection):
    """Long-lived Gemini Live connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and the wire half of the lifecycle ``BaseLiveConnection``
    drives — surviving the 15-min audio cap via `sessionResumption` and
    reconnecting on GoAway / 1006 / 1011.

    Audio shape: input 16-bit PCM @ 16 kHz mono, output 16-bit PCM @ 24 kHz
    mono. Manual VAD: automatic_activity_detection.disabled = True; the
    daemon sends `activity_start` on wake and `activity_end` on idle.
    """

    PROVIDER_NAME = "gemini"
    _logger = logger
    # The watchdog below is a rotation this connection schedules, not a
    # failure: its first reconnect attempt skips the backoff wait.
    _watchdog_is_planned = True

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
        # schedule without sleeping it.
        sleep=None,
    ) -> None:
        super().__init__(
            model=model,
            voice=voice,
            context_reset_sec=context_reset_sec,
            backoff_schedule=backoff_schedule,
            sleep=sleep,
        )
        self._api_key = api_key
        self._client = genai.Client(api_key=api_key) if connect_factory is None else None
        self._connect_factory = connect_factory
        self._rotate_after_sec = rotate_after_sec

        # Active SDK session + context manager (cleared during reconnect).
        self._send_lock = asyncio.Lock()
        self._session: AsyncSession | None = None
        self._session_cm: contextlib.AbstractAsyncContextManager[AsyncSession] | None = None
        self._input_transcript_turn: GeminiLiveTurn | None = None
        self._input_transcript_ambiguous = False

        # Latest session-resumption handle from the server. Used on
        # reconnect to resume the conversation. Cleared explicitly when
        # the idle-context-reset fires.
        self._resumption_handle: str | None = None
        # One-shot: the idle context reset raises it so `_teardown_session`
        # drops the handle once the old receive loop can no longer
        # repopulate it. A planned rotation deliberately keeps its handle
        # (ADR-0166), so nothing else may set this.
        self._drop_resumption_on_teardown = False

    def _secret_literals(self) -> tuple[str, ...]:
        """The API key, so a rejection body that echoes it still redacts.

        `_KEY_PREFIX_RE` in `secret_redaction.py` knows the `AIza`
        shape; a prefix-less or rotated key can miss it, and this is
        the fallback (ADR-0243).
        """
        return (self._api_key,) if self._api_key else ()

    # ------------------------------------------------------------------
    # LiveConnection protocol
    # ------------------------------------------------------------------

    async def acquire_turn(self) -> LiveTurn:
        await self._await_acquirable()

        async with self._turn_lock:
            if self._active_turn is not None:
                raise RuntimeError(f"{self._log_tag} a turn is already active")
            await await_connected(self)
            now_loop = asyncio.get_event_loop().time()
            turn = GeminiLiveTurn(self, started_at=now_loop)
            # Used by GeminiLiveTurn for elapsed-ms logging.
            turn._started_at_monotonic = _time.monotonic()
            self._active_turn = turn
            try:
                sent = await self._send_realtime_input(turn, activity_start=types.ActivityStart())
                if not sent or not self._owns_turn(turn):
                    raise RuntimeError("Gemini session changed during turn acquisition")
            except BaseException:  # noqa: BLE001
                # The turn never started — roll the slot back, or every
                # later acquire_turn() gets "a turn is already active"
                # until a reconnect happens to clear it.
                if self._active_turn is turn:
                    self._active_turn = None
                raise
            async with self._state_lock:
                if self._state is ConnectionState.CONNECTED:
                    self._set_state(ConnectionState.IN_TURN)
            log_event(
                self._logger, "turn.started", provider=self.PROVIDER_NAME, level=logging.INFO,
            )
            return turn

    # ------------------------------------------------------------------
    # Internal — turn-side helpers
    # ------------------------------------------------------------------

    async def _send_realtime_input(self, turn: GeminiLiveTurn, **kwargs) -> bool:
        async with self._send_lock:
            if not self._owns_turn(turn) or (
                ("audio" in kwargs or "text" in kwargs) and turn._activity_end_sent
            ):
                return False
            assert turn._session is not None
            if "audio" in kwargs and not self._input_transcript_ambiguous:
                if self._input_transcript_turn not in (None, turn):
                    # No request IDs: overlapping unfinished input cannot be
                    # attributed again until transport teardown clears it.
                    self._input_transcript_ambiguous = True
                    self._input_transcript_turn = None
                else:
                    self._input_transcript_turn = turn
            await turn._session.send_realtime_input(**kwargs)
            return True

    def _on_input_transcription(self, transcription) -> None:
        turn = self._input_transcript_turn
        if turn is not None and self._owns_turn(turn) and not turn._cancel_requested:
            text = getattr(transcription, "text", None)
            if isinstance(text, str) and text:
                turn.add_transcript(user=text)
        # Input transcription is unordered relative to response completion:
        # https://ai.google.dev/api/live#bidigeneratecontentservercontent
        if getattr(transcription, "finished", False):
            self._input_transcript_turn = None

    async def _send_text_context(self, turn: GeminiLiveTurn, text: str) -> None:
        await self._send_realtime_input(turn, text=text)

    async def _on_turn_released(self, turn: GeminiLiveTurn) -> None:
        async with self._send_lock:
            if (self._active_turn is turn and self._session is not None
                    and turn._session is self._session
                    and (turn._cancel_requested or not turn._server_turn_complete
                         or turn._tool_round_pending)):
                # Gemini has no client clear-buffer call. Reopen without the
                # old handle so abandoned input and tool calls cannot resume.
                self._on_context_reset()
                request_planned_reopen(self)
        await super()._on_turn_released(turn)

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
            gen_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW,
            )
        except Exception:  # noqa: BLE001
            pass
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=instruction or None,
            tools=(
                [types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration.model_validate(d) for d in decls
                    ],
                )]
                if decls
                else None
            ),
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
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Manual activity remains client-owned, including interruption.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
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

    async def _open_session_attempt(self) -> None:
        """Open a fresh SDK session against the current config and start
        the receive loop. Raises if the connect fails."""
        config = self._build_config()
        if self._connect_factory is not None:
            connect_call = self._connect_factory
        else:
            assert self._client is not None
            connect_call = self._client.aio.live.connect
        t0 = _time.monotonic()
        cm = connect_call(model=self._model, config=config)
        try:
            session = await cm.__aenter__()
            if session.setup_complete is None:
                await self._close_with_timeout(session)
                raise RuntimeError("Gemini session has no setup acknowledgement")
        except BaseException:  # noqa: BLE001
            await self._close_cm_with_timeout(cm)
            raise
        self._session_cm = cm
        self._session = session
        connect_ms = (_time.monotonic() - t0) * 1000
        log_event(
            self._logger, "provider.connected", provider=self.PROVIDER_NAME,
            ms=round(connect_ms), resumption=self._resumption_handle is not None,
            level=logging.INFO,
        )
        await self._mark_connected(asyncio.create_task(self._receive_loop()))

    def _watchdog_delay_sec(self) -> float:
        """How long a session runs before the planned rotation rolls it.

        Replaces a server-side 1008 idle abort — a WARNING, a lost turn
        and a ~1.4 s socket gap — with a quiet reconnect we choose the
        moment for. 0 disables it."""
        return self._rotate_after_sec

    async def _teardown_session(self) -> None:
        """See `_supervisor.SupervisedConnection`."""
        t0 = _time.monotonic()
        session, cm = self._session, self._session_cm
        turn = self._active_turn
        if turn is not None:
            turn._cancel_tools()
            if turn._cancel_requested or not turn._server_turn_complete or turn._tool_round_pending:
                self._on_context_reset()
        self._session = self._session_cm = None
        self._connected_event.clear()
        # Cancel the rotation watchdog first — it only makes sense against
        # a live session, and we are about to drop this one.
        await self._cancel_task(self._proactive_watchdog_task)
        self._proactive_watchdog_task = None
        await self._cancel_task(self._receive_task)
        self._receive_task = None
        self._input_transcript_turn = None
        self._input_transcript_ambiguous = False
        # Only now can the handle be dropped for good: until the receive
        # task above was cancelled it could still land a late
        # `session_resumption_update` and resurrect the old context.
        if self._drop_resumption_on_teardown:
            self._drop_resumption_on_teardown = False
            self._resumption_handle = None
        await self._close_with_timeout(session)
        await self._close_cm_with_timeout(cm)
        self._log_teardown(_time.monotonic() - t0)

    def _on_reconnect_attempt_failed(
        self, exc: Exception, attempt: int, transient: bool,
    ) -> None:
        super()._on_reconnect_attempt_failed(exc, attempt, transient)
        status = http_status(exc)
        if status == 409:
            log_event(
                self._logger, "provider.reconnect_conflict", provider=self.PROVIDER_NAME,
                attempt=attempt, status=status,
                exc_type=type(exc).__name__,
                detail=self._redacted(exc),
                level=logging.WARNING,
            )
        # Drop the cached handle on the first failure of ANY kind, not
        # just a 409: a server-invalidated handle also surfaces as
        # WebSocket close 1008 "BidiGenerateContent session expired".
        # Keeping a stale one costs the whole session; dropping a good
        # one costs a turn of context. See ADR-0166.
        if self._resumption_handle is not None:
            log_event(
                self._logger, "provider.resumption_dropped", provider=self.PROVIDER_NAME,
                status=status, level=logging.WARNING,
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
            log_event(
                self._logger, "provider.session_closed", provider=self.PROVIDER_NAME,
                reason="no_session", level=logging.WARNING,
            )
            return
        try:
            while True:
                response = await session._receive()
                if session is not self._session:
                    return
                if response is None:
                    # Underlying connection closed cleanly — let the
                    # supervisor drive a reconnect.
                    log_event(
                        self._logger, "provider.session_closed", provider=self.PROVIDER_NAME,
                        reason="clean_close", level=logging.WARNING,
                    )
                    request_unplanned_reopen(self)
                    return
                turn = self._active_turn
                if turn is not None and self._owns_turn(turn) and _is_progress_response(response):
                    turn._note_activity()
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
                    deferred = (
                        self._active_turn is not None
                        and secs is not None
                        and secs >= GOAWAY_DEFER_MIN_TIME_LEFT_SEC
                    )
                    log_event(
                        self._logger, "session.goaway", provider=self.PROVIDER_NAME,
                        outcome="deferred" if deferred else "now", time_left_s=secs,
                        time_left_raw=redact_secrets(str(time_left), literals=self._secret_literals()),
                        level=logging.WARNING,
                    )
                    if deferred:
                        self._deferred_reconnect.request()
                        continue
                    request_unplanned_reopen(self)
                    continue
                transcription = getattr(getattr(response, "server_content", None), "input_transcription", None)
                if transcription is not None:
                    self._on_input_transcription(transcription)
                if turn is not None and self._owns_turn(turn) and not turn._server_turn_complete:
                    await turn._on_response(response)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if session is self._session:
                self._on_receive_loop_error(e)

    def _on_context_reset(self) -> None:
        # Dropped in `_teardown_session`, not here: the old session's
        # receive loop runs until the supervisor cancels it and would
        # otherwise re-cache a handle for the context being discarded.
        self._drop_resumption_on_teardown = True

