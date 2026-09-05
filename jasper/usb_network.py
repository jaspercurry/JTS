# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single owner for the USB management network address plan.

Each speaker receives one stable IPv4 /30 derived from its immutable Pi CPU
serial.  The installer persists the plan, and this module renders the two
consumer configurations from that artifact.  Runtime readers never derive a
second copy of the address policy.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


PLAN_VERSION = 1
DERIVATION = "cpu-serial-sha256-v1"
ALLOCATION_SUPERNET = ipaddress.IPv4Network("10.64.0.0/10")
LEGACY_MANAGEMENT_CIDR = "10.12.194.1/24"
DEFAULT_CPUINFO_PATH = Path("/proc/cpuinfo")
DEFAULT_STATE_DIR = Path("/var/lib/jasper-usb-network")
DEFAULT_PLAN_PATH = DEFAULT_STATE_DIR / "plan.json"
DEFAULT_PENDING_PATH = DEFAULT_STATE_DIR / "migration_pending"
DEFAULT_LOCK_PATH = DEFAULT_STATE_DIR / ".lock"
DEFAULT_NM_PATH = Path(
    "/etc/NetworkManager/system-connections/jts-usb.nmconnection"
)
DEFAULT_DNSMASQ_PATH = Path("/etc/jasper/usbnet-dnsmasq.conf")
_SERIAL_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")


class UsbNetworkPlanError(ValueError):
    """The stable hardware identity or persisted plan is unusable."""


class IPv4ObservationState(str, Enum):
    """What was learned while inspecting an interface's primary IPv4."""

    ABSENT = "absent"
    NO_ADDRESS = "no_address"
    OBSERVED = "observed"
    ERROR = "error"


@dataclass(frozen=True)
class IPv4Observation:
    state: IPv4ObservationState
    cidr: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state is IPv4ObservationState.OBSERVED:
            if self.cidr is None or self.error is not None:
                raise ValueError("observed IPv4 state requires only a CIDR")
        elif self.state is IPv4ObservationState.ERROR:
            if self.error is None or self.cidr is not None:
                raise ValueError("IPv4 error state requires only an error")
        elif self.cidr is not None or self.error is not None:
            raise ValueError("absent/no-address IPv4 state has no detail")


def _write_boot_text(path: str | os.PathLike[str], text: str, mode: int) -> None:
    """Publish boot-critical state through JTS's canonical durable writer."""

    # Lazy so the laptop-only ``advisory-cidrs`` command remains executable by
    # file path without importing the package's Pi-side write dependencies.
    from jasper.atomic_io import atomic_write_text

    atomic_write_text(path, text, mode=mode, durable=True)


@contextmanager
def owner_lock(
    path: str | os.PathLike[str] = DEFAULT_LOCK_PATH,
    *,
    timeout: float = 5.0,
):
    """Serialize bounded plan/projection mutations across install and boot.

    Root-only state, so the lock keeps 0600 and root's own group.
    """

    # Lazy for the same reason as ``_write_boot_text``.
    from jasper.atomic_io import advisory_file_lock

    with ExitStack() as stack:
        try:
            stack.enter_context(
                advisory_file_lock(
                    path, mode=0o600, group_from_parent=False, timeout_sec=timeout
                )
            )
        except TimeoutError as exc:
            raise UsbNetworkPlanError(
                f"timed out acquiring USB network owner lock {path}"
            ) from exc
        yield


@dataclass(frozen=True)
class UsbNetworkPlan:
    version: int
    derivation: str
    identity_fingerprint: str
    allocation_supernet: str
    subnet: str
    device_address: str
    host_address: str
    broadcast_address: str

    @property
    def device_cidr(self) -> str:
        prefix = ipaddress.IPv4Network(self.subnet).prefixlen
        return f"{self.device_address}/{prefix}"

    def validate(self) -> "UsbNetworkPlan":
        if self.version != PLAN_VERSION or self.derivation != DERIVATION:
            raise UsbNetworkPlanError("unsupported USB network plan version")
        if self.allocation_supernet != str(ALLOCATION_SUPERNET):
            raise UsbNetworkPlanError("USB network allocation supernet drift")
        if not isinstance(self.identity_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{12}", self.identity_fingerprint
        ):
            raise UsbNetworkPlanError("invalid USB network identity fingerprint")
        try:
            subnet = ipaddress.IPv4Network(self.subnet, strict=True)
            device = ipaddress.IPv4Address(self.device_address)
            host = ipaddress.IPv4Address(self.host_address)
            broadcast = ipaddress.IPv4Address(self.broadcast_address)
        except (TypeError, ValueError) as exc:
            raise UsbNetworkPlanError(f"invalid USB network address: {exc}") from exc
        if subnet.prefixlen != 30 or not subnet.subnet_of(ALLOCATION_SUPERNET):
            raise UsbNetworkPlanError("USB network subnet must be a /30 in allocation range")
        expected_hosts = tuple(subnet.hosts())
        if (device, host) != expected_hosts or broadcast != subnet.broadcast_address:
            raise UsbNetworkPlanError("invalid USB network /30 geometry")
        return self


