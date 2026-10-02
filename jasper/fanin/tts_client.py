# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bounded fan-in TTS socket I/O and its wire contract."""

from __future__ import annotations

import asyncio
import json
import logging
import select
import socket
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from jasper.platform import wire
from jasper.platform.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_control.assistant_volume import EffectiveVolumeContext

logger = logging.getLogger("jasper.tts_playout")

FLUSH_ACK_TIMEOUT_SEC = 3.0
# All IPC is local to the Pi. Healthy connects and control writes complete in
# milliseconds, while one second tolerates scheduler pressure without letting
# a dead owner or full Unix-socket buffer strand voice teardown indefinitely.
# Lock waits use the same ceiling: their owner is itself bounded by the socket
# timeout, and a timed-out waiter poisons the socket to wake that owner.
CONNECT_TIMEOUT_SEC = 1.0
IO_TIMEOUT_SEC = 1.0
LOCK_TIMEOUT_SEC = 1.0
# A closed socket may not wake another thread's select on macOS.
CANCEL_POLL_SEC = 0.05


async def run_io(
    stream: TtsStream,
    method: str,
    *args,
    on_accepted=None,
    **kwargs,
):
    """Own a socket operation through cancellation and observe completed writes.

    Cancellation closes the socket; bounded I/O slices let the worker observe
    that close. The worker must finish before another flush/write.
    """
    worker = asyncio.create_task(asyncio.to_thread(getattr(stream, method), *args, **kwargs))
    cancelled = False
    current = asyncio.current_task()
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
            stream._poison(reason=None, poison_reason="cancelled")
            if current is not None:
                current.uncancel()
    try:
        result = worker.result()
    except Exception:  # noqa: BLE001
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if on_accepted is not None:
        await on_accepted()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _segment_kind(kind: str) -> str:
    if kind in {"assistant", "cue", "chirp"}:
        return kind
    logger.warning(
        "fan-in TTS IPC segment kind rejected: %r; falling back to assistant",
        kind,
    )
    return "assistant"


def _provider_token(provider_item_id: str | None) -> str:
    if provider_item_id is None:
        return "-"
    if _token_ok(provider_item_id):
        return provider_item_id
    logger.warning("fan-in TTS IPC provider item id rejected: %r", provider_item_id)
    return "-"


def _token_ok(value: str) -> bool:
    return bool(value) and value.isascii() and not any(ch.isspace() for ch in value)


def _profile_tokens(profile) -> list[str] | None:
    if profile is None:
        return None
    for field in (profile.provider, profile.model, profile.voice):
        if not _token_ok(field):
            logger.warning(
                "fan-in TTS IPC profile token rejected: provider=%r model=%r voice=%r",
                profile.provider, profile.model, profile.voice,
            )
            return None
    return [
        profile.provider,
        profile.model,
        profile.voice,
        f"{profile.source_lufs:.2f}",
        f"{profile.source_peak_dbfs:.2f}",
        f"{profile.confidence:.2f}",
    ]


