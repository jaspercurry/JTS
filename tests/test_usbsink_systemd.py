# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the process-free USB Audio Input readiness marker."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbsink.service"
PYPROJECT_PATH = REPO / "pyproject.toml"


def _value_for(unit_text: str, key: str) -> str | None:
    val: str | None = None
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        found, _, value = stripped.partition("=")
        if found == key:
            val = value
    return val


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
    assert _value_for(body, "ExecStartPre") == (
        "/usr/local/sbin/jasper-usbsink-wait-card 30"
    )
    assert _value_for(body, "TimeoutStartSec") == "40s"
    assert _value_for(body, "TimeoutStopSec") == "5s"


def test_readiness_marker_is_process_free_and_reproved_with_gadget_lifecycle():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "Type") == "oneshot"
    assert _value_for(body, "RemainAfterExit") == "yes"
    assert _value_for(body, "ExecStart") == "/bin/true"
    assert "jasper-usbgadget.service" in (_value_for(body, "Requires") or "")
    assert "jasper-usbgadget.service" in (_value_for(body, "PartOf") or "")
    assert "jasper-usbsink-volume.service" in (_value_for(body, "Wants") or "")

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


def test_readiness_marker_remains_unprivileged_and_read_only():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "User") == "jasper-recon"
    assert _value_for(body, "Group") == "jasper"
    assert _value_for(body, "CapabilityBoundingSet") == ""
    assert _value_for(body, "AmbientCapabilities") == ""
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
