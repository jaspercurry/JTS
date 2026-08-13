# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commission journey: where a round is, and what its stage can do (#2291).

Phase 4's extraction.  Two facts had no owner of their own and were therefore
kept in two places that were free to disagree:

* **Where the round is.**  The phase walk lived as six correlated fields on
  :class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session`
  (the index map, the ordered phases, the group index spans, the accepted
  phases, the accepted indexes inside an open group, and the applied flag),
  mutated in place by the capture-consuming methods around them.
* **What a stage can do.**  The stage capability declarations lived in
  :mod:`jasper.web.correction_crossover_v2` — domain semantics in the host,
  which is what Phase 4 exists to end.

Both are now here, as one small aggregate and one small declaration set.

**What this module is not.**  It runs no DSP, reads no file, renders nothing,
and emits no journal line.  A journey answers "which phase is next", "has this
group closed", and "what did this stage open without"; every one of those is a
question about *bookkeeping*, answerable from plain data, and none of them is
improved by being able to reach a curve or a socket.  Deriving the capability
shortfall here while the host still journals it is the same boundary: the
verdict is domain, the sentence is the host's, and
:func:`jasper.web.correction_crossover_v2.bind_v2_stage_seams` stays its single
emitter so a third caller cannot put the journal's account of stage shape back
out of one owner's hands.

Dependency direction, as for every module in this package: no ``jasper.web``
import and nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.  The
flow imports the phase vocabulary below and re-exports it, so every existing
``from ...crossover_v2_flow import PHASE_CHECK`` keeps resolving to this
module's object.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import AbstractSet, Iterable, Mapping

# --------------------------------------------------------------------------- #
# phase vocabulary
# --------------------------------------------------------------------------- #

PHASE_CHECK = "check"
PHASE_MEASURE = "measure"
# The owner ruling (2026-07-20) removed the human mid-flow Apply gate and had
# the session apply a trusted candidate itself. SUPERSEDED by two-stage T3
# (commit ``61ba33ff1``, #1806 / #1906): the apply is the household's explicit
# POST now. Read :mod:`jasper.active_speaker.crossover_v2_flow`'s module
# docstring for what replaced it rather than this comment.
# This phase names the brief machine-paced window between "MEASURE
# accepted" and "apply observed" — the phone sees it as the existing
# CaptureBeginDeferred hold (now captioned "Applying…", not "waiting for the
# household"), and the wizard shows a plain in-progress screen. It is still a
# control-page phase (no capture index) between MEASURE-accepted and
# VERIFY-armed.
PHASE_APPLYING = "applying"
PHASE_VERIFY = "verify"
# The two POSITION-GROUP phases (flat-linearization PR-3b). Each spans MANY
# capture-plan indexes — one prompted mic position per index — where every
# other phase spans exactly one. CLOUD_MEASURE holds the pre-apply spatial
# cloud (the N−1 summed sweeps that follow MEASURE's design-axis anchor);
# CLOUD_VERIFY holds the post-apply one (the M−1 that follow VERIFY's
# anchor). See ``CLOUD_POSITION_PROMPTS`` for the physics the prompts encode
# and ``build_v2_cloud_index_phase_map`` for the index layout.
#
# These are SESSION phases, deliberately distinct from the EXCITATION
# PROGRAM's own ``program.phase``: every cloud position plays the VERIFY-
# shaped mono summed sweep (``phase="verify"``), so
# ``program_analysis.analyze_program_capture`` routes it to ``_analyze_verify``
# with no dispatch change and the session still knows which group the
# capture belongs to. Do not conflate the two vocabularies.
PHASE_CLOUD_MEASURE = "cloud_measure"
PHASE_CLOUD_VERIFY = "cloud_verify"
# R16 lateral evidence (plan §4.4). A position group like the two clouds, but
# its captures replay the ANCHOR's per-driver MEASURE program rather than the
# summed sweep, because §4.4's uses ("compare the woofer/HF relative falloff",
# "predict each candidate's sum at all sampled positions") are per-driver claims
# a summed curve cannot answer. So it is NOT in ``SUMMED_SWEEP_PHASES``: same
# protected-neutral commissioning graph, same stimulus, same gains as MEASURE.
PHASE_LATERAL = "lateral"
# #2291's "before" measurement: ONE summed sweep at the design-axis mark, taken
# as the last thing stage 1 does — immediately before the household applies.
#
# It exists because a round could not say whether the speaker got BETTER. The
# 2026-08-10 jts3 round retained CHECK/MEASURE, the lateral walk, VERIFY, and
# five post-apply cloud positions and still had no answer, because none of that
# evidence was *the same summed acoustic question, at the same program and
# level, at the same mark, before and after the graph change*. This phase is
# that question's first half; stage 2's ``PHASE_VERIFY`` is its second.
#
# Membership in ``SUMMED_SWEEP_PHASES`` (which lives in ``.programs``, because
# it selects an excitation program rather than a place in the walk) is what makes
# the pair comparable at all: it routes ``program_for_phase`` to the very same
# ``_verify_program`` object, so the two captures share a ``program_id`` — a
# SHA-256 over the whole excitation schedule including every segment's gain —
# and that equality IS the comparability check
# :func:`jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
# runs. It is deliberately NOT a :data:`GROUP_PHASES` member: it is one capture
# at one mark, not a walk.
PHASE_ENTRY_BASELINE = "entry_baseline"
# The two-stage commission flow's untimed INTERLUDE (work order D3, issue
# #1806): a measure-only session has closed, a candidate exists, and NOTHING
# has been applied — the household is being shown what was measured, what is
# proposed, what is predicted, and the spec verdict, and is choosing. Like
# PHASE_APPLYING and PHASE_DONE this is a control-page phase with no capture
# index, and like them it is deliberately NOT in ``CAPTURE_PHASES``: no
# excitation plays and no evidence is bound while it renders.
#
# It exists because ``_phase_from_state``'s walk resolved a measure-only
# session to PHASE_DONE — the RESULT screen, "Your speaker is tuned" — over a
# speaker that had been measured and not tuned at all (work order current-state
# premise 6, a verified collision rather than a theoretical one).
PHASE_REVIEW = "review"
# The measuring session's own tail (two-stage work order D1): every stage-1
# phase is accepted, and the pre-apply cloud's close has NOT produced a
# candidate yet — either because the household has not confirmed on the phone,
# or because the fit is running. Like PHASE_REVIEW it is a control-page phase
# with no capture index, and it exists because without it those moments
# resolved to PHASE_REVIEW: the wizard told a household mid-hold that the
# measurement had produced nothing and offered to throw it away, while the
# relay was still live and the phone was waiting for their tap.
PHASE_CLOSING = "closing"
PHASE_DONE = "done"

