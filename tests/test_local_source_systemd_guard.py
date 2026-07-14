# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from jasper.accessories.registry import KNOWN_PROFILES
from jasper.local_sources import (
    local_source_lifecycles,
    local_source_park_units,
)


REPO = Path(__file__).resolve().parents[1]
GUARD = "ExecCondition=+/usr/bin/env -i PATH=/opt/jasper/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /opt/jasper/.venv/bin/jasper-local-source-allowed"
SOURCE_GUARD = GUARD
LOADER_ENV_SCRUB = (
    "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES"
)

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

# The composite USB gadget is RECOMPOSED by the source coordinator, not stopped,
# on park —
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
    declared_runtime = {
        unit
        for lifecycle in local_source_lifecycles()
        for unit in lifecycle.runtime_units
    }
    declared_sources = {
        unit: lifecycle.source.value
        for lifecycle in local_source_lifecycles()
        for unit in lifecycle.park_units
    }
    # Every declared source runtime is either source-gated here or is an
    # explicitly recomposed composite owner whose internal audio limb carries
    # the same gate (currently only jasper-usbgadget/NCM).
    # The composite gadget is runtime inventory but not a parkable source
    # unit: its NCM management function must survive source parking.
    assert set(declared_sources) | set(PARK_RESTART_UNIT_DEPLOY_FILES) == declared_runtime
    for unit, path in PARKED_UNIT_DEPLOY_FILES.items():
        assert path.exists(), unit
        text = path.read_text()
        if unit == "jasper-mux.service":
            # Shared arbiter infrastructure has role policy but no one source
            # intent. Every source-owned resource below carries a fixed id.
            assert SOURCE_GUARD in text, unit
            assert f"{SOURCE_GUARD} --source" not in text, unit
            assert LOADER_ENV_SCRUB in text, unit
            continue
        source = declared_sources[unit]
        assert f"{SOURCE_GUARD} --source {source}" in text, unit
        assert LOADER_ENV_SCRUB in text, unit


def test_every_declared_bluetooth_accessory_adapter_has_source_start_guard():
    """Optional adapters inherit Bluetooth Off from registry metadata."""

    services = {
        profile.mic.adapter_service
        for profile in KNOWN_PROFILES
        if profile.mic.status == "adapter" and profile.mic.adapter_service
    }
    assert services
    for service in services:
        path = REPO / "deploy" / "systemd" / service
        assert path.exists(), service
        text = path.read_text()
        assert f"{SOURCE_GUARD} --source bluetooth" in text, service
        assert LOADER_ENV_SCRUB in text, service


def test_source_start_guards_do_not_order_after_the_coordinator():
    """The coordinator starts these units; an After= edge would deadlock."""

    accessory_paths = {
        REPO / "deploy" / "systemd" / str(profile.mic.adapter_service)
        for profile in KNOWN_PROFILES
        if profile.mic.status == "adapter" and profile.mic.adapter_service
    }
    for path in set(PARKED_UNIT_DEPLOY_FILES.values()) | accessory_paths:
        for line in path.read_text().splitlines():
            if line.startswith(("After=", "Requires=")):
                assert "jasper-source-intent-reconcile.service" not in line, path


def test_composite_gadget_is_infrastructure_without_local_source_guard():
    """The composite USB gadget is recomposed, not stopped, on park.

    It carries the always-on USB management network, so it
    must NOT carry the local-source ExecCondition — the network has to survive
    follower parking. The audio-function gate lives inside jasper-usbgadget-up.
    """
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
