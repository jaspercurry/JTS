# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from tests.crossover_v2_fixtures import _check_analysis, _verify_analysis

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
import asyncio
from dataclasses import replace
from tests._async_wait import wait_signalled
from jasper.active_speaker import delta_probe
from jasper.active_speaker.crossover_v2.journey import PHASE_CHECK, PHASE_VERIFY
from tests.engine_twin import FakeSeams as EngineFakeSeams
from jasper.audio_measurement.program import STIMULUS_KINDS
from jasper.active_speaker.crossover_v2.capture_dispatch import CLIP_RETRY_BACKOFF_DB
from tests.crossover_v2_fixtures import (_MINTED_CAPTURE_SESSION_ID, _PERSISTED_TOP_LEVEL_KEYS, _RecordingCheckStore, _delta_probe_given_a_tracking_curve, _flow_seams, _inline_body, _install_commanded_delta, _open_prepared, _regradable_fixture, _session_from_real_open, _stage_1, _status, _topology)

from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state

from jasper.active_speaker.delta_probe import classify_delta_probe
from jasper.active_speaker.crossover_v2 import delta_probe_run
from tests.test_active_speaker_delta_probe import _GRID_HZ, _band, _commanded_lift

import dataclasses
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from jasper.active_speaker.crossover_v2 import durable_state
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_ADVISE_AGAINST_KEEP_VERDICTS,
    SEAM_DEFERRED_QUIETER_THAN_COMMANDED,
    VERDICT_MODEL_ERROR,
)
from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.contracts import (
    ADOPTION_ROW_KEEP_ITERATING,
    AdoptionOutcome,
)
from jasper.active_speaker.crossover_v2.contracts import (
    QualityStatus,
)
from jasper.active_speaker.crossover_v2.verification import (
    HEADROOM_NO_OBJECTIVES,
    HEADROOM_REACHABLE,
    Verdict,
)
from jasper.active_speaker.crossover_v2.contracts import ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status

from tests._log_events import event_records

from tests.crossover_v2_round_harness import (
    _consume_verify,
    _install_applied_graph,
    _install_entry_baseline,
    _post_apply_analysis,
    _restoring_stage_2,
    _round_session,
    _seed_round_state,
    _stub_restore_doors,
    _tracking_curve_change_from_entry,
)

from tests.crossover_v2_fixtures import (
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)

pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

def _hydrated_series_position(conductor: Any) -> Any:
    return conductor._series_position

def _round_receipt_json(store: Any, capture_session_id: str) -> dict[str, Any]:
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

@pytest.fixture
def real_bundle(monkeypatch, tmp_path):
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
        v2evidence, "open_v2_evidence_store",
        lambda topology: (store, store.session_id),
    )
    return store

def test_a_measurably_improved_round_keeps_the_graph_and_the_verdict(monkeypatch):
    """Measured improvement keeps the graph and reaches the verified screen."""
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    evaluation = conductor.round_evaluation
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.row == ADOPTION_ROW_KEEP_ITERATING
    assert evaluation.adoption.reason == HEADROOM_NO_OBJECTIVES
    assert evaluation.quality.evidence["targets"] == ["spec:no_spec_report"]
    assert attempts == []

def test_a_round_reaches_its_readers_and_publisher(monkeypatch):
    seen: list[str] = []
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
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
        records=dataclasses.replace(
            bound.records,
            round_receipt=_recorded(
                "publish_round_receipt", bound.records.round_receipt,
            ),
        ),
        **{
            name: _recorded(name, getattr(bound, name))
            for name in (
                "rollback_available", "applied_boosts",
                "entry_graph_fingerprint",
            )
        },
    )

    _consume_verify(conductor, _post_apply_analysis(conductor))

    assert set(seen) == {
        "rollback_available", "applied_boosts",
        "entry_graph_fingerprint", "publish_round_receipt",
    }

def test_the_round_receipt_lands_in_the_bundle_fingerprinted_and_readable(
    monkeypatch, real_bundle,
):
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
    assert receipt["adoption"]["reason"] == HEADROOM_NO_OBJECTIVES
    assert receipt["entry_baseline"]["program_id"] == (
        conductor.measure_entry_baseline.program_id
    )
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    identity = conductor.round_receipt_identity
    assert identity["round_id"] == _MINTED_CAPTURE_SESSION_ID
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]
    assert identity["artifact_fingerprint"]
    assert identity["topology_fingerprint"] == coordinator.topology_config_fingerprint(
        coordinator.load_output_topology()
    )

def test_a_quieter_only_shape_miss_reaches_the_table_instead_of_the_seam(
    monkeypatch, real_bundle, caplog,
):
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
    assert conductor.delta_probe.verdict == VERDICT_MODEL_ERROR
    assert conductor.delta_probe.advises_against_keep is True
    assert conductor.delta_probe.realized_louder_than_commanded is False

    assert attempts == [], "no Undo may run for a quieter-only shape miss"
    assert verdict.accepted is True

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["round_axes"]["quality"]["evidence"]["probe_rollback_class"] == ""
    safety = receipt["round_axes"]["safety"]
    assert safety["evidence"]["seam_deferred"] == (
        SEAM_DEFERRED_QUIETER_THAN_COMMANDED
    )
    assert safety["evidence"]["realized_louder_than_commanded"] is False
    assert receipt["adoption"]["outcome"] in {
        AdoptionOutcome.KEEP.value, AdoptionOutcome.KEEP_FOR_ITERATION.value,
    }

_PROPOSAL_FP = "e" * 64

def _seed_round_state_proposing(fingerprint: str) -> dict[str, Any]:
    """Stage-1 durable state that DID cross a proposal fingerprint (#2392)."""
    state = _seed_round_state()
    state["verify_priors"]["proposal_fingerprint"] = fingerprint
    v2state.save_v2_state(state)
    return state

