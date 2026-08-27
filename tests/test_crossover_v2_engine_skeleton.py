# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The engine skeleton's externally observable behaviour.

``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 1. Three things can break here and
each gets one pin at one altitude: ruling S12's surface is complete and loud;
the two held lifetimes open once and give back everything they took; and MS-14
refuses to BANK without ever refusing to play.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pytest

from jasper.active_speaker.driver_acoustics import CAPTURE_GEOMETRIES
from jasper.active_speaker.volume_latch import READBACK_TOLERANCE_DB
from jasper.audio_measurement.program_analysis import polarity_label

from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    MEASURE_KINDS,
    MEASURE_REGIMES,
    POLARITIES,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
    POSITION_AXES,
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
    REGIME_NEAR_FIELD,
    REGIME_REFERENCE_AXIS,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    DISTORTION_VS_LEVEL_NOT_IMPLEMENTED,
    INVERTED_POLARITY_NOT_IMPLEMENTED,
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,
    STUB_CODES,
    VERTICAL_AXIS_NOT_IMPLEMENTED,
    MeasureSpec,
    stub_for_code,
    stubbed_capabilities,
)
from jasper.active_speaker.crossover_v2.prior_bank import CapturePose, PriorBank
from jasper.active_speaker.crossover_v2.playback_transaction import (
    PLAYBACK_STAGES,
    STAGE_ADMIT,
    STAGE_PLAY,
    STAGE_RESTORE,
    PlaybackOutcome,
)
from jasper.active_speaker.crossover_v2.session import (
    UNPROVEN_LEVEL,
    MeasureOutcome,
    SessionStateError,
    TuningSession,
)
from jasper.active_speaker.crossover_v2.session_seams import EngineSeams

from tests._async_wait import wait_signalled


# --------------------------------------------------------------------------- #
# the smallest thing that satisfies the five seams
#
# Not the wave-1 twin, which is permanent infrastructure with 21 importers to
# serve. This is a local double so these pins do not wait on it.
# --------------------------------------------------------------------------- #


@dataclass
class _Graph:
    fingerprint: str = "graph-abc"
    installs: int = 0
    restores: int = 0
    patches: list[Mapping[str, Any]] = field(default_factory=list)
    install_raises: bool = False
    restore_raises: bool = False

    async def install(self) -> str:
        self.installs += 1
        if self.install_raises:
            raise RuntimeError("install blew up after arming half a graph")
        return self.fingerprint

    async def patch(self, changes: Mapping[str, Any]) -> None:
        self.patches.append(changes)

    async def restore(self) -> None:
        self.restores += 1
        if self.restore_raises:
            raise RuntimeError("restore blew up")


@dataclass
class _Volume:
    proven_db: float | None = -20.0
    #: One reading per prove() call, consumed in order; falls back to
    #: ``proven_db`` once exhausted. A claim preempted mid-walk is a sequence.
    readings: list[float | None] = field(default_factory=list)
    acquired: list[float] = field(default_factory=list)
    proves: int = 0
    releases: int = 0
    acquire_raises: bool = False
    release_raises: bool = False

    async def acquire(self, level_db: float) -> None:
        self.acquired.append(level_db)
        if self.acquire_raises:
            raise RuntimeError("the household is holding it")

    async def prove(self) -> float | None:
        index, self.proves = self.proves, self.proves + 1
        if index < len(self.readings):
            return self.readings[index]
        return self.proven_db

    async def release(self) -> None:
        self.releases += 1
        if self.release_raises:
            raise RuntimeError("release blew up")


@dataclass
class _Records:
    banked: list[Mapping[str, Any]] = field(default_factory=list)
    persisted: list[Mapping[str, Any]] = field(default_factory=list)

    async def bank(self, record: Mapping[str, Any]) -> str:
        self.banked.append(record)
        return f"rec-{len(self.banked)}"

    async def read(self, record_id: str) -> Mapping[str, Any] | None:
        index = int(record_id.removeprefix("rec-")) - 1
        return self.banked[index] if 0 <= index < len(self.banked) else None

    async def persist(self, state: Mapping[str, Any]) -> str:
        self.persisted.append(state)
        return f"state-{len(self.persisted)}"

    async def read_state(self, state_id: str) -> Mapping[str, Any] | None:
        index = int(state_id.removeprefix("state-")) - 1
        return self.persisted[index] if 0 <= index < len(self.persisted) else None


@dataclass
class _Play:
    stage: str = STAGE_RESTORE
    incident: str = ""
    #: One (stage, incident) per call, consumed in order; falls back to the
    #: single defaults once exhausted. A mixed-outcome walk is a sequence.
    script: list[tuple[str, str]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run(
        self,
        *,
        spec: MeasureSpec,
        position_deg: int | None,
        prompt: str,
        level_db: float,
        stimulus_dbfs: float | None,
    ) -> PlaybackOutcome:
        index = len(self.calls)
        self.calls.append({
            "spec": spec, "position_deg": position_deg, "prompt": prompt,
            "level_db": level_db, "stimulus_dbfs": stimulus_dbfs,
        })
        stage, incident = (
            self.script[index] if index < len(self.script)
            else (self.stage, self.incident)
        )
        return PlaybackOutcome(stage_reached=stage, incident=incident)


@dataclass
class _Recommender:
    asked: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, record_ids: Sequence[str]) -> Mapping[str, Any]:
        self.asked.append(tuple(record_ids))
        return {"asked": len(record_ids)}


