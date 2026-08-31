# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for the automatic-rollback seam (``bind_delta_probe_rollback``).

The measured-regression restore rides the NORMAL path: republish the prior
candidate by fingerprint, then apply it through the ordinary apply door. This
file pins the thin adapter that presses those two doors — target resolution,
door order and argument threading, the fail-closed status mapping, the refusal
swallow, the propagation boundary, and the journal events.

**Not re-pinned here**, because they already are: the two doors' own gates and
transactions are covered by ``tests/test_crossover_v2_candidate_republish.py``
and ``tests/test_correction_crossover_v2_endpoints.py``; the round-level
outcomes the seam feeds are ``tests/test_crossover_v2_round_wiring.py``'s.
The doors are substituted rather than driven: the seam's contract is *what it
does with their outcomes*.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_republish as republish_door

RESTORE_EVENT = "correction.crossover_v2_delta_probe_restore"
REFUSED_EVENT = "correction.crossover_v2_delta_probe_restore_refused"

RUN_ASYNC = object()
CAMILLA_FACTORY = object()
PREVIOUS = "fp-previous-measured"


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)


CURRENT = "fp-current-measured"


def _seed_previous_candidate(*, paired: bool = True) -> None:
    """A durable state that records a prior measured candidate.

    ``paired`` stamps the pointer's pairing at the published candidate — the
    armed shape every graded round meets. ``False`` is a pointer inherited
    from an OLDER apply (#2559's staleness class) or already consumed by the
    automatic revert (the [revert…next-apply] window).
    """
    v2host.save_v2_state({
        "session_id": "cap_x",
        "applied": True,
        "candidate": {"fingerprint": CURRENT},
        "previous_candidate_fingerprint": PREVIOUS,
        "previous_candidate_displaced_by": CURRENT if paired else "fp-older-apply",
    })


def _bound(monkeypatch, *, republish: Any = None, apply: Any = None):
    """Bind the seam over substituted republish/apply doors.

    Each outcome is a payload mapping to return or an exception to raise;
    ``None`` is the door's success shape. Records every door press with its
    arguments so the delegation itself is observable.
    """

    calls: list[tuple] = []

    def _fake_republish(raw, **_kwargs):
        calls.append(("republish", dict(raw)))
        if isinstance(republish, BaseException):
            raise republish
        return {"status": "republished"} if republish is None else republish

    def _fake_apply(raw, run_async, camilla_factory, *, status):
        del status
        calls.append(("apply", dict(raw), run_async, camilla_factory))
        if isinstance(apply, BaseException):
            raise apply
        return {"status": "applied"} if apply is None else apply

    monkeypatch.setattr(republish_door, "handle_v2_republish", _fake_republish)
    monkeypatch.setattr(v2host, "handle_v2_apply", _fake_apply)
    monkeypatch.setattr(backend, "status_payload", lambda: {"stubbed": True})
    # The displacement gate's SSOT read, absent by default so each test states
    # its own displacement posture explicitly.
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )
    return v2host.bind_delta_probe_rollback(RUN_ASYNC, CAMILLA_FACTORY), calls


def test_the_probes_rollback_presses_the_two_normal_doors_in_order(monkeypatch):
    """One restore path, not a second one that could drift from it.

    The docstring's whole claim is that the automatic caller and the operator's
    way back press the same doors. Pinned by observing both presses — republish
    FIRST, carrying the recorded prior candidate's fingerprint, then the apply
    door with the very ``run_async``/``camilla_factory`` the seam was bound
    with and the SAME fingerprint as its review expectation. A refactor that
    rebound a private restore, swapped the order, or resolved a different
    identity for either door fails here.
    """
    _seed_previous_candidate()
    rollback, calls = _bound(monkeypatch)

    assert rollback("model_error") is True
    assert calls == [
        ("republish", {"fingerprint": PREVIOUS}),
        (
            "apply",
            {"expected_candidate_fingerprint": PREVIOUS},
            RUN_ASYNC,
            CAMILLA_FACTORY,
        ),
    ]


