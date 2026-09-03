# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commission journey: where a round is, and what its stage can do (#2291).

Two facts, one owner each: where the round is (the phase walk, as one frozen
plan plus the round's position in it) and what a stage can do (the capability
declarations). This module runs no DSP, reads no file, renders nothing and
emits no journal line — every question here is bookkeeping over plain data.
The flow imports the phase vocabulary below and re-exports it, so every
``from ...crossover_v2_flow import PHASE_CHECK`` resolves to this module.
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
# The brief machine-paced window between "MEASURE accepted" and "apply
# observed": a control-page phase with no capture index.
PHASE_APPLYING = "applying"
PHASE_VERIFY = "verify"
# The two POSITION-GROUP phases (flat-linearization PR-3b). Each spans MANY
# capture-plan indexes — one prompted mic position per index — where every other
# phase spans exactly one. These are SESSION phases, deliberately distinct from
# the excitation program's own ``program.phase``: every cloud position plays the
# VERIFY-shaped mono summed sweep (``phase="verify"``), so
# ``program_analysis.analyze_program_capture`` routes it to ``_analyze_verify``
# with no dispatch change. Do not conflate the two vocabularies.
PHASE_CLOUD_MEASURE = "cloud_measure"
PHASE_CLOUD_VERIFY = "cloud_verify"
# R16 lateral evidence (plan §4.4). A position group like the two clouds, but
# its captures replay the ANCHOR's per-driver MEASURE program rather than the
# summed sweep, so it is NOT in ``SUMMED_SWEEP_PHASES``: same protected-neutral
# commissioning graph, same stimulus, same gains as MEASURE.
PHASE_LATERAL = "lateral"
# #2291's "before" measurement: ONE summed sweep at the design-axis mark, taken
# as the last thing stage 1 does. Membership in ``SUMMED_SWEEP_PHASES``'s
# COMPARED pair (not ``GROUP_SUMMED_SWEEP_PHASES``; both live in ``.programs``)
# routes ``program_for_phase`` to the very same ``_verify_program`` object, so
# this capture and VERIFY's share a ``program_id`` — a SHA-256 over the whole
# excitation schedule including every segment's gain — and that equality IS the
# comparability check ``verification.evaluate_benefit`` runs. Deliberately NOT a
# :data:`GROUP_PHASES` member: one capture at one mark, not a walk.
PHASE_ENTRY_BASELINE = "entry_baseline"
# The two-stage commission flow's untimed INTERLUDE (issue #1806): a
# measure-only session has closed, a candidate exists, and NOTHING has been
# applied. Like PHASE_APPLYING and PHASE_DONE it is a control-page phase with no
# capture index, and deliberately NOT in ``CAPTURE_PHASES``: no excitation plays
# and no evidence is bound while it renders.
PHASE_REVIEW = "review"
# The measuring session's own tail: every stage-1 phase is accepted and the
# pre-apply cloud's close has NOT produced a candidate yet — the household has
# not confirmed, or the fit is running. Also a control-page phase with no
# capture index.
PHASE_CLOSING = "closing"
PHASE_DONE = "done"

# The capturing phases in CANONICAL ORDER — the ones bound to the capture
# session's evidence and invalidated on a new session (§5.6). A given session
# runs a SUBSET of these, so a journey walks its own
# :attr:`JourneyPlan.phases` rather than this tuple. Consumers that only have
# the persisted state read its ``session_phases`` field and fall back to this.
CAPTURE_PHASES = (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_CLOUD_MEASURE,
    # LAST in stage 1, and so immediately before apply — that adjacency is the
    # whole point of the entry baseline (#2291): the less the room, the mic and
    # the household have moved, the more of the difference is the graph.
    PHASE_ENTRY_BASELINE,
    PHASE_VERIFY,
    PHASE_CLOUD_VERIFY,
)

# What a session ran before the position groups shipped. Durable state written
# then carries no ``session_phases`` field, so this — not the now-longer
# ``CAPTURE_PHASES`` — is the honest fallback for reading such a state.
PRE_CLOUD_CAPTURE_PHASES = (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY)

