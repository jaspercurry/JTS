# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291's entry baseline: the round's measured "before".

Phase 3c adds ONE capture — ``PHASE_ENTRY_BASELINE``, at the design-axis mark,
immediately before the household applies — so a correction round can say
whether the speaker got *better* rather than only whether the graph did what it
commanded. The 2026-08-10 jts3 round had CHECK/MEASURE, a lateral walk, VERIFY,
and five post-apply cloud positions, and still could not answer that, because
none of it was the same summed acoustic question before and after the change.

What this module pins, in the order the evidence has to survive:

1. **the plan** — exactly one entry, and it is LAST;
2. **comparability** — the capture replays the VERIFY program *object*, so the
   two sides share a ``program_id`` and the benefit evaluator's own
   comparability check can pass;
3. **the accept rule** — an unusable capture records nothing, a usable one
   records a baseline stamped with the shared mark;
4. **retention** — the accepted take reaches the evidence seam, reads back
   through the shipped reader into an EQUAL record, and a failing store never
   costs the household a retake;
5. **the bridge** — the record survives a real stage-1 persist, arrives at a
   real stage-2 conductor with its VALUES intact, is not erased by stage 2's
   own persist, and its absence reaches the capability journal.

**Two copies, one of them durable** (fragment ``02``'s duplication #2, closed
here). The flow state file is the ROUND's channel: ``verify_priors`` is rebuilt
from the conductor on every persist, which is what lets a fresh measuring
session write its own honest absence over a previous round's "before" — section
5 pins that, and it is deliberate. The copy that OUTLIVES the round is the
write-once retained take, which now carries the reduced curve and not only its
scalars. Both are written from one ``MeasuredResponse`` at capture time, so
neither is derived from the other and section 4's round trip is what keeps them
one fact.

Section 5's tests drive the REAL preparers through
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
import tempfile
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import journey, position_cycle, priors
from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline
from jasper.active_speaker.crossover_v2.journey import (
    GROUP_PHASES,
    PHASE_ENTRY_BASELINE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.contracts import REFERENCE_MARK_DESIGN_AXIS
from jasper.active_speaker.crossover_v2.programs import SUMMED_SWEEP_PHASES
from jasper.active_speaker.crossover_v2_flow import (
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    resolve_plan_shape,
)
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_FAIL,
    CaptureIntegrity,
    IntegrityCheck,
)
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    bank_into,
    _capture,
    _conductor,
    _roles,
    _run_phase,
    _verify_analysis,
)

# The stage-bridge harness, imported rather than re-implemented so there is one
# definition of "what a real preparer needs stubbed".
#
# The two autouse fixtures are imported under the redundant-alias re-export
# form, which is what tells the linter they are deliberate module-level names
# rather than dead imports: pytest activates an autouse fixture by its presence
# in the module namespace, so nothing here CALLS them and a plain import reads
# as unused. The alias form says the same thing a lint-suppression comment
# would, without adding to the repo's frozen suppression debt.
from tests.test_crossover_v2_stage_bridge import (
    _ENTRY_BASELINE_DB,
    _ENTRY_BASELINE_EXCLUDED,
    _ENTRY_BASELINE_FREQS_HZ,
    _ENTRY_BASELINE_GRAPH,
    _ENTRY_BASELINE_PROGRAM_ID,
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
    _seed_applied_stage_1_state,
    _stage_1,
    _stage_2,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #



# Production refuses a session with no volume owner; stand one up.
pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

def _stage_1_map() -> dict[int, str]:
    """The index→phase map the shipped stage 1 runs, at the production flags."""
    return build_v2_cloud_index_phase_map(
        plan_shape=resolve_plan_shape("full"),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )


def _baseline_only_conductor(fakes: FakeSeams, **kwargs):
    """A conductor whose only capture index is the entry baseline.

    CHECK and MEASURE are marked accepted rather than run, so a test about the
    baseline's own verdict is not also a test of the anchor's — but the
    conductor is the REAL one, with the real dispatch, priors, and seams.
    """
    return _conductor(
        fakes,
        index_phase_map={1: PHASE_ENTRY_BASELINE},
        accepted_phases=(flow.PHASE_CHECK, flow.PHASE_MEASURE),
        **kwargs,
    )


def _read_take(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """The retained take, through the SHIPPED reader and the real store envelope.

    ``BankedRecordStore.bank`` wraps a builder's record in ``schema_version``
    + ``kind`` before it lands, so handing the bare metadata to the reader would
    prove nothing about the file the speaker actually writes — and the reader
    filters on that ``kind``.
    """
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{metadata['take_id']}.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "kind": position_cycle.POSITION_EVIDENCE_KIND,
            **metadata,
        }))
        return position_cycle.read_entry_baseline_take(path)


