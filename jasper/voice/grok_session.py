# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""xAI Grok Voice Agent adapter.

Per xAI's docs (https://docs.x.ai/docs/guides/voice/agent), the Grok
Voice Agent API is **compatible with the OpenAI Realtime API
specification** — same client events, same audio format negotiation,
same flat function-tool schema, same ``conversation.item.create`` /
``response.create`` round-trip. This adapter is therefore a thin
subclass of ``OpenAIRealtimeConnection`` that:

  1. Routes the WebSocket through ``wss://api.x.ai/v1/realtime`` instead
     of OpenAI's endpoint.
  2. Normalises the one event-name divergence that xAI explicitly
     documents: text deltas come back as ``response.text.delta`` instead
     of OpenAI's GA name ``response.output_text.delta``. Audio deltas
     and tool-call events keep OpenAI's GA names on Grok, so this only
     matters if/when we start consuming text deltas.
  3. Skips the ``reasoning.effort`` field — Grok's voice models don't
     accept it.

Defaults:
  - Model: ``grok-voice-think-fast-1.0`` (per xAI's docs; the older
    ``grok-voice-fast-1.0`` is deprecated).
  - Voice: ``eve``. Other Grok voices: ``ara``, ``rex``, ``sal``, ``leo``.

Pricing: Grok Voice Agent publishes a flat $3.00/hour realtime rate —
neither audio tokens nor cached input, so the per-turn token rows price
to $0. The xAI dashboard shows idle warm WebSocket time is not billed
like active conversation time, so JTS estimates spend from active voice
turn duration: the daemon wires a ``BillableActivityMeter`` into this
connection (gated on the bundled ``flat_per_hour_usd > 0`` rate for the
active model), and this class inherits the ``set_billable_activity_meter``
plumbing from ``OpenAIRealtimeConnection``. The recorded intervals fold
into the daily spend cap via
``UsageStore._time_billed_spend_by_provider`` — so the cap constrains
Grok the same as token-billed providers. See ``jasper.usage`` and
``jasper.voice_daemon._make_connection``."""
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
    """xAI Grok Voice Agent connection over the OpenAI-compatible
    Realtime API.

    Inherits the entire OpenAI adapter — supervisor, reconnect, audio
    upsampling, tool dispatch — and only overrides:

      - ``PROVIDER_NAME`` so tool registry filters use ``"grok"`` for
        the visibility check.
      - The default base URL.
      - Suppression of the ``reasoning.effort`` field (Grok rejects it).
    """

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
        payload["audio"]["input"].pop("noise_reduction", None)
        return payload

    async def _dispatch_event(self, etype: str, event) -> None:
        # Per xAI docs, the only top-level event-name divergence from
        # OpenAI's GA is `response.text.delta` (xAI) vs
        # `response.output_text.delta` (OpenAI). We don't currently
        # consume text deltas (audio is the only modality the daemon
        # plays), so this normaliser is forward-compat only — if we
        # ever start surfacing transcripts from text events, the
        # remapping ensures the parent dispatcher sees OpenAI's name.
        if etype == "response.text.delta":
            etype = "response.output_text.delta"
        elif etype == "response.text.done":
            etype = "response.output_text.done"
        await super()._dispatch_event(etype, event)
