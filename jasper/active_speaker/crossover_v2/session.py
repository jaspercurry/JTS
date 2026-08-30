# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One tuning session: three lifetimes, one verb, and nothing else.

A session holds the seams :mod:`.session_seams` declares, opens them once, and
exposes **``measure``** over them. The wizard front end drives that verb; the
LLM-over-SSH surface reads the bank the verb fills, through the tuning tools.

**``measure`` is ONE verb.** A baseline, a candidate check and a re-measure are
the same method with different arguments — *"measuring is measuring"* — so
there is one implementation and no VERIFY.

**Analysis, recommendation and session-state saving are NOT here.** ADR-0198:
the engine's unwired verb half was deleted, and those three are capabilities of
the doors-and-banks tools instead. A driving caller the doors cannot serve is
what would bring a verb back onto this class.

**One session, one lifetime, one thread.** A session opens once and closes
once; it is not re-openable. The verb holds no lock and is not safe to call
concurrently on one instance — the walk it drives is a sequence of prompted
captures, one at a time, and a lock would be machinery ahead of a caller that
wants it. Two sessions in two threads are fine; they share nothing but the
seams handed to them.

**It does not apply, and it never will.** The apply/rollback transaction is
one *"not a target. Ever."*

**The name.** ``MeasurementSession`` is :mod:`jasper.correction.session`'s and
means something else; ``CrossoverV2Session`` is the flow's god object this
engine replaces. A third spelling of either would read as the same thing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Mapping

from ..volume_latch import fader_matches
from .contracts import DESIGN_AXIS_DEG, POSITION_AXIS_VERTICAL
from .measure_spec import (
    MeasureSpec,
    inverted_roles_for,
    level_trims_for,
    measurement_delays_for,
    stubbed_capabilities,
)
from .playback_transaction import PlaybackOutcome
from .session_seams import EngineSeams
from .spatial import take_id_for

__all__ = [
    "MeasureOutcome",
    "SessionStateError",
    "StimulusOutcome",
    "TuningSession",
]


async def _attach_cleanup_failure(
    primary: BaseException, cleanup: "Callable[[], Awaitable[None]]",
) -> None:
    """Run a cleanup; if it fails, hang the failure on ``primary`` and go on.

    The one place this engine knows the rule *a cleanup failure never replaces
    the failure that caused the cleanup*. An exception raised out of a
    ``finally`` or an ``__aexit__`` demotes the original to ``__context__`` and
    reports the symptom — so the original propagates and the cleanup's failure
    is attached to it instead. The FIRST such failure wins, because the first
    thing to fail while unwinding is the one nearest the cause.

    The broad catch is the point rather than an oversight: a seam may raise
    anything, and there is no narrower type that means "the cleanup failed".
    ``CancelledError`` is deliberately NOT caught here — it is not an
    ``Exception``. On the failed-open path nothing arrives:
    :meth:`TuningSession._release_both_after_failed_open` shields its cleanup
    and attaches any cancellation itself. On the ``__aexit__`` path one CAN
    arrive, because :meth:`TuningSession._release_slots` re-raises the cancel
    it waited out — and there the cancellation replacing the body's exception
    is the right answer, since the caller asked for it.
    """
    try:
        await cleanup()
    except Exception as cleanup_exc:  # noqa: BLE001 - see the docstring
        _attach_first(primary, cleanup_exc)


def _attach_first(primary: BaseException, failure: BaseException) -> None:
    """:func:`_attach_cleanup_failure`'s rule, for a failure already in hand."""
    if primary.__context__ is None:
        primary.__context__ = failure


async def _shielded_cleanup(
    cleanup: "Coroutine[Any, Any, None]",
) -> BaseException | None:
    """Run a cleanup to completion whatever the caller does to us.

    Returns the cancellation that arrived while the cleanup ran, or ``None``.
    Both release paths need the cleanup FINISHED before anything propagates;
    they differ only in what they then do with that cancellation, so that
    decision stays with each of them.

    The house idiom, whose reference form is
    :mod:`jasper.web.correction_crossover_v2_wired`: start the cleanup as a
    TASK, so a cancel aimed at us lands on the shield instead of on the
    cleanup, and keep waiting through a repeat cancel. A bare
    ``await asyncio.shield(coro)`` is a different and weaker thing — it
    detaches the cleanup and lets the cancellation past it, which is how a
    fader ends up stranded at measurement level (ADR-0179).

    An un-cancelled cleanup's own failure propagates from here, because that
    is the exception the caller needs. Once a cancellation has arrived the
    cleanup's failure is dropped in favour of it, the shape
    ``volume_persistence`` states: an acquisition result is secondary to
    caller cancellation.
    """
    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as stop:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - see the docstring
                break
        return stop
    return None


