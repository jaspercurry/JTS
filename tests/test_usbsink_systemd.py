# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the process-free USB Audio Input readiness marker."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)

REPO = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbsink.service"
GADGET_UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbgadget.service"
NAME_INDEX_UNIT_PATH = (
    REPO / "deploy" / "systemd" / "jasper-usbsink-name-index.service"
)
FORENSICS_SERVICE_PATH = REPO / "deploy" / "systemd" / "jasper-usbgadget-forensics.service"
FORENSICS_PATH_PATH = REPO / "deploy" / "systemd" / "jasper-usbgadget-forensics.path"
HARDWARE_RECONCILE_UNIT_PATH = (
    REPO / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service"
)
PYPROJECT_PATH = REPO / "pyproject.toml"
INSTALL_HELPER_PATH = REPO / "deploy" / "lib" / "install" / "systemd-units.sh"

USB_ROLE_TEST_SEAMS = {
    "JASPER_PI_MODEL_FILE",
    "JTS_BOOT_CONFIG_FILE",
    "JASPER_UDC_CLASS_DIR",
}


def _directive_index(unit_text: str, prefix: str, needle: str) -> int:
    for index, line in enumerate(unit_text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(prefix) and needle in stripped:
            return index
    raise AssertionError(f"no {prefix!r} directive containing {needle!r}")


def test_readiness_marker_keeps_both_guards_before_bounded_card_wait():
    body = UNIT_PATH.read_text()
    assert "ExecCondition=" in body
    assert "jasper-local-source-allowed --source usbsink" in body
    assert (
        "ExecCondition=/bin/test -d "
        "/sys/kernel/config/usb_gadget/jts-usb-audio/functions/uac2.usb0"
    ) in body
    assert _directive_index(body, "ExecCondition=", "functions/uac2.usb0") < (
        _directive_index(body, "ExecStartPre=", "jasper-usbsink-wait-card")
    )
    assert _assignments_for(body, "ExecStartPre") == (
        "/usr/local/sbin/jasper-usbsink-wait-card 30",
    )
    assert _value_for(body, "TimeoutStartSec") == "40s"
    assert _value_for(body, "TimeoutStopSec") == "5s"


def test_readiness_marker_is_process_free_and_reproved_with_gadget_lifecycle():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "Type") == "oneshot"
    assert _value_for(body, "RemainAfterExit") == "yes"
    assert _assignments_for(body, "ExecStart") == ("/bin/true",)
    assert "jasper-usbgadget.service" in _values_for(body, "Requires")
    assert "jasper-usbgadget.service" in _values_for(body, "PartOf")
    assert "jasper-usbsink-volume.service" in _values_for(body, "Wants")

    for retired in (
        "jasper-usbsink-audio",
        "WatchdogSec",
        "Restart=",
        "RuntimeDirectory",
        "MemoryHigh",
        "MemoryMax",
        "OOMScoreAdjust",
        "EnvironmentFile",
    ):
        assert retired not in body


def test_gadget_waits_for_reconciled_hardware_capability() -> None:
    body = GADGET_UNIT_PATH.read_text()
    assert "jasper-audio-hardware-reconcile.service" in (
        _values_for(body, "After")
    )
    assert "jasper-audio-hardware-reconcile.service" in (
        _values_for(body, "Wants")
    )
    unset = set(_values_for(body, "UnsetEnvironment"))
    assert USB_ROLE_TEST_SEAMS <= unset
    assert "JASPER_USBGADGET_HARDWARE_ALLOWED_CMD" in unset


def test_gadget_snapshots_controller_before_reset_and_after_bind() -> None:
    body = GADGET_UNIT_PATH.read_text()
    pre_reset = _directive_index(body, "ExecStop=", "snapshot pre_reset")
    teardown = _directive_index(body, "ExecStop=", "jasper-usbgadget-down")

    assert pre_reset < teardown
    assert "ExecStartPost=-/usr/bin/timeout 2s " in body
    assert "jasper-usbgadget-snapshot post_start" in body
    assert "ExecStop=-/usr/bin/timeout 2s " in body
    assert "jasper-usbgadget-snapshot pre_reset" in body

    unset = set(_values_for(body, "UnsetEnvironment"))
    assert {
        "JASPER_USBGADGET_SNAPSHOT_CONFIGFS_ROOT",
        "JASPER_USBGADGET_SNAPSHOT_UDC_CLASS_DIR",
        "JASPER_USBGADGET_SNAPSHOT_DEBUG_ROOT",
        "JASPER_USBGADGET_SNAPSHOT_PROC_INTERRUPTS",
        "JASPER_USBGADGET_SNAPSHOT_USB_MIC_STATUS",
        "JASPER_USBGADGET_SNAPSHOT_DIR",
    } <= unset


def test_installer_ships_usb_gadget_snapshot_helper() -> None:
    body = INSTALL_HELPER_PATH.read_text()
    assert 'deploy/usbsink/jasper-usbgadget-snapshot"' in body
    assert "/usr/local/sbin/jasper-usbgadget-snapshot" in body


