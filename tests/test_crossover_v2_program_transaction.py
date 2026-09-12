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
    PlaybackInterrupted,
    STAGE_LOCK,
    STAGE_READY,
    STAGE_RESTORE,
)
from jasper.active_speaker.crossover_v2.program_transaction import (
    STIMULUS_ADMISSION_REFUSED,
    STIMULUS_CAPTURE_NOT_BOUND,
    STIMULUS_EMISSION_FAILED,
    STIMULUS_LEVEL_NOT_READY,
    STIMULUS_NOT_CAPTURED,
    STIMULUS_NOT_COMPOSED,
    STIMULUS_PLAY_FAILED,
    ProgramForStimulus,
    ProgramPlaybackTransaction,
    StimulusCaptureError,
    StimulusCaptureStopped,
)
from jasper.active_speaker.crossover_v2.wired_stimulus import WiredStimulusCapture
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError, WiredSplCeilingExceeded, WiredSplMonitor,
)
from jasper.active_speaker.program_playback import ProgramPlaybackError
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
from jasper.audio_measurement.playback import (
    PlaybackError, PlaybackFailureCode, PlaybackCleanupState, PlaybackObservation,
    WavPlaybackCancelled, WavPlaybackCancelledBeforeSpawn,
)

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
    assert outcome.playback.emission == "completed"
    assert outcome.incident == ""
    assert seams.locked == 1 and seams.played == 1


async def test_a_refused_admission_stops_at_ready_and_never_plays():
    """MS-4's gate refuses BEFORE any audio, so ``admit`` did not complete."""
    seams = _Seams(admitted=False)

    outcome = await _run(_transaction(seams=seams))

    assert outcome.stage_reached == STAGE_READY
    assert outcome.played is False
    assert outcome.incident == STIMULUS_ADMISSION_REFUSED
    assert outcome.playback.emission == "not_started"
    assert seams.played == 0, "a refused program must not reach the speaker"
    assert seams.locked == 0


@pytest.mark.parametrize("code,emission", [
    (PlaybackFailureCode.START_FAILED, "not_started"),
    (PlaybackFailureCode.PROCESS_FAILED, "possible"),
    (PlaybackFailureCode.TIMEOUT, "possible"),
])
@pytest.mark.parametrize("cleanup", list(PlaybackCleanupState))
async def test_a_failed_emission_reports_lock_because_it_got_past_admission(code, emission, cleanup):
    seams = _Seams(play_raises=PlaybackError(
        "aplay died", code=code, wav_path=Path("/tmp/x.wav"),
        alsa_device="null", cleanup_state=cleanup, returncode=-9,
    ))
    outcome = await _run(_transaction(seams=seams))
    assert outcome.stage_reached == STAGE_LOCK
    assert outcome.played is False
    assert outcome.incident == STIMULUS_EMISSION_FAILED
    assert outcome.playback.as_dict() == {
        "emission": emission, "failure_code": code,
        "cleanup_state": cleanup, "returncode": -9,
    }


@pytest.mark.parametrize("error,emission,cleanup", [
    (WavPlaybackCancelledBeforeSpawn(), "not_started", None),
    (WavPlaybackCancelled(PlaybackObservation(
        emission="possible", cleanup_state=PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED,
    )), "possible", PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED),
])
async def test_cancel_preserves_emission_and_child_cleanup(error, emission, cleanup):
    from jasper.active_speaker.crossover_v2.playback_transaction import PlaybackInterrupted
    with pytest.raises(PlaybackInterrupted) as stopped:
        await _run(_transaction(seams=_Seams(play_raises=error)))
    assert stopped.value.playback.emission == emission
    assert stopped.value.playback.cleanup_state == cleanup


@pytest.mark.parametrize("stop", ["cancel", "spl", "microphone"])
async def test_guarded_capture_drains_playback_before_returning(tmp_path, stop):
    tasks_before = set(asyncio.all_tasks())
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []

    class Recorder:
        failure = None

        def start(self):
            pass

        def abort(self):
            events.append("abort")

    recorder = Recorder()
    observation = PlaybackObservation(
        emission="possible", cleanup_state=PlaybackCleanupState.KILLED_AND_REAPED,
        returncode=-9,
    )

    async def play():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            events.append("playback_stopped")
            raise WavPlaybackCancelled(observation)

    capture = WiredStimulusCapture(
        device=None, bundle_dir=tmp_path, recorder_factory=lambda *_: recorder,
        spl_monitor=WiredSplMonitor(None, 80, 0),
    )
    task = asyncio.create_task(capture.around(play, program=_Program()))
    await asyncio.wait_for(entered.wait(), 1)
    if stop == "cancel":
        task.cancel()
    else:
        recorder.failure = (
            WiredSplCeilingExceeded(81, 80) if stop == "spl"
            else WiredCaptureError("microphone disconnected")
        )
    await asyncio.wait_for(cleaning.wait(), 1)
    if stop == "cancel":
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    assert events == []
    release.set()
    error = PlaybackInterrupted if stop == "cancel" else StimulusCaptureStopped
    with pytest.raises(error) as caught:
        await asyncio.wait_for(task, 1)
    assert caught.value.playback == observation
    if stop != "cancel":
        assert caught.value.code == (
            "spl_ceiling_exceeded" if stop == "spl" else "wired_capture_failed"
        )
    assert events == ["playback_stopped", "abort"]
    assert capture.take_answer() is None
    assert set(asyncio.all_tasks()) <= tasks_before


