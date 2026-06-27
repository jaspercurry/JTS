# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Stage-4b usbsink FIFO-output mode (output_mode="fifo").

Hardware-free: the bridge imports sounddevice lazily inside start(), so
constructing an AudioBridge and driving _fifo_writer_loop / _capture_callback
directly never touches PortAudio or ALSA. The FIFO is a real named pipe in a
tmp dir with a fake reader thread — exactly the os.pipe/mkfifo + fake-reader
pattern the spec calls for.

The two load-bearing guards proven here:
  - default-OFF: with no kwargs / env unset the bridge stays in aloop mode
    and enqueues the S16 high-half view (byte-identical to today);
  - format width: in fifo mode the capture callback enqueues the FULL S32_LE
    bytes (CamillaDSP File-captures S32_LE — a half-width S16 stream would
    misframe every sample).
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np

from jasper.usbsink.audio_bridge import AudioBridge
from jasper.usbsink.daemon import DaemonConfig, _parse_output_mode


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _drain_reader(fifo_path: str, nbytes: int, got: list, ready: threading.Event):
    """Open the read end (reader-first ordering — this open unblocks the
    writer's O_WRONLY|O_NONBLOCK retry) and read `nbytes`, then close."""
    fd = os.open(fifo_path, os.O_RDONLY)  # blocks until a writer opens
    ready.set()
    data = b""
    while len(data) < nbytes:
        chunk = os.read(fd, nbytes - len(data))
        if not chunk:
            break
        data += chunk
    got.append(data)
    os.close(fd)


def _run_writer_until(bridge: AudioBridge, fifo_path: str, nbytes: int) -> bytes:
    """Start a reader + the writer loop, collect `nbytes`, stop both.
    Returns the bytes the reader saw."""
    got: list = []
    ready = threading.Event()
    rt = threading.Thread(
        target=_drain_reader, args=(fifo_path, nbytes, got, ready), daemon=True,
    )
    rt.start()
    wt = threading.Thread(target=bridge._fifo_writer_loop, daemon=True)
    wt.start()
    rt.join(timeout=3.0)
    bridge._fifo_stop.set()
    wt.join(timeout=3.0)
    assert not wt.is_alive(), "writer thread did not stop"
    return got[0] if got else b""


# ----------------------------------------------------------------------
# Default-OFF contract — unset env / no kwargs is byte-identical aloop
# ----------------------------------------------------------------------


def test_default_mode_is_aloop_no_fifo_thread():
    b = AudioBridge()  # no kwargs → defaults
    assert b._output_mode == "aloop"
    assert b._fifo_thread is None
    assert b._fifo_path == "/run/jasper-usbsink/lean.pipe"


def test_default_mode_capture_callback_enqueues_s16_half_width():
    """aloop mode keeps the S16 high-half view enqueue (today's behavior):
    an S32 stereo block in → an S16 stereo block out (half the bytes)."""
    b = AudioBridge(block_frames=4, channels=2)
    indata = (np.arange(4 * 2, dtype=np.int32) << 16).tobytes()  # 8 S32 samples
    b._capture_callback(indata, 4, None, 0)
    payload = b._queue.get_nowait()
    assert len(payload) == 4 * 2 * 2  # S16 stereo = HALF of S32 width


def test_from_env_default_is_aloop(monkeypatch):
    monkeypatch.delenv("JASPER_USBSINK_OUTPUT_MODE", raising=False)
    monkeypatch.delenv("JASPER_USBSINK_FIFO_PATH", raising=False)
    cfg = DaemonConfig.from_env()
    assert cfg.output_mode == "aloop"
    assert cfg.fifo_path == "/run/jasper-usbsink/lean.pipe"


def test_parse_output_mode_fallback():
    assert _parse_output_mode("") == "aloop"
    assert _parse_output_mode("aloop") == "aloop"
    assert _parse_output_mode("ALOOP") == "aloop"
    assert _parse_output_mode(" fifo ") == "fifo"
    assert _parse_output_mode("fifo") == "fifo"
    # A typo must NEVER silently select the lean path.
    assert _parse_output_mode("bogus") == "aloop"
    assert _parse_output_mode("fif0") == "aloop"


def test_from_env_invalid_mode_falls_back_to_aloop(monkeypatch):
    monkeypatch.setenv("JASPER_USBSINK_OUTPUT_MODE", "loopback")
    cfg = DaemonConfig.from_env()
    assert cfg.output_mode == "aloop"


# ----------------------------------------------------------------------
# FIFO-mode capture-callback format width (the misframe guard)
# ----------------------------------------------------------------------


def test_fifo_mode_capture_callback_enqueues_full_s32():
    """In fifo mode the capture callback enqueues full-width S32_LE bytes
    verbatim — exactly what CamillaDSP File-captures."""
    b = AudioBridge(output_mode="fifo", block_frames=480, channels=2)
    indata = np.arange(480 * 2, dtype=np.int32).tobytes()
    b._capture_callback(indata, 480, None, 0)
    payload = b._queue.get_nowait()
    assert len(payload) == 480 * 2 * 4  # full S32_LE width
    assert payload == indata


def test_fifo_mode_rms_still_computed():
    """The RMS readout is mode-agnostic — fifo mode must still update it
    so the state publisher / playing-detection keeps working."""
    b = AudioBridge(output_mode="fifo", block_frames=4, channels=2)
    half_scale = 1 << 30
    indata = np.full(4 * 2, half_scale, dtype=np.int32).tobytes()
    b._capture_callback(indata, 4, None, 0)
    assert b.last_rms_dbfs > -10.0  # ~ -6 dBFS for half scale


