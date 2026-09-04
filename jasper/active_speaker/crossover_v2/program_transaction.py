# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play seam filled: one stimulus, played and RECORDED, stage OBSERVED.

:class:`~.playback_transaction.PlaybackTransaction` over
:func:`~..program_playback.play_program`. Every stage is a return value or
exception type, never a default: ``stage_reached`` feeds ``played``, which
gates BANKING. No rung below ``ready``: :data:`BELOW_READY_INCIDENTS` arms
report ``ready``. ``wav_path`` comes from the injected ``StimulusCapture``;
``play_program``'s result names the STIMULUS emitted, not what the room heard.
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
#: One of :data:`BELOW_READY_INCIDENTS`.
STIMULUS_LEVEL_NOT_READY = "session_level_not_ready"
#: Fresh re-admission refused the program before any audio (MS-4's gate).
STIMULUS_ADMISSION_REFUSED = "program_admission_refused"
#: The program was admitted and the lock taken, and ``play_program``'s OWN
#: family refused past that point. Kept apart from the mechanics family below
#: because the host classifies them differently: this one renders the
#: program-unplayable refusal, and folding the mechanics in would send a dead
#: aplay to the household as safety copy.
STIMULUS_PLAY_FAILED = "program_play_failed"
#: The program was admitted and the emission MECHANISM failed — an aplay death,
#: a vanished device, an I/O fault (``PlaybackError``/``OSError``). The host
#: answers this family ``internal_error`` (fix-and-retry), never safety copy.
STIMULUS_EMISSION_FAILED = "stimulus_emission_failed"
#: The host could not assemble a program at all, so ``play_program`` was never
#: called. One of :data:`BELOW_READY_INCIDENTS`.
STIMULUS_NOT_COMPOSED = "program_not_composed"
#: A bound capture half produced no evidence. Two stages carry it and they are
#: two different facts: at ``ready`` the recorder never rolled, so nothing
#: played; at ``restore`` the stimulus played and the recording could not be
#: finished or placed, and the record is banked with an empty ``wav_path``.
STIMULUS_NOT_CAPTURED = "stimulus_not_captured"
#: This host bound no capture half, so the stimulus played and nothing on this
#: box recorded it (a browser capture answers through
#: :class:`~.capture_source.CaptureAnswer` instead). NOT a failure: an empty
#: ``wav_path`` beside an empty ``incident`` is what made ``analyze``'s skip
#: unattributable.
STIMULUS_CAPTURE_NOT_BOUND = "capture_not_bound"

#: The incidents reported at ``ready`` where ``ready`` did not actually
#: complete. Every other ``ready`` outcome means ready genuinely completed and a
#: later stage did not. A fourth such arm has to join this set to pass its pin.
BELOW_READY_INCIDENTS = frozenset({
    STIMULUS_LEVEL_NOT_READY,
    STIMULUS_NOT_CAPTURED,
    STIMULUS_NOT_COMPOSED,
})


@dataclass(frozen=True)
class ProgramForStimulus:
    """What one stimulus plays, assembled by the host.

    ``seams`` is the keyword mapping ``play_program`` consumes, which is what
    ``composition.bind_program_playback_seams`` already returns.
    """

    program: Any
    seams: Mapping[str, Any]


#: Host-supplied: this stimulus, as a program plus the seams bound around it.
#: Takes the same five facts the seam's ``run`` takes. May be sync or async.
Compose = Callable[..., "ProgramForStimulus | Any"]


class StimulusCaptureError(RuntimeError):
    """A bound capture half could not mint evidence for one stimulus.

    The capture half's ONE error type, narrow on purpose: this adapter's
    ``except`` arms classify by the types ``play_program`` raises, so an
    ``OSError`` escaping a recorder would land in the ``play`` arm and report a
    play that in fact succeeded. A capture half wraps its own faults in this and
    re-raises whatever the play raised untouched.
    """


class StimulusCapture(Protocol):
    """Record what the room does while one stimulus plays."""

    async def around(
        self, play: "Callable[[], Awaitable[None]]", *, program: Any,
    ) -> str:
        """Roll, run ``play()``, stop, and return the capture's path.

        The recorder is armed BEFORE ``play()`` and stopped after it: that
        ordering is the pre-roll guarantee, and it is why this is one act rather
        than two seams. ``program`` is handed over so the half can size its own
        budget from the schedule (``total_samples`` over ``sample_rate_hz``).

        Returns the **bundle-relative** path of what was heard. Raises
        :class:`StimulusCaptureError` when it cannot mint one, and lets
        ``play()``'s own exception through unchanged.
        """
        raise NotImplementedError


class ProgramPlaybackTransaction:
    """``PlaybackTransaction`` over ``play_program``, one stimulus per call.

    ``session_volume_plan`` is the plan ``play_program`` asserts against. It is
    NOT this transaction's to open or close — the session's volume claim owns
    the level (ADR-0231 §4).

    ``capture`` is ``None`` on a host that does not record what it plays: the
    Pi's wired microphone binds one, and a browser capture binds none.
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

        Never raises for a measurement problem — a transaction that raised would
        strand the session and lose the walk. Every failure is a stage plus a
        reason code, which is what a record can carry.
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
            # `assert_ready()` refused, so `ready` never completed and the
            # ladder has no rung that says so.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_LEVEL_NOT_READY,
            )
        except ProgramPlaybackRefused:
            # Refused BEFORE any audio, so `admit` did not complete.
            return PlaybackOutcome(
                stage_reached=STAGE_READY, incident=STIMULUS_ADMISSION_REFUSED,
            )
        except StimulusCaptureError:
            # `played` is the whole discriminator, and it is OBSERVED: the
            # capture half arms before the stimulus and stops after it, so its
            # faults sit on both sides of a play that may or may not have run.
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
            # Same stage, DIFFERENT family: the emission mechanism died rather
            # than the program being refused. Named types rather than a bare
            # ``RuntimeError``, so a mis-bound seam still surfaces as one.
            return PlaybackOutcome(
                stage_reached=STAGE_LOCK, incident=STIMULUS_EMISSION_FAILED,
            )

        if wav_path:
            return PlaybackOutcome(
                stage_reached=STAGE_RESTORE, wav_path=wav_path,
            )
        # Played, restored, and no bytes to point a reader at. Which of the two
        # reasons it is turns on whether anything was ever going to record.
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
