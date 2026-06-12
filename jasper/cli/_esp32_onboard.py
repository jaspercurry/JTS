"""Shared ESP32 accessory onboarding flow for JTS USB satellites."""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ._improv import push_credentials

ESP32_S3_VID = 0x303A
ESP32_S3_PIDS = {0x1001, 0x1002, 0x4001}
NMCLI_TIMEOUT_S = 5.0
ESPTOOL_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class UsbSignature:
    vid: int
    pids: frozenset[int]


@dataclass(frozen=True)
class SerialDevice:
    port: str
    vid: int
    pid: int


@dataclass(frozen=True)
class DeviceProfile:
    prog: str
    description: str
    device_label: str
    firmware_bin_path: str
    bin_help: str
    flash_help: str
    mdns_hostname: str
    mdns_help: str
    auto_help: str
    boot_signature: bytes
    boot_log_description: str
    done_message: str
    usb_signature: UsbSignature = UsbSignature(
        vid=ESP32_S3_VID,
        pids=frozenset(ESP32_S3_PIDS),
    )


def build_parser(profile: DeviceProfile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=profile.prog,
        description=profile.description,
    )
    parser.add_argument(
        "--port", default=None,
        help="explicit serial port (default: auto-detect ESP32-S3)",
    )
    parser.add_argument(
        "--bin", default=profile.firmware_bin_path, type=Path,
        help=profile.bin_help,
    )
    parser.add_argument(
        "--no-flash", action="store_true",
        help="skip the flash step even if a bin is present",
    )
    parser.add_argument(
        "--flash",
        action="store_true",
        help=profile.flash_help,
    )
    parser.add_argument(
        "--ssid",
        default=None,
        help="WiFi SSID (default: read from NetworkManager / wpa_supplicant)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="WiFi password (default: read from NetworkManager / wpa_supplicant)",
    )
    parser.add_argument(
        "--mdns-host",
        default=profile.mdns_hostname,
        help=profile.mdns_help,
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=profile.auto_help,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
    )
    return parser


def find_device(
    profile: DeviceProfile,
    explicit_port: str | None = None,
) -> SerialDevice:
    """Locate the profile's USB CDC serial port. Raises if not found."""
    if explicit_port:
        if not Path(explicit_port).exists():
            raise RuntimeError(f"explicit port not found: {explicit_port}")
        return SerialDevice(port=explicit_port, vid=0, pid=0)

    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise RuntimeError("pyserial is required (pip install pyserial)") from e

    matches = [
        p
        for p in list_ports.comports()
        if p.vid == profile.usb_signature.vid and p.pid in profile.usb_signature.pids
    ]
    if not matches:
        label = profile.device_label
        raise RuntimeError(
            f"No ESP32-S3 {label} found on USB. Plug it into the Pi and try again. "
            "If it's connected, double-check `lsusb | grep 303a` shows it."
        )
    if len(matches) > 1:
        ports = ", ".join(p.device for p in matches)
        raise RuntimeError(
            f"Multiple ESP32-S3 devices found ({ports}); pass --port to disambiguate"
        )
    p = matches[0]
    return SerialDevice(port=p.device, vid=p.vid, pid=p.pid)


def read_pi_wifi() -> tuple[str, str]:
    """Read the Pi's current WiFi SSID + PSK."""
    creds = _read_wifi_nm()
    if creds is not None:
        logging.getLogger(__name__).info(
            "read WiFi creds from NetworkManager (SSID=%s)",
            creds[0],
        )
        return creds
    creds = _read_wifi_wpa_supplicant()
    if creds is not None:
        logging.getLogger(__name__).info(
            "read WiFi creds from wpa_supplicant.conf (SSID=%s)",
            creds[0],
        )
        return creds
    raise RuntimeError(
        "Could not read Pi WiFi credentials from NetworkManager or "
        "wpa_supplicant. Are you running this as root? Pass --ssid and "
        "--password explicitly to skip auto-detection."
    )


