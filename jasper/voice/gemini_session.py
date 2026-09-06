# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time as _time

from google import genai
from google.genai import types

from ..tools import dispatch_tool
from ._base import BaseLiveConnection, BaseLiveTurn
from ._supervisor import (
    await_connected,
    failure_detail,
    http_status,
    request_unplanned_reopen,
)
from .session import (
    AudioOutChunk,
    ConnectionState,
    LiveTurn,
    log_first_chunk,
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


class GeminiLiveTurn(BaseLiveTurn):
    """A single turn against an open `GeminiLiveConnection`.

    Adds Gemini's cumulative-counter usage arithmetic and its tool-call
    metadata capture to ``BaseLiveTurn``. The connection's receive loop
    routes incoming server messages here while a turn is active. After
    `release()`, the connection's `_active_turn` slot is cleared and the
    next `acquire_turn()` returns a fresh turn.
    """

    def __init__(
        self,
        conn: "GeminiLiveConnection",
        started_at: float,
        usage_baseline: dict[str, int] | None = None,
    ) -> None:
        super().__init__(conn, started_at)
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
        self._activity_end_sent = False
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
        self._end_input_at_monotonic = _time.monotonic()
        try:
            await self._conn._send_activity_end()
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "live turn: end_input ignored (%s: %s)",
                type(e).__name__, failure_detail(e),
            )
            self._turn_lost = True
            await self._audio_q.put(None)

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
                log_first_chunk(
                    logger,
                    "gemini",
                    turn_start_monotonic=self._started_at_monotonic,
                    end_input_monotonic=self._end_input_at_monotonic,
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

        # Per-turn diagnostic line. Both this turn's delta (what gets
        # billed to the usage row) and the cumulative counter (for
        # debugging the delta math).
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

    def _record_tool_call_name(self, name: str | None) -> None:
        cleaned = str(name or "").strip()
        if cleaned:
            self._tool_call_names.append(cleaned)


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
    # Prefix this module's own log lines already carry verbatim; the
    # shared supervisor reads it from here.
    _log_tag = "live connection:"
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
        self._client = genai.Client(api_key=api_key) if connect_factory is None else None
        self._connect_factory = connect_factory
        self._rotate_after_sec = rotate_after_sec

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

    # ------------------------------------------------------------------
    # LiveConnection protocol
    # ------------------------------------------------------------------

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
        # Cancel the rotation watchdog first — it only makes sense against
        # a live session, and we are about to drop this one.
        await self._cancel_task(self._proactive_watchdog_task)
        self._proactive_watchdog_task = None
        await self._cancel_task(self._receive_task)
        self._receive_task = None
        # Only now can the handle be dropped for good: until the receive
        # task above was cancelled it could still land a late
        # `session_resumption_update` and resurrect the old context.
        if self._drop_resumption_on_teardown:
            self._drop_resumption_on_teardown = False
            self._resumption_handle = None
        await self._close_with_timeout(self._session)
        await self._close_cm_with_timeout(self._session_cm)
        self._session_cm = None
        self._session = None
        self._connected_event.clear()
        self._log_teardown(_time.monotonic() - t0)

    def _on_reconnect_attempt_failed(
        self, exc: Exception, attempt: int, transient: bool,
    ) -> None:
        super()._on_reconnect_attempt_failed(exc, attempt, transient)
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
                    request_unplanned_reopen(self)
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
                    request_unplanned_reopen(self)
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
            self._on_receive_loop_error(e)

    def _on_context_reset(self) -> None:
        # Dropped in `_teardown_session`, not here: the old session's
        # receive loop runs until the supervisor cancels it and would
        # otherwise re-cache a handle for the context being discarded.
        self._drop_resumption_on_teardown = True

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
