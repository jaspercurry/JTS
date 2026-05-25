"""Tests for TtsPlayout's gain handling AND its end-of-stream drain
primitive.

The hard MIN/MAX clamp on TtsPlayout.set_gain_db is the load-bearing
defense against accidentally playing TTS at ear-damaging levels. These
tests pin that contract: even if the volume tracker, env config, or
Camilla websocket all misbehave, no caller can push gain above
MAX_TTS_GAIN_DB.

The drain primitive (``expected_drain_at`` / ``wait_drained``) is the
load-bearing defense against the *opposite* failure: ending the turn
before the last sample exits the DAC. The orchestrator (idle watchdog
+ play-loop) anchors end-of-turn on this primitive, so its math has
to track real ring contents through write/idle/flush/append cycles.

We don't open a real ALSA stream — the API surface under test lives
outside the stream lifecycle. Where the drain tests need to drive
``write()`` we monkeypatch the stream to a no-op.
"""
from __future__ import annotations

import asyncio
import math
import time

import numpy as np
import pytest

from jasper.audio_io import TtsPlayout


def _make() -> TtsPlayout:
    """Construct without entering the async context (no ALSA open)."""
    return TtsPlayout(device="dummy", output_rate=48000, gain_db=-8.0)


class _NoopStream:
    """Stand-in for sounddevice.RawOutputStream — write() is a no-op so
    the drain math is driven by sample counts, not real audio. abort()
    and start() are also no-ops so flush() can exercise its reset path
    without a real PortAudio handle.
    """

    def write(self, _data: bytes) -> None:
        pass

    def abort(self) -> None:
        pass

    def start(self) -> None:
        pass


def _make_with_stream(*, drain_tail_sec: float = 0.0) -> TtsPlayout:
    """Construct with a no-op stream attached, bypassing __aenter__.
    Defaults the tail to 0 so deadline math is purely sample-counted
    and easy to assert against."""
    p = TtsPlayout(
        device="dummy",
        output_rate=48000,
        gain_db=-8.0,
        drain_tail_sec=drain_tail_sec,
    )
    p._stream = _NoopStream()  # type: ignore[assignment]
    return p


def _silence_pcm(*, sec: float, rate: int = TtsPlayout.INPUT_RATE) -> bytes:
    """Build a mono int16 PCM blob of the requested duration. The
    drain math keys off byte count, not amplitude, so all-zeros is
    fine."""
    n = int(round(sec * rate))
    return np.zeros(n, dtype=np.int16).tobytes()


def test_constructor_clamps_through_set_gain_db():
    """Whatever the env passes, the constructor routes it through the
    same clamp/validate path as runtime updates."""
    p = TtsPlayout(device="dummy", output_rate=48000, gain_db=-8.0)
    assert p.gain_db == -8.0


