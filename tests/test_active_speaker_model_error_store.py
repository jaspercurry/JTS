# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The per-speaker model-error store: round-trip, bounds, and atomic writes."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading

import pytest

from jasper.active_speaker import model_error_store as store
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
    ModelErrorConflictError,
    SCHEMA_VERSION,
    STATE_PATH_ENV,
    adopt_floor,
    load_state,
    model_error_state_path,
    record_model_error,
    store_snapshot,
    stored_floor,
)

METRIC = "max_db_notch_excluded"
SPEAKER = "speaker-a"


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
    restored = stored_floor(path)
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

    restored = stored_floor(path)
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

    assert stored_floor(path) is None


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
            scope=FLOOR_SCOPE_ACROSS_SITTINGS,
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
        speaker_id=SPEAKER, attempt_id="a1", metric=METRIC,
        predicted_db=1.0, realized_db=1.4,
        path=path,
    )
    adopt_floor(_floor(), path=path)
    assert len(load_state(path)["model_error"]) == 1


def test_model_error_sign_is_realized_minus_predicted(tmp_path, caplog):
    path = tmp_path / "store.json"
    with caplog.at_level(logging.INFO):
        state = record_model_error(
            speaker_id=SPEAKER, attempt_id="a1", metric=METRIC,
            predicted_db=1.0, realized_db=1.4, path=path,
        )
    record = state["model_error"][0]
    # Positive means the hardware came out WORSE than the model promised.
    assert record["error_db"] == pytest.approx(0.4)
    assert "event=active_speaker.model_error_recorded" in caplog.text
    assert f"speaker_id={SPEAKER}" in caplog.text
    state = record_model_error(
        speaker_id=SPEAKER, attempt_id="a2", metric=METRIC,
        predicted_db=1.4, realized_db=1.0, path=path,
    )
    assert state["model_error"][0]["error_db"] == pytest.approx(-0.4)


def test_model_error_write_is_idempotent_by_stable_observation_identity(
    tmp_path, caplog,
):
    path = tmp_path / "store.json"
    observation = {
        "speaker_id": SPEAKER,
        "attempt_id": "candidate-a",
        "metric": METRIC,
        "predicted_db": 0.0,
        "realized_db": 0.9,
        "path": path,
    }

    with caplog.at_level(logging.INFO):
        first = record_model_error(**observation)
        repeated = record_model_error(**observation)

    assert len(first["model_error"]) == 1
    assert len(repeated["model_error"]) == 1
    assert repeated["model_error"][0]["speaker_id"] == SPEAKER
    assert "event=active_speaker.model_error_duplicate_ignored" in caplog.text
    assert f"speaker_id={SPEAKER}" in caplog.text


def test_model_error_identity_conflict_refuses_to_replace_the_first_observation(
    tmp_path,
):
    path = tmp_path / "store.json"
    record_model_error(
        speaker_id=SPEAKER,
        attempt_id="candidate-a",
        metric=METRIC,
        predicted_db=0.0,
        realized_db=0.9,
        path=path,
    )

    with pytest.raises(ModelErrorConflictError):
        record_model_error(
            speaker_id=SPEAKER,
            attempt_id="candidate-a",
            metric=METRIC,
            predicted_db=0.0,
            realized_db=0.7,
            path=path,
        )

    records = load_state(path)["model_error"]
    assert len(records) == 1
    assert records[0]["realized_db"] == pytest.approx(0.9)


def test_store_snapshot_reads_floor_and_count_as_one_owned_view(tmp_path):
    path = tmp_path / "store.json"
    adopt_floor(_floor(), path=path)
    record_model_error(
        speaker_id=SPEAKER,
        attempt_id="candidate-a",
        metric=METRIC,
        predicted_db=0.0,
        realized_db=0.9,
        path=path,
    )

    snapshot = store_snapshot(path)

    assert snapshot.floor is not None
    assert snapshot.floor.metric == METRIC
    assert snapshot.model_error_count == 1


