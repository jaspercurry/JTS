# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from jasper.local_sources import local_source_park_units


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
    "jasper-usbsink-init.service": (
        REPO / "deploy/systemd/jasper-usbsink-init.service"
    ),
    "jasper-usbsink.service": REPO / "deploy/systemd/jasper-usbsink.service",
    "jasper-usbsink-volume.service": (
        REPO / "deploy/systemd/jasper-usbsink-volume.service"
    ),
    "jasper-mux.service": REPO / "deploy/systemd/jasper-mux.service",
}


def test_every_parked_local_source_unit_has_systemd_start_guard():
    assert set(PARKED_UNIT_DEPLOY_FILES) == set(local_source_park_units())
    for unit, path in PARKED_UNIT_DEPLOY_FILES.items():
        assert path.exists(), unit
        assert GUARD in path.read_text(), unit


def test_local_source_guard_console_script_is_installed():
    pyproject = (REPO / "pyproject.toml").read_text()
    assert (
        'jasper-local-source-allowed = "jasper.local_sources.guard:main"'
        in pyproject
    )


def test_usbsink_init_unloads_gadget_modules_in_dependency_order():
    unit = (
        REPO / "deploy/systemd/jasper-usbsink-init.service"
    ).read_text().splitlines()
    stop_posts = [
        line.removeprefix("ExecStopPost=-/sbin/rmmod ")
        for line in unit
        if line.startswith("ExecStopPost=-/sbin/rmmod ")
    ]

    assert stop_posts == ["usb_f_uac2", "u_audio", "libcomposite"]