# The capturing phases in CANONICAL ORDER — the ones bound to the relay
# session's evidence and invalidated on a new session (§5.6). A given session
# runs a SUBSET of these (a verify-only re-arm runs just ``PHASE_VERIFY``), so
# a journey walks its own :attr:`JourneyPlan.phases` — the subset its index map
# actually addresses, in this order — never this tuple directly. Consumers that
# only have the persisted state read its ``session_phases`` field and fall back
# to this tuple (see ``jasper.web.correction_crossover_v2._phase_from_state``).
CAPTURE_PHASES = (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_CLOUD_MEASURE,
    # LAST in stage 1, and so immediately before apply — that adjacency is the
    # whole point of the entry baseline (#2291), not a layout preference: the
    # less the room, the mic, and the household have moved between it and the
    # graph change, the more of the before→after difference is the graph.
    PHASE_ENTRY_BASELINE,
    PHASE_VERIFY,
    PHASE_CLOUD_VERIFY,
)

# The phases whose accepted-capture bookkeeping is PER INDEX rather than per
# phase, because one phase spans many prompted positions.
GROUP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_LATERAL})


# --------------------------------------------------------------------------- #
# the plan — which phases this session walks, and over which indexes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JourneyPlan:
    """What ONE relay session will walk, derived once from its index map.

    Every field below was a separate assignment in the conductor's constructor,
    each re-deriving from the one before it. They are one fact — "this session's
    shape" — so they are computed together in :meth:`from_index_map` and then
    frozen: a plan cannot drift from the map it was built out of, and a session
    can never walk a phase it has no capture for (the verify-only re-arm would
    otherwise sit forever "pending" on a cloud group it never runs).

    ``post_apply_verifies`` is the boost-permission evidence gate. It reads
    ``PHASE_VERIFY in phases`` unless a caller DECLARES otherwise, and the
    declaration exists because the two-stage split (work order D2) moved the
    post-apply sweep into its own session: a measuring session's phases
    correctly contain no VERIFY while the verification is very much still part
    of the journey, so reading the phases alone would silently demote every
    two-stage correction to cut-only.
    """

    #: Capture-plan index → phase, exactly as the relay will drive it.
    index_phase_map: Mapping[int, str]
    #: The phases this session runs, in :data:`CAPTURE_PHASES` order.
    phases: tuple[str, ...]
    #: Group phase → the capture indexes it spans, ascending. Group phases only.
    group_indexes: Mapping[str, tuple[int, ...]]
    #: Will the correction this session proposes be MEASURED after it is applied?
    post_apply_verifies: bool

    @classmethod
    def from_index_map(
        cls,
        index_phase_map: Mapping[int, str],
        *,
        post_apply_verifies: bool | None = None,
    ) -> "JourneyPlan":
        """Derive the plan from the relay index map the session will emit.

        ``post_apply_verifies=None`` keeps the phase-derived reading, which is
        what every caller that has not been told otherwise wants (the post-apply
        session, the recovery re-verify, the 3-entry shape, every conductor unit
        test). A caller that knows better — the measuring stage, whose own plan
        carries no VERIFY — declares it; :func:`open_stage` is where that
        declaration is derived from the tier's own numbers.
        """
        resolved = MappingProxyType(dict(index_phase_map))
        present = set(resolved.values())
        phases = tuple(p for p in CAPTURE_PHASES if p in present)
        group_indexes = MappingProxyType({
            phase: tuple(sorted(i for i, p in resolved.items() if p == phase))
            for phase in phases
            if phase in GROUP_PHASES
        })
        return cls(
            index_phase_map=resolved,
            phases=phases,
            group_indexes=group_indexes,
            post_apply_verifies=(
                PHASE_VERIFY in phases
                if post_apply_verifies is None
                else bool(post_apply_verifies)
            ),
        )

    def phase_for_index(self, index: int) -> str | None:
        """The phase this capture index belongs to, or ``None`` if unplanned."""
        return self.index_phase_map.get(index)

    def is_group(self, phase: str) -> bool:
        """Does this session run ``phase`` as a multi-position group?

        Not ``phase in GROUP_PHASES``: a session that runs no lateral walk must
        answer ``False`` for :data:`PHASE_LATERAL`, because the question every
        caller is really asking is "does per-index bookkeeping apply *here*".
        """
        return phase in self.group_indexes

    def group_offsets(self, phase: str) -> tuple[int, ...]:
        """The indexes ``phase`` spans — empty for a phase this session groups
        no positions under, which includes every single-capture phase."""
        return self.group_indexes.get(phase, ())

    def is_last_index_of_group(self, phase: str, index: int) -> bool:
        """Is ``index`` the final prompted position of its group?"""
        offsets = self.group_indexes.get(phase, ())
        return bool(offsets) and index == offsets[-1]