class SessionStateError(RuntimeError):
    """A verb was called against a session lifetime that cannot serve it.

    A programming error, not a measurement one — which is why it raises where a
    measurement problem would return a disclosure.
    """


@dataclass(frozen=True)
class StimulusOutcome:
    """One stimulus: where it played, at what level, and what came of it.

    The unit ``measure`` reports in, because the unit it works in is one
    stimulus — a walk of three positions across two ladder rungs is six of
    these, and a caller that got only a flat list of record ids could not say
    which rung a missing one belonged to.

    Said in the caller's own words and never in the play transaction's: a
    bearing, a level, a record id, a reason code. The stage vocabulary
    (``ready`` … ``restore``) is engine-internal and does not appear here.

    ``record_id`` is ``""`` when nothing was banked, and ``incident`` says why
    when the transaction knows: the stimulus never played, or it played and the
    fader could not be proven for it (:data:`UNPROVEN_LEVEL`).
    """

    position_deg: int | None
    stimulus_dbfs: float | None
    level_db: float | None
    record_id: str
    incident: str = ""

    @property
    def banked(self) -> bool:
        return bool(self.record_id)


#: The session's own reason code for the one refusal it owns: the fader could
#: not be proven for this stimulus, so the capture is not banked. MS-14's
#: refusal, in ruling S10's shape — the stimulus still played, and the session
#: still measures again.
UNPROVEN_LEVEL = "unproven_level"


@dataclass(frozen=True)
class MeasureOutcome:
    """What one ``measure`` produced, one entry per stimulus.

    ``stimuli`` is the whole answer, in the order the spec named: every
    position crossed with every ladder rung. ``record_ids`` is the convenience
    view over it — the ids that were actually banked — and is derived, never a
    second list to keep in step.

    ``record_ids`` is shorter than ``stimuli`` whenever a stimulus banked
    nothing, and each such entry says why in its own ``incident``. Three causes
    reach it, and a caller that confuses them misdiagnoses the session: a stub
    stopped the whole call before anything played (``stimuli`` is then empty);
    the play transaction did not complete ``play`` for that stimulus; or it did
    play and the fader could not be proven for it. The last is MS-14 refusing
    to CLAIM, and it is the one that leaves a played stimulus with no record.
    """

    spec: MeasureSpec
    stimuli: tuple[StimulusOutcome, ...]

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(s.record_id for s in self.stimuli if s.record_id)