def _session(
    prior: PriorBank | None = None, **overrides: Any,
) -> tuple[TuningSession, dict[str, Any]]:
    parts: dict[str, Any] = {
        "graph": _Graph(),
        "volume": _Volume(),
        "records": _Records(),
        "play": _Play(),
        "recommend": _Recommender(),
    }
    parts.update(overrides)
    session = TuningSession(
        session_id="s1",
        seams=EngineSeams(**parts),
        measurement_level_db=-20.0,
        prior=prior,
    )
    return session, parts


# --------------------------------------------------------------------------- #
# ruling S12 — the surface is complete, and every hole is named
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec, code, instrument, captured",
    [
        (
            MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD),
            NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, "R-3", True,
        ),
        (
            MeasureSpec(kind=MEASURE_KIND_BASELINE, polarity=POLARITY_INVERTED),
            INVERTED_POLARITY_NOT_IMPLEMENTED, "R-1", False,
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, level_ladder_dbfs=(-20.0, -12.0),
            ),
            DISTORTION_VS_LEVEL_NOT_IMPLEMENTED, "R-4", True,
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, position_axis=POSITION_AXIS_VERTICAL,
            ),
            VERTICAL_AXIS_NOT_IMPLEMENTED, "R-5a", False,
        ),
    ],
)
def test_every_unbuilt_mic_only_regime_is_a_named_stub(
    spec: MeasureSpec, code: str, instrument: str, captured: bool,
):
    """S12: the parameter exists today and says exactly what is missing."""
    stubs = stubbed_capabilities(spec)

    assert [stub.code for stub in stubs] == [code]
    assert stubs[0].instrument == instrument
    assert stubs[0].captured is captured
    assert code in STUB_CODES


def test_the_stub_sentence_renders_the_rulings_canonical_example():
    """The one wording pin, and the only prose assertion in this file.

    Ruling S12 fixes the SHAPE and quotes this sentence as its example; the pin
    is here so a template regression is caught, not because the words are the
    contract. See S12 for the shape; every other test asserts on ``code``.
    """
    stub = stubbed_capabilities(
        MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD)
    )[0]

    assert stub.message == (
        "near-field splice not implemented; capture banked, splice pending R-3"
    )


def test_a_spec_asking_for_nothing_unbuilt_discloses_nothing():
    """Anti-vacuity: the stub check must be able to answer "no holes"."""
    assert stubbed_capabilities(MeasureSpec(kind=MEASURE_KIND_VERIFY)) == ()


def test_a_spec_may_trip_more_than_one_stub_at_once():
    stubs = stubbed_capabilities(MeasureSpec(
        kind=MEASURE_KIND_BASELINE,
        regime=REGIME_NEAR_FIELD,
        polarity=POLARITY_INVERTED,
    ))

    assert {stub.code for stub in stubs} == {
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, INVERTED_POLARITY_NOT_IMPLEMENTED,
    }


def test_stub_codes_names_every_code_the_engine_can_emit():
    """Completeness, checked against a spec that trips all four at once.

    Not the same four constants re-listed: this walks what the function
    actually returns for a maximal spec, so a fifth stub that never joined
    ``STUB_CODES`` fails here.
    """
    every = MeasureSpec(
        kind=MEASURE_KIND_BASELINE,
        regime=REGIME_NEAR_FIELD,
        polarity=POLARITY_INVERTED,
        level_ladder_dbfs=(-12.0,),
        position_axis=POSITION_AXIS_VERTICAL,
    )

    assert {stub.code for stub in stubbed_capabilities(every)} == STUB_CODES


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "measure"},
        {"kind": MEASURE_KIND_BASELINE, "regime": "far_field"},
        {"kind": MEASURE_KIND_BASELINE, "polarity": "flipped"},
        {"kind": MEASURE_KIND_BASELINE, "position_axis": "diagonal"},
        {
            "kind": MEASURE_KIND_BASELINE,
            "position_axis": POSITION_AXIS_VERTICAL,
            "positions": (15,),
        },
        {"kind": MEASURE_KIND_BASELINE, "positions": (22.5,)},
        {"kind": MEASURE_KIND_BASELINE, "positions": (True,)},
        {"kind": MEASURE_KIND_BASELINE, "positions": (0, 22), "pose_prompts": ("a",)},
    ],
)
def test_a_spec_outside_the_vocabulary_is_refused_at_construction(kwargs: dict):
    """An out-of-vocabulary parameter fails at its own door.

    The vertical-bearing case is refused by ``PositionGeometry`` itself rather
    than by a second copy of its rule here — the copy this replaces had already
    drifted off it by a word.
    """
    with pytest.raises(ValueError):
        MeasureSpec(**kwargs)


def test_the_vertical_bearing_refusal_is_position_geometrys_own():
    """Anti-drift: the spec must fail for the reason the frame's owner gives.

    If ``PositionGeometry`` stopped refusing a vertical bearing, this file
    would go green on a spec that is no longer checked — so the pin is on the
    owner, not on a message.
    """
    with pytest.raises(ValueError):
        spatial.PositionGeometry(
            axis=POSITION_AXIS_VERTICAL, degrees=15,
            mark_distance_m=spatial.MARK_DISTANCE_M,
        )


def test_the_measure_kinds_are_the_index_columns_and_no_more():
    """Wave 4j's ``kind`` column, and ruling S1's "measuring is measuring"."""
    assert MEASURE_KINDS == (
        MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE, MEASURE_KIND_VERIFY,
    )


# --------------------------------------------------------------------------- #
# the cheap vocabulary copy must not drift off the modules that own the words
# --------------------------------------------------------------------------- #


def test_the_regime_words_are_driver_acoustics_own():
    assert set(MEASURE_REGIMES) == set(CAPTURE_GEOMETRIES)
    assert {REGIME_NEAR_FIELD, REGIME_REFERENCE_AXIS} == set(MEASURE_REGIMES)


