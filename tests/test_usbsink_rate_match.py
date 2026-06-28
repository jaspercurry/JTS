# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the usbsink drift rate-match stage (default OFF).

Hardware-free. The audio callbacks are pure functions over (bytes in, bytes
out + stats + queue side-effects); we feed synthetic blocks and assert on the
outputs and counters. The C++ ``jasper_resampler`` extension is used directly
in the cases that exercise the real loop (skipped if it isn't built), and
monkeypatched-to-raise in the fail-soft case.

Coverage (the spec's six cases plus the prime-gate behaviour the design hinges
on):
  1. FLAG OFF (default) ⇒ enqueued payload byte-IDENTICAL to today.
  2. FLAG ON, buffer at target ⇒ ratio ≈ 1, output ≈ input length.
  3. FLAG ON, buffer above target ⇒ ratio_ppm > 0 (drain faster) over a closed
     loop — the capture-follower SIGN.
  4. Hard discontinuity (buffer collapse) ⇒ de-prime + loop reset (resync).
  5. Import failure ⇒ usbsink.ratematch_unavailable logged, stage disabled,
     byte-identical (fail-soft, no crash).
  6. Daemon env parsing: JASPER_USBSINK_RATE_MATCH on/off/garbage → bool with
     fail-soft; target_ms/bw float parse + fail-soft.
  7. Stats + state_publisher: the rate_match block appears only when enabled
     and is null-safe for non-finite ppm.
"""
from __future__ import annotations

import importlib.util
import logging
import queue
import struct

import numpy as np
import pytest

from jasper.usbsink.audio_bridge import (
    BLOCK_FRAMES,
    CHANNELS,
    S32_BYTES,
    AudioBridge,
)

# Many cases need the real C++ resampler; gate them at module level but keep the
# OFF-path and import-failure cases working without it (they don't construct it).
# Probe the compiled submodule (not just the package) so a half-built tree
# without the .so is correctly treated as "not available".
_HAS_RESAMPLER = importlib.util.find_spec("jasper_resampler._resampler") is not None

needs_resampler = pytest.mark.skipif(
    not _HAS_RESAMPLER,
    reason="jasper_resampler C++ extension not built on this host",
)


def _resampler():
    """Import the extension lazily inside a test (the body is gated by
    `needs_resampler`, so this only runs when the spec was found)."""
    import jasper_resampler

    return jasper_resampler


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _s32_block(amp: int, frames: int = BLOCK_FRAMES) -> bytes:
    """Interleaved S32 stereo block at a constant amplitude (PortAudio gives
    the capture callback raw S32 bytes)."""
    return np.full(frames * CHANNELS, amp, dtype=np.int32).tobytes()


def _drive_capture(bridge: AudioBridge, n: int, amp: int = 1 << 26) -> None:
    for _ in range(n):
        bridge._capture_callback(_s32_block(amp), BLOCK_FRAMES, None, None)


# ----------------------------------------------------------------------
# Case 1 — flag OFF is byte-identical (no resampler touched)
# ----------------------------------------------------------------------


def test_flag_off_enqueues_byte_identical_payload_aloop():
    """Default (rate_match=False) aloop mode enqueues exactly the S16 high-half
    view it always has — the byte-identity guarantee."""
    bridge = AudioBridge(rate_match=False)  # aloop default
    blk = _s32_block(0x12345678)
    bridge._capture_callback(blk, BLOCK_FRAMES, None, None)
    arr = np.frombuffer(blk, dtype=np.int32)
    expected = bytes(arr.view(np.int16)[1::2])
    assert bridge._queue.get_nowait() == expected
    # The stage left no state behind.
    assert bridge._rate_match is False
    assert bridge._rate_ctl is None
    assert bridge.rate_match_enabled is False


def test_flag_off_enqueues_byte_identical_payload_fifo():
    """Default fifo mode enqueues the full-width S32 block unchanged."""
    bridge = AudioBridge(rate_match=False, output_mode="fifo")
    blk = _s32_block(0x0BADF00D)
    bridge._capture_callback(blk, BLOCK_FRAMES, None, None)
    assert bridge._queue.get_nowait() == blk


def test_flag_off_consumer_never_paused():
    bridge = AudioBridge(rate_match=False)
    assert bridge._rate_match_consumer_paused() is False


# ----------------------------------------------------------------------
# Case 2 — flag ON, prime gate + buffer at target ⇒ ratio ≈ 1
# ----------------------------------------------------------------------


@needs_resampler
def test_flag_on_primes_then_engages():
    """While priming the consumer is paused and the queue fills without
    draining; once buffered to target the stage engages."""
    bridge = AudioBridge(rate_match=True, output_mode="fifo")
    assert bridge.rate_match_enabled is True
    assert bridge._rate_match_consumer_paused() is True  # not yet primed
    # prime_blocks = ceil(target/block); fill that many blocks.
    for i in range(bridge._rate_prime_blocks + 1):
        _drive_capture(bridge, 1)
    assert bridge._rate_primed is True
    assert bridge._rate_match_consumer_paused() is False


@needs_resampler
def test_flag_on_buffer_at_target_ratio_near_unity():
    """With the buffer held right at target, the engaged loop's error is ~0 so
    the ratio stays near unity and the output length tracks the input."""
    bridge = AudioBridge(rate_match=True, output_mode="fifo")
    # Prime.
    _drive_capture(bridge, bridge._rate_prime_blocks + 1)
    assert bridge._rate_primed
    # Now hold the buffer at target: drain exactly what we add each cycle so the
    # fill stays at target. Simulate by draining one entry per capture.
    last_out_len = None
    for _ in range(200):
        # drain one (keeps buffer ~target)
        try:
            blk = bridge._queue.get_nowait()
            bridge._rate_count_drained(blk)
        except queue.Empty:
            pass
        bridge._capture_callback(_s32_block(1 << 26), BLOCK_FRAMES, None, None)
        last = bridge._queue.queue[-1] if bridge._queue.qsize() else None
        if last is not None:
            last_out_len = len(last)
    # The last resampled block is close to one full block (ratio ≈ 1).
    full = BLOCK_FRAMES * CHANNELS * S32_BYTES
    assert last_out_len is not None
    assert abs(last_out_len - full) <= 4 * CHANNELS * S32_BYTES
    assert abs(bridge.stats.rate_match_ratio_ppm) < 200.0


# ----------------------------------------------------------------------
# Case 3 — capture-follower SIGN over a closed loop
# ----------------------------------------------------------------------


@needs_resampler
def test_buffer_above_target_drives_positive_ratio_closed_loop():
    """A host running faster than the DAC (buffer trends above target) drives
    ratio_ppm > 0, so the resampler emits FEWER frames and the buffer drains —
    the load-bearing capture-follower sign.

    Closed-loop model through the real loop math: the resampler ratio feeds back
    into the buffered-frames signal (enqueued grows by output frames, drained by
    the DAC rate). The frame-accurate counters the bridge maintains are the
    control signal.
    """
    ppm = 80.0  # host faster than DAC
    bridge = AudioBridge(rate_match=True, output_mode="fifo", queue_maxblocks=64)
    # Drive a faithful closed loop: each cycle the host delivers ~1 block; the
    # DAC drains `period` frames; the loop should settle ratio_ppm ~ +ppm.
    period = BLOCK_FRAMES
    produced = period * (1.0 + ppm / 1.0e6)
    owed = 0.0
    front = 0.0
    for _ in range(120_000):
        owed += produced
        while owed >= period:
            owed -= period
            bridge._capture_callback(_s32_block(1 << 24), BLOCK_FRAMES, None, None)
        if not bridge._rate_match_consumer_paused():
            need = float(period)
            while need > 1e-9 and bridge._queue.qsize():
                entry = bridge._queue.queue[0]
                ef = len(entry) // (CHANNELS * S32_BYTES)
                avail = ef - front
                if avail <= need + 1e-9:
                    blk = bridge._queue.get_nowait()
                    bridge._rate_count_drained(blk)
                    need -= avail
                    front = 0.0
                else:
                    front += need
                    need = 0.0
    # The capture-follower sign: a faster host settles to a POSITIVE ratio_ppm.
    assert bridge.stats.rate_match_ratio_ppm > 0.0, (
        f"host-faster must drive ratio>1, got {bridge.stats.rate_match_ratio_ppm}"
    )


@needs_resampler
def test_resample_block_ratio_sign_emits_fewer_frames_when_draining():
    """Unit-level sign check on the binding directly: ratio>1 emits fewer
    output frames than input (consume host faster), ratio<1 emits more."""
    rr = _resampler()
    rc_fast = rr.RateResampler(channels=2, bytes_per_sample=2)
    rc_slow = rr.RateResampler(channels=2, bytes_per_sample=2)
    n = 2048
    sig = struct.pack(
        f"<{n * 2}h",
        *[(i % 100) - 50 for i in range(n * 2)],
    )
    fast = rc_fast.resample_block(sig, 1.01)
    slow = rc_slow.resample_block(sig, 0.99)
    assert len(fast) < len(slow), "ratio>1 must emit fewer frames than ratio<1"


# ----------------------------------------------------------------------
# Case 4 — hard discontinuity de-primes + resyncs
# ----------------------------------------------------------------------


@needs_resampler
def test_buffer_collapse_deprimes_and_resyncs():
    """Once primed, a buffer collapse below the low-water mark (a host
    pause/seek emptied it) de-primes the stage and resets the loop, so playback
    re-primes and the controller re-locks cleanly."""
    bridge = AudioBridge(rate_match=True, output_mode="fifo")
    # Prime, then run a few engaged cycles so the loop has integrated state.
    _drive_capture(bridge, bridge._rate_prime_blocks + 1)
    assert bridge._rate_primed
    for _ in range(50):
        try:
            blk = bridge._queue.get_nowait()
            bridge._rate_count_drained(blk)
        except queue.Empty:
            pass
        bridge._capture_callback(_s32_block(1 << 26), BLOCK_FRAMES, None, None)
    # Now collapse the buffer: drain everything so buffered < low-water.
    while bridge._queue.qsize():
        blk = bridge._queue.get_nowait()
        bridge._rate_count_drained(blk)
    resyncs_before = bridge.stats.rate_match_resync_count
    # The next capture sees a near-empty buffer → de-prime + reset_loop.
    bridge._capture_callback(_s32_block(1 << 26), BLOCK_FRAMES, None, None)
    assert bridge._rate_primed is False, "collapse must de-prime"
    assert bridge.stats.rate_match_resync_count == resyncs_before + 1, (
        "collapse must resync the loop"
    )
    assert bridge.stats.rate_match_locked is False


# ----------------------------------------------------------------------
# Case 5 — import failure fails soft (no crash, byte-identical)
# ----------------------------------------------------------------------


def test_import_failure_disables_stage_and_logs(monkeypatch, caplog):
    """If the jasper_resampler extension can't be imported (a dev laptop
    without the built binding), the bridge logs usbsink.ratematch_unavailable
    and runs with rate-match DISABLED — never crashing the audio daemon, and
    enqueuing byte-identical payloads."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "jasper_resampler" or name.startswith("jasper_resampler."):
            raise ImportError("simulated missing extension")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with caplog.at_level(logging.WARNING):
        bridge = AudioBridge(rate_match=True, output_mode="fifo")

    assert bridge._rate_match is False, "import failure must disable the stage"
    assert bridge._rate_ctl is None
    assert bridge.rate_match_enabled is False
    messages = [r.getMessage() for r in caplog.records]
    assert any("event=usbsink.ratematch_unavailable" in m for m in messages)

    # And it now behaves byte-identically (fifo: full S32 block unchanged).
    blk = _s32_block(0x0BADF00D)
    bridge._capture_callback(blk, BLOCK_FRAMES, None, None)
    assert bridge._queue.get_nowait() == blk


