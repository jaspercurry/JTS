# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WiiM Remote 2 BLE microphone adapter.

The remote exposes button presses as ordinary HID events, but its built-in
microphone is a vendor-shaped HID-over-GATT voice report rather than a Linux
capture device. This daemon is intentionally narrow: subscribe to the WiiM
voice GATT report, decode the remote's 16 kHz ADPCM stream, and forward PCM
frames to a local UDP mic source. The voice daemon owns push-to-talk session
routing through ``JASPER_MANUAL_MIC_SOURCES``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import socket
import sys
import time
from array import array
from dataclasses import dataclass
from typing import Any, Mapping

from dbus_next.errors import DBusError  # type: ignore

from jasper.log_event import log_event

from .constants import WIIM_REMOTE_2_MIC_UDP_PORT, WIIM_REMOTE_2_NAME_RE

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
BLUEZ_GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
BLUEZ_GATT_DESCRIPTOR_IFACE = "org.bluez.GattDescriptor1"
BLUEZ_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
BLUEZ_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

HID_REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb"
# WIIM_REMOTE_2_NAME_RE is imported from .constants — single source of truth.
WIIM_VOICE_REPORT_REFERENCE = bytes((0x03, 0x01))

WIIM_VOICE_PACKET_BYTES = 131
WIIM_VOICE_FRAMING_BYTES = 3
WIIM_STREAM_STARTUP_DROP_PACKETS = 2
WIIM_VOICE_PACKET_SAMPLES = 256
MANUAL_MIC_FRAME_SAMPLES = 1280
MANUAL_MIC_FRAME_BYTES = MANUAL_MIC_FRAME_SAMPLES * 2
WIIM_STREAM_GAP_SEC = 0.250
DEFAULT_UDP_PORT = WIIM_REMOTE_2_MIC_UDP_PORT

_ADPCM_INDEX_TABLE = (
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
)
_ADPCM_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
    13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)


class DeviceNotReady(RuntimeError):
    """Raised when the paired WiiM voice characteristic is not available."""


def _variant_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _bytes_from_dbus_value(value: Any) -> bytes:
    raw = _variant_value(value)
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    return bytes(int(x) & 0xFF for x in raw)


def _uuid(value: Any) -> str:
    return str(_variant_value(value) or "").lower()


@dataclass(frozen=True)
class VoiceCharacteristicCandidate:
    device_path: str
    characteristic_path: str
    descriptor_path: str | None


def voice_characteristic_candidates(
    managed_objects: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    name_regex: str = WIIM_REMOTE_2_NAME_RE,
) -> list[VoiceCharacteristicCandidate]:
    """Return connected WiiM HID report characteristics worth probing.

    The HID service has multiple 0x2a4d Report characteristics. The voice
    stream is the one whose Report Reference descriptor reads ``03 01``; when
    BlueZ already has the descriptor value in ObjectManager state tests can
    fully resolve it here, and at runtime ``_find_voice_characteristic`` reads
    missing descriptor values over D-Bus.
    """
    pattern = re.compile(name_regex)
    devices: set[str] = set()
    for path, ifaces in managed_objects.items():
        props = ifaces.get(BLUEZ_DEVICE_IFACE)
        if props is None:
            continue
        if not bool(_variant_value(props.get("Connected"))):
            continue
        name = str(
            _variant_value(props.get("Alias"))
            or _variant_value(props.get("Name"))
            or ""
        )
        if pattern.search(name):
            devices.add(str(path))

    candidates: list[VoiceCharacteristicCandidate] = []
    for path, ifaces in managed_objects.items():
        char_props = ifaces.get(BLUEZ_GATT_CHARACTERISTIC_IFACE)
        if char_props is None:
            continue
        device_path = next(
            (dev for dev in devices if str(path).startswith(f"{dev}/")),
            None,
        )
        if device_path is None:
            continue
        if _uuid(char_props.get("UUID")) != HID_REPORT_UUID:
            continue
        flags = {
            str(_variant_value(flag)).lower()
            for flag in _variant_value(char_props.get("Flags")) or []
        }
        if "notify" not in flags:
            continue
        descriptor_path = None
        for desc_path, desc_ifaces in managed_objects.items():
            if not str(desc_path).startswith(f"{path}/"):
                continue
            desc_props = desc_ifaces.get(BLUEZ_GATT_DESCRIPTOR_IFACE)
            if desc_props is None:
                continue
            if _uuid(desc_props.get("UUID")) == REPORT_REFERENCE_UUID:
                descriptor_path = str(desc_path)
                break
        candidates.append(VoiceCharacteristicCandidate(
            device_path=device_path,
            characteristic_path=str(path),
            descriptor_path=descriptor_path,
        ))
    return candidates


