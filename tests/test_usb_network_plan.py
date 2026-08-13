# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import ipaddress
import multiprocessing
import socket
import time
from pathlib import Path

import pytest

from jasper import atomic_io, usb_network
from jasper.usb_network import (
    ALLOCATION_SUPERNET,
    IPv4Observation,
    IPv4ObservationState,
    UsbNetworkPlanError,
    apply_plan,
    attest_plan,
    derive_plan,
    load_plan,
    observe_ipv4_cidr,
    owner_lock,
    promote_plan,
    read_cpu_serial,
    render_dnsmasq,
    render_nmconnection,
    write_plan,
)


def test_same_serial_derives_same_plan():
    assert derive_plan("10000000abcdef01") == derive_plan("10000000ABCDEF01")


def test_different_serials_derive_non_overlapping_point_to_point_subnets():
    left = derive_plan("1111111111111111")
    right = derive_plan("2222222222222222")

    assert left.subnet != right.subnet
    assert not ipaddress.IPv4Network(left.subnet).overlaps(
        ipaddress.IPv4Network(right.subnet)
    )


def test_plan_has_valid_network_device_host_broadcast_geometry():
    plan = derive_plan("10000000abcdef01")
    subnet = ipaddress.IPv4Network(plan.subnet)

    assert subnet.prefixlen == 30
    assert subnet.subnet_of(ALLOCATION_SUPERNET)
    assert tuple(map(str, subnet.hosts())) == (plan.device_address, plan.host_address)
    assert str(subnet.broadcast_address) == plan.broadcast_address
    assert plan.device_cidr == f"{plan.device_address}/30"


def test_cpu_serial_reader_fails_loudly_without_stable_identity(tmp_path):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor: 0\nSerial: 0000000000000000\n")

    with pytest.raises(UsbNetworkPlanError, match="no stable CPU serial"):
        read_cpu_serial(cpuinfo)


def test_generated_nm_and_dnsmasq_configs_share_one_plan():
    plan = derive_plan("10000000abcdef01")
    nm = render_nmconnection(plan)
    dnsmasq = render_dnsmasq(plan)

    assert f"address1={plan.device_cidr}" in nm
    assert (
        f"dhcp-range={plan.host_address},{plan.host_address},255.255.255.252,12h"
        in dnsmasq
    )
    assert "never-default=true" in nm
    assert "dhcp-option=3\n" in dnsmasq
    assert "dhcp-option=6\n" in dnsmasq


def test_plan_round_trip_and_apply_render_both_consumers(tmp_path):
    plan_path = tmp_path / "state" / "plan.json"
    nm_path = tmp_path / "etc" / "jts-usb.nmconnection"
    dnsmasq_path = tmp_path / "etc" / "usbnet-dnsmasq.conf"
    pending_path = tmp_path / "state" / "pending"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text("pending\n")
    plan = derive_plan("10000000abcdef01")

    write_plan(plan, plan_path)
    apply_plan(
        load_plan(plan_path),
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
        pending_path=pending_path,
    )

    assert nm_path.read_text() == render_nmconnection(plan)
    assert dnsmasq_path.read_text() == render_dnsmasq(plan)
    assert not pending_path.exists()
    assert nm_path.stat().st_mode & 0o777 == 0o600
    assert dnsmasq_path.stat().st_mode & 0o777 == 0o644


