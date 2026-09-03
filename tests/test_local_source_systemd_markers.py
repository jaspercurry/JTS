# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from jasper.accessories.registry import KNOWN_PROFILES
from jasper.local_sources import (
    local_source_lifecycles,
    local_source_park_units,
)
from jasper.local_sources.markers import MARKER_DIR, SHARED_LABEL, marker_path


REPO = Path(__file__).resolve().parents[1]
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

# The composite USB gadget is recomposed, not put in the ordinary source-park
# stop set. It is hardware-gated infrastructure that may carry the default-on
# management network, not a local-source unit. Its own hardware condition and
# internal audio gate decide which functions survive follower parking. This is
# a deliberate change from the pre-composite model where the gadget owner
# carried the local-source guard.
PARK_RESTART_UNIT_DEPLOY_FILES = {
    "jasper-usbgadget.service": REPO / "deploy/systemd/jasper-usbgadget.service",
}


def test_every_parked_local_source_unit_has_systemd_start_marker():
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
            # intent. Every source-owned resource below carries a fixed label.
            assert f"ConditionPathExists={marker_path(SHARED_LABEL)}" in text, unit
            assert LOADER_ENV_SCRUB in text, unit
            continue
        source = declared_sources[unit]
        assert f"ConditionPathExists={marker_path(source)}" in text, unit
        assert LOADER_ENV_SCRUB in text, unit


def _adapter_host_units() -> set[Path]:
    hosts = {
        str(profile.mic.adapter_host_service)
        for profile in KNOWN_PROFILES
        if profile.mic.status == "adapter" and profile.mic.adapter_host_service
    }
    assert hosts
    return {REPO / "deploy" / "systemd" / host for host in hosts}


def test_an_accessory_adapter_host_is_not_gated_on_the_bluetooth_marker():
    """Adapters run inside the always-on HID bridge (ADR-0225), so their host
    unit must start with Bluetooth Off — its buttons are USB-capable and its
    push-to-talk is how a wired mic is used. Bluetooth Off reaches the adapter
    through jasper-accessory-reconcile, which publishes no source and refreshes
    the host, rather than through a unit Condition that would take the buttons
    down with the microphone."""

    for path in _adapter_host_units():
        assert path.exists(), path
        text = path.read_text()
        assert f"ConditionPathExists={MARKER_DIR}/" not in text, path
        assert LOADER_ENV_SCRUB in text, path


def test_source_start_markers_do_not_order_after_the_coordinator():
    """The coordinator starts these units; an After= edge would deadlock."""

    accessory_paths = _adapter_host_units()
    for path in set(PARKED_UNIT_DEPLOY_FILES.values()) | accessory_paths:
        for line in path.read_text().splitlines():
            if line.startswith(("After=", "Requires=")):
                assert "jasper-source-intent-reconcile.service" not in line, path


def test_composite_gadget_is_infrastructure_without_local_source_marker():
    """The composite USB gadget is recomposed, not stopped, on park.

    When hardware permits it, the gadget carries the default-on management
    network, so it must not carry a source marker gate: follower parking
    withdraws audio without dropping NCM. The hardware and audio-function
    gates live inside the gadget boundary.
    """
    for unit, path in PARK_RESTART_UNIT_DEPLOY_FILES.items():
        assert path.exists(), unit
        text = path.read_text()
        assert f"ConditionPathExists={MARKER_DIR}/" not in text, (
            f"{unit} must NOT carry a source-intent marker gate — it is "
            "hardware-gated infrastructure that may carry the USB network."
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
