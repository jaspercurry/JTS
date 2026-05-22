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
    OUT_FRAME_BYTES,
    OUT_HOST,
    OUT_PORT,
    OUT_PORT_RAW,
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


# ---------------------------------------------------------------------------
# Dual-stream UDP output (PR 1 of the wake-telemetry series).
#
# The bridge emits two UDP streams from `_aec_loop`:
#   - OUT_PORT (default 9876) — post-AEC output (existing behaviour)
#   - OUT_PORT_RAW (default 9877) — chip-direct mic (pre-AEC), the
#     same near-end bytes AEC3 consumes for cancellation.
# Both batched to OUT_FRAME_BYTES (1280-sample / 80 ms) packets so
# the consumer (jasper-voice's UdpMicCapture) sees identical chunk
# shapes. See docs/HANDOFF-wake-telemetry.md for the rationale.
# ---------------------------------------------------------------------------


def _mock_socket():
    """A `socket.socket()` substitute with the methods _aec_loop calls."""
    s = MagicMock()
    s.setblocking = MagicMock()
    s.sendto = MagicMock()
    s.close = MagicMock()
    return s


def test_aec_loop_emits_both_streams(monkeypatch):
    """Four mic frames in → one packet out on EACH port. AEC output
    bytes go to OUT_PORT; chip-direct mic bytes go to OUT_PORT_RAW."""
    import socket as real_socket
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    # Disable post-AEC gain so engine output bytes == AEC packet bytes.
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    socket_factory = MagicMock(side_effect=[aec_sock, raw_sock])
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    # 8 frames of 320 samples each = 16 ms × 8 = 2 full 1280-sample
    # batches per leg. Each mic frame is byte-distinct so we can
    # verify which bytes landed in which packet.
    mic_frames = [
        bytes([i & 0xff]) * (FRAME_SAMPLES * 2)
        for i in range(1, 9)
    ]
    aec_frames = [
        bytes([(i + 100) & 0xff]) * (FRAME_SAMPLES * 2)
        for i in range(1, 9)
    ]
    engine = MagicMock()
    engine.process.side_effect = aec_frames

    _aec_loop(_AlwaysEmptyQ(), _ScriptedMicQ(mic_frames), engine)

    # Two sockets created — primary AEC, then raw mic.
    assert socket_factory.call_count == 2
    aec_sock.setblocking.assert_called_once_with(False)
    raw_sock.setblocking.assert_called_once_with(False)

    # Each leg emitted exactly 2 packets (8 frames / 4 per packet).
    assert aec_sock.sendto.call_count == 2
    assert raw_sock.sendto.call_count == 2

    # Primary stream: bytes match engine output, destination = OUT_PORT.
    expected_aec_p1 = b"".join(aec_frames[:4])
    expected_aec_p2 = b"".join(aec_frames[4:])
    aec_call_1, aec_call_2 = aec_sock.sendto.call_args_list
    assert aec_call_1.args == (expected_aec_p1, (OUT_HOST, OUT_PORT))
    assert aec_call_2.args == (expected_aec_p2, (OUT_HOST, OUT_PORT))
    assert len(aec_call_1.args[0]) == OUT_FRAME_BYTES

    # Raw stream: bytes match input mic, destination = OUT_PORT_RAW.
    expected_raw_p1 = b"".join(mic_frames[:4])
    expected_raw_p2 = b"".join(mic_frames[4:])
    raw_call_1, raw_call_2 = raw_sock.sendto.call_args_list
    assert raw_call_1.args == (expected_raw_p1, (OUT_HOST, OUT_PORT_RAW))
    assert raw_call_2.args == (expected_raw_p2, (OUT_HOST, OUT_PORT_RAW))
    assert len(raw_call_1.args[0]) == OUT_FRAME_BYTES

    # Both sockets closed on exit (finally block).
    aec_sock.close.assert_called_once()
    raw_sock.close.assert_called_once()


def test_raw_sendto_failure_does_not_affect_aec_stream(monkeypatch):
    """BlockingIOError on the raw socket is swallowed and logged;
    the primary AEC stream continues unaffected. Independent sockets,
    independent failure domains."""
    import socket as real_socket
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw_sock.sendto.side_effect = BlockingIOError(
        "simulated kernel UDP send buffer full"
    )
    socket_factory = MagicMock(side_effect=[aec_sock, raw_sock])
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    engine = MagicMock()
    engine.process.side_effect = aec_frames

    _aec_loop(_AlwaysEmptyQ(), _ScriptedMicQ(mic_frames), engine)

    # Raw attempted its single packet; the BlockingIOError did NOT
    # propagate to the AEC stream, which sent its packet normally.
    assert raw_sock.sendto.call_count == 1
    assert aec_sock.sendto.call_count == 1
    aec_sock.sendto.assert_called_once_with(
        b"".join(aec_frames), (OUT_HOST, OUT_PORT),
    )


def test_raw_port_overridable_via_env(monkeypatch):
    """Operators can move the raw stream off the default 9877
    (e.g. for two-bridge testing) without touching the AEC port."""
    monkeypatch.setenv("JASPER_AEC_UDP_PORT_RAW", "19877")

    # Re-import to pick up the env var. (Module-level constant.)
    import importlib
    import jasper.cli.aec_bridge as bridge_mod
    importlib.reload(bridge_mod)

    try:
        assert bridge_mod.OUT_PORT_RAW == 19877
        # Default AEC port unaffected
        assert bridge_mod.OUT_PORT == 9876
    finally:
        # Restore defaults so subsequent tests see canonical ports.
        monkeypatch.delenv("JASPER_AEC_UDP_PORT_RAW", raising=False)
        importlib.reload(bridge_mod)
