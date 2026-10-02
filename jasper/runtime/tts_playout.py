# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assistant PCM conversion, pacing and drain accounting."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import numpy as np

from jasper.runtime_config.assistant_loudness import (
    AssistantSourceMeter,
    DEFAULT_PROFILE_PATH as ASSISTANT_LOUDNESS_PROFILE_PATH,
    INPUT_RATE as ASSISTANT_INPUT_RATE,
    UPSAMPLE_2X_CONTEXT,
    confidence_for_measurement,
    profile_for_outputd,
    update_profile_from_measurement,
    upsample_2x,
)
from jasper.audio_control.assistant_volume import EffectiveVolumeContext
from jasper.platform.log_event import log_event
from jasper.fanin import tts_client
from jasper.service_state.tts_routing import FANIN_TTS_SOCKET

logger = logging.getLogger("jasper.tts_playout")

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_OUTPUTD_AUDIO_FRAME_BYTES = 8  # stereo S32_LE
_OUTPUTD_SAMPLE_RATE = 48_000

# Matches the i16-to-i32 shift in jasper_resampler::widen_i16_to_i32.
_SPINE_SCALE = 65_536
_I32_MIN = -(2 ** 31)
_I32_MAX = 2 ** 31 - 1
# MEASURE_PAUSE is a rare safety-control request, not an audio hot path. Its
# canonical adapter call runs synchronously so it cannot outlive the reply;
# 250 ms covers two IPC audio chunks and leaves ample room inside the daemon's
# aggregate pause budget without stalling the event loop for a full IPC second.
_OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC = 0.25
# 125 ms of stereo S32_LE; below the daemon's 2 MiB allocation cap.
_OUTPUTD_MAX_AUDIO_CHUNK_BYTES = (
    _OUTPUTD_SAMPLE_RATE * _OUTPUTD_AUDIO_FRAME_BYTES // 8
)
# Pace sustained writes so the IPC owner's pending-audio queue never
# overflows. The owner drops whole audio commands that
# arrive while its queue is full — it cannot block the socket reader,
# because a blocked reader would also stall FLUSH (barge-in) behind queued
# audio. OpenAI Realtime delivers replies faster than realtime (~11 s of
# audio in ~4 s), so an unpaced writer overflows the budget and the
# surviving chunks play as garbled "fast-forward" audio
# (event=fanin.tts_command_dropped).
# The 1.2 s watermark leaves room for a 125 ms chunk and a concurrent
# listening chirp (~0.3 s) within the owner's 2 s queue budget.
_OUTPUTD_PACE_AHEAD_SEC = 1.2

# Pacing sleeps go through this alias so tests can substitute a spy
# without patching the global asyncio module.
_pace_sleep = asyncio.sleep


def _outputd_audio_chunks(data: bytes):
    """Split S32_LE stereo payloads below the daemon's protocol cap."""
    if not data:
        return []
    if len(data) % _OUTPUTD_AUDIO_FRAME_BYTES != 0:
        raise ValueError("TTS IPC audio payload must contain whole stereo frames")
    chunk_size = _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
    chunk_size -= chunk_size % _OUTPUTD_AUDIO_FRAME_BYTES
    if chunk_size <= 0:
        raise AssertionError("TTS IPC chunk size must hold at least one frame")
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def _quantize_to_wire(arr):
    """Round i16 sample units onto the i32 spine, saturating without dither.

    Float64 represents both i32 rails exactly during rounding and clipping.
    Signal precision remains bounded by the resampler's float32 mantissa.
    """
    scaled = np.rint(arr.astype(np.float64) * _SPINE_SCALE)
    return np.clip(scaled, _I32_MIN, _I32_MAX).astype(np.int32)


