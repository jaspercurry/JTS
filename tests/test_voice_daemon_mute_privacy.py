# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mute privacy + cue observability unit tests for WakeLoop.

- mute_mic() must drop ALREADY-buffered room audio (pre-roll, acquire
  buffer, per-leg telemetry capture rings), not just future frames —
  otherwise a wake right after unmute replays pre-mute room audio into
  the turn and writes it to the wake-events corpus.
- _play_cue() must not be a silent no-op when no cue manager is
  configured: cues are the "no silent failure paths" promise, so the
  unconfigured state needs a (once-per-run) WARN in the journal.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from types import SimpleNamespace

import pytest

def _wake_loop_for_mute(tmp_path):
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._mic_muted = False
    wl._state = State.WAKE
    wl._pre_roll = deque([b"pre1", b"pre2"], maxlen=8)
    wl._acquire_buffer = deque([b"acq"], maxlen=8)
    on_ring = deque([b"on1", b"on2"], maxlen=8)
    off_ring = deque([b"off1"], maxlen=8)
    wl._legs = {
        "on": SimpleNamespace(capture_ring=on_ring),
        "off": SimpleNamespace(capture_ring=off_ring),
        "dtln": SimpleNamespace(capture_ring=None),  # leg without a ring
    }
    wl._cfg = SimpleNamespace(mic_mute_state_path=str(tmp_path / "mic_mute.env"))

    async def _noop_click(going_on: bool) -> None:
        return None

    wl._play_mute_click = _noop_click
    return wl, on_ring, off_ring


async def test_mute_clears_pre_roll_and_capture_rings(tmp_path) -> None:
    wl, on_ring, off_ring = _wake_loop_for_mute(tmp_path)

    assert await wl.mute_mic() == "ok"

    assert wl._mic_muted is True
    assert len(wl._pre_roll) == 0
    assert len(wl._acquire_buffer) == 0
    assert len(on_ring) == 0
    assert len(off_ring) == 0
    # Persisted for the next restart.
    assert "JASPER_MIC_MUTED=1" in (tmp_path / "mic_mute.env").read_text()


async def test_mute_idempotent_second_call_is_noop(tmp_path) -> None:
    wl, on_ring, _ = _wake_loop_for_mute(tmp_path)
    await wl.mute_mic()
    # Frames that leak in while muted (e.g. a race with the mic loop)
    # are not the concern of the second call — it must just return ok.
    on_ring.append(b"raced")
    assert await wl.mute_mic() == "ok"
    assert list(on_ring) == [b"raced"]


async def test_play_cue_warns_once_when_cues_unconfigured(caplog) -> None:
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._cues = None
    wl._warned_cues_unconfigured = False

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await wl._play_cue("cant_connect")
        await wl._play_cue("spend_cap_reached")

    warns = [
        r for r in caplog.records
        if "event=cue.skipped" in r.getMessage()
    ]
    assert len(warns) == 1  # once per daemon run, not per cue
    assert "cues_unconfigured" in warns[0].getMessage()
    assert "cant_connect" in warns[0].getMessage()


async def test_public_play_cue_reports_playback_failure() -> None:
    from jasper.voice_daemon import WakeLoop

    class _FakeCues:
        async def play(self, _slug: str) -> bool:
            return False

    wl = WakeLoop.for_tests()
    wl._cues = _FakeCues()

    assert await wl.play_cue("cant_connect") == "play_failed"


async def test_public_play_cue_reports_busy_when_output_active() -> None:
    from jasper.voice_daemon import WakeLoop

    class _FakeCues:
        async def play(self, _slug: str) -> bool:
            raise AssertionError("busy cue must not play")

    wl = WakeLoop.for_tests()
    wl._cues = _FakeCues()
    turn = await wl._output_gate.begin_turn()
    try:
        assert await wl.play_cue("cant_connect") == "busy"
    finally:
        await wl._output_gate.end_turn(turn)


