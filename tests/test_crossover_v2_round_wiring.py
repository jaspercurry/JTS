# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 3c: the round, wired into the host that actually runs it.

The grading itself — the four verdicts and the adoption table — is
:mod:`jasper.active_speaker.crossover_v2.round_evidence`'s and
:mod:`jasper.active_speaker.crossover_v2.verification`'s, and their own tests
pin it as arithmetic. **This module pins the WIRING**: that a real stage-2
conductor, built by the real ``prepare_v2_verify`` with the real host seams
behind it, reaches those answers and then does the right thing to the
household's speaker.

Every pin here is driven through the production host rather than by calling a
pure function, because every defect this phase can still ship is a wiring
defect. ``decide_adoption`` returning ``restore`` is worth nothing if the seam
it depends on was bound on the other stage, if the anchor predicate answers a
different question than Undo refuses on, or if the restore runs twice and the
second refusal tells a household their correction is still applied when it is
not.

What is pinned, in the order a round meets it:

1. **adoption outcomes**, reached through the two-stage host — keep, measured
   regression, and the fail-closed unproven boost;
2. **the round grades the capture the session ENDED on** — a rejected VERIFY
   keeps its own code, burns nothing, and writes no receipt, and the retry that
   lands clean is graded normally;
3. **exactly-once restore**, asserted on the sentence the household READS;
4. **``rollback_available`` is BOTH-AND** — all four seam x anchor combinations;
5. **one owner for the anchor rule** — each refusal code is a refusal Undo
   really makes;
6. **the receipt** — where it lands, that it is fingerprinted, that it reads
   back identical;
7. **a receipt-write failure costs no verdict**;
8. **the model-error store banks the TRACKING number**, not the ledger's grade;
9. **durability, both directions** — the anchor writes fsync, an ordinary
   conductor persist does not.

