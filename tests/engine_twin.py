# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A whole tuning session, in memory, with every seam under the test's thumb.

The twin ``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 1 budgets *"as part of the
engine, not as a follow-up"*: **permanent test infrastructure for the permanent
engine** (ruling S4), replacing what ``tests/crossover_v2_fixtures.py`` does for
the 25 of its 26 importers that need a session harness.

The census's blunt version: *"the new engine has to ship a ``FakeSeams``
-equivalent on day one or 54,000 lines of test have nowhere to land."* This is
that equivalent, and it is built to keep — nothing here is scaffolding, and the
old fixture is not deleted by this file. Its importers migrate in waves 2 and 3
as their subjects move, and it dies with its last one.

**What this replaces, and what it does not.** Session CONSTRUCTION only. The old
fixture keeps everything else it does until the wave that moves that subject
arrives, and this module imports nothing from it — a twin that reached back into
the thing it replaces would keep it alive rather than retire it.

**One import, one direction.** This module imports the engine and
:mod:`tests.engine_declarations`. It imports no other test module: the fixture it
replaces reaches into ``tests/test_active_speaker_profile.py`` for its preset,
which is how a fixture library ends up depending on a test file that 39 other
files import. Declarations live in their own module for that reason.

**Everything is a real seam implementation, not a mock.** Each double satisfies
its Protocol structurally and can be driven, scripted, or made to fail — so a
test states the world it wants rather than patching the world it got.

Three knobs the harness exists to give, because they are what the old fixture's
importers actually used it for:

1. **Whole-session construction in one call** — :func:`tuning_session`, with
   ``**kwargs`` passing straight to :class:`~...session.TuningSession`, so a
   test that needs one different declaration writes one keyword.
2. **Seams swapped wholesale or one at a time** — :meth:`FakeSeams.replace`,
   and :func:`dataclasses.replace` works on it for the same reason.
3. **Per-kind playback behaviour** — :attr:`FakePlay.by_kind`, which is where
   the old fixture's swappable check/measure/verify capture factories land now
   that ruling S1 has made those three one verb with a ``kind`` argument.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping, Sequence

from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.playback_transaction import (
    STAGE_RESTORE,
    PlaybackOutcome,
)
from jasper.active_speaker.crossover_v2.session import TuningSession
from jasper.active_speaker.crossover_v2.session_seams import EngineSeams

from tests.engine_declarations import (
    GRAPH_FINGERPRINT,
    SESSION_ID,
    SESSION_VOLUME_DB,
)

__all__ = [
    "FakeGraph",
    "FakePlay",
    "FakeRecommender",
    "FakeRecords",
    "FakeSeams",
    "FakeVolume",
    "GraphInstallFailed",
    "PlayScript",
    "SeamFailure",
    "open_session",
    "tuning_session",
]


class SeamFailure(RuntimeError):
    """What a twin seam raises when a test asks it to fail.

    One type for every seam, so a test asserting "the session let this out"
    cannot pass by catching something the engine itself raised —
    ``SessionStateError`` and ``ValueError`` both mean the engine refused, and
    this means the world did.
    """


class GraphInstallFailed(SeamFailure):
    """An install that failed, possibly after arming half a graph.

    Its own type because the session's failure path treats it specially: the
    graph may be half-installed, so ``restore`` is called even though
    ``install`` never returned.
    """


#: One scripted playback answer: ``(stage_reached, incident)``. The stage is
#: the last stage COMPLETED, which is the engine's own rule — a transaction
#: that failed and then correctly restored reports the stage before the
#: failure, never ``restore``.
PlayScript = tuple[str, str]

_CLEAN: PlayScript = (STAGE_RESTORE, "")


@dataclass
class FakeGraph:
    """The session graph slot: installed once, patched per candidate, restored.

    ``install_raises`` covers the contract hole wave 1a's review found — a
    conforming install may route the tweeter and then fail, so the session
    restores anyway and this counts that restore.
    """

    fingerprint: str = GRAPH_FINGERPRINT
    installs: int = 0
    restores: int = 0
    patches: list[Mapping[str, Any]] = field(default_factory=list)
    install_raises: bool = False
    restore_raises: bool = False

    def install(self) -> str:
        self.installs += 1
        if self.install_raises:
            raise GraphInstallFailed("twin graph install failed")
        return self.fingerprint

    def patch(self, changes: Mapping[str, Any]) -> None:
        self.patches.append(dict(changes))

    def restore(self) -> None:
        self.restores += 1
        if self.restore_raises:
            raise SeamFailure("twin graph restore failed")