async def test_play_cue_prepares_loudness_context_before_duck_and_play() -> None:
    from jasper.assistant_loudness import tts_envelope_lufs_for_level
    from jasper.voice_daemon import WakeLoop

    events: list[tuple[str, object]] = []

    class _Tts:
        async def prepare_assistant_context(self, **kwargs) -> None:
            events.append(("prepare", kwargs))

    class _Ducker:
        async def duck(self) -> None:
            events.append(("duck", None))

        async def restore(self) -> None:
            events.append(("restore", None))

    class _Cues:
        async def play(self, slug: str) -> bool:
            events.append(("play", slug))
            return True

    class _Volume:
        def get_listening_level(self) -> int:
            return 92

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "grok"
    wl._cfg.grok_model = "grok-voice-think-fast-1.0"
    wl._cfg.grok_voice = "eve"
    wl._tts = _Tts()
    wl._ducker = _Ducker()
    wl._cues = _Cues()
    wl._volume_coordinator = _Volume()

    assert await wl._play_cue("spend_cap_reached") is True

    assert [name for name, _ in events] == ["prepare", "duck", "play", "restore"]
    prepare = events[0][1]
    assert prepare["provider"] == "grok"
    assert prepare["model"] == "grok-voice-think-fast-1.0"
    assert prepare["voice"] == "eve"
    assert prepare["tts_envelope_lufs"] == pytest.approx(
        tts_envelope_lufs_for_level(92)
    )


async def test_dynamic_text_prepares_loudness_context_before_duck_and_speak() -> None:
    from jasper.assistant_loudness import tts_envelope_lufs_for_level
    from jasper.voice_daemon import FanInDucker, WakeLoop

    events: list[tuple[str, object]] = []

    class _Tts:
        async def prepare_assistant_context(self, **kwargs) -> None:
            events.append(("prepare", kwargs))

    class _Ducker(FanInDucker):
        def __init__(self) -> None:
            self._ducked = False

        async def duck(self) -> None:
            events.append(("duck", None))
            self._ducked = True

        async def restore(self) -> None:
            events.append(("restore", None))
            self._ducked = False

    class _Cues:
        async def speak_text(self, text: str) -> bool:
            events.append(("speak", text))
            return True

    class _Volume:
        def get_listening_level(self) -> int:
            return 64

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._cfg.gemini_model = "gemini-3.1-flash-live-preview"
    wl._cfg.gemini_voice = "Aoede"
    wl._tts = _Tts()
    wl._ducker = _Ducker()
    wl._cues = _Cues()
    wl._volume_coordinator = _Volume()

    assert await wl._play_dynamic_text("Your timer is up.") is True

    assert [name for name, _ in events] == ["prepare", "duck", "speak", "restore"]
    prepare = events[0][1]
    assert prepare["provider"] == "gemini"
    assert prepare["model"] == "gemini-3.1-flash-live-preview"
    assert prepare["voice"] == "Aoede"
    assert prepare["tts_envelope_lufs"] == pytest.approx(
        tts_envelope_lufs_for_level(64)
    )


async def test_dynamic_text_prerender_does_not_block_turn_claim() -> None:
    from jasper.voice_daemon import WakeLoop

    events: list[str] = []
    turn_task: asyncio.Task | None = None

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            nonlocal turn_task
            events.append("rendered")
            turn_task = asyncio.create_task(wl._begin_turn_output_episode())
            await asyncio.sleep(0)
            events.append(f"turn_active={wl._output_gate.active_kind}")
            return True

        async def speak_text_guarded(self, _text: str, _should_play) -> bool:
            raise AssertionError("stale dynamic text must not write")

    wl = WakeLoop.for_tests()
    wl._cues = _Cues()

    assert await wl._play_dynamic_text("Your research is ready.") is False
    assert events == ["rendered", "turn_active=turn"]
    assert turn_task is not None
    await asyncio.wait_for(turn_task, timeout=1.0)

    await wl._output_gate.end_turn(wl._turn_output_episode)
    wl._turn_output_episode = None


