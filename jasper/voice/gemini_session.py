from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from google import genai
from google.genai import types

from ..tools import ToolRegistry
from .session import VoiceSession

logger = logging.getLogger(__name__)


class GeminiLiveSession(VoiceSession):
    """Gemini 3.1 Flash Live adapter.

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
            # interrupt the model mid-turn. Necessary because we have
            # no working bleed-vs-real-speech distinguisher in
            # software — Silero VAD treats TTS bleed as "speech"
            # (which it is — TTS is by design speech-shaped), so the
            # server-side VAD AND any local VAD will both fire on the
            # model's own bleed-through. With NO_INTERRUPTION the
            # server ignores user activity until turn_complete, so the
            # model always finishes its sentence. Trade-off: real
            # barge-in is disabled. Fix path is hardware AEC — the
            # XVF3800 USB-IN as AEC reference, requires CamillaDSP-
            # routed playback architecture (TODO: future work).
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
        import time as _time
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