def test_the_polarity_words_are_the_measurement_frames_own():
    """``polarity_label`` calls itself "the ONE spelling of the map".

    Asserting the literals against the function — not the function against
    itself — so a change to either spelling reds this.
    """
    assert POLARITY_NORMAL == "normal"
    assert POLARITY_INVERTED == "inverted"
    assert polarity_label(1) == POLARITY_NORMAL
    assert polarity_label(-1) == POLARITY_INVERTED
    assert set(POLARITIES) == {POLARITY_NORMAL, POLARITY_INVERTED}


def test_the_pose_axis_words_are_spatials_own():
    assert POSITION_AXES == spatial.POSITION_AXES
    assert POSITION_AXIS_HORIZONTAL == spatial.POSITION_AXIS_HORIZONTAL
    assert POSITION_AXIS_VERTICAL == spatial.POSITION_AXIS_VERTICAL


def test_the_design_axis_is_spelled_the_way_spatial_spells_it():
    """``()`` means the design axis, and the design axis is ``0`` there."""
    assert DESIGN_AXIS_DEG == spatial._DESIGN_AXIS_GEOMETRY.degrees
    assert spatial._DESIGN_AXIS_GEOMETRY.axis == POSITION_AXIS_HORIZONTAL


# --------------------------------------------------------------------------- #
# the two held lifetimes
# --------------------------------------------------------------------------- #


async def test_open_installs_the_graph_once_and_claims_the_declared_level():
    session, parts = _session()

    async with session:
        assert parts["graph"].installs == 1
        assert parts["volume"].acquired == [-20.0]
        assert session.graph_fingerprint == "graph-abc"
        assert session.is_open

    assert parts["graph"].installs == 1
    assert parts["graph"].restores == 1
    assert parts["volume"].releases == 1
    assert not session.is_open


async def test_opening_an_open_session_is_a_programming_error():
    session, _ = _session()

    async with session:
        with pytest.raises(SessionStateError):
            await session.open()


async def test_a_closed_session_is_spent_and_will_not_re_open():
    """One lifetime per instance. Rebuilding over an existing bank is wave 2's
    first decision, and a re-open that quietly worked would answer it here."""
    session, _ = _session()
    await session.open()
    await session.close()

    with pytest.raises(SessionStateError):
        await session.open()


async def test_closing_a_closed_session_is_not_an_error():
    session, parts = _session()
    await session.open()
    await session.close()
    await session.close()

    assert parts["volume"].releases == 1


async def test_an_open_that_cannot_claim_the_fader_puts_the_graph_back():
    """Half an open is a speaker measuring through a graph nobody holds."""
    session, parts = _session(volume=_Volume(acquire_raises=True))

    with pytest.raises(RuntimeError):
        await session.open()

    assert parts["graph"].restores == 1
    assert parts["volume"].releases == 1, "a half-registered claim is given back"
    assert not session.is_open
    assert session.graph_fingerprint == ""


async def test_an_install_that_raises_mid_arming_still_restores_the_graph():
    """A conforming install may route the tweeter and then fail. Skipping the
    restore because the call raised would leave the box that way."""
    session, parts = _session(graph=_Graph(install_raises=True))

    with pytest.raises(RuntimeError):
        await session.open()

    assert parts["graph"].restores == 1
    assert not session.is_open


async def test_a_failing_graph_restore_still_gives_the_fader_back():
    session, parts = _session(graph=_Graph(restore_raises=True))
    await session.open()

    with pytest.raises(RuntimeError):
        await session.close()

    assert parts["volume"].releases == 1


async def test_a_failing_volume_release_still_restores_the_graph():
    """The fader is the slot whose loss is audible, so its exception is the one
    that reaches the caller — but the graph is still put back."""
    session, parts = _session(volume=_Volume(release_raises=True))
    await session.open()

    with pytest.raises(RuntimeError):
        await session.close()

    assert parts["graph"].restores == 1


async def test_a_release_that_raised_is_tried_again_by_the_next_close():
    """A slot whose release failed stays marked held, so the retry is real.

    A stranded fader claim leaves the speaker at a measurement level nobody
    chose; treating one failed attempt as done would make that permanent.
    """
    volume = _Volume(release_raises=True)
    session, _ = _session(volume=volume)
    await session.open()

    with pytest.raises(RuntimeError):
        await session.close()
    assert volume.releases == 1

    volume.release_raises = False
    await session.close()

    assert volume.releases == 2
    assert not session.is_open


async def test_a_close_failure_does_not_mask_the_exception_in_flight():
    """The body's failure is what the caller needs to see; the close failure is
    chained onto it rather than replacing it."""
    session, _ = _session(volume=_Volume(release_raises=True))

    with pytest.raises(ValueError, match="the real problem") as caught:
        async with session:
            raise ValueError("the real problem")

    assert isinstance(caught.value.__context__, RuntimeError)


async def test_a_cleanup_failure_during_a_failed_open_does_not_hide_the_cause():
    """The install failure is what names the cause; a release that also fails
    is attached to it rather than reported in its place."""
    session, _ = _session(
        graph=_Graph(install_raises=True), volume=_Volume(release_raises=True),
    )

    with pytest.raises(RuntimeError, match="install blew up") as caught:
        await session.open()

    assert isinstance(caught.value.__context__, RuntimeError)
    assert "release blew up" in str(caught.value.__context__)


async def test_measure_refuses_a_session_that_was_never_opened():
    session, _ = _session()

    with pytest.raises(SessionStateError):
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))


