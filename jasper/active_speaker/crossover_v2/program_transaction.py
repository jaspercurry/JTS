# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play seam filled: one stimulus, played and RECORDED, the stage OBSERVED.

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

**DISCLOSED GAP — the ladder has no rung below ``ready``, and three arms need
one.** :data:`~.playback_transaction.PLAYBACK_STAGES` starts at ``ready``, so a
failure that lands before ``ready`` completed has no stage that says so. Every
such arm reports ``ready``, and every one of them overstates what happened
(the count is :data:`BELOW_READY_INCIDENTS`, which a pin walks — this sentence
is a summary of that set and never its authority):

* :data:`STIMULUS_LEVEL_NOT_READY` — the measurement volume was not open,
  confirmed and fresh, so ``assert_ready()`` refused and ``ready`` never
  completed.
* :data:`STIMULUS_NOT_COMPOSED` — the host could not assemble a program, so
  ``play_program`` was never called and ``ready`` was never even attempted.
* :data:`STIMULUS_NOT_CAPTURED`, **when the recorder never rolled** — the
  capture half is armed BEFORE the stimulus, so a microphone that will not
  start means ``play_program`` was never called either. The same code at
  ``restore`` means the opposite half of the story: the stimulus played and the
  evidence was lost after it, which is why this one incident spans two stages
  and the stage is what tells them apart.

The incident is the load-bearing field in all three, and ``played`` is ``False``
either way, so nothing banks on any overstatement. Named here as a set rather
than left for a reader to find the last one: an honesty map that undercounts
its own gaps is the defect this adapter exists to refuse.

**The ``wav_path`` comes from a CAPTURE half, never from ``play_program``.**
That call's own result carries the path of the STIMULUS it emitted, and
reporting it as the capture would point offline analysis at the sweep the
speaker played instead of at the sound the room made. So the evidence is minted
by an injected :class:`StimulusCapture`, which records ACROSS the play — the
recorder rolls before the first sample and stops after the last — and hands
back the bundle-relative path of what it heard. One stimulus, one transaction,
one answer.

**A host may bind none** — the Pi is not always the box holding the microphone
— and such a transaction still plays, still reports ``restore``, and says
:data:`STIMULUS_CAPTURE_NOT_BOUND` rather than returning a bare ``""``. An empty
path beside an empty incident is the silence this adapter exists to refuse.

**Composition is the host's.** What to play for one stimulus — the program and
the seams bound around it — is assembled by the injected ``compose`` callable,
because it needs a CamillaDSP controller, a bundle, a config dir, a topology, a
safety profile and role targets, none of which is engine vocabulary. What this
module owns is the arity, the stage observation, and the incident mapping.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol

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
    "BELOW_READY_INCIDENTS",
    "STIMULUS_ADMISSION_REFUSED",
    "STIMULUS_CAPTURE_NOT_BOUND",
    "STIMULUS_EMISSION_FAILED",
    "STIMULUS_LEVEL_NOT_READY",
    "STIMULUS_NOT_CAPTURED",
    "STIMULUS_NOT_COMPOSED",
    "STIMULUS_PLAY_FAILED",
    "ProgramForStimulus",
    "ProgramPlaybackTransaction",
    "StimulusCapture",
    "StimulusCaptureError",
]

