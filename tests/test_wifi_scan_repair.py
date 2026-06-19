"""Tests for Pi 5 Wi-Fi scan suppression repair helpers."""
from __future__ import annotations

import json
import logging

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
