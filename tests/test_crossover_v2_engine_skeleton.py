# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The engine skeleton's externally observable behaviour.

Three things can break here and each gets one pin at one altitude: ruling
S12's surface is complete and loud (ADR-0228);
the two held lifetimes open once and give back everything they took; and MS-14
refuses to BANK without ever refusing to play.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from jasper.active_speaker.driver_acoustics import CAPTURE_GEOMETRIES
from jasper.active_speaker.volume_latch import READBACK_TOLERANCE_DB
from jasper.audio_measurement.program_analysis import polarity_label

from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    DRIVER_ROLE_TWEETER,
    DRIVER_ROLE_WOOFER,
    DRIVER_ROLES,
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
    NEAR_FIELD_SPLICE_NOT_IMPLEMENTED,
    STUB_CODES,
    VERTICAL_AXIS_NOT_IMPLEMENTED,
    MeasureSpec,
    inverted_roles_for,
    stubbed_capabilities,
)
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
from jasper.active_speaker.crossover_v2.spatial import take_id_for

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
    #: One entry per install: the polarity variant that stimulus asked for.
    inverted_roles: list[tuple[str, ...]] = field(default_factory=list)
    patches: list[Mapping[str, Any]] = field(default_factory=list)
    install_raises: bool = False
    restore_raises: bool = False
    measurement_delays: list = field(default_factory=list)
    #: One entry per install: the level match that stimulus asked for.
    level_trims: list = field(default_factory=list)

    async def install(
        self, inverted_roles: tuple[str, ...] = (), measurement_delays_us=None,
        level_trims_db=None,
    ) -> str:
        self.installs += 1
        self.inverted_roles.append(tuple(inverted_roles))
        self.measurement_delays.append(dict(measurement_delays_us or {}))
        self.level_trims.append(dict(level_trims_db or {}))
        if self.install_raises:
            raise RuntimeError("install blew up after arming half a graph")
        if level_trims_db:
            # A level match is a DIFFERENT graph, for the delay's reason: the
            # real emitter moves the mixer gains and so the fingerprint.
            matched = "+".join(
                f"{role}@{db:g}" for role, db in sorted(level_trims_db.items())
            )
            return f"{self.fingerprint}-lm-{matched}"
        if measurement_delays_us:
            # A delay coordinate is a DIFFERENT graph, exactly as a polarity
            # variant is, so it cannot answer with another one's fingerprint.
            tail = "+".join(
                f"{role}@{us:g}" for role, us in sorted(measurement_delays_us.items())
            )
            return f"{self.fingerprint}-{tail}"
        if not inverted_roles:
            return self.fingerprint
        # A variant is a DIFFERENT graph, so it must not answer with the
        # normal graph's fingerprint — the real one does not.
        return f"{self.fingerprint}-{'+'.join(inverted_roles)}"

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

    async def bank(self, record: Mapping[str, Any]) -> str:
        self.banked.append(record)
        return f"rec-{len(self.banked)}"


@dataclass
class _Play:
    stage: str = STAGE_RESTORE
    incident: str = ""
    #: Where this transaction put the bytes, minted before the write because
    #: nothing downstream can re-derive it.
    wav_path: str = ""
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
        return PlaybackOutcome(
            stage_reached=stage, incident=incident, wav_path=self.wav_path,
        )


def _session(
    *,
    level_match_trims_db: Mapping[str, float] | None = None,
    **overrides: Any,
) -> tuple[TuningSession, dict[str, Any]]:
    parts: dict[str, Any] = {
        "graph": _Graph(),
        "volume": _Volume(),
        "records": _Records(),
        "play": _Play(),
    }
    parts.update(overrides)
    session = TuningSession(
        session_id="s1",
        seams=EngineSeams(**parts),
        measurement_level_db=-20.0,
        level_match_trims_db=level_match_trims_db or {},
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
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, level_ladder_dbfs=(-20.0, -12.0),
            ),
            DISTORTION_VS_LEVEL_NOT_IMPLEMENTED, "R-4", True,
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE, position_axis=POSITION_AXIS_VERTICAL,
            ),
            VERTICAL_AXIS_NOT_IMPLEMENTED, "R-5a", True,
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


