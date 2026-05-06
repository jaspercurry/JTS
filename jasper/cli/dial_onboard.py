"""jasper-dial-onboard — Pi-mediated provisioning for the JTS rotary dial.

Plug a fresh dial into a Pi USB-C port and run this command. The script:

  1. Finds the dial on USB CDC (/dev/ttyACM* matching ESP32-S3 VID:PID)
  2. (Optional) Flashes phase-1 firmware via esptool
  3. Reads the Pi's current WiFi credentials (NetworkManager or
     wpa_supplicant)
  4. Pushes them to the dial via Improv-over-Serial — the same protocol
     improv-wifi.com uses, but driven from the Pi instead of a browser
  5. Watches for the dial to come up on the network (ping mDNS hostname)

Improv-over-Serial protocol reference:
  https://www.improv-wifi.com/serial/

The dial keeps WiFi creds in NVS flash, so subsequent boots reconnect
without intervention. Re-run this script after a network change to push
new creds — the dial accepts a SUBMIT_SETTINGS message at any time.
"""
from __future__ import annotations

import argparse
import glob
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

logger = logging.getLogger("jasper-dial-onboard")


# ESP32-S3 native USB CDC enumerates with Espressif's VID and a
# generic S3 PID. CrowPanel's HMI variant uses the same.
ESP32_S3_VID = 0x303A
ESP32_S3_PIDS = {0x1001, 0x1002, 0x4001}  # JTAG/serial PIDs Espressif ships with S3


# Improv-over-Serial framing. See https://www.improv-wifi.com/serial/.
IMPROV_HEADER = b"IMPROV\x01"  # magic + protocol version

IMPROV_PKT_CURRENT_STATE = 0x01
IMPROV_PKT_ERROR_STATE = 0x02
IMPROV_PKT_RPC = 0x03
IMPROV_PKT_RPC_RESPONSE = 0x04

# State enum values per the Improv-WiFi spec. Critical: these are
# the values inside CURRENT_STATE packet bodies, NOT the wire packet
# type IDs above. Source of truth: ImprovTypes.h in the library
# (jnthas/Improv-WiFi-Library).
IMPROV_STATE_STOPPED = 0x00
IMPROV_STATE_AWAITING_AUTH = 0x01
IMPROV_STATE_AUTHORIZED = 0x02
IMPROV_STATE_PROVISIONING = 0x03
IMPROV_STATE_PROVISIONED = 0x04

IMPROV_ERROR_NONE = 0x00

IMPROV_CMD_SUBMIT_SETTINGS = 0x01


# ---------- USB device discovery ----------


@dataclass
class Dial:
    port: str  # e.g. /dev/ttyACM0
    vid: int
    pid: int


def find_dial(explicit_port: str | None = None) -> Dial:
    """Locate the dial's USB CDC serial port. Raises if not found."""
    if explicit_port:
        if not Path(explicit_port).exists():
            raise RuntimeError(f"explicit port not found: {explicit_port}")
        return Dial(port=explicit_port, vid=0, pid=0)

    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise RuntimeError(
            "pyserial is required (pip install pyserial)"
        ) from e

    matches = [
        p for p in list_ports.comports()
        if p.vid == ESP32_S3_VID and p.pid in ESP32_S3_PIDS
    ]
    if not matches:
        raise RuntimeError(
            "No ESP32-S3 dial found on USB. Plug it into the Pi and try again. "
            "If it's connected, double-check `lsusb | grep 303a` shows it."
        )
    if len(matches) > 1:
        ports = ", ".join(p.device for p in matches)
        raise RuntimeError(
            f"Multiple ESP32-S3 devices found ({ports}); pass --port to disambiguate"
        )
    p = matches[0]
    return Dial(port=p.device, vid=p.vid, pid=p.pid)


# ---------- Pi WiFi credentials ----------


