# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tuning-LLM key and model provisioning for the correction advisor surface.

The paid call reuses the existing ``OPENAI_API_KEY`` the household pasted at
``/voice``; no second key copy is provisioned. The key is read fresh from the
group-``jasper-secrets`` compartment file
(:data:`jasper.voice.provider_state.KEYS_FILE`) rather than from
``os.environ``, because ``jasper-correction-web.service`` sources only
``/etc/jasper/jasper.env`` — so a wizard save takes effect on the next tap with
no restart. An explicit ``OPENAI_API_KEY`` in the process env still wins. With
no key the tuning surface is hidden with a nudge, never a button that errors.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jasper.env_load import read_env_file_state
from jasper.voice.provider_state import KEYS_FILE

# Kept a literal rather than a per-provider lookup: the surface is OpenAI-only.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Config knob for the tuning model id. Absent -> DEFAULT_TUNING_LLM_MODEL.
TUNING_LLM_MODEL_ENV = "JASPER_TUNING_LLM_MODEL"

# Tracks the async-research provider's default so both text surfaces name the
# same model. Imported lazily in :func:`_default_model` so importing this module
# never pulls the research package onto the socket-activated path.
_DEFAULT_MODEL_FALLBACK = "gpt-5.4"


def _default_model() -> str:
    try:
        from jasper.research.providers.openai_research import DEFAULT_MODEL
    except ImportError:  # pragma: no cover - defensive; research pkg is in-tree
        return _DEFAULT_MODEL_FALLBACK
    model = (DEFAULT_MODEL or "").strip()
    return model or _DEFAULT_MODEL_FALLBACK


def read_openai_key(
    *,
    environ: "dict[str, str] | None" = None,
    keys_path: str = KEYS_FILE,
) -> str:
    """Resolve the OpenAI API key for the tuning advisor.

    Precedence: an explicit ``OPENAI_API_KEY`` in ``environ`` (default
    :data:`os.environ`) wins — the CI / headless / operator override —
    otherwise a fresh, fail-soft read of the group-``jasper-secrets``
    compartment file. Returns ``""`` when no key is configured. Never
    raises and never logs the key.
    """
    env = os.environ if environ is None else environ
    from_env = (env.get(OPENAI_API_KEY_ENV) or "").strip()
    if from_env:
        return from_env
    file_state = read_env_file_state(keys_path)
    if not file_state.loaded:
        return ""
    return (file_state.values.get(OPENAI_API_KEY_ENV) or "").strip()


def resolve_tuning_model(
    *,
    environ: "dict[str, str] | None" = None,
) -> str:
    """The tuning model id: ``JASPER_TUNING_LLM_MODEL`` or the default."""
    env = os.environ if environ is None else environ
    model = (env.get(TUNING_LLM_MODEL_ENV) or "").strip()
    return model or _default_model()


def tuning_llm_available(
    *,
    environ: "dict[str, str] | None" = None,
    keys_path: str = KEYS_FILE,
) -> bool:
    """True when an OpenAI key is configured, so the tuning surface may show.

    Does not validate the key with the provider.
    """
    return bool(read_openai_key(environ=environ, keys_path=keys_path))


@dataclass(frozen=True)
class TuningAvailability:
    """Whether the ``/correction/`` tuning affordance shows, and the nudge."""

    available: bool
    model: str
    nudge: str = ""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "available": self.available,
            "provider": "openai",
        }
        if self.available:
            out["model"] = self.model
        else:
            out["nudge"] = self.nudge
        return out


# The one place the "no key" copy lives.
NO_KEY_NUDGE = (
    "Add an OpenAI key at /voice to enable the tuning assistant — it "
    "explains what your room is doing and can suggest bounded tweaks."
)


def availability(
    *,
    environ: "dict[str, str] | None" = None,
    keys_path: str = KEYS_FILE,
) -> TuningAvailability:
    """Resolve the tuning-surface availability block for the envelope."""
    if tuning_llm_available(environ=environ, keys_path=keys_path):
        return TuningAvailability(
            available=True,
            model=resolve_tuning_model(environ=environ),
        )
    return TuningAvailability(
        available=False,
        model="",
        nudge=NO_KEY_NUDGE,
    )