def test_no_recorded_prior_candidate_refuses_before_either_door(monkeypatch):
    """The state moved between the decision and the act; nothing is pressed.

    ``rollback_available`` answered the adoption table from the same field, so
    reaching this arm means the record changed underneath the round. The seam
    must answer "not restored" without republishing anything — a republish
    aimed by a guess would move the published-candidate slot on a speaker
    whose way back is already gone. The seeded state carries only a LEGACY
    pre-apply stash, which is also the migration pin: a state written before
    the flat pointer existed reads as "no previous candidate".
    """
    v2host.save_v2_state({
        "session_id": "cap_x",
        "applied": True,
        "pre_apply_profile": {"candidate_fingerprint": "fp-prior-baseline"},
    })
    rollback, calls = _bound(monkeypatch)

    assert rollback("model_error") is False
    assert calls == []
    # The state is untouched: nothing was restored, so ``applied`` must not
    # have been cleared by a path that did not restore anything.
    assert v2host.load_v2_state().get("applied") is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"status": "blocked"}, id="blocked"),
        pytest.param({"status": "apply_failed"}, id="apply_failed"),
        pytest.param({}, id="status_absent"),
        pytest.param({"status": "Applied"}, id="wrong_case"),
    ],
)
def test_only_an_applied_status_is_reported_as_restored(monkeypatch, payload: dict):
    """Fail-closed on the return value, which is the whole safety property.

    The conductor reads this bool to decide whether the speaker is back on its
    previous graph. Anything other than the literal ``"applied"`` must read
    False — a seam that returned True on ``blocked`` would tell the journey
    the speaker was recovered while it is still running the regressed graph,
    the exact outcome #2291 lists as "never reported as restored". The last
    two rows are not vocabulary but shape: a payload with no ``status`` at
    all, and one differing only in case.
    """
    _seed_previous_candidate()
    rollback, _ = _bound(monkeypatch, apply=payload)

    assert rollback("model_error") is False


@pytest.mark.parametrize(
    ("door", "presses"),
    [
        pytest.param("republish", 1, id="republish_refused"),
        pytest.param("apply", 2, id="apply_refused"),
    ],
)
def test_an_ordinary_refusal_reports_not_restored_instead_of_propagating(
    monkeypatch, door: str, presses: int,
):
    """``CrossoverV2Refused`` is the ORDINARY outcome for an automatic caller.

    A pruned bank cannot republish, and the apply door carries its own
    refusals. Swallowing either into ``False`` is what lets the conductor's
    refusal still reach the household — if this raised instead, the verdict
    that asked for the rollback would be lost. A republish refusal must also
    stop the sequence: applying after a failed republish would apply whatever
    candidate the slot still holds.
    """
    refusal = v2host.CrossoverV2Refused("that candidate cannot be republished")
    _seed_previous_candidate()
    rollback, calls = _bound(monkeypatch, **{door: refusal})

    assert rollback("model_error") is False
    assert len(calls) == presses, "the refusal must come from a real press"


def test_an_unpaired_pointer_refuses_before_either_door(monkeypatch):
    """The pairing rule's one equality, at the moment of action.

    A pointer whose ``previous_candidate_displaced_by`` is not the published
    candidate was recorded by some OTHER apply — the stale-stash class
    (#2559), or the automatic revert's own consumed pairing — and the AUTO
    path must not follow it. Nothing is pressed: a republish aimed by a stale
    pointer would move the published-candidate slot on evidence that belongs
    to a different apply. The household's way-back button is deliberately not
    gated on the pairing.
    """
    _seed_previous_candidate(paired=False)
    rollback, calls = _bound(monkeypatch)

    assert rollback("model_error") is False
    assert calls == []
    assert v2host.load_v2_state().get("applied") is True


def test_a_displaced_running_graph_refuses_before_either_door(monkeypatch):
    """The 2026-08-15 out-of-band class, closed by the detector we had.

    ``reconcile-current-dsp`` legitimately moves the running config without
    touching the applied-profile record; a revert fired after that would
    replace an operator's deliberate graph. The seam asks
    ``applied_profile_displacement`` at the moment of action and only a
    POSITIVE displacement refuses — the could-not-compare codes proceed,
    because an absent measurement is not evidence of a defect.
    """
    from jasper.active_speaker.baseline_profile import (
        APPLIED_PROFILE_DISPLACED,
        APPLIED_PROFILE_RUNNING_UNKNOWN,
    )

    _seed_previous_candidate()
    rollback, calls = _bound(monkeypatch)
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: {"candidate_fingerprint": CURRENT, "status": "applied"},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda applied, **kwargs: APPLIED_PROFILE_DISPLACED,
    )

    assert rollback("model_error") is False
    assert calls == []

    # The control: a comparison that could not be made is not a displacement.
    unknown_rollback, unknown_calls = _bound(monkeypatch)
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *a, **k: {"candidate_fingerprint": CURRENT, "status": "applied"},
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda applied, **kwargs: APPLIED_PROFILE_RUNNING_UNKNOWN,
    )
    assert unknown_rollback("model_error") is True
    assert len(unknown_calls) == 2


