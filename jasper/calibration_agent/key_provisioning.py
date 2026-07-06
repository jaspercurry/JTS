# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tuning-LLM key + model provisioning for the correction advisor surface.

P6 wires the calibration/tuning advisor into the ``/correction/`` flow.
The advisor is **OpenAI-shipped, provider-swappable** (revision plan
§3.4): the paid call reuses the *existing* ``OPENAI_API_KEY`` the
household already pasted at ``/voice`` when their voice provider is
OpenAI. We do NOT provision a second key copy.

**Where the key lives.** The ``/voice`` wizard writes the three provider
API keys to the group-``jasper-secrets`` compartment file
:data:`jasper.voice.provider_state.KEYS_FILE`
(``/var/lib/jasper-secrets/voice_keys.env``) — WS1 Phase 4a. Only
``jasper-voice`` + ``jasper-web`` have group read on it.

**Why read the file, not ``os.environ``.** ``/correction/`` is served by
``jasper-correction-web.service``, which is *not* one of the five Tier-A
non-root daemons — it runs as **root** and its unit sources only
``/etc/jasper/jasper.env`` via ``EnvironmentFile=``, NOT the secret
compartment. So the key is not in this process's ``os.environ``. As
root the process *can* read the compartment file directly (root bypasses
group perms), so we do a fresh, fail-soft file read here — the same
pattern :mod:`jasper.voice.provider_state` uses to read the active
provider without a restart. This also means a wizard save at ``/voice``
takes effect on the very next ``/correction/`` tap, no restart needed.

An explicit ``OPENAI_API_KEY`` in the process env (CI, headless imaging,
operator override) still wins over the file — the same escape-hatch
posture the rest of the project uses.

**Hidden, never broken.** When no OpenAI key exists (household on
Gemini/Grok voice), the tuning surface is *hidden with a nudge*
(:func:`availability`), never a button that errors when tapped.

**Model id is config, not code.** ``JASPER_TUNING_LLM_MODEL`` names the
current GPT-class text model; the default tracks the same current
flagship id the async-research provider already ships
(``jasper.research.providers.openai_research.DEFAULT_MODEL``), so a model
rename is a config-value change, not a code edit.

The ``provider`` seam stays OpenAI-only on purpose (revision plan §3.4);
:mod:`jasper.calibration_agent.model_client` already hard-rejects any
non-openai provider, and this module never widens that.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jasper.env_load import read_env_file_state
from jasper.voice.provider_state import KEYS_FILE

# The one secret we read from the voice-keys compartment. Kept a literal
# (not a per-provider lookup) because the tuning surface is OpenAI-only.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Config knob for the tuning model id. Absent -> DEFAULT_TUNING_LLM_MODEL.
TUNING_LLM_MODEL_ENV = "JASPER_TUNING_LLM_MODEL"

# The current GPT-class text flagship. Tracks the async-research
# provider's default so both text surfaces name the same current model
# and a rename is a single-source config change. Imported lazily in
# :func:`_default_model` so importing this module never pulls the
# research package (and its optional SDK) onto the socket-activated path.
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
    """The tuning model id: ``JASPER_TUNING_LLM_MODEL`` or the current
    GPT-class default. Never empty."""
    env = os.environ if environ is None else environ
    model = (env.get(TUNING_LLM_MODEL_ENV) or "").strip()
    return model or _default_model()


def tuning_llm_available(
    *,
    environ: "dict[str, str] | None" = None,
    keys_path: str = KEYS_FILE,
) -> bool:
    """True when an OpenAI key is configured, so the tuning surface may
    show. Pure availability — does not validate the key with the
    provider (a bad key fails at call time with an honest error)."""
    return bool(read_openai_key(environ=environ, keys_path=keys_path))


@dataclass(frozen=True)
class TuningAvailability:
    """Whether the ``/correction/`` tuning-assistant affordance shows,
    and the honest nudge to render when it does not."""

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


# The one place the "no key" copy lives, so the envelope block and any
# future surface read the same sentence.
_NO_KEY_NUDGE = (
    "Add an OpenAI key at /voice to enable the tuning assistant — it "
    "explains what your room is doing and can suggest bounded tweaks."
)


def availability(
    *,
    environ: "dict[str, str] | None" = None,
    keys_path: str = KEYS_FILE,
) -> TuningAvailability:
    """Resolve the tuning-surface availability block for the envelope.

    Hidden-with-nudge when no OpenAI key is configured; otherwise
    available with the resolved model id (never the key)."""
    if tuning_llm_available(environ=environ, keys_path=keys_path):
        return TuningAvailability(
            available=True,
            model=resolve_tuning_model(environ=environ),
        )
    return TuningAvailability(
        available=False,
        model="",
        nudge=_NO_KEY_NUDGE,
    )
