# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""NetworkManager operations: rollback, timeouts and secret handling."""
from __future__ import annotations

import logging
import subprocess

from jasper.net import wifi
from tests._log_events import leaked_lines


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_gather_state_shape(monkeypatch):
    # No real nmcli: every probe returns a clean "wifi adapter present, radio
    # on, no ethernet, not connected, no saved" world.
    def fake_run(cmd, *, timeout=10, log_argv=True):
        fields = cmd[cmd.index("-f") + 1] if "-f" in cmd else ""
        if fields == "TYPE":
            return _completed(cmd, stdout="wifi\n")
        if fields == "TYPE,STATE":
            return _completed(cmd, stdout="wifi:connected\n")  # no ethernet row
        if cmd[:3] == ["nmcli", "radio", "wifi"]:
            return _completed(cmd, stdout="enabled\n")
        return _completed(cmd, stdout="")

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)
    st = wifi.gather_state()
    assert set(st) == {
        "adapterPresent", "radioOn", "hasEthernet",
        "lockoutRisk", "current", "saved",
    }
    assert st["adapterPresent"] is True
    assert st["radioOn"] is True
    assert st["hasEthernet"] is False
    assert st["lockoutRisk"] == "high"


def test_connect_new_rolls_back_on_failure(monkeypatch):
    """The lockout-critical path: a failed connect must (a) delete the broken
    new profile and (b) bring the previously-active profile back up."""
    calls = []

    monkeypatch.setattr(
        wifi, "_current_wifi",
        lambda: {"profileName": "HomeNet", "ssid": "HomeNet"},
    )
    monkeypatch.setattr(wifi, "_profile_exists", lambda name: False)

    def fake_run(cmd, *, timeout=10, log_argv=True, stdin_secret=None):
        calls.append(list(cmd))
        if "connect" in cmd:
            # connect attempt fails with a non-SSID-lookup error
            return _completed(cmd, returncode=4, stderr="Error: Connection activation failed.")
        return _completed(cmd, returncode=0, stdout="")

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)

    ok, msg = wifi.connect_new("BadNet", "secretpw")
    assert ok is False
    # broken profile deleted (didn't exist before) ...
    assert any(c[:4] == ["nmcli", "connection", "delete", "BadNet"] for c in calls)
    # ... and the previous profile brought back up.
    assert any("connection" in c and "up" in c and "HomeNet" in c for c in calls)
    assert "HomeNet" in msg


def test_connect_new_reactivates_same_profile_on_failure(monkeypatch):
    """A failed reconnect can leave the already-active profile deactivated.

    Rollback must therefore reactivate it even when its profile name matches
    the requested SSID.
    """
    calls = []
    monkeypatch.setattr(
        wifi,
        "_current_wifi",
        lambda: {"profileName": "HomeNet", "ssid": "HomeNet"},
    )
    monkeypatch.setattr(wifi, "_profile_exists", lambda name: True)

    def fake_run(cmd, *, timeout=10, log_argv=True, stdin_secret=None):
        calls.append(list(cmd))
        if "connect" in cmd:
            return _completed(cmd, returncode=4, stderr="Error: Connection activation failed.")
        return _completed(cmd)

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)

    ok, message = wifi.connect_new("HomeNet", "secretpw")

    assert ok is False
    assert calls == [
        [
            "nmcli",
            "--wait",
            str(wifi._CONNECT_WAIT),
            "--ask",
            "device",
            "wifi",
            "connect",
            "HomeNet",
        ],
        [
            "nmcli",
            "--wait",
            str(wifi._ROLLBACK_WAIT),
            "connection",
            "up",
            "HomeNet",
        ],
    ]
    assert message.endswith("Restored previous network (HomeNet).")


def test_connect_new_worst_path_matches_declared_timeout_ceiling(monkeypatch):
    """Drive the real serialized fail path without sleeping: current-profile
    reads, profile lookup, visible + hidden attempts, cleanup, and rollback."""
    timeouts: list[int] = []

    def fake_run(cmd, *, timeout=10, log_argv=True, stdin_secret=None):
        timeouts.append(timeout)
        if cmd[-3:] == ["connection", "show", "--active"]:
            return _completed(cmd, stdout="Home:uuid:wifi:wlan0\n")
        if "connect" in cmd:
            return _completed(
                cmd,
                returncode=4,
                stderr="Error: No network with SSID 'MissingNet' found.",
            )
        return _completed(cmd, returncode=1, stderr="failed")

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)

    ok, _ = wifi.connect_new("MissingNet", "secretpw")

    assert ok is False
    assert timeouts == [5, 5, 5, 5, 5, 45, 45, 10, 30]
    assert sum(timeouts) == wifi.CONNECT_NEW_TIMEOUT_CEILING


