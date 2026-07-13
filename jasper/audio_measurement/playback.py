# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Policy-free WAV and continuous-tone process mechanics.

Feature owners choose the ALSA lane, cache directory, target, frequency band,
level, admission evidence, and repeat policy.  This leaf validates structural
resource bounds, emits an already-admitted WAV, bounds process diagnostics and
cleanup, and generates deterministic sine WAVs without retaining a powerful
audio or DSP host object.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import logging
import math
import os
import stat
import struct
import tempfile
import uuid
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np

from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


class PlaybackFailureCode(str, Enum):
    """Closed failure vocabulary for a WAV emission attempt."""

    INVALID_REQUEST = "invalid_request"
    MISSING_FILE = "missing_file"
    START_FAILED = "start_failed"
    TIMEOUT = "timeout"
    WAIT_FAILED = "wait_failed"
    PROCESS_FAILED = "process_failed"
    CLEANUP_FAILED = "cleanup_failed"


class PlaybackCleanupState(str, Enum):
    """Observed cleanup state for an emitted child process."""

    NOT_NEEDED = "not_needed"
    KILLED_AND_REAPED = "killed_and_reaped"
    KILL_SENT_REAP_UNCONFIRMED = "kill_sent_reap_unconfirmed"


@dataclass(frozen=True)
class PlaybackResult:
    """Successful, fully reaped WAV emission result."""

    wav_path: Path
    alsa_device: str
    returncode: int
    cleanup_state: PlaybackCleanupState = PlaybackCleanupState.NOT_NEEDED
    diagnostic_tail: str = ""


