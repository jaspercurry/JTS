# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seam between the conductor and ``crossover_v2.admission`` (#2291 5a-vi).

The claims this slice makes that nothing else asserted. Each one is a property
of the SPLIT — what the decision is allowed to know, and what the conductor
must still do — so each is written against the production entry point
(``authorize_begin``) rather than against the pure function, which is where a
wiring mistake would actually show up.

"""

from dataclasses import replace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import admission
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)

from tests._log_events import event_records
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _check_analysis,
    _conductor,
    _run_phase,
)

# Full event names as `jasper.log_event.log_event` renders them — the
# `correction.` domain prefix is part of the exact name `event_records`
# matches on, not a decoration a substring scan could ignore.
UNMAPPED_EVENT = "correction.crossover_v2_begin_decision_kind_unmapped"


@pytest.mark.parametrize(("fault", "budget", "counter", "last_fault", "code"), [
    ({"glitch_detected": True}, admission.MAX_AUTOMATIC_RETAKES_PER_POSITION, "by_speaker",
     {"linearity_ok": False}, refusal_copy.REASON_AGC_BEHAVIORAL_FAIL),
    ({"linearity_ok": False}, admission.MAX_EXTRA_ATTEMPTS_PER_POSITION, "by_household",
     {"glitch_detected": True}, refusal_copy.REASON_DRIFT_BASELINES_DISAGREE),
])
def test_a_spent_budget_stays_terminal_when_the_fault_owner_changes(fault, budget, counter, last_fault, code):
    fakes = FakeSeams()
    fakes.check = lambda program: replace(_check_analysis(program), **fault)
    c = _conductor(fakes)
    for attempt in range(1, budget + 1):
        _run_phase(c, 1, attempt)
    fakes.check = lambda program: replace(_check_analysis(program), **last_fault)
    final = _run_phase(c, 1, budget + 1)
    assert final["attempts"][counter] == budget
    assert final["terminal"] is True and final["next"] == "stop"
    assert final["code"] == code
    with pytest.raises(CaptureBeginRefused) as refused:
        c.authorize_begin(1, budget + 2)
    assert refused.value.code == code


def test_an_overspent_meter_still_raises_the_flows_own_error(monkeypatch):
    """The ledger's refusal reaches callers as ``CrossoverV2FlowError``.

    ``SlotAttempts.spend`` raises a module-local ``AttemptOverspendError``
    now that the ledger is pure — it has no business knowing the flow's error
    type — and the conductor translates at the one call site, exactly as
    ``program_for_phase`` does for the program selector. The path is defensive
    (the decision checks ``extras_left`` first, so a truthful decision never
    reaches an exhausted meter), which is why it takes a poisoned decision to
    reach it, and why nothing else would notice the translation going missing.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    slot = c._slot_of_index(1)
    c._slot_attempts[slot] = flow.SlotAttempts(admitted=1, by_household=3)

    monkeypatch.setattr(
        flow._admission, "assess_begin",
        lambda **_: admission.BeginDecision(
            admission.ADMIT,
            spends_extra=True,
            initiator=admission.ATTEMPT_INITIATOR_HOUSEHOLD,
        ),
    )

    with pytest.raises(flow.CrossoverV2FlowError) as excinfo:
        c.authorize_begin(1, 2)
    assert isinstance(excinfo.value.__cause__, admission.AttemptOverspendError)


def _exhausted_non_retriable(code: str):
    """A conductor at index 1 whose meter is spent AND whose last rejection is
    a condition no further take can clear — the state the precedence turns on."""
    c = _conductor(FakeSeams())
    slot = c._slot_of_index(1)
    c._slot_attempts[slot] = flow.SlotAttempts(
        admitted=1 + flow.MAX_EXTRA_ATTEMPTS_PER_POSITION,
        by_household=flow.MAX_EXTRA_ATTEMPTS_PER_POSITION,
    )
    c._last_reason[slot] = code
    return c


