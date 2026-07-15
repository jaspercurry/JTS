# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import commissioning_run
from jasper.active_speaker.commissioning_lifecycle import CommissioningTransition
from jasper.active_speaker.commissioning_run import (
    MAX_STATE_BYTES,
    CommissioningRunConflict,
    CommissioningRunError,
    CommissioningRunLockTimeout,
    CommissioningRunStale,
    CommissioningRunStore,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint

SESSION_ID = "0123456789ab"
SESSION_FINGERPRINT = "1" * 64
TARGET_ID = "mono:woofer"
TARGET_FINGERPRINT = "2" * 64


def _transition(
    from_state: str,
    to_state: str,
    evidence_kind: str | None,
    evidence_char: str | None,
    *,
    failure_code: str | None = None,
) -> CommissioningTransition:
    return CommissioningTransition(
        from_state=from_state,  # type: ignore[arg-type]
        to_state=to_state,  # type: ignore[arg-type]
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        evidence_fingerprint=(evidence_char * 64 if evidence_char else None),
        failure_code=failure_code,
    )


def _protection_transition() -> CommissioningTransition:
    return _transition(
        "unconfigured",
        "protected",
        "protection_evidence",
        "a",
    )


def _measurement_transition() -> CommissioningTransition:
    return _transition(
        "protected",
        "measured",
        "admitted_measurement_set",
        "b",
    )


def _candidate_transition() -> CommissioningTransition:
    return _transition(
        "measured",
        "candidate_ready",
        "candidate_artifact",
        "c",
    )


def _start(store: CommissioningRunStore):
    return store.start(
        session_id=SESSION_ID,
        session_fingerprint=SESSION_FINGERPRINT,
    )


def _recompute_fingerprint(payload: dict[str, Any]) -> None:
    core = {key: value for key, value in payload.items() if key != "fingerprint"}
    payload["fingerprint"] = json_fingerprint(core)


def _reserve_in_process(path: str, handle, start, queue) -> None:
    store = CommissioningRunStore(path=path, owner_id=handle.owner_id)
    start.wait(timeout=10)
    try:
        attempt = store.reserve_attempt(
            handle,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )
        queue.put(("ok", attempt.attempt_id, attempt.attempt_number))
    except CommissioningRunError as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _claim_live_execution_in_process(path: str, handle, start, queue) -> None:
    store = CommissioningRunStore(path=path, owner_id=handle.owner_id)
    start.wait(timeout=10)
    try:
        with store.claim_live_execution(handle):
            queue.put(("entered",))
    except CommissioningRunError as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _hold_live_execution_in_process(path: str, handle, ready, release) -> None:
    store = CommissioningRunStore(path=path, owner_id=handle.owner_id)
    with store.claim_live_execution(handle):
        ready.set()
        release.wait(timeout=30)


def test_start_persists_exact_identity_and_atomic_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)

    handle = _start(store)
    snapshot = store.snapshot()

    assert handle.session_id == SESSION_ID
    assert handle.session_fingerprint == SESSION_FINGERPRINT
    assert handle.owner_id == "3" * 32
    assert handle.owner_generation == 1
    assert len(handle.run_id) == 32
    assert snapshot["current"] == {
        "session_id": SESSION_ID,
        "session_fingerprint": SESSION_FINGERPRINT,
        "run_id": handle.run_id,
        "owner_id": "3" * 32,
        "owner_generation": 1,
        "lifecycle_state": "unconfigured",
        "attempts": [],
        "transition_journal": [],
        "started_at": snapshot["current"]["started_at"],
        "owner_claimed_at": snapshot["current"]["owner_claimed_at"],
        "updated_at": snapshot["current"]["updated_at"],
    }
    assert snapshot["fingerprint"] == json_fingerprint(
        {key: value for key, value in snapshot.items() if key != "fingerprint"}
    )
    assert oct(path.stat().st_mode & 0o777) == "0o640"


