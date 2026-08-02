# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The per-speaker model-error store: round-trip, bounds, and atomic writes."""

from __future__ import annotations

import json
import os
import stat

import pytest

from jasper.active_speaker.attempts_loop import (
    FLOOR_BASIS_MEASURED,
    FLOOR_BASIS_POLICY,
    FloorStats,
)
from jasper.active_speaker.model_error_store import (
    DEFAULT_STATE_PATH,
    MAX_MODEL_ERROR_RECORDS,
    MODEL_ERROR_STATE_KIND,
    SCHEMA_VERSION,
    STATE_PATH_ENV,
    adopt_floor,
    load_state,
    model_error_state_path,
    record_model_error,
    stored_floor,
)

METRIC = "max_db_notch_excluded"


def _floor() -> FloorStats:
    return FloorStats.from_repeat_study(
        metric=METRIC,
        median_db=0.05183,
        p95_db=0.08508,
        source="captures/repeat-floor-20260731",
        measured_at="2026-07-31",
    )


def test_path_resolution_prefers_argument_then_env_then_default(monkeypatch, tmp_path):
    monkeypatch.delenv(STATE_PATH_ENV, raising=False)
    assert model_error_state_path() == DEFAULT_STATE_PATH
    monkeypatch.setenv(STATE_PATH_ENV, str(tmp_path / "from-env.json"))
    assert model_error_state_path() == tmp_path / "from-env.json"
    explicit = tmp_path / "explicit.json"
    assert model_error_state_path(explicit) == explicit


def test_default_path_is_under_var_lib_jasper():
    assert str(DEFAULT_STATE_PATH) == (
        "/var/lib/jasper/active_speaker_model_error.json"
    )


def test_missing_file_reads_as_an_empty_store(tmp_path):
    state = load_state(tmp_path / "nothing.json")
    assert state["kind"] == MODEL_ERROR_STATE_KIND
    assert state["artifact_schema_version"] == SCHEMA_VERSION
    assert state["floor"] is None
    assert state["model_error"] == []


