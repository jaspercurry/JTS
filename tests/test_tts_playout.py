# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for TtsPlayout's gain handling AND its end-of-stream drain
primitive.

TtsPlayout.set_gain_db validates the legacy direct-device gain path:
non-finite inputs are rejected, and extreme attenuation floors to a
mute-equivalent minimum. The active runtime path no longer has a fixed
max TTS gain clamp; assistant loudness is matched by fan-in/outputd and
bounded by the peak-aware decision there.

The drain primitive (``expected_drain_at`` / ``wait_drained``) is the
load-bearing defense against the *opposite* failure: ending the turn
before the last sample exits the DAC. The orchestrator (idle watchdog
+ play-loop) anchors end-of-turn on this primitive, so its math has
to track real ring contents through write/idle/flush/append cycles.

``expected_drain_at`` / ``wait_drained`` are exercised directly against a
bare ``TtsPlayout`` — no stream needed, the deadline is a plain field.
The ring-population side — the write path that advances the deadline as
audio is queued — needs a capturing fake stream (``_CaptureOutputdStream``)
instead of opening a real socket.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import numpy as np
import pytest

import jasper.tts_playout as tts_mod
from jasper.assistant_loudness import (
    UPSAMPLE_2X_CONTEXT,
    AssistantLoudnessProfile,
    LoudnessMeasurement,
    upsample_2x,
)
from jasper.tts_playout import TtsPlayout

from ._async_wait import wait_signalled


def _make() -> TtsPlayout:
    """Construct without entering the async context (no ALSA open)."""
    return TtsPlayout(gain_db=-8.0)


class _CaptureOutputdStream:
    def __init__(self) -> None:
        self.gains: list[float] = []
        self.writes: list[bytes] = []
        self.segments_started: list[tuple[str, str | None, object | None]] = []
        self._active_segment: tuple[str, str | None, object | None] | None = None
        self.segments_ended = 0
        self.flush_acks: list[dict] = []
        self.prepares: list[tuple[str, str, str, float]] = []
        self.volume_contexts: list[object | None] = []
        self.meter_pauses = 0
        self.meter_resumes = 0
        self.ducks: list[bool] = []

    def set_gain_db(self, db: float) -> None:
        self.gains.append(db)

    def program_duck(self, on: bool) -> None:
        self.ducks.append(on)

    def prepare_assistant(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        volume_context=None,
    ) -> None:
        self.prepares.append((provider, model, voice, tts_envelope_lufs))
        self.volume_contexts.append(volume_context)

    def pause_content_meter(self) -> None:
        self.meter_pauses += 1

    def resume_content_meter(self) -> None:
        self.meter_resumes += 1

    def start_segment(
        self,
        *,
        kind: str,
        provider_item_id: str | None,
        profile=None,
    ) -> None:
        segment = (kind, provider_item_id, profile)
        if self._active_segment == segment:
            return
        self._active_segment = segment
        self.segments_started.append(segment)

    def end_segment(self) -> None:
        self.segments_ended += 1
        self._active_segment = None

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def abort(self) -> None:
        pass

    def flush_sync(self) -> dict:
        ack = {
            "ok": True,
            "requests": 1,
            "pending_frames": 2400,
            "events": [{"segment": 1, "kind": "assistant", "provider_item_id": None,
                        "queued_frames": 8400, "written_frames": 6000,
                        "drained_frames": 6000, "flushed_frames": 2400}],
            "segments": 1,
            "flushed_frames": 2400,
            "max_audio_played_ms": 125,
        }
        self.flush_acks.append(ack)
        return ack

    def start(self) -> None:
        pass


def _make_outputd(*, drain_tail_sec: float = 0.0) -> TtsPlayout:
    """TtsPlayout wired to a capturing fake stream, bypassing
    __aenter__ (no real socket)."""
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=drain_tail_sec,
    )
    p._stream = _CaptureOutputdStream()  # type: ignore[assignment]
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
    p = TtsPlayout(gain_db=-8.0)
    assert p.gain_db == -8.0


def test_positive_gain_passes_through():
    p = _make()
    p.set_gain_db(0.0)
    assert p.gain_db == 0.0
    p.set_gain_db(20.0)
    assert p.gain_db == 20.0
    p.set_gain_db(1000.0)
    assert p.gain_db == 1000.0


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


def test_no_fixed_max_tts_gain_ceiling():
    """Regression: the old -6 dB ceiling must not come back and fight
    assistant loudness matching."""
    assert not hasattr(TtsPlayout, "MAX_" + "TTS_GAIN_DB")


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
    p = _make()
    assert p.expected_drain_at() == 0.0


async def test_wait_drained_returns_immediately_when_idle():
    """wait_drained on an idle player must NOT sleep — the watchdog
    polls it on the hot path between mic frames."""
    p = _make()
    start = time.monotonic()
    await p.wait_drained()
    elapsed = time.monotonic() - start
    assert elapsed < 0.005  # well under one event-loop tick


