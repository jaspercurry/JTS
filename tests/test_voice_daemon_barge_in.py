"""In-session barge-in detection (the provider-agnostic spine, PR-2).

These pin the felt behaviour without hardware: while the assistant is
speaking (``_input_ended`` set), a sustained run of speech on the
AEC-cleaned mic leg flushes local TTS via the turn's interrupt event.

The safety contract under test:

  * DEFAULT OFF => byte-identical to the old "drop the mic during
    playback" behaviour: no VAD scoring, no interrupt, no audio forward.
  * Flag ON => synthetic high-Silero frames trip ``request_local_interrupt``
    once a sustained run accumulates, and only then.
  * Self-interrupt guard: barge-in requested on a profile with no AEC
    reference (direct_mic) hard-disables for the turn and WARNs once,
    rather than self-trip on un-cancelled TTS bleed.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types as _types

import numpy as np

if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")

# jasper.voice_daemon -> audio_io imports sounddevice at module scope, so the
# stub above must run before the import. The helpers below import jasper
# lazily (function scope) to keep that ordering without a module-top lint
# suppression.


class _SpyTurn:
    """LiveTurn stand-in exposing just the barge-in + forward surface."""

    def __init__(self) -> None:
        self._interrupt_event = asyncio.Event()
        self._interrupted = False
        self.local_interrupt_calls = 0
        self.send_audio_calls = 0

    def request_local_interrupt(self) -> None:
        self.local_interrupt_calls += 1
        self._interrupted = True
        self._interrupt_event.set()

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    async def send_audio(self, _data) -> None:
        self.send_audio_calls += 1


class _FixedVad:
    """Silero stand-in returning a fixed probability + a predict counter."""

    def __init__(self, score: float) -> None:
        self.score = score
        self.predict_calls = 0

    def predict(self, _frame) -> float:
        self.predict_calls += 1
        return self.score

    def reset(self) -> None:
        return None


def _frame() -> np.ndarray:
    return np.zeros(1280, dtype=np.int16)


def _playback_loop(*, score: float, active: bool, ref_ok: bool = True):
    """A WakeLoop parked mid-playback (``_input_ended`` set)."""
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    wl._turn = _SpyTurn()
    wl._vad = _FixedVad(score)
    wl._bg_tasks = set()
    wl._input_ended = True
    wl._barge_in_active = active
    wl._barge_in_reference_available = ref_ok
    wl._barge_in_run_started_at = 0.0
    wl._barge_in_run_peak = 0.0
    wl._barge_in_signalled_this_run = False
    return wl


# --- DEFAULT OFF: byte-identical drop ----------------------------------


def test_flag_off_frame_after_input_ended_is_dropped_exactly():
    """Pinning test: with barge-in disabled, a frame arriving after
    ``_input_ended`` is dropped exactly as before — the VAD is never
    scored, no interrupt is raised, and nothing is forwarded."""
    wl = _playback_loop(score=0.99, active=False)
    turn = wl._turn
    vad = wl._vad

    asyncio.run(wl._handle_session_frame(_frame()))

    assert vad.predict_calls == 0
    assert turn.local_interrupt_calls == 0
    assert turn.send_audio_calls == 0
    assert not turn._interrupt_event.is_set()
    # Run state untouched — the playback branch was never entered.
    assert wl._barge_in_run_started_at == 0.0


# --- Flag ON: sustained run trips the interrupt ------------------------


def test_flag_on_single_frame_does_not_trip():
    """One supra-threshold frame starts a run but does not (yet) flush —
    the sustained-arming window must elapse first."""
    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turn

    asyncio.run(wl._handle_session_frame(_frame()))

    assert turn.local_interrupt_calls == 0
    assert not turn._interrupt_event.is_set()
    assert wl._barge_in_run_started_at != 0.0  # run armed


def test_flag_on_sustained_run_trips_interrupt():
    """Once the run has lasted >= the arming window, a further
    supra-threshold frame sets the turn's interrupt event exactly once."""
    from jasper.voice_daemon import BARGE_IN_SUSTAINED_SPEECH_SEC

    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turn

    async def drive() -> None:
        await wl._handle_session_frame(_frame())  # arms the run
        # Simulate the arming window elapsing without real sleeps.
        wl._barge_in_run_started_at -= BARGE_IN_SUSTAINED_SPEECH_SEC + 0.05
        await wl._handle_session_frame(_frame())  # now sustained -> trip
        await wl._handle_session_frame(_frame())  # one-shot: no re-trigger

    asyncio.run(drive())

    assert turn.local_interrupt_calls == 1
    assert turn._interrupt_event.is_set()


