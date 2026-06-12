"""jasper-satellite-onboard - Pi-mediated provisioning for the JTS AMOLED satellite."""
from __future__ import annotations

import logging
from pathlib import Path

from ._esp32_onboard import (
    ESP32_S3_PIDS,
    ESP32_S3_VID,
    DeviceProfile,
    SerialDevice,
    find_device,
    flash_firmware as _flash_firmware,
    probe_firmware,
    read_pi_wifi,
    run_onboard,
    wait_for_online,
)
from ._improv import (
    IMPROV_CMD_GET_DEVICE_INFO,
    IMPROV_CMD_SUBMIT_SETTINGS,
    IMPROV_ERROR_NONE,
    IMPROV_HEADER,
    IMPROV_PKT_CURRENT_STATE,
    IMPROV_PKT_ERROR_STATE,
    IMPROV_PKT_RPC,
    IMPROV_PKT_RPC_RESPONSE,
    IMPROV_STATE_AUTHORIZED,
    IMPROV_STATE_AWAITING_AUTH,
    IMPROV_STATE_PROVISIONED,
    IMPROV_STATE_PROVISIONING,
    IMPROV_STATE_STOPPED,
    _build_submit_settings,
    _improv_packet,
    _scan_packets,
    push_credentials as _push_credentials,
)

logger = logging.getLogger("jasper-satellite-onboard")

__all__ = [
    "SATELLITE_PROFILE",
    "Satellite",
    "ESP32_S3_PIDS",
    "ESP32_S3_VID",
    "IMPROV_CMD_GET_DEVICE_INFO",
    "IMPROV_CMD_SUBMIT_SETTINGS",
    "IMPROV_ERROR_NONE",
    "IMPROV_HEADER",
    "IMPROV_PKT_CURRENT_STATE",
    "IMPROV_PKT_ERROR_STATE",
    "IMPROV_PKT_RPC",
    "IMPROV_PKT_RPC_RESPONSE",
    "IMPROV_STATE_AUTHORIZED",
    "IMPROV_STATE_AWAITING_AUTH",
    "IMPROV_STATE_PROVISIONED",
    "IMPROV_STATE_PROVISIONING",
    "IMPROV_STATE_STOPPED",
    "_build_submit_settings",
    "_improv_packet",
    "_scan_packets",
    "find_satellite",
    "flash_firmware",
    "main",
    "probe_jts_satellite",
    "push_credentials",
    "read_pi_wifi",
    "wait_for_online",
]

SATELLITE_PROFILE = DeviceProfile(
    prog="jasper-satellite-onboard",
    description="Provision the JTS AMOLED satellite via USB (flash + push WiFi creds)",
    device_label="satellite",
    firmware_bin_path="/opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin",
    bin_help=(
        "firmware bin to flash (skipped if missing — for already-flashed satellites)"
    ),
    flash_help=(
        "force-flash even if the satellite is already running JTS firmware "
        "(default: skip flash when the Improv probe succeeds — i.e. when "
        "the device already speaks our protocol). Mutually exclusive with "
        "--no-flash; this flag wins if both are passed."
    ),
    mdns_hostname="jasper-satellite-amoled.local",
    mdns_help=(
        "mDNS hostname to ping after provisioning "
        "(default jasper-satellite-amoled.local)"
    ),
    auto_help=(
        "idempotent mode: if the satellite already responds at --mdns-host, "
        "exit without flashing or pushing creds. If not, proceed only when "
        "the boot-log probe confirms JTS firmware; otherwise refuse before "
        "reading or pushing WiFi credentials."
    ),
    boot_signature=b"jasper-satellite-amoled firmware",
    boot_log_description='[boot] jasper-satellite-amoled firmware v<version>',
    done_message=(
        "done. Unplug from the Pi and connect to USB power; "
        "the satellite should connect to the speaker over WiFi."
    ),
)

Satellite = SerialDevice


def find_satellite(explicit_port: str | None = None) -> SerialDevice:
    return find_device(SATELLITE_PROFILE, explicit_port)


def probe_jts_satellite(port: str, *, timeout_s: float = 5.0) -> bool:
    return probe_firmware(SATELLITE_PROFILE, port, timeout_s=timeout_s, log=logger)


def flash_firmware(port: str, bin_path: Path) -> None:
    _flash_firmware(SATELLITE_PROFILE, port, bin_path, log=logger)


def push_credentials(port: str, ssid: str, password: str, *, timeout: float = 30.0) -> None:
    _push_credentials(
        port,
        ssid,
        password,
        timeout=timeout,
        device_label=SATELLITE_PROFILE.device_label,
        log=logger,
    )


def main(argv: list[str] | None = None) -> int:
    return run_onboard(SATELLITE_PROFILE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