def test_run_attempt_and_restart_events_follow_successful_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )
    initial = CommissioningRunStore(path=path, owner_id="3" * 32)

    handle = _start(initial)
    start_fingerprint = initial.snapshot()["fingerprint"]
    attempt = initial.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    attempt_fingerprint = initial.snapshot()["fingerprint"]
    restarted = CommissioningRunStore(path=path, owner_id="4" * 32)
    claimed = restarted.claim_owner()
    assert claimed is not None

    assert events == [
        (
            "correction.active_commissioning_run_started",
            {
                "session": handle.session_id,
                "run_id": handle.run_id,
                "owner_generation": 1,
                "state_fingerprint": start_fingerprint,
            },
        ),
        (
            "correction.active_commissioning_attempt_reserved",
            {
                "session": handle.session_id,
                "run_id": handle.run_id,
                "owner_generation": 1,
                "attempt_id": attempt.attempt_id,
                "attempt_number": 1,
                "target": TARGET_ID,
                "target_fingerprint": TARGET_FINGERPRINT,
                "state_fingerprint": attempt_fingerprint,
            },
        ),
        (
            "correction.active_commissioning_owner_claimed",
            {
                "session": claimed.session_id,
                "run_id": claimed.run_id,
                "prior_owner_id": "3" * 32,
                "owner_generation": 2,
                "state_fingerprint": restarted.snapshot()["fingerprint"],
            },
        ),
    ]


def test_only_committed_transition_logs_and_polling_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CommissioningRunStore(path=tmp_path / "run.json", owner_id="3" * 32)
    handle = _start(store)
    attempt = store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    assert store.snapshot()["current"]["lifecycle_state"] == "unconfigured"
    assert store.callback_is_current(handle)
    assert store.callback_is_current(attempt)
    assert events == []

    assert store.transition(handle, _protection_transition(), attempt=attempt)
    current = store.snapshot()["current"]

    assert current["lifecycle_state"] == "protected"
    assert len(current["transition_journal"]) == 1
    entry = current["transition_journal"][0]
    assert entry["attempt_id"] == attempt.attempt_id
    assert entry["target_id"] == TARGET_ID
    assert entry["target_fingerprint"] == TARGET_FINGERPRINT
    assert entry["transition"] == _protection_transition().to_dict()
    assert events == [
        (
            "correction.active_commissioning_transition",
            {
                "session": SESSION_ID,
                "run_id": handle.run_id,
                "owner_generation": 1,
                "sequence": 1,
                "from_state": "unconfigured",
                "to_state": "protected",
                "evidence_kind": "protection_evidence",
                "evidence_fingerprint": "a" * 64,
                "failure_code": None,
                "attempt_id": attempt.attempt_id,
                "target": TARGET_ID,
                "state_fingerprint": store.snapshot()["fingerprint"],
            },
        )
    ]


def test_journal_is_contiguous_hash_chained_and_tracks_current_state(
    tmp_path: Path,
) -> None:
    store = CommissioningRunStore(path=tmp_path / "run.json", owner_id="3" * 32)
    handle = _start(store)

    assert store.transition(handle, _protection_transition())
    assert store.transition(handle, _measurement_transition())
    current = store.snapshot()["current"]
    first, second = current["transition_journal"]

    assert current["lifecycle_state"] == "measured"
    assert [first["sequence"], second["sequence"]] == [1, 2]
    assert first["previous_entry_fingerprint"] is None
    assert second["previous_entry_fingerprint"] == first["fingerprint"]


def test_transition_rejects_skipping_current_state_without_writing(
    tmp_path: Path,
) -> None:
    store = CommissioningRunStore(path=tmp_path / "run.json", owner_id="3" * 32)
    handle = _start(store)
    before = store.snapshot()

    with pytest.raises(CommissioningRunConflict, match="current state"):
        store.transition(handle, _measurement_transition())

    assert store.snapshot() == before


