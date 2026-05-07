"""Audio-cue subsystem: pre-rendered TTS messages the daemon plays
when it would otherwise fall silent on a wake-blocking failure.

See `docs/HANDOFF-audible-feedback.md` for the design and how to
add a new cue.
"""
from .registry import CUES, CueDef
from .generator import (
    GeminiTTSGenerator,
    cue_hash,
    render_template,
    write_cue,
)
from .manager import AudioCueManager

__all__ = [
    "CUES",
    "CueDef",
    "AudioCueManager",
    "GeminiTTSGenerator",
    "cue_hash",
    "render_template",
    "write_cue",
]
