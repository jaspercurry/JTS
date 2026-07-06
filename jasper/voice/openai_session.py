# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import os
import time as _time
from collections import deque
from enum import Enum
from typing import Awaitable, AsyncIterator, Callable

from jasper.log_event import log_event

from ..tools import ToolRegistry, dispatch_tool
from ._supervisor import (
    ESCALATION_CUE_SLUG,
    ESCALATION_RATE_LIMIT_SEC,
    ESCALATION_REPEAT_THRESHOLD,
    DeferredReconnect,
    FailureFingerprint,
    reconnect_backoff_delay,
)
from .session import AudioOutChunk, LiveTurn

logger = logging.getLogger(__name__)


# Wire-format constants. The OpenAI Realtime ``audio/pcm`` discriminator
# accepts only 24 kHz (verified against ``RealtimeAudioFormats.AudioPCM``
# in openai-python's typed API). The XVF3800 captures at 16 kHz mono;
# we polyphase-upsample 16 → 24 inside the turn before base64-encoding.
OPENAI_AUDIO_RATE_HZ = 24000
DAEMON_MIC_RATE_HZ = 16000

# Initial-connect retry budget.
#
# Pre-2026-05-23 this was a fixed 5-element schedule capping at ~15 s
# total wall-time. That cap was the proximate cause of a permanent
# silent-dead voice daemon at boot on 2026-05-23: the daemon raced
# WiFi recovery from an unclean shutdown, hit ``[Errno -3] Temporary
# failure in name resolution`` on every attempt, exhausted the
# 5-retry cap before the network came up, and raised. With
# ``Restart=on-watchdog`` (which doesn't fire on a non-zero exit;
# the unit's old comment got that wrong) systemd never restarted it,
# and the speaker stayed silent until someone power-cycled.
#
# Two changes:
#
# 1. Initial-connect now retries forever within a time budget (default
#    10 minutes — long enough for any realistic home-network blip,
#    short enough that a real misconfiguration still surfaces via the
#    systemd outer loop within ~10 min on the doctor / dashboard).
#    Exponential backoff with jitter via the shared
#    ``reconnect_backoff_delay`` helper, same shape as the post-connect
#    supervisor reconnect — so the same DNS blip on the 61st minute
#    looks the same as the same blip at boot.
# 2. Budget exhaustion still raises ``RuntimeError``. systemd's
#    ``Restart=on-failure`` (now correctly set, was ``on-watchdog``)
#    + ``StartLimitBurst=20`` / ``StartLimitIntervalSec=300`` is the
#    outer loop: process exits non-zero, systemd waits ``RestartSec=5``,
#    spawns a fresh process, gets another 10-minute budget.
#
# Override via ``JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC``. Production
# values are wired in the constructor's ``initial_connect_budget_sec``
# default — tests pass a small value (or 0 for "single attempt").
DEFAULT_INITIAL_CONNECT_BUDGET_SEC = 600.0

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
DEFAULT_NOISE_REDUCTION = "off"
# ``auto`` is resolved by voice.input_policy before production constructs
# this adapter. If a bare test/tool instantiates the adapter with auto, omit
# provider denoising rather than sending an invalid OpenAI wire value.
_NOISE_REDUCTION_DISABLED = frozenset((
    "", "auto", "off", "none", "disabled", "false", "0",
))
_NOISE_REDUCTION_WIRE_VALUES = frozenset(("near_field", "far_field"))


