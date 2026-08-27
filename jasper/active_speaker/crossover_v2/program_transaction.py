# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play seam filled: one stimulus, played, with the stage OBSERVED.

:class:`~.playback_transaction.PlaybackTransaction` is the fifth and last seam
implementation. It wraps
:func:`~jasper.active_speaker.program_playback.play_program`, whose fail-closed
order of operations maps almost one-for-one onto the stage vocabulary — which
is the whole reason this adapter can report a stage it watched rather than one
it assumed.

**Why observation is the point.** ``stage_reached`` feeds
:attr:`~.playback_transaction.PlaybackOutcome.played`, and ``played`` is what
:meth:`~.session.TuningSession.measure` gates BANKING on. A transaction that
inferred its stage from "nothing raised" would make the engine's one honesty
gate a guess, and a stimulus that never reached the speaker would bank a record
saying it did. So every stage below is decided by something this adapter saw:
a return value or an exception type, never a default.

**The observation map**, and the one rung that cannot be expressed:

===========  ====================================================================
``ready``    ``play_program``'s ``assert_ready()`` returned — the fixed
             measurement volume is open, confirmed and inside its wall-clock
             ceiling. Microphone placement is the CALLER's precondition (MS-17):
             the engine holds no mover, and the front end's position gate
             satisfies it before ``measure`` drives the stimulus.
``admit``    ``readmit()`` allowed the program. A refusal raises
             :class:`~jasper.active_speaker.program_playback.ProgramPlaybackRefused`
             **before any audio**, so admit is decided by the exception type.
``lock``     the writer lock was entered. Not separately reported, and it does
             not need to be: a failure raised from inside it is a failure that
             got past it, so ``lock`` completed exactly when the play failure
             is the one that surfaces.
``play``     a :class:`~jasper.active_speaker.program_playback.ProgramPlaybackResult`
             came back. Its ``playback`` is documented as a *"successful, fully
             reaped WAV emission"*, so its existence IS the observation.
``restore``  ``play_program`` returned, which means the writer lock released.
             This transaction changes nothing else: the measurement graph is
             session-scoped and installed once by ``open()``, so the lock is the
             whole of what it has to put back.
===========  ====================================================================

**DISCLOSED GAP — the ladder has no rung below ``ready``.** A box whose
measurement volume is not ready never completed ``ready``, and
:data:`~.playback_transaction.PLAYBACK_STAGES` has nothing lower to say so.
That case is reported as ``ready`` with the incident
:data:`STIMULUS_LEVEL_NOT_READY`, which is the one place the stage alone
overstates what happened — named here rather than left for a reader to
discover. The incident is the load-bearing field there, and ``played`` is
``False`` either way, so nothing banks on the overstatement.

**This adapter mints no ``wav_path``, and that is not an omission.** Playing
and capturing are two seams: what the microphone heard arrives through
:class:`~.capture_source.CaptureAnswer`, and ``play_program``'s own result
carries the path of the STIMULUS it emitted, never a capture. Filling
``PlaybackOutcome.wav_path`` from it would point offline analysis at the sweep
the speaker played instead of the sound the room made. It stays ``""`` and the
analyze walker's ``no_capture_bytes`` skip stands as an honest report of the
gap.

**Composition is the host's.** What to play for one stimulus — the program and
the seams bound around it — is assembled by the injected ``compose`` callable,
because it needs a CamillaDSP controller, a bundle, a config dir, a topology, a
safety profile and role targets, none of which is engine vocabulary. What this
module owns is the arity, the stage observation, and the incident mapping.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.audio_measurement.playback import PlaybackError

