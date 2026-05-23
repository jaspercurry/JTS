"""Unit tests for jasper.web.peering_setup.

End-to-end HTTP exercises against a real ThreadingHTTPServer on a
random port (same shape as tests/test_control_server.py). State
file lives under tmp_path; restart_voice_daemon / restart_jasper_control
are monkey-patched to no-ops so tests don't touch systemctl.
"""
from __future__ import annotations

import os
import socket
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jasper.web import peering_setup

from ._web_test_helpers import _NoRedirect, post_with_csrf


@pytest.fixture
def server_with_state(tmp_path, monkeypatch):
    """Start the /peers/ wizard on a random port, point it at a tmp
    state file, suppress real systemctl calls."""
    state_path = str(tmp_path / "peering.env")
    monkeypatch.setattr(peering_setup, "restart_voice_daemon", lambda: None)
    monkeypatch.setattr(peering_setup, "_restart_jasper_control", lambda: None)
    # Stub the peer_id reader to a known value so rendered pages are
    # deterministic regardless of whether the test host has run install.sh.
    monkeypatch.setattr(peering_setup, "_peer_id", lambda *a, **kw: "test-peer-uuid")

    # Random port. Bind manually so we can read it back before serving.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = peering_setup.make_server(("127.0.0.1", port), state_path=state_path)
    base_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {"url": base_url, "state_path": state_path, "server": server}
    finally:
        server.shutdown()
        server.server_close()


# ---------- GET / ----------


def test_get_root_renders_off_by_default(server_with_state):
    """Fresh install — no env file, default state is OFF, page renders
    the off card + the toggle unchecked."""
    resp = urllib.request.urlopen(server_with_state["url"] + "/")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    # Status card shows OFF
    assert "OFF" in body
    # Peer ID is rendered
    assert "test-peer-uuid" in body
    # The toggle checkbox exists and is NOT checked
    assert 'name="enabled"' in body
    # No "Discovered peers" section when off
    assert "Discovered peers" not in body


def test_get_root_renders_on_when_state_says_so(server_with_state, monkeypatch):
    """JASPER_PEERING=on in the state file → page shows ON status,
    toggle checked. Discovered-peers section appears (may be empty
    since no daemon is running)."""
    with open(server_with_state["state_path"], "w") as f:
        f.write("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    # No peering daemon → STATUS returns None → empty peer list.
    monkeypatch.setattr(peering_setup, "_fetch_peer_status", lambda **kw: None)
    resp = urllib.request.urlopen(server_with_state["url"] + "/")
    body = resp.read().decode("utf-8")
    assert "ON" in body
    assert "kitchen" in body
    assert "Discovered peers" in body
    assert "No sibling peers visible yet" in body


def test_get_root_renders_visible_peers(server_with_state, monkeypatch):
    with open(server_with_state["state_path"], "w") as f:
        f.write("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    # Inject a fake peer list as if the daemon was running.
    monkeypatch.setattr(
        peering_setup, "_fetch_peer_status",
        lambda **kw: {
            "peers": [
                {
                    "peer_id": "bob-uuid",
                    "room": "bedroom",
                    "primary": True,
                    "address": "192.168.1.42",
                },
                {
                    "peer_id": "test-peer-uuid",  # self — filtered
                    "room": "kitchen",
                    "primary": False,
                    "address": "192.168.1.10",
                },
            ],
        },
    )
    resp = urllib.request.urlopen(server_with_state["url"] + "/")
    body = resp.read().decode("utf-8")
    assert "bedroom" in body
    # Bob shown with primary badge + short id + addr
    assert "bob-uuid"[:8] in body
    assert "192.168.1.42" in body
    assert "primary" in body
    # Self filtered out
    assert "192.168.1.10" not in body


# ---------- POST /save ----------


def test_save_enables_peering(server_with_state):
    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "kitchen"},
    )
    content = open(server_with_state["state_path"]).read()
    assert "JASPER_PEERING=on" in content
    assert "JASPER_PEER_ROOM=kitchen" in content


