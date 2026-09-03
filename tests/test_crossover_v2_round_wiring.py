# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 3c: the round, wired into the host that actually runs it.

The grading itself — the four verdicts and the adoption table — is
:mod:`jasper.active_speaker.crossover_v2.round_evidence`'s and
:mod:`jasper.active_speaker.crossover_v2.verification`'s, and their own tests
pin it as arithmetic. **This module pins the WIRING**: that a real stage-2
conductor, built by the real verify-only prepare with the real host seams
behind it, reaches those answers and then does the right thing to the
household's speaker.

Every pin here is driven through the production host rather than by calling a
pure function, because every defect this phase can still ship is a wiring
defect. ``decide_adoption`` returning ``restore`` is worth nothing if the seam
it depends on was bound on the other stage.

What is pinned, in the order a round meets it:

1. **adoption outcomes**, reached through the two-stage host — keep, measured
   regression, and the fail-closed unproven boost;
2. **the round grades the capture the session ENDED on** — a rejected VERIFY
   keeps its own code, burns nothing, and writes no receipt, and the retry that
   lands clean is graded normally;
3. **the receipt** — where it lands, that it is fingerprinted, that it reads
   back identical;
4. **a receipt-write failure costs no verdict**;
5. **the model-error store banks the TRACKING number**, not the ledger's grade;
6. **durability, both directions** — the anchor writes fsync, an ordinary
   conductor persist does not;
7. **every round banks its receipt.**

**The rollback seam's own door-level pins live next door**, in
``tests/test_crossover_v2_pin_apply_rollback.py`` — this suite drives the
round OUTCOMES through it.