class TtsStream:
    """The one TtsPlayout transport: a blocking writer over the TTS Unix socket.

    TtsPlayout does resample, mono-to-stereo, and drain accounting before
    calling into this adapter from a worker thread, so every method here may
    block for up to its bounded timeout.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._sock.settimeout(IO_TIMEOUT_SEC)
        self._recv_buffer = bytearray()
        self._lock = threading.Lock()
        self._active_segment: tuple[str, str, tuple[str, ...] | None] | None = None
        self._closed = False
        self._timeout_logged = False
        self._poison_reason: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def poison_reason(self) -> str | None:
        """Why `_poison` closed this stream (e.g. "cancelled", "lock",
        "send", "send_error", "flush_timeout", "flush_error", "peer_closed") —
        attribution for a reconnect logged far from the close."""
        return self._poison_reason

    def drop_if_peer_closed(self) -> bool:
        """Poison this live-looking stream if fan-in has closed its end (fan-in
        restarted); True when this call dropped it.

        A zero-timeout readability check, then a peek: it consumes nothing and
        never blocks. A stream whose lock is held is in use, so not stale. A
        concurrent ``_poison`` (it sets ``_closed`` before closing the socket)
        keeps its own reason."""
        if self._closed or not self._lock.acquire(blocking=False):
            return False
        try:
            if self._closed:
                return False
            try:
                readable, _, _ = select.select([self._sock], [], [], 0)
                peer_closed = bool(readable) and self._sock.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                peer_closed = not self._closed
            if peer_closed:
                self._poison(reason=None, poison_reason="peer_closed")
            return peer_closed
        finally:
            self._lock.release()

    def _readline_locked(self, timeout_sec: float) -> bytes:
        """Read one daemon response line while the caller holds _lock."""
        deadline = time.monotonic() + timeout_sec
        while True:
            newline_at = self._recv_buffer.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._recv_buffer[: newline_at + 1])
                del self._recv_buffer[: newline_at + 1]
                return line

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if self._closed:
                raise BrokenPipeError("TTS IPC socket is closed")
            readable, _, _ = select.select(
                [self._sock], [], [], min(remaining, CANCEL_POLL_SEC),
            )
            if not readable:
                continue
            chunk = self._sock.recv(4096)
            if not chunk:
                return b""
            self._recv_buffer.extend(chunk)

    def _close_unlocked(
        self, *, send_close: bool, poison_reason: str | None = None,
    ) -> None:
        if self._closed:
            return
        try:
            if send_close:
                if self._active_segment is not None:
                    self._send_line(wire.TTS_SEGMENT_END)
                    self._active_segment = None
                self._send_line(wire.TTS_CLOSE)
        except OSError:
            pass
        self._poison(reason=None, poison_reason=poison_reason)

    def _poison(
        self,
        *,
        reason: str | None,
        timeout_sec: float | None = None,
        poison_reason: str | None = None,
    ) -> None:
        """Close from any thread; blocked operations check closure each slice.

        ``reason`` drives the timeout warning below; ``poison_reason`` is the
        attribution a later reconnect log reads back (defaults to ``reason``),
        letting a non-timeout caller (cancellation) name itself without
        triggering that warning.
        """

        if self._closed:
            return
        self._closed = True
        self._poison_reason = poison_reason if poison_reason is not None else reason
        self._active_segment = None
        self._recv_buffer.clear()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if reason is not None and not self._timeout_logged:
            self._timeout_logged = True
            if timeout_sec is None:
                timeout_sec = (
                    LOCK_TIMEOUT_SEC
                    if reason == "lock"
                    else IO_TIMEOUT_SEC
                )
            log_event(
                logger,
                "tts_fanin.adapter_timeout",
                phase=reason,
                timeout_sec=timeout_sec,
                action="socket_poisoned",
                level=logging.WARNING,
            )

    @staticmethod
    def _remaining_timeout(
        ceiling_sec: float,
        deadline_monotonic: float | None,
    ) -> float:
        if deadline_monotonic is None:
            return ceiling_sec
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError("TTS IPC aggregate deadline expired")
        return min(ceiling_sec, remaining)

    @contextmanager
    def _bounded_lock(
        self,
        *,
        deadline_monotonic: float | None = None,
    ):
        try:
            timeout_sec = self._remaining_timeout(
                LOCK_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="lock", timeout_sec=0.0)
            raise
        acquired = self._lock.acquire(timeout=timeout_sec)
        if not acquired:
            # Only the owning worker can release this lock. Closing the
            # socket makes that worker fail within its current I/O slice.
            self._poison(reason="lock", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC adapter lock timed out after "
                f"{timeout_sec:.3f}s"
            )
        try:
            yield
        finally:
            self._lock.release()

    def _send_line(
        self,
        command: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        self._sendall_locked(
            wire.encode(command), deadline_monotonic=deadline_monotonic,
        )

    def _sendall_locked(
        self,
        data: bytes,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if self._closed:
            raise BrokenPipeError("TTS IPC socket is closed")
        try:
            timeout_sec = self._remaining_timeout(
                IO_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="send", timeout_sec=0.0)
            raise
        try:
            deadline = time.monotonic() + timeout_sec
            remaining_data = memoryview(data)
            while remaining_data:
                if self._closed:
                    raise BrokenPipeError("TTS IPC socket is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._sock.settimeout(min(remaining, CANCEL_POLL_SEC))
                try:
                    sent = self._sock.send(remaining_data)
                except TimeoutError:
                    continue
                if sent == 0:
                    raise BrokenPipeError("TTS IPC socket stopped accepting bytes")
                remaining_data = remaining_data[sent:]
        except TimeoutError as e:
            self._poison(reason="send", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC send timed out after "
                f"{timeout_sec:.3f}s"
            ) from e
        except OSError:
            self._poison(reason=None, poison_reason="send_error")
            raise

    def set_gain_db(self, db: float) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_gain(db))

    def program_duck(self, on: bool) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_program_duck(on))

    def prepare_assistant(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        volume_context: EffectiveVolumeContext | None = None,
    ) -> None:
        if not (
            _token_ok(provider)
            and _token_ok(model)
            and _token_ok(voice)
        ):
            logger.warning(
                "fan-in TTS IPC prepare rejected invalid profile identity: "
                "provider=%r model=%r voice=%r",
                provider, model, voice,
            )
            return
        with self._bounded_lock():
            self._send_line(
                wire.tts_prepare_assistant(
                    provider=provider,
                    model=model,
                    voice=voice,
                    tts_envelope_lufs=tts_envelope_lufs,
                    volume_context=volume_context,
                )
            )

    def pause_content_meter(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        with self._bounded_lock(deadline_monotonic=deadline_monotonic):
            self._send_line(
                wire.TTS_CONTENT_METER_PAUSE,
                deadline_monotonic=deadline_monotonic,
            )

    def resume_content_meter(self) -> None:
        with self._bounded_lock():
            self._send_line(wire.TTS_CONTENT_METER_RESUME)

    def start_segment(
        self,
        *,
        kind: str,
        provider_item_id: str | None,
        profile=None,
    ) -> None:
        profile_tokens = _profile_tokens(profile)
        segment = (
            _segment_kind(kind),
            _provider_token(provider_item_id),
            tuple(profile_tokens) if profile_tokens is not None else None,
        )
        with self._bounded_lock():
            if self._active_segment == segment:
                return
            if self._active_segment is not None:
                self._send_line(wire.TTS_SEGMENT_END)
            self._send_line(wire.tts_segment_start(*segment))
            self._active_segment = segment

    def end_segment(self) -> None:
        with self._bounded_lock():
            if self._active_segment is None:
                return
            self._send_line(wire.TTS_SEGMENT_END)
            self._active_segment = None

    def write(self, data: bytes) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_audio(len(data)))
            self._sendall_locked(data)

    def flush_sync(self) -> dict | None:
        with self._bounded_lock():
            try:
                self._send_line(wire.TTS_FLUSH_SYNC)
                self._active_segment = None
                line = self._readline_locked(FLUSH_ACK_TIMEOUT_SEC)
            except TimeoutError:
                logger.warning(
                    "fan-in TTS IPC flush ack timed out after %.1fs; "
                    "closing socket",
                    FLUSH_ACK_TIMEOUT_SEC,
                )
                self._close_unlocked(send_close=False, poison_reason="flush_timeout")
                return None
            except OSError as e:
                logger.warning("fan-in TTS IPC flush failed: %s", e)
                self._close_unlocked(send_close=False, poison_reason="flush_error")
                return None
        if not line:
            return None
        try:
            ack = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("fan-in TTS IPC flush ack parse failed: %s", e)
            return None
        if not isinstance(ack, dict):
            logger.warning("fan-in TTS IPC flush ack had unexpected shape: %r", ack)
            return None
        return ack

    def close(self) -> None:
        if self._closed:
            return
        with self._bounded_lock():
            self._close_unlocked(send_close=True)


def confirmed_tts_flush(ack: object) -> bool:
    """Validate the shared fan-in/outputd stop and segment ledger reply."""
    if not isinstance(ack, dict) or ack.get("ok") is not True:
        return False
    for key in ("requests", "pending_frames", "segments", "flushed_frames", "max_audio_played_ms"):
        value = ack.get(key)
        if type(value) is not int or value < 0:
            return False
    events = ack.get("events")
    if ack["requests"] == 0 or not isinstance(events, list) or len(events) != ack["segments"]:
        return False
    segments = set()
    for event in events:
        if not isinstance(event, dict):
            return False
        for key in ("segment", "queued_frames", "written_frames", "drained_frames", "flushed_frames"):
            value = event.get(key)
            if type(value) is not int or value < 0:
                return False
        if event["segment"] in segments:
            return False
        segments.add(event["segment"])
        item = event.get("provider_item_id")
        if "provider_item_id" not in event or not (item is None or isinstance(item, str) and item):
            return False
        if event.get("kind") not in {"assistant", "cue", "chirp"}:
            return False
        if not 0 <= event["drained_frames"] <= event["written_frames"] <= event["queued_frames"]:
            return False
        if event["flushed_frames"] > event["queued_frames"]:
            return False
    return True


async def connect(socket_path: str) -> TtsStream:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_SEC)
    connect_task = asyncio.create_task(
        asyncio.to_thread(sock.connect, socket_path)
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(connect_task),
            timeout=CONNECT_TIMEOUT_SEC,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        # The thread is bounded by the socket timeout too, but it may
        # finish after this coroutine returns. Consume that late outcome.
        connect_task.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        if isinstance(e, asyncio.CancelledError):
            raise
        log_event(
            logger,
            "tts_fanin.connect_timeout",
            socket=socket_path,
            timeout_sec=CONNECT_TIMEOUT_SEC,
            level=logging.WARNING,
        )
        raise TimeoutError(
            "TTS IPC connect timed out after "
            f"{CONNECT_TIMEOUT_SEC:.1f}s"
        ) from None
    except Exception as e:  # noqa: BLE001
        sock.close()
        logger.error(
            "fan-in TTS IPC connect failed: socket=%s exc=%s: %s",
            socket_path, type(e).__name__, e,
        )
        raise
    return TtsStream(sock)