#: The measurement volume was not open/confirmed/fresh, so nothing was played.
#: One of the below-``ready`` incidents :data:`BELOW_READY_INCIDENTS` enumerates:
#: ``ready`` is reported because the ladder has no lower rung, and this says
#: what really happened.
STIMULUS_LEVEL_NOT_READY = "session_level_not_ready"
#: Fresh re-admission refused the program before any audio (MS-4's gate).
STIMULUS_ADMISSION_REFUSED = "program_admission_refused"
#: The program was admitted and the lock taken, and ``play_program``'s OWN
#: family refused past that point — a :class:`ProgramPlaybackError` that is
#: not the admission refusal. The PROGRAM family, kept apart from the
#: mechanics family below because their classifications differ at the host:
#: this one renders the program-unplayable refusal today, and folding the
#: mechanics into it would send a dead aplay to the household as safety copy.
STIMULUS_PLAY_FAILED = "program_play_failed"
#: The program was admitted and the emission MECHANISM failed — an aplay
#: death, a vanished device, an I/O fault (``PlaybackError``/``OSError``,
#: neither a :class:`ProgramPlaybackError`). Today's classifier answers this
#: family ``internal_error`` (fix-and-retry), never the program-unplayable
#: safety copy, and the split is what lets a host keep that identity.
STIMULUS_EMISSION_FAILED = "stimulus_emission_failed"
#: The host could not assemble a program for this stimulus at all, so
#: ``play_program`` was never called. The disclosed gap's OTHER below-``ready``
#: incident — ``ready`` is reported for the same missing-rung reason.
STIMULUS_NOT_COMPOSED = "program_not_composed"
#: A bound capture half produced no evidence for this stimulus. Two stages
#: carry it and they are two different facts: at ``ready`` the recorder never
#: rolled, so nothing played; at ``restore`` the stimulus played and the
#: recording could not be finished or placed. The record is banked in the
#: second case with an empty ``wav_path`` — the room DID hear the sweep, and a
#: capture that was lost afterwards is still a fact about this session.
STIMULUS_NOT_CAPTURED = "stimulus_not_captured"
#: This host bound no capture half, so the stimulus played and nothing on this
#: box recorded it (a phone-relay session answers through
#: :class:`~.capture_source.CaptureAnswer` instead). NOT a failure, and the
#: reason it exists at all: an empty ``wav_path`` beside an empty ``incident``
#: is the silence that made ``analyze``'s skip unattributable.
STIMULUS_CAPTURE_NOT_BOUND = "capture_not_bound"

#: The disclosed gap, as DATA rather than as four paragraphs: the incidents
#: reported at ``ready`` where ``ready`` did not actually complete. Every other
#: ``ready`` outcome means ready genuinely completed and a later stage did not
#: — :data:`STIMULUS_ADMISSION_REFUSED` is the one such arm today.
#:
#: A set rather than prose because the prose already lost count once: the gap
#: paragraph called one of these *"the one place"* while the other was
#: overstating too. A fourth arm now has to join this set to pass its pin.
BELOW_READY_INCIDENTS = frozenset({
    STIMULUS_LEVEL_NOT_READY,
    STIMULUS_NOT_CAPTURED,
    STIMULUS_NOT_COMPOSED,
})


@dataclass(frozen=True)
class ProgramForStimulus:
    """What one stimulus plays, assembled by the host.

    ``seams`` is the keyword mapping ``play_program`` consumes —
    ``readmit`` / ``play_wav`` / ``writer_lock`` — which is exactly what
    ``crossover_v2.composition.bind_program_playback_seams`` already returns, so the
    host binds the shipped builder rather than a shape invented here.
    """

    program: Any
    seams: Mapping[str, Any]


#: Host-supplied: this stimulus, as a program plus the seams bound around it.
#: Takes the same five facts the seam's ``run`` takes, so a host never has to
#: reconstruct what the engine already said. May be sync or async.
Compose = Callable[..., "ProgramForStimulus | Any"]


class StimulusCaptureError(RuntimeError):
    """A bound capture half could not mint evidence for one stimulus.

    The capture half's ONE error type, and it is narrow on purpose: this
    adapter's ``except`` arms classify a play failure by the types
    ``play_program`` raises, and an ``OSError`` escaping a recorder or a WAV
    write would land in the ``play`` arm and report a play that in fact
    succeeded. A capture half therefore wraps its own faults in this before
    they cross the seam, and re-raises whatever the play raised untouched.
    """


class StimulusCapture(Protocol):
    """Record what the room does while one stimulus plays.

    **Host-supplied, for the same reason** :data:`Compose` **is**: what records
    on this box — an ALSA device, a bundle to write into, a mic identity — is
    not engine vocabulary. What the engine owns is the arity and the two rules
    below.
    """

    async def around(
        self, play: "Callable[[], Awaitable[None]]", *, program: Any,
    ) -> str:
        """Roll, run ``play()``, stop, and return the capture's path.

        **The recorder is armed BEFORE ``play()`` and stopped after it.** That
        ordering is the pre-roll guarantee and it is why this is one act rather
        than two seams: a recording that started after the first sample has
        already lost the part of the answer the analysis needs most.

        ``program`` is what will be played, handed over so the half can size
        its own budget from the schedule itself (``total_samples`` over
        ``sample_rate_hz``) rather than from a duration somebody declared
        beside it.

        Returns the **bundle-relative** path of what was heard. Raises
        :class:`StimulusCaptureError` when it cannot mint one, and lets
        ``play()``'s own exception through unchanged — the adapter classifies
        that one, and a capture half that re-wrapped it would report a lost
        recording where a refused admission happened.
        """
        raise NotImplementedError


