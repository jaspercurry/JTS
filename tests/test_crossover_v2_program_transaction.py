# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play seam filled: every stage is OBSERVED, and one is disclosed.

``stage_reached`` feeds ``PlaybackOutcome.played``, which is what ``measure``
gates banking on — so the property that matters is not "the adapter returns a
stage" but "the stage it returns is the one that actually completed". Each pin
below drives the real adapter over a real ``play_program`` with one step of the
fail-closed order made to fail, and asserts the rung that step sits on.

The one pin that is NOT about a stage is the last: no ``wav_path`` is minted
from ``play_program``'s result, because that result names the STIMULUS the
speaker emitted and not the sound the room made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_BASELINE
from jasper.active_speaker.crossover_v2.playback_transaction import (
    STAGE_LOCK,
    STAGE_READY,
    STAGE_RESTORE,
)
from jasper.active_speaker.crossover_v2.program_transaction import (
    STIMULUS_ADMISSION_REFUSED,
    STIMULUS_LEVEL_NOT_READY,
    STIMULUS_NOT_COMPOSED,
    STIMULUS_PLAY_FAILED,
    ProgramForStimulus,
    ProgramPlaybackTransaction,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
from jasper.audio_measurement.playback import PlaybackError, PlaybackFailureCode

LEVEL_DB = -20.0


class _Plan:
    """The session volume plan `play_program` asserts against."""

    def __init__(self, *, ready: bool = True) -> None:
        self._ready = ready
        self.measurement_volume_db = LEVEL_DB
        self.asserted = 0

    def assert_ready(self, now: float | None = None) -> None:
        self.asserted += 1
        if not self._ready:
            raise SessionVolumePlanError("no measurement volume is open")


class _Admission:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.refusals: tuple[Any, ...] = () if allowed else (_Reason(),)


class _Reason:
    value = "not_admitted"


class _Seams:
    """`play_program`'s three injected seams, each able to fail on cue."""

    def __init__(
        self,
        *,
        admitted: bool = True,
        play_raises: BaseException | None = None,
    ) -> None:
        self.admitted = admitted
        self.play_raises = play_raises
        self.locked = 0
        self.played = 0

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "readmit": self._readmit,
            "play_wav": self._play_wav,
            "writer_lock": self._writer_lock,
        }

    async def _readmit(self) -> _Admission:
        return _Admission(self.admitted)

    async def _play_wav(self) -> Any:
        self.played += 1
        if self.play_raises is not None:
            raise self.play_raises
        return object()

    def _writer_lock(self) -> Any:
        seams = self

        class _Lock:
            async def __aenter__(self) -> None:
                seams.locked += 1

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Lock()


class _Program:
    program_id = "prog-1"
    phase = "measure"


def _transaction(
    plan: _Plan | None = None,
    seams: _Seams | None = None,
    *,
    compose_raises: bool = False,
) -> ProgramPlaybackTransaction:
    bound = seams or _Seams()

    def _compose(**_kwargs: Any) -> ProgramForStimulus:
        if compose_raises:
            raise OSError("the rendered stimulus could not be written")
        return ProgramForStimulus(program=_Program(), seams=bound.as_kwargs())

    return ProgramPlaybackTransaction(
        compose=_compose, session_volume_plan=plan or _Plan(),
    )


async def _run(transaction: ProgramPlaybackTransaction) -> Any:
    return await transaction.run(
        spec=MeasureSpec(kind=MEASURE_KIND_BASELINE),
        position_deg=0,
        prompt="stand at the mark",
        level_db=LEVEL_DB,
        stimulus_dbfs=None,
    )


async def test_a_clean_stimulus_reports_restore_and_counts_as_played():
    seams = _Seams()

    outcome = await _run(_transaction(seams=seams))

    assert outcome.stage_reached == STAGE_RESTORE
    assert outcome.played is True
    assert outcome.incident == ""
    assert seams.locked == 1 and seams.played == 1


async def test_a_refused_admission_stops_at_ready_and_never_plays():
    """MS-4's gate refuses BEFORE any audio, so ``admit`` did not complete."""
    seams = _Seams(admitted=False)

    outcome = await _run(_transaction(seams=seams))

    assert outcome.stage_reached == STAGE_READY
    assert outcome.played is False
    assert outcome.incident == STIMULUS_ADMISSION_REFUSED
    assert seams.played == 0, "a refused program must not reach the speaker"
    assert seams.locked == 0


async def test_a_failed_emission_reports_lock_because_it_got_past_admission():
    """The rung a play failure sits on is ``lock``, not ``admit``.

    Reporting ``admit`` would say the writer lock was never taken, and a reader
    diagnosing a stuck DSP writer would look in the wrong place.
    """
    seams = _Seams(play_raises=PlaybackError(
        "aplay died",
        code=PlaybackFailureCode.PROCESS_FAILED,
        wav_path=Path("/tmp/x.wav"),
        alsa_device="null",
    ))

    outcome = await _run(_transaction(seams=seams))

    assert outcome.stage_reached == STAGE_LOCK
    assert outcome.played is False
    assert outcome.incident == STIMULUS_PLAY_FAILED
    assert seams.locked == 1, "the failure happened INSIDE the lock"