@pytest.mark.parametrize("code", sorted(flow.NON_RETRIABLE_CODES))
def test_a_non_retriable_code_outranks_a_spent_meter(code):
    """Which of two true conditions the household is told about.

    ``assess_begin`` asks "is the last rejection non-retriable?" BEFORE "are the
    extras gone?", and when BOTH hold the answer changes what a household reads:
    the condition's own sentence ("You stopped the measurement…") rather than
    the exhaustion sentence ("JTS measured this spot 4 times… and still could
    not get a clean read"). The second would be false comfort — it says try
    harder about a condition another take cannot clear.

    **The refusal CODE is identical in both orders**, which is why the ordering
    survived every count-based check: ``last_reason`` supplies it either way.
    The sentence is the only observable, so the sentence is what this anchors
    on — the DECLARED registry rendering, not the output of the function under
    test. Swapping the two branches reddens every row here.

    This replaces an evidence claim that did not hold: the slice's original
    mutation row reported this ordering RED, and it was not — the discriminating
    state above never occurs in the suite, so nothing pinned it until now.
    """
    c = _exhausted_non_retriable(code)

    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, 9)

    spec = flow.REASON_REGISTRY[code]
    assert excinfo.value.code == code
    assert excinfo.value.user_message == flow.reason_message(code, spec)
    assert "JTS measured this spot" not in excinfo.value.user_message


def test_every_begin_decision_kind_is_handled(caplog):
    """The catch-all must not answer for a kind nobody wired.

    ``DECISION_KINDS`` is the module's own enumeration; ``authorize_begin``
    gives each an arm. The assertion is the JOURNAL, not the verdict: an
    unmapped kind also produces a refusal, so "some refusal happened" would
    pass for exactly the case this exists to catch — the wrong-property class
    the round-wiring suite documents. Its sibling below proves the event does
    fire for a kind with no arm, so this guard cannot be vacuous.
    """
    c = _conductor(FakeSeams())
    c._slot_attempts[c._slot_of_index(1)] = flow.SlotAttempts(admitted=1)

    with caplog.at_level("INFO"):
        for kind in sorted(admission.DECISION_KINDS):
            decision = admission.BeginDecision(kind, code=flow.REASON_LOCATE_FAILED)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(flow._admission, "assess_begin", lambda **_: decision)
                try:
                    c.authorize_begin(1, 2)
                except (CaptureBeginRefused, CaptureBeginDeferred):
                    pass

    unmapped = event_records(caplog, UNMAPPED_EVENT)
    assert unmapped == [], (
        "a declared decision kind reached the fallback instead of its own arm: "
        f"{[r.getMessage() for r in unmapped]}"
    )


def test_an_unrecognised_begin_decision_kind_refuses_rather_than_admits(caplog):
    """The other half: the fallback exists, it shouts, and it does not admit.

    Reached with a kind no released ``assess_begin`` returns — the shape of the
    future defect. On a BEGIN gate the silent direction is the dangerous one:
    falling through starts a capture and charges a try nobody decided to spend.
    So this asserts the DIRECTION as well as the noise — no arm, no charge, and
    the capture is not armed.
    """
    c = _conductor(FakeSeams())
    slot = c._slot_of_index(1)
    c._slot_attempts[slot] = flow.SlotAttempts(admitted=1)

    with caplog.at_level("INFO"), pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._admission, "assess_begin",
            lambda **_: admission.BeginDecision("a_kind_from_the_future"),
        )
        with pytest.raises(CaptureBeginRefused) as excinfo:
            c.authorize_begin(1, 2)

    unmapped = event_records(caplog, UNMAPPED_EVENT)
    assert [r.levelname for r in unmapped] == ["ERROR"]
    assert excinfo.value.code == flow.REASON_LOCATE_FAILED
    # Not admitted: no extra charged, the meter did not advance, nothing armed.
    assert c._slot_attempts[slot].admitted == 1
    assert c._slot_attempts[slot].extras_used == 0
    assert c.armed_capture is None


def test_the_declared_kinds_are_the_ones_assess_begin_can_return():
    """``DECISION_KINDS`` is a declaration, and a declaration can go stale.

    Every kind the decision actually produces is driven out of it here, so a
    kind that stops being reachable — or one returned but never declared —
    shows up as a set difference rather than as a begin nobody wired an arm for.
    """
    spent = admission.SlotAttempts(
        admitted=1 + admission.MAX_EXTRA_ATTEMPTS_PER_POSITION,
        by_household=admission.MAX_EXTRA_ATTEMPTS_PER_POSITION,
    )

    def ask(**kw):
        base = dict(
            ledger=None, last_reason=None, non_retriable=frozenset({"stopped"}),
            default_code="locate_failed",
        )
        return admission.assess_begin(**{**base, **kw}).kind

    produced = {
        ask(),                                                    # free first take
        ask(ledger=admission.SlotAttempts(admitted=1)),           # spends an extra
        ask(ledger=admission.SlotAttempts(admitted=1), last_reason="stopped"),
        ask(ledger=spent, last_reason="other"),
    }

    assert produced == set(admission.DECISION_KINDS)


