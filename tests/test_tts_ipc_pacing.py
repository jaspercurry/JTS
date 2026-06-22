# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression: TTS IPC writes must pace to the owner's pending budget.

jasper-fanin's TTS lane accepts audio into a bounded pending queue
(``DEFAULT_MAX_PENDING_FRAMES`` in rust/jasper-fanin/src/tts.rs — 2 s
at 48 kHz) and DROPS whole audio commands that arrive while the queue
is full. It cannot block the socket reader instead: a blocked reader
would stall FLUSH (barge-in) behind queued audio. OpenAI Realtime
delivers replies faster than realtime (~11 s of audio in ~4 s), so an
unpaced writer overflowed that budget and the surviving chunks played
as garbled "fast-forward" audio, while the daemon's sample-counted
drain accounting (which counts the dropped chunks too) held the turn
open to the reply's full length — observed on JTS3 on 2026-06-11 as 82
``event=fanin.tts_command_dropped reason=pending_budget_exceeded``
journal lines lining up with every long reply.

The fix paces ``OutputdTtsPlayout.write_segment``: before each IPC
chunk, sleep off whatever queued-ahead time exceeds
``_OUTPUTD_PACE_AHEAD_SEC``. These tests pin:

* writes under the watermark do not sleep (no added latency for the
  short replies / chirps / clicks that fit the budget);
* a write that would land beyond the watermark sleeps the excess off
  before hitting the socket;
* the Python watermark stays safely under the Rust budget — parsed
  from the Rust source so the two constants cannot silently drift.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np

import jasper.audio_io as audio_io_mod
from jasper.audio_io import OutputdTtsPlayout


