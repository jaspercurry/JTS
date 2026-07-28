# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Factory: pick the TTS backend that matches the active voice
provider so cues sound in the same voice the assistant uses for
live replies.

Lives here (rather than in voice_daemon.py) so both the daemon and
the `jasper-cues` CLI dispatch identically — important because
install.sh runs the CLI for regeneration, and a divergence between
CLI and daemon would mean cached cues use one provider while
runtime synthesis uses another.

Active-provider mismatch fallback: if the configured provider has
no key but a different provider does, fall back to that one — best
effort, better wrong-voice cues than silent failures. Logs a
warning so the operator notices.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .generator import (
    GEMINI_TTS_MODEL,
    TTS_MAX_ATTEMPTS,
    TTS_RETRY_BACKOFF_SEC,
    GeminiTTSGenerator,
    GrokTTSGenerator,
    OpenAITTSGenerator,
    TTSBackend,
)

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


def build_provider_tts_backend(
    cfg: "Config",
    provider: str,
    *,
    max_attempts: int | None = None,
    retry_backoff_sec: float | None = None,
) -> "tuple[TTSBackend | None, str]":
    """Build one provider's TTS backend without choosing a fallback.

    This is the provider-to-generator dispatch owner shared by cue rendering
    and assistant-loudness seeding. Optional retry bounds are used by the
    startup seed path; omitting them preserves each generator's defaults.
    """
    attempts = (
        TTS_MAX_ATTEMPTS
        if max_attempts is None
        else max(1, int(max_attempts))
    )
    backoff = (
        TTS_RETRY_BACKOFF_SEC
        if retry_backoff_sec is None
        else max(0.0, float(retry_backoff_sec))
    )

    if provider == "openai" and cfg.openai_api_key:
        voice = cfg.openai_voice
        return (
            OpenAITTSGenerator(
                api_key=cfg.openai_api_key,
                voice=voice,
                max_attempts=attempts,
                retry_backoff_sec=backoff,
            ),
            voice,
        )
    if provider == "gemini" and cfg.gemini_api_key:
        voice = cfg.gemini_voice
        return (
            GeminiTTSGenerator(
                api_key=cfg.gemini_api_key,
                voice=voice,
                model=cfg.gemini_tts_model or GEMINI_TTS_MODEL,
                max_attempts=attempts,
                retry_backoff_sec=backoff,
            ),
            voice,
        )
    if provider == "grok" and cfg.grok_api_key:
        voice = cfg.grok_voice
        return (
            GrokTTSGenerator(
                api_key=cfg.grok_api_key,
                voice=voice,
                max_attempts=attempts,
                retry_backoff_sec=backoff,
            ),
            voice,
        )
    return None, ""


def build_cue_tts_backend(
    cfg: "Config",
) -> "tuple[TTSBackend | None, str]":
    """Returns ``(backend, voice_label)``:

    - `backend` is the synthesiser passed to AudioCueManager, or
      None when no provider has a key (cue regen disabled; playback
      works off any cached files).
    - `voice_label` flows into the cue cache hash so flipping
      provider or voice automatically invalidates baked WAVs.
    """
    provider = cfg.voice_provider
    backend, voice_label = build_provider_tts_backend(cfg, provider)
    if backend is not None:
        return backend, voice_label

    # Active provider has no key — fall back to whichever provider
    # IS configured.
    for fallback, display_name in (
        ("openai", "OpenAI"),
        ("gemini", "Gemini"),
        ("grok", "Grok"),
    ):
        backend, voice_label = build_provider_tts_backend(cfg, fallback)
        if backend is None:
            continue
        logger.warning(
            "cue tts: active provider=%s has no key; falling back to "
            "%s for cue rendering",
            provider,
            display_name,
        )
        return backend, voice_label

    logger.warning(
        "cue tts: no provider key configured; cue regen disabled "
        "(playback still works off cached files)",
    )
    return None, ""
