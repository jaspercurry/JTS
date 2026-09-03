# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WiiM Remote 2 BLE microphone adapter.

The remote exposes button presses as ordinary HID events, but its built-in
microphone is a vendor-shaped HID-over-GATT voice report rather than a Linux
capture device. This adapter is intentionally narrow: subscribe to the WiiM
voice GATT report, decode the remote's 16 kHz ADPCM stream, and forward PCM
frames to a local UDP mic source. The voice daemon owns push-to-talk session
routing through ``JASPER_MANUAL_MIC_SOURCES``.

``run`` is a task inside the ``jasper-input`` process, not a daemon of its own
(ADR-0225): it owns its own reconnect retry, and jasper.accessories.supervisor
is the backstop that keeps a fault here away from the HID button bridge.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys
import time
from array import array
from dataclasses import dataclass
from typing import Any, Mapping

from dbus_next.errors import DBusError  # type: ignore

from jasper.log_event import log_event

from ._dbus import variant_value
from .constants import (
    WIIM_REMOTE_2_MIC_UDP_PORT,
    WIIM_REMOTE_2_NAME_RE,
    WIIM_REMOTE_2_SOURCE_ID,
)

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
# The 3 bytes ahead of the ADPCM payload are an IMA block header carrying the
# encoder's own state: a BIG-endian int16 predictor then the step index. Both
# were confirmed against hardware (jts3, issue #2198). In a 147-packet
# single-burst capture, int16be(payload[0:2]) equalled the last sample the
# previous packet decoded to on 146/146 boundaries, and payload[2] equalled the
# decoder's step index on 147/147. Little-endian — the WAV IMA convention —
# matched 1/146, and that one was coincidence: a header of ff ff 01 reads as -1
# little-endian, which the previous packet happened to end on. So the byte order
# is load-bearing.
WIIM_VOICE_FRAMING_BYTES = 3
WIIM_VOICE_PACKET_SAMPLES = 256
MANUAL_MIC_FRAME_SAMPLES = 1280
MANUAL_MIC_FRAME_BYTES = MANUAL_MIC_FRAME_SAMPLES * 2
WIIM_STREAM_GAP_SEC = 0.250
DEFAULT_UDP_PORT = WIIM_REMOTE_2_MIC_UDP_PORT

# Root helper that reserves BLE connection-event length on the live remote
# link. BlueZ hardcodes that reservation to 0, which lets the controller carry
# roughly one Link Layer PDU per connection event — a quarter of the six PDUs
# each 16 ms voice notification needs. See jasper/cli/wiim_remote_ce.py.
CE_HELPER_UNIT = "jasper-wiim-remote-ce.service"
CE_REQUEST_TIMEOUT_SEC = 5.0

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


def _bytes_from_dbus_value(value: Any) -> bytes:
    raw = variant_value(value)
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    return bytes(int(x) & 0xFF for x in raw)