class _CaptureStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def set_gain_db(self, db: float) -> None:
        pass

    def start_segment(self, *, kind, provider_item_id, profile=None) -> None:
        pass

    def end_segment(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.writes.append(data)


def _make_playout(monkeypatch) -> tuple[OutputdTtsPlayout, _CaptureStream]:
    import scipy.signal

    monkeypatch.setattr(
        scipy.signal,
        "resample_poly",
        lambda arr, *, up, down: arr,
    )
    p = OutputdTtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        output_rate=48000,
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    stream = _CaptureStream()
    p._stream = stream  # type: ignore[assignment]
    return p, stream


async def test_write_under_watermark_does_not_sleep(monkeypatch):
    p, stream = _make_playout(monkeypatch)
    sleeps: list[float] = []

    async def spy_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(audio_io_mod, "_pace_sleep", spy_sleep)
    # 0.1 s of audio with an empty ring — far below the watermark.
    mono = np.zeros(4800, dtype=np.int16)

    await p.write_segment(mono.tobytes(), segment_kind="assistant")

    assert stream.writes  # audio reached the socket
    assert sleeps == []  # no pacing sleep on the fast path
    assert p.take_paced_sec() == 0.0


async def test_write_beyond_watermark_paces_off_the_excess(monkeypatch):
    p, stream = _make_playout(monkeypatch)
    # Ring already holds watermark + 0.15 s of un-played audio: the next
    # chunk must sleep ~0.15 s before writing so the owner's queue stays
    # under budget. Lower bound is the assertion (sleep(x) >= x); no
    # tight upper bound so a loaded CI runner can't flake it.
    p._ring_end_monotonic = (
        time.monotonic() + audio_io_mod._OUTPUTD_PACE_AHEAD_SEC + 0.15
    )
    mono = np.zeros(4800, dtype=np.int16)  # 0.1 s — one IPC chunk

    start = time.monotonic()
    await p.write_segment(mono.tobytes(), segment_kind="assistant")
    elapsed = time.monotonic() - start

    assert stream.writes
    assert elapsed >= 0.14  # slept the excess off (minus clock grain)


async def test_burst_write_is_paced_to_realtime(monkeypatch):
    """A faster-than-realtime burst (the OpenAI delivery shape) drains
    at ~realtime instead of overflowing the owner's queue: total wall
    time for 0.7 s of audio written instantly must be >= 0.7 s minus
    the watermark."""
    monkeypatch.setattr(audio_io_mod, "_OUTPUTD_PACE_AHEAD_SEC", 0.2)
    p, stream = _make_playout(monkeypatch)
    mono = np.zeros(4800, dtype=np.int16)  # 0.1 s per write

    start = time.monotonic()
    for _ in range(7):  # 0.7 s of audio, written back-to-back
        await p.write_segment(mono.tobytes(), segment_kind="assistant")
    elapsed = time.monotonic() - start

    assert len(stream.writes) == 7
    # Steady state holds (watermark + one 0.1 s chunk) queued ahead, so
    # pacing sleeps total 0.7 − 0.2 − 2×0.1 ≈ 0.4 s. Unpaced, this loop
    # completes in ~0.01 s — the bound proves pacing engaged.
    assert elapsed >= 0.35


async def test_paced_time_is_accounted_and_taken(monkeypatch):
    """Pacing sleeps accumulate into take_paced_sec() — the turn-ended
    log's `paced` field — and the counter resets on read, so over-pacing
    is visible in the journal rather than only inferable from latency."""
    p, stream = _make_playout(monkeypatch)
    sleeps: list[float] = []

    async def spy_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(audio_io_mod, "_pace_sleep", spy_sleep)
    # Ring already 0.30 s past the watermark: the single-IPC-chunk write
    # below must sleep that excess and account for it.
    p._ring_end_monotonic = (
        time.monotonic() + audio_io_mod._OUTPUTD_PACE_AHEAD_SEC + 0.30
    )
    mono = np.zeros(4800, dtype=np.int16)  # 0.1 s — one IPC chunk

    await p.write_segment(mono.tobytes(), segment_kind="assistant")

    assert len(sleeps) == 1
    taken = p.take_paced_sec()
    assert abs(taken - sleeps[0]) < 1e-9
    assert 0.25 <= taken <= 0.35  # ~the 0.30 s excess, minus clock grain
    assert p.take_paced_sec() == 0.0  # read resets


def test_pace_watermark_stays_under_fanin_budget():
    """Cross-language contract: the Python pace-ahead watermark plus one
    IPC chunk must stay under jasper-fanin's pending-frames budget with
    margin, or sustained writes start dropping again. Parsed from the
    Rust source so a budget change over there fails this test instead
    of silently reintroducing the garble."""
    tts_rs = (
        Path(__file__).resolve().parents[1]
        / "rust" / "jasper-fanin" / "src" / "tts.rs"
    ).read_text()
    m = re.search(
        r"DEFAULT_MAX_PENDING_FRAMES:\s*u64\s*=\s*([0-9_]+)\s*\*\s*([0-9_]+)",
        tts_rs,
    )
    assert m, (
        "DEFAULT_MAX_PENDING_FRAMES literal not found in "
        "rust/jasper-fanin/src/tts.rs — if its shape changed, update this "
        "test so the pace-ahead/budget contract stays pinned."
    )
    budget_frames = int(m.group(1).replace("_", "")) * int(
        m.group(2).replace("_", "")
    )
    budget_sec = budget_frames / audio_io_mod._OUTPUTD_SAMPLE_RATE
    ipc_chunk_sec = audio_io_mod._OUTPUTD_MAX_AUDIO_CHUNK_BYTES / (
        audio_io_mod._OUTPUTD_SAMPLE_RATE
        * audio_io_mod._OUTPUTD_AUDIO_FRAME_BYTES
    )
    # Worst-case pending at the owner: watermark + the chunk in flight.
    # Keep >= 0.25 s of margin for event-loop jitter.
    assert (
        audio_io_mod._OUTPUTD_PACE_AHEAD_SEC + ipc_chunk_sec
        <= budget_sec - 0.25
    )
