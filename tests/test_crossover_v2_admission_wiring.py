# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seam between the conductor and ``crossover_v2.admission`` (#2291 5a-vi).

Four claims this slice makes that nothing else asserted. Each one is a property
of the SPLIT — what the decision is allowed to know, and what the conductor
must still do — so each is written against the production entry point
(``authorize_begin``) rather than against the pure function, which is where a
wiring mistake would actually show up.

The first two are ordering properties that a straightforward "gather the
inputs, then ask" rewrite gets wrong. The begin path reads a meter that may not
exist yet and asks a seam that must not be asked on every begin; doing either
eagerly is invisible to every other suite in the tree.
"""

from dataclasses import replace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import admission
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
)
from jasper.capture_relay.session import CaptureBeginDeferred

from tests.test_crossover_v2_conductor import (
    CLOUD_MEASURE_INDEXES,
    FakeSeams,
    _cloud_conductor,
    _conductor,
    _run_phase,
)


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