def test_floor_adoption_and_live_record_serialize_their_whole_update(
    tmp_path, monkeypatch,
):
    """Atomic rename is not enough: both RMW paths share one store lock."""

    path = tmp_path / "store.json"
    floor_write_ready = threading.Event()
    release_floor_write = threading.Event()
    record_write_seen = threading.Event()
    real_write = store._write_state

    def staged_write(write_path, state):
        if state["floor"] is not None and not state["model_error"]:
            floor_write_ready.set()
            assert release_floor_write.wait(2.0)
        if state["model_error"]:
            record_write_seen.set()
        real_write(write_path, state)

    monkeypatch.setattr(store, "_write_state", staged_write)
    errors: list[Exception] = []

    def capture_error(action):
        try:
            action()
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            errors.append(exc)

    floor_thread = threading.Thread(
        target=capture_error,
        args=(lambda: adopt_floor(_floor(), path=path),),
        daemon=True,
    )
    floor_thread.start()
    assert floor_write_ready.wait(2.0)

    record_thread = threading.Thread(
        target=capture_error,
        args=(lambda: record_model_error(
            speaker_id=SPEAKER,
            attempt_id="candidate-a",
            metric=METRIC,
            predicted_db=0.0,
            realized_db=0.9,
            path=path,
        ),),
        daemon=True,
    )
    record_thread.start()
    record_entered_while_floor_was_paused = record_write_seen.wait(0.2)
    release_floor_write.set()
    floor_thread.join(2.0)
    record_thread.join(2.0)

    assert not floor_thread.is_alive()
    assert not record_thread.is_alive()
    assert errors == []
    assert record_entered_while_floor_was_paused is False
    state = load_state(path)
    assert state["floor"] is not None
    assert [item["attempt_id"] for item in state["model_error"]] == [
        "candidate-a",
    ]


def test_conflicting_record_writers_serialize_identity_check_and_write(
    tmp_path, monkeypatch,
):
    path = tmp_path / "store.json"
    first_write_ready = threading.Event()
    release_first_write = threading.Event()
    second_finished = threading.Event()
    real_write = store._write_state

    def staged_write(write_path, state):
        record = state["model_error"][0]
        if record["realized_db"] == 0.9:
            first_write_ready.set()
            assert release_first_write.wait(2.0)
        real_write(write_path, state)

    monkeypatch.setattr(store, "_write_state", staged_write)
    first_errors: list[Exception] = []
    second_errors: list[Exception] = []

    def write(realized_db, errors, finished=None):
        try:
            record_model_error(
                speaker_id=SPEAKER,
                attempt_id="candidate-a",
                metric=METRIC,
                predicted_db=0.0,
                realized_db=realized_db,
                path=path,
            )
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    first_thread = threading.Thread(
        target=write, args=(0.9, first_errors), daemon=True,
    )
    first_thread.start()
    assert first_write_ready.wait(2.0)
    second_thread = threading.Thread(
        target=write,
        args=(0.7, second_errors, second_finished),
        daemon=True,
    )
    second_thread.start()
    second_completed_while_first_was_paused = second_finished.wait(0.2)
    release_first_write.set()
    first_thread.join(2.0)
    second_thread.join(2.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_errors == []
    assert second_completed_while_first_was_paused is False
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], ModelErrorConflictError)
    records = load_state(path)["model_error"]
    assert len(records) == 1
    assert records[0]["realized_db"] == pytest.approx(0.9)


def test_model_error_history_is_newest_first_and_bounded(tmp_path):
    path = tmp_path / "store.json"
    for index in range(MAX_MODEL_ERROR_RECORDS + 8):
        record_model_error(
            speaker_id=SPEAKER, attempt_id=f"a{index}", metric=METRIC,
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
        speaker_id=SPEAKER, attempt_id="a1", metric=METRIC,
        predicted_db=1.0, realized_db=1.0,
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
        speaker_id=SPEAKER, attempt_id="a1", metric=METRIC,
        predicted_db=1.0, realized_db=1.1,
        path=path,
    )
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