def test_the_receipt_names_the_proposal_that_was_made(monkeypatch, real_bundle):
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
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)

def test_a_pre_2392_stage_1_still_gets_a_receipt_and_it_says_so(
    monkeypatch, real_bundle,
):
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
    from jasper.active_speaker.crossover_v2.contracts import (
        PROPOSAL_FINGERPRINT_KINDS,
    )

    _seed_round_state_proposing(_PROPOSAL_FP)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)
    _consume_verify(conductor, _post_apply_analysis(conductor))
    new_regime = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)

    old_regime = {
        key: value for key, value in new_regime.items()
        if key != "proposal_fingerprint_kind"
    }
    old_regime["proposal_fingerprint"] = "fp-stage-1"

    assert len(new_regime["proposal_fingerprint"]) == 64

    assert "proposal_fingerprint_kind" not in old_regime, "pre-#2392: absent"
    assert new_regime["proposal_fingerprint_kind"] in PROPOSAL_FINGERPRINT_KINDS
    assert new_regime["proposal_fingerprint_kind"] == "intervention_proposal"

def test_the_receipt_is_written_exactly_once_with_the_payload_that_was_graded(
    monkeypatch, real_bundle,
):
    from tests.crossover_v2_fixtures import with_records

    written: list[dict[str, Any]] = []
    real_publish = None

    _seed_round_state_proposing(_PROPOSAL_FP)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    real_publish = _flow_seams(conductor).records.round_receipt

    def _recording_publish(receipt: dict[str, Any]) -> str:
        written.append(dict(receipt))
        return real_publish(receipt)

    conductor._seams = with_records(
        _flow_seams(conductor), round_receipt=_recording_publish,
    )

    _consume_verify(conductor, _post_apply_analysis(conductor))
    _consume_verify(conductor, _post_apply_analysis(conductor), attempt=2)

    assert len(written) == 1, "a write-once receipt path may be written once"
    payload = written[0]
    assert payload["proposal_fingerprint"] == _PROPOSAL_FP
    assert payload["proposal_fingerprint_kind"] == "intervention_proposal"
    assert payload["round_id"] == _MINTED_CAPTURE_SESSION_ID
    core = {key: value for key, value in payload.items() if key != "fingerprint"}
    assert payload["fingerprint"] == json_fingerprint(core)
    assert conductor.round_receipt_identity["receipt_fingerprint"] == (
        payload["fingerprint"]
    )
    assert _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID) == payload

def test_a_failing_receipt_store_costs_the_round_nothing(monkeypatch, caplog):
    """A failed receipt write preserves the verdict and ordinal, with empty hashes."""
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    from tests.crossover_v2_fixtures import with_records

    def _explode(_receipt):
        raise OSError("no space left on device")

    conductor._seams = with_records(_flow_seams(conductor), round_receipt=_explode)

    with caplog.at_level("WARNING", logger="jasper.active_speaker.crossover_v2_flow"):
        verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    assert (
        conductor.round_evaluation.adoption.outcome
        is AdoptionOutcome.KEEP_FOR_ITERATION
    )
    assert attempts == []
    identity = conductor.round_receipt_identity
    assert identity is not None
    assert identity["artifact_fingerprint"] == ""
    assert identity["receipt_fingerprint"] == ""
    assert identity["round_ordinal"] == 1
    assert "objectives" in identity
    assert identity["spec"] is None
    assert coordinator.series_position_from_state(
        {"round_receipt": identity}
    ).ordinal == 2
    failures = event_records(caplog, "correction.crossover_v2_round_receipt_failed")
    assert failures, "a lost receipt must be recorded, not silent"
    assert [record.levelname for record in failures] == ["ERROR"]

def test_a_grader_bug_never_turns_an_accepted_capture_into_a_refusal(
    monkeypatch, caplog,
):
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=2.0)
    _install_applied_graph(monkeypatch, boosts=False)

    def _explode(**kwargs: Any) -> Any:
        raise RuntimeError("grader is broken")

    monkeypatch.setattr(coordinator, "evaluate_round", _explode)

    with caplog.at_level("WARNING"):
        verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    assert verdict.code is None
    assert conductor.round_evaluation is None
    assert conductor.round_receipt_identity is None
    assert attempts == []
    assert event_records(caplog, "correction.crossover_v2_round_grade_failed")

def test_the_fire_once_guard_holds_against_a_second_grade_on_one_trigger(
    monkeypatch,
):
    _seed_round_state()
    conductor, attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)

    first = _consume_verify(conductor, _post_apply_analysis(conductor))
    assert first.accepted is True
    assert attempts == []
    graded = conductor.round_evaluation

    second = _consume_verify(conductor, _post_apply_analysis(conductor), attempt=2)

    assert second.accepted is True, "the second pass grades nothing, so it refuses nothing"
    assert conductor.round_evaluation is graded, "the round was not re-decided"
    assert attempts == [], "no second restore was attempted"

def test_the_model_error_store_banks_the_tracking_number_not_the_ledger_grade(
    monkeypatch,
):
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

    real_record_from_verify = durable_state.attempt_record_from_verify

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

def _recorded_write_calls(monkeypatch) -> list[str]:
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
    _seed_round_state(previous_candidate=False)
    calls = _recorded_write_calls(monkeypatch)

    v2state.observe_apply_success(
        "fp-stage-1", previous_candidate_fingerprint="fp-previous",
    )

    assert calls == ["chmod", "fsync", "replace", "fsync"]
    assert (
        v2state.load_v2_state()["previous_candidate_fingerprint"] == "fp-previous"
    )

