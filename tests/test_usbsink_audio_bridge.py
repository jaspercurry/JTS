# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.usbsink.audio_bridge.AudioBridge callbacks.

The callbacks are pure functions over (bytes in, bytes out + stats
side-effects + queue side-effects). Hardware-free by construction —
we feed synthetic buffers and assert on the outputs and counters.

Sounddevice itself is only touched by start()/stop() which are mocked
in test_usbsink_daemon.py at the lifecycle layer. Here we test the
hot-path code: RMS scratch buffer reuse, S32→S16 stride view, queue
overflow handling, preempt silencing.
"""
from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from jasper.usbsink.audio_bridge import (
    AudioBridge,
    BLOCK_FRAMES,
    CHANNELS,
)


# ----------------------------------------------------------------------
# Helpers — synthesize raw S32 interleaved-stereo blocks for callback
# input. PortAudio gives us bytes; we give the callback bytes.
# ----------------------------------------------------------------------


def _silence_block(frames: int = BLOCK_FRAMES, channels: int = CHANNELS) -> bytes:
    """All-zero S32 stereo block of `frames` frames."""
    return b"\x00" * (frames * channels * 4)


def _constant_amplitude_block(
    amplitude_s32: int,
    frames: int = BLOCK_FRAMES,
    channels: int = CHANNELS,
) -> bytes:
    """All samples set to the same signed-int32 amplitude. RMS == |amp|,
    so the dBFS reading should equal 20 * log10(|amp| / S32_MAX)."""
    arr = np.full(frames * channels, amplitude_s32, dtype=np.int32)
    return arr.tobytes()


def _make_bridge(**overrides) -> AudioBridge:
    """Build an AudioBridge without starting it; nothing touches
    sounddevice at construction time. Useful for callback-only tests."""
    return AudioBridge(**overrides)


# ----------------------------------------------------------------------
# Capture callback — RMS computation
# ----------------------------------------------------------------------


def test_capture_callback_silence_yields_minus_inf_dbfs():
    """All-zero input → RMS is exactly zero → log10 fallback returns
    -inf dBFS. This is the "host muted" or "no audio yet" state."""
    bridge = _make_bridge()
    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    assert bridge.last_rms_dbfs == float("-inf")


def test_capture_callback_full_scale_is_near_0_dbfs():
    """A constant-amplitude block at S32_MAX (2^31 - 1) is full-scale
    digital, which is 0 dBFS. We test with 2^30 (half-scale) to get a
    clean -6 dB reading without floating-point weirdness at the max."""
    bridge = _make_bridge()
    half_scale = 1 << 30  # 2^30, exactly half of S32 max
    bridge._capture_callback(
        _constant_amplitude_block(half_scale), BLOCK_FRAMES, None, None,
    )
    # Constant amplitude → RMS == amplitude. (half_scale / 2^31)^2 = 0.25.
    # 10 * log10(0.25) = -6.02 dB.
    assert bridge.last_rms_dbfs == pytest.approx(-6.02, abs=0.1)


def test_capture_callback_scratch_buffer_reused_no_allocation():
    """The whole point of the pre-allocated scratch buffer (PR1 Tier 1
    fix) is that the float64 array survives across callbacks. Verify
    by checking the buffer's `id()` is stable and that repeated calls
    don't grow the daemon's allocator behavior in observable ways
    (here proxied by the buffer's identity)."""
    bridge = _make_bridge()
    scratch_id_before = id(bridge._rms_scratch)
    scratch_data_ptr_before = bridge._rms_scratch.ctypes.data

    for _ in range(50):
        bridge._capture_callback(
            _constant_amplitude_block(1 << 28), BLOCK_FRAMES, None, None,
        )

    assert id(bridge._rms_scratch) == scratch_id_before
    assert bridge._rms_scratch.ctypes.data == scratch_data_ptr_before


def test_capture_callback_smaller_block_uses_prefix_only():
    """Defensive: a smaller-than-expected block shouldn't crash. The
    callback uses arr[:n] of the scratch buffer."""
    bridge = _make_bridge()
    half = BLOCK_FRAMES // 2
    bridge._capture_callback(
        _constant_amplitude_block(1 << 29, frames=half),
        half, None, None,
    )
    # Should compute RMS over the prefix and produce a finite value.
    assert math.isfinite(bridge.last_rms_dbfs)


def test_capture_callback_increments_frame_counter():
    """Every callback increments stats.frames_captured by `frames`.
    The diagnostic loop uses callback counters for source-idle telemetry."""
    bridge = _make_bridge()
    assert bridge.stats.frames_captured == 0
    assert bridge.stats.capture_callbacks == 0
    assert bridge.stats.last_capture_callback_mono == 0.0

    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    assert bridge.stats.frames_captured == BLOCK_FRAMES
    assert bridge.stats.capture_callbacks == 1
    assert bridge.stats.last_capture_callback_mono > 0.0

    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    assert bridge.stats.frames_captured == 2 * BLOCK_FRAMES
    assert bridge.stats.capture_callbacks == 2


def test_capture_callback_status_increments_error_counter():
    """A non-None PortAudio status (e.g. CallbackFlags indicating
    overflow) increments capture_errors. Not logged per-frame (too
    chatty); the diag loop snapshots the counter."""
    bridge = _make_bridge()
    fake_status = object()  # any truthy status
    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, fake_status)
    assert bridge.stats.capture_errors == 1


