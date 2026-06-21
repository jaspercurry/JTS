# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth reader for the *active voice provider*.

The active provider is persisted by the ``/voice`` wizard to
``/var/lib/jasper/voice_provider.env`` (key ``JASPER_VOICE_PROVIDER``,
one of the ids in :data:`jasper.voice.catalog.VALID_PROVIDER_IDS`).
Per the project contract there is **no fallback default**: an unset
value means "no provider configured yet", and every surface must render
that honestly (empty / "not configured") rather than guessing a
provider.

This module is the ONE place that resolves "which provider is active"
for *display/aggregation* consumers — chiefly ``jasper-control``'s
``/state`` and ``/system`` dashboard. It deliberately re-reads the file
on each call so a wizard save is reflected immediately, **without
restarting the long-lived jasper-control daemon**. That mirrors the
home-assistant status block in :mod:`jasper.control.server`, which
re-reads its env file fresh for exactly the same reason.

Why not ``os.environ``: long-lived daemons load
``voice_provider.env`` as a systemd ``EnvironmentFile=`` at *process
start*, so ``os.environ['JASPER_VOICE_PROVIDER']`` is frozen for the
process lifetime. Only ``jasper-voice`` is restarted on a provider
switch, so any other process reading ``os.environ`` shows the previous
provider until it happens to restart. That was the stale-``/system/``
bug this module exists to prevent.

``jasper.config.Config.from_env`` remains the resolver for the
*running* daemon (``jasper-voice``), whose environment is always fresh
because it is restarted on every switch. This module is for the
processes that are **not** restarted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from ..env_load import read_env_file_state
from .catalog import (
    VALID_PROVIDER_IDS,
    default_model_id,
    provider_by_id,
)

# The wizard-owned single source of truth for active-provider state. The
# path (not the provider value) may be overridden with
# JASPER_VOICE_PROVIDER_FILE, mirroring the wizard's own --state default.
# That env var is a static deploy constant, so reading it once is fine —
# only the file's *contents* are read fresh on every call.
#
# WS1 Phase 4a — this file is deliberately KEPT broad (group `jasper`,
# under the /var/lib/jasper StateDirectory). It holds only the
# non-secret selectors (JASPER_VOICE_PROVIDER + the per-provider model /
# voice). The high-value API keys live separately in KEYS_FILE below, in
# the group-`jasper-secrets` dir that only jasper-voice + jasper-web can
# read. So jasper-control keeps reading the active provider/model here for
# /system/ (this module) without gaining access to the LLM keys. See
# docs/HANDOFF-privilege-separation.md "Phase 4".
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"

# WS1 Phase 4a — the three provider API keys (GEMINI/OPENAI/XAI) split out
# of PROVIDER_FILE into a sibling secret dir narrowed to the
# `jasper-secrets` group {jasper-voice, jasper-web}. The /voice wizard
# writes it; jasper-voice + jasper-web source it via EnvironmentFile.
# Outside the /var/lib/jasper StateDirectory on purpose — systemd's
# recursive StateDirectory chown would otherwise force its group back to
# `jasper`, re-exposing the keys to every jasper daemon. NOT read by this
# module (jasper-control has no business reading the keys).
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


def read_active_model(provider: str, path: str | None = None) -> str | None:
    """The model string configured for ``provider`` per the SSOT file,
    falling back to the catalog default for that provider when the file
    is readable but doesn't pin one (which matches what ``jasper-voice``
    itself uses). ``None`` when ``provider`` is not a known provider id
    or the SSOT file cannot be read."""
    entry = provider_by_id(provider)
    if entry is None:
        return None
    file_state = read_env_file_state(_resolve_path(path))
    if not file_state.loaded:
        return None
    model = (file_state.values.get(entry.model_env) or "").strip()
    return model or default_model_id(provider)


def read_active_provider_and_model(
    path: str | None = None,
) -> tuple[str, str | None]:
    """Resolve provider + model in a single file read. Returns
    ``("", None)`` when no provider is configured. Otherwise
    ``(provider_id, model_string)`` where the model is the file's value
    or the catalog default for that provider."""
    state = read_active_provider_state(path)
    return state.provider, state.model