@dataclass
class FakeVolume:
    """The volume-claim slot: one claim, proven per stimulus, released once.

    ``readings`` is a sequence because the fact the engine cares about is that
    a claim can be PREEMPTED between two stimuli of one walk — the reason the
    proof moved inside the loop. One entry per ``prove`` call, in order;
    ``proven_db`` answers once the sequence runs out. ``None`` means the fader
    could not be proven, which refuses to bank that stimulus and nothing else.
    """

    proven_db: float | None = SESSION_VOLUME_DB
    readings: list[float | None] = field(default_factory=list)
    acquired: list[float] = field(default_factory=list)
    proves: int = 0
    releases: int = 0
    acquire_raises: bool = False
    release_raises: bool = False

    def acquire(self, level_db: float) -> None:
        self.acquired.append(level_db)
        if self.acquire_raises:
            raise SeamFailure("twin volume acquire failed")

    def prove(self) -> float | None:
        index, self.proves = self.proves, self.proves + 1
        if index < len(self.readings):
            return self.readings[index]
        return self.proven_db

    def release(self) -> None:
        self.releases += 1
        if self.release_raises:
            raise SeamFailure("twin volume release failed")

    @property
    def held(self) -> bool:
        """Is a claim outstanding — acquired more times than released?"""
        return len(self.acquired) > self.releases


@dataclass
class FakeRecords:
    """The record slot: an in-memory bank that reads back.

    :meth:`read` and :meth:`read_state` are what make ``analyze`` an offline
    verb (ruling S3), so the twin implements both rather than stubbing them — a
    test can bank a walk, ``save`` it, drop the session, and rebuild a
    :class:`~jasper.active_speaker.crossover_v2.prior_bank.PriorBank` over the
    result, which is how a candidate-check test states its "before".
    """

    banked: list[Mapping[str, Any]] = field(default_factory=list)
    persisted: list[Mapping[str, Any]] = field(default_factory=list)
    bank_raises: bool = False

    def bank(self, record: Mapping[str, Any]) -> str:
        if self.bank_raises:
            raise SeamFailure("twin record bank failed")
        self.banked.append(dict(record))
        return f"rec-{len(self.banked)}"

    def read(self, record_id: str) -> Mapping[str, Any] | None:
        for index, record in enumerate(self.banked, start=1):
            if record_id == f"rec-{index}":
                return record
        return None

    def persist(self, state: Mapping[str, Any]) -> str:
        self.persisted.append(dict(state))
        return f"state-{len(self.persisted)}"

    def read_state(self, state_id: str) -> Mapping[str, Any] | None:
        for index, state in enumerate(self.persisted, start=1):
            if state_id == f"state-{index}":
                return state
        return None

    def by_position(self, position_deg: int | None) -> list[Mapping[str, Any]]:
        """Every banked record taken at one pose, in bank order."""
        return [r for r in self.banked if r.get("position_deg") == position_deg]

    def kinds(self) -> list[str]:
        """The ``kind`` of every banked record, in bank order."""
        return [str(r.get("kind", "")) for r in self.banked]


@dataclass
class FakePlay:
    """The play transaction: what happens when a stimulus is played.

    Three ways to say it, checked in this order, so a test picks the cheapest
    one that expresses what it means:

    * ``script`` — one answer per call, in order. A mixed walk.
    * ``by_kind`` — one answer per ``MeasureSpec.kind``. This is where the old
      fixture's swappable **check / measure / verify** capture factories land:
      ruling S1 made those three one verb taking a ``kind``, so the triple is a
      mapping now rather than three attributes.
    * ``default`` — everything else. Clean by default, because the interesting
      tests are about the exceptions.

    Every call is recorded whole, so a test can assert what the engine asked
    for — the bearing, the prompt, the declared level, the ladder rung — rather
    than only what it got back.
    """

    default: PlayScript = _CLEAN
    by_kind: dict[str, PlayScript] = field(default_factory=dict)
    script: list[PlayScript] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(
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
            "spec": spec,
            "kind": spec.kind,
            "position_deg": position_deg,
            "prompt": prompt,
            "level_db": level_db,
            "stimulus_dbfs": stimulus_dbfs,
        })
        if index < len(self.script):
            stage, incident = self.script[index]
        else:
            stage, incident = self.by_kind.get(spec.kind, self.default)
        return PlaybackOutcome(stage_reached=stage, incident=incident)

    @property
    def bearings(self) -> list[int | None]:
        """Where each stimulus was played, in order."""
        return [call["position_deg"] for call in self.calls]

    @property
    def rungs(self) -> list[float | None]:
        """Which ladder rung each stimulus was played at, in order."""
        return [call["stimulus_dbfs"] for call in self.calls]