@pytest.mark.parametrize(
    ("raised", "expected_incident"),
    [
        (ProgramPlaybackError("reap failed"), STIMULUS_PLAY_FAILED),
        (
            PlaybackError(
                "aplay died",
                code=PlaybackFailureCode.PROCESS_FAILED,
                wav_path=Path("/tmp/x.wav"),
                alsa_device="null",
            ),
            STIMULUS_EMISSION_FAILED,
        ),
        (OSError("device vanished"), STIMULUS_EMISSION_FAILED),
    ],
)
async def test_the_program_family_and_the_emission_family_stay_apart(
    raised, expected_incident,
):
    """Two incidents because the two classify differently at the host.

    ``ProgramPlaybackError`` renders the program-unplayable refusal today;
    ``PlaybackError``/``OSError`` classify to internal_error (fix-and-retry).
    A transaction that folded them into one incident would let a dead aplay
    reach the household as safety copy — the failure-identity break the split
    exists to prevent. Mutation: merge the two arms and the PlaybackError and
    OSError rows red.
    """
    seams = _Seams(play_raises=raised)

    outcome = await _run(_transaction(seams=seams))

    assert outcome.stage_reached == STAGE_LOCK
    assert outcome.played is False
    assert outcome.incident == expected_incident


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
    assert outcome.playback.emission == "completed"
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
    assert outcome.playback.emission == "completed"
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
    assert outcome.playback.emission == "completed"
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
    assert outcome.playback.emission == "completed"
    assert seams.played == 1, "the awaited program never reached the speaker"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope, phase", [
    ("drivers", "measure"), ("candidate", "entry_baseline"),
    ("candidate", "cloud_verify"), ("candidate", "lateral"),
])
async def test_shared_composer_mints_each_take_and_proves_graph_inside_play_lock(tmp_path, monkeypatch, scope, phase):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from jasper.active_speaker import program_admission, program_playback
    from jasper.active_speaker.crossover_v2.composition import bind_program_composer
    from jasper.audio_measurement.program import build_verify_program
    from tests.test_active_speaker_program_admission import _measure_program
    import jasper.dsp_apply as dsp_apply

    graph = "devices:\n  samplerate: 48000\nfilters: {}\npipeline: []\n"
    live = SimpleNamespace(text=graph, locked=False)
    events = []
    paths = []

    class Cam:
        async def normalize_config_raw(self, text, **kwargs):
            assert live.locked
            events.append("prove")
            return text

        async def get_active_config_raw(self, **kwargs):
            return live.text

    class Store:
        bundle_dir = tmp_path

        def identify_artifact(self, relative):
            paths.append(relative)
            return SimpleNamespace(path=relative)

    @asynccontextmanager
    async def lock(*args, **kwargs):
        live.locked = True
        events.append("lock")
        try:
            yield
        finally:
            live.locked = False

    async def before_play(actual_spec, program, artifact, actual_phase):
        assert actual_spec.graph_scope == scope
        assert actual_phase == phase
        assert live.locked
        events.append("before_play")

    async def play(*args, **kwargs):
        assert live.locked
        events.append("play")
        return SimpleNamespace(returncode=0)

    def readmit(*args, **kwargs):
        assert "graph_yaml" not in kwargs
        assert scope == "drivers"
        return SimpleNamespace(allowed=True)

    def readmit_summed(*args, **kwargs):
        assert kwargs["graph_yaml"] == graph
        assert kwargs["bass_extension"] == {"low_boost_db": 4.0}
        assert scope != "drivers"
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(dsp_apply, "dsp_writer_lock", lock)
    monkeypatch.setattr(program_playback, "verified_program_aplay", play)
    monkeypatch.setattr(program_admission, "readmit_program_from_wav", readmit)
    monkeypatch.setattr(program_admission, "readmit_summed_program_from_wav", readmit_summed)
    compose = bind_program_composer(
        program_for_spec=lambda spec, level: (
            _measure_program(-20) if scope == "drivers"
            else build_verify_program(2000, sweep_s=0.2)
        ),
        store=Store(), capture_session_id="same-pose", cam_factory=Cam,
        config_dir=str(tmp_path), topology=None, safety_profile={}, role_targets={},
        before_play=before_play, graph_yaml=lambda: graph,
        bass_extension_for_spec=lambda spec: {"low_boost_db": 4.0},
    )
    spec = MeasureSpec(
        kind="baseline", graph_scope=scope, program_phase=phase,
        candidate_id="banked" if scope == "candidate" else "",
    )
    first = await compose(spec=spec, level_db=-20)
    second = await compose(spec=spec, level_db=-20)
    assert paths == [f"crossover_v2/same-pose/{phase}_{ordinal:02d}_program.wav" for ordinal in range(2)]
    assert len(list((tmp_path / "crossover_v2/same-pose").glob("*_program.wav"))) == 2
    assert all((tmp_path / relative).is_file() for relative in paths)
    await program_playback.play_program(first.program, session_volume_plan=_Plan(), **first.seams)
    assert events == ["lock", "prove", "before_play", "play"]
    live.text = "devices:\n  samplerate: 44100\nfilters: {}\npipeline: []\n"
    with pytest.raises(ProgramPlaybackError):
        await program_playback.play_program(second.program, session_volume_plan=_Plan(), **second.seams)
    assert events.count("play") == 1