def test_an_ordinary_conductor_persist_is_not_fsynced(monkeypatch):
    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    assert conductor.round_receipt_identity is None
    calls = _recorded_write_calls(monkeypatch)

    v2state.persist_conductor_state(conductor, failure_code=None)

    assert calls == ["chmod", "replace"]

def test_an_unbound_anchor_probe_fails_closed_QUIETLY(caplog):
    ports = coordinator.RoundPorts(
        rollback_available=None,
    )

    with caplog.at_level("DEBUG"):
        answer = coordinator.rollback_available(ports, session_id="cap_x")

    assert answer is False
    assert not event_records(
        caplog, "correction.crossover_v2_rollback_available_failed"
    ), "an unbound probe is a configuration fact, not a failure to report"

def test_an_anchor_probe_that_raises_fails_closed_LOUDLY(caplog):

    def _explode() -> bool:
        raise RuntimeError("the durable state is unreadable")

    ports = coordinator.RoundPorts(
        rollback_available=_explode,
    )

    with caplog.at_level("DEBUG"):
        answer = coordinator.rollback_available(ports, session_id="cap_x")

    assert answer is False
    assert [
        r.levelname
        for r in event_records(
            caplog, "correction.crossover_v2_rollback_available_failed"
        )
    ] == ["WARNING"]

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
    ports = coordinator.RoundPorts(applied_boosts=seam)

    assert coordinator.applied_boosts(ports, session_id="cap_x") is expected, why

def _full_stage_2(monkeypatch) -> tuple[Any, list[int]]:
    attempts = _stub_restore_doors(monkeypatch)
    from tests.crossover_v2_fixtures import STAGE2_MAP
    conductor = _round_session(camilla_factory=lambda: SimpleNamespace(), index_phase_map=STAGE2_MAP)
    assert journey.PHASE_CLOUD_VERIFY in conductor.session_phases, (
        "this session must plan a post-apply cloud, or it is not the Full "
        "shape and grades at the other trigger"
    )
    return conductor, attempts

def _seed_full_round_state(*, previous_candidate: bool = True) -> dict[str, Any]:
    """:func:`_seed_round_state`, plus the tier the measuring session declared."""
    state = _seed_round_state(previous_candidate=previous_candidate)
    state["tier"] = "full"
    v2state.save_v2_state(state)
    return state

def _cloud_verify_indexes(conductor: Any) -> tuple[int, ...]:
    plan = conductor._journey.plan
    return tuple(
        i for i in range(1, 32)
        if plan.phase_for_index(i) == journey.PHASE_CLOUD_VERIFY
    )

def _walk_post_apply_cloud(conductor: Any, *, scale: float = 1.0) -> Any:
    verdict = None
    for attempt, index in enumerate(_cloud_verify_indexes(conductor), start=2):
        verdict = conductor._consume_cloud_position(
            journey.PHASE_CLOUD_VERIFY, index, attempt,
            _post_apply_analysis(conductor, scale=scale),
            SimpleNamespace(wav=b"fake-wav"),
        )
    assert verdict is not None, "a Full session has at least one cloud position"
    return verdict

def test_the_full_tier_grades_its_round_at_the_post_apply_cloud_close(
    monkeypatch, real_bundle,
):
    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
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
    assert verdict.payload["group_complete"] == journey.PHASE_CLOUD_VERIFY
    evaluation = conductor.round_evaluation
    assert evaluation is not None, "the cloud close did not grade the round"
    assert evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
    assert evaluation.adoption.row == ADOPTION_ROW_KEEP_ITERATING
    assert evaluation.adoption.reason == HEADROOM_REACHABLE
    assert evaluation.quality.status is QualityStatus.PASSED, (
        "the round still PASSED on quality — #2602 changed the stop "
        "condition, not the grade"
    )
    assert any(
        target.startswith("spec:") for target in evaluation.quality.evidence["targets"]
    )
    assert attempts == []
    identity = conductor.round_receipt_identity
    assert identity is not None
    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["round_id"] == _MINTED_CAPTURE_SESSION_ID
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.KEEP_FOR_ITERATION.value
    assert receipt["adoption"]["row"] == ADOPTION_ROW_KEEP_ITERATING
    assert receipt["adoption"]["reason"] == HEADROOM_REACHABLE
    core = {key: value for key, value in receipt.items() if key != "fingerprint"}
    assert receipt["fingerprint"] == json_fingerprint(core)
    assert identity["receipt_fingerprint"] == receipt["fingerprint"]
    spec = identity["spec"]
    assert isinstance(spec, dict)
    assert {"max_db", "max_hz", "graded_band_hz", "passed", "tilt"} <= set(spec)
    assert isinstance(spec["bands"], list) and spec["bands"]
    assert {"f_lo_hz", "f_hi_hz", "graded_lo_hz", "graded_hi_hz", "within_target",
            "tolerance_db", "max_deviation_db", "max_deviation_hz"} == set(
        spec["bands"][0]
    )
    assert {"step_db", "high_band_hz", "low_band_hz"} <= set(spec["tilt"])
    assert spec["max_db"] == evaluation.spec.evidence["max_db"]

