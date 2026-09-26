# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_volume as v2volume

import asyncio
import concurrent.futures
import io
import inspect
import logging
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from jasper.web import (
    correction_capture,
    correction_handlers,
    correction_runtime,
    correction_setup,
)
from jasper.active_speaker.measurement_emit import MeasurementGraphRefused
from jasper.active_speaker.crossover_v2.refusal_copy import (
    CrossoverV2Refused, REASON_INTERNAL_ERROR, REASON_REGISTRY,
)
from jasper.web.correction_runtime import refusal_envelope
from jasper.platform.systemd import no_hold

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_until_sync
from ._log_events import event_fields, event_records
from ._web_test_helpers import make_csrf_session, request_with_csrf

def test_capture_stop_holds_slot_until_owner_cleanup_is_terminal():
    stop_event = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        await asyncio.to_thread(stop_event.wait)
        cleanup_started.set()
        await asyncio.to_thread(release_cleanup.wait)

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_sweep:driver",
                open=open_capture,
                run_and_consume=run_and_consume,
                request_stop=lambda reason: stop_event.set(),
            ),
            idle_hold=no_hold,
        )
        response = correction_capture._request_capture_stop("crossover_sweep:")
        assert response["status"] == "stopping"
        assert stop_event.is_set()
        assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert correction_capture._get_capture_slot()["status"] == "stopping"
        assert not correction_capture._begin_capture_slot("crossover_sweep:summed")
        release_cleanup.set()
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "stopped"
        )
        assert correction_capture._get_capture_slot()["status"] == "stopped"
    finally:
        release_cleanup.set()

class _RecordingIdleHold:

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.active = 0

    def __call__(self, label: str):
        @contextmanager
        def _cm():
            self.events.append(("acquire", label))
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1
                self.events.append(("release", label))

        return _cm()

    @property
    def labels(self) -> list[str]:
        return [label for _kind, label in self.events]

def test_the_capture_spawn_seam_has_no_silent_idle_hold_default():
    param = inspect.signature(correction_capture._run_capture).parameters["idle_hold"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "idle_hold must stay required — pass _systemd.no_hold to opt out "
        "explicitly"
    )

def test_capture_holds_the_idle_exit_for_the_whole_background_session():
    idle_hold = _RecordingIdleHold()
    release_runner = threading.Event()
    runner_entered = threading.Event()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        runner_entered.set()
        await asyncio.to_thread(release_runner.wait)

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_v2:session",
                open=open_capture,
                run_and_consume=run_and_consume,
            ),
            idle_hold=idle_hold,
        )
        assert idle_hold.events == [("acquire", "capture:crossover_v2:session")]
        assert runner_entered.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert idle_hold.active == 1

        release_runner.set()
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "complete"
        )
        assert correction_capture._get_capture_slot()["status"] == "complete"
    finally:
        release_runner.set()
        correction_capture._set_capture_slot(None)

    wait_until_sync(lambda: not idle_hold.active)
    assert idle_hold.active == 0, "the completed session released its hold"
    assert idle_hold.events == [
        ("acquire", "capture:crossover_v2:session"),
        ("release", "capture:crossover_v2:session"),
    ]

@pytest.mark.parametrize("exc,code", [
    (RuntimeError("link timeout"), None),
    (MeasurementGraphRefused("measurement_candidate_required", {}),
     "measurement_candidate_required"),
    (MeasurementGraphRefused("measurement_unregistered", {}), "measurement_unregistered"),
])
def test_capture_releases_the_idle_hold_when_the_runner_fails(exc, code):
    idle_hold = _RecordingIdleHold()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        raise exc

    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_v2:verify",
                open=open_capture,
                run_and_consume=run_and_consume,
            ),
            idle_hold=idle_hold,
        )
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "failed"
        )
        failure = correction_capture._get_capture_slot()
        assert failure["code"] == code
        assert failure["ok"] is False
        if code == "measurement_candidate_required":
            assert failure["next_action"]["id"] == "select_candidate"
        else:
            assert failure["next_action"] is None
        if code == "measurement_unregistered":
            assert failure["error"] is REASON_REGISTRY[REASON_INTERNAL_ERROR].message
    finally:
        correction_capture._set_capture_slot(None)

    wait_until_sync(lambda: not idle_hold.active)
    assert idle_hold.active == 0
    assert idle_hold.events == [
        ("acquire", "capture:crossover_v2:verify"),
        ("release", "capture:crossover_v2:verify"),
    ]

