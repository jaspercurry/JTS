# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Undo: the one act that puts a household's previous sound back, and its anchor.

**Re-housed, not rewritten** (``docs/REFACTOR-TUNING-2026-08.md`` §3 wave 2b).
Every pin here came verbatim out of ``tests/test_crossover_v2_round_wiring.py``,
where it had been sitting inside a suite the strangler dissolves — while its
subject is the apply/rollback transaction, which §3 marks
**"not a target. Ever."** A pin whose subject never moves must not live in a
file that does; that is the whole change, and no assertion in it was altered.

Three claims, in the order a household meets them:

1. **exactly-once restore, pinned on the COPY.** ``handle_v2_restore`` is not
   idempotent — a success flips ``applied`` off, so a second call refuses — and
   a second asker reading that refusal as a FAILED rollback re-labelled its
   verdict, whose household copy says the correction is **still applied**. It is
   not. That false sentence about their own speaker was the defect, so the
   assertion reaches the string on the screen rather than the reason code, and
   the memo is pinned in BOTH directions: a refused first attempt must keep
   answering "not restored" so the Undo button stays.
2. **``rollback_available`` is BOTH-AND**, over all four seam × anchor
   combinations, plus the two ways an answer can go missing — a seam that raised
   and a seam nobody bound — because each single-half rule is wrong differently
   and each unanswerable shape fails closed for its own reason.
3. **``rollback_anchor_refusal`` has ONE owner.** The round's capability answer
   and the endpoint's refusal must be the same predicate, or a round promises a
   restore that Undo then declines.

**Driven through the production preparers**, not by calling pure functions:
every defect this covers is a wiring defect. ``decide_adoption`` returning
``restore`` is worth nothing if the seam was bound on the other stage, if the
anchor predicate answers a different question than Undo refuses on, or if the
restore runs twice.

The staging is :mod:`tests.crossover_v2_round_harness`, shared with the round
suite. The two autouse fixtures are imported here directly and under the
redundant-alias form: ``pytest`` activates an autouse fixture by its presence in
this module's namespace, nothing here calls one, and the alias says the name is
deliberate without spending a lint suppression.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker import baseline_profile as baseline_profile_mod
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_CORRECTION_MEASURED_REGRESSION,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_REGISTRY,
)
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_round_harness import (
    _bg_run_async,
    _consume_verify,
    _household_sentence,
    _install_applied_graph,
    _install_entry_baseline,
    _post_apply_analysis,
    _restored_ok,
    _restoring_stage_2,
    _seed_round_state,
    _tracking_curve_change_from_entry,
)
from tests.test_crossover_v2_stage_bridge import (
    _flow_seams,
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)


# --------------------------------------------------------------------------- #
# 3. exactly-once restore, pinned on the COPY
# --------------------------------------------------------------------------- #





# Production refuses a session with no volume owner; stand one up.
pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

def test_two_restore_triggers_run_one_undo_and_keep_the_honest_sentence(
    monkeypatch,
):
    """ONE owner, one Undo — and the source proves there is no second.

    Both the round's adoption path and the delta probe's own seam used to ask
    this host to put the previous sound back, and ``handle_v2_restore`` is NOT
    idempotent: a successful restore flips ``applied`` off, so a second call
    refuses with "nothing is applied to undo". The second asker read that
    refusal as a FAILED rollback and re-labelled its verdict
    ``correction_rollback_failed`` — whose household copy says the correction is
    still applied. It is not. That false sentence about their own speaker was
    the defect, and a once-guarded closure was the mitigation.

    **The second owner is now deleted** (the fifth-principle routing): the
    probe reports and ``coordinator._run_round_restore`` is the only caller of
    the rollback seam. So this pins the property the once-guard was standing in
    for — one restore per session — plus the structural fact that makes it hold
    without a guard at all.
    """

    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    # The round's adoption path, on a measured regression.
    first = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert first.code == REASON_CORRECTION_MEASURED_REGRESSION
    sentence = _household_sentence(conductor, first.code)
    still_applied = REASON_REGISTRY[REASON_CORRECTION_ROLLBACK_FAILED].message
    assert "STILL APPLIED" not in sentence
    assert sentence != still_applied

    # A second capture in the same session, carrying a probe verdict that used
    # to fire the seam's own immediate rollback: 2 dB LOUDER than the applied
    # filters commanded, across the whole band. Nothing restores a second time.
    _consume_verify(
        conductor,
        dataclasses.replace(
            _post_apply_analysis(conductor),
            verify_tracking_curve=_tracking_curve_change_from_entry(
                conductor, change_db=-2.0, louder_spike_db=+4.0,
            ),
        ),
        attempt=2,
    )

    assert conductor.delta_probe is not None
    assert conductor.delta_probe.rollback is True, (
        "the probe still MEASURES a rollback class — what moved is who acts"
    )
    # ONE Undo, not two.
    assert attempts == [1]

    # …and there is no second caller left to grow one back. Source-level
    # because that is the actual invariant: a behavioural pin would pass again
    # the moment someone re-added a seam behind a different guard.
    source = Path(flow.__file__).read_text(encoding="utf-8")
    assert "self._seams.rollback(" not in source, (
        "the flow must not call the rollback seam directly — restoring is "
        "coordinator._run_round_restore's, and a second owner is how the "
        "false STILL-APPLIED sentence came back last time"
    )
    assert "the previous sound has been put back" in sentence