def test_restart_advances_generation_without_guessing_lifecycle_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    prior = CommissioningRunStore(path=path, owner_id="3" * 32)
    prior_handle = _start(prior)
    prior_attempt = prior.reserve_attempt(
        prior_handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    assert prior.transition(prior_handle, _protection_transition())

    restarted = CommissioningRunStore(path=path, owner_id="4" * 32)
    claimed = restarted.claim_owner()

    assert claimed is not None
    assert claimed.owner_id == "4" * 32
    assert claimed.owner_generation == 2
    assert claimed.session_id == prior_handle.session_id
    assert claimed.run_id == prior_handle.run_id
    assert restarted.snapshot()["current"]["lifecycle_state"] == "protected"
    assert not restarted.callback_is_current(prior_handle)
    assert not restarted.callback_is_current(prior_attempt)
    assert prior.transition(prior_handle, _measurement_transition()) is False
    with pytest.raises(CommissioningRunStale, match="stale run generation"):
        restarted.reserve_attempt(
            prior_handle,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )
    fresh_attempt = restarted.reserve_attempt(
        claimed,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    assert fresh_attempt.attempt_number == 2
    assert restarted.callback_is_current(fresh_attempt)


def test_same_owner_claim_is_idempotent_and_does_not_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    before = path.read_bytes()

    assert store.claim_owner() == handle
    assert path.read_bytes() == before


def test_current_handle_never_claims_another_process_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    owner = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(owner)
    observer = CommissioningRunStore(path=path, owner_id="4" * 32)
    before = path.read_bytes()

    assert owner.current_handle() == handle
    assert observer.current_handle() is None
    assert path.read_bytes() == before


def test_stale_attempt_cannot_annotate_a_current_transition(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    prior = CommissioningRunStore(path=path, owner_id="3" * 32)
    prior_handle = _start(prior)
    prior_attempt = prior.reserve_attempt(
        prior_handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    restarted = CommissioningRunStore(path=path, owner_id="4" * 32)
    current_handle = restarted.claim_owner()
    assert current_handle is not None

    assert (
        restarted.transition(
            current_handle,
            _protection_transition(),
            attempt=prior_attempt,
        )
        is False
    )
    assert restarted.snapshot()["current"]["transition_journal"] == []


def test_advisory_lock_serializes_multiprocess_attempt_reservations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_reserve_in_process,
            args=(str(path), handle, start, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert {outcome[0] for outcome in outcomes} == {"ok"}
    assert {outcome[2] for outcome in outcomes} == {1, 2}
    assert len({outcome[1] for outcome in outcomes}) == 2
    attempts = store.snapshot()["current"]["attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]


def test_live_execution_claim_is_fail_fast_and_released_by_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    peer = CommissioningRunStore(path=path, owner_id=handle.owner_id)

    with store.claim_live_execution(handle):
        with pytest.raises(CommissioningRunConflict, match="owns execution"):
            with peer.claim_live_execution(handle):
                pytest.fail("a second execution claim must never enter")
        assert oct(store.live_execution_lock_path.stat().st_mode & 0o777) == "0o640"

    with peer.claim_live_execution(handle):
        assert peer.callback_is_current(handle)


def test_live_execution_claim_is_exclusive_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    process = context.Process(
        target=_claim_live_execution_in_process,
        args=(str(path), handle, start, queue),
    )

    with store.claim_live_execution(handle):
        process.start()
        start.set()
        outcome = queue.get(timeout=10)
    process.join(timeout=10)

    assert process.exitcode == 0
    assert outcome[0:2] == ("error", "CommissioningRunConflict")
    assert "owns execution" in outcome[2]


def test_live_execution_claim_is_released_when_holder_process_dies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_live_execution_in_process,
        args=(str(path), handle, ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=10)
        with pytest.raises(CommissioningRunConflict, match="owns execution"):
            with store.claim_live_execution(handle):
                pytest.fail("the live holder must retain execution")
        process.terminate()
        process.join(timeout=10)
        assert process.exitcode is not None and process.exitcode != 0

        with store.claim_live_execution(handle):
            assert store.callback_is_current(handle)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


def test_live_execution_claim_refuses_stale_owner_generation(tmp_path: Path) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )
    stale = _start(store)
    restarted = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="4" * 32,
    )
    fresh = restarted.claim_owner()
    assert fresh is not None

    with pytest.raises(CommissioningRunStale, match="stale run generation"):
        with store.claim_live_execution(stale):
            pytest.fail("a stale owner must never hold live execution")


def test_threaded_attempt_reservations_remain_unique_and_contiguous(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    owner_id = "3" * 32
    initial = CommissioningRunStore(path=path, owner_id=owner_id)
    handle = _start(initial)

    def reserve(_index: int):
        return CommissioningRunStore(path=path, owner_id=owner_id).reserve_attempt(
            handle,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(reserve, range(16)))

    assert {attempt.attempt_number for attempt in attempts} == set(range(1, 17))
    assert len({attempt.attempt_id for attempt in attempts}) == 16


def test_atomic_write_failure_keeps_previous_state_and_emits_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    before = path.read_bytes()
    events: list[str] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )
    monkeypatch.setattr(
        commissioning_run,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        store.transition(handle, _protection_transition())

    assert path.read_bytes() == before
    assert events == []


def test_failed_restart_claim_never_publishes_a_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    prior = CommissioningRunStore(path=path, owner_id="3" * 32)
    prior_handle = _start(prior)
    restarted = CommissioningRunStore(path=path, owner_id="4" * 32)
    original = commissioning_run.atomic_write_text
    monkeypatch.setattr(
        commissioning_run,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        restarted.claim_owner()

    monkeypatch.setattr(commissioning_run, "atomic_write_text", original)
    assert prior.snapshot()["current"]["owner_generation"] == 1
    assert prior.callback_is_current(prior_handle)
    claimed = restarted.claim_owner()
    assert claimed is not None
    assert claimed.owner_generation == 2


def test_uuid_factory_cannot_reuse_an_attempt_identity(tmp_path: Path) -> None:
    repeated = uuid.UUID(hex="5" * 32)
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
        uuid_factory=lambda: repeated,
    )
    handle = _start(store)
    first = store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    before = store.snapshot()

    with pytest.raises(CommissioningRunError, match="not unique"):
        store.reserve_attempt(
            handle,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )

    assert first.attempt_id == "5" * 32
    assert store.snapshot() == before


@pytest.mark.parametrize("location", ["root", "current", "attempt", "journal"])
def test_unknown_fields_fail_closed(tmp_path: Path, location: str) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    attempt = store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    assert store.transition(handle, _protection_transition(), attempt=attempt)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if location == "root":
        raw["future_guess"] = True
    elif location == "current":
        raw["current"]["future_guess"] = True
    elif location == "attempt":
        raw["current"]["attempts"][0]["future_guess"] = True
    else:
        raw["current"]["transition_journal"][0]["future_guess"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CommissioningRunError, match="unknown or missing"):
        store.snapshot()


def test_tampered_whole_file_fingerprint_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    _start(store)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fingerprint"] = "f" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CommissioningRunError, match="does not match"):
        store.snapshot()


@pytest.mark.parametrize(
    "corruption",
    [
        "boolean_schema",
        "boolean_generation",
        "unknown_lifecycle",
        "attempts_not_array",
        "journal_not_array",
    ],
)
def test_semantically_malformed_state_fails_closed(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    _start(store)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "boolean_schema":
        raw["schema_version"] = True
    elif corruption == "boolean_generation":
        raw["current"]["owner_generation"] = True
    elif corruption == "unknown_lifecycle":
        raw["current"]["lifecycle_state"] = "applying"
    elif corruption == "attempts_not_array":
        raw["current"]["attempts"] = {}
    else:
        raw["current"]["transition_journal"] = {}
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CommissioningRunError):
        store.snapshot()


def test_rehashed_broken_journal_chain_still_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    assert store.transition(handle, _protection_transition())
    assert store.transition(handle, _measurement_transition())
    raw = json.loads(path.read_text(encoding="utf-8"))
    second = raw["current"]["transition_journal"][1]
    second["previous_entry_fingerprint"] = "f" * 64
    _recompute_fingerprint(second)
    _recompute_fingerprint(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CommissioningRunError, match="chain is broken"):
        store.snapshot()


def test_duplicate_json_fields_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"kind":"x",'
        '"current":null,"fingerprint":"x"}',
        encoding="utf-8",
    )

    with pytest.raises(CommissioningRunError, match="duplicate"):
        CommissioningRunStore(path=path, owner_id="3" * 32).snapshot()


def test_oversized_state_fails_before_json_decode(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_bytes(b" " * (MAX_STATE_BYTES + 1))

    with pytest.raises(CommissioningRunError, match="size limit"):
        CommissioningRunStore(path=path, owner_id="3" * 32).snapshot()


def test_collection_limits_refuse_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    monkeypatch.setattr(commissioning_run, "MAX_ATTEMPTS", 1)
    store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    with pytest.raises(CommissioningRunConflict, match="attempt limit"):
        store.reserve_attempt(
            handle,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )

    monkeypatch.setattr(commissioning_run, "MAX_TRANSITIONS", 1)
    assert store.transition(handle, _protection_transition())
    with pytest.raises(CommissioningRunConflict, match="transition limit"):
        store.transition(
            handle,
            _transition("protected", "unconfigured", None, None),
        )


def test_existing_run_cannot_be_silently_replaced(tmp_path: Path) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )
    first = _start(store)

    with pytest.raises(CommissioningRunConflict, match="already exists"):
        store.start(
            session_id="fedcba987654",
            session_fingerprint="9" * 64,
        )

    assert store.snapshot()["current"]["run_id"] == first.run_id


def test_explicit_replacement_commits_fresh_run_and_one_stable_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    prior = _start(store)
    assert store.transition(prior, _protection_transition())
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    fresh = store.replace_current(
        session_id="fedcba987654",
        session_fingerprint="9" * 64,
    )
    current = store.snapshot()["current"]

    assert fresh.run_id != prior.run_id
    assert fresh.session_id == "fedcba987654"
    assert fresh.owner_generation == 1
    assert current["run_id"] == fresh.run_id
    assert current["lifecycle_state"] == "unconfigured"
    assert current["attempts"] == []
    assert current["transition_journal"] == []
    assert events == [
        (
            "correction.active_commissioning_run_replaced",
            {
                "prior_session": prior.session_id,
                "prior_run_id": prior.run_id,
                "prior_state": "protected",
                "new_session": fresh.session_id,
                "new_run_id": fresh.run_id,
            },
        )
    ]


def test_replacement_makes_prior_run_and_attempt_callbacks_stale(
    tmp_path: Path,
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )
    prior = _start(store)
    attempt = store.reserve_attempt(
        prior,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    fresh = store.replace_current(
        session_id="fedcba987654",
        session_fingerprint="9" * 64,
    )

    assert store.callback_is_current(fresh)
    assert not store.callback_is_current(prior)
    assert not store.callback_is_current(attempt)
    assert store.transition(prior, _protection_transition()) is False
    with pytest.raises(CommissioningRunStale, match="stale run generation"):
        store.reserve_attempt(
            prior,
            target_id=TARGET_ID,
            target_fingerprint=TARGET_FINGERPRINT,
        )
    assert store.snapshot()["current"]["transition_journal"] == []


def test_replacement_write_failure_preserves_prior_run_and_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    prior = _start(store)
    before = path.read_bytes()
    events: list[str] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )
    monkeypatch.setattr(
        commissioning_run,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        store.replace_current(
            session_id="fedcba987654",
            session_fingerprint="9" * 64,
        )

    assert path.read_bytes() == before
    assert store.callback_is_current(prior)
    assert events == []


def test_replacement_without_prior_emits_one_fresh_start_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    fresh = store.replace_current(
        session_id=SESSION_ID,
        session_fingerprint=SESSION_FINGERPRINT,
    )

    assert store.callback_is_current(fresh)
    assert store.snapshot()["current"]["lifecycle_state"] == "unconfigured"
    assert events == [
        (
            "correction.active_commissioning_run_started",
            {
                "session": fresh.session_id,
                "run_id": fresh.run_id,
                "owner_generation": 1,
                "state_fingerprint": store.snapshot()["fingerprint"],
            },
        )
    ]


@pytest.mark.parametrize(
    "unsafe_state",
    ["applied_unverified", "blocked_live_state_unknown"],
)
def test_replacement_refuses_unknown_or_live_post_mutation_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_state: str,
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )
    handle = _start(store)
    assert store.transition(handle, _protection_transition())
    assert store.transition(handle, _measurement_transition())
    assert store.transition(handle, _candidate_transition())
    if unsafe_state == "applied_unverified":
        unsafe = _transition(
            "candidate_ready",
            "applied_unverified",
            "applied_candidate_proof",
            "d",
        )
    else:
        unsafe = _transition(
            "candidate_ready",
            "blocked_live_state_unknown",
            "uncertain_mutation_evidence",
            "d",
            failure_code="mutation_outcome_unknown",
        )
    assert store.transition(handle, unsafe)
    before = store.snapshot()
    events: list[str] = []
    monkeypatch.setattr(
        commissioning_run,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )

    with pytest.raises(CommissioningRunConflict, match="requires recovery"):
        store.replace_current(
            session_id="fedcba987654",
            session_fingerprint="9" * 64,
        )

    assert store.snapshot() == before
    assert store.callback_is_current(handle)
    assert events == []


def test_reads_use_a_bounded_file_handle_not_stat_then_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(store)
    real_open = Path.open
    read_sizes: list[int] = []

    class TrackingReader:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def read(self, size: int = -1):
            read_sizes.append(size)
            return self.wrapped.read(size)

        def __exit__(self, *args: Any):
            return self.wrapped.__exit__(*args)

    def tracked_open(candidate: Path, *args: Any, **kwargs: Any):
        opened = real_open(candidate, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if candidate == path and mode == "rb":
            return TrackingReader(opened)
        return opened

    monkeypatch.setattr(Path, "open", tracked_open)

    assert store.snapshot()["current"]["run_id"] == handle.run_id
    assert read_sizes == [MAX_STATE_BYTES + 1]


def test_in_process_lock_contention_has_a_typed_bounded_timeout(
    tmp_path: Path,
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
        lock_timeout_s=0.025,
    )
    handle = _start(store)
    acquired = threading.Event()
    release = threading.Event()

    def hold_thread_lock() -> None:
        commissioning_run._THREAD_LOCK.acquire()
        try:
            acquired.set()
            assert release.wait(timeout=5)
        finally:
            commissioning_run._THREAD_LOCK.release()

    holder = threading.Thread(target=hold_thread_lock)
    holder.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(CommissioningRunLockTimeout, match="in-process"):
            store.snapshot()
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()
    assert store.snapshot()["current"]["run_id"] == handle.run_id


def test_file_lock_polling_uses_one_deadline_and_never_sleeps_after_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    lock_path = path.with_name(f".{path.name}.lock")
    clock = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(duration: float) -> None:
        assert duration > 0.0
        assert clock[0] < 0.025
        sleeps.append(duration)
        clock[0] += duration

    store = CommissioningRunStore(
        path=path,
        owner_id="3" * 32,
        lock_timeout_s=0.025,
        monotonic=monotonic,
        sleep=sleep,
    )
    handle = _start(store)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CommissioningRunLockTimeout, match="file lock"):
            store.snapshot()
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    assert sum(sleeps) == pytest.approx(0.025)
    assert sleeps[-1] == pytest.approx(0.005)
    assert store.snapshot()["current"]["run_id"] == handle.run_id


@pytest.mark.parametrize(
    "timeout",
    [0.0, -1.0, float("nan"), float("inf"), 10.001, True, "1"],
)
def test_lock_timeout_must_be_small_positive_and_finite(
    tmp_path: Path,
    timeout: Any,
) -> None:
    with pytest.raises(CommissioningRunError, match="lock_timeout_s"):
        CommissioningRunStore(
            path=tmp_path / "run.json",
            owner_id="3" * 32,
            lock_timeout_s=timeout,
        )


@pytest.mark.parametrize(
    ("session_id", "session_fingerprint"),
    [
        ("", SESSION_FINGERPRINT),
        ("session with spaces", SESSION_FINGERPRINT),
        (SESSION_ID, "short"),
    ],
)
def test_start_rejects_ambiguous_session_identity(
    tmp_path: Path,
    session_id: str,
    session_fingerprint: str,
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="3" * 32,
    )

    with pytest.raises(CommissioningRunError):
        store.start(
            session_id=session_id,
            session_fingerprint=session_fingerprint,
        )
    assert not (tmp_path / "run.json").exists()


def test_empty_store_snapshot_is_exact_and_owner_claim_is_noop(tmp_path: Path) -> None:
    state_dir = tmp_path / "not-created-by-status"
    store = CommissioningRunStore(
        path=state_dir / "run.json",
        owner_id="3" * 32,
    )

    snapshot = store.snapshot()

    assert snapshot["current"] is None
    assert not state_dir.exists()
    assert store.claim_owner() is None
    assert not (state_dir / "run.json").exists()
    assert snapshot["fingerprint"] == json_fingerprint(
        {key: value for key, value in snapshot.items() if key != "fingerprint"}
    )


def test_lock_and_state_are_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="3" * 32)
    _start(store)

    lock = path.with_name(f".{path.name}.lock")
    assert oct(os.stat(lock).st_mode & 0o777) == "0o640"
    assert oct(os.stat(path).st_mode & 0o777) == "0o640"


def test_typed_attempt_and_lifecycle_reads_reject_stale_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    original = CommissioningRunStore(path=path, owner_id="3" * 32)
    handle = _start(original)
    attempt = original.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
    )

    assert original.attempts(handle) == (attempt,)
    assert original.lifecycle_state(handle) == "unconfigured"

    restarted = CommissioningRunStore(path=path, owner_id="4" * 32)
    fresh = restarted.claim_owner()
    assert fresh is not None
    with pytest.raises(CommissioningRunStale):
        restarted.attempts(handle)
    with pytest.raises(CommissioningRunStale):
        restarted.lifecycle_state(handle)
    assert restarted.attempts(fresh) == ()
    assert restarted.lifecycle_state(fresh) == "unconfigured"


def test_reserve_attempt_can_reuse_one_exact_target_for_host_retries(
    tmp_path: Path,
) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="5" * 32,
    )
    handle = _start(store)

    first = store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
        reuse_existing=True,
    )
    retried = store.reserve_attempt(
        handle,
        target_id=TARGET_ID,
        target_fingerprint=TARGET_FINGERPRINT,
        reuse_existing=True,
    )

    assert retried == first
    assert store.attempts(handle) == (first,)