def test_the_elevation_hole_is_disclosed_by_the_value_not_by_the_axis_word():
    """R-5a is owed for any spec that banks an elevation, on either axis.

    The two angles are orthogonal, so a HORIZONTAL walk raised off mark height
    banks exactly the evidence a vertical walk does. Keying the disclosure on
    the axis word alone would let a compound spec bank an unanalysed elevation
    and report nothing pending.
    """
    compound = MeasureSpec(
        kind=MEASURE_KIND_BASELINE, positions=(22,), vertical_deg=22,
    )

    assert [s.code for s in stubbed_capabilities(compound)] == [
        VERTICAL_AXIS_NOT_IMPLEMENTED
    ]
    assert stubbed_capabilities(
        MeasureSpec(kind=MEASURE_KIND_BASELINE, positions=(22,))
    ) == ()


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
        inverted_role=DRIVER_ROLE_TWEETER,
    ))

    # R-1 is no longer among them: the reverse-null analysis ships, so an
    # inverted spec discloses only what its REGIME still owes.
    assert {stub.code for stub in stubs} == {NEAR_FIELD_SPLICE_NOT_IMPLEMENTED}


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
        inverted_role=DRIVER_ROLE_TWEETER,
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
        # R-1: the regime and the branch it flips are one parameter in two
        # halves, so neither half stands alone.
        {"kind": MEASURE_KIND_BASELINE, "polarity": POLARITY_INVERTED},
        {
            "kind": MEASURE_KIND_BASELINE,
            "polarity": POLARITY_INVERTED,
            "inverted_role": "midrange",
        },
        {"kind": MEASURE_KIND_BASELINE, "inverted_role": DRIVER_ROLE_TWEETER},
        {"kind": MEASURE_KIND_BASELINE, "position_axis": "diagonal"},
        {"kind": MEASURE_KIND_BASELINE, "vertical_deg": 7.5},
        {"kind": MEASURE_KIND_BASELINE, "vertical_deg": True},
        # A vertical walk commands no horizontal bearing, so it states none —
        # the invariant every pooled bearing set downstream relies on.
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

    The whole-degree cases are refused by ``PositionGeometry`` itself rather
    than by a second copy of its rule here — the copy this replaces had already
    drifted off it by a word.
    """
    with pytest.raises(ValueError):
        MeasureSpec(**kwargs)


@pytest.mark.parametrize("elevation", [0, 7, -22])
def test_a_manually_raised_pose_is_accepted_and_carries_its_elevation(elevation):
    """The rig cannot swing in elevation; a person can, and the frame records it.

    This replaces a refusal. ``PositionGeometry`` used to raise on any vertical
    pose that named a number, on the reasoning that no bearing could have been
    commanded — true of the ARM, and never true of the operator, who raises the
    microphone by hand. The bearing stays ``None`` because none was commanded;
    the elevation is the value that says where the pose actually was.

    The automation seam keeps its own refusal, which is about a positioner
    rather than about a pose — pinned by
    ``test_crossover_v2_remote_tier`` over ``position_angle_deg``.
    """
    geometry = spatial.PositionGeometry(
        axis=POSITION_AXIS_VERTICAL, degrees=None,
        mark_distance_m=spatial.MARK_DISTANCE_M, vertical_deg=elevation,
    )

    assert geometry.vertical_deg == elevation
    assert geometry.degrees is None
    assert spatial.PositionGeometry(
        axis=POSITION_AXIS_HORIZONTAL, degrees=22,
        mark_distance_m=spatial.MARK_DISTANCE_M,
    ).vertical_deg == 0


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


async def test_measure_proves_the_graph_before_every_stimulus():
    """MS-13/S6: the idempotent install IS the health check, per stimulus.

    Between two stimuli the writer lock is released and arbitrary time
    passes, so another DSP writer may have replaced the running graph; a
    session that proved only at open would play every later stimulus through
    whatever is standing. One install at open, one more per stimulus.
    Mutation: removing the per-stimulus prove leaves installs at 1 and this
    reds alone.
    """
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE, positions=(0, 15),
        ))

    assert parts["graph"].installs == 1 + 2


async def test_the_record_carries_the_fingerprint_its_own_stimulus_proved():
    """Provenance follows the prove, not the open.

    A graph swapped between open and the stimulus is re-proven (put back or
    re-named) by the per-stimulus install, and the record must name THAT
    answer — a record carrying open()'s fingerprint would claim evidence
    measured through a graph the stimulus never played.
    """
    session, parts = _session()

    async with session:
        assert session.graph_fingerprint == "graph-abc"
        parts["graph"].fingerprint = "graph-reproven"
        await session.measure(MeasureSpec(kind=MEASURE_KIND_CANDIDATE))

    [record] = parts["records"].banked
    assert record["graph_fingerprint"] == "graph-reproven"
    assert session.graph_fingerprint == "graph-reproven"


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


async def test_a_cancel_mid_walk_still_accounts_for_what_already_banked():
    """Every stimulus is a cancel point, so the accounting cannot wait for the
    walk to end.

    A record written to the store but missing from ``banked_record_ids`` is
    evidence on disk the session denies taking — a gap that would leave
    anything counting on ``banked_record_ids`` one capture short of what the
    speaker actually measured.
    """
    events: list[str] = []
    play = _PausingPlay(events=events, pause_before=2)
    session, parts = _session(play=play)
    await session.open()

    walking = asyncio.ensure_future(session.measure(MeasureSpec(
        kind=MEASURE_KIND_BASELINE, positions=(0, 22),
    )))
    await wait_signalled(play.reached, "the second stimulus", producer=walking)
    walking.cancel()

    with pytest.raises(asyncio.CancelledError):
        await walking

    assert parts["records"].banked, "the first stimulus banked before the cancel"
    assert session.banked_record_ids == ("rec-1",), (
        "a record the store holds is missing from the session's own account"
    )


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
class _PausingPlay(_Play):
    """A walk that parks inside one stimulus so a cancel can land mid-loop."""

    events: list[str] = field(default_factory=list)
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    pause_before: int = 2

    async def run(self, **kwargs: Any) -> PlaybackOutcome:
        if len(self.calls) + 1 == self.pause_before:
            self.reached.set()
            for _ in range(_RELEASE_TURNS):
                await asyncio.sleep(0)
        return await super().run(**kwargs)


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


async def test_a_refused_volume_costs_zero_graph_operations():
    """NB1 at the engine: the claim goes first, so a refusal installs nothing.

    A volume that will not establish is not a volume anything may be admitted
    against. Installing first would buy two CamillaDSP swaps — install and
    restore — for a session that never plays a stimulus, and it is the shape
    the wizard already refuses one frame up. This is the setup half of the
    order ``_release_slots`` mirrors: acquire, then install; restore, then
    release.
    """
    graph = _Graph()
    session, _ = _session(graph=graph, volume=_Volume(acquire_raises=True))

    with pytest.raises(RuntimeError, match="holding it"):
        await session.open()

    assert graph.installs == 0, (
        "the graph was installed for a session whose volume never established"
    )
    # The unwind still CALLS restore — unconditionally, because a seam that
    # raised may have armed half of what it was asked for and the session
    # cannot see how far it got. Against a graph that was never installed that
    # call is the contracted no-op, which is why the install count is the
    # property and the restore count is not.
    assert not session.is_open


async def test_a_close_cancelled_mid_release_still_gives_both_slots_back_in_order():
    """A cancelling caller waits for the fader before it gets its cancel.

    A bare ``await asyncio.shield(coro)`` detaches the give-back: the
    cancellation reaches the caller while the slots are still being handed
    over, the second release has not started, and the two end up out of the
    order ``_release_slots`` promises.

    **The order is graph-then-volume, and it flipped with W5-c1's setup
    reorder.** ``open`` now takes the claim before installing the graph, so
    reverse order of taking is the graph first — which is also the isolation
    order, since the volume release lands a level the household can hear and a
    graph put back after that would swap the pipeline underneath it.
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

    assert events == ["graph", "volume"]
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


