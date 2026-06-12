"""Pair-balance wizard flow (jasper/web/balance_flow.py).

Drives the handler layer with every external seam faked (grouping
state, peer discovery, measurement window, playback, member POST) and
real WAV bytes through the real analysis core — the synthetic-capture
loop from test_multiroom_balance, one layer up.
"""

import asyncio
import io
import time
from contextlib import asynccontextmanager

import numpy as np
import pytest

import jasper.correction.coordinator as coordinator
import jasper.correction.playback as playback
import jasper.multiroom.state as mstate
import jasper.web.rooms_setup as rooms
from jasper.multiroom.balance import synth_balance_burst
from jasper.web import balance_flow

SR = 48000

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


@pytest.fixture(autouse=True)
def _reset_state():
    balance_flow.handle_reset()
    yield
    balance_flow.handle_reset()


def run_async(coro, *, timeout=None):
    return asyncio.run(coro)


@pytest.fixture
def pair_env(monkeypatch):
    """A healthy bonded pair: self=left leader, jts3=right follower."""
    calls = {"window": 0, "played": [], "posted": []}
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
        calls["window"] += 1
        yield

    async def fake_play(path, *a, **k):
        calls["played"].append(path)

    monkeypatch.setattr(coordinator, "measurement_window", fake_window)
    monkeypatch.setattr(playback, "play_sweep", fake_play)

    def fake_post(addr, body, known=None):
        calls["posted"].append((addr, dict(body)))
        return True, "HTTP 200"

    monkeypatch.setattr(rooms, "_post_grouping_to_member", fake_post)
    return calls


def wav_bytes(samples: np.ndarray, sr: int = SR) -> bytes:
    from scipy.io import wavfile
    buf = io.BytesIO()
    wavfile.write(buf, sr,
                  (np.clip(samples, -1, 1) * 32767).astype(np.int16))
    return buf.getvalue()


def render_capture(schedule, left_db: float, right_db: float,
                   pre_s: float = 0.5) -> np.ndarray:
    sr = schedule.sample_rate
    total = int((pre_s + schedule.total_s + 0.4) * sr)
    out = np.zeros(total)
    burst = synth_balance_burst(sr).astype(np.float64)
    for spec in schedule.bursts:
        gain = left_db if spec.channel == "left" else right_db
        start = int((pre_s + spec.start_s) * sr)
        out[start:start + burst.size] += burst * 10 ** (gain / 20.0)
    out += np.random.default_rng(5).standard_normal(total) * 1e-4
    return out


def play_ok(calls) -> dict:
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 200, payload
    return payload


# ---------------------------------------------------------------------------
# /balance/play gates


def test_play_rejects_when_not_bonded(pair_env, monkeypatch):
    monkeypatch.setattr(
        mstate, "read_grouping_state",
        lambda *a, **k: {"enabled": False})
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 409 and "bond a pair" in payload["error"]


def test_play_rejects_follower(pair_env, monkeypatch):
    g = dict(LEADER_G, role="follower", leader_addr="jts.local")
    monkeypatch.setattr(
        mstate, "read_grouping_state", lambda *a, **k: g)
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 409 and "leader" in payload["error"]


def test_play_rejects_ambiguous_peers(pair_env, monkeypatch):
    monkeypatch.setattr(rooms, "_discover_speakers_cached", lambda: [
        {"address": "192.168.1.92", "name": "jts3", "hostname": "jts3"},
        {"address": "192.168.1.162", "name": "jts4", "hostname": "jts4"},
    ])
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 409 and "found 2" in payload["error"]


def test_play_rejects_same_channel_pair(pair_env, monkeypatch):
    monkeypatch.setattr(
        rooms, "_get_member_grouping",
        lambda a, known=None: dict(PEER_G, channel="left"))
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 409 and "swap" in payload["error"]


def test_play_rejects_while_active(pair_env):
    play_ok(pair_env)  # → awaiting_capture
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 409 and "already" in payload["error"]


# ---------------------------------------------------------------------------
# Happy path: play → upload → apply


def test_play_runs_inside_measurement_window(pair_env):
    payload = play_ok(pair_env)
    assert pair_env["window"] == 1
    assert len(pair_env["played"]) == 1
    assert payload["schedule"]["bursts"][0]["channel"] == "left"
    assert payload["members"]["left"]["is_self"] is True
    assert payload["members"]["right"]["label"] == "jts3"
    assert balance_flow.active_phase() == "awaiting_capture"
    assert balance_flow.handle_status()["phase"] == "awaiting_capture"


def test_play_failure_resets_to_idle(pair_env, monkeypatch):
    async def boom(path, *a, **k):
        raise RuntimeError("aplay exploded")

    monkeypatch.setattr(playback, "play_sweep", boom)
    payload, status = balance_flow.handle_play("jts.local", run_async)
    assert status == 500 and "aplay exploded" in payload["error"]
    assert balance_flow.handle_status()["phase"] == "idle"


def test_upload_without_play_conflicts(pair_env):
    payload, status = balance_flow.handle_upload(b"RIFFnope")
    assert status == 409