def _normalize_noise_reduction(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if (
        normalized
        and normalized not in _NOISE_REDUCTION_DISABLED
        and normalized not in _NOISE_REDUCTION_WIRE_VALUES
    ):
        allowed = sorted(
            (_NOISE_REDUCTION_DISABLED | _NOISE_REDUCTION_WIRE_VALUES) - {""}
        )
        raise RuntimeError(
            "OpenAI noise_reduction must be one of: " + ", ".join(allowed)
        )
    return normalized


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


class OpenAIRealtimeTurn:
    """A single turn against an open ``OpenAIRealtimeConnection``.

    Owns the per-turn audio queue, the resampler state, and per-turn
    counters. The connection's receive loop routes incoming server
    events here while a turn is active.
    """

    def __init__(self, conn: "OpenAIRealtimeConnection", started_at: float) -> None:
        self._conn = conn
        self._audio_q: asyncio.Queue[AudioOutChunk | None] = asyncio.Queue()
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
        # Tracks chunk-size distribution per turn; logged at release so a uniform vs. front-loaded delivery is visible post hoc.
        self._chunk_bytes_total: int = 0
        self._chunk_bytes_max: int = 0
        self._first_chunk_bytes: int = 0
        # Tracks whether `commit()` + `response.create()` has been sent.
        # Idempotent like Gemini's _activity_end_sent.
        self._committed = False
        self._released = False
        self._turn_lost = False
        self._server_turn_complete = False
        # Text transcript of the user audio / assistant audio streamed by
        # Realtime. Production still uses audio for interaction; the strings
        # are retained on the turn only so WakeLoop can write opt-in
        # conversation history without logging transcript content.
        self._assistant_transcript_parts: list[str] = []
        self._user_transcript_parts: list[str] = []
        # Polyphase resampler state, persists across send_audio calls.
        # Reset to None at turn start so the first frame doesn't carry
        # tail samples from the previous turn.
        self._resample_state: tuple | None = None
        # Debug: tee the exact 24 kHz bytes being sent to OpenAI into
        # a per-turn WAV file. Gated on JASPER_DEBUG_RECORD_OPENAI_AUDIO=1
        # so it stays off in production. Lets us answer "did the user's
        # full sentence reach OpenAI" without guessing — the WAV here
        # is exactly what OpenAI's STT model received.
        self._debug_wav = None
        self._debug_wav_path: str | None = None
        # The most recent assistant audio item id seen (set from
        # `response.output_item.added`). `truncate_assistant_audio` uses
        # it as the `conversation.item.truncate` target when the daemon's
        # barge-in spine doesn't carry a provider id — see the barge-in
        # capability seam below. Unused when barge-in is off (the
        # default), since nothing then drives a flush + truncate.
        self._last_assistant_item_id: str | None = None
        # Per-item received audio (ms), keyed by assistant item id. Lets
        # truncate_assistant_audio clamp the turn-wide ledger played-ms to the
        # target item's own duration (C1). Per-turn dict, discarded at turn
        # end; a tool-using turn holds only its handful of item ids.
        self._received_ms_by_item: dict[str, float] = {}
        self._server_vad_active: bool = False
        self._server_speech_started: bool = False
        self._server_speech_stopped: bool = False
        self._server_committed: bool = False
        self._server_eou_event: asyncio.Event = asyncio.Event()

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

    async def send_text_context(self, text: str) -> None:
        if self._released or self._turn_lost or self._committed:
            return
        await self._conn._send_text_context(text)

    async def submit_recorded_audio(self, pcm_16khz_int16: bytes) -> None:
        """Submit a complete pre-recorded user audio blob in one shot.

        OpenAI Realtime distinguishes two audio-input paths:

          * ``input_audio_buffer.append`` (used by ``send_audio`` above)
            — for live audio streamed over a long-running open buffer.
          * ``conversation.item.create`` with ``input_audio`` content
            — for complete pre-recorded files.

        The latter is OpenAI's documented path for pre-recorded audio
        (see developers.openai.com/api/docs/guides/realtime-conversations).
        We use it from the voice-eval harness when feeding synthesized
        prompt audio. The streaming path empirically caused the model
        to ignore tool definitions on pre-recorded audio (2026-05-21
        finding); the conversation-item path works correctly.

        Internally:
          1. Upsamples 16 kHz mono → 24 kHz (Realtime's required input
             format) using this turn's resampler state, so it composes
             cleanly with subsequent send_audio calls if any.
          2. Base64-encodes and sends ``conversation.item.create`` with
             ``input_audio`` content.
          3. Sends ``response.create`` to trigger inference.
          4. Marks the turn committed so a subsequent ``end_input()``
             is a no-op (it would otherwise try to commit an empty
             buffer and error with "the buffer is empty").

        Caller is the voice-eval harness only; production daemon code
        uses send_audio + end_input. The method lives on this adapter
        (not on the ``LiveTurn`` Protocol) because the conversation-item
        path is OpenAI-specific — other providers stream audio
        differently."""
        if self._released or self._turn_lost or self._committed:
            return
        pcm_24khz, self._resample_state = _upsample_16k_to_24k(
            pcm_16khz_int16, self._resample_state,
        )
        if not pcm_24khz:
            return
        b64 = base64.b64encode(pcm_24khz).decode("ascii")
        try:
            await self._conn._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_audio", "audio": b64}],
                },
            })
            await self._conn._send_event({"type": "response.create"})
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "openai turn: submit_recorded_audio failed (%s: %s); turn lost",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)
            return
        self._bytes_sent += len(pcm_16khz_int16)
        self._committed = True

    async def end_input(self) -> None:
        """Commit the user audio buffer and trigger a response.

        Equivalent of Gemini's ``activity_end``: server stops listening
        for more user audio and starts generating. Idempotent.

        No-op when server_vad is active — the server already committed
        the buffer via speech_stopped + committed events."""
        if self._committed or self._released or self._turn_lost:
            return
        if self._server_vad_active:
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
        async for chunk in self.audio_out_chunks():
            yield chunk.pcm

    async def audio_out_chunks(self) -> AsyncIterator[AudioOutChunk]:
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                return
            if isinstance(chunk, bytes):
                chunk = AudioOutChunk(pcm=chunk)
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
        # Close debug WAV if open. Always log the path so the user
        # can find which file goes with which turn.
        if self._debug_wav is not None:
            try:
                self._debug_wav.close()
                logger.info(
                    "debug: closed OpenAI send-audio WAV: %s",
                    self._debug_wav_path,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("debug record close failed: %s", e)
            self._debug_wav = None
        # If teardown races an already-committed response, best-effort cancel
        # it so the server doesn't keep generating after local playback has
        # gone away. No-speech aborts release an uncommitted input buffer; do
        # not send response.cancel there, because the server has no active
        # response and reports a noisy response_cancel_not_active error.
        if self._committed and not self._server_turn_complete and not self._turn_lost:
            try:
                await self._conn._cancel_response()
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "openai turn: release cancel ignored (%s: %s)",
                    type(e).__name__, e,
                )
        await self._conn._on_turn_released(self)
        assistant_text = self.assistant_transcript().strip()
        if assistant_text:
            # Keep transcript content out of logging entirely: the
            # flight recorder buffers DEBUG records and dumps them to
            # journald around failures, so even DEBUG lines must carry
            # metadata rather than household utterances.
            log_event(
                logger,
                "openai.assistant_transcript",
                chars=len(assistant_text),
                level=logging.DEBUG,
            )
        if self._chunks_received > 0:
            avg = self._chunk_bytes_total // self._chunks_received
            logger.info(
                "openai turn: ended in %.0fms, %d chunks received "
                "(sent=%dB, audio=%dB first=%dB max=%dB avg=%dB ~%.0fms total)",
                elapsed_ms, self._chunks_received, self._bytes_sent,
                self._chunk_bytes_total, self._first_chunk_bytes,
                self._chunk_bytes_max, avg, self._chunk_bytes_total / 48.0,
            )
        else:
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

    def audio_chunks_pending(self) -> int:
        return self._audio_q.qsize()

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

    def assistant_transcript(self) -> str:
        return "".join(self._assistant_transcript_parts)

    def user_transcript(self) -> str:
        return " ".join(self._user_transcript_parts)

    def interrupted(self) -> bool:
        return self._interrupted

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    # ---- Barge-in capability seam (OpenAI reference pack) ----
    #
    # Reconciliation kind for OpenAI is `needs_client_truncate` (catalog):
    # the WebSocket transport keeps the whole generated assistant turn
    # server-side, so after JTS flushes local TTS on a barge-in the client
    # must (1) stop generation with `response.cancel` and (2) trim the
    # conversation item to the *heard* boundary with
    # `conversation.item.truncate`. The daemon's `_flush_for_interrupt`
    # spine calls these — in this order — once the local flush has the
    # playout ledger's played-ms; Gemini's pack no-ops both. Grok inherits
    # this whole pack via `GrokRealtimeConnection`.

    async def cancel_response(self, reason: str) -> None:
        """Stop the in-progress OpenAI response (the local/manual cancel).

        Guard: `response.cancel` errors with `response_cancel_not_active`
        when no response is generating, so only send while one is. The
        "response in progress" predicate mirrors `release()`'s: the input
        buffer is committed, the server hasn't completed the response, and
        the connection is still up. Idempotent and never raises —
        `_cancel_response()` swallows wire errors at DEBUG."""
        if not (
            self._committed
            and not self._server_turn_complete
            and not self._turn_lost
        ):
            # No active response — cancelling now would trip the server's
            # noisy response_cancel_not_active error.
            return
        log_event(logger, "barge.cancel", reason=reason)
        await self._conn._cancel_response()

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        """Align OpenAI conversation history to what the listener heard.

        Sends `conversation.item.truncate{item_id, content_index:0,
        audio_end_ms}`. `item_id` falls back to the turn's own
        `_last_assistant_item_id` (captured from
        `response.output_item.added`) so the daemon spine never has to
        carry a provider id; `None` is tolerated (a barge-in that raced
        the first item event leaves nothing to truncate — a no-op).

        CRITICAL GUARD: `audio_end_ms` MUST be the ms *actually rendered*
        per the playout ledger, never bytes-received. A `0` from the
        ledger means it observed no rendered audio (the production fan-in
        ack can return `max_audio_played_ms=0`); truncating anyway would
        send an `audio_end_ms` past the heard boundary, which OpenAI
        rejects as out-of-range and which desyncs the conversation
        context. So a non-positive played-ms is a no-op + WARN, never a
        bytes-received guess. Idempotent and never raises."""
        if self._turn_lost:
            return
        item_id = provider_item_id or self._last_assistant_item_id
        if not item_id:
            # Barge-in raced response.output_item.added — no assistant
            # item to align yet. Nothing to truncate.
            log_event(
                logger, "barge.truncate_skipped",
                reason="no_item_id", level=logging.DEBUG,
            )
            return
        if audio_played_ms <= 0:
            log_event(
                logger, "barge.truncate_skipped",
                reason="zero_played_ms", item_id=item_id,
                level=logging.WARNING,
            )
            return
        audio_end_ms = int(audio_played_ms)
        received_ms = self._received_ms_by_item.get(item_id)
        if received_ms is not None and audio_end_ms > received_ms:
            # C1: the playout ledger reports a turn-WIDE max played-ms, but a
            # multi-segment (tool-using) turn can carry an earlier item whose
            # ledger ms exceeds THIS in-flight item's audio. Truncating the
            # item past its own received duration is the out-of-range case the
            # server rejects. Clamp to what this item actually received — an
            # upper bound on what could have been heard (truncates down).
            log_event(
                logger, "barge.truncate_clamped",
                item_id=item_id, requested_ms=audio_end_ms,
                clamped_ms=int(received_ms), level=logging.DEBUG,
            )
            audio_end_ms = int(received_ms)
        log_event(
            logger, "barge.truncate",
            # getattr-guarded so the log can't itself raise (e.g. a turn
            # built with a stub connection, or a torn-down `_conn`); the
            # send below is what actually needs a live connection, and it
            # is wrapped. Grok overrides PROVIDER_NAME to "grok".
            provider=getattr(self._conn, "PROVIDER_NAME", "openai"),
            item_id=item_id, audio_end_ms=audio_end_ms,
        )
        try:
            await self._conn._send_event({
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            })
        except Exception as e:  # noqa: BLE001
            log_event(
                logger, "barge.truncate_failed",
                item_id=item_id, error=type(e).__name__, detail=str(e),
                level=logging.WARNING,
            )

    def request_local_interrupt(self) -> None:
        # Local barge-in (PR-2 spine): arm the local playback flush only —
        # this does NOT itself send conversation.item.truncate / response.cancel.
        # Provider reconciliation is the seam above (cancel_response /
        # truncate_assistant_audio), which the daemon's _flush_for_interrupt
        # drives *after* the flush. OpenAI/Grok never set _interrupt_event from
        # the server side, so this is the only path that arms the flush race
        # for these providers.
        self._interrupted = True
        self._interrupt_event.set()

    def drop_pending_audio(self) -> int:
        # The "distinct signal" anticipated by _on_response_done's sentinel
        # comment. A local-barge flush clears the DAC ring, but the response
        # was burst-delivered into _audio_q, so the play loop would resume
        # writing the backlog and the assistant would talk over the user.
        # Drain the queued chunks now, PRESERVING any terminal None sentinel
        # so audio_out_chunks still ends the turn.
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

    # ---- Server VAD ----

    def server_vad_active(self) -> bool:
        return self._server_vad_active

    def server_speech_started(self) -> bool:
        return self._server_speech_started

    def server_speech_detected(self) -> bool:
        return self._server_speech_stopped and self._server_committed

    async def wait_for_server_eou(self) -> None:
        await self._server_eou_event.wait()

    def mark_server_vad(self) -> None:
        self._server_vad_active = True

    def _mark_server_vad(self) -> None:
        self.mark_server_vad()

    def _on_speech_started(self) -> None:
        self._server_speech_started = True
        log_event(logger, "server_vad.speech_started")

    def _on_speech_stopped(self) -> None:
        self._server_speech_stopped = True
        log_event(logger, "server_vad.speech_stopped")
        if self._server_committed:
            self._server_eou_event.set()

    def _on_server_committed(self) -> None:
        self._server_committed = True
        self._committed = True
        log_event(logger, "server_vad.committed")
        if self._server_speech_stopped:
            self._server_eou_event.set()

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
        chunk_bytes = len(data)
        self._chunk_bytes_total += chunk_bytes
        if chunk_bytes > self._chunk_bytes_max:
            self._chunk_bytes_max = chunk_bytes
        if not self._first_chunk_logged:
            self._first_chunk_logged = True
            self._first_chunk_bytes = chunk_bytes
            first_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
            logger.info(
                "first audio chunk from OpenAI in %.0fms (turn start→1st chunk, "
                "%d bytes ~%.0fms audio)",
                first_ms, chunk_bytes, chunk_bytes / 48.0,
            )
        item_id = self._last_assistant_item_id
        if item_id:
            # 24 kHz mono pcm16 = 48 bytes/ms. Accumulate per item so a later
            # truncate can clamp to THIS item's received duration (C1).
            self._received_ms_by_item[item_id] = (
                self._received_ms_by_item.get(item_id, 0.0) + chunk_bytes / 48.0
            )
        await self._audio_q.put(AudioOutChunk(
            pcm=data,
            provider_item_id=item_id,
        ))

    def _note_activity(self) -> None:
        """Reset the pre-response idle anchor.

        Called by the connection's receive loop on intermediate server
        events (e.g. a tool-call response.done) where the model is
        producing output but no audio chunk has arrived yet. The
        watchdog in ``jasper/voice_daemon.py:_idle_watchdog`` reads
        ``last_activity_at()`` to decide when to abandon a turn that
        looks stuck; without this reset it measures from turn-start
        across the entire tool dispatch and fires mid-flight at small
        ``JASPER_IDLE_TIMEOUT_SEC`` values (production: 10 s).

        ``_on_audio_delta`` does NOT call this — chunks arrive on a
        hot path and the loop clock is already read inline for the
        ``_last_chunk_at`` companion update."""
        self._last_activity_at = asyncio.get_event_loop().time()

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
        self._note_activity()
        self._server_turn_complete = True
        self._record_usage(usage)
        # Sentinel lets consumer drain queued chunks then exit; barge-in (if added later) must use a distinct signal.
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_q.put_nowait(None)

    def _on_assistant_item_id(self, item_id: str | None) -> None:
        if item_id:
            self._last_assistant_item_id = item_id

    def _on_assistant_text_delta(self, delta: str) -> None:
        if not delta:
            return
        self._assistant_transcript_parts.append(delta)
        self._note_activity()

    def _on_assistant_text_done(self, text: str) -> None:
        if not text:
            return
        current = self.assistant_transcript()
        if current:
            # Some providers send both deltas and a final text field.
            # Trust the deltas unless the final text clearly contains
            # more content, in which case replace the aggregate.
            if len(text) > len(current) and text.startswith(current):
                self._assistant_transcript_parts = [text]
            return
        self._assistant_transcript_parts = [text]

    def _on_user_text_done(self, text: str) -> None:
        text = text.strip()
        if text:
            current = self.user_transcript()
            merged = _merge_transcript_completion(current, text)
            if merged != current:
                self._user_transcript_parts = [merged]

    def _on_connection_lost(self) -> None:
        if self._released or self._turn_lost:
            return
        self._turn_lost = True
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_q.put_nowait(None)


