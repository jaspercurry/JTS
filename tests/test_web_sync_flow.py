# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Handler-level tests for the acoustic-sync apply path.

The signal/analysis math is covered by test_multiroom_sync_measure.py; this
file pins the /sync/apply -> /grouping/set wiring, in particular that the
browser's X-JTS-Token is forwarded so the leader's token-gated /grouping/set
write isn't 403'd by the mandatory control-token gate (WS1 Phase 2).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from types import SimpleNamespace

import pytest

import jasper.correction.coordinator as coordinator
import jasper.multiroom.state as mstate
from jasper.web import rooms_setup as rooms
from jasper.web import active_speaker_flow, sync_flow


LEADER_G = {
    "enabled": True,
    "role": "leader",
    "channel": "left",
    "bond_id": "bond-x",
    "leader_addr": "",
    "peer_addr": "192.168.1.92",
    "peer_name": "jts3",
    "trim_db": 0.0,
}
PEER_G = {
    "enabled": True,
    "role": "follower",
    "channel": "right",
    "bond_id": "bond-x",
    "leader_addr": "jts.local",
    "trim_db": -1.25,
}


class FakeProc:
    def __init__(self) -> None:
        self._done = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self.terminated = False
        self.wait_count = 0

    def terminate(self) -> None:
        self.terminated = True
        self._loop.call_soon_threadsafe(self._done.set)

    async def wait(self) -> int:
        self.wait_count += 1
        await self._done.wait()
        return 0


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop

    async def _cancel_pending() -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks(loop)
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(_cancel_pending(), loop).result(timeout=2)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _reset_sync_state():
    sync_flow.handle_stop()
    yield
    sync_flow.handle_stop()


@pytest.fixture
def sync_env(loop_thread, monkeypatch):
    calls = {
        "window_open": 0,
        "window_closed": 0,
        "window_mode": "ok",
        "futures": [],
        "procs": [],
    }
    monkeypatch.setattr(mstate, "read_grouping_state", lambda: dict(LEADER_G))
    monkeypatch.setattr(rooms, "self_addresses", lambda: {"192.168.1.74"})
    monkeypatch.setattr(
        rooms,
        "discover_speakers_cached",
        lambda: [
            {"address": "192.168.1.92", "name": "jts3", "hostname": "jts3"}
        ],
    )
    monkeypatch.setattr(
        rooms,
        "_get_member_grouping",
        lambda _addr, _known=None: dict(PEER_G),
    )
    monkeypatch.setattr(active_speaker_flow, "active_phase", lambda: None)

    @asynccontextmanager
    async def fake_window():
        calls["window_open"] += 1
        if calls["window_mode"] == "fail":
            raise RuntimeError("window refused")
        try:
            yield
        finally:
            calls["window_closed"] += 1

    monkeypatch.setattr(coordinator, "measurement_window", fake_window)

    async def fake_spawn(_wav_path: str):
        proc = FakeProc()
        calls["procs"].append(proc)
        return proc

    monkeypatch.setattr(sync_flow, "_start_playback", fake_spawn)
    monkeypatch.setattr(sync_flow, "_marker_wav_path", lambda: "/tmp/marker.wav")

    def schedule(coro):
        future = asyncio.run_coroutine_threadsafe(coro, loop_thread)
        calls["futures"].append(future)
        return future

    def run_async(coro, *, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, loop_thread).result(timeout)

    calls["schedule"] = schedule
    calls["run_async"] = run_async
    try:
        yield calls
    finally:
        sync_flow.handle_stop()
        for proc in calls["procs"]:
            proc.terminate()
        deadline = time.monotonic() + 2.0
        for future in calls["futures"]:
            try:
                future.result(timeout=max(0.01, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError:
                future.cancel()
            except (concurrent.futures.CancelledError, RuntimeError):
                pass


class FakeHandler:
    """Carries the optional X-JTS-Token the apply path forwards."""

    def __init__(self, *, token: str | None = None):
        self.headers = {}
        if token is not None:
            self.headers["X-JTS-Token"] = token
        self.rfile = io.BytesIO(b"{}")


SELF_G = {
    "role": "leader",
    "channel": "left",
    "bond_id": "bond-x",
    "leader_addr": "",
}


def start_ok(sync_env) -> dict:
    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.OK, payload
    return payload


def test_start_rejects_unbonded_follower_ambiguous_and_bad_channels(
    sync_env,
    monkeypatch,
):
    monkeypatch.setattr(mstate, "read_grouping_state", lambda: {"enabled": False})
    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.CONFLICT
    assert "bond a pair" in payload["error"]

    monkeypatch.setattr(
        mstate,
        "read_grouping_state",
        lambda: {**LEADER_G, "role": "follower"},
    )
    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.CONFLICT
    assert "leader" in payload["error"]

    legacy = dict(LEADER_G)
    legacy.pop("peer_addr")
    legacy.pop("peer_name")
    monkeypatch.setattr(mstate, "read_grouping_state", lambda: legacy)
    monkeypatch.setattr(
        rooms,
        "discover_speakers_cached",
        lambda: [
            {"address": "192.168.1.92", "name": "jts3"},
            {"address": "192.168.1.93", "name": "jts4"},
        ],
    )
    monkeypatch.setattr(rooms, "_map_peers", lambda fn, addrs: [fn(a) for a in addrs])
    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.CONFLICT
    assert "found 2" in payload["error"]

    monkeypatch.setattr(mstate, "read_grouping_state", lambda: dict(LEADER_G))
    monkeypatch.setattr(
        rooms,
        "_get_member_grouping",
        lambda _addr, _known=None: {**PEER_G, "channel": "left"},
    )
    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.CONFLICT
    assert "one left + one right" in payload["error"]
    assert sync_env["window_open"] == 0


def test_start_rejects_active_commissioning(sync_env, monkeypatch):
    monkeypatch.setattr(active_speaker_flow, "active_phase", lambda: "commissioning")

    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])

    assert status == HTTPStatus.CONFLICT
    assert "active-speaker commissioning" in payload["error"]
    assert sync_env["window_open"] == 0


