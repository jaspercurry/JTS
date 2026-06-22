# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pattern-2 text-provider registry for async research.

Providers parse their own env keys from a plain mapping. A missing key
returns no client; a broken provider is fault-isolated so one adapter can
never take down the rest of the daemon when Phase 2 wires this in.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .base import (
    RESEARCH_ANSWER_INSTRUCTIONS,
    ResearchError,
    ResearchRequest,
    ResearchResult,
    TextLLMClient,
    TextLLMProvider,
)
from .catalog import PROVIDERS, TextProviderEntry, default_model, provider_by_id
from .scheduler import (
    DEFAULT_CONCURRENCY,
    DEFAULT_DB_PATH,
    DEFAULT_MAX_RUNTIME_SEC,
    DEFAULT_MAX_RESULT_CHARS,
    DONE,
    FAILED,
    RUNNING,
    ResearchJob,
    ResearchJobStore,
    ResearchScheduler,
    ResearchStartResult,
)

logger = logging.getLogger(__name__)

_PROVIDER_LIFECYCLE_ERRORS = (
    AttributeError,
    ImportError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True)
class ActiveResearchProvider:
    provider_id: str
    client: TextLLMClient

    async def aclose(self) -> None:
        aclose = getattr(self.client, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except _PROVIDER_LIFECYCLE_ERRORS:
            logger.exception(
                "research provider %s aclose failed during shutdown",
                self.provider_id,
            )


def active_research_provider(env: Mapping[str, str]) -> ActiveResearchProvider | None:
    for entry in PROVIDERS:
        try:
            client = entry.provider.build_client(env)
        except _PROVIDER_LIFECYCLE_ERRORS:
            logger.exception(
                "research provider %s build_client failed; skipping it",
                entry.id,
            )
            continue
        if client is None:
            continue
        return ActiveResearchProvider(provider_id=entry.id, client=client)
    return None


__all__ = [
    "ActiveResearchProvider",
    "DONE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_DB_PATH",
    "DEFAULT_MAX_RUNTIME_SEC",
    "DEFAULT_MAX_RESULT_CHARS",
    "FAILED",
    "PROVIDERS",
    "RESEARCH_ANSWER_INSTRUCTIONS",
    "RUNNING",
    "ResearchError",
    "ResearchJob",
    "ResearchJobStore",
    "ResearchRequest",
    "ResearchResult",
    "ResearchScheduler",
    "ResearchStartResult",
    "TextLLMClient",
    "TextLLMProvider",
    "TextProviderEntry",
    "active_research_provider",
    "default_model",
    "provider_by_id",
]