def read_cpu_serial(path: str | os.PathLike[str] = DEFAULT_CPUINFO_PATH) -> str:
    """Read a stable Pi CPU serial, failing instead of inventing an identity."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsbNetworkPlanError(f"cannot read CPU serial from {path}: {exc}") from exc
    for raw in text.splitlines():
        key, separator, value = raw.partition(":")
        if separator and key.strip().lower() == "serial":
            serial = value.strip().lower()
            if _SERIAL_RE.fullmatch(serial) and int(serial, 16) != 0:
                return serial
            break
    raise UsbNetworkPlanError(f"no stable CPU serial in {path}")


def derive_plan(serial: str) -> UsbNetworkPlan:
    """Derive a deterministic collision-resistant /30 from ``serial``."""

    normalized = serial.strip().lower()
    if not _SERIAL_RE.fullmatch(normalized) or int(normalized, 16) == 0:
        raise UsbNetworkPlanError("CPU serial must be 8-64 non-zero hex characters")
    digest = hashlib.sha256(f"{DERIVATION}\0{normalized}".encode()).digest()
    subnet_count = ALLOCATION_SUPERNET.num_addresses // 4
    subnet_index = int.from_bytes(digest[:4], "big") % subnet_count
    network_address = int(ALLOCATION_SUPERNET.network_address) + subnet_index * 4
    subnet = ipaddress.IPv4Network((network_address, 30))
    device, host = tuple(subnet.hosts())
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return UsbNetworkPlan(
        version=PLAN_VERSION,
        derivation=DERIVATION,
        identity_fingerprint=fingerprint,
        allocation_supernet=str(ALLOCATION_SUPERNET),
        subnet=str(subnet),
        device_address=str(device),
        host_address=str(host),
        broadcast_address=str(subnet.broadcast_address),
    ).validate()


def load_plan(path: str | os.PathLike[str] = DEFAULT_PLAN_PATH) -> UsbNetworkPlan:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        plan = UsbNetworkPlan(**raw)
        return plan.validate()
    except UsbNetworkPlanError:
        raise
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise UsbNetworkPlanError(f"cannot load USB network plan from {path}: {exc}") from exc


def attest_plan(
    plan: UsbNetworkPlan,
    cpuinfo_path: str | os.PathLike[str] = DEFAULT_CPUINFO_PATH,
) -> UsbNetworkPlan:
    """Require the persisted plan to belong to this physical Pi."""

    expected = derive_plan(read_cpu_serial(cpuinfo_path))
    if plan.validate() != expected:
        raise UsbNetworkPlanError(
            "persisted USB network plan does not match this Pi CPU serial"
        )
    return plan


def write_plan(
    plan: UsbNetworkPlan,
    path: str | os.PathLike[str] = DEFAULT_PLAN_PATH,
) -> None:
    plan.validate()
    _write_boot_text(
        path,
        json.dumps(asdict(plan), indent=2, sort_keys=True) + "\n",
        0o644,
    )


def render_nmconnection(plan: UsbNetworkPlan) -> str:
    plan.validate()
    return f"""# Generated by jasper.usb_network from {DEFAULT_PLAN_PATH}.
# Do not edit: re-run the JTS installer to converge this projection.
[connection]
id=jts-usb
uuid=d996616d-cd6c-4e38-b34b-8d41f0a19d88
type=ethernet
interface-name=usb0
autoconnect=true
autoconnect-priority=-100
autoconnect-retries=0

