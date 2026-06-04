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
    GeminiTTSGenerator,
    GrokTTSGenerator,
    OpenAITTSGenerator,
    TTSBackend,
)

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


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
    if provider == "openai" and cfg.openai_api_key:
        return (
            OpenAITTSGenerator(
                api_key=cfg.openai_api_key, voice=cfg.openai_voice,
            ),
            cfg.openai_voice,
        )
    if provider == "gemini" and cfg.gemini_api_key:
        return (
            GeminiTTSGenerator(
                api_key=cfg.gemini_api_key, voice=cfg.gemini_voice,
                model=cfg.gemini_tts_model,
            ),
            cfg.gemini_voice,
        )
    if provider == "grok" and cfg.grok_api_key:
        return (
            GrokTTSGenerator(
                api_key=cfg.grok_api_key, voice=cfg.grok_voice,
            ),
            cfg.grok_voice,
        )
    # Active provider has no key — fall back to whichever provider
    # IS configured.
    if cfg.openai_api_key:
        logger.warning(
            "cue tts: active provider=%s has no key; falling back to "
            "OpenAI for cue rendering", provider,
        )
        return (
            OpenAITTSGenerator(
                api_key=cfg.openai_api_key, voice=cfg.openai_voice,
            ),
            cfg.openai_voice,
        )
    if cfg.gemini_api_key:
        logger.warning(
            "cue tts: active provider=%s has no key; falling back to "
            "Gemini for cue rendering", provider,
        )
        return (
            GeminiTTSGenerator(
                api_key=cfg.gemini_api_key, voice=cfg.gemini_voice,
                model=cfg.gemini_tts_model,
            ),
            cfg.gemini_voice,
        )
    if cfg.grok_api_key:
        logger.warning(
            "cue tts: active provider=%s has no key; falling back to "
            "Grok for cue rendering", provider,
        )
        return (
            GrokTTSGenerator(
                api_key=cfg.grok_api_key, voice=cfg.grok_voice,
            ),
            cfg.grok_voice,
        )
    logger.warning(
        "cue tts: no provider key configured; cue regen disabled "
        "(playback still works off cached files)",
    )
    return None, ""