def test_the_flow_and_the_module_name_one_ledger():
    """The re-exports are the SAME objects, not a second definition.

    Two suites import ``SlotAttempts`` and ``MAX_EXTRA_ATTEMPTS_PER_POSITION``
    from the flow while the ledger itself now lives in ``admission``. Identity
    is what makes that safe: a copy could drift, and a ledger built through one
    name would not be the ledger the other name's bound checks.
    """
    assert flow.SlotAttempts is admission.SlotAttempts
    assert (
        flow.MAX_EXTRA_ATTEMPTS_PER_POSITION
        is admission.MAX_EXTRA_ATTEMPTS_PER_POSITION
    )
    assert flow.ATTEMPT_INITIATOR_HOUSEHOLD is admission.ATTEMPT_INITIATOR_HOUSEHOLD
    assert flow.ATTEMPT_INITIATOR_SPEAKER is admission.ATTEMPT_INITIATOR_SPEAKER


# --------------------------------------------------------------------------- #
# the settle ladder (#2291 Phase 5b)
# --------------------------------------------------------------------------- #
#
# The same trio the begin gate keeps — declared set, every member handled, an
# unrecognised member loud and CONSERVATIVE — plus the two laziness properties a
# "gather the inputs, then ask" rewrite of the split would lose. The
# conservative direction is inverted here and that inversion is the point: on a
# begin gate, falling through admits a capture nobody authorised; on a settle,
# falling through hands back a retry screen whose button leads to a pre-play
# refusal, which is the 2026-08-03 shape the ruling exists to make unreachable.

SETTLE_UNMAPPED_EVENT = "correction.crossover_v2_settle_kind_unmapped"


def _spent(extras=admission.MAX_EXTRA_ATTEMPTS_PER_POSITION):
    return admission.SlotAttempts(admitted=1 + extras, by_household=extras)


def test_the_declared_settle_kinds_are_the_ones_the_ladder_can_return():
    """``SETTLE_KINDS`` is a declaration, and a declaration can go stale.

    Both directions, as the begin gate's sibling does: every kind the two halves
    actually produce is driven out of them here, so a kind that stops being
    reachable — or one returned but never declared — shows up as a set
    difference rather than as a settle nobody wired an arm for. One vocabulary
    across both halves is what makes the union the right comparison.
    """
    produced = {
        admission.settle_spent_slot(ledger=None, is_group=lambda: False),
        admission.settle_spent_slot(
            ledger=admission.SlotAttempts(admitted=1), is_group=lambda: False,
        ),
        admission.settle_spent_slot(ledger=_spent(), is_group=lambda: False),
        admission.settle_spent_slot(ledger=_spent(), is_group=lambda: True),
        admission.settle_spent_slot(
            ledger=admission.SlotAttempts(admitted=1), is_group=lambda: False,
            code="wiring", non_retriable=frozenset({"wiring"}),
        ),
        admission.settle_group_position(
            index=2, retained={2}, floor=3, unwalked_count=lambda: 0,
        ),
        admission.settle_group_position(
            index=2, retained={1}, floor=3, unwalked_count=lambda: 0,
        ),
        admission.settle_group_position(
            index=2, retained={1}, floor=1, unwalked_count=lambda: 0,
        ),
    }

    assert produced == set(admission.SETTLE_KINDS)
    # The two halves PARTITION the vocabulary, and that is load-bearing: a group
    # kind arriving from the first half is a wiring defect, not a valid answer,
    # so each arm below is checked against the half that produces it.
    assert admission.SETTLE_SLOT_KINDS | admission.SETTLE_GROUP_KINDS == (
        admission.SETTLE_KINDS
    )
    assert not (admission.SETTLE_SLOT_KINDS & admission.SETTLE_GROUP_KINDS)
    assert {
        admission.settle_group_position(
            index=i, retained=r, floor=f, unwalked_count=(lambda u=u: u),
        )
        for i, r, f, u in (
            (2, {2}, 3, 0), (2, {1}, 3, 0), (2, {1}, 1, 0), (2, set(), 4, 1),
        )
    } <= set(admission.SETTLE_GROUP_KINDS)