# --------------------------------------------------------------------------- #
# the release paths under cancellation
#
# Both pins read the ordered log at the INSTANT the failure surfaces, never
# after a settling sleep: a release that is still running in a detached task
# has already lost the property, and a pin that only counted the calls would
# go green as soon as the abandoned task finished.
# --------------------------------------------------------------------------- #


#: Loop turns a slow release stays in flight for. It must exceed the one or
#: two turns a detached release gets between the cancel landing and the test
#: waking, or the mutation these pins exist for would finish in time to look
#: shielded. Turns rather than seconds: no wall clock, so a starved runner
#: cannot flip either direction.
_RELEASE_TURNS = 50


@dataclass
class _SlowVolume(_Volume):
    """A claim whose calls stay in flight, each logged when it COMPLETES.

    ``slow_acquire`` registers the claim BEFORE it yields, which is the state
    that makes a cancelled acquire dangerous: the fader has already moved.
    """

    events: list[str] = field(default_factory=list)
    releasing: asyncio.Event = field(default_factory=asyncio.Event)
    acquiring: asyncio.Event = field(default_factory=asyncio.Event)
    slow_acquire: bool = False

    async def acquire(self, level_db: float) -> None:
        self.acquired.append(level_db)
        if self.slow_acquire:
            self.acquiring.set()
            for _ in range(_RELEASE_TURNS):
                await asyncio.sleep(0)
        if self.acquire_raises:
            raise RuntimeError("the household is holding it")

    async def release(self) -> None:
        self.releasing.set()
        for _ in range(_RELEASE_TURNS):
            await asyncio.sleep(0)
        self.releases += 1
        self.events.append("volume")


@dataclass
class _LoggingGraph(_Graph):
    events: list[str] = field(default_factory=list)

    async def restore(self) -> None:
        await super().restore()
        self.events.append("graph")


async def test_a_close_cancelled_mid_release_still_gives_both_slots_back_in_order():
    """A cancelling caller waits for the fader before it gets its cancel.

    A bare ``await asyncio.shield(coro)`` detaches the give-back: the
    cancellation reaches the caller while the fader is still being handed
    over, the graph restore has not started, and the two end up out of the
    order ``_release_slots`` promises.
    """
    events: list[str] = []
    volume = _SlowVolume(events=events)
    session, _ = _session(graph=_LoggingGraph(events=events), volume=volume)
    await session.open()

    closing = asyncio.ensure_future(session.close())
    await wait_signalled(volume.releasing, "the release started", producer=closing)
    closing.cancel()
    # A second cancel while the shielded give-back is in flight: a caller that
    # cancels twice must not be obeyed the second time either, or the retry
    # loop's tolerance is untested and abandoning the release becomes free.
    await asyncio.sleep(0)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert events == ["volume", "graph"]
    assert not session.is_open


async def test_a_cancelled_cleanup_after_a_failed_open_does_not_replace_the_cause():
    """The hole the shield closes, and the reason it is part of the decision.

    ``_attach_cleanup_failure`` catches ``Exception``, which is not a
    ``CancelledError`` — so an unshielded give-back lets a cancellation out
    over the very exception ``open`` is propagating, and the caller is told
    the session was cancelled rather than that the fader was already held.
    """
    events: list[str] = []
    volume = _SlowVolume(events=events, acquire_raises=True)
    session, _ = _session(graph=_LoggingGraph(events=events), volume=volume)

    opening = asyncio.ensure_future(session.open())
    await wait_signalled(volume.releasing, "the release started", producer=opening)
    opening.cancel()

    with pytest.raises(RuntimeError, match="the household is holding it") as caught:
        await opening

    assert events == ["volume", "graph"]
    assert isinstance(caught.value.__context__, asyncio.CancelledError)


async def test_a_cancel_during_acquire_still_gives_the_half_taken_claim_back():
    """The cancellation half of *an open that fails puts back what it took*.

    ``open``'s guard catches ``BaseException`` because a ``CancelledError`` is
    not an ``Exception``. Catching only ``Exception`` skips the give-back on
    this path entirely: the acquire has already registered the claim, so the
    fader stays at measurement level while both held-flags read ``False`` —
    nothing a later ``close()`` could give back, on a session that reports
    itself shut.
    """
    events: list[str] = []
    volume = _SlowVolume(events=events, slow_acquire=True)
    session, _ = _session(graph=_LoggingGraph(events=events), volume=volume)

    opening = asyncio.ensure_future(session.open())
    await wait_signalled(volume.acquiring, "the acquire started", producer=opening)
    opening.cancel()

    with pytest.raises(asyncio.CancelledError):
        await opening

    assert events == ["volume", "graph"]
    assert volume.releases == 1
    assert not session.is_open


# --------------------------------------------------------------------------- #
# the four verbs
# --------------------------------------------------------------------------- #


async def test_one_measure_reports_one_entry_per_position_it_names():
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, positions=(-22, 0, 22),
            candidate_id="cand-7",
        ))

    assert [s.position_deg for s in outcome.stimuli] == [-22, 0, 22]
    assert outcome.record_ids == ("rec-1", "rec-2", "rec-3")
    assert [call["position_deg"] for call in parts["play"].calls] == [-22, 0, 22]
    assert {row["kind"] for row in parts["records"].banked} == {
        MEASURE_KIND_CANDIDATE
    }
    assert {row["candidate_id"] for row in parts["records"].banked} == {"cand-7"}
    assert {row["graph_fingerprint"] for row in parts["records"].banked} == {
        "graph-abc"
    }


