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
4. **retention** — the accepted take reaches the evidence seam, and a failing
   store never costs the household a retake;
5. **the bridge** — the record survives a real stage-1 persist, arrives at a
   real stage-2 conductor with its VALUES intact, is not erased by stage 2's
   own persist, and its absence reaches the capability journal.

.. warning::
   Sections 5's tests drive the REAL preparers through
   ``tests/test_crossover_v2_stage_bridge.py``'s harness, so this module
   inherits that module's known residue (issue #2312): it must not share a
   pytest process with ``tests/test_correction_crossover_v2_endpoints.py``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline
from jasper.active_speaker.crossover_v2_flow import (
    GROUP_PHASES,
    PHASE_ENTRY_BASELINE,
    PHASE_VERIFY,
    REFERENCE_MARK_DESIGN_AXIS,
    SUMMED_SWEEP_PHASES,
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

from tests.test_crossover_v2_conductor import (
    FC_HZ,
    FakeSeams,
    _capture,
    _conductor,
    _roles,
    _run_phase,
    _verify_analysis,
)

# The stage-bridge harness, imported rather than re-implemented so there is one
# definition of "what a real preparer needs stubbed". The autouse fixtures come
# with it by name.
from tests.test_crossover_v2_stage_bridge import (  # noqa: F401  (autouse fixtures)
    _ENTRY_BASELINE_DB,
    _ENTRY_BASELINE_EXCLUDED,
    _ENTRY_BASELINE_FREQS_HZ,
    _ENTRY_BASELINE_GRAPH,
    _ENTRY_BASELINE_PROGRAM_ID,
    _isolated_v2_state,
    _production_host_seams,
    _seed_applied_stage_1_state,
    _stage_1,
    _stage_2,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _stage_1_map() -> dict[int, str]:
    """The index→phase map the shipped stage 1 runs, at the production flags."""
    return build_v2_cloud_index_phase_map(
        plan_shape=resolve_plan_shape("full"),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
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
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    labels = [entry.kind_label for entry in plan.entries]

    assert labels.count("entry_baseline") == 1
    assert labels[-1] == "entry_baseline"
    # The relay drives 0-based entry indexes; the last entry must address the
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
    assert PHASE_ENTRY_BASELINE in flow.CAPTURE_PHASES


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

    entry_program = conductor._program_for_phase(PHASE_ENTRY_BASELINE)
    verify_program = conductor._program_for_phase(PHASE_VERIFY)

    assert entry_program is verify_program
    assert entry_program is not None
    assert entry_program.program_id == verify_program.program_id
    assert entry_program.program_id


def test_the_entry_baseline_gets_no_tracking_prior():
    """Nothing is applied yet, so there is no prediction to track.

    ``_entry_baseline_priors`` withholds ``predicted_sum`` (and the candidate's
    crossover transfers) for the reason ``_cloud_priors`` withholds them: a
    capture that cannot support a claim must not be handed the prior that
    invites one. Handing it MEASURE's prediction would grade the ENTRY graph
    against the CANDIDATE's model and report the whole intended correction as a
    realization error.
    """
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=_stage_1_map())
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)

    priors = conductor._entry_baseline_priors()

    assert priors.predicted_sum is None
    assert priors.configured_crossover_response_by_role is None
    assert priors.configured_polarity_sign_by_role is None
    # …and the one prior it DOES carry, so this is not passing on an
    # all-empty MeasurementPriors.
    assert priors.crossover_fc_hz == FC_HZ


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
    analysis = fakes.verify(conductor._program_for_phase(PHASE_ENTRY_BASELINE))
    assert baseline.program_id == analysis.program_id
    assert baseline.reference_mark == REFERENCE_MARK_DESIGN_AXIS
    # A real reduced curve, not an empty husk.
    assert len(baseline.curve.hz) == len(baseline.curve.db) > 0
    assert len(baseline.excluded) == len(baseline.curve.hz)


# --------------------------------------------------------------------------- #
# 4. retention
# --------------------------------------------------------------------------- #


def test_the_accepted_baseline_is_handed_to_the_retention_seam_once():
    """``entry_baseline`` is not a group phase, so nothing retains it implicitly.

    The reuse is explicit in ``_retain_entry_baseline``; this pins that it
    happens, once, with the three facts a later replay needs to know what it is
    looking at — and that the take id lands on the record as its
    ``artifact_ref``.
    """
    retained: list[tuple[str, Any, dict[str, Any]]] = []
    fakes = FakeSeams()
    conductor = _baseline_only_conductor(
        fakes,
        seams=dataclasses.replace(
            fakes.seams(),
            retain_position=lambda pid, result, meta: retained.append(
                (pid, result, dict(meta))
            ),
        ),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    conductor.consume_capture(1, 1, _capture())

    assert len(retained) == 1
    position_id, _result, metadata = retained[0]
    assert metadata["phase"] == PHASE_ENTRY_BASELINE
    assert metadata["program_id"] == conductor.measure_entry_baseline.program_id
    assert metadata["reference_mark"] == REFERENCE_MARK_DESIGN_AXIS
    assert metadata["graph_fingerprint"]
    assert conductor.measure_entry_baseline.artifact_ref == position_id


def test_a_failing_retention_store_does_not_cost_the_household_a_retake():
    """Evidence retention is forensics, never a gate.

    Same guarantee ``_retain_cloud_position`` gives, stated for the phase that
    reuses it: a full disk must not turn an acoustically-good baseline into a
    retake. The record is still banked — what is lost is only the pointer to
    the stored bytes.
    """
    fakes = FakeSeams()

    def _explode(_pid, _result, _meta):
        raise OSError("disk full")

    conductor = _baseline_only_conductor(
        fakes, seams=dataclasses.replace(fakes.seams(), retain_position=_explode),
    )

    conductor.authorize_begin(1, 1)
    conductor.on_armed()
    outcome = conductor.consume_capture(1, 1, _capture())

    assert outcome["accepted"] is True
    assert conductor.measure_entry_baseline is not None
    # No artifact to point at, and the record says so rather than naming a
    # take nobody stored.
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
    this key on every write — including ``prepare_v2_verify``'s own opening
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