def test_exactly_one_of_the_two_round_triggers_fires_in_any_session(
    monkeypatch, real_bundle,
):
    _seed_round_state()
    express, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(express, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    assert journey.PHASE_CLOUD_VERIFY not in express._journey.plan.phases
    assert _consume_verify(express, _post_apply_analysis(express)).accepted
    assert express.round_evaluation is not None, "Express grades at VERIFY"

    _seed_full_round_state()
    full, _full_attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(full, scale=1.5)

    assert journey.PHASE_CLOUD_VERIFY in full._journey.plan.phases
    assert _consume_verify(full, _post_apply_analysis(full)).accepted
    assert full.round_evaluation is None, "Full does not grade at VERIFY"

    _walk_post_apply_cloud(full)

    assert full.round_evaluation is not None, "Full grades at the cloud close"

def test_a_probe_finding_at_the_cloud_close_banks_advice(
    monkeypatch, real_bundle,
):

    _seed_full_round_state()
    conductor, attempts = _full_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

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

    assert verdict.accepted is True
    assert attempts == []
    assert conductor.round_evaluation is not None
    assert (
        conductor.round_evaluation.adoption.outcome is AdoptionOutcome.RESTORE
    )
    quality = conductor.round_evaluation.quality
    assert quality.status is QualityStatus.REGRESSED
    assert quality.evidence["probe_rollback_class"] == VERDICT_MODEL_ERROR

    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["adoption"]["outcome"] == AdoptionOutcome.RESTORE.value
    assert receipt["advice"]["delta_probe"]["verdict"] == VERDICT_MODEL_ERROR
    assert conductor.round_receipt_identity is not None
    assert conductor.round_receipt_identity["round_ordinal"] == 1

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
    """Non-finite objectives cannot count as measured progress."""

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1,
        "objectives": {"tilt_db": float("nan"), "ripple_db": float("inf")},
    }})

    assert position.previous_objectives is not None
    assert position.previous_objectives.tilt_db is None
    assert position.previous_objectives.ripple_db is None

def test_a_topology_change_between_rounds_resets_the_series(monkeypatch):

    monkeypatch.setattr(
        coordinator, "topology_config_fingerprint", lambda _topology: "new-topology",
    )

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 4,
        "objectives": {"tilt_db": 1.0, "ripple_db": 0.5},
        "topology_fingerprint": "old-topology",
    }})

    assert position.ordinal == 1
    assert position.previous_objectives is None

def test_a_matching_topology_fingerprint_keeps_the_series_going(monkeypatch):
    """The read-side guard does not fire when nothing about the topology moved."""

    monkeypatch.setattr(
        coordinator, "topology_config_fingerprint", lambda _topology: "same-topology",
    )

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 4,
        "objectives": {"tilt_db": 1.0, "ripple_db": 0.5},
        "topology_fingerprint": "same-topology",
    }})

    assert position.ordinal == 5

def test_a_receipt_with_no_topology_fingerprint_is_not_a_mismatch():

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 4,
        "objectives": {"tilt_db": 1.0, "ripple_db": 0.5},
    }})

    assert position.ordinal == 5

@pytest.mark.parametrize(
    ("case", "receipt"),
    [
        ("objectives absent", {"round_ordinal": 9}),
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

    position = coordinator.series_position_from_state({"round_receipt": receipt})

    assert position.ordinal == 10, case

def test_a_graded_round_banks_what_the_next_one_needs_to_read(
    monkeypatch, real_bundle,
):

    _seed_round_state()
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))

    identity = conductor.round_receipt_identity
    assert identity is not None
    assert identity["round_ordinal"] == 1, "the first graded round is round 1"
    assert "objectives" in identity, "the next round has nothing to compare to"

    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.ordinal == 2

def test_a_graded_round_banks_WHICH_EPOCH_its_ordinal_counts_in(
    monkeypatch, real_bundle,
):

    state = _seed_round_state()
    state[coordinator.ROUND_ORDINAL_EPOCH_STATE_KEY] = 2
    v2state.save_v2_state(state)
    conductor, _attempts = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=1.5)
    _install_applied_graph(monkeypatch, boosts=False)

    _consume_verify(conductor, _post_apply_analysis(conductor))

    identity = conductor.round_receipt_identity
    assert identity is not None
    assert identity["round_ordinal"] == 1
    assert identity["round_ordinal_epoch"] == 2

def test_the_status_block_forwards_the_receipt_to_the_screen():

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
    v2state.save_v2_state(state)

    block = v2status.crossover_v2_status_block()

    assert block is not None
    assert block["round_receipt"] == receipt, (
        "the screen cannot name a round the status block never forwards"
    )

_USABLE_ANALYSIS = SimpleNamespace(
    capture_integrity=SimpleNamespace(failed=(), not_evaluated=()),
    verify_tracking={"max_db_notch_excluded": 0.1, "n_bins": 10},
    summed_response=None,
    program_id="prog-1",
)

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
        rollback_available=rollback_available,
        applied_boosts=(lambda: boosts),
        entry_graph_fingerprint=(lambda: "graph-1"),
        publish_round_receipt=publish,
    )
    evidence = coordinator.RoundEvidence(
        session_id="cap_direct",

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
                "rollback_available": lambda: True,
            },
            AdoptionOutcome.RESTORE,
        ),
        (
            "no anchor to restore to",
            {"analysis": None},
            AdoptionOutcome.RECOVERY_REQUIRED,
        ),
    ],
    ids=["keep_for_iteration", "restore", "no_anchor"],
)
def test_every_adoption_outcome_banks_advice(case, kwargs, outcome):
    banked = []
    decision = _direct_round(publish=lambda receipt: banked.append(receipt) or "art",
                             **kwargs)

    assert decision.evaluation.adoption.outcome is outcome, case
    assert len(banked) == 1, case
    assert decision.receipt_identity is not None, case
    assert decision.receipt_identity["artifact_fingerprint"] == "art", case
    assert banked[0]["adoption"]["outcome"] == outcome.value, case
    assert decision.receipt_identity["adoption"] == outcome.value, case

def test_a_round_with_no_publishing_seam_still_remembers_where_it_sat():
    decision = _direct_round(publish=None)

    identity = decision.receipt_identity
    assert identity is not None
    assert identity["artifact_fingerprint"] == ""
    assert identity["receipt_fingerprint"] == ""
    assert identity["round_ordinal"] == 1
    assert coordinator.series_position_from_state(
        {"round_receipt": identity}
    ).ordinal == 2