def _read_wifi_nm() -> tuple[str, str] | None:
    """Read SSID and PSK from NetworkManager."""
    if not shutil.which("nmcli"):
        return None
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show", "--active"],
            check=True,
            capture_output=True,
            text=True,
            timeout=NMCLI_TIMEOUT_S,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Timed out after 5 s reading active WiFi connection via nmcli. "
            "NetworkManager or D-Bus may be wedged; pass --ssid and --password "
            "explicitly, or retry after restarting NetworkManager."
        ) from e
    name = None
    for line in out.splitlines():
        fields = _split_nmcli_terse(line, maxsplit=1)
        if len(fields) == 2 and fields[0] in {"802-11-wireless", "wifi"}:
            name = fields[1]
            break
    if not name:
        return None

    try:
        out = subprocess.run(
            [
                "nmcli",
                "-s",
                "-t",
                "-f",
                "802-11-wireless.ssid,802-11-wireless-security.psk",
                "connection",
                "show",
                name,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=NMCLI_TIMEOUT_S,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Timed out after 5 s reading WiFi secrets via nmcli. "
            "NetworkManager or D-Bus may be wedged; pass --ssid and --password "
            "explicitly, or retry after restarting NetworkManager."
        ) from e
    fields = dict(
        parts
        for line in out.splitlines()
        if len(parts := _split_nmcli_terse(line, maxsplit=1)) == 2
    )
    ssid = fields.get("802-11-wireless.ssid", "")
    psk = fields.get("802-11-wireless-security.psk", "")
    if ssid and psk:
        return ssid, psk
    return None


def _split_nmcli_terse(line: str, *, maxsplit: int = -1) -> list[str]:
    """Split nmcli terse output on unescaped colons and unescape fields."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    splits = 0
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":" and (maxsplit < 0 or splits < maxsplit):
            fields.append("".join(current))
            current = []
            splits += 1
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def _read_wifi_wpa_supplicant() -> tuple[str, str] | None:
    """Parse wpa_supplicant.conf for the first network's SSID and PSK."""
    paths = [
        "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf",
        "/etc/wpa_supplicant/wpa_supplicant.conf",
    ]
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        match = re.search(
            r"network\s*=\s*\{[^}]*?ssid\s*=\s*\"([^\"]+)\"[^}]*?psk\s*=\s*\"([^\"]+)\"",
            text,
            re.DOTALL,
        )
        if match:
            return match.group(1), match.group(2)
    return None


def probe_firmware(
    profile: DeviceProfile,
    port: str,
    *,
    timeout_s: float = 5.0,
    log: logging.Logger | None = None,
) -> bool:
    """Identify whether ``port`` is running the profile's JTS firmware."""
    import serial  # pyserial; deferred so import is optional for tests

    log = log or logging.getLogger(__name__)
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:  # noqa: BLE001
        log.debug("%s probe: open %s failed: %s", profile.device_label, port, e)
        return False

    try:
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = ser.read(256)
            except (OSError, IOError) as e:
                log.debug("%s probe: read failed: %s", profile.device_label, e)
                return False
            if chunk:
                buf.extend(chunk)
                if profile.boot_signature in buf:
                    return True
            else:
                time.sleep(0.05)
        return False
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass


def flash_firmware(
    profile: DeviceProfile,
    port: str,
    bin_path: Path,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Flash a firmware bin via esptool."""
    log = log or logging.getLogger(__name__)
    if not bin_path.exists():
        log.info("skipping flash - no firmware bin at %s", bin_path)
        return

    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--baud",
        "460800",
        "write-flash",
        "-z",
        "0x0",
        str(bin_path),
    ]
    log.info("flashing %s -> %s", bin_path, port)
    subprocess.run(cmd, check=True, timeout=ESPTOOL_TIMEOUT_S)
    time.sleep(2.0)


def wait_for_online(hostname: str, timeout: float = 30.0) -> str | None:
    """Resolve ``hostname`` until it answers, or return None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return socket.gethostbyname(hostname)
        except socket.gaierror:
            time.sleep(1.0)
    return None


def run_onboard(profile: DeviceProfile, argv: list[str] | None = None) -> int:
    parser = build_parser(profile)
    args = parser.parse_args(argv)
    log = logging.getLogger(profile.prog)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.auto:
        ip = wait_for_online(args.mdns_host, timeout=3.0)
        if ip is not None:
            log.info(
                "%s already online at %s (%s) - auto mode short-circuit, "
                "no flash or cred push needed",
                profile.device_label,
                args.mdns_host,
                ip,
            )
            return 0
        log.info(
            "%s not yet online at %s - proceeding with onboard flow",
            profile.device_label,
            args.mdns_host,
        )

    try:
        device = find_device(profile, args.port)
    except RuntimeError as e:
        log.error(str(e))
        return 1
    log.info(
        "found %s on %s (vid=0x%04x pid=0x%04x)",
        profile.device_label,
        device.port,
        device.vid,
        device.pid,
    )

    is_jts_device = probe_firmware(profile, device.port, log=log)
    if is_jts_device:
        log.info(
            "boot-log probe succeeded - device is running JTS %s firmware, "
            "no flash needed",
            profile.device_label,
        )
    else:
        log.info(
            "boot-log probe didn't see JTS firmware signature - "
            "device either needs flashing, or it's not a JTS %s",
            profile.device_label,
        )

    should_flash = False
    if is_jts_device:
        should_flash = bool(args.flash)
    else:
        if args.no_flash:
            should_flash = False
        elif args.auto:
            log.error(
                "auto mode refuses to push WiFi credentials to %s because "
                "the boot-log probe did not positively identify JTS %s "
                "firmware. Confirm the hardware and rerun manually before "
                "provisioning.",
                device.port,
                profile.device_label,
            )
            return 4
        else:
            should_flash = True

    if should_flash:
        try:
            flash_firmware(profile, device.port, args.bin, log=log)
            if args.port is None:
                device = find_device(profile, None)
        except subprocess.CalledProcessError as e:
            log.error("esptool failed: %s", e)
            return 2
        except subprocess.TimeoutExpired:
            log.error(
                "esptool timed out after %.0f s while flashing %s. "
                "Check the USB connection and retry.",
                ESPTOOL_TIMEOUT_S,
                args.bin,
            )
            return 2

    if args.ssid is None or args.password is None:
        try:
            ssid, password = read_pi_wifi()
        except RuntimeError as e:
            log.error(str(e))
            return 3
    else:
        ssid, password = args.ssid, args.password

    try:
        push_credentials(
            device.port,
            ssid,
            password,
            device_label=profile.device_label,
            log=log,
        )
    except (RuntimeError, TimeoutError) as e:
        log.error("Improv push failed: %s", e)
        return 4

    log.info("%s reports PROVISIONED - checking mDNS %s", profile.device_label, args.mdns_host)
    ip = wait_for_online(args.mdns_host, timeout=20.0)
    if ip is None:
        speaker_host = os.environ.get("JASPER_HOSTNAME", "jts.local")
        log.warning(
            "%s did not appear at %s within 20 s. WiFi probably worked "
            "(Improv waits for it before reporting PROVISIONED), but mDNS "
            "may be filtered on your network. The %s should still reach "
            "%s for HTTP control.",
            profile.device_label,
            args.mdns_host,
            profile.device_label,
            speaker_host,
        )
    else:
        log.info("%s online at %s (%s)", profile.device_label, args.mdns_host, ip)

    log.info(profile.done_message)
    return 0
