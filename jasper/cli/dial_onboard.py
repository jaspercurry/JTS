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
IMPROV_CMD_GET_DEVICE_INFO = 0x03


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

    Tries NetworkManager first (Trixie/Bookworm default), falls back
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


# Boot-log signature emitted by the JTS dial firmware in setup().
# See firmware/dial/src/main.cpp's `Serial.println("[boot] jasper-dial
# firmware v...")` — this is the unique identifier we look for in the
# probe. Generic ESP32-S3 dev kits, Arduino default sketches, or other
# Improv-using firmwares will not emit this exact string.
_DIAL_BOOT_SIGNATURE = b"jasper-dial firmware"


def probe_jts_dial(port: str, *, timeout_s: float = 5.0) -> bool:
    """Identify whether the device on `port` is running our dial
    firmware. Returns True on positive ID, False on timeout / error.

    Mechanism: opening /dev/ttyACMx on the ESP32-S3 native USB-CDC
    causes the chip to reset (the USB-Serial-JTAG controller pulses
    the chip's reset line on CDC SET_CONTROL_LINE_STATE — observed
    empirically with `dtr=False, rts=False` still resetting; the
    behavior is on the ROM side, not pyserial's). The dial's setup()
    then prints `[boot] jasper-dial firmware v<version>` over Serial
    within ~1-2 seconds. We open, listen for that exact string, and
    return True if seen.

    Why this beats sending an Improv RPC: the dial's `tryConnectStored`
    in setup() blocks for up to 15s on stored-creds WiFi reconnect
    BEFORE loop() runs, and improv.handleSerial() runs in loop(). So
    an RPC sent at port-open sits in the buffer for 15+ seconds before
    a reply comes — too slow for udev-fired auto-onboarding. The boot
    log appears immediately in setup(), much faster.

    Why opening the port at all is acceptable: on the JTS dial path
    `--auto` mode runs an mDNS pre-check first (jasper-dial.local in
    3s); already-online dials short-circuit before this probe runs,
    so the chip-reset disruption only fires when the dial wasn't
    going to be useful anyway (fresh, off-network, or unrelated S3).
    Unrelated ESP32-S3 boards plugged into the Pi will reset on open
    too, but no flash or cred-push happens after a probe miss — chip
    reboots, no other side effect.
    """
    import serial  # pyserial; deferred so import is optional for tests

    # Open the port with pyserial's defaults — on Linux + ESP32-S3
    # native USB CDC, this raises DTR which the USB-Serial-JTAG ROM
    # treats as a reset request. The chip reboots and emits the boot
    # log within ~1-2 s, which is what we look for. Suppressing DTR
    # via dsrdtr=False/dtr=False was tried and made the probe
    # unreliable: on a chip that's already booted from a prior open,
    # the suppressed-DTR open doesn't trigger a fresh boot log, so
    # the probe sees nothing even on a real JTS dial. Reset is the
    # signal here, not the bug.
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except (OSError, Exception) as e:  # noqa: BLE001
        logger.debug("dial probe: open %s failed: %s", port, e)
        return False

    try:
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = ser.read(256)
            except (OSError, IOError) as e:
                logger.debug("dial probe: read failed: %s", e)
                return False
            if chunk:
                buf.extend(chunk)
                if _DIAL_BOOT_SIGNATURE in buf:
                    return True
            else:
                time.sleep(0.05)
        return False
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass


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
        "--flash", action="store_true",
        help="force-flash even if the dial is already running JTS firmware "
        "(default: skip flash when the Improv probe succeeds — i.e. when "
        "the device already speaks our protocol). Mutually exclusive with "
        "--no-flash; this flag wins if both are passed.",
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
        "--auto", action="store_true",
        help="udev-triggered idempotent mode: if the dial already responds at "
        "--mdns-host, exit without flashing or pushing creds. If not, proceed "
        "with the normal flash + provision flow. Designed for the on-plug-in "
        "udev rule so re-plugging an already-provisioned dial is a no-op.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Idempotency short-circuit. In --auto mode (udev-triggered re-plug),
    # if the dial already resolves on mDNS we treat that as "already
    # provisioned and on the same WiFi", and exit without touching it.
    # Opening the serial port to talk Improv would reset the dial via
    # DTR; skipping is genuinely cheaper. Manual runs without --auto
    # always proceed (operator may want to re-flash or re-push creds).
    if args.auto:
        ip = wait_for_online(args.mdns_host, timeout=3.0)
        if ip is not None:
            logger.info(
                "dial already online at %s (%s) — auto mode short-circuit, "
                "no flash or cred push needed",
                args.mdns_host, ip,
            )
            return 0
        logger.info(
            "dial not yet online at %s — proceeding with onboard flow",
            args.mdns_host,
        )

    try:
        dial = find_dial(args.port)
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    logger.info("found dial on %s (vid=0x%04x pid=0x%04x)", dial.port, dial.vid, dial.pid)

    # Boot-log fingerprint: does the device emit our JTS firmware
    # signature on serial open? The udev rule that fires us only
    # matches by ESP32-S3 VID/PID, which any unrelated S3 board will
    # satisfy. This probe is the actual filter — narrows from "any
    # S3 board" to "this S3 board is running our dial firmware".
    is_jts_dial = probe_jts_dial(dial.port)
    if is_jts_dial:
        logger.info(
            "boot-log probe succeeded — device is running JTS dial "
            "firmware, no flash needed",
        )
    else:
        logger.info(
            "boot-log probe didn't see JTS firmware signature — "
            "device either needs flashing, or it's not a JTS dial",
        )

    # Flash decision matrix:
    #   is_jts_dial    args.flash  args.no_flash  args.auto  → flash?
    #   ─────────────  ──────────  ─────────────  ─────────  ────────
    #   True           True        *              *          yes (forced)
    #   True           False       *              *          no (already JTS)
    #   False          *           True           *          no (--no-flash wins)
    #   False          False       False          True       no (auto: try push only)
    #   False          *           False          False      yes (manual fresh-chip path)
    #
    # The "False / auto" cell tries the Improv push without flashing.
    # The boot-log probe is unreliable: it depends on the chip resetting
    # at serial open, which only happens on the first port-open after
    # USB plug-in. Subsequent opens skip the reset and the boot log
    # never shows, even on a real JTS dial. Falling through to push in
    # --auto mode means: if it IS our dial that the probe missed, the
    # push handshakes successfully. If it's an unrelated ESP32-S3, the
    # push times out cleanly (Improv handshake fails). Either way no
    # flash happens, so we never write JTS firmware over unrelated
    # hardware. To intentionally flash a fresh chip use `--flash`.
    should_flash = False
    if is_jts_dial:
        should_flash = bool(args.flash)
    else:
        if args.no_flash:
            should_flash = False
        elif args.auto:
            logger.info(
                "device on %s didn't show the JTS boot signature, but "
                "the probe is unreliable when the chip doesn't reset on "
                "serial open. Trying Improv push anyway — if it's a JTS "
                "dial the push will succeed, otherwise it'll time out "
                "cleanly without flashing.",
                dial.port,
            )
            should_flash = False
        else:
            should_flash = True

    if should_flash:
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
        speaker_host = os.environ.get("JASPER_HOSTNAME", "jts.local")
        logger.warning(
            "dial did not appear at %s within 20 s. WiFi probably worked "
            "(Improv waits for it before reporting PROVISIONED), but mDNS "
            "may be filtered on your network. The dial should still reach "
            "%s for HTTP control.", args.mdns_host, speaker_host,
        )
    else:
        logger.info("dial online at %s (%s)", args.mdns_host, ip)

    logger.info("done. Unplug from the Pi and connect to USB power; turning the knob will adjust volume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