# --------------------------------------------------------------------------- #
# the journey — the plan, plus how far this round has got through it
# --------------------------------------------------------------------------- #


class CommissionJourney:
    """One round's position in its :class:`JourneyPlan`.

    The state machine is deliberately tiny, because the transitions genuinely
    are: a capture is accepted (:meth:`accept`), or the apply is observed
    (:meth:`mark_applied`). Everything else on this class is a derivation from
    those two facts plus the frozen plan — which is the point of the extraction.
    The conductor used to spread the same state over three mutable fields and
    ask questions of them from a dozen places; the fields could disagree (a
    phase in ``_accepted`` whose group indexes were not all in
    ``_group_accepted``) and nothing said they must not.

    There is no illegal-transition guard, and that is a decision rather than an
    omission. The relay drives indexes in order and re-accepting a settled
    position is how a geometry retake legitimately lands, so a guard here would
    have to encode the retry policy — which is the capture layer's, not the
    journey's. What this class owns is that the *derivations* stay consistent
    with whatever the capture layer accepted.
    """

    __slots__ = ("plan", "_accepted", "_group_accepted", "_applied")

    def __init__(
        self,
        plan: JourneyPlan,
        *,
        accepted_phases: Iterable[str] = (),
        applied: bool = False,
    ) -> None:
        self.plan = plan
        self._accepted: set[str] = set(accepted_phases)
        # Per-group progress. ``_accepted`` holds PHASES (one entry per group,
        # added when the group CLOSES); this holds the accepted indexes inside
        # an open group, so accepting position 3 of 8 does not read as "the
        # pre-apply cloud is done."
        self._group_accepted: dict[str, set[int]] = {
            phase: set() for phase in plan.group_indexes
        }
        self._applied = bool(applied)

    # --- transitions ---------------------------------------------------------

    def accept(self, phase: str, index: int) -> None:
        """Record one resolved capture, closing its group when that was the last.

        ``_group_accepted`` means RESOLVED, not "has a curve": a position the
        flow gave up on lands here too, because the relay advanced past it and
        the phase would otherwise never close. What was actually *measured* is
        the retained per-position evidence's business, not this ledger's.
        """
        if phase not in self._group_accepted:
            self._accepted.add(phase)
            return
        self._group_accepted[phase].add(index)
        if self._group_accepted[phase] >= set(self.plan.group_indexes[phase]):
            self._accepted.add(phase)

    def mark_applied(self) -> None:
        """The apply has been observed — arms the soft-held VERIFY (§5.2)."""
        self._applied = True

    # --- derivations ---------------------------------------------------------

    @property
    def applied(self) -> bool:
        return self._applied

    @property
    def accepted_phases(self) -> frozenset[str]:
        return frozenset(self._accepted)

    def accepted_capture_phases(self) -> tuple[str, ...]:
        """The accepted phases in :data:`CAPTURE_PHASES` order, for the snapshot.

        Ordered rather than a set because it is persisted and read back by a
        later stage; an unordered dump would make two equal states compare
        unequal on disk.
        """
        return tuple(p for p in CAPTURE_PHASES if p in self._accepted)

    def phase_status(self, phase: str) -> str:
        return "accepted" if phase in self._accepted else "pending"

    def pending_phases(self) -> tuple[str, ...]:
        return tuple(p for p in self.plan.phases if p not in self._accepted)

    @property
    def current_phase(self) -> str:
        for phase in self.plan.phases:
            if phase not in self._accepted:
                # Everything before VERIFY accepted but not yet applied ⇒ an
                # apply is pending. RETAINED and unreached by any shipped
                # session since the two-stage split (D10): stage 1 has no
                # VERIFY in its plan at all, and stage 2 is constructed
                # applied. The wizard's own resolution (``_phase_from_state``)
                # is what routes those two shapes, to the review interlude and
                # to PHASE_VERIFY respectively.
                if (
                    phase == PHASE_VERIFY
                    and PHASE_MEASURE in self._accepted
                    and not self._applied
                ):
                    return PHASE_APPLYING
                return phase
        return PHASE_DONE

    def unresolved_in_group(self, phase: str, *, excluding: int) -> tuple[int, ...]:
        """The group's positions still unwalked, ignoring the one being decided.

        The capture layer asks this to answer "can this group still reach its
        position floor" — curves in hand PLUS positions the household has not
        walked yet, never the count so far, which would make the answer depend
        on walk order and end a session at position 1 of 8 with seven good spots
        still ahead.
        """
        accepted: AbstractSet[int] = self._group_accepted.get(phase, frozenset())
        return tuple(
            other
            for other in self.plan.group_indexes.get(phase, ())
            if other != excluding and other not in accepted
        )


