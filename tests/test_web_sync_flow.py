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
