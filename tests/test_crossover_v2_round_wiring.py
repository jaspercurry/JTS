# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


from jasper.active_speaker.crossover_v2 import durable_state as v2durable
import asyncio
from tests._async_wait import wait_signalled
from tests.crossover_v2_fixtures import (
    _MINTED_CAPTURE_SESSION_ID,
    _PERSISTED_TOP_LEVEL_KEYS,
    _RecordingCheckStore,
    _inline_body,
    _open_prepared,
    _regradable_fixture,
    _session_from_real_open,
    _stage_1,
    _status,
    _topology,
)

from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state


import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

from jasper.active_speaker.crossover_v2 import coordinator
from jasper.active_speaker.crossover_v2.contracts import (
    AdoptionOutcome,
)
from jasper.active_speaker.crossover_v2.verification import (
    Verdict,
)
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status

from tests._log_events import event_records

from tests.crossover_v2_round_harness import (
    _seed_round_state,
)

from tests.crossover_v2_fixtures import (
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)


pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")


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
    conductor, state = _stage_1(monkeypatch)
    assert state["round_receipt"] is None
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


def test_a_truncated_measured_record_reads_as_absent_not_as_a_curve(monkeypatch):
    import numpy as np

    from tests.crossover_v2_banked_round import _decimate_verify_measured
    freqs, _commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    state = {"verify_priors": {"verify_measured": _decimate_verify_measured((freqs, predicted + error, predicted))}}
    assert v2durable.verify_measured_curve_from_state(state) is not None
    state["verify_priors"]["verify_measured"]["measured_db"] = (
        state["verify_priors"]["verify_measured"]["measured_db"][:-3]
    )
    assert v2durable.verify_measured_curve_from_state(state) is None


def test_persisted_payload_top_level_keys_are_the_whole_bridge(monkeypatch):
    conductor, stage_1_state = _stage_1(monkeypatch)

    assert set(stage_1_state) == _PERSISTED_TOP_LEVEL_KEYS
    built = v2durable.build_conductor_state(conductor, {}, failure_code=None)
    assert set(built.state) == _PERSISTED_TOP_LEVEL_KEYS - {
        "kind",
        "schema_version",
        "updated_at",
    }


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
    assert 'provides="" requires="" missing=""' in declared[0]


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
