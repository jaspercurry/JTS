"""OpenAI Responses-backed research provider.

The OpenAI SDK import is intentionally lazy so importing the registry is
hardware-free and dependency-light in tests and setup processes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jasper.voice._supervisor import reconnect_backoff_delay

from ..base import ResearchError, ResearchRequest, ResearchResult, TextLLMClient


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENV = "JASPER_RESEARCH_OPENAI_MODEL"
DEFAULT_MODEL = "gpt-5.4-mini"
_PENDING_STATUSES = {"queued", "in_progress"}


class OpenAIResearchClient:
    """Small wrapper around OpenAI Responses background mode."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        provider_error_classes: tuple[type[Exception], ...] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client
        self._sleep = sleep
        self._provider_error_classes = provider_error_classes

    def _openai_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ResearchError("OpenAI research provider is missing the openai package") from e

        self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client

    def _provider_errors(self) -> tuple[type[Exception], ...]:
        if self._provider_error_classes is not None:
            return self._provider_error_classes
        try:
            from openai import OpenAIError
        except ImportError:
            return ()
        return (OpenAIError,)

    async def complete(self, req: ResearchRequest) -> ResearchResult:
        client = self._openai_client()
        provider_errors = self._provider_errors()
        try:
            response = await client.responses.create(
                model=self.model,
                input=req.query,
                background=True,
            )
            attempt = 1
            while getattr(response, "status", None) in _PENDING_STATUSES:
                await self._sleep(reconnect_backoff_delay(attempt))
                attempt += 1
                response = await client.responses.retrieve(response.id)
        except provider_errors as e:
            detail = str(e) or type(e).__name__
            raise ResearchError(f"OpenAI research request failed: {detail}") from e

        status = getattr(response, "status", "")
        if status != "completed":
            detail = getattr(response, "error", None) or status or "unknown"
            raise ResearchError(f"OpenAI research failed: {detail}")

        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            raise ResearchError("OpenAI research completed without text")
        return ResearchResult(text=text)


class OpenAIResearchProvider:
    def build_client(self, env: Mapping[str, str]) -> TextLLMClient | None:
        key = env.get(OPENAI_API_KEY_ENV, "").strip()
        if not key:
            return None
        model = env.get(OPENAI_MODEL_ENV, "").strip() or DEFAULT_MODEL
        return OpenAIResearchClient(api_key=key, model=model)


PROVIDER = OpenAIResearchProvider()