def test_start_holds_one_window_reports_public_status_and_rejects_double_start(
    sync_env,
):
    payload = start_ok(sync_env)

    assert payload["members"]["left"] == {
        "label": "this speaker (jts.local)",
        "is_self": True,
        "trim_db": 0.0,
    }
    assert payload["members"]["right"] == {
        "label": "jts3",
        "is_self": False,
        "trim_db": -1.2,
    }
    assert sync_flow.active_phase() == "measuring"
    status_payload = sync_flow.handle_status()
    assert status_payload == {
        "phase": "measuring",
        "error": "",
        "members": payload["members"],
        "result": None,
        "recommendation": None,
        "playing": False,
    }
    assert sync_env["window_open"] == 1
    assert sync_env["window_closed"] == 0

    second, status = sync_flow.handle_start("jts.local", sync_env["schedule"])
    assert status == HTTPStatus.CONFLICT
    assert "already running" in second["error"]
    assert sync_env["window_open"] == 1


def test_start_surfaces_window_entry_failure(sync_env):
    sync_env["window_mode"] = "fail"

    payload, status = sync_flow.handle_start("jts.local", sync_env["schedule"])

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "window refused" in payload["error"]
    assert sync_flow.active_phase() is None
    assert sync_flow.handle_status()["phase"] == "idle"


def test_start_surfaces_scheduler_failure(sync_env):
    def fail_schedule(_coro):
        raise RuntimeError("loop stopped")

    payload, status = sync_flow.handle_start("jts.local", fail_schedule)

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload["error"] == "could not start the measurement window"
    assert sync_flow.active_phase() is None
    assert sync_flow.handle_status()["phase"] == "idle"
    assert "loop stopped" in sync_flow.handle_status()["error"]


def test_start_timeout_cancels_future_and_late_window_cannot_open(
    sync_env,
    monkeypatch,
):
    scheduled: list[object] = []

    class DeferredFuture:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    future = DeferredFuture()

    def defer(coro):
        scheduled.append(coro)
        return future

    monkeypatch.setattr(sync_flow, "WINDOW_OPEN_TIMEOUT_S", 0.01)
    payload, status = sync_flow.handle_start("jts.local", defer)

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "could not pause" in payload["error"]
    assert future.cancelled is True
    assert sync_flow.handle_status()["phase"] == "idle"
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert sync_env["window_open"] == 0
    assert sync_flow.handle_status()["phase"] == "idle"


