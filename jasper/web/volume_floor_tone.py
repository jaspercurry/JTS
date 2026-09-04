# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Held reference tone for the /sound/setup/ volume-floor audition.

Owns the tone's process, its COMMISSIONING claim on the main fader and the
one session that arbitrates them; the sound page keeps only the two route
adapters that bind its CamillaDSP controller.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from jasper.audio_measurement.correction_lane import (
    CORRECTION_TONE_DIR,
    popen_correction_play,
)
from jasper.log_event import log_event
from jasper.sound.settings import SoundSettings, load_sound_settings
from jasper.volume_curve import percent_to_db
from jasper.volume_owner import ClaimKind, VolumeClaimHandle, volume_owner

from ._common import terminate_process

logger = logging.getLogger(__name__)

VOLUME_FLOOR_TONE_FREQS_HZ = (125.0, 500.0, 2000.0)
VOLUME_FLOOR_TONE_SOURCE_DBFS = -12.0
VOLUME_FLOOR_TONE_CHUNK_DURATION_S = 8.0
VOLUME_FLOOR_TONE_SEGMENT_DURATION_S = 0.75
VOLUME_FLOOR_TONE_MAX_DURATION_S = 10 * 60.0
VOLUME_FLOOR_TONE_SAMPLE_RATE = 48000
VOLUME_FLOOR_TONE_STARTUP_CHECK_S = 0.08


