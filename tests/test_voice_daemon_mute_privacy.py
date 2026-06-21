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

import logging
import sys
import types
from collections import deque
from types import SimpleNamespace


# Stub heavyweight/host-specific deps only when genuinely absent —
# a fake httpx shadowing a real install breaks jasper.tools imports.
def _stub_if_missing(name: str, module: types.ModuleType) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = module


_httpx = types.ModuleType("httpx")
_httpx.Timeout = lambda *a, **kw: SimpleNamespace(
    read=5.0, write=5.0, connect=5.0, pool=5.0,
)
_stub_if_missing("httpx", _httpx)
_stub_if_missing("sounddevice", types.ModuleType("sounddevice"))
_rapidfuzz = types.ModuleType("rapidfuzz")
_rapidfuzz.fuzz = types.SimpleNamespace()
_stub_if_missing("rapidfuzz", _rapidfuzz)


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
