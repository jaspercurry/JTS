# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bundle placement and record annotation over the shared wired capture kernel."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from jasper.active_speaker.restore_wait import await_restore_task_resilient
from jasper.audio_measurement.playback import (
    PlaybackError, PlaybackObservation, WavPlaybackCancelled,
    WavPlaybackCancelledBeforeSpawn,
)
from jasper.log_event import log_event
from jasper.dsp_apply import _maybe_call
from jasper.json_fields import finite_float
from .playback_transaction import PlaybackInterrupted

from jasper.active_speaker.bundles import (
    CAPTURE_KIND_SEQUENTIAL, capture_artifact_relpath, register_capture,
)
from jasper.audio_measurement.bundles import read_artifact_manifest
from jasper.audio_measurement.program import ExcitationProgram
from jasper.audio_measurement.wired_capture import (
    WIRED_POST_ROLL_S, WIRED_PRE_PLAY_ALLOWANCE_S, WiredCaptureAnswer,
    WiredCaptureError, WiredMicDevice, WiredSplCeilingExceeded, WiredSplMonitor,
    make_wired_recorder, mint_wired_answer,
)

from .program_transaction import StimulusCaptureError, StimulusCaptureStopped

logger = logging.getLogger(__name__)


def _capture_stopped(
    cause: WiredCaptureError, playback: PlaybackObservation,
) -> StimulusCaptureStopped:
    if isinstance(cause, WiredSplCeilingExceeded):
        log_event(
            logger, "active_speaker.measurement_spl_ceiling_stop", level=logging.ERROR,
            observed_db_spl=round(cause.observed_db_spl, 2),
            ceiling_db_spl=cause.ceiling_db_spl, weighting="Z",
        )
    return StimulusCaptureStopped(getattr(cause, "code", "wired_capture_failed"), str(cause), playback)


def place_wired_answer(
    bundle_dir: Path, answer: WiredCaptureAnswer, *, phase: str, group: str,
) -> WiredCaptureAnswer:
    """Place and register raw bytes once; later analysis uses this exact path."""
    if answer.wav_path:
        return answer
    relative = capture_artifact_relpath("summed", group, None)
    path = bundle_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(answer.wav)
    entry = register_capture(
        bundle_dir, relative_path=relative,
        kind=CAPTURE_KIND_SEQUENTIAL if phase in ("check", "measure", "lateral") else "summed",
        payload={"speaker_group_id": group, "phase": phase, "measurement_status": "captured"},
    )
    if entry is None:
        raise StimulusCaptureError("the raw capture could not be registered")
    digest = next(
        row["sha256"] for row in read_artifact_manifest(bundle_dir)["artifacts"]
        if row["path"] == relative
    )
    return replace(answer, wav_path=relative, wav_sha256=digest)


