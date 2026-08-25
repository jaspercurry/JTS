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
``measure`` with :data:`~.measure_spec.MEASURE_KIND_VERIFY` plus ``analyze``.

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
from typing import Any, Mapping

from .measure_spec import MeasureSpec, CapabilityStub, stubbed_capabilities
from .session_seams import EngineSeams
from .playback_transaction import PlaybackOutcome

__all__ = [
    "AnalyzeOutcome",
    "MeasureOutcome",
    "RecommendOutcome",
    "SaveOutcome",
    "SessionStateError",
    "TuningSession",
]


class SessionStateError(RuntimeError):
    """A verb was called against a session lifetime that cannot serve it.

    A programming error, not a measurement one — which is why it raises where a
    measurement problem would return a disclosure.
    """


@dataclass(frozen=True)
class MeasureOutcome:
    """What one ``measure`` produced.

    ``record_ids`` is one id per position that was actually measured and
    banked, in the order the spec named them. It is empty when a stub stopped
    the stimulus, and it is SHORTER than ``spec.positions`` when a position's
    play transaction did not reach :data:`~.playback_transaction.STAGE_PLAY` —
    which the matching entry in ``playbacks`` explains by stage and reason code.
    """

    spec: MeasureSpec
    record_ids: tuple[str, ...]
    playbacks: tuple[PlaybackOutcome, ...]
    disclosures: tuple[CapabilityStub, ...]


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
    and the ONE declared level every stimulus in this session plays at. Use it
    as a context manager, or call :meth:`open` and :meth:`close` — the second
    exists because a web front end's lifetime is a sequence of HTTP requests and
    cannot hold a ``with``.

    Three declarations and four fields of state. The 102-attribute session this
    replaces is what happens when a class accumulates the answers instead of the
    seams.
    """

    session_id: str
    seams: EngineSeams
    #: The one level every stimulus plays at, and the one the volume claim is
    #: taken for. Ruling S8's recipe turns on it being ONE: *"same drive
    #: voltage across every per-driver measurement; no gain is touched between
    #: them."*
    measurement_level_db: float

    _open: bool = field(default=False, init=False)
    _graph_fingerprint: str = field(default="", init=False)
    _banked: list[str] = field(default_factory=list, init=False)
    _disclosures: list[CapabilityStub] = field(default_factory=list, init=False)

    # ---------------------------------------------------------------- lifetime

    def open(self) -> None:
        """Open the three slots, in the order their contracts require.

        The graph goes in first and its proof runs once, before anything can
        play (MS-13); the volume claim is taken at the declared level second, so
        the fader the graph will be measured through is already this session's.
        An open that fails halfway puts back the half it took — a graph left
        installed by a failed claim is a speaker measuring through a graph
        nobody is holding.

        The record store has no open: it is a sink whose lifetime IS this
        session's, and the id every record carries is the key that says so.
        """
        if self._open:
            raise SessionStateError(f"session {self.session_id} is already open")
        self._graph_fingerprint = self.seams.graph.install()
        claimed = False
        try:
            self.seams.volume.acquire(self.measurement_level_db)
            claimed = True
        finally:
            if not claimed:
                self.seams.graph.restore()
        self._open = True

    def close(self) -> None:
        """Release both held slots, even if releasing one raises.

        Idempotent: closing a closed session is what an ``__exit__`` after an
        explicit :meth:`close` does, and it is not an error. A leaked graph or a
        stranded fader claim is the failure worth spending a ``finally`` on —
        the fader especially, because a session that dies holding one leaves the
        speaker at a measurement level nobody chose.
        """
        if not self._open:
            return
        self._open = False
        try:
            self.seams.volume.release()
        finally:
            self.seams.graph.restore()

    def __enter__(self) -> "TuningSession":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def graph_fingerprint(self) -> str:
        """Which graph this session's evidence was measured through.

        Provenance on a record, never a gate — ``""`` when the host could not
        name it.
        """
        return self._graph_fingerprint

    @property
    def banked_record_ids(self) -> tuple[str, ...]:
        return tuple(self._banked)

    # ------------------------------------------------------------------- verbs

    def measure(self, spec: MeasureSpec) -> MeasureOutcome:
        """Measure what the spec asks for, at every position it names.

        One verb for all three kinds. The order is fixed and each step is
        somebody's invariant:

        1. **Stubs first** (ruling S12). A capability this engine has not built
           is named before anything plays, and a stub whose ``captured`` is
           ``False`` means there is no stimulus to play at all — so the session
           says what it cannot do instead of quietly measuring something else.
        2. **The level is proven before any audio** (MS-14). An unproven fader
           refuses to BANK, never to play: ruling S10's *refusing to WORK dies;
           refusing to CLAIM stays*, which is why an unproven level still runs
           the transaction and simply banks nothing.
        3. **One play transaction per position**, ready → admit → lock → play →
           restore, behind :mod:`.playback_transaction`.
        4. **Bank what played.** A transaction that never reached ``play`` has
           no evidence, and banking a record for it would be the dishonest kind
           of completeness.

        Disclosures accumulate onto the session, deduplicated by code, so
        :meth:`analyze` reports each hole once whether or not the caller kept
        this outcome and however many specs tripped it.
        """
        self._require_open()
        stubs = stubbed_capabilities(spec)
        self._disclose(stubs)
        if any(not stub.captured for stub in stubs):
            return MeasureOutcome(
                spec=spec, record_ids=(), playbacks=(), disclosures=stubs,
            )

        proven_level_db = self.seams.volume.prove()
        bearings: tuple[int | None, ...] = spec.positions or (None,)

        banked: list[str] = []
        playbacks: list[PlaybackOutcome] = []
        for bearing in bearings:
            outcome = self.seams.play.run(
                spec=spec,
                position_deg=bearing,
                level_db=self.measurement_level_db,
            )
            playbacks.append(outcome)
            if not outcome.played or proven_level_db is None:
                continue
            banked.append(self.seams.records.bank(
                self._record(spec, bearing, outcome, proven_level_db)
            ))

        self._banked.extend(banked)
        return MeasureOutcome(
            spec=spec,
            record_ids=tuple(banked),
            playbacks=tuple(playbacks),
            disclosures=stubs,
        )

    def analyze(self) -> AnalyzeOutcome:
        """Every analysis the banked records support — and every one they do not.

        **Deliberately not gated on an open session.** Ruling S3's whole return
        on banking complete records is that a session can be re-analyzed offline
        forever, by analyses that did not exist when it was captured. A verb
        that needed a live graph and a held fader to read a file would give that
        back.

        **Today it runs nothing.** §1's wholesale default is wave 2's, and it
        lands in this method rather than in any analysis unit — *do not decouple
        the analysis layer, replace its caller.* What ships now is the honest
        half: the capability holes this session hit, reported rather than
        skipped.
        """
        return AnalyzeOutcome(results={}, disclosures=tuple(self._disclosures))

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
        """
        state_id = self.seams.records.persist({
            "session_id": self.session_id,
            "graph_fingerprint": self._graph_fingerprint,
            "measurement_level_db": self.measurement_level_db,
            "record_ids": tuple(self._banked),
            "disclosures": tuple(stub.code for stub in self._disclosures),
        })
        return SaveOutcome(state_id=state_id, record_ids=tuple(self._banked))

    # --------------------------------------------------------------- internals

    def _disclose(self, stubs: tuple[CapabilityStub, ...]) -> None:
        """Add each hole once, keeping the order it was first hit in."""
        seen = {stub.code for stub in self._disclosures}
        self._disclosures.extend(
            stub for stub in stubs if stub.code not in seen
        )

    def _require_open(self) -> None:
        if not self._open:
            raise SessionStateError(
                f"session {self.session_id} must be open to measure — its graph "
                "is proven and its level claimed at open()"
            )

    def _record(
        self,
        spec: MeasureSpec,
        bearing: int | None,
        outcome: PlaybackOutcome,
        proven_level_db: float,
    ) -> Mapping[str, Any]:
        """The columns wave 4j's index needs, and not one field more.

        *"Six columns, one table, one writer, one reader. If a design
        conversation starts adding a schema migration story, it has left the
        brief."* Wave 4 adds the five-block record around these; nothing here
        re-derives a curve, and nothing here is a second store — the banked
        files stay the single source of truth and this stays rebuildable by
        rescanning them.
        """
        return {
            "session_id": self.session_id,
            "kind": spec.kind,
            "position_deg": bearing,
            "position_axis": spec.position_axis,
            "candidate_id": spec.candidate_id,
            "regime": spec.regime,
            "polarity": spec.polarity,
            "graph_fingerprint": self._graph_fingerprint,
            "level_db": proven_level_db,
            "incident": outcome.incident,
        }
