# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for the *active voice provider*.

:func:`select_voice` is the one writer of the provider and model selection
in ``/var/lib/jasper/voice_provider.env`` (key ``JASPER_VOICE_PROVIDER``,
one of the ids in :data:`jasper.voice.catalog.VALID_PROVIDER_IDS`); the
``/assistant/voice/`` wizard and ``jasper-settings`` both call it (ADR-0350).
Per the project contract there is **no fallback default**: an unset
value means "no provider configured yet", and every surface must render
that honestly (empty / "not configured") rather than guessing a
provider.

This module is the ONE place that resolves "which provider is active"
for *display/aggregation* consumers — chiefly ``jasper-control``'s
``/state`` and ``/system`` dashboard. It deliberately re-reads the file
on each call so a wizard save is reflected immediately, **without
restarting the long-lived jasper-control daemon**.

Why not ``os.environ``: long-lived daemons load
``voice_provider.env`` as a systemd ``EnvironmentFile=`` at *process
start*, so ``os.environ['JASPER_VOICE_PROVIDER']`` is frozen for the
process lifetime. Only ``jasper-voice`` is restarted on a provider
switch, so any other process reading ``os.environ`` shows the previous
provider until it happens to restart.

``jasper.config.Config.from_env`` remains the resolver for the
*running* daemon (``jasper-voice``), whose environment is always fresh
because it is restarted on every switch. This module is for the
processes that are **not** restarted.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..atomic_io import locked_update_env_file
from ..env_load import (
    BASE_ENV_PATH,
    merged_env_files,
    parse_bool_value,
    read_env_file_state,
)
from ..log_event import log_event
from . import model_discovery
from .catalog import (
    PROVIDERS,
    VALID_PROVIDER_IDS,
    ProviderCatalogEntry,
    default_model_id,
    provider_by_id,
)

logger = logging.getLogger(__name__)

# The single source of truth for active-provider state. The path (not the
# provider value) may be overridden with JASPER_VOICE_PROVIDER_FILE, mirroring
# the wizard's own --state default. That env var is a static deploy constant,
# so reading it once is fine — only the file's *contents* are read fresh on
# every call.
#
# This file is deliberately KEPT broad (group `jasper`, under the /var/lib/jasper
# StateDirectory). It holds only the non-secret selectors (JASPER_VOICE_PROVIDER + the
# per-provider model / voice). The high-value API keys live separately in KEYS_FILE
# below, in the group-`jasper-secrets` dir that only jasper-voice + jasper-web can read.
# So jasper-control keeps reading the active provider/model here for /system/ (this
# module) without gaining access to the LLM keys.
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"
PROVIDER_FILE_MODE = 0o640
VOICE_PROVIDER_ENV_OWNER = (
    "jasper.voice.provider_state; change it at /assistant/voice/ or with jasper-settings"
)

# The three provider API keys (GEMINI/OPENAI/XAI) split out of PROVIDER_FILE into a
# sibling secret dir narrowed to the `jasper-secrets` group {jasper-voice, jasper-web}.
# The /voice wizard writes it; jasper-voice + jasper-web source it via EnvironmentFile.
# Outside the /var/lib/jasper StateDirectory on purpose — systemd's recursive
# StateDirectory chown would otherwise force its group back to `jasper`, re-exposing the
# keys to every jasper daemon. NOT read by this module (jasper-control has no business
# reading the keys).
KEYS_FILE = "/var/lib/jasper-secrets/voice_keys.env"

ProviderStateStatus = Literal[
    "configured",
    "unset",
    "missing",
    "unreadable",
    "invalid",
]