class SweepPlaybackError(RuntimeError):
    """Typed WAV-process failure.

    The historical class name remains canonical so legacy Room callers can
    continue catching ``SweepPlaybackError`` while neutral callers use the
    ``PlaybackError`` alias.
    """

    def __init__(
        self,
        message: str,
        *,
        code: PlaybackFailureCode,
        wav_path: Path,
        alsa_device: str,
        returncode: int | None = None,
        cleanup_state: PlaybackCleanupState = PlaybackCleanupState.NOT_NEEDED,
        diagnostic_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.wav_path = wav_path
        self.alsa_device = alsa_device
        self.returncode = returncode
        self.cleanup_state = cleanup_state
        self.diagnostic_tail = diagnostic_tail


PlaybackError = SweepPlaybackError


class WavSourceFailureCode(str, Enum):
    """Closed failure vocabulary for content-bound WAV sources."""

    UNSAFE_PATH = "unsafe_path"
    READ_FAILED = "read_failed"
    RESOURCE_LIMIT = "resource_limit"
    CONTENT_MISMATCH = "content_mismatch"
    INVALID_WAV = "invalid_wav"
    CLEANUP_FAILED = "cleanup_failed"


class WavSourceError(RuntimeError):
    """An exact feature-owned WAV artifact could not be safely consumed."""

    def __init__(
        self,
        message: str,
        *,
        code: WavSourceFailureCode,
        wav_path: Path,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.wav_path = wav_path


class WavPlaybackCancelledBeforeSpawn(asyncio.CancelledError):
    """The final content recheck was cancelled before aplay could start."""


_DIAGNOSTIC_TAIL_BYTES = 8 * 1024
_TONE_CHUNK_SAMPLES = 64 * 1024
_PROCESS_CLEANUP_TIMEOUT_S = 2.0
_WAV_HASH_CHUNK_BYTES = 64 * 1024
_WAV_FRAME_CHUNK = 64 * 1024
MAX_VERIFIED_WAV_BYTES = 64 * 1024 * 1024
MAX_VERIFIED_WAV_CHANNELS = 8

# The longest shipped consumer is Room crossover leveling: 90 s at 48 kHz.
# This bounds disk/CPU work before any allocation or file creation while still
# preserving every current call shape.
MAX_TONE_SAMPLES = 90 * 48_000
MAX_TONE_SAMPLE_RATE = 192_000
MAX_TONE_DURATION_S = 90.0


@dataclass(frozen=True)
class _CleanupOutcome:
    diagnostic_tail: str
    state: PlaybackCleanupState
    cancellation: asyncio.CancelledError | None = None


class _ProcessWaitFailure(RuntimeError):
    """Internal wrapper that keeps process/pipe failures catchable narrowly."""


@dataclass(slots=True)
class _VerifiedWavSource:
    path: Path
    artifact: ArtifactIdentity
    fd: int
    device: int
    inode: int
    mtime_ns: int
    channels: int
    sample_width_bytes: int
    sample_rate_hz: int
    frame_count: int
    closed: bool = False

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.sample_rate_hz

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
            self.closed = True


async def _read_diagnostic_tail(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > _DIAGNOSTIC_TAIL_BYTES:
            del tail[: len(tail) - _DIAGNOSTIC_TAIL_BYTES]
    return bytes(tail).decode(errors="replace").strip()


async def _wait_and_read_diagnostic_tail(
    proc: asyncio.subprocess.Process,
) -> str:
    stderr_task = asyncio.create_task(_read_diagnostic_tail(proc.stderr))
    try:
        await proc.wait()
        return await stderr_task
    except asyncio.CancelledError:
        stderr_task.cancel()
        stderr_task.add_done_callback(_consume_task_result)
        raise
    except Exception as exc:  # noqa: BLE001 -- process waits can fail arbitrarily
        stderr_task.cancel()
        stderr_task.add_done_callback(_consume_task_result)
        raise _ProcessWaitFailure from exc


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 -- cleanup cannot recover task failures
        pass


async def _settle_after_kill(
    task: asyncio.Task[str],
    *,
    cleanup_timeout_s: float,
) -> _CleanupOutcome:
    """Observe killed-process cleanup despite repeated caller cancellation."""

    waiter = asyncio.create_task(asyncio.wait({task}, timeout=cleanup_timeout_s))
    cancellation: asyncio.CancelledError | None = None
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError as exc:
            cancellation = exc
    waiter.result()
    if not task.done():
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return _CleanupOutcome(
            diagnostic_tail="",
            state=PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED,
            cancellation=cancellation,
        )
    try:
        diagnostic = task.result()
    except asyncio.CancelledError:
        return _CleanupOutcome(
            diagnostic_tail="",
            state=PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED,
            cancellation=cancellation,
        )
    except _ProcessWaitFailure:
        return _CleanupOutcome(
            diagnostic_tail="",
            state=PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED,
            cancellation=cancellation,
        )
    return _CleanupOutcome(
        diagnostic_tail=diagnostic,
        state=PlaybackCleanupState.KILLED_AND_REAPED,
        cancellation=cancellation,
    )


async def _kill_and_settle(
    proc: asyncio.subprocess.Process,
    task: asyncio.Task[str],
) -> _CleanupOutcome:
    try:
        if proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass
    except OSError:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return _CleanupOutcome(
            diagnostic_tail="",
            state=PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED,
        )
    return await _settle_after_kill(
        task,
        cleanup_timeout_s=_PROCESS_CLEANUP_TIMEOUT_S,
    )


def _wav_source_error_code(exc: OSError) -> WavSourceFailureCode:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return WavSourceFailureCode.UNSAFE_PATH
    return WavSourceFailureCode.READ_FAILED


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, _WAV_HASH_CHUNK_BYTES):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("verified WAV snapshot write made no progress")
        view = view[written:]


def _snapshot_verified_wav(
    source_fd: int,
    *,
    artifact: ArtifactIdentity,
    path: Path,
) -> int:
    """Copy exact bytes into a sealed memfd (or unlinked read-only fallback)."""

    snapshot_fd: int | None = None
    temporary_path: str | None = None
    memfd_create = getattr(os, "memfd_create", None)
    sealed = memfd_create is not None
    try:
        if sealed:
            assert memfd_create is not None
            flags = getattr(os, "MFD_CLOEXEC", 0x0001) | getattr(
                os,
                "MFD_ALLOW_SEALING",
                0x0002,
            )
            snapshot_fd = memfd_create("jasper-measurement-wav", flags)
        else:
            snapshot_fd, temporary_path = tempfile.mkstemp(
                prefix="jasper-measurement-wav."
            )
            os.fchmod(snapshot_fd, 0o600)

        digest = hashlib.sha256()
        copied = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, _WAV_HASH_CHUNK_BYTES):
            copied += len(chunk)
            if copied > MAX_VERIFIED_WAV_BYTES:
                raise WavSourceError(
                    "measurement WAV exceeds the verified-source byte bound",
                    code=WavSourceFailureCode.RESOURCE_LIMIT,
                    wav_path=path,
                )
            digest.update(chunk)
            _write_all(snapshot_fd, chunk)
        if copied != artifact.byte_size or digest.hexdigest() != artifact.sha256:
            raise WavSourceError(
                "measurement WAV bytes do not match their artifact identity",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=path,
            )
        os.fsync(snapshot_fd)
        os.lseek(snapshot_fd, 0, os.SEEK_SET)

        if sealed:
            seals = (
                getattr(fcntl, "F_SEAL_SEAL", 0x0001)
                | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
                | getattr(fcntl, "F_SEAL_GROW", 0x0004)
                | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
            )
            fcntl.fcntl(
                snapshot_fd,
                getattr(fcntl, "F_ADD_SEALS", 1033),
                seals,
            )
        else:
            assert temporary_path is not None
            read_flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            read_fd = os.open(temporary_path, read_flags)
            os.close(snapshot_fd)
            snapshot_fd = read_fd
            os.unlink(temporary_path)
            temporary_path = None
        return_fd = snapshot_fd
        snapshot_fd = None
        return return_fd
    finally:
        if snapshot_fd is not None:
            os.close(snapshot_fd)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _inspect_pcm_wav(fd: int, *, path: Path) -> tuple[int, int, int, int]:
    duplicate = os.dup(fd)
    try:
        with os.fdopen(duplicate, "rb") as raw:
            duplicate = -1
            with wave.open(raw, "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                frame_count = wav.getnframes()
                if wav.getcomptype() != "NONE":
                    raise WavSourceError(
                        "measurement WAV must contain uncompressed PCM",
                        code=WavSourceFailureCode.INVALID_WAV,
                        wav_path=path,
                    )
                if not 1 <= channels <= MAX_VERIFIED_WAV_CHANNELS:
                    raise WavSourceError(
                        "measurement WAV channel count is outside the supported bound",
                        code=WavSourceFailureCode.RESOURCE_LIMIT,
                        wav_path=path,
                    )
                if sample_width not in {1, 2, 3, 4}:
                    raise WavSourceError(
                        "measurement WAV sample width is unsupported",
                        code=WavSourceFailureCode.INVALID_WAV,
                        wav_path=path,
                    )
                if not 1 <= sample_rate <= MAX_TONE_SAMPLE_RATE:
                    raise WavSourceError(
                        "measurement WAV sample rate is outside the supported bound",
                        code=WavSourceFailureCode.RESOURCE_LIMIT,
                        wav_path=path,
                    )
                if frame_count <= 0 or frame_count / sample_rate > MAX_TONE_DURATION_S:
                    raise WavSourceError(
                        "measurement WAV duration is outside the supported bound",
                        code=WavSourceFailureCode.RESOURCE_LIMIT,
                        wav_path=path,
                    )
                frame_width = channels * sample_width
                frames_read = 0
                while frames_read < frame_count:
                    chunk = wav.readframes(
                        min(_WAV_FRAME_CHUNK, frame_count - frames_read)
                    )
                    if not chunk or len(chunk) % frame_width:
                        raise WavSourceError(
                            "measurement WAV PCM data is truncated or malformed",
                            code=WavSourceFailureCode.INVALID_WAV,
                            wav_path=path,
                        )
                    frames_read += len(chunk) // frame_width
                if frames_read != frame_count:
                    raise WavSourceError(
                        "measurement WAV frame count does not match its PCM data",
                        code=WavSourceFailureCode.INVALID_WAV,
                        wav_path=path,
                    )
    except WavSourceError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise WavSourceError(
            "measurement artifact is not a readable PCM WAV",
            code=WavSourceFailureCode.INVALID_WAV,
            wav_path=path,
        ) from exc
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    os.lseek(fd, 0, os.SEEK_SET)
    return channels, sample_width, sample_rate, frame_count


def _open_verified_wav_source(
    bundle_dir: str | Path,
    artifact: ArtifactIdentity,
) -> _VerifiedWavSource:
    if not isinstance(artifact, ArtifactIdentity):
        raise ValueError("artifact must be an ArtifactIdentity")
    path = Path(bundle_dir).joinpath(*artifact.relative_path.split("/"))
    if artifact.byte_size > MAX_VERIFIED_WAV_BYTES:
        raise WavSourceError(
            "measurement WAV exceeds the verified-source byte bound",
            code=WavSourceFailureCode.RESOURCE_LIMIT,
            wav_path=path,
        )
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    directory_fd: int | None = None
    fd: int | None = None
    snapshot_fd: int | None = None
    try:
        directory_fd = os.open(Path(bundle_dir), directory_flags)
        parts = artifact.relative_path.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WavSourceError(
                "measurement WAV artifact must be a regular file",
                code=WavSourceFailureCode.UNSAFE_PATH,
                wav_path=path,
            )
        if opened.st_size != artifact.byte_size:
            raise WavSourceError(
                "measurement WAV size does not match its artifact identity",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=path,
            )
        snapshot_fd = _snapshot_verified_wav(
            fd,
            artifact=artifact,
            path=path,
        )
        source_after_copy = os.fstat(fd)
        if (
            source_after_copy.st_dev != opened.st_dev
            or source_after_copy.st_ino != opened.st_ino
            or source_after_copy.st_size != opened.st_size
            or source_after_copy.st_mtime_ns != opened.st_mtime_ns
        ):
            raise WavSourceError(
                "measurement WAV changed while its immutable snapshot was created",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=path,
            )
        os.close(fd)
        fd = None
        channels, sample_width, sample_rate, frame_count = _inspect_pcm_wav(
            snapshot_fd,
            path=path,
        )
        verified = os.fstat(snapshot_fd)
        source = _VerifiedWavSource(
            path=path,
            artifact=artifact,
            fd=snapshot_fd,
            device=verified.st_dev,
            inode=verified.st_ino,
            mtime_ns=verified.st_mtime_ns,
            channels=channels,
            sample_width_bytes=sample_width,
            sample_rate_hz=sample_rate,
            frame_count=frame_count,
        )
        snapshot_fd = None
        return source
    except WavSourceError:
        raise
    except OSError as exc:
        raise WavSourceError(
            "measurement WAV could not be opened without following links",
            code=_wav_source_error_code(exc),
            wav_path=path,
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if snapshot_fd is not None:
            os.close(snapshot_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _verify_open_wav_source(source: _VerifiedWavSource) -> None:
    if not isinstance(source, _VerifiedWavSource) or source.closed:
        raise ValueError("verified WAV source is closed or invalid")
    try:
        current = os.fstat(source.fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != source.device
            or current.st_ino != source.inode
            or current.st_size != source.artifact.byte_size
            or current.st_mtime_ns != source.mtime_ns
            or _sha256_fd(source.fd) != source.artifact.sha256
        ):
            raise WavSourceError(
                "measurement WAV changed after admission verification",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=source.path,
            )
        observed = _inspect_pcm_wav(source.fd, path=source.path)
        expected = (
            source.channels,
            source.sample_width_bytes,
            source.sample_rate_hz,
            source.frame_count,
        )
        if observed != expected:
            raise WavSourceError(
                "measurement WAV format changed after admission verification",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=source.path,
            )
    except WavSourceError:
        raise
    except OSError as exc:
        raise WavSourceError(
            "measurement WAV could not be reverified",
            code=WavSourceFailureCode.READ_FAILED,
            wav_path=source.path,
        ) from exc


async def _drain_blocking_task(
    task: asyncio.Task[Any],
) -> tuple[Any, BaseException | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    return task.result(), cancellation


@asynccontextmanager
async def verified_wav_source(
    bundle_dir: str | Path,
    artifact: ArtifactIdentity,
) -> AsyncIterator[_VerifiedWavSource]:
    """Verify one no-link feature WAV and yield its immutable byte snapshot."""

    opening = asyncio.create_task(
        asyncio.to_thread(_open_verified_wav_source, bundle_dir, artifact)
    )
    source, cancellation = await _drain_blocking_task(opening)
    if cancellation is not None:
        source.close()
        raise cancellation
    try:
        yield source
    finally:
        try:
            source.close()
        except WavSourceError:
            raise
        except OSError as exc:
            raise WavSourceError(
                "verified WAV snapshot descriptor could not be closed",
                code=WavSourceFailureCode.CLEANUP_FAILED,
                wav_path=source.path,
            ) from exc


def validate_wav_playback_request(
    wav_path: str | Path,
    *,
    alsa_device: str,
    timeout_s: float,
) -> tuple[Path, float]:
    """Validate legacy path-based WAV inputs without emitting audio."""

    path = Path(wav_path)
    timeout = validate_wav_playback_control(
        path,
        alsa_device=alsa_device,
        timeout_s=timeout_s,
    )
    if not path.is_file():
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="failed",
            failure_code=PlaybackFailureCode.MISSING_FILE.value,
            device=alsa_device,
            level=logging.WARNING,
        )
        raise PlaybackError(
            f"WAV not found: {path}",
            code=PlaybackFailureCode.MISSING_FILE,
            wav_path=path,
            alsa_device=alsa_device,
        )
    return path, timeout


def validate_wav_playback_control(
    path: Path,
    *,
    alsa_device: str,
    timeout_s: float,
) -> float:
    _validate_alsa_device(alsa_device, wav_path=path)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise PlaybackError(
            "playback timeout must be a finite positive number",
            code=PlaybackFailureCode.INVALID_REQUEST,
            wav_path=path,
            alsa_device=alsa_device,
        )
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or timeout <= 0:
        raise PlaybackError(
            "playback timeout must be a finite positive number",
            code=PlaybackFailureCode.INVALID_REQUEST,
            wav_path=path,
            alsa_device=alsa_device,
        )
    return timeout


def _validate_alsa_device(alsa_device: object, *, wav_path: Path) -> str:
    if not isinstance(alsa_device, str) or not alsa_device.strip():
        raise PlaybackError(
            "ALSA device must be a non-empty string",
            code=PlaybackFailureCode.INVALID_REQUEST,
            wav_path=wav_path,
            alsa_device=str(alsa_device),
        )
    return alsa_device


async def _play_wav_source(
    path: Path,
    *,
    spawn_path: str,
    pass_fds: tuple[int, ...],
    alsa_device: str,
    timeout_s: float,
) -> PlaybackResult:
    timeout = validate_wav_playback_control(
        path,
        alsa_device=alsa_device,
        timeout_s=timeout_s,
    )

    try:
        process_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
        }
        if pass_fds:
            process_kwargs["pass_fds"] = pass_fds
        proc = await asyncio.create_subprocess_exec(
            "aplay",
            "-D",
            alsa_device,
            "-q",
            spawn_path,
            **process_kwargs,
        )
    except OSError as exc:
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="failed",
            failure_code=PlaybackFailureCode.START_FAILED.value,
            device=alsa_device,
            level=logging.WARNING,
        )
        raise PlaybackError(
            "could not start aplay",
            code=PlaybackFailureCode.START_FAILED,
            wav_path=path,
            alsa_device=alsa_device,
        ) from exc

    operation_task = asyncio.create_task(_wait_and_read_diagnostic_tail(proc))
    try:
        diagnostic = await asyncio.wait_for(
            asyncio.shield(operation_task),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        cleanup = await _kill_and_settle(proc, operation_task)
        if cleanup.cancellation is not None:
            log_event(
                logger,
                "audio_measurement.playback",
                operation="wav",
                result="cancelled",
                device=alsa_device,
                cleanup_state=cleanup.state.value,
            )
            raise cleanup.cancellation
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="failed",
            failure_code=PlaybackFailureCode.TIMEOUT.value,
            device=alsa_device,
            cleanup_state=cleanup.state.value,
            level=logging.WARNING,
        )
        raise PlaybackError(
            f"aplay timed out after {timeout_s} s playing {path}",
            code=PlaybackFailureCode.TIMEOUT,
            wav_path=path,
            alsa_device=alsa_device,
            returncode=proc.returncode,
            cleanup_state=cleanup.state,
            diagnostic_tail=cleanup.diagnostic_tail,
        ) from exc
    except asyncio.CancelledError as exc:
        cleanup = await _kill_and_settle(proc, operation_task)
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="cancelled",
            device=alsa_device,
            cleanup_state=cleanup.state.value,
        )
        raise cleanup.cancellation or exc
    except _ProcessWaitFailure as exc:
        cleanup = await _kill_and_settle(proc, operation_task)
        if cleanup.cancellation is not None:
            log_event(
                logger,
                "audio_measurement.playback",
                operation="wav",
                result="cancelled",
                device=alsa_device,
                cleanup_state=cleanup.state.value,
            )
            raise cleanup.cancellation
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="failed",
            failure_code=PlaybackFailureCode.WAIT_FAILED.value,
            device=alsa_device,
            cleanup_state=cleanup.state.value,
            level=logging.WARNING,
        )
        raise PlaybackError(
            "aplay process wait failed",
            code=PlaybackFailureCode.WAIT_FAILED,
            wav_path=path,
            alsa_device=alsa_device,
            returncode=proc.returncode,
            cleanup_state=cleanup.state,
            diagnostic_tail=cleanup.diagnostic_tail,
        ) from (exc.__cause__ or exc)

    returncode = proc.returncode
    if returncode != 0:
        log_event(
            logger,
            "audio_measurement.playback",
            operation="wav",
            result="failed",
            failure_code=PlaybackFailureCode.PROCESS_FAILED.value,
            device=alsa_device,
            returncode=returncode,
            level=logging.WARNING,
        )
        raise PlaybackError(
            f"aplay failed (rc={returncode}, device={alsa_device}): {diagnostic}",
            code=PlaybackFailureCode.PROCESS_FAILED,
            wav_path=path,
            alsa_device=alsa_device,
            returncode=returncode,
            diagnostic_tail=diagnostic,
        )
    log_event(
        logger,
        "audio_measurement.playback",
        operation="wav",
        result="completed",
        device=alsa_device,
    )
    return PlaybackResult(
        wav_path=path,
        alsa_device=alsa_device,
        returncode=0,
        diagnostic_tail=diagnostic,
    )


