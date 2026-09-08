# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One wired recording and one annotated take record, shared by every door."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from jasper.active_speaker.bundles import (
    CAPTURE_KIND_SEQUENTIAL, capture_artifact_relpath, register_capture,
)
from jasper.audio_measurement.bundles import read_artifact_manifest
from jasper.audio_measurement.mic_identity import SUPPORTED_MODELS
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError, WiredMicDevice, WiredRecorder,
    build_capture_integrity_report, encode_wav_s32, scan_zero_runs,
    select_capture_channel,
)

from .program_transaction import StimulusCaptureError

# Covers buffered playback and decay after the program's own tail.
WIRED_POST_ROLL_S = 1.0
# Recorder rolls across admission, graph proof and the writer lock.
WIRED_PRE_PLAY_ALLOWANCE_S = 20.0


@dataclass(frozen=True)
class WiredCaptureAnswer:
    wav: bytes
    device: Mapping[str, Any] | None = None
    setup: Mapping[str, Any] | None = None
    capture_integrity: Mapping[str, Any] | None = None
    wav_path: str = ""
    wav_sha256: str = ""


def setup_from_hint(hint: Any) -> Mapping[str, Any] | None:
    if hint is None or not getattr(hint, "resolvable", False):
        return None
    return {"calibration": {
        "mode": "stored", "calibration_id": str(hint.calibration_id),
        "model": str(hint.model),
    }}


def mint_wired_answer(
    recording: Any, *, device: WiredMicDevice, setup: Mapping[str, Any] | None = None,
) -> WiredCaptureAnswer:
    channel, mono, rms_dbfs = select_capture_channel(recording)
    zero_count, zero_runs = scan_zero_runs(mono)
    wav, frames = encode_wav_s32(mono, sample_rate_hz=recording.sample_rate_hz)
    return WiredCaptureAnswer(
        wav=wav, setup=setup,
        capture_integrity=build_capture_integrity_report(
            recording, encoded_frames=frames,
            zero_run_count=zero_count, zero_runs=zero_runs,
        ),
        device={
            "label": f"{device.model_label} ({device.card_id})", "wired": True,
            "card": device.card_id, "usb_id": device.usb_id,
            "model_key": device.model_key, "pcm": device.pcm,
            "channel_selected": channel,
            "channel_rms_dbfs": [round(value, 1) if math.isfinite(value) else None for value in rms_dbfs],
        },
    )


def make_wired_recorder(
    device: WiredMicDevice, *, sample_rate_hz: int, max_capture_s: float,
) -> WiredRecorder:
    return WiredRecorder(
        device.pcm, sample_rate_hz=sample_rate_hz,
        channels=int(SUPPORTED_MODELS.get(device.model_key, {}).get("capture_channels", 2)),
        max_capture_s=max_capture_s,
    )


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
    _pending: list[WiredCaptureAnswer] = field(default_factory=list)

    async def around(self, play: Callable[[], Awaitable[None]], *, program: Any) -> str:
        self._pending.clear()
        rate = int(program.sample_rate_hz)
        budget = float(program.total_samples) / rate + WIRED_PRE_PLAY_ALLOWANCE_S + WIRED_POST_ROLL_S
        recorder = (
            self.recorder_factory(rate, budget) if self.recorder_factory
            else make_wired_recorder(self.device, sample_rate_hz=rate, max_capture_s=budget)
        )
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
            except (WiredCaptureError, OSError, ValueError) as exc:
                raise StimulusCaptureError("the measurement recorder never rolled") from exc
            await play()
            played = True
        finally:
            if not played:
                recorder.abort()
        try:
            recording = await asyncio.to_thread(recorder.finish, tail_s=WIRED_POST_ROLL_S)
            answer = await asyncio.to_thread(self._mint_and_place, recording, str(program.phase))
        except (WiredCaptureError, OSError, ValueError) as exc:
            raise StimulusCaptureError("the capture could not be placed") from exc
        self._pending.append(answer)
        return answer.wav_path

    def _mint_and_place(self, recording: Any, phase: str) -> WiredCaptureAnswer:
        answer = mint_wired_answer(
            recording, device=self.device,
            setup=self.setup_reference() if self.setup_reference else None,
        )
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
        answer = self.capture.take_answer()
        return await self.bank_answer(record, answer)

    async def bank_answer(self, record: Mapping[str, Any], answer: Any) -> str:
        metadata = await asyncio.to_thread(self.enrich, answer, record) if self.enrich else {}
        # Analysis owns pose/attempt identity. Engine facts name what actually played.
        payload = {**metadata, **record}
        for name in ("take_id", "position_deg", "position_axis", "vertical_deg", "prompt"):
            if name in metadata:
                payload[name] = metadata[name]
        if answer is not None:
            payload.update({
                **({"capture_integrity": answer.capture_integrity} if answer.capture_integrity else {}),
                **({"capture_device": answer.device} if answer.device else {}),
                **({"capture_setup": answer.setup} if answer.setup else {}),
                "wav_path": answer.wav_path, "wav_sha256": answer.wav_sha256,
                "wav_bytes": len(answer.wav),
            })
        record_id = await self.inner.bank(payload)
        if self.after_bank:
            await asyncio.to_thread(self.after_bank, payload, record_id)
        return record_id