async def test_a_ladder_measures_every_position_at_every_rung():
    """R-4's axis: the unit is position × rung, and each rung is a stimulus
    level — never a second claim on the fader (ruling S8)."""
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(0, 22),
            level_ladder_dbfs=(-20.0, -12.0),
        ))

    assert [(s.position_deg, s.stimulus_dbfs) for s in outcome.stimuli] == [
        (0, -20.0), (0, -12.0), (22, -20.0), (22, -12.0),
    ]
    assert len(outcome.record_ids) == 4
    # One claim, taken once, at the declared level — the ladder never moved it.
    assert parts["volume"].acquired == [-20.0]
    assert {call["level_db"] for call in parts["play"].calls} == {-20.0}
    assert [row["stimulus_dbfs"] for row in parts["records"].banked] == [
        -20.0, -12.0, -20.0, -12.0,
    ]


async def test_a_spec_naming_no_position_measures_the_design_axis():
    """``()`` and ``(0,)`` are one pose, spelled the way ``spatial`` spells it."""
    empty, empty_parts = _session()
    async with empty:
        await empty.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    explicit, explicit_parts = _session()
    async with explicit:
        await explicit.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(DESIGN_AXIS_DEG,),
        ))

    assert empty_parts["records"].banked == explicit_parts["records"].banked
    assert empty_parts["records"].banked[0]["position_deg"] == DESIGN_AXIS_DEG


async def test_the_pose_prompt_reaches_both_the_transaction_and_the_record():
    """MS-17: one record shape, and the prompt rides it whichever mover
    satisfied the precondition."""
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(0, 22),
            pose_prompts=("stand at the mark", "step 22 degrees left"),
        ))

    assert [call["prompt"] for call in parts["play"].calls] == [
        "stand at the mark", "step 22 degrees left",
    ]
    assert [row["prompt"] for row in parts["records"].banked] == [
        "stand at the mark", "step 22 degrees left",
    ]


async def test_an_unproven_level_refuses_to_bank_but_never_to_play():
    """MS-14 in the shape ruling S10 preserves.

    The stimulus still plays and the session can still measure again; what the
    unproven fader costs is the CLAIM, not the work.
    """
    session, parts = _session(volume=_Volume(proven_db=None))

    async with session:
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert parts["play"].calls, "the stimulus must still have played"
    assert outcome.record_ids == ()
    assert parts["records"].banked == []
    assert outcome.stimuli[0].incident == UNPROVEN_LEVEL
    assert outcome.stimuli[0].level_db is None


async def test_the_level_is_proven_per_stimulus_not_once_per_spec():
    """A claim can be preempted between two positions of one walk. A single
    proof taken before the walk would stamp an unverified level into every
    record after it — the 8.712 dB shape."""
    volume = _Volume(readings=[-20.0, None, -20.0])
    session, parts = _session(volume=volume)

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(-22, 0, 22),
        ))

    assert volume.proves == 3
    assert [s.record_id for s in outcome.stimuli] == ["rec-1", "", "rec-2"]
    assert outcome.stimuli[1].incident == UNPROVEN_LEVEL
    assert [row["position_deg"] for row in parts["records"].banked] == [-22, 22]
    # The unproven rung refused ITS bank and nothing else: the walk went on.
    assert len(parts["play"].calls) == 3


async def test_a_reading_that_disagrees_with_the_declared_level_is_not_proven():
    """G-5. ``prove()`` is contracted to return a reading only when it agrees;
    the session re-checks against the level it declared rather than trusting
    the answer, so the number banked and the number played are one number.

    A drifted reading stamped into a record while the speaker played at another
    is the 8.712 dB incident's exact shape.
    """
    drifted = _Volume(proven_db=-20.0 - (READBACK_TOLERANCE_DB * 10))
    session, parts = _session(volume=drifted)

    async with session:
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert parts["records"].banked == []
    assert outcome.stimuli[0].incident == UNPROVEN_LEVEL


async def test_a_reading_inside_the_confirm_tolerance_is_proven_and_banked():
    """Anti-vacuity for the check above: agreement is not exact equality."""
    nudged = _Volume(proven_db=-20.0 + (READBACK_TOLERANCE_DB / 2))
    session, parts = _session(volume=nudged)

    async with session:
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert len(outcome.record_ids) == 1
    assert parts["records"].banked[0]["level_db"] == nudged.proven_db


async def test_a_transaction_that_never_reached_play_banks_nothing():
    session, parts = _session(
        play=_Play(stage=STAGE_ADMIT, incident="relay_timeout")
    )

    async with session:
        outcome = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert outcome.record_ids == ()
    assert parts["records"].banked == []
    assert outcome.stimuli[0].incident == "relay_timeout"


async def test_a_mixed_walk_banks_what_played_and_says_why_for_the_rest():
    """The continue-not-break rule: one failed position must not end the walk,
    and every entry says what became of its own stimulus."""
    session, parts = _session(play=_Play(script=[
        (STAGE_RESTORE, ""),
        (STAGE_ADMIT, "relay_timeout"),
        (STAGE_RESTORE, ""),
    ]))

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(-22, 0, 22),
        ))

    assert len(outcome.stimuli) == 3, "the walk continued past the failure"
    assert [s.record_id for s in outcome.stimuli] == ["rec-1", "", "rec-2"]
    assert [s.incident for s in outcome.stimuli] == ["", "relay_timeout", ""]
    assert [s.banked for s in outcome.stimuli] == [True, False, True]
    assert [row["position_deg"] for row in parts["records"].banked] == [-22, 22]


