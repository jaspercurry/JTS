# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synthesis contract for the interaction earcons.

These pin the promises made in jasper/voice/earcons.py's docstring:
the rendered PCM has no onset/offset step (the old mute-click roughness),
never clips, and the up-cue vs down-cue of each family are distinct. If
the recipes are retuned, these still hold — they assert shape, not exact
samples.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

import pytest

from jasper.voice.earcons import (
    _generate_listening_chirp,
    _generate_mute_click,
    _synthetic_audio_profile,
)

_SR = 24000


def _samples(pcm: bytes) -> list[int]:
    assert len(pcm) % 2 == 0, "PCM must be whole int16 frames"
    return list(struct.unpack("<%dh" % (len(pcm) // 2), pcm))


ALL_EARCONS = [
    ("chime_ascending", lambda: _generate_listening_chirp(going_on=True)),
    ("chime_descending", lambda: _generate_listening_chirp(going_on=False)),
    ("sparkle_ascending", lambda: _generate_mute_click(going_on=True)),
    ("sparkle_descending", lambda: _generate_mute_click(going_on=False)),
]


@pytest.mark.parametrize("name,render", ALL_EARCONS, ids=[n for n, _ in ALL_EARCONS])
def test_earcon_is_valid_int16_pcm(name: str, render) -> None:
    pcm = render()
    assert isinstance(pcm, bytes)
    assert len(pcm) > 0
    # decodes as whole int16 frames
    _samples(pcm)


@pytest.mark.parametrize("name,render", ALL_EARCONS, ids=[n for n, _ in ALL_EARCONS])
def test_earcon_no_onset_or_offset_transient(name: str, render) -> None:
    """The exponential envelope + tail fade must start and end at silence
    so mixing the earcon in doesn't inject a click. This is the specific
    regression the recipe port fixes vs the old truncated mute click."""
    s = _samples(render())
    # Starts and ends at exactly zero (no DC step), and rises from / falls
    # to silence continuously: the adjacent-sample delta near each boundary
    # is small, so there is no jump-to-amplitude step. (We check slew near
    # the edges, not absolute level — a fast exp attack legitimately reaches
    # peak within a few ms, but never in a single sample.)
    assert s[0] == 0
    assert s[-1] == 0
    w = int(_SR * 0.0005)  # 0.5 ms
    head_slew = max(abs(s[i + 1] - s[i]) for i in range(w))
    tail_slew = max(abs(s[-i - 1] - s[-i - 2]) for i in range(w))
    assert head_slew < 500, "onset steps instead of ramping from silence"
    assert tail_slew < 500, "offset steps instead of ramping to silence"


@pytest.mark.parametrize("name,render", ALL_EARCONS, ids=[n for n, _ in ALL_EARCONS])
def test_earcon_does_not_clip(name: str, render) -> None:
    """Normalized to ~-6 dBFS, so nothing should reach the int16 rails."""
    s = _samples(render())
    peak = max(abs(v) for v in s)
    assert 0 < peak < 32767, "earcon must be audible and below full-scale"
    # Normalization targets -6 dBFS (~16384); allow rounding slack.
    assert peak <= 16600


@pytest.mark.parametrize("name,render", ALL_EARCONS, ids=[n for n, _ in ALL_EARCONS])
def test_earcon_duration_reasonable(name: str, render) -> None:
    """Short musical cues, not drones — bounded so a recipe change can't
    silently ship a multi-second earcon onto the wake path."""
    dur = (len(render()) // 2) / _SR
    assert 0.1 < dur < 1.2


def test_up_and_down_cues_differ() -> None:
    """Ascending and descending members of each family are distinct
    renders, so start/end and on/off are audibly different."""
    assert _generate_listening_chirp(going_on=True) != _generate_listening_chirp(
        going_on=False
    )
    assert _generate_mute_click(going_on=True) != _generate_mute_click(
        going_on=False
    )


def test_families_are_distinct() -> None:
    """Chime and sparkle are different sounds, not the same recipe."""
    assert _generate_listening_chirp(going_on=True) != _generate_mute_click(
        going_on=True
    )


def test_render_is_deterministic() -> None:
    """Pre-rendered once at startup and cached — must be pure."""
    assert _generate_listening_chirp(going_on=True) == _generate_listening_chirp(
        going_on=True
    )
    assert _generate_mute_click(going_on=False) == _generate_mute_click(
        going_on=False
    )


@dataclass(frozen=True)
class _FakeMeasurement:
    source_lufs: float
    source_peak_dbfs: float


def test_synthetic_audio_profile_uses_measured_source_level(monkeypatch):
    import jasper.voice.earcons as earcons

    monkeypatch.setattr(
        earcons,
        "measure_pcm_24k_mono",
        lambda pcm, **_: _FakeMeasurement(
            source_lufs=-31.25, source_peak_dbfs=-12.0
        ),
    )

    profile = _synthetic_audio_profile(
        model="synthetic-mute-click",
        voice="mute",
        pcm=b"\x00\x00\x01\x00",
    )

    assert profile.provider == "jts"
    assert profile.model == "synthetic-mute-click"
    assert profile.voice == "mute"
    assert profile.source_lufs == -31.25
    assert profile.source_peak_dbfs == -12.0
    assert profile.confidence == 1.0
    assert profile.method == "synthetic_generated"


def test_synthetic_audio_profile_fallback_log_is_structured(
    monkeypatch,
    caplog,
):
    import jasper.voice.earcons as earcons

    def fail_measurement(_pcm, **_kwargs):
        raise RuntimeError("meter failed")

    monkeypatch.setattr(earcons, "measure_pcm_24k_mono", fail_measurement)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=b"\x00\x00\x01\x00",
        )

    assert profile.confidence == 0.0
    assert "event=audio.synthetic_profile" in caplog.text
    assert "result=fallback" in caplog.text
    assert "model=synthetic-mute-click" in caplog.text
    assert "voice=mute" in caplog.text
