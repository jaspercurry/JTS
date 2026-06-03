from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass


if "httpx" not in sys.modules:
    httpx = types.ModuleType("httpx")

    class _Timeout:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    httpx.Timeout = _Timeout
    sys.modules["httpx"] = httpx
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")
if "rapidfuzz" not in sys.modules:
    rapidfuzz = types.ModuleType("rapidfuzz")
    rapidfuzz.fuzz = types.SimpleNamespace()
    sys.modules["rapidfuzz"] = rapidfuzz


@dataclass(frozen=True)
class _FakeMeasurement:
    source_lufs: float
    source_peak_dbfs: float


class _FakeTts:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict]] = []

    async def write_segment(self, pcm: bytes, **kwargs) -> None:
        self.calls.append((pcm, kwargs))


def test_synthetic_audio_profile_uses_measured_source_level(monkeypatch):
    import jasper.voice_daemon as vd

    monkeypatch.setattr(
        vd,
        "measure_pcm_24k_mono",
        lambda pcm: _FakeMeasurement(source_lufs=-31.25, source_peak_dbfs=-12.0),
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

    def fail_measurement(_pcm):
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
    wl = WakeLoop.__new__(WakeLoop)
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
            },
        )
    ]
