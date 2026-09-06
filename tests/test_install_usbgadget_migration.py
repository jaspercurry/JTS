# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install-time USB gadget ordering contracts.

The installer expresses no composition intent of its own: it enables the units
and then asks the gadget's converger to reconcile ConfigFS with the shared
truth table. The source-intent coordinator still owns canonical On and its
direct-lane-before-advertising sequence. The shell harness models fresh
installs and upgrades without systemd, ConfigFS, root, or USB hardware.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
NM_DEVICE_POLICY = ROOT / "deploy" / "usb-network" / "90-jasper-usbnet.conf"
PLAN_UNIT = ROOT / "deploy" / "systemd" / "jasper-usb-network-plan.service"
NM_PLAN_DROPIN = (
    ROOT
    / "deploy"
    / "systemd"
    / "NetworkManager.service.d"
    / "jasper-usb-network-plan.conf"
)
GADGET_UNIT = ROOT / "deploy" / "systemd" / "jasper-usbgadget.service"


def _harness(
    tmp_path: Path,
    *,
    gadget_rc: int = 0,
    converge_rc: int = 0,
) -> str:
    """Source the install fragment with a ``systemctl`` shim and a stub
    converger, both of which record every call in order."""

    log = tmp_path / "calls.log"
    converge = tmp_path / "jasper-usbgadget-converge"
    converge.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "converge %s\\n" "$*" >> "{log}"\n'
        f"exit {converge_rc}\n"
    )
    converge.chmod(0o755)
    return f"""
set -uo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path / 'systemd'}"
JASPER_USBGADGET_CONVERGE="{converge}"
systemctl() {{
  echo "$*" >> "{log}"
  if [[ "${{1:-}}" == "enable" && "$*" == *--now* && "$*" == *jasper-usbgadget.service* ]]; then
    return {gadget_rc}
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
    log = tmp_path / "calls.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_install_enables_the_units_then_converges_once(tmp_path):
    proc, calls = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    compose_idx = calls.index("enable --now jasper-usbgadget.service")
    converge_idx = calls.index("converge install")
    assert compose_idx < converge_idx
    assert calls.count("converge install") == 1


def test_install_never_restarts_the_gadget_or_its_consumers_itself(tmp_path):
    """#3194: install is not an owner of the descriptor or of the blast radius.

    Every gadget teardown/rebuild, and the fan-in -> usbmic refresh that has to
    follow one, belongs to the converger. An installer that restarts the unit
    directly is the deploy-time rebind this issue is about.
    """

    _proc, calls = _run(tmp_path)

    assert "restart jasper-usbgadget.service" not in calls
    assert "try-restart jasper-fanin.service" not in calls
    assert "try-restart jasper-usbmic.service" not in calls


def test_install_expresses_no_composition_intent_of_its_own(tmp_path):
    """The old baseline parked jasper-usbsink first, which flipped the desired
    composition Off and made the coordinator flip it back — the double bind.
    The truth table's live-consumer gate now owns the anti-stale decision."""

    _proc, calls = _run(tmp_path)

    for call in calls:
        assert "jasper-usbsink.service" not in call, call


def test_failed_converge_refuses_to_continue(tmp_path):
    proc, calls = _run(tmp_path, converge_rc=1)

    assert proc.returncode != 0
    assert "possibly stale UAC2 advertised" in proc.stderr
    assert "converge install" in calls


def test_enable_usbgadget_enables_device_activated_dhcp(tmp_path):
    _proc, calls = _run(tmp_path)

    assert "enable jasper-usbnet-dhcp.service" in calls


def test_enable_usbgadget_reports_real_gadget_failure(tmp_path):
    proc, _calls = _run(tmp_path, gadget_rc=1)

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


def test_installer_stages_the_converger_and_its_shared_truth_table():
    source = FRAGMENT.read_text()

    assert "deploy/usbsink/jasper-usbgadget-converge" in source
    assert "/usr/local/sbin/jasper-usbgadget-converge" in source
    assert "deploy/usbsink/jasper-usbgadget-compose.sh" in source
    assert "/usr/local/sbin/jasper-usbgadget-compose.sh" in source


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
    assert "jasper.usb_network converge" in body
    assert "jasper.usb_network stage" not in body
    assert "jasper.usb_network promote" not in body
    nm_snapshot = body.index(
        '_snapshot_unit_install_destination "${nm_path}"'
    )
    dnsmasq_snapshot = body.index(
        '_snapshot_unit_install_destination "${dnsmasq_path}"'
    )
    converge = body.index("jasper.usb_network converge")
    assert nm_snapshot < dnsmasq_snapshot < converge
    assert "usb_network_migration_pending" in body
    assert "live_files=preserved" in body
    assert "nmcli --wait 10 general reload conf" in body
    assert "nmcli --wait 10 connection load" in body
    assert "/etc/NetworkManager/system-connections/jts-usb.nmconnection" in body
    assert "nmcli --wait 10 connection reload" not in body
    assert "nmcli --wait 10 device set usb0 managed yes" in body
    assert "nmcli --wait 10 -t -f NAME,DEVICE connection show --active" in body
    assert "nmcli --wait 10 connection up jts-usb ifname usb0" in body
    assert "event=install.usb_network_converged" in body


def test_usb_network_boot_gate_blocks_gadget_but_never_wifi_on_plan_failure():
    plan_unit = PLAN_UNIT.read_text(encoding="utf-8")
    nm_dropin = NM_PLAN_DROPIN.read_text(encoding="utf-8")
    gadget = GADGET_UNIT.read_text(encoding="utf-8")

    assert "Before=NetworkManager.service jasper-usbgadget.service" in plan_unit
    assert "ExecStart=/opt/jasper/.venv/bin/python -m jasper.usb_network promote" in plan_unit
    assert "Requires=jasper-usb-network-plan.service" in gadget
    assert "jasper-usb-network-plan.service" in next(
        line for line in gadget.splitlines() if line.startswith("After=")
    )
    assert "Wants=jasper-usb-network-plan.service" in nm_dropin
    assert "After=jasper-usb-network-plan.service" in nm_dropin
    assert "Requires=jasper-usb-network-plan.service" not in nm_dropin


def test_deferred_install_never_replaces_either_live_projection():
    source = FRAGMENT.read_text(encoding="utf-8")
    body = source.split("install_usb_network_files() {", 1)[1].split("\n}\n", 1)[0]

    pending_start = body.index('if [[ -e "${pending_path}" ]]')
    return_index = body.index("return 0", pending_start)
    nmcli_index = body.index("if command -v nmcli", pending_start)
    assert pending_start < return_index < nmcli_index
    pending_branch = body[pending_start:return_index]
    assert "nmcli" not in pending_branch
    assert "install -m 0600" not in body
    assert "install -m 0644" not in body.split('local plan_path=', 1)[1]


def test_usb_network_plan_trust_anchor_is_root_owned_and_separate_from_shared_state():
    source = FRAGMENT.read_text(encoding="utf-8")
    body = source.split("install_usb_network_files() {", 1)[1].split("\n}\n", 1)[0]
    plan_unit = PLAN_UNIT.read_text(encoding="utf-8")

    assert 'usb_network_state_dir="/var/lib/jasper-usb-network"' in body
    assert 'install -d -o root -g root -m 0755 "${usb_network_state_dir}"' in body
    assert 'install -d -m 0755 "${STATE_DIR}"' not in body
    assert "ReadWritePaths=/etc/NetworkManager/system-connections /etc/jasper /var/lib/jasper-usb-network" in plan_unit
    assert " /var/lib/jasper\n" not in plan_unit
