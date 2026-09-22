# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the voice wizard's selection and fields from the provider catalog."""
from __future__ import annotations

from dataclasses import dataclass

from jasper.voice.catalog import (
    ProviderCatalogEntry, default_model_id, default_voice_id, provider_by_id,
)
from jasper.voice.model_discovery import DiscoverySnapshot
from jasper.voice.provider_state import resolve_active_provider

from ._common import mask_secret, value_for_env


def active_provider_id(state: dict[str, str]) -> str:
    return resolve_active_provider({
        "JASPER_VOICE_PROVIDER": value_for_env(state, "JASPER_VOICE_PROVIDER", ""),
    })


def selected_provider(
    state: dict[str, str], requested: str | None,
) -> ProviderCatalogEntry | None:
    return provider_by_id(active_provider_id(state) if requested is None else requested)


def model_options(
    provider: ProviderCatalogEntry, discovered: DiscoverySnapshot | None,
    current: str = "",
) -> dict[str, str]:
    options = {model.id: model.display_label for model in provider.models}
    if discovered:
        for model_id in discovered.models:
            options.setdefault(model_id, f"{model_id} (experimental; discovered)")
    if current and current not in options:
        options = {current: f"{current} (custom; experimental)", **options}
    return options


def provider_model_ids(
    provider: ProviderCatalogEntry, discovered: DiscoverySnapshot | None,
) -> list[str]:
    return list(model_options(provider, discovered))


def submitted_settings(
    provider: ProviderCatalogEntry, form: dict[str, str],
) -> dict[str, str]:
    fields = {"model": provider.model_env, "voice": provider.voice_env}
    fields.update((extra.name, extra.env) for extra in provider.extras)
    return {
        env: value for name, env in fields.items()
        if (value := form.get(f"{provider.id}_{name}", "").strip())
    }


@dataclass(frozen=True)
class Choice:
    name: str
    label: str
    value: str
    options: dict[str, str]
    hint: str = ""

    def __post_init__(self) -> None:
        if self.value not in self.options:
            self.options[self.value] = f"{self.value} (custom)"


@dataclass(frozen=True)
class ProviderSettings:
    provider: ProviderCatalogEntry
    masked_key: str
    model: Choice
    options: tuple[Choice, ...]


def provider_settings(
    provider: ProviderCatalogEntry, state: dict[str, str],
    discovered: DiscoverySnapshot | None,
) -> ProviderSettings:
    model = value_for_env(state, provider.model_env, default_model_id(provider.id))
    voice = value_for_env(state, provider.voice_env, default_voice_id(provider.id))
    return ProviderSettings(
        provider=provider,
        masked_key=mask_secret(value_for_env(state, provider.key_env)),
        model=Choice(f"{provider.id}_model", "Model", model,
                     model_options(provider, discovered, model)),
        options=(
            Choice(f"{provider.id}_voice", "Voice", voice,
                   {v.id: v.label for v in provider.voices}),
            *(Choice(f"{provider.id}_{extra.name}", extra.label,
                     value_for_env(state, extra.env, extra.default),
                     {option.id: option.label for option in extra.options}, extra.hint)
              for extra in provider.extras),
        ),
    )