def test_live_mutation_issue_is_cross_store_idempotent_and_exclusive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    first = CommissioningRunStore(path=path, owner_id="5" * 32)
    second = CommissioningRunStore(path=path, owner_id="5" * 32)
    handle = _start(first)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                store.issue_live_mutation,
                handle,
                purpose="summed_measurement",
                operation_fingerprint="6" * 64,
            )
            for store in (first, second)
        ]
    one, two = (future.result() for future in futures)

    assert one == two
    assert one.status == "issued"
    assert len(one.issuance_id) == 32
    assert first.current_live_mutation(handle) == one
    assert second.current_live_mutation(handle) == one
    with pytest.raises(CommissioningRunConflict, match="owns execution"):
        second.issue_live_mutation(
            handle,
            purpose="summed_measurement",
            operation_fingerprint="8" * 64,
        )


def test_live_mutation_phases_reopen_and_same_operation_retry_is_unique(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="5" * 32)
    handle = _start(store)
    issued = store.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )

    reopened = CommissioningRunStore(path=path, owner_id="5" * 32)
    assert reopened.current_live_mutation(handle) == issued
    assert reopened.pending_live_mutation(handle) is None
    with pytest.raises(CommissioningRunConflict, match="exact issuance"):
        reopened.record_live_mutation_intent(
            handle,
            issued,
            rollback_artifact_path="runtime-rollback/reused/predecessor.json",
            rollback_artifact_fingerprint="7" * 64,
        )
    first_path = f"runtime-rollback/{issued.issuance_id}/predecessor.json"
    pending = reopened.record_live_mutation_intent(
        handle,
        issued,
        rollback_artifact_path=first_path,
        rollback_artifact_fingerprint="7" * 64,
    )
    assert pending.status == "mutation_pending"
    reopened = CommissioningRunStore(path=path, owner_id="5" * 32)
    assert reopened.current_live_mutation(handle) == pending
    assert reopened.pending_live_mutation(handle) == pending
    with pytest.raises(CommissioningRunConflict, match="requires exact restored"):
        reopened.record_live_mutation_committed(
            handle,
            pending,
            commit_evidence_fingerprint="8" * 64,
        )
    restored = reopened.record_live_mutation_restored(
        handle,
        pending,
        restoration_evidence_fingerprint="8" * 64,
    )
    reopened = CommissioningRunStore(path=path, owner_id="5" * 32)
    assert reopened.current_live_mutation(handle) == restored
    assert reopened.pending_live_mutation(handle) is None
    committed = reopened.record_live_mutation_committed(
        handle,
        restored,
        commit_evidence_fingerprint="9" * 64,
    )
    reopened = CommissioningRunStore(path=path, owner_id="5" * 32)
    assert reopened.current_live_mutation(handle) == committed

    retried = reopened.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )
    assert retried.status == "issued"
    assert retried.issuance_id != issued.issuance_id
    with pytest.raises(CommissioningRunConflict, match="exact issuance"):
        reopened.record_live_mutation_intent(
            handle,
            retried,
            rollback_artifact_path=first_path,
            rollback_artifact_fingerprint="a" * 64,
        )
    second_path = f"runtime-rollback/{retried.issuance_id}/predecessor.json"
    retried_pending = reopened.record_live_mutation_intent(
        handle,
        retried,
        rollback_artifact_path=second_path,
        rollback_artifact_fingerprint="a" * 64,
    )
    assert retried_pending.rollback_artifact_path != pending.rollback_artifact_path
    with pytest.raises(CommissioningRunConflict, match="does not equal"):
        reopened.record_live_mutation_committed(
            handle,
            restored,
            commit_evidence_fingerprint="b" * 64,
        )