def _failed_integrity() -> CaptureIntegrity:
    """An integrity record with a FAILED check — the unusable shape.

    Built from the type's own vocabulary rather than a stand-in, so the
    conductor's gate is exercised against what the analyzer really produces.
    """
    return CaptureIntegrity(
        checks=(
            IntegrityCheck(
                name=flow.INTEGRITY_CHECK_SWEEP_HEARD, status=INTEGRITY_FAIL,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# 1. the plan
# --------------------------------------------------------------------------- #


def test_stage_1_plans_exactly_one_entry_baseline_and_it_is_last():
    """Position, not membership.

    "Immediately before apply" is the entry baseline's whole justification —
    every capture that followed it would be room, microphone, and household
    drift landing inside the before→after bracket. An entry that merely EXISTS
    somewhere in the plan would satisfy a membership check and lose that.
    """
    plan = build_v2_capture_plan(
        _roles(), FC_HZ, plan_shape=resolve_plan_shape("full"),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    labels = [entry.kind_label for entry in plan.entries]

    assert labels.count("entry_baseline") == 1
    assert labels[-1] == "entry_baseline"
    # The capture drives 0-based entry indexes; the last entry must address the
    # last slot, or "last in the list" would not mean "last in the session".
    assert plan.entries[-1].index == plan.capture_target - 1

    index_phase = _stage_1_map()
    assert index_phase[max(index_phase)] == PHASE_ENTRY_BASELINE
    assert list(index_phase.values()).count(PHASE_ENTRY_BASELINE) == 1


def test_the_entry_baseline_is_a_summed_sweep_and_not_a_position_group():
    """The two set memberships that decide what this phase IS.

    ``SUMMED_SWEEP_PHASES`` is what routes it to the VERIFY program (the
    comparability condition, pinned behaviourally below) and to the host's
    live-graph play branch. ``GROUP_PHASES`` is per-index group bookkeeping for
    a phase spanning many prompted positions — this is one capture at one mark,
    and joining it would give it a group's close, geometry verdict, and combine
    over a single curve.
    """
    assert PHASE_ENTRY_BASELINE in SUMMED_SWEEP_PHASES
    assert PHASE_ENTRY_BASELINE not in GROUP_PHASES
    assert PHASE_ENTRY_BASELINE in journey.CAPTURE_PHASES


# --------------------------------------------------------------------------- #
# 2. comparability — the same program object, on a real conductor
# --------------------------------------------------------------------------- #


def test_the_entry_baseline_replays_the_verify_program_object_itself():
    """Identity AND ``program_id``, because either alone is satisfiable falsely.

    Object identity alone would pass if both sides resolved to ``None``. Equal
    ids alone would pass for two independently composed programs that happen to
    agree today and would diverge the moment either composer changed. Together
    they say what #2291 needs: the before and the after are the same excitation
    schedule at the same level, cryptographically, because they are the same
    object.
    """
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=_stage_1_map())
    # CHECK's gain solve is what composes the programs; run it for real.
    _run_phase(conductor, 1, 1)

    entry_program = conductor.program_for_phase(PHASE_ENTRY_BASELINE)
    verify_program = conductor.program_for_phase(PHASE_VERIFY)

    assert entry_program is verify_program
    assert entry_program is not None
    assert entry_program.program_id == verify_program.program_id
    assert entry_program.program_id


def test_the_entry_baseline_gets_no_tracking_prior():
    """Nothing is applied yet, so there is no prediction to track.

    ``entry_baseline_priors`` withholds ``predicted_sum`` (and the candidate's
    crossover transfers) for the reason ``cloud_priors`` withholds them: a
    capture that cannot support a claim must not be handed the prior that
    invites one. Handing it MEASURE's prediction would grade the ENTRY graph
    against the CANDIDATE's model and report the whole intended correction as a
    realization error.

    Asserted against the priors module that OWNS the rule rather than through
    the flow's one-line delegation to it: the withholding is analysis-layer
    behaviour and survives the flow whole, so a pin reaching through a private
    conductor method would be pinning the delegation instead of the rule.
    """
    entry = priors.entry_baseline_priors(fc_hz=FC_HZ)

    assert entry.predicted_sum is None
    assert entry.configured_crossover_response_by_role is None
    assert entry.configured_polarity_sign_by_role is None
    # …and the one prior it DOES carry, so this is not passing on an
    # all-empty MeasurementPriors.
    assert entry.crossover_fc_hz == FC_HZ
    # The conductor reaches it and adds nothing of its own — which is what
    # makes the assertion above a statement about the shipped path.
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=_stage_1_map())
    assert conductor._entry_baseline_priors() == entry


# --------------------------------------------------------------------------- #
# 3. the accept rule
# --------------------------------------------------------------------------- #


def test_an_unusable_capture_is_rejected_and_records_no_baseline():
    """A capture the shipped comparability rule calls UNUSABLE is not evidence.

    ``evaluate_capture_validity`` is the same function the post-apply side is
    graded by, so a baseline that slipped past it would be a "before" the
    evaluator would then refuse — the round would lose its comparison anyway,
    but only after the household had walked away.
    """
    fakes = FakeSeams()
    fakes.verify = lambda program: dataclasses.replace(
        _verify_analysis(program), capture_integrity=_failed_integrity(),
    )
    conductor = _baseline_only_conductor(fakes)

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is False
    assert conductor.measure_entry_baseline is None
    assert PHASE_ENTRY_BASELINE not in conductor.accepted_phases


def test_a_usable_capture_records_a_baseline_stamped_with_program_and_mark():
    """The accept path's product: a record the evaluator can compare.

    ``program_id`` is asserted against the ANALYSIS's own, not against a
    literal, so this cannot pass by agreeing with a constant nobody measured.
    """
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(fakes)

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is True
    baseline = conductor.measure_entry_baseline
    assert baseline is not None
    analysis = fakes.verify(conductor.program_for_phase(PHASE_ENTRY_BASELINE))
    assert baseline.program_id == analysis.program_id
    assert baseline.reference_mark == REFERENCE_MARK_DESIGN_AXIS
    # A real reduced curve, not an empty husk.
    assert len(baseline.curve.hz) == len(baseline.curve.db) > 0
    assert len(baseline.excluded) == len(baseline.curve.hz)


# --------------------------------------------------------------------------- #
# 4. retention
# --------------------------------------------------------------------------- #


def test_the_retained_take_rehydrates_into_the_record_the_round_holds():
    """The seam, end to end: what the round grades, and what outlives it, agree.

    ``entry_baseline`` is not a group phase, so nothing retains it implicitly —
    the reuse is explicit in ``_retain_entry_baseline``, and this pins that it
    happens once. What it asserts is not a field list: the retained take is
    read back through the SHIPPED reader and rehydrated through the SHIPPED
    ``from_dict``, and the result must EQUAL the record the round is about to
    grade against. Field-by-field agreement is implied by that and cannot pass
    while a column is silently dropped.

    This is fragment ``02``'s duplication #2 closed. The arrays used to reach
    disk only through the flow state file, which ``persist_conductor_state``
    rebuilds on every write, so a banked round's "before" stopped existing the
    moment the next round persisted — and the receipt's digest (``n_bins``,
    ``n_excluded``) was all that was left of it. The retained take is
    write-once and keyed by ``take_id``.
    """
    retained: list[tuple[str, Any, dict[str, Any]]] = []
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(),
            bank_take=bank_into(retained, with_capture=True),
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())

    assert len(retained) == 1
    _result, metadata = retained[0]
    assert metadata["phase"] == PHASE_ENTRY_BASELINE
    assert conductor.measure_entry_baseline.artifact_ref == metadata["take_id"]
    assert EntryBaseline.from_dict(
        _read_take(metadata)
    ) == conductor.measure_entry_baseline


def test_the_retained_take_carries_the_phase_the_baseline_measured():
    """Ruling S3 on the kind that banked magnitude and threw phase away.

    This take already carried a curve — the reduced, magnitude-only benefit
    curve the round grades its before→after claim over. What it never carried
    was the complex response its own analysis computed, so an offline
    re-analysis had to re-derive phase from the WAV and the forward model could
    not run from the bank.

    Two curves on two bases now ride, and neither is derivable from the other:
    the graded arrays above, and ``curves`` on the shared log basis with
    ``phase_deg``. Asserted as a RECONSTRUCTION against the analysis's own
    values at the bins the record names, not as key presence.
    """
    import numpy as np

    retained: list[dict[str, Any]] = []
    fakes = FakeSeams()
    measured: list = []

    def _complex_verify(program, **kwargs):
        analysis = _verify_analysis(program, **kwargs)
        summed = analysis.summed_response
        wound = dataclasses.replace(
            summed,
            # The fixture's TF is real and positive, so every phase is 0.0 and
            # a record that carried none would round-trip. Wind a ramp on.
            complex_tf=np.abs(summed.complex_tf) * np.exp(
                1j * np.linspace(-9.0, 9.0, np.asarray(summed.freqs_hz).size)
            ),
        )
        analysis = dataclasses.replace(analysis, summed_response=wound)
        measured.append(wound)
        return analysis

    fakes.verify = _complex_verify
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(), bank_take=bank_into(retained),
        ),
    )
    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())

    (metadata,) = retained
    (curve,) = metadata["curves"]
    source = measured[-1]
    rebuilt = 10.0 ** (np.asarray(curve["magnitude_db"]) / 20.0) * np.exp(
        1j * np.radians(np.asarray(curve["phase_deg"]))
    )
    at = {float(hz): i for i, hz in enumerate(source.freqs_hz)}
    sampled = [at[hz] for hz in curve["freqs_hz"]]

    assert curve["role"] == "summed"
    assert np.allclose(rebuilt, np.asarray(source.complex_tf)[sampled])
    # Not vacuous: the wound phase really is non-zero.
    assert np.any(np.abs(np.asarray(curve["phase_deg"])) > 1.0)
    # And the graded arrays are untouched — a second basis, not a replacement.
    assert metadata["freqs_hz"] and metadata["magnitude_db"]


