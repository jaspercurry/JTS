# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Program-playback entry for the crossover session (Wave 2, deliverable D).

:func:`play_program` is the one entry that plays a compiled excitation program
(CHECK / MEASURE) through the speaker's real DSP chain. It composes two things:

* the session-scoped fixed measurement volume
  (:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan`) — it
  ACQUIRES the volume assertion, it never opens or closes the session (one
  session spans every phase; the flow owns open/close);
* program admission
  (:func:`jasper.active_speaker.program_admission.readmit_program_from_wav`) —
  re-admitted from a fresh WAV byte readback right before playback, exactly as
  ``play_admitted_wav`` re-admits before an isolated driver sweep.

**The graph is no longer this function's business.**
:class:`jasper.active_speaker.crossover_v2.session_graph.MeasurementSessionGraph`
installs it once per session and proves it before each stimulus, so the
load/restore pair this used to bracket every capture with — and the two ducks
and five-plus CamillaDSP round-trips it cost — are gone. What stays here is the
writer lock: a stimulus still must not have the graph swapped out from under it
mid-capture, and holding the lock across the play is what prevents that.

Playback itself rides the existing verified-aplay path
(:func:`verified_program_aplay` → ``play_verified_wav``) to ``correction_substream``.
The play seam and the writer lock are injected callables so the orchestration is
exercised end-to-end with a fake aplay/DSP boundary.

VERIFY needs no machinery here: it plays a mono summed sweep through the APPLIED
production graph — the real system, not a commissioning construct — so it reuses
the existing summed-sweep playback, NOT this program graph.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# The ALSA lane a program WAV is played into — the correction fan-in substream
# that feeds CamillaDSP's capture, same as the isolated driver sweep
# (``jasper.active_speaker.web_commissioning.COMMISSION_TONE_ALSA_DEVICE``).
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

# Injected seams, bound to the real CamillaController by
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

    Wraps the existing content-bound path — ``verified_wav_source`` snapshots and
    sha256-verifies the exact program WAV bytes, ``play_verified_wav`` re-verifies
    and emits them through a stable fd. Wave 5 binds this as ``play_program``'s
    ``play_wav`` seam; tests inject a fake so no aplay is spawned.

    ``alsa_device=None`` (the production shape) resolves the correction
    lane's CURRENT transport per call via ``correction_play_device()`` — a
    def-time constant default would freeze the device at import and ignore
    an arm (P6c-ii). Passing a device explicitly remains the test/override
    seam.
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

    Order of operations (all fail-closed):

    1. ``session_volume_plan.assert_ready()`` — the session volume assertion
       (raises if the fixed measurement volume is not open, confirmed, and within
       its wall-clock ceiling).
    2. ``readmit()`` — fresh re-admission from the rendered WAV bytes; a refused
       program raises :class:`ProgramPlaybackRefused` before any audio.
    3. Under ``writer_lock``: play the admitted WAV via ``play_wav`` (the
       verified-aplay path).

    The lock is held across the play rather than around a swap: the session
    graph is installed and proven before the caller reaches here, and what the
    lock still buys is that no other DSP writer can replace it mid-capture.

    Emits ``event=active_speaker.program_playback`` start/end markers carrying the
    ``program_id``. The play seam and writer lock are injected so this
    orchestration is testable with a fake aplay/DSP boundary.
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