def test_stale_window_exit_failure_cannot_reset_replacement_session(monkeypatch):
    exit_started = asyncio.Event()
    new_releases: list[bool] = []

    @asynccontextmanager
    async def fail_on_exit():
        try:
            yield
        finally:
            exit_started.set()
            raise RuntimeError("old window exit failed")

    monkeypatch.setattr(coordinator, "measurement_window", fail_on_exit)

    async def run() -> None:
        entered = threading.Event()
        with sync_flow._lock:
            sync_flow._reset_locked()
            sync_flow._state["phase"] = "measuring"
            session_token = int(sync_flow._state["session_token"])
        task = asyncio.create_task(sync_flow._session_window(session_token, entered))
        assert await asyncio.to_thread(entered.wait, 1)

        sync_flow.handle_stop()
        with sync_flow._lock:
            sync_flow._state.update(
                phase="measuring",
                error="",
                release_window=lambda: new_releases.append(True),
            )
        await asyncio.wait_for(exit_started.wait(), timeout=1)
        await task

    asyncio.run(run())

    assert new_releases == []
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["error"] == ""


def test_same_session_window_exit_failure_resets_analyzed_session(monkeypatch):
    @asynccontextmanager
    async def fail_on_exit():
        try:
            yield
        finally:
            raise RuntimeError("window restore failed")

    monkeypatch.setattr(coordinator, "measurement_window", fail_on_exit)

    async def run() -> None:
        entered = threading.Event()
        with sync_flow._lock:
            sync_flow._reset_locked()
            sync_flow._state["phase"] = "measuring"
            session_token = int(sync_flow._state["session_token"])
        task = asyncio.create_task(sync_flow._session_window(session_token, entered))
        assert await asyncio.to_thread(entered.wait, 1)
        with sync_flow._lock:
            sync_flow._state["phase"] = "analyzed"
            release = sync_flow._state["release_window"]
        release()
        await task

    asyncio.run(run())

    status = sync_flow.handle_status()
    assert status["phase"] == "idle"
    assert status["result"] is None
    assert status["recommendation"] is None
    assert status["error"] == "measurement window failed: window restore failed"


