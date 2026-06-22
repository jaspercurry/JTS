# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Catalog ↔ cue-TTS-factory completeness contract.

AGENTS.md promises that cue WAVs are baked from the *active provider's*
TTS endpoint so cues sound in the assistant's own voice, and the
"adding a fourth provider" checklist includes a cue TTS generator path.
``build_cue_tts_backend`` dispatches on hardcoded ``provider == "<id>"``
branches, so a provider added to ``jasper/voice/catalog.py`` without a
factory branch would not error — it would silently hit the wrong-voice
fallback chain (or disable cue regen) even with its key configured.
This pins the N-way completeness: for every catalog provider with only
its own key configured, the factory must return a backend without
taking the "falling back" path.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from jasper.cues.factory import build_cue_tts_backend
from jasper.voice.catalog import PROVIDERS


def _cfg_for(active_id: str) -> SimpleNamespace:
    """A Config stand-in with only the active provider's key set.

    Attribute names follow the established convention (see
    ``test_provider_key_accepts_each_catalog_provider``):
    ``{provider_id}_api_key`` / ``{provider_id}_voice`` on Config.
    A new provider whose factory branch needs an extra Config field
    (like gemini's ``gemini_tts_model``) will fail here with an
    AttributeError — extend this helper alongside the factory branch.
    """
    attrs: dict[str, str] = {"voice_provider": active_id}
    for provider in PROVIDERS:
        slug = provider.id.replace("-", "_")
        attrs[f"{slug}_api_key"] = "test-key" if provider.id == active_id else ""
        attrs[f"{slug}_voice"] = "TestVoice"
    attrs["gemini_tts_model"] = "test-tts-model"
    return SimpleNamespace(**attrs)


def test_every_catalog_provider_has_a_cue_tts_dispatch_branch(caplog) -> None:
    for provider in PROVIDERS:
        with caplog.at_level(logging.WARNING, logger="jasper.cues.factory"):
            caplog.clear()
            backend, voice_label = build_cue_tts_backend(_cfg_for(provider.id))

        fellback = [r for r in caplog.records if "falling back" in r.getMessage()]
        assert backend is not None and not fellback, (
            f"provider {provider.id!r} (with only its own key configured) "
            "did not get a first-class branch in "
            "jasper/cues/factory.py:build_cue_tts_backend — cues would "
            "bake in a fallback provider's voice (or not at all). Add the "
            "dispatch branch per the 'adding a fourth provider' checklist."
        )
        assert voice_label == "TestVoice", (
            f"provider {provider.id!r}: factory returned voice label "
            f"{voice_label!r}, not the active provider's configured voice"
        )
