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
import audioop
import base64
import contextlib
import json
import logging
import os
import time as _time
from typing import TYPE_CHECKING

from jasper.log_event import log_event

if TYPE_CHECKING:
    import wave

from ..tools import dispatch_tool
from ._base import BaseLiveConnection, BaseLiveTurn
from ._supervisor import (
    await_connected, failure_detail, request_planned_reopen, request_unplanned_reopen,
)
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

# Bound a provider that opens the socket but never accepts session.update.
SESSION_SETUP_TIMEOUT_SEC = 15.0

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

# Inbound event types that prove the turn is making progress, so they
# advance the pre-response idle anchor: the whole `response.*` namespace
# (created, in_progress, output items, audio and text deltas, done), the
# input transcription under `conversation.item.*`, and the commit
# acknowledgement. Excluded on purpose — `error`, `session.*` and
# `rate_limits.updated` prove only that the socket is open. See #4532.
_PROGRESS_EVENT_PREFIXES = ("response.", "conversation.item.")
_PROGRESS_EVENT_TYPES = frozenset(("input_audio_buffer.committed",))


def _is_progress_event(etype: str) -> bool:
    return etype in _PROGRESS_EVENT_TYPES or etype.startswith(_PROGRESS_EVENT_PREFIXES)


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
        self._conn: OpenAIRealtimeConnection = conn
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
        self._session = getattr(conn, "_conn", None)
        self._response_id: str | None = None
        self._response_item_ids: set[str] = set()
        self._input_item_id: str | None = None
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
        self._debug_wav: wave.Wave_write | None = None
        self._debug_wav_path: str | None = None
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
            await self._conn._send_event({
                "type": "conversation.item.create",
                "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }, turn=self)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "openai turn: send_text_context failed (%s: %s); turn lost",
                type(e).__name__, e,
            )
            self._turn_lost = True
            await self._audio_q.put(None)

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
        self._cancel_tools()
        elapsed_ms = (_time.monotonic() - self._started_at_monotonic) * 1000
        self.drop_pending_audio()
        self._audio_q.put_nowait(None)
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
        if self._released or self._turn_lost or not self._committed or self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_tools()
        log_event(logger, "barge.cancel", reason=reason)
        if not self._server_turn_complete:
            if self._tool_round_pending:
                await self._on_response_done(None)
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
            }, turn=self)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger, "barge.truncate_failed",
                item_id=item_id, error=type(e).__name__,
                detail=failure_detail(e, literals=self._conn._secret_literals()),
                level=logging.WARNING,
            )

    # ---- Internal — called by the connection's receive loop ----

    async def _on_audio_delta(self, b64_audio: str, item_id: str | None = None) -> None:
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
        self._cancel_tools()
        self._note_activity()
        self._server_turn_complete = True
        self._record_usage(usage)
        self._audio_q.put_nowait(None)

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

        # Manual VAD allows one outstanding commit and response.create.
        # Release reopens unresolved requests instead of rebinding their acks.
        self._pending_commit: OpenAIRealtimeTurn | None = None
        self._pending_response: OpenAIRealtimeTurn | None = None

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
            async with self._state_lock:
                if self._state is ConnectionState.CONNECTED:
                    self._set_state(ConnectionState.IN_TURN)
            logger.info("openai turn: started")
            return turn

    # ------------------------------------------------------------------
    # Internal — turn-side helpers
    # ------------------------------------------------------------------

    def _owns_turn(self, turn: OpenAIRealtimeTurn) -> bool:
        return (
            self._active_turn is turn and not turn._released and not turn._turn_lost
            and self._conn is not None and turn._session is self._conn
            and self._connected_event.is_set()
        )

    def _can_respond(self, turn: OpenAIRealtimeTurn) -> bool:
        return self._owns_turn(turn) and not turn._cancel_requested and not turn._server_turn_complete

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
            if self._conn is None:
                raise RuntimeError(f"{self._log_tag} no active session")
            await self._conn.send(event)
            return True

    async def _send_audio_chunk(
        self, turn: OpenAIRealtimeTurn, pcm_16khz: bytes,
    ) -> bool:
        # Polyphase 16 → 24 kHz upsample. State persists per-turn.
        pcm_24khz, turn._resample_state = _upsample_16k_to_24k(
            pcm_16khz, turn._resample_state,
        )
        if not pcm_24khz:
            return False
        # Debug tee — see OpenAIRealtimeTurn._debug_wav docstring.
        if os.environ.get("JASPER_DEBUG_RECORD_OPENAI_AUDIO", "").strip() in ("1", "true", "yes", "on"):
            try:
                if turn._debug_wav is None:
                    import wave as _wave
                    debug_dir = os.environ.get(
                        "JASPER_DEBUG_OPENAI_AUDIO_DIR",
                        "/tmp/jasper-openai-debug",
                    )
                    os.makedirs(debug_dir, exist_ok=True)
                    ts = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
                    path = f"{debug_dir}/{ts}-{id(turn):x}.wav"
                    w = _wave.open(path, "wb")
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(OPENAI_AUDIO_RATE_HZ)
                    turn._debug_wav = w
                    turn._debug_wav_path = path
                    logger.info("debug: recording OpenAI send audio → %s", path)
                assert turn._debug_wav is not None
                turn._debug_wav.writeframes(pcm_24khz)
            except Exception as e:  # noqa: BLE001
                logger.warning("debug record failed (will skip rest of turn): %s", e)
                turn._debug_wav = None
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
            logger.debug("%s cancel ignored (%s)", self._log_tag, type(e).__name__)

    async def _on_turn_released(self, turn: OpenAIRealtimeTurn) -> None:
        if self._active_turn is not turn:
            return
        async with self._send_lock:
            session = self._conn
            if session is not None and turn._session is session:
                try:
                    if turn._response_id or self._pending_response is turn:
                        await session.send({"type": "response.cancel"})
                    await session.send({"type": "input_audio_buffer.clear"})
                except Exception as e:  # noqa: BLE001
                    if self._conn is session:
                        self._connected_event.clear()
                        request_unplanned_reopen(self)
                    logger.warning("%s release failed (%s)", self._log_tag, type(e).__name__)
                # An abandoned response can still add tool calls to history.
                # A fresh session removes them without publishing stale results.
                else:
                    unresolved = (
                        turn._committed and (not turn._server_turn_complete or turn._tool_round_pending)
                        or self._pending_commit is turn or self._pending_response is turn
                    )
                    if self._conn is session and unresolved:
                        request_planned_reopen(self)
        if self._active_turn is turn:
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
            events = aiter(conn)
            async with asyncio.timeout(SESSION_SETUP_TIMEOUT_SEC):
                async for event in events:
                    etype = _event_type(event)
                    if etype == "session.updated":
                        break
                    if etype == "error":
                        error = _event_field(event, "error")
                        invalid = (
                            _event_field(error, "type") == "invalid_request_error"
                            and _event_field(error, "code") != "rate_limit_exceeded"
                        )
                        error_cls = ValueError if invalid else RuntimeError
                        raise error_cls(failure_detail(
                            error_cls(error), literals=self._secret_literals(),
                        ))
                else:
                    raise ConnectionError("session closed before setup acknowledgement")
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, TimeoutError):
                e.args = ("session setup acknowledgement timed out",)
            logger.warning(
                f"{self._log_tag} session.update failed (%s: %s); "
                "closing and re-raising for supervisor retry",
                type(e).__name__, failure_detail(e, literals=self._secret_literals()),
            )
            await self._close_with_timeout(conn)
            await self._close_cm_with_timeout(cm)
            self._conn = None
            self._conn_cm = None
            raise
        self._deferred_reconnect.clear()
        await self._mark_connected(asyncio.create_task(self._receive_loop(events, conn)))

    async def _teardown_session(self) -> None:
        t0 = _time.monotonic()
        conn, cm = self._conn, self._conn_cm
        if self._active_turn is not None:
            self._active_turn._cancel_tools()
        self._conn = self._conn_cm = None
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

    async def _receive_loop(self, events, conn) -> None:
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
            async for event in events:
                if self._conn is not conn:
                    return
                etype = _event_type(event)
                if etype is None:
                    continue
                await self._dispatch_event(etype, event)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if self._conn is conn:
                self._on_receive_loop_error(e)
            return
        if self._conn is conn and not self._stopping.is_set():
            logger.warning(
                f"{self._log_tag} receive iteration ended cleanly "
                "(server closed, likely the 60-minute hard cap); reconnecting",
            )
            request_unplanned_reopen(self)

    async def _dispatch_event(self, etype: str, event) -> None:
        turn = self._active_turn
        if turn is not None and self._owns_turn(turn) and _is_progress_event(etype):
            turn._note_activity()
        if etype == "error":
            detail = failure_detail(
                RuntimeError(str(_event_field(event, "error"))), literals=self._secret_literals(),
            )
            logger.warning("%s server error: %s", self._log_tag, detail)
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
                    log_event(logger, "openai.user_transcript", chars=len(text), level=logging.DEBUG)
            elif etype.endswith(".failed"):
                log_event(logger, "openai.user_transcription_failed", level=logging.WARNING)
            return

        response = _event_field(event, "response")
        response_id = _event_field(response, "id") if response is not None else _event_field(event, "response_id")
        if not turn._response_id or response_id != turn._response_id:
            if etype == "response.done":
                log_event(logger, "voice.stale_response", provider=self.PROVIDER_NAME, level=logging.DEBUG)
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
                from .trace import emit as _trace_emit  # lazy: optional evaluation trace
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
        function_calls = _extract_function_calls(response)
        turn._tool_round_pending = bool(function_calls)
        log_event(
            logger, "voice.response_done", provider=self.PROVIDER_NAME,
            response_id=_event_field(response, "id"), status=status,
            function_calls=len(function_calls),
            input_tokens=(usage or {}).get("input_tokens", 0),
            output_tokens=(usage or {}).get("output_tokens", 0),
        )
        if status != "completed":
            if status == "cancelled" and turn._cancel_requested:
                await turn._on_response_done(None)
            else:
                turn._on_connection_lost()
            return
        if not function_calls or turn._cancel_requested:
            await turn._on_response_done(None)
            return
        turn._start_tool_round(lambda: self._run_tool_round(function_calls, turn))

    async def _run_tool_round(self, function_calls: list, turn: OpenAIRealtimeTurn) -> None:
        for fc in function_calls:
            if not self._can_respond(turn):
                return
            if not await self._dispatch_function_call(fc, turn):
                return
        if self._can_respond(turn):
            turn._tool_round_pending = False
            try:
                await self._send_event({"type": "response.create"}, turn=turn)
            except Exception:  # noqa: BLE001
                turn._on_connection_lost()

    async def _dispatch_function_call(self, fc, turn: OpenAIRealtimeTurn) -> bool:
        """Dispatch one call and send its output; `_run_tool_round` requests the next response."""
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
                "openai tool %s: bad JSON arguments; treating as empty", name,
            )

        # Grok inherits this dispatch path via
        # GrokRealtimeConnection(OpenAIRealtimeConnection); `dispatch_tool`
        # owns the per-tool timeout, scalar-wrapping, {"error": …} shapes,
        # and timing logs uniformly across providers.
        t0 = _time.monotonic()
        payload = await dispatch_tool(self._registry, name, args)

        if self._can_respond(turn) and call_id:
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
                sent = await self._send_event({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }, turn=turn)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "tool %s: could not send function_call_output (%s: %s); "
                    "next turn may be confused",
                    name, type(e).__name__, e,
                )
                turn._on_connection_lost()
                return False
            if not sent:
                return False
            send_ms = (_time.monotonic() - t_send) * 1000
            total_ms = (_time.monotonic() - t0) * 1000
            logger.info(
                "tool result item sent to OpenAI in %.0fms (total dispatch %.0fms)",
                send_ms, total_ms,
            )
            return sent
        return False


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