@dataclass(frozen=True)
class ActiveProviderState:
    """Status-bearing read of the active-provider SSOT file.

    ``provider`` / ``model`` keep the old display contract: empty and
    ``None`` mean no usable provider. ``status`` preserves why, so
    diagnostics can distinguish first-time setup from a permission
    problem or bad value instead of collapsing every failure into
    "unset".
    """

    provider: str
    model: str | None
    status: ProviderStateStatus
    path: str
    raw_provider: str = ""
    error: str = ""

    @property
    def configured(self) -> bool:
        return self.status == "configured"

    @property
    def detail(self) -> str:
        if self.status == "configured":
            return ""
        if self.status == "missing":
            return f"{self.path} missing"
        if self.status == "unreadable":
            return self.error or f"{self.path} unreadable"
        if self.status == "invalid":
            return f"unsupported JASPER_VOICE_PROVIDER={self.raw_provider!r}"
        return "JASPER_VOICE_PROVIDER unset"


def _resolve_path(path: str | None) -> str:
    if path is not None:
        return path
    return os.environ.get("JASPER_VOICE_PROVIDER_FILE", PROVIDER_FILE)


def resolve_active_provider(env: dict[str, str]) -> str:
    """Select+validate the active provider id from an already-parsed env
    mapping. Returns ``""`` (unconfigured) when the value is unset or is
    not a recognized provider id — **never** a guessed default. Pure; no
    IO, so the wizard (which already has the env loaded) and the file
    readers below share one validation rule."""
    provider = (env.get("JASPER_VOICE_PROVIDER") or "").strip()
    return provider if provider in VALID_PROVIDER_IDS else ""


def read_active_provider(path: str | None = None) -> str:
    """Read the active provider id fresh from the SSOT file. ``""`` when
    unconfigured. Best-effort: a missing or unreadable file reads as
    unconfigured rather than raising."""
    return read_active_provider_state(path).provider


def read_active_provider_state(path: str | None = None) -> ActiveProviderState:
    """Read the active provider id and its diagnostic status.

    This is still fail-soft, but unlike :func:`read_active_provider` it
    does not erase the difference between a legitimate first-time setup
    state and a bad diagnostic context such as a non-root process being
    unable to traverse ``/var/lib/jasper``.
    """
    resolved = _resolve_path(path)
    file_state = read_env_file_state(resolved)
    if file_state.status == "missing":
        return ActiveProviderState("", None, "missing", resolved)
    if file_state.status == "unreadable":
        return ActiveProviderState(
            "",
            None,
            "unreadable",
            resolved,
            error=file_state.error,
        )

    env = file_state.values
    raw = (env.get("JASPER_VOICE_PROVIDER") or "").strip()
    provider = resolve_active_provider(env)
    if not provider:
        return ActiveProviderState(
            "",
            None,
            "invalid" if raw else "unset",
            resolved,
            raw_provider=raw,
        )

    entry = provider_by_id(provider)
    assert entry is not None
    model = (env.get(entry.model_env) or "").strip()
    return ActiveProviderState(
        provider,
        model or default_model_id(provider),
        "configured",
        resolved,
        raw_provider=raw,
    )


def read_active_model_from_env_files(
    provider: str, paths: "tuple[str, ...] | None" = None,
) -> str:
    """The model ``provider`` resolves to from the merged env FILES
    (:func:`jasper.env_load.merged_env_files`) — never from this
    process's own ``os.environ``.

    The model's documented home is ``jasper.env`` (the operator base
    file; ``.env.example`` ships the keys there), but :func:`select_voice`
    (the wizard and ``jasper-settings voice --model``) writes it to *this*
    module's file instead — the later file wins, which
    is exactly what merging the full env-file set (operator file first,
    wizard file after — the same order ``jasper-voice`` sources them in)
    gives you. That is why this doesn't just read :data:`PROVIDER_FILE`
    alone: a model an operator pinned only in ``jasper.env`` would read
    back as the catalog default there.

    Bypassing ``os.environ`` matters because a calling-shell export of
    ``JASPER_GEMINI_MODEL``/``JASPER_OPENAI_MODEL``/``JASPER_GROK_MODEL``
    outranks both files there (``jasper.env_load.load_env_files`` uses
    ``setdefault``), so a reader built on ``Config``/``os.environ`` —
    jasper-doctor invoked as ``sudo -E jasper-doctor``, say — can name a
    model ``jasper-voice`` does not actually run. Same drift class
    :func:`read_active_provider_state` closes for the provider selector
    itself (issue #3133).

    Falls back to the catalog default when neither file pins one,
    matching what ``jasper-voice`` resolves from a clean environment.
    ``""`` for an unknown provider id."""
    entry = provider_by_id(provider)
    if entry is None:
        return ""
    value = merged_env_files(paths).get(entry.model_env, "").strip()
    return value or default_model_id(provider)


