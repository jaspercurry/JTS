"""Unit tests for jasper-aec-bridge stall-recovery.

The bridge's mic input is a PortAudio `InputStream` that is opened
once at startup and runs until process exit. When the underlying
ALSA capture PCM enters an unrecoverable state (typically a USB
underrun on the XVF chip's UAC2 capture endpoint), PortAudio
silently stops invoking the registered callback — no exception,
no error code, no recovery hook. The bridge sits there, draining
nothing onto its mic queue.

Without stall detection, `_aec_loop` would log a per-second
    warning forever, never sending fresh UDP mic frames, and the
    wake-word detector reading udp:9876 would stay deaf. This was
hit in production on 2026-05-11: ~10 minutes of silent failure,
"Hey Jarvis" got no response, no audible cue.

These tests pin the contract:
  - Threshold breach → `BridgeStalled` raised → process exits 1
    → systemd `Restart=on-failure` revives with a fresh stream.
  - Successful frame resets the counter (so a brief 1-2 s ALSA
    stutter doesn't flap the daemon).
"""
from __future__ import annotations

import sys
import types
from queue import Empty
from unittest.mock import MagicMock

import numpy as np
import pytest

# aec_bridge.py imports sounddevice at module level for the
# `sd.InputStream` / `sd.RawOutputStream` calls. Neither is touched
# by the stall logic itself — but the import has to succeed. Stub
# before the bridge module loads; matches the pattern in
# tests/test_doctor.py for sounddevice and tests/conftest.py for
# camilladsp.
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")

from jasper.cli import aec_bridge  # noqa: E402
from jasper.cli.aec_bridge import (  # noqa: E402
    BridgeStalled,
    FRAME_SAMPLES,
    MicDeviceUnavailable,
    _aec_loop,
    _shutdown,
    _validate_mic_device,
)


class _AlwaysEmptyQ:
    """Queue stub whose `get` always raises Empty without blocking.
    Bypasses the real 1-second wait so the test runs in ms."""

    def get(self, timeout=None):
        raise Empty

    def get_nowait(self):
        raise Empty

    def qsize(self):
        return 0


class _ScriptedMicQ:
    """Mic-queue stub driven by a list of (Empty | bytes) items.
    When the script is exhausted, sets `_shutdown` and raises Empty
    so the loop exits cleanly (no BridgeStalled needed)."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def get(self, timeout=None):
        if self._i >= len(self._script):
            _shutdown.set()
            raise Empty
        item = self._script[self._i]
        self._i += 1
        if item is Empty:
            raise Empty
        return item

    def qsize(self):
        return 0


@pytest.fixture(autouse=True)
def _reset_shutdown_and_stub_sd(monkeypatch):
    """Each test gets a clean `_shutdown` and a no-op
    `sd.RawOutputStream` (the loop opens one on entry)."""
    _shutdown.clear()
    out_stream = MagicMock()
    sd_mod = MagicMock()
    sd_mod.RawOutputStream = MagicMock(return_value=out_stream)
    monkeypatch.setattr(aec_bridge, "sd", sd_mod)
    yield
    _shutdown.clear()


def test_raises_bridge_stalled_at_threshold(monkeypatch):
    """N consecutive empty-mic seconds = `BridgeStalled` raised,
    so `main()` returns 1 and systemd restarts us."""
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "3")
    engine = MagicMock()

    with pytest.raises(BridgeStalled) as excinfo:
        _aec_loop(_AlwaysEmptyQ(), _AlwaysEmptyQ(), engine)

    assert "3s" in str(excinfo.value)
    # No mic frame ever arrived, so the engine wasn't asked to process.
    engine.process.assert_not_called()


def test_counter_resets_on_successful_frame(monkeypatch):
    """A 2-second stutter followed by a recovering mic stream must
    NOT trip the 3-second threshold — the counter resets on each
    real frame, so total empties = threshold doesn't matter; only
    *consecutive* empties do."""
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "3")

    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    # 3 total empties (= threshold) but interrupted by a frame in the
    # middle. Without the counter reset, this would raise; with it,
    # consecutive_empty_sec peaks at 2 in each run.
    script = [Empty, Empty, frame, Empty]

    engine = MagicMock()
    engine.process = MagicMock(return_value=frame)

    # Returns normally — no BridgeStalled.
    _aec_loop(_AlwaysEmptyQ(), _ScriptedMicQ(script), engine)
    engine.process.assert_called_once()


def test_disabled_when_threshold_is_zero(monkeypatch):
    """Escape hatch for operators: `JASPER_AEC_STALL_RESTART_SEC=0`
    preserves the legacy log-forever behaviour. Useful if a quirk
    of the operator's hardware causes false-positive stalls and
    they'd rather babysit the daemon manually."""
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")

    # Even with 10 consecutive empties, no raise — instead the
    # script exhausts and _shutdown trips, returning cleanly.
    script = [Empty] * 10
    engine = MagicMock()

    _aec_loop(_AlwaysEmptyQ(), _ScriptedMicQ(script), engine)
    engine.process.assert_not_called()


def test_validate_mic_device_raises_before_bridge_starts(monkeypatch):
    """A missing XVF/Array device must fail before the bridge opens the
    shared `jasper_capture` reference tap used by the music path."""
    sd_mod = MagicMock()
    sd_mod.query_devices.side_effect = ValueError(
        "No input device matching 'Array'"
    )
    monkeypatch.setattr(aec_bridge, "sd", sd_mod)

    with pytest.raises(MicDeviceUnavailable):
        _validate_mic_device()

    sd_mod.query_devices.assert_called_once_with("Array", "input")


def test_main_exits_before_engine_init_when_mic_missing(monkeypatch):
    """If the mic is absent, do not construct the AEC engine or start
    capture threads that would touch `jasper_capture`."""
    sd_mod = MagicMock()
    sd_mod.query_devices.side_effect = ValueError(
        "No input device matching 'Array'"
    )
    monkeypatch.setattr(aec_bridge, "sd", sd_mod)
    engine_cls = MagicMock()
    monkeypatch.setattr(aec_bridge, "_Aec3Engine", engine_cls)

    assert aec_bridge.main() == 1
    engine_cls.assert_not_called()
