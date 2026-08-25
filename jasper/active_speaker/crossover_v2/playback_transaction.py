# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play transaction: a named boundary INSIDE ``measure``, not a fifth verb.

Ruling S1 settled the verb set at four and, in the same breath, kept the
engineering fact the rejected fifth verb was standing on: *"the playback
transaction — ready → admit → lock → play → restore, where every recorded
incident in this inventory happened — becomes a **named internal module inside
``measure``**, a first-class code boundary with its own contract, but **not** a
vocabulary item."* Pipeline mechanics are *"just the mechanics of how we
execute the verbs… the LLM doesn't really care about"* them.

So this module is the boundary and the contract. **Nothing is built behind it
yet** — the transaction's body is wave 2's (the VERIFY region lifts as
``measure`` + ``analyze``), its graph half is wave 6's and its volume half is
wave 5's. What exists here is the shape they land into, cut now so the waves
that fill it do not each cut their own.

**Playing and capturing are two seams, not one.** This module owns the
stimulus: getting the box ready, admitting the program, proving the level,
playing, and putting everything back. What the microphone heard arrives through
:class:`~.capture_source.CaptureAnswer`, which the capture provider owns and
which is unchanged by any of this. A transaction therefore returns no WAV.

**Where the mover fits, and why it is not an axis** (MS-17). ``ready`` is the
stage that carries the *"the microphone is at position P"* precondition, and it
is the ONLY place a front end differs: an arm reports the pose, a person
confirms it, a third mover does whatever it does. The engine asks the stage,
never the mover, so no code below this seam branches on who moved the mic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .measure_spec import MeasureSpec

__all__ = [
    "PLAYBACK_STAGES",
    "STAGE_ADMIT",
    "STAGE_LOCK",
    "STAGE_PLAY",
    "STAGE_READY",
    "STAGE_RESTORE",
    "PlaybackOutcome",
    "PlaybackTransaction",
]

#: The box is ready to be measured and the microphone is where the spec asked
#: for. The mover-agnostic precondition (MS-17) is satisfied at this stage and
#: nowhere else.
STAGE_READY = "ready"
#: The stimulus program is admitted onto the measurement path. MS-4 binds this
#: stage: a stimulus enters pre-DSP, never through the post-crossover active
#: ring, whose single-producer epoch takeover would *admit* a stray writer that
#: a raw ``hw`` device would refuse.
STAGE_ADMIT = "admit"
#: The declared level is held for the length of this stimulus. The PROOF that
#: the fader agrees is the session's, taken through its volume-claim slot before
#: this transaction is called — one prover, one door (MS-14). Ruling S10 fixes
#: the shape of a disagreement: it refuses to BANK the capture, never to play
#: the stimulus and never to try again, which is why an unproven level still
#: reaches this stage.
STAGE_LOCK = "lock"
#: The stimulus plays.
STAGE_PLAY = "play"
#: Whatever this transaction changed is put back, on every path out — including
#: the failing ones. The inventory's recorded incidents concentrate here.
STAGE_RESTORE = "restore"

#: In order. A transaction reports the LAST stage it reached, so a reader can
#: place a failure on this line rather than guess from a message.
PLAYBACK_STAGES = (
    STAGE_READY,
    STAGE_ADMIT,
    STAGE_LOCK,
    STAGE_PLAY,
    STAGE_RESTORE,
)


@dataclass(frozen=True)
class PlaybackOutcome:
    """What one stimulus did, said in facts a record can carry.

    ``stage_reached`` is a member of :data:`PLAYBACK_STAGES`; a clean run
    reaches :data:`STAGE_RESTORE`. ``incident`` is a reason code and is ``""``
    when there was none — never a sentence, because the household's copy is
    :mod:`.refusal_copy`'s job and a transaction that minted its own would be
    the second vocabulary this refactor exists to remove.

    Carries no level, and deliberately: the number that says what the fader
    actually read is the session's volume claim's to report, and a second copy
    of it here is the *"two fields of this block disagreeing"* shape that cost
    the campaign the 8.712 dB level bug.
    """

    stage_reached: str
    incident: str = ""

    def __post_init__(self) -> None:
        if self.stage_reached not in PLAYBACK_STAGES:
            raise ValueError(
                f"a playback stage must be one of {PLAYBACK_STAGES}, "
                f"got {self.stage_reached!r}"
            )

    @property
    def played(self) -> bool:
        """Did the stimulus actually play?

        The one question ``measure`` asks of an outcome before deciding whether
        there is evidence to bank.
        """
        return PLAYBACK_STAGES.index(self.stage_reached) >= PLAYBACK_STAGES.index(
            STAGE_PLAY
        )


@runtime_checkable
class PlaybackTransaction(Protocol):
    """Ready → admit → lock → play → restore, for ONE stimulus.

    Structural on purpose, in the register :class:`~.capture_source.CaptureAnswer`
    established: the engine names a contract, and whoever satisfies it is free to
    satisfy it their own way.

    ``spec`` says WHAT to play; :mod:`.programs` says what that composes to —
    it owns *what a session plays, how loud, and for which phase*, including
    the only level clamp on the summed sweep, so a transaction body composes
    THROUGH that module and never beside it. ``position_deg`` is the bearing
    the stimulus is played for, in :class:`~.spatial.PositionGeometry`'s frame,
    and ``None`` where the pose commands no bearing. ``level_db`` is the
    session's ONE declared level — what the program is admitted against, which
    is the thing MS-14's read-back is a proof about.

    **Never raises for a measurement problem.** A transaction that could not
    reach ``play`` reports the stage it stopped at and the reason code, because
    an exception here would strand the session and lose the restore. An
    exception remains correct for a programming error.
    """

    def run(
        self,
        *,
        spec: "MeasureSpec",
        position_deg: int | None,
        level_db: float,
    ) -> PlaybackOutcome: ...
