# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from dataclasses import dataclass

@dataclass(frozen=True)
class _FakeMeasurement:
    source_lufs: float
    source_peak_dbfs: float


class _FakeTts:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict]] = []

    async def write_segment(self, pcm: bytes, **kwargs) -> None:
        self.calls.append((pcm, kwargs))

    async def wait_drained(self) -> None:
        return None


def test_synthetic_audio_profile_uses_measured_source_level(monkeypatch):
    import jasper.voice_daemon as vd

    monkeypatch.setattr(
        vd,
        "measure_pcm_24k_mono",
        lambda pcm, **_: _FakeMeasurement(
            source_lufs=-31.25, source_peak_dbfs=-12.0
        ),
    )

    profile = vd._synthetic_audio_profile(
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
    import jasper.voice_daemon as vd

    def fail_measurement(_pcm, **_kwargs):
        raise RuntimeError("meter failed")

    monkeypatch.setattr(vd, "measure_pcm_24k_mono", fail_measurement)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        profile = vd._synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=b"\x00\x00\x01\x00",
        )

    assert profile.confidence == 0.0
    assert "event=audio.synthetic_profile" in caplog.text
    assert "result=fallback" in caplog.text
    assert "model=synthetic-mute-click" in caplog.text
    assert "voice=mute" in caplog.text


async def test_mute_click_uses_matched_cue_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile
    from jasper.voice_daemon import WakeLoop

    profile = AssistantLoudnessProfile(
        provider="jts",
        model="synthetic-mute-click",
        voice="unmute",
        source_lufs=-30.0,
        source_peak_dbfs=-12.0,
        confidence=1.0,
        updated_at="static",
        method="synthetic_generated",
    )
    tts = _FakeTts()
    # STATED, not inherited: the bake width comes from `tts_wire_is_wide()`,
    # which reads the box's own fanin.env — absent on a test runner, and an
    # undeclared box is WIDE since #3655. The flag asserted below is this
    # value, so the test must declare it rather than read the host's.
    wl = WakeLoop.for_tests(_earcon_wide=False)
    wl._tts = tts
    wl._mute_click_on_pcm = b"on"
    wl._mute_click_off_pcm = b"off"
    wl._mute_click_on_profile = profile
    wl._mute_click_off_profile = object()

    await wl._play_mute_click(going_on=True)

    assert tts.calls == [
        (
            b"on",
            {
                "segment_kind": "cue",
                "source_profile": profile,
                # The earcon bake's width travels with its bytes. This
                # WakeLoop is DECLARED narrow above, so the flag reads False.
                "pcm_wide": False,
            },
        )
    ]


async def test_listening_chirp_uses_matched_chirp_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile
    from jasper.voice_daemon import WakeLoop

    profile = AssistantLoudnessProfile(
        provider="jts",
        model="synthetic-listening-chirp",
        voice="wake_start",
        source_lufs=-15.0,
        source_peak_dbfs=-14.9,
        confidence=1.0,
        updated_at="static",
        method="synthetic_generated",
    )
    tts = _FakeTts()
    # STATED, not inherited — see the mute-click test above.
    wl = WakeLoop.for_tests(_earcon_wide=False)
    wl._tts = tts
    wl._chirp_on_pcm = b"wake"
    wl._chirp_off_pcm = b"end"
    wl._chirp_on_profile = profile
    wl._chirp_off_profile = object()

    await wl._play_listening_chirp(going_on=True)

    assert tts.calls == [
        (
            b"wake",
            {
                "segment_kind": "chirp",
                "source_profile": profile,
                "pcm_wide": False,
            },
        )
    ]