# The phases whose accepted-capture bookkeeping is PER INDEX rather than per
# phase, because one phase spans many prompted positions.
GROUP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_LATERAL})

# --------------------------------------------------------------------------- #
# who READS a lateral group
# --------------------------------------------------------------------------- #
#
# ``PHASE_LATERAL`` says what a group PLAYS; a CONSUMER says who reads it. The
# two walks play identically and differ in WHICH POSE TABLE they run, which is
# the distinction the validator below enforces.

#: The walk over the ratified pose table. The DEFAULT. Its historical spelling,
#: kept because the string is banked on every round that ran one.
LATERAL_CONSUMER_FC_SELECTOR = "fc_selector"

#: An operator-staged walk over poses the request itself states, banked for the
#: offline P2 forward model.
LATERAL_CONSUMER_FORWARD_MODEL = "forward_model_evidence"

LATERAL_CONSUMERS = (LATERAL_CONSUMER_FC_SELECTOR, LATERAL_CONSUMER_FORWARD_MODEL)


def validated_lateral_consumer(consumer: str, *, states_own_poses: bool) -> str:
    """Return ``consumer``, or raise :class:`ValueError`.

    ``consumer`` must be in :data:`LATERAL_CONSUMERS`, and states its own poses
    if and only if it is :data:`LATERAL_CONSUMER_FORWARD_MODEL` — the ratified
    table is the other walk's and an evidence walk may not borrow it.
    """
    if consumer not in LATERAL_CONSUMERS:
        raise ValueError(
            f"a lateral consumer must be one of {LATERAL_CONSUMERS}, "
            f"got {consumer!r}"
        )
    if states_own_poses != (consumer == LATERAL_CONSUMER_FORWARD_MODEL):
        raise ValueError(
            f"exactly the {LATERAL_CONSUMER_FORWARD_MODEL} walk states its own "
            f"poses: {LATERAL_CONSUMER_FC_SELECTOR} runs over the ratified "
            "table, and neither walk may borrow the other's"
        )
    return consumer


