# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair-balance walkthrough flow (jasper/web/balance_flow.py).

Drives the handler layer against a REAL background asyncio loop (the
same shape correction_setup hosts in production) with every external
seam faked: grouping state, peer discovery, the measurement window, a
terminable fake playback process, and the member POST. Lock offsets
are made exact by rewinding the recorded ramp ``t0`` instead of
patching clocks.
"""

import asyncio
import concurrent.futures
import os
import threading
import time
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus

import pytest

import jasper.correction.coordinator as coordinator
import jasper.multiroom.state as mstate
import jasper.web.rooms_setup as rooms
from jasper.multiroom.balance import (
    RAMP_LEAD_IN_S,
    RAMP_RATE_DB_S,
    RAMP_START_DBFS,
)
from jasper.web import balance_flow

LEADER_G = {
    "enabled": True, "role": "leader", "channel": "left",
    "bond_id": "bond-x", "leader_addr": "", "buffer_ms": 400,
    "codec": "flac", "trim_db": 0.0, "error": None,
}
PEER_G = {
    "enabled": True, "role": "follower", "channel": "right",
    "bond_id": "bond-x", "leader_addr": "jts.local",
    "buffer_ms": 400, "codec": "flac", "trim_db": 0.0, "error": None,
}


class FakeProc:
    """Stands in for the aplay subprocess: wait() blocks until
    terminate() (lock/stop path) or finish() (WAV ended naturally —
    the not_heard path)."""

    def __init__(self):
        self._done = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self._loop.call_soon_threadsafe(self._done.set)

    def finish(self):
        self._loop.call_soon_threadsafe(self._done.set)

    async def wait(self):
        await self._done.wait()


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop

    async def _cancel_pending():
        current = asyncio.current_task()
        pending = [
            task for task in asyncio.all_tasks(loop)
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    fut = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
    try:
        fut.result(timeout=2)
    except concurrent.futures.TimeoutError:
        pass
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


@pytest.fixture(autouse=True)
def _reset_state():
    balance_flow.handle_stop()
    yield
    balance_flow.handle_stop()


@pytest.fixture
def pair_env(loop_thread, monkeypatch):
    """A healthy bonded pair plus fakes for every seam; returns the
    call log + the schedule/run_async bridges bound to the loop."""
    calls = {"window_open": 0, "window_closed": 0, "futures": [],
             "spawned": [], "procs": [], "posted": [],
             "volume_normalized": 0, "volume_restored": 0}
    monkeypatch.setattr(
        mstate, "read_grouping_state", lambda *a, **k: dict(LEADER_G))
    monkeypatch.setattr(rooms, "_self_addresses",
                        lambda: {"192.168.1.74"})
    monkeypatch.setattr(rooms, "_discover_speakers_cached", lambda: [
        {"address": "192.168.1.92", "name": "jts3", "hostname": "jts3"},
    ])
    monkeypatch.setattr(
        rooms, "_get_member_grouping",
        lambda a, known=None: dict(PEER_G))
    monkeypatch.setattr(
        rooms, "_map_peers", lambda fn, addrs: [fn(a) for a in addrs])

    @asynccontextmanager
    async def fake_window(**kwargs):
        calls["window_open"] += 1
        try:
            yield
        finally:
            calls["window_closed"] += 1

    monkeypatch.setattr(coordinator, "measurement_window", fake_window)

    class FakeVolumeGuardReport:
        def public_dict(self):
            return {
                "snapshot": {
                    "main_volume_db": -28.0,
                    "snapcast_clients": [],
                },
                "calibration_main_volume_db": -12.0,
                "calibration_snapcast_percent": 100,
            }

    @asynccontextmanager
    async def fake_volume_guard(hostname, members):
        calls["volume_normalized"] += 1
        try:
            yield FakeVolumeGuardReport()
        finally:
            calls["volume_restored"] += 1

    monkeypatch.setattr(balance_flow, "_volume_guard_context",
                        fake_volume_guard)

    async def fake_spawn(wav_path):
        proc = FakeProc()
        calls["spawned"].append(wav_path)
        calls["procs"].append(proc)
        return proc

    monkeypatch.setattr(balance_flow, "_start_playback", fake_spawn)
    # The ramp WAVs are expensive to render and irrelevant here.
    monkeypatch.setattr(
        balance_flow, "_ramp_wav_path", lambda ch: f"/tmp/{ch}.wav")

    def fake_post(addr, body, known=None, *, token=None):
        calls["posted"].append((addr, dict(body), token))
        return True, "HTTP 200"

    monkeypatch.setattr(rooms, "_post_grouping_to_member", fake_post)

    def schedule(coro):
        fut = asyncio.run_coroutine_threadsafe(coro, loop_thread)
        calls["futures"].append(fut)
        return fut

    def run_async(coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(
            coro, loop_thread).result(timeout)

    def drain():
        balance_flow.handle_stop()
        for proc in calls["procs"]:
            proc.terminate()
        deadline = time.monotonic() + 2.0
        for fut in calls["futures"]:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                fut.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                with suppress(concurrent.futures.CancelledError, RuntimeError):
                    fut.result(timeout=0)
            except (concurrent.futures.CancelledError, RuntimeError):
                pass

    calls["schedule"] = schedule
    calls["run_async"] = run_async
    try:
        yield calls
    finally:
        drain()


class FakeHandler:
    """Just enough of BaseHTTPRequestHandler for _read_json plus the
    optional X-JTS-Token the apply path forwards to /grouping/set."""

    def __init__(self, body: bytes = b"{}", *, token: str | None = None):
        import io
        self.headers = {"Content-Length": str(len(body))}
        if token is not None:
            self.headers["X-JTS-Token"] = token
        self.rfile = io.BytesIO(body)


def start_ok(env) -> dict:
    payload, status = balance_flow.handle_start(
        "jts.local", env["schedule"])
    assert status == 200, payload
    return payload


def ramp_ok(env, channel: str) -> dict:
    seed_floor()
    body = f'{{"channel": "{channel}"}}'.encode()
    payload, status = balance_flow.handle_ramp(
        FakeHandler(body), env["run_async"], env["schedule"])
    assert status == 200, payload
    return payload


def lock_at(env, channel: str, offset_s: float) -> tuple[dict, int]:
    """Rewind the live ramp's t0 so the lock arrives at an exact
    offset, then post the lock."""
    with balance_flow._lock:
        balance_flow._state["ramp"]["t0"] = time.monotonic() - offset_s
    body = f'{{"channel": "{channel}"}}'.encode()
    return balance_flow.handle_lock(FakeHandler(body))


def meter_frame(db: float = -70.0) -> tuple[dict, int]:
    body = f'{{"db": {db}}}'.encode()
    return balance_flow.handle_meter(FakeHandler(body))


def seed_floor(db: float = -70.0, count: int = 4) -> None:
    for _ in range(count):
        payload, status = meter_frame(db)
        assert status == 200, payload


def drive_at(offset_s: float) -> float:
    return RAMP_START_DBFS + RAMP_RATE_DB_S * (offset_s - RAMP_LEAD_IN_S)


# ---------------------------------------------------------------------------
# /balance/start gates


def test_start_rejects_when_not_bonded(pair_env, monkeypatch):
    monkeypatch.setattr(
        mstate, "read_grouping_state",
        lambda *a, **k: {"enabled": False})
    payload, status = balance_flow.handle_start(
        "jts.local", pair_env["schedule"])
    assert status == 409 and "bond a pair" in payload["error"]


def test_start_rejects_follower(pair_env, monkeypatch):
    g = dict(LEADER_G, role="follower", leader_addr="jts.local")
    monkeypatch.setattr(
        mstate, "read_grouping_state", lambda *a, **k: g)
    payload, status = balance_flow.handle_start(
        "jts.local", pair_env["schedule"])
    assert status == 409 and "leader" in payload["error"]


def test_start_rejects_ambiguous_peers(pair_env, monkeypatch):
    monkeypatch.setattr(rooms, "_discover_speakers_cached", lambda: [
        {"address": "192.168.1.92", "name": "jts3", "hostname": "jts3"},
        {"address": "192.168.1.162", "name": "jts4", "hostname": "jts4"},
    ])
    payload, status = balance_flow.handle_start(
        "jts.local", pair_env["schedule"])
    assert status == 409 and "found 2" in payload["error"]


def test_start_rejects_same_channel_pair(pair_env, monkeypatch):
    monkeypatch.setattr(
        rooms, "_get_member_grouping",
        lambda a, known=None: dict(PEER_G, channel="left"))
    payload, status = balance_flow.handle_start(
        "jts.local", pair_env["schedule"])
    assert status == 409 and "swap" in payload["error"]


def test_start_opens_one_window_and_rejects_double_start(pair_env):
    payload = start_ok(pair_env)
    assert payload["members"]["left"]["is_self"] is True
    assert payload["members"]["right"]["label"] == "jts3"
    assert pair_env["window_open"] == 1
    assert pair_env["window_closed"] == 0  # held across the session
    assert pair_env["volume_normalized"] == 1
    assert balance_flow.active_phase() == "measuring"
    payload, status = balance_flow.handle_start(
        "jts.local", pair_env["schedule"])
    assert status == 409 and "already" in payload["error"]


# ---------------------------------------------------------------------------
# Ramp + lock


def test_ramp_requires_session_and_valid_channel(pair_env):
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "left"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 409  # no session
    start_ok(pair_env)
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "centre"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 400


def test_ramp_plays_the_channel_wav_once(pair_env):
    start_ok(pair_env)
    payload = ramp_ok(pair_env, "left")
    assert payload["duration_s"] > 20
    assert payload["target_dbfs"] == -55.0
    assert pair_env["spawned"] == ["/tmp/left.wav"]
    assert balance_flow.handle_status()["ramping"] == "left"
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "right"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 409 and "already playing" in payload["error"]


def test_ramp_waits_for_backend_mic_floor(pair_env):
    start_ok(pair_env)
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "left"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 200
    assert payload["ok"] is False
    assert payload["need_floor"] is True
    assert "microphone level frames" in payload["error"]
    assert payload["meter"]["frames"] == 0
    assert pair_env["spawned"] == []


def test_ramp_fails_visibly_when_mic_floor_never_arrives(pair_env):
    start_ok(pair_env)
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "left"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 200 and payload["need_floor"]
    with balance_flow._lock:
        balance_flow._state["floor_wait_started_at"] = (
            time.monotonic() - balance_flow.FLOOR_WAIT_TIMEOUT_S - 0.1
        )

    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "left"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 409
    assert payload["ok"] is False
    assert payload["need_floor"] is False
    assert "microphone" in payload["error"]
    assert pair_env["spawned"] == []
    with balance_flow._lock:
        assert balance_flow._state["floor_wait_started_at"] is None


def test_early_lock_keeps_listening(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    payload, status = lock_at(pair_env, "left", 0.6)  # inside lead-in
    assert status == 200
    assert payload["keep_listening"] and not payload["ok"]
    assert not pair_env["procs"][0].terminated
    assert balance_flow.handle_status()["ramping"] == "left"


def test_lock_mismatched_channel_conflicts(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    payload, status = balance_flow.handle_lock(
        FakeHandler(b'{"channel": "right"}'))
    assert status == 409


def test_full_walkthrough_computes_trims(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    payload, status = lock_at(pair_env, "left", 10.0)
    assert status == 200 and payload["ok"]
    assert pair_env["procs"][0].terminated
    assert payload["phase"] == "measuring"
    assert payload["drive_dbfs"] == pytest.approx(drive_at(10.0), abs=0.1)

    ramp_ok(pair_env, "right")
    payload, status = lock_at(pair_env, "right", 14.0)
    assert status == 200 and payload["ok"]
    assert payload["phase"] == "analyzed"
    rec = payload["recommendation"]
    # Right needed 4 s more ramp = 6 dB more drive → left louder by 6.
    assert rec["delta_db"] == pytest.approx(6.0, abs=0.1)
    assert rec["left_trim_db"] == pytest.approx(-6.0, abs=0.1)
    assert rec["right_trim_db"] == 0.0
    deadline = time.monotonic() + 2.0
    while (pair_env["window_closed"] != 1
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert pair_env["window_closed"] == 1  # renderers restored
    assert balance_flow.active_phase() is None  # analyzed ≠ active
    deadline = time.monotonic() + 2.0
    while (pair_env["volume_restored"] != 1
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert pair_env["volume_restored"] == 1


def test_backend_meter_frames_lock_the_ramp(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    with balance_flow._lock:
        balance_flow._state["ramp"]["t0"] = time.monotonic() - 10.0

    # Two hits over target are not enough; the third backend frame locks.
    for _ in range(2):
        payload, status = meter_frame(-50.0)
        assert status == 200 and not payload["locked"]
    payload, status = meter_frame(-50.0)
    assert status == 200 and payload["locked"]
    assert payload["drive_dbfs"] == pytest.approx(drive_at(10.0), abs=0.1)
    assert pair_env["procs"][0].terminated


def test_lock_in_ceiling_hold_uses_ceiling_drive(pair_env):
    from jasper.multiroom.balance import RAMP_CEIL_DBFS, ramp_duration_s
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    payload, status = lock_at(
        pair_env, "left", ramp_duration_s() - 0.5)
    assert payload["ok"]
    assert payload["drive_dbfs"] == pytest.approx(RAMP_CEIL_DBFS)


def test_ramp_ending_unheard_marks_channel(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    pair_env["procs"][0].finish()  # WAV ended, nobody locked
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        st = balance_flow.handle_status()
        if st["locks"].get("left", {}).get("not_heard"):
            break
        time.sleep(0.02)
    st = balance_flow.handle_status()
    assert st["locks"]["left"] == {"not_heard": True}
    assert st["ramping"] == ""
    # Retry is allowed and clears the failed answer.
    ramp_ok(pair_env, "left")
    assert balance_flow.handle_status()["locks"].get("left") is None


def test_stop_terminates_playback_and_releases_window(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    payload, status = balance_flow.handle_stop()
    assert status == 200
    assert pair_env["procs"][0].terminated
    deadline = time.monotonic() + 2.0
    while (pair_env["window_closed"] != 1
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert pair_env["window_closed"] == 1
    assert balance_flow.handle_status()["phase"] == "idle"


def test_stop_during_ramp_spawn_terminates_unregistered_playback(
    pair_env, monkeypatch,
):
    start_ok(pair_env)
    seed_floor()

    async def stop_during_spawn(wav_path):
        proc = FakeProc()
        pair_env["spawned"].append(wav_path)
        pair_env["procs"].append(proc)
        balance_flow.handle_stop()
        return proc

    monkeypatch.setattr(balance_flow, "_start_playback", stop_during_spawn)
    payload, status = balance_flow.handle_ramp(
        FakeHandler(b'{"channel": "left"}'),
        pair_env["run_async"], pair_env["schedule"])
    assert status == 409
    assert "session" in payload["error"]
    assert pair_env["procs"][0].terminated
    st = balance_flow.handle_status()
    assert st["phase"] == "idle"
    assert st["ramping"] == ""


def test_activity_advances_the_idle_deadline(pair_env):
    """S1: the held window's deadline is inactivity-based — each
    session activity (ramp start, single-channel lock) pushes it out,
    so an active walkthrough is never yanked mid-use. Deterministic:
    monotonic advances between calls, no sleeping."""
    start_ok(pair_env)
    with balance_flow._lock:
        d_start = balance_flow._state["idle_deadline"]
    ramp_ok(pair_env, "left")
    with balance_flow._lock:
        d_ramp = balance_flow._state["idle_deadline"]
    lock_at(pair_env, "left", 10.0)  # first-channel lock, session continues
    with balance_flow._lock:
        d_lock = balance_flow._state["idle_deadline"]
    assert d_ramp > d_start
    assert d_lock > d_ramp
    assert pair_env["window_closed"] == 0  # still held across the bumps


def test_inactivity_releases_the_window(pair_env, monkeypatch):
    """S1: with no activity, the holder releases the window (renderers +
    wake loop restored) within one idle window and resets to idle —
    an abandoned phone tab can't hold the speaker paused indefinitely."""
    monkeypatch.setattr(balance_flow, "IDLE_TIMEOUT_S", 0.2)
    start_ok(pair_env)
    deadline = time.monotonic() + 3.0
    while (pair_env["window_closed"] != 1
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert pair_env["window_closed"] == 1
    st = balance_flow.handle_status()
    assert st["phase"] == "idle"
    assert "timed out" in st["error"]


def test_ramp_wav_path_is_stable_and_bounded(monkeypatch):
    """N4: the per-channel ramp WAV uses a stable name (no unique
    tempfile per process), so socket-activation restarts can't strand
    multi-MB orphans in tmpfs. Same path on repeat; both channels land
    under the temp dir."""
    import tempfile as _tf
    balance_flow._state["wav_paths"].clear()
    try:
        p1 = balance_flow._ramp_wav_path("left")
        p2 = balance_flow._ramp_wav_path("left")
        pr = balance_flow._ramp_wav_path("right")
        assert p1 == p2  # cached, one render per channel per process
        assert p1 != pr
        for p in (p1, pr):
            assert p.startswith(_tf.gettempdir())
            assert os.path.exists(p)
    finally:
        for p in set(balance_flow._state["wav_paths"].values()):
            try:
                os.unlink(p)
            except OSError:
                pass
        balance_flow._state["wav_paths"].clear()


# ---------------------------------------------------------------------------
# Apply (contract unchanged from v1)


def analyzed_state(pair_env):
    start_ok(pair_env)
    ramp_ok(pair_env, "left")
    lock_at(pair_env, "left", 10.0)
    ramp_ok(pair_env, "right")
    lock_at(pair_env, "right", 14.0)


def test_apply_writes_peer_first_then_self(pair_env):
    analyzed_state(pair_env)
    payload, status = balance_flow.handle_apply(FakeHandler(token="tok-abc"))
    assert status == 200 and payload["ok"]
    addrs = [a for a, _b, _t in pair_env["posted"]]
    assert addrs == ["192.168.1.92", ""]  # peer hop first, self last
    peer_body = pair_env["posted"][0][1]
    self_body = pair_env["posted"][1][1]
    assert peer_body["channel"] == "right"
    assert peer_body["trim_db"] == 0.0
    assert peer_body["bond_id"] == "bond-x"
    assert self_body["channel"] == "left"
    assert self_body["trim_db"] == pytest.approx(-6.0, abs=0.1)
    assert balance_flow.handle_status()["phase"] == "applied"


def test_apply_forwards_control_token_to_every_member(pair_env):
    """Regression: /grouping/set is a MANDATORY token-gated mutation, so a
    tokenless write — notably the loopback self-write — is 403'd. Apply
    must forward the browser's X-JTS-Token to BOTH members (the cross-LAN
    peer and self), exactly as the /rooms bond fan-out does."""
    analyzed_state(pair_env)
    payload, status = balance_flow.handle_apply(FakeHandler(token="tok-xyz"))
    assert status == 200 and payload["ok"]
    forwarded = [t for _a, _b, t in pair_env["posted"]]
    assert forwarded == ["tok-xyz", "tok-xyz"]


def test_apply_partial_failure_reports_and_stops(pair_env, monkeypatch):
    analyzed_state(pair_env)

    def fail_post(addr, body, known=None, *, token=None):
        return False, "connection refused"

    monkeypatch.setattr(rooms, "_post_grouping_to_member", fail_post)
    payload, status = balance_flow.handle_apply(FakeHandler())
    assert status == 502 and not payload["ok"]
    assert len(payload["writes"]) == 1  # stopped at the first failure
    assert balance_flow.handle_status()["phase"] != "applied"


def test_apply_without_analysis_conflicts(pair_env):
    payload, status = balance_flow.handle_apply(FakeHandler())
    assert status == HTTPStatus.CONFLICT


# ---------------------------------------------------------------------------
# Mutual exclusion with correction


def test_correction_start_blocked_by_balance(pair_env):
    start_ok(pair_env)
    from jasper.web.correction_setup import _reserve_start_slot
    assert _reserve_start_slot() == "balance:measuring"


# ---------------------------------------------------------------------------
# Page shell


def test_render_page_links_module_and_csrf():
    html = balance_flow.render_page("tok-123").decode()
    assert '/assets/balance/js/main.js' in html
    assert 'tok-123' in html
    assert "Balance speakers" in html
    assert 'id="stop"' in html  # the big red button exists


def test_start_with_roster_survives_foreign_bond_claimer(
    pair_env, monkeypatch,
):
    """THE 2026-06-12 live regression: a foreign endpoint-tier Pi
    transiently claims the bond_id, making inference see two peers.
    With the roster recorded, /balance/start resolves the household's
    actual sibling and proceeds."""
    monkeypatch.setattr(
        mstate, "read_grouping_state",
        lambda *a, **k: dict(LEADER_G, peer_addr="192.168.1.92",
                             peer_name="jts3"))
    monkeypatch.setattr(rooms, "_discover_speakers_cached", lambda: [
        {"address": "192.168.1.92", "name": "jts3", "hostname": "jts3"},
        {"address": "192.168.1.162", "name": "jts4", "hostname": "jts4"},
    ])
    # BOTH candidates claim our bond — inference alone would fail.
    monkeypatch.setattr(
        rooms, "_get_member_grouping",
        lambda a, known=None: dict(PEER_G))
    payload = start_ok(pair_env)
    assert payload["members"]["right"]["label"] == "jts3"
