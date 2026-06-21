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


class InterruptReconcile(StrEnum):
    """How a provider reconciles its conversation history after JTS cuts
    assistant playback short on a barge-in / local cancel.

    This is the barge-in "pack" capability declaration: the robust-barge-in
    packs (later PRs) branch on this kind, never on provider name, so adding
    or swapping a provider needs no ``if provider == "openai"`` edit in pack
    code — same self-similar-registry boundary the transit providers use.
    The matching adapter methods are ``LiveTurn.cancel_response()`` and
    ``LiveTurn.truncate_assistant_audio()`` in ``jasper/voice/session.py``.

    Kinds (from the "Provider Interruption Contract" in
    docs/HANDOFF-voice-providers.md):

    - ``needs_client_truncate`` — the WebSocket transport keeps the full
      generated assistant turn server-side, so the client must send
      ``conversation.item.truncate`` at the heard boundary to align history
      (OpenAI Realtime).
    - ``server_self_truncates`` — the provider drops the unspoken tail on its
      own when user activity interrupts (Gemini Live's
      ``START_OF_ACTIVITY_INTERRUPTS``); there is no client truncate call to
      synthesize.
    - ``inherits`` — same wire shape as the provider this adapter subclasses;
      resolved to the base provider's kind via ``interrupt_reconcile_base``
      (Grok inherits OpenAI). Lets ``resolve_interrupt_reconcile()`` follow
      the one real subclass edge instead of duplicating the base's choice.
    """
    NEEDS_CLIENT_TRUNCATE = "needs_client_truncate"
    SERVER_SELF_TRUNCATES = "server_self_truncates"
    INHERITS = "inherits"


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
    # Barge-in reconciliation kind — the "pack" capability declaration the
    # robust-barge-in packs branch on (see ``InterruptReconcile``). REQUIRED,
    # no default: a correctness-bearing capability is declared per provider,
    # never silently defaulted (same no-silent-fallback stance the active-
    # provider selection takes). A future provider that omits it fails loudly
    # at construction instead of inheriting a wrong barge-in behaviour.
    # tests/test_voice_catalog.py pins the known values; "Adding a fourth
    # provider" in docs/HANDOFF-voice-providers.md lists it.
    interrupt_reconcile: InterruptReconcile
    # Only meaningful when ``interrupt_reconcile`` is ``INHERITS``: the
    # provider id whose reconciliation kind this one adopts (its adapter
    # subclasses that provider's adapter). Empty otherwise.
    interrupt_reconcile_base: str = ""
    extras: tuple[ProviderExtra, ...] = ()
    # Pricing-editor metadata (consumed by jasper/web/voice_setup.py): the
    # public pricing page a human/chatbot reads — no provider API exposes
    # voice-model prices — and which ``jasper.usage.Pricing`` buckets this
    # provider's cost model actually uses (Gemini Live can't split
    # text/cached; Grok is flat-rate). Single source per provider so adding
    # a backend touches the catalog entry + model_pricing.json, nothing else.
    pricing_url: str = ""
    pricing_buckets: tuple[str, ...] = ()


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
        pricing_url="https://ai.google.dev/gemini-api/docs/pricing",
        pricing_buckets=(
            "audio_input_per_million_usd",
            "audio_output_per_million_usd",
        ),
        # Gemini cuts the unspoken tail server-side on
        # START_OF_ACTIVITY_INTERRUPTS; no client truncate call to make.
        interrupt_reconcile=InterruptReconcile.SERVER_SELF_TRUNCATES,
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
        pricing_url="https://platform.openai.com/docs/pricing",
        pricing_buckets=(
            "audio_input_per_million_usd",
            "audio_output_per_million_usd",
            "text_input_per_million_usd",
            "text_output_per_million_usd",
            "cached_input_per_million_usd",
        ),
        # WebSocket playback keeps the whole assistant turn server-side; the
        # client must send conversation.item.truncate at the heard boundary.
        interrupt_reconcile=InterruptReconcile.NEEDS_CLIENT_TRUNCATE,
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
        pricing_url="https://docs.x.ai/developers/pricing",
        pricing_buckets=("flat_per_hour_usd",),
        # GrokRealtimeConnection subclasses the OpenAI adapter and reuses its
        # conversation.item.truncate / response.cancel wire shape — inherit
        # OpenAI's reconciliation kind rather than restating it.
        interrupt_reconcile=InterruptReconcile.INHERITS,
        interrupt_reconcile_base="openai",
    ),
)


PROVIDER_IDS_MANIFEST_FILE = "/var/lib/jasper/voice_provider_ids"

VALID_PROVIDER_IDS = frozenset(provider.id for provider in PROVIDERS)


def provider_ids_manifest_text() -> str:
    """Shell-readable provider-id manifest emitted during install."""
    return "".join(f"{provider_id}\n" for provider_id in sorted(VALID_PROVIDER_IDS))


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


def resolve_interrupt_reconcile(provider_id: str) -> InterruptReconcile:
    """Resolve a provider's concrete barge-in reconciliation kind.

    Follows an ``INHERITS`` declaration through ``interrupt_reconcile_base``
    so callers (the robust-barge-in packs) always get a concrete kind —
    never ``INHERITS`` — without encoding the subclass relationship
    themselves. Raises if an ``INHERITS`` entry has no resolvable base or the
    inheritance chain cycles."""
    provider = _require_provider(provider_id)
    seen: set[str] = set()
    while provider.interrupt_reconcile is InterruptReconcile.INHERITS:
        if provider.id in seen:
            raise RuntimeError(
                f"voice provider {provider.id!r}: cyclic interrupt_reconcile "
                "inheritance",
            )
        seen.add(provider.id)
        base_id = provider.interrupt_reconcile_base
        if not base_id:
            raise RuntimeError(
                f"voice provider {provider.id!r} declares INHERITS interrupt "
                "reconciliation but sets no interrupt_reconcile_base",
            )
        provider = _require_provider(base_id)
    return provider.interrupt_reconcile


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