def test_receipt_write_failure_preserves_continuity_across_more_rounds():
    """A missing artifact stays visible without resetting or ending iteration."""
    def _explode(_receipt):
        raise OSError("no space left on device")

    state = {}
    for ordinal in range(1, 9):
        position = coordinator.series_position_from_state(state)
        decision = _direct_round(
            publish=_explode,
            round_ordinal=position.ordinal,
            previous_objectives=position.previous_objectives,
            previous_trusted_floor_hz=position.previous_trusted_floor_hz,
        )
        assert position.ordinal == ordinal
        assert decision.evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION
        assert decision.evaluation.headroom.evidence["advisory"] is True
        assert decision.receipt_identity is not None
        assert decision.receipt_identity["artifact_fingerprint"] == ""
        assert decision.receipt_identity["receipt_fingerprint"] == ""
        state = {"round_receipt": decision.receipt_identity}

    assert coordinator.series_position_from_state(state).ordinal == 9

def test_the_receipt_banks_the_probes_band_resolved_realization_verbatim():
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
    banked = []

    decision = _direct_round(
        publish=lambda r: banked.append(r) or "art", delta_probe=probe,
    )

    assert len(banked) == 1, case
    assert "realization" not in banked[0]["round_measurements"], case
    assert decision.evaluation.adoption.outcome is AdoptionOutcome.KEEP_FOR_ITERATION

def test_the_receipt_banks_the_per_position_residual_role_labelled():
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
    banked = []

    _direct_round(publish=lambda r: banked.append(r) or "art")

    assert banked[0]["round_measurements"] == {}

RECEIPT_MAP_KEYS = {
    "round_axes": {"trust", "safety", "quality", "headroom"},
    "evidence_identities": {
        "session_id",
        "entry_baseline_artifact",
        "commanded_delta_present",
        "candidate_fingerprint",
        "tuning_graph_fingerprint",
    },
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
    receipt = _widest_receipt()

    for field, expected in RECEIPT_MAP_KEYS.items():
        added, missing = _key_drift(receipt[field], expected)
        assert not added, f"{field} grew {sorted(added)}. {_KEY_DRIFT_REMEDY}"
        assert not missing, f"{field} lost {sorted(missing)}. {_KEY_DRIFT_REMEDY}"

def test_a_kept_round_banks_its_blend_instruction_and_it_reads_back():

    decision = _direct_round(publish=lambda _r: "art",
                             analysis=_REGION_ANALYSIS)

    identity = decision.receipt_identity
    assert identity["adoption"].startswith("keep")
    assert isinstance(identity["blend"], Mapping)
    assert set(identity["blend"]) == {"filters", "residual_db"}

    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.previous_blend_correction is not None

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

    banked, incumbent = _BANKED_INSTRUCTION, _APPLIED_INCUMBENT
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile."
        "load_applied_baseline_profile_state",
        lambda: {"blend_correction": [dict(f) for f in incumbent]},
    )
    v2state.save_v2_state(_state_carrying_a_banked_instruction())

    prepared = v2host.prepare_v2_session(
        _inline_body(), status=_status(), run_async=None, camilla_factory=None,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)

    assert _hydrated_series_position(conductor).ordinal == 9
    assert conductor._candidate_blend_correction() == banked
    assert flow.CrossoverV2Session._applied_blend_correction(
        SimpleNamespace()
    ) == incumbent

_ABSENT = object()

def test_the_production_incumbent_reader_refuses_a_corrupt_profile(monkeypatch):

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
    good = [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5}]
    assert _profile({"blend_correction": good}) == tuple(good)
    assert _profile(None) is None

def test_no_instruction_makes_the_next_candidate_hold_the_applied_graph(
    monkeypatch,
):

    from jasper.active_speaker import crossover_v2_flow as flow

    prescribe = flow.CrossoverV2Session._candidate_blend_correction
    applied = ({"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0,
                "gain": -2.5},)
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile."
        "load_applied_baseline_profile_state",
        lambda: {"blend_correction": [dict(f) for f in applied]},
    )

    def _session(instruction):
        return SimpleNamespace(
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

    assert prescribe(_session(_ABSENT)) == applied
    assert prescribe(_session(None)) == applied
    assert prescribe(_session(())) == ()
    fresh = ({"biquad_type": "Peaking", "freq": 1200.0, "q": 2.0,
              "gain": -1.0},)
    assert prescribe(_session(fresh)) == fresh

def test_no_instruction_and_an_empty_instruction_are_different_answers():

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

    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1, "objectives": {"tilt_db": 0.0, "ripple_db": 0.0},
        "blend": blend,
    }})

    assert position.previous_blend_correction is None

def test_the_two_region_residuals_on_the_receipt_name_their_instruments():

    from jasper.active_speaker.crossover_v2 import blend_correction as bc
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
    decision = _direct_round(publish=lambda _r: "art", trusted_floor_hz=143.0)

    identity = decision.receipt_identity
    assert identity["trusted_floor_hz"] == 143.0
    position = coordinator.series_position_from_state({"round_receipt": identity})
    assert position.previous_trusted_floor_hz == 143.0

def test_a_receipt_from_before_the_floor_shipped_reads_back_as_unknown():
    position = coordinator.series_position_from_state({"round_receipt": {
        "round_ordinal": 1,
        "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
    }})

    assert position.previous_trusted_floor_hz is None

def test_the_position_role_reaches_the_combiners_own_input_struct():
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

    capture = spatial.cloud_position_capture(position)

    assert capture.position_id == "p_onax"
    assert capture.role == "onax"

