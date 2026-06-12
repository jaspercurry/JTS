"""Shared Improv-over-Serial helpers for ESP32 accessory onboarding."""
from __future__ import annotations

import logging
import time
from enum import IntEnum

logger = logging.getLogger(__name__)


# Improv-over-Serial framing. See https://www.improv-wifi.com/serial/.
IMPROV_HEADER = b"IMPROV\x01"  # magic + protocol version


class ImprovPacketType(IntEnum):
    CURRENT_STATE = 0x01
    ERROR_STATE = 0x02
    RPC = 0x03
    RPC_RESPONSE = 0x04


# State enum values per the Improv-WiFi spec. Critical: these are
# the values inside CURRENT_STATE packet bodies, NOT the wire packet
# type IDs above. Source of truth: ImprovTypes.h in the library
# (jnthas/Improv-WiFi-Library).
class ImprovState(IntEnum):
    STOPPED = 0x00
    AWAITING_AUTH = 0x01
    AUTHORIZED = 0x02
    PROVISIONING = 0x03
    PROVISIONED = 0x04


class ImprovError(IntEnum):
    NONE = 0x00


class ImprovCommand(IntEnum):
    SUBMIT_SETTINGS = 0x01
    GET_DEVICE_INFO = 0x03


IMPROV_PKT_CURRENT_STATE = ImprovPacketType.CURRENT_STATE
IMPROV_PKT_ERROR_STATE = ImprovPacketType.ERROR_STATE
IMPROV_PKT_RPC = ImprovPacketType.RPC
IMPROV_PKT_RPC_RESPONSE = ImprovPacketType.RPC_RESPONSE

IMPROV_STATE_STOPPED = ImprovState.STOPPED
IMPROV_STATE_AWAITING_AUTH = ImprovState.AWAITING_AUTH
IMPROV_STATE_AUTHORIZED = ImprovState.AUTHORIZED
IMPROV_STATE_PROVISIONING = ImprovState.PROVISIONING
IMPROV_STATE_PROVISIONED = ImprovState.PROVISIONED

IMPROV_ERROR_NONE = ImprovError.NONE

IMPROV_CMD_SUBMIT_SETTINGS = ImprovCommand.SUBMIT_SETTINGS
IMPROV_CMD_GET_DEVICE_INFO = ImprovCommand.GET_DEVICE_INFO


def _improv_packet(pkt_type: int, data: bytes) -> bytes:
    """Frame a packet per the Improv-over-Serial spec.

    Wire format: IMPROV<v> <type:1> <len:1> <data:len> <checksum:1>
    Checksum = sum of all preceding bytes mod 256.
    """
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
        bytes([len(ssid_b)])
        + ssid_b
        + bytes([len(pass_b)])
        + pass_b
    )
    # RPC: command(1) + data_len(1) + data
    pkt_data = bytes([IMPROV_CMD_SUBMIT_SETTINGS, len(rpc_data)]) + rpc_data
    return _improv_packet(IMPROV_PKT_RPC, pkt_data)


def _scan_packets(buf: bytearray) -> list[tuple[int, bytes]]:
    """Pull complete Improv packets out of ``buf``.

    Mutates ``buf`` to remove consumed bytes. Returns a list of
    ``(pkt_type, data)`` tuples.
    """
    out: list[tuple[int, bytes]] = []
    while True:
        idx = buf.find(IMPROV_HEADER)
        if idx < 0:
            # No header yet; preserve the longest suffix that might be
            # the beginning of the next header.
            keep = len(IMPROV_HEADER) - 1
            if len(buf) > keep:
                del buf[:-keep]
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
        body = bytes(buf[: total - 1])
        expected = sum(body) & 0xFF
        actual = buf[total - 1]
        if expected == actual:
            del buf[:total]
            out.append((pkt_type, data))
        else:
            # Drop only the first byte of the bad frame so header hunting
            # can resync if another IMPROV header appears inside or after it.
            del buf[:1]
            logger.warning("dropped Improv frame with bad checksum")


def push_credentials(
    port: str,
    ssid: str,
    password: str,
    *,
    timeout: float = 30.0,
    device_label: str = "device",
    log: logging.Logger | None = None,
) -> None:
    """Open the serial port and run the Improv credential handshake."""
    import serial  # pyserial; deferred so import is optional for tests

    log = log or logger
    device_title = device_label.capitalize()
    log.info("opening %s @ 115200 baud", port)
    with serial.Serial(port, 115200, timeout=0.2) as ser:
        # Drain stale bytes that may be in the buffer from a prior session
        # (for example, a boot log).
        time.sleep(0.5)
        ser.reset_input_buffer()

        pkt = _build_submit_settings(ssid, password)
        log.info("sending SUBMIT_SETTINGS (ssid=%s, pass=<%d chars>)", ssid, len(password))
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
                        log.info("%s reports state=0x%02x", device_label, state)
                        if state == IMPROV_STATE_PROVISIONED:
                            return
                    elif pkt_type == IMPROV_PKT_ERROR_STATE and len(data) == 1:
                        # The library emits ERROR_STATE=0x00 (ERROR_NONE)
                        # right before PROVISIONED on success; only non-zero
                        # values are fatal.
                        if data[0] != IMPROV_ERROR_NONE:
                            raise RuntimeError(
                                f"Improv error from {device_label}: 0x{data[0]:02x}"
                            )
                    elif pkt_type == IMPROV_PKT_RPC_RESPONSE:
                        # Optional redirect URL; JTS does not need it.
                        pass
            else:
                time.sleep(0.05)
        raise TimeoutError(
            f"{device_title} did not reach PROVISIONED state within {timeout:.0f}s. "
            f"Check the {device_label}'s WS2812 status LED - red blink means WiFi "
            "associated but no DHCP, solid red means SSID/password rejected."
        )
