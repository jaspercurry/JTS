# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play seam filled: every stage is OBSERVED, and one is disclosed.

``stage_reached`` feeds ``PlaybackOutcome.played``, which is what ``measure``
gates banking on — so the property that matters is not "the adapter returns a
stage" but "the stage it returns is the one that actually completed". Each pin
below drives the real adapter over a real ``play_program`` with one step of the
fail-closed order made to fail, and asserts the rung that step sits on.

The pins that are NOT about a stage are about the evidence: the ``wav_path``
comes from the CAPTURE half that recorded across the stimulus, never from
``play_program``'s own result (which names the sweep the speaker emitted), and
a stimulus that played without leaving evidence says WHY rather than handing
back a bare ``""``.
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
    STIMULUS_CAPTURE_NOT_BOUND,
    STIMULUS_LEVEL_NOT_READY,
    STIMULUS_NOT_CAPTURED,
    STIMULUS_NOT_COMPOSED,
    STIMULUS_PLAY_FAILED,
    ProgramForStimulus,
    ProgramPlaybackTransaction,
    StimulusCaptureError,
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
    sample_rate_hz = 48_000
    total_samples = 48_000


CAPTURE_RELPATH = "summed/summed_measure_deadbeef.wav"


class _Capture:
    """The host's recording half: rolls across the play, or fails on cue.

    Records the ORDER it saw, because the ordering IS the contract — a
    recorder armed after the first sample has already lost the answer.
    """

    def __init__(
        self,
        *,
        start_raises: bool = False,
        place_raises: bool = False,
        relpath: str = CAPTURE_RELPATH,
    ) -> None:
        self.start_raises = start_raises
        self.place_raises = place_raises
        self.relpath = relpath
        self.log: list[str] = []
        self.programs: list[Any] = []

    async def around(
        self, play: Any, *, program: Any,
    ) -> str:
        self.programs.append(program)
        if self.start_raises:
            raise StimulusCaptureError("the recorder never rolled")
        self.log.append("rolled")
        await play()
        self.log.append("stopped")
        if self.place_raises:
            raise StimulusCaptureError("the capture could not be placed")
        return self.relpath