# ----------------------------------------------------------------------
# Case 6 — daemon env parsing
# ----------------------------------------------------------------------


def test_daemon_parses_rate_match_bool_flag():
    from jasper.usbsink import daemon as d

    assert d._parse_bool_flag("on", default=False, name="X") is True
    assert d._parse_bool_flag("1", default=False, name="X") is True
    assert d._parse_bool_flag("true", default=False, name="X") is True
    assert d._parse_bool_flag("off", default=True, name="X") is False
    assert d._parse_bool_flag("0", default=True, name="X") is False
    # Unset → default (default-OFF safety).
    assert d._parse_bool_flag("", default=False, name="X") is False
    # Garbage → default, fail-soft (no crash).
    assert d._parse_bool_flag("maybe", default=False, name="X") is False
    assert d._parse_bool_flag("garbage", default=True, name="X") is True


def test_daemon_parses_rate_match_floats_fail_soft():
    from jasper.usbsink import daemon as d

    assert d._parse_float_env("0.064", default=0.128, name="X") == pytest.approx(0.064)
    assert d._parse_float_env("", default=40.0, name="X") == pytest.approx(40.0)
    # Garbage → default, fail-soft.
    assert d._parse_float_env("xx", default=0.128, name="X") == pytest.approx(0.128)
    assert d._parse_float_env("12abc", default=500.0, name="X") == pytest.approx(500.0)


