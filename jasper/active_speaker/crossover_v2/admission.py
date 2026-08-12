# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Who may start one more capture, and what it costs (#2291 Phase 5a-vi).

The seventh sibling of :mod:`.programs`, :mod:`.priors`, :mod:`.spatial`,
:mod:`.candidates`, :mod:`.fc_sweep` and :mod:`.planning`.  Those own what a
capture *means*; this one owns the meter in front of it — the bounded-retry
ruling of 2026-08-03 (issue #2086), which says one prompted position gets its
planned capture plus at most :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION` extras,
pooled across everyone who can ask for one.

**This module DECIDES; it does not act** — the split :mod:`.accountability`
established, for the same reason.  :func:`assess_begin` answers "may this begin
start, and who pays for it" and hands back a :class:`BeginDecision`.  The
conductor owns every irreversible half: the ``CaptureBeginRefused`` /
``CaptureBeginDeferred`` it raises, the ``_last_failure_*`` fields that raise
stamps, the ``_armed_*`` capture identity, the journal line, and the two ledger
mutations (:meth:`SlotAttempts.spend` and the ``admitted`` increment).  A pure
gate can be asked the same question twice and answer the same way, which is
what makes a replayed begin safe to re-evaluate.

**No household vocabulary lives here** — the rule :mod:`.coordinator` states.
A refusal leaves as a *kind* plus the opaque reason code it concerns; the flow
owns ``REASON_REGISTRY`` and renders the sentence.  Two consequences are worth
naming, because both look like things this module should own:

* **The registry projections arrive as arguments.**  ``non_retriable`` and
  ``default_code`` are the flow's, derived from ``REASON_REGISTRY`` there.
  Stating them keeps the "inputs are stated, never reached for" rule
  :mod:`.priors` set, and it is also the only shape available: importing the
  registry back would be the reverse dependency this package exists to prevent.
* **The two sentence builders take their diagnosis, they do not render it.**
  :func:`extras_spent_message` and :func:`spent_slot_outcome` compose the
  *count* and the *consequence*, which are facts about the ledger and the
  group.  The ``diagnosis`` half is rendered by the flow's
  ``reason_diagnosis`` and passed in.

**The apply gate is a port for one reason: call count.**  The VERIFY hold asks
the host whether the auto-apply failed, and that question is only asked on the
hold branch.  A value resolved at call time would ask it on every begin — a
seam call the shipped flow does not make — so :func:`assess_begin` takes a
callable and invokes it exactly where the conductor did.  Its ``OSError`` /
``RuntimeError`` / ``ValueError`` guard stays with the conductor, whose seam it
is.

**The settle path's decision half lives here too** (#2291 Phase 5b), in
:func:`settle_spent_slot` and :func:`settle_group_position`.  This module's
first version recorded that it could not leave — true of moving
``_resolve_spent_slot`` *whole*, since it returns the flow's ``PhaseVerdict``
and performs the group close under ``_close_lock``, and false of the
decision/act split the package actually uses: a ladder that answers with a
*kind* never touches the carrier.  What stayed with the conductor is every act
— rendering the diagnosis, the journal line, the ``_group_unresolved``
attribution, the group close, and building the verdict.  See those two
functions for why the ladder is split in two at exactly the lock boundary.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Container

__all__ = [
    "ATTEMPT_INITIATOR_HOUSEHOLD",
    "ATTEMPT_INITIATOR_SPEAKER",
    "DECISION_KINDS",
    "MAX_EXTRA_ATTEMPTS_PER_POSITION",
    "SETTLE_BELOW_POSITION_FLOOR",
    "SETTLE_GROUP_CLOSE_REQUIRED",
    "SETTLE_GROUP_KINDS",
    "SETTLE_KEPT_EARLIER_TAKE",
    "SETTLE_KINDS",
    "SETTLE_PHASE_CANNOT_PROCEED",
    "SETTLE_POSITION_UNRESOLVED",
    "SETTLE_RETRY_REMAINS",
    "SETTLE_SLOT_KINDS",
    "AttemptOverspendError",
    "BeginDecision",
    "SlotAttempts",
    "assess_begin",
    "extra_initiator",
    "extras_spent_message",
    "pilot_heard_for",
    "reflection_measured_for",
    "settle_group_position",
    "settle_spent_slot",
    "spent_slot_outcome",
]


# --------------------------------------------------------------------------- #
# The bounded-retry ruling (owner, 2026-08-03, issue #2086)
# --------------------------------------------------------------------------- #
#
# One prompted position gets its PLANNED capture plus at most this many EXTRA
# attempts, POOLED across everyone who can ask for one: the household's "Try
# again" and voluntary retakes, and the conductor's own geometry retakes. In the
# owner's words: *"We need a finite bound: do up to three more measurements to
# see if we can get a read. If we still can't, attribute it to X."*
#
# What this replaces: a per-REASON-CODE budget (``ReasonSpec.retry_budget``,
# 0-2) measured against a cumulative per-slot attempt count. Two measured
# defects fell out of that shape and killed two live sessions on 2026-08-03
# (#2083 entries 4 and 6):
#
# * an ACCEPTED capture left the counter standing, so one voluntary retake of a
#   healthy position could start at zero headroom;
# * each reason code held its OWN counter while all of them drew on the same
#   plan attempts and the same operator, so alternating conditions walked the
#   meter to 12 attempts behind a screen still reading "step 6".
#
# One pooled counter with an honest surface is the whole replacement. It is
# deliberately NOT derived from ``ReasonSpec.retry_budget``: the point of the
# ruling is that the bound belongs to the POSITION and the household's patience,
# not to whichever condition happened to fire last.
MAX_EXTRA_ATTEMPTS_PER_POSITION = 3

# Who asked for one extra attempt. Pooled against the single bound above (the
# ruling is explicit that the bound is shared), but recorded separately so the
# count the household reads is truthful about who spent what — "the speaker used
# 2 extra tries; you have 1 left" rather than a bare total that reads as though
# the household burned them.
#
# The split is observed at the REJECTION that kept the plan alive, never at the
# relay's ``retake`` flag: a geometry rung does not travel the retake path at
# all (it rejects a good capture so the runner stays on the same index — see
# ``GEOMETRY_RETRY_POSITIONS``' own note), so every geometry rung on 2026-08-03
# was authorized with ``retake=false``. Reading the flag would have attributed
# every system-forced take to the household.
ATTEMPT_INITIATOR_HOUSEHOLD = "household"
ATTEMPT_INITIATOR_SPEAKER = "speaker"


class AttemptOverspendError(RuntimeError):
    """A slot was charged an extra attempt it did not have.

    Module-local for the reason :class:`~.programs.NoProgramForPhaseError` is:
    the flow's ``CrossoverV2FlowError`` is the vocabulary every caller already
    handles, and a pure ledger has no business knowing it. The conductor
    translates at the one call site.
    """


#: :attr:`BeginDecision.kind` — admit this begin (``spends_extra`` says whether
#: it costs one of the position's extras, and ``initiator`` who is charged).
ADMIT = "admit"
#: Hold the begin: VERIFY is soft-held until an apply is observed.
DEFER_AWAITING_APPLY = "defer_awaiting_apply"
#: Refuse: the auto-apply hit a TERMINAL failure, named by ``code``.
REFUSE_APPLY_FAILED = "refuse_apply_failed"
#: Refuse: the slot's last rejection was a condition another take cannot clear.
REFUSE_NON_RETRIABLE = "refuse_non_retriable"
#: Refuse: the slot's extras are gone (the backstop — see :func:`assess_begin`).
REFUSE_EXTRAS_SPENT = "refuse_extras_spent"

#: Every kind :func:`assess_begin` can return.  Declared so the flow's handling
#: can be VERIFIED rather than trusted — the discipline
#: :data:`.spatial.SCREEN_KINDS` and :data:`.coordinator.REFUSAL_KINDS` already
#: keep, and the reason a kind added here without an arm in ``authorize_begin``
#: is caught by a test rather than by a household.
#:
#: **The unhandled direction is REFUSE.**  A begin gate's catch-all decides
#: whether an unrecognised answer starts a capture, and the honest default for
#: an admission decision nobody wired is to not admit: an extra take costs the
#: household a try it may need, while a refusal costs it a retry it can make
#: again.  The flow's fallback says so loudly for exactly that reason.
DECISION_KINDS = frozenset({
    ADMIT,
    DEFER_AWAITING_APPLY,
    REFUSE_APPLY_FAILED,
    REFUSE_NON_RETRIABLE,
    REFUSE_EXTRAS_SPENT,
})


@dataclass
class SlotAttempts:
    """One prompted position's attempt ledger (owner ruling #2086).

    The single meter for a slot. ``admitted`` counts every attempt the
    conductor let start — the first is the PLANNED capture and is free; each
    one after it spends an extra against
    :data:`MAX_EXTRA_ATTEMPTS_PER_POSITION`, attributed to whoever asked.

    An ACCEPTED capture never adds to ``by_household``/``by_speaker`` beyond
    the extra its own admission already spent, and the planned capture adds
    nothing at all — so a position measured cleanly on the first take still has
    its full three extras available if the household chooses to redo it. That
    is the "accepted captures never consume retry budget" half of the ruling:
    before it, acceptance left the old cumulative counter standing and one
    voluntary retake of a healthy position could start at zero headroom.

    Mutable on purpose (the frozen ``PhaseVerdict`` in the flow is a value;
    this is per-session state the conductor advances).
    """

    admitted: int = 0
    by_household: int = 0
    by_speaker: int = 0

    @property
    def extras_used(self) -> int:
        return self.by_household + self.by_speaker

    @property
    def extras_left(self) -> int:
        return max(0, MAX_EXTRA_ATTEMPTS_PER_POSITION - self.extras_used)

    def spend(self, initiator: str) -> None:
        """Charge one extra attempt to ``initiator``.

        Callers gate on :attr:`extras_left` first; an unchecked overspend would
        be a bug, so it raises rather than silently capping — a meter that lies
        about its own total is the defect this class replaces.
        """
        if self.extras_left <= 0:
            raise AttemptOverspendError(
                "slot has no extra attempts left "
                f"({self.extras_used}/{MAX_EXTRA_ATTEMPTS_PER_POSITION})"
            )
        if initiator == ATTEMPT_INITIATOR_SPEAKER:
            self.by_speaker += 1
        else:
            self.by_household += 1

    def to_payload(self) -> dict[str, Any]:
        """The honest count, as the phone renders it (ruling item 2).

        Numbers only — the page composes the eyebrow ("Measurement 6 of 6 —
        extra try 2 of 3") because the §2.1 screen grammar makes the counter
        the page's slot, and a second sentence written here would be the same
        fact stated twice. ``by_speaker`` is what makes the count truthful
        about who spent what.
        """
        return {
            "used": self.extras_used,
            "allowed": MAX_EXTRA_ATTEMPTS_PER_POSITION,
            "left": self.extras_left,
            "by_speaker": self.by_speaker,
            "by_household": self.by_household,
        }


@dataclass(frozen=True)
class BeginDecision:
    """What :func:`assess_begin` concluded about one ``begin_capture``.

    ``code`` is an opaque reason token on every refusal — the flow renders the
    sentence the household reads from its own registry. ``spends_extra`` and
    ``initiator`` are meaningful only on :data:`ADMIT`, and the conductor
    performs the charge; deciding who pays and taking the payment are the two
    halves this module keeps apart.
    """

    kind: str
    code: str = ""
    spends_extra: bool = False
    initiator: str = ""


def extra_initiator(last_reason: str | None, *, geometry_locked_code: str) -> str:
    """Who is asking for the extra attempt about to be admitted.

    Read off the rejection that kept the plan alive at this slot, because
    that is the only place the distinction is visible: a geometry rung is
    the conductor demanding a wider take of a capture that was otherwise
    fine, and it travels the ordinary begin path with ``retake=false``
    (see ``GEOMETRY_RETRY_POSITIONS`` — rejecting is the only lever
    that keeps a fixed-length plan on the same index). Everything else —
    a "Try again" after a quality rejection, a voluntary retake — is the
    household choosing to spend one.

    ``geometry_locked_code`` is stated rather than imported for the reason the
    module docstring gives: the reason codes are the flow's.
    """
    return (
        ATTEMPT_INITIATOR_SPEAKER
        if last_reason == geometry_locked_code
        else ATTEMPT_INITIATOR_HOUSEHOLD
    )


def extras_spent_message(
    ledger: SlotAttempts, *, diagnosis: str, outcome: str,
) -> str:
    """The household sentence for a position whose extras are gone.

    Keeps an evidence-derived diagnosis when one exists, then names the
    count and terminal outcome. It deliberately does NOT reuse the full
    registry ``message``: retriable rows end by inviting an action the
    flow will no longer grant.
    """
    used = ledger.extras_used
    tries = "try" if used == 1 else "tries"
    count = (
        f"JTS measured this spot {ledger.admitted} times — the planned one "
        f"plus {used} extra {tries} — and still could not get a clean read."
    )
    return " ".join(part for part in (diagnosis, count, outcome) if part)


def spent_slot_outcome(
    *,
    is_group: bool,
    index: int,
    unresolved: Container[int],
    retained: Container[int],
) -> str:
    """The state after an exhausted slot, derived from conductor state.

    The three facts arrive stated: whether the phase is a position group, and
    — for a group — whether this index was left out or is still represented by
    an earlier take. The conductor reads them off ``_group_unresolved`` and
    ``_retained_group_indexes``, which remain its own.
    """
    if is_group:
        if index in unresolved:
            return "This position was left out and the group continued."
        if index in retained:
            return (
                "JTS kept the earlier measurement for this position and "
                "the group continued."
            )
        return (
            "The measurement cannot continue because too few positions "
            "produced a clean read."
        )
    return "The measurement cannot continue because this step needs a clean read."


def pilot_heard_for(
    code: str | None, paired: tuple[str, bool | None, bool | None] | None,
) -> bool | None:
    """The pilot evidence recorded WITH ``code``, else ``None`` (#2085).

    ``paired`` is the ``(code, pilot_heard, reflection_measured)`` triple the
    conductor holds for the capture position being described — or, for the
    global form the persisted terminal state uses, the triple assembled from
    its ``_last_failure_*`` pair. Both forms re-check the code because the
    failure being described is not always the failure last consumed: the flow's
    ``_refuse`` can name a code the capture loop never produced, and a replayed
    begin can address an older slot. Attaching one capture's evidence to
    another's code would put a confident, wrong sentence in front of a
    household. An unknown pairing degrades to the registry copy, which claims
    nothing unmeasured.
    """
    if code is None or paired is None or paired[0] != code:
        return None
    return paired[1]


def reflection_measured_for(
    code: str | None, paired: tuple[str, bool | None, bool | None] | None,
) -> bool | None:
    """The gate discriminator recorded with ``code`` at this position."""
    if code is None or paired is None or paired[0] != code:
        return None
    return paired[2]


def assess_begin(
    *,
    verify_hold: bool,
    apply_failure_code: Callable[[], str],
    ledger: SlotAttempts | None,
    last_reason: str | None,
    non_retriable: Container[str],
    default_code: str,
    geometry_locked_code: str,
) -> BeginDecision:
    """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7).

    ``verify_hold`` is the conductor's "this is VERIFY and no apply has been
    observed" — VERIFY is soft-held until one is. **Since the two-stage split
    (work order D10) no shipped session reaches that hold**: stage 1 has no
    VERIFY index at all, and stage 2's conductor is constructed
    ``applied=True``, so the flag is false before either the deferral or the
    ``apply_failed`` refusal below. The machinery is retained rather than
    deleted — no new design may depend on it, and a conductor built without a
    prior apply still gets the honest hold. If the auto-apply hit a TERMINAL
    failure (``apply_failure_code`` names a reason), the hold is refused
    outright rather than held toward a dishonest relay_timeout — the household
    sees the real reason, not a manufactured "link timed out." Every other
    begin is admitted.

    **Retry exhaustion does NOT normally arrive here** (owner ruling #2086). A
    slot that has spent its extras is settled at the verdict that spent the
    last one — the flow's ``_resolve_spent_slot`` either drops the position and
    advances the group, or names the honest end — so the household is never
    handed a "try again" screen whose button is about to end the session.
    :data:`REFUSE_EXTRAS_SPENT` is the backstop for a begin that reaches a
    settled slot anyway (a page that ignored the verdict, a replayed event),
    and the copy the flow renders for it says the tries are gone rather than
    inviting one more.

    The returned ``code`` on :data:`REFUSE_EXTRAS_SPENT` is the condition
    actually observed at this slot — never a generic exhaustion code that would
    erase what went wrong — which is what keeps the household's sentence and
    the persisted terminal failure describing the same thing.
    """
    if verify_hold:
        failure_code = apply_failure_code()
        if failure_code:
            return BeginDecision(REFUSE_APPLY_FAILED, code=failure_code)
        return BeginDecision(DEFER_AWAITING_APPLY)
    # ONE pooled meter per slot: the planned capture, then at most
    # MAX_EXTRA_ATTEMPTS_PER_POSITION extras, whoever asks for them. The
    # first attempt of any slot is always admitted and always free — and a slot
    # with no meter yet has had none, which is why ``ledger`` is read rather
    # than created. Nothing is charged before the answer is ADMIT, so the hold
    # above leaves no ledger entry behind for a capture that never started.
    if ledger is None or not ledger.admitted:
        return BeginDecision(ADMIT)
    # The ``is not None`` half narrows the code this returns and changes no
    # answer: the flow passes a ``frozenset[str]``, in which ``None`` is never
    # a member, so the membership test alone already excluded it.
    if last_reason is not None and last_reason in non_retriable:
        # Not exhaustion — a condition another take cannot clear. Its own copy
        # already names the one action that helps, so the flow publishes it
        # unchanged (and it never promised "measure again").
        return BeginDecision(REFUSE_NON_RETRIABLE, code=last_reason)
    if ledger.extras_left <= 0:
        return BeginDecision(REFUSE_EXTRAS_SPENT, code=last_reason or default_code)
    return BeginDecision(
        ADMIT,
        spends_extra=True,
        initiator=extra_initiator(
            last_reason, geometry_locked_code=geometry_locked_code
        ),
    )


# --------------------------------------------------------------------------- #
# the settle ladder — a position whose meter is empty (#2291 Phase 5b)
# --------------------------------------------------------------------------- #
#
# The other half of the bounded-retry ruling. :func:`assess_begin` answers "may
# one more capture start"; this answers "the last one is spent — what is the
# honest outcome". Owner ruling #2086 item 3 put the answer HERE, at the verdict
# that spent the extra, rather than at the next begin: a household must never be
# shown a retry screen whose button only leads to a pre-play refusal, which is
# the 2026-08-03 shape (a screen reading "step 6, one last time" over a slot the
# meter had already closed).

#: The slot still has extras. Nothing settles; the household retries as before.
SETTLE_RETRY_REMAINS = "retry_remains"
#: A single-capture phase with nothing left to spend: this take is the last word.
SETTLE_PHASE_CANNOT_PROCEED = "phase_cannot_proceed"
#: A position group — the outcome needs the group's own lock-guarded facts, so
#: the ladder continues in :func:`settle_group_position` under the caller's
#: close lock. Not an outcome: the one kind that names a rung rather than an end.
SETTLE_GROUP_CLOSE_REQUIRED = "group_close_required"
#: An earlier take of this position is still standing — nothing was lost.
SETTLE_KEPT_EARLIER_TAKE = "kept_earlier_take"
#: Too few curves in hand and too few positions left to reach the floor.
SETTLE_BELOW_POSITION_FLOOR = "below_position_floor"
#: Drop this position, record the observed condition against it, and advance.
SETTLE_POSITION_UNRESOLVED = "position_unresolved"

#: What :func:`settle_spent_slot` can answer — the rungs decided before the
#: caller takes its close lock.
SETTLE_SLOT_KINDS = frozenset({
    SETTLE_RETRY_REMAINS,
    SETTLE_PHASE_CANNOT_PROCEED,
    SETTLE_GROUP_CLOSE_REQUIRED,
})

#: What :func:`settle_group_position` can answer — the three outcomes only a
#: position group can produce, decided under that lock.
SETTLE_GROUP_KINDS = frozenset({
    SETTLE_KEPT_EARLIER_TAKE,
    SETTLE_BELOW_POSITION_FLOOR,
    SETTLE_POSITION_UNRESOLVED,
})

#: Every kind the settle ladder can answer with. One vocabulary for one ladder
#: — the split is the LOCK boundary, not a second decision — but the two halves
#: partition it, and the partition is load-bearing: a group kind arriving from
#: the first half is as much a wiring defect as an undeclared one, so each arm
#: is checked against the set of the half that produces it.
SETTLE_KINDS = SETTLE_SLOT_KINDS | SETTLE_GROUP_KINDS


def settle_spent_slot(
    *, ledger: SlotAttempts | None, is_group: Callable[[], bool],
) -> str:
    """Does this rejection settle the position, and can it settle alone?

    The ladder's first two rungs. A slot with extras left (or no meter yet) is
    not settled at all. Once the extras are gone, a single-capture phase is
    decided right here — there is no group to fall back on — while a position
    group's outcome depends on facts that are only true while its close lock is
    held, so it answers :data:`SETTLE_GROUP_CLOSE_REQUIRED` and the caller
    continues into :func:`settle_group_position` under that lock.

    Splitting the ladder there is the lock boundary and nothing else: the group
    rungs read "what has this group retained" and "what is still unwalked", and
    a decision made from those facts outside the lock could be acted on after
    another close had already changed them.

    ``is_group`` is a **callable** for the reason :func:`assess_begin`'s
    ``apply_failure_code`` is one — call count. It reaches the journey plan, and
    the shipped flow does not ask it while a slot still has retries to offer; a
    value resolved at call time would ask it on every rejected capture.
    """
    if ledger is None or ledger.extras_left > 0:
        return SETTLE_RETRY_REMAINS
    return (
        SETTLE_GROUP_CLOSE_REQUIRED if is_group()
        else SETTLE_PHASE_CANNOT_PROCEED
    )


def settle_group_position(
    *,
    index: int,
    retained: Collection[int],
    floor: int,
    unwalked_count: Callable[[], int],
) -> str:
    """The outcome for a spent position of a group — the ladder's last three rungs.

    In order, and the order is the point:

    1. **An earlier take is still standing.** The household retook a position
       that had already been accepted and the retakes failed; a rejection never
       replaces a retained curve, so nothing was lost and this position is not
       unresolved at all. It is the "keep the earlier measurement" escape,
       applied automatically once there is no try left to offer — and it has to
       be asked FIRST, because a position with a curve in hand must never be
       counted as a loss against the floor.
    2. **The group can no longer reach its floor.** Curves in hand PLUS the
       positions the household has not walked yet — never the count so far,
       which would make the answer depend on walk order and end a session at
       position 1 of 8 with seven good spots still ahead.
    3. **Otherwise, drop it and carry on.** The cloud tolerates variable
       position counts down to its floor; below the declared plan length the
       claim is degraded and disclosed, not refused.

    ``retained`` is passed as a collection because both rungs read it — rung 1
    for membership, rung 2 for size — and it is already in the caller's hand.
    ``floor`` is a plain value: it is a total function of the phase alone
    (:func:`~jasper.active_speaker.crossover_v2.spatial.group_position_floor`
    is two constants), so resolving it eagerly can neither raise nor be
    observed. ``unwalked_count`` is a callable for the same call-count reason
    ``is_group`` is: it reaches the journey, and rung 2 is asked only of a
    position rung 1 did not already answer for.

    Called with the caller's close lock held — see :func:`settle_spent_slot`.
    """
    if index in retained:
        return SETTLE_KEPT_EARLIER_TAKE
    if len(retained) + unwalked_count() < floor:
        return SETTLE_BELOW_POSITION_FLOOR
    return SETTLE_POSITION_UNRESOLVED
