# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The executor: one stated plan, poses outer, takes inner, one held session.

Five things are pinned here:

1. the SHAPE of a walk -- M poses of N configs cost M placements and M x N
   takes, each measured through its own candidate's graph, with the speaker put
   back once;
2. the GATE -- the real :class:`PositionGate`, granting a pose batch once and
   carrying that grant across the configs at that pose;
3. the INTERRUPTION -- every take already banked stays banked, and the package
   names where the run stopped;
4. the LEVEL BOUND -- a run states a ceiling at or under this box's own
   commissioning stop, or it is refused;
5. the REFUSALS a run makes before anything plays -- a level policy nothing
   steps yet, and a stop pose the spec will not carry.

The specs a walk plays, the poses a request resolves to and the angle bounds are
``tests/test_angle_capture_seam.py``'s; nothing here re-asserts them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import plan_run
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    POSITION_AXIS_VERTICAL,
)
from jasper.active_speaker.crossover_v2.position_gate import (
    POSITION_HOLD_EXPIRED_CODE,
    PositionGate,
)
from tests.engine_twin import FakeGraph, FakeSeams, SeamFailure, open_session

#: What the CLI's own table calls a failure that ends a run. Stated here too,
#: because ``aborts`` is the CALLER's vocabulary and a test is a caller.
_ABORTS = {SeamFailure: "seam_failed"}

_SCOPES = {"fp-a": "candidate", "fp-b": "candidate"}


def _walk(angles: list[int], candidates: tuple[str, ...]) -> ac.AngleCaptureRequest:
    """One summed config per candidate at each angle, poses in the stated order."""
    return ac.AngleCaptureRequest(
        stops=tuple(
            ac.AngleStop(angle, ac.REGIME_SUMMED, candidate_id=candidate)
            for angle in angles
            for candidate in candidates
        ),
        template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE),
    )


@dataclass
class _StoppingGraph(FakeGraph):
    """A graph slot that goes away part-way through a walk.

    The seam a run cannot carry on past: the install IS the per-take health
    check, so a failing one means the speaker is no longer held the way the
    remaining takes would be measured.
    """

    stop_after: int = 0

    async def install(self, *args: object, **kwargs: object) -> str:
        if self.installs >= self.stop_after:
            raise SeamFailure("the measurement graph went away")
        return await super().install(*args, **kwargs)  # type: ignore[arg-type]


class AnsweredGate(PositionGate):
    """The REAL gate, with a driver that reports the microphone in place at once.

    A subclass rather than a second task polling :meth:`pending`: the hold it
    answers is the one the run is waiting on, in the same call, so a test needs
    neither a clock nor a busy loop to drive a walk. Every other rule -- the
    batch carry, the per-hold budget, the batch identity check -- is the
    shipped gate's, so a walk that publishes the wrong batch still fails here.

    :attr:`grants` records each hold that was actually OPENED, which is the
    count of times the microphone was asked to move.
    """

    def __init__(self) -> None:
        super().__init__()
        self.grants: list[tuple[int, int]] = []

    def gate(self, index: int, attempt: int, entry: object) -> None:
        try:
            super().gate(index, attempt, entry)
        except CaptureBeginDeferred:
            pending = self.published()["pending"]
            assert pending is not None
            held = (int(pending["index"]), int(pending["attempt"]))
            self.grants.append(held)
            self.release(*held)
            super().gate(index, attempt, entry)


async def _run_gated(request, *, seams=None, gate=None, aborts=_ABORTS):
    """The walk, under an open session, with every placement grant answered."""
    async with open_session(seams) as (session, fakes):
        return await plan_run.run_plan(
            request, session=session, gate=gate,
            candidate_scopes=_SCOPES, aborts=aborts,
        ), fakes


# --------------------------------------------------------------------------- #
# the shape of a walk
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("angles", "candidates"),
    [([0], ("fp-a",)), ([0, 20], ("fp-a", "fp-b")), ([0, -20, 20], ("fp-a",))],
    ids=["one-pose-one-config", "two-poses-two-configs", "three-poses"],
)
def test_a_walk_costs_one_placement_per_pose_and_one_take_per_stop(
    angles: list[int], candidates: tuple[str, ...],
) -> None:
    """The capability the executor exists for: the microphone moves once per POSE
    however many configs play there, every stop is measured, and the speaker is
    put back exactly once at the end."""
    request = _walk(angles, candidates)
    gate = AnsweredGate()

    result, fakes = asyncio.run(_run_gated(request, gate=gate))

    assert result.status == plan_run.RUN_MEASURED
    assert len(result.wall_s) == len(angles)
    assert result.mic_moves == len(angles)
    assert result.takes_measured == len(angles) * len(candidates)
    assert result.takes_skipped == 0
    assert len(fakes.banked) == len(angles) * len(candidates)
    # Each take measured through ITS candidate's graph, proven per take (one
    # install at open, one per take), and put back once.
    assert fakes.graph.scopes == [("candidate", cid) for _a in angles for cid in candidates]
    assert fakes.graph.installs == 1 + len(angles) * len(candidates)
    assert fakes.graph.restores == 1


