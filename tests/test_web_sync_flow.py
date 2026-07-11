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

import io

import pytest

from jasper.web import rooms_setup as rooms
from jasper.web import sync_flow


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


@pytest.fixture
def analyzed(monkeypatch):
    """A sync session parked at phase=analyzed with a recommendation, plus a
    capturing fake for the cross-speaker write. Returns the captured calls."""
    captured: list[dict] = []

    def fake_post(addr, body, known=None, *, token=None):
        captured.append({"addr": addr, "body": dict(body), "token": token})
        return True, "HTTP 200"

    monkeypatch.setattr(rooms, "_post_grouping_to_member", fake_post)
    monkeypatch.setattr(rooms, "_self_addresses", lambda: {"192.168.1.74"})

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
    with pytest.raises(RuntimeError):
        asyncio.run(sync_flow.relay_run_and_consume(object(), object()))

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

    async def fake_play_marker_once(wav_path):
        with sync_flow._lock:
            sync_flow._state["playback"] = {"proc": proc}

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
    try:
        asyncio.run(sync_flow.relay_run_and_consume(FakeClient(), pi_session))
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