# --------------------------------------------------------------------------- #
# stage capabilities — one declaration per commission stage
# --------------------------------------------------------------------------- #

#: The seams a stage may or may not bind, and the priors a stage may need
#: handed to it. Slugs rather than an enum: they are journal vocabulary before
#: they are anything else, and one line each is the whole of their behaviour.
CAPABILITY_FINDINGS = "findings"
CAPABILITY_ROLLBACK = "rollback"
CAPABILITY_COMMANDED_DELTA = "commanded_delta"
CAPABILITY_PREDICTED_SUM = "predicted_sum"
CAPABILITY_ENTRY_BASELINE = "entry_baseline"


@dataclass(frozen=True)
class V2StageCapabilities:
    """What ONE commission stage binds, and what it needs handed to it.

    The v2 commission runs as two relay sessions against two session objects
    (``prepare_v2_session`` measures and stops; the household applies;
    ``prepare_v2_verify`` verifies). The seams they bind are identical except in
    two places, and until #2291 Phase 3 both shapes were hand-assembled at their
    own call site — which is how the rollback seam came to be bound on the stage
    that never reaches a VERIFY verdict and left off the stage that does.

    ``provides`` lists ONLY the seams that DIFFER between stages — the ones
    :func:`jasper.web.correction_crossover_v2.bind_v2_stage_seams` branches on.
    Capture, analysis, evidence publication, and the apply gates are
    unconditional on both stages, so naming them here would restate a constant
    in a second place rather than record a decision.

    ``requires`` is what a stage needs the PREVIOUS one to have left on disk.
    It is OBSERVABILITY, not a gate: a stage opens either way, and a missing
    input is journalled so "the probe reported unavailable" carries a reason
    instead of reading like a pass. A stage that refused to open on a prior it
    could still run without would strand a household whose only remaining move
    is the one being refused.
    """

    stage: str
    provides: frozenset[str]
    requires: frozenset[str] = frozenset()