def test_boot_critical_plan_projection_and_marker_writes_are_durable(
    monkeypatch, tmp_path,
):
    real_write = atomic_io.atomic_write_text
    calls: list[tuple[Path, int, bool]] = []

    def record_write(path, text, *, mode=0o644, durable=False, **kwargs):
        calls.append((Path(path), mode, durable))
        return real_write(path, text, mode=mode, durable=durable, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", record_write)
    plan = derive_plan("10000000abcdef01")
    plan_path = tmp_path / "state" / "plan.json"
    nm_path = tmp_path / "etc" / "jts-usb.nmconnection"
    dnsmasq_path = tmp_path / "etc" / "usbnet-dnsmasq.conf"
    pending_path = tmp_path / "state" / "pending"

    write_plan(plan, plan_path)
    apply_plan(
        plan,
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
        pending_path=pending_path,
    )
    promote_plan(
        plan,
        observation=IPv4Observation(
            IPv4ObservationState.OBSERVED, cidr="10.12.194.1/24"
        ),
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
        pending_path=pending_path,
    )

    assert calls == [
        (plan_path, 0o644, True),
        (nm_path, 0o600, True),
        (dnsmasq_path, 0o644, True),
        (pending_path, 0o644, True),
    ]


def test_corrupt_plan_is_rejected_instead_of_fabricating_an_address(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"version": 1, "subnet": "10.64.0.0/30"}\n')

    with pytest.raises(UsbNetworkPlanError):
        load_plan(path)


def test_wrong_type_plan_is_normalized_to_plan_error(tmp_path):
    plan = derive_plan("10000000abcdef01")
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    path.write_text(path.read_text().replace(
        f'"identity_fingerprint": "{plan.identity_fingerprint}"',
        '"identity_fingerprint": null',
    ))

    with pytest.raises(UsbNetworkPlanError, match="fingerprint"):
        load_plan(path)


def test_cloned_plan_is_rejected_on_a_different_pi(tmp_path):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("Serial: 2222222222222222\n")
    cloned_plan = derive_plan("1111111111111111")

    with pytest.raises(UsbNetworkPlanError, match="does not match this Pi"):
        attest_plan(cloned_plan, cpuinfo)


def test_apply_rolls_back_both_consumers_when_second_promotion_fails(
    monkeypatch, tmp_path,
):
    plan = derive_plan("10000000abcdef01")
    nm_path = tmp_path / "nm"
    dnsmasq_path = tmp_path / "dnsmasq"
    nm_path.write_text("old nm\n")
    dnsmasq_path.write_text("old dnsmasq\n")
    real_write = usb_network._write_boot_text

    failed = False

    def fail_second(path, text, mode):
        nonlocal failed
        if Path(path) == dnsmasq_path and not failed:
            failed = True
            raise OSError("injected second projection failure")
        return real_write(path, text, mode)

    monkeypatch.setattr(usb_network, "_write_boot_text", fail_second)

    with pytest.raises(OSError, match="injected"):
        apply_plan(plan, nm_path=nm_path, dnsmasq_path=dnsmasq_path)

    assert nm_path.read_text() == "old nm\n"
    assert dnsmasq_path.read_text() == "old dnsmasq\n"
    assert nm_path.stat().st_mode & 0o777 == 0o644
    assert dnsmasq_path.stat().st_mode & 0o777 == 0o644


def test_legacy_live_address_defers_and_leaves_both_live_files_untouched(tmp_path):
    plan = derive_plan("10000000abcdef01")
    nm_path = tmp_path / "jts-usb.nmconnection"
    dnsmasq_path = tmp_path / "usbnet-dnsmasq.conf"
    pending_path = tmp_path / "pending"
    nm_path.write_text("legacy nm address1=10.12.194.1/24\n")
    dnsmasq_path.write_text("legacy dhcp-range=10.12.194.10,10.12.194.20,12h\n")

    applied = promote_plan(
        plan,
        observation=IPv4Observation(
            IPv4ObservationState.OBSERVED, cidr="10.12.194.1/24"
        ),
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
        pending_path=pending_path,
    )

    assert applied is False
    assert nm_path.read_text() == "legacy nm address1=10.12.194.1/24\n"
    assert dnsmasq_path.read_text() == (
        "legacy dhcp-range=10.12.194.10,10.12.194.20,12h\n"
    )
    assert f"desired={plan.device_cidr}" in pending_path.read_text()
    assert "observed=10.12.194.1/24" in pending_path.read_text()


class _IoctlSocket:
    def fileno(self):
        return 7

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_ipv4_observation_distinguishes_absent_interface(monkeypatch):
    monkeypatch.setattr(usb_network.socket, "if_nameindex", lambda: [(1, "lo")])

    assert observe_ipv4_cidr("usb0") == IPv4Observation(
        IPv4ObservationState.ABSENT
    )


def test_ipv4_observation_distinguishes_existing_interface_without_address(
    monkeypatch,
):
    monkeypatch.setattr(
        usb_network.socket, "if_nameindex", lambda: [(1, "lo"), (7, "usb0")]
    )
    monkeypatch.setattr(usb_network.socket, "socket", lambda *_args: _IoctlSocket())

    def no_address(_fd, _request, _payload):
        raise OSError(errno.EADDRNOTAVAIL, "no IPv4 address")

    monkeypatch.setattr(usb_network.fcntl, "ioctl", no_address)

    assert observe_ipv4_cidr("usb0") == IPv4Observation(
        IPv4ObservationState.NO_ADDRESS
    )


def test_ipv4_observation_preserves_inspection_error(monkeypatch):
    monkeypatch.setattr(
        usb_network.socket, "if_nameindex", lambda: [(1, "lo"), (7, "usb0")]
    )
    monkeypatch.setattr(usb_network.socket, "socket", lambda *_args: _IoctlSocket())

    def denied(_fd, _request, _payload):
        raise OSError(errno.EPERM, "inspection denied")

    monkeypatch.setattr(usb_network.fcntl, "ioctl", denied)

    observation = observe_ipv4_cidr("usb0")
    assert observation.state is IPv4ObservationState.ERROR
    assert "inspection denied" in (observation.error or "")


def test_ipv4_observation_returns_observed_cidr(monkeypatch):
    monkeypatch.setattr(
        usb_network.socket, "if_nameindex", lambda: [(1, "lo"), (7, "usb0")]
    )
    monkeypatch.setattr(usb_network.socket, "socket", lambda *_args: _IoctlSocket())

    def observed(_fd, request, _payload):
        value = "10.64.1.1" if request == 0x8915 else "255.255.255.252"
        return b"\0" * 20 + socket.inet_aton(value)

    monkeypatch.setattr(usb_network.fcntl, "ioctl", observed)

    assert observe_ipv4_cidr("usb0") == IPv4Observation(
        IPv4ObservationState.OBSERVED, cidr="10.64.1.1/30"
    )


def test_inspection_error_refuses_promotion_and_preserves_consumers(tmp_path):
    plan = derive_plan("10000000abcdef01")
    nm_path = tmp_path / "nm"
    dnsmasq_path = tmp_path / "dnsmasq"
    nm_path.write_text("old nm\n")
    dnsmasq_path.write_text("old dnsmasq\n")

    with pytest.raises(UsbNetworkPlanError, match="cannot safely inspect"):
        promote_plan(
            plan,
            observation=IPv4Observation(
                IPv4ObservationState.ERROR, error="inspection denied"
            ),
            nm_path=nm_path,
            dnsmasq_path=dnsmasq_path,
        )

    assert nm_path.read_text() == "old nm\n"
    assert dnsmasq_path.read_text() == "old dnsmasq\n"


@pytest.mark.parametrize(
    "state", [IPv4ObservationState.ABSENT, IPv4ObservationState.NO_ADDRESS]
)
def test_confirmed_safe_interface_states_permit_promotion(state, tmp_path):
    plan = derive_plan("10000000abcdef01")
    nm_path = tmp_path / "nm"
    dnsmasq_path = tmp_path / "dnsmasq"

    applied = promote_plan(
        plan,
        observation=IPv4Observation(state),
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
    )

    assert applied is True
    assert nm_path.read_text() == render_nmconnection(plan)
    assert dnsmasq_path.read_text() == render_dnsmasq(plan)


def _hold_owner_lock(path: str, ready, release):
    with owner_lock(path, timeout=2):
        ready.set()
        release.wait(2)


def test_owner_lock_has_bounded_cross_process_exclusion(tmp_path):
    lock_path = tmp_path / "state" / ".lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_owner_lock, args=(str(lock_path), ready, release)
    )
    process.start()
    try:
        assert ready.wait(2)
        started = time.monotonic()
        with pytest.raises(UsbNetworkPlanError, match="timed out"):
            with owner_lock(lock_path, timeout=0.1):
                pass
        assert time.monotonic() - started < 1
    finally:
        release.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