def test_drain_deadline_includes_chunk_and_tail():
    """`expected_drain_at` adds the configured tail to whatever ring
    deadline is queued (regression catch: if the `+ self._drain_tail_sec`
    in `expected_drain_at` gets removed, this test fails).

    The ring deadline itself is set directly here — populating it as
    audio is written is the write path's job, pinned separately below
    via ``_make_outputd``."""
    p = TtsPlayout(drain_tail_sec=0.05)
    ring_end = time.monotonic() + 0.4
    p._ring_end_monotonic = ring_end
    assert p.expected_drain_at() == pytest.approx(ring_end + 0.05, abs=1e-9)


async def test_wait_drained_requests_the_full_remaining_deadline(monkeypatch):
    """wait_drained must ask for (at least) the deadline's remaining
    duration — the mechanism the idle watchdog leans on to never end a
    turn before the last sample exits the DAC.

    A prior version of this test measured real wall-clock elapsed time
    against a ~50ms + 50ms-slack budget. That flaked under adversarial
    parallel load (observed elapsed 0.2989s vs. a 0.0998s budget — see
    https://github.com/jaspercurry/JTS/issues/2066): `asyncio.sleep` only
    guarantees it won't wake *early*, and has no upper bound on how late
    a CPU-starved event loop resumes the sleeper. That is a scheduler
    property outside wait_drained's own control, not a correctness bug —
    ending a turn *early* is the failure mode this primitive defends
    against, not sleeping "too long" under contention.

    So pin the mechanism instead of the wall clock: jasper/tts_playout.py
    imports `asyncio` as a full module, so `tts_mod.asyncio.sleep`
    is patchable from the test side. Recording the requested duration
    instead of actually sleeping removes the flaky scheduler dependency
    entirely while still asserting the one thing wait_drained computes
    and controls.
    """
    p = _make()
    wait_sec = 0.05  # keep the test fast
    p._ring_end_monotonic = time.monotonic() + wait_sec
    remaining = p.expected_drain_at() - time.monotonic()
    assert remaining > 0.0

    requested: list[float] = []

    async def spy_sleep(delay: float) -> None:
        requested.append(delay)

    monkeypatch.setattr(tts_mod.asyncio, "sleep", spy_sleep)
    await p.wait_drained()

    assert len(requested) == 1
    # No real sleep happens on either side of this comparison, so the only
    # gap between `remaining` (read above) and wait_drained's own internal
    # read of the same monotonic clock is a few microseconds of Python
    # execution — not scheduler jitter. A 10ms epsilon stays far below the
    # 50ms `wait_sec` used here, so a real logic bug (stale ring end,
    # wrong chunk duration, double-counting the tail) still fails loudly.
    assert requested[0] == pytest.approx(remaining, abs=0.01)


# The ring-population side of the drain primitive (deadline chaining across
# busy writes, resetting when stale, the empty-write guard) is exercised via
# _make_outputd, with a capturing fake stream attached.


async def test_drain_appends_when_speaker_busy():
    """Back-pressure case: two writes in quick succession queue
    end-to-end. Deadline = now + 2 * chunk_duration, not now + chunk
    (which would be the wrong "stream restarted from idle" answer)."""
    p = _make_outputd()
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
    p = _make_outputd()
    chunk_sec = 0.1
    await p.write(_silence_pcm(sec=chunk_sec))
    first_deadline = p.expected_drain_at()

    # Fast-forward our notion of "now" past the first deadline. The
    # write code only reads time.monotonic() in tts_playout, so patching
    # there is sufficient.
    fake_now = first_deadline + 1.0
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: fake_now)

    await p.write(_silence_pcm(sec=chunk_sec))
    second_deadline = p.expected_drain_at()
    # New deadline is anchored on the fake "now", not chained to the
    # first.
    assert second_deadline == pytest.approx(fake_now + chunk_sec, abs=1e-6)


async def test_drain_unchanged_after_empty_write():
    """Defensive: a zero-byte PCM write must not corrupt the drain
    sentinel. Without the early-return guard, ``len(pcm)=0`` would
    set ``_ring_end_monotonic = now + 0``, masking the idle state."""
    p = _make_outputd()
    await p.write(b"")
    assert p.expected_drain_at() == 0.0


@pytest.mark.parametrize("state", ["empty", "closed", "unavailable", "refused", "accepted"])
async def test_write_segment_reports_transport_acceptance(monkeypatch, state):
    p = _make_outputd()
    observed = []

    async def first_write():
        observed.append(p.expected_drain_at())

    if state == "closed":
        p._stream = None
    elif state == "unavailable":
        async def unavailable():
            return None
        monkeypatch.setattr(p, "_current_outputd_stream", unavailable)
    elif state == "refused":
        p.set_emission_admission(lambda: "measurement")
    accepted = await p.write_segment(
        b"" if state == "empty" else _silence_pcm(sec=0.01),
        on_first_write=first_write,
    )
    assert accepted is (state == "accepted")
    assert len(observed) == int(accepted)
    if observed:
        assert observed[0] > 0