# --- The voice selection's one writer (ADR-0350) ------------------------


class VoiceSelectionRefused(ValueError):
    """:func:`select_voice` declined; ``reason`` is the slug a front end prints."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class VoiceSelection:
    """The provider and model in effect after :func:`select_voice`, and which
    of the two (``"provider"``, ``"model"``) it changed."""

    provider: str
    model: str
    changed: tuple[str, ...]


def voice_env_files(
    path: str | None = None, keys_path: str | None = None,
) -> tuple[str, ...]:
    """The env files a provider's key and model can come from, in
    ``jasper-voice``'s ``EnvironmentFile=`` order (later wins)."""
    return (BASE_ENV_PATH, _resolve_path(path), keys_path or KEYS_FILE)


def keys_set(paths: tuple[str, ...]) -> frozenset[str]:
    """The provider key variables ``paths`` set to a non-empty value.

    Names only: no key value leaves this function. An unreadable file raises
    OSError rather than reading as "unset".
    """
    env = merged_env_files(paths, require_readable=True)
    return frozenset(p.key_env for p in PROVIDERS if env.get(p.key_env, "").strip())


def offered_models(
    provider: ProviderCatalogEntry,
    discovered: model_discovery.DiscoverySnapshot | None,
) -> list[str]:
    """The models the voice wizard offers for ``provider``: the catalog's,
    then those its Refresh discovered."""
    return list(dict.fromkeys([
        *(model.id for model in provider.models),
        *(discovered.models if discovered else ()),
    ]))


def select_voice(
    provider: str | None = None,
    model: str | None = None,
    *,
    via: str,
    client: str | None = None,
    path: str | None = None,
    keys_path: str | None = None,
    discovery_path: str | None = None,
) -> VoiceSelection:
    """Select ``provider`` (default: the active one) and ``model`` (default:
    the one it already runs) for every front end.

    The catalog is not a runtime allow-list, so a model is accepted when the
    wizard offers it (:func:`offered_models`) or it is already in effect.
    Refuses (:class:`VoiceSelectionRefused`) an unknown provider or model, and
    a provider whose API key no env file sets — checking presence only.
    ``via`` names the front end on the ``event=voice.save`` line. Raises
    OSError when a file cannot be read or written.
    """
    path = _resolve_path(path)
    files = voice_env_files(path, keys_path)
    before = read_active_provider_state(path).provider
    target = before if provider is None else provider
    entry = provider_by_id(target)
    if entry is None:
        if provider is None:
            raise VoiceSelectionRefused(
                "provider_unset", "No voice provider is selected yet; name one.",
            )
        raise VoiceSelectionRefused(
            "unknown_provider",
            f"Unknown provider {provider!r}; choose one of: "
            f"{', '.join(sorted(VALID_PROVIDER_IDS))}.",
        )
    keys = keys_set(files)
    in_effect = read_active_model_from_env_files(target, files)
    old = read_active_model_from_env_files(before, files) or None
    if model is not None and model != in_effect:
        offered = offered_models(entry, model_discovery.load_cache(
            discovery_path or model_discovery.DEFAULT_CACHE_PATH,
        ).get(target))
        if model not in offered:
            raise VoiceSelectionRefused(
                "unknown_model",
                f"{entry.label} offers no model {model!r}; choose one of: "
                f"{', '.join(offered)}.",
            )
    if entry.key_env not in keys:
        raise VoiceSelectionRefused(
            "key_unset",
            f"{entry.label} has no API key configured yet. Paste a "
            f"{entry.key_env} value at /assistant/voice/ before selecting it "
            "as active.",
        )
    updates = {"JASPER_VOICE_PROVIDER": target}
    if model is not None:
        updates[entry.model_env] = model
    locked_update_env_file(
        path, updates, mode=PROVIDER_FILE_MODE, owner=VOICE_PROVIDER_ENV_OWNER,
    )
    after = in_effect if model is None else model
    log_event(logger, "voice.save", provider=target, model=after, via=via, client=client)
    changed = tuple(
        name for name, was, now in (("provider", before, target), ("model", old, after))
        if was != now
    )
    return VoiceSelection(target, after, changed)