class ProgramPlaybackTransaction:
    """``PlaybackTransaction`` over ``play_program``, one stimulus per call.

    ``session_volume_plan`` is the plan ``play_program`` asserts against. It is
    NOT this transaction's to open or close — the session's volume claim owns
    the level, and MS-14's proof is taken through that claim before ``run`` is
    called. One prover, one door.

    ``capture`` is the host's recording half, or ``None`` on a host that does
    not record what it plays. Optional rather than required because the two
    shipped capture sources differ exactly here: the Pi's wired microphone
    records across the stimulus and binds one, and a phone-relay session
    answers through its own conversation and binds none.
    """

    def __init__(
        self,
        *,
        compose: Compose,
        session_volume_plan: Any,
        capture: StimulusCapture | None = None,
    ) -> None:
        self._compose = compose
        self._session_volume_plan = session_volume_plan
        self._capture = capture

    async def run(
        self,
        *,
        spec: "MeasureSpec",
        position_deg: int | None,
        prompt: str,
        level_db: float,
        stimulus_dbfs: float | None,
    ) -> PlaybackOutcome:
        """Play one stimulus, record it, and report the last stage COMPLETED.

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
            # One of the disclosed gap's below-`ready` arms: `play_program`
            # was never called, so `ready` was never even attempted.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_NOT_COMPOSED,
            )

        played = False

        async def _play() -> None:
            nonlocal played
            await play_program(
                prepared.program,
                session_volume_plan=self._session_volume_plan,
                **dict(prepared.seams),
            )
            played = True

        wav_path = ""
        try:
            if self._capture is None:
                await _play()
            else:
                wav_path = await self._capture.around(
                    _play, program=prepared.program,
                )
        except SessionVolumePlanError:
            # The disclosed gap's other below-`ready` arm: `assert_ready()`
            # refused, so `ready` never completed and the ladder has no rung
            # that says so. The incident is what carries the truth.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_LEVEL_NOT_READY,
            )
        except ProgramPlaybackRefused:
            # Refused BEFORE any audio, so `admit` did not complete.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_ADMISSION_REFUSED,
            )
        except StimulusCaptureError:
            # `played` is the whole discriminator, and it is OBSERVED rather
            # than inferred from where the exception came from: the capture
            # half arms before the stimulus and stops after it, so its faults
            # sit on both sides of a play that may or may not have happened.
            if not played:
                return PlaybackOutcome(
                    stage_reached=STAGE_READY, incident=STIMULUS_NOT_CAPTURED,
                )
            return PlaybackOutcome(
                stage_reached=STAGE_RESTORE, incident=STIMULUS_NOT_CAPTURED,
            )
        except ProgramPlaybackError:
            # Past admission and inside the writer lock: `lock` completed and
            # `play` did not, and the refusal is `play_program`'s OWN family.
            return PlaybackOutcome(
                stage_reached=STAGE_LOCK, incident=STIMULUS_PLAY_FAILED,
            )
        except (PlaybackError, OSError):
            # Same stage, DIFFERENT family: the emission mechanism died (aplay,
            # device, I/O) rather than the program being refused. Two incidents
            # because the two classify differently at the host — see the codes'
            # own comments. Named types rather than a bare ``RuntimeError``:
            # the seam keeps raising for a PROGRAMMING error, so a mis-bound
            # seam must still surface as one.
            return PlaybackOutcome(
                stage_reached=STAGE_LOCK, incident=STIMULUS_EMISSION_FAILED,
            )

        if wav_path:
            return PlaybackOutcome(
                stage_reached=STAGE_RESTORE, wav_path=wav_path,
            )
        # Played, restored, and no bytes to point a reader at. Which of the two
        # reasons it is turns on whether anything was ever going to record —
        # never on a bare `""`, which is the silence the module refuses.
        return PlaybackOutcome(
            stage_reached=STAGE_RESTORE,
            incident=(
                STIMULUS_CAPTURE_NOT_BOUND if self._capture is None
                else STIMULUS_NOT_CAPTURED
            ),
        )


async def _resolve(value: Any) -> ProgramForStimulus:
    """Allow a host to compose synchronously or not, without two seams."""
    if inspect.isawaitable(value):
        return await value
    return value