@dataclass(frozen=True)
class WiredStimulusCapture:
    device: WiredMicDevice
    bundle_dir: Path
    recorder_factory: Callable[[int, float], Any] | None = None
    setup_reference: Callable[[], Mapping[str, Any] | None] | None = None
    spl_monitor: WiredSplMonitor | None = None
    read_loudness_volume_db: Callable[[], float | None | Awaitable[float | None]] | None = None
    _pending: list[WiredCaptureAnswer] = field(default_factory=list)

    async def around(self, play: Callable[[], Awaitable[None]], *, program: Any) -> str:
        self._pending.clear()
        if self.spl_monitor is not None:
            self.spl_monitor.reset()
        rate = int(program.sample_rate_hz)
        budget = float(program.total_samples) / rate + WIRED_PRE_PLAY_ALLOWANCE_S + WIRED_POST_ROLL_S
        recorder = (
            self.recorder_factory(rate, budget) if self.recorder_factory
            else make_wired_recorder(self.device, sample_rate_hz=rate, max_capture_s=budget)
        )
        if self.spl_monitor is not None:
            recorder.spl_monitor = self.spl_monitor
        started = asyncio.create_task(asyncio.to_thread(recorder.start))
        played = False
        try:
            try:
                await asyncio.shield(started)
            except asyncio.CancelledError:
                # The kernel bounds startup. Wait for that worker before aborting
                # so it cannot open the device after cleanup has returned.
                drained = asyncio.gather(started, return_exceptions=True)
                while not drained.done():
                    try:
                        await asyncio.shield(drained)
                    except asyncio.CancelledError:
                        continue
                raise
            except WiredSplCeilingExceeded as exc:
                raise _capture_stopped(exc, PlaybackObservation(emission="not_started")) from exc
            except (WiredCaptureError, OSError, ValueError) as exc:
                if self.spl_monitor is not None and isinstance(exc, WiredCaptureError):
                    raise _capture_stopped(exc, PlaybackObservation(emission="not_started")) from exc
                raise StimulusCaptureError("the measurement recorder never rolled") from exc
            if self.spl_monitor is None:
                await play()
            else:
                await self.guarded_play(play, recorder)
            played = True
        finally:
            if not played:
                recorder.abort()
        async def _finish() -> str:
            try:
                recording = await asyncio.to_thread(recorder.finish, tail_s=WIRED_POST_ROLL_S)
                answer = await asyncio.to_thread(self._mint_and_place, recording, str(program.phase))
            except (WiredCaptureError, OSError, ValueError) as exc:
                if self.spl_monitor is not None and isinstance(exc, WiredCaptureError):
                    raise _capture_stopped(exc, PlaybackObservation(emission="completed")) from exc
                raise StimulusCaptureError("the capture could not be placed") from exc
            if isinstance(program, ExcitationProgram):
                answer = replace(answer, program=program.to_dict())
            self._pending.append(answer)
            return answer.wav_path

        finishing = asyncio.create_task(_finish())
        try:
            return await await_restore_task_resilient(finishing)
        except asyncio.CancelledError as exc:
            if finishing.cancelled():
                raise
            raise PlaybackInterrupted(
                PlaybackObservation(emission="completed"), wav_path=finishing.result(),
            ) from exc

    @staticmethod
    async def guarded_play(play: Callable[[], Awaitable[None]], recorder: Any) -> None:
        if recorder.failure is not None:
            raise _capture_stopped(recorder.failure, PlaybackObservation(emission="not_started"))
        observation = PlaybackObservation()

        async def _play() -> None:
            nonlocal observation
            try:
                await play()
            except (WavPlaybackCancelled, PlaybackError) as exc:
                observation = exc.observation
                raise
            except WavPlaybackCancelledBeforeSpawn:
                observation = PlaybackObservation(emission="not_started")
                raise
            else:
                observation = PlaybackObservation(emission="completed")

        async def _watch() -> WiredCaptureError:
            while recorder.failure is None:
                await asyncio.sleep(0.01)
            return recorder.failure

        playing = asyncio.create_task(_play())
        watching = asyncio.create_task(_watch())
        failure = None
        try:
            try:
                done, _ = await asyncio.wait((playing, watching), return_when=asyncio.FIRST_COMPLETED)
                if watching in done:
                    failure = watching.result()
                else:
                    await playing
            finally:
                for task in (playing, watching):
                    if not task.done():
                        task.cancel()

                async def _drain() -> None:
                    await asyncio.gather(playing, watching, return_exceptions=True)

                await await_restore_task_resilient(asyncio.create_task(_drain()))
        except asyncio.CancelledError as exc:
            raise PlaybackInterrupted(observation) from exc
        if failure is not None:
            raise _capture_stopped(failure, observation)

    def _mint_and_place(self, recording: Any, phase: str) -> WiredCaptureAnswer:
        answer = mint_wired_answer(
            recording, device=self.device,
            setup=self.setup_reference() if self.setup_reference else None,
        )
        if self.spl_monitor is not None:
            answer = replace(answer, capture_integrity={
                **(answer.capture_integrity or {}),
                "spl": {
                    "weighting": "Z",
                    "max_window_db_spl": round(self.spl_monitor.max_window_db_spl, 2),
                    "loudest_half_second_db_spl": round(self.spl_monitor.loudest_half_second_db_spl, 2),
                    "ceiling_db_spl": self.spl_monitor.ceiling_db_spl,
                },
            })
        return place_wired_answer(self.bundle_dir, answer, phase=phase, group=phase)

    def take_answer(self) -> WiredCaptureAnswer | None:
        return self._pending.pop() if self._pending else None


@dataclass
class CapturedRecordStore:
    inner: Any
    capture: Any
    enrich: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]] | None = None
    after_bank: Callable[[Mapping[str, Any], str], None] | None = None

    async def bank(self, record: Mapping[str, Any]) -> str:
        return await self.bank_answer(record, self.capture.take_answer())

    async def bank_answer(self, record: Mapping[str, Any], answer: Any) -> str:
        metadata = await asyncio.to_thread(self.enrich, answer, record) if self.enrich else {}
        # Analysis owns pose/attempt identity. Engine facts name what actually played.
        payload = {**metadata, **record, **{name: metadata[name] for name in
            ("take_id", "position_deg", "position_axis", "vertical_deg", "prompt") if name in metadata}}
        error = ""
        try:
            loudness = await _maybe_call(getattr(self.capture, "read_loudness_volume_db", None))
        except Exception as exc:  # noqa: BLE001
            loudness, error = None, type(exc).__name__
        payload["loudness_volume_db"] = finite_float(loudness)
        if payload["loudness_volume_db"] is None:
            log_event(logger, "active_speaker.capture_loudness_unknown", level=logging.WARNING,
                      take_id=payload.get("take_id"), error_type=error,
                      reason="read_failed" if error else "unavailable" if loudness is None else "invalid_value")
        if answer is not None:
            for key, attr in (("capture_integrity", "capture_integrity"), ("capture_device", "device"),
                              ("capture_setup", "setup"), ("program", "program")):
                if value := getattr(answer, attr, None):
                    payload[key] = value
            payload.update(wav_path=answer.wav_path, wav_sha256=answer.wav_sha256, wav_bytes=len(answer.wav))
        payload["program_id"] = (payload.get("program") or {}).get("program_id")
        record_id = await self.inner.bank(payload)
        if self.after_bank:
            await asyncio.to_thread(self.after_bank, payload, record_id)
        return record_id
