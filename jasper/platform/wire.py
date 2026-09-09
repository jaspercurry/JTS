# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The command vocabulary of the fan-in, mux, TTS and voice control sockets.

One formatter per verb, so a wire word is spelled once on the Python side and
the readers it must match are a single grep away:

* fan-in's source gate — ``rust/jasper-fanin/src/state.rs``;
* the TTS playout verbs — ``rust/jasper-tts-protocol/src/lib.rs``, the parser
  both fan-in and outputd serve the playout connection with;
* mux's own verbs — ``jasper/mux.py``'s command handler;
* the voice daemon's external-IPC verbs — ``jasper/voice/control_socket.py``.

Formatters return the command LINE without its terminator; :func:`encode` adds
it. Callers that hand the line to :mod:`jasper.platform.uds` pass the string —
that client terminates and encodes it itself.
"""
from __future__ import annotations

from typing import Protocol, Sequence


class _EffectiveVolumeContext(Protocol):
    """The five fields ``_volume_context_tokens`` reads — a structural stand-in
    for ``jasper.assistant_volume.EffectiveVolumeContext`` so this bottom-layer
    module carries no upward import; callers pass the real dataclass."""

    canonical_db: float
    downstream_db: float
    tts_envelope_lufs: float
    muted: bool
    stamp_boot_ns: int


def encode(command: str) -> bytes:
    """The on-wire form of one command line."""
    return (command + "\n").encode("ascii")


# Every JTS daemon answers a diagnostic snapshot to the same word.
STATUS = "STATUS"

# --- jasper-fanin control socket: mux's source gate ---------------------

FANIN_NONE = "NONE"


def fanin_select(label: str) -> str:
    """Gate fan-in's mix to one input lane."""
    return f"SELECT {label}"


def fanin_lane_mute(label: str, *, muted: bool) -> str:
    """Silence or unsilence one lane at its mix stage, orthogonal to SELECT."""
    return f"{'MUTE' if muted else 'UNMUTE'} {label}"


# --- jasper-mux control socket ------------------------------------------

MUX_AUTO = "AUTO"


def mux_select(source: str) -> str:
    return f"SELECT {source}"


def mux_preempt(source: str) -> str:
    return f"PREEMPT {source}"


def mux_test_select(label: str, owner: str) -> str:
    return f"TEST_SELECT {label} {owner}"


def mux_test_release(owner: str) -> str:
    return f"TEST_RELEASE {owner}"


# --- TTS playout socket (fan-in or outputd, per the resolved route) -----

TTS_CLOSE = "CLOSE"
TTS_FLUSH_SYNC = "FLUSH_SYNC"
TTS_SEGMENT_END = "SEGMENT_END"
TTS_CONTENT_METER_PAUSE = "CONTENT_METER_PAUSE"
TTS_CONTENT_METER_RESUME = "CONTENT_METER_RESUME"

# The payload verb DECLARES the connection's sample width: "AUDIO" is S16LE,
# "AUDIO32" is S32LE at spine scale. See `TtsWireWidth` in
# rust/jasper-tts-protocol/src/lib.rs.
TTS_AUDIO_NARROW = "AUDIO"
TTS_AUDIO_WIDE = "AUDIO32"


def tts_program_duck(on: bool) -> str:
    """Switch fan-in's program-lane attenuation. Depth is fan-in's."""
    return "PROGRAM_DUCK_ON" if on else "PROGRAM_DUCK_OFF"


def tts_gain(db: float) -> str:
    return f"GAIN {db:.3f}"


def tts_audio(verb: str, byte_count: int) -> str:
    """Header for one payload chunk; ``verb`` is TTS_AUDIO_NARROW/_WIDE."""
    return f"{verb} {byte_count}"


def tts_segment_start(
    kind: str, provider_item_id: str, profile_tokens: Sequence[str] | None,
) -> str:
    parts = ["SEGMENT_START", kind, provider_item_id]
    if profile_tokens is not None:
        parts.extend(profile_tokens)
    return " ".join(parts)


def _volume_context_tokens(context: _EffectiveVolumeContext) -> list[str]:
    """The five absolute fields, in the order both verbs carry them."""
    return [
        f"{context.canonical_db:.3f}",
        f"{context.downstream_db:.3f}",
        f"{context.tts_envelope_lufs:.3f}",
        "1" if context.muted else "0",
        str(int(context.stamp_boot_ns)),
    ]


def tts_volume_context(context: _EffectiveVolumeContext) -> str:
    return "VOLUME_CONTEXT " + " ".join(_volume_context_tokens(context))


def tts_prepare_assistant(
    *,
    provider: str,
    model: str,
    voice: str,
    tts_envelope_lufs: float,
    volume_context: _EffectiveVolumeContext | None = None,
) -> str:
    parts: list[str] = [
        "PREPARE_ASSISTANT",
        provider,
        model,
        voice,
        f"{float(tts_envelope_lufs):.2f}",
    ]
    if volume_context is not None:
        parts.extend(_volume_context_tokens(volume_context))
    return " ".join(parts)


# --- jasper-voice control socket (external IPC into the wake loop) ------

VOICE_END = "END"
VOICE_MEASURE_PAUSE = "MEASURE_PAUSE"
VOICE_MEASURE_RESUME = "MEASURE_RESUME"


def voice_start(source: str | None = None) -> str:
    """Manual session start (long-press begin). ``source`` names the input
    for a push-to-talk speaker; omitted, the daemon uses its default mic."""
    return f"START {source}" if source else "START"


def voice_cue_play(slug: str) -> str:
    """Play a registered audio cue through the daemon's fan-in-backed
    TtsPlayout, so a caller doesn't have to recreate the output path."""
    return f"CUE_PLAY {slug}"


def voice_mic_mute(muted: bool) -> str:
    """User-driven mic mute/unmute at the wake-loop gate."""
    return "MUTE" if muted else "UNMUTE"
