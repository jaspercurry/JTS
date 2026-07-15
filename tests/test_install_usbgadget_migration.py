# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install-time USB gadget ordering contracts.

The installer must never advertise UAC2 from stale derived enablement. It first
parks ``jasper-usbsink`` and establishes an NCM-only gadget; the later source-
intent coordinator owns canonical On and its direct-lane-before-advertising
sequence. The shell harness models fresh installs and upgrades without systemd,
ConfigFS, root, or USB hardware.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
NM_DEVICE_POLICY = ROOT / "deploy" / "usb-network" / "90-jasper-usbnet.conf"


def _harness(
    tmp_path: Path,
    *,
    derived_enabled: bool,
    derived_active: bool | None = None,
    gadget_active: bool = False,
    gadget_rc: int = 0,
    park_rc: int = 0,
    restart_rc: int = 0,
    uac2_present: bool = False,
) -> str:
    """Source the install fragment with a stateful ``systemctl`` shim."""

    log = tmp_path / "systemctl.log"
    enabled_state = 1 if derived_enabled else 0
    active_state = enabled_state if derived_active is None else int(derived_active)
    gadget_active_state = 1 if gadget_active else 0
    uac2_path = tmp_path / "UAC2Gadget"
    if uac2_present:
        uac2_path.mkdir(exist_ok=True)
    return f"""
set -uo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / 'systemd'}"
JASPER_UAC2_CARD_PATH="{uac2_path}"
USBSINK_ENABLED={enabled_state}
USBSINK_ACTIVE={active_state}
systemctl() {{
  echo "$*" >> "{log}"
  if [[ "${{1:-}}" == "is-enabled" && "$*" == *jasper-usbsink.service* ]]; then
    [[ "${{USBSINK_ENABLED}}" == "1" ]]
    return
  fi
  if [[ "${{1:-}}" == "is-active" && "$*" == *jasper-usbsink.service* ]]; then
    [[ "${{USBSINK_ACTIVE}}" == "1" ]]
    return
  fi
  if [[ "${{1:-}}" == "is-active" && "$*" == *jasper-usbgadget.service* ]]; then
    return $((1 - {gadget_active_state}))
  fi
  if [[ "${{1:-}}" == "disable" && "$*" == *jasper-usbsink.service* ]]; then
    if [[ {park_rc} != 0 ]]; then return {park_rc}; fi
    USBSINK_ENABLED=0
    USBSINK_ACTIVE=0
    return 0
  fi
  if [[ "${{1:-}}" == "enable" && "$*" == *--now* && "$*" == *jasper-usbgadget.service* ]]; then
    return {gadget_rc}
  fi
  if [[ "${{1:-}}" == "restart" && "$*" == *jasper-usbgadget.service* ]]; then
    return {restart_rc}
  fi
  return 0
}}
source "{FRAGMENT}"
enable_usbgadget
"""