async def play_wav(
    wav_path: str | Path,
    *,
    alsa_device: str,
    timeout_s: float,
) -> PlaybackResult:
    """Emit one already-admitted legacy path WAV and wait until it is reaped."""

    path, timeout = validate_wav_playback_request(
        wav_path,
        alsa_device=alsa_device,
        timeout_s=timeout_s,
    )
    return await _play_wav_source(
        path,
        spawn_path=str(path),
        pass_fds=(),
        alsa_device=alsa_device,
        timeout_s=timeout,
    )


async def play_verified_wav(
    source: _VerifiedWavSource,
    *,
    alsa_device: str,
    timeout_s: float,
) -> PlaybackResult:
    """Reverify and emit one immutable artifact snapshot through its stable fd."""

    if not isinstance(source, _VerifiedWavSource) or source.closed:
        raise ValueError("source must be an open verified WAV source")
    timeout = validate_wav_playback_control(
        source.path,
        alsa_device=alsa_device,
        timeout_s=timeout_s,
    )
    verification = asyncio.create_task(
        asyncio.to_thread(_verify_open_wav_source, source)
    )
    _result, cancellation = await _drain_blocking_task(verification)
    if cancellation is not None:
        raise WavPlaybackCancelledBeforeSpawn from cancellation
    # The Pi production surface is Linux. pass_fds keeps this immutable snapshot
    # alive in aplay; later writes or pathname replacement cannot change it.
    return await _play_wav_source(
        source.path,
        spawn_path=f"/proc/self/fd/{source.fd}",
        pass_fds=(source.fd,),
        alsa_device=alsa_device,
        timeout_s=timeout,
    )


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _legacy_filename_key(value: float, *, units_per_step: float) -> int | None:
    scaled = value * units_per_step
    rounded = round(scaled)
    return int(rounded) if scaled == rounded else None


