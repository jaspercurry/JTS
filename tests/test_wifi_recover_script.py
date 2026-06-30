# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-wifi-recover.

The recovery timer must be cheap when Wi-Fi is healthy, repair the narrow
brcmfmac scan-suppression wedge even when NetworkManager still reports an
active profile, and delegate no-active recovery to the PSK-owning guardian.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-wifi-recover"


def _write_fake(bin_dir: Path, name: str, body: str) -> Path:
    fake = bin_dir / name
    fake.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _setup_fakes(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    nmcli_log = tmp_path / "nmcli.log"
    journalctl_log = tmp_path / "journalctl.log"
    python_log = tmp_path / "python.log"
    guardian_log = tmp_path / "guardian.log"

    nmcli = _write_fake(
        bin_dir,
        "nmcli",
        r"""
printf '%s\n' "$*" >> "$JASPER_NMCLI_LOG"
if [[ "$*" == *"connection show --active"* ]]; then
    printf '%s' "${JASPER_NMCLI_ACTIVE:-}"
fi
exit 0
""",
    )
    journalctl = _write_fake(
        bin_dir,
        "journalctl",
        r"""
printf '%s\n' "$*" >> "$JASPER_JOURNALCTL_LOG"
printf '%s' "${JASPER_JOURNALCTL_KERNEL:-}"
exit 0
""",
    )
    python = _write_fake(
        bin_dir,
        "python3",
        r"""
printf '%s\n' "$*" >> "$JASPER_PYTHON_LOG"
printf '{"attempted":true,"reason":"test"}\n'
exit "${JASPER_PYTHON_RC:-0}"
""",
    )
    guardian = _write_fake(
        bin_dir,
        "jasper-wifi-guardian",
        r"""
printf '%s\n' "$*" >> "$JASPER_GUARDIAN_LOG"
exit "${JASPER_GUARDIAN_RC:-0}"
""",
    )

    return {
        "bin_dir": bin_dir,
        "nmcli": nmcli,
        "journalctl": journalctl,
        "python": python,
        "guardian": guardian,
        "nmcli_log": nmcli_log,
        "journalctl_log": journalctl_log,
        "python_log": python_log,
        "guardian_log": guardian_log,
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _run_recover(
    tmp_path: Path,
    *,
    active: str = "",
    kernel: str = "",
    stash: bool = True,
    reason: str = "systemd",
    python_rc: int = 0,
    guardian_rc: int = 0,
    python_path: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    paths = _setup_fakes(tmp_path)
    stash_path = tmp_path / "wifi_guardian.env"
    if stash:
        stash_path.write_text("JASPER_WIFI_SSID=Home\n", encoding="utf-8")

    # python_path=None → use the fake (venv-python-present path); pass a
    # bogus path to exercise the "venv python missing → skip repair" branch.
    scan_python = str(paths["python"]) if python_path is None else python_path

    env = os.environ.copy()
    env.update({
        "PATH": f"{paths['bin_dir']}:{env['PATH']}",
        "JASPER_WIFI_STASH_FILE": str(stash_path),
        "JASPER_NMCLI": str(paths["nmcli"]),
        "JASPER_NMCLI_LOG": str(paths["nmcli_log"]),
        "JASPER_JOURNALCTL": str(paths["journalctl"]),
        "JASPER_JOURNALCTL_LOG": str(paths["journalctl_log"]),
        "JASPER_WIFI_GUARDIAN": str(paths["guardian"]),
        "JASPER_GUARDIAN_LOG": str(paths["guardian_log"]),
        "JASPER_WIFI_SCAN_REPAIR_PYTHON": scan_python,
        "JASPER_PYTHON_LOG": str(paths["python_log"]),
        "JASPER_NMCLI_ACTIVE": active,
        "JASPER_JOURNALCTL_KERNEL": kernel,
        "JASPER_PYTHON_RC": str(python_rc),
        "JASPER_GUARDIAN_RC": str(guardian_rc),
    })
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--reason", reason],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return proc, paths


def test_active_wifi_systemd_tick_is_silent_and_cheap(tmp_path):
    proc, paths = _run_recover(
        tmp_path,
        # nmcli -t -f TYPE,NAME --active → "<type>:<name>".
        active="802-11-wireless:Home\n",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert _read(paths["guardian_log"]) == ""
    assert _read(paths["python_log"]) == ""
    assert "connection show --active" in _read(paths["nmcli_log"])
    # Cost contract (#1033): even a healthy active-WiFi tick reads the
    # recent kernel log to catch the brcmfmac scan-suppression wedge that
    # NetworkManager still reports as "active". Pin that the `journalctl -k`
    # probe runs on EVERY tick — removing it (e.g. only checking on the
    # no-active path) must fail here, since that is the silent regression
    # this assertion guards against.
    journalctl_args = _read(paths["journalctl_log"])
    assert journalctl_args != ""
    assert "-k --since" in journalctl_args


def test_manual_active_wifi_reports_steady(tmp_path):
    proc, paths = _run_recover(
        tmp_path,
        active="802-11-wireless:Home\n",
        reason="manual",
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.steady active=Home" in proc.stderr
    assert _read(paths["guardian_log"]) == ""
    assert _read(paths["python_log"]) == ""


def test_active_wifi_with_scan_suppression_runs_repair_without_guardian(tmp_path):
    proc, paths = _run_recover(
        tmp_path,
        active="802-11-wireless:Home\n",
        kernel="brcmf_cfg80211_scan: Scanning suppressed: status (4)\n",
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.scan_suppressed iface=wlan0 active=Home" in proc.stderr
    assert "event=wifi_recover.scan_repair_ok iface=wlan0" in proc.stderr
    assert "-m jasper.wifi_scan_repair --iface wlan0 --json" in _read(
        paths["python_log"]
    )
    assert _read(paths["guardian_log"]) == ""
    assert '"attempted":true' in proc.stdout


def test_no_active_wifi_delegates_to_guardian_without_scan_evidence(tmp_path):
    proc, paths = _run_recover(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.no_active iface=wlan0" in proc.stderr
    assert "event=wifi_recover.guardian_start" in proc.stderr
    assert "event=wifi_recover.guardian_ok" in proc.stderr
    assert "--reason wifi-recover" in _read(paths["guardian_log"])
    assert _read(paths["python_log"]) == ""


def test_scan_suppression_runs_bounded_repair_before_guardian(tmp_path):
    proc, paths = _run_recover(
        tmp_path,
        kernel="brcmf_cfg80211_scan: Scanning suppressed: status (4)\n",
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.scan_suppressed iface=wlan0" in proc.stderr
    assert "event=wifi_recover.scan_repair_ok iface=wlan0" in proc.stderr
    assert "-m jasper.wifi_scan_repair --iface wlan0 --json" in _read(
        paths["python_log"]
    )
    assert "--reason wifi-recover" in _read(paths["guardian_log"])
    assert '"attempted":true' in proc.stdout


def test_scan_repair_failure_still_runs_guardian(tmp_path):
    proc, paths = _run_recover(
        tmp_path,
        kernel="Scanning suppressed: status (4)\n",
        python_rc=42,
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.scan_repair_fail iface=wlan0 rc=42" in proc.stderr
    assert "--reason wifi-recover" in _read(paths["guardian_log"])


def test_scan_repair_skipped_when_venv_python_missing(tmp_path):
    """No system-python fallback: if the /opt/jasper venv python is absent,
    the repair is skipped with a clear event (not a misleading ImportError
    failure) and the guardian still runs."""
    proc, paths = _run_recover(
        tmp_path,
        kernel="brcmf_cfg80211_scan: Scanning suppressed: status (4)\n",
        python_path=str(tmp_path / "definitely" / "no" / "python"),
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.scan_suppressed iface=wlan0" in proc.stderr
    assert "event=wifi_recover.scan_repair_skip" in proc.stderr
    assert "reason=no_venv_python" in proc.stderr
    # The fake python is never invoked, and the guardian still runs.
    assert _read(paths["python_log"]) == ""
    assert "--reason wifi-recover" in _read(paths["guardian_log"])


def test_active_wifi_with_colon_in_name_is_parsed(tmp_path):
    """A colon-bearing profile name (nmcli escapes it as `\\:`) must be
    read as active, not mis-split — otherwise recover would needlessly
    treat a healthy box as down."""
    proc, paths = _run_recover(
        tmp_path,
        active="802-11-wireless:Cafe\\:Work\n",
        reason="manual",
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_recover.steady active=Cafe:Work" in proc.stderr
    assert _read(paths["guardian_log"]) == ""


def test_guardian_failure_is_returned(tmp_path):
    proc, paths = _run_recover(tmp_path, guardian_rc=5)

    assert proc.returncode == 5
    assert "event=wifi_recover.guardian_fail rc=5" in proc.stderr
    assert "--reason wifi-recover" in _read(paths["guardian_log"])


def test_no_stash_is_silent_for_systemd_and_visible_for_manual(tmp_path):
    systemd, systemd_paths = _run_recover(tmp_path / "systemd", stash=False)
    manual, manual_paths = _run_recover(
        tmp_path / "manual",
        stash=False,
        reason="manual",
    )

    assert systemd.returncode == 0
    assert systemd.stderr == ""
    assert _read(systemd_paths["nmcli_log"]) == ""
    assert manual.returncode == 0
    assert "event=wifi_recover.absent reason=no_stash" in manual.stderr
    assert _read(manual_paths["nmcli_log"]) == ""