@pytest.mark.parametrize("exc,code", [
    (CrossoverV2Refused("hold expired", code="position_hold_expired"), "position_hold_expired"),
    (CrossoverV2Refused("unexpected: detail", code=REASON_INTERNAL_ERROR), None),
    (RuntimeError("link timeout"), None),
])
def test_a_refused_capture_logs_its_code_and_only_an_unexpected_one_a_traceback(
    caplog, exc, code,
):
    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        raise exc

    caplog.set_level(logging.INFO)
    correction_capture._set_capture_slot(None)
    try:
        correction_capture._run_capture(
            correction_capture.CaptureKind(
                label="crossover_v2:session",
                open=open_capture,
                run_and_consume=run_and_consume,
            ),
            idle_hold=no_hold,
        )
        wait_until_sync(
            lambda: correction_capture._get_capture_slot()["status"] == "failed"
        )
    finally:
        correction_capture._set_capture_slot(None)

    (record,) = event_records(caplog, "correction.capture_failed")
    assert (record.exc_info is None) is (code is not None)
    assert event_fields(caplog, "correction.capture_failed").get("code") == code

def test_capture_drops_the_idle_hold_when_the_runner_never_spawns(
    monkeypatch,
):
    """A failed spawn must not leave a hold nobody will ever release."""
    idle_hold = _RecordingIdleHold()

    def open_capture():
        return SimpleNamespace(pi_session=object())

    async def run_and_consume(_pi_session):
        raise AssertionError("never scheduled")

    def _refuse(coro, _loop):
        coro.close()
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(
        correction_setup.asyncio, "run_coroutine_threadsafe", _refuse,
    )
    correction_capture._set_capture_slot(None)
    try:
        with pytest.raises(RuntimeError, match="event loop is closed"):
            correction_capture._run_capture(
                correction_capture.CaptureKind(
                    label="crossover_v2:session",
                    open=open_capture,
                    run_and_consume=run_and_consume,
                ),
                idle_hold=idle_hold,
            )
    finally:
        correction_capture._set_capture_slot(None)

    assert idle_hold.active == 0
    assert idle_hold.labels == [
        "capture:crossover_v2:session", "capture:crossover_v2:session",
    ]

def test_the_v2_dispatch_threads_the_idle_hold_into_the_capture_runner(
    monkeypatch,
):
    idle_hold = _RecordingIdleHold()
    seen: dict[str, object] = {}

    def _fake_prepare(raw, *, status, run_async, camilla_factory):
        seen["prepare_kwargs"] = {
            "status", "run_async", "camilla_factory",
        }
        return SimpleNamespace(
            label="crossover_v2:session",
            open=lambda *a, **kw: None,
            run_and_consume=lambda *a, **kw: None,
            request_stop=lambda reason: None,
            position_gate=None,
            request_complete=None,
            request_retake=None, join_spec=None, session_id="test",
        )

    def _fake_run_capture(kind, *, idle_hold):
        seen["orchestrator"] = idle_hold
        return {"status": "awaiting_capture"}

    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _h: {})
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", dict)
    monkeypatch.setattr(v2host, "prepare_v2_session", _fake_prepare)
    monkeypatch.setattr(correction_capture, "_run_capture", _fake_run_capture)
    monkeypatch.setattr(correction_capture, "_stage_capture", _fake_run_capture)

    correction_handlers._handle_crossover_v2_capture(
        None, idle_hold=idle_hold,
    )

    assert "idle_hold" not in seen["prepare_kwargs"]
    assert "idle_hold" not in inspect.signature(v2host.prepare_v2_session).parameters
    assert seen["orchestrator"] is idle_hold

    from jasper.platform import systemd as _systemd

    built: dict[str, object] = {}
    monkeypatch.setattr(
        _systemd,
        "make_http_server",
        lambda _target, handler_cls: built.setdefault("cls", handler_cls),
    )
    correction_setup.make_server(0, idle_hold=idle_hold)
    assert built["cls"].idle_hold is idle_hold