def test_the_ladder_does_not_ask_the_journey_while_the_slot_has_retries():
    """``is_group`` is a port for call count, and this is the count.

    The shipped flow reads the phase's group-ness only once a slot's extras are
    gone. Passing it as a VALUE — the obvious rewrite — asks the journey plan on
    every rejected capture instead, which is a read the flow does not make.
    Bypassing the port therefore reddens exactly this test.
    """
    asked = []

    assert admission.settle_spent_slot(
        ledger=admission.SlotAttempts(admitted=1),
        is_group=lambda: asked.append("plan") or True,
    ) == admission.SETTLE_RETRY_REMAINS
    assert asked == [], "the journey was asked about a slot that still has tries"

    # And it IS asked once the meter is empty — otherwise the guard above would
    # pass for a port that is never invoked at all.
    assert admission.settle_spent_slot(
        ledger=_spent(), is_group=lambda: asked.append("plan") or True,
    ) == admission.SETTLE_GROUP_CLOSE_REQUIRED
    assert asked == ["plan"]


def test_the_group_rung_does_not_count_unwalked_spots_for_a_retained_one():
    """``unwalked_count`` is the second call-count port.

    A position whose earlier take still stands is answered by rung 1, and the
    shipped flow never reaches the journey for it. Resolving the count eagerly
    would ask on every settled group position, including the one rung 1 has
    already answered for.
    """
    counted = []

    assert admission.settle_group_position(
        index=4, retained={4}, floor=3,
        unwalked_count=lambda: counted.append("walk") or 0,
    ) == admission.SETTLE_KEPT_EARLIER_TAKE
    assert counted == [], "the journey was walked for a position already retained"

    assert admission.settle_group_position(
        index=4, retained={1}, floor=3,
        unwalked_count=lambda: counted.append("walk") or 0,
    ) == admission.SETTLE_BELOW_POSITION_FLOOR
    assert counted == ["walk"]


def test_the_floor_rung_counts_unwalked_spots_not_the_walk_so_far():
    """Rung 2's stated rule, pinned as arithmetic rather than as prose.

    Curves in hand PLUS positions not yet walked. Counting only what is in hand
    would end a session at position 1 of 8 with seven good spots still ahead —
    the failure the rung's comment names — so one retained curve and four
    unwalked spots must clear a floor of three.
    """
    assert admission.settle_group_position(
        index=2, retained={1}, floor=3, unwalked_count=lambda: 4,
    ) == admission.SETTLE_POSITION_UNRESOLVED
    # Same curve in hand, nothing left to walk: now it genuinely cannot reach.
    assert admission.settle_group_position(
        index=2, retained={1}, floor=3, unwalked_count=lambda: 0,
    ) == admission.SETTLE_BELOW_POSITION_FLOOR
    # EXACTLY at the floor still stands. ``MIN_RESOLVED_CLOUD_POSITIONS`` is the
    # fewest positions that still make a cloud, not the fewest that fail — the
    # comparison is strict, and an off-by-one here would end a session that had
    # just enough spots left to finish. Neither case above sits on the boundary,
    # so without this the two phrasings are indistinguishable.
    assert admission.settle_group_position(
        index=2, retained={1}, floor=3, unwalked_count=lambda: 2,
    ) == admission.SETTLE_POSITION_UNRESOLVED
    assert admission.settle_group_position(
        index=2, retained={1}, floor=3, unwalked_count=lambda: 1,
    ) == admission.SETTLE_BELOW_POSITION_FLOOR


def test_the_settle_kinds_are_the_journal_and_payload_words_the_phone_reads():
    """The kind IS the outcome token, not a translation of it.

    ``terminal_outcome`` and the ``position_attempts_spent`` journal line
    carried these exact strings before the ladder was named, and the phone and
    support tooling read them. Declaring the vocabulary must not have renamed
    the wire words — so the constants are pinned to their literals rather than
    only to each other.
    """
    assert admission.SETTLE_PHASE_CANNOT_PROCEED == "phase_cannot_proceed"
    assert admission.SETTLE_BELOW_POSITION_FLOOR == "below_position_floor"
    assert admission.SETTLE_KEPT_EARLIER_TAKE == "kept_earlier_take"
    assert admission.SETTLE_POSITION_UNRESOLVED == "position_unresolved"
    assert admission.SETTLE_CONDITION_NOT_RETRIABLE == "condition_not_retriable"


