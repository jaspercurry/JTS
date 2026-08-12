# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seam between the conductor and ``crossover_v2.admission`` (#2291 5a-vi).

The claims this slice makes that nothing else asserted. Each one is a property
of the SPLIT — what the decision is allowed to know, and what the conductor
must still do — so each is written against the production entry point
(``authorize_begin``) rather than against the pure function, which is where a
wiring mistake would actually show up.

The first two are ordering properties that a straightforward "gather the
inputs, then ask" rewrite gets wrong. The begin path reads a meter that may not
exist yet and asks a seam that must not be asked on every begin; doing either
eagerly is invisible to every other suite in the tree.

The kind-vocabulary trio below is the discipline ``spatial.SCREEN_KINDS`` and
``coordinator.REFUSAL_KINDS`` already keep, applied to the decision this slice
introduced: declared set, every member handled, and an unrecognised member loud
and REFUSED rather than quietly admitted.
"""

from dataclasses import replace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import admission
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
)
from jasper.capture_relay.session import CaptureBeginDeferred, CaptureBeginRefused

from tests.test_crossover_v2_conductor import (
    CLOUD_MEASURE_INDEXES,
    FakeSeams,
    _cloud_conductor,
    _conductor,
    _run_phase,
)

UNMAPPED_EVENT = "crossover_v2_begin_decision_kind_unmapped"


def _held_at_verify(fakes: FakeSeams, **kwargs):
    """A conductor parked at the VERIFY anchor with no apply observed."""
    c = _conductor(fakes, **kwargs)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    return c


def test_a_held_begin_leaves_no_meter_behind():
    """The VERIFY hold must not open a ledger for a capture that never ran.

    ``authorize_begin`` reads the slot's meter before it decides, and the
    obvious way to write that read — ``setdefault`` — creates the entry on
    every begin including a held one. A meter that exists with ``admitted=0``
    is not inert: ``_with_attempt_payload`` stamps an ``attempts`` block onto
    every later verdict for that slot where there was none before, and the
    snapshot carries a position the session never measured. Nothing else in
    the tree looks at ``_slot_attempts`` after a hold, so this is the only
    place the difference is visible.
    """
    fakes = FakeSeams()
    c = _held_at_verify(fakes)
    slot = c._slot_of_index(3)

    with pytest.raises(CaptureBeginDeferred) as excinfo:
        c.authorize_begin(3, 3)

    assert excinfo.value.code == "awaiting_apply"
    assert slot not in c._slot_attempts, (
        "a held begin opened a meter for a capture that never started"
    )

    # And the release still opens exactly one, on the take that really begins.
    fakes.apply_done = True
    c.note_apply_complete()
    c.authorize_begin(3, 3)
    assert c._slot_attempts[slot].admitted == 1
    assert c._slot_attempts[slot].extras_used == 0


def test_an_ordinary_begin_never_asks_the_apply_seam():
    """``apply_failed`` is asked on the hold branch and nowhere else.

    It is a host seam — in production it reads the auto-apply thread's verdict
    — so resolving it eagerly to hand the decision a value would put a seam
    call on every begin of every phase. The port exists to keep the call where
    the conductor made it; this counts that it stayed there.
    """
    fakes = FakeSeams()
    asked: list[int] = []

    def counting_apply_failed() -> str:
        asked.append(1)
        return fakes.apply_failed_code

    seams = replace(fakes.seams(), apply_failed=counting_apply_failed)
    c = _conductor(fakes, seams=seams)

    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    assert asked == [], "an ordinary begin asked the apply seam"

    # The hold asks it exactly once — the branch the port exists for.
    with pytest.raises(CaptureBeginDeferred):
        c.authorize_begin(3, 3)
    assert len(asked) == 1


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
    assert "no extra attempts left" in str(excinfo.value)


def test_the_spent_slot_outcome_tells_left_out_from_kept():
    """The three group sentences, and the precedence between the first two.

    Found unguarded by this slice's own mutation batch: swapping the
    ``unresolved`` and ``retained`` reads left 330 conductor tests green. Only
    the third sentence ("too few positions") was asserted anywhere, so a
    position the flow gave up on and a position still covered by an earlier
    take were interchangeable as far as the suite was concerned — two opposite
    things to tell a household, one of which says work was lost when it was
    not. The prose moved verbatim in this slice; the guard is what makes that
    checkable.
    """
    fakes = FakeSeams()
    c = _cloud_conductor(fakes)
    index = CLOUD_MEASURE_INDEXES[0]

    # Neither given up on nor already measured: the group has nothing here.
    assert "too few positions" in c._spent_slot_outcome(PHASE_CLOUD_MEASURE, index)

    # Measured once, so an earlier curve stands.
    _run_phase(c, index, 1)
    assert c._spent_slot_outcome(PHASE_CLOUD_MEASURE, index) == (
        "JTS kept the earlier measurement for this position and "
        "the group continued."
    )

    # Given up on WINS over the retained read — the order is the claim.
    c._group_unresolved[PHASE_CLOUD_MEASURE][index] = flow.REASON_LOCATE_FAILED
    assert c._spent_slot_outcome(PHASE_CLOUD_MEASURE, index) == (
        "This position was left out and the group continued."
    )

    # A single-capture phase has no group to continue with.
    assert c._spent_slot_outcome(PHASE_CHECK, 1) == (
        "The measurement cannot continue because this step needs a clean read."
    )


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

    unmapped = [r.getMessage() for r in caplog.records if UNMAPPED_EVENT in r.getMessage()]
    assert unmapped == [], (
        f"a declared decision kind reached the fallback instead of its own arm: {unmapped}"
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

    unmapped = [r for r in caplog.records if UNMAPPED_EVENT in r.getMessage()]
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
            verify_hold=False, apply_failure_code=lambda: "", ledger=None,
            last_reason=None, non_retriable=frozenset({"stopped"}),
            default_code="locate_failed", geometry_locked_code="geometry",
        )
        return admission.assess_begin(**{**base, **kw}).kind

    produced = {
        ask(),                                                    # free first take
        ask(ledger=admission.SlotAttempts(admitted=1)),           # spends an extra
        ask(verify_hold=True),                                    # the hold
        ask(verify_hold=True, apply_failure_code=lambda: "apply_failed"),
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