async def test_a_stub_that_captures_nothing_stops_the_stimulus():
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, polarity=POLARITY_INVERTED,
        ))

    assert parts["play"].calls == []
    assert outcome.stimuli == ()
    assert outcome.record_ids == ()
    assert [stub.code for stub in outcome.disclosures] == [
        INVERTED_POLARITY_NOT_IMPLEMENTED
    ]


async def test_an_aborted_call_never_claims_a_capture_was_banked():
    """S12 honesty, turned on the disclosure itself.

    A near-field spec that also asks for inverted polarity captures NOTHING —
    the polarity stub stops the stimulus — so the near-field stub must stop
    saying "capture banked" for evidence that does not exist.
    """
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            regime=REGIME_NEAR_FIELD,
            polarity=POLARITY_INVERTED,
        ))

    assert parts["play"].calls == []
    assert {stub.code for stub in outcome.disclosures} == {
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, INVERTED_POLARITY_NOT_IMPLEMENTED,
    }
    assert not any(stub.captured for stub in outcome.disclosures)
    assert all("nothing captured" in stub.message for stub in outcome.disclosures)
    assert not any(stub.captured for stub in (await session.analyze()).disclosures)


async def test_a_stub_whose_capture_still_happens_measures_and_discloses():
    """R-3: the near-field capture ships, and the splice is what is owed."""
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    assert len(outcome.record_ids) == 1
    assert parts["records"].banked[0]["regime"] == REGIME_NEAR_FIELD
    assert [stub.code for stub in outcome.disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert outcome.disclosures[0].captured is True


async def test_the_outcome_carries_the_spec_it_answers():
    """A caller holding one outcome can say which parameters produced it —
    which is what makes a bank of them comparable."""
    spec = MeasureSpec(kind=MEASURE_KIND_CANDIDATE, candidate_id="cand-7")
    session, _ = _session()

    async with session:
        outcome = await session.measure(spec)

    assert isinstance(outcome, MeasureOutcome)
    assert outcome.spec is spec


async def test_analyze_reports_the_holes_and_needs_no_open_session():
    """Ruling S3: a banked session is re-analyzable offline, forever."""
    session, _ = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    outcome = await session.analyze()

    assert not session.is_open
    assert [stub.code for stub in outcome.disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert outcome.results == {}


async def test_analyze_names_each_hole_once_however_many_specs_hit_it():
    """A ten-position near-field walk owes the splice once, not ten times."""
    session, _ = _session()

    async with session:
        for _ in range(3):
            await session.measure(MeasureSpec(
                kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
            ))

    assert [stub.code for stub in (await session.analyze()).disclosures] == [
        NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]


async def test_a_hole_that_later_banks_evidence_stops_saying_it_captured_nothing():
    """Order must not decide what ``analyze`` believes is in the bank.

    An aborted near-field call discloses "nothing captured"; a later near-field
    call that banks a capture must upgrade that, or the session keeps reporting
    there is nothing for R-3's splice to read.
    """
    session, _ = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            regime=REGIME_NEAR_FIELD,
            polarity=POLARITY_INVERTED,
        ))
        assert not (await session.analyze()).disclosures[0].captured

        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    disclosures = (await session.analyze()).disclosures
    near_field = [
        s for s in disclosures if s.code == NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert len(near_field) == 1, "still one entry per hole"
    assert near_field[0].captured is True


async def test_a_hole_that_already_banked_evidence_is_not_downgraded():
    """The reverse never happens: banked evidence does not stop existing."""
    session, _ = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            regime=REGIME_NEAR_FIELD,
            polarity=POLARITY_INVERTED,
        ))

    near_field = [
        s for s in (await session.analyze()).disclosures
        if s.code == NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert len(near_field) == 1
    assert near_field[0].captured is True


async def test_a_banked_record_can_be_read_back_by_its_id():
    """The read door is what makes ``analyze`` an offline verb (ruling S3)."""
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(22,),
        ))

    record = await parts["records"].read(outcome.record_ids[0])

    assert record is not None
    assert record["position_deg"] == 22
    assert await parts["records"].read("rec-99") is None


async def test_recommend_asks_the_prescriber_over_everything_banked():
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, positions=(0, 22),
        ))

    outcome = await session.recommend()

    assert parts["recommend"].asked == [("rec-1", "rec-2")]
    assert outcome.record_ids == ("rec-1", "rec-2")
    assert outcome.recommendation == {"asked": 2}


async def test_save_persists_the_session_state_over_the_records_measure_banked():
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    outcome = await session.save()

    assert outcome.state_id == "state-1"
    assert outcome.record_ids == ("rec-1",)
    state = parts["records"].persisted[0]
    assert state["session_id"] == "s1"
    assert state["graph_fingerprint"] == "graph-abc"
    assert state["record_ids"] == ("rec-1",)
    # A disclosure travels as its code AND whether the capture happened: those
    # are two different facts to whatever reads the bank back.
    assert state["disclosures"] == (
        {"code": NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, "captured": True},
    )


# --------------------------------------------------------------------------- #
# rebuilding a session over a previous bank
# --------------------------------------------------------------------------- #


async def _banked(records: _Records, *specs: MeasureSpec) -> tuple[str, TuningSession]:
    """Run a whole session over ``records``; return its state id and the session."""
    session, _parts = _session(records=records)
    async with session:
        for spec in specs:
            await session.measure(spec)
    return (await session.save()).state_id, session


def _pose(record: Mapping[str, Any]) -> CapturePose:
    return CapturePose(
        position_axis=record["position_axis"],
        position_deg=record["position_deg"],
        stimulus_dbfs=record["stimulus_dbfs"],
    )