# ----------------------------------------------------------------------
# Capture callback — S32→S16 stride view + queueing
# ----------------------------------------------------------------------


def test_capture_callback_enqueues_s16_block_of_correct_size():
    """Each callback enqueues one bytes object of size frames *
    channels * 2 (since S16). Output queue is bounded; one block per
    callback when not full."""
    bridge = _make_bridge()
    bridge._capture_callback(
        _constant_amplitude_block(1 << 28), BLOCK_FRAMES, None, None,
    )
    assert bridge._queue.qsize() == 1
    block = bridge._queue.get_nowait()
    assert isinstance(block, bytes)
    assert len(block) == BLOCK_FRAMES * CHANNELS * 2


def test_capture_callback_s32_to_s16_takes_high_16_bits():
    """The stride-view conversion (`arr.view(np.int16)[1::2]`) selects
    the high half of each int32 on little-endian. Verify by feeding a
    known S32 value and checking the S16 output matches `x >> 16`."""
    bridge = _make_bridge()
    # Pick a value whose high 16 bits and low 16 bits are distinct
    # so we'd notice if we accidentally read the low half.
    s32_val = 0x12345678  # high 16 = 0x1234, low 16 = 0x5678
    bridge._capture_callback(
        _constant_amplitude_block(s32_val), BLOCK_FRAMES, None, None,
    )
    block = bridge._queue.get_nowait()
    # Unpack first sample as little-endian signed int16
    first_s16 = struct.unpack("<h", block[:2])[0]
    assert first_s16 == 0x1234  # high 16 bits


def test_capture_callback_drops_on_queue_full():
    """When the playback callback is slow / stalled, the queue fills.
    Capture must drop blocks rather than block the PortAudio thread —
    blocking would propagate stall pressure to the gadget side and
    XRUN the host."""
    bridge = _make_bridge(queue_maxblocks=2)
    # Fill the queue
    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    assert bridge._queue.full()
    dropped_before = bridge.stats.frames_dropped_full

    # Third callback should hit queue.Full and increment the counter.
    bridge._capture_callback(_silence_block(), BLOCK_FRAMES, None, None)
    assert bridge.stats.frames_dropped_full == dropped_before + BLOCK_FRAMES
    # Queue size unchanged — drop, don't blocking-put.
    assert bridge._queue.qsize() == 2


# ----------------------------------------------------------------------
# Playback callback — preempt silencing, underrun, normal path
# ----------------------------------------------------------------------