def _volume_floor_tone_wav_path() -> Path:
    """Generate and cache the volume-floor reference WAV.

    A short repeating low/mid/high sequence, not a steady sine, so the floor is
    judged across the speaker rather than around one narrow frequency.
    """

    cache_dir = Path(
        os.environ.get("JASPER_VOLUME_FLOOR_TONE_DIR", str(CORRECTION_TONE_DIR))
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    freq_key = "-".join(str(int(freq)) for freq in VOLUME_FLOOR_TONE_FREQS_HZ)
    wav_path = cache_dir / (
        f"volume_floor_reference_{freq_key}Hz_"
        f"{int(VOLUME_FLOOR_TONE_CHUNK_DURATION_S * 1000)}ms_"
        f"{int(abs(VOLUME_FLOOR_TONE_SOURCE_DBFS) * 10)}dbm_"
        f"{VOLUME_FLOOR_TONE_SAMPLE_RATE}Hz.wav"
    )
    if wav_path.exists():
        return wav_path

    import numpy as np
    from scipy.io import wavfile

    sample_rate = VOLUME_FLOOR_TONE_SAMPLE_RATE
    total_n = int(round(VOLUME_FLOOR_TONE_CHUNK_DURATION_S * sample_rate))
    segment_n = max(1, int(round(VOLUME_FLOOR_TONE_SEGMENT_DURATION_S * sample_rate)))
    amp = 10 ** (VOLUME_FLOOR_TONE_SOURCE_DBFS / 20.0)
    fade = max(8, int(0.005 * sample_rate))
    parts: list[Any] = []
    samples_written = 0
    while samples_written < total_n:
        for freq_hz in VOLUME_FLOOR_TONE_FREQS_HZ:
            t = np.arange(segment_n, dtype=np.float64) / sample_rate
            sig = amp * np.sin(2 * math.pi * freq_hz * t)
            if fade * 2 < segment_n:
                sig[:fade] *= np.linspace(0.0, 1.0, fade) ** 2
                sig[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2
            parts.append(sig)
            samples_written += len(sig)
            if samples_written >= total_n:
                break
    out = np.concatenate(parts)[:total_n]
    int16 = (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16)
    tmp_path = wav_path.with_name(f".{wav_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        wavfile.write(str(tmp_path), sample_rate, int16)
        os.replace(tmp_path, wav_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    logger.info(
        "volume floor reference tone cached: %s (%s Hz, %.1f s, %.1f dBFS)",
        wav_path,
        ",".join(str(int(freq)) for freq in VOLUME_FLOOR_TONE_FREQS_HZ),
        VOLUME_FLOOR_TONE_CHUNK_DURATION_S,
        VOLUME_FLOOR_TONE_SOURCE_DBFS,
    )
    return wav_path


class _LoopingVolumeFloorTone:
    """Small `aplay` loop independent of per-request asyncio loops."""

    def __init__(
        self,
        wav_path: str | Path,
        *,
        on_finish: Callable[[Any, str], None] | None = None,
        max_duration_s: float = VOLUME_FLOOR_TONE_MAX_DURATION_S,
    ) -> None:
        self._wav_path = Path(wav_path)
        self._max_duration_s = max_duration_s
        self._on_finish = on_finish
        self._stop = threading.Event()
        self._proc_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="jts-volume-floor-tone",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate_current()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    @property
    def error(self) -> str | None:
        with self._error_lock:
            return self._error

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set() and not self.error

    def _set_error(self, message: str) -> None:
        with self._error_lock:
            self._error = message

    def _terminate_current(self) -> None:
        with self._proc_lock:
            proc = self._proc
        terminate_process(proc)

    def _run(self) -> None:
        deadline = time.monotonic() + self._max_duration_s
        finish_reason = ""
        try:
            while not self._stop.is_set():
                if time.monotonic() >= deadline:
                    finish_reason = "timeout"
                    self._set_error("volume floor tone safety timeout")
                    break
                try:
                    proc = popen_correction_play(
                        self._wav_path,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    finish_reason = "error"
                    self._set_error(str(exc))
                    log_event(
                        logger,
                        "sound.volume_floor_tone",
                        level=logging.ERROR,
                        exc_info=True,
                        action="play",
                        result="error",
                    )
                    break

                with self._proc_lock:
                    self._proc = proc

                rc: int | None = None
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if self._stop.is_set():
                        self._terminate_current()
                        break
                    if time.monotonic() >= deadline:
                        finish_reason = "timeout"
                        self._set_error("volume floor tone safety timeout")
                        self._terminate_current()
                        break
                    time.sleep(0.05)

                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None

                if self._stop.is_set():
                    finish_reason = "stopped"
                    break
                if finish_reason:
                    break
                if rc not in (0, None):
                    finish_reason = "error"
                    self._set_error(f"aplay exited with rc={rc}")
                    log_event(
                        logger,
                        "sound.volume_floor_tone",
                        level=logging.WARNING,
                        action="play",
                        result="error",
                        rc=rc,
                    )
                    break
                # Natural EOF of the short cached WAV: immediately loop it.
        finally:
            if finish_reason in {"error", "timeout"} and self._on_finish:
                self._on_finish(self, finish_reason)


async def _claim_floor_level(
    household_db: float, floor_target_db: float,
) -> VolumeClaimHandle | None:
    """Take the audition's COMMISSIONING claim over the household level.

    The household level is declared from the snapshot this audition just read:
    in the web process nothing else has told the owner what the speaker plays
    at, and without it the release below has no reference and would leave the
    fader parked at the floor.

    ``None`` when no owner is registered — the audition still runs, one level
    quieter than it asked for at worst. The owner logs that refusal itself.
    """
    owner = volume_owner()
    if owner is None:
        return None
    await owner.declare_household_level_db(household_db)
    return await owner.acquire_level(ClaimKind.COMMISSIONING, floor_target_db)


async def _release_floor_level(
    claim: VolumeClaimHandle | None, household_db: float,
) -> None:
    """Give the audition's claim back, landing on the household level.

    The declaration is outranked by the held claim and therefore writes
    nothing; the release is the single fader move.
    """
    owner = volume_owner()
    if owner is None or claim is None:
        return
    await owner.declare_household_level_db(household_db)
    await owner.release(claim)


class _VolumeFloorToneSession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._camilla_op_lock = threading.Lock()
        self._runner: Any | None = None
        # The COMMISSIONING claim this audition holds on the main fader. The
        # level it sits at moves with the slider, so the claim outlives it.
        self._claim: VolumeClaimHandle | None = None
        self._original_db: float | None = None
        self._original_mute: bool | None = None
        self._floor_db: float | None = None
        self._camilla_factory: Callable[[], Any] | None = None
        self._starting = False
        self._cancel_start = False
        self._generation = 0

    async def _acquire_camilla_op_lock(self) -> None:
        await asyncio.to_thread(self._camilla_op_lock.acquire)

    async def start_or_update(
        self,
        raw: dict[str, Any],
        *,
        camilla_factory: Callable[[], Any],
        runner_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        settings = SoundSettings.from_mapping(
            {
                **load_sound_settings().to_dict(),
                "volume_floor_db": raw.get("volume_floor_db"),
            }
        )
        floor_db = settings.volume_floor_db
        await self._stop_if_finished(camilla_factory=camilla_factory)

        runner_factory = runner_factory or _LoopingVolumeFloorTone
        started_runner: Any | None = None
        runner: Any | None
        generation: int
        action: str
        while True:
            with self._lock:
                if self._runner is not None:
                    runner = self._runner
                    generation = self._generation
                    action = "update"
                    break
                if not self._starting:
                    self._starting = True
                    self._cancel_start = False
                    runner = None
                    generation = self._generation
                    action = "start"
                    break
            await asyncio.sleep(0.02)

        if action == "start":
            original: tuple[float, bool] | None = None
            acquired_camilla_op = False
            try:
                runner = runner_factory(
                    _volume_floor_tone_wav_path(),
                    on_finish=self._runner_finished,
                )
                await self._acquire_camilla_op_lock()
                acquired_camilla_op = True
                camilla = camilla_factory()
                original = await camilla.get_volume_and_mute(best_effort=True)
                if original is None:
                    raise RuntimeError("CamillaDSP volume state is unavailable")
                self._claim = await _claim_floor_level(
                    original[0], percent_to_db(1, floor_db=floor_db),
                )
                await camilla.set_main_mute(False)
                with self._lock:
                    cancelled = self._cancel_start
                    self._starting = False
                    self._cancel_start = False
                    if not cancelled:
                        self._original_db, self._original_mute = original
                        self._camilla_factory = camilla_factory
                        self._runner = runner
                        self._floor_db = floor_db
                        self._generation += 1
                if cancelled:
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                    return self._inactive_payload(
                        floor_db=floor_db,
                        status="stopped",
                    )
                try:
                    runner.start()
                except (OSError, RuntimeError):
                    with self._lock:
                        if self._runner is runner:
                            self._clear_active_locked()
                            self._generation += 1
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                    original = None
                    raise
                started_runner = runner
            except (OSError, RuntimeError):
                with self._lock:
                    self._starting = False
                    self._cancel_start = False
                if original is not None:
                    await self._restore_snapshot(
                        camilla_factory=camilla_factory,
                        original_db=original[0],
                        original_mute=original[1],
                    )
                raise
            finally:
                if acquired_camilla_op:
                    self._camilla_op_lock.release()
        else:
            await self._acquire_camilla_op_lock()
            try:
                with self._lock:
                    active = self._runner is runner and self._generation == generation
                if not active:
                    return self._inactive_payload(
                        floor_db=floor_db,
                        status="stale",
                    )
                camilla = camilla_factory()
                # The claim is held; only the level it sits at moves. One
                # settle, so the tone steps between floors instead of jumping
                # to the household level and back down.
                owner = volume_owner()
                if owner is not None and self._claim is not None:
                    self._claim = await owner.relevel(
                        self._claim, percent_to_db(1, floor_db=floor_db),
                    )
                await camilla.set_main_mute(False)
                with self._lock:
                    if self._runner is runner and self._generation == generation:
                        self._floor_db = floor_db
                    else:
                        return self._inactive_payload(
                            floor_db=floor_db,
                            status="stale",
                        )
            finally:
                self._camilla_op_lock.release()

        if started_runner is not None:
            await asyncio.sleep(VOLUME_FLOOR_TONE_STARTUP_CHECK_S)
            error = getattr(started_runner, "error", None)
            if error:
                await self.stop(
                    camilla_factory=camilla_factory,
                    reason="startup_failed",
                )
                raise RuntimeError(str(error))

        log_event(
            logger,
            "sound.volume_floor_tone",
            action=action,
            floor_db=f"{floor_db:.1f}",
            result="ok",
        )
        return {
            "ok": True,
            "active": True,
            "continuous": True,
            "status": "started" if action == "start" else "updated",
            "volume_floor_db": floor_db,
            "percent": 1,
            "db": round(percent_to_db(1, floor_db=floor_db), 3),
        }

    def _inactive_payload(self, *, floor_db: float, status: str) -> dict[str, Any]:
        return {
            "ok": True,
            "active": False,
            "continuous": False,
            "status": status,
            "volume_floor_db": floor_db,
            "percent": 1,
            "db": round(percent_to_db(1, floor_db=floor_db), 3),
        }

    async def stop(
        self,
        *,
        camilla_factory: Callable[[], Any],
        reason: str,
    ) -> dict[str, Any]:
        original_db: float | None
        original_mute: bool | None
        with self._lock:
            starting = self._starting
            if starting:
                self._cancel_start = True
            runner = self._runner
            floor_db = self._floor_db
            original_db = self._original_db
            original_mute = self._original_mute
            if runner is not None:
                self._clear_active_locked()
                self._generation += 1
        if runner is not None:
            runner.stop()
        if original_db is not None and original_mute is not None:
            await self._acquire_camilla_op_lock()
            try:
                await self._restore_snapshot(
                    camilla_factory=camilla_factory,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            finally:
                self._camilla_op_lock.release()
        status = "stopped" if runner is not None or starting else "idle"
        log_event(
            logger,
            "sound.volume_floor_tone",
            action="stop",
            reason=reason,
            status=status,
        )
        payload = {"ok": True, "active": False, "status": status, "reason": reason}
        if floor_db is not None:
            payload["volume_floor_db"] = floor_db
        return payload

    async def _stop_if_finished(
        self,
        *,
        camilla_factory: Callable[[], Any],
    ) -> None:
        with self._lock:
            runner = self._runner
            finished = runner is not None and not getattr(runner, "running", False)
        if finished:
            await self.stop(camilla_factory=camilla_factory, reason="expired")

    def _runner_finished(self, runner: Any, reason: str) -> None:
        original_db: float | None
        original_mute: bool | None
        with self._lock:
            if self._runner is not runner:
                return
            camilla_factory = self._camilla_factory
            floor_db = self._floor_db
            original_db = self._original_db
            original_mute = self._original_mute
            self._clear_active_locked()
            self._generation += 1
        if camilla_factory is None:
            return
        try:
            asyncio.run(
                self._restore_after_runner_finish(
                    camilla_factory=camilla_factory,
                    reason=reason,
                    floor_db=floor_db,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            )
        except (OSError, RuntimeError):
            log_event(
                logger,
                "sound.volume_floor_tone",
                level=logging.ERROR,
                exc_info=True,
                action="restore",
                result="error",
                reason=reason,
            )

    async def _restore_after_runner_finish(
        self,
        *,
        camilla_factory: Callable[[], Any],
        reason: str,
        floor_db: float | None,
        original_db: float | None,
        original_mute: bool | None,
    ) -> None:
        if original_db is not None and original_mute is not None:
            await self._acquire_camilla_op_lock()
            try:
                await self._restore_snapshot(
                    camilla_factory=camilla_factory,
                    original_db=original_db,
                    original_mute=original_mute,
                )
            finally:
                self._camilla_op_lock.release()
        log_event(
            logger,
            "sound.volume_floor_tone",
            level=logging.WARNING,
            action="restore",
            reason=reason,
            floor_db="" if floor_db is None else f"{floor_db:.1f}",
        )

    async def _restore_snapshot(
        self,
        *,
        camilla_factory: Callable[[], Any],
        original_db: float,
        original_mute: bool,
    ) -> None:
        camilla = camilla_factory()
        # The ONE restore funnel: every path out of an audition reaches here,
        # so releasing the claim here also covers the cancelled start, the
        # failed runner and the outer error path. The release lands on the
        # household level the start declared (``original_db``); the mute
        # ordering around it stays this session's, not the owner's.
        claim, self._claim = self._claim, None
        if original_mute:
            await camilla.set_main_mute(True, best_effort=True)
            await _release_floor_level(claim, original_db)
        else:
            await _release_floor_level(claim, original_db)
            await camilla.set_main_mute(False, best_effort=True)

    def _clear_active_locked(self) -> None:
        self._runner = None
        self._original_db = None
        self._original_mute = None
        self._floor_db = None
        self._camilla_factory = None


_VOLUME_FLOOR_TONE_SESSION = _VolumeFloorToneSession()