def test_the_first_restore_outcome_is_what_a_later_asker_is_handed(monkeypatch):
    """The control: the guard REMEMBERS, it does not merely suppress.

    A guard that returned ``False`` on every repeat would satisfy "one Undo"
    and still produce the false sentence. This pins the remembering half
    directly on the seam both owners share.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    rollback = _flow_seams(conductor).rollback

    assert rollback("first") is True
    assert rollback("second") is True
    assert rollback("third") is True
    assert attempts == [1]


def test_a_refused_first_restore_is_also_remembered_verbatim(monkeypatch, caplog):
    """…and in the other direction, which is the one that must stay loud.

    A first attempt that could NOT restore must keep answering "not restored",
    so the household keeps getting the "still applied" sentence and the Undo
    button. A guard that cached only successes would let a later asker retry
    into a different answer about the same speaker.

    The return values alone cannot see this — a re-attempted Undo on a speaker
    with no anchor refuses identically every time, so ``False`` twice is what a
    MISSING guard produces too (a mutation removing the memo left an earlier
    version of this test green). What separates them is whether the endpoint
    was entered a second time, and the seam already says so in the journal: one
    ``restore_refused`` for the real attempt, then ``restore_repeat`` for every
    asker handed the remembered answer.
    """
    _seed_round_state(anchor=False)  # nothing stashed to go back to
    conductor, attempts = _restoring_stage_2(monkeypatch)
    rollback = _flow_seams(conductor).rollback

    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        assert rollback("first") is False
        assert rollback("second") is False
    assert attempts == []

    lines = [record.getMessage() for record in caplog.records]
    refused = [
        line for line in lines
        if "event=correction.crossover_v2_delta_probe_restore_refused" in line
    ]
    repeats = [
        line for line in lines
        if "event=correction.crossover_v2_delta_probe_restore_repeat" in line
    ]
    assert len(refused) == 1
    assert len(repeats) == 1
    assert "restored=false" in repeats[0]


# --------------------------------------------------------------------------- #
# 4. rollback_available is BOTH-AND
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seam_bound", "anchor", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
    ids=["seam+anchor", "seam-only", "anchor-only", "neither"],
)
def test_rollback_available_needs_both_the_seam_and_the_anchor(
    seam_bound, anchor, expected,
):
    """All four combinations, because each single-half rule is wrong differently.

    Seam-only says yes on a speaker whose durable state carries no
    ``pre_apply_profile``: the round would issue a restore instruction Undo then
    refuses, and the household would be told the old sound was coming back when
    nothing could bring it. Anchor-only ignores that a caller may have no
    rollback binding at all. Pinning only the true corner, or only one false
    one, would leave an ``or`` in place of the ``and`` looking correct.

    Asked of the rule's OWNER (#2291 Phase 5 moved it to the coordinator), and
    of a port set rather than a conductor. That the production conductor hands
    the coordinator these two seams is a different claim, pinned end-to-end by
    :func:`test_a_round_reaches_every_one_of_its_five_seams` and by the restore
    outcomes above it.
    """
    ports = coordinator.RoundPorts(
        rollback=(lambda _cause: True) if seam_bound else None,
        rollback_available=lambda: anchor,
    )

    assert coordinator.rollback_available(ports, session_id="cap_x") is expected


def test_an_anchor_seam_that_raises_fails_closed():
    """"We could not confirm an anchor" is not "there is one".

    The cost of the wrong answer is a restore instruction nothing can carry
    out, which surfaces as ``recovery_required`` anyway — one round later and
    less honestly.
    """

    def _explode() -> bool:
        raise RuntimeError("the durable state is unreadable")

    ports = coordinator.RoundPorts(
        rollback=lambda _cause: True, rollback_available=_explode,
    )

    assert coordinator.rollback_available(ports, session_id="cap_x") is False


@pytest.mark.parametrize(
    ("seam", "expected", "why"),
    [
        (None, True, "no seam bound at all"),
        (lambda: (_ for _ in ()).throw(RuntimeError("unreadable")), True,
         "the seam raised"),
        (lambda: False, False, "the seam answered cut-only"),
        (lambda: True, True, "the seam answered boosted"),
    ],
    ids=["unbound", "raises", "cut-only", "boosted"],
)
def test_an_unreadable_boost_reads_as_boosted(seam, expected, why):
    """``boosted`` fails CLOSED, on both of the two ways it can go unanswered.

    ``boosted`` is what routes an unprovable round to a restore rather than to
    "ask the household" (#2318's fail-closed cell), so the wrong default leaves
    a driver being driven on evidence nobody has. The two unanswerable shapes —
    no seam, and a seam that raised — are the ones no end-to-end round reaches,
    because the production host always binds it and the applied-profile SSOT
    always answers; a mutation flipping either default survived the whole
    round suite before this pin existed.
    """
    ports = coordinator.RoundPorts(applied_boosts=seam)

    assert coordinator.applied_boosts(ports, session_id="cap_x") is expected, why


def test_a_round_reaches_every_one_of_its_five_seams(monkeypatch):
    """The conductor→coordinator port mapping, pinned as reach rather than identity.

    #2291 Phase 5 moved the round's sequencing behind
    :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round`, which is
    handed a narrowed :class:`RoundPorts` instead of the conductor's seams. That
    narrowing is a place two names can be crossed, and most crossings would
    still pass the outcome tests above: a swapped pair usually raises inside a
    guard and fails closed, which several rows here expect anyway.

    So this asserts the weaker fact that no single-outcome test implies — that
    ONE round reaches all five — on a restoring round, the only shape in which
    every seam is live. Comparing the port objects instead would pin the
    assignment and not the call, and the call is what a round is.
    """
    seen: list[str] = []
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    # A baseline FLATTER than the post-apply capture — the graph made the
    # speaker measurably worse — so the table says restore and the rollback
    # seam is live. Every other adoption row leaves at least one seam untouched.
    _install_entry_baseline(conductor, scale=0.5)
    _install_applied_graph(monkeypatch, boosts=False)
    bound = _flow_seams(conductor)

    def _recorded(name: str, seam: Any) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            seen.append(name)
            return seam(*args, **kwargs)
        return _call

    conductor._seams = dataclasses.replace(
        bound,
        **{
            name: _recorded(name, getattr(bound, name))
            for name in (
                "rollback", "rollback_available", "applied_boosts",
                "entry_graph_fingerprint", "publish_round_receipt",
            )
        },
    )

    _consume_verify(conductor, _post_apply_analysis(conductor))

    assert set(seen) == {
        "rollback", "rollback_available", "applied_boosts",
        "entry_graph_fingerprint", "publish_round_receipt",
    }


# --------------------------------------------------------------------------- #
# 5. rollback_anchor_refusal has ONE owner
# --------------------------------------------------------------------------- #


def _topology_mismatched_state() -> dict[str, Any]:
    state = _seed_round_state()
    state["pre_apply_profile"] = {
        "candidate_fingerprint": "fp-previous",
        "source": {"topology_fingerprint": "a-fingerprint-from-another-speaker"},
    }
    v2host.save_v2_state(state)
    return state


@pytest.mark.parametrize(
    ("build_state", "code"),
    [
        (lambda: _seed_round_state(anchor=False), v2host.ANCHOR_NO_PRE_APPLY_PROFILE),
        (_topology_mismatched_state, v2host.ANCHOR_TOPOLOGY_CHANGED),
    ],
    ids=["no-pre-apply-profile", "topology-changed"],
)
def test_each_anchor_refusal_is_a_refusal_undo_really_makes(
    monkeypatch, build_state, code,
):
    """One predicate, two readers — pinned by making both readers answer.

    ``handle_v2_restore`` raises on these preconditions and the round's
    ``rollback_available`` seam asks about them before committing to a restore.
    If they were two transcriptions, a round could promise a restore Undo then
    refuses. So each case asserts the SAME state produces the named refusal
    code, a capability answer of "no", and an endpoint that actually declines
    with that refusal's own sentence.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    state = build_state()

    refusal = v2host.rollback_anchor_refusal(state)

    assert refusal is not None
    assert refusal.code == code
    assert v2host._rollback_anchor_available() is False
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())
    assert str(excinfo.value) == refusal.message


def test_the_not_applied_precondition_is_the_same_one_undo_refuses_on(monkeypatch):
    """The third precondition, separated because it has no anchor to build.

    "Nothing is applied" is reached by state that never applied AND by state a
    successful Undo has just cleared, which is why it must stay a refusal
    rather than a special case of the two above.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    state = _seed_round_state()
    state["applied"] = False
    v2host.save_v2_state(state)

    refusal = v2host.rollback_anchor_refusal(state)

    assert refusal is not None
    assert refusal.code == v2host.ANCHOR_NOT_APPLIED
    assert v2host._rollback_anchor_available() is False
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())
    assert str(excinfo.value) == refusal.message


def test_a_valid_anchor_lets_the_endpoint_through(monkeypatch):
    """The positive control the three refusals need.

    Without it, a predicate that refused unconditionally would satisfy every
    pin above — and would take Undo away from every household.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    _seed_round_state()

    assert v2host.rollback_anchor_refusal(v2host.load_v2_state()) is None
    assert v2host._rollback_anchor_available() is True

    payload = v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())

    assert payload["status"] == "restored"
    # …and the anchor is gone afterwards, which is what makes a second Undo
    # refuse and the once-guard load-bearing.
    assert v2host._rollback_anchor_available() is False