def test_a_failing_retention_store_does_not_cost_the_household_a_retake():
    """Evidence retention is forensics, never a gate.

    A full disk must not turn an acoustically-good baseline into a retake. The
    round still grades against its own "before" — that record is held in memory
    and written to the flow state file, neither of which this seam touches.
    What a failed store costs is the copy that would have OUTLIVED the round,
    and the record says so by naming no artifact rather than naming a take
    nobody wrote.

    The RAISE is the binding's to catch (``bind_position_retention``), which is
    why this drives the seam's own vocabulary for it: ``""`` — nothing stored.
    This is the only site that reads that answer, so this is where it is pinned.
    """
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(), bank_take=lambda _result, _record: "",
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is True
    assert conductor.measure_entry_baseline is not None
    assert conductor.measure_entry_baseline.artifact_ref == ""


def test_an_unnamed_entry_graph_degrades_to_a_word_rather_than_a_crash():
    """No fingerprint seam is the ordinary state of an offline conductor.

    ``EntryBaseline`` requires a non-empty trimmed identity on both the write
    and the read side, so ``""`` would make the record unrehydratable — the
    round would lose its baseline to a missing *provenance* field. The sentinel
    survives the round trip and says what is true.
    """
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(fakes)
    assert conductor._seams.entry_graph_fingerprint is None

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())

    baseline = conductor.measure_entry_baseline
    assert baseline.graph_fingerprint == flow.ENTRY_GRAPH_FINGERPRINT_UNKNOWN
    assert EntryBaseline.from_dict(baseline.to_dict()) == baseline


