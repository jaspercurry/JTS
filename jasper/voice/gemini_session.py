from __future__ import annotations

import asyncio
import logging
import time as _time
from enum import Enum
from typing import AsyncIterator, Callable

from google import genai
from google.genai import types

from ..tools import ToolRegistry
from .session import LiveConnection, LiveTurn, VoiceSession

logger = logging.getLogger(__name__)


# Bounded exponential backoff for reconnect attempts. Caps at 8s so the
# daemon recovers within ~15s after a transient drop, but doesn't
# hammer the API into OVERLOADED_TOO_MANY_RETRIES_PER_REQUEST
# (livekit/agents#1679). Total wall-time across the schedule is 15s,
# which fits comfortably under the 30s the user typically waits before
# repeating a wake-word.
RECONNECT_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0)

# Keepalive period — Vertex Live API closes idle connections after 10
# min (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/troubleshooting).
# 4 min gives 6+ min headroom even if the keepalive task lags briefly.
KEEPALIVE_PERIOD_SEC = 240.0


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

    def __init__(self, conn: "GeminiLiveConnection", started_at: float) -> None:
        self._conn = conn
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self._turn_count = 0
        self._interrupted = False
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
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                return
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

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def usage_tokens(self) -> dict[str, int]:
        return dict(self._usage)

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
    # an incoming server message to this active turn. Mirrors the old
    # GeminiLiveSession._dispatch logic.
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
            await self._audio_q.put(data)

        # Tool calls.
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            self._last_activity_at = asyncio.get_event_loop().time()
            await self._conn._handle_tool_call(tool_call)

        # Server content: turn_complete + interrupted.
        sc = getattr(response, "server_content", None)
        if sc is not None:
            if getattr(sc, "turn_complete", False):
                self._turn_count += 1
                self._last_activity_at = asyncio.get_event_loop().time()
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
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            in_tok = getattr(usage, "prompt_token_count", None)
            out_tok = getattr(usage, "response_token_count", None)
            if in_tok is not None:
                self._usage["input_tokens"] = int(in_tok)
            if out_tok is not None:
                self._usage["output_tokens"] = int(out_tok)

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
        context_reset_sec: float = 300.0,
        keepalive_period_sec: float = KEEPALIVE_PERIOD_SEC,
        backoff_schedule: tuple[float, ...] = RECONNECT_BACKOFF_SCHEDULE,
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
        self._state = ConnectionState.IDLE_INIT
        self._state_lock = asyncio.Lock()

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

        # Background tasks: receive loop, keepalive, reconnect supervisor.
        self._receive_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Triggered by the receive loop when it hits a drop / GoAway /
        # exception so the supervisor wakes up and reconnects.
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Pause turn acquisition while a reconnect is in progress so
        # the daemon doesn't try to send audio into a half-open WS.
        self._connected_event: asyncio.Event = asyncio.Event()

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
            self._state = ConnectionState.CLOSED

    async def acquire_turn(self) -> LiveTurn:
        if self._state is ConnectionState.FAILED:
            raise RuntimeError("live connection: in FAILED state; daemon paused")
        if self._state is ConnectionState.CLOSED:
            raise RuntimeError("live connection: closed")

        # If we're mid-reconnect, wait for the connected event so the
        # turn doesn't open against a half-open WS. Bounded so we don't
        # block forever if the connection is permanently down.
        if not self._connected_event.is_set():
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(),
                    timeout=sum(self._backoff_schedule) + 5.0,
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
            turn = GeminiLiveTurn(self, started_at=now_loop)
            # Used by GeminiLiveTurn for elapsed-ms logging.
            turn._started_at_monotonic = _time.monotonic()
            self._active_turn = turn
            await self._send_activity_start()
            async with self._state_lock:
                if self._state is ConnectionState.CONNECTED:
                    self._state = ConnectionState.IN_TURN
            logger.info("live turn: started (activity_start sent)")
            return turn

    def is_paused(self) -> bool:
        return self._state in (
            ConnectionState.RECONNECTING,
            ConnectionState.PAUSED_FOR_BACKOFF,
            ConnectionState.FAILED,
        )

    # ------------------------------------------------------------------
    # Internal — turn-side helpers
    # ------------------------------------------------------------------

    async def _send_activity_start(self) -> None:
        if self._session is None:
            return
        await self._session.send_realtime_input(activity_start=types.ActivityStart())

    async def _send_activity_end(self) -> None:
        if self._session is None:
            return
        await self._session.send_realtime_input(activity_end=types.ActivityEnd())

    async def _send_audio_blob(self, pcm: bytes) -> None:
        if self._session is None:
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
                self._state = ConnectionState.CONNECTED

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
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=instruction or None,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
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
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,
                ),
                activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            ),
            # sessionResumption is how the persistent-single pattern
            # survives the server's 15-min audio cap. On every connect
            # the server sends `session_resumption_update.new_handle`
            # which we cache; on reconnect we pass it back so the
            # conversation context resumes seamlessly.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resumption_handle,
            ),
        )

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._state = ConnectionState.CONNECTING
        last_exc: Exception | None = None
        # 409 Conflict on connect = concurrent-session-limit exceeded
        # on Google's side (Tier 0=3, Tier 1=50, Tier 2=1000 per
        # project — see https://discuss.ai.google.dev/t/is-the-gemini-live-api-rate-limit-per-key-or-per-user/78114).
        # Server-side session teardown lags client-side close, so rapid
        # open/close cycles (e.g. wake false-fires on music) can race
        # past the ceiling transiently. Belt-and-suspenders: even with
        # the persistent-single rework removing per-wake churn, leave
        # this 409-specific retry in place so we recover cleanly if
        # the daemon restart hits a slot that's still being torn down.
        for attempt, delay in enumerate([0.0, 1.0, 2.0, 4.0]):
            if delay > 0:
                logger.warning(
                    "live connection: connect retry %d after %.1fs (last: %s)",
                    attempt, delay, last_exc,
                )
                await asyncio.sleep(delay)
            try:
                await self._open_session()
                return
            except Exception as e:  # noqa: BLE001
                status = getattr(getattr(e, "response", None), "status_code", None)
                ws_code = getattr(getattr(e, "rcvd", None), "code", None)
                last_exc = e
                is_409 = status == 409 or "409" in str(e) or "Conflict" in str(e)
                if not is_409:
                    async with self._state_lock:
                        self._state = ConnectionState.FAILED
                    raise
                logger.warning(
                    "live connection: connect 409 Conflict (status=%s ws=%s); will retry",
                    status, ws_code,
                )
        # Exhausted retries.
        async with self._state_lock:
            self._state = ConnectionState.FAILED
        raise RuntimeError(
            f"live connection: connect failed after retries; last error: {last_exc}"
        )

    async def _open_session(self) -> None:
        """Open a fresh SDK session against the current config and start
        the receive loop. Raises if the connect fails."""
        config = self._build_config()
        connect_call = (
            self._connect_factory
            if self._connect_factory is not None
            else self._client.aio.live.connect
        )
        t0 = _time.monotonic()
        self._session_cm = connect_call(model=self._model, config=config)
        self._session = await self._session_cm.__aenter__()
        connect_ms = (_time.monotonic() - t0) * 1000
        handle_short = (self._resumption_handle or "")[:8] or "<new>"
        logger.info(
            "live connection: connect ok in %.0fms (resumption=%s)",
            connect_ms, handle_short,
        )
        self._reconnect_event.clear()
        self._receive_task = asyncio.create_task(self._receive_loop())
        async with self._state_lock:
            self._state = ConnectionState.CONNECTED
        self._connected_event.set()

    async def _teardown_session(self) -> None:
        """Tear down whatever's currently open — session + receive task —
        without affecting the supervisor. Used both on normal close and
        as a step in reconnect."""
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("live connection: session.close() error (ignored): %s", e)
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.debug("live connection: session __aexit__ error (ignored): %s", e)
        self._session_cm = None
        self._session = None
        self._connected_event.clear()

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
            self._state = ConnectionState.RECONNECTING
        # Tear down the old session before opening a new one so we don't
        # leak a half-open WS through the SDK.
        await self._teardown_session()
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
        for attempt, delay in enumerate(self._backoff_schedule, start=1):
            if self._stopping.is_set():
                return
            async with self._state_lock:
                self._state = ConnectionState.PAUSED_FOR_BACKOFF
            logger.info(
                "live connection: reconnect attempt %d after %.1fs backoff", attempt, delay,
            )
            await asyncio.sleep(delay)
            if self._stopping.is_set():
                return
            try:
                await self._open_session()
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                logger.warning(
                    "live connection: reconnect attempt %d failed (%s: %s)",
                    attempt, type(e).__name__, e,
                )
        # Exhausted retries.
        async with self._state_lock:
            self._state = ConnectionState.FAILED
        logger.error(
            "live connection: failed after %d retries; daemon will pause. Last error: %s",
            len(self._backoff_schedule), last_exc,
        )

    async def _receive_loop(self) -> None:
        """Iterate the SDK's `session.receive()` and route messages.

        Audio chunks / tool calls / turn_complete / interrupted go to
        the active turn (if any). Connection-level messages
        (`session_resumption_update`, `go_away`) update connection
        state directly. On any exception the receive loop wakes the
        supervisor to drive a reconnect."""
        assert self._session is not None
        try:
            async for response in self._session.receive():
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
                    logger.warning(
                        "live connection: GoAway received, time_left=%s, will reconnect",
                        time_left,
                    )
                    # Don't break here; let the server-side close drive
                    # the receive loop's exit. But pre-warm the supervisor
                    # so the backoff timer starts ticking now.
                    self._reconnect_event.set()
                    continue
                # Per-turn: route everything else to the active turn.
                turn = self._active_turn
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
                try:
                    # Sending an empty audio blob with stream-end-False
                    # is a cheap way to keep the WS hot. We don't want
                    # to send activity_start/end here as that would race
                    # with the daemon's manual VAD.
                    await self._session.send_realtime_input(
                        audio=types.Blob(data=b"", mime_type=self.INPUT_MIME)
                    )
                    logger.debug("live connection: keepalive sent")
                except Exception as e:  # noqa: BLE001
                    # If keepalive fails the WS is already broken — let
                    # the receive loop trigger reconnect. Don't double-trip.
                    logger.debug(
                        "live connection: keepalive send failed (%s: %s)",
                        type(e).__name__, e,
                    )
        except asyncio.CancelledError:
            raise

    async def _maybe_reset_context(self) -> None:
        """If the connection has been idle longer than the configured
        threshold AND we have at least one previous turn, drop the
        resumption handle and reopen with a fresh session.

        Without this, conversational context bleeds across hour-long
        gaps — "what time is it?" at 9 AM and 5 PM, the second one
        shouldn't remember weather queries from the morning."""
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
        await self._open_session()
        # Reset the idle marker so we don't immediately re-trigger.
        self._last_turn_end_at = asyncio.get_event_loop().time()

    async def _handle_tool_call(self, tool_call) -> None:
        """Dispatch tool calls from the model with structured timing logs.

        Log format per call:
          tool {name} start args={...}                      [t=0.000s]
          tool {name} fn done in 412ms ok payload={...}     [HTTP + parsing]
          tool {name} response sent to Gemini in 614ms      [total round-trip]
        Failure paths log `timed out` or `raised:` with the same elapsed.
        """
        assert self._registry is not None
        responses = []
        t0 = _time.monotonic()
        for fc in tool_call.function_calls:
            tool = self._registry.get(fc.name)
            args = dict(fc.args or {})
            if tool is None:
                payload: dict = {"error": f"unknown tool {fc.name}"}
                logger.warning("tool %s start args=%s → unknown tool", fc.name, args)
            else:
                logger.info("tool %s start args=%s", fc.name, args)
                t_fn = _time.monotonic()
                try:
                    out = tool.fn(**args)
                    if asyncio.iscoroutine(out):
                        # 12s gives async tool calls (httpx HTTP +
                        # parsing) headroom on a busy Pi event loop
                        # where ONNX wake-word + audio resampling +
                        # Gemini WebSocket compete for CPU. Anything
                        # slower than that probably means the upstream
                        # API is genuinely failing — we'd rather report
                        # the timeout than hang the session further.
                        out = await asyncio.wait_for(out, timeout=12.0)
                    # Pass dict outputs straight through; only wrap scalars
                    # so the model doesn't see {"result": {"ok": true}}.
                    payload = out if isinstance(out, dict) else {"value": out}
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    # Truncate the payload preview — weather/subway
                    # responses can be 4-8 KB and flood the journal.
                    preview = repr(payload)
                    if len(preview) > 240:
                        preview = preview[:237] + "..."
                    logger.info(
                        "tool %s fn done in %.0fms ok payload=%s",
                        fc.name, fn_ms, preview,
                    )
                except asyncio.TimeoutError:
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    payload = {"error": f"{fc.name} timed out"}
                    logger.warning(
                        "tool %s fn TIMED OUT after %.0fms", fc.name, fn_ms,
                    )
                except Exception as e:  # noqa: BLE001
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    payload = {"error": str(e)}
                    logger.warning(
                        "tool %s fn RAISED after %.0fms: %s",
                        fc.name, fn_ms, e,
                    )
            responses.append(
                types.FunctionResponse(
                    id=fc.id, name=fc.name, response=payload
                )
            )
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