def test_the_v2_dispatch_carries_its_routes_stage_into_the_capture_kind(
    monkeypatch,
):
    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2 as v2host

    seen: dict[str, object] = {}

    def _fake_prepare(raw, *, status, run_async, camilla_factory):
        return SimpleNamespace(
            label=(
                v2host.V2_CAPTURE_KIND_SESSION
            ),
            open=lambda *a, **kw: None,
            run_and_consume=lambda *a, **kw: None,
            request_stop=lambda reason: None,
            position_gate=None,
            request_complete=None,
            request_retake=None, join_spec=None, session_id="test",
        )

    def _fake_run_capture(kind, *, idle_hold):
        seen["kind"] = kind
        return {"status": "awaiting_capture"}

    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _h: {})
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", dict)
    monkeypatch.setattr(v2host, "prepare_v2_session", _fake_prepare)
    monkeypatch.setattr(correction_capture, "_run_capture", _fake_run_capture)
    monkeypatch.setattr(correction_capture, "_stage_capture", _fake_run_capture)

    correction_handlers._handle_crossover_v2_capture(None)

    expected_label = "crossover_v2:session"
    assert seen["kind"].label == expected_label

@pytest.mark.parametrize(
    "recovery", ["exact_restored", "failed", v2volume.RECOVERY_DEFERRED]
)
def test_capture_recovers_stranded_volume_before_preparing(monkeypatch, recovery):
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, REGIME_SUMMED
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2 as v2host

    plan = SimpleNamespace(needs_recovery=True)
    calls = []
    real_prepare = v2host.prepare_v2_session
    body = {"plan": AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),)).to_dict()}

    def recover(run_async, camilla_factory):
        assert run_async is correction_runtime.run_async
        assert camilla_factory is correction_runtime.camilla_controller
        calls.append("recover")
        plan.needs_recovery = recovery != "exact_restored"
        return not plan.needs_recovery, recovery

    def prepare(raw, **kwargs):
        calls.append("prepare")
        if plan.needs_recovery:
            return real_prepare(raw, **kwargs)
        return SimpleNamespace(
            label="crossover_v2:session",
            open=None,
            run_and_consume=None,
            request_stop=None,
            position_gate=None,
            request_complete=None,
            request_retake=None,
            session_id="test",
            join_spec=None,
        )

    def stage(kind, **kwargs):
        calls.append("stage")
        return {"status": "awaiting_capture", "session_id": kind.session_id}

    monkeypatch.setattr(v2volume, "session_volume_plan", lambda: plan)
    monkeypatch.setattr(v2volume, "recover_session_volume", recover)
    monkeypatch.setattr(correction_runtime, "read_json_body", lambda _: body)
    monkeypatch.setattr(correction_capture, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_crossover_backend, "status_payload", dict)
    monkeypatch.setattr(v2host, "prepare_v2_session", prepare)
    monkeypatch.setattr(correction_capture, "_stage_capture", stage)

    if recovery == "exact_restored":
        assert correction_handlers._handle_crossover_v2_capture(None) == {
            "capture": {"status": "awaiting_capture", "session_id": "test"},
        }
        assert calls == ["recover", "prepare", "stage"]
        assert not plan.needs_recovery
    else:
        with pytest.raises(CrossoverV2Refused) as expected:
            real_prepare(body, status={}, run_async=None, camilla_factory=None)
        with pytest.raises(CrossoverV2Refused) as actual:
            correction_handlers._handle_crossover_v2_capture(None)
        assert refusal_envelope(actual.value) == refusal_envelope(expected.value)
        assert calls == ["recover", "prepare"]
        assert plan.needs_recovery