# --------------------------------------------------------------------------- #
# the condition rung — a rejection the next begin would refuse (#2086)
# --------------------------------------------------------------------------- #


def test_a_non_retriable_rejection_settles_however_many_extras_remain():
    """The ladder's first rung, and the precedence that makes it first.

    A slot with every extra unspent is still settled when the rejection names a
    condition no further take can clear, because ``assess_begin`` would refuse
    that take — so the meter and the gate must not disagree about whether a
    retry exists. Demoting this rung below the meter reddens the first two rows;
    dropping it reddens all three.
    """
    fresh = admission.SlotAttempts(admitted=1)
    non_retriable = frozenset({"wiring"})

    assert admission.settle_spent_slot(
        ledger=fresh, is_group=lambda: False,
        code="wiring", non_retriable=non_retriable,
    ) == admission.SETTLE_CONDITION_NOT_RETRIABLE
    # No meter at all — the first take of a position — settles the same way.
    assert admission.settle_spent_slot(
        ledger=None, is_group=lambda: False,
        code="wiring", non_retriable=non_retriable,
    ) == admission.SETTLE_CONDITION_NOT_RETRIABLE
    # And it OUTRANKS a spent meter, exactly as it does at the begin gate: the
    # household reads the condition, never "JTS measured this spot 4 times".
    assert admission.settle_spent_slot(
        ledger=_spent(), is_group=lambda: True,
        code="wiring", non_retriable=non_retriable,
    ) == admission.SETTLE_CONDITION_NOT_RETRIABLE

    # A retriable code with extras left is untouched — the rung is about the
    # condition, not about rejections in general.
    assert admission.settle_spent_slot(
        ledger=fresh, is_group=lambda: False,
        code="quiet_room", non_retriable=non_retriable,
    ) == admission.SETTLE_RETRY_REMAINS
    # …and so are the stated defaults, which is what lets the meter rungs be
    # asked in isolation.
    assert admission.settle_spent_slot(
        ledger=fresh, is_group=lambda: False,
    ) == admission.SETTLE_RETRY_REMAINS
    assert admission.settle_spent_slot(
        ledger=fresh, is_group=lambda: False, code="wiring",
    ) == admission.SETTLE_RETRY_REMAINS


def test_the_condition_rung_does_not_ask_the_journey():
    """``is_group`` stays a call-count port on the new rung too.

    A settled condition ends the phase whatever the phase's shape is, so the
    journey plan is not consulted — the same read the ladder already declines to
    make while a slot has retries left.
    """
    asked: list[str] = []

    assert admission.settle_spent_slot(
        ledger=_spent(),
        is_group=lambda: asked.append("plan") or True,
        code="wiring", non_retriable=frozenset({"wiring"}),
    ) == admission.SETTLE_CONDITION_NOT_RETRIABLE
    assert asked == [], "the journey was asked about a slot the condition closed"


@pytest.mark.parametrize(("budget", "charges"), [
    (0, ["speaker"] * 6), (3, ["speaker"] * 6),
    (4, ["operator", "speaker"] * 3), (1, ["operator"]),
])
def test_one_ledger_bounds_charges_and_reports_the_same_remaining_work(budget, charges):
    ledger = admission.SlotAttempts(admitted=100, retries_per_pose=budget)
    assert ledger.to_payload()["left"] == budget
    for charge in charges:
        assert ledger.can_retry(charge)
        ledger.spend(charge)
    payload = ledger.to_payload()
    assert payload["left"] == 0
    assert payload["by_household"] == charges.count("operator")
    assert payload["by_speaker"] == charges.count("speaker")
    assert not ledger.can_retry("operator")
    for charge in ("operator", "speaker"):
        if not ledger.can_retry(charge):
            with pytest.raises(admission.AttemptOverspendError):
                ledger.spend(charge)
    assert ledger.to_payload() == payload