def test_a_raising_fingerprint_seam_is_survived_the_same_way():
    """The control for the branch above: a bound-but-broken seam.

    Without this, ``entry_graph_fingerprint=None`` would be the only path
    tested and a seam that raised would take the capture down with it.
    """
    fakes = FakeSeams()

    def _explode() -> str:
        raise RuntimeError("camilla is not answering")

    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(), entry_graph_fingerprint=_explode,
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is True
    assert (
        conductor.measure_entry_baseline.graph_fingerprint
        == flow.ENTRY_GRAPH_FINGERPRINT_UNKNOWN
    )


def test_a_seam_that_answers_with_nothing_gets_the_same_word():
    """The branch a mutation caught this file half-guarding.

    ``entry_graph_fingerprint=None`` (above) and a seam RETURNING ``""`` are
    two different lines in ``_entry_graph_fingerprint``, and the second is the
    one production actually hits: ``_active_graph_fingerprint`` returns ``""``
    on a speaker with no applied Layer-A profile — its first-ever round, which
    is precisely when #2291's rollback-anchor question matters most. With only
    the ``None`` case covered, deleting the ``or ENTRY_GRAPH_FINGERPRINT_UNKNOWN``
    fallback broke nothing.
    """
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(), entry_graph_fingerprint=lambda: "   ",
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is True
    baseline = conductor.measure_entry_baseline
    assert baseline.graph_fingerprint == flow.ENTRY_GRAPH_FINGERPRINT_UNKNOWN
    assert EntryBaseline.from_dict(baseline.to_dict()) == baseline