def _run(tmp_path: Path, **kwargs):
    proc = subprocess.run(
        ["bash", "-c", _harness(tmp_path, **kwargs)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    log = tmp_path / "systemctl.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_fresh_install_establishes_audio_off_before_ncm_gadget(tmp_path):
    proc, calls = _run(tmp_path, derived_enabled=False)

    assert proc.returncode == 0, proc.stderr
    park_idx = calls.index("disable --now jasper-usbsink.service")
    compose_idx = calls.index("enable --now jasper-usbgadget.service")
    assert park_idx < compose_idx
    assert "enable jasper-usbsink.service" not in calls
    assert "start jasper-usbsink.service" not in calls
    assert "event=install.usb_gadget_baseline audio=off" in proc.stdout


def test_upgrade_on_state_is_parked_without_install_side_restore(tmp_path):
    """Canonical On is intentionally restored later by the coordinator."""

    proc, calls = _run(
        tmp_path,
        derived_enabled=True,
        derived_active=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert calls.index("disable --now jasper-usbsink.service") < calls.index(
        "enable --now jasper-usbgadget.service"
    )
    assert "enable jasper-usbsink.service" not in calls
    assert "start jasper-usbsink.service" not in calls


def test_active_upgrade_recomposes_network_only_after_parking_audio(tmp_path):
    """A bound descriptor must not take gadget-up's stale idempotent fast path."""

    proc, calls = _run(
        tmp_path,
        derived_enabled=True,
        derived_active=True,
        gadget_active=True,
    )

    assert proc.returncode == 0, proc.stderr
    park_idx = calls.index("disable --now jasper-usbsink.service")
    compose_idx = calls.index("enable --now jasper-usbgadget.service")
    recompose_idx = calls.index("restart jasper-usbgadget.service")
    assert park_idx < compose_idx < recompose_idx


def test_active_ncm_only_gadget_is_not_flapped_on_every_deploy(tmp_path):
    """A deploy over the NCM management link must preserve converged NCM."""

    proc, calls = _run(
        tmp_path,
        derived_enabled=False,
        derived_active=False,
        gadget_active=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "restart jasper-usbgadget.service" not in calls


def test_active_gadget_recomposes_when_uac2_card_proves_descriptor_drift(tmp_path):
    proc, calls = _run(
        tmp_path,
        derived_enabled=False,
        derived_active=False,
        gadget_active=True,
        uac2_present=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "restart jasper-usbgadget.service" in calls


def test_inactive_failed_gadget_recomposes_when_uac2_descriptor_survived(tmp_path):
    """A failed oneshot can leave ConfigFS bound even while systemd is inactive."""

    proc, calls = _run(
        tmp_path,
        derived_enabled=False,
        derived_active=False,
        gadget_active=False,
        uac2_present=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "restart jasper-usbgadget.service" in calls


def test_failed_audio_park_refuses_to_compose(tmp_path):
    proc, calls = _run(
        tmp_path,
        derived_enabled=True,
        derived_active=True,
        park_rc=1,
    )

    assert proc.returncode != 0
    assert "could not park USB Audio Input" in proc.stderr
    assert "enable --now jasper-usbgadget.service" not in calls


def test_failed_network_only_recompose_refuses_to_continue(tmp_path):
    proc, calls = _run(
        tmp_path,
        derived_enabled=False,
        gadget_active=True,
        uac2_present=True,
        restart_rc=1,
    )

    assert proc.returncode != 0
    assert "possibly stale UAC2 advertised" in proc.stderr
    assert "restart jasper-usbgadget.service" in calls


def test_enable_usbgadget_enables_device_activated_dhcp(tmp_path):
    _proc, calls = _run(tmp_path, derived_enabled=False)

    assert "enable jasper-usbnet-dhcp.service" in calls


def test_enable_usbgadget_reports_real_gadget_failure(tmp_path):
    proc, _calls = _run(
        tmp_path,
        derived_enabled=False,
        gadget_rc=1,
    )

    assert proc.returncode == 0, proc.stderr
    assert "journalctl -u jasper-usbgadget" in proc.stdout
    assert "no UDC yet" not in proc.stdout


def test_enable_usbgadget_does_not_interpret_or_restore_canonical_on():
    source = FRAGMENT.read_text()
    body = source.split("enable_usbgadget() {", 1)[1].split("\n}\n", 1)[0]

    assert "canonical_usbsink_intent_enabled" not in source
    assert "source_intent_enabled" not in body
    assert "systemctl enable jasper-usbsink.service" not in body
    assert "systemctl start jasper-usbsink.service" not in body


def test_usbnet_networkmanager_policy_owns_only_usb0_without_carrier():
    """Override the OS gadget default narrowly; keep carrierless static IP up."""

    policy = NM_DEVICE_POLICY.read_text(encoding="utf-8")

    assert "[device-jts-usb]" in policy
    assert "match-device=interface-name:usb0" in policy
    assert "managed=1" in policy
    assert "ignore-carrier=yes" in policy
    assert "match-device=*" not in policy


def test_usbnet_install_reloads_policy_and_bounds_existing_device_activation():
    """Upgrades converge an existing usb0; later recreation is NM-owned."""

    source = FRAGMENT.read_text(encoding="utf-8")
    body = source.split("install_usb_network_files() {", 1)[1].split("\n}\n", 1)[0]

    assert 'deploy/usb-network/90-jasper-usbnet.conf"' in body
    assert "/etc/NetworkManager/conf.d/90-jasper-usbnet.conf" in body
    assert "nmcli --wait 10 general reload conf" in body
    assert "nmcli --wait 10 connection load" in body
    assert "/etc/NetworkManager/system-connections/jts-usb.nmconnection" in body
    assert "nmcli --wait 10 connection reload" not in body
    assert "nmcli --wait 10 device set usb0 managed yes" in body
    assert "nmcli --wait 10 -t -f NAME,DEVICE connection show --active" in body
    assert "nmcli --wait 10 connection up jts-usb ifname usb0" in body
    assert "event=install.usb_network_converged" in body
