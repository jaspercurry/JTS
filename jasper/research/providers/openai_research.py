# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""OpenAI Responses-backed research provider.

The OpenAI SDK import is intentionally lazy so importing the registry is
hardware-free and dependency-light in tests and setup processes.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jasper.voice._supervisor import reconnect_backoff_delay

from ..base import (
    RESEARCH_ANSWER_INSTRUCTIONS,
    ResearchError,
    ResearchRequest,
    ResearchResult,
    TextLLMClient,
)

logger = logging.getLogger(__name__)


OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENV = "JASPER_RESEARCH_OPENAI_MODEL"
# Full current flagship (not the mini): stronger research answers, still fast
# enough for a <=30 s spoken summary. Any override via JASPER_RESEARCH_OPENAI_MODEL
# MUST have a matching row in jasper/data/model_pricing.json or the spend cap
# records the job at $0 (daemon_main warns on an unpriced research model).
DEFAULT_MODEL = "gpt-5.4"
_PENDING_STATUSES = {"queued", "in_progress"}
# Narrow cleanup-error set for best-effort shutdown/cancel paths — combined
# with the SDK's OpenAIError (via _provider_errors) at the call sites so we
# don't reach for a broad `except Exception`.
_CLEANUP_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


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
        # Strong refs to detached best-effort cancel tasks so they aren't
        # garbage-collected mid-flight (and don't warn) before they run.
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()

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
        response: Any = None
        try:
            response = await client.responses.create(
                model=self.model,
                input=req.query,
                instructions=RESEARCH_ANSWER_INSTRUCTIONS,
                background=True,
            )
            attempt = 1
            while getattr(response, "status", None) in _PENDING_STATUSES:
                await self._sleep(reconnect_backoff_delay(attempt))
                attempt += 1
                response = await client.responses.retrieve(response.id)
        except asyncio.CancelledError:
            # The scheduler's per-job runtime ceiling cancelled us. A
            # `background=True` response keeps running (and billing) server-side
            # until cancelled, and we'll never fetch its result — so best-effort
            # cancel it. Detached so cancellation isn't blocked; re-raise so the
            # scheduler still marks the job failed.
            self._spawn_remote_cancel(client, getattr(response, "id", None))
            raise
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
        usage = _normalize_usage(getattr(response, "usage", None))
        return ResearchResult(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            usage=usage,
        )

    async def aclose(self) -> None:
        """Drain the underlying OpenAI HTTP pool on daemon shutdown.

        The AsyncOpenAI SDK exposes async ``close()`` (not ``aclose()``); the
        registry's ``ActiveResearchProvider.aclose`` duck-types THIS method, so
        the cleanup contract is actually fulfilled (mirrors transit BusClient,
        whose pool would otherwise leak FDs across daemon restarts). Lazy
        client → nothing to close if ``complete()`` never ran.
        """
        # Let any in-flight best-effort cancels finish before closing the pool —
        # otherwise a shutdown-time cancel races teardown against a closed client
        # and the server-side job isn't actually stopped. Bounded (at most
        # `concurrency` of them) and fail-soft.
        if self._cleanup_tasks:
            await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)
        client = self._client
        close = getattr(client, "close", None) if client is not None else None
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except _CLEANUP_ERRORS + self._provider_errors():
            logger.warning("research: OpenAI client close failed", exc_info=True)

    def _spawn_remote_cancel(self, client: Any, response_id: str | None) -> None:
        if not response_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (interpreter teardown) — the server-side job will
            # lapse on its own; nothing more we can do without blocking.
            return
        task = loop.create_task(self._cancel_remote(client, response_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _cancel_remote(self, client: Any, response_id: str) -> None:
        try:
            await client.responses.cancel(response_id)
            logger.info(
                "research: cancelled timed-out OpenAI background job %s",
                response_id,
            )
        except _CLEANUP_ERRORS + self._provider_errors():
            logger.warning(
                "research: best-effort cancel of OpenAI job %s failed",
                response_id, exc_info=True,
            )


class OpenAIResearchProvider:
    def build_client(self, env: Mapping[str, str]) -> TextLLMClient | None:
        key = env.get(OPENAI_API_KEY_ENV, "").strip()
        if not key:
            return None
        model = env.get(OPENAI_MODEL_ENV, "").strip() or DEFAULT_MODEL
        return OpenAIResearchClient(api_key=key, model=model)


PROVIDER = OpenAIResearchProvider()


def _normalize_usage(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        usage = _deep_dict(raw)
    else:
        dump = getattr(raw, "model_dump", None)
        if callable(dump):
            usage = _deep_dict(dump())
        else:
            usage = _deep_dict({
                "input_tokens": getattr(raw, "input_tokens", 0),
                "output_tokens": getattr(raw, "output_tokens", 0),
                "input_token_details": getattr(raw, "input_token_details", None),
                "output_token_details": getattr(raw, "output_token_details", None),
            })

    if "input_token_details" not in usage and "input_tokens_details" in usage:
        usage["input_token_details"] = usage.pop("input_tokens_details")
    if "output_token_details" not in usage and "output_tokens_details" in usage:
        usage["output_token_details"] = usage.pop("output_tokens_details")

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    input_details = usage.get("input_token_details")
    if not isinstance(input_details, dict):
        usage["input_token_details"] = {"text_tokens": input_tokens}
    elif "text_tokens" not in input_details and "audio_tokens" not in input_details:
        input_details["text_tokens"] = input_tokens
    output_details = usage.get("output_token_details")
    if not isinstance(output_details, dict):
        usage["output_token_details"] = {"text_tokens": output_tokens}
    elif "text_tokens" not in output_details and "audio_tokens" not in output_details:
        output_details["text_tokens"] = output_tokens
    return usage


def _deep_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _deep_dict(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_deep_dict(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _deep_dict(dump())
    return value