This module drives the REAL preparers through
:mod:`tests.crossover_v2_round_harness` over
``tests/test_crossover_v2_stage_bridge.py``'s seams.  That harness used to
leak fakes into any module that first imported them inside its patched window
(issue #2312), so this file carried a warning not to share a pytest process
with ``tests/test_correction_crossover_v2_endpoints.py``.  #2312 is fixed — the
harness now unwinds every binding by identity, see its own comment — and the
suites co-run green in either order.
"""

from __future__ import annotations

import itertools

import dataclasses
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ROLLBACK_VERDICTS,
    SEAM_DEFERRED_QUIETER_THAN_COMMANDED,
    VERDICT_MODEL_ERROR,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    DELTA_PROBE_REASON_BY_VERDICT,
    correction_rollback_failed_message,
)
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.contracts import (
    ADOPTION_ROW_KEEP_ITERATING,
    AdoptionDecision,
    AdoptionOutcome,
    IterationHeadroom,
)
from jasper.active_speaker.crossover_v2.contracts import (
    EvidenceTrust,
    QualityStatus,
    SafetyStatus,
)
from jasper.active_speaker.crossover_v2.verification import (
    ADOPTION_MEASURED_REGRESSION,
    ADOPTION_REALIZED_AND_IMPROVED,
    HEADROOM_CAP_REACHED,
    HEADROOM_NO_OBJECTIVES,
    HEADROOM_REACHABLE,
    ADOPTION_UNPROVEN,
    ADOPTION_UNPROVEN_BOOST,
    CAPTURE_INTEGRITY_FAILED,
    CAPTURE_INTEGRITY_UNAVAILABLE,
    REALIZATION_NO_COMPARATOR,
    REALIZATION_NO_TRACKING,
    SAFETY_BOOST_OVER_DECLARED_BOUND,
    SAFETY_CLIPPED_CAPTURE,
    SAFETY_NO_FINDING,
    SAFETY_UNCOMMANDED_LEVEL_LOUDER,
    TRUST_MEASURED,
    Verdict,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_CORRECTION_MEASURED_REGRESSION,
    REASON_CORRECTION_ROLLBACK_FAILED,
    REASON_CORRECTION_UNPROVEN_BOOST,
    REASON_REGISTRY,
    REASON_VERIFY_OUT_OF_TOLERANCE,
)
from jasper.active_speaker.crossover_v2_flow import (
    ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status

# The round harness: one staging of "a real stage 2, post-apply, with a
# comparable before" — a fixture library rather than a test file, so no
# suite depends on another's collection.
from tests.crossover_v2_round_harness import (
    _bg_run_async,
    _consume_verify,
    _household_sentence,
    _install_applied_graph,
    _install_entry_baseline,
    _post_apply_analysis,
    _restoring_stage_2,
    _seed_round_state,
    _stub_restore_doors,
    _tracking_curve_change_from_entry,
)

# The stage-bridge harness: one definition of "what a real preparer needs
# stubbed". The two autouse fixtures come with it by name — pytest activates
# an autouse fixture by its presence in this namespace, and nothing here calls
# one, so they are re-exported under the redundant-alias form. That is the
# idiom for "this module-level name is deliberate", and it says so without
# spending a lint suppression against the repo's frozen noqa budget.
from tests.test_crossover_v2_stage_bridge import (
    _MINTED_CAPTURE_SESSION_ID,
    _flow_seams,
    _isolated_v2_state as _isolated_v2_state,
    _open_prepared,
    _production_host_seams as _production_host_seams,
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



# Production refuses a session with no volume owner; stand one up.
pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

def _hydrated_series_position(conductor: Any) -> Any:
    """The :class:`SeriesPosition` this conductor was SEEDED with.

    Distinct from anything the session computes for itself: it is purely what
    the preparer resolved off durable state and handed over, and there is no
    public reader because a session may only carry it, never re-derive it.
    """
    return conductor._series_position












def _round_receipt_json(store: Any, capture_session_id: str) -> dict[str, Any]:
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
        / capture_session_id
        / "round_receipt.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #




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




# --------------------------------------------------------------------------- #
# 1. adoption outcomes, through the REAL two-stage host
# --------------------------------------------------------------------------- #


def test_a_measurably_improved_round_keeps_the_graph_and_the_verdict(monkeypatch):
    """The one keep row, reached by measuring rather than by asserting a table.

    A household whose correction worked must land on the ordinary verified
    screen — the round adds an answer, it does not add a refusal. This is the
    control every restore pin below needs: without it, a wiring bug that
    refused EVERY round would satisfy those tests and fail nobody.

    **#2537 update (corrected in commit c1ea01838).** The quality axis's
    STATUS is keyed on ``(realization, benefit)`` only — #2291's own table,
    unchanged in what it reads. Spec rides as disclosure (a next-round
    target) and never decides keep-vs-keep_for_iteration; an earlier cut of
    #2537 folded spec into the decision and was corrected back out (the
    permutation invariant — "spec is any in every row" — is load-bearing and
    is what this Express-tier fixture would otherwise have tripped, since it
    walks no post-apply cloud and so never has a spec report at all).

    **Bites update.** The graph is still kept and the household still lands on
    the ordinary verified screen — that is what this control pins. What moved
    is which KEEP row: an Express round grades no objectives, and under the
    ethos ("only the round budget, the plateau, and the safety class end a
    series") missing evidence may no longer end one, so the round keeps AND
    offers another bite instead of keeping terminally.
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
    # KEEP_FOR_ITERATION, not RESTORE: the graph stays on the speaker. The two
    # keeping outcomes leave the speaker in the same state — what differs is
    # whether another round is offered.
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.row == ADOPTION_ROW_KEEP_ITERATING
    # The reason still NAMES the ungradable objectives — the status changed,
    # the diagnosis did not.
    assert evaluation.adoption.reason == HEADROOM_NO_OBJECTIVES
    # The disclosure target list still names the unwalked spatial arm — spec
    # not deciding the status is not spec vanishing from the receipt.
    assert evaluation.quality.evidence["targets"] == ["spec:no_spec_report"]
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

    **#2537 update (corrected in commit c1ea01838).** The pre-#2537 fail-closed
    cell fired on an INDETERMINATE benefit alone, and that is exactly the
    2026-08-15 JTS3 cycle-4 defect #2537 exists to fix: a measured, safe,
    improving-but-unprovable candidate was reverted BECAUSE it carried a
    boost, even though its capture was perfectly usable and its realization
    tracked. #2537 moves the boost-fail-closed rule onto the evidence-TRUST
    axis, which is deliberately blind to benefit
    (:func:`~jasper.active_speaker.crossover_v2.verification.evaluate_evidence_trust`'s
    own docstring: "a benefit that came out indeterminate is deliberately NOT
    here"). So this cell now needs UNTRUSTED evidence — a capture that could
    not be graded at all, not merely one with no comparable "before" — and
    only THEN does a boosting intervention fail closed rather than ask. See
    ``test_the_same_unproven_round_without_a_boost_asks_instead_of_restoring``
    for the sibling scenario (trusted evidence, indeterminate benefit) that
    the pre-#2537 cell wrongly restored and #2537 now correctly keeps.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)
    # No integrity record at all — genuinely UNUSABLE evidence, not merely an
    # unprovable benefit. VERIFY's own capture gate still accepts it (see
    # tests/test_crossover_v2_verify_grading.py's
    # test_verify_without_an_integrity_record_is_not_refused_but_says_so);
    # only the round's evidence-trust axis refuses it.
    analysis = dataclasses.replace(
        _post_apply_analysis(conductor), capture_integrity=None,
    )

    verdict = _consume_verify(conductor, analysis)

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_UNPROVEN_BOOST
    evaluation = conductor.round_evaluation
    assert evaluation.trust.status is EvidenceTrust.UNTRUSTED
    assert evaluation.trust.reason == CAPTURE_INTEGRITY_UNAVAILABLE
    assert evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    assert evaluation.adoption.reason == ADOPTION_UNPROVEN_BOOST
    assert attempts == [1]


def test_the_same_unproven_round_without_a_boost_asks_instead_of_restoring(
    monkeypatch,
):
    """The sibling of the fail-closed boost pin: TRUSTED evidence, same doubt.

    The candidate only cuts (unboosted) and has no comparable "before", so the
    benefit is INDETERMINATE — but the capture itself is perfectly usable and
    the realization tracked. An unverified cut can wait for a household to
    decide; nothing is restored, and the capture's own verdict stands.

    **#2537 update (corrected in commit c1ea01838).** This is the exact JTS3
    2026-08-15 cycle-4 shape the owner ruled on, and the pre-#2537 table
    restored it — the fail-closed boost cell fired on INDETERMINATE benefit
    alone, which reverted a measured, safe round to an unmeasured state. Now
    it lands on ``KEEP_FOR_ITERATION``: trusted evidence with an outstanding
    quality target, not a question to ask and not a restore. Proven below on
    BOTH a boosted and an unboosted candidate — see
    ``test_an_unproven_boost_with_a_valid_anchor_comes_back_off`` for the
    genuinely UNTRUSTED-evidence sibling that still fails closed.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    assert conductor.measure_entry_baseline is None
    _install_applied_graph(monkeypatch, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    evaluation = conductor.round_evaluation
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.reason == ADOPTION_UNPROVEN
    assert evaluation.trust.status is EvidenceTrust.TRUSTED
    assert evaluation.quality.status is QualityStatus.MISSED
    assert attempts == []

    # …and the boost is provably invisible here: the SAME usable capture,
    # boosted instead of cut-only, lands on the identical outcome and reason.
    _seed_round_state()
    boosted_conductor, boosted_attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)

    boosted_verdict = _consume_verify(
        boosted_conductor, _post_apply_analysis(boosted_conductor),
    )

    assert boosted_verdict.accepted is True
    boosted_evaluation = boosted_conductor.round_evaluation
    assert boosted_evaluation.adoption.outcome is evaluation.adoption.outcome
    assert boosted_evaluation.adoption.reason == evaluation.adoption.reason
    assert boosted_attempts == []


def test_an_unproven_boost_with_no_anchor_escalates_instead_of_promising(
    monkeypatch,
):
    """A restore nobody can perform is not a restore.

    Same evidence as the fail-closed boost pin — genuinely UNTRUSTED evidence
    (no integrity record), not merely an unprovable benefit; see
    ``test_an_unproven_boost_with_a_valid_anchor_comes_back_off`` for why that
    distinction is #2537's whole correction — one changed fact: the speaker
    has no stashed profile to go back to, which is every first-ever apply.
    The table must escalate rather than issue a restore instruction Undo
    would then refuse.
    """
    _seed_round_state(previous_candidate=False)
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)
    analysis = dataclasses.replace(
        _post_apply_analysis(conductor), capture_integrity=None,
    )

    verdict = _consume_verify(conductor, analysis)

    assert verdict.accepted is False
    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    evaluation = conductor.round_evaluation
    assert evaluation.trust.status is EvidenceTrust.UNTRUSTED
    outcome = evaluation.adoption.outcome
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
    always right and only the sentence lied. Same genuinely-UNTRUSTED evidence
    shape as the pin above (#2537) — see its docstring.
    """
    _seed_round_state(previous_candidate=False)
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_applied_graph(monkeypatch, boosts=True)
    analysis = dataclasses.replace(
        _post_apply_analysis(conductor), capture_integrity=None,
    )

    verdict = _consume_verify(conductor, analysis)
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
    # #2859: and it asserts no CAUSE. This arm is reached by four named
    # refusals, only some of which are "this speaker was never corrected" —
    # see test_the_no_anchor_arm_states_no_cause below.
    assert "first measured crossover" not in sentence


def test_the_no_anchor_arm_states_no_cause():
    """#2859 defect 1, pinned at the sentence rather than at a screen.

    ``rollback_anchor_available=False`` reports one capability — no prior
    candidate fingerprint is recorded — which covers a first-ever apply AND
    any prior profile that was not a measured-candidate apply. The sentence
    used to end "this was its first measured crossover", a claim about only
    one of those; on jts3, 2026-08-22, it told a corrected speaker's
    household that their speaker had never been corrected.
    """
    sentence = correction_rollback_failed_message(False)
    assert "first" not in sentence.lower()
    assert "never" not in sentence.lower()
    # The remedies are the half that IS true of every arm.
    assert "measure again" in sentence.lower()
    assert "Sound page" in sentence
    # …and the other arm is untouched, so this is a narrowing and not a
    # deletion of the branch.
    assert "previous tuning" in correction_rollback_failed_message(True)
    assert correction_rollback_failed_message(None) == (
        correction_rollback_failed_message(True)
    )


def test_the_attempted_and_failed_arm_keeps_its_way_back_pointer(monkeypatch):
    """The other arm of the same code, and the reason it stays a branch.

    Here a stored previous sound DOES exist and the automatic restore failed
    against it, so going back to the previous tuning is a real remedy and the
    copy should still offer it. Pinned beside its sibling because a fix that
    removed the pointer everywhere would satisfy that test and strand this
    household with no action at all.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)
    # A prior candidate is recorded, but the apply door does not put it back.
    monkeypatch.setattr(
        v2host, "handle_v2_apply",
        lambda *a, **k: {"status": "blocked"},
    )

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.code == REASON_CORRECTION_ROLLBACK_FAILED
    assert attempts == []  # nothing went live: the counted success door never ran
    sentence = _household_sentence(conductor, verdict.code)
    assert "previous tuning" in sentence
    assert "STILL APPLIED" in sentence


# --------------------------------------------------------------------------- #
# 1b. exactly one restore, and the seams it reaches
# --------------------------------------------------------------------------- #


def test_two_restore_triggers_run_one_undo_and_keep_the_honest_sentence(
    monkeypatch,
):
    """ONE owner, one restore — and the source proves there is no second.

    Both the round's adoption path and the delta probe's own seam used to ask
    this host to put the previous sound back, and the restore is not
    idempotent: a completed one re-stamps the DISPLACED candidate as the new
    previous one, so a second ask would republish-and-apply the very graph the
    first one just took off. The historical second asker also read the
    repeat's refusal as a FAILED rollback and re-labelled its verdict
    ``correction_rollback_failed`` — whose household copy says the correction
    is still applied. It was not. That false sentence about their own speaker
    was the defect, and a once-guarded closure was the mitigation.

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
    # ONE restore, not two.
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

    A guard that returned ``False`` on every repeat would satisfy "one
    restore" and still produce the false sentence. This pins the remembering
    half directly on the seam both owners share.
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
    so the household keeps getting the "still applied" sentence. A guard that
    cached only successes would let a later asker retry into a different
    answer about the same speaker — and, under the normal-path mechanism,
    retry a republish aimed at a record that already said no.

    The return values alone cannot see this — a re-attempted restore on a
    speaker with no recorded prior candidate refuses identically every time,
    so ``False`` twice is what a MISSING guard produces too (a mutation
    removing the memo left an earlier version of this test green). What
    separates them is whether the restore was attempted a second time, and the
    seam already says so in the journal: one ``restore_refused`` for the real
    attempt, then ``restore_repeat`` for every asker handed the remembered
    answer.
    """
    _seed_round_state(previous_candidate=False)  # nothing recorded to go back to
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
        _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
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
    # A KEEPING outcome — the point of this test is that the round describes
    # the accepted retry rather than the rejected first attempt, whose
    # ``recovery_required`` would have been unmistakable here.
    assert (
        conductor.round_evaluation.adoption.outcome
        is AdoptionOutcome.KEEP_FOR_ITERATION
    )
    # An Express tier walks no post-apply cloud, so the fourth axis has no
    # objectives to grade. Since the bites ruling that is not an ending: the
    # reason names the missing evidence and the series stays open.
    assert conductor.round_evaluation.adoption.reason == HEADROOM_NO_OBJECTIVES
    assert conductor.round_receipt_identity is not None
    assert attempts == []



# --------------------------------------------------------------------------- #
# 3. the receipt
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

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

    assert receipt["round_id"] == _MINTED_CAPTURE_SESSION_ID
    assert receipt["adoption"]["outcome"] == (
        AdoptionOutcome.KEEP_FOR_ITERATION.value
    )
    # An Express tier walks no post-apply cloud, so the fourth axis has no
    # objectives to grade. Since the bites ruling that is not an ending: the
    # reason names the missing evidence and the series stays open.
    assert receipt["adoption"]["reason"] == HEADROOM_NO_OBJECTIVES
    assert receipt["entry_baseline"]["program_id"] == (
        conductor.measure_entry_baseline.program_id
    )
    # Fingerprinted by the contract, not merely stamped: re-hash the payload's
    # own core and it has to come back the same.
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    identity = conductor.round_receipt_identity
    assert identity["round_id"] == _MINTED_CAPTURE_SESSION_ID
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]
    assert identity["artifact_fingerprint"]


def test_a_quieter_only_shape_miss_reaches_the_table_instead_of_the_seam(
    monkeypatch, real_bundle, caplog,
):
    """#2559 piece 2, end to end: the deferral holds and the round grades.

    2026-08-15 14:47 (jts3): a ``model_error`` whose realized deviation pointed
    entirely quieter — a −3.32 dB dip at 1330 Hz, nothing realized louder than
    declared anywhere, tracking passed, 2.399 dB of measured improvement — was
    reverted by the probe's own seam. That seam PREEMPTED the adoption table
    (the round ended ``attempt_decision decision=ungraded``), so
    ``decide_adoption`` never saw the evidence at all.

    The seam is gone and the deferral is not: it now decides whether the
    QUALITY axis escalates, so the same class still keeps and the same
    measurement still stands. Three things have to hold together, and none
    implies another: no Undo runs, the capture is still accepted so the round
    is graded, and the deferral is on the record. A deferral nobody could see
    would be the same dishonesty in the other direction.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)
    caplog.set_level(logging.WARNING, logger=flow.logger.name)

    verdict = _consume_verify(
        conductor,
        dataclasses.replace(
            _post_apply_analysis(conductor),
            verify_tracking_curve=_tracking_curve_change_from_entry(
                conductor, change_db=-2.0,
            ),
        ),
    )

    assert conductor.delta_probe is not None
    # Still a rollback verdict by the probe's own reckoning — the deferral is a
    # decision about what to DO with it, not a demotion of the measurement.
    assert conductor.delta_probe.verdict == VERDICT_MODEL_ERROR
    assert conductor.delta_probe.rollback is True
    assert conductor.delta_probe.realized_louder_than_commanded is False

    assert attempts == [], "no Undo may run for a quieter-only shape miss"
    assert verdict.accepted is True

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    # The deferral reaches the record through the axis that now reads it: the
    # quality axis declined to escalate, and names the class it declined on.
    assert receipt["round_axes"]["quality"]["evidence"]["probe_rollback_class"] == ""
    safety = receipt["round_axes"]["safety"]
    assert safety["evidence"]["seam_deferred"] == (
        SEAM_DEFERRED_QUIETER_THAN_COMMANDED
    )
    assert safety["evidence"]["realized_louder_than_commanded"] is False
    # …and the table it was handed to actually ran, which is the whole point of
    # deferring rather than restoring.
    assert receipt["adoption"]["outcome"] in {
        AdoptionOutcome.KEEP.value, AdoptionOutcome.KEEP_FOR_ITERATION.value,
    }


def test_a_louder_shape_miss_still_restores_at_the_seam(monkeypatch, real_bundle):
    """The control, at the same place: the narrowing is one direction wide.

    Same harness, same magnitude, opposite sign. The seam restores, the session
    ends on its own verdict, and no round receipt is written — the shipped
    behaviour for every seam rollback, unchanged.
    """
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    verdict = _consume_verify(
        conductor,
        dataclasses.replace(
            _post_apply_analysis(conductor),
            verify_tracking_curve=_tracking_curve_change_from_entry(
                conductor, change_db=+2.0,
            ),
        ),
    )

    assert conductor.delta_probe is not None
    assert conductor.delta_probe.verdict == VERDICT_MODEL_ERROR
    assert conductor.delta_probe.safety_anchored is True
    assert conductor.delta_probe.realized_louder_than_commanded is True
    assert attempts == [1]
    assert verdict.accepted is False


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

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value
    assert receipt["restore_result"]["attempted"] is True
    assert receipt["restore_result"]["restored"] is True
    assert receipt["restore_result"]["reason"] == ADOPTION_MEASURED_REGRESSION


# --------------------------------------------------------------------------- #
# 3b. #2392 — WHICH identity the receipt's proposal fingerprint is
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

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

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

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

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
    new_regime = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

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
    assert payload["round_id"] == _MINTED_CAPTURE_SESSION_ID
    # The bytes handed to the seam ARE the receipt: same digest, and it is the
    # digest the session then reports.
    core = {key: value for key, value in payload.items() if key != "fingerprint"}
    assert payload["fingerprint"] == json_fingerprint(core)
    assert conductor.round_receipt_identity["receipt_fingerprint"] == (
        payload["fingerprint"]
    )
    assert _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID) == payload


