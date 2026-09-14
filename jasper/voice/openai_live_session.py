# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""GPT-Live voice streaming with managed Responses delegation.

See https://developers.openai.com/api/docs/guides/live-migration.
One billable Live session is owned by one wake conversation.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import time
from typing import Any

from ..backoff import reconnect_delay
from ..log_event import log_event
from ._base import SESSION_CLOSE_TIMEOUT_SEC, BaseLiveConnection, BaseLiveTurn, ToolCall
from ._supervisor import failure_detail, is_transient
from .openai_session import _upsample_16k_to_24k
from .session import AudioOutChunk, ConnectionState, TurnCapture, TurnUsage

logger = logging.getLogger(__name__)

FRONTEND_INSTRUCTIONS = (
    "You are Jasper, a concise household voice assistant. Answer briefly and naturally. "
    "Keep listening while the user pauses to think or finishes a thought. A bare wake "
    "word, a half-finished phrase, background noise, music, or nearby conversation is "
    "not a request: stay silent and keep listening. A standalone stop, cancel, never "
    "mind, okay thanks, or goodbye is not a half-finished phrase, it is a request to "
    "end the conversation: delegate it at once so the backend can end_conversation. "
    "Speak only once the user has asked you something, and never greet the user or "
    "announce that you are ready. "
    "Accept follow-up questions without asking for a wake word. Let the user interrupt "
    "or correct you. Delegate all requests needing tools, local device information, "
    "current facts, actions, or deeper reasoning to the backend. The backend has the "
    "speaker's local tools for transit, timers, music and other functions; use those "
    "instead of guessing or pretending an action succeeded. "
    "Cancel my timer and stop music are tool requests, not requests to end the conversation. "
    "Do not invite another question after every answer."
)

# Seconds of quiet output delta still played after the last audible one, so
# the fan-in TTS lane stays fed between words. Must end well inside
# `response_stall_timeout_sec`, or playout never drains and the host's
# follow-up window never opens (`continuous_watchdog` in .conversation).
SILENCE_BRIDGE_SEC = 0.8

# int16 RMS floor for "this delta carries speech" (about -60 dBFS).
AUDIBLE_RMS_FLOOR = 32

# Ceiling on waiting for the server's `session.close` ack. Live bills per
# connected minute, so the close is still sent and the transport still
# torn down; only the ack is given up on. Measured ~2.9 s per turn end on
# jts.local, which is dead time the next wake would inherit.
CLOSE_ACK_TIMEOUT_SEC = 1.5

# Ceiling on the whole turn-acquire handshake, every attempt together, so
# a retry never doubles what a wake waits for. The daemon puts no bound of
# its own on `acquire_turn`.
SESSION_OPEN_BUDGET_SEC = 15.0

# Session opens one wake pays for. Live holds no socket between
# conversations, so the acquire is the only retry it has — there is no
# supervisor behind it. The second attempt covers the 409 race against the
# session the previous conversation just closed (`_supervisor.is_transient`).
SESSION_OPEN_ATTEMPTS = 2


def _parse_call(call: dict) -> ToolCall:
    """A backend `function_call` item. Arguments that are not a JSON
    object are answered without dispatching the tool.

    Must be total: this runs in `on_event`, ahead of the round task, so a
    raise here would escape `_receive`'s exception boundary and drop the
    whole connection instead of just this round.
    """
    call_id = call.get("call_id", "")
    name = call.get("name", "")
    try:
        args = json.loads(call.get("arguments", ""))
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
    except (ValueError, TypeError):
        return ToolCall(call_id, name, {}, {"error": "invalid_arguments"})
    return ToolCall(call_id, name, args)


