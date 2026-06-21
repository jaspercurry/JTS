# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared contracts for async text research providers.

This is intentionally smaller than the realtime voice provider layer:
research is one request to one text model, with no turns, audio frames,
session resumption, or provider-side voice state.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


RESEARCH_ANSWER_INSTRUCTIONS = (
    "Answer for a smart speaker to read aloud. Keep the answer to 30 "
    "seconds or less, roughly 75 words. Be direct and useful. Do not use "
    "markdown, bullets, citations, links, or preambles unless essential."
)


@dataclass(frozen=True)
class ResearchRequest:
    query: str


@dataclass(frozen=True)
class ResearchResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    usage: dict | None = None


class ResearchError(RuntimeError):
    """Provider-side failure while completing a research request."""


@runtime_checkable
class TextLLMClient(Protocol):
    """One-shot text LLM client used by the background scheduler."""

    async def complete(self, req: ResearchRequest) -> ResearchResult:
        """Return the research result for one request."""
        ...


@runtime_checkable
class TextLLMProvider(Protocol):
    """Structural provider contract for the Pattern-2 registry."""

    def build_client(self, env: Mapping[str, str]) -> TextLLMClient | None:
        """Return a configured client, or None when this provider is unset."""
        ...