class TtsPlayout:
    """Assistant-audio playout: gain validation, drain-deadline timing, and
    the fan-in TTS IPC client.

    Provider PCM enters as 24 kHz mono; write() polyphase-upsamples it 2x to
    the fan-in socket's fixed 48 kHz, duplicates mono to stereo, updates the
    drain deadline, and writes bytes to this class's socket adapter. Gain
    travels as metadata so the TTS IPC owner applies the final clamp at its
    mix boundary.
    """

    # The two ends of this class's resample, published so callers that count
    # frames on either side read the rate from the code that converts them.
    # The fan-in wire is fixed at the rate this module writes.
    INPUT_RATE = ASSISTANT_INPUT_RATE
    OUTPUT_RATE = _OUTPUTD_SAMPLE_RATE

    # Floor — below this, TTS is effectively silent. Used when the
    # user mutes, when Camilla is unreachable at startup, or when a
    # volume reading looks malformed.
    MIN_TTS_GAIN_DB = -60.0

    def __init__(
        self,
        socket_path: str = FANIN_TTS_SOCKET,
        gain_db: float = 0.0,
        *,
        drain_tail_sec: float = 0.085,  # production wires from cfg.tts_drain_tail_sec
        provider: str = "",
        model: str = "",
        voice: str = "",
        profile_path: str = ASSISTANT_LOUDNESS_PROFILE_PATH,
    ) -> None:
        # Initial value is the floor (effectively silent) so the daemon
        # cannot accidentally play TTS loud during the brief window
        # between construction and the first configured gain. Until
        # then we'd rather have inaudible TTS than blast.
        self._gain_db = self.MIN_TTS_GAIN_DB
        # Cumulative pacing-sleep time since the last take_paced_sec().
        self._paced_total_sec = 0.0
        self._stream: tts_client.TtsStream | None = None
        # One-shot latch so a write() before __aenter__ (no stream yet) is
        # audible in the journal instead of a silent no-op.
        self._closed_stream_warned = False
        # Drain tracking — see `expected_drain_at`. None (not 0.0)
        # because CLOCK_MONOTONIC's reference is platform-defined; 0.0
        # is briefly a legitimate now() value on a freshly-booted Pi.
        self._drain_tail_sec = float(drain_tail_sec)
        self._ring_end_monotonic: float | None = None
        # Input samples carried between chunks of one segment — see
        # `_upsample_chunk`. None means "no segment in progress".
        self._upsample_tail = None
        # Emission-time admission authority — see set_emission_admission.
        self._emission_admission: "Callable[[], str | None] | None" = None
        self._emission_refusal_logged = False
        self.set_gain_db(gain_db)
        self._socket_path = socket_path
        self._provider = provider
        self._model = model
        self._voice = voice
        self._profile_path = profile_path
        self._assistant_meter: AssistantSourceMeter | None = None
        self._profile_cache_key: tuple[str, str, str, str] | None = None
        self._profile_cache = None
        # Keeps references so scheduled profile-save tasks (see
        # _schedule_assistant_source_profile_save) aren't garbage-collected
        # mid-flight.
        self._profile_save_tasks: set[asyncio.Task] = set()
        # One publisher owns reconnect. Without this lock, simultaneous meter
        # and audio callers can each connect after the same poisoned adapter
        # and leave one live but unreachable socket behind.
        self._reconnect_lock = asyncio.Lock()

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def set_emission_admission(
        self,
        admission: "Callable[[], str | None] | None",
    ) -> None:
        """Install the authority asked before every write.

        `admission` returns a refusal code while assistant audio must not
        be heard at all (an armed room-correction window), else None. It is
        asked per write, not per episode, so a caller that passed an earlier
        check and is already mid-playout is refused too (issue #1913)."""
        self._emission_admission = admission

    async def write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
        on_first_write: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Return whether PCM reached the transport. Observe the first accepted
        chunk even if a later chunk fails or the write is cancelled.
        """
        admission = self._emission_admission
        refusal = admission() if admission is not None else None
        if refusal is not None:
            # Once per refusal streak: a burst-delivery provider hands over a
            # whole response as many chunks, and one line each would flood the
            # journal for the length of a held measurement session.
            if not self._emission_refusal_logged:
                self._emission_refusal_logged = True
                log_event(
                    logger,
                    "tts_write.refused",
                    reason=refusal,
                    segment_kind=segment_kind,
                )
            return False
        self._emission_refusal_logged = False
        return await self._write_segment(
            pcm,
            provider_item_id=provider_item_id,
            segment_kind=segment_kind,
            source_profile=source_profile,
            pcm_wide=pcm_wide,
            on_first_write=on_first_write,
        )

    def expected_drain_at(self) -> float:
        """Monotonic deadline at which the last-queued sample's tail
        will have cleared the OS audio stack — i.e. the speaker is
        silent. Returns ``0.0`` when nothing is queued (the sentinel
        naturally compares as "already drained" against
        ``time.monotonic()``)."""
        if self._ring_end_monotonic is None:
            return 0.0
        return self._ring_end_monotonic + self._drain_tail_sec

    async def wait_drained(self) -> None:
        """Block until ``expected_drain_at`` has passed. Cheap when
        nothing is queued (the 0.0 sentinel yields negative remaining,
        which skips the sleep). Single ``asyncio.sleep`` otherwise —
        deadline is known up-front, no polling."""
        remaining = self.expected_drain_at() - time.monotonic()
        if remaining > 0.0:
            await asyncio.sleep(remaining)

    def take_paced_sec(self) -> float:
        """Pacing-sleep seconds accumulated since the last call; resets.

        The voice daemon reads this once per turn for the turn-ended
        accounting line.
        """
        v = self._paced_total_sec
        self._paced_total_sec = 0.0
        return v

    async def __aenter__(self) -> "TtsPlayout":
        self._stream = await self._connect_stream()
        return self

    async def _connect_stream(self) -> tts_client.TtsStream:
        stream = await tts_client.connect(self._socket_path)
        try:
            stream.set_gain_db(self.gain_db)
        except OSError:
            stream.close()
            raise
        logger.info("fan-in TTS IPC connected: socket=%s", self._socket_path)
        return stream

    async def _current_stream(self) -> tts_client.TtsStream | None:
        stream = self._stream
        if stream is not None and stream.closed:
            async with self._reconnect_lock:
                # Another waiter may have published the replacement while we
                # queued for the reconnect lock. Re-read inside ownership so
                # every caller shares that adapter and no loser socket exists.
                stream = self._stream
                if stream is None or not stream.closed:
                    return stream
                log_event(
                    logger,
                    "tts_fanin.reconnect",
                    reason="closed_socket",
                    socket=self._socket_path,
                    poison_reason=stream.poison_reason,
                )
                try:
                    stream = await self._connect_stream()
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        "tts_fanin.reconnect_failed",
                        reason="closed_socket",
                        socket=self._socket_path,
                        exc_type=type(e).__name__,
                        err=str(e),
                        level=logging.WARNING,
                    )
                    return None
                self._stream = stream
        return stream

    def set_gain_db(self, db: float) -> None:
        """Update TTS gain and push the wire-level value into the stream.

        Non-finite inputs are rejected and very low finite values floor
        to the mute-equivalent minimum. Single-float assignment is atomic
        under the GIL, so no lock is needed for concurrent reads of
        `gain_db`. The stream push below runs regardless of whether the
        clamp step above actually changed anything, so a rejected input
        still re-syncs the stream to the (unchanged) active gain.
        """
        try:
            parsed = float(db)
        except (TypeError, ValueError):
            logger.warning("tts gain rejected (not a number): %r", db)
        else:
            if parsed != parsed or parsed in (float("inf"), float("-inf")):
                logger.warning("tts gain rejected (non-finite): %r", db)
            else:
                clamped = max(self.MIN_TTS_GAIN_DB, parsed)
                if clamped != self._gain_db:
                    self._gain_db = clamped
                    # DEBUG (not INFO): the active TTS IPC owner publishes
                    # the richer assistant loudness decision telemetry, and
                    # this low-level floor log is noisy.
                    if clamped != parsed:
                        logger.debug(
                            "tts gain set: requested %.1f dB -> floored to "
                            "%.1f dB",
                            parsed, clamped,
                        )
                    else:
                        logger.debug("tts gain set: %.1f dB", clamped)
        stream = self._stream
        if stream is None or stream.closed:
            return
        try:
            stream.set_gain_db(self.gain_db)
        except OSError as e:
            logger.warning("fan-in TTS IPC gain update failed: %s", e)

    async def program_duck(self, on: bool) -> bool:
        """Switch fan-in's program duck on/off over this playout's connection.

        Fan-in owns the duck depth; this only asks for the state. Goes through
        the same reconnect path as every other command, so the first turn
        after a fan-in restart ducks rather than playing over undimmed music.
        Returns False when there is no live connection to ask on or the ask
        failed, so the caller can own its own restore.
        """
        # A silent duck failure means music does not step back under the
        # assistant, so every path out of here says why.
        def failed(reason: str, **fields: str) -> bool:
            log_event(
                logger,
                "voice.duck_failed",
                on=str(bool(on)).lower(),
                reason=reason,
                level=logging.WARNING,
                **fields,
            )
            return False

        stream = await self._current_stream()
        if stream is None:
            return failed("no_connection")
        try:
            await asyncio.to_thread(stream.program_duck, on)
        except OSError as e:
            return failed("send", detail=str(e))
        return True

    async def prepare_assistant_context(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        context_tts_envelope_lufs: float | None = None,
        muted: bool | None = None,
        context_stamp_boot_ns: int | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._voice = voice
        # New turn: force a fresh profile read even if identity is unchanged
        # from the last turn, since a mid-turn save under that same identity
        # (see _save_assistant_source_profile) may have rewritten it since.
        self._profile_cache_key = None
        self._profile_cache = None
        prepare_kwargs = {
            "provider": provider,
            "model": model,
            "voice": voice,
            "tts_envelope_lufs": tts_envelope_lufs,
        }
        if (
            canonical_volume_db is not None
            and downstream_volume_db is not None
            and context_tts_envelope_lufs is not None
            and muted is not None
            and context_stamp_boot_ns is not None
        ):
            prepare_kwargs["volume_context"] = EffectiveVolumeContext(
                canonical_db=canonical_volume_db,
                downstream_db=downstream_volume_db,
                tts_envelope_lufs=context_tts_envelope_lufs,
                muted=muted,
                stamp_boot_ns=context_stamp_boot_ns,
            )
        await self._send_control("prepare_assistant", **prepare_kwargs)

    async def pause_content_meter(self) -> None:
        await self._send_control("pause_content_meter")

    async def refresh_connection(self) -> None:
        """Replace a stream whose fan-in end is gone, through the ordinary
        reconnect path. Fan-in restarts on every layout save, and the socket
        only learns of it on its next send; the measurement pause calls this
        when a window opens, before :meth:`pause_content_meter_for_measurement`,
        which never reconnects itself.

        It retries a stream an earlier call dropped as ``peer_closed`` (fan-in
        was still restarting then). A stream poisoned for any other reason is
        left as it is, so that pause still fails closed on it."""
        stream = self._stream
        if stream is not None and (
            stream.drop_if_peer_closed()
            or (stream.closed and stream.poison_reason == "peer_closed")
        ):
            await self._current_stream()

    async def pause_content_meter_for_measurement(
        self,
        deadline_monotonic: float,
    ) -> None:
        """Fail-closed meter pause that cannot outlive MEASURE_PAUSE.

        Do not reconnect here: a missing or closed adapter (`stream is None
        or stream.closed`) fails the window closed instead; ordinary access,
        :meth:`refresh_connection` first of all, owns reconnection.
        """

        stream = self._stream
        if stream is None or stream.closed:
            raise OSError("canonical TTS IPC adapter unavailable")
        control_deadline = min(
            deadline_monotonic,
            time.monotonic() + _OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC,
        )
        # Deliberately synchronous: the bounded adapter critical section may
        # hold the event loop for at most 250 ms, and no worker can emit PAUSE
        # after this coroutine reports failure and voice reopens admission.
        stream.pause_content_meter(deadline_monotonic=control_deadline)

    async def resume_content_meter(self) -> None:
        await self._send_control("resume_content_meter")

    async def _send_control(self, method: str, **kwargs) -> None:
        for attempt in range(2):
            stream = await self._current_stream()
            if stream is None:
                return
            try:
                await asyncio.to_thread(getattr(stream, method), **kwargs)
                return
            except OSError as e:
                if (
                    attempt == 0
                    and stream.closed
                    and not isinstance(e, TimeoutError)
                ):
                    log_event(
                        logger,
                        "tts_fanin.control_retry",
                        method=method,
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    continue
                logger.warning(
                    "fan-in TTS IPC %s failed: %s", method, e
                )
                return

    async def write(self, pcm: bytes) -> None:
        await self.write_segment(pcm)

    async def _write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
        on_first_write: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Send un-gained 48 kHz stereo PCM to the TTS IPC owner.

        Gain is sent as metadata and enforced by fan-in's final mix
        clamp. Drain accounting mirrors TtsPlayout.write so the voice
        daemon's turn-ending contract stays identical.

        ``pcm`` is 24 kHz mono: provider input is S16, while generated earcons
        use S32_LE with ``pcm_wide=True``. Wide input is divided by 2^16 into
        i16 sample units before the shared resampler and quantizer.
        """
        if not pcm:
            return False
        if self._stream is None:
            if not self._closed_stream_warned:
                logger.warning(
                    "TtsPlayout.write called on a closed stream - "
                    "%d bytes silently dropped. Did you forget "
                    "`async with tts:`? (Suppressing further such "
                    "warnings for this instance.)",
                    len(pcm),
                )
                self._closed_stream_warned = True
            return False
        stream = await self._current_stream()
        if stream is None:
            return False

        if pcm_wide:
            # /2^16 is exact in binary floating point (it changes the exponent
            # only), so this costs nothing beyond the float32 mantissa the
            # whole path already runs at.
            arr = np.frombuffer(pcm, dtype=np.int32).astype(np.float32)
            arr = arr / np.float32(_SPINE_SCALE)
        else:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if (
            segment_kind == "assistant"
            and self._provider
            and self._model
            and self._voice
        ):
            if self._assistant_meter is None:
                self._assistant_meter = AssistantSourceMeter()
            self._assistant_meter.observe_pcm_24k(pcm)
        # The wire is fixed at 48 kHz; provider/cue PCM is always 24 kHz, so
        # this upsample ratio is always exactly 2.
        arr = self._upsample_chunk(arr).astype(np.float32, copy=False)
        mono = _quantize_to_wire(arr)
        stereo = np.repeat(mono, 2)

        chunk_duration_sec = len(mono) / _OUTPUTD_SAMPLE_RATE
        write_start = time.monotonic()
        for attempt in range(2):
            try:
                await tts_client.run_io(stream, "set_gain_db", self.gain_db)
                profile = self._profile_for_segment(
                    segment_kind, source_profile=source_profile,
                )
                await tts_client.run_io(
                    stream, "start_segment",
                    kind=segment_kind,
                    provider_item_id=provider_item_id,
                    profile=profile,
                )
                break
            except OSError as e:
                if (
                    attempt == 0
                    and stream.closed
                    and not isinstance(e, TimeoutError)
                ):
                    log_event(
                        logger,
                        "tts_fanin.segment_setup_retry",
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    stream = await self._current_stream()
                    if stream is None:
                        return False
                    continue
                raise
        paced_sec = 0.0
        oversleep_sec = 0.0
        accepted = False

        async def commit_chunk() -> None:
            nonlocal accepted
            sent_at = time.monotonic()
            committed_end = max(self._ring_end_monotonic or sent_at, sent_at)
            self._ring_end_monotonic = committed_end + len(chunk) / (
                _OUTPUTD_SAMPLE_RATE * _OUTPUTD_AUDIO_FRAME_BYTES
            )
            if not accepted:
                accepted = True
                if on_first_write is not None:
                    try:
                        await on_first_write()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("TTS acceptance observer failed: %s", e)

        for chunk in _outputd_audio_chunks(stereo.tobytes()):
            now = time.monotonic()
            pace_excess = (self._ring_end_monotonic or now) - now - _OUTPUTD_PACE_AHEAD_SEC
            if pace_excess > 0:
                slept_at = time.monotonic()
                await _pace_sleep(pace_excess)
                # What the loop actually gave back beyond what was asked for:
                # the observable form of event-loop lag on this path (#5091).
                oversleep_sec += max(
                    0.0, time.monotonic() - slept_at - pace_excess,
                )
                paced_sec += pace_excess
                self._paced_total_sec += pace_excess
            try:
                await tts_client.run_io(stream, "write", chunk, on_accepted=commit_chunk)
            except OSError:
                if stream.closed:
                    log_event(
                        logger,
                        "tts_fanin.audio_write_failed",
                        reason="closed_socket",
                        level=logging.WARNING,
                    )
                raise
        queued_at = time.monotonic()
        # Exclude deliberate pacing sleeps so the warning keeps meaning
        # "the IPC itself is slow", not "the writer paced as designed".
        write_ms = (queued_at - write_start) * 1000 - paced_sec * 1000
        chunk_ms = chunk_duration_sec * 1000
        if write_ms > chunk_ms + 100:
            # This timer is LOCAL — setup, scheduling, locks, socket writes and
            # callbacks all land in it. It says nothing about when the provider
            # sent the audio; pair it with `provider.output_deficit` to tell a
            # network gap from a local delivery stall (#5091).
            log_event(
                logger,
                "tts_fanin.write_slow",
                write_ms=int(write_ms),
                chunk_ms=int(chunk_ms),
                paced_ms=int(paced_sec * 1000),
                oversleep_ms=int(oversleep_sec * 1000),
                frames=len(mono),
                rate=_OUTPUTD_SAMPLE_RATE,
                level=logging.WARNING,
            )
        return accepted

    def _profile_for_segment(self, segment_kind: str, *, source_profile=None):
        if source_profile is not None:
            return source_profile
        if (
            segment_kind == "chirp"
            or not (self._provider and self._model and self._voice)
        ):
            return None
        key = (self._provider, self._model, self._voice, self._profile_path)
        if self._profile_cache_key != key:
            self._profile_cache_key = key
            self._profile_cache = profile_for_outputd(
                self._provider,
                self._model,
                self._voice,
                path=self._profile_path,
            )
        return self._profile_cache

    def _upsample_chunk(self, arr):
        """2x the next chunk of this segment, seamlessly across the join.

        `upsample_2x` reads UPSAMPLE_2X_CONTEXT input samples either side of
        every output sample, so a chunk resampled alone is wrong at BOTH of
        its edges — measured at -9 dB peak, once per provider delta, ~10 times
        a second. Carrying the previous chunk's tail supplies the left context
        and holds the right back until the next chunk arrives, which delays
        the wire by UPSAMPLE_2X_CONTEXT input samples (0.42 ms) and leaves
        that much of a segment's last chunk unplayed. Sample count is
        unchanged: every chunk still yields exactly twice its own samples.
        """
        tail = self._upsample_tail
        if tail is None:
            tail = np.zeros(2 * UPSAMPLE_2X_CONTEXT, dtype=arr.dtype)
        buf = np.concatenate((tail, arr))
        self._upsample_tail = buf[-2 * UPSAMPLE_2X_CONTEXT:].copy()
        skip = 2 * UPSAMPLE_2X_CONTEXT
        return upsample_2x(buf)[skip:skip + 2 * arr.size]

    async def end_segment(self) -> None:
        self._upsample_tail = None
        stream = self._stream
        if stream is not None and not stream.closed:
            try:
                await tts_client.run_io(stream, "end_segment")
            except OSError as e:
                logger.warning("fan-in TTS IPC segment end failed: %s", e)
        self._schedule_assistant_source_profile_save()

    def _pop_assistant_meter(self) -> AssistantSourceMeter | None:
        meter = self._assistant_meter
        self._assistant_meter = None
        return meter

    def _schedule_assistant_source_profile_save(self) -> None:
        """Run the profile save off the chirp's critical path.

        ``meter.finish()`` runs a pure-Python per-sample IIR filter twice
        over the reply audio (assistant_loudness.py's ``_biquad``); awaited
        inline here it blocked the loop for ~0.7s per second of reply,
        delaying the end-of-turn chirp. The meter is popped now, by value,
        so a segment that starts before this task gets to run cannot steal
        it from the segment that just ended.
        """
        meter = self._pop_assistant_meter()
        task = asyncio.create_task(self._save_assistant_source_profile(meter))
        self._profile_save_tasks.add(task)
        task.add_done_callback(self._profile_save_tasks.discard)

    async def _save_assistant_source_profile(
        self, meter: AssistantSourceMeter | None
    ) -> None:
        if meter is None or not (self._provider and self._model and self._voice):
            return
        measurement = await asyncio.to_thread(meter.finish)
        if measurement is None:
            return
        confidence = confidence_for_measurement(measurement)
        try:
            await asyncio.to_thread(
                update_profile_from_measurement,
                self._provider,
                self._model,
                self._voice,
                measurement,
                path=self._profile_path,
                method="passive_live",
                confidence=confidence,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant loudness profile save failed: %s", e)
        # Deliberately does not touch _profile_cache{,_key}: this measurement
        # is from a segment already spoken in the running turn, and the gain
        # for the rest of the turn must stay pinned to what
        # `prepare_assistant_context` loaded at turn start (else the reply
        # audibly slides mid-turn as each segment's save rewrites the source
        # this call rereads).

    async def flush(self) -> dict | None:
        self._upsample_tail = None
        stream = await self._current_stream()
        if stream is None:
            await self._save_assistant_source_profile(self._pop_assistant_meter())
            return None
        ack: dict | None = None
        try:
            ack = await tts_client.run_io(stream, "flush_sync")
        except Exception as e:  # noqa: BLE001
            logger.warning("fan-in TTS IPC flush failed: %s", e)
        if tts_client.confirmed_tts_flush(ack):
            self._ring_end_monotonic = None
            log_event(
                logger,
                "tts_flush.ack",
                transport="fanin",
                ok=ack.get("ok"),
                segments=ack.get("segments"),
                flushed_frames=ack.get("flushed_frames"),
                max_audio_played_ms=ack.get("max_audio_played_ms"),
            )
        else:
            ack = None
        self._schedule_assistant_source_profile_save()
        return ack

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            await asyncio.to_thread(stream.close)