def _tone_cache_filename(
    *,
    frequency: float,
    duration: float,
    level_dbfs: float,
    sample_rate: int,
) -> str:
    frequency_key = _legacy_filename_key(frequency, units_per_step=1.0)
    duration_ms_key = _legacy_filename_key(duration, units_per_step=1000.0)
    level_tenths_key = _legacy_filename_key(
        abs(level_dbfs),
        units_per_step=10.0,
    )
    if (
        frequency_key is not None
        and duration_ms_key is not None
        and level_tenths_key is not None
    ):
        return (
            f"tone_{frequency_key}Hz_{duration_ms_key}ms_"
            f"{level_tenths_key}dbm_{sample_rate}Hz.wav"
        )
    exact_key = "_".join(
        struct.pack("!d", value).hex() for value in (frequency, duration, level_dbfs)
    )
    return f"tone_exact_{exact_key}_{sample_rate}Hz.wav"


def _validated_tone_shape(
    *,
    freq_hz: float,
    duration_s: float,
    dbfs: float,
    sample_rate: int,
) -> tuple[float, float, float, int, int]:
    frequency = _finite_number(freq_hz, field="freq_hz")
    duration = _finite_number(duration_s, field="duration_s")
    level_dbfs = _finite_number(dbfs, field="dbfs")
    if frequency <= 0:
        raise ValueError("freq_hz must be positive")
    if not 0 < duration <= MAX_TONE_DURATION_S:
        raise ValueError(
            f"duration_s must be positive and at most {MAX_TONE_DURATION_S:g}"
        )
    if level_dbfs > 0:
        raise ValueError("dbfs must not exceed full scale (0 dBFS)")
    if type(sample_rate) is not int or not 1 <= sample_rate <= MAX_TONE_SAMPLE_RATE:
        raise ValueError(
            f"sample_rate must be an integer between 1 and {MAX_TONE_SAMPLE_RATE}"
        )
    if frequency >= sample_rate / 2:
        raise ValueError("freq_hz must be below the sample-rate Nyquist frequency")

    sample_count = int(round(duration * sample_rate))
    if not 1 <= sample_count <= MAX_TONE_SAMPLES:
        raise ValueError(f"tone sample count must be between 1 and {MAX_TONE_SAMPLES}")
    return frequency, duration, level_dbfs, sample_rate, sample_count