def _speech_like_24k(chunks: int, chunk_samples: int) -> np.ndarray:
    """A band-limited 24 kHz signal long enough to hold `chunks` joins."""
    t = np.arange(chunks * chunk_samples) / TtsPlayout.INPUT_RATE
    tone = (
        8000.0 * np.sin(2 * np.pi * 440.0 * t)
        + 4000.0 * np.sin(2 * np.pi * 1970.0 * t)
        + 1500.0 * np.sin(2 * np.pi * 5000.0 * t)
    )
    return tone.astype(np.int16)


def _played_mono(stream) -> np.ndarray:
    """The mono 48 kHz signal behind a capture stream's stereo writes."""
    return np.frombuffer(b"".join(stream.writes), dtype=np.int16)[::2]


def _playout_with_capture_stream():
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=0.0,
        drain_tail_sec=0.0,
        # The comparisons below are in i16 sample units.
        wire_wide=False,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]
    return p, stream


async def test_chunked_upsample_matches_the_whole_signal_at_every_join():
    """Provider audio arrives as ~95 ms deltas and each was resampled on its
    own, so both edges of every chunk were interpolated against silence — a
    tick at ~10 joins a second. Chunked playout now reproduces the
    whole-signal resample, delayed by the context it carries, and still emits
    exactly two output samples per input sample.
    """
    chunk = int(TtsPlayout.INPUT_RATE * 0.095)
    source = _speech_like_24k(10, chunk)
    p, stream = _playout_with_capture_stream()

    for start in range(0, source.size, chunk):
        await p.write(source[start:start + chunk].tobytes())

    played = _played_mono(stream).astype(np.float64)
    assert played.size == 2 * source.size
    reference = upsample_2x(source.astype(np.float64))
    delay = 2 * UPSAMPLE_2X_CONTEXT
    error = np.max(np.abs(played[delay:] - reference[: played.size - delay]))
    peak = np.max(np.abs(reference))
    assert 20 * np.log10(max(error, 1e-9) / peak) <= -60.0


@pytest.mark.parametrize("boundary", ["flush", "end_segment"])
async def test_a_finished_segment_starts_the_upsampler_from_silence(boundary):
    """A flush cuts the lane off mid-reply and end_segment closes a reply:
    either way the next segment is different audio, so none of the last one
    may bleed into its leading interpolation."""
    chunk = int(TtsPlayout.INPUT_RATE * 0.095)
    p, stream = _playout_with_capture_stream()

    await p.write(_speech_like_24k(1, chunk).tobytes())
    await getattr(p, boundary)()
    stream.writes.clear()
    await p.write(np.zeros(chunk, dtype=np.int16).tobytes())

    assert not _played_mono(stream).any()


async def test_outputd_transport_sends_gain_metadata_without_pregain(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=TtsPlayout.MIN_TTS_GAIN_DB,
        drain_tail_sec=0.0,
        # STATED, not inherited. The byte-level expectations below are S16, and
        # what the box the suite runs on RESOLVES is not this test's subject —
        # tests/test_tts_wire_width.py owns that question. An undeclared box now
        # resolves WIDE (ADR-0100: undeclared is the ring), so leaving this to
        # the resolver made these assertions depend on the host's /var/lib state.
        wire_wide=False,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([10000, -10000], dtype=np.int16)
    await p.write(mono.tobytes())

    assert stream.gains == [TtsPlayout.MIN_TTS_GAIN_DB]
    assert stream.segments_started == [("assistant", None, None)]
    assert stream.writes == [
        np.array([10000, 10000, -10000, -10000], dtype=np.int16).tobytes()
    ]
    assert p.expected_drain_at() != 0.0


async def test_outputd_transport_chunks_long_payloads_on_frame_boundaries(monkeypatch):

    monkeypatch.setattr(tts_mod, "_OUTPUTD_MAX_AUDIO_CHUNK_BYTES", 8)
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
        # S16 frame bytes are what the chunk boundaries below are counted in.
        wire_wide=False,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2, 3, 4, 5], dtype=np.int16)
    await p.write(mono.tobytes())

    stereo = np.repeat(mono, 2).tobytes()
    assert stream.gains == [-8.0]
    assert stream.writes == [stereo[:8], stereo[8:16], stereo[16:]]