# ----------------------------------------------------------------------
# FIFO writer thread — drains queue to pipe, silence, preempt, reader-first
# ----------------------------------------------------------------------


def test_fifo_writer_drains_queue_to_pipe(tmp_path):
    fifo = str(tmp_path / "lean.pipe")
    os.mkfifo(fifo)
    b = AudioBridge(output_mode="fifo", fifo_path=fifo, block_frames=4, channels=2)
    block = bytes(range(4 * 2 * 4))  # one S32 stereo block, 32 bytes
    b._queue.put_nowait(block)
    got = _run_writer_until(b, fifo, 32)
    assert got == block  # exact bytes, full S32 width
    assert b.stats.fifo_writes >= 1
    assert b.stats.last_fifo_write_mono > 0.0


def test_fifo_writer_emits_silence_when_queue_empty(tmp_path):
    fifo = str(tmp_path / "lean.pipe")
    os.mkfifo(fifo)
    b = AudioBridge(output_mode="fifo", fifo_path=fifo, block_frames=4, channels=2)
    # Nothing queued → writer emits a full-width silence block.
    got = _run_writer_until(b, fifo, 32)
    assert got == bytes(32)  # silence
    assert b.stats.fifo_underrun >= 4


def test_fifo_writer_preempt_writes_silence_not_audio(tmp_path):
    fifo = str(tmp_path / "lean.pipe")
    os.mkfifo(fifo)
    b = AudioBridge(output_mode="fifo", fifo_path=fifo, block_frames=4, channels=2)
    b.set_preempted(True)
    b._queue.put_nowait(bytes([0xAB] * 32))  # real audio — must NOT reach pipe
    got = _run_writer_until(b, fifo, 32)
    assert got == bytes(32)  # silence, not 0xAB


def test_fifo_writer_waits_for_reader_then_writes(tmp_path):
    """ENXIO retry: a writer started before any reader must not crash and
    must deliver once a reader appears (reader-first / SNAPFIFO idiom)."""
    fifo = str(tmp_path / "lean.pipe")
    os.mkfifo(fifo)
    b = AudioBridge(output_mode="fifo", fifo_path=fifo, block_frames=4, channels=2)
    b._queue.put_nowait(bytes(32))
    wt = threading.Thread(target=b._fifo_writer_loop, daemon=True)
    wt.start()
    time.sleep(0.3)  # writer is spinning on ENXIO (no reader yet)
    assert wt.is_alive(), "writer died with no reader present"
    got: list = []
    ready = threading.Event()
    rt = threading.Thread(
        target=_drain_reader, args=(fifo, 32, got, ready), daemon=True,
    )
    rt.start()
    rt.join(timeout=3.0)
    b._fifo_stop.set()
    wt.join(timeout=3.0)
    assert not wt.is_alive()
    assert got and len(got[0]) == 32


def test_fifo_writer_reopens_after_reader_goes_away(tmp_path):
    """A reader that closes (CamillaDSP reload) → BrokenPipeError → the
    writer closes + reopens and keeps serving the next reader. Proves the
    lean lane survives a CamillaDSP config reload without dying."""
    fifo = str(tmp_path / "lean.pipe")
    os.mkfifo(fifo)
    b = AudioBridge(output_mode="fifo", fifo_path=fifo, block_frames=4, channels=2)
    wt = threading.Thread(target=b._fifo_writer_loop, daemon=True)
    wt.start()
    try:
        # First reader: take one block, then close (simulates reader gone).
        got1: list = []
        r1_ready = threading.Event()
        r1 = threading.Thread(
            target=_drain_reader, args=(fifo, 32, got1, r1_ready), daemon=True,
        )
        r1.start()
        r1.join(timeout=3.0)
        assert got1 and len(got1[0]) == 32

        # Give the writer a moment to hit EPIPE and reopen O_NONBLOCK.
        # Second reader must still get served (writer recovered).
        got2: list = []
        r2_ready = threading.Event()
        r2 = threading.Thread(
            target=_drain_reader, args=(fifo, 32, got2, r2_ready), daemon=True,
        )
        r2.start()
        r2.join(timeout=3.0)
        assert got2 and len(got2[0]) == 32, "writer did not recover after EPIPE"
    finally:
        b._fifo_stop.set()
        wt.join(timeout=3.0)
        assert not wt.is_alive()


# ----------------------------------------------------------------------
# _ensure_fifo
# ----------------------------------------------------------------------


def test_ensure_fifo_creates_pipe(tmp_path):
    import stat

    fifo = str(tmp_path / "sub" / "lean.pipe")
    b = AudioBridge(output_mode="fifo", fifo_path=fifo)
    b._ensure_fifo(fifo)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    # Idempotent — second call is a no-op, no raise.
    b._ensure_fifo(fifo)


def test_ensure_fifo_rejects_non_fifo_path(tmp_path):
    import pytest

    real_file = tmp_path / "lean.pipe"
    real_file.write_text("not a fifo")
    b = AudioBridge(output_mode="fifo", fifo_path=str(real_file))
    with pytest.raises(RuntimeError, match="not a FIFO"):
        b._ensure_fifo(str(real_file))
