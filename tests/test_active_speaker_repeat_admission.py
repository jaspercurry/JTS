# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
import os

import pytest

from jasper.active_speaker import repeat_admission as admission


@pytest.fixture(autouse=True)
def process_owner(monkeypatch):
    monkeypatch.setattr(admission, "OWNER_ID", "f" * 32)
    monkeypatch.setattr(admission, "_CLAIM_ERROR", None)


@pytest.fixture
def legacy_record():
    return {
        "schema_version": 1,
        "kind": "jts_active_speaker_repeat_admission",
        "comparison": {"comparison_set_id": "a" * 32, "fingerprint": "a" * 64},
        "targets": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "target_fingerprint": "mono:woofer-fingerprint",
                "owner_id": "e" * 32,
                "attempts": 2,
                "status": "active",
                "inflight": "b" * 32,
                "results": [{"attempt": 1, "accepted": True}],
                "reason": None,
                "updated_at": "2026-07-01T12:00:00Z",
            },
        },
        "updated_at": "2026-07-01T12:00:00Z",
    }


@pytest.fixture
def repeat_path(tmp_path, legacy_record):
    path = tmp_path / "repeat.json"
    path.write_text(json.dumps(legacy_record), encoding="utf-8")
    return path


def test_owner_claim_publishes_the_lock_group_writable(repeat_path):
    admission.claim_owner(path=repeat_path)
    lock_path = repeat_path.with_name(f".{repeat_path.name}.lock")

    assert os.stat(lock_path).st_mode & 0o777 == 0o660


def test_snapshot_reads_without_taking_the_lock(repeat_path, legacy_record):
    assert admission.snapshot(legacy_record["comparison"], path=repeat_path) == legacy_record
    assert not repeat_path.with_name(f".{repeat_path.name}.lock").exists()


def test_missing_snapshot_is_empty_without_creating_files(tmp_path):
    path = tmp_path / "repeat.json"

    state = admission.snapshot(path=path)

    assert state["targets"] == {}
    assert state["comparison"] is None
    assert list(tmp_path.iterdir()) == []


def test_snapshot_rejects_a_different_comparison(repeat_path):
    with pytest.raises(ValueError):
        admission.snapshot(
            {"comparison_set_id": "c" * 32, "fingerprint": "c" * 64},
            path=repeat_path,
        )


@pytest.mark.parametrize(
    "status,reason",
    [("active", "service_restarted"), ("ready", "service_restarted_during_finalization")],
)
def test_new_process_owner_aborts_unfinished_work_without_losing_results(
    repeat_path, legacy_record, status, reason
):
    prior = legacy_record["targets"]["mono:woofer"]
    prior.update(status=status, inflight="b" * 32 if status == "active" else None)
    repeat_path.write_text(json.dumps(legacy_record), encoding="utf-8")

    assert admission.snapshot(path=repeat_path)["targets"]["mono:woofer"] == prior
    admission.claim_owner(path=repeat_path)
    target = admission.snapshot(path=repeat_path)["targets"]["mono:woofer"]

    assert target["status"] == "aborted"
    assert target["reason"] == reason
    assert target["inflight"] is None
    assert target["attempts"] == prior["attempts"]
    assert target["results"] == prior["results"]


@pytest.mark.parametrize(
    "owner,status",
    [("f", "active"), ("f", "ready"), ("e", "completed"), ("e", "refused"), ("e", "aborted")],
)
def test_owner_claim_preserves_current_or_terminal_work(
    repeat_path, legacy_record, owner, status
):
    legacy_record["targets"]["mono:woofer"].update(
        owner_id=owner * 32, status=status, inflight=None,
    )
    repeat_path.write_text(json.dumps(legacy_record), encoding="utf-8")
    before = repeat_path.read_bytes()

    assert admission.claim_owner(path=repeat_path) == legacy_record
    assert repeat_path.read_bytes() == before


def test_failed_startup_claim_keeps_status_unavailable_until_successful_retry(
    repeat_path, legacy_record, monkeypatch
):
    original = admission.atomic_write_text
    events = []
    monkeypatch.setattr(
        admission, "log_event", lambda _logger, event, **fields: events.append((event, fields)),
    )

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission, "atomic_write_text", fail)
    with pytest.raises(OSError):
        admission.claim_owner(path=repeat_path)
    with pytest.raises(RuntimeError):
        admission.snapshot(path=repeat_path)
    assert json.loads(repeat_path.read_text(encoding="utf-8")) == legacy_record
    assert events == []

    monkeypatch.setattr(admission, "atomic_write_text", original)
    admission.claim_owner(path=repeat_path)

    target = admission.snapshot(path=repeat_path)["targets"]["mono:woofer"]
    assert target["status"] == "aborted"
    assert events == [(
        "correction.crossover_repeat_aborted",
        {"target": "mono:woofer", "attempts": 2, "reason": "service_restarted"},
    )]


@pytest.mark.parametrize(
    "mutation",
    [
        {"attempts": -1},
        {"attempts": 0},
        {"attempts": 9},
        {"attempts": None},
        {"status": "mystery"},
        {"inflight": "short"},
        {"owner_id": "z" * 32},
        {"results": [{"attempt": 0}]},
        {"results": [{"attempt": 1}, {"attempt": 1}]},
        {"results": [{"attempt": 1, "audio_emitted": "yes"}]},
        {"results": ""},
        {"target_id": "other"},
    ],
)
def test_semantically_corrupt_state_fails_closed(repeat_path, legacy_record, mutation):
    legacy_record["targets"]["mono:woofer"].update(mutation)
    repeat_path.write_text(json.dumps(legacy_record), encoding="utf-8")

    with pytest.raises(RuntimeError):
        admission.snapshot(path=repeat_path)


@pytest.mark.parametrize(
    "result,expected",
    [({}, True), ({"audio_emitted": False}, False), ({"audio_emitted": True}, True),
     ({"audio_emitted": None}, True), ({"audio_emitted": 0}, True)],
)
def test_only_explicit_no_audio_is_refunded(result, expected):
    assert admission.result_emitted_audio(result) is expected


@pytest.mark.parametrize(
    "results,expected",
    [(None, 0), ({}, 0), ([], 0),
     ([{"audio_emitted": False}, {}, {"audio_emitted": True}, None], 2),
     (({"audio_emitted": False}, {}), 1)],
)
def test_measurement_attempts_counts_recorded_audio(results, expected):
    assert admission.measurement_attempts(results) == expected