def test_a_position_that_declares_no_role_carries_an_empty_one():
    import numpy as np

    position = SimpleNamespace(
        position_id="p0", role=None, sample_rate_hz=48_000,
        response=SimpleNamespace(
            freqs_hz=np.linspace(20.0, 20_000.0, 9),
            magnitude_db=np.zeros(9),
            complex_tf=np.ones(9, dtype=complex),
        ),
    )

    assert spatial.cloud_position_capture(position).role == ""

@pytest.mark.parametrize("probe_verdict", [None, *sorted(DELTA_PROBE_ADVISE_AGAINST_KEEP_VERDICTS)])
@pytest.mark.parametrize("previous_candidate", [True, False])
def test_round_advice_keeps_the_applied_graph(
    monkeypatch, real_bundle, probe_verdict, previous_candidate,
):
    _seed_round_state(previous_candidate=previous_candidate)
    conductor, apply_calls = _restoring_stage_2(monkeypatch)
    _install_entry_baseline(conductor, scale=0.4)
    _install_applied_graph(monkeypatch, boosts=False)
    before = _flow_seams(conductor).entry_graph_fingerprint()
    probe = None
    if probe_verdict is not None:

        commanded = _commanded_lift()
        probe = dataclasses.replace(
            classify_delta_probe(_GRID_HZ, commanded, commanded, band_hz=_band()),
            verdict=probe_verdict,
            safety_anchored=True,
            boost_over_declared_bound=True,
            realized_louder_than_commanded=True,
        )
        monkeypatch.setattr(delta_probe_run, "run_delta_probe", lambda *a, **k: probe)

    verdict = _consume_verify(conductor, _post_apply_analysis(conductor))

    assert verdict.accepted is True
    assert apply_calls == []
    assert _flow_seams(conductor).entry_graph_fingerprint() == before
    receipt = _round_receipt_json(real_bundle, _MINTED_CAPTURE_SESSION_ID)
    assert receipt["applied_graph_fingerprint"] == before
    assert receipt["adoption"]["outcome"] == (
        AdoptionOutcome.RESTORE.value if previous_candidate
        else AdoptionOutcome.RECOVERY_REQUIRED.value
    )
    advice = receipt["advice"]
    assert advice["adoption_row"] == conductor.round_evaluation.adoption.row
    assert advice["verdicts"] == conductor.round_evaluation.to_dict()["verdicts"]
    if probe is not None:
        assert advice["delta_probe"] == probe.to_dict()
        assert advice["delta_probe"]["advises_against_keep"] is True
    v2state.persist_conductor_state(conductor, failure_code=None)
    assert v2state.load_v2_state()["round_receipt"]["advice"] == advice

@pytest.mark.parametrize(
    "restore, failure, write_failed, expected_state",
    [
        ("exact_restored", None, False, "closed"),
        ("exact_restored", asyncio.CancelledError, False, "closed"),
        ("exact_restored", RuntimeError, False, "closed"),
        ("failed", RuntimeError, False, "open"),
        ("deferred", None, False, "open"),
        ("exact_restored", None, True, "open"),
    ],
)
async def test_prepared_run_closes_its_bundle_after_confirmed_cleanup(
    monkeypatch, tmp_path, restore, failure, write_failed, expected_state,
):
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore

    info = open_bundle(_topology(), calibration_id="", sessions_dir=tmp_path / "sessions")
    bundle = Path(info["bundle_dir"])
    store = CommissioningEvidenceStore.open(bundle, expected_session_id=info["session_id"])
    monkeypatch.setattr(v2evidence, "open_v2_evidence_store", lambda topology: (store, store.session_id))
    prepared = v2host.prepare_v2_session(
        _inline_body(), status=_status(), run_async=asyncio.run, camilla_factory=None,
    )
    cleanup_started, cleanup_finished = asyncio.Event(), asyncio.Event()
    error = failure() if failure else None
    artifacts = []

    async def worker(session):
        artifacts.append(store.publish_json_artifact("completed_take.json", {"accepted": True}))
        cleanup_started.set()
        await cleanup_finished.wait()
        v2state._persist_execution_result(session.session_id, volume_restore=restore)
        if error is not None:
            raise error

    _open_prepared(monkeypatch, prepared, run=worker)
    task = asyncio.create_task(prepared.run_and_consume(
        SimpleNamespace(session_id=_MINTED_CAPTURE_SESSION_ID),
    ))
    await wait_signalled(cleanup_started, "measurement cleanup started", producer=task)
    assert json.loads((bundle / "info.json").read_text())["state"] == "open"
    if write_failed:
        monkeypatch.setattr(v2host, "mark_state", lambda *args: None)
    cleanup_finished.set()
    if error is not None or write_failed:
        with pytest.raises(type(error) if error is not None else OSError) as caught:
            await task
        if error is not None:
            assert caught.value is error
    else:
        await task
    assert json.loads((bundle / "info.json").read_text())["state"] == expected_state
    assert store.reopen_json_artifact(artifacts[0])["accepted"] is True