def read_pi_wifi() -> tuple[str, str]:
    """Read the Pi's current WiFi SSID + PSK. Returns (ssid, password).

    Tries NetworkManager first (moOde 9 / Bookworm default), falls back
    to wpa_supplicant.conf. Raises if neither yields a usable secret.
    """
    creds = _read_wifi_nm()
    if creds is not None:
        logger.info("read WiFi creds from NetworkManager (SSID=%s)", creds[0])
        return creds
    creds = _read_wifi_wpa_supplicant()
    if creds is not None:
        logger.info("read WiFi creds from wpa_supplicant.conf (SSID=%s)", creds[0])
        return creds
    raise RuntimeError(
        "Could not read Pi WiFi credentials from NetworkManager or "
        "wpa_supplicant. Are you running this as root? Pass --ssid and "
        "--password explicitly to skip auto-detection."
    )


def _read_wifi_nm() -> tuple[str, str] | None:
    """NetworkManager path. Files at /etc/NetworkManager/system-connections/
    are root-readable plaintext. Find the active WiFi connection and pull
    psk from it."""
    if not shutil.which("nmcli"):
        return None
    # Active connection name for the wifi device.
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show", "--active"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    name = None
    for line in out.splitlines():
        # NM separator is colon; name itself can contain colons (escaped \:).
        if line.startswith("802-11-wireless:") or line.startswith("wifi:"):
            name = line.split(":", 1)[1]
            break
    if not name:
        return None
    # `nmcli --show-secrets` returns ssid + psk for the connection.
    try:
        out = subprocess.run(
            ["nmcli", "-s", "-t", "-f",
             "802-11-wireless.ssid,802-11-wireless-security.psk",
             "connection", "show", name],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    fields = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    ssid = fields.get("802-11-wireless.ssid", "").strip()
    psk = fields.get("802-11-wireless-security.psk", "").strip()
    if ssid and psk:
        return ssid, psk
    return None


def _read_wifi_wpa_supplicant() -> tuple[str, str] | None:
    """Parse wpa_supplicant.conf for the first network's ssid + psk. Older
    Raspberry Pi OS (Bullseye and earlier) uses this; newer versions
    delegate to NetworkManager."""
    paths = [
        "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf",
        "/etc/wpa_supplicant/wpa_supplicant.conf",
    ]
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            text = Path(path).read_text()
        except (OSError, PermissionError):
            continue
        # Greedy regex over a `network={ ... ssid=... psk=... }` block.
        # Quoted values are unquoted; bare hex psk is rare on home routers.
        m = re.search(
            r"network\s*=\s*\{[^}]*?ssid\s*=\s*\"([^\"]+)\"[^}]*?psk\s*=\s*\"([^\"]+)\"",
            text, re.DOTALL,
        )
        if m:
            return m.group(1), m.group(2)
    return None


# ---------- Improv-over-Serial ----------


def _improv_packet(pkt_type: int, data: bytes) -> bytes:
    """Frame a packet per the Improv-over-Serial spec.
    Wire format: IMPROV<v> <type:1> <len:1> <data:len> <checksum:1>
    Checksum = sum of all preceding bytes mod 256."""
    if len(data) > 255:
        raise ValueError("Improv data payload too large")
    body = IMPROV_HEADER + bytes([pkt_type, len(data)]) + data
    checksum = sum(body) & 0xFF
    return body + bytes([checksum])


def _build_submit_settings(ssid: str, password: str) -> bytes:
    ssid_b = ssid.encode("utf-8")
    pass_b = password.encode("utf-8")
    if len(ssid_b) > 255 or len(pass_b) > 255:
        raise ValueError("ssid/password too long for Improv (>255 bytes)")
    rpc_data = (
        bytes([len(ssid_b)]) + ssid_b
        + bytes([len(pass_b)]) + pass_b
    )
    # RPC: command(1) + data_len(1) + data
    pkt_data = bytes([IMPROV_CMD_SUBMIT_SETTINGS, len(rpc_data)]) + rpc_data
    return _improv_packet(IMPROV_PKT_RPC, pkt_data)


def _scan_packets(buf: bytearray) -> list[tuple[int, bytes]]:
    """Pull complete Improv packets out of `buf`. Mutates `buf` to remove
    consumed bytes. Returns list of (pkt_type, data) tuples."""
    out: list[tuple[int, bytes]] = []
    while True:
        idx = buf.find(IMPROV_HEADER)
        if idx < 0:
            # No header yet; preserve last 6 bytes (might be partial).
            if len(buf) > len(IMPROV_HEADER):
                del buf[: -len(IMPROV_HEADER)]
            return out
        if idx > 0:
            del buf[:idx]
        if len(buf) < len(IMPROV_HEADER) + 2:
            return out  # need type + len
        pkt_type = buf[len(IMPROV_HEADER)]
        data_len = buf[len(IMPROV_HEADER) + 1]
        total = len(IMPROV_HEADER) + 2 + data_len + 1
        if len(buf) < total:
            return out  # need more data
        data = bytes(buf[len(IMPROV_HEADER) + 2 : len(IMPROV_HEADER) + 2 + data_len])
        # Validate checksum and skip frame if wrong (resync at next IMPROV).
        body = bytes(buf[: total - 1])
        expected = sum(body) & 0xFF
        actual = buf[total - 1]
        del buf[:total]
        if expected == actual:
            out.append((pkt_type, data))
        else:
            logger.warning("dropped Improv frame with bad checksum")


def push_credentials(port: str, ssid: str, password: str, *, timeout: float = 30.0) -> None:
    """Open the dial's serial port and run the Improv handshake.

    Walks the dial through PROVISIONING → PROVISIONED, raising on error
    or timeout. The dial library validates the SSID/password by actually
    connecting to WiFi before reporting PROVISIONED, so a successful
    return means the dial is on WiFi."""
    import serial  # pyserial; deferred so import is optional for tests

    logger.info("opening %s @ 115200 baud", port)
    with serial.Serial(port, 115200, timeout=0.2) as ser:
        # Drain stale bytes that may be in the buffer from a prior session
        # (e.g. boot log).
        time.sleep(0.5)
        ser.reset_input_buffer()

        pkt = _build_submit_settings(ssid, password)
        logger.info("sending SUBMIT_SETTINGS (ssid=%s, pass=<%d chars>)", ssid, len(password))
        ser.write(pkt)
        ser.flush()

        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                for pkt_type, data in _scan_packets(buf):
                    if pkt_type == IMPROV_PKT_CURRENT_STATE and len(data) == 1:
                        state = data[0]
                        logger.info("dial reports state=0x%02x", state)
                        if state == IMPROV_STATE_PROVISIONED:
                            return
                    elif pkt_type == IMPROV_PKT_ERROR_STATE and len(data) == 1:
                        # The library emits ERROR_STATE=0x00 (ERROR_NONE)
                        # right before PROVISIONED on success — it's a
                        # "clear any previous error" message, not a
                        # failure signal. Only treat non-zero as fatal.
                        if data[0] != IMPROV_ERROR_NONE:
                            raise RuntimeError(
                                f"Improv error from dial: 0x{data[0]:02x}"
                            )
                    elif pkt_type == IMPROV_PKT_RPC_RESPONSE:
                        # Optional redirect URL — we don't need it.
                        pass
            else:
                time.sleep(0.05)
        raise TimeoutError(
            f"Dial did not reach PROVISIONED state within {timeout:.0f}s. "
            "Check the dial's WS2812 status LED — red blink means WiFi "
            "associated but no DHCP, solid red means SSID/password rejected."
        )


# ---------- esptool flash (optional) ----------


def flash_firmware(port: str, bin_path: Path) -> None:
    """Flash a phase-1 firmware bin via esptool. Skipped if the bin
    doesn't exist — onboarding is still useful when the dial is already
    flashed and just needs WiFi creds rotated."""
    if not bin_path.exists():
        logger.info("skipping flash — no firmware bin at %s", bin_path)
        return

    # 460800 baud (not 921600): empirically, ESP32-S3 native USB CDC
    # corrupts mid-transfer at 921600 ("Serial data stream stopped:
    # Possible serial noise or corruption."). 460800 completes a 900 KB
    # flash in ~7 seconds, fast enough.
    #
    # `write-flash` (hyphen) is the esptool 5.x command name — `write_flash`
    # still works but prints a deprecation warning.
    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "--port", port,
        "--baud", "460800",
        "write-flash",
        "-z",
        "0x0", str(bin_path),
    ]
    logger.info("flashing %s → %s", bin_path, port)
    subprocess.run(cmd, check=True)
    # esptool resets the chip after write_flash; give it a moment to
    # come up before we try to talk Improv.
    time.sleep(2.0)