# --------------------------------------------------------------------------- #
# 4. a receipt-write failure does not lose the verdict
# --------------------------------------------------------------------------- #


def test_a_failing_receipt_store_costs_the_round_nothing(monkeypatch, caplog):
    """Forensics must never outrank the thing it is forensics about.

    The verdict is what protects the household's speaker; the receipt is what
    lets someone reconstruct why afterwards. A full disk, a tamper-check
    mismatch, or a bundle that closed under us must not reverse a verdict,
    refuse a capture, or crash the capture path — it is a WARN and a journal
    line.

    **#2609 splits what the loss actually costs.** The ARTIFACT is lost and
    says so (both fingerprints empty). The series' MEMORY is not: the ordinal
    and objectives are read off the round's own evaluation, so a durably broken
    evidence store can no longer pin every round at 1 and quietly disable both
    the cap and the plateau stop.
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
    assert (
        conductor.round_evaluation.adoption.outcome
        is AdoptionOutcome.KEEP_FOR_ITERATION
    )
    assert attempts == []
    # Nothing CLAIMS a receipt: both fingerprints are empty, which is how a
    # reader tells "no artifact was banked" from "here is where it landed".
    identity = conductor.round_receipt_identity
    assert identity is not None
    assert identity["artifact_fingerprint"] == ""
    assert identity["receipt_fingerprint"] == ""
    # …and the series still knows where it is, which is the #2609 half. Fed
    # back exactly as the host persists it.
    assert identity["round_ordinal"] == 1
    assert "objectives" in identity
    # This round's cloud pipeline was made to fail, so there is no spec report
    # — and the key says so rather than fabricating an empty verdict.
    assert identity["spec"] is None
    assert coordinator.series_position_from_state(
        {"round_receipt": identity}
    ).ordinal == 2
    # The loss is recorded rather than silent.
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
# 5. the model-error store banks the TRACKING number
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

    def _record_with_a_different_grade(analysis, *, attempt_id, sitting_id):
        record = real_record_from_verify(
            analysis, attempt_id=attempt_id, sitting_id=sitting_id,
        )
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
# 6. durability, both directions
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


def test_the_apply_write_that_creates_the_way_back_is_fsynced(monkeypatch):
    """Power loss here leaves a corrected speaker with no recorded way back.

    ``observe_apply_success`` records ``previous_candidate_fingerprint`` — the
    only pointer the way back resolves its target from — in the same moment
    the new graph goes live. Atomic is not durable: without the fsync a power
    cut can lose the whole write while leaving the DSP graph changed.
    """
    _seed_round_state(previous_candidate=False)
    calls = _recorded_write_calls(monkeypatch)

    v2host.observe_apply_success(
        "fp-stage-1", previous_candidate_fingerprint="fp-previous",
    )

    assert calls == ["chmod", "fsync", "replace", "fsync"]
    assert (
        v2host.load_v2_state()["previous_candidate_fingerprint"] == "fp-previous"
    )


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

    The parametrized table below varies the ``rollback`` seam and the state
    seam's ANSWER, but always binds a state probe. The docstring's other half
    — no probe bound at all — had no row.

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


@pytest.mark.parametrize(
    ("seam_bound", "known", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
    ids=["seam+state", "seam-only", "state-only", "neither"],
)
def test_rollback_available_needs_both_the_seam_and_the_state(
    seam_bound, known, expected,
):
    """All four combinations, because each single-half rule is wrong differently.

    Seam-only says yes on a speaker whose pre-apply stash names no prior
    candidate: the round would issue a restore instruction the republish door
    then refuses, and the household would be told the old sound was coming
    back when nothing could bring it. State-only ignores that a caller may
    have no rollback binding at all. Pinning only the true corner, or only one
    false one, would leave an ``or`` in place of the ``and`` looking correct.

    Asked of the rule's OWNER (#2291 Phase 5 moved it to the coordinator), and
    of a port set rather than a conductor. That the production conductor hands
    the coordinator these two seams is a different claim, pinned end-to-end by
    :func:`test_a_round_reaches_every_one_of_its_five_seams` and by the restore
    outcomes above it.
    """
    ports = coordinator.RoundPorts(
        rollback=(lambda _cause: True) if seam_bound else None,
        rollback_available=lambda: known,
    )

    assert coordinator.rollback_available(ports, session_id="cap_x") is expected


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


def test_the_no_anchor_escalation_is_logged_at_error(monkeypatch, caplog):
    """C8. Loudness is the remedy here, so the LEVEL is the pin.

    ``recovery_required`` with no anchor means the speaker is on an unverified
    graph and the automatic remedy does not exist. Nobody is coming unless the
    journal shouts; a demotion to WARNING would be invisible in exactly the
    situation that needs an operator.
    """
    _seed_round_state(previous_candidate=False)
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
    household about a regression when the round found something else.

    **#2537 update (corrected in commit c1ea01838).** ``decide_adoption`` no
    longer takes ``realization=/benefit=/spec=`` — it takes three axis
    ``Verdict``s (``trust``/``safety``/``quality``), and two of those axes are
    new sources of a RESTORE reason that did not exist in the pre-#2537 table:
    the safety axis's three hazard reasons, and the trust axis's own capture/
    realization reasons on an UNBOOSTED untrusted round (see
    ``test_an_unproven_boost_with_a_valid_anchor_comes_back_off`` for why a
    boosted untrusted round instead reads ``ADOPTION_UNPROVEN_BOOST``). This
    walk is rebuilt over the CURRENT three axes, with each Verdict carrying a
    REAL reason a real evaluator would produce — the exhaustiveness claim is
    about ``round_restore_reason``'s actual reason vocabulary, so a walk using
    placeholder strings could not check it.
    """
    reasons = set()
    trust_reasons = (
        CAPTURE_INTEGRITY_UNAVAILABLE, CAPTURE_INTEGRITY_FAILED,
        REALIZATION_NO_TRACKING, REALIZATION_NO_COMPARATOR,
    )
    safety_reasons = (
        SAFETY_BOOST_OVER_DECLARED_BOUND, SAFETY_UNCOMMANDED_LEVEL_LOUDER,
        SAFETY_CLIPPED_CAPTURE,
    )
    for trust_status, trust_reason in (
        (EvidenceTrust.TRUSTED, TRUST_MEASURED),
        *((EvidenceTrust.UNTRUSTED, r) for r in trust_reasons),
    ):
        for safety_status, safety_reason in (
            (SafetyStatus.SAFE, SAFETY_NO_FINDING),
            *((SafetyStatus.UNSAFE, r) for r in safety_reasons),
        ):
            for quality_status, quality_reason in (
                (QualityStatus.PASSED, ADOPTION_REALIZED_AND_IMPROVED),
                (QualityStatus.MISSED, ADOPTION_UNPROVEN),
                (QualityStatus.REGRESSED, ADOPTION_MEASURED_REGRESSION),
            ):
                # #2602's axis is walked too: it cannot produce a restore
                # (both its answers keep), and walking it is what PROVES that
                # rather than assuming it — a headroom value that did reach a
                # restoring row would surface here as a reason with no
                # household code.
                for boosted, headroom_status in itertools.product(
                    (True, False), IterationHeadroom
                ):
                    decision = coordinator.decide_adoption(
                        trust=Verdict(trust_status, trust_reason, {}),
                        safety=Verdict(safety_status, safety_reason, {}),
                        quality=Verdict(quality_status, quality_reason, {}),
                        headroom=Verdict(headroom_status, "h", {}),
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

    Everything else is the same real preparer behind the same stubbed doors,
    and the returned counter is the same "how many times did a restore actually
    run" the Express tests assert on.
    """
    attempts = _stub_restore_doors(monkeypatch)
    prepared = v2host.prepare_v2_session(
        {"stage": "post_apply"}, status=_status(), run_async=_bg_run_async,
        camilla_factory=lambda: SimpleNamespace(), verify_only=True,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    assert flow.PHASE_CLOUD_VERIFY in conductor.session_phases, (
        "this session must plan a post-apply cloud, or it is not the Full "
        "shape and grades at the other trigger"
    )
    return conductor, attempts


def _seed_full_round_state(*, previous_candidate: bool = True) -> dict[str, Any]:
    """:func:`_seed_round_state`, plus the tier the measuring session declared."""
    state = _seed_round_state(previous_candidate=previous_candidate)
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

    Lands on the bare ``KEEP``, not ``KEEP_FOR_ITERATION`` — #2537's quality
    STATUS is keyed on ``(realization, benefit)`` alone (corrected in commit
    c1ea01838; spec rides as disclosure only, never as a decision input, per
    the restored "spec is any in every row" permutation invariant). This
    fixture's post-apply positions all play ``_in_room_summed_db()``
    (scaled), which its own docstring names as "an UNCORRECTED speaker in a
    room" and which genuinely fails one flat-spec band — so the receipt's
    target list still names it (see the assertion below), but it does not
    move the outcome off ``KEEP``.
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
    # #2602's HEADLINE CASE, end to end: the Full tier walks a post-apply
    # cloud, so the fourth axis can see this result still has tilt and ripple
    # left in it. The graph is KEPT exactly as before — ``KEEP_FOR_ITERATION``
    # leaves the speaker in the same state ``KEEP`` does — and the round now
    # says another one is worth running instead of declaring the series over.
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.row == ADOPTION_ROW_KEEP_ITERATING
    assert evaluation.adoption.reason == HEADROOM_REACHABLE
    assert evaluation.quality.status is QualityStatus.PASSED, (
        "the round still PASSED on quality — #2602 changed the stop "
        "condition, not the grade"
    )
    # Spec still names its own miss on the target list — it just does not
    # move the outcome off KEEP.
    assert any(
        target.startswith("spec:") for target in evaluation.quality.evidence["targets"]
    )
    # Nothing was put back, because nothing needed to be.
    assert attempts == []
    # …and the round banked a receipt, at the path a later reader can build
    # from the round id alone.
    identity = conductor.round_receipt_identity
    assert identity is not None
    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["round_id"] == _MINTED_CAPTURE_SESSION_ID
    # The receipt records the same answer the evaluation reached — #2602's
    # row 6, not row 1. A receipt that still said "keep" here would be the
    # banked artifact disagreeing with the screen.
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.KEEP_FOR_ITERATION.value
    assert receipt["adoption"]["row"] == ADOPTION_ROW_KEEP_ITERATING
    assert receipt["adoption"]["reason"] == HEADROOM_REACHABLE
    # Fingerprinted by the contract, not merely stamped — the same re-hash the
    # Express receipt pin makes, so a Full round's receipt is held to it too.
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]
    # …and the round's own flatness verdicts ride the same durable record the
    # objectives do, not only the tilt/ripple pair. A driver chaining rounds
    # decides from this block, and "tilt = 2.4 dB" with no band, no frequency
    # and no graded span is a number nobody can act on.
    spec = identity["spec"]
    assert isinstance(spec, dict)
    assert {"max_db", "max_hz", "graded_band_hz", "passed", "tilt"} <= set(spec)
    assert isinstance(spec["bands"], list) and spec["bands"]
    assert {"f_lo_hz", "f_hi_hz", "graded_lo_hz", "graded_hi_hz", "passed",
            "tolerance_db", "max_deviation_db", "max_deviation_hz"} == set(
        spec["bands"][0]
    )
    assert {"step_db", "high_band_hz", "low_band_hz"} <= set(spec["tilt"])
    # The same numbers the axis decided on, not a second reading of them.
    assert spec["max_db"] == evaluation.spec.evidence["max_db"]


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
    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
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


def test_a_probe_rollback_at_the_cloud_close_banks_its_round(
    monkeypatch, real_bundle,
):
    """A probe ROLLBACK at the cloud close banks a receipt — the founding ask.

    **This test's subject is the reverse of what it used to be, and the
    reversal is the ruling.** The close used to run the probe, restore from its
    own seam, and return a refusal BEFORE grading — so a rollback round wrote
    no receipt at all. The ethos names that as the bug it was written against:
    *the bug is that no round receipt was written on the failed verify, leaving
    that round's realization only in journal events.*

    Four things have to hold together. The graph still comes off (the class did
    not become a keep), exactly one Undo runs, the round is GRADED, and the
    receipt is on disk naming the probe class that took it off. The old shape
    satisfied only the first two.
    """

    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    # 2 dB LOUDER than commanded across the band: a ``model_error`` the #2559
    # deferral does not spare, which is the class this branch exists for. The
    # Full tier grades at the cloud close, so VERIFY only stashes it.
    #
    # Stated against the ENTRY capture, not against a flat model curve: the
    # direction that withholds the deferral is a measured change since
    # series-2 D1, and a decoupled fixture would let the entry baseline's own
    # shape decide it — which is the fixture testing itself.
    assert _consume_verify(
        conductor,
        dataclasses.replace(
            _post_apply_analysis(conductor),
            verify_tracking_curve=_tracking_curve_change_from_entry(
                conductor, change_db=-2.0, louder_spike_db=+4.0,
            ),
        ),
    ).accepted

    verdict = _walk_post_apply_cloud(conductor)

    assert verdict.accepted is False
    # The graph came off, once.
    assert attempts == [1]
    # …and the round was graded rather than skipped.
    assert conductor.round_evaluation is not None
    assert (
        conductor.round_evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    )
    quality = conductor.round_evaluation.quality
    assert quality.status is QualityStatus.REGRESSED
    assert quality.evidence["probe_rollback_class"] == VERDICT_MODEL_ERROR

    # The receipt exists, and records what the restore DID.
    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value
    assert receipt["restore_result"]["attempted"] is True
    assert receipt["restore_result"]["restored"] is True
    assert conductor.round_receipt_identity is not None
    assert conductor.round_receipt_identity["round_ordinal"] == 1


# --------------------------------------------------------------------------
# the series' own memory, across rounds (#2602)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "raw", "ordinal", "previous"),
    [
        ("no state at all", {}, 1, None),
        ("no receipt yet", {"session_id": "s"}, 1, None),
        ("receipt is not a mapping", {"round_receipt": "corrupt"}, 1, None),
        (
            "a receipt written before #2602 knew about ordinals",
            {"round_receipt": {"row": "row1_trusted_safe_passed"}},
            1, None,
        ),
        (
            "round 1 banked its objectives",
            {"round_receipt": {
                "round_ordinal": 1,
                "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
            }},
            2, (2.37, 0.9),
        ),
        (
            "an ordinal with no objectives beside it",
            {"round_receipt": {"round_ordinal": 2}},
            3, None,
        ),
        (
            "a bool is not an ordinal",
            {"round_receipt": {"round_ordinal": True}},
            1, None,
        ),
        (
            "a nonsense ordinal",
            {"round_receipt": {"round_ordinal": 0}},
            1, None,
        ),
    ],
    ids=[
        "no_state", "no_receipt", "corrupt_receipt", "pre_2602_receipt",
        "after_round_one", "ordinal_without_objectives", "bool_ordinal",
        "zero_ordinal",
    ],
)
def test_the_series_position_reader(case, raw, ordinal, previous):
    """Every shape durable state can be in, and what the next round inherits.

    Unreadable shapes resolve to the FIRST round, never to "the cap was
    reached": a bad byte on disk must not be able to silently switch iteration
    back off, which is the behaviour #2602 exists to add.
    """

    position = coordinator.series_position_from_state(raw)

    assert position.ordinal == ordinal, case
    if previous is None:
        assert position.previous_objectives is None, case
    else:
        assert position.previous_objectives is not None, case
        assert (
            position.previous_objectives.tilt_db,
            position.previous_objectives.ripple_db,
        ) == previous, case


def test_a_poisoned_objective_reads_as_absent_not_as_a_number():
    """A NaN would sail through every comparison in the headroom axis.

    ``NaN < plateau`` is ``False`` and ``NaN <= plateau`` is ``False``, so a
    corrupt write would make a series look permanently un-plateaued AND
    permanently un-flat — iterating to the cap every time on evidence that is
    not evidence.
    """

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1,
        "objectives": {"tilt_db": float("nan"), "ripple_db": float("inf")},
    }})

    assert position.previous_objectives is not None
    assert position.previous_objectives.tilt_db is None
    assert position.previous_objectives.ripple_db is None


@pytest.mark.parametrize(
    ("case", "receipt"),
    [
        ("objectives absent", {"round_ordinal": 9}),
        # The branch EVERY real round after the first takes — a banked receipt
        # always carries objectives beside its ordinal. The reader returns from
        # two different places depending on this key, and a clamp added to only
        # the second one is invisible to the case above. (Found by the #2602
        # gate: a clamp mutation on this branch survived all 637 tests.)
        (
            "objectives present",
            {
                "round_ordinal": 9,
                "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
            },
        ),
    ],
    ids=["objectives_absent", "objectives_present"],
)
def test_the_reader_never_clamps_the_cap_itself(case, receipt):
    """One enforcer. The headroom axis is where the cap lives.

    A reader that quietly clamped at 3 would be a second owner of the rule,
    and the two could disagree about what "the last round" means.

    Both return paths are exercised, because "the reader does not clamp" is a
    property of the FUNCTION and the function has two exits.
    """

    position = coordinator.series_position_from_state({"round_receipt": receipt})

    assert position.ordinal == 10, case


def test_a_graded_round_banks_what_the_next_one_needs_to_read(
    monkeypatch, real_bundle,
):
    """The round-trip: what ``_write_round_receipt`` writes, the reader reads.

    Writer and reader live in one module precisely so they cannot drift, and
    this is what proves they have not. Without it a series could bank the
    ordinal under one key and look for it under another — and every round
    would look like round 1, forever, with nothing red.
    """

    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))

    identity = conductor.round_receipt_identity
    assert identity is not None
    assert identity["round_ordinal"] == 1, "the first graded round is round 1"
    assert "objectives" in identity, "the next round has nothing to compare to"

    # Fed back exactly as the host persists it — ``state["round_receipt"]``.
    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.ordinal == 2


def test_a_graded_round_banks_WHICH_EPOCH_its_ordinal_counts_in(
    monkeypatch, real_bundle,
):
    """The ordinal alone is ambiguous the moment a reset door has run.

    A republish (and Start-Over's applied branch) replaces durable state
    wholesale and drops ``round_receipt``, so the next round is ordinal 1 again
    on a speaker that has already been tuned. The epoch is what tells that
    round apart from a fresh box's first one, and it is only useful if the
    RECEIPT carries it — the receipt is the series' only memory between
    sessions, so an epoch that lived anywhere else would not survive to be read.

    Driven end to end: the epoch is seeded into durable state, the REAL
    preparer resolves the series position off it, and the graded round's own
    banked identity is what is asserted.
    """

    state = _seed_round_state()
    state[coordinator.ROUND_ORDINAL_EPOCH_STATE_KEY] = 2
    v2host.save_v2_state(state)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))

    identity = conductor.round_receipt_identity
    assert identity is not None
    # Round ONE — of the third epoch. Both numbers, because either alone is the
    # ambiguity this exists to remove.
    assert identity["round_ordinal"] == 1
    assert identity["round_ordinal_epoch"] == 2


def test_the_status_block_forwards_the_receipt_to_the_screen():
    """The projection without which every round sentence is dead on a real box.

    ``persist_conductor_state`` has written ``state["round_receipt"]`` since
    #2537, and ``crossover_v2_status_block`` did not forward it — so the
    envelope read ``None`` in production and the round nudges existed only in
    unit tests that hand-built the status dict. #2602 makes that load-bearing:
    a series that cannot tell a household another round is coming has not
    delivered the ruling.
    """

    receipt = {
        "round_id": "s1",
        "adoption": "keep_for_iteration",
        "row": "row6_trusted_safe_passed_reachable",
        "reason": "flatter_result_reachable",
        "round_ordinal": 1,
        "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
    }
    state = _seed_round_state()
    state["round_receipt"] = receipt
    v2host.save_v2_state(state)

    block = v2status.crossover_v2_status_block()

    assert block is not None
    assert block["round_receipt"] == receipt, (
        "the screen cannot name a round the status block never forwards"
    )


# --------------------------------------------------------------------------- #
# 7. every round banks its receipt (the ethos's fifth principle)
#
# A direct ``run_round`` harness rather than the conductor fixtures above:
# these are properties of the COORDINATOR — which arms bank, what the identity
# carries, what survives a broken store — and driving them through a full
# stage-2 session would make the failure mode "somewhere in twelve hundred
# lines" instead of "this arm".
# --------------------------------------------------------------------------- #


_USABLE_ANALYSIS = SimpleNamespace(
    capture_integrity=SimpleNamespace(failed=(), not_evaluated=()),
    verify_tracking={"max_db_notch_excluded": 0.1, "n_bins": 10},
    summed_response=None,
    program_id="prog-1",
)

#: The same analysis, plus the VERIFY absolute claim that names a crossover
#: region. Decision 10's blend record rides only when there IS a region, so a
#: round graded from the analysis above banks none — which is what
#: ``test_a_round_with_no_cloud_banks_no_residuals_rather_than_empty_ones``
#: pins, and why the widest-receipt fixture needs this one instead.
#: ``[824.35, 3297.4]`` is the band every series-1 round actually graded.
_REGION_ANALYSIS = SimpleNamespace(
    capture_integrity=SimpleNamespace(failed=(), not_evaluated=()),
    verify_tracking={"max_db_notch_excluded": 0.1, "n_bins": 10},
    summed_response=None,
    program_id="prog-1",
    verify_absolute={"band_hz": [824.35, 3297.4], "worst_db": -2.9},
)


def _direct_round(
    *,
    analysis=_USABLE_ANALYSIS,
    publish=None,
    rollback=None,
    rollback_available=None,
    boosts=False,
    round_ordinal=1,
    previous_objectives=None,
    previous_trusted_floor_hz=None,
    trusted_floor_hz=None,
    delta_probe=None,
    position_residuals=(),
):
    ports = coordinator.RoundPorts(
        rollback=rollback,
        rollback_available=rollback_available,
        applied_boosts=(lambda: boosts),
        entry_graph_fingerprint=(lambda: "graph-1"),
        publish_round_receipt=publish,
    )
    evidence = coordinator.RoundEvidence(
        session_id="cap_direct",
        tier="express",
        post_analysis=analysis,
        entry_baseline=None,
        spec_report=None,
        proposal_fingerprint="a" * 64,
        commanded_delta_present=False,
        realization_tolerance_db=1.0,
        reference_mark="design_axis",
        proposal_fingerprint_kind="candidate",
        candidate_fingerprint="b" * 64,
        delta_probe=delta_probe,
        round_ordinal=round_ordinal,
        previous_objectives=previous_objectives,
        previous_trusted_floor_hz=previous_trusted_floor_hz,
        trusted_floor_hz=trusted_floor_hz,
        position_residuals=position_residuals,
    )
    return coordinator.run_round(evidence, ports)


@pytest.mark.parametrize(
    ("case", "kwargs", "outcome"),
    [
        (
            "kept, and another bite is coming",
            {},
            AdoptionOutcome.KEEP_FOR_ITERATION,
        ),
        (
            "restored, because the capture was unmeasurable",
            {
                "analysis": None,
                "rollback": lambda _reason: True,
                "rollback_available": lambda: True,
            },
            AdoptionOutcome.RESTORE,
        ),
        (
            "the restore itself failed",
            {
                "analysis": None,
                "rollback": lambda _reason: False,
                "rollback_available": lambda: True,
            },
            AdoptionOutcome.RECOVERY_REQUIRED,
        ),
        (
            "no anchor to restore to",
            {"analysis": None},
            AdoptionOutcome.RECOVERY_REQUIRED,
        ),
    ],
    ids=["keep_for_iteration", "restore", "restore_failed", "no_anchor"],
)
def test_every_arm_of_the_adoption_act_banks_a_receipt(case, kwargs, outcome):
    """The ethos's fifth principle, as a guard on every exit.

    *Every round, kept or restored or refused, banks its measurement into the
    series state so the next bite is commanded from it.* Parametrized over the
    three arms of ``_act_on_adoption`` rather than over the outcomes, because
    the arms are the code paths — an outcome added later routes through one of
    them and inherits this pin.

    What forced it into writing: the 2026-08-16 shortfall rollback wrote no
    round receipt on its failed verify, leaving that round's realization in
    journal events only.
    """
    banked = []
    decision = _direct_round(publish=lambda receipt: banked.append(receipt) or "art",
                             **kwargs)

    assert decision.evaluation.adoption.outcome is outcome, case
    assert len(banked) == 1, case
    assert decision.receipt_identity is not None, case
    assert decision.receipt_identity["artifact_fingerprint"] == "art", case
    # The banked artifact and the identity describe the same decision.
    assert banked[0]["adoption"]["outcome"] == outcome.value, case
    assert decision.receipt_identity["adoption"] == outcome.value, case


def test_a_round_with_no_publishing_seam_still_remembers_where_it_sat():
    """A host that cannot bank an artifact must not lose the series' count.

    This used to return ``None`` outright, so a caller with no publish seam
    produced no identity at all — and ``series_position_from_state`` reads the
    ordinal off exactly that identity. The empty fingerprints are how the
    record says no artifact was written.
    """
    decision = _direct_round(publish=None)

    identity = decision.receipt_identity
    assert identity is not None
    assert identity["artifact_fingerprint"] == ""
    assert identity["receipt_fingerprint"] == ""
    assert identity["round_ordinal"] == 1
    assert coordinator.series_position_from_state(
        {"round_receipt": identity}
    ).ordinal == 2


def test_the_cap_still_bites_when_every_receipt_write_fails():
    """#2609's actual acceptance: three rounds, a broken store, and a stop.

    The gate drove twelve rounds proving the old shape — a persistently
    failing receipt write pinned the ordinal at 1, which disabled the cap AND
    the plateau stop, and the series then ended only when the measurement
    itself said "flat enough". Walked here as a real series: each round's
    identity feeds the next round's reader, exactly as the host carries it.
    """
    def _explode(_receipt):
        raise OSError("no space left on device")

    ordinals = []
    reasons = []
    state = {}
    for _ in range(3):
        position = coordinator.series_position_from_state(state)
        decision = _direct_round(
            publish=_explode,
            round_ordinal=position.ordinal,
            previous_objectives=position.previous_objectives,
            previous_trusted_floor_hz=position.previous_trusted_floor_hz,
        )
        ordinals.append(position.ordinal)
        reasons.append(decision.evaluation.headroom.reason)
        state = {"round_receipt": decision.receipt_identity}

    assert ordinals == [1, 2, 3], "the series must count past a broken store"
    assert reasons[-1] == HEADROOM_CAP_REACHED
    assert (
        coordinator.series_position_from_state(state).ordinal == 4
    ), "and the cap keeps biting after it"


def test_the_receipt_banks_the_probes_band_resolved_realization_verbatim():
    """#2649's numbers are the next bite's command inputs, not a log line.

    Banked VERBATIM off the probe's own ``to_dict`` rather than reshaped here:
    the probe owns what a realization report says, and a second shaping in the
    receipt writer is a second owner of the same fact.
    """
    realization = {
        "pooled": 0.664,
        "graded_band_hz": [250.0, 16000.0],
        "trusted_floor_hz": 143.0,
        "trust_ceiling_hz": 16444.9,
        "bands": {
            "crossover": {
                "band_hz": [1000.0, 4000.0], "n_bins": 40,
                "ratio": 1.31, "graded": True,
            },
        },
    }
    probe = SimpleNamespace(
        verdict="matched", reason="",
        to_dict=lambda: {"verdict": "matched", "realization": realization},
    )
    banked = []

    _direct_round(publish=lambda r: banked.append(r) or "art", delta_probe=probe)

    assert banked[0]["round_measurements"]["realization"] == realization


@pytest.mark.parametrize(
    ("case", "probe"),
    [
        ("no probe ran at all", None),
        (
            "a probe from a build with no band-resolved report",
            SimpleNamespace(verdict="matched", to_dict=lambda: {"verdict": "matched"}),
        ),
        (
            "a probe whose to_dict raises",
            SimpleNamespace(
                verdict="matched",
                to_dict=lambda: (_ for _ in ()).throw(ValueError("boom")),
            ),
        ),
    ],
    ids=["absent", "older_build", "raising"],
)
def test_a_probe_that_cannot_report_costs_the_receipt_nothing_else(case, probe):
    """Three honest absences, none of them an error.

    The realization block is optional evidence. Losing the whole receipt over
    a probe that cannot answer would be exactly the trade this module refuses
    everywhere else — and the round's verdict, which is what protects the
    speaker, must be untouched by it.
    """
    banked = []

    decision = _direct_round(
        publish=lambda r: banked.append(r) or "art", delta_probe=probe,
    )

    assert len(banked) == 1, case
    assert "realization" not in banked[0]["round_measurements"], case
    assert decision.evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION


def test_the_receipt_banks_the_per_position_residual_role_labelled():
    """§4.2: "on-axis 0.4 dB, off-axis 2.9 dB" is an instruction.

    The role is the half that makes the number readable — a residual with no
    role says the cloud disagreed, and one with a role says which listening
    position it disagreed at, which is what separates a speaker defect from a
    room feature.
    """
    residuals = (
        {"position_id": "p0", "role": "onax", "rms_db": 0.42, "n_bins": 380},
        {"position_id": "p1", "role": "offax", "rms_db": 2.91, "n_bins": 380},
    )
    banked = []

    _direct_round(
        publish=lambda r: banked.append(r) or "art", position_residuals=residuals,
    )

    assert banked[0]["round_measurements"]["position_residuals"] == [
        dict(row) for row in residuals
    ]


def test_a_round_with_no_cloud_banks_no_residuals_rather_than_empty_ones():
    """An Express tier walks no positions, and a zero-length list of measured
    numbers is a claim that measuring happened and found nothing."""
    banked = []

    _direct_round(publish=lambda r: banked.append(r) or "art")

    assert banked[0]["round_measurements"] == {}


# --------------------------------------------------------------------------- #
# #2662 G5 — the opaque maps get an enumerated key set
# --------------------------------------------------------------------------- #

#: Every key the round's three ``Mapping[str, Any]`` receipt fields may carry,
#: enumerated at their writers: ``round_axes`` from ``RoundEvaluation.axes``,
#: and the other two from ``_write_round_receipt``/``_round_measurements`` in
#: :mod:`~jasper.active_speaker.crossover_v2.coordinator`.
RECEIPT_MAP_KEYS = {
    "round_axes": {"trust", "safety", "quality", "headroom"},
    "evidence_identities": {
        "session_id",
        "tier",
        "entry_baseline_artifact",
        "commanded_delta_present",
        "candidate_fingerprint",
    },
    # Both optional; the empty and single-key cases are pinned above. This is
    # the widest the map gets.
    # ``blend`` (decision 10) rides only when the round had a crossover region
    # to speak about — see ``_round_measurements``. It carries the region's
    # commanded-vs-realized pair AND the reason code for a round that
    # prescribed nothing, deliberately together rather than split across
    # ``round_axes``: that map is the four ADOPTION axes and every value in it
    # is a Verdict, which a blend reason is not — a fifth key of a different
    # shape there would read as a fifth axis, which decision 10 forbids.
    "round_measurements": {"realization", "position_residuals", "blend"},
}

_KEY_DRIFT_REMEDY = (
    "The receipt's opaque maps are enumerated because seven of RoundReceipt's "
    "fifteen fields are Mapping[str, Any], so a new inner key nests with no "
    "schema behind it. Adding one is fine — say so here, and bump "
    "contracts.SCHEMA_VERSION in the same diff."
)


def _key_drift(actual, expected):
    """``(added, missing)`` for one mapping against its enumerated key set."""

    return set(actual) - set(expected), set(expected) - set(actual)


def _widest_receipt():
    """One banked receipt with BOTH optional instruments reporting."""

    probe = SimpleNamespace(
        verdict="matched", reason="",
        to_dict=lambda: {"verdict": "matched", "realization": {"pooled": 0.664}},
    )
    banked = []
    _direct_round(
        publish=lambda r: banked.append(r) or "art",
        analysis=_REGION_ANALYSIS,
        delta_probe=probe,
        position_residuals=({"position_id": "p0", "role": "onax", "rms_db": 0.4},),
    )
    return banked[0]


def test_the_receipt_key_guard_sees_a_planted_key(monkeypatch):
    """The key-set guard's own positive control, planted through the REAL path.

    Set arithmetic on a hand-built dict would prove only that ``-`` works. This
    plants the key where one would actually arrive — in the writer — and reads
    it back off a banked receipt, so it also proves the drive below reaches
    that writer at all. A guard whose ``_direct_round`` stopped producing
    ``round_measurements`` would otherwise report an empty diff and read as
    compliance.
    """
    real = coordinator._round_measurements
    monkeypatch.setattr(
        coordinator,
        "_round_measurements",
        lambda evidence, evaluation: {
            **real(evidence, evaluation), "smuggled_in": 1,
        },
    )

    added, missing = _key_drift(
        _widest_receipt()["round_measurements"],
        RECEIPT_MAP_KEYS["round_measurements"],
    )

    assert added == {"smuggled_in"}
    assert missing == set()


def test_the_receipts_opaque_maps_carry_only_their_enumerated_keys():
    """#2662 G5: accretion into the three opaque maps becomes a real diff.

    ``SCHEMA_VERSION`` sat at ``1`` through three field additions in one week
    and is stamped into every ``to_dict`` payload, so a reader cannot tell two
    shapes apart by it. That is not fixed by bumping it once; it is fixed by
    something failing when the shape moves, which is this.
    """
    receipt = _widest_receipt()

    for field, expected in RECEIPT_MAP_KEYS.items():
        added, missing = _key_drift(receipt[field], expected)
        assert not added, f"{field} grew {sorted(added)}. {_KEY_DRIFT_REMEDY}"
        assert not missing, f"{field} lost {sorted(missing)}. {_KEY_DRIFT_REMEDY}"


def test_a_kept_round_banks_its_blend_instruction_and_it_reads_back():
    """Decision 10's series carry: prescription out, prescription back.

    Banked as a mapping rather than a bare list because the next round needs
    BOTH halves — the filters to build its candidate, and the region residual
    to know whether the region is still improving. Splitting them across two
    keys would let a series remember a prescription from one round and a
    reading from another.
    """

    decision = _direct_round(publish=lambda _r: "art",
                             analysis=_REGION_ANALYSIS)

    identity = decision.receipt_identity
    assert identity["adoption"].startswith("keep")
    assert isinstance(identity["blend"], Mapping)
    assert set(identity["blend"]) == {"filters", "residual_db"}

    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.previous_blend_correction is not None


#: The 2026-08-18 round's own banked instruction, and a DIFFERENT readable
#: correction for the speaker to be playing. The two blend tests below share
#: them so "the instruction" and "the applied graph" are provably the same two
#: answers in both, rather than two pairs that happen to differ.
_BANKED_INSTRUCTION = ({"biquad_type": "Peaking", "freq": 2120.3384, "q": 2.0,
                        "gain": -0.7171},)
_APPLIED_INCUMBENT = ({"biquad_type": "Peaking", "freq": 1200.0, "q": 2.0,
                       "gain": -2.5},)


def _state_carrying_a_banked_instruction() -> dict[str, Any]:
    """Durable state as a kept round 8 leaves it: ordinal, objectives, blend."""
    return {
        "round_receipt": {
            "round_ordinal": 8,
            "objectives": {"tilt_db": 0.4, "ripple_db": 0.9},
            "trusted_floor_hz": 143.0,
            "blend": {
                "filters": [dict(f) for f in _BANKED_INSTRUCTION],
                "residual_db": 0.7304,
            },
        },
    }


def test_a_banked_instruction_reaches_the_next_rounds_measure_stage(monkeypatch):
    """#2698 — the hop the test above stops one step short of.

    That test proves the receipt CARRIES the instruction and that the reader
    parses it back. Neither fact iterates anything: ``_blend_prescription`` is
    read at candidate-build time, which runs in the MEASURE stage, so a series
    converges only if the MEASURING session is hydrated with its position too.
    Before #2698 it was not: the one ``series_position=`` line lived in
    the verify-only prepare alone, ``self._series_position`` was ``None`` on
    every measuring session, and the build silently fell back to the incumbent.
    Measured on 2026-08-18: a round banked ``Peaking 2120.34 Hz, -0.7171 dB``
    and the next round emitted zero blend filters, with the done screen still
    promising the region would be trimmed.

    Driven through the REAL stage-1 preparer, and DISCRIMINATING: the applied
    profile carries a different, readable correction, so "carried the
    instruction" and "fell back to the graph" are two distinguishable answers
    rather than one. A fixture with no incumbent would pass against ``()`` for
    the wrong reason.
    """

    banked, incumbent = _BANKED_INSTRUCTION, _APPLIED_INCUMBENT
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile."
        "load_applied_baseline_profile_state",
        lambda: {"blend_correction": [dict(f) for f in incumbent]},
    )
    v2host.save_v2_state(_state_carrying_a_banked_instruction())

    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)

    # The series survived into the MEASURING session, ordinal and all — a
    # position resolved to ``first()`` would carry no instruction and the
    # assertion below would then be testing the fallback.
    assert _hydrated_series_position(conductor).ordinal == 9
    # …and what ``_build_candidate`` hands the emitter is that instruction,
    # not the graph the speaker is already playing.
    assert conductor._blend_prescription() == banked
    # The control that makes the assertion above mean something: the fallback
    # is reachable, readable, and a DIFFERENT answer.
    assert flow.CrossoverV2Session._applied_blend_correction(
        SimpleNamespace()
    ) == incumbent


#: "the session resolved no position at all", distinct from "it resolved one
#: carrying no instruction" — both must hold the applied graph.
_ABSENT = object()


def test_the_production_incumbent_reader_refuses_a_corrupt_profile(monkeypatch):
    """The converged panel finding, asserted at the PRODUCTION method.

    ``tests/test_crossover_v2_blend_correction.py`` drives the two readers in
    sequence; that proves they compose correctly and cannot notice
    ``_applied_blend_correction`` ceasing to call the strict one — which is
    exactly the shape of the original defect (a guard pointed at the wrong
    reader). This drives the shipped method.
    """

    from jasper.active_speaker import crossover_v2_flow as flow

    reader = flow.CrossoverV2Session._applied_blend_correction

    def _profile(payload):
        monkeypatch.setattr(
            "jasper.active_speaker.baseline_profile."
            "load_applied_baseline_profile_state",
            lambda: payload,
        )
        return reader(SimpleNamespace())

    corrupt = [{"biquad_type": "Peaking", "freq": "1900", "q": 2.0,
                "gain": -1.0}]
    assert _profile({"blend_correction": corrupt}) is None
    assert _profile({"blend_correction": [{"biquad_type": "Peaking",
                                           "freq": 1900.0, "q": 2.0,
                                           "gain": 0.5}]}) is None
    # Positive control: a real record must still read through, or "everything
    # is unknown" would satisfy every assertion above while making the
    # correction permanently unreachable.
    good = [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5}]
    assert _profile({"blend_correction": good}) == tuple(good)
    assert _profile(None) is None


def test_no_instruction_makes_the_next_candidate_hold_the_applied_graph(
    monkeypatch,
):
    """Panel ruling 2, at the hop that carries it out.

    ``_blend_prescription`` is the only place the difference between "no
    instruction" and "apply nothing" becomes a graph. A series instruction
    wins; its ABSENCE falls back to what the speaker is already playing, which
    is what stops a restored round — or a fresh series on an
    already-corrected speaker — from silently dropping an adopted correction.
    """

    from jasper.active_speaker import crossover_v2_flow as flow

    prescribe = flow.CrossoverV2Session._blend_prescription
    applied = ({"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0,
                "gain": -2.5},)
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile."
        "load_applied_baseline_profile_state",
        lambda: {"blend_correction": [dict(f) for f in applied]},
    )

    def _session(instruction):
        # The REAL incumbent reader, so the fallback is proved to go through
        # the same strict path the solve's incumbent does rather than a stub
        # that could disagree with it.
        return SimpleNamespace(
            # A9's source 0, explicitly absent. Named rather than left off: the
            # method reads it FIRST, so a stand-in without the field raises
            # instead of exercising the two sources this test is about — and a
            # stand-in that quietly carried one would be testing the
            # prescription path under a name that says "no instruction".
            _prescribed_blend=None,
            _series_position=(
                None if instruction is _ABSENT
                else SimpleNamespace(previous_blend_correction=instruction)
            ),
            _applied_blend_correction=lambda: (
                flow.CrossoverV2Session._applied_blend_correction(
                    SimpleNamespace()
                )
            ),
        )

    # No instruction -> hold what is applied.
    assert prescribe(_session(_ABSENT)) == applied
    assert prescribe(_session(None)) == applied
    # An EXPLICIT empty instruction -> apply nothing, overriding the graph.
    assert prescribe(_session(())) == ()
    # A real instruction -> that, not the applied graph.
    fresh = ({"biquad_type": "Peaking", "freq": 1200.0, "q": 2.0,
              "gain": -1.0},)
    assert prescribe(_session(fresh)) == fresh


def test_a_restored_round_banks_no_blend_instruction(monkeypatch):
    """Panel ruling, 2026-08-18: a restore does not propagate its prescription.

    A prescription is derived from a measurement taken THROUGH a specific
    incumbent. A restored round threw that graph away, so applying its
    prescription next would compose a correction onto a base that no longer
    exists. The next round instead derives from the applied (restored) profile
    — which is what a ``None`` instruction tells the apply path to do.

    What the round COMMANDED is still history and still banked, in the
    artifact's ``round_measurements.blend``. This key is not history; it is an
    instruction, and a discarded round has no standing to issue one.
    """

    # A USABLE capture that still restores. The unusable-capture path would
    # short-circuit the blend solve to ``None`` and pass this test without ever
    # reaching the gate — the mutation that removes the gate survived exactly
    # that fixture. The adoption verdict is forced instead, so the round
    # genuinely has a blend record and genuinely restores.
    restore = AdoptionDecision(
        outcome=AdoptionOutcome.RESTORE,
        row="row4_untrusted_evidence",
        reason="forced_for_this_test",
    )
    # Patched at ``verification``, which is where ``evaluate_round`` imports
    # it from at call time — patching the coordinator's own name would bind
    # nothing and the round would quietly keep its graph.
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.verification.decide_adoption",
        lambda **_k: restore,
    )

    decision = _direct_round(
        publish=lambda _r: "art",
        analysis=_REGION_ANALYSIS,
        rollback=lambda _reason: True,
        rollback_available=lambda: True,
    )

    identity = decision.receipt_identity
    assert identity["adoption"] == "restore"
    assert decision.evaluation.blend is not None, (
        "the fixture must reach the blend solve, or the gate is untested"
    )
    assert identity["blend"] is None

    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.previous_blend_correction is None, (
        "a restored round handed the next one an instruction"
    )
    assert position.previous_blend_residual_db is None


def test_a_failed_restore_keeps_its_measured_blend_record(monkeypatch):
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.verification.decide_adoption",
        lambda **_k: AdoptionDecision(
            outcome=AdoptionOutcome.RESTORE,
            row="row4_untrusted_evidence",
            reason="forced_for_this_test",
        ),
    )
    decision = _direct_round(
        analysis=_REGION_ANALYSIS,
        rollback=lambda _reason: False,
        rollback_available=lambda: True,
    )
    assert decision.evaluation.blend is not None
    assert decision.evaluation.region_benefit is not None


def test_no_instruction_and_an_empty_instruction_are_different_answers():
    """The distinction the apply path turns on.

    ``None`` means "this series has no instruction for you" and the next
    candidate derives from the APPLIED graph; ``()`` means "apply no blend
    correction" and it does exactly that. Collapsing them would make a
    restored round — or a fresh series on an already-corrected speaker —
    silently drop an adopted correction.
    """

    empty = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1, "objectives": {"tilt_db": 0.0, "ripple_db": 0.0},
        "blend": {"filters": [], "residual_db": 1.0},
    }})
    absent = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1, "objectives": {"tilt_db": 0.0, "ripple_db": 0.0},
    }})

    assert empty.previous_blend_correction == ()
    assert absent.previous_blend_correction is None


@pytest.mark.parametrize(
    "blend",
    [
        {"filters": "not-a-list", "residual_db": 1.0},
        {"filters": [{"biquad_type": "Peaking", "freq": 1.9e3, "q": 2.0,
                      "gain": 0.5}], "residual_db": 1.0},
        {"filters": [{"biquad_type": "Peaking", "freq": "1900", "q": 2.0,
                      "gain": -1.0}], "residual_db": 1.0},
        "not-a-mapping",
        [{"biquad_type": "Peaking", "freq": 1.9e3, "q": 2.0, "gain": -1.0}],
    ],
    ids=["bad-filters", "boost", "string-freq", "string", "legacy-list"],
)
def test_an_unreadable_instruction_reads_as_no_instruction(blend):
    """An instruction nobody can vouch for must not be able to REMOVE an
    adopted correction any more than it can invent one — so it resolves to
    "no instruction", not to "apply nothing"."""

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1, "objectives": {"tilt_db": 0.0, "ripple_db": 0.0},
        "blend": blend,
    }})

    assert position.previous_blend_correction is None


def test_the_two_region_residuals_on_the_receipt_name_their_instruments():
    """Panel correctness SF3.

    The receipt carries two residuals over the same band, referenced
    differently and therefore numerically different. Unlabelled, a reader
    reasonably takes the gap for a defect in one of them.
    """

    # The identity carries the instruction; the artifact carries the numbers.
    # Drive the measurements builder directly for the labelled pair.
    from jasper.active_speaker.crossover_v2 import blend_correction as bc
    from jasper.active_speaker.crossover_v2.verification import Verdict
    from jasper.active_speaker.crossover_v2.contracts import BenefitStatus

    blend = bc.BlendCorrection(
        filters=(), reason=bc.BLEND_NOTHING_TO_CUT, band_hz=(824.35, 3297.4),
        reading=bc.BlendRegionReading(
            band_hz=(824.35, 3297.4), residual_db=1.0006, n_bins=109,
            worst_db=-2.9, worst_hz=1938.0,
        ),
    )
    measurements = coordinator._round_measurements(
        SimpleNamespace(
            delta_probe=None, position_residuals=(), alignment_prescription=None,
            topology_prescription=None,
        ),
        SimpleNamespace(
            blend=blend,
            region_benefit=Verdict(
                BenefitStatus.INDETERMINATE, "residual_within_margin",
                {"post_residual_db": 0.8902},
            ),
        ),
    )

    assert measurements["blend"]["realized"]["instrument"] == (
        "cloud_flat_reference"
    )
    assert measurements["blend"]["region_benefit"]["instrument"] == (
        "region_local_reference"
    )


def test_the_trusted_floor_rides_the_identity_and_reads_back(monkeypatch):
    """#2609 SF5's round trip: the frame travels with the objectives.

    Without it the next round differences two numbers graded over different
    band edges — worth ±0.518 dB on an unchanged curve across a 7↔10 ms gate,
    2.1x the plateau bar — and calls the difference progress.
    """
    decision = _direct_round(publish=lambda _r: "art", trusted_floor_hz=143.0)

    identity = decision.receipt_identity
    assert identity["trusted_floor_hz"] == 143.0
    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.previous_trusted_floor_hz == 143.0


def test_a_receipt_from_before_the_floor_shipped_reads_back_as_unknown():
    """Absent is not zero. A missing floor must not compare equal to a real
    one, and must not refuse every comparison either — see
    ``_floors_comparable`` for which direction unknown takes."""
    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1,
        "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
    }})

    assert position.previous_trusted_floor_hz is None


@pytest.mark.parametrize(
    "verdict",
    sorted(DELTA_PROBE_ROLLBACK_VERDICTS),
    ids=sorted(DELTA_PROBE_ROLLBACK_VERDICTS),
)
def test_a_routed_probe_rollback_keeps_its_own_household_sentence(verdict):
    """The copy the routing was NOT allowed to change.

    Each rollback class has its own sentence, and a household whose speaker was
    reverted for a shape mismatch must not start reading the generic
    unverifiable one because the DECISION moved from the probe's seam to the
    adoption table. Walked end to end: the coordinator's refusal cause, through
    the flow's own mapper, to the code whose copy the household reads.
    """
    probe = SimpleNamespace(
        verdict=verdict, reason="", rollback=True,
        # LOUDER than commanded, so ``model_error`` does not take the #2559
        # deferral — this test is about the classes that DO restore, and the
        # deferred one has its own test above.
        realized_louder_than_commanded=True,
        # …and NOT over the declared boost bound, so the SAFETY axis stays
        # quiet and the row under test is the probe-class one rather than
        # ``row3_unsafe``.
        boost_over_declared_bound=False,
        max_signed_error_db=2.0,
        residual_offset_db=None, residual_offset_tolerance_db=None,
        to_dict=lambda: {"verdict": verdict},
    )

    decision = _direct_round(
        publish=lambda _r: "art",
        rollback=lambda _reason: True,
        rollback_available=lambda: True,
        delta_probe=probe,
    )

    assert decision.evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    assert decision.refusal is not None
    assert flow.round_restore_reason(decision.refusal.cause) == (
        DELTA_PROBE_REASON_BY_VERDICT[verdict]
    ), "the probe's class must reach the household as its own sentence"


def test_an_unknown_probe_class_falls_to_the_floor_not_to_a_guess():
    """A class this build does not have must not borrow another's sentence.

    The floor is the unverifiable code — true of every unmapped cause by
    construction — never the measured-regression one, which would claim a
    finding the round did not make.
    """
    from jasper.active_speaker.crossover_v2.verification import (
        ADOPTION_PROBE_ROLLBACK_CLASS,
    )

    code = flow.round_restore_reason(
        f"{ADOPTION_PROBE_ROLLBACK_CLASS}:a_class_from_the_future"
    )

    assert code == refusal_copy.REASON_CORRECTION_UNVERIFIABLE_RESULT
    assert code != REASON_CORRECTION_MEASURED_REGRESSION


def test_the_position_role_reaches_the_combiners_own_input_struct():
    """§4.2's one line, pinned where it was missing.

    The role was written to the position RECORD and the persisted row and read
    by nothing analytical, because ``cloud_position_capture`` — the ONLY
    builder of the combiner's per-position struct — dropped it. Everything
    downstream (the role-labelled residual, and any reader of
    ``CombinedResponse.position_roles``) is silently unlabelled if this line
    goes away, and no combine-level assertion would notice: the reduction is
    unweighted, so the numbers stay identical.
    """
    import numpy as np

    complex_tf = np.ones(9, dtype=complex)
    position = SimpleNamespace(
        position_id="p_onax",
        role="onax",
        sample_rate_hz=48_000,
        response=SimpleNamespace(
            freqs_hz=np.linspace(20.0, 20_000.0, 9),
            magnitude_db=np.zeros(9),
            complex_tf=complex_tf,
        ),
    )

    capture = flow.cloud_position_capture(position)

    assert capture.position_id == "p_onax"
    assert capture.role == "onax"


def test_a_position_that_declares_no_role_carries_an_empty_one():
    """``""`` rather than ``None``: the combiner's field is a string, and a
    ``None`` would reach a receipt as ``null`` where every other unlabelled
    position reads as empty."""
    import numpy as np

    position = SimpleNamespace(
        position_id="p0", role=None, sample_rate_hz=48_000,
        response=SimpleNamespace(
            freqs_hz=np.linspace(20.0, 20_000.0, 9),
            magnitude_db=np.zeros(9),
            complex_tf=np.ones(9, dtype=complex),
        ),
    )

    assert flow.cloud_position_capture(position).role == ""
