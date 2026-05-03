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
    mono. Server VAD detects end-of-turn from the audio stream — we don't
    need to send an explicit audio_stream_end. Tool calls arrive on
    response.tool_call; we dispatch to the registered callable and reply
    with send_tool_response.

    NOTE: google-genai Live is still Preview. Surface fields like
    usage_metadata and server_content.turn_complete are guarded so an SDK
    bump that renames them degrades to a warning rather than a crash.
    """

    INPUT_MIME = "audio/pcm;rate=16000"

    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._registry: ToolRegistry | None = None
        self._session = None
        self._session_cm = None
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._receive_task: asyncio.Task | None = None
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self._turn_complete = False

    async def connect(self, registry: ToolRegistry, system_instruction: str) -> None:
        self._registry = registry
        config: dict = {"response_modalities": ["AUDIO"]}
        if system_instruction:
            config["system_instruction"] = system_instruction
        decls = registry.function_declarations()
        if decls:
            config["tools"] = [{"function_declarations": decls}]

        self._session_cm = self._client.aio.live.connect(
            model=self._model, config=config
        )
        self._session = await self._session_cm.__aenter__()
        self._turn_complete = False
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        if self._session is None:
            return
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm_16khz_int16, mime_type=self.INPUT_MIME)
        )

    async def end_input(self) -> None:
        # Server VAD handles end-of-turn; nothing to do client-side. Kept on
        # the interface so an OpenAI adapter (which DOES need an explicit
        # commit) can override.
        return None

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
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.debug("session close error (ignored): %s", e)
            self._session_cm = None
            self._session = None
        await self._audio_q.put(None)

    def usage_tokens(self) -> dict[str, int]:
        return dict(self._usage)

    def turn_complete(self) -> bool:
        return self._turn_complete

    async def _receive_loop(self) -> None:
        assert self._session is not None
        try:
            async for response in self._session.receive():
                await self._dispatch(response)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("gemini receive loop error: %s", e)
        finally:
            await self._audio_q.put(None)

    async def _dispatch(self, response) -> None:
        # Audio frames live on response.data (raw 24 kHz int16 PCM).
        data = getattr(response, "data", None)
        if data:
            await self._audio_q.put(data)

        # Tool calls.
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            await self._handle_tool_call(tool_call)

        # Server content: text transcripts, turn_complete signal.
        sc = getattr(response, "server_content", None)
        if sc is not None and getattr(sc, "turn_complete", False):
            self._turn_complete = True

        # Usage metadata: present on some responses, fields named with
        # token_count suffixes. Guarded since SDK is still Preview.
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            in_tok = getattr(usage, "prompt_token_count", None)
            out_tok = getattr(usage, "response_token_count", None)
            if in_tok is not None:
                self._usage["input_tokens"] = int(in_tok)
            if out_tok is not None:
                self._usage["output_tokens"] = int(out_tok)

    async def _handle_tool_call(self, tool_call) -> None:
        assert self._registry is not None
        responses = []
        for fc in tool_call.function_calls:
            tool = self._registry.get(fc.name)
            args = dict(fc.args or {})
            if tool is None:
                result = {"error": f"unknown tool {fc.name}"}
            else:
                try:
                    out = tool.fn(**args)
                    if asyncio.iscoroutine(out):
                        out = await asyncio.wait_for(out, timeout=5.0)
                    result = out if isinstance(out, dict) else {"result": out}
                except asyncio.TimeoutError:
                    result = {"error": f"{fc.name} timed out"}
                except Exception as e:  # noqa: BLE001
                    result = {"error": str(e)}
            responses.append(
                types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"result": result}
                )
            )
        if self._session is not None:
            await self._session.send_tool_response(function_responses=responses)