def _capture(monkeypatch, status, run="wired-live"):
    """Put crossover run ``run`` in the slot at ``status``; return the flow's view of it."""
    slot = None if status is None else {"kind": "crossover_v2:session", "session_id": run, "status": status}
    monkeypatch.setattr(correction_capture, "_pending_capture", None)
    monkeypatch.setattr(correction_capture, "_capture_slot", slot)
    return correction_capture._get_capture_slot_for("crossover_v2:")


def _spy_slow_reads(monkeypatch, tmp_path):
    """Log the crossover status answer's slow reads; each read answers with its call ordinal."""
    from jasper.active_speaker import controllability_ledger, setup_status
    from jasper.web import correction_crossover_backend as backend
    from jasper.web import correction_crossover_v2_state as v2state

    calls: list[str] = []
    applied = {"config": {"sha256": "a" * 64}, "source": {"measured_candidate_fingerprint": "fp"}}

    def read(name, answer):
        def spy(*_args, **_kwargs):
            calls.append(name)
            return {**answer, "read": len(calls)}
        return spy

    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json"))
    monkeypatch.setattr(v2state, "_state_path_override", tmp_path / "v2_state.json")
    monkeypatch.setattr(v2volume, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False))
    monkeypatch.setattr(backend, "_run_snapshot", None)
    monkeypatch.setattr(backend, "load_applied_baseline_profile_state", lambda: applied)
    monkeypatch.setattr(setup_status, "read_active_speaker_setup_status", read("setup", {"active": False}))
    monkeypatch.setattr(backend, "latest_banked_rounds", read("rounds", {}))
    monkeypatch.setattr(controllability_ledger, "read_controllability_ledger", read("ledger", {"rounds": []}))
    return calls, applied


@pytest.mark.parametrize("status", ["starting", "awaiting_capture", "stopping"])
def test_a_live_capture_reuses_its_first_status_answer_until_an_apply_or_another_run(
    monkeypatch, tmp_path, status,
):
    """#5632 F1: the microphone reader shares this process, so a live run's
    polls must not recompile graphs or rescan the banks."""
    from jasper.web import correction_crossover_flow as flow

    calls, applied = _spy_slow_reads(monkeypatch, tmp_path)
    idle, _ = flow.handle_status(capture=_capture(monkeypatch, None))
    capture = _capture(monkeypatch, status)
    first, _ = flow.handle_status(capture=capture)
    calls.clear()
    again, _ = flow.handle_status(capture=capture)
    envelope, _ = flow.handle_envelope(capture=capture)
    assert calls == []
    assert set(again) == set(idle)
    assert (idle["snapshot_at"], first["snapshot_at"], again["snapshot_at"]) == (None, None, first["generated_at"])
    assert [again[key] for key in ("setup", "timing")] == [first[key] for key in ("setup", "timing")]
    assert again["crossover_v2"]["controllability"] == first["crossover_v2"]["controllability"]
    assert (envelope["capture"]["session_id"], envelope["snapshot_at"]) == ("wired-live", first["generated_at"])
    applied["config"] = {"sha256": "b" * 64}
    assert flow.handle_status(capture=capture)[0]["snapshot_at"] is None
    assert calls == ["setup", "rounds", "ledger"]
    calls.clear()
    assert flow.handle_status(capture=_capture(monkeypatch, status, run="wired-next"))[0]["snapshot_at"] is None
    assert calls == ["setup", "rounds", "ledger"]


