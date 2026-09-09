# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One wired recording placed in a bundle, and one annotated take record.

The capture kernel itself — the answer type, the minter, the recorder factory
and the capture budget — is the LEAF's
(:mod:`jasper.audio_measurement.wired_capture`), so the bass bench and the CLI
doors reach it without importing this package. What stays here is what needs
the engine: placing the raw bytes in a bundle's artifact registry, and the
play-seam capture half that drives the recorder around a program.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from jasper.active_speaker.restore_wait import await_restore_task_resilient
from jasper.audio_measurement.playback import PlaybackObservation
from .playback_transaction import PlaybackInterrupted

from jasper.active_speaker.bundles import (
    CAPTURE_KIND_SEQUENTIAL, capture_artifact_relpath, register_capture,
)
from jasper.audio_measurement.bundles import read_artifact_manifest
from jasper.audio_measurement.wired_capture import (
    WIRED_POST_ROLL_S, WIRED_PRE_PLAY_ALLOWANCE_S, WiredCaptureAnswer,
    WiredCaptureError, WiredMicDevice, make_wired_recorder, mint_wired_answer,
)

from .program_transaction import StimulusCaptureError


def place_wired_answer(
    bundle_dir: Path, answer: WiredCaptureAnswer, *, phase: str, group: str,
    stimulus_sha256: str = "",
) -> WiredCaptureAnswer:
    """Place and register raw bytes once; later analysis uses this exact path.

    ``stimulus_sha256`` is the rendered program WAV's own content hash,
    declared under the ``provenance.stimulus`` key every other capture writer
    uses, because that is what
    :func:`~.round_captures.discover_captures` binds a capture to its program
    by: the phase label is not the program (#3504), and a ladder step's rung
    differs from its neighbour's only by drive.
    """
    if answer.wav_path:
        return answer
    relative = capture_artifact_relpath("summed", group, None)
    path = bundle_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(answer.wav)
    entry = register_capture(
        bundle_dir, relative_path=relative,
        kind=CAPTURE_KIND_SEQUENTIAL if phase in ("check", "measure", "lateral") else "summed",
        payload={
            "speaker_group_id": group, "phase": phase,
            "measurement_status": "captured",
            **({"provenance": {
                "stimulus": {"phase": phase, "wav_sha256": stimulus_sha256},
            }} if stimulus_sha256 else {}),
        },
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
    #: The rendered stimulus this capture is OF, handed over per take by the
    #: composer that rendered it. Empty until one is declared, and a capture
    #: placed without one declares no stimulus rather than guessing at it.
    _stimulus_sha256: list[str] = field(default_factory=list)

    def declare_stimulus(self, sha256: str) -> None:
        """The digest of the program about to play. One capture's handoff."""
        self._stimulus_sha256[:] = [sha256] if sha256 else []

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
        async def _finish() -> str:
            try:
                recording = await asyncio.to_thread(recorder.finish, tail_s=WIRED_POST_ROLL_S)
                answer = await asyncio.to_thread(self._mint_and_place, recording, program)
            except (WiredCaptureError, OSError, ValueError) as exc:
                raise StimulusCaptureError("the capture could not be placed") from exc
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

    def _mint_and_place(self, recording: Any, program: Any) -> WiredCaptureAnswer:
        answer = mint_wired_answer(
            recording, device=self.device,
            setup=self.setup_reference() if self.setup_reference else None,
        )
        phase = str(program.phase)
        return place_wired_answer(
            self.bundle_dir, answer, phase=phase, group=phase,
            stimulus_sha256=next(iter(self._stimulus_sha256), ""),
        )

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