def test_playback_callback_normal_path_writes_block_from_queue():
    """When not preempted and queue has data, playback writes the
    dequeued bytes into outdata and increments output counters."""
    bridge = _make_bridge()
    # Stage a block in the queue
    payload = b"\x11\x22" * (BLOCK_FRAMES * CHANNELS)
    bridge._queue.put_nowait(payload)
    out = bytearray(BLOCK_FRAMES * CHANNELS * 2)

    bridge._playback_callback(out, BLOCK_FRAMES, None, None)

    assert bytes(out) == payload
    assert bridge.stats.frames_played == BLOCK_FRAMES
    assert bridge.stats.frames_output == BLOCK_FRAMES
    assert bridge.stats.playback_callbacks == 1
    assert bridge.stats.last_playback_callback_mono > 0.0


def test_playback_callback_preempt_silences_output_and_drains_queue():
    """When preempted, the playback callback writes zeros regardless
    of queue contents, drains one queue entry, and still records output
    progress so idle/preempted USB does not look like a daemon wedge."""
    bridge = _make_bridge()
    bridge.set_preempted(True)
    # Backlog a noisy block
    bridge._queue.put_nowait(b"\xff" * (BLOCK_FRAMES * CHANNELS * 2))
    out = bytearray(BLOCK_FRAMES * CHANNELS * 2)
    # Pre-poison the output to verify it gets overwritten
    for i in range(len(out)):
        out[i] = 0xAA

    bridge._playback_callback(out, BLOCK_FRAMES, None, None)

    assert all(b == 0 for b in out), "preempted output must be silenced"
    # Queue was drained to prevent backlog buildup
    assert bridge._queue.qsize() == 0
    assert bridge.stats.frames_output == BLOCK_FRAMES
    assert bridge.stats.playback_callbacks == 1
    # Preempt discards queued host audio, so frames_played keeps its
    # "content from capture queue" meaning.
    assert bridge.stats.frames_played == 0


def test_playback_callback_underrun_silences_and_increments_counter():
    """Empty queue + not preempted = underrun. Output should be
    silence and the underrun counter should advance so the diag loop
    can spot persistent starvation."""
    bridge = _make_bridge()
    out = bytearray(BLOCK_FRAMES * CHANNELS * 2)
    for i in range(len(out)):
        out[i] = 0xAA

    bridge._playback_callback(out, BLOCK_FRAMES, None, None)

    assert all(b == 0 for b in out)
    assert bridge.stats.frames_underrun == BLOCK_FRAMES
    assert bridge.stats.frames_output == BLOCK_FRAMES
    assert bridge.stats.playback_callbacks == 1


def test_playback_callback_status_increments_error_counter():
    """A non-None PortAudio status on the playback side increments
    playback_errors. Same pattern as capture."""
    bridge = _make_bridge()
    out = bytearray(BLOCK_FRAMES * CHANNELS * 2)
    bridge._playback_callback(out, BLOCK_FRAMES, None, "any-status")
    assert bridge.stats.playback_errors == 1
    assert bridge.stats.frames_output == BLOCK_FRAMES
    assert bridge.stats.playback_callbacks == 1


def test_playback_callback_partial_block_truncates_and_zeros_rest():
    """Defensive: if the queue has a shorter-than-expected block (e.g.
    a host-side rate negotiation produced a partial), copy what fits
    and zero the rest. Don't crash, don't write past outdata."""
    bridge = _make_bridge()
    short_payload = b"\x55" * 100  # way less than a block
    bridge._queue.put_nowait(short_payload)
    out = bytearray(BLOCK_FRAMES * CHANNELS * 2)
    for i in range(len(out)):
        out[i] = 0xAA

    bridge._playback_callback(out, BLOCK_FRAMES, None, None)

    assert bytes(out[:100]) == short_payload
    assert all(b == 0 for b in out[100:])


# ----------------------------------------------------------------------
# Preempt state machine
# ----------------------------------------------------------------------


def test_set_preempted_idempotent_no_double_log():
    """set_preempted should no-op when state already matches —
    prevents the mux from spamming logs on every redundant POST."""
    bridge = _make_bridge()
    bridge.set_preempted(True)
    assert bridge.is_preempted is True
    bridge.set_preempted(True)  # idempotent
    assert bridge.is_preempted is True
    bridge.set_preempted(False)
    assert bridge.is_preempted is False