async def test_outputd_partial_write_keeps_accepted_prefix_in_drain_ledger(
    monkeypatch,
):

    class _FailSecondWrite(_CaptureOutputdStream):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def write(self, data: bytes) -> None:
            self.attempts += 1
            if self.attempts == 2:
                raise OSError("second AUDIO command failed")
            super().write(data)

    monkeypatch.setattr(tts_mod, "_OUTPUTD_MAX_AUDIO_CHUNK_BYTES", 8)
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=1.0,
    )
    stream = _FailSecondWrite()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2, 3, 4, 5], dtype=np.int16)
    accepted = []

    async def first_write():
        accepted.append(len(stream.writes))

    with pytest.raises(OSError):
        await p.write_segment(mono.tobytes(), on_first_write=first_write)

    assert accepted == [1]
    assert stream.attempts == 2
    assert len(stream.writes) == 1
    assert p._ring_end_monotonic is not None
    assert p.expected_drain_at() > time.monotonic()


async def test_cancelled_write_observes_accepted_chunk_before_exit():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class BlockedStream(_CaptureOutputdStream):
        def write(self, data):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            super().write(data)

    p = _make_outputd()
    p._stream = stream = BlockedStream()
    observed = []

    async def first_write():
        observed.append(len(stream.writes))

    writing = asyncio.create_task(p.write_segment(
        _silence_pcm(sec=0.01), on_first_write=first_write,
    ))
    try:
        await wait_signalled(entered, "transport write", producer=writing)
        writing.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    assert observed == [1]
    assert p.expected_drain_at() > 0


@pytest.mark.parametrize("method", ["set_gain_db", "start_segment", "end_segment", "flush_sync"])
async def test_cancelled_output_control_owns_its_thread(method):
    loop = asyncio.get_running_loop()
    entered, completed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    p = _make_outputd()

    def control(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1)
        loop.call_soon_threadsafe(completed.set)

    setattr(p._stream, method, control)
    operation = (p.flush() if method == "flush_sync" else
                 p.end_segment() if method == "end_segment" else
                 p.write_segment(_silence_pcm(sec=0.01)))
    pending = asyncio.create_task(operation)
    try:
        await wait_signalled(entered, method, producer=pending)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        pending.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert completed.is_set()
    assert p._stream.writes == []


@pytest.mark.parametrize("ack", [None, {}, [], {"ok": False}, {"ok": True}])
async def test_unconfirmed_flush_preserves_the_drain_deadline(ack):
    p = _make_outputd()
    await p.write(_silence_pcm(sec=0.01))
    deadline = p.expected_drain_at()
    p._stream.flush_sync = lambda: ack
    assert await p.flush() is None
    assert p.expected_drain_at() == deadline
    await asyncio.gather(*p._profile_save_tasks)


async def test_outputd_transport_sends_provider_segment_identity(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2], dtype=np.int16)
    await p.write_segment(
        mono.tobytes(),
        provider_item_id="msg_abc123",
        segment_kind="assistant",
    )
    await p.end_segment()

    assert stream.segments_started == [("assistant", "msg_abc123", None)]
    assert stream.segments_ended == 1


async def test_outputd_transport_caches_loudness_profile_between_chunks(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    profile = AssistantLoudnessProfile(
        provider="openai",
        model="gpt-realtime-2",
        voice="verse",
        source_lufs=-18.0,
        source_peak_dbfs=-2.0,
        confidence=0.75,
        updated_at="2026-06-01T00:00:00Z",
        method="seed_tts",
    )
    calls = 0

    def fake_profile(*args, **kwargs):
        nonlocal calls
        calls += 1
        return profile

    monkeypatch.setattr(tts_mod, "profile_for_outputd", fake_profile)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
        provider="openai",
        model="gpt-realtime-2",
        voice="verse",
        profile_path="/tmp/profiles.json",
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2], dtype=np.int16)
    await p.write_segment(mono.tobytes(), segment_kind="assistant")
    await p.write_segment(mono.tobytes(), segment_kind="assistant")

    assert calls == 1
    assert stream.segments_started == [("assistant", None, profile)]


async def test_outputd_transport_uses_explicit_source_profile(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)

    def fail_profile_lookup(*_args, **_kwargs):
        raise AssertionError("explicit profile should skip voice profile lookup")

    monkeypatch.setattr(tts_mod, "profile_for_outputd", fail_profile_lookup)
    profile = AssistantLoudnessProfile(
        provider="jts",
        model="synthetic-mute-click",
        voice="mute",
        source_lufs=-28.0,
        source_peak_dbfs=-12.0,
        confidence=1.0,
        updated_at="static",
        method="synthetic_generated",
    )
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
        provider="openai",
        model="gpt-realtime-2",
        voice="verse",
        profile_path="/tmp/profiles.json",
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2], dtype=np.int16)
    await p.write_segment(
        mono.tobytes(),
        segment_kind="cue",
        source_profile=profile,
    )

    assert stream.segments_started == [("cue", None, profile)]