def test_restart_distinguishes_safe_issued_work_from_pending_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    original = CommissioningRunStore(path=path, owner_id="5" * 32)
    handle = _start(original)
    issued = original.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )

    restarted = CommissioningRunStore(path=path, owner_id="6" * 32)
    fresh = restarted.claim_owner()
    assert fresh is not None
    assert restarted.current_live_mutation(fresh) == issued
    assert restarted.pending_live_mutation(fresh) is None
    with pytest.raises(CommissioningRunStale, match="must be released"):
        restarted.record_live_mutation_intent(
            fresh,
            issued,
            rollback_artifact_path=(
                f"runtime-rollback/{issued.issuance_id}/predecessor.json"
            ),
            rollback_artifact_fingerprint="7" * 64,
        )
    released = restarted.release_live_mutation(fresh, issued)
    assert released.status == "released"
    assert released.terminal_owner_generation == 2
    reopened_released = CommissioningRunStore(path=path, owner_id="6" * 32)
    assert reopened_released.current_live_mutation(fresh) == released

    retried = reopened_released.issue_live_mutation(
        fresh,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )
    pending = reopened_released.record_live_mutation_intent(
        fresh,
        retried,
        rollback_artifact_path=(
            f"runtime-rollback/{retried.issuance_id}/predecessor.json"
        ),
        rollback_artifact_fingerprint="7" * 64,
    )
    recovered_store = CommissioningRunStore(path=path, owner_id="7" * 32)
    recovered_handle = recovered_store.claim_owner()
    assert recovered_handle is not None
    assert recovered_store.current_live_mutation(recovered_handle) == pending
    assert recovered_store.pending_live_mutation(recovered_handle) == pending
    restored = recovered_store.record_live_mutation_restored(
        recovered_handle,
        pending,
        restoration_evidence_fingerprint="b" * 64,
    )
    assert restored.status == "restored"
    assert restored.started_owner_generation == 2
    assert restored.resolved_owner_generation == 3
    assert recovered_store.pending_live_mutation(recovered_handle) is None
    with pytest.raises(CommissioningRunStale):
        original.pending_live_mutation(handle)
    assert (
        oct(os.stat(recovered_store.live_mutation_path).st_mode & 0o777)
        == "0o640"
    )


