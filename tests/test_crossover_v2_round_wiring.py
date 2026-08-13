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

This module drives the REAL preparers through
``tests/test_crossover_v2_stage_bridge.py``'s harness.  That harness used to
leak fakes into any module that first imported them inside its patched window
(issue #2312), so this file carried a warning not to share a pytest process
with ``tests/test_correction_crossover_v2_endpoints.py``.  #2312 is fixed — the
harness now unwinds every binding by identity, see its own comment — and the
suites co-run green in either order.
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
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.contracts import AdoptionOutcome
from jasper.active_speaker.crossover_v2.round_evidence import (
    EntryBaseline,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.contracts import (
    BenefitStatus,
    RealizationStatus,
    SpecStatus,
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
from tests.crossover_v2_fixtures import (
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
            conductor.program_for_phase(flow.PHASE_VERIFY), summed_db=_in_room_summed_db() * scale,
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


def _install_applied_graph(monkeypatch, *, boosts: bool) -> None:
    """Put a real applied profile on the speaker — boosted, or cut-only.

    **The input the PRODUCTION predicate reads, not an injection into the
    conductor.** An earlier version of this helper assigned
    ``conductor._candidate`` directly, and that is exactly how #2318's
    fail-closed cell stayed green over dead code: stage 2 builds a fresh
    conductor with no ``candidate=`` ctor parameter, so the production read was
    always ``None`` and ``boosted`` was always ``False``, while the suite
    supplied by hand the one thing the shipped path never has.

    So this sets the applied-profile SSOT instead, and every link after it is
    production: ``_applied_graph_boosts`` → ``profile_linearization`` (which
    prefers the recomposition snapshot) → ``camilla_yaml.linearization_has_boost``.
    Stubbing the loader is unavoidable — there is no profile on disk in a
    hardware-free test — but it is the boundary of the system, not a step
    inside the rule under test.

    The mapping is the REDUCED ``{role: [filter_dict, ...]}`` shape a frozen
    profile actually carries, deliberately not the unreduced candidate shape:
    passing the latter through ``linearization_filters_by_role`` would be a
    silent ``{}``, which reads as "this graph boosts nothing".
    """
    gain_db = 3.0 if boosts else -3.0
    monkeypatch.setattr(
        baseline_profile_mod,
        "load_applied_baseline_profile_state",
        lambda *a, **k: {
            "candidate_fingerprint": "fp-live-graph",
            "recomposition_snapshot": {
                "linearization": {"woofer": [{"gain": gain_db}]},
            },
        },
    )


def _post_apply_analysis(conductor: Any, *, scale: float = 1.0, max_db: float = 0.9):
    """One post-apply VERIFY analysis on this conductor's own program.

    ``max_db`` drives BOTH the flow's tracking gate and the realization verdict
    (they read the same ``max_db_notch_excluded``), so a value inside
    ``VERIFY_TOLERANCE_DB`` is an accepted capture with a MATCHED realization.
    """
    return _verify_analysis(
        conductor.program_for_phase(flow.PHASE_VERIFY),
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


def _bare_conductor() -> Any:
    """The smallest real conductor: the refusal mapping reads no session state.

    Built directly rather than through the two-stage host because
    ``_round_refusal_for`` is a pure translation from a coordinator kind to a
    household code — giving it a prepared stage would test the harness.
    """
    from tests.crossover_v2_fixtures import (
        CAPS, FC_HZ, SESSION, SESSION_VOLUME_DB, FakeSeams, _preset, _roles,
    )

    return flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )


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
    _install_applied_graph(monkeypatch, boosts=False)

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
    _install_applied_graph(monkeypatch, boosts=False)

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
    _install_applied_graph(monkeypatch, boosts=True)

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
    _install_applied_graph(monkeypatch, boosts=False)

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
    refuse.
    """
    _seed_round_state(anchor=False)
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    outcome = conductor.round_evaluation.adoption.outcome
    assert outcome is AdoptionOutcome.RECOVERY_REQUIRED
    # Nothing was attempted, and the record says so rather than implying a
    # restore that silently failed.
    assert attempts == []


def test_the_no_anchor_arm_does_not_send_the_household_to_undo(monkeypatch):
    """#2291 SF-A: the remedy on the screen has to be one that EXISTS.

    ``correction_rollback_failed`` covers two situations, and until this was
    branched they shared one sentence ending "Tap Undo to restore the previous
    sound." For the arm that got here BECAUSE there is no stored previous
    sound, Undo refuses on the very predicate that routed them — so the most
    ordinary case there is, a speaker's first-ever correction, was handed a
    dead end.

    Pinned on the rendered string rather than on the code, because the code was
    always right and only the sentence lied.
    """
    _seed_round_state(anchor=False)
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    assert attempts == []

    sentence = _household_sentence(conductor, verdict.code)
    # No pointer at a control that would refuse.
    assert "Undo" not in sentence
    # Still honest about where the speaker actually is.
    assert "still applied" in sentence.lower()
    # …and it names remedies that exist.
    assert "measure again" in sentence.lower()
    assert "Sound page" in sentence


def test_the_attempted_and_failed_arm_keeps_its_undo_pointer(monkeypatch):
    """The other arm of the same code, and the reason it stays a branch.

    Here a stored previous sound DOES exist and the automatic restore failed
    against it, so Undo is a real remedy and the copy should still offer it.
    Pinned beside its sibling because a fix that removed the Undo pointer
    everywhere would satisfy that test and strand this household with no
    action at all.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)
    # The anchor is real, but the restore does not complete.
    monkeypatch.setattr(
        v2host, "handle_v2_restore",
        lambda *a, **k: {"status": "restore_failed"},
    )

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    assert attempts == []  # the stubbed endpoint never reached the DSP leg
    sentence = _household_sentence(conductor, verdict.code)
    assert "Undo" in sentence
    assert "STILL APPLIED" in sentence


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
    _install_applied_graph(monkeypatch, boosts=True)

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
    _install_applied_graph(monkeypatch, boosts=False)

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
    _install_applied_graph(monkeypatch, boosts=False)

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
    _install_applied_graph(monkeypatch, boosts=False)

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

    :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round` writes the
    receipt LAST for exactly this reason: the receipt is the record of an event,
    and the event includes whether the previous sound actually came back. A
    receipt written before the restore would describe an intention.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))
    assert attempts == [1]

    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value
    assert receipt["restore_result"]["attempted"] is True
    assert receipt["restore_result"]["restored"] is True
    assert receipt["restore_result"]["reason"] == ADOPTION_MEASURED_REGRESSION


# --------------------------------------------------------------------------- #
# 6b. #2392 — WHICH identity the receipt's proposal fingerprint is
# --------------------------------------------------------------------------- #
#
# The migration story, driven through the real two-stage host rather than
# asserted on a hand-built contract, because the fact under test is a durable
# WRITE: a proposal fingerprint that never crosses ``verify_priors`` reaches
# the receipt as an empty string, which the contract refuses, and the round
# loses its receipt to the fail-soft handler. That is the exact shape of the
# defect the ERROR log line above was earned by.
#
# ``_seed_applied_stage_1_state`` writes NO ``verify_priors.proposal_
# fingerprint``, which makes it a genuine pre-#2392 durable state rather than a
# simulated one — so the "old regime" side of every comparison below is the
# real thing.

#: Stands in for an ``InterventionProposal.fingerprint``. Deliberately the same
#: SHAPE as the candidate fingerprint it must be told apart from (both are what
#: ``json_fingerprint`` returns), because a discrimination that only works when
#: the two look different is not a discrimination.
_PROPOSAL_FP = "e" * 64


def _seed_round_state_proposing(fingerprint: str) -> dict[str, Any]:
    """Stage-1 durable state that DID cross a proposal fingerprint (#2392)."""
    state = _seed_round_state()
    state["verify_priors"]["proposal_fingerprint"] = fingerprint
    v2host.save_v2_state(state)
    return state


def test_the_receipt_names_the_proposal_that_was_made(monkeypatch, real_bundle):
    """#2392's acceptance criterion, on the durable artifact.

    The receipt's ``proposal_fingerprint`` is the fingerprint of the
    ``InterventionProposal`` stage 1 committed — NOT ``fp-stage-1``, the
    candidate that happened to be applied, which is what every receipt written
    before #2392 carried in this field.

    The candidate identity is asserted too, and on the same receipt: taking the
    field over for the proposal must not cost the record a fact it used to
    carry, or "identifies the proposal" would have been bought by losing
    "identifies the candidate".
    """
    _seed_round_state_proposing(_PROPOSAL_FP)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    assert conductor.measure_proposal_fingerprint == _PROPOSAL_FP, (
        "the fingerprint has to survive the durable hop before it can reach a receipt"
    )
    assert _consume_verify(conductor, _post_apply_analysis(conductor)).accepted is True

    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    assert receipt["proposal_fingerprint"] == _PROPOSAL_FP
    assert receipt["proposal_fingerprint_kind"] == "intervention_proposal"
    assert receipt["evidence_identities"]["candidate_fingerprint"] == "fp-stage-1"
    # Still a real receipt, re-derivable from its own bytes.
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)


def test_a_pre_2392_stage_1_still_gets_a_receipt_and_it_says_so(
    monkeypatch, real_bundle,
):
    """The other half of the migration story: the old regime, still honest.

    A stage-2 re-arm whose stage 1 ran before #2392 has no proposal fingerprint
    to carry — and a household mid-commission when the deploy lands is exactly
    that. It must still get a receipt (losing one to a missing field would be a
    regression against the very handler #2291 Phase 3c made ERROR), and that
    receipt must not claim a proposal identity it never had.
    """
    _seed_round_state()  # NO verify_priors.proposal_fingerprint — the old shape
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    assert conductor.measure_proposal_fingerprint == ""
    assert _consume_verify(conductor, _post_apply_analysis(conductor)).accepted is True

    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    assert receipt["proposal_fingerprint"] == "fp-stage-1"
    assert receipt["proposal_fingerprint_kind"] == "candidate"
    assert receipt["evidence_identities"]["candidate_fingerprint"] == "fp-stage-1"
    assert conductor.round_receipt_identity is not None, "a receipt was still written"


def test_two_receipts_from_the_two_regimes_are_told_apart_by_the_receipt_itself(
    monkeypatch, real_bundle, tmp_path,
):
    """The discrimination, stated as the property a later reader depends on.

    Both fingerprints are 64-hex SHA-256 — ``json_fingerprint``'s output — so
    NOTHING about the value distinguishes them, and "the formats are disjoint"
    was never available as a migration story. Given that premise (proved on real
    objects elsewhere; see the comment below), this asserts the receipts are
    separable anyway, in all three states, which is only true because the
    receipt carries the marker.
    """
    from jasper.active_speaker.crossover_v2.contracts import (
        PROPOSAL_FINGERPRINT_KINDS,
    )

    _seed_round_state_proposing(_PROPOSAL_FP)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)
    _consume_verify(conductor, _post_apply_analysis(conductor))
    new_regime = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)

    # The pre-#2392 record, as a banked artifact would present it: the key is
    # ABSENT, not empty. That is the third state, and it is the one a reader
    # meets on every receipt already on disk.
    old_regime = {
        key: value for key, value in new_regime.items()
        if key != "proposal_fingerprint_kind"
    }
    old_regime["proposal_fingerprint"] = "fp-stage-1"

    # The two regimes' VALUES carry no discriminator. That premise is proved on
    # real objects — a real candidate fingerprint against a real proposal
    # fingerprint, both 64-hex from ``json_fingerprint`` — by
    # ``test_a_proposal_fingerprint_and_a_candidate_fingerprint_are_the_same_shape``
    # in tests/test_crossover_v2_proposal.py. It is not re-derived here,
    # because the candidate identity this harness seeds is the placeholder
    # ``fp-stage-1`` rather than a real digest: asserting a shape against it
    # would be asserting a property of the fixture.
    assert len(new_regime["proposal_fingerprint"]) == 64

    # What IS this test's own claim: separable anyway, by the receipt's own
    # word, in all three states.
    assert "proposal_fingerprint_kind" not in old_regime, "pre-#2392: absent"
    assert new_regime["proposal_fingerprint_kind"] in PROPOSAL_FINGERPRINT_KINDS
    assert new_regime["proposal_fingerprint_kind"] == "intervention_proposal"


def test_the_receipt_is_written_exactly_once_with_the_payload_that_was_graded(
    monkeypatch, real_bundle,
):
    """The write property: one call, and the bytes are the receipt's own.

    A write-once artifact path punishes a second write, and a payload that
    disagreed with the contract object would make the fingerprint in the
    identity block a claim about something else. Both are asserted at the seam
    the host actually binds, so a future caller that grades twice or hands the
    seam a re-serialized copy fails here.
    """
    written: list[dict[str, Any]] = []
    real_publish = None

    _seed_round_state_proposing(_PROPOSAL_FP)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    real_publish = _flow_seams(conductor).publish_round_receipt

    def _recording_publish(receipt: dict[str, Any]) -> str:
        written.append(dict(receipt))
        return real_publish(receipt)

    conductor._seams = dataclasses.replace(
        _flow_seams(conductor), publish_round_receipt=_recording_publish,
    )

    _consume_verify(conductor, _post_apply_analysis(conductor))
    # Same trigger again — the fire-once guard is what keeps this at one write.
    _consume_verify(conductor, _post_apply_analysis(conductor), attempt=2)

    assert len(written) == 1, "a write-once receipt path may be written once"
    payload = written[0]
    assert payload["proposal_fingerprint"] == _PROPOSAL_FP
    assert payload["proposal_fingerprint_kind"] == "intervention_proposal"
    assert payload["round_id"] == _MINTED_RELAY_SESSION_ID
    # The bytes handed to the seam ARE the receipt: same digest, and it is the
    # digest the session then reports.
    core = {key: value for key, value in payload.items() if key != "fingerprint"}
    assert payload["fingerprint"] == json_fingerprint(core)
    assert conductor.round_receipt_identity["receipt_fingerprint"] == (
        payload["fingerprint"]
    )
    assert _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID) == payload


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
    _install_applied_graph(monkeypatch, boosts=False)

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
    failures = [
        record for record in caplog.records
        if "event=correction.crossover_v2_round_receipt_failed" in record.getMessage()
    ]
    assert failures, "a lost receipt must be recorded, not silent"
    # …at ERROR. The LEVEL is the pin, not incidental: this is the event that
    # would have fired on every shipped round for a whole phase while the
    # receipt silently went unwritten, and a fail-soft path whose only trace is
    # a WARNING is one nobody reads.
    assert [record.levelname for record in failures] == ["ERROR"]


def test_a_grader_bug_never_turns_an_accepted_capture_into_a_refusal(
    monkeypatch, caplog,
):
    """The stated safety property of round grading, pinned.

    :func:`~jasper.active_speaker.crossover_v2.coordinator.run_round` promises
    that a bug in the grader logs and returns the caller's own verdict
    untouched — the round exists to ADD an honest answer, and it must never
    cost the household a verdict the measurement gate already reached. That
    promise had no coverage, which is exactly where a later refactor turns a
    fail-soft into a fail-closed and nobody notices until a good capture starts
    being refused.

    Patched on the COORDINATOR's binding, not on ``round_evidence``'s: #2291
    Phase 5 made that a module-scope ``from`` import, so rebinding the source
    module's attribute would leave the guarded call resolving to the real
    grader and pass this test over a fail-soft that had been deleted.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=2.0)
    _install_applied_graph(monkeypatch, boosts=False)

    def _explode(**kwargs: Any) -> Any:
        raise RuntimeError("grader is broken")

    monkeypatch.setattr(coordinator, "evaluate_round", _explode)

    with caplog.at_level("WARNING"):
        verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    # The capture's own verdict survives, unchanged.
    assert verdict.accepted is True
    assert verdict.code is None
    # And nothing was invented from a round that never graded.
    assert conductor.round_evaluation is None
    assert conductor.round_receipt_identity is None
    assert attempts == []
    assert "correction.crossover_v2_round_grade_failed" in caplog.text


def test_the_fire_once_guard_holds_against_a_second_grade_on_one_trigger(
    monkeypatch,
):
    """One session, one round — even if the same trigger fires twice.

    The two triggers are mutually exclusive by construction (``_consume_verify``
    grades only when this session plans no post-apply cloud; the cloud close
    grades only when it does), so the guard's real job is not arbitrating
    between them. It is re-entry on ONE trigger: a re-armed VERIFY, a retake,
    or any future caller reaching the same site twice. Without it the second
    pass would re-run adoption — a second restore attempt, and a second write
    to a write-once receipt path.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    first = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert first.code == REASON_CORRECTION_MEASURED_REGRESSION
    assert attempts == [1]
    graded = conductor.round_evaluation

    # Same trigger, again. The guard — not the seam's once-guard — is what has
    # to stop this: adoption must not be re-run at all.
    second = _consume_verify(conductor, _post_apply_analysis(conductor), attempt=2)

    assert second.accepted is True, "the second pass grades nothing, so it refuses nothing"
    assert conductor.round_evaluation is graded, "the round was not re-decided"
    assert attempts == [1], "no second restore was attempted"


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
    _install_applied_graph(monkeypatch, boosts=False)
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


# --------------------------------------------------------------------------- #
# 10. the #2331 review ledger — six claims the panel found unpinned
#
# Each of these was a sentence in a docstring, a level in a log call, or an
# exhaustiveness claim that nothing asserted. They are grouped here rather than
# scattered because they share one origin (the Phase 5a-i round-tail review) and
# one lesson: a stated guarantee with no test is where the next regression hides.
# --------------------------------------------------------------------------- #


def test_a_failed_restore_is_regraded_with_the_restore_failed_flag(monkeypatch):
    """C3. The flag is what makes the receipt's "neither graph" claim true.

    ``_regrade_after_failed_restore`` re-runs the SAME table with
    ``restore_failed=True``, and that argument is the entire mechanism: it is
    what turns the round into ``RECOVERY_REQUIRED`` and what makes the receipt
    say the speaker is on neither the entry graph nor a verified one. The
    OUTCOME was pinned; the argument was not, so a re-grade that dropped the
    flag would produce a second ordinary restore decision and keep passing.
    """
    seen: list[dict[str, Any]] = []
    real = coordinator.decide_adoption

    def _spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(coordinator, "decide_adoption", _spy)
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)  # flatter before ⇒ regression
    _install_applied_graph(monkeypatch, boosts=False)
    # A rollback seam that reports failure is what routes into the re-grade.
    conductor._seams = dataclasses.replace(
        _flow_seams(conductor), rollback=lambda _cause: False,
    )

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert [call["restore_failed"] for call in seen] == [True]
    assert (
        conductor.round_evaluation.adoption.outcome
        is AdoptionOutcome.RECOVERY_REQUIRED
    )
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED


def test_an_unbound_anchor_probe_fails_closed_QUIETLY(caplog):
    """C7. ``rollback_available`` claims BOTH halves fail closed; one was pinned.

    The parametrized table above varies the ``rollback`` seam and the anchor's
    ANSWER, but always binds an anchor probe. The docstring's other half — no
    probe bound at all — had no row.

    **The ANSWER alone does not pin it, and finding that out is why this test
    reads the journal.** Deleting the ``seam is None`` guard still returns
    ``False``: ``None()`` raises ``TypeError``, which the surrounding handler
    catches. A first version of this pin asserted only the ``False`` and
    survived exactly that mutation — pinned for the wrong property, which is the
    shape of defect the whole ledger exists to close.

    What distinguishes the two is the journal, and the difference is one a
    support read depends on: an unbound probe is a *configuration* fact (a
    single-stage or future caller that cannot restore at all), not a failure. It
    must not spend a WARNING with a traceback, or the log cries wolf in the one
    place an operator goes looking for a real anchor-read failure.
    """
    ports = coordinator.RoundPorts(
        rollback=lambda _cause: True, rollback_available=None,
    )

    with caplog.at_level("DEBUG"):
        answer = coordinator.rollback_available(ports, session_id="cap_x")

    assert answer is False
    assert not [
        r for r in caplog.records
        if "crossover_v2_rollback_available_failed" in r.getMessage()
    ], "an unbound probe is a configuration fact, not a failure to report"


def test_an_anchor_probe_that_raises_fails_closed_LOUDLY(caplog):
    """The control for the pin above: a real failure DOES reach the journal.

    Without it, "quietly" could be satisfied by a reader that never logs at all,
    and the WARN that tells an operator their durable state is unreadable would
    be free to disappear.
    """

    def _explode() -> bool:
        raise RuntimeError("the durable state is unreadable")

    ports = coordinator.RoundPorts(
        rollback=lambda _cause: True, rollback_available=_explode,
    )

    with caplog.at_level("DEBUG"):
        answer = coordinator.rollback_available(ports, session_id="cap_x")

    assert answer is False
    assert [
        r.levelname for r in caplog.records
        if "crossover_v2_rollback_available_failed" in r.getMessage()
    ] == ["WARNING"]


def test_the_no_anchor_escalation_is_logged_at_error(monkeypatch, caplog):
    """C8. Loudness is the remedy here, so the LEVEL is the pin.

    ``recovery_required`` with no anchor means the speaker is on an unverified
    graph and the automatic remedy does not exist. Nobody is coming unless the
    journal shouts; a demotion to WARNING would be invisible in exactly the
    situation that needs an operator.
    """
    _seed_round_state(anchor=False)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    with caplog.at_level("INFO"):
        _consume_verify(conductor, _post_apply_analysis(conductor))

    records = [
        r for r in caplog.records
        if "crossover_v2_round_recovery_required" in r.getMessage()
    ]
    assert records, "the no-anchor escalation must reach the journal"
    assert [r.levelname for r in records] == ["ERROR"]


def test_a_failed_restore_is_logged_at_error_and_a_successful_one_is_not(
    monkeypatch, caplog,
):
    """C9. Same reasoning as C8, and its control.

    A restore that did not complete leaves the speaker on the graph the round
    just judged bad, with its remedy spent — an operator-visible event. A
    restore that DID complete is ordinary business at INFO. Pinning only the
    failure level would pass for a blanket ERROR that cried wolf on every
    successful undo.
    """
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)
    conductor._seams = dataclasses.replace(
        _flow_seams(conductor), rollback=lambda _cause: False,
    )

    with caplog.at_level("INFO"):
        _consume_verify(conductor, _post_apply_analysis(conductor))

    failed = [
        r for r in caplog.records
        if "crossover_v2_round_restore " in r.getMessage() + " "
    ]
    assert [r.levelname for r in failed] == ["ERROR"]

    caplog.clear()
    _seed_round_state()
    ok_conductor, _ = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(ok_conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    with caplog.at_level("INFO"):
        _consume_verify(ok_conductor, _post_apply_analysis(ok_conductor))

    succeeded = [
        r for r in caplog.records
        if "crossover_v2_round_restore " in r.getMessage() + " "
    ]
    assert [r.levelname for r in succeeded] == ["INFO"]


def test_every_restore_the_table_can_ask_for_has_its_own_household_code():
    """C5. ``round_restore_reason``'s docstring claims the map is exhaustive.

    It ends in ``.get(cause, MEASURED_REGRESSION)`` and calls that "a floor, not
    a branch anything reaches" — a claim about the TABLE, which nothing checked.
    So walk every corner ``decide_adoption`` can be asked, collect the reasons
    it returns with a RESTORE intent, and require a deliberate entry for each:
    any reason other than ``measured_regression`` that lands on the
    measured-regression code is reaching the fallback, which would tell a
    household about a regression when the round found a realization failure.
    """
    reasons = set()
    for realization in RealizationStatus:
        for benefit in BenefitStatus:
            for spec in SpecStatus:
                for boosted in (True, False):
                    decision = coordinator.decide_adoption(
                        realization=realization, benefit=benefit, spec=spec,
                        boosted=boosted, rollback_available=True,
                    )
                    if decision.outcome is AdoptionOutcome.RESTORE:
                        reasons.add(decision.reason)

    assert reasons, "the table must be able to ask for a restore at all"
    for reason in sorted(reasons):
        code = flow.round_restore_reason(reason)
        if reason != ADOPTION_MEASURED_REGRESSION:
            assert code != REASON_CORRECTION_MEASURED_REGRESSION, (
                f"{reason!r} reaches the fallback rather than its own code"
            )


def test_every_refusal_kind_the_coordinator_can_return_is_mapped(caplog):
    """C6. The catch-all must not answer for a kind nobody wired.

    ``REFUSAL_KINDS`` is the coordinator's own enumeration; the flow maps each
    to a household code. A kind added there and forgotten here used to fall
    through an ``else`` and wear ``correction_rollback_failed``'s sentence —
    telling a household their correction is still applied on evidence that says
    nothing of the kind.

    **The journal is the assertion, not the verdict, and admitting why is the
    point.** This pin's first version checked only that every kind produced
    *some* refusal with *some* code — which an unmapped kind does too, because
    the conservative fallback is still a refusal. It was the wrong-property
    class that :func:`test_an_unbound_anchor_probe_fails_closed_QUIETLY` twenty
    lines below documents, written twenty lines above it, in the same sitting.

    So the completeness claim is asserted where completeness is actually
    observable: **no kind may raise the unmapped event.** That guard cannot be
    vacuous, because its sibling
    (:func:`test_an_unrecognised_refusal_kind_is_loud_rather_than_silent`)
    proves the event fires for a kind with no arm.
    """
    conductor = _bare_conductor()

    with caplog.at_level("INFO"):
        for kind in sorted(coordinator.REFUSAL_KINDS):
            refusal = coordinator.RoundRefusal(
                kind=kind, cause=ADOPTION_MEASURED_REGRESSION,
                rollback_anchor_available=True,
            )
            verdict = conductor._round_refusal_for(refusal)
            assert verdict.accepted is False
            assert verdict.code

    unmapped = [
        r.getMessage() for r in caplog.records
        if "crossover_v2_round_refusal_kind_unmapped" in r.getMessage()
    ]
    assert unmapped == [], (
        "a declared refusal kind reached the fallback instead of its own arm: "
        f"{unmapped}"
    )


def test_an_unrecognised_refusal_kind_is_loud_rather_than_silent(caplog):
    """The other half of C6: the fallback exists, and it shouts.

    Reached with a kind no released coordinator returns, which is exactly the
    shape of the future defect — the point is that it cannot arrive quietly.
    """
    conductor = _bare_conductor()
    refusal = coordinator.RoundRefusal(kind="a_kind_from_the_future")

    with caplog.at_level("INFO"):
        verdict = conductor._round_refusal_for(refusal)

    unmapped = [
        r for r in caplog.records
        if "crossover_v2_round_refusal_kind_unmapped" in r.getMessage()
    ]
    assert [r.levelname for r in unmapped] == ["ERROR"]
    # Still refuses, and under the most conservative code available.
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED


# --------------------------------------------------------------------------- #
# 10. the FULL tier's trigger — the post-apply cloud close
#
# Everything above drives ``_consume_verify``, which is the EXPRESS trigger.
# The Full tier grades somewhere else entirely — at the end of
# ``_close_cloud_group`` for ``PHASE_CLOUD_VERIFY``, once the spatial arm has
# landed — and until #2291 Phase 5a-iv that call site had no coverage at all.
#
# **Measured, not assumed** (2026-08-11, against a ``git archive`` of
# ``origin/main`` at 5dcd872a4): deleting the ``_grade_round_once`` call from
# that branch left the 15 crossover-reaching suites entirely GREEN — 874
# collected, 863 passed, 11 skipped, exit 0. The same deletion at the Express
# site fails 18 of them. Two suites reach this branch —
# ``test_crossover_v2_conductor`` and ``test_correction_crossover_v2_endpoints``
# — and both stop at the capture verdict, so a Full household's adoption
# decision, its automatic restore, and its receipt were all riding on a call
# nothing checked was still there.
#
# The bar for this section is therefore a property rather than a coverage
# claim: **each test below fails if the Full trigger stops grading.** They
# assert through the close, on the same three things sections 1-6 assert for
# Express — the adoption outcome, the restore that outcome commands, and the
# receipt it banks.
# --------------------------------------------------------------------------- #


def _full_stage_2(monkeypatch) -> tuple[Any, list[int]]:
    """A REAL Full-tier stage 2 — VERIFY, then the post-apply position group.

    :func:`_restoring_stage_2` prepares the RECOVERY shape (one entry at the
    mark), which is the Express-shaped session every test above wants and is
    exactly why none of them can reach the Full trigger. Two things differ here
    and both are the household's own declarations, read by production code: the
    durable state carries ``tier="full"`` (written by the measuring session, so
    the tier chooser governs both stages), and the caller asks for
    ``stage="post_apply"`` rather than the §5.2 recovery re-verify.

    Everything else is the same real preparer behind the same stubbed DSP leg,
    and the returned counter is the same "how many times did a restore actually
    run" the Express tests assert on.
    """
    attempts: list[int] = []

    async def _counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        return await _restored_ok(*args, **kwargs)

    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _counted,
    )
    prepared = v2host.prepare_v2_verify(
        {"stage": "post_apply"}, status=_status(), run_async=_bg_run_async,
        camilla_factory=lambda: SimpleNamespace(),
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    assert flow.PHASE_CLOUD_VERIFY in conductor.session_phases, (
        "this session must plan a post-apply cloud, or it is not the Full "
        "shape and grades at the other trigger"
    )
    return conductor, attempts


def _seed_full_round_state(*, anchor: bool = True) -> dict[str, Any]:
    """:func:`_seed_round_state`, plus the tier the measuring session declared."""
    state = _seed_round_state(anchor=anchor)
    state["tier"] = "full"
    v2host.save_v2_state(state)
    return state


def _cloud_verify_indexes(conductor: Any) -> tuple[int, ...]:
    """The post-apply group's indexes, read off the conductor's OWN plan.

    Derived rather than written down: the Full tier's position count is a
    shipped range, and a test that hardcoded five would silently stop covering
    the close the day that range moved.
    """
    plan = conductor._journey.plan
    return tuple(
        i for i in range(1, 32)
        if plan.phase_for_index(i) == flow.PHASE_CLOUD_VERIFY
    )


def _walk_post_apply_cloud(conductor: Any, *, scale: float = 1.0) -> Any:
    """Every prompted post-apply position, in order, through the real consume.

    Reached at ``_consume_cloud_position`` for the reason :func:`_consume_verify`
    is reached directly: the runner in between is a thread and a websocket. The
    close, the delta probe, and the round grading all hang off this call, so
    nothing between here and them is stubbed.
    """
    verdict = None
    for attempt, index in enumerate(_cloud_verify_indexes(conductor), start=2):
        verdict = conductor._consume_cloud_position(
            flow.PHASE_CLOUD_VERIFY, index, attempt,
            _post_apply_analysis(conductor, scale=scale),
            SimpleNamespace(wav=b"fake-wav"),
        )
    assert verdict is not None, "a Full session has at least one cloud position"
    return verdict


def test_the_full_tier_grades_its_round_at_the_post_apply_cloud_close(
    monkeypatch, real_bundle,
):
    """The Full trigger's headline, and the pin its call site never had.

    Three assertions, in the order the evidence arrives, and the first is what
    makes the other two mean something: **VERIFY alone does not grade a Full
    round**. Its post-apply evidence is not complete — the spatial arm has not
    landed — so a session that graded there would decide adoption on a subset
    of what it is about to hold. That assertion is also what makes this test
    unsatisfiable by the Express trigger: if grading moved back to
    ``_consume_verify`` this fails before it ever reaches the close.

    Then the close runs and all three of the round's outputs appear at once:
    the adoption verdict, its reason, and the receipt on disk. Deleting the
    ``_grade_round_once`` call from the ``PHASE_CLOUD_VERIFY`` branch leaves
    every one of them absent.
    """
    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
    # 1.5x the deviation before, 1.0x after: measurably flatter, by more than
    # #2291's claim margin — the keep row, so a restore here would be a defect.
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    verify_verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verify_verdict.accepted is True
    assert conductor.round_evaluation is None, (
        "a Full session must NOT grade at VERIFY — the spatial arm has not "
        "landed, so the round's evidence is not complete"
    )

    verdict = _walk_post_apply_cloud(conductor)

    assert verdict.accepted is True
    assert verdict.payload["group_complete"] == flow.PHASE_CLOUD_VERIFY
    evaluation = conductor.round_evaluation
    assert evaluation is not None, "the cloud close did not grade the round"
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP
    assert evaluation.adoption.reason == ADOPTION_REALIZED_AND_IMPROVED
    # Nothing was put back, because nothing needed to be.
    assert attempts == []
    # …and the round banked a receipt, at the path a later reader can build
    # from the round id alone.
    identity = conductor.round_receipt_identity
    assert identity is not None
    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)
    assert receipt["round_id"] == _MINTED_RELAY_SESSION_ID
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.KEEP.value
    assert receipt["adoption"]["reason"] == ADOPTION_REALIZED_AND_IMPROVED
    # Fingerprinted by the contract, not merely stamped — the same re-hash the
    # Express receipt pin makes, so a Full round's receipt is held to it too.
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]


def test_the_full_tier_restores_a_measured_regression_at_the_cloud_close(
    monkeypatch, real_bundle,
):
    """The restore arm, reached through the close rather than through VERIFY.

    A Full household whose correction made the speaker measurably worse must
    get their previous sound back automatically, and the only code path that
    can do that for them runs after the last cloud position. Every restore pin
    above proves the Express path does it; none proves this one does, and the
    two reach ``coordinator.run_round`` from different call sites.

    ``scale=0.4`` is a before-side FLATTER than the after: the deviation grew,
    which is a measured regression. Asserted on the counter the stubbed DSP leg
    increments, so this cannot pass on a decision nobody acted on.
    """
    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    assert _consume_verify(conductor, _post_apply_analysis(conductor)).accepted

    verdict = _walk_post_apply_cloud(conductor)

    evaluation = conductor.round_evaluation
    assert evaluation is not None, "the cloud close did not grade the round"
    assert evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    assert evaluation.adoption.reason == ADOPTION_MEASURED_REGRESSION
    # The graph really went back — once.
    assert attempts == [1]
    # And the household is told in the ROUND's words rather than the capture's:
    # the group's own screens accepted this position, and the refusal that
    # reaches the screen is the round's.
    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_MEASURED_REGRESSION
    receipt = _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value


def test_exactly_one_of_the_two_round_triggers_fires_in_any_session(
    monkeypatch, real_bundle,
):
    """The mutual exclusion, measured on both shapes instead of stated in prose.

    ``_grade_round_once``'s docstring has always said the two triggers are
    "mutually exclusive by construction", and until now that sentence was the
    whole guarantee: the fire-once re-entry pin exercises ONE trigger, and no
    test had ever driven the other. A property asserted on one of its two cases
    is a property nobody has checked.

    Both branch on the same fact — ``PHASE_CLOUD_VERIFY in plan.phases`` — so
    the honest way to pin it is to run both shapes and assert the complement:
    the Express session grades at VERIFY **and plans no cloud close to reach**;
    the Full session does **not** grade at VERIFY and grades at the close. A
    regression that made both fire breaks the Full case's middle assertion; one
    that made neither fire breaks its last.
    """
    # Express: the recovery/one-entry shape, which is what every other test in
    # this module prepares.
    _seed_round_state()
    express, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(express, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    assert flow.PHASE_CLOUD_VERIFY not in express._journey.plan.phases
    assert _consume_verify(express, _post_apply_analysis(express)).accepted
    assert express.round_evaluation is not None, "Express grades at VERIFY"

    # Full: the tier's own post-apply walk.
    _seed_full_round_state()
    full, _full_attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(full, scale=1.5)

    assert flow.PHASE_CLOUD_VERIFY in full._journey.plan.phases
    assert _consume_verify(full, _post_apply_analysis(full)).accepted
    assert full.round_evaluation is None, "Full does not grade at VERIFY"

    _walk_post_apply_cloud(full)

    assert full.round_evaluation is not None, "Full grades at the cloud close"


def test_a_delta_probe_refusal_at_the_cloud_close_burns_no_round(
    monkeypatch, real_bundle,
):
    """The ordering inside the close: refuse FIRST, grade only after.

    ``_close_cloud_group`` runs the delta probe before it grades and returns on
    a refusal without grading. That ordering is load-bearing and was
    comment-only: the probe has already rolled back and named itself with the
    more specific code, a group can be RETAKEN, and the receipt is write-once —
    so grading here would burn the fire-once guard on evidence the household
    may yet replace, exactly as it would on a rejected VERIFY.

    The two shipped tests that drive a refusal through this branch
    (``test_a_spent_final_slot_terminalizes_its_close_time_refusal`` and its
    endpoint twin) assert only the terminal code, so swapping the two
    statements left them green. This asserts the consequence the ordering
    exists for.
    """
    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)
    assert _consume_verify(conductor, _post_apply_analysis(conductor)).accepted

    monkeypatch.setattr(
        flow.CrossoverV2Session, "_delta_probe_refusal",
        lambda self, _probe: flow.REASON_CORRECTION_MODEL_ERROR,
    )

    verdict = _walk_post_apply_cloud(conductor)

    assert verdict.accepted is False
    assert verdict.code == flow.REASON_CORRECTION_MODEL_ERROR, (
        "the probe's own, more specific code must reach the household"
    )
    # Nothing was graded, nothing was acted on, and nothing was banked.
    assert conductor.round_evaluation is None
    assert conductor.round_receipt_identity is None
    assert attempts == []
    with pytest.raises(FileNotFoundError):
        _round_receipt_json(real_bundle, _MINTED_RELAY_SESSION_ID)
