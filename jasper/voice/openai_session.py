# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""OpenAI Realtime API adapter for jasper-voice.

The wire half of the ``LiveConnection`` / ``LiveTurn`` contract; the
provider-independent half lives in ``_base.py``. Events are JSON-shaped
dicts (or typed Pydantic in the SDK) with names like
``input_audio_buffer.append`` and ``response.output_audio.delta``.

Audio
  Input: PCM16 mono, **24 kHz** (OpenAI Realtime's ``audio/pcm`` is
    24 kHz-only — confirmed against the SDK's ``Literal[24000]`` rate
    enum). We polyphase-upsample the XVF3800's native 16 kHz mic
    capture to 24 kHz inside the turn's ``send_audio`` path so the
    rest of the daemon stays 16 kHz everywhere.
  Output: PCM16 mono, 24 kHz, which the existing ``TtsPlayout``
    24→48 kHz upsampler handles unchanged.

Manual VAD
  ``session.update`` sets ``turn_detection: None`` (literally JSON
  ``null``, Python ``None``). The server does not auto-create
  responses; the client commits each turn explicitly. ``end_input()``
  sends ``input_audio_buffer.commit()`` followed by
  ``response.create()`` to flush audio and trigger inference.

Tool calls
  Completed responses carry function calls in response.output. Each
  result is returned with conversation.item.create, followed by one
  response.create for the round. All events and results retain their
  turn owner until release.

Session lifecycle
  60-minute hard cap, no resumption mechanism. When the cap or any drop
  is hit, the supervisor reconnects the same way as for any other drop.
  Lost conversational context is acceptable — the daemon already biases
  toward fresh sessions via the opt-in idle context-reset, which here is
  just a reopen since there is no handle to drop.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time as _time
from typing import Any

from jasper.log_event import log_event
from jasper.secret_redaction import redact_secrets

from ._base import (
    OPENAI_AUDIO_RATE_HZ,
    BaseLiveConnection,
    BaseLiveTurn,
    ToolCall,
    upsample_16k_to_24k,
)
from ._supervisor import (
    openai_error_is_terminal, request_planned_reopen, request_unplanned_reopen,
)
from .input_policy import NOISE_REDUCTION_FAR, NOISE_REDUCTION_NEAR
from .session import AudioOutChunk, TurnCapture
from .trace import emit as _trace_emit

logger = logging.getLogger(__name__)


# Bound a provider that opens the socket but never accepts session.update.
SESSION_SETUP_TIMEOUT_SEC = 15.0

# Default reasoning effort for ``gpt-realtime-2``. Smart-speaker queries
# are short and concrete; we don't need ``medium`` / ``high`` reasoning
# (which trade ~1+ extra second of TTFA for marginally smarter answers
# the user won't notice). ``low`` is the SDK default; ``minimal`` is
# ~1.1 s TTFA at the cost of less coherent multi-step answers. Override
# via ``JASPER_OPENAI_REASONING_EFFORT`` if needed.
DEFAULT_REASONING_EFFORT = "low"

# The host's intent -> the OpenAI wire value this adapter sends. None (no
# provider denoising) omits the block; voice.input_policy owns the spellings.
_NOISE_REDUCTION_WIRE_VALUES = {
    NOISE_REDUCTION_NEAR: "near_field",
    NOISE_REDUCTION_FAR: "far_field",
}

# Inbound event types that prove the turn is making progress, so they
# advance the pre-response idle anchor: the whole `response.*` namespace
# (created, in_progress, output items, audio and text deltas, done), the
# input transcription completing or streaming, and the commit
# acknowledgement. Excluded on purpose — `error`, `session.*`,
# `rate_limits.updated`, `conversation.item.created/.deleted/.truncated`
# (client-echoed acks of events we sent, e.g. the barge-in truncate) and
# `conversation.item.input_audio_transcription.failed` prove only that
# the socket is open. See #4532.
_PROGRESS_EVENT_PREFIXES = ("response.",)
_PROGRESS_EVENT_TYPES = frozenset((
    "input_audio_buffer.committed",
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.delta",
))


