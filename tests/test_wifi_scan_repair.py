# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Pi 5 Wi-Fi scan suppression repair helpers."""
from __future__ import annotations

import json
import logging
import socket as _socket
import struct

import pytest

from jasper import wifi_scan_repair


def test_parse_enum_constant_handles_sequential_and_assigned_values():
    header = """
    enum nl80211_commands {
        NL80211_CMD_UNSPEC,
        NL80211_CMD_GET_WIPHY,
        NL80211_CMD_CRIT_PROTOCOL_STOP = 219,
        __NL80211_CMD_AFTER_LAST,
        NL80211_CMD_MAX = __NL80211_CMD_AFTER_LAST - 1,
    };
    """

    assert (
        wifi_scan_repair.parse_enum_constant(
            header, "nl80211_commands", "NL80211_CMD_CRIT_PROTOCOL_STOP",
        )
        == 219
    )
    assert (
        wifi_scan_repair.parse_enum_constant(
            header, "nl80211_commands", "NL80211_CMD_MAX",
        )
        == 219
    )


def test_crit_stop_dry_run_builds_attrs_without_socket(monkeypatch):
    monkeypatch.setattr(
        wifi_scan_repair,
        "load_nl80211_constants",
        lambda: {
            "NL80211_CMD_CRIT_PROTOCOL_STOP": 1,
            "NL80211_ATTR_IFINDEX": 2,
            "NL80211_ATTR_WDEV": 3,
        },
    )
    monkeypatch.setattr(wifi_scan_repair.socket, "if_nametoindex", lambda iface: 7)
    monkeypatch.setattr(wifi_scan_repair, "wdev_for_iface", lambda iface: 0x1)

    result = wifi_scan_repair.send_crit_proto_stop("wlan0", dry_run=True)

    assert result["iface"] == "wlan0"
    assert result["ifindex"] == 7
    assert result["wdev"] == 1
    assert result["dryRun"] is True


def test_maybe_repair_skips_inside_cooldown(tmp_path):
    state_path = tmp_path / "repair.json"
    state_path.write_text(
        json.dumps({"nextAllowedAt": 130.0}),
        encoding="utf-8",
    )

    result = wifi_scan_repair.maybe_repair_scan_suppression(
        "wlan0",
        state_path=state_path,
        now=100.0,
    )

    assert result.attempted is False
    assert result.reason == "cooldown"
    assert result.cooldown_remaining == 30.0
    assert result.to_dict()["cooldownRemaining"] == 30.0


def test_maybe_repair_skips_non_brcmfmac_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wifi_scan_repair,
        "iface_driver_name",
        lambda iface: "iwlwifi",
    )

    result = wifi_scan_repair.maybe_repair_scan_suppression(
        "wlan0",
        state_path=tmp_path / "repair.json",
        now=100.0,
    )

    assert result.attempted is False
    assert result.reason == "driver_not_brcmfmac"
    assert result.driver == "iwlwifi"


def test_maybe_repair_attempt_success_writes_cooldown(tmp_path, monkeypatch, caplog):
    state_path = tmp_path / "repair.json"
    monkeypatch.setattr(
        wifi_scan_repair,
        "iface_driver_name",
        lambda iface: "brcmfmac",
    )
    monkeypatch.setattr(
        wifi_scan_repair,
        "send_crit_proto_stop",
        lambda iface: {"iface": iface, "ack": True},
    )

    with caplog.at_level(logging.INFO, logger="jasper.wifi_scan_repair"):
        result = wifi_scan_repair.maybe_repair_scan_suppression(
            "wlan0",
            state_path=state_path,
            now=100.0,
            attempt_cooldown_s=15.0,
        )

    assert result.attempted is True
    assert result.ack is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["lastAck"] is True
    assert state["nextAllowedAt"] == 115.0
    # Pin the migrated emit: the bool ack renders as lowercase logfmt
    # (`true`), not Python's `True`. This is the intentional, more-correct
    # on-the-wire change the canonical helper makes.
    attempt_lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("event=wifi_scan_repair.attempt ")
    ]
    assert attempt_lines == [
        "event=wifi_scan_repair.attempt iface=wlan0 driver=brcmfmac ack=true"
    ]


def test_maybe_repair_error_uses_failure_cooldown(tmp_path, monkeypatch):
    state_path = tmp_path / "repair.json"
    monkeypatch.setattr(
        wifi_scan_repair,
        "iface_driver_name",
        lambda iface: "brcmfmac",
    )

    def fail(_iface):
        raise RuntimeError("no nl80211")

    monkeypatch.setattr(wifi_scan_repair, "send_crit_proto_stop", fail)

    result = wifi_scan_repair.maybe_repair_scan_suppression(
        "wlan0",
        state_path=state_path,
        now=100.0,
        failure_cooldown_s=90.0,
    )

    assert result.attempted is True
    assert result.reason == "error"
    assert result.ack is False
    assert "no nl80211" in (result.error or "")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["lastAck"] is False
    assert state["nextAllowedAt"] == 190.0


