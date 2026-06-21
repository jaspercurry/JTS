# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression: _end_turn must finalize the assistant TTS segment.

The passive assistant-loudness profile (the per-provider source-LUFS
measurement fanin uses to normalize reply volume) is saved by
`TtsPlayout.end_segment()`. `_play_responses` calls it when the
provider's audio iterator closes at turn end — which OpenAI's adapter
does at response.done, but Gemini's only does on release(). Teardown
(`_end_turn_inner`) cancels the playback task BEFORE release(), so on
Gemini the cancelled task never reached end_segment() and the
measurement was silently discarded. Net effect observed on JTS3
(2026-06-11): OpenAI accumulated a calibrated profile from live
replies while Gemini never earned one, so every Gemini reply played at
fanin's louder fallback gain (`reason=fallback_profile`) — the "Gemini
is louder than OpenAI" bug.

The fix calls `self._tts.end_segment()` in `_end_turn_inner` right
after the bg-task cancel join. These tests pin that contract:

* teardown calls end_segment exactly once;
* an end_segment failure (socket gone mid-teardown) does not abort
  the rest of teardown — usage row still closes, state still flips.
"""

from __future__ import annotations

import asyncio
import sys
import types


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


class _FakeTurn:
    """Minimal LiveTurn stand-in covering the surface _end_turn reads."""

    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    async def end_input(self) -> None:
        return None

    async def release(self) -> None:
        return None

    def usage_tokens(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def usage_breakdown(self):
        return None

    def bytes_sent(self) -> int:
        return 0

    def chunks_received(self) -> int:
        return 0

    def turn_lost(self) -> bool:
        return False


class _FakeUsageStore:
    def __init__(self) -> None:
        self.close_calls = 0

    def close_session(self, session_id, in_tokens, out_tokens, usage=None):
        assert session_id is not None
        self.close_calls += 1
        return 0.0


class _RecordingTts:
    """TtsPlayout stand-in that records end_segment calls."""

    def __init__(self, *, end_segment_raises: bool = False) -> None:
        self.end_segment_calls = 0
        self._raises = end_segment_raises

    async def end_segment(self):
        self.end_segment_calls += 1
        if self._raises:
            raise OSError("fan-in socket gone")

    async def resume_content_meter(self):
        return None

    def take_paced_sec(self) -> float:
        return 0.0


def _make_wakeloop(tts: _RecordingTts):
    from jasper.voice_daemon import State, WakeLoop

    class _Noop:
        def note_voice_session(self, *_a, **_k):
            return None

        def resume(self):
            return None

    class _AsyncNoop:
        async def restore(self):
            return None

    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    wl._turn = _FakeTurn()
    wl._session_id = 7
    wl._usage_store = _FakeUsageStore()
    wl._bg_tasks = set()
    wl._peering_current_epoch = "ep-1"
    wl._user_speech_seen = True
    wl._server_vad_this_turn = False
    wl._max_silero_score_in_turn = 0.0
    wl._max_silero_raw_in_turn = 0.0
    wl._silero_aec_armed_at_ms = None
    wl._silero_raw_armed_at_ms = None
    wl._input_ended = False
    wl._ending = False

    wl._volume_coordinator = _Noop()
    wl._content_activity = _Noop()
    wl._ducker = _AsyncNoop()
    wl._tts = tts

    async def _noop_stage(_stage):
        await asyncio.sleep(0)

    async def _noop_outcome(_outcome, _detail=None):
        return None

    async def _noop_peering(_reason):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._telemetry_stage = _noop_stage
    wl._telemetry_outcome = _noop_outcome
    wl._notify_peering_session_ended = _noop_peering
    wl._play_listening_chirp = _noop_chirp
    return wl


def test_teardown_calls_end_segment_once():
    """Teardown finalizes the TTS segment after cancelling playback.

    This is what saves the passive loudness measurement for providers
    whose audio iterator (and so _play_responses' own end_segment call)
    is still open when the turn is torn down — the Gemini shape.
    """
    tts = _RecordingTts()
    wl = _make_wakeloop(tts)

    asyncio.run(wl._end_turn())

    assert tts.end_segment_calls == 1


def test_teardown_survives_end_segment_failure():
    """A failing end_segment (e.g. fan-in socket gone) must not abort
    the rest of teardown: usage row still closes, state flips to WAKE."""
    from jasper.voice_daemon import State

    tts = _RecordingTts(end_segment_raises=True)
    wl = _make_wakeloop(tts)

    asyncio.run(wl._end_turn())

    assert tts.end_segment_calls == 1
    assert wl._usage_store.close_calls == 1
    assert wl._state is State.WAKE
    assert wl._turn is None