async def test_a_prior_bank_is_exactly_what_save_wrote_read_back_again():
    """The round trip, which is the type's whole contract.

    Every field is asserted against the session that wrote it or against the
    store's own record list — never against a literal — so this cannot pass by
    agreeing with a constant the writer never produced.
    """
    records = _Records()
    state_id, wrote = await _banked(
        records,
        MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD),
        MeasureSpec(kind=MEASURE_KIND_BASELINE, positions=(22,)),
    )

    bank = await PriorBank.read(records, state_id)

    assert bank is not None
    assert bank.state_id == state_id
    assert bank.session_id == wrote.session_id
    assert bank.measurement_level_db == wrote.measurement_level_db
    assert bank.graph_fingerprint == wrote.graph_fingerprint
    assert bank.record_ids == wrote.banked_record_ids
    assert bank.disclosures == (await wrote.analyze()).disclosures
    assert bank.disclosures == (
        stub_for_code(NEAR_FIELD_SPLICE_NOT_IMPLEMENTED, captured=True),
    )


async def test_a_state_id_the_store_cannot_resolve_reads_as_no_prior():
    """A missing bank is a fact to disclose, never an exception (ruling S10).

    The state file this replaces is overwritten every persist, so "the prior
    round's state is gone" is an ordinary outcome and not a corruption.
    """
    assert await PriorBank.read(_Records(), "state-99") is None


async def test_a_pose_measured_twice_resolves_to_the_later_baseline():
    """A retake supersedes the attempt it followed.

    "Immediately before apply" is what makes the before→after bracket honest,
    so when one pose carries two baselines the nearer one wins. This is a
    tiebreak WITHIN a pose and never across poses — see the walk below.
    """
    records = _Records()
    state_id, _wrote = await _banked(
        records,
        MeasureSpec(kind=MEASURE_KIND_BASELINE, positions=(22,)),
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, positions=(22,)),
        MeasureSpec(kind=MEASURE_KIND_BASELINE, positions=(22,)),
    )

    bank = await PriorBank.read(records, state_id)

    assert bank is not None
    assert bank.baseline_for(_pose(records.banked[0])) == "rec-3"


async def test_a_pose_the_prior_never_baselined_has_no_before():
    """A round with no comparable "before" says so; it does not promote one.

    Neither a candidate capture at the same pose nor a baseline at a different
    one is this capture's comparand.
    """
    records = _Records()
    state_id, _wrote = await _banked(
        records,
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, positions=(22,)),
        MeasureSpec(kind=MEASURE_KIND_BASELINE, positions=(-22,)),
    )

    bank = await PriorBank.read(records, state_id)

    assert bank is not None
    assert bank.baseline_for(_pose(records.banked[0])) == ""


async def test_each_capture_of_one_walk_names_the_before_taken_at_ITS_pose():
    """The whole reason the comparand is resolved per capture and not per bank.

    A prior that walked three poses in ONE ``measure`` call banked three
    "befores". A bank-wide answer would stamp the LAST pose's baseline onto
    every capture — so a verify at −22° would be differenced against a baseline
    measured at +22°, and the verdict would report the room's off-axis
    behaviour as the correction's effect.
    """
    records = _Records()
    prior_id, _wrote = await _banked(records, MeasureSpec(
        kind=MEASURE_KIND_BASELINE, positions=(-22, 0, 22),
    ))
    before_at = {r["position_deg"]: f"rec-{i}"
                 for i, r in enumerate(records.banked, start=1)}
    assert len(before_at) == 3

    session, parts = _session(
        records=records, prior=await PriorBank.read(records, prior_id),
    )
    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_VERIFY, positions=(-22, 0, 22), candidate_id="c1",
        ))

    after = parts["records"].banked[3:]
    assert [r["position_deg"] for r in after] == [-22, 0, 22]
    assert [r["baseline_record_id"] for r in after] == [
        before_at[-22], before_at[0], before_at[22],
    ]


async def test_each_rung_of_a_ladder_names_the_before_taken_at_ITS_rung():
    """The same rule on the axis a level ladder moves along.

    One bearing, several stimulus levels. Pairing across rungs would difference
    a quiet capture against a loud one and call the difference a correction —
    which is why the rung is part of the pose rather than a field the match
    ignores.
    """
    records = _Records()
    prior_id, _wrote = await _banked(records, MeasureSpec(
        kind=MEASURE_KIND_BASELINE, level_ladder_dbfs=(-20.0, -12.0),
    ))
    before_at = {r["stimulus_dbfs"]: f"rec-{i}"
                 for i, r in enumerate(records.banked, start=1)}
    assert len(before_at) == 2

    session, parts = _session(
        records=records, prior=await PriorBank.read(records, prior_id),
    )
    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_VERIFY, level_ladder_dbfs=(-20.0, -12.0),
        ))

    after = parts["records"].banked[2:]
    assert [r["stimulus_dbfs"] for r in after] == [-20.0, -12.0]
    assert [r["baseline_record_id"] for r in after] == [
        before_at[-20.0], before_at[-12.0],
    ]


async def test_every_capture_names_the_before_it_will_be_graded_against():
    """One hop from a capture to its comparand, so a verdict re-runs offline.

    Ruling S3's return on banking complete records is that any future analysis
    can grade a banked session. An analysis that had to find the session state
    first, and re-derive which record was the "before", would be re-deriving
    the pairing rather than reading it.
    """
    records = _Records()
    prior_id, _wrote = await _banked(records, MeasureSpec(kind=MEASURE_KIND_BASELINE))
    bank = await PriorBank.read(records, prior_id)

    session, parts = _session(prior=bank)
    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_VERIFY, candidate_id="c1"))

    banked = parts["records"].banked[0]
    assert banked["baseline_record_id"] == "rec-1"
    assert banked["candidate_id"] == "c1"