class ImaAdpcmDecoder:
    """Continuous IMA ADPCM decoder, low nibble first.

    TI's BLE voice HID examples use this nibble order, and the WiiM Remote 2
    capture confirmed the same shape: each 128-byte report payload decodes to
    256 signed 16-bit samples at 16 kHz.
    """

    def __init__(self) -> None:
        self.predictor = 0
        self.index = 0

    def reset(self) -> None:
        self.predictor = 0
        self.index = 0

    def decode(self, payload: bytes) -> list[int]:
        samples: list[int] = []
        for byte in payload:
            samples.append(self._decode_nibble(byte & 0x0F))
            samples.append(self._decode_nibble((byte >> 4) & 0x0F))
        return samples

    def _decode_nibble(self, nibble: int) -> int:
        step = _ADPCM_STEP_TABLE[self.index]
        diff = step >> 3
        if nibble & 0x01:
            diff += step >> 2
        if nibble & 0x02:
            diff += step >> 1
        if nibble & 0x04:
            diff += step
        if nibble & 0x08:
            self.predictor -= diff
        else:
            self.predictor += diff
        self.predictor = max(-32768, min(32767, self.predictor))
        self.index += _ADPCM_INDEX_TABLE[nibble]
        self.index = max(0, min(len(_ADPCM_STEP_TABLE) - 1, self.index))
        return self.predictor


class WiimVoicePacketStream:
    """Convert WiiM voice notifications into 80 ms PCM frames."""

    def __init__(self) -> None:
        self._decoder = ImaAdpcmDecoder()
        self._pcm = bytearray()
        self._drop_remaining = WIIM_STREAM_STARTUP_DROP_PACKETS
        self._last_packet_at: float | None = None
        self.packets = 0
        self.frames = 0
        self.bad_packets = 0
        self.resets = 0

    def reset(self) -> None:
        self._decoder.reset()
        self._pcm.clear()
        self._drop_remaining = WIIM_STREAM_STARTUP_DROP_PACKETS
        self._last_packet_at = None
        self.resets += 1

    def feed_notification(
        self,
        payload: bytes,
        *,
        now: float | None = None,
    ) -> list[bytes]:
        now = time.monotonic() if now is None else now
        if self._last_packet_at is not None:
            gap = now - self._last_packet_at
            if gap > WIIM_STREAM_GAP_SEC:
                log_event(
                    logger,
                    "wiim_remote_mic.stream_reset",
                    gap_ms=round(gap * 1000),
                    level=logging.DEBUG,
                )
                self.reset()
        self._last_packet_at = now

        if len(payload) != WIIM_VOICE_PACKET_BYTES:
            self.bad_packets += 1
            log_event(
                logger,
                "wiim_remote_mic.bad_packet",
                length=len(payload),
                expected=WIIM_VOICE_PACKET_BYTES,
                level=logging.WARNING,
            )
            return []

        self.packets += 1
        if self._drop_remaining:
            self._drop_remaining -= 1
            return []

        adpcm = payload[WIIM_VOICE_FRAMING_BYTES:]
        samples = self._decoder.decode(adpcm)
        if len(samples) != WIIM_VOICE_PACKET_SAMPLES:
            raise AssertionError("WiiM ADPCM packet decoded to wrong size")

        pcm = array("h", samples)
        if sys.byteorder != "little":
            pcm.byteswap()
        self._pcm.extend(pcm.tobytes())

        out: list[bytes] = []
        while len(self._pcm) >= MANUAL_MIC_FRAME_BYTES:
            out.append(bytes(self._pcm[:MANUAL_MIC_FRAME_BYTES]))
            del self._pcm[:MANUAL_MIC_FRAME_BYTES]
            self.frames += 1
        return out


class UdpPcmSink:
    def __init__(self, host: str, port: int) -> None:
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)

    def send(self, frame: bytes) -> None:
        self._sock.sendto(frame, self.addr)

    def close(self) -> None:
        self._sock.close()


async def _read_descriptor_value(bus: Any, path: str) -> bytes:
    intro = await bus.introspect(BLUEZ_BUS, path)
    proxy = bus.get_proxy_object(BLUEZ_BUS, path, intro)
    descriptor = proxy.get_interface(BLUEZ_GATT_DESCRIPTOR_IFACE)
    return _bytes_from_dbus_value(await descriptor.call_read_value({}))


async def _find_voice_characteristic(
    bus: Any,
    managed_objects: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    name_regex: str,
) -> VoiceCharacteristicCandidate:
    candidates = voice_characteristic_candidates(
        managed_objects,
        name_regex=name_regex,
    )
    match: VoiceCharacteristicCandidate | None = None
    for candidate in candidates:
        if candidate.descriptor_path is None:
            continue
        desc_props = managed_objects.get(candidate.descriptor_path, {}).get(
            BLUEZ_GATT_DESCRIPTOR_IFACE,
            {},
        )
        value = _bytes_from_dbus_value(desc_props.get("Value"))
        if not value:
            value = await _read_descriptor_value(bus, candidate.descriptor_path)
        if value == WIIM_VOICE_REPORT_REFERENCE:
            if match is not None:
                raise DeviceNotReady(
                    "multiple WiiM Remote 2 voice reports found; "
                    "leave exactly one remote connected and retry"
                )
            match = candidate
    if match is not None:
        return match
    raise DeviceNotReady("connected WiiM Remote 2 voice report not found")