def test_max_gain_clamp():
    """Even if a future caller passes 0 dB or higher (which the config
    validator should already block), TtsPlayout must clamp to MAX."""
    p = _make()
    p.set_gain_db(0.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB
    p.set_gain_db(20.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB
    p.set_gain_db(1000.0)
    assert p.gain_db == TtsPlayout.MAX_TTS_GAIN_DB


def test_min_gain_clamp():
    """Floor exists so 'mute' / unreachable-Camilla can fall to silence
    without integer-underflow into bizarre territory."""
    p = _make()
    p.set_gain_db(-100.0)
    assert p.gain_db == TtsPlayout.MIN_TTS_GAIN_DB
    p.set_gain_db(-1e6)
    assert p.gain_db == TtsPlayout.MIN_TTS_GAIN_DB


def test_in_range_passes_through():
    p = _make()
    p.set_gain_db(-12.5)
    assert p.gain_db == -12.5
    p.set_gain_db(-30.0)
    assert p.gain_db == -30.0


def test_non_finite_inputs_held():
    """NaN / inf must not corrupt gain — hold the prior value."""
    p = _make()
    p.set_gain_db(-15.0)
    p.set_gain_db(float("nan"))
    assert p.gain_db == -15.0
    p.set_gain_db(float("inf"))
    assert p.gain_db == -15.0
    p.set_gain_db(float("-inf"))
    assert p.gain_db == -15.0


def test_garbage_inputs_held():
    p = _make()
    p.set_gain_db(-12.0)
    p.set_gain_db(None)  # type: ignore[arg-type]
    assert p.gain_db == -12.0
    p.set_gain_db("loud")  # type: ignore[arg-type]
    assert p.gain_db == -12.0
    p.set_gain_db([0.0])  # type: ignore[arg-type]
    assert p.gain_db == -12.0


def test_linear_gain_matches_db():
    """Sanity-check the dB → linear conversion at the boundaries."""
    p = _make()
    p.set_gain_db(0.0)  # clamps to -6
    expected = 10 ** (TtsPlayout.MAX_TTS_GAIN_DB / 20.0)
    assert math.isclose(p._gain_linear, expected, rel_tol=1e-9)
    p.set_gain_db(-20.0)
    assert math.isclose(p._gain_linear, 0.1, rel_tol=1e-9)


def test_max_below_zero_dbfs():
    """Sanity: MAX must be <= 0 dB. If someone bumps the constant
    positive, gain math overflows int16 against Gemini's source peaks."""
    assert TtsPlayout.MAX_TTS_GAIN_DB <= 0.0
    assert TtsPlayout.MIN_TTS_GAIN_DB < TtsPlayout.MAX_TTS_GAIN_DB


# ---------------------------------------------------------------------------
# Drain primitive — TtsPlayout.expected_drain_at / wait_drained
#
# Contract: end-of-turn timing anchors on samples ACTUALLY QUEUED to the
# audio stack. Idle watchdog and play loop both consult the same deadline,
# so it has to be exact across the relevant transitions: cold start,
# busy append, idle rollover, barge-in flush, and tail-override.
# ---------------------------------------------------------------------------


def test_drain_idle_when_nothing_written():
    """Sentinel: a freshly-constructed player reports 0.0 (= drained)."""
    p = _make_with_stream()
    assert p.expected_drain_at() == 0.0


async def test_wait_drained_returns_immediately_when_idle():
    """wait_drained on an idle player must NOT sleep — the watchdog
    polls it on the hot path between mic frames."""
    p = _make_with_stream()
    start = time.monotonic()
    await p.wait_drained()
    elapsed = time.monotonic() - start
    assert elapsed < 0.005  # well under one event-loop tick


async def test_drain_deadline_includes_chunk_and_tail():
    """Writing N seconds of audio sets the deadline to roughly
    now + N + tail. Exercises both the sample-counted ring deadline
    AND the configured tail being applied (regression catch: if
    the `+ self._drain_tail_sec` in `expected_drain_at` gets removed,
    this test fails)."""
    tail_sec = 0.05
    p = _make_with_stream(drain_tail_sec=tail_sec)
    chunk_sec = 0.4
    before = time.monotonic()
    await p.write(_silence_pcm(sec=chunk_sec))
    deadline = p.expected_drain_at()
    # Lower bound: ring_end ≥ before + chunk; deadline = ring_end + tail.
    # Upper bound: slack of 0.25 s absorbs to_thread scheduling jitter
    # on noisy CI machines.
    assert before + chunk_sec + tail_sec <= deadline
    assert deadline <= time.monotonic() + chunk_sec + tail_sec + 0.25


async def test_drain_appends_when_speaker_busy():
    """Back-pressure case: two writes in quick succession queue
    end-to-end. Deadline = now + 2 * chunk_duration, not now + chunk
    (which would be the wrong "stream restarted from idle" answer)."""
    p = _make_with_stream()
    chunk_sec = 0.4
    before = time.monotonic()
    await p.write(_silence_pcm(sec=chunk_sec))
    await p.write(_silence_pcm(sec=chunk_sec))
    deadline = p.expected_drain_at()
    assert before + 2 * chunk_sec <= deadline
    assert deadline <= time.monotonic() + 2 * chunk_sec + 0.05


async def test_drain_anchors_fresh_after_idle_gap(monkeypatch):
    """The opposite of the append case: if enough wall-clock has
    passed that the prior deadline is in the past, the next write must
    anchor on now() — NOT chain onto the stale deadline.

    Without this, an idle daemon would push every subsequent end-of-turn
    further into the future based on every cue / chirp ever written.
    """
    p = _make_with_stream()
    chunk_sec = 0.1
    await p.write(_silence_pcm(sec=chunk_sec))
    first_deadline = p.expected_drain_at()

    # Fast-forward our notion of "now" past the first deadline. The
    # write code only reads time.monotonic() in audio_io, so patching
    # there is sufficient.
    import jasper.audio_io as audio_io_mod
    fake_now = first_deadline + 1.0
    monkeypatch.setattr(audio_io_mod.time, "monotonic", lambda: fake_now)

    await p.write(_silence_pcm(sec=chunk_sec))
    second_deadline = p.expected_drain_at()
    # New deadline is anchored on the fake "now", not chained to the
    # first.
    assert second_deadline == pytest.approx(fake_now + chunk_sec, abs=1e-6)


async def test_drain_resets_on_flush():
    """Barge-in (`flush`) discards the ring; the tracked deadline
    must reset so the next write anchors fresh on now()."""
    p = _make_with_stream()
    await p.write(_silence_pcm(sec=2.0))  # would-be-long deadline
    assert p.expected_drain_at() > time.monotonic() + 1.0
    await p.flush()
    assert p.expected_drain_at() == 0.0


async def test_wait_drained_sleeps_until_deadline():
    """End-to-end: wait_drained on a non-idle player blocks until
    the deadline elapses (within event-loop scheduling jitter)."""
    p = _make_with_stream()
    wait_sec = 0.05  # keep the test fast
    await p.write(_silence_pcm(sec=wait_sec))
    start = time.monotonic()
    await p.wait_drained()
    elapsed = time.monotonic() - start
    # Lower bound: we did wait. Upper bound: not more than 50 ms slop.
    assert elapsed >= wait_sec - 0.005
    assert elapsed < wait_sec + 0.05


async def test_drain_unchanged_after_empty_write():
    """Defensive: a zero-byte PCM write must not corrupt the drain
    sentinel. Without the early-return guard, ``len(pcm)=0`` would
    set ``_ring_end_monotonic = now + 0``, masking the idle state."""
    p = _make_with_stream()
    await p.write(b"")
    assert p.expected_drain_at() == 0.0