def test_a_live_capture_reuses_an_unreadable_ledger_without_rescanning(monkeypatch, tmp_path):
    from jasper.active_speaker import controllability_ledger
    from jasper.web import correction_crossover_flow as flow

    calls, _ = _spy_slow_reads(monkeypatch, tmp_path)

    def unreadable():
        calls.append("ledger")
        raise OSError("bundle root unreadable")

    monkeypatch.setattr(controllability_ledger, "read_controllability_ledger", unreadable)
    capture = _capture(monkeypatch, "awaiting_capture")
    answers = [flow.handle_status(capture=capture)[0] for _ in range(2)]
    assert calls == ["setup", "rounds", "ledger"]
    assert [answer["crossover_v2"]["controllability"] for answer in answers] == [None, None]


@pytest.mark.parametrize("status", [None, "complete", "stopped", "failed"])
def test_status_outside_a_live_capture_reads_every_slow_block_fresh(monkeypatch, tmp_path, status):
    from jasper.web import correction_crossover_flow as flow

    calls, _ = _spy_slow_reads(monkeypatch, tmp_path)
    capture = _capture(monkeypatch, status)
    answers = [flow.handle_status(capture=capture)[0] for _ in range(2)]
    assert calls == ["setup", "rounds", "ledger"] * 2
    assert [(answer["snapshot_at"], answer["setup"]["read"], answer["crossover_v2"]["controllability"]["read"])
            for answer in answers] == [(None, 1, 3), (None, 4, 6)]

def test_capture_stop_callback_is_atomic_with_starting_state():
    stopped = threading.Event()
    kind = "crossover_sweep:driver"

    correction_capture._set_capture_slot(None)
    try:
        assert correction_capture._begin_capture_slot(
            kind,
            request_stop=lambda reason: stopped.set(),
        )
        response = correction_capture._request_capture_stop("crossover_sweep:")
        assert response["status"] == "stopping"
        assert stopped.is_set()
        waiting = correction_capture._publish_capture_waiting(kind)
        assert waiting["status"] == "stopping"
    finally:
        correction_capture._set_capture_slot(None)

def test_run_async_timeout_waits_for_coroutine_cleanup():
    started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    finished = threading.Event()
    failures = []

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)

    def invoke():
        try:
            correction_runtime.run_async(operation(), timeout=0.05)
        except concurrent.futures.TimeoutError:
            pass
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert not finished.is_set()
    release_cleanup.set()
    assert finished.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    worker.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    assert failures == []

def test_ensure_loop_hands_concurrent_callers_one_running_loop(monkeypatch):
    prior_loop = correction_runtime._loop
    prior_thread = correction_runtime._loop_thread
    prior_running = correction_runtime._loop_running.is_set()
    before = {
        t for t in threading.enumerate() if t.name == "jasper-correction-loop"
    }
    correction_runtime._loop = None
    correction_runtime._loop_thread = None
    correction_runtime._loop_running.clear()

    open_the_gate = threading.Event()
    at_the_gate = threading.Semaphore(0)
    built: list = []
    real_run_loop = correction_runtime._run_loop

    def gated_run_loop(loop, running):
        built.append(loop)
        at_the_gate.release()
        open_the_gate.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        real_run_loop(loop, running)

    monkeypatch.setattr(correction_runtime, "_run_loop", gated_run_loop)

    workers = 8
    seen: list = []
    seen_lock = threading.Lock()

    def call_ensure_loop():
        loop = correction_runtime.ensure_loop()
        with seen_lock:
            seen.append(loop)

    threads = [
        threading.Thread(target=call_ensure_loop, daemon=True)
        for _ in range(workers)
    ]
    try:
        threads[0].start()
        assert at_the_gate.acquire(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        for thread in threads[1:]:
            thread.start()
        assert not at_the_gate.acquire(timeout=0.2), (
            "a caller inside the startup window started a second loop"
        )
        open_the_gate.set()
        for thread in threads:
            thread.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
            assert not thread.is_alive()
        assert len(built) == 1
        assert len(seen) == workers
        assert all(loop is seen[0] for loop in seen)
        assert seen[0].is_running()
        started = {
            t for t in threading.enumerate() if t.name == "jasper-correction-loop"
        } - before
        assert len(started) == 1
    finally:
        open_the_gate.set()
        correction_runtime._loop = prior_loop
        correction_runtime._loop_thread = prior_thread
        if prior_running:
            correction_runtime._loop_running.set()
        else:
            correction_runtime._loop_running.clear()
        for loop in built:
            loop.call_soon_threadsafe(loop.stop)
        for thread in threading.enumerate():
            if thread.name == "jasper-correction-loop" and thread not in before:
                thread.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        for loop in built:
            if not loop.is_running():
                loop.close()

def test_run_async_drain_alarm_keeps_owner_fail_closed(monkeypatch):
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    drain_alarm = threading.Event()
    finished = threading.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)

    monkeypatch.setattr(
        correction_runtime,
        "RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S",
        0.01,
    )
    monkeypatch.setattr(
        correction_runtime,
        "log_event",
        lambda _logger, event, **_fields: (
            drain_alarm.set()
            if event == "correction.async_cancel_drain_timeout"
            else None
        ),
    )

    def invoke():
        try:
            correction_runtime.run_async(operation(), timeout=0.01)
        except concurrent.futures.TimeoutError:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        assert cleanup_started.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert drain_alarm.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
        assert not finished.is_set()
    finally:
        release_cleanup.set()
    assert finished.wait(timeout=DEFAULT_SIGNAL_TIMEOUT_S)
    worker.join(timeout=DEFAULT_SIGNAL_TIMEOUT_S)