async def test_a_session_with_no_prior_still_banks_and_says_the_before_is_empty():
    """MS-14's shape applied to the comparand: refuse to CLAIM, never to work.

    A first-ever round has no "before". That is an honest fact about the
    capture, and the capture is still evidence.
    """
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_VERIFY))

    assert parts["records"].banked[0]["baseline_record_id"] == ""


async def test_analyze_reports_the_priors_holes_before_its_own_and_each_once():
    """A round's evidence is both sides of it, so both sides' holes are named.

    Prior first because it happened first, and a hole both sides hit is one
    hole — the reader is being told what the round cannot claim, not how many
    sessions tripped over it.
    """
    records = _Records()
    prior_id, _wrote = await _banked(
        records,
        MeasureSpec(kind=MEASURE_KIND_BASELINE, position_axis=POSITION_AXIS_VERTICAL),
        MeasureSpec(kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD),
    )

    session, _parts = _session(records=records, prior=await PriorBank.read(records, prior_id))
    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_VERIFY, regime=REGIME_NEAR_FIELD,
        ))

    codes = [stub.code for stub in (await session.analyze()).disclosures]

    assert codes == [
        VERTICAL_AXIS_NOT_IMPLEMENTED, NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,
    ]


async def test_a_hole_the_prior_could_not_capture_is_upgraded_when_this_session_does():
    """The merge keeps the best news, across banks as well as within one.

    The prior's near-field spec was aborted by its vertical sibling and banked
    nothing; this session's near-field spec ran and banked. Reporting the
    aborted rendering would tell ``analyze`` there is no evidence waiting when
    there is.
    """
    records = _Records()
    prior_id, _wrote = await _banked(records, MeasureSpec(
        kind=MEASURE_KIND_BASELINE,
        regime=REGIME_NEAR_FIELD,
        position_axis=POSITION_AXIS_VERTICAL,
    ))
    bank = await PriorBank.read(records, prior_id)
    assert [s.captured for s in bank.disclosures] == [False, False]

    session, _parts = _session(records=records, prior=bank)
    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_VERIFY, regime=REGIME_NEAR_FIELD,
        ))

    near_field = [
        s for s in (await session.analyze()).disclosures
        if s.code == NEAR_FIELD_SPLICE_NOT_IMPLEMENTED
    ]
    assert len(near_field) == 1
    assert near_field[0].captured is True


@pytest.mark.parametrize(
    "disclosures",
    [
        None,
        "near_field_splice_not_implemented",
        [{"code": "a_hole_a_later_build_named", "captured": True}],
        [["near_field_splice_not_implemented", True]],
        [{}],
    ],
    ids=["absent", "not-a-list", "unknown-code", "not-a-mapping", "empty"],
)
async def test_a_banked_disclosure_this_build_cannot_describe_is_dropped(disclosures):
    """Rendering a hole this build has no words for would say nothing to a person.

    The bank still carries the raw code for whoever wrote it; what is dropped
    is the pretence that this build can explain it.
    """
    records = _Records()
    state_id = await records.persist({"record_ids": (), "disclosures": disclosures})

    assert (await PriorBank.read(records, state_id)).disclosures == ()


# --------------------------------------------------------------------------- #
# the play transaction's own vocabulary
# --------------------------------------------------------------------------- #


def test_the_play_stages_are_the_five_the_ruling_names_in_order():
    assert PLAYBACK_STAGES == ("ready", "admit", "lock", "play", "restore")


@pytest.mark.parametrize(
    "stage, played",
    [("ready", False), ("admit", False), ("lock", False),
     ("play", True), ("restore", True)],
)
def test_a_transaction_played_only_once_it_completed_the_play_stage(
    stage: str, played: bool,
):
    """``stage_reached`` is the last stage COMPLETED, so a transaction that
    failed and then correctly restored reports the stage before the failure —
    never ``restore``, which would make ``played`` vacuously true."""
    assert PlaybackOutcome(stage_reached=stage).played is played


def test_an_outcome_naming_a_stage_that_does_not_exist_is_refused():
    with pytest.raises(ValueError):
        PlaybackOutcome(stage_reached="playing")


@pytest.mark.parametrize(
    "incident",
    ["relay timed out", "RelayTimeout", "the fader could not be proven", "9lives"],
)
def test_an_incident_that_is_a_sentence_rather_than_a_code_is_refused(
    incident: str,
):
    """The household's copy is ``refusal_copy``'s job; a transaction minting
    its own would be the second vocabulary this refactor exists to remove."""
    with pytest.raises(ValueError):
        PlaybackOutcome(stage_reached=STAGE_PLAY, incident=incident)


def test_a_reason_code_shaped_incident_is_accepted():
    assert PlaybackOutcome(
        stage_reached=STAGE_ADMIT, incident="relay_timeout",
    ).incident == "relay_timeout"
    assert PlaybackOutcome(stage_reached=STAGE_RESTORE).incident == ""


def test_the_sessions_own_incident_code_is_reason_code_shaped():
    """``UNPROVEN_LEVEL`` travels the same field as a transaction's incident,
    so it obeys the same shape."""
    assert PlaybackOutcome(
        stage_reached=STAGE_PLAY, incident=UNPROVEN_LEVEL,
    ).incident == UNPROVEN_LEVEL


def test_the_confirm_tolerance_prove_is_specified_against_is_the_repos_one():
    """``VolumeClaim.prove``'s contract names ``fader_matches``'s tolerance —
    the confirm tolerance wave 5 collapses the other writers onto."""
    assert READBACK_TOLERANCE_DB == 0.05