class GeminiLiveSession(VoiceSession):
    """Legacy per-wake adapter, kept for backward compatibility.

    Predates the persistent-connection rework. New code should use
    `GeminiLiveConnection` (long-lived) and `GeminiLiveTurn` (per wake)
    instead. The voice daemon now runs against the new types; this class
    is preserved so the existing test_gemini_session.py tests keep
    passing and any out-of-tree consumers don't immediately break.

    Audio shape: input 16-bit PCM @ 16 kHz mono, output 16-bit PCM @ 24 kHz
    mono. Tool calls arrive on response.tool_call; we dispatch the registered
    callable and reply with send_tool_response.

    Lifecycle: turn_count() returns the number of completed turns observed
    (so the idle watchdog can detect "model just finished a turn"). When the
    daemon ends input it calls end_input() which fires audio_stream_end=True
    so the server flushes any cached audio.
    """

    INPUT_MIME = "audio/pcm;rate=16000"

    def __init__(self, api_key: str, model: str, voice: str = "Aoede") -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._voice = voice
        self._registry: ToolRegistry | None = None
        self._session = None
        self._session_cm = None
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._receive_task: asyncio.Task | None = None
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self._turn_count = 0
        self._interrupted = False
        # Updated each time the server sends an audio chunk or fires
        # turn_complete. The idle watchdog uses this to avoid timing out
        # mid-TTS — only after the model goes silent for `timeout`
        # seconds does the session end. Initialised lazily in connect().
        self._last_activity_at: float = 0.0
        # Loop-time of the most recent audio chunk specifically (not
        # tool calls or turn_complete). Voice daemon's barge-in gate
        # uses this: if a chunk arrived in the last ~500ms the model
        # is currently producing TTS and mic frames need VAD gating.
        self._last_chunk_at: float = 0.0
        # Counters used to detect "Gemini accepted our connection but
        # returned nothing" failure mode (quota exhaustion, service
        # degradation, etc — the API doesn't surface a clean error).
        self._bytes_sent: int = 0
        self._chunks_received: int = 0
        # Set when the model signals user-interrupted-our-speech, so the
        # playback task can race writing-current-chunk against
        # something-just-changed and flush its output buffer ASAP.
        self._interrupt_event = asyncio.Event()
        # First-chunk timing — connect() resets these to the current
        # loop time on every open. Initialised here so direct _dispatch
        # calls in tests don't AttributeError on the un-connected path.
        self._connect_ts: float = 0.0
        self._first_chunk_logged: bool = False

    async def connect(self, registry: ToolRegistry, system_instruction: str) -> None:
        self._registry = registry
        decls = registry.function_declarations()
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system_instruction or None,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            # Pin the prebuilt voice so it's consistent across sessions
            # (without this the server picks a different voice each time).
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice,
                    ),
                ),
            ),
            # NO_INTERRUPTION: server doesn't let user activity
            # interrupt the model mid-turn. See note in
            # GeminiLiveConnection._build_config for details.
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            ),
        )
        # 409 Conflict on connect = concurrent-session-limit exceeded
        # on Google's side (Tier 0=3, Tier 1=50, Tier 2=1000 per
        # project — see https://discuss.ai.google.dev/t/is-the-gemini-live-api-rate-limit-per-key-or-per-user/78114).
        # Server-side session teardown lags client-side close, so rapid
        # open/close cycles (e.g. wake false-fires on music) can race
        # past the ceiling transiently. Retry with exponential backoff
        # before giving up — usually the previous session's slot frees
        # within a couple of seconds.
        last_exc: Exception | None = None
        for attempt, delay in enumerate([0.0, 1.0, 2.0, 4.0]):
            if delay > 0:
                logger.warning(
                    "gemini connect retry %d after %.1fs (last: %s)",
                    attempt, delay, last_exc,
                )
                await asyncio.sleep(delay)
            try:
                self._session_cm = self._client.aio.live.connect(
                    model=self._model, config=config
                )
                self._session = await self._session_cm.__aenter__()
                break
            except Exception as e:  # noqa: BLE001
                # Surface the underlying status if the SDK exposes it
                # (httpx errors carry .response.status_code; WebSocket
                # ConnectionClosedError carries .rcvd.code).
                status = getattr(getattr(e, "response", None), "status_code", None)
                ws_code = getattr(getattr(e, "rcvd", None), "code", None)
                last_exc = e
                # Only retry on 409 (concurrent-session-overlap) — other
                # errors (auth, malformed config, etc) won't fix
                # themselves with a wait.
                is_409 = status == 409 or "409" in str(e) or "Conflict" in str(e)
                if not is_409:
                    raise
                logger.warning(
                    "gemini connect 409 Conflict (status=%s ws=%s); will retry",
                    status, ws_code,
                )
        else:
            raise RuntimeError(
                f"gemini connect failed after retries; last error: {last_exc}"
            )

        self._turn_count = 0
        self._last_activity_at = asyncio.get_event_loop().time()
        self._connect_ts = self._last_activity_at  # for first-chunk timing
        self._first_chunk_logged = False
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._session is None:
            return
        self._bytes_sent += len(pcm_16khz_int16)
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm_16khz_int16, mime_type=self.INPUT_MIME)
        )

    async def end_input(self) -> None:
        if self._session is None:
            return
        await self._session.send_realtime_input(audio_stream_end=True)

    async def audio_out(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("session.close() error (ignored): %s", e)
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.debug("session __aexit__ error (ignored): %s", e)
            self._session_cm = None
            self._session = None
        await self._audio_q.put(None)

    def usage_tokens(self) -> dict[str, int]:
        return dict(self._usage)

    def turn_count(self) -> int:
        return self._turn_count

    def last_activity_at(self) -> float:
        return self._last_activity_at

    def last_chunk_at(self) -> float:
        return self._last_chunk_at

    def bytes_sent(self) -> int:
        """Total bytes of audio PCM sent to the server during this session."""
        return self._bytes_sent

    def chunks_received(self) -> int:
        """Total audio response chunks received from the server."""
        return self._chunks_received

    def interrupted(self) -> bool:
        return self._interrupted

    async def wait_for_interrupt(self) -> None:
        """Block until the model reports the user interrupted its speech.
        Returns immediately if an interrupt has fired since the last
        clear_interrupted() call."""
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    async def _receive_loop(self) -> None:
        assert self._session is not None
        try:
            async for response in self._session.receive():
                await self._dispatch(response)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Try to surface WebSocket close codes/reasons specifically —
            # they're the closest thing Gemini Live gives us to an
            # explicit error signal (1011 = server internal error,
            # 1008 = policy violation, 1013 = try again later, etc).
            close_code = getattr(getattr(e, "rcvd", None), "code", None)
            close_reason = getattr(getattr(e, "rcvd", None), "reason", None)
            if close_code is not None:
                logger.warning(
                    "gemini WS closed: code=%s reason=%r (type=%s)",
                    close_code, close_reason, type(e).__name__,
                )
            else:
                logger.warning(
                    "gemini receive loop error (%s): %s",
                    type(e).__name__, e,
                )
        finally:
            await self._audio_q.put(None)

    async def _dispatch(self, response) -> None:
        # Audio frames live on response.data (raw 24 kHz int16 PCM).
        data = getattr(response, "data", None)
        if data:
            now = asyncio.get_event_loop().time()
            self._last_activity_at = now
            self._last_chunk_at = now
            self._chunks_received += 1
            if not self._first_chunk_logged:
                self._first_chunk_logged = True
                first_ms = (now - self._connect_ts) * 1000
                logger.info(
                    "first audio chunk from Gemini in %.0fms (session open→1st chunk)",
                    first_ms,
                )
            await self._audio_q.put(data)

        # Tool calls.
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            self._last_activity_at = asyncio.get_event_loop().time()
            await self._handle_tool_call(tool_call)

        # Server content: turn_complete + interrupted.
        sc = getattr(response, "server_content", None)
        if sc is not None:
            if getattr(sc, "turn_complete", False):
                self._turn_count += 1
                self._last_activity_at = asyncio.get_event_loop().time()
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
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            in_tok = getattr(usage, "prompt_token_count", None)
            out_tok = getattr(usage, "response_token_count", None)
            if in_tok is not None:
                self._usage["input_tokens"] = int(in_tok)
            if out_tok is not None:
                self._usage["output_tokens"] = int(out_tok)

    async def _handle_tool_call(self, tool_call) -> None:
        """Dispatch tool calls from the model with structured timing logs.

        Log format per call:
          tool {name} start args={...}                      [t=0.000s]
          tool {name} fn done in 412ms ok payload={...}     [HTTP + parsing]
          tool {name} response sent to Gemini in 614ms      [total round-trip]
        Failure paths log `timed out` or `raised:` with the same elapsed.
        """
        assert self._registry is not None
        responses = []
        t0 = _time.monotonic()
        for fc in tool_call.function_calls:
            tool = self._registry.get(fc.name)
            args = dict(fc.args or {})
            if tool is None:
                payload: dict = {"error": f"unknown tool {fc.name}"}
                logger.warning("tool %s start args=%s → unknown tool", fc.name, args)
            else:
                logger.info("tool %s start args=%s", fc.name, args)
                t_fn = _time.monotonic()
                try:
                    out = tool.fn(**args)
                    if asyncio.iscoroutine(out):
                        # 12s gives async tool calls (httpx HTTP +
                        # parsing) headroom on a busy Pi event loop
                        # where ONNX wake-word + audio resampling +
                        # Gemini WebSocket compete for CPU. Anything
                        # slower than that probably means the upstream
                        # API is genuinely failing — we'd rather report
                        # the timeout than hang the session further.
                        out = await asyncio.wait_for(out, timeout=12.0)
                    # Pass dict outputs straight through; only wrap scalars
                    # so the model doesn't see {"result": {"ok": true}}.
                    payload = out if isinstance(out, dict) else {"value": out}
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    # Truncate the payload preview — weather/subway
                    # responses can be 4-8 KB and flood the journal.
                    preview = repr(payload)
                    if len(preview) > 240:
                        preview = preview[:237] + "..."
                    logger.info(
                        "tool %s fn done in %.0fms ok payload=%s",
                        fc.name, fn_ms, preview,
                    )
                except asyncio.TimeoutError:
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    payload = {"error": f"{fc.name} timed out"}
                    logger.warning(
                        "tool %s fn TIMED OUT after %.0fms", fc.name, fn_ms,
                    )
                except Exception as e:  # noqa: BLE001
                    fn_ms = (_time.monotonic() - t_fn) * 1000
                    payload = {"error": str(e)}
                    logger.warning(
                        "tool %s fn RAISED after %.0fms: %s",
                        fc.name, fn_ms, e,
                    )
            responses.append(
                types.FunctionResponse(
                    id=fc.id, name=fc.name, response=payload
                )
            )
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
