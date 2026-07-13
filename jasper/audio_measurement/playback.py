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
import logging
import math
import struct
import uuid
import wave
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

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


_DIAGNOSTIC_TAIL_BYTES = 8 * 1024
_TONE_CHUNK_SAMPLES = 64 * 1024
_PROCESS_CLEANUP_TIMEOUT_S = 2.0

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


def _validated_playback_request(
    wav_path: str | Path,
    *,
    alsa_device: str,
    timeout_s: float,
) -> tuple[Path, float]:
    path = Path(wav_path)
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
    return path, timeout


def _validate_alsa_device(alsa_device: object, *, wav_path: Path) -> str:
    if not isinstance(alsa_device, str) or not alsa_device.strip():
        raise PlaybackError(
            "ALSA device must be a non-empty string",
            code=PlaybackFailureCode.INVALID_REQUEST,
            wav_path=wav_path,
            alsa_device=str(alsa_device),
        )
    return alsa_device


async def play_wav(
    wav_path: str | Path,
    *,
    alsa_device: str,
    timeout_s: float,
) -> PlaybackResult:
    """Emit one already-admitted WAV and return only after it is reaped."""

    path, timeout = _validated_playback_request(
        wav_path,
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

    try:
        proc = await asyncio.create_subprocess_exec(
            "aplay",
            "-D",
            alsa_device,
            "-q",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
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
        struct.pack("!d", value).hex()
        for value in (frequency, duration, level_dbfs)
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
        raise ValueError(
            f"tone sample count must be between 1 and {MAX_TONE_SAMPLES}"
        )
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
                    int16 = (
                        np.clip(signal, -1.0, 1.0) * 32767.0
                    ).astype("<i2")
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

        operation_task = asyncio.create_task(
            _wait_and_read_diagnostic_tail(self._proc)
        )
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
                    if cleanup.state
                    is PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED
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
