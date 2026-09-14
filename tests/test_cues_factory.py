# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Catalog ↔ cue-TTS-factory completeness contract.

AGENTS.md promises that cue WAVs are baked from the *active provider's*
TTS endpoint so cues sound in the assistant's own voice, and the
"adding a fourth provider" checklist includes a cue TTS generator path.
``build_provider_tts_backend`` owns the necessary provider-specific
constructors, and ``build_cue_tts_backend`` adds the cue fallback policy.
A provider added to ``jasper/voice/catalog.py`` without a constructor branch
would not error — it would silently hit the wrong-voice fallback chain (or
disable cue regen) even with its key configured.
This pins the N-way completeness: for every catalog provider with only
its own key configured, the factory must return a backend without
taking the "falling back" path.
"""
from __future__ import annotations

import asyncio
import logging
import wave
from types import SimpleNamespace

from jasper.cues import manager as manager_mod
from jasper.cues.factory import build_cue_tts_backend, build_env_cue_manager
from jasper.cues.generator import CHIME_MODEL, TTS_MODEL, WAV_RATE
from jasper.cues.registry import VOICE_NOT_SET_UP_CUE_SLUG, find as find_cue
from jasper.voice.catalog import PROVIDERS

from tests._playout import FakeTts


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

        degraded = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert backend is not None and not degraded, (
            f"provider {provider.id!r} (with only its own key configured) "
            "did not get a first-class branch in "
            "jasper/cues/factory.py:build_provider_tts_backend — cues would "
            "bake in a fallback provider's voice (or not at all). Add the "
            "dispatch branch per the 'adding a fourth provider' checklist."
        )
        assert voice_label == "TestVoice", (
            f"provider {provider.id!r}: factory returned voice label "
            f"{voice_label!r}, not the active provider's configured voice"
        )


def test_env_cue_manager_bakes_and_plays_a_chime_with_no_provider_configured(
    tmp_path, monkeypatch,
):
    """NN-6 (issue #4814): a genuinely fresh box — no JASPER_VOICE_PROVIDER
    at all — must still produce an audible park cue, not silence.
    `build_env_cue_manager` is the shared choke point behind both
    `jasper-cues regenerate` and the daemon's boot-park player."""
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://jts.local")
    monkeypatch.setattr("jasper.cues.factory.load_env_files", lambda *_: None)

    mgr = build_env_cue_manager(warn=lambda _msg: None)

    # The chime's cache-key model is its own token, distinct from any real
    # provider's — so configuring a provider later misses this cache entry
    # and re-bakes real speech over it instead of reusing the chime's hash.
    assert CHIME_MODEL != TTS_MODEL

    written = mgr.regenerate(slug=VOICE_NOT_SET_UP_CUE_SLUG)
    assert written == [VOICE_NOT_SET_UP_CUE_SLUG]

    cue = find_cue(VOICE_NOT_SET_UP_CUE_SLUG)
    wav_path = mgr.expected_path(cue)
    with wave.open(wav_path, "rb") as f:
        assert f.getnframes() > 0
        assert f.getframerate() == WAV_RATE

    tts = FakeTts()
    mgr.attach_tts(tts)
    ok = asyncio.run(mgr.play(VOICE_NOT_SET_UP_CUE_SLUG))
    assert ok is True
    assert len(tts.writes) == 1

    snap = mgr.snapshot()
    assert snap["last"] == {
        "outcome": manager_mod.OUTCOME_DELIVERED,
        "reason": manager_mod.REASON_OK,
        "slug": VOICE_NOT_SET_UP_CUE_SLUG,
        "age_seconds": snap["last"]["age_seconds"],
    }