def test_a_bound_fingerprint_seam_reaches_the_record():
    """…and the positive case, so the three above are not passing vacuously."""
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(), entry_graph_fingerprint=lambda: "fp-live-graph",
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())

    assert conductor.measure_entry_baseline.graph_fingerprint == "fp-live-graph"


# --------------------------------------------------------------------------- #
# 5. the stage bridge — driven through the REAL preparers
# --------------------------------------------------------------------------- #


def test_a_captured_baseline_reaches_the_durable_state(tmp_path):
    """The WRITE side, driven from a real capture through the real persist.

    The other bridge tests seed the state file, so none of them can see the
    conductor→disk half — a mutation that persisted a hard ``None`` for this
    key left every one of them green. This drives it end to end: the conductor
    captures, ``persist_conductor_state`` writes, and the module-level reader
    hands back a record equal to the one the conductor holds.
    """
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(fakes)
    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())
    assert conductor.measure_entry_baseline is not None

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}

    written = state["verify_priors"]["entry_baseline"]
    assert written is not None
    assert written["program_id"] == conductor.measure_entry_baseline.program_id
    assert written["reference_mark"] == REFERENCE_MARK_DESIGN_AXIS
    # Round-trips to an EQUAL record, so nothing was lost or reshaped by the
    # serialization the two stages actually communicate through.
    assert (
        v2host.entry_baseline_prior_from_state(state)
        == conductor.measure_entry_baseline
    )


def test_the_baseline_crosses_the_bridge_with_its_values_intact(monkeypatch):
    """Stage 1 writes it, stage 2 is constructed with it — values and all.

    "Not None" would pass for a conductor handed anything at all, so every
    field the benefit comparison reads is checked: the curve's points, the
    exclusion mask, the program id, and the mark. The mask especially — a
    dropped mask would still produce a comparable-looking record while
    silently changing which bins the residual is pooled over.
    """
    _seed_applied_stage_1_state()

    conductor, _state = _stage_2(monkeypatch)

    baseline = conductor.measure_entry_baseline
    assert baseline is not None
    assert list(baseline.curve.hz) == _ENTRY_BASELINE_FREQS_HZ
    assert list(baseline.curve.db) == _ENTRY_BASELINE_DB
    assert list(baseline.excluded) == _ENTRY_BASELINE_EXCLUDED
    assert baseline.program_id == _ENTRY_BASELINE_PROGRAM_ID
    assert baseline.reference_mark == REFERENCE_MARK_DESIGN_AXIS
    assert baseline.graph_fingerprint == _ENTRY_BASELINE_GRAPH


