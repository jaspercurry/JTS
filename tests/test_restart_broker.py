"""Contract tests for the WS1 Phase 3 privileged restart broker.

Pins the closed verb vocabulary, the unit allowlist, SO_PEERCRED peer-uid
auth, the request/response wire contract, and the root fallback — the
properties the user-drop PR relies on. No real systemctl runs: the broker's
``subprocess.run`` and the uid allowlist are patched so the suite is
hardware-free and side-effect-free.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jasper.control import restart_broker

# The broker's peer-cred auth uses SO_PEERCRED (Linux-only — the broker runs on
# the Pi). On a macOS dev box the constant is absent, so the server round-trip
# tests are skipped there; CI on Linux is the source of truth. The pure-helper
# and client-fallback tests run everywhere.
requires_peercred = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"),
    reason="SO_PEERCRED is Linux-only (the restart broker runs on the Pi)",
)

_REPO = Path(__file__).resolve().parent.parent
_CONTROL_UNIT = _REPO / "deploy" / "systemd" / "jasper-control.service"


def test_control_unit_declares_broker_runtime_dir():
    """The broker binds /run/jasper-control/restart.sock; systemd must create
    that dir (RuntimeDirectory) owned by the unit's user, or the broker can't
    bind after the user drop."""
    text = _CONTROL_UNIT.read_text()
    assert "RuntimeDirectory=jasper-control" in text
    # The default socket path lives under that runtime dir.
    assert restart_broker.DEFAULT_SOCKET_PATH.startswith("/run/jasper-control/")


# --------------------------------------------------------------------------
# Pure helpers: verb vocabulary, unit normalization, allowlist.
# --------------------------------------------------------------------------


def test_verb_vocabulary_is_closed_and_complete():
    # The exact set the clients use. A new verb must be added deliberately.
    assert restart_broker.ALLOWED_VERBS == frozenset({
        "restart", "try-restart", "start", "stop",
        "enable", "enable-now", "disable-now", "reset-failed",
    })


@pytest.mark.parametrize("verb,no_block,expected", [
    ("restart", True, ["systemctl", "restart", "--no-block", "jasper-voice.service"]),
    ("restart", False, ["systemctl", "restart", "jasper-voice.service"]),
    ("try-restart", True, ["systemctl", "try-restart", "--no-block", "jasper-voice.service"]),
    ("start", True, ["systemctl", "start", "--no-block", "jasper-voice.service"]),
    ("stop", True, ["systemctl", "stop", "--no-block", "jasper-voice.service"]),
    # enable / reset-failed never take --no-block even when asked.
    ("enable", True, ["systemctl", "enable", "jasper-voice.service"]),
    ("enable-now", True, ["systemctl", "enable", "--now", "--no-block", "jasper-voice.service"]),
    ("disable-now", False, ["systemctl", "disable", "--now", "jasper-voice.service"]),
    ("reset-failed", True, ["systemctl", "reset-failed", "jasper-voice.service"]),
])
def test_build_argv(verb, no_block, expected):
    assert restart_broker._build_argv(
        verb, ["jasper-voice.service"], no_block=no_block,
    ) == expected


@pytest.mark.parametrize("raw,normalized", [
    ("jasper-voice", "jasper-voice.service"),
    ("jasper-voice.service", "jasper-voice.service"),
    ("shairport-sync", "shairport-sync.service"),
    ("jasper-web.socket", "jasper-web.socket"),
    (" librespot ", "librespot.service"),
])
def test_normalize_unit(raw, normalized):
    assert restart_broker._normalize_unit(raw) == normalized


def test_managed_units_cover_every_routed_client_unit():
    # Units the wizard / mux / correction / wake-corpus client sites send.
    must_contain = {
        "jasper-voice.service", "jasper-control.service", "jasper-web.service",
        "jasper-mux.service", "jasper-input.service",
        "shairport-sync.service", "nqptp.service", "librespot.service",
        "jasper-usbsink.service", "jasper-usbsink-init.service",
        "bluetooth.service", "bluealsa.service", "bluealsa-aplay.service",
        "bt-agent.service",
        "jasper-aec-bridge.service", "jasper-aec-init.service",
        "jasper-aec-reconcile.service", "jasper-grouping-reconcile.service",
        "jasper-camilla.service", "jasper-outputd.service",
    }
    assert must_contain <= restart_broker.MANAGED_UNITS


# --------------------------------------------------------------------------
# Server: a real UDS broker on a tmp socket, with subprocess + uid patched.
# --------------------------------------------------------------------------


@dataclass
class _FakeProc:
    returncode: int = 0
    stderr: str = ""


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """Start a real broker on a tmp socket. Yields (socket_path, calls)
    where `calls` records every systemctl argv the broker would run. The
    peer-uid allowlist is widened to include the test runner's uid so the
    in-process client is authorized; `subprocess.run` is faked so nothing
    real restarts."""
    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("SO_PEERCRED is Linux-only (the restart broker runs on the Pi)")
    calls: list[list[str]] = []
    rc_holder = {"rc": 0, "stderr": ""}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _FakeProc(returncode=rc_holder["rc"], stderr=rc_holder["stderr"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {os.getuid(), 0})

    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    assert server is not None
    # serve_forever runs on a daemon thread started by start_broker.
    yield sock_path, calls, rc_holder
    server.shutdown()
    server.server_close()


def test_happy_path_restart(broker):
    sock_path, calls, _ = broker
    resp = restart_broker.request_restart(
        "jasper-voice", verb="restart", reason="test", socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert resp["action"] == "restart"
    assert resp["units"] == ["jasper-voice.service"]
    assert calls == [["systemctl", "restart", "--no-block", "jasper-voice.service"]]


def test_enable_now_maps_through_broker(broker):
    sock_path, calls, _ = broker
    resp = restart_broker.request_restart(
        "shairport-sync.service", verb="enable-now", no_block=False,
        socket_path=sock_path,
    )
    assert resp["ok"] is True
    assert calls == [["systemctl", "enable", "--now", "shairport-sync.service"]]


def test_unknown_verb_rejected_without_running_anything(broker):
    sock_path, calls, _ = broker
    resp = restart_broker.request_restart(
        "jasper-voice.service", verb="exec", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert "unknown verb" in resp["error"]
    assert calls == []


def test_unit_not_in_allowlist_rejected(broker):
    sock_path, calls, _ = broker
    resp = restart_broker.request_restart(
        "sshd.service", verb="stop", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert "allowlist" in resp["error"]
    assert calls == []


def test_one_bad_unit_blocks_the_whole_request(broker):
    sock_path, calls, _ = broker
    resp = restart_broker.request_restart(
        "jasper-voice.service", "sshd.service", verb="restart",
        socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert calls == []


def test_nonzero_systemctl_surfaces_rc_and_stderr(broker):
    sock_path, calls, rc_holder = broker
    rc_holder["rc"] = 5
    rc_holder["stderr"] = "Unit not loaded"
    resp = restart_broker.request_restart(
        "jasper-voice.service", verb="restart", socket_path=sock_path,
    )
    assert resp["ok"] is False
    assert resp["rc"] == 5
    assert "Unit not loaded" in resp["stderr"]
    assert calls  # it DID attempt the restart


@requires_peercred
def test_unauthorized_peer_uid_rejected(tmp_path, monkeypatch):
    """A peer whose uid is not in the allowlist is refused before any
    systemctl runs."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(list(argv)) or _FakeProc(),
    )
    # Allow nobody the test runner is (use an impossible uid set).
    monkeypatch.setattr(restart_broker, "_allowed_uids", lambda: {999999})
    sock_path = str(tmp_path / "restart.sock")
    server = restart_broker.start_broker(sock_path)
    try:
        resp = restart_broker.request_restart(
            "jasper-voice.service", verb="restart", socket_path=sock_path,
        )
        assert resp["ok"] is False
        assert "unauthorized" in resp["error"]
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_json_request_rejected(broker):
    sock_path, calls, _ = broker
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        sock.connect(sock_path)
        sock.sendall(b"this is not json\n")
        data = sock.recv(4096)
    resp = json.loads(data.decode().splitlines()[0])
    assert resp["ok"] is False
    assert calls == []