def test_a_per_driver_stop_is_skipped_and_counted() -> None:
    """A per-driver stop plays the phase's own composed program rather than a
    spec, so this loop measures nothing for it and says so."""
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(0, ac.REGIME_PER_DRIVER),
        ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a"),
    ))

    result, fakes = asyncio.run(_run_gated(request, gate=AnsweredGate()))

    assert (result.takes_measured, result.takes_skipped) == (1, 1)
    assert result.stops_planned == 2
    assert result.mic_moves == 1
    assert len(fakes.banked) == 1


def test_an_ungated_run_asks_for_no_placement_grant() -> None:
    """``gate=None`` says the microphone is already where the plan asks: nothing
    waits and no grant is counted."""
    result, fakes = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b"))))

    assert (result.status, result.mic_moves) == (plan_run.RUN_MEASURED, 0)
    assert result.takes_measured == 2


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def test_a_pose_batch_is_granted_once_and_carried_across_its_configs() -> None:
    """Three configs at one pose is ONE hold, because the microphone moved once.

    Read off the GATE, not off the run's own count: the batch identity the run
    publishes has to be the one the gate grants a batch on, or the second config
    would open a second hold nobody is coming to release.
    """
    gate = AnsweredGate()

    result, _fakes = asyncio.run(
        _run_gated(_walk([0, 30], ("fp-a", "fp-b", "fp-a")), gate=gate),
    )

    assert result.takes_measured == 6
    # Two holds for six takes: configs 2 and 3 of each pose ride the first
    # config's release, which is the batch identity the run has to publish.
    assert gate.grants == [(1, 1), (4, 4)]


def test_a_stop_this_loop_skips_does_not_cost_a_second_placement_grant() -> None:
    """A per-driver stop between two summed ones is played by nobody here, and
    the microphone never left the pose — so the grant the first summed stop
    opened carries the second, as it does for any two configs at one place."""
    gate = AnsweredGate()
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a"),
        ac.AngleStop(0, ac.REGIME_PER_DRIVER),
        ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-b"),
    ))

    result, _fakes = asyncio.run(_run_gated(request, gate=gate))

    assert result.status == plan_run.RUN_MEASURED
    assert (result.takes_measured, result.takes_skipped) == (2, 1)
    assert gate.grants == [(1, 1)]
    assert result.mic_moves == 1


def test_a_grant_nobody_gives_ends_the_run_under_the_gate_s_own_word(
    monkeypatch,
) -> None:
    """The hold budget is the gate's, and its refusal is what the run reports —
    a second vocabulary for "nothing reported the microphone in place" would send
    an operator looking in the wrong place.

    The poll cadence is dropped to nothing so the wait is the CLOCK's, not the
    suite's; the two ticks are the whole loop, which the gate ends itself."""
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0.0)
    ticks = iter([0.0, 1e6])

    result, fakes = asyncio.run(
        _run_gated(
            _walk([0], ("fp-a",)),
            gate=PositionGate(clock=lambda: next(ticks)),
        ),
    )

    assert result.status == plan_run.RUN_INTERRUPTED
    assert result.reason == POSITION_HOLD_EXPIRED_CODE
    assert result.takes_measured == 0
    assert fakes.play.calls == []


# --------------------------------------------------------------------------- #
# interruption
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("banked_before_stop", [0, 1, 3])
def test_an_interrupted_run_keeps_what_it_banked_and_names_where_it_stopped(
    banked_before_stop: int,
) -> None:
    """The speaker stopped being held, so the rest would be guesswork — and the
    ids of what DID land are the only handle anybody has on those takes."""
    request = _walk([0, 20], ("fp-a", "fp-b"))
    seams = FakeSeams(graph=_StoppingGraph(stop_after=1 + banked_before_stop))

    result, fakes = asyncio.run(
        _run_gated(request, seams=seams, gate=AnsweredGate()),
    )

    assert result.status == plan_run.RUN_INTERRUPTED
    assert result.reason == _ABORTS[SeamFailure]
    assert result.takes_measured == banked_before_stop
    assert result.attempts == banked_before_stop + 1
    assert len(fakes.banked) == banked_before_stop
    assert result.stopped_at == {
        "pose_index": banked_before_stop // 2, "index": banked_before_stop + 1,
    }
    # The one give-back still happens: a session that dies holding its claim
    # leaves the speaker at a measurement level nobody chose.
    assert fakes.graph.restores == 1
    assert fakes.volume.releases == 1


