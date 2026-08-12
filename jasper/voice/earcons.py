# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthesized interaction earcons (wake / end-of-turn / mute / unmute).

These are the short musical cues the speaker plays around a voice
interaction. They are NOT the spoken TTS cues in `jasper.cues` — they
are tone recipes rendered to 24 kHz mono PCM once at daemon
startup (the same shape `TtsPlayout.write()` accepts), then played
fire-and-forget through outputd.

The recipes are ports of the "chime" and "sparkle" sounds from the
cuelume Web Audio palette (https://github.com/Danilaa1/cuelume):
layered sine notes, each with an exponential attack→decay envelope, over
a lowpass feedback-delay "shimmer" tail. The exponential envelope starts
and ends at near-silence, so a rendered earcon has no onset/offset step
— the source of the old mute click's roughness.

cuelume is MIT-licensed (Copyright (c) 2026 Daniel Belyi); its full
notice is preserved verbatim at `jasper/voice/CUELUME_LICENSE` and logged
in the attribution inventory `LICENSE-third-party.md`. The Python in this
module is JTS's own reimplementation (Apache-2.0) — only the sound
*designs* (note choices, envelopes, shimmer) are cuelume's.

Name → sound (the function names predate this recipe port and are kept
to avoid a rename ripple across the daemon + ~10 test files; the
`going_on` flag still means "the up-cue" when True, "the down-cue" when
False):

  _generate_listening_chirp(going_on=True)  → chime, ascending 5th (wake)
  _generate_listening_chirp(going_on=False) → chime, descending 5th one
                                              octave lower (end of turn)
  _generate_mute_click(going_on=True)   → sparkle, ascending arpeggio
                                          (unmute / assistant resumed)
  _generate_mute_click(going_on=False)  → sparkle, descending arpeggio one
                                          octave lower (mute / paused)

Each pair shares timbre + envelope and mirrors contour and register, so
"start vs end" and "on vs off" are unmistakable without the listener
having to think about it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..assistant_loudness import (
    AssistantLoudnessProfile,
    measure_pcm_24k_mono,
)
from ..log_event import log_event

logger = logging.getLogger("jasper.voice_daemon")

# 24 kHz mono — what TtsPlayout.write() / outputd accept, and the rate every
# supported voice provider streams. The SAMPLE WIDTH is the box's, not this
# module's: int16 on a narrow wire, int32 at spine scale on a wide one (see
# `_to_pcm32`).
_SR = 24000

# The narrow bake's full-scale constant promoted by the exact 2^16 the Rust
# `widen_i16_to_i32` shifts by. Deliberately NOT `2**31 - 1`.
#
# The payoff is ROUND-TRIP IDENTITY, not loudness. `2**31 - 1` would make the
# wide earcon louder by 1 part in 32768 — about 0.0003 dB, which nobody could
# hear and which would not be worth a constant on its own. What it WOULD break
# is the exactness of the two conversions the whole width axis rests on:
# `widen_i16_to_i32` and `narrow_i32_to_i16_round` are inverses only on this
# grid, so a wide bake normalized to the container's top code would no longer
# narrow back to the narrow bake, and every equivalence this module's tests
# assert — and every lossless conversion a width mismatch relies on — would
# become approximate.
_PCM32_FULL_SCALE = 32767 * 65536
_I32_MIN = -(2 ** 31)
_I32_MAX = 2 ** 31 - 1

# Final peak the rendered buffer is normalized to (~-6 dBFS). Outputd's
# loudness stage matches perceived level to the room's silence target
# regardless, so this only sets clean headroom + a healthy signal for the
# source-loudness measurement in `_synthetic_audio_profile`.
_TARGET_PEAK = 0.5

# Raised-cosine fade applied to the very end of every earcon so the final
# sample is exactly zero — belt-and-braces against a tail-truncation click
# even though the exp decay already ends near silence.
_TAIL_FADE_SEC = 0.005


SYNTHETIC_AUDIO_PROFILE_PROVIDER = "jts"
SYNTHETIC_AUDIO_PROFILE_UPDATED_AT = "static"


def _synthetic_audio_profile(
    *,
    model: str,
    voice: str,
    pcm: bytes,
    wide: bool = False,
    fallback_source_lufs: float = -24.0,
    fallback_peak_dbfs: float = -12.0,
) -> AssistantLoudnessProfile:
    """Build source-loudness metadata for generated earcons.

    These sounds are not provider TTS, so using the active assistant
    voice profile would misdescribe their source level. Outputd still
    owns the final gain decision; this profile only tells it what
    loudness/peak the synthetic source PCM starts with.

    `wide` says which width `pcm` was baked at; the measurement normalizes
    both to the same sample units, so an earcon's profile is identical on a
    narrow and a wide box.
    """
    try:
        measurement = measure_pcm_24k_mono(pcm, wide=wide)
        source_lufs = measurement.source_lufs
        source_peak_dbfs = measurement.source_peak_dbfs
        confidence = 1.0
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "audio.synthetic_profile",
            result="fallback",
            model=model,
            voice=voice,
            exc_type=type(e).__name__,
            err=str(e),
            level=logging.WARNING,
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


@dataclass(frozen=True)
class _ToneLayer:
    """One sine voice in a recipe. Mirrors cuelume's ToneLayer: an
    exponential attack→decay envelope on a single frequency, delayed by
    `offset` seconds relative to the recipe start."""
    frequency: float
    offset: float
    attack: float
    decay: float
    peak: float


@dataclass(frozen=True)
class _Shimmer:
    """A lowpass feedback-delay (echo) tail — cuelume's `shimmer`. Adds
    air and a soft ring-out so the earcon reads as "designed" rather than
    a bare beep. Purely linear, so it survives peak normalization."""
    delay: float
    feedback: float
    wet: float
    lowpass: float


@dataclass(frozen=True)
class _Recipe:
    layers: tuple[_ToneLayer, ...]
    shimmer: _Shimmer | None = field(default=None)


# --- Recipes (ported from cuelume chime/sparkle) ---
#
# Note frequencies (equal temperament): C5 523.25, G5 783.99, C6 1046.5,
# E6 1318.51, G6 1568.0, A4 440.0, C#5 554.37, E5 659.25, A5 880.0,
# A6 1760.0, C#7 2217.46, E7 2637.02, A7 3520.0.
#
# CHIME — a two-note bell. Wake is cuelume's chime verbatim: an ascending
# perfect fifth C6→G6. End-of-turn mirrors it: a descending fifth one
# octave lower (G5→C5) so "closing" reads as both downward and lower.
_CHIME_SHIMMER = _Shimmer(delay=0.12, feedback=0.25, wet=0.18, lowpass=4000.0)
_CHIME_ASCENDING = _Recipe(
    layers=(
        _ToneLayer(1046.5, offset=0.0, attack=0.006, decay=0.22, peak=0.09),
        _ToneLayer(1568.0, offset=0.09, attack=0.006, decay=0.26, peak=0.08),
    ),
    shimmer=_CHIME_SHIMMER,
)
_CHIME_DESCENDING = _Recipe(
    layers=(
        _ToneLayer(783.99, offset=0.0, attack=0.006, decay=0.22, peak=0.09),
        _ToneLayer(523.25, offset=0.09, attack=0.006, decay=0.26, peak=0.08),
    ),
    shimmer=_CHIME_SHIMMER,
)

# SPARKLE — a quick four-note arpeggio twinkle. Unmute is cuelume's
# sparkle verbatim: an ascending A-major arpeggio A6→C#7→E7→A7. Mute
# mirrors it: the same arpeggio one octave lower, descending A6→E6→C#6→A5,
# so pausing reads as downward and lands below the resume cue.
_SPARKLE_SHIMMER = _Shimmer(delay=0.07, feedback=0.35, wet=0.22, lowpass=6000.0)
_SPARKLE_ASCENDING = _Recipe(
    layers=(
        _ToneLayer(1760.0, offset=0.0, attack=0.003, decay=0.09, peak=0.045),
        _ToneLayer(2217.46, offset=0.045, attack=0.003, decay=0.09, peak=0.04),
        _ToneLayer(2637.02, offset=0.09, attack=0.003, decay=0.10, peak=0.038),
        _ToneLayer(3520.0, offset=0.135, attack=0.003, decay=0.12, peak=0.032),
    ),
    shimmer=_SPARKLE_SHIMMER,
)
_SPARKLE_DESCENDING = _Recipe(
    layers=(
        _ToneLayer(1760.0, offset=0.0, attack=0.003, decay=0.09, peak=0.045),
        _ToneLayer(1318.51, offset=0.045, attack=0.003, decay=0.09, peak=0.04),
        _ToneLayer(1108.73, offset=0.09, attack=0.003, decay=0.10, peak=0.038),
        _ToneLayer(880.0, offset=0.135, attack=0.003, decay=0.12, peak=0.032),
    ),
    shimmer=_SPARKLE_SHIMMER,
)


def _render_layers(layers: tuple[_ToneLayer, ...]) -> list[float]:
    """Sum every layer's enveloped sine into one float buffer.

    Envelope matches cuelume's exponential ramp: from near-silence up to
    `peak` over `attack`, then back down to near-silence over `decay`.
    Because both ends approach ~0, the note starts and stops with no
    amplitude step — no click. Phase runs from the note's own onset, so
    each sine begins at 0.
    """
    eps = 1e-4  # cuelume's exponentialRamp floor (~-80 dBFS)
    end_sample = 0
    for lay in layers:
        stop = int(round((lay.offset + lay.attack + lay.decay) * _SR))
        end_sample = max(end_sample, stop)
    buf = [0.0] * (end_sample + 1)
    for lay in layers:
        start = int(round(lay.offset * _SR))
        a = max(1, int(round(lay.attack * _SR)))
        d = max(1, int(round(lay.decay * _SR)))
        w = 2.0 * math.pi * lay.frequency / _SR
        up = (lay.peak / eps)
        down = (eps / lay.peak)
        for i in range(a + d):
            n = start + i
            if n >= len(buf):
                break
            if i < a:
                env = eps * (up ** (i / a))
            else:
                env = lay.peak * (down ** ((i - a) / d))
            buf[n] += math.sin(w * i) * env
    return buf


def _apply_shimmer(dry: list[float], sh: _Shimmer) -> list[float]:
    """Lowpass feedback-delay echo — cuelume's shimmer. The dry signal is
    injected into a delay line that feeds back on itself through a one-pole
    lowpass (damping), and the echo content is mixed to the output at
    `wet`. The buffer is extended so the echoes ring out to near-silence
    (bounded so a high-feedback recipe can't run away)."""
    n_dry = len(dry)
    delay = max(1, int(round(sh.delay * _SR)))
    if sh.feedback <= 0.0 or sh.wet <= 0.0:
        return list(dry)
    # Echoes until the feedback tail falls below ~-34 dB, capped at 0.25 s
    # so the cue rings out without dragging.
    n_echo = max(1, math.ceil(math.log(0.02) / math.log(sh.feedback)))
    tail = min(delay * (n_echo + 1), int(0.25 * _SR))
    total = n_dry + tail
    line = [0.0] * total  # signal circulating through the delay line
    out = [0.0] * total
    # One-pole lowpass pole for the feedback path: larger alpha = lower
    # cutoff. alpha = exp(-2*pi*fc/sr).
    alpha = math.exp(-2.0 * math.pi * sh.lowpass / _SR)
    lp = 0.0
    for n in range(total):
        d = dry[n] if n < n_dry else 0.0
        delayed = line[n - delay] if n >= delay else 0.0
        lp = (1.0 - alpha) * delayed + alpha * lp
        line[n] = d + sh.feedback * lp
        out[n] = d + sh.wet * (line[n] - d)
    return out


def _normalized(buf: list[float]):
    """Yield the recipe's samples normalized to `_TARGET_PEAK` with the short
    raised-cosine tail fade applied — the shape both bakes pack, in ±1.0 units.

    Extracted so the narrow and wide bakes share ONE normalization and ONE
    fade. The float operations happen here in exactly the order `_to_pcm16`
    always applied them, which is why the narrow bake's bytes are unchanged;
    `test_earcons_wire_width.py` pins that against a committed golden rather
    than against this argument.
    """
    peak = max((abs(x) for x in buf), default=0.0)
    scale = (_TARGET_PEAK / peak) if peak > 0.0 else 0.0
    n = len(buf)
    fade = min(int(_TAIL_FADE_SEC * _SR), n)
    for i, x in enumerate(buf):
        v = x * scale
        if i >= n - fade:
            k = n - i  # fade..1
            v *= 0.5 * (1.0 - math.cos(math.pi * k / fade))
        yield v


def _to_pcm16(buf: list[float]) -> bytes:
    """Bake to little-endian int16 — the narrow wire, byte-for-byte as shipped.

    `int(v * 32767.0)` truncates toward zero. Kept exactly, including that:
    these bytes are what every narrow box has always emitted, and rounding them
    "better" would change the earcon on the whole fleet for no reason this
    change needs.
    """
    out = bytearray(len(buf) * 2)
    for i, v in enumerate(_normalized(buf)):
        s = int(v * 32767.0)
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        out[2 * i] = s & 0xFF
        out[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(out)


def _to_pcm32(buf: list[float]) -> bytes:
    """Bake to little-endian int32 at the i32 spine scale — the wide wire.

    The scale is the narrow bake's own full-scale constant PROMOTED by the
    exact 2^16 `widen_i16_to_i32` shifts by (`_PCM32_FULL_SCALE`), not
    `i32::MAX`. That keeps the wide earcon the same signal at the same level as
    the narrow one, differing only in the bits below the S16 grid — which is
    the whole claim. Using the container's own top code instead would make the
    wide earcon a hair louder and break the equivalence the tests assert.

    Round-to-nearest saturating, per the campaign's rule for a JTS-owned
    quantizer; no dither. This is a NEW edge, so unlike `_to_pcm16` it has no
    shipped bytes to preserve.
    """
    out = bytearray(len(buf) * 4)
    for i, v in enumerate(_normalized(buf)):
        s = round(v * _PCM32_FULL_SCALE)
        if s > _I32_MAX:
            s = _I32_MAX
        elif s < _I32_MIN:
            s = _I32_MIN
        out[4 * i:4 * i + 4] = (s & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(out)


def _bake(buf: list[float], *, wide: bool) -> bytes:
    """Pack a rendered recipe onto the box's assistant wire."""
    return _to_pcm32(buf) if wide else _to_pcm16(buf)


def _render_recipe(recipe: _Recipe, *, wide: bool = False) -> bytes:
    buf = _render_layers(recipe.layers)
    if recipe.shimmer is not None:
        buf = _apply_shimmer(buf, recipe.shimmer)
    return _bake(buf, wide=wide)


def _generate_mute_click(*, going_on: bool, wide: bool = False) -> bytes:
    """Sparkle earcon as 24 kHz mono PCM at the box's wire width — the
    shape `TtsPlayout.write()` accepts. `going_on=True` (unmute / assistant
    resumed) is the ascending arpeggio; `going_on=False` (mute / paused)
    is the descending arpeggio one octave lower.

    Named `_generate_mute_click` for historical reasons — see the module
    docstring. Rendered once at startup and cached by the caller; not a
    registered TTS cue (those are spoken text)."""
    return _render_recipe(
        _SPARKLE_ASCENDING if going_on else _SPARKLE_DESCENDING, wide=wide
    )


def _generate_listening_chirp(*, going_on: bool, wide: bool = False) -> bytes:
    """Chime earcon as 24 kHz mono PCM at the box's wire width — the
    shape `TtsPlayout.write()` accepts. `going_on=True` (wake) is the ascending
    perfect fifth; `going_on=False` (end of turn) is the descending fifth
    one octave lower, so "closing" reads as downward and lower.

    Named `_generate_listening_chirp` for historical reasons — see the
    module docstring. Rendered once at startup and cached by the caller."""
    return _render_recipe(
        _CHIME_ASCENDING if going_on else _CHIME_DESCENDING, wide=wide
    )