class OpenAILiveTurn(BaseLiveTurn):
    continuous_input = True
    # Live's own VAD stops generation when the user talks over it, and the
    # protocol carries no frontend cancel/truncate command, so this turn is
    # deliberately not `session.Interruptible`.
    owns_interruption = True

    def __init__(self, conn, started_at):
        super().__init__(conn, started_at)
        self._conn: OpenAILiveConnection = conn
        self._resample_state = None
        self._input_q = asyncio.Queue(maxsize=16)
        self._input_admitted = True
        self._sender = None
        self._transcript_intervals = {"user": [], "assistant": []}
        self._seconds = 0.0
        self._quiet_played = 0
        self._quiet_discarded = 0
        self._finalized = False
        self._delegation_id = None
        # Delegation the in-flight tool round answers; a correction moves
        # `_delegation_id` on and abandons that round's results.
        self._round_delegation = None
        self._response_ids = {}
        self._calls = {}
        self._counted_responses = set()
        self.backend_pending = False

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._released or self._turn_lost:
            return
        if self._input_admitted:
            await self._input_q.put(pcm_16khz_int16)
            self._bytes_sent += len(pcm_16khz_int16)

    def discard_input(self) -> None:
        self._input_admitted = False
        self._resample_state = None
        while not self._input_q.empty():
            self._input_q.get_nowait()

    async def _send_audio_stream(self) -> None:
        while not self._released and not self._turn_lost:
            started = time.monotonic()
            try:
                pcm = self._input_q.get_nowait()
            except asyncio.QueueEmpty:
                pcm = bytes(2560)  # 80 ms at 16 kHz, including button-release silence
            else:
                if not self._input_admitted:
                    pcm = bytes(len(pcm))
            wire, self._resample_state = _upsample_16k_to_24k(pcm, self._resample_state)
            await self._conn._send({"type": "session.input_audio.append", "audio": base64.b64encode(wire).decode("ascii")})
            await asyncio.sleep(max(0, len(pcm) / 32000 - (time.monotonic() - started)))

    async def send_text_context(self, text: str) -> None:
        await self._conn._send({"type": "session.instructions.append", "content": text, "delegation_id": None})

    async def end_input(self) -> None:
        # Live needs silence as well as speech to advance its audio timeline.
        self._end_input_at_monotonic = time.monotonic()

    def usage(self) -> TurnUsage:
        # Live bills the frontend session per minute, not per token, so
        # this replaces the base turn's token counts with the metered
        # seconds. The backend's own tokens are a separate spend row
        # (`_on_backend_event`), not part of this turn's usage.
        return TurnUsage(breakdown={"seconds": self._seconds, "finalized": self._finalized})

    def capture(self) -> TurnCapture:
        return TurnCapture(
            user_text=self.user_transcript() or None,
            assistant_text=self.assistant_transcript() or None,
            data={"transcript_intervals": self._transcript_intervals, "voice_usage": self.usage().breakdown},
        )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.discard_input()
        await self._conn._cancel_task(self._sender)
        self._cancel_tools()
        try:
            await self._conn._close_live_session()
        finally:
            self._audio_q.put_nowait(None)
            self._log_release()
            await self._conn._on_turn_released(self)

    def _release_fields(self) -> dict[str, Any]:
        """Live bills the frontend session per metered second, not per
        token, and bridges the quiet between audible deltas."""
        return {
            "seconds": round(self._seconds, 3),
            "finalized": self._finalized,
            "quiet_played": self._quiet_played,
            "quiet_discarded": self._quiet_discarded,
        }

    async def on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind in {"session.usage.updated", "session.closed"}:
            self._seconds = max(self._seconds, float((event.get("usage") or {}).get("seconds", 0)))
            self._finalized = kind == "session.closed"
            self._server_turn_complete = self._finalized
            return
        if kind == "response.event":
            await self._on_backend_event(event)
            return
        if self._released or self._turn_lost:
            return
        if kind == "session.output_audio.delta":
            self._on_output_audio(base64.b64decode(event["delta"]))
        elif kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            speaker = "user" if kind == "session.input_transcript.delta" else "assistant"
            self._transcript_intervals[speaker].append({k: event[k] for k in ("delta", "start_ms", "end_ms")})
            self.add_transcript(**{speaker: event["delta"]})
            self._note_activity()
        elif kind == "session.delegation.created":
            delegation = event["delegation"]
            self._cancel_tools()
            self._delegation_id = delegation["id"]
            self._response_ids.clear()
            self._calls.clear()
            self.backend_pending = True
            self._note_activity()

    def _on_output_audio(self, pcm: bytes) -> None:
        """Admit one output delta to playout.

        An audible delta is the turn's answer audio and its activity
        anchor. The quiet between audible deltas is played too — the lane
        starves without it — but only while the answer is still running,
        never as an unbounded tail.
        """
        if not pcm:
            return
        now = time.monotonic()
        if audioop.rms(pcm, 2) > AUDIBLE_RMS_FLOOR:
            self._last_chunk_at = now
            self._chunks_received += 1
            self._note_activity()
            self._enqueue_audio(AudioOutChunk(pcm))
        elif self._last_chunk_at and now - self._last_chunk_at <= SILENCE_BRIDGE_SEC:
            self._quiet_played += 1
            self._enqueue_audio(AudioOutChunk(pcm))
        else:
            self._quiet_discarded += 1

    async def _on_backend_event(self, envelope: dict) -> None:
        event = envelope["event"]
        kind = event["type"]
        delegation = envelope.get("delegation_id")
        response = event.get("response") or {}
        response_id = response.get("id") or self._response_ids.get(delegation)
        if kind == "response.completed":
            if response_id in self._counted_responses:
                return
            self._counted_responses.add(response_id)
            usage = response.get("usage") or {}
            if usage and self._conn._usage_recorder is not None:
                self._conn._usage_recorder(
                    provider="openai", model=self._conn._backend_model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    usage={
                        "input_token_details": {"text_tokens": usage.get("input_tokens", 0),
                            "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)},
                        "output_token_details": {"text_tokens": usage.get("output_tokens", 0)},
                    },
                )
        if self._released or self._turn_lost or delegation != self._delegation_id or delegation is None:
            return
        self._note_activity()
        if kind == "response.created":
            self._response_ids[delegation] = response_id
            self._calls[response_id] = []
        elif kind == "response.output_item.done":
            item = event["item"]
            if item.get("type") == "function_call":
                self._calls.setdefault(response_id, []).append(item)
        elif kind == "response.completed":
            calls = self._calls.pop(response_id, [])
            if calls:
                # A correction cancels the old round before a new round is started.
                await self._drain_tool_round()
                # Must stay after the drain above: setting this before the
                # old round's task is cancelled would let it answer under
                # the new delegation.
                self._round_delegation = delegation
                self._start_tool_calls([_parse_call(c) for c in calls])
            else:
                self.backend_pending = False
        elif kind in {"response.failed", "response.incomplete"}:
            self.backend_pending = False
            self._on_connection_lost()

    def _tools_may_run(self) -> bool:
        return not self._released and self._round_delegation == self._delegation_id

    async def _send_tool_result(self, call: ToolCall, payload: dict) -> bool:
        await self._conn._send({"type": "response.item.create", "item": {
            "type": "function_call_output", "call_id": call.id, "output": self._tool_result_json(call.name, payload),
        }})
        self._note_activity()
        return True

    async def _finish_tool_round(self) -> bool:
        await self._conn._send({"type": "response.create"})
        return True


class OpenAILiveConnection(BaseLiveConnection):
    PROVIDER_NAME = "openai_live"
    _logger = logger
    _log_tag = "openai live connection:"

    def __init__(self, *, api_key, model="gpt-live-1", voice="marin", backend_model="gpt-5.4-mini", connect=None):
        super().__init__(model=model, voice=voice)
        self._api_key = api_key
        self._backend_model = backend_model
        self._connect = connect
        self._client = None
        self._session_cm = None
        self._session = None
        self._started = asyncio.Event()
        self._closed = asyncio.Event()
        self._billable_activity_meter = None
        self._usage_recorder = None

    def set_billable_activity_meter(self, meter) -> None:
        self._billable_activity_meter = meter

    def set_background_usage_recorder(self, recorder) -> None:
        self._usage_recorder = recorder

    def _secret_literals(self) -> tuple[str, ...]:
        return (self._api_key,)

    async def start(self, registry, system_instruction) -> None:
        self._registry = registry
        self._system_instruction_provider = system_instruction if callable(system_instruction) else lambda: system_instruction
        self._set_state(ConnectionState.CONNECTED)

    async def acquire_turn(self) -> OpenAILiveTurn:
        async with self._turn_lock:
            if self._active_turn is not None:
                raise RuntimeError("Live conversation already active")
            self._set_state(ConnectionState.CONNECTING)
            try:
                async with asyncio.timeout(SESSION_OPEN_BUDGET_SEC):
                    turn = await self._open_session_for_turn()
                if self._billable_activity_meter is not None:
                    self._billable_activity_meter.mark_started()
                self._set_state(ConnectionState.IN_TURN)
                turn._sender = asyncio.create_task(turn._send_audio_stream())
                turn._sender.add_done_callback(lambda task: turn._on_connection_lost() if not task.cancelled() and task.exception() else None)
                return turn
            except BaseException as exc:  # noqa: BLE001 — release the socket on cancellation and redact SDK failures
                try:
                    await self._teardown_session()
                finally:
                    self._active_turn = None
                    self._set_state(ConnectionState.CONNECTED)
                if isinstance(exc, Exception):
                    raise RuntimeError(failure_detail(exc, literals=self._secret_literals())) from None
                raise

    async def _open_session_for_turn(self) -> OpenAILiveTurn:
        """Open this wake's session, retrying one transient failure.

        A terminal failure — a rejected key, an account out of credit —
        is raised on the first attempt: `_open_session` has already
        recorded it and announced its remedy, and retrying cannot help.
        Each attempt gets its own turn, because tearing a half-open
        session down marks the turn it was opened for lost.
        """
        attempt = 0
        while True:
            attempt += 1
            self._started.clear()
            self._closed.clear()
            turn = OpenAILiveTurn(self, time.monotonic())
            self._active_turn = turn
            try:
                await self._open_session()
            except Exception as exc:  # noqa: BLE001
                if (
                    attempt >= SESSION_OPEN_ATTEMPTS
                    or self._stopping.is_set()
                    or not is_transient(exc)
                ):
                    raise
                self._on_reconnect_attempt_failed(exc, attempt, True)
                await self._teardown_session()
                await self._sleep(reconnect_delay(attempt, transient=True))
            else:
                return turn

    async def _open_session_attempt(self) -> None:
        assert self._registry is not None
        assert self._system_instruction_provider is not None
        # Held locally: a concurrent `stop()` nulls the shared field while
        # this awaits, and the attempt still owns the turn it opened for.
        turn = self._active_turn
        connect = self._connect
        if connect is None:
            from openai import AsyncOpenAI  # lazy — optional provider SDK
            self._client = AsyncOpenAI(api_key=self._api_key)
            connect = self._connect = self._client.live.connect
        self._session_cm = connect()
        self._session = await self._session_cm.__aenter__()
        self._receive_task = asyncio.create_task(self._receive(turn))
        await self._send({"type": "session.start", "session": {
            "model": self._model, "instructions": FRONTEND_INSTRUCTIONS, "store": False,
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": self._voice}},
            "delegation": {"type": "responses", "responses": {
                "model": self._backend_model, "instructions": self._system_instruction_provider(),
                "tools": [dict(t, strict=False) for t in self._registry.openai_tools(provider="openai_live")] + [{"type": "web_search"}],
                "tool_choice": "auto", "parallel_tool_calls": False,
            }},
        }})
        await self._started.wait()
        if turn.turn_lost() or self._stopping.is_set():
            raise RuntimeError("Live session failed during startup")

    async def _send(self, event) -> None:
        if self._session is None:
            raise RuntimeError("Live socket is closed")
        await self._session.send(event)

    async def _receive(self, turn: OpenAILiveTurn) -> None:
        try:
            async for raw in self._session:
                event = raw if isinstance(raw, dict) else raw.model_dump()
                if event["type"] == "session.started":
                    self._started.set()
                elif event["type"] == "error":
                    raise RuntimeError("Live command rejected")
                else:
                    await turn.on_event(event)
                    if event["type"] == "session.closed":
                        self._closed.set()
                        break
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger, "provider.session_lost", provider=self.PROVIDER_NAME,
                detail=failure_detail(exc, literals=self._secret_literals()),
            )
        finally:
            if not turn._released:
                turn._on_connection_lost()
            self._started.set()

    async def _close_live_session(self) -> None:
        try:
            if self._session is not None and not self._closed.is_set():
                await self._send({"type": "session.close"})
                await asyncio.wait_for(self._closed.wait(), CLOSE_ACK_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger, "provider.close_failed", provider=self.PROVIDER_NAME,
                phase="finalize",
                detail=failure_detail(exc, literals=self._secret_literals()),
                level=logging.WARNING,
            )
        finally:
            turn = self._active_turn
            if self._billable_activity_meter is not None:
                self._billable_activity_meter.mark_ended(
                    seconds=turn._seconds if turn and turn._finalized else None,
                )
            await self._teardown_session()

    async def _teardown_session(self) -> None:
        await self._cancel_task(self._receive_task)
        self._receive_task = None
        try:
            await self._close_cm_with_timeout(self._session_cm)
        finally:
            self._session_cm = self._session = None

    async def stop(self) -> None:
        # Set before the release, so an acquire still dialling fails its
        # open instead of handing back a turn on a closed connection.
        self._stopping.set()
        # Released first: only the turn's own path sends `session.close` and
        # settles the billable interval. `super().stop()` then tears down
        # what is left, idempotently.
        turn = self._active_turn
        if turn is not None:
            # Bounded: the release ends by taking `_turn_lock`, which an
            # acquire still dialling holds for up to
            # SESSION_OPEN_BUDGET_SEC — past the unit's TimeoutStopSec.
            try:
                await asyncio.wait_for(turn.release(), SESSION_CLOSE_TIMEOUT_SEC)
            except TimeoutError:
                log_event(
                    logger, "provider.close_failed", provider=self.PROVIDER_NAME,
                    phase="release", detail="release timed out",
                    level=logging.WARNING,
                )
        await super().stop()
        if self._client is not None:
            await self._client.close()
