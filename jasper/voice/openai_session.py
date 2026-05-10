"""OpenAI Realtime API adapter for jasper-voice.

Mirrors the Gemini Live adapter (gemini_session.py): same
``LiveConnection`` + ``LiveTurn`` protocols, same supervisor / backoff /
escalation-cue helpers, same manual-VAD pattern where the daemon owns
turn boundaries via wake-then-silence-detect. Differences from Gemini
are wire-format only — events are JSON-shaped dicts (or typed Pydantic
in the SDK) with names like ``input_audio_buffer.append`` and
``response.output_audio.delta`` instead of Google's ``send_realtime_input``
/ ``server_content`` envelopes.

Audio
  Input: PCM16 mono, **24 kHz** (OpenAI Realtime's ``audio/pcm`` is
    24 kHz-only — confirmed against the SDK's ``Literal[24000]`` rate
    enum). We polyphase-upsample the XVF3800's native 16 kHz mic
    capture to 24 kHz inside the turn's ``send_audio`` path so the
    rest of the daemon stays 16 kHz everywhere.
  Output: PCM16 mono, 24 kHz — matches Gemini, so the existing
    ``TtsPlayout`` 24→48 kHz upsampler handles playback unchanged.

Manual VAD
  ``session.update`` sets ``turn_detection: None`` (literally JSON
  ``null``, Python ``None``). The server does not auto-create
  responses; the client commits each turn explicitly. ``end_input()``
  sends ``input_audio_buffer.commit()`` followed by
  ``response.create()`` to flush audio and trigger inference. This is
  the same overall shape as Gemini's ``activity_start`` /
  ``activity_end`` markers — daemon code at the wake-loop level is
  unchanged.

Tool calls
  Registry produces flat OpenAI tool schemas via
  ``registry.openai_tools()``. The model emits
  ``response.function_call_arguments.done`` with the arguments as a
  single JSON string; we ``json.loads`` it, dispatch the registered
  callable with the same 12 s timeout the Gemini adapter uses, and
  reply with ``conversation.item.create`` of type
  ``function_call_output`` plus a fresh ``response.create()``.

Session lifecycle
  60-minute hard cap, no resumption mechanism (unlike Gemini). When the
  cap or any drop is hit, the supervisor reconnects the same way as for
  any other drop. Lost conversational context is acceptable — the
  daemon already biases toward fresh sessions via the 5-minute idle
  context-reset, which on OpenAI is just "tear down and reopen" since
  there's no handle to drop.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import contextlib
import json
import logging
import time as _time
from collections import deque
from enum import Enum
from typing import Awaitable, AsyncIterator, Callable

from ..tools import ToolRegistry
from ._supervisor import (
    ESCALATION_CUE_SLUG,
    ESCALATION_RATE_LIMIT_SEC,
    ESCALATION_REPEAT_THRESHOLD,
    FailureFingerprint,
    reconnect_backoff_delay,
)
from .session import LiveConnection, LiveTurn

logger = logging.getLogger(__name__)


# Wire-format constants. The OpenAI Realtime ``audio/pcm`` discriminator
# accepts only 24 kHz (verified against ``RealtimeAudioFormats.AudioPCM``
# in openai-python's typed API). The XVF3800 captures at 16 kHz mono;
# we polyphase-upsample 16 → 24 inside the turn before base64-encoding.
OPENAI_AUDIO_RATE_HZ = 24000
DAEMON_MIC_RATE_HZ = 16000

# Connect retry schedule used for the initial daemon-startup connect.
# The supervisor's own reconnect path uses the shared exponential-with-
# jitter schedule from ``_supervisor.reconnect_backoff_delay``.
INITIAL_CONNECT_BACKOFF_SCHEDULE = (0.0, 1.0, 2.0, 4.0, 8.0)

# Default reasoning effort for ``gpt-realtime-2``. Smart-speaker queries
# are short and concrete; we don't need ``medium`` / ``high`` reasoning
# (which trade ~1+ extra second of TTFA for marginally smarter answers
# the user won't notice). ``low`` is the SDK default; ``minimal`` is
# ~1.1 s TTFA at the cost of less coherent multi-step answers. Override
# via ``JASPER_OPENAI_REASONING_EFFORT`` if needed.
DEFAULT_REASONING_EFFORT = "low"

# Default sampling temperature. OpenAI Realtime accepts 0.6 – 1.2 with
# 0.8 default; we pin to 0.7 — a touch more deterministic than the SDK
# default, matching the Gemini adapter's 0.3 spirit (tight responses,
# low creative drift).
DEFAULT_TEMPERATURE = 0.7


class ConnectionState(Enum):
    """States for the persistent OpenAI Realtime connection state machine.

    Same shape as the Gemini connection's state machine for consistency,
    minus the resumption-handle-related transitions that don't apply to
    OpenAI."""
    IDLE_INIT = "idle_init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    IN_TURN = "in_turn"
    RECONNECTING = "reconnecting"
    PAUSED_FOR_BACKOFF = "paused_for_backoff"
    FAILED = "failed"
    CLOSED = "closed"


# ---------- Audio helpers ---------------------------------------------------


def _upsample_16k_to_24k(
    pcm_16k: bytes, state: tuple | None,
) -> tuple[bytes, tuple]:
    """Polyphase upsample 16 kHz mono int16 → 24 kHz mono int16.

    Uses ``audioop.ratecv``. State must persist across calls within a
    turn so the resampler doesn't introduce phase discontinuities at
    frame boundaries — pass the returned state back in on the next
    call. Reset state to ``None`` at turn start.

    ``audioop`` was REMOVED from Python 3.13's stdlib (PEP 594), and
    PiOS Trixie ships 3.13. The ``audioop-lts`` backport on PyPI is a
    drop-in replacement that registers under the ``audioop`` import
    name — pyproject.toml depends on it conditionally for 3.13+, so
    this import resolves transparently on every supported Python
    version. If/when ``audioop-lts`` stops being maintained, swap to
    ``scipy.signal.resample_poly`` or a hand-rolled 3:2 polyphase
    filter."""
    return audioop.ratecv(
        pcm_16k, 2, 1, DAEMON_MIC_RATE_HZ, OPENAI_AUDIO_RATE_HZ, state,
    )


# ---------- Per-turn adapter ------------------------------------------------


class OpenAIRealtimeTurn(LiveTurn):
    """A single turn against an open ``OpenAIRealtimeConnection``.

    Owns the per-turn audio queue, the resampler state, and per-turn
    counters. The connection's receive loop routes incoming server
    events here while a turn is active.
    """

    def __init__(self, conn: "OpenAIRealtimeConnection", started_at: float) -> None:
        self._conn = conn
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        # Modality-aware breakdown accumulator. OpenAI Realtime emits
        # `response.usage.input_token_details.{audio,text,cached}_tokens`
        # and `output_token_details.{audio,text}_tokens` per
        # response.done; we sum across responses within a turn so the
        # spend cap sees the full breakdown when it computes cost.
        # Pricing.estimate_cost reads this dict and prices each bucket
        # at the right rate ($32 audio in, $4 text in, $0.40 cached,
        # $64 audio out, $24 text out for gpt-realtime-2).
        self._usage_breakdown: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "input_token_details": {
                "audio_tokens": 0,
                "text_tokens": 0,
                "cached_tokens": 0,
            },
            "output_token_details": {
                "audio_tokens": 0,
                "text_tokens": 0,
            },
        }
        self._interrupted = False
        self._interrupt_event = asyncio.Event()
        self._last_activity_at: float = started_at
        self._last_chunk_at: float = 0.0
        # Updated by `audio_out()` each time the consumer dequeues a
        # chunk — the right anchor for the idle watchdog's tail wait.
        # See `last_chunk_played_at()` docstring on LiveTurn for why.
        self._last_chunk_dequeued_at: float = 0.0
        self._first_chunk_logged = False
        self._started_at = started_at
        self._started_at_monotonic: float = _time.monotonic()
        self._bytes_sent: int = 0
        self._chunks_received: int = 0
        # Tracks whether `commit()` + `response.create()` has been sent.
        # Idempotent like Gemini's _activity_end_sent.
        self._committed = False
        self._released = False
        self._turn_lost = False
        self._server_turn_complete = False
        # Polyphase resampler state, persists across send_audio calls.
        # Reset to None at turn start so the first frame doesn't carry
        # tail samples from the previous turn.
        self._resample_state: tuple | None = None
        # The most recent assistant audio item id seen, kept for
        # potential `conversation.item.truncate(item_id=..., audio_end_ms=...)`
        # calls when implementing real barge-in. Today the daemon uses
        # NO_INTERRUPTION-equivalent semantics (model talks to completion),
        # so this stays unused; recorded so the field is ready when
        # someone wires up barge-in for real.
        self._last_assistant_item_id: str | None = None

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._released or self._turn_lost or self._committed:
            # After commit, the buffer is closed for input — further
            # frames belong to a turn that doesn't exist yet.
            return
        try:
            await self._conn._send_audio_chunk(self, pcm_16khz_int16)
            self._bytes_sent += len(pcm_16khz_int16)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "openai turn: send_audio failed (%s: %s); turn lost",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

    async def end_input(self) -> None:
        """Commit the user audio buffer and trigger a response.

        Equivalent of Gemini's ``activity_end``: server stops listening
        for more user audio and starts generating. Idempotent."""
        if self._committed or self._released or self._turn_lost:
            return
        self._committed = True
        try:
            await self._conn._commit_and_create_response(self)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "openai turn: end_input ignored (%s: %s)",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

    async def audio_out(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                return
            # Stamp the dequeue time so the idle watchdog can see the
            # consumer making real-time progress through the queue,
            # not just network arrivals. Without this, OpenAI's "all
            # chunks arrive in 1.4 s, played over 7 s" pattern would
            # let the watchdog end the turn while ~5 s of audio is
            # still queued.
            self._last_chunk_dequeued_at = asyncio.get_event_loop().time()
            yield chunk

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        elapsed_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
        await self._audio_q.put(None)
        # If the daemon released the turn before sending end_input
        # (no-speech abort, hard cap, etc.), best-effort cancel the
        # in-flight response so the server doesn't keep generating.
        if not self._committed and not self._turn_lost:
            try:
                await self._conn._cancel_response()
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "openai turn: release cancel ignored (%s: %s)",
                    type(e).__name__, e,
                )
        await self._conn._on_turn_released(self)
        logger.info(
            "openai turn: ended in %.0fms, %d chunks received (sent=%dB)",
            elapsed_ms, self._chunks_received, self._bytes_sent,
        )

    def last_activity_at(self) -> float:
        return self._last_activity_at

    def last_chunk_at(self) -> float:
        return self._last_chunk_at

    def last_chunk_played_at(self) -> float:
        return self._last_chunk_dequeued_at

    def server_turn_complete(self) -> bool:
        return self._server_turn_complete

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def usage_tokens(self) -> dict[str, int]:
        return dict(self._usage)

    def usage_breakdown(self) -> dict | None:
        # Deep-copy of the accumulator so callers can't mutate the
        # turn's internal state through the returned reference.
        return {
            "input_tokens": self._usage_breakdown["input_tokens"],
            "output_tokens": self._usage_breakdown["output_tokens"],
            "input_token_details": dict(self._usage_breakdown["input_token_details"]),
            "output_token_details": dict(self._usage_breakdown["output_token_details"]),
        }

    def turn_lost(self) -> bool:
        return self._turn_lost

    def interrupted(self) -> bool:
        return self._interrupted

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    # ---- Internal — called by the connection's receive loop ----

    async def _on_audio_delta(self, b64_audio: str) -> None:
        try:
            data = base64.b64decode(b64_audio)
        except Exception as e:  # noqa: BLE001
            logger.warning("openai turn: bad base64 audio delta (%s)", e)
            return
        if not data:
            return
        now = asyncio.get_event_loop().time()
        self._last_activity_at = now
        self._last_chunk_at = now
        self._chunks_received += 1
        if not self._first_chunk_logged:
            self._first_chunk_logged = True
            first_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
            logger.info(
                "first audio chunk from OpenAI in %.0fms (turn start→1st chunk)",
                first_ms,
            )
        await self._audio_q.put(data)

    def _record_usage(self, usage: dict | None) -> None:
        """Accumulate tokens from one response.done. A tool-using turn
        spans multiple OpenAI responses, each carrying its own usage —
        sum them so the spend cap reflects the full round-trip cost,
        rather than only the final audio response (which would
        under-count). Called by both the deferred-completion path
        (intermediate tool-call response.done) and the final
        ``_on_response_done``.

        Also accumulates the modality breakdown
        (input.audio/text/cached, output.audio/text) so
        ``usage_breakdown()`` returns the full split for cost
        estimation."""
        if not usage:
            return
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        if isinstance(in_tok, int):
            self._usage["input_tokens"] += in_tok
            self._usage_breakdown["input_tokens"] += in_tok
        if isinstance(out_tok, int):
            self._usage["output_tokens"] += out_tok
            self._usage_breakdown["output_tokens"] += out_tok
        # Modality breakdown — the SDK gives both fields per response;
        # sum them across the turn's responses.
        in_d = usage.get("input_token_details") or {}
        for k in ("audio_tokens", "text_tokens", "cached_tokens"):
            v = in_d.get(k)
            if isinstance(v, int):
                self._usage_breakdown["input_token_details"][k] += v
        out_d = usage.get("output_token_details") or {}
        for k in ("audio_tokens", "text_tokens"):
            v = out_d.get(k)
            if isinstance(v, int):
                self._usage_breakdown["output_token_details"][k] += v

    async def _on_response_done(self, usage: dict | None) -> None:
        self._last_activity_at = asyncio.get_event_loop().time()
        self._server_turn_complete = True
        self._record_usage(usage)

    def _on_assistant_item_id(self, item_id: str | None) -> None:
        if item_id:
            self._last_assistant_item_id = item_id

    def _on_connection_lost(self) -> None:
        if self._released or self._turn_lost:
            return
        self._turn_lost = True
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_q.put_nowait(None)


# ---------- Long-lived connection ------------------------------------------


class OpenAIRealtimeConnection(LiveConnection):
    """Long-lived OpenAI Realtime connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and a state machine that survives reconnects. Mirrors the
    structure of ``GeminiLiveConnection`` so the daemon's wake/turn loop
    is provider-agnostic.
    """

    PROVIDER_NAME = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-realtime-2",
        voice: str = "marin",
        context_reset_sec: float = 0.0,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        temperature: float = DEFAULT_TEMPERATURE,
        # Production: leave None → supervisor reconnects FOREVER with
        # the shared exponential-with-jitter schedule. Tests pass a
        # bounded tuple to make exhaustion observable.
        backoff_schedule: tuple[float, ...] | None = None,
        # Test seam: replace the SDK's connect call. The factory must be
        # callable as ``factory(model: str)`` and return an async context
        # manager whose ``__aenter__`` yields a connection-like object
        # exposing ``.send(event_dict) / .__aiter__() / .close()``.
        connect_factory=None,
        # Subclass override: ``GrokRealtimeConnection`` flips the base URL
        # without touching the rest of the wiring.
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._context_reset_sec = context_reset_sec
        self._reasoning_effort = reasoning_effort
        self._temperature = temperature
        self._backoff_schedule = backoff_schedule
        self._connect_factory = connect_factory
        self._base_url = base_url
        # Lazy SDK client — only built when ``connect_factory`` is None.
        # We do this lazily so test setups can construct the connection
        # object without the openai package installed.
        self._client = None

        self._registry: ToolRegistry | None = None
        self._system_instruction_provider: Callable[[], str] | None = None

        self._state = ConnectionState.IDLE_INIT
        self._state_lock = asyncio.Lock()
        # CONNECTED ↔ IN_TURN cycles every wake; logging each transition
        # at INFO floods the journal. Filter mirrors gemini_session.
        self._noisy_transitions = frozenset({
            (ConnectionState.CONNECTED, ConnectionState.IN_TURN),
            (ConnectionState.IN_TURN, ConnectionState.CONNECTED),
        })

        # SDK connection + context manager (cleared during reconnect).
        self._conn = None
        self._conn_cm = None
        self._send_lock = asyncio.Lock()

        self._last_turn_end_at: float = 0.0
        self._active_turn: OpenAIRealtimeTurn | None = None
        self._turn_lock = asyncio.Lock()

        # Count of `response.output_audio.delta` events that arrived
        # while `_active_turn is None` (server response that landed
        # AFTER the daemon's idle watchdog already released the turn).
        # Logging each delta would be 50-200 lines per orphan response;
        # we accumulate here and surface the total in the matching
        # `response.done` warning, then reset.
        self._orphan_delta_count: int = 0

        self._receive_task: asyncio.Task | None = None
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._connected_event: asyncio.Event = asyncio.Event()

        # Tight-retry-loop detection — same logic as Gemini, lifted into
        # ``_supervisor.FailureFingerprint``.
        self._recent_failure_fingerprints: deque[FailureFingerprint] = deque(
            maxlen=ESCALATION_REPEAT_THRESHOLD,
        )
        self._last_escalation_at: float = float("-inf")
        self._failure_escalation_cb: Callable[[str], Awaitable[object]] | None = None

    # ------------------------------------------------------------------
    # Public LiveConnection protocol
    # ------------------------------------------------------------------

    def _set_state(self, new_state: ConnectionState) -> None:
        old = self._state
        if old is new_state:
            return
        self._state = new_state
        if (old, new_state) not in self._noisy_transitions:
            logger.info(
                "openai connection state: %s → %s",
                old.value, new_state.value,
            )

    def set_failure_escalation_cb(
        self, cb: Callable[[str], Awaitable[object]] | None,
    ) -> None:
        """Wire the supervisor's tight-retry-loop escalation cue.

        Voice daemon calls this after both the connection and the
        ``WakeLoop`` are constructed (the loop owns the cue manager and
        knows how to suppress the cue mid-session)."""
        self._failure_escalation_cb = cb

    def _maybe_fire_escalation_cue(self) -> None:
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
            "openai connection: %d consecutive identical reconnect failures "
            "(%s, code=%s, %r) — firing %s cue",
            ESCALATION_REPEAT_THRESHOLD,
            first.exc_type, first.close_code, first.reason[:60],
            ESCALATION_CUE_SLUG,
        )
        asyncio.create_task(
            self._failure_escalation_cb(ESCALATION_CUE_SLUG),
            name="jasper-supervisor-escalation-cue",
        )

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
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    async def stop(self) -> None:
        if self._state is ConnectionState.CLOSED:
            return
        self._stopping.set()
        for task in (self._supervisor_task, self._receive_task):
            if task is not None:
                task.cancel()
        for task in (self._supervisor_task, self._receive_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._supervisor_task = None
        self._receive_task = None
        await self._teardown_session()
        if self._active_turn is not None:
            self._active_turn._on_connection_lost()
            self._active_turn = None
        async with self._state_lock:
            self._set_state(ConnectionState.CLOSED)

    async def acquire_turn(self) -> LiveTurn:
        if self._state is ConnectionState.FAILED:
            raise RuntimeError("openai connection: in FAILED state; daemon paused")
        if self._state is ConnectionState.CLOSED:
            raise RuntimeError("openai connection: closed")

        if not self._connected_event.is_set():
            timeout = (
                sum(self._backoff_schedule) + 5.0
                if self._backoff_schedule is not None
                else 15.0
            )
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "openai connection: not connected after backoff window"
                )

        await self._maybe_reset_context()

        async with self._turn_lock:
            if self._active_turn is not None:
                raise RuntimeError("openai connection: a turn is already active")
            now_loop = asyncio.get_event_loop().time()
            turn = OpenAIRealtimeTurn(self, started_at=now_loop)
            turn._started_at_monotonic = _time.monotonic()
            self._active_turn = turn
            # Fresh turn — discard any orphan-delta count left over from
            # a previous response that landed after release. The counter
            # is also reset inside the orphan response.done handler, so
            # this is a belt-and-suspenders reset for edge cases where
            # the orphan response.done never arrives.
            self._orphan_delta_count = 0
            async with self._state_lock:
                if self._state is ConnectionState.CONNECTED:
                    self._set_state(ConnectionState.IN_TURN)
            logger.info("openai turn: started")
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

    async def _send_event(self, event: dict) -> None:
        """Send a single client event to the SDK connection.

        The SDK's typed wrappers (``conn.input_audio_buffer.append(...)``,
        ``conn.response.create(...)``, etc.) call into the same low-level
        ``send`` under the hood; we use ``send`` directly so the test
        seam doesn't have to mock the entire typed surface, only a
        single ``send(dict)`` method.

        Serialised through ``_send_lock`` so concurrent producers
        (audio-frame send vs. tool-result send) can't interleave at the
        WebSocket frame boundary."""
        if self._conn is None:
            raise RuntimeError("openai connection: no active session")
        async with self._send_lock:
            await self._conn.send(event)

    async def _send_audio_chunk(
        self, turn: OpenAIRealtimeTurn, pcm_16khz: bytes,
    ) -> None:
        # Polyphase 16 → 24 kHz upsample. State persists per-turn.
        pcm_24khz, turn._resample_state = _upsample_16k_to_24k(
            pcm_16khz, turn._resample_state,
        )
        if not pcm_24khz:
            return
        b64 = base64.b64encode(pcm_24khz).decode("ascii")
        await self._send_event({
            "type": "input_audio_buffer.append",
            "audio": b64,
        })

    async def _commit_and_create_response(self, turn: OpenAIRealtimeTurn) -> None:
        # Two events, in order: commit closes the user audio buffer (the
        # server then materialises it as a user message item); create
        # then asks the model to produce a response. Both required under
        # manual VAD — the server doesn't auto-commit or auto-respond.
        await self._send_event({"type": "input_audio_buffer.commit"})
        await self._send_event({"type": "response.create"})

    async def _cancel_response(self) -> None:
        # Best-effort: tell the server to stop generating. Idempotent on
        # the server side — extra cancels for a non-existent response
        # are silently ignored.
        if self._conn is None:
            return
        try:
            await self._send_event({"type": "response.cancel"})
        except Exception as e:  # noqa: BLE001
            logger.debug("openai connection: cancel ignored (%s)", e)

    async def _on_turn_released(self, turn: OpenAIRealtimeTurn) -> None:
        async with self._turn_lock:
            if self._active_turn is turn:
                self._active_turn = None
                self._last_turn_end_at = asyncio.get_event_loop().time()
        async with self._state_lock:
            if self._state is ConnectionState.IN_TURN:
                self._set_state(ConnectionState.CONNECTED)

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    def _build_session_payload(self) -> dict:
        """Build the ``session.update`` payload sent immediately after
        the WebSocket handshake completes.

        Manual VAD: ``turn_detection`` is JSON ``null`` (Python None).
        Tools come from the tool registry's OpenAI-shape serializer —
        provider-locked tools are filtered out at this stage so they
        never reach the model.

        Schema is verified against
        ``openai.types.realtime.realtime_session_create_request_param.
        RealtimeSessionCreateRequestParam`` in the SDK source. The
        notable surprises (vs. the generic 'Realtime' docs around the
        web): voice lives at ``audio.output.voice`` not at the session
        top level, and ``temperature`` was removed from this schema in
        Realtime 2 — the model has its own defaults."""
        instruction = (
            self._system_instruction_provider()
            if self._system_instruction_provider is not None
            else ""
        )
        tools = (
            self._registry.openai_tools(provider=self.PROVIDER_NAME)
            if self._registry is not None
            else []
        )
        session: dict = {
            "type": "realtime",
            "model": self._model,
            "output_modalities": ["audio"],
            "instructions": instruction or "",
            "audio": {
                "input": {
                    # 24 kHz is the only PCM rate OpenAI accepts on
                    # ``audio/pcm``; we upsample from 16 kHz inside the
                    # turn. ``turn_detection: None`` puts us in manual
                    # VAD mode — the daemon owns commit() and
                    # response.create().
                    "format": {
                        "type": "audio/pcm",
                        "rate": OPENAI_AUDIO_RATE_HZ,
                    },
                    "turn_detection": None,
                },
                "output": {
                    # Voice belongs HERE in Realtime 2 — at session
                    # top-level it errors with `Unknown parameter:
                    # 'session.voice'` and the entire session.update
                    # gets rejected (cascading into "no tools, no
                    # config, model auto-responds with defaults").
                    # The OpenAI Voice union: alloy / ash / ballad /
                    # coral / echo / sage / shimmer / verse / marin /
                    # cedar, plus a custom-VoiceID escape hatch.
                    "voice": self._voice,
                    "format": {
                        "type": "audio/pcm",
                        "rate": OPENAI_AUDIO_RATE_HZ,
                    },
                },
            },
            "tools": tools,
            "tool_choice": "auto",
        }
        # ``reasoning.effort`` is gated to reasoning-capable models
        # (``gpt-realtime-2``). We detect that from the model name
        # carrying "-2"; older models (gpt-realtime, gpt-realtime-1.5,
        # gpt-realtime-mini) don't accept the field.
        if self._reasoning_effort and "-2" in self._model:
            session["reasoning"] = {"effort": self._reasoning_effort}
        return session

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open_session_with_retry(
                INITIAL_CONNECT_BACKOFF_SCHEDULE,
                phase="initial-connect",
            )
        except Exception:
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            raise

    async def _open_session_with_retry(
        self, schedule: tuple[float, ...], *, phase: str,
    ) -> None:
        """Initial-connect retry loop. Auth/config errors propagate
        immediately; transient errors (network blip, 5xx, WS reset)
        retry through the schedule."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate(schedule):
            if delay > 0:
                logger.warning(
                    "openai connection: %s retry %d after %.1fs (last: %s: %s)",
                    phase, attempt, delay,
                    type(last_exc).__name__ if last_exc else "?",
                    last_exc,
                )
                await asyncio.sleep(delay)
            try:
                await self._open_session()
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if not _is_transient(e):
                    raise
                logger.warning(
                    "openai connection: %s transient failure on attempt %d/%d "
                    "(%s: %s)",
                    phase, attempt + 1, len(schedule),
                    type(e).__name__, e,
                )
        raise RuntimeError(
            f"openai connection: {phase} failed after {len(schedule)} retries; "
            f"last error: {last_exc}"
        )

    def _resolve_connect_call(self):
        """Return a callable ``(model: str) -> AsyncContextManager[conn]``
        that opens a Realtime WebSocket. Built lazily so test paths
        without the openai package installed don't fail at construction."""
        if self._connect_factory is not None:
            return self._connect_factory
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs = {"api_key": self._api_key}
            if self._base_url:
                # Used by GrokRealtimeConnection via its docs-stated
                # OpenAI-compatible endpoint.
                kwargs["websocket_base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return lambda model: self._client.realtime.connect(model=model)

    async def _open_session(self) -> None:
        connect_call = self._resolve_connect_call()
        t0 = _time.monotonic()
        cm = connect_call(model=self._model)
        try:
            conn = await cm.__aenter__()
        except Exception:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            raise
        self._conn_cm = cm
        self._conn = conn
        connect_ms = (_time.monotonic() - t0) * 1000
        logger.info(
            "openai connection: connect ok in %.0fms (model=%s)",
            connect_ms, self._model,
        )
        # Send session.update immediately so subsequent turns inherit
        # the right voice/tool/VAD config. Doing this AFTER assigning
        # ``self._conn`` so ``_send_event`` can reach the connection.
        try:
            await self._send_event({
                "type": "session.update",
                "session": self._build_session_payload(),
            })
        except Exception as e:
            # If session.update fails, the WS is already open but
            # unconfigured. Tear down so the supervisor can retry from
            # a clean slate.
            logger.warning(
                "openai connection: session.update failed (%s: %s); "
                "closing and re-raising for supervisor retry",
                type(e).__name__, e,
            )
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            self._conn = None
            self._conn_cm = None
            raise
        self._reconnect_event.clear()
        self._receive_task = asyncio.create_task(self._receive_loop(conn))
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()

    async def _teardown_session(self) -> None:
        t0 = _time.monotonic()
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await asyncio.wait_for(self._receive_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        if self._conn is not None:
            try:
                await asyncio.wait_for(self._conn.close(), timeout=3.0)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("openai connection: close error (ignored): %s", e)
        if self._conn_cm is not None:
            try:
                await asyncio.wait_for(
                    self._conn_cm.__aexit__(None, None, None), timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("openai connection: __aexit__ error (ignored): %s", e)
        self._conn_cm = None
        self._conn = None
        self._connected_event.clear()
        teardown_ms = (_time.monotonic() - t0) * 1000
        logger.info("openai connection: session torn down in %.0fms", teardown_ms)

    async def _supervisor_loop(self) -> None:
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
        await self._teardown_session()
        if self._active_turn is not None:
            self._active_turn._on_connection_lost()
            async with self._turn_lock:
                self._active_turn = None

        last_exc: Exception | None = None
        attempt = 0
        bounded = self._backoff_schedule is not None
        max_attempts = len(self._backoff_schedule) if bounded else None
        while not self._stopping.is_set():
            attempt += 1
            if bounded and attempt > max_attempts:
                break
            delay = (
                self._backoff_schedule[attempt - 1]
                if bounded
                else reconnect_backoff_delay(attempt)
            )
            async with self._state_lock:
                self._set_state(ConnectionState.PAUSED_FOR_BACKOFF)
            logger.info(
                "openai connection: reconnect attempt %d after %.1fs backoff",
                attempt, delay,
            )
            await asyncio.sleep(delay)
            if self._stopping.is_set():
                return
            try:
                await self._open_session()
                self._recent_failure_fingerprints.clear()
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                logger.warning(
                    "openai connection: reconnect attempt %d failed (%s: %s)",
                    attempt, type(e).__name__, e,
                )
                self._recent_failure_fingerprints.append(
                    FailureFingerprint.from_exception(e),
                )
                self._maybe_fire_escalation_cue()

        if bounded and not self._stopping.is_set():
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            logger.error(
                "openai connection: bounded test schedule exhausted after %d "
                "retries. Last error: %s", attempt - 1, last_exc,
            )

    async def _receive_loop(self, conn) -> None:
        """Iterate the SDK connection's event stream and route events.

        Accepts both Pydantic-typed events (have ``.type`` attribute and
        ``.model_dump()``) and dict events (test seam) — anything that
        looks dict-like via ``getattr`` access works."""
        try:
            async for event in conn:
                etype = _event_type(event)
                if etype is None:
                    continue
                await self._dispatch_event(etype, event)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            close_code = getattr(getattr(e, "rcvd", None), "code", None)
            close_reason = getattr(getattr(e, "rcvd", None), "reason", None)
            if close_code is not None:
                logger.warning(
                    "openai connection: disconnected (code=%s reason=%r), reconnecting",
                    close_code, close_reason,
                )
            else:
                logger.warning(
                    "openai connection: receive loop error (%s: %s), reconnecting",
                    type(e).__name__, e,
                )
            self._reconnect_event.set()

    async def _dispatch_event(self, etype: str, event) -> None:
        turn = self._active_turn

        if etype == "error":
            err = _event_field(event, "error") or {}
            logger.warning("openai connection: server error: %s", err)
            return

        if etype == "session.created" or etype == "session.updated":
            return

        # Audio chunk for the active turn.
        if etype == "response.output_audio.delta":
            delta = _event_field(event, "delta")
            if isinstance(delta, str):
                if turn is None:
                    # Server still streaming a response after the daemon
                    # released the turn. Tracked here, reported once in
                    # the trailing response.done — per-delta logging
                    # would flood the journal.
                    self._orphan_delta_count += 1
                else:
                    await turn._on_audio_delta(delta)
            return

        # Track the assistant audio item id for future barge-in support.
        if etype == "response.output_item.added":
            item = _event_field(event, "item") or {}
            if isinstance(item, dict) and item.get("type") == "message":
                if turn is not None:
                    turn._on_assistant_item_id(item.get("id"))
            return

        # Function-call argument streaming events. The official OpenAI
        # cookbook dispatches tools on `response.done`, NOT on
        # `function_call_arguments.done` — dispatching on the latter
        # would send `conversation.item.create` + `response.create`
        # while response 1 is still in-flight server-side, which
        # races against (or is rejected by) the server. Ignoring
        # these events lets the canonical handler in `response.done`
        # do the work.
        if etype in (
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        ):
            return

        # Server-side response complete.
        if etype == "response.done":
            await self._handle_response_done(event, turn)
            return

        # Server-side VAD events. We run manual VAD (turn_detection=None)
        # so these shouldn't normally fire — log and ignore if they do.
        if etype in (
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
        ):
            logger.debug(
                "openai connection: unexpected VAD event %s under manual VAD",
                etype,
            )
            return

        # Other informational events. Logged at DEBUG.
        logger.debug("openai connection: event %s", etype)

    async def _maybe_reset_context(self) -> None:
        """OpenAI Realtime has no resumption handle, so 'context reset'
        is just 'tear down and reopen'. Long idle gaps theoretically
        bleed conversational context across hours — in practice the
        terse-tool system prompt makes this a hypothetical concern, so
        the reset is opt-in (default 0 = disabled). When enabled, busts
        the prompt cache on the first turn after reset and blocks the
        wake event for 1-6 s during the reopen, so use a long threshold
        (hours, not minutes) if at all. Skipped if no prior turn has
        happened on this connection."""
        if self._context_reset_sec <= 0:
            return
        if self._last_turn_end_at <= 0.0:
            return
        idle_for = asyncio.get_event_loop().time() - self._last_turn_end_at
        if idle_for < self._context_reset_sec:
            return
        logger.info(
            "openai context reset: idle for %.0fs > threshold (%.0fs); "
            "reopening for a fresh session",
            idle_for, self._context_reset_sec,
        )
        await self._teardown_session()
        try:
            await self._open_session_with_retry(
                INITIAL_CONNECT_BACKOFF_SCHEDULE,
                phase="context-reset-reopen",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "openai connection: context-reset reopen failed (%s: %s); "
                "triggering supervisor reconnect",
                type(e).__name__, e,
            )
            self._reconnect_event.set()
            raise
        self._last_turn_end_at = asyncio.get_event_loop().time()

    async def _handle_response_done(self, event, turn: "OpenAIRealtimeTurn | None") -> None:
        """Dispatch a `response.done` event.

        OpenAI splits a tool-using turn across multiple responses:
            response 1: optional preamble audio + function_call output(s)
            (client dispatches each tool, sends function_call_output items,
             sends ONE response.create)
            response 2: the final audio answer

        This handler runs the canonical OpenAI-cookbook flow: examine
        ``response.output[]`` for ``function_call`` items, dispatch
        them, send their results, kick off response 2 with one
        ``response.create``, and DEFER turn-completion to response 2's
        own ``response.done``. If there are no function_calls in the
        output, this is the final response — flip server_turn_complete.
        """
        response = _event_field(event, "response")
        usage_dict = _normalise_usage(_event_field(response, "usage") if response is not None else None)
        function_calls = _extract_function_calls(response)

        # Diagnostic log: per-response breakdown. Reading the
        # audio/text split is the difference between "175 output
        # tokens means 8.75 s of audio that got truncated" (would
        # indicate a bug) vs "175 output tokens means 80 audio + 95
        # text transcript = 1.6 s of audio total" (model just gave a
        # short answer, no bug). Without this line we couldn't tell
        # the two apart from journalctl alone.
        if usage_dict:
            in_d = usage_dict.get("input_token_details") or {}
            out_d = usage_dict.get("output_token_details") or {}
            logger.info(
                "openai response.done: in=%d (audio=%d text=%d cached=%d) "
                "out=%d (audio=%d text=%d) function_calls=%d",
                int(usage_dict.get("input_tokens") or 0),
                int(in_d.get("audio_tokens") or 0),
                int(in_d.get("text_tokens") or 0),
                int(in_d.get("cached_tokens") or 0),
                int(usage_dict.get("output_tokens") or 0),
                int(out_d.get("audio_tokens") or 0),
                int(out_d.get("text_tokens") or 0),
                len(function_calls),
            )

        if turn is None:
            # Server-completed a response with no active turn to deliver
            # it to. Two common shapes:
            #   (a) idle watchdog raced the server: the wake loop ended
            #       the turn before the first audio chunk arrived,
            #       _end_turn fired a belated commit+response.create
            #       during cleanup, the server then generated and
            #       streamed audio deltas that hit a released turn and
            #       got silently dropped (see the orphan-delta counter
            #       in _dispatch_event).
            #   (b) connection reset / user-spoke-too-soon path: model
            #       was generating against the prior turn when the turn
            #       was torn down for unrelated reasons.
            # Either way, output audio tokens we paid for were not
            # heard. Surface a single warning per orphan response that
            # includes the dropped-delta count, so the next debugger
            # has one log line that says exactly what happened.
            if usage_dict:
                out_d_orphan = usage_dict.get("output_token_details") or {}
                logger.warning(
                    "openai response.done arrived AFTER turn release: "
                    "out=%d tokens (audio=%d) — %d audio deltas were "
                    "silently dropped. Daemon's idle watchdog likely "
                    "raced the server response; raise "
                    "JASPER_IDLE_TIMEOUT_SEC or look at why the silence "
                    "detector didn't trip earlier.",
                    int(usage_dict.get("output_tokens") or 0),
                    int(out_d_orphan.get("audio_tokens") or 0),
                    self._orphan_delta_count,
                )
            self._orphan_delta_count = 0
            # If the orphan response carried function_calls we still
            # MUST send synthetic function_call_outputs back — otherwise
            # the server-side conversation history retains dangling
            # function_call items with no matching outputs, and the
            # next turn sees its previous call as "still in progress"
            # and responds with confused fallbacks like "It's still
            # starting up" even though the user just asked something
            # brand new. We do NOT send response.create after these
            # synthetic outputs: we don't want the model to generate an
            # audio response that has no turn to play through.
            if function_calls and self._conn is not None:
                for fc in function_calls:
                    call_id = _event_field(fc, "call_id") or ""
                    name = _event_field(fc, "name") or "?"
                    if not call_id:
                        continue
                    try:
                        await self._send_event({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": json.dumps(
                                    {"error": "turn cancelled before dispatch"}
                                ),
                            },
                        })
                        logger.info(
                            "tool %s: turn-aborted, sent cancelled "
                            "function_call_output to keep server state clean",
                            name,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "tool %s: could not send cancelled output (%s: %s); "
                            "next turn may be confused",
                            name, type(e).__name__, e,
                        )
            return

        if function_calls:
            # Tool round. A single user-facing turn produces multiple
            # OpenAI responses when the model uses a tool:
            #   response 1: function_call(s) → response.done (this branch)
            #   <client sends function_call_output items + response.create>
            #   response 2: response.output_audio.delta × N → response.done
            # We MUST NOT flip server_turn_complete here — the audio
            # answer is still in flight. The no-function_calls branch
            # below is the only place that closes the turn.
            for fc in function_calls:
                await self._dispatch_function_call(fc)
            # Single response.create at the end of the round, regardless
            # of how many tools were called. Multiple response.create
            # calls would conflict (server rejects with "active response
            # in progress").
            try:
                await self._send_event({"type": "response.create"})
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "openai connection: response.create after tool round "
                    "failed (%s: %s); turn may stall",
                    type(e).__name__, e,
                )
            # Accumulate usage from this response — the model burned
            # input tokens reading the prompt + output tokens emitting
            # the function call. Don't flip server_turn_complete; the
            # audio answer is still in flight.
            turn._record_usage(usage_dict)
            return

        # No function_calls: this is the final response. Flip turn
        # completion so the daemon's idle watchdog can close after the
        # tail buffer drains.
        await turn._on_response_done(usage_dict)

    async def _dispatch_function_call(self, fc) -> None:
        """Run one function_call from a response.done's output[]:
        invoke the registered tool, send the result as a
        function_call_output. The caller in `_handle_response_done`
        sends a single ``response.create`` after all function_calls in
        the round have been dispatched (NOT once per call — that would
        produce overlapping response.creates which the server rejects).

        Log format mirrors the Gemini adapter's dispatch logging so
        journalctl is provider-uniform."""
        assert self._registry is not None
        name = _event_field(fc, "name") or ""
        call_id = _event_field(fc, "call_id") or ""
        arguments_json = _event_field(fc, "arguments") or "{}"

        try:
            args = json.loads(arguments_json) if arguments_json else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
            logger.warning(
                "openai tool %s: bad JSON arguments %r; treating as empty",
                name, arguments_json,
            )

        tool = self._registry.get(name)
        t0 = _time.monotonic()
        if tool is None:
            payload: dict = {"error": f"unknown tool {name}"}
            logger.warning("tool %s start args=%s → unknown tool", name, args)
        else:
            logger.info("tool %s start args=%s", name, args)
            t_fn = _time.monotonic()
            try:
                out = tool.fn(**args)
                if asyncio.iscoroutine(out):
                    out = await asyncio.wait_for(out, timeout=12.0)
                payload = out if isinstance(out, dict) else {"value": out}
                fn_ms = (_time.monotonic() - t_fn) * 1000
                preview = repr(payload)
                if len(preview) > 240:
                    preview = preview[:237] + "..."
                logger.info(
                    "tool %s fn done in %.0fms ok payload=%s",
                    name, fn_ms, preview,
                )
            except asyncio.TimeoutError:
                fn_ms = (_time.monotonic() - t_fn) * 1000
                payload = {"error": f"{name} timed out"}
                logger.warning("tool %s fn TIMED OUT after %.0fms", name, fn_ms)
            except Exception as e:  # noqa: BLE001
                fn_ms = (_time.monotonic() - t_fn) * 1000
                payload = {"error": str(e)}
                logger.warning(
                    "tool %s fn RAISED after %.0fms: %s", name, fn_ms, e,
                )

        if self._conn is not None and call_id:
            t_send = _time.monotonic()
            await self._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(payload),
                },
            })
            send_ms = (_time.monotonic() - t_send) * 1000
            total_ms = (_time.monotonic() - t0) * 1000
            logger.info(
                "tool result item sent to OpenAI in %.0fms (total dispatch %.0fms)",
                send_ms, total_ms,
            )