# --------------------------------------------------------------------------- #
# refused before anything plays
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "walk",
    [
        {"level_mode": ac.LEVEL_ACQUIRE_AT_ANCHOR},
        {"level_mode": ac.LEVEL_SERIES, "main_volume_series_db": (-20.0, -14.0)},
    ],
    ids=["acquire-at-anchor", "series"],
)
def test_a_level_policy_nothing_steps_yet_is_refused_before_a_stimulus(
    walk: dict,
) -> None:
    """One session holds ONE level, so a walk asking for another between stops is
    refused up front rather than measured at a level it did not mean."""
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-a"),), **walk,
    )

    result, fakes = asyncio.run(_run_gated(request))

    assert result.status == plan_run.RUN_REFUSED
    assert result.reason == ac.WALK_POLICY_UNSUPPORTED_YET
    assert (result.takes_measured, result.wall_s) == (0, ())
    assert fakes.play.calls == []
    assert fakes.banked == []


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {
            "stops": (ac.AngleStop(20, ac.REGIME_SUMMED, candidate_id="fp-a"),),
            "template": ac.walk_template(
                kind=MEASURE_KIND_CANDIDATE, position_axis=POSITION_AXIS_VERTICAL,
            ),
        },
        # ``fp-z`` is in no scope map: the stop names a candidate the caller
        # resolved nothing for.
        {"stops": (ac.AngleStop(0, ac.REGIME_SUMMED, candidate_id="fp-z"),)},
    ],
    ids=["pose-the-spec-refuses", "candidate-with-no-scope"],
)
def test_a_stop_the_spec_will_not_carry_refuses_the_run_before_it_plays(
    request_kwargs: dict,
) -> None:
    """A pose the spec refuses and a candidate nothing resolved a graph scope for
    are both stops this run cannot place, and the run stops there rather than
    measuring the stops it CAN place."""
    request = ac.AngleCaptureRequest(**request_kwargs)

    result, fakes = asyncio.run(_run_gated(request))

    assert result.status == plan_run.RUN_REFUSED
    assert result.reason == ac.WALK_STIMULUS_NOT_ACCEPTED
    assert fakes.play.calls == []


# --------------------------------------------------------------------------- #
# the level bound
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stated", "expected"), [(None, 85.0), (80.0, 80.0), (85.0, 85.0)],
    ids=["none-takes-the-stop", "under-the-stop", "at-the-stop"],
)
def test_a_run_plays_under_the_commissioning_stop_by_default(
    stated: float | None, expected: float,
) -> None:
    """A run stating no ceiling is not asking to be unbounded: the box's own
    commissioning stop is what bounds it."""
    assert plan_run.take_spl_ceiling(
        stated, commissioning_stop_db_spl=85.0,
    ) == expected


def test_a_ceiling_above_the_commissioning_stop_is_refused_not_clamped() -> None:
    """The number was typed; clamping it would let an operator believe a louder
    measurement had been allowed."""
    with pytest.raises(ac.LateralWalkRefused) as refused:
        plan_run.take_spl_ceiling(90.0, commissioning_stop_db_spl=85.0)

    assert refused.value.reason == ac.WALK_CEILING_ABOVE_STOP


@pytest.mark.parametrize(
    ("ceiling", "note"),
    [(None, plan_run.SPL_MONITOR_UNAVAILABLE), (85.0, "ceiling_85_db_spl")],
    ids=["no-calibration", "watched"],
)
def test_the_run_discloses_what_watched_its_level(
    ceiling: float | None, note: str,
) -> None:
    """A box that cannot turn a recording into dB SPL says so rather than
    claiming a bound nothing measured."""
    assert plan_run.spl_monitor_note(ceiling) == note


# --------------------------------------------------------------------------- #
# the package
# --------------------------------------------------------------------------- #


def test_the_package_round_trips_through_json_with_its_counts() -> None:
    """The answer a caller reads is a document, so everything in it has to
    survive being written down."""
    result, _fakes = asyncio.run(_run_gated(_walk([0], ("fp-a", "fp-b"))))

    document = json.loads(json.dumps(result.to_dict()))

    assert document["kind"] == plan_run.PLAN_RESULT_KIND
    assert document["schema_version"] == plan_run.PLAN_RESULT_SCHEMA_VERSION
    assert document["takes_measured"] == len(document["takes"]) == 2
    assert document["stopped_at"] is None
    assert document["stops_planned"] == 2
    assert [take["candidate_id"] for take in document["takes"]] == ["fp-a", "fp-b"]
    # 1-based, the base the gate and the persisted take identity both count in.
    assert [take["index"] for take in document["takes"]] == [1, 2]
    assert all(take["record_ids"] for take in document["takes"])


def test_one_walk_fingerprints_alike_and_an_edited_stop_does_not() -> None:
    """The receipt names WHICH walk ran, off the same document the spool banks —
    so two runs of one walk agree and a changed angle does not."""
    walk = _walk([0, 20], ("fp-a",))

    assert plan_run.request_fingerprint(walk) == plan_run.request_fingerprint(
        _walk([0, 20], ("fp-a",)),
    )
    assert plan_run.request_fingerprint(walk) != plan_run.request_fingerprint(
        _walk([0, 21], ("fp-a",)),
    )