@dataclass
class TuningSession:
    """One session's lifetimes and the ``measure`` verb over them.

    Construct with an identity, the injected :class:`~.session_seams.EngineSeams`
    and the ONE declared level every stimulus in this session plays at. Use it as
    an async context manager, or call :meth:`open` and :meth:`close` — the second
    exists because a web front end's lifetime is a sequence of HTTP requests and
    cannot hold an ``async with``.

    ``seams`` is public because construction and testing need it, and it is
    **engine-internal** — see :class:`~.session_seams.EngineSeams` for why
    reaching through it is a front end doing engine work.
    """

    session_id: str
    seams: EngineSeams
    #: The one level every stimulus plays at, and the one the volume claim is
    #: taken for. Ruling S8's recipe turns on it being ONE: *"same drive
    #: voltage across every per-driver measurement; no gain is touched between
    #: them."* A level ladder moves the stimulus, never this.
    measurement_level_db: float
    #: The per-role attenuation a ``level_matched`` spec's graph carries, in dB
    #: and never positive. Resolved ONCE by the host that opened this session,
    #: from the box's own banked evidence — the session applies it and never
    #: derives it, which is what keeps one speaker's level match off another
    #: speaker's measurement. Empty means no spec here may ask for one; the
    #: host refuses that pairing at open, where an operator can still act on
    #: it, so :func:`~.measure_spec.level_trims_for` answers empty rather than
    #: raising mid-walk.
    level_match_trims_db: Mapping[str, float] = field(default_factory=dict)

    _graph_installed: bool = field(default=False, init=False)
    _volume_held: bool = field(default=False, init=False)
    _spent: bool = field(default=False, init=False)
    _graph_fingerprint: str = field(default="", init=False)
    _banked: list[str] = field(default_factory=list, init=False)
    #: How many takes this session has minted an id for. The ordinal in
    #: :meth:`_next_take_id`, and deliberately in memory only — a persisted
    #: registry of minted ids would be a second index over the bank, which the
    #: one-index rule forbids.
    _takes_minted: int = field(default=0, init=False)

    # ---------------------------------------------------------------- lifetime

    async def open(self) -> None:
        """Open the two held slots, in the order their contracts require.

        **The claim goes first, and the order is :meth:`close`'s mirror.** The
        teardown puts the graph back BEFORE the fader comes down, so setup
        takes the fader before the graph goes in — reverse order of taking is
        what makes the pair a stack rather than two independent lifetimes.

        It is also the cheaper failure. A volume that will not establish is not
        a volume anything may be admitted against, so a session that installed
        the measurement graph first would buy two CamillaDSP swaps — install
        and restore — for a session that never plays a stimulus. Acquiring
        first means a refused volume costs zero graph operations, which is the
        property NB1 pins one frame up in the wizard's own open arm.

        **An open that fails puts back everything it took, including the half a
        failing call may have armed.** ``install`` is inside the guard as well
        as ``acquire``: a conforming install that routes the tweeter and then
        raises has left a graph nobody holds, and a session that skipped the
        restore because the call raised would leave the box that way. Both
        seams' release halves are idempotent and safe against nothing-held for
        exactly this path.

        **The guard catches ``BaseException``, and that is the cancellation
        half of the same rule.** Both awaits below are cancel points, and a
        ``CancelledError`` is not an ``Exception`` — so an ``except Exception``
        here would let a cancel land on an acquire that had already registered
        the claim and skip the give-back entirely, leaving the fader at
        measurement level with nothing marked held for a later :meth:`close` to
        release. The bare ``raise`` still propagates the cancellation; the
        acquire itself stays unshielded, because an acquire a caller no longer
        wants should not finish (ADR-0179).

        The record store has no open: it is a sink whose lifetime IS this
        session's, and the id every record carries is the key that says so.

        **Not re-openable.** A session that has been closed is spent, and
        opening it again raises. A second session over the first one's evidence
        reads that bank through the tuning tools rather than re-entering it.
        """
        if self._spent:
            raise SessionStateError(
                f"session {self.session_id} has been closed — a session opens "
                "once; rebuilding one over an existing bank is not this wave's"
            )
        if self.is_open:
            raise SessionStateError(f"session {self.session_id} is already open")
        try:
            await self.seams.volume.acquire(self.measurement_level_db)
            self._volume_held = True
            self._graph_fingerprint = await self.seams.graph.install()
            self._graph_installed = True
        except BaseException as opening_exc:  # noqa: BLE001 - re-raised below
            await self._release_both_after_failed_open(opening_exc)
            raise

    async def close(self) -> None:
        """Release both held slots, even if releasing one raises.

        Idempotent, and **re-attemptable per slot**: a release that raises
        leaves that slot still marked held, so a second :meth:`close` tries it
        again rather than treating the failure as done. A leaked graph or a
        stranded fader claim is worth a second attempt — the fader especially,
        because a session that dies holding one leaves the speaker at a
        measurement level nobody chose.

        Closing an already-closed session is not an error; that is what an
        ``__aexit__`` after an explicit :meth:`close` does.
        """
        self._spent = True
        await self._release_slots()

    async def __aenter__(self) -> "TuningSession":
        await self.open()
        return self

    async def __aexit__(
        self, _exc_type: object, exc_value: BaseException | None, _tb: object,
    ) -> None:
        """Close, without letting a close-time failure hide the real one.

        A raise from inside the ``async with`` body is the failure the caller
        needs to see. Letting a close failure propagate on top of it would
        demote the original to ``__context__`` and report the symptom instead —
        so when something is already in flight the close failure is attached to
        it and the original goes on. With nothing in flight, a close failure IS
        the failure and propagates normally.
        """
        if exc_value is None:
            await self.close()
            return
        await _attach_cleanup_failure(exc_value, self.close)

    @property
    def is_open(self) -> bool:
        return self._graph_installed or self._volume_held

    @property
    def graph_fingerprint(self) -> str:
        """Which graph this session's evidence was measured through.

        Provenance on a record, never a gate — ``""`` when the host could not
        name it, and ``""`` again after an open that failed, because the graph
        it named was restored.
        """
        return self._graph_fingerprint

    @property
    def banked_record_ids(self) -> tuple[str, ...]:
        return tuple(self._banked)

    # ------------------------------------------------------------------- verbs

    async def measure(self, spec: MeasureSpec) -> MeasureOutcome:
        """Measure what the spec asks for: every position, at every rung.

        One verb for all three kinds. The order is fixed and each step is
        somebody's invariant:

        1. **Stubs first** (ruling S12). A capability this engine has not built
           is named before anything plays, and a stub whose ``captured`` is
           ``False`` means there is no stimulus to play at all — so the session
           says what it cannot do instead of quietly measuring something else.
           Such a stub ABORTS the call: ``stimuli`` is empty and nothing played.
        2. **One play transaction per stimulus**, ready → admit → lock → play →
           restore, behind :mod:`.playback_transaction` — and **the graph is
           proven-or-reinstalled immediately before each one** (MS-13/S6: the
           idempotent ``install`` IS the health check), because between two
           stimuli another DSP writer may have replaced it. The record's
           ``graph_fingerprint`` is that prove's answer. The unit is position ×
           ladder rung: a ladder moves the stimulus level, never the claim.
        3. **The level is proven per stimulus** (MS-14), immediately before that
           stimulus's transaction. A claim can be preempted between two
           positions of one walk, so a single proof taken before the walk would
           stamp an unverified level into every record after it. An unproven
           fader refuses to BANK that stimulus and nothing else — ruling S10's
           *refusing to WORK dies; refusing to CLAIM stays* — so the stimulus
           still plays, the next rung is still attempted, and the entry says
           :data:`UNPROVEN_LEVEL`.
        4. **Bank what played.** A transaction that never completed ``play`` has
           no evidence, and banking a record for it would be the dishonest kind
           of completeness.
        """
        self._require_open()
        if any(not stub.captured for stub in stubbed_capabilities(spec)):
            return MeasureOutcome(spec=spec, stimuli=())

        prompts = spec.pose_prompts
        rungs: tuple[float | None, ...] = spec.level_ladder_dbfs or (None,)

        stimuli: list[StimulusOutcome] = []
        for index, bearing in enumerate(self._bearings(spec)):
            prompt = prompts[index] if index < len(prompts) else ""
            for stimulus_dbfs in rungs:
                stimulus = await self._one_stimulus(
                    spec, bearing, prompt, stimulus_dbfs,
                )
                stimuli.append(stimulus)
                # Accounted as each one banks, never after the walk: every
                # stimulus is a cancel point, and a record written to the store
                # but missing from this list is one `banked_record_ids` denies —
                # evidence on disk that the session says it never took.
                if stimulus.record_id:
                    self._banked.append(stimulus.record_id)

        return MeasureOutcome(spec=spec, stimuli=tuple(stimuli))

    # --------------------------------------------------------------- internals

    async def _release_both_after_failed_open(
        self, opening_exc: BaseException,
    ) -> None:
        """Give back both halves after an :meth:`open` that failed part-way.

        **Unconditional, and that is the point.** A seam that raised may have
        armed half of what it was asked for — an ``install`` that routed the
        tweeter before failing, an ``acquire`` that registered the claim before
        failing — and the session cannot see how far either got. Both release
        halves are contracted idempotent and safe against nothing-held for
        exactly this call, the same shape MS-11 gives the fan-in gate, where an
        *indeterminate* select still releases.

        A cleanup that itself fails is attached to ``opening_exc`` rather than
        raised over it: the exception :meth:`open` is already propagating names
        the real cause, and replacing it with a symptom from the cleanup would
        report the wrong thing. The volume's failure is the one attached when
        both fail — it is the slot whose loss is audible.

        **A cancellation arriving here is a cleanup failure, not the answer**,
        and is attached exactly as a raising release would be: letting one past
        would replace the very exception this function exists to preserve.
        """
        cancelled = await _shielded_cleanup(self._give_back_both(opening_exc))
        if cancelled is not None:
            _attach_first(opening_exc, cancelled)
        self._volume_held = False
        self._graph_installed = False

    async def _give_back_both(self, opening_exc: BaseException) -> None:
        """Both releases, unconditionally, each attaching its own failure.

        **Volume first here, graph first in :meth:`_give_back_held`, and the
        asymmetry is deliberate.** This list is ordered by which failure the
        caller should be told about — ``_attach_first`` keeps the earliest, and
        the volume's is the one to keep because its loss is audible. The
        teardown's order is about which WRITE lands first, where the graph must
        go back before the fader moves. Nothing here is arming a live
        pipeline: both releases run unconditionally against seams that may have
        armed nothing, so their order carries no isolation meaning.
        """
        for release in (self.seams.volume.release, self.seams.graph.restore):
            await _attach_cleanup_failure(opening_exc, release)

    async def _release_slots(self) -> None:
        """Give back whatever is still held, in reverse order of taking.

        Each slot's flag clears only once its release RETURNS, so a release
        that raised is attempted again by the next :meth:`close`. The volume's
        release runs in a ``finally`` so a raising graph restore cannot skip
        it, and the volume's exception is the one that reaches the caller —
        it is the slot whose loss is audible.

        **Shielded, and the cancellation still propagates.** A cancelling
        caller waits for both releases before it gets its ``CancelledError``:
        the caller that cancelled is not the one who has to hear a fader left
        at measurement level.
        """
        cancelled = await _shielded_cleanup(self._give_back_held())
        if cancelled is not None:
            raise cancelled

    async def _give_back_held(self) -> None:
        """The ordered give-back :meth:`_release_slots` shields.

        **Graph first, and it is reverse order of taking now that the claim is
        taken first.** It is also the isolation order the wizard's own teardown
        keeps: the fader release lands the household level, and a graph put
        back after that would swap the pipeline under audio already at a level
        the household can hear.

        The volume release still runs in the ``finally``, so a graph that will
        not come back cannot strand the fader at measurement level, and the
        volume's exception is still the one that reaches the caller — it is the
        slot whose loss is audible.
        """
        try:
            if self._graph_installed:
                await self.seams.graph.restore()
                self._graph_installed = False
        finally:
            if self._volume_held:
                await self.seams.volume.release()
                self._volume_held = False

    def _bearings(self, spec: MeasureSpec) -> tuple[int | None, ...]:
        """Where this spec measures, with the design axis spelled once.

        A spec naming no position measures the design axis, and the design axis
        is :data:`~.contracts.DESIGN_AXIS_DEG` — the same ``0`` that
        ``spatial._DESIGN_AXIS_GEOMETRY`` uses for a capture with no prompted
        move. So ``positions=()`` and ``positions=(0,)`` are one pose and one
        record, not two spellings of the same place.

        On the vertical axis this rig commands no bearing at all, so an
        unpositioned vertical walk is ``None`` rather than a ``0``. That
        distinction is load-bearing now that a vertical spec captures: ``None``
        is what keeps a raised take out of every pooled bearing set downstream
        (``evidence_packet._angle_deg_block`` is the one a reader sees), where a
        ``0`` would join the horizontal seats as "on the design axis". Where the
        microphone was raised to rides ``vertical_deg`` instead.
        """
        if spec.positions:
            return spec.positions
        if spec.position_axis == POSITION_AXIS_VERTICAL:
            return (None,)
        return (DESIGN_AXIS_DEG,)

    async def _one_stimulus(
        self,
        spec: MeasureSpec,
        bearing: int | None,
        prompt: str,
        stimulus_dbfs: float | None,
    ) -> StimulusOutcome:
        """Prove the graph, prove the level, play, and bank exactly one stimulus.

        **The graph is proven per stimulus, not only at open** — the design's
        own *"install once — and the idempotent install IS the health check"*
        (MS-13, ruling S6). Between two stimuli the writer lock is released
        and arbitrary time passes, so another DSP writer may have replaced the
        running graph; ``install()`` is the install-or-prove that puts it back
        (ruling S10's shape: repair and disclose, never refuse to play). The
        fingerprint the record carries is THIS prove's answer, so a record
        names the graph its own stimulus actually played through rather than
        the one open() installed. A stage bound to
        ``composition.NoRoutedPhasesGraph`` answers ``""`` throughout, which
        is the same honest "no graph to name" it answered at open.

        **The polarity variant is chosen HERE, at the same call** (R-1). The
        flip lives in the graph's per-driver branch, so a spec asking for an
        inverted capture installs a different graph — and the fingerprint this
        prove answers with is that graph's, which is what keeps the record's
        provenance true of the stimulus it actually played.
        """
        self._graph_fingerprint = await self.seams.graph.install(
            inverted_roles_for(spec),
            measurement_delays_for(spec),
            level_trims_for(spec, self.level_match_trims_db),
        )
        proven_level_db = await self._proven_level()
        outcome: PlaybackOutcome = await self.seams.play.run(
            spec=spec,
            position_deg=bearing,
            prompt=prompt,
            level_db=self.measurement_level_db,
            stimulus_dbfs=stimulus_dbfs,
        )
        record_id = ""
        incident = outcome.incident
        if outcome.played:
            if proven_level_db is None:
                incident = incident or UNPROVEN_LEVEL
            else:
                record_id = await self.seams.records.bank(self._record(
                    spec, bearing, prompt, stimulus_dbfs, outcome,
                    proven_level_db, self._next_take_id(spec.kind),
                ))
        return StimulusOutcome(
            position_deg=bearing, stimulus_dbfs=stimulus_dbfs,
            level_db=proven_level_db, record_id=record_id, incident=incident,
        )

    def _next_take_id(self, kind: str) -> str:
        """This session's next take id — the name the store files a record by.

        **The engine holds no position identity, and this is the consequence.**
        :class:`~.measure_spec.MeasureSpec` names bearings, prompts and rungs;
        it carries no position id, no take id and no attempt, and there is no
        retake concept here at all — a re-measure is another :meth:`measure`
        call. The one index :meth:`measure` does hold, its ``enumerate`` over
        the bearings, is per POSITION and not per record: an inner ladder of
        rungs makes several records under one of them. So nothing in reach
        identifies a take, and a record banked without a name is a record the
        store cannot file.

        What it mints instead is ``entry_baseline_record``'s precedent, which
        solved this exact shape for the one other capture with no prompted
        spot: a position id built from WHAT the take is plus an ordinal —
        ``f"{kind}_{n:02d}"`` — run through :func:`~.spatial.take_id_for`, the
        repo's one spelling of a take id. Minting the string here instead would
        be a fifth copy of that convention.

        ``n`` counts takes minted by THIS session, in memory, so two records of
        one session never collide however many specs or rungs produced them.
        Nothing wider is claimed: uniqueness across sessions is the store's
        relay-scoped path, not a name.

        The attempt is ``0`` on every engine take, and truthfully — the suffix
        exists because a geometry RETAKE reuses its position id, and this
        session's ordinal has already moved on by then.
        """
        ordinal = self._takes_minted
        self._takes_minted += 1
        return take_id_for(f"{kind}_{ordinal:02d}", 0)

    async def _proven_level(self) -> float | None:
        """This stimulus's fader level, or ``None`` when it is not proven.

        :meth:`~.session_seams.VolumeClaim.prove` is contracted to return a
        reading only when it AGREES with the declared level, so this re-checks
        the answer against that level rather than trusting it. The check is
        :func:`~jasper.active_speaker.volume_latch.fader_matches` — the repo's
        one *"do these two fader dB values agree?"* test, at the confirm
        tolerance wave 5 collapses every other writer onto.

        Not defensive decoration: a number that disagrees with the level the
        program was admitted against is the 8.712 dB incident's exact shape —
        two fields of one block saying different things — and the cost of
        catching it here is one comparison against the invariant this session
        already declares. Banking the level is then banking ONE number that
        both the stimulus and the record agree on.
        """
        reading = await self.seams.volume.prove()
        if reading is None or not fader_matches(reading, self.measurement_level_db):
            return None
        return float(reading)

    def _require_open(self) -> None:
        if not self.is_open:
            raise SessionStateError(
                f"session {self.session_id} must be open to measure — its graph "
                "is proven and its level claimed at open()"
            )

    def _record(
        self,
        spec: MeasureSpec,
        bearing: int | None,
        prompt: str,
        stimulus_dbfs: float | None,
        outcome: PlaybackOutcome,
        proven_level_db: float,
        take_id: str,
    ) -> Mapping[str, Any]:
        """One stimulus, as the facts wave 4's five blocks are built around.

        Wave 4j's index reads six of these — session, kind, position, candidate,
        timestamp, path — and the store supplies the last two, since only it
        knows where it put the record and when. The rest are here because they
        are what a reader needs to tell two captures of the same position apart:
        which regime, which polarity, which ladder rung, which graph, at what
        proven level.

        ``polarity`` and ``inverted_role`` travel together for the reason
        :class:`~.measure_spec.MeasureSpec` checks them together: a reverse-null
        pair is only comparable to a reader that knows WHICH branch was flipped,
        and *"inverted"* alone does not say. ``""`` on every normal capture.

        Wave 4 adds the five blocks around these; nothing here re-derives a
        curve, and nothing here is a second store — the banked files stay the
        single source of truth and the index stays rebuildable by rescanning
        them.

        ``baseline_record_id`` is the "before" THIS capture is meant to be
        compared against, named on the record rather than left for a reader to
        infer. This session names none: pairing a capture with its comparand is
        the tuning tools' read over the bank (ADR-0198), so the key rides as
        ``""`` — the same honest empty the schema always allowed — rather than
        disappearing and moving every reader's shape.

        ``wav_path`` is the record → capture pointer, and it is what makes a
        capture reachable from a banked record at all. Taken from the
        transaction that played, because that is the only party that can say it:
        a bundle-relative capture path is NOT derivable from the take id —
        ``bundles.capture_artifact_relpath`` appends a ``uuid4`` hex, and its
        caller mints the path BEFORE the write precisely so the record can carry
        it. ``""`` when no bytes were placed, said plainly.

        ``take_id`` is what the store files this record BY, minted by
        :meth:`_next_take_id` at bank time rather than derived here — see there
        for why the engine has no position identity to derive one from. A
        record without it is a record the store cannot place.
        """
        # Asked through the ONE translation the install used, never re-derived
        # from the flag: the record then states the trims the stimulus actually
        # played through rather than a second answer to the same question.
        applied_trims = level_trims_for(spec, self.level_match_trims_db)
        return {
            "session_id": self.session_id,
            "take_id": take_id,
            "kind": spec.kind,
            "baseline_record_id": "",
            "position_deg": bearing,
            "position_axis": spec.position_axis,
            "vertical_deg": spec.vertical_deg,
            "prompt": prompt,
            "candidate_id": spec.candidate_id,
            "regime": spec.regime,
            "polarity": spec.polarity,
            "inverted_role": spec.inverted_role,
            # Derived from what INSTALLED, not from what the spec ASKED: a spec
            # can ask for a level match the session was opened with no trims to
            # supply (``level_trims_for`` answers empty then), and a record
            # that read ``level_matched`` off the flag would claim a match its
            # own graph did not carry. Reading it off ``applied_trims`` — the
            # same value the trims key below is gated on — makes the boolean
            # and the numbers one fact that cannot disagree, however the engine
            # was reached.
            "level_matched": bool(applied_trims),
            # The numbers only when there ARE numbers, on ``vertical_deg``'s
            # terms: an absent key reads as the un-matched capture every
            # record banked before this existed was, so no schema moves.
            **(
                {"level_match_trims_db": applied_trims}
                if applied_trims
                else {}
            ),
            "graph_fingerprint": self._graph_fingerprint,
            "level_db": proven_level_db,
            "stimulus_dbfs": stimulus_dbfs,
            "incident": outcome.incident,
            "wav_path": outcome.wav_path,
        }