# ---------- mDNS reachability check ----------


def wait_for_online(hostname: str, timeout: float = 30.0) -> str | None:
    """Resolve `hostname` (e.g. jasper-dial.local) until it answers, or
    return None on timeout. Resolution alone is the success signal —
    we don't need to open a port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return socket.gethostbyname(hostname)
        except socket.gaierror:
            time.sleep(1.0)
    return None


# ---------- entry point ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-dial-onboard",
        description="Provision the JTS rotary dial via USB (flash + push WiFi creds)",
    )
    parser.add_argument(
        "--port", default=None,
        help="explicit serial port (default: auto-detect ESP32-S3)",
    )
    parser.add_argument(
        "--bin", default="/opt/jasper/firmware/dial/jasper-dial.bin", type=Path,
        help="firmware bin to flash (skipped if missing — for already-flashed dials)",
    )
    parser.add_argument(
        "--no-flash", action="store_true",
        help="skip the flash step even if a bin is present",
    )
    parser.add_argument(
        "--ssid", default=None,
        help="WiFi SSID (default: read from NetworkManager / wpa_supplicant)",
    )
    parser.add_argument(
        "--password", default=None,
        help="WiFi password (default: read from NetworkManager / wpa_supplicant)",
    )
    parser.add_argument(
        "--mdns-host", default="jasper-dial.local",
        help="mDNS hostname to ping after provisioning (default jasper-dial.local)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        dial = find_dial(args.port)
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    logger.info("found dial on %s (vid=0x%04x pid=0x%04x)", dial.port, dial.vid, dial.pid)

    if not args.no_flash:
        try:
            flash_firmware(dial.port, args.bin)
            # Re-discover post-flash — esptool resets the chip and the
            # CDC serial may briefly drop. Auto-detect can pick the new
            # port if it changed.
            if args.port is None:
                dial = find_dial(None)
        except subprocess.CalledProcessError as e:
            logger.error("esptool failed: %s", e)
            return 2

    if args.ssid is None or args.password is None:
        try:
            ssid, password = read_pi_wifi()
        except RuntimeError as e:
            logger.error(str(e))
            return 3
    else:
        ssid, password = args.ssid, args.password

    try:
        push_credentials(dial.port, ssid, password)
    except (RuntimeError, TimeoutError) as e:
        logger.error("Improv push failed: %s", e)
        return 4

    logger.info("dial reports PROVISIONED — checking mDNS %s", args.mdns_host)
    ip = wait_for_online(args.mdns_host, timeout=20.0)
    if ip is None:
        logger.warning(
            "dial did not appear at %s within 20 s. WiFi probably worked "
            "(Improv waits for it before reporting PROVISIONED), but mDNS "
            "may be filtered on your network. The dial should still reach "
            "jasper.local for HTTP control.", args.mdns_host,
        )
    else:
        logger.info("dial online at %s (%s)", args.mdns_host, ip)

    logger.info("done. Unplug from the Pi and connect to USB power; turning the knob will adjust volume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