def test_save_disables_peering(server_with_state):
    # Pre-populate as on.
    with open(server_with_state["state_path"], "w") as f:
        f.write("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    # Note: no `enabled` key in form → checkbox unchecked → off
    post_with_csrf(
        server_with_state["url"], "/save",
        {"room": "kitchen"},
    )
    content = open(server_with_state["state_path"]).read()
    assert "JASPER_PEERING=off" in content


def test_save_sanitizes_room_name(server_with_state):
    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "Living Room!!! @#$%"},
    )
    content = open(server_with_state["state_path"]).read()
    # Spaces become dashes; punctuation stripped.
    assert "JASPER_PEER_ROOM=Living-Room" in content


def test_save_preserves_operator_tuning_knobs(server_with_state):
    """Operators can manually set JASPER_PEER_ARB_WINDOW_MS /
    JASPER_PEER_BREAK_THRESHOLD in peering.env to tune the protocol.
    A wizard save (toggle, room, primary) must NOT wipe those values
    — that'd silently reset careful tuning."""
    # Pre-populate with operator tuning + an enabled state.
    with open(server_with_state["state_path"], "w") as f:
        f.write(
            "JASPER_PEERING=on\n"
            "JASPER_PEER_ROOM=kitchen\n"
            "JASPER_PEER_ARB_WINDOW_MS=200\n"
            "JASPER_PEER_BREAK_THRESHOLD=0.92\n"
        )
    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "kitchen", "primary": "1"},
    )
    content = open(server_with_state["state_path"]).read()
    # New values written
    assert "JASPER_PEERING=on" in content
    assert "JASPER_PEER_ROOM=kitchen" in content
    assert "JASPER_PEER_PRIMARY=1" in content
    # Operator-set tuning preserved
    assert "JASPER_PEER_ARB_WINDOW_MS=200" in content
    assert "JASPER_PEER_BREAK_THRESHOLD=0.92" in content


def test_save_unchecking_primary_removes_it_from_file(server_with_state):
    """Unchecking the primary checkbox should drop the key from the
    env file — defaults take over (primary=False)."""
    with open(server_with_state["state_path"], "w") as f:
        f.write("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\nJASPER_PEER_PRIMARY=1\n")
    # NOTE: no `primary` key in form → unchecked
    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "kitchen"},
    )
    content = open(server_with_state["state_path"]).read()
    assert "JASPER_PEER_PRIMARY" not in content


def test_save_primary_flag(server_with_state):
    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "kitchen", "primary": "1"},
    )
    content = open(server_with_state["state_path"]).read()
    assert "JASPER_PEER_PRIMARY=1" in content


def test_save_triggers_both_daemon_restarts(server_with_state, monkeypatch):
    """The peering toggle requires BOTH jasper-voice and jasper-control
    to restart so they pick up the new mode. Verify both are called."""
    voice_called = []
    control_called = []
    monkeypatch.setattr(peering_setup, "restart_voice_daemon",
                         lambda: voice_called.append(1))
    monkeypatch.setattr(peering_setup, "_restart_jasper_control",
                         lambda: control_called.append(1))

    post_with_csrf(
        server_with_state["url"], "/save",
        {"enabled": "1", "room": "kitchen"},
    )
    assert voice_called == [1]
    assert control_called == [1]


def test_save_rejects_missing_csrf_token(server_with_state):
    """Direct POST without the csrf cookie + form field must 403.
    Defense against cross-origin attackers getting the browser to POST
    to /save — they can't read the SameSite=Strict cookie, so they
    can't craft the matching form field."""
    import urllib.request
    data = urllib.parse.urlencode({"enabled": "1", "room": "kitchen"}).encode()
    req = urllib.request.Request(
        server_with_state["url"] + "/save",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener = urllib.request.build_opener(_NoRedirect())
        opener.open(req)
    assert exc_info.value.code == 403
    # No state file should have been written.
    assert not os.path.exists(server_with_state["state_path"])


# `_NoRedirect` is provided by tests/_web_test_helpers (imported above).


# Need urllib.error for HTTPError class
import urllib.error  # noqa: E402
