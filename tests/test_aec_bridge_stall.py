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
    so the loop exits cleanly (no BridgeStalled needed).

    Looks up the bridge module's `_shutdown` at call time (not at
    import) — `test_raw_port_overridable_via_env` calls
    `importlib.reload(aec_bridge)`, which rebinds the module's
    `_shutdown` to a fresh Event. The top-of-file import in this
    test file still holds the OLD Event; `_aec_loop` (whose
    `__globals__` IS the module dict) looks up the NEW one. A
    stale-import `_shutdown.set()` would set the wrong Event and
    the loop would never exit.
    """

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def get(self, timeout=None):
        if self._i >= len(self._script):
            aec_bridge._shutdown.set()  # live module ref, reload-safe
            raise Empty
        item = self._script[self._i]
        self._i += 1
        if item is Empty:
            raise Empty
        return item

    def get_nowait(self):
        # Same semantics as get() but never blocks. Used by the
        # raw0_q drain in _aec_loop.
        return self.get(timeout=0)

    def qsize(self):
        return max(0, len(self._script) - self._i)


@pytest.fixture(autouse=True)
def _reset_shutdown_and_stub_sd(monkeypatch):
    """Each test gets a clean `_shutdown` and a no-op
    `sd.RawOutputStream` (the loop opens one on entry).

    Clears the LIVE `aec_bridge._shutdown` (not the top-of-file
    import) so a prior test's `importlib.reload(aec_bridge)`
    doesn't leave a stale Event set in the freshly-loaded module.
    The top-of-file `_shutdown` is the OLD Event; cleared too for
    completeness, but the loop's actual check goes through the
    module-dict lookup.
    """
    aec_bridge._shutdown.clear()
    _shutdown.clear()
    out_stream = MagicMock()
    sd_mod = MagicMock()
    sd_mod.RawOutputStream = MagicMock(return_value=out_stream)
    monkeypatch.setattr(aec_bridge, "sd", sd_mod)
    aec_bridge._bridge_stats.reset()
    yield
    aec_bridge._shutdown.clear()
    _shutdown.clear()
    aec_bridge._bridge_stats.reset()


def test_bridge_stats_snapshot_writes_monotonic_counters(tmp_path):
    path = tmp_path / "aec_bridge_stats.json"
    stats = aec_bridge._BridgeStats()
    stats.inc("frames_processed", 3)
    stats.inc_nested("queue_drops", "mic", 2)
    stats.inc_nested("packets_sent_by_leg", "on", 1)

    stats.write_snapshot(path)

    import json
    data = json.loads(path.read_text())
    assert data["schema_version"] == aec_bridge.BRIDGE_STATS_SCHEMA_VERSION
    assert data["pid"] > 0
    assert data["counters"]["frames_processed"] == 3
    assert data["counters"]["queue_drops"]["mic"] == 2
    assert data["counters"]["packets_sent_by_leg"]["on"] == 1


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
    # 3rd socket for the raw mic 0 leg (OUT_PORT_RAW0). The bridge
    # creates it unconditionally even when raw0_q is None (matches
    # the always-create-raw pattern). Not exercised in this test
    # since we don't pass a raw0_q; just needs to exist so the
    # socket() factory doesn't StopIteration.
    raw0_sock = _mock_socket()
    socket_factory = MagicMock(side_effect=[aec_sock, raw_sock, raw0_sock])
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

    # Three sockets created — AEC, chip-direct raw, truly-raw mic 0.
    assert socket_factory.call_count == 3
    aec_sock.setblocking.assert_called_once_with(False)
    raw_sock.setblocking.assert_called_once_with(False)
    raw0_sock.setblocking.assert_called_once_with(False)
    # raw0_sock isn't fed (no raw0_q passed) — must NOT emit.
    raw0_sock.sendto.assert_not_called()

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

    # All sockets closed on exit (finally block).
    aec_sock.close.assert_called_once()
    raw_sock.close.assert_called_once()
    raw0_sock.close.assert_called_once()


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
    # 3rd socket for raw mic 0 (always created, never fed in this test).
    raw0_sock = _mock_socket()
    socket_factory = MagicMock(side_effect=[aec_sock, raw_sock, raw0_sock])
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


def test_raw0_port_default_9879():
    """Default raw mic 0 UDP port is the canonical 9879. wake-corpus
    recorder + wake_enroll CLI both subscribe to this port; if it
    drifts from the bridge's default they'd silently get no audio."""
    import jasper.cli.aec_bridge as bridge_mod
    from jasper.cli.wake_enroll import DEFAULT_AEC_RAW0_PORT
    assert bridge_mod.OUT_PORT_RAW0 == 9879
    assert DEFAULT_AEC_RAW0_PORT == 9879


def test_aec_loop_emits_raw0_when_raw0_q_passed(monkeypatch):
    """When raw0_q is provided, 4 raw0 frames in → one packet out on
    OUT_PORT_RAW0. Byte-distinct from the mic_q frames so we can
    verify the right bytes landed on the right port.

    Uses the top-of-file `_aec_loop` and `_ScriptedMicQ` to share
    the same `_shutdown` Event the autouse fixture manages —
    avoids the "prior reload-test left module-state diverged"
    failure mode.
    """
    import socket as real_socket
    from jasper.cli.aec_bridge import OUT_PORT_RAW0
    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    socket_factory = MagicMock(side_effect=[aec_sock, raw_sock, raw0_sock])
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    # 4 mic frames + 4 distinct raw0 frames → exactly 1 packet per leg.
    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    raw0_frames = [bytes([i + 200]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    engine = MagicMock()
    engine.process.side_effect = aec_frames

    _aec_loop(
        _AlwaysEmptyQ(), _ScriptedMicQ(mic_frames), engine,
        raw0_q=_ScriptedMicQ(raw0_frames),
    )

    # raw0 emitted exactly its 1280-sample packet to OUT_PORT_RAW0.
    raw0_sock.sendto.assert_called_once()
    call = raw0_sock.sendto.call_args
    assert call.args[0] == b"".join(raw0_frames)
    assert call.args[1] == (OUT_HOST, OUT_PORT_RAW0)
    assert len(call.args[0]) == OUT_FRAME_BYTES


def test_aec_loop_emits_ref_when_enabled(monkeypatch):
    """Corpus ref output is opt-in and emits the exact 16 kHz ref
    frames the AEC loop consumed."""
    import socket as real_socket
    from jasper.cli.aec_bridge import OUT_PORT_REF

    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    ref_sock = _mock_socket()
    socket_factory = MagicMock(
        side_effect=[aec_sock, raw_sock, raw0_sock, ref_sock],
    )
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    ref_frames = [bytes([i + 50]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    engine = MagicMock()
    engine.process.side_effect = aec_frames

    _aec_loop(
        _ScriptedMicQ(ref_frames),
        _ScriptedMicQ(mic_frames),
        engine,
        emit_ref=True,
    )

    ref_sock.sendto.assert_called_once_with(
        b"".join(ref_frames), (OUT_HOST, OUT_PORT_REF),
    )
    ref_sock.close.assert_called_once()


def test_aec_loop_emits_usb_raw_and_webrtc_when_usb_queue_passed(monkeypatch):
    """Corpus USB mode emits cheap-mic raw plus a second WebRTC AEC
    output, without changing the primary XVF AEC/raw/raw0 packets."""
    import socket as real_socket
    from jasper.aec_sweep import USB_AEC3_CORPUS_OVERRIDES
    from jasper.cli.aec_bridge import OUT_PORT_USB_RAW, OUT_PORT_USB_WEBRTC

    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    usb_raw_sock = _mock_socket()
    usb_webrtc_sock = _mock_socket()
    socket_factory = MagicMock(
        side_effect=[
            aec_sock, raw_sock, raw0_sock, usb_raw_sock, usb_webrtc_sock,
        ],
    )
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_frames = [bytes([i + 20]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_clean_frames = [
        bytes([i + 150]) * (FRAME_SAMPLES * 2) for i in range(1, 5)
    ]
    engine = MagicMock()
    engine.process.side_effect = aec_frames
    usb_engine = MagicMock()
    usb_engine.process.side_effect = usb_clean_frames
    observed_overrides = []

    def fake_select_engine(overrides=None, label=None):
        observed_overrides.append((overrides, label))
        return usb_engine

    monkeypatch.setattr(aec_bridge, "_select_engine", fake_select_engine)

    _aec_loop(
        _AlwaysEmptyQ(),
        _ScriptedMicQ(mic_frames),
        engine,
        usb_raw_q=_ScriptedMicQ(usb_frames),
    )

    usb_raw_sock.sendto.assert_called_once_with(
        b"".join(usb_frames), (OUT_HOST, OUT_PORT_USB_RAW),
    )
    usb_webrtc_sock.sendto.assert_called_once_with(
        b"".join(usb_clean_frames), (OUT_HOST, OUT_PORT_USB_WEBRTC),
    )
    assert observed_overrides == [
        (USB_AEC3_CORPUS_OVERRIDES, "usb_webrtc/aec3_edge_combo_80"),
    ]
    usb_engine.close.assert_called_once()


def test_aec_loop_emits_aec3_sweep_variants_when_enabled(monkeypatch):
    """AEC3 corpus sweep runs three independent WebRTC engines on the
    same mic/ref frames and emits each as its own UDP leg."""
    import socket as real_socket
    from jasper.aec_sweep import AEC3_SWEEP_ENV_FLAG, AEC3_SWEEP_VARIANTS
    from jasper.cli.aec_bridge import OUT_PORT_AEC3_SWEEP

    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.setenv(AEC3_SWEEP_ENV_FLAG, "1")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    sweep_socks = [_mock_socket() for _ in AEC3_SWEEP_VARIANTS]
    socket_factory = MagicMock(
        side_effect=[aec_sock, raw_sock, raw0_sock, *sweep_socks],
    )
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    sweep_outputs = [
        [bytes([i + offset]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
        for offset in (130, 150, 170)
    ]
    engine = MagicMock()
    engine.process.side_effect = aec_frames
    sweep_engines = []
    observed_overrides = []

    for frames in sweep_outputs:
        e = MagicMock()
        e.process.side_effect = frames
        sweep_engines.append(e)

    def fake_select_engine(overrides=None, label=None):
        observed_overrides.append((overrides, label))
        return sweep_engines[len(observed_overrides) - 1]

    monkeypatch.setattr(aec_bridge, "_select_engine", fake_select_engine)

    _aec_loop(_AlwaysEmptyQ(), _ScriptedMicQ(mic_frames), engine)

    assert socket_factory.call_count == 3 + len(AEC3_SWEEP_VARIANTS)
    assert [item[0] for item in observed_overrides] == [
        variant.env_overrides for variant in AEC3_SWEEP_VARIANTS
    ]
    for variant, sock, frames in zip(AEC3_SWEEP_VARIANTS, sweep_socks, sweep_outputs):
        sock.sendto.assert_called_once_with(
            b"".join(frames), (OUT_HOST, OUT_PORT_AEC3_SWEEP[variant.leg]),
        )
    for e in sweep_engines:
        e.close.assert_called_once()


def test_aec_loop_can_feed_aec3_sweep_from_usb_mic(monkeypatch):
    """USB sweep mode reuses the stable variant UDP slots but feeds
    those engines from the cheap USB mic instead of the XVF mic."""
    import socket as real_socket
    from jasper.aec_sweep import (
        AEC3_SWEEP_ENV_FLAG,
        AEC3_SWEEP_SOURCE_USB,
        AEC3_SWEEP_VARIANTS,
        USB_AEC3_SWEEP_BASELINE_OVERRIDES,
    )
    from jasper.cli.aec_bridge import OUT_PORT_AEC3_SWEEP, OUT_PORT_USB_WEBRTC

    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.setenv(AEC3_SWEEP_ENV_FLAG, "1")
    monkeypatch.setattr(
        aec_bridge, "AEC3_SWEEP_INPUT_SOURCE", AEC3_SWEEP_SOURCE_USB,
    )
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    usb_raw_sock = _mock_socket()
    usb_webrtc_sock = _mock_socket()
    sweep_socks = [_mock_socket() for _ in AEC3_SWEEP_VARIANTS]
    socket_factory = MagicMock(
        side_effect=[
            aec_sock, raw_sock, raw0_sock, usb_raw_sock, usb_webrtc_sock,
            *sweep_socks,
        ],
    )
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_frames = [bytes([i + 20]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_clean_frames = [
        bytes([i + 120]) * (FRAME_SAMPLES * 2) for i in range(1, 5)
    ]
    sweep_outputs = [
        [bytes([i + offset]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
        for offset in (130, 150, 170)
    ]
    engine = MagicMock()
    engine.process.side_effect = aec_frames
    usb_engine = MagicMock()
    usb_engine.process.side_effect = usb_clean_frames
    sweep_engines = []
    observed_usb_webrtc_overrides = []
    observed_variant_overrides = []

    for frames in sweep_outputs:
        e = MagicMock()
        e.process.side_effect = frames
        sweep_engines.append(e)

    def fake_select_engine(overrides=None, label=None):
        if overrides is None:
            return usb_engine
        if overrides == USB_AEC3_SWEEP_BASELINE_OVERRIDES:
            observed_usb_webrtc_overrides.append((overrides, label))
            return usb_engine
        observed_variant_overrides.append((overrides, label))
        return sweep_engines[len(observed_variant_overrides) - 1]

    monkeypatch.setattr(aec_bridge, "_select_engine", fake_select_engine)

    _aec_loop(
        _AlwaysEmptyQ(),
        _ScriptedMicQ(mic_frames),
        engine,
        usb_raw_q=_ScriptedMicQ(usb_frames),
    )

    assert socket_factory.call_count == 5 + len(AEC3_SWEEP_VARIANTS)
    assert observed_usb_webrtc_overrides == [
        (USB_AEC3_SWEEP_BASELINE_OVERRIDES, "usb_webrtc/aec3_sweep_delay_40"),
    ]
    assert [item[0] for item in observed_variant_overrides] == [
        variant.env_overrides for variant in AEC3_SWEEP_VARIANTS
    ]
    usb_webrtc_sock.sendto.assert_called_once_with(
        b"".join(usb_clean_frames), (OUT_HOST, OUT_PORT_USB_WEBRTC),
    )
    for e in sweep_engines:
        assert [call.args[0] for call in e.process.call_args_list] == usb_frames
    for variant, sock, frames in zip(AEC3_SWEEP_VARIANTS, sweep_socks, sweep_outputs):
        sock.sendto.assert_called_once_with(
            b"".join(frames), (OUT_HOST, OUT_PORT_AEC3_SWEEP[variant.leg]),
        )


def test_aec_loop_emits_usb_dtln_when_enabled(monkeypatch):
    """USB DTLN is a separate opt-in neural leg, fed by cheap USB raw
    plus the same reference frame as the WebRTC corpus path."""
    import socket as real_socket
    from jasper.aec_engines import dtln as dtln_mod
    from jasper.cli.aec_bridge import (
        OUT_PORT_USB_DTLN,
        OUT_PORT_USB_RAW,
        OUT_PORT_USB_WEBRTC,
    )

    monkeypatch.setenv("JASPER_AEC_STALL_RESTART_SEC", "0")
    monkeypatch.setenv("JASPER_AEC_CORPUS_USB_DTLN_ENABLED", "1")
    monkeypatch.delenv("JASPER_AEC_MIC_GAIN_DB", raising=False)

    aec_sock = _mock_socket()
    raw_sock = _mock_socket()
    raw0_sock = _mock_socket()
    usb_raw_sock = _mock_socket()
    usb_webrtc_sock = _mock_socket()
    usb_dtln_sock = _mock_socket()
    socket_factory = MagicMock(
        side_effect=[
            aec_sock, raw_sock, raw0_sock,
            usb_raw_sock, usb_webrtc_sock, usb_dtln_sock,
        ],
    )
    monkeypatch.setattr(real_socket, "socket", socket_factory)

    mic_frames = [bytes([i]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_frames = [bytes([i + 20]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    aec_frames = [bytes([i + 100]) * (FRAME_SAMPLES * 2) for i in range(1, 5)]
    usb_clean_frames = [
        bytes([i + 150]) * (FRAME_SAMPLES * 2) for i in range(1, 5)
    ]
    usb_dtln_frames = [
        bytes([i + 180]) * (FRAME_SAMPLES * 2) for i in range(1, 5)
    ]
    engine = MagicMock()
    engine.process.side_effect = aec_frames
    usb_engine = MagicMock()
    usb_engine.process.side_effect = usb_clean_frames
    usb_dtln_engine = MagicMock()
    usb_dtln_engine.process.side_effect = usb_dtln_frames
    dtln_cls = MagicMock(return_value=usb_dtln_engine)

    monkeypatch.setattr(
        aec_bridge, "_select_engine",
        lambda overrides=None, label=None: usb_engine,
    )
    monkeypatch.setattr(dtln_mod, "DTLNEngine", dtln_cls)
    monkeypatch.setattr(dtln_mod, "default_model_dir", lambda: "/models")

    _aec_loop(
        _AlwaysEmptyQ(),
        _ScriptedMicQ(mic_frames),
        engine,
        usb_raw_q=_ScriptedMicQ(usb_frames),
    )

    usb_raw_sock.sendto.assert_called_once_with(
        b"".join(usb_frames), (OUT_HOST, OUT_PORT_USB_RAW),
    )
    usb_webrtc_sock.sendto.assert_called_once_with(
        b"".join(usb_clean_frames), (OUT_HOST, OUT_PORT_USB_WEBRTC),
    )
    usb_dtln_sock.sendto.assert_called_once_with(
        b"".join(usb_dtln_frames), (OUT_HOST, OUT_PORT_USB_DTLN),
    )
    dtln_cls.assert_called_once()
    usb_dtln_engine.close.assert_called_once()
    usb_engine.close.assert_called_once()


# ---------------------------------------------------------------------------
# Slow-drip stall watchdog (_MicStarvationWatchdog) — the rate-based detector
# that catches an intermittent trickle the consecutive-empty check misses.
# Regression for the 2026-05-31 incident: the bridge ran ~13 h effectively
# deaf (a frame every few seconds) without the continuous counter ever
# reaching its threshold, so it never restarted.
# ---------------------------------------------------------------------------


def test_starvation_watchdog_flags_sustained_slow_drip(caplog):
    """A mic delivering ~1 frame every 15 s (far below the per-window floor)
    trips the watchdog after max_starved_windows windows — the failure mode
    the consecutive-empty counter never catches, because each trickle resets
    it. The buildup is logged before the restart (observability)."""
    wd = aec_bridge._MicStarvationWatchdog(
        window_sec=10.0, min_frames_per_window=10, max_starved_windows=3,
    )
    stalled_at = None
    with caplog.at_level("WARNING"):
        for step in range(500):          # 50 s in 0.1 s steps
            now = step * 0.1
            if step % 150 == 0:          # one frame every 15 s
                wd.record_frame()
            if wd.stalled(now):
                stalled_at = now
                break
    assert stalled_at is not None, "watchdog never escalated on a slow drip"
    # 3 starved windows of 10 s, scored at window boundaries.
    assert 29.0 <= stalled_at <= 41.0
    # The collapse is visible in the journal, not a silent restart.
    assert any("mic starvation" in r.message for r in caplog.records)


def test_starvation_watchdog_healthy_mic_never_trips():
    """A full-rate mic (~12.5 frames/s) keeps every window well above the
    floor — the watchdog must never escalate."""
    wd = aec_bridge._MicStarvationWatchdog(
        window_sec=10.0, min_frames_per_window=10, max_starved_windows=3,
    )
    for step in range(900):              # 72 s at ~12.5 frames/s
        now = step * 0.08
        wd.record_frame()
        assert not wd.stalled(now)


def test_starvation_watchdog_brief_blip_recovers():
    """One starved window followed by recovery never reaches the consecutive
    count — a brief ALSA stutter must not flap the daemon."""
    wd = aec_bridge._MicStarvationWatchdog(
        window_sec=10.0, min_frames_per_window=10, max_starved_windows=3,
    )
    for step in range(900):
        now = step * 0.1
        if now >= 10.0:                  # silent for the first window, then healthy
            wd.record_frame()
        assert not wd.stalled(now)


def test_starvation_watchdog_disabled_when_max_windows_zero():
    """max_starved_windows=0 is the off switch — never escalates, even with a
    totally dead mic."""
    wd = aec_bridge._MicStarvationWatchdog(max_starved_windows=0)
    for step in range(1000):
        assert not wd.stalled(step * 1.0)   # no frames ever, still never trips