[ethernet]

[ipv4]
method=manual
address1={plan.device_cidr}
never-default=true

[ipv6]
method=link-local

[proxy]
"""


def render_dnsmasq(plan: UsbNetworkPlan) -> str:
    plan.validate()
    netmask = ipaddress.IPv4Network(plan.subnet).netmask
    return f"""# Generated by jasper.usb_network from {DEFAULT_PLAN_PATH}.
# DHCP only on usb0: no DNS, default route, forwarding, or internet sharing.
user=nobody
group=nogroup
interface=usb0
except-interface=lo
bind-dynamic
port=0
dhcp-range={plan.host_address},{plan.host_address},{netmask},12h
dhcp-option=3
dhcp-option=6
dhcp-leasefile=/run/jasper-usbnet/dnsmasq.leases
log-facility=-
"""


def apply_plan(
    plan: UsbNetworkPlan,
    *,
    nm_path: str | os.PathLike[str] = DEFAULT_NM_PATH,
    dnsmasq_path: str | os.PathLike[str] = DEFAULT_DNSMASQ_PATH,
    pending_path: str | os.PathLike[str] = DEFAULT_PENDING_PATH,
) -> None:
    """Render both live consumers from one already-validated plan."""

    plan.validate()
    nm_target = Path(nm_path)
    dnsmasq_target = Path(dnsmasq_path)
    originals: dict[Path, tuple[bytes, int] | None] = {}
    for target in (nm_target, dnsmasq_target):
        try:
            originals[target] = (target.read_bytes(), target.stat().st_mode & 0o777)
        except FileNotFoundError:
            originals[target] = None
    try:
        _write_boot_text(nm_target, render_nmconnection(plan), 0o600)
        _write_boot_text(dnsmasq_target, render_dnsmasq(plan), 0o644)
    except OSError:
        for target, original in originals.items():
            if original is None:
                target.unlink(missing_ok=True)
            else:
                content, mode = original
                _write_boot_text(target, content.decode("utf-8"), mode)
        raise
    try:
        Path(pending_path).unlink()
    except FileNotFoundError:
        pass


def promote_plan(
    plan: UsbNetworkPlan,
    *,
    observation: IPv4Observation,
    nm_path: str | os.PathLike[str] = DEFAULT_NM_PATH,
    dnsmasq_path: str | os.PathLike[str] = DEFAULT_DNSMASQ_PATH,
    pending_path: str | os.PathLike[str] = DEFAULT_PENDING_PATH,
) -> bool:
    """Apply ``plan`` unless replacing a currently-live USB address.

    Returning ``False`` is a successful, explicit deferral: both installed
    projections remain untouched so a same-install gadget recreation returns
    on the address carrying the current SSH session.  At next boot usb0 is
    absent and the boot gate promotes the plan before NetworkManager starts.
    """

    plan.validate()
    if not isinstance(observation, IPv4Observation):
        raise UsbNetworkPlanError("invalid USB network IPv4 observation")
    if observation.state is IPv4ObservationState.ERROR:
        raise UsbNetworkPlanError(
            f"cannot safely inspect USB network interface: {observation.error}"
        )
    observed_cidr = observation.cidr
    if (
        observation.state is IPv4ObservationState.OBSERVED
        and observed_cidr != plan.device_cidr
    ):
        _write_boot_text(
            pending_path,
            f"desired={plan.device_cidr}\nobserved={observed_cidr}\n",
            0o644,
        )
        return False
    apply_plan(
        plan,
        nm_path=nm_path,
        dnsmasq_path=dnsmasq_path,
        pending_path=pending_path,
    )
    return True


def observe_ipv4_cidr(interface: str = "usb0") -> IPv4Observation:
    """Inspect an interface without confusing absence with read failure."""

    try:
        ifname = interface.encode("ascii", "strict")[:15]
    except UnicodeError as exc:
        return IPv4Observation(IPv4ObservationState.ERROR, error=str(exc))
    request = struct.pack("256s", ifname)
    try:
        interface_names = {name for _index, name in socket.if_nameindex()}
    except OSError as exc:
        return IPv4Observation(IPv4ObservationState.ERROR, error=str(exc))
    if interface not in interface_names:
        return IPv4Observation(IPv4ObservationState.ABSENT)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                address_raw = fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24]
            except OSError as exc:
                if exc.errno == errno.EADDRNOTAVAIL:
                    return IPv4Observation(IPv4ObservationState.NO_ADDRESS)
                if exc.errno in (errno.ENODEV, errno.ENXIO):
                    return IPv4Observation(IPv4ObservationState.ABSENT)
                return IPv4Observation(IPv4ObservationState.ERROR, error=str(exc))
            try:
                netmask_raw = fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24]
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.ENXIO):
                    return IPv4Observation(IPv4ObservationState.ABSENT)
                return IPv4Observation(IPv4ObservationState.ERROR, error=str(exc))
    except OSError as exc:
        return IPv4Observation(IPv4ObservationState.ERROR, error=str(exc))
    try:
        address = ipaddress.IPv4Address(address_raw)
        netmask = ipaddress.IPv4Address(netmask_raw)
        prefix = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
        return IPv4Observation(
            IPv4ObservationState.ERROR, error=f"invalid IPv4 ioctl result: {exc}"
        )
    return IPv4Observation(
        IPv4ObservationState.OBSERVED, cidr=f"{address}/{prefix}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote")
    promote.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH)
    promote.add_argument("--nm", type=Path, default=DEFAULT_NM_PATH)
    promote.add_argument("--dnsmasq", type=Path, default=DEFAULT_DNSMASQ_PATH)
    promote.add_argument("--pending", type=Path, default=DEFAULT_PENDING_PATH)
    promote.add_argument("--interface", default="usb0")
    promote.add_argument("--cpuinfo", type=Path, default=DEFAULT_CPUINFO_PATH)
    promote.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    converge = sub.add_parser("converge")
    converge.add_argument("--cpuinfo", type=Path, default=DEFAULT_CPUINFO_PATH)
    converge.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH)
    converge.add_argument("--nm", type=Path, default=DEFAULT_NM_PATH)
    converge.add_argument("--dnsmasq", type=Path, default=DEFAULT_DNSMASQ_PATH)
    converge.add_argument("--pending", type=Path, default=DEFAULT_PENDING_PATH)
    converge.add_argument("--interface", default="usb0")
    converge.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    sub.add_parser("advisory-cidrs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "advisory-cidrs":
            print(ALLOCATION_SUPERNET)
            print(LEGACY_MANAGEMENT_CIDR)
            return 0
        with owner_lock(args.lock):
            if args.command == "converge":
                plan = derive_plan(read_cpu_serial(args.cpuinfo))
                write_plan(plan, args.plan)
                print(
                    "event=usb_network.plan_staged "
                    f"version={plan.version} fingerprint={plan.identity_fingerprint} "
                    f"device={plan.device_cidr}"
                )
            else:
                plan = attest_plan(load_plan(args.plan), args.cpuinfo)
            observation = observe_ipv4_cidr(args.interface)
            if not promote_plan(
                plan,
                observation=observation,
                nm_path=args.nm,
                dnsmasq_path=args.dnsmasq,
                pending_path=args.pending,
            ):
                print(
                    "event=usb_network.plan_deferred "
                    f"desired={plan.device_cidr} observed={observation.cidr} "
                    "activation=next_boot"
                )
                return 0
            print(
                "event=usb_network.plan_applied "
                f"version={plan.version} fingerprint={plan.identity_fingerprint} "
                f"subnet={plan.subnet} device={plan.device_address}"
            )
    except (OSError, UsbNetworkPlanError) as exc:
        print(f"event=usb_network.plan_failed error={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOCATION_SUPERNET",
    "DEFAULT_PENDING_PATH",
    "DEFAULT_PLAN_PATH",
    "DEFAULT_STATE_DIR",
    "DERIVATION",
    "IPv4Observation",
    "IPv4ObservationState",
    "LEGACY_MANAGEMENT_CIDR",
    "PLAN_VERSION",
    "UsbNetworkPlan",
    "UsbNetworkPlanError",
    "apply_plan",
    "attest_plan",
    "derive_plan",
    "load_plan",
    "observe_ipv4_cidr",
    "owner_lock",
    "promote_plan",
    "read_cpu_serial",
    "render_dnsmasq",
    "render_nmconnection",
    "write_plan",
]