async def test_a_banked_record_names_the_capture_the_transaction_wrote():
    """Without this, an offline reader reads records that reach no audio.

    A bundle-relative capture path is NOT derivable from anything else on the
    record: ``bundles.capture_artifact_relpath`` appends a ``uuid4`` hex, so the
    transaction that mints it before the write is the only party that can say
    it. It comes off the play outcome for that reason, rather than being
    re-derived here from an id that cannot produce it.
    """
    minted = "captures/summed/s1_a01-9f2c.wav"
    session, parts = _session(play=_Play(wav_path=minted))

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert [row["wav_path"] for row in parts["records"].banked] == [minted]


async def test_every_banked_take_of_one_session_is_named_apart_from_the_others():
    """A record the store cannot file is a record that was not banked.

    The engine holds no position identity — no position id, no take id, no
    attempt — and its one index is per POSITION, so a ladder of rungs makes
    several records under it. Two records of one session must still be two
    names, or the second overwrites the first at the store's path.

    Uniqueness is claimed for the SESSION and no wider: two sessions are two
    relay-scoped paths, which is what makes a global registry of minted ids
    unnecessary.
    """
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, positions=(0, 22),
            level_ladder_dbfs=(-12.0, -6.0),
        ))

    take_ids = [row["take_id"] for row in parts["records"].banked]
    assert len(take_ids) == 4
    assert len(set(take_ids)) == 4
    # Minted through the one spelling of a take id, not a second convention.
    assert take_ids[0] == take_id_for(f"{MEASURE_KIND_BASELINE}_00", 0)


