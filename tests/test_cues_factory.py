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

import pytest

from jasper.cues import manager as manager_mod
from jasper.cues.factory import build_cue_tts_backend, build_env_cue_manager
from jasper.cues.generator import CHIME_MODEL, GEMINI_TTS_MODEL, WAV_RATE
from jasper.cues.registry import CUES, VOICE_NOT_SET_UP_CUE_SLUG, find as find_cue
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

    mgr = build_env_cue_manager(warn=lambda _msg, **_kw: None)

    # The chime's cache-key model is its own token, distinct from any real
    # provider's — so configuring a provider later misses this cache entry
    # and re-bakes real speech over it instead of reusing the chime's hash.
    assert CHIME_MODEL != GEMINI_TTS_MODEL

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


def test_env_cue_manager_does_not_chime_for_a_config_error_other_than_no_provider(
    tmp_path, monkeypatch,
):
    """Adversarial follow-up to issue #4814: a provider IS configured, but
    some OTHER config value is invalid (JASPER_WAKE_THRESHOLD out of range
    raises VoiceConfigError, not VoiceProviderNotConfigured). This must NOT
    substitute a chime — that would silently mask a real misconfiguration
    — and must not touch any already-cached WAV. Only VoiceProviderNotConfigured
    degrades to the chime."""
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("JASPER_WAKE_THRESHOLD", "2.0")  # outside [0.0, 1.0]
    monkeypatch.setenv("JASPER_SOUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("JASPER_MANAGEMENT_URL", "https://jts.local")
    monkeypatch.setattr("jasper.cues.factory.load_env_files", lambda *_: None)

    cue = find_cue("cant_connect")
    existing = tmp_path / f"{cue.slug}-realprovider01.wav"
    existing.write_bytes(b"REAL SPOKEN AUDIO")

    mgr = build_env_cue_manager(warn=lambda _msg, **_kw: None)
    with pytest.raises(RuntimeError):
        mgr.regenerate()

    assert existing.read_bytes() == b"REAL SPOKEN AUDIO"
    assert [p.name for p in tmp_path.iterdir()] == [existing.name]


def test_chime_bake_fills_holes_only_and_never_touches_an_existing_wav(tmp_path):
    """Adversarial follow-up to issue #4814: even when the chime backend IS
    selected (e.g. a transient VoiceProviderNotConfigured from a secrets
    file unreadable without sudo, on a box that already has real spoken
    cues cached), regenerate() must never overwrite or prune a WAV that
    already exists for a slug — a chime only ever fills a hole."""
    from jasper.cues.generator import ChimeTTSGenerator
    from jasper.cues.manager import AudioCueManager

    cue = find_cue("cant_connect")
    existing = tmp_path / f"{cue.slug}-realprovider01.wav"
    existing.write_bytes(b"REAL SPOKEN AUDIO")

    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="chime",
        backend=ChimeTTSGenerator(),
    )
    written = mgr.regenerate()

    assert cue.slug not in written
    assert existing.read_bytes() == b"REAL SPOKEN AUDIO"
    assert set(written) == {c.slug for c in CUES if c.slug != cue.slug}
