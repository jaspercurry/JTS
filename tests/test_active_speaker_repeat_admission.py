# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from jasper.active_speaker import repeat_admission as admission


def _reserve_in_process(path: str, comparison: dict, start, queue) -> None:
    start.wait()
    try:
        result = admission.reserve(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="mono:woofer-fingerprint",
            path=path,
        )
        queue.put(("ok", result["attempt"]))
    except (OSError, RuntimeError, ValueError) as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _comparison(seed: str = "a") -> dict[str, str]:
    return {"comparison_set_id": seed * 32, "fingerprint": seed * 64}


def _reserve(path, comparison, target="mono:woofer"):
    return admission.reserve(
        comparison,
        target_id=target,
        target_fingerprint=f"{target}-fingerprint",
        path=path,
    )


def test_four_attempts_are_authoritative_and_fifth_is_refused(tmp_path):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    for attempt in range(1, 5):
        reservation = _reserve(path, comparison)
        assert reservation["attempt"] == attempt
        admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="mono:woofer-fingerprint",
            token=reservation["token"],
            result={"accepted": False, "reject_reason": "level_outlier"},
            status="active",
            path=path,
        )
    with pytest.raises(ValueError, match="four attempts"):
        _reserve(path, comparison)
    assert admission.snapshot(comparison, path=path)["targets"]["mono:woofer"][
        "attempts"
    ] == 4


def test_fourth_transport_failure_is_terminal_and_actionable():
    assert admission.failure_status(1) == "active"
    assert admission.failure_status(3) == "active"
    assert admission.failure_status(4) == "refused"
    assert admission.failure_status("malformed") == "refused"


