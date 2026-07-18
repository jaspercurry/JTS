# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Program-playback entry for the crossover conductor (Wave 2, deliverable D).

:func:`play_program` is the one entry that plays a compiled excitation program
(CHECK / MEASURE) through the speaker's real DSP chain. It composes the other
three Wave 2 pieces:

* the session-scoped fixed measurement volume
  (:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan`) — it
  ACQUIRES the volume assertion, it never opens or closes the session (one
  session spans every phase; the flow owns open/close);
* the channel-routed program graph
  (:func:`jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config`) —
  loaded once, under the DSP writer lock, and the prior graph is restored after;
* program admission
  (:func:`jasper.active_speaker.program_admission.readmit_program_from_wav`) —
  re-admitted from a fresh WAV byte readback right before playback, exactly as
  ``play_admitted_wav`` re-admits before an isolated driver sweep.

Playback itself rides the existing verified-aplay path
(:func:`verified_program_aplay` → ``play_verified_wav``) to ``correction_substream``.
The CamillaDSP graph seams and the writer lock are injected callables so the
orchestration is exercised end-to-end with a fake aplay/DSP boundary; Wave 5
binds the real CamillaController-backed seams.

VERIFY needs no machinery here: it plays a mono summed sweep through the APPLIED
production graph — the real system, not a commissioning construct — so it reuses
the existing summed-sweep playback (Wave 5 wires it), NOT this program graph.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

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

# The ALSA lane a program WAV is played into — the correction fan-in substream
# that feeds CamillaDSP's capture, same as the isolated driver sweep
# (``jasper.active_speaker.web_commissioning.COMMISSION_TONE_ALSA_DEVICE``).
CORRECTION_SUBSTREAM = "correction_substream"

# Injected seams (Wave 5 binds CamillaController-backed implementations).
ReadCurrentConfigPath = Callable[[], Awaitable[str | None]]
LoadProgramGraph = Callable[[str], Awaitable[bool]]
RestoreGraph = Callable[[str], Awaitable[bool]]
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


class ProgramGraphRestoreError(ProgramPlaybackError):
    """Playback completed but the prior DSP graph could not be restored."""


@dataclass(frozen=True)
class ProgramPlaybackResult:
    """A completed program emission and the fresh admission that authorized it."""

    playback: PlaybackResult
    admission: ProgramAdmission
    entry_config_path: str


async def verified_program_aplay(
    bundle_dir: str | Path,
    artifact: ArtifactIdentity,
    *,
    alsa_device: str = CORRECTION_SUBSTREAM,
    timeout_s: float,
) -> PlaybackResult:
    """The production ``play_wav`` seam: verified-aplay of the program WAV.

    Wraps the existing content-bound path — ``verified_wav_source`` snapshots and
    sha256-verifies the exact program WAV bytes, ``play_verified_wav`` re-verifies
    and emits them through a stable fd. Wave 5 binds this as ``play_program``'s
    ``play_wav`` seam; tests inject a fake so no aplay is spawned.
    """
    async with verified_wav_source(bundle_dir, artifact) as source:
        return await play_verified_wav(
            source, alsa_device=alsa_device, timeout_s=timeout_s
        )


async def _safe_restore(
    restore_graph: RestoreGraph, entry_config_path: str, *, program_id: str
) -> tuple[bool, Exception | None]:
    """Restore the prior graph without ever raising into a finally block."""
    try:
        restored = await restore_graph(entry_config_path)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        log_event(
            logger,
            "active_speaker.program_playback",
            level=logging.CRITICAL,
            action="restore",
            result="failed",
            program_id=program_id,
            error=str(exc),
        )
        return False, exc
    if restored is not True:
        log_event(
            logger,
            "active_speaker.program_playback",
            level=logging.CRITICAL,
            action="restore",
            result="rejected",
            program_id=program_id,
        )
    return bool(restored is True), None


async def play_program(
    program: ExcitationProgram,
    *,
    program_graph_yaml: str,
    session_volume_plan,
    readmit: Readmit,
    read_current_config_path: ReadCurrentConfigPath,
    load_program_graph: LoadProgramGraph,
    restore_graph: RestoreGraph,
    play_wav: PlayWav,
    writer_lock: WriterLock,
) -> ProgramPlaybackResult:
    """Play one CHECK/MEASURE program through the real DSP chain, then restore.

    Order of operations (all fail-closed):

    1. ``session_volume_plan.assert_ready()`` — the session volume assertion
       (raises if the fixed measurement volume is not open, confirmed, and within
       its wall-clock ceiling).
    2. ``readmit()`` — fresh re-admission from the rendered WAV bytes; a refused
       program raises :class:`ProgramPlaybackRefused` before any audio.
    3. Under ``writer_lock``: read the current config path (the restore target),
       load the program graph, play the admitted WAV via ``play_wav`` (the
       verified-aplay path), then restore the prior graph — restore ALWAYS runs,
       including on playback failure.

    Emits ``event=active_speaker.program_playback`` start/end markers carrying the
    ``program_id``. The CamillaDSP seams and writer lock are injected so this
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
        entry_config_path = await read_current_config_path()
        if not entry_config_path:
            raise ProgramPlaybackError(
                "no current DSP config to restore after the program; refusing to "
                "load the program graph"
            )
        log_event(
            logger,
            "active_speaker.program_playback",
            action="start",
            program_id=program.program_id,
            phase=program.phase,
            session_volume_db=f"{session_volume_plan.measurement_volume_db}",
        )
        try:
            await load_program_graph(program_graph_yaml)
            playback = await play_wav()
        finally:
            restored, _restore_error = await _safe_restore(
                restore_graph, entry_config_path, program_id=program.program_id
            )
        # Reached only when load + play did not raise. A played program whose
        # prior graph did NOT come back is not "completed" — the speaker is in
        # the wrong graph, and the end marker must say so.
        log_event(
            logger,
            "active_speaker.program_playback",
            action="end",
            result="completed" if restored else "restore_failed",
            level=logging.INFO if restored else logging.CRITICAL,
            program_id=program.program_id,
            phase=program.phase,
            restored=restored,
        )
        if not restored:
            raise ProgramGraphRestoreError(
                "program played but the prior DSP graph could not be restored; "
                "reapply the speaker profile before playing audio"
            )
        return ProgramPlaybackResult(
            playback=playback,
            admission=fresh,
            entry_config_path=entry_config_path,
        )