@pytest.mark.parametrize(
    "open_stage_under_test",
    [pytest.param(_stage_1, id="session")],
)
def test_each_stage_binds_its_own_sessions_check_publisher(
    monkeypatch, open_stage_under_test,
):
    from jasper.audio_measurement.program_analysis import GainPlan

    store = _RecordingCheckStore()
    monkeypatch.setattr(
        v2evidence, "open_v2_evidence_store",
        lambda topology: (store, store.session_id),
    )
    conductor, _state = open_stage_under_test(monkeypatch)

    conductor._seams.records.check(
        GainPlan(
            gain_db={"woofer": -11.0}, predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
        {"bands": []},
    )

    payload = dict(store.published)[f"crossover_v2/{_MINTED_CAPTURE_SESSION_ID}/check.json"]
    assert payload["gain_plan_db"] == {"woofer": -11.0}

def test_persisted_verify_priors_carries_only_measurement_context(monkeypatch):
    _conductor, state = _stage_1(monkeypatch)

    assert set(state["verify_priors"]) == {
        "predicted_sum",
        "predicted_spec",
        "gate_window_ms",
        "pilot_transfer_reference",
        "commanded_delta",
        "declared_transfer",
        "entry_baseline",
        "proposal_fingerprint",
        "verify_measured",
        "alignment_objective",
    }

def test_the_measured_verify_curve_is_persisted_beside_the_priors(monkeypatch):
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    conductor._verify_tracking_curve = (freqs, predicted + error, predicted)

    v2state.persist_conductor_state(conductor, failure_code=None)
    record = (v2state.load_v2_state() or {})["verify_priors"]["verify_measured"]

    assert set(record) == {"freqs_hz", "measured_db", "predicted_db"}
    n = len(record["freqs_hz"])
    assert 0 < n <= v2durable.MAX_PERSISTED_SUM_POINTS
    assert len(record["measured_db"]) == n
    assert len(record["predicted_db"]) == n
    persisted_error = np.asarray(record["measured_db"]) - np.asarray(
        record["predicted_db"]
    )
    assert float(np.max(persisted_error)) == pytest.approx(6.0, abs=1e-9)
    assert float(np.min(persisted_error)) == pytest.approx(0.0, abs=1e-9)

def test_a_session_with_no_verify_capture_persists_no_measured_curve(monkeypatch):
    conductor, _state = _stage_1(monkeypatch)
    v2state.persist_conductor_state(conductor, failure_code=None)
    priors = (v2state.load_v2_state() or {})["verify_priors"]
    assert priors["verify_measured"] is None
    assert v2durable.verify_measured_curve_from_state({"verify_priors": priors}) is None

def test_a_verdict_can_be_re_graded_from_the_store_alone(monkeypatch):
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    _install_commanded_delta(conductor, (freqs, commanded))
    live = _delta_probe_given_a_tracking_curve(
        conductor, (freqs, predicted + error, predicted),
    )
    assert live is not None
    assert live.advises_against_keep is True

    v2state.persist_conductor_state(conductor, failure_code=None)
    state = v2state.load_v2_state() or {}

    stored_freqs, stored_measured, stored_predicted = (
        v2durable.verify_measured_curve_from_state(state)
    )
    assert stored_freqs.size < freqs.size
    stored_commanded = v2durable.commanded_delta_prior_from_state(state)
    commanded_on_grid = np.interp(
        stored_freqs, stored_commanded[0], stored_commanded[1]
    )
    regraded = delta_probe.classify_delta_probe(
        stored_freqs,
        (stored_measured - stored_predicted) + commanded_on_grid,
        commanded_on_grid,
        band_hz=live.requested_band_hz,
        expected_offset_db=live.expected_offset_db,
    )

    assert regraded.verdict == live.verdict
    assert regraded.reason == live.reason
    assert regraded.advises_against_keep == live.advises_against_keep
    assert regraded.max_error_db == pytest.approx(live.max_error_db, abs=0.05)
    assert regraded.exceedance_octaves == pytest.approx(
        live.exceedance_octaves, abs=0.05
    )

def test_an_anchored_verdict_is_re_gradable_from_the_store_alone(monkeypatch):
    import numpy as np

    from jasper.active_speaker.crossover_v2.contracts import ResponseCurve
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    conductor, _state = _stage_1(monkeypatch)
    freqs, _flat_commanded, error = _regradable_fixture()
    commanded = np.where((freqs >= 300.0) & (freqs <= 8_000.0), 6.0, 0.0)
    predicted = np.zeros_like(freqs)
    _install_commanded_delta(conductor, (freqs, commanded))

    anchor_db = -2.5
    conductor._measure_entry_baseline = EntryBaseline(
        program_id=conductor.program_for_phase(
            journey.PHASE_VERIFY
        ).program_id,
        reference_mark="design_axis_mark",
        curve=ResponseCurve(freqs, (predicted - commanded) + anchor_db),
        excluded=tuple(False for _ in freqs),
        graph_fingerprint="fingerprint",
        captured_at="2026-08-15T00:00:00Z",
    )

    live = _delta_probe_given_a_tracking_curve(
        conductor, (freqs, predicted + error, predicted),
    )
    assert live is not None
    assert live.entry_anchor_offset_db == pytest.approx(anchor_db, abs=1e-6)

    v2state.persist_conductor_state(conductor, failure_code=None)
    state = v2state.load_v2_state() or {}

    stored_freqs, stored_measured, stored_predicted = (
        v2durable.verify_measured_curve_from_state(state)
    )
    assert stored_freqs.size < freqs.size  # the decimation really happened
    stored_commanded = v2durable.commanded_delta_prior_from_state(state)
    commanded_on_grid = np.interp(
        stored_freqs, stored_commanded[0], stored_commanded[1]
    )
    stored_entry = v2durable.entry_baseline_prior_from_state(state)
    assert stored_entry is not None
    entry_on_grid = np.interp(
        stored_freqs,
        np.asarray(stored_entry.curve.hz, dtype=float),
        np.asarray(stored_entry.curve.db, dtype=float),
    )
    regraded = delta_probe.classify_delta_probe(
        stored_freqs,
        (stored_measured - stored_predicted) + commanded_on_grid,
        commanded_on_grid,
        band_hz=live.requested_band_hz,
        expected_offset_db=live.expected_offset_db,
        entry_delta_db=(entry_on_grid - stored_predicted) + commanded_on_grid,
    )

    assert regraded.verdict == live.verdict
    assert regraded.reason == live.reason
    assert regraded.advises_against_keep == live.advises_against_keep
    assert regraded.entry_anchor_offset_db == pytest.approx(
        live.entry_anchor_offset_db, abs=0.05,
    )
    assert regraded.residual_offset_db == pytest.approx(
        live.residual_offset_db, abs=0.05,
    )
    assert regraded.quiet_probe_coverage == pytest.approx(
        live.quiet_probe_coverage, abs=0.05,
    )

def test_a_truncated_measured_record_reads_as_absent_not_as_a_curve(monkeypatch):
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, _commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    conductor._verify_tracking_curve = (freqs, predicted + error, predicted)
    v2state.persist_conductor_state(conductor, failure_code=None)

    state = v2state.load_v2_state() or {}
    assert v2durable.verify_measured_curve_from_state(state) is not None
    state["verify_priors"]["verify_measured"]["measured_db"] = (
        state["verify_priors"]["verify_measured"]["measured_db"][:-3]
    )
    assert v2durable.verify_measured_curve_from_state(state) is None

def test_only_stage_1_binds_the_findings_publisher(monkeypatch):
    stage_1_conductor, _state = _stage_1(monkeypatch)

    assert callable(_flow_seams(stage_1_conductor).records.findings)

def test_persisted_payload_top_level_keys_are_the_whole_bridge(monkeypatch):
    _conductor, stage_1_state = _stage_1(monkeypatch)

    assert set(stage_1_state) == _PERSISTED_TOP_LEVEL_KEYS

def test_stage_1_declares_itself_too(monkeypatch, caplog):
    """Both stages declare; the measuring one needs nothing handed to it."""
    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        _conductor, _state = _stage_1(monkeypatch)

    declared = [
        record.getMessage() for record in caplog.records
        if "event=correction.crossover_v2_stage_capabilities" in record.getMessage()
    ]
    assert len(declared) == 1
    assert "stage=measure" in declared[0]
    assert "provides=findings requires=" in declared[0]
    assert 'requires="" missing=""' in declared[0]

@pytest.mark.parametrize(
    "n_bins",
    [400, 512, 513, 1023, 1536, 4096],
)
def test_the_commanded_delta_persists_on_the_same_grid_as_the_predicted_sum(n_bins):
    import numpy as np

    freqs = np.linspace(20.0, 24000.0, n_bins)
    curve = np.sin(np.log10(freqs) * 7.0)

    reduced_delta = v2durable._decimate_delta((freqs, curve))
    reduced_sum = v2durable._decimate_sum((freqs, curve))

    assert len(reduced_delta["freqs_hz"]) <= v2durable.MAX_PERSISTED_SUM_POINTS
    assert reduced_delta["freqs_hz"] == reduced_sum["freqs_hz"]

def test_the_commanded_delta_is_block_averaged_in_db_not_in_power():
    import numpy as np

    freqs = np.linspace(20.0, 24000.0, 2 * v2durable.MAX_PERSISTED_SUM_POINTS)
    swing = np.tile([6.0, -6.0], v2durable.MAX_PERSISTED_SUM_POINTS)

    reduced_delta = v2durable._decimate_delta((freqs, swing))
    reduced_sum = v2durable._decimate_sum((freqs, swing))

    assert reduced_delta["delta_db"] == pytest.approx([0.0] * len(swing[::2]))
    assert reduced_sum["magnitude_db"][0] > 1.0

@pytest.mark.parametrize("phase", [PHASE_CHECK, PHASE_VERIFY])
def test_prepared_flow_prices_clip_retries_from_the_played_program(monkeypatch, phase):
    conductor = _session_from_real_open(monkeypatch, EngineFakeSeams())["conductor"]
    program = conductor.program_for_phase(phase)
    analysis_factory, assess = {
        PHASE_CHECK: (_check_analysis, conductor._check_verdict),
        PHASE_VERIFY: (_verify_analysis, conductor._verify_verdict),
    }[phase]
    analysis = analysis_factory(program)
    verdict = assess(replace(analysis, locations=tuple(
        replace(location, clipped=True) for location in analysis.locations
    )))
    assert verdict.code == "clipped" and not verdict.accepted
    assert verdict.charge == "speaker" and verdict.next == "retake_quieter"
    assert type(verdict.next_gain_db) is float
    played_gain = max(segment.gain_db for segment in program.segments if segment.kind in STIMULUS_KINDS)
    assert verdict.next_gain_db == pytest.approx(played_gain - CLIP_RETRY_BACKOFF_DB)

def test_the_real_preparer_builds_a_session_over_the_five_seams(monkeypatch):
    from tests.engine_twin import FakeSeams

    fakes = FakeSeams()
    captured = _session_from_real_open(monkeypatch, fakes)
    session = captured["tuning"]

    assert session.session_id == _MINTED_CAPTURE_SESSION_ID
    assert session.measurement_level_db == captured["conductor"]._session_volume_db
    assert session.measurement_level_db < 0.0, "the hearing clamp is never relaxed"
    assert session.seams.graph is fakes.graph
    assert session.seams.records is fakes.records
    assert not session.is_open, "opening is the run's, not the preparer's"

async def test_a_session_from_the_real_preparer_drives_the_measure_verb(monkeypatch):
    from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_BASELINE
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from tests.engine_twin import FakeSeams

    fakes = FakeSeams()
    session = _session_from_real_open(monkeypatch, fakes)["tuning"]

    await session.open()
    fakes.volume.proven_db = session.measurement_level_db
    measured = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
    await session.close()

    assert measured.record_ids == session.banked_record_ids
    assert measured.record_ids != ()
    assert fakes.graph.installs == 2 and fakes.graph.restores == 1
    assert not fakes.volume.held, "the claim went back"
    assert not session.is_open