def _is_progress_event(etype: str) -> bool:
    return etype in _PROGRESS_EVENT_TYPES or etype.startswith(_PROGRESS_EVENT_PREFIXES)


def _noise_reduction_wire_value(intent: str | None) -> str:
    """Host intent -> OpenAI wire value; "" omits the session block."""
    if intent is None:
        return ""
    if intent not in _NOISE_REDUCTION_WIRE_VALUES:
        raise RuntimeError(
            "OpenAI noise_reduction must be None or one of: "
            + ", ".join(sorted(_NOISE_REDUCTION_WIRE_VALUES))
        )
    return _NOISE_REDUCTION_WIRE_VALUES[intent]


# ---------- Per-turn adapter ------------------------------------------------


class OpenAIRealtimeTurn(BaseLiveTurn):
    """A single turn against an open ``OpenAIRealtimeConnection``.

    Adds the resampler state and its barge-in wire state to
    ``BaseLiveTurn``. The connection's receive loop routes incoming
    server events here while a turn is active.
    """

    # Reported per response.done; usage.Pricing prices each bucket.
    usage_detail_buckets = {
        "input_token_details": ("audio_tokens", "text_tokens", "cached_tokens"),
        "output_token_details": ("audio_tokens", "text_tokens"),
    }

    def __init__(self, conn: "OpenAIRealtimeConnection", started_at: float) -> None:
        super().__init__(conn, started_at)
        self._conn: OpenAIRealtimeConnection = conn
        # Chunk-size distribution per turn; reported by `_release_fields`.
        self._chunk_bytes_total: int = 0
        self._chunk_bytes_max: int = 0
        self._first_chunk_bytes: int = 0
        # Whether `commit()` + `response.create()` has been sent; makes
        # `end_input` idempotent.
        self._committed = False
        self._response_id: str | None = None
        self._response_item_ids: set[str] = set()
        self._input_item_id: str | None = None
        # Polyphase resampler state, persists across send_audio calls.
        # Reset to None at turn start so the first frame doesn't carry
        # tail samples from the previous turn.
        self._resample_state: tuple | None = None
        self._received_ms_by_item: dict[str, float] = {}

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._released or self._turn_lost or self._committed:
            # After commit, the buffer is closed for input — further
            # frames belong to a turn that doesn't exist yet.
            return
        try:
            if await self._conn._send_audio_chunk(self, pcm_16khz_int16):
                self._bytes_sent += len(pcm_16khz_int16)
        except Exception as e:  # noqa: BLE001
            self._on_send_failed(e, operation="audio")

    async def send_text_context(self, text: str) -> None:
        if self._released or self._turn_lost or self._committed:
            return
        try:
            await self._conn._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }, turn=self)
        except Exception as e:  # noqa: BLE001
            self._on_send_failed(e, operation="text_context")

    async def end_input(self) -> None:
        """Commit the user audio buffer and trigger a response.

        The server stops listening for more user audio and starts
        generating. Idempotent."""
        if self._committed or self._released or self._turn_lost:
            return
        self._committed = True
        self._end_input_at_monotonic = _time.monotonic()
        try:
            await self._conn._commit_and_create_response(self)
        except Exception as e:  # noqa: BLE001
            self._on_send_failed(e, operation="end_input")

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._cancel_tools()
        self.drop_pending_audio()
        self._audio_q.put_nowait(None)
        self._log_release()
        await self._conn._on_turn_released(self)

    def _release_fields(self) -> dict[str, Any]:
        """Chunk-size distribution, so front-loaded vs uniform delivery is
        visible post hoc. 24 kHz mono pcm16 = 48 bytes/ms."""
        return {
            "audio_bytes": self._chunk_bytes_total,
            "first_chunk_bytes": self._first_chunk_bytes,
            "max_chunk_bytes": self._chunk_bytes_max,
        }

    def capture(self) -> TurnCapture | None:
        user = self.user_transcript().strip() or None
        assistant = self.assistant_transcript().strip() or None
        if user is None and assistant is None:
            return None
        return TurnCapture(user_text=user, assistant_text=assistant)

    # ---- Interruptible (OpenAI reference pack) ----
    # `response.cancel` then `conversation.item.truncate`, in that order,
    # from the flush's playout-ledger accounting. See ADR-0115 and
    # ``session.Interruptible``. Grok inherits this pack via
    # ``GrokRealtimeConnection``.

    async def cancel_response(self, reason: str) -> None:
        if self._released or self._turn_lost or not self._committed or self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_tools()
        log_event(
            logger, "barge.cancel", reason=reason,
            provider=self._conn.PROVIDER_NAME,
        )
        if not self._server_turn_complete:
            if self._tool_round_pending:
                await self._on_response_done()
            else:
                await self._conn._cancel_response(self)

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        """Trim only an explicitly identified item owned by this turn."""
        if self._released or self._turn_lost or provider_item_id is None:
            return
        received_ms = self._received_ms_by_item.get(provider_item_id)
        if received_ms is None or type(audio_played_ms) is not int or audio_played_ms < 0:
            return
        item_id = provider_item_id
        audio_end_ms = min(audio_played_ms, int(received_ms))
        log_event(
            logger, "barge.truncate", provider=self._conn.PROVIDER_NAME,
            item_id=item_id, audio_end_ms=audio_end_ms,
        )
        try:
            await self._conn._send_event({
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }, turn=self)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger, "barge.truncate_failed",
                item_id=item_id, error=type(e).__name__,
                detail=self._conn._redacted(e),
                level=logging.WARNING,
            )

    # ---- Internal — called by the connection's receive loop ----

    async def _on_audio_delta(self, b64_audio: str, item_id: str | None = None) -> None:
        try:
            data = base64.b64decode(b64_audio)
        except Exception as e:  # noqa: BLE001
            log_event(
                self._conn._logger, "provider.audio_decode_failed", provider=self._conn.PROVIDER_NAME,
                exc_type=type(e).__name__, detail=self._conn._redacted(e),
                level=logging.WARNING,
            )
            return
        if not data:
            return
        chunk_bytes = len(data)
        self._chunk_bytes_total += chunk_bytes
        if chunk_bytes > self._chunk_bytes_max:
            self._chunk_bytes_max = chunk_bytes
        if not self._first_chunk_bytes:
            self._first_chunk_bytes = chunk_bytes
        self._note_audio_chunk(_time.monotonic())
        if item_id:
            # 24 kHz mono pcm16 = 48 bytes/ms. Accumulate per item so a later
            # truncate can clamp to this item's received duration.
            self._received_ms_by_item[item_id] = (
                self._received_ms_by_item.get(item_id, 0.0) + chunk_bytes / 48.0
            )
        self._enqueue_audio(AudioOutChunk(
            pcm=data,
            provider_item_id=item_id,
        ))

    def _record_usage(self, usage: dict | None) -> None:
        """Add one ``response.done`` usage payload to the turn's counts.

        Called by ``_handle_response_done`` for every response.done —
        intermediate tool-call rounds and the final response alike.
        """
        if not usage:
            return
        self.add_usage(
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            input_token_details=usage.get("input_token_details"),
            output_token_details=usage.get("output_token_details"),
        )

    async def _on_response_done(self) -> None:
        self._cancel_tools()
        self._note_activity()
        self._server_turn_complete = True
        self._audio_q.put_nowait(None)

    def _tools_may_run(self) -> bool:
        return self._conn._owns_turn(self) and not self._cancel_requested and not self._server_turn_complete

    async def _send_tool_result(self, call: ToolCall, payload: dict) -> bool:
        if not call.id:
            return False
        output = self._tool_result_json(call.name, payload)
        try:
            sent = await self._conn._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call.id,
                    "output": output,
                },
            }, turn=self)
        except Exception as e:  # noqa: BLE001
            log_event(
                self._conn._logger, "provider.tool_result_send_failed", provider=self._conn.PROVIDER_NAME,
                tool=redact_secrets(call.name, literals=self._conn._secret_literals()),
                exc_type=type(e).__name__, detail=self._conn._redacted(e),
                level=logging.WARNING,
            )
            self._on_connection_lost()
            return False
        if sent:
            self._note_activity()
        return sent

    async def _finish_tool_round(self) -> bool:
        self._tool_round_pending = False
        try:
            await self._conn._send_event({"type": "response.create"}, turn=self)
        except Exception:  # noqa: BLE001
            self._on_connection_lost()
            return False
        return True

    def _on_assistant_text_delta(self, delta: str) -> None:
        if not delta:
            return
        self.add_transcript(assistant=delta)
        self._note_activity()

    def _on_assistant_text_done(self, text: str) -> None:
        # Realtime sends both deltas and a final text field. Trust the
        # deltas unless the final text extends them.
        current = self.assistant_transcript()
        if text and (not current or text.startswith(current)):
            self.set_transcript(assistant=text)

    def _on_user_text_done(self, text: str) -> None:
        self.set_transcript(user=_merge_transcript_completion(self.user_transcript(), text))