def test_cli_json_reports_repair_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        wifi_scan_repair,
        "iface_driver_name",
        lambda iface: "brcmfmac",
    )
    monkeypatch.setattr(
        wifi_scan_repair,
        "send_crit_proto_stop",
        lambda iface: {"iface": iface, "ack": True},
    )

    rc = wifi_scan_repair.main([
        "--iface", "wlan0",
        "--state-path", str(tmp_path / "repair.json"),
        "--json",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["attempted"] is True
    assert payload["reason"] == "attempted"
    assert payload["ack"] is True
    assert payload["iface"] == "wlan0"


# ----------------------------- send_crit_proto_stop recv-loop termination


def _nlmsg(nlmsg_type: int, payload: bytes = b"") -> bytes:
    """Pack one netlink message of ``nlmsg_type`` with ``payload``."""
    length = 16 + len(payload)
    return struct.pack("IHHII", length, nlmsg_type, 0, 0, 0) + payload


class _FakeNetlinkSocket:
    """A fake AF_NETLINK socket for driving send_crit_proto_stop().

    ``recv_frames`` is the queue of byte buffers recv() returns in order;
    once exhausted it either repeats the last frame forever (``repeat_last``)
    or raises ``socket.timeout`` to model the kernel going quiet. Records
    the recv() count so a test can prove the loop is bounded."""

    def __init__(self, recv_frames, *, repeat_last: bool = False):
        self._frames = list(recv_frames)
        self._repeat_last = repeat_last
        self.recv_count = 0
        self.timeout_set = None

    # context-manager protocol (send_crit_proto_stop uses `with socket(...)`)
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, t):
        self.timeout_set = t

    def bind(self, addr):
        pass

    def send(self, msg):
        return len(msg)

    def recv(self, bufsize):
        self.recv_count += 1
        if self._frames:
            frame = self._frames.pop(0)
            if self._repeat_last and not self._frames:
                self._frames.append(frame)
            return frame
        raise _socket.timeout("no more netlink data")


def _patch_crit_stop_env(monkeypatch, fake_sock):
    monkeypatch.setattr(
        wifi_scan_repair,
        "load_nl80211_constants",
        lambda: {
            "NL80211_CMD_CRIT_PROTOCOL_STOP": 1,
            "NL80211_ATTR_IFINDEX": 2,
            "NL80211_ATTR_WDEV": 3,
        },
    )
    monkeypatch.setattr(wifi_scan_repair.socket, "if_nametoindex", lambda iface: 7)
    monkeypatch.setattr(wifi_scan_repair, "wdev_for_iface", lambda iface: 0x1)
    monkeypatch.setattr(wifi_scan_repair, "get_genl_family", lambda sock, fam: (0x10, 1))
    # macOS has no AF_NETLINK/SOCK_RAW netlink; the socket() call expression
    # evaluates these argument constants before our fake is substituted, so
    # stub them too (their values are irrelevant — the fake ignores them).
    if not hasattr(wifi_scan_repair.socket, "AF_NETLINK"):
        monkeypatch.setattr(wifi_scan_repair.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(
        wifi_scan_repair.socket, "socket", lambda *a, **kw: fake_sock,
    )


def test_crit_stop_returns_ack_on_errno_zero(monkeypatch):
    """A single NLMSG_ERROR with errno 0 is the kernel ACK → ack=True."""
    ack_frame = _nlmsg(wifi_scan_repair.NLMSG_ERROR, struct.pack("i", 0))
    fake = _FakeNetlinkSocket([ack_frame])
    _patch_crit_stop_env(monkeypatch, fake)

    result = wifi_scan_repair.send_crit_proto_stop("wlan0")

    assert result["ack"] is True
    assert fake.recv_count == 1


def test_crit_stop_bounded_on_unexpected_traffic(monkeypatch):
    """Unexpected (non-ERROR) messages must not spin the recv loop forever.

    The socket here returns an unrecognized netlink message type on EVERY
    recv() and NEVER times out. The old `while True` loop relied solely on
    the socket timeout to terminate, so it would loop indefinitely on this
    input. The bounded loop raises after NETLINK_MAX_RECV_ITERS reads. The
    recv_count assertion is what fails (hangs) on the unbounded version."""
    # NLMSG_MIN_TYPE+ (0x10) is a benign "not an ACK" type the loop ignores.
    noise = _nlmsg(0x10)
    fake = _FakeNetlinkSocket([noise], repeat_last=True)
    _patch_crit_stop_env(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="no ACK after"):
        wifi_scan_repair.send_crit_proto_stop("wlan0")

    # Bounded: never more than the cap, regardless of how chatty the socket is.
    assert fake.recv_count == wifi_scan_repair.NETLINK_MAX_RECV_ITERS


def test_crit_stop_raises_on_nlmsg_done_without_ack(monkeypatch):
    """An end-of-dump NLMSG_DONE with no ACK terminates with a clear error."""
    done_frame = _nlmsg(wifi_scan_repair.NLMSG_DONE)
    fake = _FakeNetlinkSocket([done_frame])
    _patch_crit_stop_env(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="NLMSG_DONE"):
        wifi_scan_repair.send_crit_proto_stop("wlan0")

    assert fake.recv_count == 1


def test_crit_stop_raises_on_kernel_error(monkeypatch):
    """A real NLMSG_ERROR (nonzero errno) propagates as OSError."""
    # errno -1 (EPERM) packed as the netlink error code.
    err_frame = _nlmsg(wifi_scan_repair.NLMSG_ERROR, struct.pack("i", -1))
    fake = _FakeNetlinkSocket([err_frame])
    _patch_crit_stop_env(monkeypatch, fake)

    with pytest.raises(OSError):
        wifi_scan_repair.send_crit_proto_stop("wlan0")