def test_read_json_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_runtime.BadRequest, match="Content-Length"):
        correction_runtime.read_json_body(Handler())

def test_read_wav_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_runtime.BadRequest, match="Content-Length"):
        correction_runtime.read_wav_body(Handler())

def test_read_wav_body_rejects_large_or_incomplete_body():
    class TooLarge:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"12345")

    with pytest.raises(correction_runtime.BadRequest, match="too large"):
        correction_runtime.read_wav_body(TooLarge(), max_bytes=4)

    class Incomplete:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"123")

    with pytest.raises(correction_runtime.BadRequest, match="incomplete"):
        correction_runtime.read_wav_body(Incomplete())

def _post_with_csrf(base: str, path: str, data: bytes, **kwargs):
    kwargs.setdefault("session", make_csrf_session(base, page_path="/sync"))
    return request_with_csrf(base, path, data, **kwargs)

def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"

def test_get_serves_the_speaker_timing_page_on_the_manifest_label():
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/sync", timeout=5)
        assert resp.status == 200
        body = resp.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert "<title>Speaker timing</title>" in body
    assert '<h1 class="app-header__title">Speaker timing</h1>' in body
    assert 'href="/sound/pair/"' in body
    assert "/assets/sync/sync.css?v=" in body

def test_e2e_unknown_path_404s():
    server, base = _start_server()
    try:
        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("expected 404 for unknown path")
    finally:
        server.shutdown()
        server.server_close()

def test_e2e_invalid_json_returns_400():
    server, base = _start_server()
    try:
        _post_with_csrf(
            base,
            "/crossover/v2/position-ready",
            b"{not json",
            content_type="application/json",
            expect_status=400,
        )
    finally:
        server.shutdown()
        server.server_close()

def _stored_umik2(tmp_path, *, serial="810-8494"):
    """Establish a UMIK-2 calibration and remember it as the household mic."""
    from jasper.audio_measurement import calibration
    from jasper.audio_measurement.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial=serial,
        root=tmp_path / "cal",
    )
    write_household_mic(
        household_mic_from_calibration(record, serial=serial),
        path=tmp_path / "household_mic.json",
    )
    return record

def _setup_reference(record, *, model="minidsp_umik2"):
    """The reference shape the measurement source mints from the record."""
    return {
        "calibration": {
            "mode": "stored",
            "calibration_id": record.calibration_id,
            "model": model,
        },
    }