def _uuid(value: Any) -> str:
    return str(variant_value(value) or "").lower()


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
        if not bool(variant_value(props.get("Connected"))):
            continue
        name = str(
            variant_value(props.get("Alias"))
            or variant_value(props.get("Name"))
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
            str(variant_value(flag)).lower()
            for flag in variant_value(char_props.get("Flags")) or []
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
    """IMA ADPCM decoder, low nibble first.

    TI's BLE voice HID examples use this nibble order, and the WiiM Remote 2
    capture confirmed the same shape: each 128-byte report payload decodes to
    256 signed 16-bit samples at 16 kHz. Decoding high-nibble-first on the same
    hardware capture railed 1572 samples and pulled DC to -26085, so the order
    is not a coin flip.

    State is normally carried across packets, but every WiiM packet also
    reports the encoder's state in its header, so ``resync`` can realign the
    two — see ``WiimVoicePacketStream.feed_notification``.
    """

    def __init__(self) -> None:
        self.predictor = 0
        self.index = 0

    def resync(self, predictor: int, index: int) -> None:
        """Adopt the encoder's own state, as reported in a packet header.

        IMA ADPCM is a pure integrator with no leakage, so any disagreement
        between encoder and decoder state is permanent — it never decays out.
        Adopting the transmitted state each packet keeps the two in lockstep
        and bounds the damage from a dropped packet to that packet alone.
        """
        self.predictor = max(-32768, min(32767, predictor))
        self.index = max(0, min(len(_ADPCM_STEP_TABLE) - 1, index))

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
        self._last_packet_at: float | None = None
        self._segment_first_at: float | None = None
        self._segment_last_at: float | None = None
        self._segment_packets = 0
        self.packets = 0
        self.frames = 0
        self.bad_packets = 0
        self.resets = 0

    def close_segment(self) -> None:
        """Log the delivery rate of the stream segment that just ended.

        This is the starved-link detector, and the *rate* is the whole point:
        a packet count on its own has no denominator. The remote only streams
        while the button is held, so a per-connection count mixes holds with
        idle time — the same 10 s hold logs ~625 packets healthy and ~148
        starved, and a journal reader has no way to tell which hold produced
        which. Per segment the two separate at a glance: ~62/s healthy, ~15/s
        when the BLE connection-event reservation is not in force (see
        ``jasper/cli/wiim_remote_ce.py``), and no line at all when idle.

        The rate is over the observed arrival span, so N packets divide by the
        N-1 gaps between them; a segment too short to have a gap is dropped
        rather than reported at a made-up rate.
        """
        first, last = self._segment_first_at, self._segment_last_at
        packets = self._segment_packets
        self._segment_first_at = None
        self._segment_last_at = None
        self._segment_packets = 0
        if first is None or last is None or packets < 2:
            return
        span = last - first
        if span <= 0:
            return
        log_event(
            logger,
            "wiim_remote_mic.segment",
            packets=packets,
            duration_ms=round(span * 1000),
            rate_hz=round((packets - 1) / span, 1),
        )

    def reset(self) -> None:
        # Only the partial frame is dropped, so a silence is not spliced onto
        # the audio either side of it. The decoder needs no reset: the next
        # packet's header supplies the encoder's state before anything is
        # decoded from it.
        #
        # A reset is exactly a segment boundary — in practice the end of one
        # push-to-talk hold — so this is where the closing segment's rate is
        # reported.
        self.close_segment()
        self._pcm.clear()
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
        if self._segment_first_at is None:
            self._segment_first_at = now
        self._segment_last_at = now
        self._segment_packets += 1

        # Adopt the encoder's state before decoding. In an unbroken stream this
        # is what the decoder already holds, so it changes nothing; after a lost
        # packet it is the difference between losing that packet's 16 ms and
        # decoding every later packet from a predictor that never recovers.
        self._decoder.resync(
            int.from_bytes(payload[0:2], "big", signed=True),
            payload[2],
        )
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


def _start_ce_helper() -> dict[str, Any]:
    """Blocking broker call. Runs on a worker thread — never the event loop.

    Guarded lazy import (mirrors jasper/fanin/coupling_reconcile.py): a
    missing or broken control package degrades to a reported failure instead
    of an exception that would take the adapter down.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as exc:
        return {"ok": False, "error": f"restart_broker unavailable: {exc}"}
    return restart_broker.manage_units(
        CE_HELPER_UNIT,
        verb="start",
        reason="wiim-remote-mic-notify-started",
        # The broker answers as soon as systemd has queued the job; the helper
        # journals its own verified verdict under event=wiim_remote_ce.*. We
        # only need to know the request was accepted, so don't wait for the
        # oneshot to finish.
        no_block=True,
        timeout=CE_REQUEST_TIMEOUT_SEC,
    )


async def _request_ce_reservation() -> None:
    """Ask the root helper to reserve connection-event time for this link.

    Per-connection and transient: the reservation dies with the connection,
    and this process outlives disconnects (``_run_subscription`` returns and
    ``run`` loops), so unit ordering cannot do this — it has to fire here, on
    every reconnect.

    Offloaded to a thread on purpose. ``manage_units`` is a blocking socket
    call, and this coroutine shares its event loop with the D-Bus reader that
    delivers the mic notifications; calling it inline would stall that loop
    and drop exactly the packets this reservation exists to save. The wait is
    bounded by the broker client's own socket timeout, so no outer deadline is
    needed (and none is added — cancelling a ``to_thread`` would orphan the
    thread rather than stop it).

    Fail-soft in every direction: a broken broker means degraded audio, never
    a dead adapter.
    """
    try:
        result = await asyncio.to_thread(_start_ce_helper)
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "wiim_remote_mic.ce_request",
            unit=CE_HELPER_UNIT,
            ok=0,
            error=f"{type(exc).__name__}: {exc}",
            level=logging.WARNING,
        )
        return
    if result.get("ok"):
        log_event(logger, "wiim_remote_mic.ce_request", unit=CE_HELPER_UNIT, ok=1)
        return
    log_event(
        logger,
        "wiim_remote_mic.ce_request",
        unit=CE_HELPER_UNIT,
        ok=0,
        error=str(result.get("error") or f"rc={result.get('rc')}")[:200],
        level=logging.WARNING,
    )


async def _first_of(*awaitables: Any) -> None:
    """Return when the first awaitable completes; cancel and drain the rest."""

    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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


@dataclass(frozen=True)
class MicAdapterConfig:
    """Where the decoded stream goes and how fast reconnects are retried."""

    source_id: str = WIIM_REMOTE_2_SOURCE_ID
    device_name_regex: str = WIIM_REMOTE_2_NAME_RE
    udp_host: str = "127.0.0.1"
    udp_port: int = DEFAULT_UDP_PORT
    retry_sec: float = 2.0


async def _run_subscription(config: MicAdapterConfig) -> None:
    bus = await _connect_bluez()
    sink = UdpPcmSink(config.udp_host, config.udp_port)
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
            name_regex=config.device_name_regex,
        )
        log_event(
            logger,
            "wiim_remote_mic.connected",
            device=candidate.device_path,
            characteristic=candidate.characteristic_path,
            udp=f"{config.udp_host}:{config.udp_port}",
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
            if not bool(variant_value(changed["Connected"])):
                done.set()

        dev_props.on_properties_changed(on_device_properties)

        await char.call_start_notify()
        log_event(
            logger,
            "wiim_remote_mic.notify_started",
            source=config.source_id,
            udp=f"{config.udp_host}:{config.udp_port}",
        )
        await _request_ce_reservation()
        try:
            # Also wake on the BUS dying (a bluetoothd restart), not only on
            # the device's Connected property going false: on a dead bus no
            # property change can ever arrive, so waiting on `done` alone hangs
            # this task forever with nothing raised for the supervisor to see.
            await _first_of(done.wait(), bus.wait_for_disconnect())
        finally:
            try:
                await char.call_stop_notify()
            except (DBusError, OSError):
                pass
        # A hold still in progress when the link drops never sees a gap, so
        # close it here or its rate is never reported — and that is precisely
        # the sample an operator chasing a slow-mic report wants.
        stream.close_segment()
        log_event(
            logger,
            "wiim_remote_mic.disconnected",
            source=config.source_id,
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


async def run(config: MicAdapterConfig) -> None:
    """Stream the remote's mic for as long as this task lives.

    Reconnect is owned here rather than left to the supervisor: a WiiM link
    drops on every idle timeout, which is ordinary rather than a fault, and
    each reconnect must re-request the connection-event reservation.
    """
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

    while True:
        try:
            await _run_subscription(config)
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
        await asyncio.sleep(config.retry_sec)