def test_stage_2_persist_does_not_erase_the_baseline_it_was_handed(monkeypatch):
    """The carry-forward, on the write that happens before any tone plays.

    Stage 2 never captures a baseline, so its conductor persists ``None`` for
    this key on every write — including the verify-only prepare's own opening
    persist. Without the carry-forward that first write would erase the exact
    measurement stage 2 exists to grade against, and the round would report
    ``entry_baseline_unavailable`` about a record it had just been handed.
    """
    _seed_applied_stage_1_state()

    _conductor_2, state = _stage_2(monkeypatch)

    assert state["verify_priors"]["entry_baseline"] is not None
    assert (
        state["verify_priors"]["entry_baseline"]["program_id"]
        == _ENTRY_BASELINE_PROGRAM_ID
    )
    # …and it is still readable as a record, not merely present as a dict.
    assert v2host.entry_baseline_prior_from_state(state) is not None


def test_a_measuring_session_replaces_the_baseline_rather_than_inheriting_one(
    monkeypatch,
):
    """The other half of the carry-forward, which is the load-bearing one.

    A baseline is a property of the graph on the speaker WHEN IT WAS TAKEN. A
    fresh measuring session must write its own — or its own honest absence —
    rather than let a previous round's "before" be differenced against this
    round's "after". Grading across that boundary is the false comparison
    #2291 exists to stop.
    """
    _seed_applied_stage_1_state()

    _conductor_1, state = _stage_1(monkeypatch)

    assert flow.PHASE_MEASURE in state["session_phases"]
    assert state["verify_priors"]["entry_baseline"] is None


def test_stage_2_without_a_baseline_says_so_on_the_capability_line(
    monkeypatch, caplog,
):
    """The absence has to be observable, not only visible in the verdict.

    ``requires`` is observability, not a gate: stage 2 still opens and still
    verifies. What it cannot do is claim the speaker got better — so the one
    line a support read greps has to name the missing input, or an
    ``indeterminate`` benefit verdict arrives with nothing anywhere explaining
    it.
    """
    state = _seed_applied_stage_1_state()
    del state["verify_priors"]["entry_baseline"]
    v2host.save_v2_state(state)

    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        conductor, _state = _stage_2(monkeypatch)

    assert conductor.measure_entry_baseline is None
    unavailable = [
        record.getMessage() for record in caplog.records
        if "event=correction.crossover_v2_stage_capability_unavailable"
        in record.getMessage()
    ]
    assert len(unavailable) == 1
    assert "missing=entry_baseline" in unavailable[0]


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"freqs_hz": [100.0, 200.0], "magnitude_db": [0.0, 1.0]},
        {
            "freqs_hz": [100.0, 200.0],
            "magnitude_db": [0.0, 1.0],
            "excluded": [False],
            "program_id": "p",
            "reference_mark": REFERENCE_MARK_DESIGN_AXIS,
            "graph_fingerprint": "g",
            "captured_at": "2026-08-10T00:00:00Z",
        },
    ],
    ids=["absent", "empty", "no-mask", "mask-length-mismatch"],
)
def test_an_unreadable_record_reads_as_no_baseline_rather_than_raising(record):
    """Every honest "there is nothing comparable here" is the same answer.

    A state file from before this key shipped, a truncated write, and a
    hand-edited one all mean one thing to the round. The reader must not raise
    mid-open over any of them — a stage that refused to start on an unreadable
    prior would strand a household whose only remaining move is the one being
    refused.
    """
    state = {"verify_priors": {"entry_baseline": record}}

    assert v2host.entry_baseline_prior_from_state(state) is None