def test_a_zero_attempt_ledger_gets_a_free_first_attempt():
    """``assess_begin``'s precondition, asserted against the pure function.

    Deliberately the one test in this module NOT written against
    ``authorize_begin`` — because the production wiring cannot reach the branch
    it pins, which is exactly why the branch needed a test before #2291 Phase
    5c-iii could be trusted not to delete it as dead code.

    "No attempts yet" is expressible two ways: no ledger at all, or a ledger
    that exists with ``admitted == 0``. The flow only ever produces the first
    spelling — it holds one :class:`~...admission.SlotAttempts` per slot and
    reaches it through ``setdefault``, which returns the existing entry, so a
    zero-attempt ledger is never handed back. That is a property of one caller.
    ``assess_begin`` is a public pure function, and any caller may construct a
    fresh ledger and ask.

    Both spellings must mean the same thing: ADMIT, free. Were the
    ``not ledger.admitted`` half dropped as unreachable, this call would fall
    through to the extras arithmetic and charge a household's very first
    attempt at a position — spending one of ``MAX_EXTRA_ATTEMPTS_PER_POSITION``
    before the planned capture has happened at all.
    """
    fresh = admission.SlotAttempts()
    assert fresh.admitted == 0

    from_fresh_ledger = admission.assess_begin(
        ledger=fresh,
        last_reason=None,
        non_retriable=frozenset(),
        default_code="unused",
    )
    from_no_ledger = admission.assess_begin(
        ledger=None,
        last_reason=None,
        non_retriable=frozenset(),
        default_code="unused",
    )

    assert from_fresh_ledger.kind == admission.ADMIT
    assert from_fresh_ledger.spends_extra is False
    # The two spellings of "no attempts yet" are the same decision, field for
    # field — including the initiator, which must not be attributed to anyone.
    assert from_fresh_ledger == from_no_ledger


def _lateral_conductor(fakes):
    return _conductor(fakes, index_phase_map=flow.build_v2_cloud_index_phase_map(
        include_lateral=True,
    ))


def _settled_conductor(index):
    conductor = _lateral_conductor(FakeSeams())
    conductor._slot_attempts[conductor._slot_of_index(index)] = _spent()
    return conductor


@pytest.mark.parametrize("unresolved,retained,reads", [
    (False, False, ["unresolved", "retained"]),
    (False, True, ["unresolved", "retained"]),
    (True, False, ["unresolved"]),
    (True, True, ["unresolved"]),
])
def test_the_spent_slot_outcome_tells_left_out_from_kept(monkeypatch, unresolved, retained, reads):
    conductor = _lateral_conductor(FakeSeams())
    seen = []

    class Membership:
        def __init__(self, name, present):
            self.name, self.present = name, present

        def __contains__(self, index):
            seen.append(self.name)
            assert index == 3
            return self.present

    conductor._group_unresolved[PHASE_LATERAL] = Membership("unresolved", unresolved)
    monkeypatch.setattr(conductor, "_retained_group_indexes", lambda phase: Membership("retained", retained))
    conductor._spent_slot_outcome(PHASE_LATERAL, 3)
    assert seen == reads


def test_the_conductor_passes_the_group_ness_port_rather_than_resolving_it(monkeypatch):
    index = 3
    c = _lateral_conductor(FakeSeams())
    slot = c._slot_of_index(index)
    c._slot_attempts[slot] = admission.SlotAttempts(admitted=1)

    asked = []
    real = c._journey.plan.is_group
    monkeypatch.setattr(
        type(c._journey.plan), "is_group",
        lambda self, phase: asked.append(phase) or real(phase),
    )

    verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)
    settled = c._resolve_spent_slot(PHASE_LATERAL, index, slot, verdict)

    assert settled is verdict, "a slot with tries left must settle nothing"
    assert asked == [], (
        "the conductor resolved the group-ness of a phase whose slot still has tries"
    )


def test_the_conductor_passes_the_unwalked_port_rather_than_resolving_it(monkeypatch):
    index = 3
    c = _settled_conductor(index)
    slot = c._slot_of_index(index)

    walked = []
    real = c._journey.unresolved_in_group
    monkeypatch.setattr(
        type(c._journey), "unresolved_in_group",
        lambda self, phase, *, excluding: (
            walked.append(phase) or real(phase, excluding=excluding)
        ),
    )

    monkeypatch.setattr(
        type(c), "_retained_group_indexes", lambda self, phase: {index},
    )

    verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)
    settled = c._resolve_spent_slot(PHASE_LATERAL, index, slot, verdict)

    assert settled.payload["kept_earlier_take"] is True
    assert walked == [], (
        "the conductor walked the journey for a position rung 1 had already answered"
    )