@dataclass
class FakeRecommender:
    """The prescriber seam: records what it was asked over, answers a stub.

    Thin on purpose — §3's wave-3 table says the prescriber is already shipped
    and already decoupled, **do not re-extract it**, so the twin's job is to
    prove the verb reaches it and to let a test see which records it reached
    with.
    """

    answer: Mapping[str, Any] = field(default_factory=dict)
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, record_ids: Sequence[str]) -> Mapping[str, Any]:
        ids = tuple(record_ids)
        self.asked.append(ids)
        return dict(self.answer) if self.answer else {"records": len(ids)}


@dataclass
class FakeSeams:
    """All five seams, each controllable, and the engine's view of them.

    The name ``crossover_v2_fixtures`` gave the same idea, kept because §3
    names it — *"a ``FakeSeams``-equivalent"* — and because the thing it means
    has not changed even though every seam behind it has.

    Construct with no arguments for a session where everything works, or hand
    it whichever doubles a test needs to misbehave. :meth:`replace` swaps one
    or several and returns a new bundle; :func:`dataclasses.replace` does the
    same thing, and both are supported because a test reads better with
    whichever it reaches for.
    """

    graph: FakeGraph = field(default_factory=FakeGraph)
    volume: FakeVolume = field(default_factory=FakeVolume)
    records: FakeRecords = field(default_factory=FakeRecords)
    play: FakePlay = field(default_factory=FakePlay)
    recommend: FakeRecommender = field(default_factory=FakeRecommender)

    def seams(self) -> EngineSeams:
        """The frozen bundle the engine actually takes."""
        return EngineSeams(
            graph=self.graph,
            volume=self.volume,
            records=self.records,
            play=self.play,
            recommend=self.recommend,
        )

    def replace(self, **overrides: Any) -> "FakeSeams":
        """This bundle with some seams swapped, as a new bundle."""
        return replace(self, **overrides)

    @property
    def banked(self) -> list[Mapping[str, Any]]:
        """Shorthand for ``seams.records.banked`` — the assertion tests make
        most often, and the one that reads worst through three dots."""
        return self.records.banked


def tuning_session(
    seams: FakeSeams | None = None,
    *,
    session_id: str = SESSION_ID,
    measurement_level_db: float = SESSION_VOLUME_DB,
    **kwargs: Any,
) -> tuple[TuningSession, FakeSeams]:
    """One CLOSED session and the seams behind it.

    The twin's front door, and the shape the old fixture's ``_conductor``
    established: hand it nothing for a working session, hand it a
    :class:`FakeSeams` to control one. Extra keywords pass straight through to
    :class:`~jasper.active_speaker.crossover_v2.session.TuningSession`, so a
    declaration this signature does not name is still one keyword away and this
    helper never has to grow a parameter to keep up with the engine.

    Returns the session AND its seams, because a test that only got the session
    would have to reach through ``session.seams`` to assert anything — and that
    field is engine-internal by contract.

    **Closed**, not open: opening is the lifetime under test in a good half of
    these, so the caller decides. :func:`open_session` is the open one.
    """
    fakes = seams if seams is not None else FakeSeams()
    session = TuningSession(
        session_id=session_id,
        seams=fakes.seams(),
        measurement_level_db=measurement_level_db,
        **kwargs,
    )
    return session, fakes


@contextmanager
def open_session(
    seams: FakeSeams | None = None, **kwargs: Any,
) -> Iterator[tuple[TuningSession, FakeSeams]]:
    """:func:`tuning_session`, opened and guaranteed closed::

        with open_session() as (session, fakes):
            session.measure(spec)

    A context manager rather than a bare generator, deliberately: a generator
    handed out with ``next()`` is closed by the garbage collector the moment
    the caller drops it, which runs the ``finally`` below and hands the test a
    SPENT session that refuses to measure. The failure is confusing enough
    that offering the shape at all would be a trap.

    Works as a ``pytest`` fixture body unchanged — ``@contextmanager`` wraps a
    generator, and ``pytest`` takes the generator form too.

    The close is in a ``finally`` because a session that dies holding its claim
    leaves the speaker at a measurement level nobody chose — the thing the
    engine spends a ``finally`` on, and the thing a test harness must not undo.
    """
    session, fakes = tuning_session(seams, **kwargs)
    session.open()
    try:
        yield session, fakes
    finally:
        session.close()