def test_readable_nmcli_error_scrubs_echoed_psk():
    # nmcli can echo the submitted password back in error text; it must
    # never survive into the string that is logged AND returned to the
    # browser. Both scrub patterns: literal PSK and `password <arg>`.
    psk = "hunter2secret"
    proc = _completed(
        ["nmcli"], returncode=4,
        stderr=f"Error: 802-11-wireless-security.psk: '{psk}' invalid; password {psk}",
    )
    msg = wifi._readable_nmcli_error(proc, psk)
    assert psk not in msg
    assert "<redacted>" in msg


def test_readable_nmcli_error_scrubs_password_token_without_literal():
    # Even if we don't have the literal PSK, `password <arg>` echo is masked.
    proc = _completed(
        ["nmcli"], returncode=4, stderr="Error: password abc123def not accepted",
    )
    msg = wifi._readable_nmcli_error(proc, None)
    assert "abc123def" not in msg
    assert "password <redacted>" in msg


def test_connect_new_scrubs_psk_from_returned_message(monkeypatch):
    psk = "TopSecretWifiPass"
    monkeypatch.setattr(
        wifi, "_current_wifi",
        lambda: {"profileName": "HomeNet", "ssid": "HomeNet"},
    )
    monkeypatch.setattr(wifi, "_profile_exists", lambda name: False)

    def fake_run(cmd, *, timeout=10, log_argv=True, stdin_secret=None):
        if "connect" in cmd:
            # nmcli echoes the PSK back in its failure text.
            return _completed(
                cmd, returncode=4,
                stderr=f"Error: secrets were required but not provided: password {psk}",
            )
        return _completed(["nmcli"])

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)

    ok, msg = wifi.connect_new("MyNet", psk)
    assert ok is False
    assert psk not in msg


def test_connect_new_never_puts_psk_on_argv(monkeypatch):
    """Non-negotiable 3 (issue #4279 item 8): the PSK must never land on
    nmcli's argv, where it is visible in /proc/<pid>/cmdline to root for
    the connect window. It rides the child's stdin instead, paired with
    `--ask`."""
    psk = "hunter2-super-secret-psk"
    captured: list[tuple[list[str], str | None]] = []

    monkeypatch.setattr(wifi, "_current_wifi", lambda: None)
    monkeypatch.setattr(wifi, "_profile_exists", lambda name: False)
    monkeypatch.setattr(wifi, "_stash_after_saved", lambda *a, **k: None)

    def fake_run(cmd, *, timeout=10, log_argv=True, stdin_secret=None):
        captured.append((list(cmd), stdin_secret))
        return _completed(cmd, returncode=0)

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)

    ok, _ = wifi.connect_new("MyNet", psk)

    assert ok is True
    assert captured  # sanity: connect_new actually shelled out
    for cmd, _secret in captured:
        assert all(psk not in arg for arg in cmd)

    connect_calls = [(cmd, secret) for cmd, secret in captured if "connect" in cmd]
    assert connect_calls
    for cmd, secret in connect_calls:
        assert "--ask" in cmd
        # ... and the mechanism that keeps it off argv actually got it.
        assert secret == psk


def test_run_nmcli_stdin_secret_reaches_child_not_log(caplog):
    """Pins the real `subprocess.run` wiring the non-negotiable rests on:
    every other test here monkeypatches `_run_nmcli` itself, so
    `input=...` was never exercised against a real child process. `cat`
    echoes stdin to stdout without ever taking the secret on its own
    argv, standing in for nmcli's `--ask` prompt read."""
    psk = "real-stdin-secret-psk"
    caplog.set_level(logging.INFO, logger=wifi.logger.name)

    proc = wifi._run_nmcli(["cat"], stdin_secret=psk)

    assert leaked_lines(caplog, psk) == []
    assert psk in proc.stdout


def test_set_radio_passes_on_off(monkeypatch):
    seen = []

    def fake_run(cmd, *, timeout=10, log_argv=True):
        seen.append(list(cmd))
        return _completed(cmd, returncode=0)

    monkeypatch.setattr(wifi, "_run_nmcli", fake_run)
    ok, _ = wifi.set_radio(False)
    assert ok is True
    assert ["nmcli", "radio", "wifi", "off"] == seen[-1]