async def _connect_bluez():
    from dbus_next import BusType  # type: ignore
    from dbus_next.aio import MessageBus  # type: ignore

    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def _run_subscription(args: argparse.Namespace, stop: asyncio.Event) -> None:
    bus = await _connect_bluez()
    sink = UdpPcmSink(args.udp_host, args.udp_port)
    stream = WiimVoicePacketStream()
    done = asyncio.Event()
    try:
        intro = await bus.introspect(BLUEZ_BUS, "/")
        om = bus.get_proxy_object(BLUEZ_BUS, "/", intro).get_interface(
            BLUEZ_OBJECT_MANAGER_IFACE,
        )
        managed = await om.call_get_managed_objects()
        candidate = await _find_voice_characteristic(
            bus,
            managed,
            name_regex=args.device_name_regex,
        )
        log_event(
            logger,
            "wiim_remote_mic.connected",
            device=candidate.device_path,
            characteristic=candidate.characteristic_path,
            udp=f"{args.udp_host}:{args.udp_port}",
        )

        char_intro = await bus.introspect(BLUEZ_BUS, candidate.characteristic_path)
        char_proxy = bus.get_proxy_object(
            BLUEZ_BUS,
            candidate.characteristic_path,
            char_intro,
        )
        char = char_proxy.get_interface(BLUEZ_GATT_CHARACTERISTIC_IFACE)
        char_props = char_proxy.get_interface(BLUEZ_PROPERTIES_IFACE)

        def on_char_properties(iface: str, changed: dict, _invalidated: list) -> None:
            if iface != BLUEZ_GATT_CHARACTERISTIC_IFACE or "Value" not in changed:
                return
            try:
                payload = _bytes_from_dbus_value(changed["Value"])
                for frame in stream.feed_notification(payload):
                    sink.send(frame)
            except (AssertionError, OSError, TypeError, ValueError) as exc:
                log_event(
                    logger,
                    "wiim_remote_mic.packet_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    level=logging.WARNING,
                )

        char_props.on_properties_changed(on_char_properties)

        dev_intro = await bus.introspect(BLUEZ_BUS, candidate.device_path)
        dev_proxy = bus.get_proxy_object(BLUEZ_BUS, candidate.device_path, dev_intro)
        dev_props = dev_proxy.get_interface(BLUEZ_PROPERTIES_IFACE)

        def on_device_properties(iface: str, changed: dict, _invalidated: list) -> None:
            if iface != BLUEZ_DEVICE_IFACE or "Connected" not in changed:
                return
            if not bool(_variant_value(changed["Connected"])):
                done.set()

        dev_props.on_properties_changed(on_device_properties)

        await char.call_start_notify()
        log_event(
            logger,
            "wiim_remote_mic.notify_started",
            source=args.source_id,
            udp=f"{args.udp_host}:{args.udp_port}",
        )
        try:
            while not stop.is_set() and not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
        finally:
            try:
                await char.call_stop_notify()
            except (DBusError, OSError):
                pass
        log_event(
            logger,
            "wiim_remote_mic.disconnected",
            source=args.source_id,
            packets=stream.packets,
            frames=stream.frames,
            bad_packets=stream.bad_packets,
            resets=stream.resets,
        )
    finally:
        sink.close()
        disconnect = getattr(bus, "disconnect", None)
        if callable(disconnect):
            disconnect()


async def _run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    last_error_key: str | None = None
    last_error_logged_at = 0.0

    def should_log_error(key: str) -> bool:
        nonlocal last_error_key, last_error_logged_at
        now = time.monotonic()
        if key != last_error_key or now - last_error_logged_at >= 60.0:
            last_error_key = key
            last_error_logged_at = now
            return True
        return False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    while not stop.is_set():
        try:
            await _run_subscription(args, stop)
            last_error_key = None
        except DeviceNotReady as exc:
            detail = str(exc)
            log_event(
                logger,
                "wiim_remote_mic.not_ready",
                detail=detail,
                level=(
                    logging.INFO
                    if should_log_error(f"not_ready:{detail}")
                    else logging.DEBUG
                ),
            )
        except (
            AttributeError,
            DBusError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            log_event(
                logger,
                "wiim_remote_mic.subscription_failed",
                error=detail,
                level=(
                    logging.WARNING
                    if should_log_error(f"error:{detail}")
                    else logging.DEBUG
                ),
            )
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.retry_sec)
        except asyncio.TimeoutError:
            pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", default="wiim_remote_2")
    parser.add_argument("--device-name-regex", default=WIIM_REMOTE_2_NAME_RE)
    parser.add_argument("--udp-host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--retry-sec", type=float, default=2.0)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
