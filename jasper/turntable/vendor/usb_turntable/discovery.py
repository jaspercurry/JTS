"""USB serial discovery for macOS and Linux."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .errors import PortDiscoveryError


MAC_PATTERNS = (
    "/dev/cu.wchusbserial*",
    "/dev/cu.usbserial*",
    "/dev/cu.usbmodem*",
    "/dev/cu.CH34*",
    "/dev/cu.SLAB_USBtoUART*",
)
LINUX_PATTERNS = (
    "/dev/serial/by-id/*",
    "/dev/serial/by-path/*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
)


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    stable_path: str
    vid: str | None
    pid: str | None
    serial: str | None
    manufacturer: str | None
    product: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _is_candidate(path: str, platform: str) -> bool:
    name = os.path.basename(path).lower()
    if any(token in name for token in ("bluetooth", "incoming-port")):
        return False
    if platform.startswith("linux"):
        return path.startswith("/dev/serial/by-") or bool(re.fullmatch(r"tty(?:USB|ACM)\d+", os.path.basename(path)))
    return path.startswith("/dev/cu.") and any(
        token in name for token in ("wchusbserial", "usbserial", "usbmodem", "ch34", "slab_usbtouart")
    )


def _stable_priority(path: str) -> tuple[int, str]:
    if "/by-id/" in path:
        return (0, path)
    if "/by-path/" in path:
        return (1, path)
    return (2, path)


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None
    return value or None


def _linux_usb_metadata(real_path: str, sysfs_root: Path) -> dict[str, str | None]:
    tty = os.path.basename(real_path)
    device_link = sysfs_root / "class" / "tty" / tty / "device"
    try:
        current = device_link.resolve(strict=True)
    except OSError:
        return {"vid": None, "pid": None, "serial": None, "manufacturer": None, "product": None}
    for candidate in (current, *current.parents):
        vid = _read_text(candidate / "idVendor")
        pid = _read_text(candidate / "idProduct")
        if vid and pid:
            return {
                "vid": vid.lower(),
                "pid": pid.lower(),
                "serial": _read_text(candidate / "serial"),
                "manufacturer": _read_text(candidate / "manufacturer"),
                "product": _read_text(candidate / "product"),
            }
        if candidate == sysfs_root:
            break
    return {"vid": None, "pid": None, "serial": None, "manufacturer": None, "product": None}


def discover_devices(
    paths: Iterable[str] | None = None,
    *,
    platform: str | None = None,
    sysfs_root: str | Path = "/sys",
) -> list[dict[str, str | None]]:
    """Return de-duplicated, JSON-serializable USB serial device records."""
    platform = platform or sys.platform
    if paths is None:
        patterns = LINUX_PATTERNS if platform.startswith("linux") else MAC_PATTERNS
        paths = (item for pattern in patterns for item in glob.glob(pattern))
    candidates = sorted({str(path) for path in paths if _is_candidate(str(path), platform)}, key=_stable_priority)
    selected: dict[str, str] = {}
    for path in candidates:
        real = os.path.realpath(path)
        selected.setdefault(real, path)
    records = []
    for real, stable in sorted(selected.items(), key=lambda item: _stable_priority(item[1])):
        metadata = (
            _linux_usb_metadata(real, Path(sysfs_root))
            if platform.startswith("linux")
            else {"vid": None, "pid": None, "serial": None, "manufacturer": None, "product": None}
        )
        records.append(DeviceInfo(path=real, stable_path=stable, **metadata).to_dict())
    return records


def select_port(requested: str | None = None) -> str:
    if requested and requested != "auto":
        if not os.path.exists(requested):
            raise PortDiscoveryError(f"Serial port does not exist: {requested}")
        return requested
    devices = discover_devices()
    if not devices:
        raise PortDiscoveryError("No likely ComXim USB serial device was found")
    if len(devices) != 1:
        choices = ", ".join(str(device["stable_path"]) for device in devices)
        raise PortDiscoveryError(f"More than one USB serial device was found; choose --port ({choices})")
    return str(devices[0]["stable_path"])


def discover_serial_ports(paths: Iterable[str] | None = None) -> list[str]:
    """Compatibility helper returning only selected paths."""
    return [str(device["stable_path"]) for device in discover_devices(paths)]
