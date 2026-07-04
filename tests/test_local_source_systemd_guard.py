# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from jasper.local_sources import (
    local_source_park_restart_units,
    local_source_park_units,
)


REPO = Path(__file__).resolve().parents[1]
GUARD = "ExecCondition=/opt/jasper/.venv/bin/jasper-local-source-allowed"

PARKED_UNIT_DEPLOY_FILES = {
    "shairport-sync.service": REPO / "deploy/systemd/shairport-sync.service",
    "nqptp.service": REPO / "deploy/systemd/nqptp.service",
    "librespot.service": REPO / "deploy/systemd/librespot.service",
    "bluealsa-aplay.service": (
        REPO / "deploy/systemd/bluealsa-aplay.service.d/jts-output.conf"
    ),
    "bluealsa.service": (
        REPO / "deploy/systemd/bluealsa.service.d/jts-restart.conf"
    ),
    "bt-agent.service": REPO / "deploy/systemd/bt-agent.service",
    "jasper-usbsink.service": REPO / "deploy/systemd/jasper-usbsink.service",
    "jasper-usbsink-volume.service": (
        REPO / "deploy/systemd/jasper-usbsink-volume.service"
    ),
    "jasper-mux.service": REPO / "deploy/systemd/jasper-mux.service",
}

# The composite USB gadget is RECOMPOSED (park_restart), not stopped, on park —
# it is infrastructure now (it carries the always-on USB management network),
# NOT a local-source unit. It must NOT carry the local-source ExecCondition:
# the network function has to survive follower parking. The AUDIO-function gate
# lives inside jasper-usbgadget-up instead. This is a deliberate change from
# the pre-composite model where the gadget owner carried the guard.
PARK_RESTART_UNIT_DEPLOY_FILES = {
    "jasper-usbgadget.service": REPO / "deploy/systemd/jasper-usbgadget.service",
}


def test_every_parked_local_source_unit_has_systemd_start_guard():
    assert set(PARKED_UNIT_DEPLOY_FILES) == set(local_source_park_units())
    for unit, path in PARKED_UNIT_DEPLOY_FILES.items():
        assert path.exists(), unit
        assert GUARD in path.read_text(), unit


def test_park_restart_units_are_infrastructure_without_local_source_guard():
    """park_restart units (the composite USB gadget) are recomposed, not
    stopped, on park. They carry the always-on USB management network, so they
    must NOT carry the local-source ExecCondition — the network has to survive
    follower parking. The audio-function gate lives inside jasper-usbgadget-up.
    """
    assert set(PARK_RESTART_UNIT_DEPLOY_FILES) == set(
        local_source_park_restart_units()
    )
    for unit, path in PARK_RESTART_UNIT_DEPLOY_FILES.items():
        assert path.exists(), unit
        assert GUARD not in path.read_text(), (
            f"{unit} must NOT carry the local-source ExecCondition — it is "
            "infrastructure carrying the always-on USB network."
        )


def test_local_source_guard_console_script_is_installed():
    pyproject = (REPO / "pyproject.toml").read_text()
    assert (
        'jasper-local-source-allowed = "jasper.local_sources.guard:main"'
        in pyproject
    )


def test_usbgadget_unloads_gadget_modules_in_dependency_order():
    """The composite gadget owner unloads BOTH the network (usb_f_ncm/u_ether)
    and audio (usb_f_uac2/u_audio) function modules ahead of libcomposite, so a
    clean stop leaves only the ~50 KB dwc2 module. Function modules first, then
    their ether/audio helpers, then libcomposite last."""
    unit = (
        REPO / "deploy/systemd/jasper-usbgadget.service"
    ).read_text().splitlines()
    stop_posts = [
        line.removeprefix("ExecStopPost=-/sbin/rmmod ")
        for line in unit
        if line.startswith("ExecStopPost=-/sbin/rmmod ")
    ]

    assert stop_posts == [
        "usb_f_ncm", "usb_f_uac2", "u_ether", "u_audio", "libcomposite",
    ]