def test_only_one_concurrent_inflight_reservation_wins(tmp_path):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    barrier = threading.Barrier(3)
    outcomes = []

    def worker():
        barrier.wait()
        try:
            outcomes.append(("ok", _reserve(path, comparison)))
        except ValueError as exc:
            outcomes.append(("error", str(exc)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("error") == 1
    assert "already in progress" in next(value for kind, value in outcomes if kind == "error")


def test_exact_token_and_ready_gate_final_measurement(tmp_path):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    reservation = _reserve(path, comparison)
    with pytest.raises(ValueError, match="matching inflight"):
        admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="mono:woofer-fingerprint",
            token="wrong",
            result={},
            status="ready",
            path=path,
        )
    admission.finish(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="mono:woofer-fingerprint",
        token=reservation["token"],
        result={"accepted": True},
        status="ready",
        path=path,
    )
    with pytest.raises(ValueError, match="is ready"):
        _reserve(path, comparison)
    completed = admission.complete(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="mono:woofer-fingerprint",
        path=path,
    )
    assert completed["status"] == "completed"


def test_result_payload_cannot_override_authoritative_attempt_number(tmp_path):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    reservation = _reserve(path, comparison)
    finished = admission.finish(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="mono:woofer-fingerprint",
        token=reservation["token"],
        result={"attempt": 99, "accepted": False},
        status="active",
        path=path,
    )
    assert finished["results"] == [{"attempt": 1, "accepted": False}]


def test_new_process_owner_aborts_inflight_without_resetting_attempts(
    tmp_path, monkeypatch
):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    _reserve(path, comparison)
    monkeypatch.setattr(admission, "OWNER_ID", "new-process")
    # Reads are pure: only the explicit single-service startup claim retires
    # the previous process's unfinished work.
    assert admission.snapshot(comparison, path=path)["targets"]["mono:woofer"][
        "status"
    ] == "active"
    state = admission.claim_owner(path=path)
    target = state["targets"]["mono:woofer"]
    assert target["status"] == "aborted"
    assert target["attempts"] == 1
    assert target["inflight"] is None
    with pytest.raises(ValueError, match="is aborted"):
        _reserve(path, comparison)


def test_failed_startup_owner_claim_keeps_status_unavailable_until_reset(
    tmp_path, monkeypatch
):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    _reserve(path, comparison)
    monkeypatch.setattr(admission, "OWNER_ID", "new-process")
    original = admission.atomic_write_text
    monkeypatch.setattr(
        admission,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        admission.claim_owner(path=path)
    with pytest.raises(RuntimeError, match="ownership claim failed"):
        admission.snapshot(comparison, path=path)
    monkeypatch.setattr(admission, "atomic_write_text", original)
    fresh = _comparison("b")
    admission.activate(fresh, path=path)
    assert _reserve(path, fresh)["attempt"] == 1


def test_flock_serializes_true_multiprocess_reservations(tmp_path):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_reserve_in_process,
            args=(str(path), comparison, start, queue),
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
    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("error") == 1
    state = admission.snapshot(comparison, path=path)
    assert state["targets"]["mono:woofer"]["attempts"] == 1
    assert state["targets"]["mono:woofer"]["status"] == "active"


def test_single_service_process_contract_is_pinned():
    socket_unit = Path("deploy/jasper-correction-web.socket").read_text()
    service_unit = Path("deploy/jasper-correction-web.service").read_text()
    assert "Accept=no" in socket_unit
    assert service_unit.count("\nExecStart=") == 1


def test_new_comparison_resets_old_terminal_state(tmp_path):
    path = tmp_path / "repeat.json"
    first = _comparison("a")
    second = _comparison("b")
    admission.activate(first, path=path)
    _reserve(path, first)
    admission.activate(second, path=path)
    reservation = _reserve(path, second)
    assert reservation["attempt"] == 1


def test_reservation_write_failure_never_publishes_an_attempt(tmp_path, monkeypatch):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    original = admission.atomic_write_text

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(admission, "atomic_write_text", fail)
    with pytest.raises(OSError, match="disk full"):
        _reserve(path, comparison)
    monkeypatch.setattr(admission, "atomic_write_text", original)
    assert admission.snapshot(comparison, path=path)["targets"] == {}


@pytest.mark.parametrize("finish_status", ["active", "ready"])
def test_finish_write_failure_leaves_inflight_gate_closed(
    tmp_path, monkeypatch, finish_status
):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    reservation = _reserve(path, comparison)
    original = admission.atomic_write_text
    events = []
    monkeypatch.setattr(
        admission,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        admission,
        "atomic_write_text",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="mono:woofer-fingerprint",
            token=reservation["token"],
            result={"accepted": finish_status == "ready"},
            status=finish_status,
            path=path,
        )
    monkeypatch.setattr(admission, "atomic_write_text", original)
    target = admission.snapshot(comparison, path=path)["targets"]["mono:woofer"]
    assert target["inflight"] == reservation["token"]
    with pytest.raises(ValueError, match="already in progress"):
        _reserve(path, comparison)
    assert events == []


def test_complete_write_failure_stays_ready_and_blocks_fifth(tmp_path, monkeypatch):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    reservation = _reserve(path, comparison)
    admission.finish(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="mono:woofer-fingerprint",
        token=reservation["token"],
        result={"accepted": True},
        status="ready",
        path=path,
    )
    original = admission.atomic_write_text
    monkeypatch.setattr(
        admission,
        "atomic_write_text",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        admission.complete(
            comparison,
            target_id="mono:woofer",
            target_fingerprint="mono:woofer-fingerprint",
            path=path,
        )
    monkeypatch.setattr(admission, "atomic_write_text", original)
    assert admission.snapshot(comparison, path=path)["targets"]["mono:woofer"][
        "status"
    ] == "ready"
    with pytest.raises(ValueError, match="is ready"):
        _reserve(path, comparison)


@pytest.mark.parametrize(
    "mutation",
    [
        {"attempts": -1},
        {"attempts": 0},
        {"attempts": 5},
        {"attempts": None, "results": [{"attempt": 1}]},
        {"status": "mystery"},
        {"inflight": "short"},
        {"owner_id": "z" * 32},
        {"results": [{"attempt": 0}]},
        {"results": [{"attempt": 1}, {"attempt": 1}]},
        {"results": ""},
        {"target_id": "other"},
    ],
)
def test_semantically_corrupt_state_fails_closed(tmp_path, mutation):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    _reserve(path, comparison)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["targets"]["mono:woofer"].update(mutation)
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeError, match="state is invalid"):
        admission.snapshot(comparison, path=path)


@pytest.mark.parametrize(
    "target_id,target_fingerprint",
    [("", "fingerprint"), ("mono:woofer", "")],
)
def test_empty_target_binding_is_rejected_before_write(
    tmp_path, target_id, target_fingerprint
):
    path = tmp_path / "repeat.json"
    comparison = _comparison()
    admission.activate(comparison, path=path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="complete target binding"):
        admission.reserve(
            comparison,
            target_id=target_id,
            target_fingerprint=target_fingerprint,
            path=path,
        )
    assert path.read_bytes() == before