# ---------- Long-lived connection ------------------------------------------


class OpenAIRealtimeConnection:
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
        noise_reduction: str = DEFAULT_NOISE_REDUCTION,
        temperature: float = DEFAULT_TEMPERATURE,
        # Proactive pre-cap reconnect — see `_proactive_reconnect_watchdog`.
        # Both default to 0 (disabled) so tests and bare-construction don't
        # spawn surprise tasks. Production wires production values from
        # Config (3600 / 300 → fires at 55 min uptime). Cap and buffer
        # are independent so OpenAI raising the cap to e.g. 7200 s only
        # requires changing the cap value; buffer (intent: "5 min before
        # whatever the cap is") stays correct.
        session_max_sec: float = 0.0,
        proactive_buffer_sec: float = 0.0,
        # Production: leave None → supervisor reconnects FOREVER with
        # the shared exponential-with-jitter schedule. Tests pass a
        # bounded tuple to make exhaustion observable.
        backoff_schedule: tuple[float, ...] | None = None,
        # Initial-connect time budget in seconds. None → read from
        # ``JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC`` env var
        # (default ``DEFAULT_INITIAL_CONNECT_BUDGET_SEC`` = 600 s).
        # Tests pass a small value (e.g. 0.5) for fast budget-exhaustion
        # assertions. Pass 0 for "single attempt, no retries"
        # (preserves the auth-error-propagates-immediately behaviour
        # that ``_is_transient`` already encodes — non-transient errors
        # never retry regardless of budget).
        initial_connect_budget_sec: float | None = None,
        # Test seam: replace the SDK's connect call. The factory must be
        # callable as ``factory(model: str)`` and return an async context
        # manager whose ``__aenter__`` yields a connection-like object
        # exposing ``.send(event_dict) / .__aiter__() / .close()``.
        connect_factory=None,
        # Test seam: monotonic clock source. Defaults to
        # ``time.monotonic``; tests inject a fake clock so they can
        # fast-forward through the budget without waiting in real time.
        clock=None,
        # Test seam: sleep function. Defaults to ``asyncio.sleep``;
        # tests inject a no-op so backoff doesn't burn wall-time.
        sleep=None,
        # Subclass override: ``GrokRealtimeConnection`` flips the base URL
        # without touching the rest of the wiring.
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._context_reset_sec = context_reset_sec
        self._reasoning_effort = reasoning_effort
        self._noise_reduction = _normalize_noise_reduction(noise_reduction)
        self._temperature = temperature
        self._session_max_sec = session_max_sec
        self._proactive_buffer_sec = proactive_buffer_sec
        self._backoff_schedule = backoff_schedule
        # Resolve the initial-connect budget: explicit kwarg > env var >
        # module default. Read once at construction so a test override
        # via kwarg can't be quietly clobbered by an ambient env var.
        if initial_connect_budget_sec is None:
            self._initial_connect_budget_sec = _read_initial_connect_budget_env()
        else:
            self._initial_connect_budget_sec = float(initial_connect_budget_sec)
        self._connect_factory = connect_factory
        # Wall-clock + sleep seams. Used by the initial-connect time
        # budget so tests can fast-forward through the schedule.
        self._monotonic = clock if clock is not None else _time.monotonic
        self._sleep = sleep if sleep is not None else asyncio.sleep
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

        # Optional billable-activity meter (time-billed providers, e.g.
        # Grok). Wired by the daemon before start() when the active
        # provider bills realtime activity; None for token-billed providers.
        # See jasper.usage.BillableActivityMeter.
        self._billable_activity_meter = None
        self._billable_activity_interval_open: bool = False

        # Proactive pre-cap reconnect — watchdog state.
        # Task that fires at (session_max_sec - proactive_buffer_sec); set
        # by `_open_session`, cancelled by `_teardown_session`.
        self._proactive_watchdog_task: asyncio.Task | None = None
        # When the watchdog fires mid-turn we defer the reconnect to
        # avoid tearing down the user's in-flight conversation; the
        # shared primitive is checked in `_on_turn_released` to fire the
        # deferred reconnect (provider-agnostic mechanism; OpenAI's
        # trigger is the proactive pre-cap watchdog — see _supervisor).
        self._deferred_reconnect = DeferredReconnect()
        self._server_vad_active: bool = False

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

    def set_billable_activity_meter(self, meter) -> None:
        """Wire a ``BillableActivityMeter`` for time-billed providers.

        Daemon calls this before ``start()``. Once set, ``acquire_turn``
        marks billable realtime activity up and turn release / connection
        loss marks it down. The warm idle WebSocket is intentionally not
        counted: xAI's dashboard reports Voice Realtime charges that match
        active turn time, not socket-open wall clock."""
        self._billable_activity_meter = meter

    def _mark_billable_activity_started(self) -> None:
        meter = self._billable_activity_meter
        if meter is None or self._billable_activity_interval_open:
            return
        meter.mark_started()
        self._billable_activity_interval_open = True

    def _mark_billable_activity_ended(self) -> None:
        meter = self._billable_activity_meter
        if meter is None or not self._billable_activity_interval_open:
            return
        meter.mark_ended()
        self._billable_activity_interval_open = False

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
            self._mark_billable_activity_started()
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

    def supports_server_vad(self) -> bool:
        return True

    def supports_provider_vad(self) -> bool:
        # OpenAI Realtime exposes native server-side VAD (`server_vad`) — the
        # same engine `supports_server_vad()` lets the daemon switch to
        # mid-session. Capability, not current config: production runs manual
        # VAD. Grok inherits this (xAI is OpenAI-compatible). Separate from
        # barge-in support — see the LiveConnection docstring.
        return True

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
        # Debug tee — see OpenAIRealtimeTurn._debug_wav docstring.
        if os.environ.get("JASPER_DEBUG_RECORD_OPENAI_AUDIO", "").strip() in ("1", "true", "yes", "on"):
            try:
                if turn._debug_wav is None:
                    import wave as _wave
                    import time as _time_mod
                    debug_dir = os.environ.get(
                        "JASPER_DEBUG_OPENAI_AUDIO_DIR",
                        "/tmp/jasper-openai-debug",
                    )
                    os.makedirs(debug_dir, exist_ok=True)
                    ts = _time_mod.strftime("%Y%m%dT%H%M%SZ", _time_mod.gmtime())
                    path = f"{debug_dir}/{ts}-{id(turn):x}.wav"
                    w = _wave.open(path, "wb")
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(OPENAI_AUDIO_RATE_HZ)
                    turn._debug_wav = w
                    turn._debug_wav_path = path
                    logger.info("debug: recording OpenAI send audio → %s", path)
                turn._debug_wav.writeframes(pcm_24khz)
            except Exception as e:  # noqa: BLE001
                logger.warning("debug record failed (will skip rest of turn): %s", e)
                turn._debug_wav = None
        b64 = base64.b64encode(pcm_24khz).decode("ascii")
        await self._send_event({
            "type": "input_audio_buffer.append",
            "audio": b64,
        })

    async def _send_text_context(self, text: str) -> None:
        await self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })

    async def _commit_and_create_response(self, turn: OpenAIRealtimeTurn) -> None:
        # Two events, in order: commit closes the user audio buffer (the
        # server then materialises it as a user message item); create
        # then asks the model to produce a response. Both required under
        # manual VAD — the server doesn't auto-commit or auto-respond.
        await self._send_event({"type": "input_audio_buffer.commit"})
        await self._send_event({"type": "response.create"})

    async def create_response_only(self) -> None:
        """Send response.create WITHOUT a preceding commit — used when
        server_vad has already committed the audio buffer."""
        await self._send_event({"type": "response.create"})

    async def _create_response_only(self) -> None:
        await self.create_response_only()

    async def set_turn_detection(self, mode: dict | None) -> None:
        """Switch turn detection mid-session.

        mode=None restores manual VAD. mode={...} activates server_vad
        (with create_response/interrupt_response already set to false by
        the caller so the daemon retains response timing control)."""
        if mode is not None and not self._server_vad_active:
            await self._send_event({"type": "input_audio_buffer.clear"})
        self._server_vad_active = mode is not None
        await self._send_event({
            "type": "session.update",
            "session": {
                # session.type is required on every session.update, not
                # just the first — omitting it returns
                # missing_required_parameter and the switch silently
                # no-ops, leaving the daemon waiting for a
                # speech_started event the server will never send.
                "type": "realtime",
                "audio": {
                    "input": {
                        "turn_detection": mode,
                    },
                },
            },
        })
        log_event(
            logger,
            "server_vad.switch",
            mode="server_vad" if mode is not None else "manual",
        )

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
        if self._server_vad_active:
            try:
                await self.set_turn_detection(None)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "openai connection: failed to restore manual VAD "
                    "after turn (%s: %s); next turn's session.update "
                    "will correct",
                    type(e).__name__, e,
                )
        self._mark_billable_activity_ended()
        async with self._turn_lock:
            if self._active_turn is turn:
                self._active_turn = None
                self._last_turn_end_at = asyncio.get_event_loop().time()
        async with self._state_lock:
            if self._state is ConnectionState.IN_TURN:
                self._set_state(ConnectionState.CONNECTED)
        # Fire any reconnect the proactive watchdog deferred for this turn.
        if self._deferred_reconnect.fire_if_pending(self._reconnect_event.set):
            logger.info(
                "openai connection: proactive reconnect — turn just ended, "
                "firing the watchdog-deferred reconnect",
            )

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
            # model's tool choice. Without this, every "why
            # didn't my phrase work?" debug is guesswork
            # (e.g. "kitchen medium" routed to set_volume(50)
            # on 2026-05-24 — STT mishearing or model
            # mis-routing? Could not tell). The model's
            # decisions still come from the raw audio, not
            # this transcript — STT here is observability,
            # not the input path.
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
        if self._noise_reduction not in _NOISE_REDUCTION_DISABLED:
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

    async def _do_initial_connect(self) -> None:
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open_session_with_retry(phase="initial-connect")
        except Exception:  # noqa: BLE001
            async with self._state_lock:
                self._set_state(ConnectionState.FAILED)
            raise

    async def _open_session_with_retry(self, *, phase: str) -> None:
        """Initial-connect retry loop with a time budget.

        Behaviour:
          * Each attempt calls ``_open_session()``; on success returns.
          * Auth / local-validation errors (non-transient per
            ``_is_transient``) propagate immediately — no retry, no
            wait. Surfaces a bad API key or malformed config without
            burning 10 minutes pretending it's a network issue.
          * Transient errors (network blip, DNS failure, 5xx, WS
            reset) retry with exponential backoff + jitter via the
            shared ``reconnect_backoff_delay`` helper, until either
            the next attempt succeeds OR the wall-time budget is
            exhausted.
          * On budget exhaustion: ``RuntimeError``. Caller (the
            daemon's ``start()`` path) lets that propagate so the
            process exits non-zero and systemd's ``Restart=on-failure``
            spawns a fresh process with another full budget.

        The budget covers cumulative wall-time, NOT a fixed retry
        count — a slow-to-resolve DNS lookup that takes 5 s per
        attempt and a fast WS-reset that's instant get the same
        amount of patience.

        Structured logging: ``event=openai.initial_connect.{...}`` so
        the boot-time funnel is greppable in journalctl alongside the
        other ``event=...`` lines the daemon emits.
        """
        budget_sec = self._initial_connect_budget_sec
        # Negative is meaningless; clamp to 0 ("single attempt").
        if budget_sec < 0:
            budget_sec = 0.0
        start = self._monotonic()
        deadline = start + budget_sec
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._open_session()
                if attempt > 1:
                    elapsed = self._monotonic() - start
                    log_event(
                        logger,
                        "openai.initial_connect.success",
                        phase=phase,
                        attempt=attempt,
                        elapsed_sec=f"{elapsed:.1f}",
                    )
                else:
                    log_event(
                        logger,
                        "openai.initial_connect.success",
                        phase=phase,
                        attempt=attempt,
                    )
                return
            except Exception as e:  # noqa: BLE001
                if not _is_transient(e):
                    log_event(
                        logger,
                        "openai.initial_connect.fatal",
                        phase=phase,
                        attempt=attempt,
                        exc=type(e).__name__,
                        reason=repr(str(e)[:200]),
                        level=logging.WARNING,
                    )
                    raise
                now = self._monotonic()
                elapsed = now - start
                if now >= deadline:
                    log_event(
                        logger,
                        "openai.initial_connect.exhausted",
                        phase=phase,
                        attempts=attempt,
                        elapsed_sec=f"{elapsed:.1f}",
                        budget_sec=f"{budget_sec:.1f}",
                        exc=type(e).__name__,
                        reason=repr(str(e)[:200]),
                        level=logging.ERROR,
                    )
                    raise RuntimeError(
                        f"openai connection: {phase} budget of "
                        f"{budget_sec:.0f}s exhausted after {attempt} "
                        f"attempt(s); last error: {e}"
                    )
                delay = reconnect_backoff_delay(attempt)
                # Don't oversleep past the deadline — if there's only
                # 2 s of budget left, sleeping 32 s would be pointless.
                # The clamp lets us still get one more retry near the
                # edge of the budget rather than burning the remaining
                # time on a sleep that already missed the deadline.
                remaining = deadline - now
                if delay > remaining:
                    delay = max(0.0, remaining)
                log_event(
                    logger,
                    "openai.initial_connect.attempt",
                    phase=phase,
                    attempt=attempt,
                    elapsed_sec=f"{elapsed:.1f}",
                    budget_sec=f"{budget_sec:.1f}",
                    exc=type(e).__name__,
                    reason=repr(str(e)[:200]),
                    level=logging.WARNING,
                )
                log_event(
                    logger,
                    "openai.initial_connect.backoff",
                    phase=phase,
                    attempt=attempt,
                    delay_sec=f"{delay:.2f}",
                    remaining_sec=f"{remaining:.1f}",
                    level=logging.WARNING,
                )
                await self._sleep(delay)

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
        except Exception:  # noqa: BLE001
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
        except Exception as e:  # noqa: BLE001
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
        self._deferred_reconnect.clear()
        self._receive_task = asyncio.create_task(self._receive_loop(conn))
        async with self._state_lock:
            self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()
        # Kick off the proactive pre-cap watchdog. No-op when either
        # `session_max_sec` or `proactive_buffer_sec` is 0 (disabled).
        self._start_proactive_watchdog()

    async def _teardown_session(self) -> None:
        t0 = _time.monotonic()
        # Cancel the proactive watchdog first — its only job is to fire on
        # a CONNECTED session, and we're about to leave that state.
        if self._proactive_watchdog_task is not None:
            self._proactive_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._proactive_watchdog_task
            self._proactive_watchdog_task = None
        self._deferred_reconnect.clear()
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
        # Close any in-flight billable-activity interval (time-billed
        # providers). Idle WebSocket lifetime is not counted.
        self._mark_billable_activity_ended()
        teardown_ms = (_time.monotonic() - t0) * 1000
        logger.info("openai connection: session torn down in %.0fms", teardown_ms)

    def _start_proactive_watchdog(self) -> None:
        """Schedule the proactive pre-cap reconnect for the just-opened
        session.

        OpenAI Realtime enforces a hard cap (60 min today, no resumption,
        no pre-cap warning event — verified against the realtime-
        conversations docs as of 2026-05). When the cap fires, the
        server sends a 1001 close and the supervisor reactively
        reconnects — that costs the user a ~3 s `cant_connect` cue. The
        watchdog avoids that by tearing the session down voluntarily a
        bit before the cap, during an idle window, so the next wake
        hits a fresh connection.

        Disabled when either knob is 0 — bare construction in tests
        doesn't spawn a surprise task."""
        if self._session_max_sec <= 0 or self._proactive_buffer_sec <= 0:
            return
        delay = self._session_max_sec - self._proactive_buffer_sec
        if delay <= 0:
            # Misconfiguration (buffer ≥ cap). Log loudly and skip — a
            # zero/negative delay would fire immediately on every
            # reconnect, which is a worse failure than just not doing
            # the proactive reconnect at all.
            logger.warning(
                "openai connection: proactive watchdog disabled — "
                "session_max_sec=%.0f ≤ proactive_buffer_sec=%.0f",
                self._session_max_sec, self._proactive_buffer_sec,
            )
            return
        self._proactive_watchdog_task = asyncio.create_task(
            self._proactive_reconnect_watchdog(delay),
            name="jasper-openai-proactive-watchdog",
        )

    async def _proactive_reconnect_watchdog(self, delay_sec: float) -> None:
        """Sleep until just before the cap, then trigger a reconnect.

        If a turn is in flight when the timer fires, set a pending flag
        and let `_on_turn_released` fire the reconnect once the turn
        ends. The 5-minute default buffer covers any realistic turn —
        the daemon's own idle watchdog ends turns within ~12 s — so the
        deferral always resolves well before the real cap.
        """
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
            # Already reconnecting / closing for another reason; the
            # current watchdog task is about to be cancelled by the
            # teardown path anyway.
            return
        if self._active_turn is not None:
            logger.info(
                "openai connection: proactive watchdog fired mid-turn — "
                "deferring reconnect until turn release "
                "(uptime≈%.0fs, buffer=%.0fs)",
                delay_sec, self._proactive_buffer_sec,
            )
            self._deferred_reconnect.request()
            return
        logger.info(
            "openai connection: proactive reconnect — preempting "
            "%.0f-min cap (firing at %.0fs uptime, %.0fs buffer)",
            self._session_max_sec / 60.0, delay_sec,
            self._proactive_buffer_sec,
        )
        self._reconnect_event.set()

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
        looks dict-like via ``getattr`` access works.

        A clean iteration exit (no exception) means the remote closed
        the WebSocket with a normal close code — typically 1001 "going
        away" when OpenAI Realtime hits its 60-minute hard cap. The
        ``websockets`` library treats 1000/1001 as the end of the
        stream and ends ``async for`` without raising, so the only
        signal we get for the cap is the iterator running out. Both
        the exception path AND the clean-exit path must wake the
        supervisor, otherwise the daemon sits on a dead session and
        every subsequent wake silently fails in ``send_audio``."""
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
            return
        if not self._stopping.is_set():
            logger.warning(
                "openai connection: receive iteration ended cleanly "
                "(server closed, likely the 60-minute hard cap); reconnecting",
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

        # Assistant audio transcript — the text version of the audio
        # the model is speaking. Production plays the audio, but we
        # also persist the transcript at turn release so operational
        # investigations can line up what it heard, what tool it used,
        # and what it actually said. The eval harness also consumes
        # these via the `text_out` trace event.
        if etype in (
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
            "response.output_text.delta",
        ):
            delta = _event_field(event, "delta")
            if isinstance(delta, str) and delta:
                if turn is not None:
                    turn._on_assistant_text_delta(delta)
                from .trace import emit as _trace_emit
                _trace_emit("text_out", {"delta": delta})
            return

        if etype in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
            "response.output_text.done",
        ):
            text = _event_field(event, "transcript")
            if not isinstance(text, str):
                text = _event_field(event, "text")
            if isinstance(text, str) and turn is not None:
                turn._on_assistant_text_done(text)
            return

        # Track the assistant audio item id — truncate_assistant_audio's
        # conversation.item.truncate target on a barge-in.
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

        # User audio transcription (what the STT model heard the user
        # say). Diagnostic only — the realtime model's tool choice
        # comes from the raw audio, not this transcript. Keep transcript
        # content out of logging entirely: the flight recorder buffers
        # DEBUG records and dumps them to journald around failures.
        # See the comment block next to ``transcription`` in
        # ``_session_config`` for the full rationale.
        if etype == "conversation.item.input_audio_transcription.completed":
            transcript = _event_field(event, "transcript")
            if isinstance(transcript, str):
                text = transcript.strip()
                if turn is not None:
                    turn._on_user_text_done(text)
                log_event(
                    logger,
                    "openai.user_transcript",
                    chars=len(text),
                    level=logging.DEBUG,
                )
            return
        if etype == "conversation.item.input_audio_transcription.failed":
            err = _event_field(event, "error") or {}
            log_event(
                logger,
                "openai.user_transcription_failed",
                error=str(err.get("message") if isinstance(err, dict) else err),
                level=logging.WARNING,
            )
            return

        # Server-side response complete.
        if etype == "response.done":
            await self._handle_response_done(event, turn)
            return

        if etype == "input_audio_buffer.speech_started":
            if self._server_vad_active and turn is not None:
                turn._on_speech_started()
            else:
                logger.debug("openai connection: VAD event %s (no server_vad turn)", etype)
            return

        if etype == "input_audio_buffer.speech_stopped":
            if self._server_vad_active and turn is not None:
                turn._on_speech_stopped()
            else:
                logger.debug("openai connection: VAD event %s (no server_vad turn)", etype)
            return

        if etype == "input_audio_buffer.committed":
            if self._server_vad_active and turn is not None:
                turn._on_server_committed()
            else:
                logger.debug("openai connection: event %s", etype)
            return

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
            await self._open_session_with_retry(phase="context-reset-reopen")
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
            #
            # Reset the pre-response idle anchor — without this, the
            # watchdog fires mid-dispatch at small
            # JASPER_IDLE_TIMEOUT_SEC values (production 2026-05-21,
            # IDLE_TIMEOUT_SEC=10, weather query).
            turn._note_activity()
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

        # Grok inherits this dispatch path via
        # GrokRealtimeConnection(OpenAIRealtimeConnection); `dispatch_tool`
        # owns the per-tool timeout, scalar-wrapping, {"error": …} shapes,
        # and timing logs uniformly across providers.
        t0 = _time.monotonic()
        payload = await dispatch_tool(self._registry, name, args)

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


def _read_initial_connect_budget_env() -> float:
    """Read ``JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC`` from the
    environment, falling back to ``DEFAULT_INITIAL_CONNECT_BUDGET_SEC``.

    Garbage values (non-numeric strings, negative numbers) log a
    warning and fall back to the default — better the daemon boots
    with the documented behaviour than refuses to start over a typo
    in jasper.env."""
    raw = os.environ.get("JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC")
    if raw is None or raw == "":
        return DEFAULT_INITIAL_CONNECT_BUDGET_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC=%r is not a number; "
            "falling back to default %.0fs",
            raw, DEFAULT_INITIAL_CONNECT_BUDGET_SEC,
        )
        return DEFAULT_INITIAL_CONNECT_BUDGET_SEC
    if value < 0:
        logger.warning(
            "JASPER_OPENAI_INITIAL_CONNECT_BUDGET_SEC=%s is negative; "
            "falling back to default %.0fs",
            raw, DEFAULT_INITIAL_CONNECT_BUDGET_SEC,
        )
        return DEFAULT_INITIAL_CONNECT_BUDGET_SEC
    return value


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