def test_forensics_is_opt_in_ram_bounded_and_deploy_persistent() -> None:
    from jasper.control import usb_gadget_forensics

    service = FORENSICS_SERVICE_PATH.read_text()
    path = FORENSICS_PATH_PATH.read_text()
    install = INSTALL_HELPER_PATH.read_text()

    assert "ConditionPathExists=/var/lib/jasper/usb_gadget_forensics.env" in service
    assert usb_gadget_forensics.ENABLED_FILE in service
    assert usb_gadget_forensics.ENABLED_FILE in path
    assert "ExecStart=/usr/local/sbin/jasper-usbgadget-snapshot watch" in service
    assert "MemoryMax=32M" in service
    assert "RuntimeDirectory=jasper-usb-gadget-forensics" in service
    assert "ReadWritePaths=/var/lib/jasper/usb-gadget-incidents" in service
    assert "CapabilityBoundingSet=" in service
    assert "jasper-usbgadget.service" not in service
    assert "PathExists=/var/lib/jasper/usb_gadget_forensics.env" in path
    assert "WantedBy=multi-user.target" in path
    assert "jasper-usbgadget-forensics.path" in install
    assert "install -d -m 0750 /var/lib/jasper/usb-gadget-incidents" in install
    assert "systemctl enable --now jasper-usbgadget-forensics.path" in install
    assert "systemctl try-restart jasper-usbgadget-forensics.service" in install

    unset = set(_values_for(service, "UnsetEnvironment"))
    assert {
        "JASPER_USBGADGET_FORENSICS_ENABLED_FILE",
        "JASPER_USBGADGET_FORENSICS_RUN_DIR",
        "JASPER_USBGADGET_FORENSICS_INTERVAL",
        "JASPER_USBGADGET_FORENSICS_MAX_BYTES",
        # The repair action's systemctl seam (jasper-usbgadget-converge's own
        # JASPER_USBGADGET_SYSTEMCTL pattern): a test-only override, so this
        # always-installed root daemon must never inherit one.
        "JASPER_USBGADGET_SYSTEMCTL",
    } <= unset


def test_hardware_reconciler_strips_usb_role_test_seams() -> None:
    body = HARDWARE_RECONCILE_UNIT_PATH.read_text()
    unset = set(_values_for(body, "UnsetEnvironment"))

    assert USB_ROLE_TEST_SEAMS <= unset


def test_readiness_marker_remains_unprivileged_and_read_only():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "User") == "jasper-recon"
    assert _value_for(body, "Group") == "jasper"
    assert _values_for(body, "CapabilityBoundingSet") == ()
    assert _values_for(body, "AmbientCapabilities") == ()
    assert _value_for(body, "NoNewPrivileges") == "true"
    assert _value_for(body, "ProtectSystem") == "strict"
    assert _value_for(body, "ProtectHome") == "true"
    assert _value_for(body, "PrivateDevices") == "true"


def test_only_volume_observer_keeps_a_usb_console_script():
    scripts = tomllib.loads(PYPROJECT_PATH.read_text())["project"]["scripts"]
    assert "jasper-usbsink-python-lab" not in scripts
    assert "jasper-usbsink" not in scripts
    assert scripts.get("jasper-usbsink-volume") == (
        "jasper.cli.usbsink_volume_main:main"
    )


def test_name_index_unit_keeps_depmod_off_the_gadget_start_budget() -> None:
    """#2176: `depmod` measured 10.3 s on a Pi Zero 2 W, over 2x the gadget
    unit's TimeoutStartSec=5s — and that 5 s is mirrored into
    jasper/source_intent.py as the single source of truth for a derived
    timeout budget that reaches install.sh and three nginx
    proxy_read_timeout values, so it must not move to accommodate depmod.

    The structural invariant that replaces a timeout bump: the gadget's
    ExecStartPre is the fast publish half only, and the slow half lives in
    its own unit with its own generous, bounded start timeout. Own unit =
    own cgroup, so the `systemctl restart jasper-usbgadget` install.sh
    issues right after `enable --now` cannot SIGTERM the rebuild."""
    gadget = GADGET_UNIT_PATH.read_text()
    index_unit = NAME_INDEX_UNIT_PATH.read_text()

    # The gadget still runs only the fast publish half, with no argument that
    # would drag the index phase back onto its start path.
    assert (
        "-/usr/local/sbin/jasper-usbsink-name-patch"
        in _assignments_for(gadget, "ExecStartPre")
    )
    assert "jasper-usbsink-name-patch --index" not in gadget
    assert _value_for(gadget, "TimeoutStartSec") == "5s"

    assert _value_for(index_unit, "Type") == "oneshot"
    assert _assignments_for(index_unit, "ExecStart") == (
        "/usr/local/sbin/jasper-usbsink-name-patch --index",
    )
    # RemainAfterExit must stay unset. `systemctl start` on an already-active
    # oneshot is a no-op, so RemainAfterExit=yes would make the first rebuild
    # succeed and every later kick silently do nothing — a rename or kernel
    # update would never re-index and the marker would never promote, which
    # is #2176 wearing a different hat.
    assert _value_for(index_unit, "RemainAfterExit") is None
    # Bounded (a wedged depmod must not sit forever) but far above the
    # 10.3 s measurement, and — unlike the gadget's — not part of any
    # client budget chain, because nothing waits on this unit.
    timeout = _value_for(index_unit, "TimeoutStartSec")
    assert timeout.endswith("s")
    assert 60 <= int(timeout[:-1]) <= 600, timeout

    # Ordering it Before= the gadget would put depmod straight back on the
    # start path this unit exists to keep it off. (Directive-aware: the unit's
    # own comments explain the choice and must not satisfy this.)
    assert _values_for(index_unit, "Before") == ()
    # Kick-only: started on demand by the publish half, never boot-enabled.
    sections = [
        line.strip() for line in index_unit.splitlines()
        if line.strip().startswith("[")
    ]
    assert "[Install]" not in sections, sections

    install = INSTALL_HELPER_PATH.read_text()
    assert 'deploy/systemd/jasper-usbsink-name-index.service"' in install
