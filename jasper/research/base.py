"""Shared contracts for async text research providers.

This is intentionally smaller than the realtime voice provider layer:
research is one request to one text model, with no turns, audio frames,
session resumption, or provider-side voice state.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResearchRequest:
    query: str


@dataclass(frozen=True)
class ResearchResult:
    text: str


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