def test_a_successful_revert_consumes_its_own_pairing(monkeypatch):
    """The [revert…next-apply] window, shut from the revert's own side.

    The revert's apply re-stamps the pointer at the graph a round measured
    WORSE; left paired, the next graded round could automatically re-apply it
    (the ping-pong). So a completed revert sets the pairing to ``None`` — the
    pointer survives for the household's button, the AUTO path stays disarmed
    until the next ordinary apply records a fresh pairing.
    """
    _seed_previous_candidate()
    rollback, _ = _bound(monkeypatch)

    assert rollback("model_error") is True
    state = v2host.load_v2_state()
    assert state["previous_candidate_displaced_by"] is None
    # The pointer itself is untouched here (the stubbed apply door does not
    # re-stamp): the household's way back is still on offer.
    assert state["previous_candidate_fingerprint"] == PREVIOUS


@pytest.mark.parametrize(
    ("paired", "preflight_code", "expected"),
    [
        pytest.param(True, None, True, id="paired+admitted"),
        pytest.param(False, None, False, id="unpaired"),
        pytest.param(True, "not_found", False, id="bank-refuses"),
    ],
)
def test_rollback_available_pairs_and_preflights(
    monkeypatch, paired, preflight_code, expected,
):
    """``rollback_available`` means "the automatic restore will not refuse".

    The adversarial review's core finding: a bare "a fingerprint exists"
    re-opened the promise-then-refuse drift #2291 closed. The state half now
    answers the three static questions the action re-asks — pointer recorded,
    paired to the apply under grade, republish door would admit it — so a
    round that cannot restore routes to ``recovery_required`` upfront. The
    live displacement check stays at the moment of action, the same
    static/live split the old probe kept.
    """
    _seed_previous_candidate(paired=paired)
    monkeypatch.setattr(
        republish_door, "republish_preflight", lambda fingerprint: preflight_code,
    )

    assert v2host._previous_candidate_known() is expected


def test_a_wider_failure_still_propagates_rather_than_reading_as_a_clean_no(
    monkeypatch,
):
    """The docstring's "two honest halves", pinned as the second half.

    The seam deliberately does NOT claim to never raise: an ``OSError`` from
    the CamillaDSP socket is not a "we checked and could not restore", and
    ``coordinator._run_round_restore`` owns that wider family on the other
    side of the seam. Widening the ``except`` here would collapse "could not
    run" into the same ``False`` as "ran and refused", and the honest-failure
    distinction #2291 asks for would be gone.
    """
    _seed_previous_candidate()
    rollback, _ = _bound(monkeypatch, apply=OSError("camilla socket is gone"))

    with pytest.raises(OSError):
        rollback("model_error")


def test_each_outcome_is_visible_in_the_journal_under_its_own_event(
    monkeypatch, caplog,
):
    """Observability, because a silent automatic rollback is unauditable.

    Two distinct event names, and a WARNING (not INFO) whenever the speaker
    was NOT restored — an operator reading the journal after a bad round has
    to be able to tell "we put the old tuning back" from "we tried and could
    not" without reading the code.
    """

    caplog.set_level(logging.INFO, logger=v2host.logger.name)

    _seed_previous_candidate()
    rollback, _ = _bound(monkeypatch)
    rollback("model_error")
    # Re-seeded between phases: a completed revert consumes the pointer's
    # pairing, and these three phases each describe a FRESH armed round.
    _seed_previous_candidate()
    rollback_blocked, _ = _bound(monkeypatch, apply={
        "status": "blocked",
        "issues": [
            {"severity": "blocker", "code": "graph_would_clip", "message": "x"},
        ],
    })
    rollback_blocked("model_error")
    _seed_previous_candidate()
    rollback_refused, _ = _bound(
        monkeypatch, republish=v2host.CrossoverV2Refused("bank pruned"),
    )
    rollback_refused("model_error")

    # Matched on the ``event=<name> `` token, not a bare substring: the
    # success event's name is a PREFIX of the refusal's, so a substring test
    # counts every refusal line twice.
    by_event = {}
    for record in caplog.records:
        message = record.getMessage()
        for event in (RESTORE_EVENT, REFUSED_EVENT):
            if f"event={event} " in message:
                by_event.setdefault(event, []).append(record)

    assert len(by_event.get(RESTORE_EVENT, [])) == 2
    assert [r.levelno for r in by_event[RESTORE_EVENT]] == [
        logging.INFO, logging.WARNING,
    ]
    # A blocked apply's cause exists nowhere else — this caller has no screen
    # — so the blocking issue's id rides the probe line (#2519's reasoning).
    assert "code=graph_would_clip" in by_event[RESTORE_EVENT][1].getMessage()
    assert len(by_event.get(REFUSED_EVENT, [])) == 1
    assert by_event[REFUSED_EVENT][0].levelno == logging.WARNING
    # The probe's own reason rides every line, so the journal says WHY the
    # speaker rolled itself back.
    for records in by_event.values():
        for record in records:
            assert "model_error" in record.getMessage()
