# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One tuning session: three lifetimes, four verbs, and nothing else.

The engine ``docs/REFACTOR-TUNING-2026-08.md`` §1 draws. A session holds the
three seams :mod:`.session_seams` declares, opens them once, and exposes ruling
S1's four verbs over them: **``measure`` · ``analyze`` · ``recommend`` ·
``save``.** Both front ends — the ``/correction/`` wizard, where a person moves
the microphone, and the LLM-plus-arm runner, where the arm does — call these
four and nothing else. *"If a change has to be made in both, it belongs in the
engine."*

**``measure`` is ONE verb.** A baseline, a candidate check and a re-measure are
the same method with different arguments — *"measuring is measuring"* — so
there is one implementation and no VERIFY. What today's code calls VERIFY is
``measure`` with :data:`~.contracts.MEASURE_KIND_VERIFY` plus ``analyze``.

**One session, one lifetime, one thread.** A session opens once and closes
once; it is not re-openable. Measuring against a previous session's evidence is
a different act with a different type — :class:`~.prior_bank.PriorBank`, handed
in at construction — because the two sides of a round sit on two graphs with an
irreversible apply between them. The
verbs hold no lock and are not safe to call concurrently on one instance — the
walk they drive is a sequence of prompted captures, one at a time, and a lock
would be machinery ahead of a caller that wants it. Two sessions in two threads
are fine; they share nothing but the seams handed to them.

**What this class deliberately does NOT do yet.** The skeleton is seams and
contracts; the waves fill them.

* **It plays nothing and installs nothing.** Every side effect crosses an
  injected seam, and no production implementation of any of them exists yet:
  the graph is wave 6, the volume claim wave 5, the record store waves 3 and 4,
  the play transaction wave 2.
* **``analyze`` runs no analysis.** §1's wholesale default — *every* analysis
  whose input kinds are present in the bank — lands in wave 2, in the CALLER.
  The 92 analysis units port whole and unedited; what changes is that something
  finally calls all of them. Today ``analyze`` reports only what this session
  could not do, which is the half of its contract that ships with the surface.
* **It does not apply, and it never will.** The apply/rollback transaction is
  §3's one *"not a target. Ever."*

**The name.** ``MeasurementSession`` is :mod:`jasper.correction.session`'s and
means something else; ``CrossoverV2Session`` is the flow's god object this
engine replaces. A third spelling of either would read as the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .contracts import DESIGN_AXIS_DEG, POSITION_AXIS_VERTICAL
from .measure_spec import CapabilityStub, MeasureSpec, stubbed_capabilities
from .playback_transaction import PlaybackOutcome
from .prior_bank import CapturePose, PriorBank
from .session_seams import EngineSeams

__all__ = [
    "AnalyzeOutcome",
    "MeasureOutcome",
    "RecommendOutcome",
    "SaveOutcome",
    "SessionStateError",
    "StimulusOutcome",
    "TuningSession",
]


def _attach_cleanup_failure(
    primary: BaseException, cleanup: "Callable[[], None]",
) -> None:
    """Run a cleanup; if it fails, hang the failure on ``primary`` and go on.

    The one place this engine knows the rule *a cleanup failure never replaces
    the failure that caused the cleanup*. An exception raised out of a
    ``finally`` or an ``__exit__`` demotes the original to ``__context__`` and
    reports the symptom — so the original propagates and the cleanup's failure
    is attached to it instead. The FIRST such failure wins, because the first
    thing to fail while unwinding is the one nearest the cause.

    The broad catch is the point rather than an oversight: a seam may raise
    anything, and there is no narrower type that means "the cleanup failed".
    """
    try:
        cleanup()
    except Exception as cleanup_exc:  # noqa: BLE001 - see the docstring
        if primary.__context__ is None:
            primary.__context__ = cleanup_exc


