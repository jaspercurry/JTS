# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""xAI wire format over the shared OpenAI Realtime lifecycle.

Session fields follow https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech.
The adapter retains the configured model; it does not track xAI's latest alias.
"""
from __future__ import annotations

import logging

from .openai_session import OpenAIRealtimeConnection

logger = logging.getLogger(__name__)


# Per xAI docs: clients connect to wss://api.x.ai/v1/realtime, which is
# the OpenAI-compatible endpoint. The openai-python SDK accepts a
# ``websocket_base_url`` kwarg on AsyncOpenAI; passing the xAI host
# routes the WebSocket without changing any wire-format code.
GROK_WEBSOCKET_BASE_URL = "wss://api.x.ai/v1"


class GrokRealtimeConnection(OpenAIRealtimeConnection):
    PROVIDER_NAME = "grok"

    def __init__(
        self,
        api_key: str,
        model: str = "grok-voice-think-fast-1.0",
        voice: str = "eve",
        context_reset_sec: float = 0.0,
        # xAI doesn't publish a hard session cap analogous to OpenAI's
        # 60-min one, so the proactive watchdog defaults to disabled.
        # Pass both knobs through from Config to enable if a cap is
        # observed empirically.
        session_max_sec: float = 0.0,
        proactive_buffer_sec: float = 0.0,
        backoff_schedule: tuple[float, ...] | None = None,
        connect_factory=None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            voice=voice,
            context_reset_sec=context_reset_sec,
            # `reasoning_effort` accepts a string but the parent's
            # `_build_session_payload` only emits it when the model
            # name contains "-2" — Grok models don't, so the field is
            # naturally skipped without a separate override.
            reasoning_effort="",
            session_max_sec=session_max_sec,
            proactive_buffer_sec=proactive_buffer_sec,
            backoff_schedule=backoff_schedule,
            connect_factory=connect_factory,
            base_url=base_url or GROK_WEBSOCKET_BASE_URL,
        )

    def _build_session_payload(self) -> dict:
        payload = super()._build_session_payload()
        return {
            "instructions": payload["instructions"],
            "voice": self._voice,
            "turn_detection": {"type": None},
            "audio": {
                direction: {"format": payload["audio"][direction]["format"]}
                for direction in ("input", "output")
            },
            "tools": payload["tools"],
        }

    async def _dispatch_event(self, etype: str, event) -> None:
        if etype == "response.text.delta":
            etype = "response.output_text.delta"
        elif etype == "response.text.done":
            etype = "response.output_text.done"
        await super()._dispatch_event(etype, event)
