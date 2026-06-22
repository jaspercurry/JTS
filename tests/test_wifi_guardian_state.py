# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.control.wifi_guardian_state.snapshot().

Verifies the /state.resilience.wifi_guardian block surfaces the right
fields and never raises — `/state` is called frequently and must stay
fail-soft.

Mocks subprocess.run for both nmcli and journalctl since neither is
guaranteed on a dev machine; we never actually shell out.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from jasper.control import wifi_guardian_state
from jasper import wifi_guardian_persistence


def _proc(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="",
    )


@pytest.fixture
def stash_path(tmp_path, monkeypatch):
    p = tmp_path / "wifi_guardian.env"
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(p))
    return p


def test_snapshot_disabled_when_no_stash_and_no_active(monkeypatch, stash_path):
    """Default fresh-install case: nothing to recover, nothing to
    show. enabled=False so the dashboard can render "off"."""
    def fake_run(*args, **kwargs):
        return _proc(stdout="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["enabled"] is False
    assert snap["stash_ssid"] is None
    assert snap["active_ssid"] is None
    assert snap["stash_matches_active"] is None


def test_snapshot_enabled_via_stat_when_psk_unreadable(monkeypatch, stash_path, caplog):
    """WS1 Phase 3b-2: the non-root jasper-control cannot read the 0600
    PSK-bearing stash. `enabled` must still be True (derived from a stat, not a
    read), the SSID gracefully omitted, and the read NEVER attempted (so
    read_stash can't log a permission WARNING on every /state poll) —
    active_ssid still populated so the card degrades honestly."""
    import os

    wifi_guardian_persistence.write_stash(stash_path, "Home", "p", "wpa-psk")

    # Simulate the unreadable 0600 PSK file deterministically (regardless of the
    # test-runner uid): os.access(R_OK) is False for the stash only.
    real_access = os.access
    monkeypatch.setattr(
        wifi_guardian_state.os, "access",
        lambda p, mode: (
            False if str(p) == str(stash_path) else real_access(p, mode)
        ),
    )
    # The read must not even be attempted when the file is unreadable.
    attempted: list = []
    monkeypatch.setattr(
        wifi_guardian_state, "read_stash",
        lambda p: attempted.append(p),  # noqa: ARG005
    )

    nmcli_responses = iter([
        _proc(stdout="Home:802-11-wireless\n"),
        _proc(stdout="802-11-wireless.ssid:Home\n"),
    ])

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "nmcli":
            try:
                return next(nmcli_responses)
            except StopIteration:
                return _proc()
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["enabled"] is True               # from stat, not a read
    assert snap["stash_ssid"] is None            # PSK file unreadable → omitted
    assert snap["stash_matches_active"] is None
    assert snap["active_ssid"] == "Home"         # nmcli still works
    assert attempted == []                       # read never attempted
    assert "stash_read_failed" not in caplog.text


def test_snapshot_steady_state(monkeypatch, stash_path):
    """Stash + active match → enabled, fields populated, matches=True."""
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "p", "wpa-psk",
    )

    nmcli_responses = iter([
        # connection show --active
        _proc(stdout="Home:802-11-wireless\n"),
        # connection show Home (ssid lookup)
        _proc(stdout="802-11-wireless.ssid:Home\n"),
    ])
    journalctl_response = _proc(stdout="")

    def fake_run(cmd, *args, **kwargs):
        # cmd[0] dispatches nmcli vs journalctl
        if cmd[0] == "nmcli":
            try:
                return next(nmcli_responses)
            except StopIteration:
                return _proc()
        if cmd[0] == "journalctl":
            return journalctl_response
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["enabled"] is True
    assert snap["stash_ssid"] == "Home"
    assert snap["stash_key_mgmt"] == "wpa-psk"
    assert snap["active_ssid"] == "Home"
    assert snap["stash_matches_active"] is True


def test_snapshot_drift_detected(monkeypatch, stash_path):
    """Stash says Home but NM is on Cafe → matches=False."""
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "p", "wpa-psk",
    )

    nmcli_responses = iter([
        _proc(stdout="Cafe:802-11-wireless\n"),
        _proc(stdout="802-11-wireless.ssid:Cafe\n"),
    ])

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "nmcli":
            try:
                return next(nmcli_responses)
            except StopIteration:
                return _proc()
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["stash_ssid"] == "Home"
    assert snap["active_ssid"] == "Cafe"
    assert snap["stash_matches_active"] is False


def test_snapshot_handles_missing_nmcli(monkeypatch, stash_path):
    """nmcli unreachable → active_ssid stays None, matches stays None.
    Must not raise."""
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "p", "wpa-psk",
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "nmcli":
            raise FileNotFoundError("nmcli: command not found")
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["enabled"] is True
    assert snap["stash_ssid"] == "Home"
    assert snap["active_ssid"] is None
    assert snap["stash_matches_active"] is None


def test_snapshot_psk_never_exposed(stash_path):
    """The snapshot must not contain the PSK anywhere — it's read by
    /state which is unauthenticated on the LAN."""
    psk = "extremely-secret-psk-leak-detector"
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", psk, "wpa-psk",
    )

    def fake_run(cmd, *args, **kwargs):
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    # Walk every string value in the dict.
    for k, v in snap.items():
        if isinstance(v, str):
            assert psk not in v, f"PSK leaked in snapshot key {k}: {v}"


def test_snapshot_parses_journalctl_last_action(monkeypatch, stash_path):
    """When the last guardian run logged `event=wifi_guardian.steady_state`,
    surface that in `last_action`."""
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "p", "wpa-psk",
    )

    nmcli_responses = iter([
        _proc(stdout="Home:802-11-wireless\n"),
        _proc(stdout="802-11-wireless.ssid:Home\n"),
    ])

    # Multi-line JSON output from journalctl -o json -n N
    journal_json = (
        '{"MESSAGE":"jasper-wifi-guardian[systemd]: ...","__REALTIME_TIMESTAMP":"1716480000000000"}\n'
        '{"MESSAGE":"event=wifi_guardian.steady_state ssid=Home","__REALTIME_TIMESTAMP":"1716480005000000"}\n'
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "nmcli":
            try:
                return next(nmcli_responses)
            except StopIteration:
                return _proc()
        if cmd[0] == "journalctl":
            return _proc(stdout=journal_json)
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["last_action"] == "steady_state"
    assert snap["last_run_at"] is not None
    # Should be ISO-formatted UTC; sanity check
    assert "T" in snap["last_run_at"]
    assert snap["last_run_at"].endswith("Z")


def test_snapshot_handles_journalctl_missing(monkeypatch, stash_path):
    """journalctl unreachable → last_action stays None; never raises."""
    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "p", "wpa-psk",
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "journalctl":
            raise FileNotFoundError()
        # Default empty nmcli response.
        return _proc()

    with patch.object(subprocess, "run", side_effect=fake_run):
        snap = wifi_guardian_state.snapshot()

    assert snap["last_action"] is None
    assert snap["last_run_at"] is None
