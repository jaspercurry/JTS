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
    FLOOR_SCOPE_ACROSS_SITTINGS,
    FLOOR_SCOPE_WITHIN_SITTING,
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
    store_snapshot,
)
from tests._log_events import event_records

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
    restored = store_snapshot(path).floor
    assert restored is not None
    assert restored.metric == METRIC
    assert restored.basis == FLOOR_BASIS_MEASURED
    assert restored.claim_floor_db == pytest.approx(0.17016)
    assert restored.p95_db == pytest.approx(0.08508)
    assert restored.measured_at == "2026-07-31"
    assert restored.source == "captures/repeat-floor-20260731"
    # #2081: WHICH comparisons the floor licenses rides with the number. A
    # scope lost in persistence would silently re-widen an adopted narrow floor
    # on the next boot, which is the failure this store exists to prevent.
    assert restored.scope == FLOOR_SCOPE_WITHIN_SITTING


def test_a_floor_adopted_across_sittings_does_not_narrow_on_reload(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(
        FloorStats.from_repeat_study(
            metric=METRIC, median_db=0.05183, p95_db=0.08508,
            source="a study that re-placed the mic between repeats",
            measured_at="2026-08-14", scope=FLOOR_SCOPE_ACROSS_SITTINGS,
        ),
        path=path,
    )
    restored = store_snapshot(path).floor
    assert restored is not None
    assert restored.scope == FLOOR_SCOPE_ACROSS_SITTINGS


def test_a_floor_written_before_2081_reloads_as_the_narrow_scope(tmp_path):
    """No stored floor predating #2081 has a scope key, and every one of them
    came from the fixed-mic study — so defaulting to the narrow value is the
    truth about them, not a guess, and it is also the fail-closed direction."""
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    raw = json.loads(path.read_text())
    del raw["floor"]["scope"]
    path.write_text(json.dumps(raw))

    restored = store_snapshot(path).floor
    assert restored is not None
    assert restored.scope == FLOOR_SCOPE_WITHIN_SITTING


def test_a_floor_whose_scope_is_unreadable_is_dropped_rather_than_guessed(
    tmp_path,
):
    """Same ruling the basis check already makes: a floor nobody can say what
    it licenses is worse than no floor, because the alternative to a floor is
    refusing to grade — which claims nothing."""
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    raw = json.loads(path.read_text())
    raw["floor"]["scope"] = "whenever_you_like"
    path.write_text(json.dumps(raw))

    assert store_snapshot(path).floor is None


def test_a_policy_bar_floor_round_trips_without_growing_a_fake_p95(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(
        FloorStats.from_policy_bar(
            metric="linearization_residual_rms_db",
            claim_floor_db=0.5,
            source="a shipped constant",
            scope=FLOOR_SCOPE_ACROSS_SITTINGS,
        ),
        path=path,
    )
    restored = store_snapshot(path).floor
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
            scope=FLOOR_SCOPE_ACROSS_SITTINGS,
        ),
        path=path,
    )
    restored = store_snapshot(path).floor
    assert restored is not None
    assert restored.metric == "other_metric"
    assert restored.p95_db is None


def _seed_history(path, *attempt_ids):
    path.write_text(json.dumps({
        "kind": MODEL_ERROR_STATE_KIND,
        "model_error": [{"attempt_id": attempt_id} for attempt_id in attempt_ids],
    }), encoding="utf-8")


def test_adopting_a_floor_preserves_existing_model_error_history(tmp_path):
    path = tmp_path / "store.json"
    _seed_history(path, "a1")
    adopt_floor(_floor(), path=path)
    assert len(load_state(path)["model_error"]) == 1


def test_store_snapshot_reads_floor_and_count_as_one_owned_view(tmp_path):
    path = tmp_path / "store.json"
    _seed_history(path, "candidate-a")
    adopt_floor(_floor(), path=path)

    snapshot = store_snapshot(path)

    assert snapshot.floor is not None
    assert snapshot.floor.metric == METRIC
    assert snapshot.model_error_count == 1


def test_over_long_history_on_disk_is_trimmed_on_read(tmp_path):
    path = tmp_path / "store.json"
    path.write_text(json.dumps({
        "kind": MODEL_ERROR_STATE_KIND,
        "model_error": [{"attempt_id": f"a{i}"} for i in range(200)],
    }), encoding="utf-8")
    assert len(load_state(path)["model_error"]) == MAX_MODEL_ERROR_RECORDS


def test_a_corrupt_file_reads_as_empty_rather_than_half_trusted(tmp_path, caplog):
    path = tmp_path / "store.json"
    path.write_text("{not json at all", encoding="utf-8")
    state = load_state(path)
    assert state["floor"] is None
    assert state["model_error"] == []
    assert event_records(caplog, "active_speaker.model_error_store_unreadable")


def test_a_bad_byte_reads_as_empty_too_not_a_vanished_status_block(tmp_path, caplog):
    """#2082 item 4: `UnicodeDecodeError` from a torn write is not an
    `OSError` or a `json.JSONDecodeError` -- uncaught, it would propagate
    past every outer fail-soft catch in the v2 status block and take the
    whole block down instead of costing only this store's history."""
    path = tmp_path / "store.json"
    path.write_bytes(b"floor: " + bytes([0xFF, 0xFE]) + b" not valid utf-8")
    state = load_state(path)
    assert state["floor"] is None
    assert state["model_error"] == []
    assert event_records(caplog, "active_speaker.model_error_store_unreadable")


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
    assert store_snapshot(path).floor is None


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "store.json", "store.json.lock",
    ]
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