def test_setup_reference_resolves_the_remembered_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )

    record = _stored_umik2(tmp_path)
    resolved = v2evidence.resolve_setup_calibration(_setup_reference(record), None)
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id

def test_setup_reference_resolves_an_uploaded_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    from jasper.audio_measurement import calibration
    from jasper.audio_measurement.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = calibration.store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "cal",
    )
    write_household_mic(
        household_mic_from_calibration(record),
        path=tmp_path / "household_mic.json",
    )
    resolved = v2evidence.resolve_setup_calibration(
        _setup_reference(record, model="other"), None,
    )
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id

@pytest.mark.parametrize(
    ("device", "expect_applied"),
    (
        ({"label": "iMM-6C"}, False),          # a DIFFERENT registered model
        ({"label": "UMIK-2 (2752:002b)"}, True),
        ({"label": "Some Unbranded Capture"}, True),  # nothing to contradict
        ({}, True),                            # no label reported
        (None, True),                          # no device offered at all
    ),
)
def test_setup_reference_refuses_a_different_mic(
    tmp_path, monkeypatch, device, expect_applied,
):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    record = _stored_umik2(tmp_path)
    before = household_path.read_text()

    resolved = v2evidence.resolve_setup_calibration(_setup_reference(record), device)

    if expect_applied:
        assert resolved is not None
        assert resolved.calibration_id == record.calibration_id
    else:
        assert resolved is None
    assert household_path.read_text() == before  # never re-persisted either way

def test_setup_reference_mismatch_is_journalled(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )
    caplog.set_level(logging.WARNING, logger="jasper.audio_measurement.household_mic")

    record = _stored_umik2(tmp_path)
    v2evidence.resolve_setup_calibration(
        _setup_reference(record), {"label": "iMM-6C", "device_id": "dayton"},
    )
    assert "event=correction.calibration_device_identity_mismatch" in caplog.text
    assert "stored_model=minidsp_umik2" in caplog.text

def test_setup_reference_without_a_calibration_resolves_to_nothing(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )

    assert v2evidence.resolve_setup_calibration(None, None) is None
    assert v2evidence.resolve_setup_calibration({}, None) is None
    assert v2evidence.resolve_setup_calibration({"calibration": {"mode": "none"}}, None) \
        is None

def test_a_stale_setup_reference_is_a_named_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    with pytest.raises(ValueError, match="no longer available"):
        v2evidence.resolve_setup_calibration(
            {
                "calibration": {
                    "mode": "stored",
                    "calibration_id": "does-not-exist",
                    "model": "minidsp_umik2",
                },
            },
            None,
        )
    assert not household_path.exists()  # no write on a resolution miss

def test_a_setup_reference_without_an_id_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "cal"))
    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "household_mic.json"),
    )

    with pytest.raises(ValueError, match="calibration_id is required"):
        v2evidence.resolve_setup_calibration(
            {"calibration": {"mode": "stored", "model": "minidsp_umik2"}}, None,
        )

def test_default_setup_calibration_for_spec_present_and_absent(tmp_path, monkeypatch):
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    assert correction_capture._default_setup_calibration_for_spec() is None

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.audio_measurement.household_mic import (
        household_mic_from_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )

    hint = correction_capture._default_setup_calibration_for_spec()
    assert hint is not None
    assert hint.mode == "serial"
    assert hint.model == "minidsp_umik2"
    assert hint.serial_display == "8494"
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is True

