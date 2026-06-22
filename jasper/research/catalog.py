# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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


_PROVIDER_ENTRIES: tuple[TextProviderEntry, ...] = (
    TextProviderEntry(
        id="openai",
        label="OpenAI Responses",
        key_env=openai_research.OPENAI_API_KEY_ENV,
        model_env=openai_research.OPENAI_MODEL_ENV,
        default_model=openai_research.DEFAULT_MODEL,
        provider=openai_research.PROVIDER,
    ),
)


def _validate_providers(entries: tuple[TextProviderEntry, ...]) -> tuple[TextProviderEntry, ...]:
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise ValueError(f"duplicate research provider id: {entry.id}")
        seen.add(entry.id)
    return entries


PROVIDERS = _validate_providers(_PROVIDER_ENTRIES)


def provider_by_id(provider_id: str) -> TextProviderEntry | None:
    for entry in PROVIDERS:
        if entry.id == provider_id:
            return entry
    return None


def default_model(provider_id: str) -> str:
    entry = provider_by_id(provider_id)
    return entry.default_model if entry is not None else ""