def test_barge_in_telemetry_surfaces_through_session_status():
    """A fired barge-in increments the daemon-lifetime counters that
    /state.voice.barge_in pulls through from session_status."""
    from jasper.voice_daemon import BARGE_IN_SUSTAINED_SPEECH_SEC

    wl = _playback_loop(score=0.9, active=True)

    base = wl.session_status()
    assert base["barge_in_count_session"] == 0
    assert base["barge_in_last_at"] is None
    assert base["barge_in_last_leg"] is None

    async def drive() -> None:
        await wl._handle_session_frame(_frame())  # arm
        wl._barge_in_run_started_at -= BARGE_IN_SUSTAINED_SPEECH_SEC + 0.05
        await wl._handle_session_frame(_frame())  # trip

    asyncio.run(drive())

    fired = wl.session_status()
    assert fired["barge_in_count_session"] == 1
    assert fired["barge_in_last_leg"] == "on"
    assert isinstance(fired["barge_in_last_at"], str) and fired["barge_in_last_at"]


def test_flag_on_subthreshold_breaks_run():
    """A sub-threshold frame resets the run so a stale anchor can't trip
    later, and re-arms the one-shot for a fresh run."""
    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turn

    async def drive() -> None:
        await wl._handle_session_frame(_frame())  # arm
        wl._barge_in_run_started_at -= 1.0  # would trip on next supra frame
        wl._vad.score = 0.1  # ...but a quiet frame lands first
        await wl._handle_session_frame(_frame())

    asyncio.run(drive())

    assert turn.local_interrupt_calls == 0
    assert wl._barge_in_run_started_at == 0.0
    assert wl._barge_in_signalled_this_run is False


def test_flag_on_threshold_respected():
    """A frame just under the configured threshold never arms the run."""
    wl = _playback_loop(score=0.49, active=True)  # cfg threshold 0.5
    turn = wl._turn

    asyncio.run(wl._handle_session_frame(_frame()))

    assert turn.local_interrupt_calls == 0
    assert wl._barge_in_run_started_at == 0.0


# --- Self-interrupt-loop guard -----------------------------------------


def test_resolve_disables_barge_in_without_aec_reference(monkeypatch, tmp_path, caplog):
    """Barge-in requested on a profile with no AEC reference is hard-
    disabled for the turn and WARNs once — the self-interrupt guard."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._cfg.mic_device = "Array"
    wl._barge_in_reference_available = False
    wl._barge_in_no_ref_warned = False

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._resolve_barge_in_for_turn()
        first = [r for r in caplog.records if "barge.disabled_no_reference" in r.getMessage()]
        # WARN is one-shot per daemon — a second turn does not re-spam.
        wl._resolve_barge_in_for_turn()
        second = [r for r in caplog.records if "barge.disabled_no_reference" in r.getMessage()]

    assert wl._barge_in_active is False
    assert len(first) == 1
    assert len(second) == 1


def test_resolve_enables_barge_in_with_reference(monkeypatch, tmp_path):
    """Flag on + AEC reference present => barge-in active for the turn,
    read fresh from the SSOT file."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=on\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True

    wl._resolve_barge_in_for_turn()

    assert wl._barge_in_active is True


def test_resolve_defaults_off(monkeypatch, tmp_path):
    """No flag in the SSOT file => barge-in stays OFF even with a valid
    provider and a reference present."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True

    wl._resolve_barge_in_for_turn()

    assert wl._barge_in_active is False


def test_aec_reference_available_classifies_legs():
    from jasper.voice_daemon import _aec_reference_available

    assert _aec_reference_available("udp:9876") is True
    assert _aec_reference_available(" UDP:9876 ") is True
    assert _aec_reference_available("Array") is False
    assert _aec_reference_available("hw:1,0") is False
    assert _aec_reference_available("") is False


def test_disabled_branch_never_calls_playback_handler(monkeypatch):
    """Belt-and-suspenders for the pinning contract: the dispatch only
    enters the playback handler when the flag is active."""
    wl = _playback_loop(score=0.99, active=False)
    called = {"n": 0}

    async def _spy(_frame):
        called["n"] += 1

    monkeypatch.setattr(wl, "_handle_playback_frame", _spy)
    asyncio.run(wl._handle_session_frame(_frame()))
    assert called["n"] == 0

    wl._barge_in_active = True
    asyncio.run(wl._handle_session_frame(_frame()))
    assert called["n"] == 1
