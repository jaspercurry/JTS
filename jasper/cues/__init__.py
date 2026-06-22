# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Audio-cue subsystem: pre-rendered TTS messages the daemon plays
when it would otherwise fall silent on a wake-blocking failure,
plus dynamic-text rendering for variable-content cues like timer
fire announcements.

See `docs/HANDOFF-audible-feedback.md` for the design and how to
add a new cue.
"""
from .registry import CUES, CueDef
from .generator import (
    GeminiTTSGenerator,
    GrokTTSGenerator,
    OpenAITTSGenerator,
    TTSBackend,
    cue_hash,
    render_template,
    write_cue,
)
from .factory import build_cue_tts_backend
from .manager import AudioCueManager

__all__ = [
    "CUES",
    "CueDef",
    "AudioCueManager",
    "GeminiTTSGenerator",
    "GrokTTSGenerator",
    "OpenAITTSGenerator",
    "TTSBackend",
    "build_cue_tts_backend",
    "cue_hash",
    "render_template",
    "write_cue",
]