.. warning::
   This module drives the REAL preparers through
   ``tests/test_crossover_v2_stage_bridge.py``'s harness, so it inherits that
   module's known residue (issue #2312): it must not share a pytest process
   with ``tests/test_correction_crossover_v2_endpoints.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker import baseline_profile as baseline_profile_mod
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.contracts import AdoptionOutcome
from jasper.active_speaker.crossover_v2.round_evidence import (
    EntryBaseline,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.verification import (
    ADOPTION_MEASURED_REGRESSION,
    ADOPTION_REALIZED_AND_IMPROVED,
    ADOPTION_UNPROVEN_BOOST,
)
from jasper.active_speaker.crossover_v2_flow import (
    ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
    REASON_CORRECTION_MEASURED_REGRESSION,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_CORRECTION_UNPROVEN_BOOST,
    REASON_REGISTRY,
    REASON_VERIFY_OUT_OF_TOLERANCE,
    REFERENCE_MARK_DESIGN_AXIS,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.web import correction_crossover_v2 as v2host

# The conductor module's analysis fixtures: the shape the production analyzer
# emits for one summed VERIFY sweep. Imported rather than re-implemented so
# there is one definition of "what a VERIFY capture looks like" — the same
# reason ``tests/test_crossover_v2_entry_baseline.py`` imports them.
from tests.test_crossover_v2_conductor import (
    _in_room_summed_db,
    _verify_analysis,
)

# The stage-bridge harness: one definition of "what a real preparer needs
# stubbed". The two autouse fixtures come with it by name — pytest activates
# an autouse fixture by its presence in this namespace, and nothing here calls
# one, so they are re-exported under the redundant-alias form. That is the
# idiom for "this module-level name is deliberate", and it says so without
# spending a lint suppression against the repo's frozen noqa budget.
from tests.test_crossover_v2_stage_bridge import (
    _COMMANDED_FREQS_HZ,
    _MINTED_RELAY_SESSION_ID,
    _flow_seams,
    _isolated_v2_state as _isolated_v2_state,
    _open_prepared,
    _production_host_seams as _production_host_seams,
    _seed_applied_stage_1_state,
    _status,
)

# --------------------------------------------------------------------------- #
# private reaches, all of them, in one place
#
# The same convention the stage-bridge module states: each helper below reaches
# past a public surface because the fact it touches has no public accessor, and
# each is named for the FACT rather than the attribute so a future public
# property can replace the body without touching a test.
# --------------------------------------------------------------------------- #


def _install_entry_baseline(conductor: Any, *, scale: float) -> EntryBaseline:
    """Give a stage-2 conductor the "before" stage 1 would have handed it.

    Production rehydrates this from the durable bridge key, and
    ``tests/test_crossover_v2_entry_baseline.py`` owns that path. What a ROUND
    test needs is a baseline that is genuinely *comparable* with the post-apply
    capture it will be differenced against — same program id, same grid, same
    mark — and the only way to get that is to build it from THIS conductor's own
    ``_verify_program``, which does not exist until the conductor does. A
    seeded state file cannot: it would have to guess the program id the
    conductor is about to compose, and a lookalike grades
    ``incomparable_program`` instead of grading the speaker.

    ``scale`` multiplies the fixture's in-room deviation, so a scale ABOVE the
    post-apply capture's is a speaker that measurably improved and one below it
    is a measured regression. Higher deviation is worse.
    """
    measured = measured_response_from_analysis(
        _verify_analysis(
            conductor._verify_program, summed_db=_in_room_summed_db() * scale,
        ),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    assert measured is not None
    baseline = EntryBaseline.from_measurement(
        measured,
        graph_fingerprint="fp-entry-graph",
        captured_at="2026-08-10T00:00:00Z",
        artifact_ref="entry_baseline_09_a01",
    )
    conductor._measure_entry_baseline = baseline
    return baseline


def _install_applied_candidate(conductor: Any, *, boosts: bool) -> None:
    """Give a stage-2 conductor the applied candidate it is verifying.

    ``prepare_v2_verify`` constructs its conductor with no ``candidate=``: the
    durable state carries only the applied FINGERPRINT, and stage 2 never
    builds one. The adoption table's ``boosted`` modifier nevertheless asks
    what that candidate's linearization does — through the shipped predicate
    ``camilla_yaml.linearization_has_boost`` — so a round test that wants the
    fail-closed boost cell has to supply one. The shape is the persisted
    ``{role: LinearizationFit.to_dict()}`` mapping the shipped reducer takes,
    not a hand-reduced lookalike, so the predicate under test is the production
    one end to end.
    """
    gain_db = 3.0 if boosts else -3.0
    conductor._candidate = SimpleNamespace(
        fingerprint="fp-stage-1",
        linearization={"woofer": {"filters": [{"gain": gain_db}]}},
        # The rest of the surface ``persist_conductor_state`` reads off an
        # applied candidate. Present so a round test can also persist, not
        # because the round reads any of it.
        analysis={},
        program_id="prog-stage-1",
        role_attenuations_db={"woofer": -3.0, "tweeter": -6.0},
        alignment=SimpleNamespace(to_dict=dict),
        linearization_outcome="fitted",
    )


def _post_apply_analysis(conductor: Any, *, scale: float = 1.0, max_db: float = 0.9):
    """One post-apply VERIFY analysis on this conductor's own program.

    ``max_db`` drives BOTH the flow's tracking gate and the realization verdict
    (they read the same ``max_db_notch_excluded``), so a value inside
    ``VERIFY_TOLERANCE_DB`` is an accepted capture with a MATCHED realization.
    """
    return _verify_analysis(
        conductor._verify_program,
        max_db=max_db,
        summed_db=_in_room_summed_db() * scale,
    )


def _consume_verify(conductor: Any, analysis: Any, *, attempt: int = 1) -> Any:
    """Drive the production VERIFY trigger site.

    ``_consume_verify`` is where the Express tier grades its round, and it is
    the real entry point the relay runner calls — not a test-only shim. Reached
    directly because the runner in between is a thread and a websocket.
    """
    return conductor._consume_verify(analysis, attempt=attempt)


def _round_receipt_json(store: Any, relay_session_id: str) -> dict[str, Any]:
    """The receipt as it sits in the bundle, read off the filesystem.

    Read as bytes rather than through the store's own reader, because "the
    store can find what the store wrote" is not the claim: the claim is that
    the receipt occupies a path a later reader can construct from the round id
    alone. ``EVIDENCE_ROOT/artifacts/`` is the store's strict namespace, which
    the seam's relative path is placed inside.
    """
    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    path = (
        Path(store.bundle_dir)
        .joinpath(*EVIDENCE_ROOT.split("/"))
        / "artifacts"
        / "crossover_v2"
        / relay_session_id
        / "round_receipt.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _seed_round_state(*, anchor: bool = True) -> dict[str, Any]:
    """The durable state a household reaches stage 2 with, post-apply.

    ``anchor`` decides whether an Undo target exists. The stashed profile
    carries no ``source.topology_fingerprint``, which the shipped predicate
    allows through (a stash that predates the fingerprint is validated by the
    restore path itself) — so this is a genuinely valid anchor, not one that
    passes by skipping a check nobody could satisfy here.
    """
    state = _seed_applied_stage_1_state()
    # The seeded entry baseline sits on a five-point grid of its own, which
    # cannot be compared with a real capture. Rounds in this module install a
    # comparable one on the conductor; drop the placeholder so a test that
    # forgets is INDETERMINATE rather than quietly graded against a stranger.
    state["verify_priors"]["entry_baseline"] = None
    if anchor:
        state["pre_apply_profile"] = {"candidate_fingerprint": "fp-previous"}
    v2host.save_v2_state(state)
    return state


@pytest.fixture
def real_bundle(monkeypatch, tmp_path):
    """A REAL write-once evidence bundle behind ``open_v2_evidence_store``.

    The stage-bridge harness substitutes a recorder with no publish method at
    all, which is right for a module about seam bindings and wrong for any pin
    that says something about the receipt — including a pin that NO receipt was
    written, which against that stand-in would be true for the wrong reason.
    The real store is canonical-JSON, write-once, fsync'd, and tamper-checked
    on the way out: the properties a receipt needs.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_2_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"],
    )
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda topology: (store, store.session_id),
    )
    return store


async def _restored_ok(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """The DSP half of Undo, stubbed — and ONLY that half.

    Everything else on the restore path runs for real: the anchor predicate,
    the refusal, ``observe_restore``'s durable clear, and therefore the
    non-idempotence a second Undo meets. That is what makes the exactly-once
    pin below discriminating — a once-guard removed from
    ``bind_delta_probe_rollback`` really does produce the false
    "still applied" sentence, because the second call really does refuse.
    """
    return {"status": "restored"}


def _bg_run_async(coro: Any, *, timeout: Any = None) -> Any:
    import asyncio

    return asyncio.run(coro)


def _restoring_stage_2(monkeypatch) -> tuple[Any, list[int]]:
    """A real stage 2 whose rollback seam can actually complete a restore.

    ``tests/test_crossover_v2_stage_bridge.py``'s ``_stage_2`` passes
    ``run_async=None`` / ``camilla_factory=None``, which is right for a module
    about what crosses the bridge and wrong here: with them the rollback seam
    raises before it reaches ``handle_v2_restore``, and every adoption restore
    would read as a failed one. This binds the real endpoint behind a stubbed
    DSP leg, and returns a counter of how many times that leg actually ran.
    """
    attempts: list[int] = []

    async def _counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        return await _restored_ok(*args, **kwargs)

    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _counted,
    )
    prepared = v2host.prepare_v2_verify(
        {}, status=_status(), run_async=_bg_run_async,
        camilla_factory=lambda: SimpleNamespace(),
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    return conductor, attempts


# --------------------------------------------------------------------------- #
# 1. adoption outcomes, through the REAL two-stage host
# --------------------------------------------------------------------------- #


def test_a_measurably_improved_round_keeps_the_graph_and_the_verdict(monkeypatch):
    """The one keep row, reached by measuring rather than by asserting a table.

    A household whose correction worked must land on the ordinary verified
    screen — the round adds an answer, it does not add a refusal. This is the
    control every restore pin below needs: without it, a wiring bug that
    refused EVERY round would satisfy those tests and fail nobody.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    # 1.5x the deviation before, 1.0x after: measurably flatter, by more than
    # #2291's claim margin.
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_candidate(conductor, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    evaluation = conductor.round_evaluation
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP
    assert evaluation.adoption.reason == ADOPTION_REALIZED_AND_IMPROVED
    # Nothing was put back, because nothing needed to be.
    assert attempts == []


def test_a_measured_regression_restores_and_refuses_under_its_own_code(monkeypatch):
    """The 2026-08-10 shape: tracking passed and the speaker got worse.

    This is the round #2291 exists for. A realization pass must not override a
    measured regression, the graph has to come off, and the household has to be
    told the specific thing that happened — not a generic verify failure and
    not the "still applied" sentence, which would be false about their speaker.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    # 0.4x before, 1.0x after: the speaker measured BETTER before the apply.
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_candidate(conductor, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_MEASURED_REGRESSION
    evaluation = conductor.round_evaluation
    assert evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    assert evaluation.adoption.reason == ADOPTION_MEASURED_REGRESSION
    # The restore genuinely ran, which is what makes the code's copy
    # ("the previous sound has been put back") a true sentence.
    assert attempts == [1]
    assert conductor.round_evaluation is not None


def test_an_unproven_boost_with_a_valid_anchor_comes_back_off(monkeypatch):
    """Fail closed: energy into a driver that nobody can show helped.

    Two facts have to meet for this cell — an INDETERMINATE benefit (here, no
    comparable "before" at all) and a boosting intervention — and the outcome
    has to be the boost's OWN code, not the measured-regression one. The
    difference is a real sentence: nothing measured worse, JTS simply could not
    tell, and said so.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    assert conductor.measure_entry_baseline is None  # no comparable before
    _install_applied_candidate(conductor, boosts=True)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_UNPROVEN_BOOST
    evaluation = conductor.round_evaluation
    assert evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    assert evaluation.adoption.reason == ADOPTION_UNPROVEN_BOOST
    assert attempts == [1]


def test_the_same_unproven_round_without_a_boost_asks_instead_of_restoring(
    monkeypatch,
):
    """The control for the pin above: the modifier is the BOOST, not the doubt.

    Identical evidence, one changed fact — the candidate only cuts. An
    unverified cut can wait for a household to decide, so the table lands on
    ``user_decision``, nothing is restored, and the capture's own verdict
    stands. Without this, a wiring bug that restored every indeterminate round
    would look exactly like the fail-closed boost working.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    assert conductor.measure_entry_baseline is None
    _install_applied_candidate(conductor, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    assert conductor.round_evaluation.adoption.outcome is AdoptionOutcome.USER_DECISION
    assert attempts == []


def test_an_unproven_boost_with_no_anchor_escalates_instead_of_promising(
    monkeypatch,
):
    """A restore nobody can perform is not a restore.

    Same evidence as the fail-closed boost, one changed fact — the speaker has
    no stashed profile to go back to, which is every first-ever apply. The
    table must escalate rather than issue a restore instruction Undo would then
    refuse, and the household's sentence becomes the honest "it is still
    applied, tap Undo" one.
    """
    _seed_round_state(anchor=False)
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_candidate(conductor, boosts=True)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    outcome = conductor.round_evaluation.adoption.outcome
    assert outcome is AdoptionOutcome.RECOVERY_REQUIRED
    # Nothing was attempted, and the record says so rather than implying a
    # restore that silently failed.
    assert attempts == []


# --------------------------------------------------------------------------- #
# 2. the round grades the capture the session ENDED on
# --------------------------------------------------------------------------- #


def test_a_rejected_verify_keeps_its_own_code_and_burns_no_round(
    monkeypatch, real_bundle,
):
    """A capture the household is about to retake is not the round's evidence.

    Two things have to hold at once, and each protects a different household.

    The refusal keeps its OWN code — ``verify_out_of_tolerance``, whose copy
    names the specific thing that went wrong and offers the retry. Replacing it
    with the round's more general code would cost them the actionable half of
    their screen.

    And the round's fire-once guard stays unburned. VERIFY carries a retry
    budget, so a rejected capture does not end the session: grading it would
    spend the one grading on evidence the household then replaced, and the
    receipt — write-once — would describe a capture the round did not end on.
    A session that ends on a terminal rejection writes no receipt at all, which
    is the honest record: its post-apply evidence never completed.

    The fixtures are a regression the round WOULD have restored on, so this
    cannot pass because there was nothing to grade.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_candidate(conductor, boosts=True)

    verdict = _consume_verify(
        conductor,
        _post_apply_analysis(conductor, max_db=flow.VERIFY_TOLERANCE_DB + 1.0),
    )

    assert verdict.accepted is False
    assert verdict.code == REASON_VERIFY_OUT_OF_TOLERANCE
    assert conductor.round_evaluation is None
    assert conductor.round_receipt_identity is None
    # …and nothing landed in the write-once bundle either, which is the fact
    # that actually matters: the receipt cannot be amended later.
    with pytest.raises(FileNotFoundError):
        _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)
    # Nothing was done to the speaker either: the shipped verify-fail path
    # already owns what happens next.
    assert attempts == []


def test_the_retry_after_a_rejected_verify_is_the_capture_that_gets_graded(
    monkeypatch, real_bundle,
):
    """The half that makes the guard worth leaving unburned.

    This is the reproduced defect, as a test: attempt 1 out of tolerance,
    attempt 2 clean and accepted. If the rejected attempt had graded, the
    session would finish carrying that capture's ``realization=FAILED`` and an
    adoption of ``recovery_required`` — demanding operator recovery for a round
    that went on to succeed. The round must describe the capture the household
    actually ended on.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_candidate(conductor, boosts=False)

    rejected = _consume_verify(
        conductor,
        _post_apply_analysis(conductor, max_db=flow.VERIFY_TOLERANCE_DB + 1.0),
    )
    assert rejected.accepted is False

    retry = _consume_verify(conductor, _post_apply_analysis(conductor), attempt=2)

    assert retry.accepted is True
    assert conductor.round_evaluation.adoption.outcome is AdoptionOutcome.KEEP
    assert conductor.round_evaluation.adoption.reason == ADOPTION_REALIZED_AND_IMPROVED
    assert conductor.round_receipt_identity is not None
    assert attempts == []


# --------------------------------------------------------------------------- #
# 3. exactly-once restore, pinned on the COPY
# --------------------------------------------------------------------------- #


def _household_sentence(conductor: Any, code: str) -> str:
    """The verdict text the wizard renders, through the production envelope.

    Not the reason code: the code is an internal identity, and the regression
    that matters is a household reading "the new tuning is STILL APPLIED" about
    a speaker that has already been put back. So the assertion has to reach the
    string on the screen.
    """
    v2host.persist_conductor_state(conductor, failure_code=code)
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2host.crossover_v2_status_block(),
    })
    return str(envelope["verdict_text"])


def test_two_restore_triggers_run_one_undo_and_keep_the_honest_sentence(
    monkeypatch,
):
    """Two owners, one closure, one Undo — and the copy proves it.

    Both the round's adoption path and the delta probe can ask this host to put
    the previous sound back, and ``handle_v2_restore`` is NOT idempotent: a
    successful restore flips ``applied`` off, so a second call refuses with
    "nothing is applied to undo". Without the once-guard the second asker reads
    that refusal as a FAILED rollback and re-labels its verdict
    ``correction_rollback_failed`` — whose household copy says the correction is
    still applied. It is not. That false sentence about their own speaker is
    the defect.

    Asserted on the rendered text rather than on ``rollback(...) is True``
    twice, because the boolean is not what a household reads and a pin on it
    would survive exactly the regression that matters.
    """
    import numpy as np

    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_candidate(conductor, boosts=False)

    # Trigger 1: the round's adoption path, on a measured regression.
    first = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert first.code == REASON_CORRECTION_MEASURED_REGRESSION

    # Trigger 2: the delta probe, on the next capture. On the commanded axis
    # the bridge really rehydrated, the speaker delivered 2 dB less than the
    # applied filters asked for across the whole band — a rollback verdict.
    freqs = np.asarray(_COMMANDED_FREQS_HZ, dtype=float)
    predicted = np.zeros_like(freqs)
    second = _consume_verify(
        conductor,
        dataclasses.replace(
            _post_apply_analysis(conductor),
            verify_tracking_curve=(freqs, predicted - 2.0, predicted),
        ),
        attempt=2,
    )

    assert conductor.delta_probe is not None
    assert conductor.delta_probe.rollback is True
    assert second.accepted is False
    # ONE Undo, not two.
    assert attempts == [1]
    # The sentence FIRST, because it is the claim: the household must not be
    # told their speaker is still corrected when it has already been put back.
    sentence = _household_sentence(conductor, second.code)
    still_applied = REASON_REGISTRY[REASON_CORRECTION_ROLLBACK_FAILED].message
    assert "STILL APPLIED" not in sentence
    assert sentence != still_applied
    assert "the previous sound has been put back" in sentence
    # …which happens because the second asker was handed the FIRST outcome, so
    # its verdict kept its own specific code.
    assert second.code != REASON_CORRECTION_ROLLBACK_FAILED


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
    monkeypatch, seam_bound, anchor, expected,
):
    """All four combinations, because each single-half rule is wrong differently.

    Seam-only says yes on a speaker whose durable state carries no
    ``pre_apply_profile``: the round would issue a restore instruction Undo then
    refuses, and the household would be told the old sound was coming back when
    nothing could bring it. Anchor-only ignores that a caller may have no
    rollback binding at all. Pinning only the true corner, or only one false
    one, would leave an ``or`` in place of the ``and`` looking correct.
    """
    _seed_round_state(anchor=anchor)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    if not seam_bound:
        conductor._seams = dataclasses.replace(_flow_seams(conductor), rollback=None)

    assert conductor._rollback_available() is expected


def test_an_anchor_seam_that_raises_fails_closed(monkeypatch):
    """"We could not confirm an anchor" is not "there is one".

    The cost of the wrong answer is a restore instruction nothing can carry
    out, which surfaces as ``recovery_required`` anyway — one round later and
    less honestly.
    """
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)

    def _explode() -> bool:
        raise RuntimeError("the durable state is unreadable")

    conductor._seams = dataclasses.replace(
        _flow_seams(conductor), rollback_available=_explode,
    )

    assert conductor._rollback_available() is False


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


# --------------------------------------------------------------------------- #
# 6. the receipt
# --------------------------------------------------------------------------- #


def test_the_round_receipt_lands_in_the_bundle_fingerprinted_and_readable(
    monkeypatch, real_bundle,
):
    """Where it is, that it is fingerprinted, and that it reads back identical.

    A receipt is the only thing that lets someone reconstruct a round after the
    household has walked away, so all three facts matter and none implies
    another: a receipt at an unpredictable path cannot be found, one whose
    fingerprint is a decorative string cannot be checked for tampering, and one
    that does not survive its own readback is not a record.

    The fingerprint is RE-DERIVED here from the bytes on disk through the
    contract's own hash, rather than compared to the identity block alone —
    otherwise a receipt could carry any string in that field and still pass.
    """
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_candidate(conductor, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert verdict.accepted is True

    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    assert receipt["round_id"] == _MINTED_RELAY_SESSION_ID
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.KEEP.value
    assert receipt["adoption"]["reason"] == ADOPTION_REALIZED_AND_IMPROVED
    assert receipt["entry_baseline"]["program_id"] == (
        conductor.measure_entry_baseline.program_id
    )
    # Fingerprinted by the contract, not merely stamped: re-hash the payload's
    # own core and it has to come back the same.
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    identity = conductor.round_receipt_identity
    assert identity["round_id"] == _MINTED_RELAY_SESSION_ID
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]
    assert identity["artifact_fingerprint"]


def test_the_receipt_records_what_the_round_DID_not_only_what_it_decided(
    monkeypatch, real_bundle,
):
    """The restore result has to be on it, or a recovery cannot be read back.

    ``_write_round_receipt`` runs LAST for exactly this reason: the receipt is
    the record of an event, and the event includes whether the previous sound
    actually came back. A receipt written before the restore would describe an
    intention.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_candidate(conductor, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))
    assert attempts == [1]

    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value
    assert receipt["restore_result"]["attempted"] is True
    assert receipt["restore_result"]["restored"] is True
    assert receipt["restore_result"]["reason"] == ADOPTION_MEASURED_REGRESSION


# --------------------------------------------------------------------------- #
# 7. a receipt-write failure does not lose the verdict
# --------------------------------------------------------------------------- #


def test_a_failing_receipt_store_costs_the_round_nothing(monkeypatch, caplog):
    """Forensics must never outrank the thing it is forensics about.

    The verdict is what protects the household's speaker; the receipt is what
    lets someone reconstruct why afterwards. A full disk, a tamper-check
    mismatch, or a bundle that closed under us must not reverse a verdict,
    refuse a capture, or crash the capture path — it is a WARN and a journal
    line.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_candidate(conductor, boosts=False)

    def _explode(_receipt):
        raise OSError("no space left on device")

    conductor._seams = dataclasses.replace(
        _flow_seams(conductor), publish_round_receipt=_explode,
    )

    with caplog.at_level("WARNING", logger="jasper.active_speaker.crossover_v2_flow"):
        verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    # The verdict survived, whole.
    assert verdict.accepted is True
    assert conductor.round_evaluation.adoption.outcome is AdoptionOutcome.KEEP
    assert attempts == []
    # The loss is recorded rather than silent, and nothing claims a receipt.
    assert conductor.round_receipt_identity is None
    assert any(
        "event=correction.crossover_v2_round_receipt_failed" in record.getMessage()
        for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# 8. the model-error store banks the TRACKING number
# --------------------------------------------------------------------------- #


def test_the_model_error_store_banks_the_tracking_number_not_the_ledger_grade(
    monkeypatch,
):
    """Two quantities that agree today, and must not share a source.

    ``model_error_store`` owns prediction/realization error; the attempts
    ledger owns the acoustic grade. They read the same tracking scalar right
    now, and that coincidence is the hazard — a future change to what the
    LEDGER grades must not silently change what the STORE banks. The two are
    forced apart here by handing the flow an attempt record whose grade is a
    different number, so an implementation reading ``record.grade_db`` banks
    the wrong one and says so.
    """
    banked: list[dict[str, Any]] = []
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_candidate(conductor, boosts=False)
    conductor._seams = dataclasses.replace(
        _flow_seams(conductor),
        record_model_error=lambda **observation: (
            banked.append(dict(observation)) or True
        ),
    )

    real_record_from_verify = flow.attempt_record_from_verify

    def _record_with_a_different_grade(analysis, *, attempt_id):
        record = real_record_from_verify(analysis, attempt_id=attempt_id)
        return dataclasses.replace(record, grade_db=99.0)

    monkeypatch.setattr(
        flow, "attempt_record_from_verify", _record_with_a_different_grade,
    )

    _consume_verify(conductor, _post_apply_analysis(conductor, max_db=0.7))

    assert len(banked) == 1
    assert banked[0]["metric"] == ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
    assert banked[0]["realized_db"] == pytest.approx(0.7)
    assert banked[0]["predicted_db"] == 0.0


# --------------------------------------------------------------------------- #
# 9. durability, both directions
# --------------------------------------------------------------------------- #


def _recorded_write_calls(monkeypatch) -> list[str]:
    """``atomic_write_text``'s syscall order, as ``tests/test_atomic_io.py`` reads it.

    The shipped ordering pattern, reused rather than re-invented: a durable
    write is ``chmod`` → ``fsync`` (the file) → ``replace`` → ``fsync`` (the
    parent directory), and a cheap one is the same list with both fsyncs
    absent.
    """
    calls: list[str] = []
    real_chmod = os.chmod
    real_replace = os.replace

    def recording_chmod(target, mode):
        calls.append("chmod")
        real_chmod(target, mode)

    def recording_replace(source, target):
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "fsync", lambda _fd: calls.append("fsync"))
    return calls


def test_the_apply_write_that_creates_the_rollback_anchor_is_fsynced(monkeypatch):
    """Power loss here leaves a corrected speaker with no way back.

    ``observe_apply_success`` creates ``pre_apply_profile`` — the only pointer
    Undo restores from — in the same moment the new graph goes live. Atomic is
    not durable: without the fsync a power cut can lose the whole write while
    leaving the DSP graph changed.
    """
    _seed_round_state(anchor=False)
    calls = _recorded_write_calls(monkeypatch)

    v2host.observe_apply_success(
        "fp-stage-1", pre_apply_profile={"candidate_fingerprint": "fp-previous"},
    )

    assert calls == ["chmod", "fsync", "replace", "fsync"]


def test_the_restore_write_that_clears_the_anchor_is_fsynced_too(monkeypatch):
    """The mirror, and it fails the other way round.

    ``observe_restore`` flips ``applied`` off after the previous graph is
    already back. A lost write leaves the state claiming a correction the
    speaker is no longer playing — and an Undo button for a graph that is
    already gone.
    """
    _seed_round_state()
    calls = _recorded_write_calls(monkeypatch)

    v2host.observe_restore()

    assert calls == ["chmod", "fsync", "replace", "fsync"]


def test_an_ordinary_conductor_persist_is_not_fsynced(monkeypatch):
    """The other direction, which is what makes the two above mean anything.

    ``persist_conductor_state`` runs after every consumed capture, and an fsync
    per capture buys nothing the next capture's write does not already redo.
    Pinning only the durable side would pass for a blanket ``durable=True`` and
    leave that cost unguarded on a 1 GB Pi.

    Scoped deliberately to a conductor with no round receipt: the receipt
    identity is its OWN durability trigger (a receipt whose pointer is lost is
    a receipt nobody can resolve), and that branch is a different claim.
    """
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    assert conductor.round_receipt_identity is None
    calls = _recorded_write_calls(monkeypatch)

    v2host.persist_conductor_state(conductor, failure_code=None)

    assert calls == ["chmod", "replace"]