async def test_a_second_measure_keeps_counting_where_the_first_stopped():
    """The ordinal is the SESSION's, not the call's.

    Two specs measured by one session are one bank. An ordinal that restarted
    per call would mint the same name twice and file the second capture over
    the first — the failure the ordinal exists to prevent, reintroduced by the
    scope it is kept at.
    """
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(kind=MEASURE_KIND_CANDIDATE))

    take_ids = [row["take_id"] for row in parts["records"].banked]
    assert take_ids == [
        take_id_for(f"{MEASURE_KIND_BASELINE}_00", 0),
        take_id_for(f"{MEASURE_KIND_CANDIDATE}_01", 0),
    ]


async def test_a_capture_that_placed_no_bytes_banks_an_empty_pointer():
    """``""`` is a fact about the capture, never a refusal to bank it.

    ``baseline_record_id``'s precedent, on the other pointer: a record whose
    audio was never retained still grades, and a reader learns that from the
    empty field rather than from the record's absence.
    """
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))

    assert parts["records"].banked[0]["wav_path"] == ""


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


async def test_a_vertical_spec_plays_banks_and_labels_the_take_it_took():
    """R-5a: a hand-raised pose is measured and recorded, not refused.

    This axis used to capture NOTHING — the stub stopped the stimulus. The
    owner ruled that class of refusal out: the operator raises the microphone
    by hand, so the take is taken and labelled with where it was taken from.

    ``position_deg`` stays ``None`` because no bearing was commanded, and that
    is what keeps a raised take out of every pooled bearing set downstream.
    The disclosure survives, saying the honest remaining thing: the capture is
    banked and no analysis reads it yet.
    """
    records = _Records()
    session, parts = _session(records=records)

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            position_axis=POSITION_AXIS_VERTICAL,
            vertical_deg=22,
        ))

    assert parts["play"].calls != []
    assert len(outcome.record_ids) == 1
    banked = records.banked[0]
    assert banked["position_axis"] == POSITION_AXIS_VERTICAL
    assert banked["vertical_deg"] == 22
    assert banked["position_deg"] is None


async def test_a_stub_whose_capture_still_happens_measures_and_banks():
    """R-3: the near-field capture ships, and the splice is what is owed."""
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, regime=REGIME_NEAR_FIELD,
        ))

    assert len(outcome.record_ids) == 1
    assert parts["records"].banked[0]["regime"] == REGIME_NEAR_FIELD


async def test_the_outcome_carries_the_spec_it_answers():
    """A caller holding one outcome can say which parameters produced it —
    which is what makes a bank of them comparable."""
    spec = MeasureSpec(kind=MEASURE_KIND_CANDIDATE, candidate_id="cand-7")
    session, _ = _session()

    async with session:
        outcome = await session.measure(spec)

    assert isinstance(outcome, MeasureOutcome)
    assert outcome.spec is spec


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


