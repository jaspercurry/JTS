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

import pytest

from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import admission
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)

from tests._log_events import event_records
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _conductor,
    _run_phase,
)

# Full event names as `jasper.platform.log_event.log_event` renders them — the
# `correction.` domain prefix is part of the exact name `event_records`
# matches on, not a decoration a substring scan could ignore.
UNMAPPED_EVENT = "correction.crossover_v2_begin_decision_kind_unmapped"


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
    c._slot_attempts[slot] = admission.SlotAttempts(admitted=1, by_household=3)

    monkeypatch.setattr(
        flow._admission, "assess_begin",
        lambda **_: admission.BeginDecision(admission.ADMIT),
    )

    with pytest.raises(contracts.CrossoverV2FlowError) as excinfo:
        c.authorize_begin(1, 2)
    assert isinstance(excinfo.value.__cause__, admission.AttemptOverspendError)


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
    c._slot_attempts[c._slot_of_index(1)] = admission.SlotAttempts(admitted=1)

    with caplog.at_level("INFO"):
        for kind in sorted(admission.DECISION_KINDS):
            decision = admission.BeginDecision(kind, code=refusal_copy.REASON_LOCATE_FAILED)
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
    So this asserts the DIRECTION as well as the noise — no arm and no charge.
    """
    c = _conductor(FakeSeams())
    slot = c._slot_of_index(1)
    c._slot_attempts[slot] = admission.SlotAttempts(admitted=1)

    with caplog.at_level("INFO"), pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            flow._admission, "assess_begin",
            lambda **_: admission.BeginDecision("a_kind_from_the_future"),
        )
        with pytest.raises(CaptureBeginRefused) as excinfo:
            c.authorize_begin(1, 2)

    unmapped = event_records(caplog, UNMAPPED_EVENT)
    assert [r.levelname for r in unmapped] == ["ERROR"]
    assert excinfo.value.code == refusal_copy.REASON_LOCATE_FAILED
    # Not admitted: no extra charged, the meter did not advance.
    assert c._slot_attempts[slot].admitted == 1
    assert c._slot_attempts[slot].extras_used == 0


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
        base = dict(ledger=None, default_code="locate_failed")
        return admission.assess_begin(**{**base, **kw}).kind

    produced = {
        ask(),                                                    # free first take
        ask(ledger=admission.SlotAttempts(admitted=1)),           # spends an extra
        ask(ledger=spent),
    }

    assert produced == set(admission.DECISION_KINDS)


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
    """A pose's first take is admitted even with no retries, whether the ledger is absent or fresh."""
    fresh = admission.SlotAttempts(retries_per_pose=0)

    from_fresh_ledger = admission.assess_begin(
        ledger=fresh,
        default_code="unused",
    )
    from_no_ledger = admission.assess_begin(
        ledger=None,
        default_code="unused",
    )

    assert from_fresh_ledger == from_no_ledger == admission.BeginDecision(admission.ADMIT)