def ensure_sine_wav(
    *,
    freq_hz: float,
    duration_s: float,
    dbfs: float,
    sample_rate: int,
    cache_dir: Path,
) -> Path:
    """Generate one structurally bounded deterministic mono sine WAV."""

    (
        frequency,
        duration,
        level_dbfs,
        rate,
        sample_count,
    ) = _validated_tone_shape(
        freq_hz=freq_hz,
        duration_s=duration_s,
        dbfs=dbfs,
        sample_rate=sample_rate,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    wav_path = cache_dir / _tone_cache_filename(
        frequency=frequency,
        duration=duration,
        level_dbfs=level_dbfs,
        sample_rate=rate,
    )
    if wav_path.exists():
        try:
            with wave.open(str(wav_path), "rb") as existing:
                valid_header = (
                    existing.getnchannels() == 1
                    and existing.getsampwidth() == 2
                    and existing.getframerate() == rate
                    and existing.getnframes() == sample_count
                )
            if valid_header and wav_path.stat().st_size == 44 + sample_count * 2:
                return wav_path
        except (OSError, EOFError, wave.Error):
            pass

    amp = 10 ** (level_dbfs / 20.0)
    fade = max(8, int(0.005 * rate))
    fade_in = np.linspace(0.0, 1.0, fade) ** 2
    fade_out = np.linspace(1.0, 0.0, fade) ** 2
    tmp_path = cache_dir / f".{wav_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp_path.open("xb") as raw_stream:
            with wave.open(raw_stream, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(rate)
                for start in range(0, sample_count, _TONE_CHUNK_SAMPLES):
                    stop = min(sample_count, start + _TONE_CHUNK_SAMPLES)
                    t = np.arange(start, stop, dtype=np.float64) / rate
                    signal = amp * np.sin(2 * math.pi * frequency * t)
                    if fade * 2 < sample_count and start < fade:
                        fade_stop = min(stop, fade)
                        signal[: fade_stop - start] *= fade_in[start:fade_stop]
                    fade_start = sample_count - fade
                    if fade * 2 < sample_count and stop > fade_start:
                        overlap_start = max(start, fade_start)
                        signal[overlap_start - start :] *= fade_out[
                            overlap_start - fade_start : stop - fade_start
                        ]
                    int16 = (np.clip(signal, -1.0, 1.0) * 32767.0).astype("<i2")
                    writer.writeframesraw(int16.tobytes())
        tmp_path.replace(wav_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    log_event(
        logger,
        "audio_measurement.tone_cached",
        frequency_hz=frequency,
        duration_s=duration,
        level_dbfs=level_dbfs,
        sample_rate=rate,
    )
    return wav_path


class TonePlayer:
    """Cancellable continuous tone process for a caller-admitted WAV."""

    def __init__(self, wav_path: str | Path, *, alsa_device: str) -> None:
        self._wav_path = Path(wav_path)
        self._alsa_device = _validate_alsa_device(
            alsa_device,
            wav_path=self._wav_path,
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._stop_requested = asyncio.Event()

    async def play(self) -> None:
        """Block until ``aplay`` exits naturally or :meth:`cancel` runs."""

        if not self._wav_path.is_file():
            log_event(
                logger,
                "audio_measurement.continuous_tone",
                result="failed",
                failure_code=PlaybackFailureCode.MISSING_FILE.value,
                device=self._alsa_device,
                level=logging.WARNING,
            )
            raise PlaybackError(
                f"tone WAV not found: {self._wav_path}",
                code=PlaybackFailureCode.MISSING_FILE,
                wav_path=self._wav_path,
                alsa_device=self._alsa_device,
            )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-D",
                self._alsa_device,
                "-q",
                str(self._wav_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log_event(
                logger,
                "audio_measurement.continuous_tone",
                result="failed",
                failure_code=PlaybackFailureCode.START_FAILED.value,
                device=self._alsa_device,
                level=logging.WARNING,
            )
            raise PlaybackError(
                "could not start continuous-tone aplay",
                code=PlaybackFailureCode.START_FAILED,
                wav_path=self._wav_path,
                alsa_device=self._alsa_device,
            ) from exc

        log_event(
            logger,
            "audio_measurement.continuous_tone",
            result="started",
            device=self._alsa_device,
        )

        operation_task = asyncio.create_task(_wait_and_read_diagnostic_tail(self._proc))
        stop_task = asyncio.create_task(self._stop_requested.wait())
        if self._cancelled:
            self._stop_requested.set()
        try:
            done, _pending = await asyncio.wait(
                {operation_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError as exc:
            stop_task.cancel()
            cleanup = await _kill_and_settle(self._proc, operation_task)
            log_event(
                logger,
                "audio_measurement.continuous_tone",
                result="cancelled",
                device=self._alsa_device,
                cleanup_state=cleanup.state.value,
            )
            raise cleanup.cancellation or exc

        if operation_task not in done:
            cleanup = await _kill_and_settle(self._proc, operation_task)
            stop_task.cancel()
            stop_task.add_done_callback(_consume_task_result)
            log_event(
                logger,
                "audio_measurement.continuous_tone",
                result="cancelled",
                device=self._alsa_device,
                returncode=self._proc.returncode,
                cleanup_state=cleanup.state.value,
                level=(
                    logging.WARNING
                    if cleanup.state is PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED
                    else logging.INFO
                ),
            )
            if cleanup.cancellation is not None:
                raise cleanup.cancellation
            if cleanup.state is PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED:
                raise PlaybackError(
                    "continuous-tone aplay cleanup could not be confirmed",
                    code=PlaybackFailureCode.CLEANUP_FAILED,
                    wav_path=self._wav_path,
                    alsa_device=self._alsa_device,
                    returncode=self._proc.returncode,
                    cleanup_state=cleanup.state,
                    diagnostic_tail=cleanup.diagnostic_tail,
                )
            return

        stop_task.cancel()
        stop_task.add_done_callback(_consume_task_result)
        try:
            diagnostic = operation_task.result()
        except _ProcessWaitFailure as exc:
            cleanup = await _kill_and_settle(self._proc, operation_task)
            log_event(
                logger,
                "audio_measurement.continuous_tone",
                result="failed",
                failure_code=PlaybackFailureCode.WAIT_FAILED.value,
                device=self._alsa_device,
                cleanup_state=cleanup.state.value,
                level=logging.WARNING,
            )
            raise PlaybackError(
                "continuous-tone aplay process wait failed",
                code=PlaybackFailureCode.WAIT_FAILED,
                wav_path=self._wav_path,
                alsa_device=self._alsa_device,
                returncode=self._proc.returncode,
                cleanup_state=cleanup.state,
            ) from (exc.__cause__ or exc)

        result = "cancelled" if self._cancelled else "completed"
        if self._proc.returncode != 0 and not self._cancelled:
            result = "failed"
        log_event(
            logger,
            "audio_measurement.continuous_tone",
            result=result,
            device=self._alsa_device,
            returncode=self._proc.returncode,
            cleanup_state=(
                PlaybackCleanupState.KILLED_AND_REAPED.value
                if self._cancelled
                else PlaybackCleanupState.NOT_NEEDED.value
            ),
            level=logging.WARNING if result == "failed" else logging.INFO,
        )
        if result == "failed":
            raise PlaybackError(
                "aplay failed playing continuous tone "
                f"(rc={self._proc.returncode}, device={self._alsa_device}): "
                f"{diagnostic}",
                code=PlaybackFailureCode.PROCESS_FAILED,
                wav_path=self._wav_path,
                alsa_device=self._alsa_device,
                returncode=self._proc.returncode,
                diagnostic_tail=diagnostic,
            )

    def cancel(self) -> None:
        """Request a stop from the owning event-loop thread."""

        self._cancelled = True
        self._stop_requested.set()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            except OSError:
                log_event(
                    logger,
                    "audio_measurement.continuous_tone",
                    result="cleanup_failed",
                    failure_code=PlaybackFailureCode.CLEANUP_FAILED.value,
                    device=self._alsa_device,
                    level=logging.WARNING,
                )

    @property
    def cancelled(self) -> bool:
        return self._cancelled