async def test_outputd_flush_returns_ack_and_resets_drain_deadline(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    mono = np.array([1, 2], dtype=np.int16)
    await p.write(mono.tobytes())
    assert p.expected_drain_at() != 0.0

    ack = await p.flush()

    assert ack == stream.flush_acks[0]
    assert ack["max_audio_played_ms"] == 125
    assert p.expected_drain_at() == 0.0


async def test_outputd_flush_silences_before_saving_profile(monkeypatch):
    events: list[str] = []

    class _OrderingStream(_CaptureOutputdStream):
        def flush_sync(self) -> dict:
            events.append("flush")
            return super().flush_sync()

    async def fake_save_profile(meter) -> None:
        events.append("save")

    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    p._stream = _OrderingStream()  # type: ignore[assignment]
    monkeypatch.setattr(p, "_save_assistant_source_profile", fake_save_profile)

    await p.flush()

    await asyncio.gather(*p._profile_save_tasks)
    assert events == ["flush", "save"]


async def test_outputd_end_segment_marks_ended_before_saving_profile(monkeypatch):
    """The profile save now runs as a background task (it must not block
    the caller's end-of-turn chirp), so this pins that the stream's
    end_segment still fires first and the save still eventually runs —
    not that end_segment() blocks until it completes.
    """
    events: list[str] = []

    class _OrderingStream(_CaptureOutputdStream):
        def end_segment(self) -> None:
            events.append("end")
            super().end_segment()

    async def fake_save_profile(meter) -> None:
        events.append("save")

    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    stream = _OrderingStream()
    stream.start_segment(kind="assistant", provider_item_id=None, profile=None)
    p._stream = stream  # type: ignore[assignment]
    monkeypatch.setattr(p, "_save_assistant_source_profile", fake_save_profile)

    await p.end_segment()
    for task in list(p._profile_save_tasks):
        await task

    assert events == ["end", "save"]


async def test_outputd_end_segment_does_not_block_on_slow_meter_finish(monkeypatch):
    """Regression pin for the loop-blocking measurement pass: a slow
    ``meter.finish()`` (the real one runs a pure-Python IIR filter twice
    over the reply, ~0.7s of blocking per second of reply) must not delay
    end_segment()'s return, and the profile save must still land once the
    caller awaits the task it kept a reference to.
    """
    saved: list[tuple[str, str, str, LoudnessMeasurement]] = []

    def fake_update_profile(provider, model, voice, measurement, **kwargs):
        saved.append((provider, model, voice, measurement))

    monkeypatch.setattr(
        tts_mod, "update_profile_from_measurement", fake_update_profile,
    )

    measurement = LoudnessMeasurement(
        source_lufs=-18.0,
        source_peak_dbfs=-3.0,
        voiced_duration_sec=2.0,
        total_duration_sec=2.5,
    )

    class _SlowMeter:
        def finish(self) -> LoudnessMeasurement:
            time.sleep(0.3)
            return measurement

    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    p._provider = "acme"
    p._model = "m1"
    p._voice = "v1"
    p._assistant_meter = _SlowMeter()  # type: ignore[assignment]
    p._stream = None

    start = time.monotonic()
    await p.end_segment()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1

    tasks = list(p._profile_save_tasks)
    assert tasks
    for task in tasks:
        await task

    assert saved == [("acme", "m1", "v1", measurement)]


@pytest.mark.parametrize(
    ("on", "wire"),
    [(True, b"PROGRAM_DUCK_ON\n"), (False, b"PROGRAM_DUCK_OFF\n")],
)
def test_outputd_stream_adapter_program_duck_wire_bytes(on, wire):
    """The exact verb fan-in already parses. Depth is fan-in's — the wire
    carries the requested state and nothing else."""
    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    seen: list[bytes] = []

    def serve() -> None:
        seen.append(child.recv(64))
        child.close()

    server = threading.Thread(target=serve)
    server.start()
    try:
        adapter.program_duck(on)
    finally:
        server.join(timeout=1.0)
        adapter.close()

    assert seen == [wire]


def test_outputd_stream_adapter_flush_sync_reads_ack_from_socket():
    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            assert child.recv(64) == b"FLUSH_SYNC\n"
            child.sendall(
                b'{"ok":true,"segments":1,"max_audio_played_ms":42}\n'
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            child.close()

    server = threading.Thread(target=serve)
    server.start()
    try:
        ack = adapter.flush_sync()
    finally:
        adapter.close()
        server.join(timeout=1.0)

    assert not server.is_alive()
    assert not errors
    assert ack == {"ok": True, "segments": 1, "max_audio_played_ms": 42}


def test_outputd_stream_adapter_flush_sync_timeout_is_bounded(monkeypatch):
    parent, child = socket.socketpair()
    child.settimeout(0.5)
    adapter = tts_mod._OutputdStreamAdapter(parent)
    monkeypatch.setattr(tts_mod, "_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC", 0.01)

    start = time.monotonic()
    try:
        assert adapter.flush_sync() is None
        assert time.monotonic() - start < 0.5
        assert child.recv(64) == b"FLUSH_SYNC\n"
        with pytest.raises(OSError):
            adapter.write(b"\0\0\0\0")
    finally:
        adapter.close()
        child.close()


def test_outputd_adapter_lock_timeout_poisons_and_preserves_owner(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_LOCK_TIMEOUT_SEC", 0.01)
    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    adapter._lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="adapter lock timed out"):
            adapter.resume_content_meter()
        assert adapter.closed
        assert adapter._lock.locked(), "timed-out waiter must not release owner lock"
        assert "event=tts_fanin.adapter_timeout" in caplog.text
        assert "phase=lock" in caplog.text
    finally:
        adapter._lock.release()
        adapter.close()
        child.close()


def test_outputd_lock_timeout_shutdown_unblocks_nonreading_sendall(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_LOCK_TIMEOUT_SEC", 0.02)
    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_IO_TIMEOUT_SEC", 0.5)
    parent, child = socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    child.settimeout(0.5)
    adapter = tts_mod._OutputdStreamAdapter(parent)
    writer_errors: list[BaseException] = []

    def write_until_poisoned() -> None:
        try:
            adapter.write(b"x" * (4 * 1024 * 1024))
        except (OSError, RuntimeError) as e:
            writer_errors.append(e)

    writer = threading.Thread(target=write_until_poisoned)
    writer.start()
    received = b""
    while b"\n" not in received:
        received += child.recv(128)
    assert received.startswith(b"AUDIO ")

    with pytest.raises(TimeoutError, match="adapter lock timed out"):
        adapter.pause_content_meter()
    writer.join(timeout=0.5)

    assert not writer.is_alive(), "shutdown must wake the blocked sendall"
    assert adapter.closed
    assert writer_errors
    assert all(not isinstance(e, RuntimeError) for e in writer_errors)
    assert "phase=lock" in caplog.text
    adapter.close()
    child.close()


async def test_outputd_connect_timeout_closes_blocked_socket(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC", 0.01)
    connect_entered = threading.Event()
    closed = threading.Event()

    class _BlockingSocket:
        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            connect_entered.set()
            if not closed.wait(timeout=0.5):
                raise AssertionError("connect was not closed on timeout")
            raise OSError("closed")

        def shutdown(self, _how: int) -> None:
            closed.set()

        def close(self) -> None:
            closed.set()

    fake_socket = _BlockingSocket()
    monkeypatch.setattr(tts_mod.socket, "socket", lambda *_a, **_k: fake_socket)
    p = TtsPlayout(socket_path="/tmp/nonresponsive-outputd.sock")

    with pytest.raises(TimeoutError, match="connect timed out"):
        await p._connect_stream_adapter()

    assert connect_entered.is_set()
    assert closed.is_set()
    assert "event=tts_fanin.connect_timeout" in caplog.text


async def test_meter_control_recovers_on_access_after_stuck_lock(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_LOCK_TIMEOUT_SEC", 0.01)
    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    p = TtsPlayout(socket_path="/tmp/outputd-test.sock")
    p._stream = adapter  # type: ignore[assignment]
    adapter._lock.acquire()
    try:
        await p.pause_content_meter()
    finally:
        adapter._lock.release()
    assert adapter.closed

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)
    await p.pause_content_meter()
    assert p._stream is replacement
    assert replacement.meter_pauses == 1
    child.close()


async def test_closed_outputd_adapter_reconnect_is_single_publisher(
    monkeypatch,
) -> None:
    """Concurrent callers share the replacement published under the lock."""

    parent, child = socket.socketpair()
    closed_stream = tts_mod._OutputdStreamAdapter(parent)
    closed_stream.close()
    child.close()
    p = TtsPlayout(socket_path="/tmp/outputd-test.sock")
    p._stream = closed_stream  # type: ignore[assignment]
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    replacement = _CaptureOutputdStream()
    connect_calls = 0

    async def fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        connect_entered.set()
        await release_connect.wait()
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)
    first = asyncio.create_task(p._current_outputd_stream())
    await wait_signalled(
        connect_entered,
        "first outputd reconnect",
        producer=first,
    )
    second = asyncio.create_task(p._current_outputd_stream())
    await asyncio.sleep(0)
    release_connect.set()

    first_stream, second_stream = await asyncio.gather(first, second)
    assert connect_calls == 1
    assert first_stream is replacement
    assert second_stream is replacement
    assert p._stream is replacement


async def test_measurement_meter_pause_has_250ms_cap_and_no_late_send() -> None:
    """The fail-closed control is synchronous and cannot escape its reply."""

    class _RefusingLock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            return False

        def release(self) -> None:
            raise AssertionError("a waiter must not release unowned lock")

    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    lock = _RefusingLock()
    adapter._lock = lock  # type: ignore[assignment]
    p = TtsPlayout(socket_path="/tmp/outputd-test.sock")
    p._stream = adapter  # type: ignore[assignment]

    with pytest.raises(TimeoutError, match="adapter lock timed out"):
        await p.pause_content_meter_for_measurement(
            time.monotonic() + 10.0,
        )

    assert len(lock.timeouts) == 1
    assert 0.0 < lock.timeouts[0] <= (
        tts_mod._OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC
    )
    assert adapter.closed
    assert child.recv(64) == b""
    await asyncio.sleep(0)
    assert child.recv(64) == b"", "no worker may emit a late PAUSE command"
    child.close()


@pytest.mark.parametrize("accepted_prefix", [False, True])
async def test_cancelled_nonreading_audio_write_is_bounded_and_reconnects(
    monkeypatch, accepted_prefix,
) -> None:

    monkeypatch.setattr(tts_mod, "_OUTPUTD_IPC_IO_TIMEOUT_SEC", 5)
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    parent, child = socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    child.settimeout(0.5)
    adapter = tts_mod._OutputdStreamAdapter(parent)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    p._stream = adapter  # type: ignore[assignment]
    observed = []

    async def first_write():
        observed.append(True)

    writing = asyncio.create_task(
        p.write_segment(b"\x01\x00" * 200_000, segment_kind="cue", on_first_write=first_write)
    )

    def read_to_audio_header() -> bytes:
        with child.makefile("rb") as incoming:
            remaining_commands = int(accepted_prefix)
            while True:
                header = incoming.readline()
                if header.startswith(b"AUDIO "):
                    if not remaining_commands:
                        return header
                    length = int(header.split()[1])
                    assert len(incoming.read(length)) == length
                    remaining_commands -= 1

    received = await asyncio.to_thread(read_to_audio_header)
    assert b"AUDIO " in received
    writing.cancel()
    await asyncio.sleep(0)
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writing, timeout=0.75)

    assert adapter.closed
    assert len(observed) == int(accepted_prefix)

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)
    await p.write_segment(b"\x01\x00" * 2, segment_kind="cue")
    assert p._stream is replacement
    assert replacement.writes
    child.close()


def test_outputd_stream_adapter_sends_loudness_control_protocol():
    parent, child = socket.socketpair()
    adapter = tts_mod._OutputdStreamAdapter(parent)
    profile = AssistantLoudnessProfile(
        provider="openai",
        model="gpt-realtime-2",
        voice="verse",
        source_lufs=-18.25,
        source_peak_dbfs=-2.5,
        confidence=0.8,
        updated_at="2026-06-01T00:00:00Z",
        method="passive_live",
    )
    try:
        adapter.prepare_assistant(
            provider="openai",
            model="gpt-realtime-2",
            voice="verse",
            tts_envelope_lufs=-42.34,
        )
        assert (
            child.recv(128)
            == b"PREPARE_ASSISTANT openai gpt-realtime-2 verse -42.34\n"
        )
        adapter.prepare_assistant(
            provider="openai",
            model="gpt-realtime-2",
            voice="verse",
            tts_envelope_lufs=-42.34,
            volume_context=tts_mod.EffectiveVolumeContext(
                canonical_db=-30.0,
                downstream_db=0.0,
                tts_envelope_lufs=-42.34,
                muted=False,
                stamp_boot_ns=123456,
            ),
        )
        assert child.recv(160) == (
            b"PREPARE_ASSISTANT openai gpt-realtime-2 verse -42.34 "
            b"-30.000 0.000 -42.340 0 123456\n"
        )
        adapter.pause_content_meter()
        assert child.recv(128) == b"CONTENT_METER_PAUSE\n"
        adapter.resume_content_meter()
        assert child.recv(128) == b"CONTENT_METER_RESUME\n"
        adapter.start_segment(
            kind="assistant",
            provider_item_id="msg_abc123",
            profile=profile,
        )
        assert child.recv(256) == (
            b"SEGMENT_START assistant msg_abc123 openai gpt-realtime-2 "
            b"verse -18.25 -2.50 0.80\n"
        )
    finally:
        adapter.close()
        child.close()


async def test_program_duck_reconnects_after_a_closed_socket(monkeypatch):
    """Otherwise the first assistant turn after a fan-in restart plays over
    undimmed music: the duck is the only command that never writes audio, so
    nothing else would heal the connection first."""
    p = TtsPlayout(socket_path="/tmp/outputd-test.sock", drain_tail_sec=0.0)
    parent, child = socket.socketpair()
    closed_stream = tts_mod._OutputdStreamAdapter(parent)
    closed_stream.close()
    child.close()
    p._stream = closed_stream  # type: ignore[assignment]

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    assert await p.program_duck(True) is True
    assert p._stream is replacement
    assert replacement.ducks == [True]


async def test_program_duck_reports_a_failure_when_there_is_no_connection():
    p = TtsPlayout(socket_path="/tmp/outputd-test.sock", drain_tail_sec=0.0)

    assert await p.program_duck(True) is False


async def test_outputd_transport_reconnects_after_closed_socket(monkeypatch):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    closed_stream = tts_mod._OutputdStreamAdapter(parent)
    closed_stream.close()
    child.close()
    p._stream = closed_stream  # type: ignore[assignment]

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    mono = np.array([1, 2], dtype=np.int16)
    await p.write_segment(
        mono.tobytes(),
        provider_item_id="msg_abc123",
        segment_kind="assistant",
    )

    assert p._stream is replacement
    assert replacement.gains == [-8.0]
    assert replacement.segments_started == [("assistant", "msg_abc123", None)]
    assert replacement.writes


async def test_outputd_transport_reconnects_and_retries_after_broken_pipe(
    monkeypatch,
):
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    broken_stream = tts_mod._OutputdStreamAdapter(parent)
    child.close()
    p._stream = broken_stream  # type: ignore[assignment]

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    mono = np.array([1, 2], dtype=np.int16)
    await p.write_segment(
        mono.tobytes(),
        provider_item_id="msg_abc123",
        segment_kind="assistant",
    )

    assert broken_stream.closed
    assert p._stream is replacement
    assert replacement.gains == [-8.0]
    assert replacement.segments_started == [("assistant", "msg_abc123", None)]
    assert replacement.writes


async def test_outputd_prepare_reconnects_and_retries_after_broken_pipe(
    monkeypatch,
):
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    broken_stream = tts_mod._OutputdStreamAdapter(parent)
    child.close()
    p._stream = broken_stream  # type: ignore[assignment]

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    await p.prepare_assistant_context(
        provider="openai",
        model="gpt-realtime-2",
        voice="marin",
        tts_envelope_lufs=-41.0,
    )

    assert broken_stream.closed
    assert p._stream is replacement
    assert replacement.prepares == [
        ("openai", "gpt-realtime-2", "marin", -41.0)
    ]


async def test_outputd_prepare_preserves_snapshot_stamp() -> None:
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    stream = _CaptureOutputdStream()
    p._stream = stream  # type: ignore[assignment]

    await p.prepare_assistant_context(
        provider="openai",
        model="gpt-realtime-2",
        voice="marin",
        tts_envelope_lufs=-41.0,
        canonical_volume_db=-30.0,
        downstream_volume_db=0.0,
        context_tts_envelope_lufs=-41.0,
        muted=False,
        context_stamp_boot_ns=123456,
    )

    assert stream.volume_contexts == [
        tts_mod.EffectiveVolumeContext(
            canonical_db=-30.0,
            downstream_db=0.0,
            tts_envelope_lufs=-41.0,
            muted=False,
            stamp_boot_ns=123456,
        )
    ]


async def test_outputd_prepare_reconnect_failure_is_best_effort(
    monkeypatch,
):
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    broken_stream = tts_mod._OutputdStreamAdapter(parent)
    child.close()
    p._stream = broken_stream  # type: ignore[assignment]

    async def fake_connect():
        raise OSError("outputd still unavailable")

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    await p.prepare_assistant_context(
        provider="openai",
        model="gpt-realtime-2",
        voice="marin",
        tts_envelope_lufs=-41.0,
    )

    assert broken_stream.closed
    assert p._stream is broken_stream


async def test_outputd_meter_control_reconnects_and_retries_after_broken_pipe(
    monkeypatch,
):
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    broken_stream = tts_mod._OutputdStreamAdapter(parent)
    child.close()
    p._stream = broken_stream  # type: ignore[assignment]

    replacement = _CaptureOutputdStream()

    async def fake_connect():
        return replacement

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    await p.pause_content_meter()

    assert broken_stream.closed
    assert p._stream is replacement
    assert replacement.meter_pauses == 1


async def test_outputd_meter_control_reconnect_failure_is_best_effort(
    monkeypatch,
):
    p = TtsPlayout(
        socket_path="/tmp/outputd-test.sock",
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    parent, child = socket.socketpair()
    broken_stream = tts_mod._OutputdStreamAdapter(parent)
    child.close()
    p._stream = broken_stream  # type: ignore[assignment]

    async def fake_connect():
        raise OSError("outputd still unavailable")

    monkeypatch.setattr(p, "_connect_stream_adapter", fake_connect)

    await p.pause_content_meter()

    assert broken_stream.closed
    assert p._stream is broken_stream