#: Stage 1 — measure, then stop. Binds the findings publisher because a level-
#: frame finding is banked only by the MEASURE candidate's own gate (#1866);
#: stage 2 builds no MEASURE candidate, so binding it there would ship a seam
#: nothing can reach. Requires nothing: it is the first stage, and it hydrates
#: its own prior snapshot through ``CrossoverV2Session.hydrate``.
STAGE_MEASURE_CAPABILITIES = V2StageCapabilities(
    stage="measure",
    provides=frozenset({CAPABILITY_FINDINGS}),
)

#: Stage 2 — the post-apply verdict. Binds rollback because this is the only
#: stage that reaches the delta probe, and PR-L5's rollback is automatic on the
#: non-matched verdicts: a session without the seam still refuses, but under
#: ``REASON_CORRECTION_ROLLBACK_FAILED``, whose copy tells the household the
#: correction is STILL APPLIED. Requires the two stage-1 curves the probe and
#: the tracking check grade against, plus #2291's entry baseline — the measured
#: "before" this stage's benefit verdict differences its own capture against.
#: Without it the round can still say the graph did what it commanded; it
#: cannot say the speaker got better, which is the question the issue exists
#: for, so its absence must reach the journal rather than pass unremarked.
STAGE_VERIFY_CAPABILITIES = V2StageCapabilities(
    stage="verify",
    provides=frozenset({CAPABILITY_ROLLBACK}),
    requires=frozenset({
        CAPABILITY_COMMANDED_DELTA,
        CAPABILITY_PREDICTED_SUM,
        CAPABILITY_ENTRY_BASELINE,
    }),
)


def available_stage_priors(
    *,
    commanded_delta: bool,
    predicted_sum: bool,
    entry_baseline: bool,
) -> tuple[str, ...]:
    """Which of stage 2's required priors actually crossed the bridge.

    The caller states three facts about its own rehydration and this names them,
    so the slug↔prior binding — what ``commanded_delta`` the capability MEANS —
    is stated where the slug is declared rather than at the host call site that
    happens to hold the value. Sorted, because the shortfall it feeds is.
    """
    return tuple(sorted(
        slug
        for slug, present in (
            (CAPABILITY_COMMANDED_DELTA, commanded_delta),
            (CAPABILITY_PREDICTED_SUM, predicted_sum),
            (CAPABILITY_ENTRY_BASELINE, entry_baseline),
        )
        if present
    ))


@dataclass(frozen=True)
class StageOpening:
    """One stage's journey shape, and what it opened without.

    The single contract both host preparers build. Each used to assemble the
    same three answers — the capability declaration, the walk, and whether the
    round would be verified after apply — from its own reading of the plan
    shape and the durable state, which is how two call sites free to disagree
    came to disagree. They now differ in their arguments and nothing else.
    """

    capabilities: V2StageCapabilities
    plan: JourneyPlan
    #: ``requires`` minus what was handed over, sorted. Empty is the good case.
    missing: tuple[str, ...]

    @property
    def stage(self) -> str:
        return self.capabilities.stage


def open_stage(
    capabilities: V2StageCapabilities,
    *,
    index_phase_map: Mapping[int, str],
    verify_capture_target: int | None = None,
    available: Iterable[str] = (),
) -> StageOpening:
    """Resolve one stage's opening: its declaration, its walk, its shortfall.

    ``available`` is what this stage actually got handed, so ``requires`` minus
    it is what is missing. The caller states facts; the verdict is derived here,
    which is why the declaration and the shortfall cannot disagree.

    Nothing is refused. A missing prior narrows what the stage can CLAIM — the
    delta probe reports unavailable, the benefit verdict grades indeterminate —
    and both of those are honest outcomes the round already knows how to carry.
    Refusing to open would instead strand a household whose only remaining move
    is the one being refused.

    ``verify_capture_target`` is the tier's declared count of post-apply capture
    positions, and ``>= 1`` is the whole of the rule: a tier that declares no
    post-apply positions drops boost permission along with them. The stage
    states the tier's own number and this decides what it means, which is the
    last piece of domain reasoning Phase 4 took out of ``prepare_v2_session``.
    ``None`` leaves the plan's phase-derived reading in place — the answer the
    verifying stage, whose plan really does contain VERIFY, already gives.
    """
    return StageOpening(
        capabilities=capabilities,
        plan=JourneyPlan.from_index_map(
            index_phase_map,
            post_apply_verifies=(
                None if verify_capture_target is None
                else int(verify_capture_target) >= 1
            ),
        ),
        missing=tuple(sorted(capabilities.requires - set(available))),
    )