# ---------- Module-level event helpers --------------------------------------


def _event_type(event) -> str | None:
    """Return the event ``type`` field whether the event is a dict or a
    Pydantic model from the openai SDK."""
    t = getattr(event, "type", None)
    if t is None and isinstance(event, dict):
        t = event.get("type")
    return t


def _event_field(event, name: str):
    """Return ``event.<name>`` whether ``event`` is a Pydantic model or a
    dict. Pydantic models from the openai SDK expose top-level event
    fields as attributes; dict events store them as keys."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _normalise_usage(usage_obj) -> dict | None:
    """Convert a usage object (RealtimeResponseUsage Pydantic model
    in production, dict in tests) into a flat ``{input_tokens, ...}``
    dict so downstream code doesn't have to care about the shape."""
    if usage_obj is None:
        return None
    if isinstance(usage_obj, dict):
        return usage_obj
    if hasattr(usage_obj, "model_dump"):
        return usage_obj.model_dump()
    # Last-resort: scrape attributes by name. Keeps the token counter
    # working if a future SDK release changes its model representation.
    return {
        "input_tokens": getattr(usage_obj, "input_tokens", None),
        "output_tokens": getattr(usage_obj, "output_tokens", None),
    }


def _extract_function_calls(response) -> list:
    """Return the list of ``function_call`` items in a Realtime response's
    ``output[]``. Empty list if the response had no tool calls.

    Each returned item is whatever the SDK gave us (dict in tests,
    ``RealtimeConversationItemFunctionCall`` Pydantic model in
    production); ``_event_field`` handles both shapes when reading
    ``name`` / ``call_id`` / ``arguments`` later."""
    if response is None:
        return []
    output = _event_field(response, "output")
    if not output:
        return []
    return [
        item for item in output
        if _event_field(item, "type") == "function_call"
    ]


def _is_transient(exc: BaseException) -> bool:
    """Decide whether an exception from ``__aenter__`` / ``send`` /
    ``recv`` is worth retrying in the initial-connect path.

    Transient: network errors, server 5xx, WebSocket resets, rate-limit
    bursts, 409 (race against a recently-closed prior session). Non-
    transient: auth failures (401/403), config errors (400, malformed
    payloads), explicit ``ValueError`` from local validation. Non-
    transient errors propagate out of ``start()`` so the daemon doesn't
    keep retrying a fundamentally broken setup."""
    # Local-validation errors — never retry.
    if isinstance(exc, (TypeError, ValueError, ImportError, AttributeError)):
        return False
    # Anything HTTP-ish from the openai SDK or websockets:
    status = (
        getattr(exc, "status_code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    if status is not None:
        if status in (401, 403, 404):
            return False
        if 400 <= status < 500 and status != 429 and status != 409:
            return False
        return True
    # No status — treat as transient (network blip, WS reset, etc.).
    return True