# --------------------------------------------------------------------------
# Client: BrokerUnavailable + the root fallback.
# --------------------------------------------------------------------------


def test_request_restart_raises_when_socket_absent(tmp_path):
    with pytest.raises(restart_broker.BrokerUnavailable):
        restart_broker.request_restart(
            "jasper-voice.service", socket_path=str(tmp_path / "nope.sock"),
            timeout=0.5,
        )


def test_manage_units_falls_back_to_direct_systemctl_when_root(tmp_path, monkeypatch):
    """Broker unreachable + euid 0 -> direct systemctl (the PR1 transition
    safety net), logged loudly."""
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    direct_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        direct_calls.append(list(argv))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    resp = restart_broker.manage_units(
        "jasper-voice", verb="restart", reason="fallback test", timeout=0.5,
    )
    assert resp["ok"] is True
    assert direct_calls == [
        ["systemctl", "restart", "--no-block", "jasper-voice.service"],
    ]


def test_manage_units_no_fallback_when_non_root(tmp_path, monkeypatch):
    """Broker unreachable + non-root -> error dict, never a direct systemctl.
    This is the post-user-drop behaviour: the broker is the only path."""
    monkeypatch.setattr(
        restart_broker, "DEFAULT_SOCKET_PATH", str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: ran.append(list(argv)),
    )
    resp = restart_broker.manage_units(
        "jasper-voice", verb="restart", timeout=0.5,
    )
    assert resp["ok"] is False
    assert "unavailable" in resp["error"]
    assert ran == []  # NO direct systemctl when non-root


def test_manage_units_empty_units_is_noop():
    resp = restart_broker.manage_units(verb="restart")
    assert resp["ok"] is True
    assert resp["units"] == []


def test_manage_units_prefers_broker_over_fallback(broker, monkeypatch):
    """When the broker IS reachable, manage_units uses it and never touches
    the direct path — even as root."""
    sock_path, calls, _ = broker
    monkeypatch.setattr(restart_broker, "DEFAULT_SOCKET_PATH", sock_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    resp = restart_broker.manage_units("jasper-mux", verb="restart")
    assert resp["ok"] is True
    # Exactly one call, made by the broker (not a fallback duplicate).
    assert calls == [["systemctl", "restart", "--no-block", "jasper-mux.service"]]