def test_default_setup_calibration_for_spec_resolvable_is_a_fresh_check(
    tmp_path, monkeypatch,
):
    cal_root = tmp_path / "cal"
    household_path = tmp_path / "household_mic.json"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_root))
    monkeypatch.setenv("JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(household_path))

    from jasper.audio_measurement.calibration import store_calibration
    from jasper.audio_measurement import household_mic
    from jasper.audio_measurement.household_mic import (
        household_mic_from_calibration,
        resolve_household_mic_calibration,
        write_household_mic,
    )

    record = store_calibration(
        text="20 -1\n100 0\n1000 1\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="miniDSP UMIK-2",
        source="https://vendor.example/cal.txt",
        serial="810-8494",
        root=cal_root,
    )
    write_household_mic(
        household_mic_from_calibration(record, serial="810-8494"),
        path=household_path,
    )

    calls = []

    def flaky_resolve(household, *, root=None):
        calls.append(household)
        if len(calls) == 1:
            return resolve_household_mic_calibration(household, root=root)
        return None

    monkeypatch.setattr(household_mic, "resolve_household_mic_calibration", flaky_resolve)

    hint = correction_capture._default_setup_calibration_for_spec()
    assert hint is not None  # the hint itself still ships
    assert hint.calibration_id == record.calibration_id
    assert hint.resolvable is False  # but the one-tap confirm is not offered
    assert len(calls) == 2

def test_e2e_correction_posts_require_csrf():
    server, base = _start_server()
    try:
        req = urllib.request.Request(
            f"{base}/crossover/reset",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("expected HTTP 403")
    finally:
        server.shutdown()
        server.server_close()

def test_sync_analyze_rejects_oversized_capture_before_body_read():
    handler_cls = correction_setup._make_handler_class(
        hostname="jts.local", idle_hold=nullcontext,
    )
    handler = handler_cls.__new__(handler_cls)
    handler.path = "/sync/analyze"
    handler.headers = Message()
    handler.headers["Content-Length"] = str(2 * 1024 * 1024 + 1)
    handler.rfile = io.BytesIO(b"")
    sent: dict = {}

    def _send_json(payload, status=200):
        sent["payload"] = payload
        sent["status"] = int(status)

    handler._send_json = _send_json

    correction_setup._dispatch_sync(handler)

    assert sent["status"] == 400
    assert "WAV body too large" in sent["payload"]["error"]


@pytest.mark.parametrize("code", [None, "unknown_refusal", "seat_anchor_unusable"])
@pytest.mark.parametrize("explicit", [False, True])
def test_refusal_envelope_preserves_codes_and_classifies_at_most_once(code, explicit, monkeypatch):
    from jasper.active_speaker import program_failure  # lazy: numpy import cost

    classified = []
    classify = program_failure.classify_program_failure

    def once(exc):
        classified.append(exc)
        return classify(exc)

    monkeypatch.setattr(program_failure, "classify_program_failure", once)
    exc = ValueError("request")
    if code is not None:
        exc.code = code
    body = (refusal_envelope(code=code, message="request") if explicit
            else refusal_envelope(exc))
    assert len(classified) == (0 if explicit or code else 1)
    assert body["ok"] is False
    assert body["code"] == code
    assert set(body) == {"ok", "code", "next_action", "error"}
    assert isinstance(body["error"], str)
    if code == "seat_anchor_unusable":
        assert body["next_action"]["id"] == "measure_seat_level"
    else:
        assert body["next_action"] is None


def test_refusal_envelope_carries_an_independent_resolution_action():
    """The response action is a copy of the refusal's registry action."""

    from jasper.active_speaker.crossover_v2.refusal_copy import (
        CrossoverV2Refused, REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID, REASON_REGISTRY,
    )

    refused = CrossoverV2Refused(
        "copy", code=REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID,
    )
    assert refusal_envelope(refused)["next_action"] == (
        REASON_REGISTRY[REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID].next_action
    )
    # Mutating the response body must not reach back into the registry.
    action = refusal_envelope(refused)["next_action"]
    assert action is not None
    action["href"] = "/tampered/"
    assert REASON_REGISTRY[REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID].next_action[
        "href"
    ] == "/sound/speaker/#driver-safety-issues"

    assert refusal_envelope(CrossoverV2Refused("no code"))["next_action"] is None
    assert refusal_envelope(ValueError("not ours"))["next_action"] is None
