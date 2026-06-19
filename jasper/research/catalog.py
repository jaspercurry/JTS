"""Catalog of text LLM providers usable by the async research tool."""
from __future__ import annotations

from dataclasses import dataclass

from .base import TextLLMProvider
from .providers import openai_research


@dataclass(frozen=True)
class TextProviderEntry:
    id: str
    label: str
    key_env: str
    model_env: str
    default_model: str
    provider: TextLLMProvider


PROVIDERS: tuple[TextProviderEntry, ...] = (
    TextProviderEntry(
        id="openai",
        label="OpenAI Responses",
        key_env=openai_research.OPENAI_API_KEY_ENV,
        model_env=openai_research.OPENAI_MODEL_ENV,
        default_model=openai_research.DEFAULT_MODEL,
        provider=openai_research.PROVIDER,
    ),
)


def provider_by_id(provider_id: str) -> TextProviderEntry | None:
    for entry in PROVIDERS:
        if entry.id == provider_id:
            return entry
    return None


def default_model(provider_id: str) -> str:
    entry = provider_by_id(provider_id)
    return entry.default_model if entry is not None else ""