def _merge_disclosures(
    stubs: "Iterable[CapabilityStub]",
) -> list[CapabilityStub]:
    """Each hole once, in the order it was first hit, keeping the best news.

    A hole reported as having captured nothing is UPGRADED when a later entry
    for the same code did bank a capture — otherwise a session whose first
    near-field spec was aborted by a sibling stub would keep saying there is no
    evidence waiting, long after a second spec put some in the bank. The
    reverse never happens: a hole that has banked evidence does not stop having
    it.

    One implementation, used for both the merges the engine does: the running
    one a session accumulates over its own specs, and the read-time one
    ``analyze`` does across a prior bank and this session.
    """
    at: dict[str, int] = {}
    merged: list[CapabilityStub] = []
    for stub in stubs:
        index = at.get(stub.code)
        if index is None:
            at[stub.code] = len(merged)
            merged.append(stub)
        elif stub.captured and not merged[index].captured:
            merged[index] = stub
    return merged


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
    disclosures: tuple[CapabilityStub, ...]

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(s.record_id for s in self.stimuli if s.record_id)


@dataclass(frozen=True)
class AnalyzeOutcome:
    """Every analysis the bank supported, and every one it did not.

    ``results`` is keyed by analysis name. ``disclosures`` is the half ruling
    S12 and §1's second property share: *a missing input is DISCLOSED, never
    silently skipped* — because silence is what let the current defects hide.

    **Read an empty ``results`` as "nothing is wired to run yet", never as
    "everything ran and found nothing".** Wave 2 lands the wholesale registry in
    :meth:`TuningSession.analyze` and, with it, the per-analysis not-run
    disclosure §1 words as *"no distortion analysis: no distortion-vs-level
    capture in this session"*. Naming that shape before the registry exists
    would be guessing at it.

    ``results`` is a mapping the caller must treat as read-only. A frozen
    dataclass freezes rebinding, not the object bound — wave 2's registry
    should hand this a copy rather than the dict it is still filling.
    """

    results: Mapping[str, Any]
    disclosures: tuple[CapabilityStub, ...]


@dataclass(frozen=True)
class RecommendOutcome:
    """What the prescriber answered, and over which banked records."""

    recommendation: Mapping[str, Any]
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class SaveOutcome:
    """The persisted session state, and everything it accounts for."""

    state_id: str
    record_ids: tuple[str, ...]


