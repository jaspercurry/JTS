"""Curated voice-provider model and voice catalog.

The catalog powers the /voice setup wizard. It is intentionally not a
runtime allow-list: the adapters pass the configured model string to the
provider SDK, so an operator can still try a newly released model before
this file is refreshed. Unknown configured models are surfaced by the
wizard as custom experimental choices rather than silently replaced.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModelStatus(StrEnum):
    TESTED = "tested"
    FALLBACK = "fallback"
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    status: ModelStatus
    note: str = ""
    default: bool = False

    @property
    def display_label(self) -> str:
        suffix = self.status.value
        if self.note:
            suffix = f"{suffix}; {self.note}"
        return f"{self.label} ({suffix})"


@dataclass(frozen=True)
class VoiceOption:
    id: str
    label: str
    default: bool = False


@dataclass(frozen=True)
class ProviderExtraOption:
    id: str
    label: str


@dataclass(frozen=True)
class ProviderExtra:
    name: str
    env: str
    label: str
    default: str
    options: tuple[ProviderExtraOption, ...]
    hint: str = ""


@dataclass(frozen=True)
class ProviderCatalogEntry:
    id: str
    label: str
    vendor: str
    key_env: str
    key_prefix_hint: str
    key_url: str
    model_env: str
    voice_env: str
    cost_hint: str
    models: tuple[ModelOption, ...]
    voices: tuple[VoiceOption, ...]
    extras: tuple[ProviderExtra, ...] = ()


PROVIDERS: tuple[ProviderCatalogEntry, ...] = (
    ProviderCatalogEntry(
        id="gemini",
        label="Gemini Live",
        vendor="Google",
        key_env="GEMINI_API_KEY",
        key_prefix_hint="AIzaSy...",
        key_url="https://aistudio.google.com/apikey",
        model_env="JASPER_GEMINI_MODEL",
        voice_env="JASPER_GEMINI_VOICE",
        cost_hint="~$0.025 / minute",
        models=(
            ModelOption(
                id="gemini-3.1-flash-live-preview",
                label="3.1 Flash Live preview",
                status=ModelStatus.TESTED,
                note="default",
                default=True,
            ),
            ModelOption(
                id="gemini-2.5-flash-native-audio-preview-12-2025",
                label="2.5 Flash native-audio preview",
                status=ModelStatus.FALLBACK,
                note="silent-session recovery",
            ),
        ),
        # Gender/style hints sourced from Google's prebuilt-voices
        # catalogue (ai.google.dev/gemini-api/docs/speech-generation).
        voices=(
            VoiceOption(id="Aoede", label="Aoede - feminine, breezy", default=True),
            VoiceOption(id="Charon", label="Charon - masculine, informative"),
            VoiceOption(id="Fenrir", label="Fenrir - masculine, excitable"),
            VoiceOption(id="Kore", label="Kore - feminine, firm"),
            VoiceOption(id="Puck", label="Puck - masculine, upbeat"),
            VoiceOption(id="Leda", label="Leda - feminine, youthful"),
            VoiceOption(id="Orus", label="Orus - masculine, firm"),
            VoiceOption(id="Zephyr", label="Zephyr - feminine, bright"),
        ),
    ),
    ProviderCatalogEntry(
        id="openai",
        label="OpenAI Realtime",
        vendor="OpenAI",
        key_env="OPENAI_API_KEY",
        key_prefix_hint="sk-...",
        key_url="https://platform.openai.com/api-keys",
        model_env="JASPER_OPENAI_MODEL",
        voice_env="JASPER_OPENAI_VOICE",
        cost_hint="~$0.30 / minute (gpt-realtime-2)",
        models=(
            ModelOption(
                id="gpt-realtime-2",
                label="gpt-realtime-2",
                status=ModelStatus.TESTED,
                note="default",
                default=True,
            ),
            ModelOption(
                id="gpt-realtime-mini",
                label="gpt-realtime-mini",
                status=ModelStatus.FALLBACK,
                note="lower cost, no reasoning",
            ),
            ModelOption(
                id="gpt-realtime-1.5",
                label="gpt-realtime-1.5",
                status=ModelStatus.FALLBACK,
                note="older GA",
            ),
        ),
        # Gender/style hints sourced from OpenAI's voice catalogue
        # (platform.openai.com/docs/guides/realtime). The user picked
        # `ash` once expecting feminine and got masculine - these
        # hints exist to head that off.
        voices=(
            VoiceOption(id="marin", label="marin - feminine, warm", default=True),
            VoiceOption(id="cedar", label="cedar - masculine, calm"),
            VoiceOption(id="alloy", label="alloy - neutral, balanced"),
            VoiceOption(id="ash", label="ash - masculine, soft"),
            VoiceOption(id="ballad", label="ballad - masculine, expressive"),
            VoiceOption(id="coral", label="coral - feminine, bright"),
            VoiceOption(id="echo", label="echo - masculine, smooth"),
            VoiceOption(id="sage", label="sage - feminine, even"),
            VoiceOption(id="shimmer", label="shimmer - feminine, light"),
            VoiceOption(id="verse", label="verse - masculine, melodic"),
        ),
        extras=(
            ProviderExtra(
                name="reasoning_effort",
                env="JASPER_OPENAI_REASONING_EFFORT",
                label="Reasoning effort (gpt-realtime-2)",
                default="low",
                options=(
                    ProviderExtraOption(
                        id="minimal",
                        label="minimal - ~1.1 s TTFA, less coherent multi-step",
                    ),
                    ProviderExtraOption(
                        id="low",
                        label="low (default) - best for short voice queries",
                    ),
                    ProviderExtraOption(id="medium", label="medium"),
                    ProviderExtraOption(id="high", label="high"),
                    ProviderExtraOption(
                        id="xhigh",
                        label="xhigh - slowest, most thorough",
                    ),
                ),
                hint=(
                    "Only meaningful on gpt-realtime-2. "
                    "Silently ignored on older models."
                ),
            ),
        ),
    ),
    ProviderCatalogEntry(
        id="grok",
        label="Grok Voice Agent",
        vendor="xAI",
        key_env="XAI_API_KEY",
        key_prefix_hint="xai-...",
        key_url="https://console.x.ai/",
        model_env="JASPER_GROK_MODEL",
        voice_env="JASPER_GROK_VOICE",
        cost_hint="$3 / hour flat (~$0.05 / minute)",
        models=(
            ModelOption(
                id="grok-voice-think-fast-1.0",
                label="grok-voice-think-fast-1.0",
                status=ModelStatus.TESTED,
                note="default",
                default=True,
            ),
        ),
        # Gender/style hints sourced from xAI's voice catalogue
        # (docs.x.ai/docs/guides/voice/agent).
        voices=(
            VoiceOption(id="eve", label="eve - feminine, warm", default=True),
            VoiceOption(id="ara", label="ara - feminine, casual"),
            VoiceOption(id="rex", label="rex - masculine, confident"),
            VoiceOption(id="sal", label="sal - masculine, casual"),
            VoiceOption(id="leo", label="leo - masculine, smooth"),
        ),
    ),
)


VALID_PROVIDER_IDS = frozenset(provider.id for provider in PROVIDERS)


def provider_by_id(provider_id: str) -> ProviderCatalogEntry | None:
    for provider in PROVIDERS:
        if provider.id == provider_id:
            return provider
    return None


def _require_provider(provider_id: str) -> ProviderCatalogEntry:
    provider = provider_by_id(provider_id)
    if provider is None:
        raise KeyError(f"unknown voice provider {provider_id!r}")
    return provider


def default_model_id(provider_id: str) -> str:
    provider = _require_provider(provider_id)
    defaults = [model for model in provider.models if model.default]
    if len(defaults) != 1:
        raise RuntimeError(
            f"voice provider {provider_id!r} must have exactly one "
            "default model",
        )
    return defaults[0].id


def default_voice_id(provider_id: str) -> str:
    provider = _require_provider(provider_id)
    defaults = [voice for voice in provider.voices if voice.default]
    if len(defaults) != 1:
        raise RuntimeError(
            f"voice provider {provider_id!r} must have exactly one "
            "default voice",
        )
    return defaults[0].id


def default_extra_value(provider_id: str, name: str) -> str:
    provider = _require_provider(provider_id)
    for extra in provider.extras:
        if extra.name == name:
            return extra.default
    raise KeyError(
        f"voice provider {provider_id!r} has no extra field {name!r}",
    )