def test_analyze_gates_errors_retry_and_success(sync_env, monkeypatch):
    from jasper.multiroom import sync_measure

    payload, status = sync_flow.handle_analyze(b"wav")
    assert status == HTTPStatus.CONFLICT
    assert "no active" in payload["error"]

    start_ok(sync_env)
    monkeypatch.setattr(
        sync_measure,
        "analyze_wav_bytes",
        lambda _wav: (_ for _ in ()).throw(ValueError("bad WAV")),
    )
    payload, status = sync_flow.handle_analyze(b"bad")
    assert status == HTTPStatus.BAD_REQUEST
    assert payload["error"] == "bad WAV"
    assert sync_flow.active_phase() == "measuring"

    retry = SimpleNamespace(
        ok=False,
        delta_ms=0.5,
        confidence=0.2,
        to_dict=lambda: {"ok": False, "warnings": ["low_confidence"]},
    )
    monkeypatch.setattr(sync_measure, "analyze_wav_bytes", lambda _wav: retry)
    monkeypatch.setattr(
        sync_measure,
        "recommend_channel_delays",
        lambda _delta: SimpleNamespace(
            to_dict=lambda: {"left_delay_ms": 0.5, "right_delay_ms": 0.0}
        ),
    )
    payload, status = sync_flow.handle_analyze(b"retry")
    assert status == HTTPStatus.OK
    assert payload["ok"] is False
    assert sync_flow.active_phase() == "measuring"
    assert sync_env["window_closed"] == 0

    good = SimpleNamespace(
        ok=True,
        delta_ms=-1.25,
        confidence=0.9,
        to_dict=lambda: {"ok": True, "delta_ms": -1.25, "confidence": 0.9},
    )
    monkeypatch.setattr(sync_measure, "analyze_wav_bytes", lambda _wav: good)
    monkeypatch.setattr(
        sync_measure,
        "recommend_channel_delays",
        lambda _delta: SimpleNamespace(
            to_dict=lambda: {"left_delay_ms": 0.0, "right_delay_ms": 1.25}
        ),
    )
    payload, status = sync_flow.handle_analyze(b"good")
    assert status == HTTPStatus.OK
    assert payload["ok"] is True
    assert sync_flow.active_phase() is None
    assert sync_flow.handle_status()["phase"] == "analyzed"
    deadline = time.monotonic() + 1.0
    while sync_env["window_closed"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sync_env["window_closed"] == 1


def test_stop_terminates_playback_releases_window_and_watcher_finishes(sync_env):
    start_ok(sync_env)
    payload, status = sync_flow.handle_play(
        sync_env["run_async"],
        sync_env["schedule"],
    )
    assert status == HTTPStatus.OK
    assert payload == {"ok": True}
    assert sync_flow.handle_status()["playing"] is True

    stop_payload, stop_status = sync_flow.handle_stop()

    assert stop_status == HTTPStatus.OK
    assert stop_payload == {"ok": True}
    assert sync_env["procs"][0].terminated is True
    deadline = time.monotonic() + 1.0
    while sync_env["window_closed"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sync_env["window_closed"] == 1
    for future in sync_env["futures"]:
        future.result(timeout=1)
    assert sync_flow.active_phase() is None
    assert sync_flow.handle_status() == {
        "phase": "idle",
        "error": "",
        "members": None,
        "result": None,
        "recommendation": None,
        "playing": False,
    }


def test_play_watcher_schedule_failure_terminates_reaps_and_clears(
    sync_env,
    caplog,
):
    start_ok(sync_env)

    def fail_schedule(_coro):
        raise RuntimeError("loop rejected watcher")

    with caplog.at_level(logging.ERROR, logger="jasper.web.sync"):
        payload, status = sync_flow.handle_play(
            sync_env["run_async"],
            fail_schedule,
        )

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload == {
        "ok": False,
        "error": "could not monitor marker playback",
    }
    assert len(sync_env["procs"]) == 1
    proc = sync_env["procs"][0]
    assert proc.terminated is True
    assert proc.wait_count == 1
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["playing"] is False
    events = [
        record.getMessage()
        for record in caplog.records
        if "event=sync.play_watch_schedule_failed" in record.getMessage()
    ]
    assert len(events) == 1
    assert "reaped=true" in events[0]


def test_stale_play_spawn_cannot_attach_to_new_session(monkeypatch):
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state["phase"] = "measuring"

    terminated: list[bool] = []

    class Proc:
        def terminate(self):
            terminated.append(True)

    def run_async(coro, *, timeout):
        coro.close()
        with sync_flow._lock:
            sync_flow._reset_locked()
            sync_flow._state["phase"] = "measuring"
        return Proc()

    monkeypatch.setattr(sync_flow, "_marker_wav_path", lambda: "/tmp/marker.wav")
    scheduled: list[object] = []

    payload, status = sync_flow.handle_play(run_async, scheduled.append)

    assert status == HTTPStatus.CONFLICT
    assert "session changed" in payload["error"]
    assert terminated == [True]
    assert scheduled == []
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["playing"] is False


def test_relay_marker_post_spawn_stale_cleans_only_own_process(monkeypatch):
    class Proc:
        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    old_proc = Proc()
    new_proc = Proc()
    spawned = asyncio.Event()
    finish_spawn = asyncio.Event()

    async def delayed_spawn(_wav_path):
        spawned.set()
        await finish_spawn.wait()
        return old_proc

    monkeypatch.setattr(sync_flow, "_start_playback", delayed_spawn)

    async def run() -> None:
        with sync_flow._lock:
            sync_flow._reset_locked()
            sync_flow._state["phase"] = "measuring"
            session_token = int(sync_flow._state["session_token"])
        task = asyncio.create_task(
            sync_flow._play_marker_once("/tmp/marker.wav", session_token)
        )
        await asyncio.wait_for(spawned.wait(), timeout=1)
        sync_flow.handle_stop()
        with sync_flow._lock:
            sync_flow._state.update(
                phase="measuring",
                playback={"proc": new_proc},
            )
        finish_spawn.set()
        with pytest.raises(RuntimeError, match="session changed"):
            await task

    asyncio.run(run())

    assert old_proc.terminated is True
    assert new_proc.terminated is False
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["playing"] is True


def test_stale_analysis_cannot_complete_or_release_new_session(monkeypatch):
    from jasper.multiroom import sync_measure

    started = threading.Event()
    finish = threading.Event()
    old_releases: list[bool] = []
    new_releases: list[bool] = []

    result = SimpleNamespace(
        ok=True,
        delta_ms=1.0,
        confidence=0.9,
        to_dict=lambda: {"ok": True, "delta_ms": 1.0},
    )

    def analyze(_wav):
        started.set()
        assert finish.wait(1)
        return result

    monkeypatch.setattr(sync_measure, "analyze_wav_bytes", analyze)
    monkeypatch.setattr(
        sync_measure,
        "recommend_channel_delays",
        lambda _delta: SimpleNamespace(
            to_dict=lambda: {"left_delay_ms": 1.0, "right_delay_ms": 0.0}
        ),
    )
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state.update(
            phase="measuring",
            release_window=lambda: old_releases.append(True),
        )

    outcome: list[tuple[dict, int]] = []
    thread = threading.Thread(
        target=lambda: outcome.append(sync_flow.handle_analyze(b"wav"))
    )
    thread.start()
    assert started.wait(1)
    sync_flow.handle_stop()
    with sync_flow._lock:
        sync_flow._state.update(
            phase="measuring",
            release_window=lambda: new_releases.append(True),
        )
    finish.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcome[0][1] == HTTPStatus.CONFLICT
    assert "session changed" in outcome[0][0]["error"]
    assert old_releases == [True]
    assert new_releases == []
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["result"] is None


def test_apply_owns_session_until_bounded_grouping_write_finishes(
    sync_env,
    monkeypatch,
):
    entered = threading.Event()
    finish = threading.Event()
    writes: list[dict] = []

    def post(_addr, body, _known=None, *, token=None):
        writes.append(dict(body))
        entered.set()
        assert finish.wait(1)
        return True, "HTTP 200"

    monkeypatch.setattr(rooms, "post_grouping_to_member", post)
    monkeypatch.setattr(rooms, "self_addresses", lambda: {"192.168.1.74"})
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state.update(
            phase="analyzed",
            members={
                "left": {
                    "is_self": True,
                    "label": "self",
                    "trim_db": 0.0,
                    "grouping": dict(SELF_G),
                },
                "right": {
                    "is_self": False,
                    "label": "peer",
                    "trim_db": 0.0,
                    "grouping": {},
                },
            },
            recommendation={"left_delay_ms": 0.0, "right_delay_ms": 1.0},
        )
        session_token = int(sync_flow._state["session_token"])

    outcome: list[tuple[dict, int]] = []
    thread = threading.Thread(
        target=lambda: outcome.append(sync_flow.handle_apply(FakeHandler()))
    )
    thread.start()
    assert entered.wait(1)

    assert sync_flow.active_phase() == "applying"
    stop_payload, stop_status = sync_flow.handle_stop()
    assert stop_status == HTTPStatus.CONFLICT
    assert "apply is in progress" in stop_payload["error"]
    start_payload, start_status = sync_flow.handle_start(
        "jts.local",
        sync_env["schedule"],
    )
    assert start_status == HTTPStatus.CONFLICT
    assert "already running" in start_payload["error"]
    with sync_flow._lock:
        assert sync_flow._state["session_token"] == session_token

    finish.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcome[0][1] == HTTPStatus.OK
    assert outcome[0][0]["ok"] is True
    assert writes == [{
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "bond-x",
        "leader_addr": "",
        "left_delay_ms": 0.0,
        "right_delay_ms": 1.0,
    }]
    assert sync_flow.handle_status()["phase"] == "applied"


def test_stale_relay_failure_cannot_reset_new_session(monkeypatch):
    import jasper.capture_relay.session as relay_session

    started = threading.Event()
    finish = threading.Event()
    new_releases: list[bool] = []

    def fail_after_session_changes(*_args, **_kwargs):
        started.set()
        assert finish.wait(1)
        raise RuntimeError("relay failed")

    monkeypatch.setattr(relay_session, "run_capture", fail_after_session_changes)
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state["phase"] = "measuring"
        session_token = int(sync_flow._state["session_token"])

    async def run() -> None:
        task = asyncio.create_task(
            sync_flow.relay_run_and_consume(
                object(),
                object(),
                session_token=session_token,
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        sync_flow.handle_stop()
        with sync_flow._lock:
            sync_flow._state.update(
                phase="measuring",
                release_window=lambda: new_releases.append(True),
            )
        finish.set()
        with pytest.raises(RuntimeError, match="relay failed"):
            await task

    asyncio.run(run())

    assert new_releases == []
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["error"] == ""


def test_stale_successful_relay_cannot_mutate_or_release_new_session(monkeypatch):
    import jasper.capture_relay.session as relay_session
    from jasper.multiroom import sync_measure

    started = threading.Event()
    finish = threading.Event()
    old_releases: list[bool] = []
    new_releases: list[bool] = []

    def succeed_after_session_changes(*_args, **_kwargs):
        started.set()
        assert finish.wait(1)
        return SimpleNamespace(wav=b"old-session-wav")

    result = SimpleNamespace(
        ok=True,
        delta_ms=2.0,
        confidence=0.95,
        to_dict=lambda: {"ok": True, "delta_ms": 2.0},
    )
    monkeypatch.setattr(relay_session, "run_capture", succeed_after_session_changes)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)
    monkeypatch.setattr(sync_measure, "analyze_wav_bytes", lambda _wav: result)
    monkeypatch.setattr(
        sync_measure,
        "recommend_channel_delays",
        lambda _delta: SimpleNamespace(
            to_dict=lambda: {"left_delay_ms": 2.0, "right_delay_ms": 0.0}
        ),
    )
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state.update(
            phase="measuring",
            release_window=lambda: old_releases.append(True),
        )
        session_token = int(sync_flow._state["session_token"])

    async def run() -> None:
        task = asyncio.create_task(
            sync_flow.relay_run_and_consume(
                object(),
                object(),
                session_token=session_token,
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        sync_flow.handle_stop()
        with sync_flow._lock:
            sync_flow._state.update(
                phase="measuring",
                error="",
                result=None,
                recommendation=None,
                release_window=lambda: new_releases.append(True),
            )
        finish.set()
        with pytest.raises(RuntimeError, match="session changed"):
            await task

    asyncio.run(run())

    assert old_releases == [True]
    assert new_releases == []
    assert sync_flow.handle_status()["phase"] == "measuring"
    assert sync_flow.handle_status()["result"] is None
    assert sync_flow.handle_status()["recommendation"] is None


@pytest.fixture
def analyzed(monkeypatch):
    """A sync session parked at phase=analyzed with a recommendation, plus a
    capturing fake for the cross-speaker write. Returns the captured calls."""
    captured: list[dict] = []

    def fake_post(addr, body, known=None, *, token=None):
        captured.append({"addr": addr, "body": dict(body), "token": token})
        return True, "HTTP 200"

    monkeypatch.setattr(rooms, "post_grouping_to_member", fake_post)
    monkeypatch.setattr(rooms, "self_addresses", lambda: {"192.168.1.74"})

    with sync_flow._lock:
        sync_flow._state.update({
            "phase": "analyzed",
            "members": {
                "left": {"is_self": True, "label": "this speaker",
                         "trim_db": 0.0, "grouping": dict(SELF_G)},
                "right": {"is_self": False, "label": "peer",
                          "trim_db": 0.0, "grouping": {}},
            },
            "recommendation": {"left_delay_ms": 0.0, "right_delay_ms": 1.25},
        })
    try:
        yield captured
    finally:
        sync_flow.handle_stop()


def test_apply_forwards_control_token(analyzed):
    payload, status = sync_flow.handle_apply(FakeHandler(token="tok-xyz"))
    assert status == 200 and payload["ok"]
    assert len(analyzed) == 1
    call = analyzed[0]
    assert call["addr"] == ""             # self-only write (the leader)
    assert call["token"] == "tok-xyz"     # the regression: token forwarded
    assert call["body"]["right_delay_ms"] == 1.25
    assert sync_flow.handle_status()["phase"] == "applied"


def test_apply_without_token_passes_none(analyzed):
    """Gate-off speakers send no token; the handler forwards None rather
    than raising, preserving the default-off pass-through."""
    payload, status = sync_flow.handle_apply(FakeHandler())
    assert status == 200 and payload["ok"]
    assert analyzed[0]["token"] is None


def test_apply_rejection_restores_analyzed_for_retry(analyzed, monkeypatch):
    monkeypatch.setattr(
        rooms,
        "post_grouping_to_member",
        lambda *_args, **_kwargs: (False, "HTTP 503"),
    )

    payload, status = sync_flow.handle_apply(FakeHandler())

    assert status == HTTPStatus.BAD_GATEWAY
    assert payload["ok"] is False
    assert payload["detail"] == "HTTP 503"
    assert sync_flow.handle_status()["phase"] == "analyzed"


def test_apply_exception_restores_analyzed_for_retry(analyzed, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("unexpected grouping transport failure")

    monkeypatch.setattr(rooms, "post_grouping_to_member", fail)

    with pytest.raises(RuntimeError, match="unexpected grouping transport failure"):
        sync_flow.handle_apply(FakeHandler())

    assert sync_flow.handle_status()["phase"] == "analyzed"


def test_relay_run_and_consume_failure_releases_window(monkeypatch):
    """A phone-relay sync capture that fails must release the held measurement
    window (renderers/voice come back) and surface an error on /sync/status,
    rather than hang until the 240 s SESSION_MAX_S cap with a silent stuck
    'measuring' session. Pins the resilience fix from the adversarial review."""
    import asyncio

    import jasper.capture_relay.session as relay_session
    from jasper.web import sync_flow

    released = []
    sync_flow._state.update({
        "phase": "measuring", "error": "", "members": None, "result": None,
        "recommendation": None, "playback": None,
        "release_window": lambda: released.append(True),
    })

    def _boom(*_a, **_k):
        raise RuntimeError("relay died mid-capture")

    monkeypatch.setattr(relay_session, "run_capture", _boom)
    session_token = sync_flow._state["session_token"]
    with pytest.raises(RuntimeError):
        asyncio.run(
            sync_flow.relay_run_and_consume(
                object(),
                object(),
                session_token=session_token,
            )
        )

    assert released == [True]  # held window released on failure
    assert sync_flow._state["phase"] == "idle"  # reset, not stuck "measuring"
    assert "failed" in sync_flow._state["error"]  # visible on /sync/status


def test_relay_run_and_consume_publishes_sweep_complete(monkeypatch):
    """Should-fix (P7 review, pre-existing bug): the capture page records until
    it sees `sweep_complete` (or its hard `duration_ms` deadline) — the sync
    relay path never published it, so every sync relay capture died on the
    deadline having uploaded nothing. Pins: sweep_started before the marker,
    sweep_complete only AFTER the playback process exits, both posted to the
    relay client with the pi_session's identifiers, and the analysis still runs
    on the phone's (real-shape WAV) bytes."""
    import asyncio
    import io
    import wave
    from types import SimpleNamespace

    import jasper.capture_relay.session as relay_session
    from jasper.web import sync_flow

    # A real mono 48 kHz WAV (silence): analyze_wav_bytes runs for REAL and
    # returns ok=False (no markers) without raising — the event contract under
    # test is transport-side, not the acoustics.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000 * 3)
    wav_bytes = buf.getvalue()

    with sync_flow._lock:
        sync_flow._state.update({
            "phase": "measuring", "error": "", "members": None, "result": None,
            "recommendation": None, "playback": None, "release_window": None,
        })

    class FakeProc:
        """Stands in for the aplay asyncio subprocess: wait() resolves and
        records that playback had to FINISH before sweep_complete. terminate()
        exists for the teardown handle_stop() → _reset_locked() path."""

        def __init__(self):
            self.waited = False
            self.returncode = 0

        async def wait(self):
            self.waited = True
            return 0

        def terminate(self):
            return None

    proc = FakeProc()

    async def fake_play_marker_once(wav_path, session_token):
        with sync_flow._lock:
            sync_flow._state["playback"] = {"proc": proc}
        return proc

    monkeypatch.setattr(sync_flow, "_play_marker_once", fake_play_marker_once)
    monkeypatch.setattr(sync_flow, "_marker_wav_path", lambda: "/tmp/fake-marker.wav")

    def fake_run_capture(client, pi_session, *, on_armed, **kw):
        on_armed()
        return SimpleNamespace(wav=wav_bytes, device=None)

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "purge", lambda c, s: None)

    events: list[dict] = []

    class FakeClient:
        def post_host_event(self, session_id, pull_token, payload):
            events.append({
                "session_id": session_id,
                "pull_token": pull_token,
                **payload,
            })

    pi_session = SimpleNamespace(session_id="sid-1", pull_token="tok-1")
    session_token = sync_flow._state["session_token"]
    try:
        asyncio.run(
            sync_flow.relay_run_and_consume(
                FakeClient(),
                pi_session,
                session_token=session_token,
            )
        )
    finally:
        sync_flow.handle_stop()

    phases = [e["phase"] for e in events]
    assert phases == ["sweep_started", "sweep_complete"]
    assert proc.waited is True  # complete only after playback truly ended
    assert all(e["session_id"] == "sid-1" and e["pull_token"] == "tok-1"
               for e in events)


def test_handle_play_aborts_and_kills_proc_when_a_concurrent_play_won(
    monkeypatch,
):
    """Two concurrent /sync/play calls must not both spawn overlapping aplay
    markers into one delay measurement. The pre-spawn check released the lock,
    so this pins the post-spawn re-validation: the loser kills its just-spawned
    proc and returns CONFLICT instead of recording a second playback."""
    from http import HTTPStatus

    from jasper.web import sync_flow

    with sync_flow._lock:
        sync_flow._state.update({
            "phase": "measuring", "error": "", "members": None, "result": None,
            "recommendation": None, "playback": None, "release_window": None,
        })

    terminated: list[bool] = []

    class WinnerProc:
        def terminate(self):
            return None

    winner_proc = WinnerProc()

    class LoserProc:
        def terminate(self):
            terminated.append(True)

    def fake_run_async(coro, *, timeout):
        coro.close()  # never actually exec aplay
        # Simulate the concurrent play that won the race between our pre-check
        # and our post-spawn re-check.
        with sync_flow._lock:
            sync_flow._state["playback"] = {"proc": winner_proc}
        return LoserProc()

    monkeypatch.setattr(sync_flow, "_marker_wav_path", lambda: "/tmp/fake.wav")

    scheduled: list[object] = []

    def fake_schedule(coro):
        coro.close()
        scheduled.append(coro)

    try:
        resp, status = sync_flow.handle_play(fake_run_async, fake_schedule)
    finally:
        with sync_flow._lock:
            sync_flow._reset_locked()

    assert status == HTTPStatus.CONFLICT
    assert "already playing" in resp["error"]
    assert terminated == [True]  # loser terminated its just-spawned proc
    assert scheduled == []  # no watcher scheduled for the aborted proc


def test_handle_play_records_playback_on_the_happy_path(monkeypatch):
    """Sanity: with no concurrent winner, handle_play records the spawned proc
    and schedules its watcher — the re-validation is a guard, not a block."""
    from http import HTTPStatus

    from jasper.web import sync_flow

    with sync_flow._lock:
        sync_flow._state.update({
            "phase": "measuring", "error": "", "members": None, "result": None,
            "recommendation": None, "playback": None, "release_window": None,
        })

    terminated: list[bool] = []

    class OkProc:
        def terminate(self):
            terminated.append(True)

    proc = OkProc()

    def fake_run_async(coro, *, timeout):
        coro.close()
        return proc

    monkeypatch.setattr(sync_flow, "_marker_wav_path", lambda: "/tmp/fake.wav")

    scheduled: list[object] = []

    def fake_schedule(coro):
        coro.close()
        scheduled.append(coro)

    try:
        resp, status = sync_flow.handle_play(fake_run_async, fake_schedule)
        assert status == HTTPStatus.OK
        assert resp == {"ok": True}
        with sync_flow._lock:
            assert sync_flow._state["playback"]["proc"] is proc
        assert len(scheduled) == 1  # watcher scheduled
        assert terminated == []  # the happy path must not kill its own proc
    finally:
        with sync_flow._lock:
            sync_flow._reset_locked()