@dataclass
class TuningSession:
    """One session's lifetimes and the four verbs over them.

    Construct with an identity, the injected :class:`~.session_seams.EngineSeams`,
    the ONE declared level every stimulus in this session plays at, and — for a
    session that grades against a previous one — that session's
    :class:`~.prior_bank.PriorBank`. Use it as a context manager, or call
    :meth:`open` and :meth:`close` — the second exists because a web front end's
    lifetime is a sequence of HTTP requests and cannot hold a ``with``.

    Four declarations and five fields of state. The 102-attribute session this
    replaces is what happens when a class accumulates the answers instead of the
    seams.

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
    #: The previous session's bank this one measures against, or ``None`` for a
    #: session with no "before" — a first-ever round, or a baseline session that
    #: is itself the before. Read-only: a session banks into its own store and
    #: never into a prior one. See :class:`~.prior_bank.PriorBank`.
    prior: PriorBank | None = None

    _graph_installed: bool = field(default=False, init=False)
    _volume_held: bool = field(default=False, init=False)
    _spent: bool = field(default=False, init=False)
    _graph_fingerprint: str = field(default="", init=False)
    _banked: list[str] = field(default_factory=list, init=False)
    _disclosures: list[CapabilityStub] = field(default_factory=list, init=False)

    # ---------------------------------------------------------------- lifetime

    def open(self) -> None:
        """Open the two held slots, in the order their contracts require.

        The graph goes in first and its proof runs once, before anything can
        play (MS-13); the volume claim is taken at the declared level second, so
        the fader the graph will be measured through is already this session's.

        **An open that fails puts back everything it took, including the half a
        failing call may have armed.** ``install`` is inside the guard as well
        as ``acquire``: a conforming install that routes the tweeter and then
        raises has left a graph nobody holds, and a session that skipped the
        restore because the call raised would leave the box that way. Both
        seams' release halves are idempotent and safe against nothing-held for
        exactly this path.

        The record store has no open: it is a sink whose lifetime IS this
        session's, and the id every record carries is the key that says so.

        **Not re-openable.** A session that has been closed is spent, and
        opening it again raises. A second session over the first one's evidence
        is :class:`~.prior_bank.PriorBank`, and it reads that bank rather than
        re-entering it.
        """
        if self._spent:
            raise SessionStateError(
                f"session {self.session_id} has been closed — a session opens "
                "once; rebuilding one over an existing bank is not this wave's"
            )
        if self.is_open:
            raise SessionStateError(f"session {self.session_id} is already open")
        try:
            self._graph_fingerprint = self.seams.graph.install()
            self._graph_installed = True
            self.seams.volume.acquire(self.measurement_level_db)
            self._volume_held = True
        except Exception as opening_exc:  # noqa: BLE001 - re-raised below
            self._graph_fingerprint = ""
            self._release_both_after_failed_open(opening_exc)
            raise

    def close(self) -> None:
        """Release both held slots, even if releasing one raises.

        Idempotent, and **re-attemptable per slot**: a release that raises
        leaves that slot still marked held, so a second :meth:`close` tries it
        again rather than treating the failure as done. A leaked graph or a
        stranded fader claim is worth a second attempt — the fader especially,
        because a session that dies holding one leaves the speaker at a
        measurement level nobody chose.

        Closing an already-closed session is not an error; that is what an
        ``__exit__`` after an explicit :meth:`close` does.
        """
        self._spent = True
        self._release_slots()

    def __enter__(self) -> "TuningSession":
        self.open()
        return self

    def __exit__(
        self, _exc_type: object, exc_value: BaseException | None, _tb: object,
    ) -> None:
        """Close, without letting a close-time failure hide the real one.

        A raise from inside the ``with`` body is the failure the caller needs
        to see. Letting a close failure propagate on top of it would demote the
        original to ``__context__`` and report the symptom instead — so when
        something is already in flight the close failure is attached to it and
        the original goes on. With nothing in flight, a close failure IS the
        failure and propagates normally.

        With nothing in flight, a close failure IS the failure and propagates
        normally.
        """
        if exc_value is None:
            self.close()
            return
        _attach_cleanup_failure(exc_value, self.close)

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

    def measure(self, spec: MeasureSpec) -> MeasureOutcome:
        """Measure what the spec asks for: every position, at every rung.

        One verb for all three kinds. The order is fixed and each step is
        somebody's invariant:

        1. **Stubs first** (ruling S12). A capability this engine has not built
           is named before anything plays, and a stub whose ``captured`` is
           ``False`` means there is no stimulus to play at all — so the session
           says what it cannot do instead of quietly measuring something else.
           When such a stub wins, every stub returned alongside it is
           re-rendered as having captured nothing, because none of them did.
        2. **One play transaction per stimulus**, ready → admit → lock → play →
           restore, behind :mod:`.playback_transaction`. The unit is position ×
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

        Disclosures accumulate onto the session, deduplicated by code, so
        :meth:`analyze` reports each hole once whether or not the caller kept
        this outcome and however many specs tripped it.
        """
        self._require_open()
        stubs = stubbed_capabilities(spec)
        if any(not stub.captured for stub in stubs):
            aborted = tuple(stub.aborted() for stub in stubs)
            self._disclose(aborted)
            return MeasureOutcome(spec=spec, stimuli=(), disclosures=aborted)
        self._disclose(stubs)

        prompts = spec.pose_prompts
        rungs: tuple[float | None, ...] = spec.level_ladder_dbfs or (None,)

        stimuli: list[StimulusOutcome] = []
        for index, bearing in enumerate(self._bearings(spec)):
            prompt = prompts[index] if index < len(prompts) else ""
            for stimulus_dbfs in rungs:
                stimuli.append(self._one_stimulus(
                    spec, bearing, prompt, stimulus_dbfs,
                ))

        self._banked.extend(s.record_id for s in stimuli if s.record_id)
        return MeasureOutcome(
            spec=spec, stimuli=tuple(stimuli), disclosures=stubs,
        )

    def analyze(self) -> AnalyzeOutcome:
        """Every analysis the banked records support — and every one they do not.

        **Deliberately not gated on an open session.** Ruling S3's whole return
        on banking complete records is that a session can be re-analyzed offline
        forever, by analyses that did not exist when it was captured. A verb
        that needed a live graph and a held fader to read a file would give that
        back, and :meth:`~.session_seams.RecordStore.read` is the door that
        makes it reachable.

        **Today it runs nothing.** §1's wholesale default is wave 2's, and it
        lands in this method rather than in any analysis unit — *do not decouple
        the analysis layer, replace its caller.* What ships now is the honest
        half: the capability holes this session hit, reported rather than
        skipped.

        **A prior bank's own disclosures are reported too, and first.** They are
        the capability holes the PRIOR session hit, re-rendered in this build's
        wording, because a round's evidence is both sides of it. Prior first
        because it happened first; a hole both sides hit is named once, and the
        side that banked a capture for it wins.

        **OWED, and not built here: a MISSING "before" is not disclosed.** A
        session with no prior, or a capture whose pose the prior never
        baselined, reports nothing about it — the record says so in its empty
        ``baseline_record_id`` and this verb stays silent. That disclosure
        belongs with the verdict analysis that would consume the comparand, and
        it lands in the wave that builds it. Naming a missing input is §1's
        rule; this method does not keep it yet, and says so rather than
        implying it does.
        """
        prior = self.prior.disclosures if self.prior is not None else ()
        return AnalyzeOutcome(
            results={},
            disclosures=tuple(_merge_disclosures([*prior, *self._disclosures])),
        )

    def recommend(self) -> RecommendOutcome:
        """Ask the prescriber what to do about everything banked so far.

        One word for what the code has called propose, prescribe and recommend —
        *"the plain word wins"* — and a thin call, because §3 says the
        prescriber is already shipped and already decoupled: **do not
        re-extract** it.

        Open-session-free for :meth:`analyze`'s reason: a recommendation is a
        reading of the bank, not an act on the speaker.
        """
        ids = tuple(self._banked)
        return RecommendOutcome(
            recommendation=self.seams.recommend(ids), record_ids=ids,
        )

    def save(self) -> SaveOutcome:
        """Persist the session's own state. *"Saving is simple."*

        The per-capture records are already banked by :meth:`measure` — a
        capture is evidence the moment it exists, and holding it until a save
        would be a way to lose it. This verb writes the session-level state that
        accounts for them.

        **The five keys it writes are exactly what
        :meth:`~.prior_bank.PriorBank.read` reads back**, which is the whole
        reason a later session can grade against this one. One of them is
        shaped by that round trip: a disclosure is written as its code AND
        whether the capture happened, because those are two different facts to
        the analysis that reads the bank.

        The prior's own disclosures are NOT copied in. This state says what
        THIS session disclosed; what the round as a whole cannot claim is
        :meth:`analyze`'s answer, composed at read time from both banks.
        """
        ids = tuple(self._banked)
        state_id = self.seams.records.persist({
            "session_id": self.session_id,
            "graph_fingerprint": self._graph_fingerprint,
            "measurement_level_db": self.measurement_level_db,
            "record_ids": ids,
            "disclosures": tuple(
                {"code": stub.code, "captured": stub.captured}
                for stub in self._disclosures
            ),
        })
        return SaveOutcome(state_id=state_id, record_ids=ids)

    # --------------------------------------------------------------- internals

    def _release_both_after_failed_open(self, opening_exc: BaseException) -> None:
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
        """
        for release in (self.seams.volume.release, self.seams.graph.restore):
            _attach_cleanup_failure(opening_exc, release)
        self._volume_held = False
        self._graph_installed = False

    def _release_slots(self) -> None:
        """Give back whatever is still held, in reverse order of taking.

        Each slot's flag clears only once its release RETURNS, so a release
        that raised is attempted again by the next :meth:`close`. The graph's
        restore runs in a ``finally`` so a raising volume release cannot skip
        it, and the volume's exception is the one that reaches the caller —
        it is the slot whose loss is audible.
        """
        try:
            if self._volume_held:
                self.seams.volume.release()
                self._volume_held = False
        finally:
            if self._graph_installed:
                self.seams.graph.restore()
                self._graph_installed = False

    def _bearings(self, spec: MeasureSpec) -> tuple[int | None, ...]:
        """Where this spec measures, with the design axis spelled once.

        A spec naming no position measures the design axis, and the design axis
        is :data:`~.contracts.DESIGN_AXIS_DEG` — the same ``0`` that
        ``spatial._DESIGN_AXIS_GEOMETRY`` uses for a capture with no prompted
        move. So ``positions=()`` and ``positions=(0,)`` are one pose and one
        record, not two spellings of the same place.

        On the vertical axis this rig commands no bearing at all, so an
        unpositioned vertical walk is ``None`` rather than a ``0`` that
        ``PositionGeometry`` would refuse. Unreachable today — a vertical spec
        is a stub that captures nothing — and written so that stops being true
        without this becoming wrong.
        """
        if spec.positions:
            return spec.positions
        if spec.position_axis == POSITION_AXIS_VERTICAL:
            return (None,)
        return (DESIGN_AXIS_DEG,)

    def _one_stimulus(
        self,
        spec: MeasureSpec,
        bearing: int | None,
        prompt: str,
        stimulus_dbfs: float | None,
    ) -> StimulusOutcome:
        """Prove, play, and bank exactly one stimulus."""
        proven_level_db = self._proven_level()
        outcome: PlaybackOutcome = self.seams.play.run(
            spec=spec,
            position_deg=bearing,
            prompt=prompt,
            level_db=self.measurement_level_db,
            stimulus_dbfs=stimulus_dbfs,
        )
        if not outcome.played:
            return StimulusOutcome(
                position_deg=bearing, stimulus_dbfs=stimulus_dbfs,
                level_db=proven_level_db, record_id="",
                incident=outcome.incident,
            )
        if proven_level_db is None:
            return StimulusOutcome(
                position_deg=bearing, stimulus_dbfs=stimulus_dbfs,
                level_db=None, record_id="",
                incident=outcome.incident or UNPROVEN_LEVEL,
            )
        record_id = self.seams.records.bank(self._record(
            spec, bearing, prompt, stimulus_dbfs, outcome, proven_level_db,
        ))
        return StimulusOutcome(
            position_deg=bearing, stimulus_dbfs=stimulus_dbfs,
            level_db=proven_level_db, record_id=record_id,
            incident=outcome.incident,
        )

    def _proven_level(self) -> float | None:
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
        from jasper.active_speaker.volume_latch import fader_matches

        reading = self.seams.volume.prove()
        if reading is None or not fader_matches(reading, self.measurement_level_db):
            return None
        return float(reading)

    def _baseline_for(
        self,
        spec: MeasureSpec,
        bearing: int | None,
        stimulus_dbfs: float | None,
    ) -> str:
        """The prior's "before" at this capture's own pose, or ``""``."""
        if self.prior is None:
            return ""
        return self.prior.baseline_for(CapturePose(
            position_axis=spec.position_axis,
            position_deg=bearing,
            stimulus_dbfs=stimulus_dbfs,
        ))

    def _disclose(self, stubs: tuple[CapabilityStub, ...]) -> None:
        """Add each hole this session hit, once — see :func:`_merge_disclosures`."""
        self._disclosures[:] = _merge_disclosures([*self._disclosures, *stubs])

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
    ) -> Mapping[str, Any]:
        """One stimulus, as the facts wave 4's five blocks are built around.

        Wave 4j's index reads six of these — session, kind, position, candidate,
        timestamp, path — and the store supplies the last two, since only it
        knows where it put the record and when. The rest are here because they
        are what a reader needs to tell two captures of the same position apart:
        which regime, which polarity, which ladder rung, which graph, at what
        proven level.

        Wave 4 adds the five blocks around these; nothing here re-derives a
        curve, and nothing here is a second store — the banked files stay the
        single source of truth and the index stays rebuildable by rescanning
        them.

        ``baseline_record_id`` is the "before" THIS capture is meant to be
        compared against, named on the record rather than left for a reader to
        infer. It is what makes a verdict re-computable offline forever
        (ruling S3): a capture that carries its own comparand needs one hop to
        grade, where a capture that only carried its session id would need the
        reader to find that session's state first and re-derive the pairing.

        Resolved **per capture, by pose** — the prior's baseline at this
        bearing, on this axis, at this ladder rung. One walk is many poses, so a
        bank-wide answer would stamp the prior's last pose onto every capture
        and hand the verdict a comparand measured somewhere else. ``""`` where
        the prior baselined no such pose, and ``""`` for a session with no
        prior: an honest fact about the capture, never a refusal to bank it.
        """
        return {
            "session_id": self.session_id,
            "kind": spec.kind,
            "baseline_record_id": self._baseline_for(
                spec, bearing, stimulus_dbfs,
            ),
            "position_deg": bearing,
            "position_axis": spec.position_axis,
            "prompt": prompt,
            "candidate_id": spec.candidate_id,
            "regime": spec.regime,
            "polarity": spec.polarity,
            "graph_fingerprint": self._graph_fingerprint,
            "level_db": proven_level_db,
            "stimulus_dbfs": stimulus_dbfs,
            "incident": outcome.incident,
        }
