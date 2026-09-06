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
  Registry produces flat OpenAI tool schemas via
  ``registry.openai_tools()``. The model emits
  ``response.function_call_arguments.done`` with the arguments as a
  single JSON string; we ``json.loads`` it, dispatch the registered
  callable, and reply with ``conversation.item.create`` of type
  ``function_call_output`` plus a fresh ``response.create()``.

Session lifecycle
  60-minute hard cap, no resumption mechanism. When the cap or any drop
  is hit, the supervisor reconnects the same way as for any other drop.
  Lost conversational context is acceptable — the daemon already biases
  toward fresh sessions via the opt-in idle context-reset, which here is
  just a reopen since there is no handle to drop.
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

from jasper.log_event import log_event

from ..tools import dispatch_tool
from ._base import BaseLiveConnection, BaseLiveTurn
from ._supervisor import await_connected, failure_detail, request_unplanned_reopen
from .session import (
    AudioOutChunk,
    ConnectionState,
    LiveTurn,
    TurnCapture,
    TurnUsage,
    log_first_chunk,
)

logger = logging.getLogger(__name__)


# Wire-format constants. The OpenAI Realtime ``audio/pcm`` discriminator
# accepts only 24 kHz (verified against ``RealtimeAudioFormats.AudioPCM``
# in openai-python's typed API). The XVF3800 captures at 16 kHz mono;
# we polyphase-upsample 16 → 24 inside the turn before base64-encoding.
OPENAI_AUDIO_RATE_HZ = 24000
DAEMON_MIC_RATE_HZ = 16000

# Default reasoning effort for ``gpt-realtime-2``. Smart-speaker queries
# are short and concrete; we don't need ``medium`` / ``high`` reasoning
# (which trade ~1+ extra second of TTFA for marginally smarter answers
# the user won't notice). ``low`` is the SDK default; ``minimal`` is
# ~1.1 s TTFA at the cost of less coherent multi-step answers. Override
# via ``JASPER_OPENAI_REASONING_EFFORT`` if needed.
DEFAULT_REASONING_EFFORT = "low"

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


