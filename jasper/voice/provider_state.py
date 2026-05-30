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

from ..env_load import parse_env_file
from .catalog import (
    VALID_PROVIDER_IDS,
    default_model_id,
    provider_by_id,
)

# The wizard-owned single source of truth. Same constant as
# jasper.web.voice_setup.PROVIDER_FILE — kept here so non-web consumers
# don't import the web layer just to learn the path. The path (not the
# provider value) may be overridden with JASPER_VOICE_PROVIDER_FILE,
# mirroring the wizard's own --state default. That env var is a static
# deploy constant, so reading it once is fine — only the file's
# *contents* are read fresh on every call.
PROVIDER_FILE = "/var/lib/jasper/voice_provider.env"


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
    return resolve_active_provider(parse_env_file(_resolve_path(path)))


def read_active_model(provider: str, path: str | None = None) -> str | None:
    """The model string configured for ``provider`` per the SSOT file,
    falling back to the catalog default for that provider when the file
    doesn't pin one (which matches what ``jasper-voice`` itself uses).
    ``None`` when ``provider`` is not a known provider id."""
    entry = provider_by_id(provider)
    if entry is None:
        return None
    model = (parse_env_file(_resolve_path(path)).get(entry.model_env) or "").strip()
    return model or default_model_id(provider)


def read_active_provider_and_model(
    path: str | None = None,
) -> tuple[str, str | None]:
    """Resolve provider + model in a single file read. Returns
    ``("", None)`` when no provider is configured. Otherwise
    ``(provider_id, model_string)`` where the model is the file's value
    or the catalog default for that provider."""
    env = parse_env_file(_resolve_path(path))
    provider = resolve_active_provider(env)
    if not provider:
        return "", None
    entry = provider_by_id(provider)  # not None: provider is validated
    assert entry is not None  # for type-checkers; guaranteed by resolve
    model = (env.get(entry.model_env) or "").strip()
    return provider, (model or default_model_id(provider))