@pytest.mark.parametrize(
    "half,declared",
    [
        ("settle_spent_slot", admission.SETTLE_SLOT_KINDS),
        ("settle_group_position", admission.SETTLE_GROUP_KINDS),
    ],
)
def test_every_settle_kind_is_handled(caplog, half, declared):
    index = 3

    with caplog.at_level("INFO"):
        for kind in sorted(declared):
            c = _settled_conductor(index)
            verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(flow._admission, half, lambda kind=kind, **_: kind)
                c._resolve_spent_slot(
                    PHASE_LATERAL, index, c._slot_of_index(index), verdict,
                )

    unmapped = event_records(caplog, SETTLE_UNMAPPED_EVENT)
    assert unmapped == [], (
        "a declared settle kind reached a fallback instead of its own arm: "
        f"{[r.getMessage() for r in unmapped]}"
    )


def test_an_unrecognised_settle_kind_ends_the_phase_rather_than_retrying(caplog):
    index = 3
    c = _settled_conductor(index)
    verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)

    with caplog.at_level("INFO"), pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._admission, "settle_spent_slot",
            lambda **_: "a_kind_from_the_future",
        )
        settled = c._resolve_spent_slot(
            PHASE_LATERAL, index, c._slot_of_index(index), verdict,
        )

    unmapped = event_records(caplog, SETTLE_UNMAPPED_EVENT)
    assert [r.levelname for r in unmapped] == ["ERROR"]
    assert settled.payload["terminal"] is True
    assert settled.payload["terminal_outcome"] == admission.SETTLE_PHASE_CANNOT_PROCEED
    assert settled.to_capture_dict()["next"] == "stop"

    assert settled is not verdict


def test_an_unrecognised_group_settle_kind_ends_the_phase_rather_than_advancing(caplog):
    index = 3
    c = _settled_conductor(index)
    verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)

    with caplog.at_level("INFO"), pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._admission, "settle_group_position",
            lambda **_: "a_kind_from_the_future",
        )
        settled = c._resolve_spent_slot(
            PHASE_LATERAL, index, c._slot_of_index(index), verdict,
        )

    unmapped = event_records(caplog, SETTLE_UNMAPPED_EVENT)
    assert [r.levelname for r in unmapped] == ["ERROR"]
    assert settled.payload["terminal"] is True
    assert settled.payload["terminal_outcome"] == admission.SETTLE_BELOW_POSITION_FLOOR

    assert index not in c._group_unresolved[PHASE_LATERAL]


@pytest.mark.parametrize("code", sorted(flow.NON_RETRIABLE_CODES))
def test_a_non_retriable_capture_verdict_rides_out_terminal(code):
    c = _lateral_conductor(FakeSeams())
    index = 3
    slot = c._slot_of_index(index)

    c._slot_attempts[slot] = flow.SlotAttempts(admitted=1)

    settled = c._resolve_spent_slot(
        PHASE_LATERAL, index, slot, flow.PhaseVerdict(False, code=code),
    )

    assert settled.payload["terminal"] is True
    assert settled.payload["terminal_outcome"] == (
        admission.SETTLE_CONDITION_NOT_RETRIABLE
    )

    assert settled.code == code
    assert settled.to_capture_dict()["next"] == "stop"
    assert c._slot_attempts[slot].extras_used == 0
    assert index not in c._group_unresolved[PHASE_LATERAL]


def test_the_flow_states_the_condition_inputs_the_ladder_needs():
    c = _lateral_conductor(FakeSeams())
    index = 3
    slot = c._slot_of_index(index)
    c._slot_attempts[slot] = flow.SlotAttempts(admitted=1)
    seen: list[dict] = []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._admission, "settle_spent_slot",
            lambda **kw: seen.append(kw) or admission.SETTLE_RETRY_REMAINS,
        )
        c._resolve_spent_slot(
            PHASE_LATERAL, index, slot,
            flow.PhaseVerdict(False, code=refusal_copy.REASON_CHANNEL_MAP_MISMATCH),
        )

    assert seen[0]["code"] == refusal_copy.REASON_CHANNEL_MAP_MISMATCH
    assert seen[0]["non_retriable"] is flow.NON_RETRIABLE_CODES


def test_a_retriable_rejection_on_a_fresh_slot_still_offers_the_retry():
    c = _lateral_conductor(FakeSeams())
    index = 3
    slot = c._slot_of_index(index)
    c._slot_attempts[slot] = flow.SlotAttempts(admitted=1)
    verdict = flow.PhaseVerdict(False, code=flow.REASON_LOCATE_FAILED)

    settled = c._resolve_spent_slot(PHASE_LATERAL, index, slot, verdict)

    assert settled is verdict
    assert "terminal" not in settled.payload