from ..program_playback import (
    ProgramPlaybackError,
    ProgramPlaybackRefused,
    play_program,
)
from ..session_volume_plan import SessionVolumePlanError
from .playback_transaction import (
    STAGE_LOCK,
    STAGE_READY,
    STAGE_RESTORE,
    PlaybackOutcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .measure_spec import MeasureSpec

__all__ = [
    "STIMULUS_ADMISSION_REFUSED",
    "STIMULUS_LEVEL_NOT_READY",
    "STIMULUS_NOT_COMPOSED",
    "STIMULUS_PLAY_FAILED",
    "ProgramForStimulus",
    "ProgramPlaybackTransaction",
]

#: The measurement volume was not open/confirmed/fresh, so nothing was played.
#: The incident that carries the disclosed gap above — ``ready`` is reported
#: because the ladder has no lower rung, and this says what really happened.
STIMULUS_LEVEL_NOT_READY = "session_level_not_ready"
#: Fresh re-admission refused the program before any audio (MS-4's gate).
STIMULUS_ADMISSION_REFUSED = "program_admission_refused"
#: The program was admitted and the lock taken, and the emission failed.
STIMULUS_PLAY_FAILED = "program_play_failed"
#: The host could not assemble a program for this stimulus at all.
STIMULUS_NOT_COMPOSED = "program_not_composed"


@dataclass(frozen=True)
class ProgramForStimulus:
    """What one stimulus plays, assembled by the host.

    ``seams`` is the keyword mapping ``play_program`` consumes —
    ``readmit`` / ``play_wav`` / ``writer_lock`` — which is exactly what
    ``crossover_v2_flow.bind_program_playback_seams`` already returns, so the
    host binds the shipped builder rather than a shape invented here.
    """

    program: Any
    seams: Mapping[str, Any]


#: Host-supplied: this stimulus, as a program plus the seams bound around it.
#: Takes the same five facts the seam's ``run`` takes, so a host never has to
#: reconstruct what the engine already said. May be sync or async.
Compose = Callable[..., "ProgramForStimulus | Any"]


class ProgramPlaybackTransaction:
    """``PlaybackTransaction`` over ``play_program``, one stimulus per call.

    ``session_volume_plan`` is the plan ``play_program`` asserts against. It is
    NOT this transaction's to open or close — the session's volume claim owns
    the level, and MS-14's proof is taken through that claim before ``run`` is
    called. One prover, one door.
    """

    def __init__(self, *, compose: Compose, session_volume_plan: Any) -> None:
        self._compose = compose
        self._session_volume_plan = session_volume_plan

    async def run(
        self,
        *,
        spec: "MeasureSpec",
        position_deg: int | None,
        prompt: str,
        level_db: float,
        stimulus_dbfs: float | None,
    ) -> PlaybackOutcome:
        """Play one stimulus and report the last stage that COMPLETED.

        Never raises for a measurement problem — a transaction that raised
        would strand the session and lose the walk. Every failure below is a
        stage plus a reason code, which is what a record can carry.
        """
        try:
            prepared = await _resolve(self._compose(
                spec=spec,
                position_deg=position_deg,
                prompt=prompt,
                level_db=level_db,
                stimulus_dbfs=stimulus_dbfs,
            ))
        except (OSError, ValueError):
            # The two ways composing genuinely fails on a working wiring: the
            # rendered stimulus could not be written, or the parameters do not
            # describe a program. Anything else is a mis-bound host, which the
            # seam keeps raising for.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_NOT_COMPOSED,
            )

        try:
            await play_program(
                prepared.program,
                session_volume_plan=self._session_volume_plan,
                **dict(prepared.seams),
            )
        except SessionVolumePlanError:
            # The disclosed gap: `ready` never completed, and the ladder has
            # no rung that says so. The incident is what carries the truth.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_LEVEL_NOT_READY,
            )
        except ProgramPlaybackRefused:
            # Refused BEFORE any audio, so `admit` did not complete.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_ADMISSION_REFUSED,
            )
        except (ProgramPlaybackError, PlaybackError, OSError):
            # Past admission and inside the writer lock, so `lock` completed
            # and `play` did not. Named types rather than a bare
            # ``RuntimeError``: the seam keeps raising for a PROGRAMMING error,
            # so a mis-bound seam must still surface as one.
            return PlaybackOutcome(
                stage_reached=STAGE_LOCK, incident=STIMULUS_PLAY_FAILED,
            )

        return PlaybackOutcome(stage_reached=STAGE_RESTORE)


async def _resolve(value: Any) -> ProgramForStimulus:
    """Allow a host to compose synchronously or not, without two seams."""
    if inspect.isawaitable(value):
        return await value
    return value
