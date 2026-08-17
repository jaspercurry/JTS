# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — preserved-behavior pin for the automatic-rollback seam.

#2291 Phase 3 says "Bind rollback in the stage that executes VERIFY," and its
acceptance criteria require that "a clear measured regression restores the
prior graph exactly once" and that "restore failure produces
``recovery_required`` … it is never reported as restored." Every one of those
runs through ``bind_delta_probe_rollback`` — the closure the conductor calls
when the delta probe measures that an applied correction is not doing what its
filters commanded.

**What exists is binding-level; this is behavior-level.**
``tests/test_crossover_v2_stage_bridge.py``'s
``test_only_stage_1_binds_the_rollback_seam`` asserts *where* the closure is
wired — that stage 1 gets it and stage 2, the stage that actually reaches a
post-apply verdict, does not (a current defect it tags to #2291 Phase 3). What
no test exercises is what the closure *does* once called: neither of its
``event=`` names appears anywhere in ``tests/``, and the conductor's own probe
tests inject a fake ``rollback=lambda reason: ...`` rather than this one. So
the status mapping, the refusal swallow, and the propagation boundary below
are unasserted.

**Not re-pinned here**, because they already are: the apply transaction's
ordering and failure behavior, and ``handle_v2_restore``'s own three refusal
branches and end-to-end apply→Undo round trip, are covered in depth by
``tests/test_active_speaker_baseline_profile.py`` and
``tests/test_correction_crossover_v2_endpoints.py``. This file pins only the
thin adapter between them — which is exactly the piece a strangler would
rewrite when it moves rollback into a stage-capability factory.

``handle_v2_restore`` is substituted rather than driven: the seam's contract
is *what it does with that function's outcome*, and the CamillaDSP-backed
behaviour behind it is another file's subject.
"""

from __future__ import annotations

import logging

import pytest

from jasper.web import correction_crossover_v2 as v2host

RESTORE_EVENT = "correction.crossover_v2_delta_probe_restore"
REFUSED_EVENT = "correction.crossover_v2_delta_probe_restore_refused"

RUN_ASYNC = object()
CAMILLA_FACTORY = object()


def _bound(monkeypatch, outcome):
    """Bind the seam over a substituted ``handle_v2_restore``.

    ``outcome`` is either a payload mapping to return or an exception to
    raise. Records the arguments so the delegation itself is observable.
    """

    calls: list[tuple] = []

    def _fake_restore(run_async, camilla_factory):
        calls.append((run_async, camilla_factory))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(v2host, "handle_v2_restore", _fake_restore)
    return v2host.bind_delta_probe_rollback(RUN_ASYNC, CAMILLA_FACTORY), calls


def test_the_probes_rollback_runs_the_households_own_undo_path(monkeypatch):
    """One restore path, not a second one that could drift from it.

    The docstring's whole claim is that the automatic caller and the Undo
    button press the same button. Pinned by observing that the seam calls
    ``handle_v2_restore`` — with the very ``run_async``/``camilla_factory``
    it was bound with, so a refactor that quietly rebound it to a private
    copy, or dropped the CamillaDSP factory on the floor, fails here.
    """

    rollback, calls = _bound(monkeypatch, {"status": "restored"})

    assert rollback("model_error") is True
    assert calls == [(RUN_ASYNC, CAMILLA_FACTORY)]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"status": "blocked"}, id="blocked"),
        pytest.param({"status": "restore_failed"}, id="restore_failed"),
        pytest.param({}, id="status_absent"),
        pytest.param({"status": "Restored"}, id="wrong_case"),
    ],
)
def test_only_a_restored_status_is_reported_as_restored(monkeypatch, payload: dict):
    """Fail-closed on the return value, which is the whole safety property.

    The conductor reads this bool to decide whether the speaker is back on its
    previous graph. Anything other than the literal ``"restored"`` must read
    False — a seam that returned True on ``restore_failed`` would tell the
    journey the speaker was recovered while it is still running the regressed
    graph, the exact outcome #2291 lists as "never reported as restored."

    ``restore_applied_baseline_profile``'s own docstring is the SSOT for the
    vocabulary and names exactly three statuses: ``{"restored", "blocked",
    "restore_failed"}``. The two failure statuses are rows here; ``restored``
    is the true case covered above. (``restore_target_invalid`` and
    ``restore_target_missing`` are *issue codes* nested inside a ``blocked``
    payload, not statuses — an earlier revision of this test listed them as
    statuses and was wrong.)

    The last two rows are not vocabulary but shape: a payload with no
    ``status`` at all, and one differing only in case. Both must read False,
    because "we could not tell" is not "we restored it".
    """

    rollback, _ = _bound(monkeypatch, payload)

    assert rollback("model_error") is False


def test_an_ordinary_refusal_reports_not_restored_instead_of_propagating(monkeypatch):
    """A first-ever apply has nothing stashed, and that is not an exception.

    ``CrossoverV2Refused`` is the ORDINARY outcome for an automatic caller.
    Swallowing it into ``False`` is what lets the conductor's own refusal
    still reach the household with the Undo button on it — if this raised
    instead, the verdict that asked for the rollback would be lost.
    """

    refusal = v2host.CrossoverV2Refused(
        "this is the first measured crossover on this speaker"
    )
    rollback, calls = _bound(monkeypatch, refusal)

    assert rollback("model_error") is False
    assert calls, "the refusal must come from a real attempt, not a short-circuit"


def test_a_wider_failure_still_propagates_rather_than_reading_as_a_clean_no(
    monkeypatch,
):
    """The docstring's "two honest halves", pinned as the second half.

    The seam deliberately does NOT claim to never raise: an ``OSError`` from
    the CamillaDSP socket is not a "we checked and there was nothing to
    restore", and
    ``coordinator._run_round_restore`` owns that wider family on the other
    side of the seam. Widening the ``except`` here would
    collapse "could not run" into the same ``False`` as "ran and refused",
    and the honest-failure distinction #2291 asks for would be gone.
    """

    rollback, _ = _bound(monkeypatch, OSError("camilla socket is gone"))

    with pytest.raises(OSError):
        rollback("model_error")


def test_each_outcome_is_visible_in_the_journal_under_its_own_event(
    monkeypatch, caplog
):
    """Observability, because a silent automatic rollback is unauditable.

    Two distinct event names, and a WARNING (not INFO) whenever the speaker
    was NOT restored — an operator reading the journal after a bad round has
    to be able to tell "we put the old graph back" from "we tried and could
    not" without reading the code.
    """

    caplog.set_level(logging.INFO, logger=v2host.logger.name)

    rollback, _ = _bound(monkeypatch, {"status": "restored"})
    rollback("model_error")
    rollback_failed, _ = _bound(monkeypatch, {"status": "restore_failed"})
    rollback_failed("model_error")
    rollback_refused, _ = _bound(
        monkeypatch, v2host.CrossoverV2Refused("nothing to undo")
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
    assert len(by_event.get(REFUSED_EVENT, [])) == 1
    assert by_event[REFUSED_EVENT][0].levelno == logging.WARNING
    # The probe's own reason rides every line, so the journal says WHY the
    # speaker rolled itself back.
    for records in by_event.values():
        for record in records:
            assert "model_error" in record.getMessage()