def _transaction(
    plan: _Plan | None = None,
    seams: _Seams | None = None,
    *,
    compose_raises: bool = False,
    capture: _Capture | None = None,
) -> ProgramPlaybackTransaction:
    bound = seams or _Seams()

    def _compose(**_kwargs: Any) -> ProgramForStimulus:
        if compose_raises:
            raise OSError("the rendered stimulus could not be written")
        return ProgramForStimulus(program=_Program(), seams=bound.as_kwargs())

    return ProgramPlaybackTransaction(
        compose=_compose, session_volume_plan=plan or _Plan(), capture=capture,
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

    outcome = await _run(_transaction(seams=seams, capture=_Capture()))

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


async def test_the_wav_path_is_the_capture_halfs_and_never_the_stimulus():
    """``play_program``'s result names the sweep that was EMITTED.

    Reporting it as the capture would point offline analysis at the stimulus
    instead of at the sound the room made, and every downstream verdict would
    be about the wrong signal. So the path can only come from the half that
    recorded — and it comes back whole, because the bundle-relative name
    carries a ``uuid4`` no reader can re-derive.
    """
    capture = _Capture()

    outcome = await _run(_transaction(capture=capture))

    assert outcome.wav_path == CAPTURE_RELPATH
    assert outcome.incident == ""
    assert [p.program_id for p in capture.programs] == ["prog-1"], (
        "the capture half sizes its own budget from the program that plays"
    )


async def test_the_recorder_rolls_before_the_stimulus_and_stops_after_it():
    """The pre-roll guarantee, and the reason this is ONE transaction.

    A recording that started after the first sample has already lost the part
    of the answer the analysis needs most, so the ordering is asserted rather
    than assumed: rolled, played, stopped.
    """
    order: list[str] = []
    seams = _Seams()
    inner_play_wav = seams._play_wav

    async def _logged_play_wav() -> Any:
        order.append("played")
        return await inner_play_wav()

    seams._play_wav = _logged_play_wav  # type: ignore[method-assign]

    class _Ordered(_Capture):
        async def around(self, play: Any, *, program: Any) -> str:
            order.append("rolled")
            await play()
            order.append("stopped")
            return self.relpath

    await _run(_transaction(seams=seams, capture=_Ordered()))

    assert order == ["rolled", "played", "stopped"]
    assert seams.played == 1


async def test_a_played_stimulus_with_no_evidence_never_returns_a_silent_path():
    """The defect this whole seam exists to remove.

    An empty ``wav_path`` beside an empty ``incident`` tells a reader nothing:
    ``analyze`` can only answer it with a generic "no bytes", and the two real
    causes — nothing was ever going to record here, versus a recording that was
    lost — send an operator to two different places. So EVERY played-and-
    restored outcome either carries a path or names which one it was.

    Mutation: collapse the adapter's tail to a bare
    ``PlaybackOutcome(stage_reached=STAGE_RESTORE)`` and this pin alone reds.
    """
    outcome = await _run(_transaction())

    assert outcome.played is True
    assert outcome.wav_path == ""
    assert outcome.incident == STIMULUS_CAPTURE_NOT_BOUND


async def test_a_bound_half_that_hands_back_no_path_is_not_the_unbound_case():
    """Two causes, two codes, and a broken half is not an absent one.

    A host that bound a recorder and got nothing back has a fault to chase; a
    host that bound none never had a microphone here. Collapsing them would
    make ``capture_not_bound`` appear on a wired session, which is the one
    reading that sends the operator to the wrong box.
    """
    outcome = await _run(_transaction(capture=_Capture(relpath="")))

    assert outcome.played is True
    assert outcome.wav_path == ""
    assert outcome.incident == STIMULUS_NOT_CAPTURED


async def test_a_recorder_that_never_rolled_means_the_stimulus_never_played():
    """The capture half arms BEFORE the play, so its early fault is below-ready.

    ``played`` is False, nothing banks, and the seam's own ``play_wav`` was
    never reached — which is what makes the ``ready`` report an overstatement
    the incident has to carry.
    """
    seams = _Seams()

    outcome = await _run(
        _transaction(seams=seams, capture=_Capture(start_raises=True))
    )

    assert outcome.stage_reached == STAGE_READY
    assert outcome.played is False
    assert outcome.incident == STIMULUS_NOT_CAPTURED
    assert seams.played == 0 and seams.locked == 0


async def test_a_capture_lost_after_the_stimulus_still_reports_it_played():
    """The same code on the other side of the play, told apart by the STAGE.

    The room really did hear the sweep, so a record IS banked for it — with an
    empty path and this reason on it. Reporting ``ready`` here would deny a
    stimulus the household stood through.
    """
    seams = _Seams()

    outcome = await _run(
        _transaction(seams=seams, capture=_Capture(place_raises=True))
    )

    assert outcome.stage_reached == STAGE_RESTORE
    assert outcome.played is True
    assert outcome.incident == STIMULUS_NOT_CAPTURED
    assert seams.played == 1, "the stimulus reached the speaker"


async def test_a_play_failure_under_a_bound_capture_is_still_a_play_failure():
    """The capture half lets the play's own exception through unchanged.

    A half that re-wrapped it would report a lost recording where a refused
    admission happened, and the adapter's whole classification would move to
    the wrong module.
    """
    seams = _Seams(admitted=False)
    capture = _Capture()

    outcome = await _run(_transaction(seams=seams, capture=capture))

    assert outcome.stage_reached == STAGE_READY
    assert outcome.incident == STIMULUS_ADMISSION_REFUSED
    assert capture.log == ["rolled"], "the recorder rolled and the play refused"


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


async def test_an_async_compose_is_awaited_rather_than_passed_through():
    """The shape PRODUCTION uses, which the pins above did not drive.

    The host's compose renders a WAV and fingerprints it, so it is `async def`
    and hands the work to a thread — awaiting it on the correction loop is the
    whole reason. Every other pin here binds a SYNC compose, so without this
    one the awaitable branch of `_resolve` ships unexercised and a transaction
    that forgot to await would hand `play_program` a coroutine object.
    """
    seams = _Seams()

    async def _compose(**_kwargs: Any) -> ProgramForStimulus:
        return ProgramForStimulus(program=_Program(), seams=seams.as_kwargs())

    transaction = ProgramPlaybackTransaction(
        compose=_compose, session_volume_plan=_Plan(),
    )

    outcome = await _run(transaction)

    assert outcome.stage_reached == STAGE_RESTORE
    assert outcome.played is True
    assert seams.played == 1, "the awaited program never reached the speaker"
