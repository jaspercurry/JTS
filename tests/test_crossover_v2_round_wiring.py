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

import pytest

from jasper.active_speaker.crossover_v2 import coordinator
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status


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


@pytest.mark.parametrize(
    ("case", "raw", "ordinal"),
    [
        ("no state at all", {}, 1),
        ("no receipt yet", {"session_id": "s"}, 1),
        ("receipt is not a mapping", {"round_receipt": "corrupt"}, 1),
        (
            "a receipt written before #2602 knew about ordinals",
            {"round_receipt": {"row": "row1_trusted_safe_passed"}},
            1,
        ),
        (
            "round 1 banked its objectives",
            {"round_receipt": {
                "round_ordinal": 1,
                "objectives": {"tilt_db": 2.37, "ripple_db": 0.9},
            }},
            2,
        ),
        (
            "an ordinal with no objectives beside it",
            {"round_receipt": {"round_ordinal": 2}},
            3,
        ),
        (
            "a bool is not an ordinal",
            {"round_receipt": {"round_ordinal": True}},
            1,
        ),
        (
            "a nonsense ordinal",
            {"round_receipt": {"round_ordinal": 0}},
            1,
        ),
    ],
    ids=[
        "no_state", "no_receipt", "corrupt_receipt", "pre_2602_receipt",
        "after_round_one", "ordinal_without_objectives", "bool_ordinal",
        "zero_ordinal",
    ],
)
def test_the_series_position_reader(case, raw, ordinal):

    position = coordinator.series_position_from_state(raw)

    assert position.ordinal == ordinal, case


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