async def test_a_box_that_was_never_ready_says_so_in_the_incident():
    """The disclosed gap, arm one of two: no rung exists below ``ready``.

    The stage alone overstates what happened, so the incident is what carries
    the truth — and ``played`` is False either way, so nothing banks on it.
    The other arm is the compose failure pinned below.
    """
    seams = _Seams()

    outcome = await _run(_transaction(_Plan(ready=False), seams))

    assert outcome.stage_reached == STAGE_READY
    assert outcome.played is False
    assert outcome.incident == STIMULUS_LEVEL_NOT_READY
    assert seams.played == 0 and seams.locked == 0


async def test_a_host_that_cannot_compose_a_program_is_an_incident_not_a_raise():
    """A transaction that raised would strand the session and lose the walk.

    The disclosed gap's other below-``ready`` arm: ``play_program`` is never
    called here, so ``ready`` is not merely incomplete — it is never attempted,
    and ``ready`` is reported for the same missing-rung reason.
    """
    outcome = await _run(_transaction(compose_raises=True))

    assert outcome.stage_reached == STAGE_READY
    assert outcome.played is False
    assert outcome.incident == STIMULUS_NOT_COMPOSED


async def test_no_wav_path_is_minted_from_the_stimulus_the_speaker_emitted():
    """Playing and capturing are two seams.

    ``play_program``'s result names the sweep that was emitted. Reporting it as
    the capture would point offline analysis at the stimulus instead of at the
    sound the room made, and every downstream verdict would be about the wrong
    signal.
    """
    outcome = await _run(_transaction())

    assert outcome.wav_path == ""


async def test_the_level_is_asserted_once_per_stimulus_by_the_callee():
    """One prover, one door — this transaction adds no second level check."""
    plan = _Plan()

    await _run(_transaction(plan))
    await _run(_transaction(plan))

    assert plan.asserted == 2


@pytest.mark.parametrize("refusal", [True, False])
async def test_the_transaction_never_raises_for_a_measurement_problem(refusal: bool):
    """Anti-vacuity across the refusal shapes: every one returns an outcome."""
    seams = _Seams(admitted=not refusal)
    if not refusal:
        seams.play_raises = OSError("device vanished")

    outcome = await _run(_transaction(seams=seams))

    assert outcome.played is False
    assert outcome.incident != ""


async def test_a_programming_error_still_raises_rather_than_becoming_an_incident():
    """The seam's other half: an exception remains correct for a bug.

    A mis-bound seam is not a measurement problem, and swallowing it into a
    reason code would hide a wiring defect behind a household-shaped refusal.
    """
    seams = _Seams(play_raises=TypeError("play_wav() got an unexpected kwarg"))

    with pytest.raises(TypeError):
        await _run(_transaction(seams=seams))


async def test_a_cancel_reaches_the_caller_rather_than_becoming_an_incident():
    """The join's inheritance: `measure` is cancellable at every stimulus.

    A transaction that turned a cancellation into a reason code would let the
    walk continue past a cancel the caller asked for.
    """
    seams = _Seams(play_raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run(_transaction(seams=seams))


def test_every_below_ready_return_names_a_disclosed_incident():
    """The honesty map, defended by a pin instead of by prose.

    Prose cannot defend the map: the disclosed gap already lost count once —
    it called `session_level_not_ready` "the one place" while
    `STIMULUS_NOT_COMPOSED` was overstating too. A third arm that returns
    `ready` without ready having completed would be the same defect again, and
    a reader comparing four paragraphs would be the only thing standing
    between it and a record that looks correct.

    So: every `STAGE_READY` return in the adapter must pair the stage with one
    of the incidents the module DISCLOSES as below-ready. A new arm either
    names itself in that set or reds this.

    A source-text pin, under the exception `test_crossover_v2_verification`
    already records for its import-direction guard: a return that does not
    exist has no behaviour to observe, and the property is about the SET of
    returns rather than about any one call.
    """
    import ast
    from pathlib import Path

    from jasper.active_speaker.crossover_v2 import program_transaction as subject

    # Two categories, and every ready-return must fall in one:
    #   - the disclosed gap: ready did NOT complete (the module owns this set)
    #   - ready DID complete and a later stage did not (admission refusal)
    allowed = set(subject.BELOW_READY_INCIDENTS) | {
        subject.STIMULUS_ADMISSION_REFUSED,
    }
    tree = ast.parse(Path(subject.__file__).read_text(encoding="utf-8"))
    run = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )

    ready_returns: list[str] = []
    for node in ast.walk(run):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        kwargs = {kw.arg: kw.value for kw in node.value.keywords}
        stage = kwargs.get("stage_reached")
        if not isinstance(stage, ast.Name) or stage.id != "STAGE_READY":
            continue
        incident = kwargs.get("incident")
        ready_returns.append(
            incident.id if isinstance(incident, ast.Name) else "<none>"
        )

    assert ready_returns, "anti-vacuity: the adapter must still have ready arms"
    named = {getattr(subject, name, name) for name in ready_returns}
    assert named <= allowed, (
        "a STAGE_READY return names an incident no category claims: "
        f"{sorted(named - allowed)}. Either ready really completed and a later "
        "stage did not, or ready did not complete — in which case add it to "
        "BELOW_READY_INCIDENTS and name it in the disclosed-gap paragraph."
    )
