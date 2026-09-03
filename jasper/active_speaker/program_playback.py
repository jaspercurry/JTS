# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one entry that plays a compiled excitation program (CHECK / MEASURE).

:func:`play_program` acquires the session volume assertion but never opens or
closes the session — one session spans every phase and the flow owns
open/close. The graph is not this module's business:
``crossover_v2.session_graph.MeasurementSessionGraph`` installs and proves it
once per session; the writer lock held across the play is only what stops
another DSP writer replacing the graph mid-capture. VERIFY does not come
through here — it plays a summed sweep through the APPLIED production graph.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from jasper.audio_measurement.correction_lane import correction_play_device
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.playback import (
    PlaybackResult,
    play_verified_wav,
    verified_wav_source,
)
from jasper.audio_measurement.program import ExcitationProgram
from jasper.log_event import log_event

from .program_admission import ProgramAdmission

logger = logging.getLogger(__name__)

# Bound to the real CamillaController by
# ``crossover_v2.composition.bind_program_playback_seams``.
PlayWav = Callable[[], Awaitable[PlaybackResult]]
WriterLock = Callable[[], AbstractAsyncContextManager]
Readmit = Callable[[], Awaitable[ProgramAdmission]]


class ProgramPlaybackError(RuntimeError):
    """A program could not be played through the real DSP chain."""


class ProgramPlaybackRefused(ProgramPlaybackError):
    """Fresh re-admission refused the program before any audio."""

    def __init__(self, admission: ProgramAdmission) -> None:
        reasons = ",".join(reason.value for reason in admission.refusals)
        super().__init__(f"program re-admission refused: {reasons}")
        self.admission = admission


@dataclass(frozen=True)
class ProgramPlaybackResult:
    """A completed program emission and the fresh admission that authorized it."""

    playback: PlaybackResult
    admission: ProgramAdmission


async def verified_program_aplay(
    bundle_dir: str | Path,
    artifact: ArtifactIdentity,
    *,
    alsa_device: str | None = None,
    timeout_s: float,
) -> PlaybackResult:
    """The production ``play_wav`` seam: verified-aplay of the program WAV.

    ``alsa_device=None`` resolves the correction lane's CURRENT transport per
    call; a def-time default would freeze the device at import and ignore an arm.
    """
    if alsa_device is None:
        alsa_device = correction_play_device()
    async with verified_wav_source(bundle_dir, artifact) as source:
        return await play_verified_wav(
            source, alsa_device=alsa_device, timeout_s=timeout_s
        )


async def play_program(
    program: ExcitationProgram,
    *,
    session_volume_plan,
    readmit: Readmit,
    play_wav: PlayWav,
    writer_lock: WriterLock,
) -> ProgramPlaybackResult:
    """Play one CHECK/MEASURE program through the session's measurement graph.

    Fail-closed in order: the session volume assertion, then fresh re-admission
    from the rendered WAV bytes (a refusal raises before any audio), then the
    play under ``writer_lock``.
    """
    session_volume_plan.assert_ready()

    fresh = await readmit()
    if not fresh.allowed:
        log_event(
            logger,
            "active_speaker.program_playback",
            level=logging.WARNING,
            result="refused",
            program_id=program.program_id,
            phase=program.phase,
            refusals=",".join(reason.value for reason in fresh.refusals),
        )
        raise ProgramPlaybackRefused(fresh)

    async with writer_lock():
        log_event(
            logger,
            "active_speaker.program_playback",
            action="start",
            program_id=program.program_id,
            phase=program.phase,
            session_volume_db=f"{session_volume_plan.measurement_volume_db}",
        )
        playback = await play_wav()
        log_event(
            logger,
            "active_speaker.program_playback",
            action="end",
            result="completed",
            program_id=program.program_id,
            phase=program.phase,
        )
        return ProgramPlaybackResult(playback=playback, admission=fresh)