def test_clean_abort_requires_exact_restore_and_is_a_retryable_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    store = CommissioningRunStore(path=path, owner_id="5" * 32)
    handle = _start(store)
    issued = store.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )
    pending = store.record_live_mutation_intent(
        handle,
        issued,
        rollback_artifact_path=(
            f"runtime-rollback/{issued.issuance_id}/predecessor.json"
        ),
        rollback_artifact_fingerprint="7" * 64,
    )
    with pytest.raises(CommissioningRunConflict, match="requires exact restored"):
        store.record_live_mutation_aborted(
            handle,
            pending,
            failure_evidence_fingerprint="8" * 64,
        )

    restored = store.record_live_mutation_restored(
        handle,
        pending,
        restoration_evidence_fingerprint="8" * 64,
    )
    aborted = store.record_live_mutation_aborted(
        handle,
        restored,
        failure_evidence_fingerprint="9" * 64,
    )
    assert aborted.status == "aborted"
    assert aborted.issuance_id == issued.issuance_id
    assert aborted.restoration_evidence_fingerprint == "8" * 64
    assert aborted.terminal_evidence_fingerprint == "9" * 64
    assert aborted.terminal_owner_generation == handle.owner_generation

    reopened = CommissioningRunStore(path=path, owner_id="5" * 32)
    assert reopened.current_live_mutation(handle) == aborted
    assert reopened.pending_live_mutation(handle) is None
    with pytest.raises(CommissioningRunConflict, match="requires exact restored"):
        reopened.record_live_mutation_committed(
            handle,
            aborted,
            commit_evidence_fingerprint="a" * 64,
        )
    retried = reopened.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )
    assert retried.status == "issued"
    assert retried.issuance_id != aborted.issuance_id
    with pytest.raises(CommissioningRunConflict, match="does not equal"):
        reopened.record_live_mutation_aborted(
            handle,
            restored,
            failure_evidence_fingerprint="b" * 64,
        )


def test_live_mutation_sidecar_is_strictly_fingerprinted(tmp_path: Path) -> None:
    store = CommissioningRunStore(
        path=tmp_path / "run.json",
        owner_id="5" * 32,
    )
    handle = _start(store)
    issued = store.issue_live_mutation(
        handle,
        purpose="summed_measurement",
        operation_fingerprint="6" * 64,
    )
    store.record_live_mutation_intent(
        handle,
        issued,
        rollback_artifact_path=(
            f"runtime-rollback/{issued.issuance_id}/predecessor.json"
        ),
        rollback_artifact_fingerprint="7" * 64,
    )
    payload = json.loads(store.live_mutation_path.read_text())
    payload["operation_fingerprint"] = "8" * 64
    store.live_mutation_path.write_text(json.dumps(payload))

    with pytest.raises(CommissioningRunError, match="fingerprint"):
        store.pending_live_mutation(handle)