def test_floor_round_trips_through_the_store(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    restored = stored_floor(path)
    assert restored is not None
    assert restored.metric == METRIC
    assert restored.basis == FLOOR_BASIS_MEASURED
    assert restored.claim_floor_db == pytest.approx(0.17016)
    assert restored.p95_db == pytest.approx(0.08508)
    assert restored.measured_at == "2026-07-31"
    assert restored.source == "captures/repeat-floor-20260731"


def test_a_policy_bar_floor_round_trips_without_growing_a_fake_p95(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(
        FloorStats.from_policy_bar(
            metric="linearization_residual_rms_db",
            claim_floor_db=0.5,
            source="a shipped constant",
        ),
        path=path,
    )
    restored = stored_floor(path)
    assert restored is not None
    assert restored.basis == FLOOR_BASIS_POLICY
    assert restored.p95_db is None
    assert restored.median_db is None


def test_adopting_a_floor_replaces_rather_than_merges(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    adopt_floor(
        FloorStats.from_policy_bar(
            metric="other_metric", claim_floor_db=0.5, source="policy",
        ),
        path=path,
    )
    restored = stored_floor(path)
    assert restored is not None
    assert restored.metric == "other_metric"
    assert restored.p95_db is None


def test_adopting_a_floor_preserves_existing_model_error_history(tmp_path):
    path = tmp_path / "store.json"
    record_model_error(
        attempt_id="a1", metric=METRIC, predicted_db=1.0, realized_db=1.4,
        path=path,
    )
    adopt_floor(_floor(), path=path)
    assert len(load_state(path)["model_error"]) == 1


def test_model_error_sign_is_realized_minus_predicted(tmp_path):
    path = tmp_path / "store.json"
    state = record_model_error(
        attempt_id="a1", metric=METRIC,
        predicted_db=1.0, realized_db=1.4, path=path,
    )
    record = state["model_error"][0]
    # Positive means the hardware came out WORSE than the model promised.
    assert record["error_db"] == pytest.approx(0.4)
    state = record_model_error(
        attempt_id="a2", metric=METRIC,
        predicted_db=1.4, realized_db=1.0, path=path,
    )
    assert state["model_error"][0]["error_db"] == pytest.approx(-0.4)


def test_model_error_history_is_newest_first_and_bounded(tmp_path):
    path = tmp_path / "store.json"
    for index in range(MAX_MODEL_ERROR_RECORDS + 8):
        record_model_error(
            attempt_id=f"a{index}", metric=METRIC,
            predicted_db=0.0, realized_db=float(index), path=path,
        )
    records = load_state(path)["model_error"]
    assert len(records) == MAX_MODEL_ERROR_RECORDS
    newest = MAX_MODEL_ERROR_RECORDS + 7
    assert records[0]["attempt_id"] == f"a{newest}"
    assert records[-1]["attempt_id"] == f"a{newest - MAX_MODEL_ERROR_RECORDS + 1}"


def test_over_long_history_on_disk_is_trimmed_on_read(tmp_path):
    path = tmp_path / "store.json"
    path.write_text(json.dumps({
        "kind": MODEL_ERROR_STATE_KIND,
        "model_error": [{"attempt_id": f"a{i}"} for i in range(200)],
    }), encoding="utf-8")
    assert len(load_state(path)["model_error"]) == MAX_MODEL_ERROR_RECORDS


def test_context_rides_along_verbatim(tmp_path):
    path = tmp_path / "store.json"
    state = record_model_error(
        attempt_id="a1", metric=METRIC, predicted_db=1.0, realized_db=1.0,
        path=path, context={"build_sha": "abc123", "band_hz": [1000.0, 4000.0]},
    )
    assert state["model_error"][0]["context"] == {
        "build_sha": "abc123", "band_hz": [1000.0, 4000.0],
    }


def test_a_corrupt_file_reads_as_empty_rather_than_half_trusted(tmp_path, caplog):
    path = tmp_path / "store.json"
    path.write_text("{not json at all", encoding="utf-8")
    state = load_state(path)
    assert state["floor"] is None
    assert state["model_error"] == []
    assert "model_error_store_unreadable" in caplog.text


@pytest.mark.parametrize("floor_payload", [
    {"metric": "", "claim_floor_db": 0.17, "basis": FLOOR_BASIS_MEASURED},
    {"metric": "m", "claim_floor_db": 0.0, "basis": FLOOR_BASIS_MEASURED},
    {"metric": "m", "claim_floor_db": -1.0, "basis": FLOOR_BASIS_MEASURED},
    {"metric": "m", "claim_floor_db": "0.17", "basis": FLOOR_BASIS_MEASURED},
    {"metric": "m", "claim_floor_db": True, "basis": FLOOR_BASIS_MEASURED},
    {"metric": "m", "claim_floor_db": 0.17, "basis": "invented"},
    {"metric": "m", "claim_floor_db": 0.17},
])
def test_an_unusable_stored_floor_is_dropped_not_half_trusted(
    tmp_path, floor_payload,
):
    """No floor means the loop refuses to grade, which is the safe failure."""

    path = tmp_path / "store.json"
    path.write_text(
        json.dumps({"kind": MODEL_ERROR_STATE_KIND, "floor": floor_payload}),
        encoding="utf-8",
    )
    assert load_state(path)["floor"] is None
    assert stored_floor(path) is None


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    record_model_error(
        attempt_id="a1", metric=METRIC, predicted_db=1.0, realized_db=1.1,
        path=path,
    )
    assert sorted(item.name for item in tmp_path.iterdir()) == ["store.json"]
    json.loads(path.read_text(encoding="utf-8"))


def test_the_store_is_group_readable_but_not_world_readable(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o640


def test_parent_directories_are_created_on_first_write(tmp_path):
    path = tmp_path / "deep" / "nested" / "store.json"
    adopt_floor(_floor(), path=path)
    assert path.exists()


def test_updated_at_and_state_path_are_recorded_on_write(tmp_path):
    path = tmp_path / "store.json"
    state = adopt_floor(_floor(), path=path)
    assert state["state_path"] == str(path)
    assert state["updated_at"] is not None
    assert state["updated_at"].endswith("Z")