async def test_mute_click_prepares_loudness_context_before_write() -> None:
    from jasper.assistant_loudness import tts_envelope_lufs_for_level
    from jasper.voice_daemon import WakeLoop

    events: list[tuple[str, object]] = []

    class _Tts:
        async def prepare_assistant_context(self, **kwargs) -> None:
            events.append(("prepare", kwargs))

        async def write_segment(self, pcm: bytes, **kwargs) -> None:
            events.append(("write_segment", {"pcm": pcm, **kwargs}))

    class _Volume:
        def get_listening_level(self) -> int:
            return 77

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "openai"
    wl._cfg.openai_model = "gpt-realtime-2"
    wl._cfg.openai_voice = "marin"
    wl._tts = _Tts()
    wl._volume_coordinator = _Volume()

    await wl._play_mute_click(going_on=False)

    assert [name for name, _ in events] == ["prepare", "write_segment"]
    prepare = events[0][1]
    assert prepare["provider"] == "openai"
    assert prepare["model"] == "gpt-realtime-2"
    assert prepare["voice"] == "marin"
    assert prepare["tts_envelope_lufs"] == pytest.approx(
        tts_envelope_lufs_for_level(77)
    )
    segment = events[1][1]
    assert segment["segment_kind"] == "cue"
    assert segment["source_profile"].provider == "jts"


async def test_fanin_prepare_carries_absolute_volume_context() -> None:
    from jasper.assistant_volume import EffectiveVolumeContext
    from jasper.voice_daemon import WakeLoop

    prepares = []

    class _Tts:
        async def prepare_assistant_context(self, **kwargs) -> None:
            prepares.append(kwargs)

    class _Volume:
        def get_listening_level(self) -> int:
            return 50

        async def effective_volume_context(self):
            return EffectiveVolumeContext(-25.0, -25.0, -41.0, False)

    wl = WakeLoop.for_tests()
    wl._cfg.duck_transport = "fanin"
    wl._tts = _Tts()
    wl._volume_coordinator = _Volume()

    await wl._prepare_assistant_loudness_context()

    assert prepares[0]["canonical_volume_db"] == -25.0
    assert prepares[0]["downstream_volume_db"] == -25.0
    assert prepares[0]["context_tts_envelope_lufs"] == -41.0
    assert prepares[0]["muted"] is False


async def test_post_dsp_prepare_omits_volume_context(monkeypatch) -> None:
    from jasper.voice_daemon import WakeLoop

    prepares = []

    class _Tts:
        async def prepare_assistant_context(self, **kwargs) -> None:
            prepares.append(kwargs)

    class _Volume:
        def get_listening_level(self) -> int:
            return 50

        async def effective_volume_context(self):
            raise AssertionError("post-DSP voice must not read pre-DSP context")

    monkeypatch.setenv("JASPER_TTS_MIX_STAGE", "post_dsp")
    wl = WakeLoop.for_tests()
    wl._cfg.duck_transport = "fanin"
    wl._tts = _Tts()
    wl._volume_coordinator = _Volume()

    await wl._prepare_assistant_loudness_context()

    assert "canonical_volume_db" not in prepares[0]


async def test_mute_click_skips_when_output_active() -> None:
    from jasper.voice_daemon import WakeLoop

    class _Tts:
        async def write_segment(self, *_args, **_kwargs) -> None:
            raise AssertionError("mute click must not write during active output")

    wl = WakeLoop.for_tests()
    wl._tts = _Tts()
    turn = await wl._output_gate.begin_turn()
    try:
        await wl._play_mute_click(going_on=True)
    finally:
        await wl._output_gate.end_turn(turn)


async def test_listening_chirp_writes_inside_turn_episode() -> None:
    from jasper.voice_daemon import WakeLoop

    events: list[tuple[bytes, dict]] = []

    class _Tts:
        async def write_segment(self, pcm: bytes, **kwargs) -> None:
            events.append((pcm, kwargs))

    wl = WakeLoop.for_tests()
    wl._tts = _Tts()
    wl._chirp_on_pcm = b"wake"
    profile = object()
    wl._chirp_on_profile = profile
    turn = await wl._output_gate.begin_turn()
    try:
        await wl._play_listening_chirp(going_on=True)
    finally:
        await wl._output_gate.end_turn(turn)

    assert events == [
        (
            b"wake",
            {
                "segment_kind": "chirp",
                "source_profile": profile,
            },
        )
    ]