# --------------------------------------------------------------------------- #
# R-1 — the reverse-null, end to end at the engine's own altitude
# --------------------------------------------------------------------------- #


def test_the_driver_role_words_are_the_presets_own():
    """The cheap copy, pinned to the module that owns the roles.

    The program graph is scoped to a 2-way preset, so its role set is exactly
    that preset's — a third role appearing there without appearing here would
    be a branch no measurement could name.
    """
    from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY

    assert DRIVER_ROLES == DRIVER_ROLES_BY_WAY[2]
    assert (DRIVER_ROLE_WOOFER, DRIVER_ROLE_TWEETER) == DRIVER_ROLES


@pytest.mark.parametrize(
    "spec, expected",
    [
        (MeasureSpec(kind=MEASURE_KIND_BASELINE), ()),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE,
                polarity=POLARITY_INVERTED,
                inverted_role=DRIVER_ROLE_TWEETER,
            ),
            (DRIVER_ROLE_TWEETER,),
        ),
        (
            MeasureSpec(
                kind=MEASURE_KIND_BASELINE,
                polarity=POLARITY_INVERTED,
                inverted_role=DRIVER_ROLE_WOOFER,
            ),
            (DRIVER_ROLE_WOOFER,),
        ),
    ],
)
def test_the_spec_translates_into_exactly_the_branches_the_graph_must_flip(
    spec: MeasureSpec, expected: tuple[str, ...],
):
    """The one translation from polarity words into graph vocabulary."""
    assert inverted_roles_for(spec) == expected


async def test_an_inverted_capture_plays_and_banks_like_any_other():
    """R-1's whole point: the verb stopped being a stub that captures nothing."""
    session, parts = _session()

    async with session:
        outcome = await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_TWEETER,
        ))

    assert len(parts["play"].calls) == 1
    assert len(outcome.record_ids) == 1
    # R-1 ships: an inverted take plays, banks, AND is analysed, so it owes no
    # disclosure. The stub row it used to carry is gone from the registry.


async def test_the_named_branch_reaches_the_graph_that_stimulus_installs():
    """The sign is applied by INSTALLING a different graph, so the flip has to
    travel on the install — not on a patch afterwards, which would leave the
    fingerprint naming the non-inverted twin."""
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_WOOFER,
        ))

    # One at open() and one per stimulus.
    assert parts["graph"].inverted_roles == [(), (), (DRIVER_ROLE_WOOFER,)]


async def test_the_delay_coordinate_reaches_the_graph_that_stimulus_installs():
    """R-1's delay travels the same road its polarity does: by INSTALLING a
    different graph. A coordinate applied any other way would leave the
    fingerprint naming a graph that carried a different delay."""
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_TWEETER,
            delayed_role=DRIVER_ROLE_TWEETER,
            delay_us=250.0,
        ))

    assert parts["graph"].measurement_delays == [
        {}, {}, {DRIVER_ROLE_TWEETER: 250.0},
    ]


async def test_two_coordinates_are_two_graphs_not_one_reused():
    """The confirmation plays three coordinates in a row. If the graph seam
    keyed its cache on polarity alone it would hand the second coordinate the
    first one's graph and measure the wrong delay."""
    session, parts = _session()

    async with session:
        for delay_us in (100.0, 200.0):
            await session.measure(MeasureSpec(
                kind=MEASURE_KIND_BASELINE,
                polarity=POLARITY_INVERTED,
                inverted_role=DRIVER_ROLE_TWEETER,
                delayed_role=DRIVER_ROLE_TWEETER,
                delay_us=delay_us,
            ))

    installed = [d for d in parts["graph"].measurement_delays if d]
    assert installed == [
        {DRIVER_ROLE_TWEETER: 100.0}, {DRIVER_ROLE_TWEETER: 200.0},
    ]


async def test_the_level_match_reaches_the_graph_that_stimulus_installs():
    """The session was opened with the box's own trims; the SPEC decides which
    stimuli carry them. Applying them any other way would leave the fingerprint
    naming a graph whose branches were not levelled."""
    session, parts = _session(level_match_trims_db={DRIVER_ROLE_TWEETER: -9.5})

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_TWEETER,
            level_matched=True,
        ))

    # One at open() and one per stimulus, and the un-matched spec carries none
    # even though the session was holding trims all along.
    assert parts["graph"].level_trims == [
        {}, {}, {DRIVER_ROLE_TWEETER: -9.5},
    ]