def test_upload_rejection_keeps_awaiting(pair_env):
    play_ok(pair_env)
    sched = balance_flow._state["schedule"]
    silence = np.random.default_rng(3).standard_normal(
        int((sched.total_s + 1.0) * SR)) * 1e-4
    payload, status = balance_flow.handle_upload(wav_bytes(silence))
    assert status == 200
    assert payload["rejected"] and not payload["ok"]
    assert payload["result"]["reason"] == "no_alignment"
    assert balance_flow.handle_status()["phase"] == "awaiting_capture"


def test_upload_recommends_member_trims(pair_env):
    play_ok(pair_env)
    sched = balance_flow._state["schedule"]
    capture = render_capture(sched, left_db=-6.0, right_db=-9.0)
    payload, status = balance_flow.handle_upload(wav_bytes(capture))
    assert status == 200 and payload["ok"], payload
    assert payload["result"]["delta_db"] == pytest.approx(3.0, abs=0.3)
    rec = payload["recommendation"]
    assert rec["left_trim_db"] == pytest.approx(-3.0, abs=0.3)
    assert rec["right_trim_db"] == 0.0
    assert not rec["clamped"]
    assert balance_flow.handle_status()["phase"] == "analyzed"


def test_upload_composes_with_existing_trims(pair_env, monkeypatch):
    # Peer (right) already trimmed -2; capture still shows left +1.
    monkeypatch.setattr(
        rooms, "_get_member_grouping",
        lambda a, known=None: dict(PEER_G, trim_db=-2.0))
    play_ok(pair_env)
    sched = balance_flow._state["schedule"]
    capture = render_capture(sched, left_db=-6.0, right_db=-7.0)
    payload, _ = balance_flow.handle_upload(wav_bytes(capture))
    assert payload["ok"]
    rec = payload["recommendation"]
    # left -1; renormalize lifts right's -2 → left -1+? Walk it:
    # left 0-1=-1, right -2 → lift +1 → left 0... no: lift = -max(-1,-2)=1
    # → left 0, right -1. The pair regains a wasted dB.
    assert rec["left_trim_db"] == pytest.approx(0.0, abs=0.3)
    assert rec["right_trim_db"] == pytest.approx(-1.0, abs=0.3)


def test_apply_writes_peer_first_then_self(pair_env):
    play_ok(pair_env)
    sched = balance_flow._state["schedule"]
    balance_flow.handle_upload(
        wav_bytes(render_capture(sched, -6.0, -9.0)))
    payload, status = balance_flow.handle_apply()
    assert status == 200 and payload["ok"]
    addrs = [a for a, _ in pair_env["posted"]]
    assert addrs == ["192.168.1.92", ""]  # peer hop first, self last
    peer_body = pair_env["posted"][0][1]
    self_body = pair_env["posted"][1][1]
    assert peer_body["channel"] == "right"
    assert peer_body["trim_db"] == 0.0
    assert peer_body["bond_id"] == "bond-x"
    assert self_body["channel"] == "left"
    assert self_body["trim_db"] == pytest.approx(-3.0, abs=0.3)
    assert balance_flow.handle_status()["phase"] == "applied"


def test_apply_partial_failure_reports_and_stops(pair_env, monkeypatch):
    play_ok(pair_env)
    sched = balance_flow._state["schedule"]
    balance_flow.handle_upload(
        wav_bytes(render_capture(sched, -6.0, -9.0)))

    def fail_post(addr, body, known=None):
        return False, "connection refused"

    monkeypatch.setattr(rooms, "_post_grouping_to_member", fail_post)
    payload, status = balance_flow.handle_apply()
    assert status == 502 and not payload["ok"]
    assert any(not w["ok"] for w in payload["writes"].values())
    # Stopped at the first failure — never reached the second member.
    assert len(payload["writes"]) == 1
    assert balance_flow.handle_status()["phase"] != "applied"


def test_apply_without_analysis_conflicts(pair_env):
    payload, status = balance_flow.handle_apply()
    assert status == 409


# ---------------------------------------------------------------------------
# Mutual exclusion + expiry


def test_active_phase_expires_after_deadline(pair_env, monkeypatch):
    play_ok(pair_env)
    assert balance_flow.active_phase() == "awaiting_capture"
    monkeypatch.setattr(
        time, "monotonic",
        lambda: balance_flow._state["deadline"] + 1.0)
    assert balance_flow.active_phase() is None
    assert balance_flow.handle_status()["phase"] == "idle"


def test_correction_start_blocked_by_balance(pair_env):
    play_ok(pair_env)
    from jasper.web.correction_setup import _reserve_start_slot
    assert _reserve_start_slot() == "balance:awaiting_capture"


# ---------------------------------------------------------------------------
# Page shell


def test_render_page_links_module_and_csrf():
    html = balance_flow.render_page("tok-123").decode()
    assert '/assets/balance/js/main.js' in html
    assert 'tok-123' in html
    assert "Balance speakers" in html