def test_daemon_from_env_default_off(monkeypatch):
    from jasper.usbsink import daemon as d

    for key in (
        "JASPER_USBSINK_RATE_MATCH",
        "JASPER_USBSINK_RATE_MATCH_TARGET_MS",
        "JASPER_USBSINK_RATE_MATCH_BW",
        "JASPER_USBSINK_RATE_MATCH_MAX_ADJUST_PPM",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = d.DaemonConfig.from_env()
    assert cfg.rate_match is False
    assert cfg.rate_match_target_ms == pytest.approx(40.0)
    assert cfg.rate_match_bw == pytest.approx(0.128)
    assert cfg.rate_match_max_adjust_ppm == pytest.approx(500.0)


def test_daemon_from_env_on(monkeypatch):
    from jasper.usbsink import daemon as d

    monkeypatch.setenv("JASPER_USBSINK_RATE_MATCH", "on")
    monkeypatch.setenv("JASPER_USBSINK_RATE_MATCH_TARGET_MS", "30")
    monkeypatch.setenv("JASPER_USBSINK_RATE_MATCH_BW", "0.064")
    monkeypatch.setenv("JASPER_USBSINK_RATE_MATCH_MAX_ADJUST_PPM", "250")
    cfg = d.DaemonConfig.from_env()
    assert cfg.rate_match is True
    assert cfg.rate_match_target_ms == pytest.approx(30.0)
    assert cfg.rate_match_bw == pytest.approx(0.064)
    assert cfg.rate_match_max_adjust_ppm == pytest.approx(250.0)


# ----------------------------------------------------------------------
# Case 7 — state_publisher rate_match block
# ----------------------------------------------------------------------


class _FakeStats:
    rate_match_ratio_ppm = 12.5
    rate_match_err_frames = -3.0
    rate_match_locked = True
    rate_match_resync_count = 2
    rate_match_clamp_count = 0
    rate_match_qfill_frames = 1920


class _FakeBridge:
    def __init__(self, *, enabled: bool, ppm: float = 12.5):
        self._enabled = enabled
        self.is_preempted = False
        self.last_rms_dbfs = -20.0
        self.stats = _FakeStats()
        self.stats.rate_match_ratio_ppm = ppm

    @property
    def rate_match_enabled(self) -> bool:
        return self._enabled


def _make_publisher(bridge, tmp_path):
    from jasper.usbsink.state_publisher import StatePublisher

    return StatePublisher(
        bridge,
        state_path=str(tmp_path / "state.json"),
        host_card_path=str(tmp_path / "nope"),
    )


def test_state_omits_rate_match_when_disabled(tmp_path):
    import json

    pub = _make_publisher(_FakeBridge(enabled=False), tmp_path)
    pub._write_state()
    data = json.loads((tmp_path / "state.json").read_text())
    assert "rate_match" not in data, (
        "default state.json must be byte-identical (no rate_match block)"
    )
    # The legacy keys are still present + unchanged.
    assert set(data) == {
        "playing", "preempted", "host_connected", "rms_dbfs", "updated_at",
    }


def test_state_includes_rate_match_when_enabled(tmp_path):
    import json

    pub = _make_publisher(_FakeBridge(enabled=True), tmp_path)
    pub._write_state()
    data = json.loads((tmp_path / "state.json").read_text())
    rm = data["rate_match"]
    assert rm["enabled"] is True
    assert rm["ratio_ppm"] == pytest.approx(12.5)
    assert rm["err_frames"] == pytest.approx(-3.0)
    assert rm["locked"] is True
    assert rm["resync_count"] == 2
    assert rm["qfill_frames"] == 1920


def test_state_rate_match_null_safe_for_non_finite_ppm(tmp_path):
    import json

    bridge = _FakeBridge(enabled=True, ppm=float("inf"))
    pub = _make_publisher(bridge, tmp_path)
    # allow_nan=False JSON would raise if ppm were written as inf; the publisher
    # must coerce non-finite to null.
    pub._write_state()
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["rate_match"]["ratio_ppm"] is None