async def test_a_session_holding_no_trims_installs_none():
    """The refusal for that pairing is the host's, at open. The engine's own
    answer is empty rather than a raise mid-walk, so a spec cannot invent a
    level match out of a session that was given none."""
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, level_matched=True,
        ))

    assert parts["graph"].level_trims == [{}, {}]


async def test_a_record_states_the_level_match_that_installed_not_the_one_asked():
    """Defense in depth: the record's ``level_matched`` is derived from what
    the graph actually CARRIED, never from what the spec asked.

    A spec can ask for a level match a session was opened with no trims to
    supply — the host refuses that pairing before open, but the engine is a
    separate unit and must be self-consistent however it is reached. Here the
    session holds no trims and the spec asks for a match, so nothing installs;
    the record must say ``level_matched=False`` and carry no numbers rather
    than claim a match its own graph never played. Reading the boolean off the
    installed trims is what keeps it from ever disagreeing with the trims key.
    """
    session, parts = _session()  # holds NO trims

    async with session:
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, level_matched=True,
        ))

    record, = parts["records"].banked
    assert record["level_matched"] is False
    assert "level_match_trims_db" not in record


async def test_a_banked_level_matched_record_says_what_levelled_it():
    """A reverse-null depth is only readable by somebody who knows whether the
    branches were levelled before they were summed, and by how much — so the
    record carries both, and the fingerprint separates it from its unmatched
    twin."""
    session, parts = _session(level_match_trims_db={DRIVER_ROLE_TWEETER: -9.5})

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE, level_matched=True,
        ))

    plain, matched = parts["records"].banked
    assert plain["level_matched"] is False
    # Absent, not empty: a record banked before this existed reads the same way.
    assert "level_match_trims_db" not in plain
    assert matched["level_matched"] is True
    assert matched["level_match_trims_db"] == {DRIVER_ROLE_TWEETER: -9.5}
    assert plain["graph_fingerprint"] != matched["graph_fingerprint"]
    assert plain["kind"] == matched["kind"], "same kind, different level match"


async def test_a_stage_with_no_measurement_graph_refuses_the_level_match():
    """``NoRoutedPhasesGraph`` measures through the APPLIED graph and has no
    per-driver branch to trim. Dropping the request silently would bank an
    unmatched capture under a record claiming a level match."""
    from jasper.active_speaker.crossover_v2.composition import NoRoutedPhasesGraph

    with pytest.raises(ValueError):
        await NoRoutedPhasesGraph().install(
            (), None, {DRIVER_ROLE_TWEETER: -9.5},
        )


async def test_a_banked_inverted_record_says_which_branch_was_flipped():
    """A reverse-null pair is only readable by somebody who knows the sign
    convention it was taken under, so the record carries both halves — and the
    fingerprint distinguishes it from its non-inverted twin."""
    session, parts = _session()

    async with session:
        await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
        await session.measure(MeasureSpec(
            kind=MEASURE_KIND_BASELINE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_TWEETER,
        ))

    normal, flipped = parts["records"].banked
    assert (normal["polarity"], normal["inverted_role"]) == (POLARITY_NORMAL, "")
    assert (flipped["polarity"], flipped["inverted_role"]) == (
        POLARITY_INVERTED, DRIVER_ROLE_TWEETER,
    )
    assert normal["graph_fingerprint"] != flipped["graph_fingerprint"]
    assert normal["kind"] == flipped["kind"], "same kind, different polarity"


async def test_a_stage_with_no_measurement_graph_refuses_the_flip():
    """``NoRoutedPhasesGraph`` measures through the APPLIED graph and has no
    per-driver branch to invert. Dropping the request silently would bank a
    normal capture under an inverted record — the lie S12 exists to refuse."""
    from jasper.active_speaker.crossover_v2.composition import NoRoutedPhasesGraph

    graph = NoRoutedPhasesGraph()

    assert await graph.install() == ""
    with pytest.raises(ValueError):
        await graph.install((DRIVER_ROLE_TWEETER,))