# ---------- Long-lived connection ------------------------------------------


class OpenAIRealtimeConnection(BaseLiveConnection):
    """Long-lived OpenAI Realtime connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and the wire half of the lifecycle ``BaseLiveConnection``
    drives.
    """

    PROVIDER_NAME = "openai"
    _logger = logger
    _turn_class = OpenAIRealtimeTurn
    # The watchdog below pre-empts a server cap rather than rotating on
    # our own schedule, so its reconnect backs off from attempt 1.
    _watchdog_is_planned = False

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-realtime-2",
        voice: str = "marin",
        context_reset_sec: float = 0.0,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        noise_reduction: str | None = None,
        # Proactive pre-cap reconnect — see `_watchdog_delay_sec`.
        # Both default to 0 (disabled) so tests and bare-construction don't
        # spawn surprise tasks. Production wires production values from
        # Config (3600 / 300 → fires at 55 min uptime). Cap and buffer
        # are independent so OpenAI raising the cap to e.g. 7200 s only
        # requires changing the cap value; buffer (intent: "5 min before
        # whatever the cap is") stays correct.
        session_max_sec: float = 0.0,
        proactive_buffer_sec: float = 0.0,
        # Test seam: replace the SDK's connect call. The factory must be
        # callable as ``factory(model: str)`` and return an async context
        # manager whose ``__aenter__`` yields a connection-like object
        # exposing ``.send(event_dict) / .__aiter__() / .close()``.
        connect_factory=None,
        # Test seam: monotonic clock source, read by the reconnect
        # nudge gate. Defaults to ``time.monotonic``.
        clock=None,
        # Test seam: sleep function. Defaults to ``asyncio.sleep``;
        # tests inject a no-op so backoff doesn't burn wall-time.
        sleep=None,
        # Subclass override: ``GrokRealtimeConnection`` flips the base URL
        # without touching the rest of the wiring.
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            model=model,
            voice=voice,
            context_reset_sec=context_reset_sec,
            sleep=sleep,
            nudge_clock=clock,
        )
        self._api_key = api_key
        self._reasoning_effort = reasoning_effort
        self._noise_reduction = _noise_reduction_wire_value(noise_reduction)
        self._session_max_sec = session_max_sec
        self._proactive_buffer_sec = proactive_buffer_sec
        self._connect_factory = connect_factory
        self._base_url = base_url
        # Lazy SDK client — only built when ``connect_factory`` is None.
        # We do this lazily so test setups can construct the connection
        # object without the openai package installed.
        self._client = None

        # SDK connection + context manager (cleared during reconnect).
        self._session = None
        self._session_cm = None
        self._send_lock = asyncio.Lock()

        # Manual VAD allows one outstanding commit and response.create.
        # Release reopens unresolved requests instead of rebinding their acks.
        self._pending_commit: OpenAIRealtimeTurn | None = None
        self._pending_response: OpenAIRealtimeTurn | None = None

    # ------------------------------------------------------------------
    # Internal — turn-side helpers
    # ------------------------------------------------------------------

    async def _send_event(self, event: dict, *, turn: OpenAIRealtimeTurn | None = None) -> bool:
        async with self._send_lock:
            if turn is not None:
                if not self._owns_turn(turn):
                    return False
                if event["type"] == "response.create":
                    if turn._cancel_requested:
                        return False
                    if self._pending_response is not None:
                        raise RuntimeError("response.create acknowledgement still pending")
                    self._pending_response = turn
                elif event["type"] == "input_audio_buffer.commit":
                    self._pending_commit = turn
                elif event.get("item", {}).get("type") == "function_call_output":
                    if turn._cancel_requested:
                        return False
            if self._session is None:
                raise RuntimeError(f"{self._log_tag} no active session")
            await self._session.send(event)
            return True

    async def _send_audio_chunk(
        self, turn: OpenAIRealtimeTurn, pcm_16khz: bytes,
    ) -> bool:
        # Polyphase 16 → 24 kHz upsample. State persists per-turn.
        pcm_24khz, turn._resample_state = upsample_16k_to_24k(
            pcm_16khz, turn._resample_state,
        )
        if not pcm_24khz:
            return False
        b64 = base64.b64encode(pcm_24khz).decode("ascii")
        return await self._send_event({
            "type": "input_audio_buffer.append",
            "audio": b64,
        }, turn=turn)

    async def _commit_and_create_response(self, turn: OpenAIRealtimeTurn) -> None:
        await self._send_event({"type": "input_audio_buffer.commit"}, turn=turn)
        await self._send_event({"type": "response.create"}, turn=turn)

    async def _cancel_response(self, turn: OpenAIRealtimeTurn) -> None:
        try:
            await self._send_event({"type": "response.cancel"}, turn=turn)
        except Exception as e:  # noqa: BLE001
            log_event(
                self._logger, "provider.cancel_ignored", provider=self.PROVIDER_NAME,
                exc_type=type(e).__name__, detail=self._redacted(e), level=logging.DEBUG,
            )

    async def _on_turn_released(self, turn: OpenAIRealtimeTurn) -> None:
        if self._active_turn is not turn:
            return
        async with self._send_lock:
            session = self._session
            if session is not None and turn._session is session:
                try:
                    if turn._response_id or self._pending_response is turn:
                        await session.send({"type": "response.cancel"})
                    await session.send({"type": "input_audio_buffer.clear"})
                except Exception as e:  # noqa: BLE001
                    if self._session is session:
                        self._connected_event.clear()
                        request_unplanned_reopen(self)
                    log_event(
                        self._logger, "provider.release_failed", provider=self.PROVIDER_NAME,
                        exc_type=type(e).__name__, detail=self._redacted(e), level=logging.WARNING,
                    )
                # An abandoned response can still add tool calls to history.
                # A fresh session removes them without publishing stale results.
                else:
                    unresolved = (
                        turn._committed and (not turn._server_turn_complete or turn._tool_round_pending)
                        or self._pending_commit is turn or self._pending_response is turn
                    )
                    if self._session is session and unresolved:
                        request_planned_reopen(self)
        await super()._on_turn_released(turn)

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
        input_audio: dict = {
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
            # Input transcription for diagnostics — emits one
            # ``conversation.item.input_audio_transcription.
            # completed`` event per user utterance so we can
            # see what STT actually heard, separate from the
            # model's tool choice, when debugging misrouted
            # commands. The model's decisions still come from
            # the raw audio, not this transcript — STT here is
            # observability, not the input path.
            #
            # gpt-4o-mini-transcribe: OpenAI's recommended
            # successor to whisper-1 (~$0.003/min audio, less
            # than whisper-1's $0.006, and more accurate per
            # their docs). ``language: "en"`` is a hint that
            # improves accuracy on the speech-through-music
            # case our AEC chain has to navigate.
            "transcription": {
                "model": "gpt-4o-mini-transcribe",
                "language": "en",
            },
        }
        if self._noise_reduction:
            input_audio["noise_reduction"] = {"type": self._noise_reduction}

        session: dict = {
            "type": "realtime",
            "model": self._model,
            "output_modalities": ["audio"],
            "instructions": instruction or "",
            "audio": {
                "input": input_audio,
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
            # `truncation: "auto"` lets the server prune old conversation
            # items as context fills, preserving the prompt-cache prefix.
            # Required for long-lived smart-speaker sessions: complements
            # (does not replace) the opt-in idle context reset by handling
            # the steady-state context bloat the reset doesn't address.
            # When `context_reset_sec` is 0 (default), this is the only
            # context-management strategy in play.
            "truncation": "auto",
        }
        # ``reasoning.effort`` is gated to reasoning-capable models
        # (``gpt-realtime-2``). We detect that from the model name
        # carrying "-2"; older models (gpt-realtime, gpt-realtime-1.5,
        # gpt-realtime-mini) don't accept the field.
        if self._reasoning_effort and "-2" in self._model:
            session["reasoning"] = {"effort": self._reasoning_effort}
        return session

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

    async def _open_session_attempt(self) -> None:
        connect_call = self._resolve_connect_call()
        t0 = _time.monotonic()
        cm = connect_call(model=self._model)
        try:
            conn = await cm.__aenter__()
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            raise
        self._session_cm = cm
        self._session = conn
        connect_ms = (_time.monotonic() - t0) * 1000
        log_event(
            self._logger, "provider.connected", provider=self.PROVIDER_NAME,
            ms=round(connect_ms), model=self._model, level=logging.INFO,
        )
        try:
            await self._send_event({
                "type": "session.update",
                "session": self._build_session_payload(),
            })
            events = aiter(conn)
            async with asyncio.timeout(SESSION_SETUP_TIMEOUT_SEC):
                async for event in events:
                    etype = _event_type(event)
                    if etype == "session.updated":
                        break
                    if etype == "error":
                        error = _event_field(event, "error")
                        code, error_type, message = (_event_field(error, key) for key in ("code", "type", "message"))
                        self._log_server_error(code=code, error_type=error_type, message=message)
                        error_cls = ValueError if openai_error_is_terminal(code=code, error_type=error_type) else RuntimeError
                        raise error_cls(self._redacted(
                            error_cls(message or f"{error_type or '?'} {code or '?'}"),
                        ))
                else:
                    raise ConnectionError("session closed before setup acknowledgement")
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, TimeoutError):
                e.args = ("session setup acknowledgement timed out",)
            log_event(
                self._logger, "provider.setup_failed", provider=self.PROVIDER_NAME,
                exc_type=type(e).__name__, detail=self._redacted(e), level=logging.WARNING,
            )
            await self._close_with_timeout(conn)
            await self._close_cm_with_timeout(cm)
            self._session = None
            self._session_cm = None
            raise
        self._deferred_reconnect.clear()
        await self._mark_connected(asyncio.create_task(self._receive_loop(events, conn)))

    async def _teardown_session(self) -> None:
        t0 = _time.monotonic()
        conn, cm = self._session, self._session_cm
        if self._active_turn is not None:
            self._active_turn._cancel_tools()
        self._session = self._session_cm = None
        self._connected_event.clear()
        self._pending_commit = self._pending_response = None
        # Cancel the proactive watchdog first — its only job is to fire on
        # a CONNECTED session, and we're about to leave that state.
        await self._cancel_task(self._proactive_watchdog_task)
        self._proactive_watchdog_task = None
        self._deferred_reconnect.clear()
        await self._cancel_task(self._receive_task)
        self._receive_task = None
        await self._close_with_timeout(conn)
        await self._close_cm_with_timeout(cm)
        # Close any in-flight billable-activity interval (time-billed
        # providers). Idle WebSocket lifetime is not counted.
        self._mark_billable_activity_ended()
        self._log_teardown(_time.monotonic() - t0)

    def _watchdog_delay_sec(self) -> float:
        """How long into a session to pre-empt OpenAI's hard cap.

        The cap is 60 min, with no resumption and no pre-cap warning
        event (per the realtime-conversations docs). When it fires the
        server sends a 1001 close and the supervisor reconnects
        reactively, costing the user a ~3 s `cant_connect` cue; firing a
        buffer ahead of it, in an idle window, means the next wake hits a
        fresh connection instead. Disabled when either knob is 0."""
        if self._session_max_sec <= 0 or self._proactive_buffer_sec <= 0:
            return 0.0
        delay = self._session_max_sec - self._proactive_buffer_sec
        if delay <= 0:
            # Misconfiguration (buffer ≥ cap). Log loudly and skip — a
            # zero/negative delay would fire immediately on every
            # reconnect, which is a worse failure than just not doing
            # the proactive reconnect at all.
            log_event(
                self._logger, "provider.watchdog_disabled", provider=self.PROVIDER_NAME,
                session_max_sec=round(self._session_max_sec), proactive_buffer_sec=round(self._proactive_buffer_sec),
                level=logging.WARNING,
            )
            return 0.0
        return delay

    async def _receive_loop(self, events, conn) -> None:
        """The websockets library treats close codes 1000/1001 as end-of-stream and ends async for without raising.
        Both the exception path and the clean-exit path must wake the supervisor, or later wakes silently fail in send_audio on a dead session."""
        try:
            async for event in events:
                if self._session is not conn:
                    return
                etype = _event_type(event)
                if etype is None:
                    continue
                await self._dispatch_event(etype, event)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if self._session is conn:
                self._on_receive_loop_error(e)
            return
        if self._session is conn and not self._stopping.is_set():
            log_event(
                self._logger, "provider.session_closed", provider=self.PROVIDER_NAME,
                reason="clean_close", level=logging.WARNING,
            )
            request_unplanned_reopen(self)

    async def _dispatch_event(self, etype: str, event) -> None:
        turn = self._active_turn
        if turn is not None and self._owns_turn(turn) and _is_progress_event(etype):
            turn._note_activity()
        if etype == "error":
            error = _event_field(event, "error")
            code, error_type, message = (_event_field(error, key) for key in ("code", "type", "message"))
            self._log_server_error(code=code, error_type=error_type, message=message)
            return
        if etype in ("session.created", "session.updated"):
            return

        if etype == "input_audio_buffer.committed":
            owner, self._pending_commit = self._pending_commit, None
            if owner is not None and self._owns_turn(owner):
                owner._input_item_id = _event_field(event, "item_id")
            return
        if etype == "response.created":
            owner, self._pending_response = self._pending_response, None
            if owner is not None and self._owns_turn(owner):
                owner._response_id = _event_field(_event_field(event, "response"), "id")
                owner._response_item_ids.clear()
            return
        if turn is None or not self._owns_turn(turn):
            return

        if etype.startswith("conversation.item.input_audio_transcription."):
            if not turn._input_item_id or _event_field(event, "item_id") != turn._input_item_id:
                return
            if etype.endswith(".completed"):
                text = _event_field(event, "transcript")
                if isinstance(text, str):
                    turn._on_user_text_done(text)
            elif etype.endswith(".failed"):
                log_event(
                    logger, "provider.transcript_failed",
                    provider=self.PROVIDER_NAME, side="user",
                    level=logging.WARNING,
                )
            return

        response = _event_field(event, "response")
        response_id = _event_field(response, "id") if response is not None else _event_field(event, "response_id")
        if not turn._response_id or response_id != turn._response_id:
            if etype == "response.done":
                log_event(logger, "provider.response_stale", provider=self.PROVIDER_NAME, level=logging.DEBUG)
            return
        if etype == "response.done":
            await self._handle_response_done(response, turn)
            return
        if turn._cancel_requested:
            return
        if etype == "response.output_item.added":
            item = _event_field(event, "item")
            item_id = _event_field(item, "id")
            if isinstance(item_id, str) and item_id:
                turn._response_item_ids.add(item_id)
            return

        item_id = _event_field(event, "item_id")
        if item_id not in turn._response_item_ids:
            return
        if etype == "response.output_audio.delta":
            delta = _event_field(event, "delta")
            if isinstance(delta, str):
                await turn._on_audio_delta(delta, item_id)
        elif etype in (
            "response.audio_transcript.delta", "response.output_audio_transcript.delta",
            "response.output_text.delta",
        ):
            delta = _event_field(event, "delta")
            if isinstance(delta, str) and delta:
                turn._on_assistant_text_delta(delta)
                _trace_emit("text_out", {"delta": delta})
        elif etype in (
            "response.audio_transcript.done", "response.output_audio_transcript.done",
            "response.output_text.done",
        ):
            text = _event_field(event, "transcript") or _event_field(event, "text")
            if isinstance(text, str):
                turn._on_assistant_text_done(text)

    async def _handle_response_done(self, response, turn: OpenAIRealtimeTurn) -> None:
        status = _event_field(response, "status")
        if status == "in_progress":
            return
        # Retire before a tool await so duplicate completion cannot dispatch twice.
        turn._response_id = None
        usage = _normalise_usage(_event_field(response, "usage"))
        turn._record_usage(usage)
        function_calls = self._extract_function_calls(response)
        turn._tool_round_pending = bool(function_calls)
        log_event(
            logger, "provider.response_done", provider=self.PROVIDER_NAME,
            response_id=_event_field(response, "id"), status=status,
            function_calls=len(function_calls),
            input_tokens=(usage or {}).get("input_tokens", 0),
            output_tokens=(usage or {}).get("output_tokens", 0),
        )
        if status != "completed":
            if status == "cancelled" and turn._cancel_requested:
                await turn._on_response_done()
            else:
                turn._on_connection_lost()
            return
        if not function_calls or turn._cancel_requested:
            await turn._on_response_done()
            return
        turn._start_tool_calls(function_calls)

    def _extract_function_calls(self, response) -> list[ToolCall]:
        """Parse the ``function_call`` items out of a Realtime response's
        ``output[]``. Empty list if the response had no tool calls.

        Items are dicts in tests and ``RealtimeConversationItemFunctionCall``
        models in production; ``_event_field`` reads both. Realtime sends
        arguments as a JSON string — anything that is not an object becomes
        empty args, which `dispatch_tool` answers from the tool's signature."""
        calls = []
        for item in _event_field(response, "output") or ():
            if _event_field(item, "type") != "function_call":
                continue
            name = _event_field(item, "name") or ""
            try:
                args = json.loads(_event_field(item, "arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
                log_event(
                    self._logger, "provider.tool_arguments_invalid", provider=self.PROVIDER_NAME,
                    tool=redact_secrets(name, literals=self._secret_literals()), level=logging.WARNING,
                )
            calls.append(
                ToolCall(id=_event_field(item, "call_id") or "", name=name, args=args)
            )
        return calls


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


def _merge_transcript_completion(current: str, text: str) -> str:
    """Merge one completed user transcript into the turn aggregate.

    Grok can emit progressive ``input_audio_transcription.completed`` strings
    for one user item. Treat prefix-shaped completions as refinements instead
    of appending them into ``/chat`` as repeated commands.
    """
    current = current.strip()
    text = text.strip()
    if not current:
        return text
    if not text:
        return current
    if _transcript_is_prefix(current, text):
        return text
    if _transcript_is_prefix(text, current):
        return current
    return f"{current} {text}"


def _transcript_is_prefix(short: str, long: str) -> bool:
    short_key = _transcript_compare_key(short)
    long_key = _transcript_compare_key(long)
    if not short_key:
        return True
    return long_key == short_key or long_key.startswith(f"{short_key} ")


def _transcript_compare_key(text: str) -> str:
    boundary = ".,!?;:"
    return " ".join(
        token.strip(boundary).casefold()
        for token in text.split()
        if token.strip(boundary)
    )
