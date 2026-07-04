# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `enable_usbgadget` in deploy/lib/install/systemd-units.sh.

The regression this guards (adversarial review core-0): on an upgrade,
`migrate_usbsink_init_to_usbgadget` runs `systemctl disable --now
jasper-usbsink-init.service` while the OLD in-memory unit graph still has
jasper-usbsink `PartOf=jasper-usbsink-init`, so that stop PROPAGATES and leaves
an enabled (possibly playing) USB-audio bridge STOPPED. `enable_usbgadget`'s
`enable --now jasper-usbgadget.service` is only a START, and PartOf= never
propagates a start — so without an explicit restore, the deploy ends with the
gadget composed but the bridge daemon down until reboot.

`enable_usbgadget` therefore does a restore-if-enabled (`start
jasper-usbsink.service` iff `is-enabled --quiet` is true). This must run
UNCONDITIONALLY (not gated on SKIP_RESTART) because the migration's stop is
itself unconditional.

The fragment is sourced into a harness with stub install.sh globals plus a
`systemctl` shim that records calls and drives the `is-enabled` result, so the
function is exercised hardware-free and root-free (the pattern the core
audio-graph loop / migration tests already use).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"


def _harness(tmp_path: Path, *, usbsink_enabled: bool, gadget_rc: int = 0) -> str:
    """Bash that sources the fragment with a `systemctl` shim and invokes
    `enable_usbgadget`.

    `usbsink_enabled` drives the shim's `is-enabled --quiet jasper-usbsink`
    result; `gadget_rc` makes `enable --now jasper-usbgadget.service` fail so
    the failure-echo branch can be exercised.
    """
    log = tmp_path / "systemctl.log"
    enabled_rc = 0 if usbsink_enabled else 1
    return f"""
set -uo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / "systemd"}"
systemctl() {{
  echo "$*" >> "{log}"
  # `is-enabled --quiet jasper-usbsink.service` drives the restore branch.
  if [[ "${{1:-}}" == "is-enabled" && "$*" == *jasper-usbsink.service* ]]; then
    return {enabled_rc}
  fi
  # `enable --now jasper-usbgadget.service` can be forced to fail.
  if [[ "${{1:-}}" == "enable" && "$*" == *--now* && "$*" == *jasper-usbgadget.service* ]]; then
    return {gadget_rc}
  fi
  return 0
}}
source "{FRAGMENT}"
enable_usbgadget
"""


def _run(tmp_path: Path, *, usbsink_enabled: bool, gadget_rc: int = 0):
    script = _harness(tmp_path, usbsink_enabled=usbsink_enabled, gadget_rc=gadget_rc)
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=20,
    )
    log = tmp_path / "systemctl.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_enable_usbgadget_restarts_bridge_when_usb_audio_enabled(tmp_path):
    """core-0: with USB audio enabled, enable_usbgadget must `start
    jasper-usbsink.service` so the migration's PartOf stop doesn't strand it."""
    proc, calls = _run(tmp_path, usbsink_enabled=True)
    assert proc.returncode == 0, proc.stderr
    assert "enable --now jasper-usbgadget.service" in calls
    assert "is-enabled --quiet jasper-usbsink.service" in calls
    assert "start jasper-usbsink.service" in calls, (
        "an enabled USB-audio bridge must be restored after the gadget migration"
    )


def test_enable_usbgadget_leaves_bridge_stopped_when_usb_audio_disabled(tmp_path):
    """The restore is is-enabled-gated: a household that never enabled USB audio
    must not have the bridge started by a deploy."""
    proc, calls = _run(tmp_path, usbsink_enabled=False)
    assert proc.returncode == 0, proc.stderr
    assert "is-enabled --quiet jasper-usbsink.service" in calls
    assert "start jasper-usbsink.service" not in calls


def test_enable_usbgadget_enables_device_activated_dhcp(tmp_path):
    """The scoped dnsmasq is enabled (device-activated, so `enable` wires the
    WantedBy pull without starting it until usb0 appears)."""
    _proc, calls = _run(tmp_path, usbsink_enabled=False)
    assert "enable jasper-usbnet-dhcp.service" in calls


def test_enable_usbgadget_reports_real_gadget_failure(tmp_path):
    """core-9: a genuine gadget enable failure prints a journalctl-pointing
    warning (NOT the old 'likely no UDC' misdirection — an ExecCondition skip
    would have returned rc=0 and never reached this branch)."""
    proc, _calls = _run(tmp_path, usbsink_enabled=False, gadget_rc=1)
    assert proc.returncode == 0, proc.stderr
    assert "journalctl -u jasper-usbgadget" in proc.stdout
    assert "no UDC yet" not in proc.stdout
