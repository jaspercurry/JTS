from __future__ import annotations

import logging

from ..assistant_loudness import (
    AssistantLoudnessProfile,
    measure_pcm_24k_mono,
)

logger = logging.getLogger("jasper.voice_daemon")


SYNTHETIC_AUDIO_PROFILE_PROVIDER = "jts"
SYNTHETIC_AUDIO_PROFILE_UPDATED_AT = "static"


def _synthetic_audio_profile(
    *,
    model: str,
    voice: str,
    pcm: bytes,
    fallback_source_lufs: float = -24.0,
    fallback_peak_dbfs: float = -12.0,
) -> AssistantLoudnessProfile:
    """Build source-loudness metadata for generated earcons.

    These sounds are not provider TTS, so using the active assistant
    voice profile would misdescribe their source level. Outputd still
    owns the final gain decision; this profile only tells it what
    loudness/peak the synthetic source PCM starts with.
    """
    try:
        measurement = measure_pcm_24k_mono(pcm)
        source_lufs = measurement.source_lufs
        source_peak_dbfs = measurement.source_peak_dbfs
        confidence = 1.0
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=audio.synthetic_profile result=fallback model=%s "
            "voice=%s exc_type=%s err=%s",
            model, voice, type(e).__name__, e,
        )
        source_lufs = fallback_source_lufs
        source_peak_dbfs = fallback_peak_dbfs
        confidence = 0.0
    return AssistantLoudnessProfile(
        provider=SYNTHETIC_AUDIO_PROFILE_PROVIDER,
        model=model,
        voice=voice,
        source_lufs=round(float(source_lufs), 2),
        source_peak_dbfs=round(float(source_peak_dbfs), 2),
        confidence=confidence,
        updated_at=SYNTHETIC_AUDIO_PROFILE_UPDATED_AT,
        method="synthetic_generated",
    )


def _generate_mute_click(*, going_on: bool) -> bytes:
    """Synthesize a short decay-sine click as 24 kHz int16 mono PCM
    — same shape `TtsPlayout.write()` accepts. Higher pitch on
    unmute, lower on mute, so the user gets a directional cue.

    Intentionally not a registered cue: cues are TTS-generated and
    spoken, this is a sub-100 ms synthesized blip. Kept inline so
    the cue cache / regen system isn't paid for two trivial WAVs.
    """
    import math
    sr = 24000
    dur_samples = int(sr * 0.06)  # 60 ms
    freq = 900.0 if going_on else 600.0
    peak = 0.25  # ~-12 dBFS before TtsPlayout's gain stage
    out = bytearray(dur_samples * 2)
    for i in range(dur_samples):
        t = i / sr
        env = math.exp(-t * 50.0)  # ~20 ms half-life
        s = int(math.sin(2.0 * math.pi * freq * t) * env * peak * 32767.0)
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        # little-endian int16
        out[2 * i] = s & 0xFF
        out[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(out)


def _generate_listening_chirp(*, going_on: bool) -> bytes:
    """Synthesize a two-tone listening cue as 24 kHz int16 mono PCM
    — same shape `TtsPlayout.write()` accepts. Wake = ascending
    musical fifth in the upper register (A5 880 Hz → E6 1320 Hz);
    end-of-turn = descending fifth one octave lower (E5 660 Hz →
    A4 440 Hz). Same interval shape so the pair reads as a matched
    family; distinct registers so "starting" vs "ending" lands
    without the listener having to think about it. End-chirp's
    highest note (660 Hz) sits below the wake-chirp's lowest note
    (880 Hz) so the contrast is unmistakable. Phase-continuous
    through the note change so each pair reads as one connected
    cue rather than two beeps.

    Distinct from `_generate_mute_click`: two-note interval (vs.
    single-tone decay) so start/stop listening is clearly
    different from mic mute/unmute. Inline for the same reason
    as the mute click — sub-100 ms synthesized blip, not worth
    a TTS-cached WAV.
    """
    import math
    sr = 24000
    seg_samples = int(sr * 0.035)  # 35 ms per note → 70 ms total
    total = seg_samples * 2
    ramp = int(sr * 0.005)  # 5 ms cosine attack/release
    if going_on:
        f1, f2 = 880.0, 1320.0  # wake: upper register, ascending
    else:
        f1, f2 = 660.0, 440.0   # end: lower register, descending
    peak = 0.18  # ~-15 dBFS — subtler than mute click since these fire often
    out = bytearray(total * 2)
    phase = 0.0
    for i in range(total):
        freq = f1 if i < seg_samples else f2
        phase += 2.0 * math.pi * freq / sr
        if i < ramp:
            env = 0.5 * (1.0 - math.cos(math.pi * i / ramp))
        elif i >= total - ramp:
            env = 0.5 * (1.0 - math.cos(math.pi * (total - i) / ramp))
        else:
            env = 1.0
        s = int(math.sin(phase) * env * peak * 32767.0)
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        out[2 * i] = s & 0xFF
        out[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(out)
