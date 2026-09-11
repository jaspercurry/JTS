# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The TTS-playout stand-ins, one per real surface: `FakeOutputdStream` for
`jasper.tts_playout._OutputdStreamAdapter` (the blocking socket writer) and
`FakeTts` for `jasper.tts_playout.TtsPlayout`.

Both record every call; per-test behaviour comes from the constructor hooks
(`on_write`, `on_drain`, `write_error`, …) rather than a subclass. Their
shapes are pinned against the real classes by
`test_the_shared_playout_fakes_track_the_real_surface` in
tests/test_tts_playout.py — a fake that has drifted from the object it stands
in for passes while the defect it should catch is present.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

_DEFAULT_FLUSH_ACK = {
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


class FakeOutputdStream:
    """Capturing stand-in for the blocking socket adapter.

    `on_write` runs inside `write` before the bytes are recorded, so a hook
    that raises leaves the failed chunk out of `writes` exactly as a failed
    socket send does.
    """

    def __init__(self, *, on_write: Callable[[bytes], None] | None = None) -> None:
        self._on_write = on_write
        self._active_segment: tuple[str, str | None, object | None] | None = None
        self.closed = False
        self.poison_reason: str | None = None
        self.gains: list[float] = []
        self.ducks: list[bool] = []
        self.prepares: list[tuple[str, str, str, float]] = []
        self.volume_contexts: list[object | None] = []
        self.meter_pauses = 0
        self.meter_resumes = 0
        self.segments_started: list[tuple[str, str | None, object | None]] = []
        self.segments_ended = 0
        self.write_attempts = 0
        self.writes: list[bytes] = []
        self.flush_acks: list[dict] = []

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

    def pause_content_meter(self, *, deadline_monotonic: float | None = None) -> None:
        del deadline_monotonic
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
        self.write_attempts += 1
        if self._on_write is not None:
            self._on_write(data)
        self.writes.append(data)

    def flush_sync(self) -> dict:
        self.flush_acks.append(_DEFAULT_FLUSH_ACK)
        return _DEFAULT_FLUSH_ACK

    def _poison(
        self,
        *,
        reason: str | None = None,
        timeout_sec: float | None = None,
        poison_reason: str | None = None,
    ) -> None:
        del timeout_sec
        self.closed = True
        self.poison_reason = poison_reason if poison_reason is not None else reason

    def close(self) -> None:
        self.closed = True


class FakeTts:
    """Capturing stand-in for `TtsPlayout`: everything succeeds and drains
    instantly unless a constructor hook says otherwise.

    `calls` is the method-name log in call order; pass `on_call` to append
    into a list a test also feeds from its ducker or cue manager, which is
    how the ordering between those collaborators gets asserted.
    """

    def __init__(
        self,
        *,
        accept: bool = True,
        write_error: BaseException | None = None,
        end_segment_error: BaseException | None = None,
        connect_error: BaseException | None = None,
        flush_error: BaseException | None = None,
        flush_ack: dict | None = None,
        on_drain: Callable[[], Awaitable[None]] | None = None,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self._accept = accept
        self._write_error = write_error
        self._end_segment_error = end_segment_error
        self._connect_error = connect_error
        self._flush_error = flush_error
        self._flush_ack = flush_ack
        self._on_drain = on_drain
        self._on_call = on_call
        self.calls: list[str] = []
        self.prepares: list[dict] = []
        self.segments: list[dict] = []
        self.writes: list[bytes] = []
        self.meter_pauses = 0
        self.meter_resumes = 0
        self.drain_calls = 0
        self.end_segment_calls = 0
        self.flush_calls = 0

    def _note(self, name: str) -> None:
        self.calls.append(name)
        if self._on_call is not None:
            self._on_call(name)

    def set_emission_admission(self, admission) -> None:
        # Deliberately unrecorded: wiring done once at construction, which
        # would otherwise head every `calls` sequence a test asserts on.
        del admission

    async def write(self, pcm: bytes) -> None:
        await self.write_segment(pcm)

    async def write_segment(
        self,
        pcm: bytes,
        *,
        on_first_write: Callable[[], Awaitable[None]] | None = None,
        # Recorded AS PASSED, so a caller that stops sending a keyword fails
        # its test rather than matching this fake's default for it.
        **kwargs,
    ) -> bool:
        self._note("write_segment")
        if self._write_error is not None:
            raise self._write_error
        if not self._accept:
            return False
        self.segments.append({"pcm": pcm, **kwargs})
        self.writes.append(pcm)
        if on_first_write is not None:
            await on_first_write()
        # The real write always crosses `asyncio.to_thread`, so it always
        # yields; a fake that never did would let a test see an impossible
        # uninterruptible burst of chunks.
        await asyncio.sleep(0)
        return True

    async def prepare_assistant_context(self, **kwargs) -> None:
        self._note("prepare_assistant_context")
        self.prepares.append(kwargs)

    async def pause_content_meter(self) -> None:
        self._note("pause_content_meter")
        self.meter_pauses += 1

    async def pause_content_meter_for_measurement(
        self, deadline_monotonic: float,
    ) -> None:
        del deadline_monotonic
        self._note("pause_content_meter_for_measurement")

    async def resume_content_meter(self) -> None:
        self._note("resume_content_meter")
        self.meter_resumes += 1

    async def end_segment(self) -> None:
        self._note("end_segment")
        self.end_segment_calls += 1
        if self._end_segment_error is not None:
            raise self._end_segment_error

    async def wait_drained(self) -> None:
        self._note("wait_drained")
        self.drain_calls += 1
        if self._on_drain is not None:
            await self._on_drain()

    async def flush(self) -> dict | None:
        self._note("flush")
        self.flush_calls += 1
        if self._flush_error is not None:
            raise self._flush_error
        return self._flush_ack

    def expected_drain_at(self) -> float:
        return 0.0

    def take_paced_sec(self) -> float:
        return 0.0

    async def __aenter__(self) -> "FakeTts":
        if self._connect_error is not None:
            raise self._connect_error
        return self

    async def __aexit__(self, *exc) -> None:
        return None