# --- Per-provider barge-in enable flag ---------------------------------
#
# Full-duplex barge-in (the user talking over the assistant flushes local
# TTS) is opt-in **per provider** with defaults declared in the catalog. It is a provider
# selector, so it lives alongside JASPER_VOICE_PROVIDER + the model/voice
# selectors in the broad-group SSOT file (read fresh on every call), NOT
# in the typed ``Config``: a long-lived reader that is not restarted on a
# wizard toggle (jasper-control's ``/state``) would otherwise show a stale
# value. jasper-voice IS restarted on a *provider* switch, but the
# barge-in toggle can change without one, so jasper-voice also resolves it
# fresh (once per turn) rather than from its start-time ``Config``.


def barge_in_env_key(provider: str) -> str:
    """The SSOT env key carrying ``provider``'s barge-in enable flag,
    e.g. ``JASPER_BARGE_IN_GEMINI``."""
    return f"JASPER_BARGE_IN_{provider.upper()}"


def resolve_barge_in_enabled(provider: str, env: Mapping[str, str]) -> bool:
    """Resolve the saved toggle, or the adapter's declared default."""
    entry = provider_by_id(provider)
    if entry is None:
        return False
    raw = env.get(barge_in_env_key(provider), "true" if entry.barge_in_default else "")
    return parse_bool_value(raw) is True


# read_barge_in_enabled runs once per turn-open on jasper-voice's event
# loop. A full open+read+parse of the SSOT file every turn would expose the
# latency-sensitive turn-open path to a stalled /var/lib FS even when
# barge-in is OFF (the common case). Gate the parse on the file's mtime+size:
# the steady state is a single os.stat, and a wizard/operator toggle (which
# rewrites the file) bumps the mtime and forces a re-parse — so live toggle
# still works without a daemon restart. Keyed by path; in production it holds
# a single entry (one SSOT file), and the mtime+size key makes a stale read
# impossible.
_ENV_FILE_STATE_CACHE: dict[str, tuple[tuple[int, int], object]] = {}


def _read_env_file_state_mtime_cached(path: str):
    try:
        st = os.stat(path)
    except OSError:
        # Missing/unreadable: drop any stale entry and let the uncached
        # reader return its fail-soft FileState(loaded=False).
        _ENV_FILE_STATE_CACHE.pop(path, None)
        return read_env_file_state(path)
    key = (st.st_mtime_ns, st.st_size)
    cached = _ENV_FILE_STATE_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    state = read_env_file_state(path)
    _ENV_FILE_STATE_CACHE[path] = (key, state)
    return state


def read_barge_in_enabled(provider: str, path: str | None = None) -> bool:
    """Read the saved barge-in toggle fresh, using the catalog default when unset."""
    if provider not in VALID_PROVIDER_IDS:
        return False
    file_state = _read_env_file_state_mtime_cached(_resolve_path(path))
    if not file_state.loaded:
        return resolve_barge_in_enabled(provider, {})
    return resolve_barge_in_enabled(provider, file_state.values)