class OpenAIRealtimeTurn(BaseLiveTurn):
    """A single turn against an open ``OpenAIRealtimeConnection``.

    Adds the resampler state, OpenAI's modality-aware usage accumulator
    and its transcript/barge-in wire state to ``BaseLiveTurn``. The
    connection's receive loop routes incoming server events here while a
    turn is active.
    """

    def __init__(self, conn: "OpenAIRealtimeConnection", started_at: float) -> None:
        super().__init__(conn, started_at)
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
        # Tracks chunk-size distribution per turn; logged at release so a uniform vs. front-loaded delivery is visible post hoc.
        self._chunk_bytes_total: int = 0
        self._chunk_bytes_max: int = 0
        self._first_chunk_bytes: int = 0
        # Whether `commit()` + `response.create()` has been sent; makes
        # `end_input` idempotent.
        self._committed = False
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
        try:
            await self._conn._send_text_context(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "openai turn: send_text_context failed (%s: %s); turn lost",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

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

        The server stops listening for more user audio and starts
        generating. Idempotent."""
        if self._committed or self._released or self._turn_lost:
            return
        self._committed = True
        self._end_input_at_monotonic = _time.monotonic()
        try:
            await self._conn._commit_and_create_response(self)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "openai turn: end_input ignored (%s: %s)",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

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

    def usage(self) -> TurnUsage:
        return TurnUsage(
            input_tokens=int(self._usage.get("input_tokens", 0)),
            output_tokens=int(self._usage.get("output_tokens", 0)),
            # Copied out of the accumulator so a caller can't mutate the
            # turn's internal state through the returned reference.
            breakdown={
                "input_tokens": self._usage_breakdown["input_tokens"],
                "output_tokens": self._usage_breakdown["output_tokens"],
                "input_token_details": dict(
                    self._usage_breakdown["input_token_details"],
                ),
                "output_token_details": dict(
                    self._usage_breakdown["output_token_details"],
                ),
            },
        )

    def capture(self) -> TurnCapture | None:
        user = self.user_transcript().strip() or None
        assistant = self.assistant_transcript().strip() or None
        if user is None and assistant is None:
            return None
        return TurnCapture(user_text=user, assistant_text=assistant)

    def assistant_transcript(self) -> str:
        return "".join(self._assistant_transcript_parts)

    def user_transcript(self) -> str:
        return " ".join(self._user_transcript_parts)

    # ---- Interruptible (OpenAI reference pack) ----
    # `response.cancel` then `conversation.item.truncate`, in that order,
    # from the flush's playout-ledger accounting. See ADR-0115 and
    # ``session.Interruptible``. Grok inherits this pack via
    # ``GrokRealtimeConnection``.

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
                item_id=item_id, error=type(e).__name__,
                detail=failure_detail(e, literals=self._conn._secret_literals()),
                level=logging.WARNING,
            )

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
            log_first_chunk(
                logger,
                getattr(self._conn, "PROVIDER_NAME", "openai"),
                turn_start_monotonic=self._started_at_monotonic,
                end_input_monotonic=self._end_input_at_monotonic,
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
        ``usage().breakdown`` returns the full split for cost
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


# ---------- Long-lived connection ------------------------------------------


class OpenAIRealtimeConnection(BaseLiveConnection):
    """Long-lived OpenAI Realtime connection.

    One instance per daemon. Holds the SDK client, the active WebSocket
    session, and the wire half of the lifecycle ``BaseLiveConnection``
    drives.
    """

    PROVIDER_NAME = "openai"
    _logger = logger
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
        noise_reduction: str = DEFAULT_NOISE_REDUCTION,
        # Proactive pre-cap reconnect — see `_watchdog_delay_sec`.
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
            backoff_schedule=backoff_schedule,
            sleep=sleep,
            nudge_clock=clock,
        )
        self._api_key = api_key
        self._reasoning_effort = reasoning_effort
        self._noise_reduction = _normalize_noise_reduction(noise_reduction)
        self._session_max_sec = session_max_sec
        self._proactive_buffer_sec = proactive_buffer_sec
        self._connect_factory = connect_factory
        self._base_url = base_url
        self._log_tag = f"{self.PROVIDER_NAME} connection:"
        # Lazy SDK client — only built when ``connect_factory`` is None.
        # We do this lazily so test setups can construct the connection
        # object without the openai package installed.
        self._client = None

        # SDK connection + context manager (cleared during reconnect).
        self._conn = None
        self._conn_cm = None
        self._send_lock = asyncio.Lock()

        # Count of `response.output_audio.delta` events that arrived
        # while `_active_turn is None` (server response that landed
        # AFTER the daemon's idle watchdog already released the turn).
        # Logging each delta would be 50-200 lines per orphan response;
        # we accumulate here and surface the total in the matching
        # `response.done` warning, then reset.
        self._orphan_delta_count: int = 0

        # Optional billable-activity meter (time-billed providers, e.g.
        # Grok). Wired by the daemon before start() when the active
        # provider bills realtime activity; None for token-billed providers.
        # See jasper.usage.BillableActivityMeter.
        self._billable_activity_meter = None
        self._billable_activity_interval_open: bool = False

    def _secret_literals(self) -> tuple[str, ...]:
        """The API key, so a rejection body that echoes it still redacts.

        `_KEY_PREFIX_RE` in `secret_redaction.py` only knows the `sk-`
        shape; a rotated or legacy OpenAI key can miss it, and this is
        the fallback. `GrokRealtimeConnection` inherits this unchanged —
        its `xai-` keys are already prefix-covered, but the exact value
        still redacts either way.
        """
        return (self._api_key,) if self._api_key else ()

    # ------------------------------------------------------------------
    # Public LiveConnection protocol
    # ------------------------------------------------------------------

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
            raise RuntimeError(f"{self._log_tag} no active session")
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

    async def _cancel_response(self) -> None:
        # Best-effort: tell the server to stop generating. Idempotent on
        # the server side — extra cancels for a non-existent response
        # are silently ignored.
        if self._conn is None:
            return
        try:
            await self._send_event({"type": "response.cancel"})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self._log_tag} cancel ignored (%s)", e)

    async def _on_turn_released(self, turn: OpenAIRealtimeTurn) -> None:
        self._mark_billable_activity_ended()
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
        self._conn_cm = cm
        self._conn = conn
        connect_ms = (_time.monotonic() - t0) * 1000
        logger.info(
            f"{self._log_tag} connect ok in %.0fms (model=%s)",
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
                f"{self._log_tag} session.update failed (%s: %s); "
                "closing and re-raising for supervisor retry",
                type(e).__name__, failure_detail(e, literals=self._secret_literals()),
            )
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            self._conn = None
            self._conn_cm = None
            raise
        self._deferred_reconnect.clear()
        await self._mark_connected(asyncio.create_task(self._receive_loop(conn)))

    async def _teardown_session(self) -> None:
        t0 = _time.monotonic()
        # Cancel the proactive watchdog first — its only job is to fire on
        # a CONNECTED session, and we're about to leave that state.
        await self._cancel_task(self._proactive_watchdog_task)
        self._proactive_watchdog_task = None
        self._deferred_reconnect.clear()
        await self._cancel_task(self._receive_task)
        self._receive_task = None
        await self._close_with_timeout(self._conn)
        await self._close_cm_with_timeout(self._conn_cm)
        self._conn_cm = None
        self._conn = None
        self._connected_event.clear()
        # Close any in-flight billable-activity interval (time-billed
        # providers). Idle WebSocket lifetime is not counted.
        self._mark_billable_activity_ended()
        self._log_teardown(_time.monotonic() - t0)

    def _watchdog_delay_sec(self) -> float:
        """How long into a session to pre-empt OpenAI's hard cap.

        The cap is 60 min today, with no resumption and no pre-cap
        warning event (verified against the realtime-conversations docs
        as of 2026-05). When it fires the server sends a 1001 close and
        the supervisor reconnects reactively, costing the user a ~3 s
        `cant_connect` cue; firing a buffer ahead of it, in an idle
        window, means the next wake hits a fresh connection instead.
        Disabled when either knob is 0."""
        if self._session_max_sec <= 0 or self._proactive_buffer_sec <= 0:
            return 0.0
        delay = self._session_max_sec - self._proactive_buffer_sec
        if delay <= 0:
            # Misconfiguration (buffer ≥ cap). Log loudly and skip — a
            # zero/negative delay would fire immediately on every
            # reconnect, which is a worse failure than just not doing
            # the proactive reconnect at all.
            logger.warning(
                f"{self._log_tag} proactive watchdog disabled — "
                "session_max_sec=%.0f ≤ proactive_buffer_sec=%.0f",
                self._session_max_sec, self._proactive_buffer_sec,
            )
            return 0.0
        return delay

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
            self._on_receive_loop_error(e)
            return
        if not self._stopping.is_set():
            logger.warning(
                f"{self._log_tag} receive iteration ended cleanly "
                "(server closed, likely the 60-minute hard cap); reconnecting",
            )
            request_unplanned_reopen(self)

    async def _dispatch_event(self, etype: str, event) -> None:
        turn = self._active_turn

        if etype == "error":
            err = _event_field(event, "error") or {}
            logger.warning(f"{self._log_tag} server error: %s", err)
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

        logger.debug(f"{self._log_tag} event %s", etype)

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
            # JASPER_IDLE_TIMEOUT_SEC values.
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
                    f"{self._log_tag} response.create after tool round "
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
        produce overlapping response.creates which the server rejects)."""
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
            # Serialize + wire-send guarded like the sibling sends
            # (send_audio, end_input, …). A tool returning a payload that
            # is not JSON-serializable would otherwise raise out of this
            # unguarded send, propagate to _receive_loop's broad except,
            # and force a full session reconnect. Contain it to this one
            # tool: emit a synthetic error output so the server still sees
            # a function_call_output for the call_id, and let the turn's
            # single response.create proceed.
            try:
                output = json.dumps(payload)
            except (TypeError, ValueError) as e:
                logger.warning(
                    "tool %s: result not JSON-serializable (%s: %s); "
                    "sending error output instead of reconnecting",
                    name, type(e).__name__, e,
                )
                output = json.dumps(
                    {"error": f"tool result not serializable: {type(e).__name__}"}
                )
            try:
                await self._send_event({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                })
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "tool %s: could not send function_call_output (%s: %s); "
                    "next turn may be confused",
                    name, type(e).__name__, e,
                )
                return
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