# --------------------------------------------------------------------------- #
# the plan — which phases this session walks, and over which indexes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JourneyPlan:
    """What ONE capture session will walk, derived once from its index map.

    The four fields are one fact — this session's shape — computed together in
    :meth:`from_index_map` and then frozen, so a plan cannot drift from the map
    it was built out of and a session can never walk a phase it has no capture
    for. ``post_apply_verifies`` reads ``PHASE_VERIFY in phases`` unless a
    caller DECLARES otherwise: a two-stage measuring session's phases correctly
    contain no VERIFY while the verification is still part of the journey.
    """

    #: Capture-plan index → phase, exactly as the capture will drive it.
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
        """Derive the plan from the capture index map the session will emit.

        ``post_apply_verifies=None`` keeps the phase-derived reading; the
        measuring stage, whose own plan carries no VERIFY, declares it, and
        :func:`open_stage` derives that declaration from the tier's numbers.
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
        answer ``False`` for :data:`PHASE_LATERAL`, because the question is
        whether per-index bookkeeping applies *here*.
        """
        return phase in self.group_indexes

    def group_offsets(self, phase: str) -> tuple[int, ...]:
        """The indexes ``phase`` spans — empty for any phase this session does
        not group positions under, which includes every single-capture phase."""
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

    Two transitions only — a capture is accepted (:meth:`accept`) or the apply
    is observed (:meth:`mark_applied`) — and everything else is derived from
    those plus the frozen plan.

    There is no illegal-transition guard, and that is a decision: the capture
    drives indexes in order and re-accepting a settled position is how a
    geometry retake legitimately lands, so a guard here would have to encode the
    capture layer's retry policy. What this class owns is that the derivations
    stay consistent with whatever the capture layer accepted.
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
        # an open group.
        self._group_accepted: dict[str, set[int]] = {
            phase: set() for phase in plan.group_indexes
        }
        self._applied = bool(applied)

    # --- transitions ---------------------------------------------------------

    def accept(self, phase: str, index: int) -> None:
        """Record one resolved capture, closing its group when that was the last.

        ``_group_accepted`` means RESOLVED, not "has a curve": a position the
        flow gave up on lands here too, because the capture advanced past it and
        the phase would otherwise never close.
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

    def mark_restored(self) -> None:
        """The applied graph has been put back — disarms the VERIFY hold (#2616).

        This object is the single owner of ``applied``; a durable-state writer
        that clears it holds no conductor, so without this a restored live
        session kept ``applied`` True in memory and the next
        ``persist_conductor_state`` wrote that stale True back over the clear.
        Unconditional, like its inverse: restoring a session that never applied
        is a no-op rather than an error.
        """
        self._applied = False

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
                # apply is pending. Unreached by any shipped session since the
                # two-stage split: stage 1 has no VERIFY in its plan and stage 2
                # is constructed applied; the wizard's ``_phase_from_state``
                # routes those two shapes.
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

        Curves in hand PLUS positions the household has not walked yet, never
        the count so far, which would make "can this group still reach its
        position floor" depend on walk order.
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

#: The seams a stage may or may not bind, and the priors a stage may need handed
#: to it. Slugs rather than an enum: they are journal vocabulary first.
CAPABILITY_FINDINGS = "findings"
CAPABILITY_ROLLBACK = "rollback"
CAPABILITY_COMMANDED_DELTA = "commanded_delta"
CAPABILITY_PREDICTED_SUM = "predicted_sum"
CAPABILITY_ENTRY_BASELINE = "entry_baseline"


@dataclass(frozen=True)
class V2StageCapabilities:
    """What ONE commission stage binds, and what it needs handed to it.

    The v2 commission runs as two capture sessions against two session objects,
    binding identical seams except in two places.

    ``provides`` lists ONLY the seams that DIFFER between stages — the ones
    :func:`jasper.web.correction_crossover_v2.bind_v2_stage_seams` branches on.
    ``requires`` is what a stage needs the PREVIOUS one to have left on disk,
    and it is OBSERVABILITY, not a gate: a stage opens either way and a missing
    input is journalled, because refusing to open would strand a household whose
    only remaining move is the one being refused.
    """

    stage: str
    provides: frozenset[str]
    requires: frozenset[str] = frozenset()


#: Stage 1 — measure, then stop. Binds the findings publisher because a
#: level-frame finding is banked only by the MEASURE candidate's own gate
#: (#1866), and stage 2 builds no MEASURE candidate. Requires nothing: it is the
#: first stage and hydrates its own prior snapshot.
STAGE_MEASURE_CAPABILITIES = V2StageCapabilities(
    stage="measure",
    provides=frozenset({CAPABILITY_FINDINGS}),
)

#: Stage 2 — the post-apply verdict. Binds rollback because this is the only
#: stage that reaches the delta probe, and PR-L5's rollback is automatic on the
#: non-matched verdicts. Requires the two stage-1 curves the probe and the
#: tracking check grade against, plus #2291's entry baseline — without it the
#: round cannot say the speaker got better, so its absence must reach the
#: journal rather than pass unremarked.
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

    The slug↔prior binding is stated where the slug is declared rather than at
    the host call site that happens to hold the value. Sorted, because the
    shortfall it feeds is.
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

    The single contract both host preparers build; they differ in their
    arguments and nothing else.
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
    it is what is missing. Nothing is refused: a missing prior narrows what the
    stage can CLAIM — the delta probe reports unavailable, the benefit verdict
    grades indeterminate — and both are honest outcomes the round carries.

    ``verify_capture_target`` is the tier's declared count of post-apply capture
    positions, and ``>= 1`` is the whole of the rule: a tier that declares no
    post-apply positions drops boost permission along with them. ``None`` leaves
    the plan's phase-derived reading in place.
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
