# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from dbus_next import Variant

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_UDP_PORT
from jasper.accessories.wiim_remote_mic import (
    BLUEZ_DEVICE_IFACE,
    BLUEZ_GATT_CHARACTERISTIC_IFACE,
    BLUEZ_GATT_DESCRIPTOR_IFACE,
    DEFAULT_UDP_PORT,
    HID_REPORT_UUID,
    MANUAL_MIC_FRAME_BYTES,
    REPORT_REFERENCE_UUID,
    WIIM_STREAM_GAP_SEC,
    WIIM_VOICE_PACKET_BYTES,
    WIIM_VOICE_PACKET_SAMPLES,
    WIIM_VOICE_REPORT_REFERENCE,
    DeviceNotReady,
    WiimVoicePacketStream,
    _find_voice_characteristic,
    voice_characteristic_candidates,
)


class _FakeDescriptor:
    def __init__(self, result: object) -> None:
        self._result = result

    async def call_read_value(self, options: dict) -> object:
        assert options == {}
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeProxy:
    def __init__(self, result: object) -> None:
        self._result = result

    def get_interface(self, interface: str) -> _FakeDescriptor:
        assert interface == BLUEZ_GATT_DESCRIPTOR_IFACE
        return _FakeDescriptor(self._result)


class _FakeBus:
    def __init__(self, results: dict[str, object] | None = None) -> None:
        self.results = results or {}
        self.introspected: list[str] = []

    async def introspect(self, service: str, path: str) -> str:
        assert service == "org.bluez"
        self.introspected.append(path)
        return path

    def get_proxy_object(self, service: str, path: str, intro: str) -> _FakeProxy:
        assert service == "org.bluez"
        assert intro == path
        return _FakeProxy(self.results[path])


def _managed_voice_reports(
    values: list[object | None],
    *,
    descriptorless: int = 0,
    separate_devices: bool = False,
) -> tuple[dict[str, dict[str, dict[str, object]]], list[str], list[str]]:
    device_prefix = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE"
    managed: dict[str, dict[str, dict[str, object]]] = {}

    def add_device(index: int) -> str:
        device = (
            f"{device_prefix}_{index:02X}"
            if separate_devices
            else f"{device_prefix}_FF"
        )
        managed.setdefault(
            device,
            {
                BLUEZ_DEVICE_IFACE: {
                    "Connected": True,
                    "Alias": "WiiM Remote 2",
                }
            },
        )
        return device

    chars: list[str] = []
    descs: list[str] = []
    for index, value in enumerate(values):
        device = add_device(index)
        char = f"{device}/service0020/char{index:04x}"
        desc = f"{char}/desc0001"
        chars.append(char)
        descs.append(desc)
        managed[char] = {
            BLUEZ_GATT_CHARACTERISTIC_IFACE: {
                "UUID": HID_REPORT_UUID,
                "Flags": ["notify"],
            }
        }
        descriptor_props: dict[str, object] = {"UUID": REPORT_REFERENCE_UUID}
        if value is not None:
            descriptor_props["Value"] = value
        managed[desc] = {BLUEZ_GATT_DESCRIPTOR_IFACE: descriptor_props}
    for offset in range(descriptorless):
        device = add_device(len(values) + offset)
        char = f"{device}/service0020/char{len(values) + offset:04x}"
        chars.append(char)
        managed[char] = {
            BLUEZ_GATT_CHARACTERISTIC_IFACE: {
                "UUID": HID_REPORT_UUID,
                "Flags": ["notify"],
            }
        }
    return managed, chars, descs


def _packet(seq: int) -> bytes:
    adpcm = bytes(((seq + i) & 0xFF) for i in range(WIIM_VOICE_PACKET_BYTES - 3))
    return bytes((0x21, 0x02, seq & 0xFF)) + adpcm


def test_default_udp_port_matches_profile_constant():
    assert DEFAULT_UDP_PORT == WIIM_REMOTE_2_MIC_UDP_PORT


def test_wiim_packet_stream_drops_startup_packets_and_batches_80ms_frame():
    stream = WiimVoicePacketStream()
    emitted = []

    for idx in range(7):
        emitted.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))

    assert len(emitted) == 1
    assert len(emitted[0]) == MANUAL_MIC_FRAME_BYTES
    # 2 startup packets are discarded, then 5 * 256 samples make one
    # jasper-voice UDP frame.
    assert stream.packets == 7
    assert stream.frames == 1


def test_wiim_packet_stream_gap_resets_decoder_and_startup_drop():
    stream = WiimVoicePacketStream()
    emitted = []
    for idx in range(7):
        emitted.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))
    assert len(emitted) == 1

    after_gap = 7 * 0.016 + WIIM_STREAM_GAP_SEC + 0.010
    assert stream.feed_notification(_packet(10), now=after_gap) == []
    assert stream.feed_notification(_packet(11), now=after_gap + 0.016) == []
    assert stream.resets == 1

    # Third packet after the gap is decoded but not enough for an 80 ms frame.
    assert stream.feed_notification(_packet(12), now=after_gap + 0.032) == []
    assert stream.frames == 1


def test_wiim_packet_stream_rejects_unexpected_report_lengths():
    stream = WiimVoicePacketStream()

    assert stream.feed_notification(b"\x00" * 130, now=0.0) == []
    assert stream.bad_packets == 1


def test_adpcm_decode_packet_shape_is_16khz_16ms_chunk():
    stream = WiimVoicePacketStream()
    # Skip the two startup packets.
    stream.feed_notification(_packet(0), now=0.0)
    stream.feed_notification(_packet(1), now=0.016)
    stream.feed_notification(_packet(2), now=0.032)

    # One decoded WiiM notification is 256 samples, still below the 1280-sample
    # UDP frame threshold. The private byte buffer length is pinned indirectly
    # by feeding four more packets and expecting exactly one frame.
    out = []
    for idx in range(3, 7):
        out.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))
    assert len(out) == 1
    assert len(out[0]) == WIIM_VOICE_PACKET_SAMPLES * 5 * 2


def test_voice_characteristic_candidates_match_connected_wiim_hid_report():
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    char = f"{device}/service0020/char0039"
    desc = f"{char}/desc003b"
    managed = {
        device: {
            BLUEZ_DEVICE_IFACE: {
                "Connected": True,
                "Alias": "WiiM Remote 2",
            }
        },
        char: {
            BLUEZ_GATT_CHARACTERISTIC_IFACE: {
                "UUID": HID_REPORT_UUID,
                "Flags": ["read", "notify"],
            }
        },
        desc: {
            BLUEZ_GATT_DESCRIPTOR_IFACE: {
                "UUID": REPORT_REFERENCE_UUID,
                "Value": list(WIIM_VOICE_REPORT_REFERENCE),
            }
        },
    }

    candidates = voice_characteristic_candidates(managed)

    assert len(candidates) == 1
    assert candidates[0].device_path == device
    assert candidates[0].characteristic_path == char
    assert candidates[0].descriptor_path == desc


def test_voice_characteristic_candidates_ignore_disconnected_wiim():
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    managed = {
        device: {
            BLUEZ_DEVICE_IFACE: {
                "Connected": False,
                "Alias": "WiiM Remote 2",
            }
        },
        f"{device}/service0020/char0039": {
            BLUEZ_GATT_CHARACTERISTIC_IFACE: {
                "UUID": HID_REPORT_UUID,
                "Flags": ["notify"],
            }
        },
    }

    assert voice_characteristic_candidates(managed) == []


@pytest.mark.asyncio
async def test_find_voice_characteristic_uses_only_cached_matching_report():
    managed, chars, _descs = _managed_voice_reports(
        [b"\x01\x01", Variant("ay", WIIM_VOICE_REPORT_REFERENCE)]
    )
    bus = _FakeBus()

    match = await _find_voice_characteristic(
        bus,
        managed,
        name_regex="WiiM Remote 2",
    )

    assert match.characteristic_path == chars[1]
    assert bus.introspected == []


@pytest.mark.asyncio
async def test_find_voice_characteristic_reads_missing_descriptor_value():
    managed, chars, descs = _managed_voice_reports([None])
    bus = _FakeBus({descs[0]: Variant("ay", WIIM_VOICE_REPORT_REFERENCE)})

    match = await _find_voice_characteristic(
        bus,
        managed,
        name_regex="WiiM Remote 2",
    )

    assert match.characteristic_path == chars[0]
    assert bus.introspected == [descs[0]]


@pytest.mark.asyncio
async def test_find_voice_characteristic_rejects_no_match_and_descriptorless():
    managed, _chars, _descs = _managed_voice_reports(
        [b"\x02\x01"],
        descriptorless=1,
    )

    with pytest.raises(DeviceNotReady, match="voice report not found"):
        await _find_voice_characteristic(
            _FakeBus(),
            managed,
            name_regex="WiiM Remote 2",
        )


@pytest.mark.asyncio
async def test_find_voice_characteristic_rejects_multiple_matches_with_guidance():
    managed, _chars, _descs = _managed_voice_reports(
        [WIIM_VOICE_REPORT_REFERENCE, WIIM_VOICE_REPORT_REFERENCE],
        separate_devices=True,
    )

    with pytest.raises(
        DeviceNotReady,
        match="leave exactly one remote connected and retry",
    ):
        await _find_voice_characteristic(
            _FakeBus(),
            managed,
            name_regex="WiiM Remote 2",
        )


@pytest.mark.asyncio
async def test_find_voice_characteristic_propagates_descriptor_read_error():
    managed, _chars, descs = _managed_voice_reports([None])
    bus = _FakeBus({descs[0]: OSError("BlueZ read failed")})

    with pytest.raises(OSError, match="BlueZ read failed"):
        await _find_voice_characteristic(
            bus,
            managed,
            name_regex="WiiM Remote 2",
        )


@pytest.mark.asyncio
async def test_find_voice_characteristic_scans_after_match_and_propagates_error():
    managed, _chars, descs = _managed_voice_reports([WIIM_VOICE_REPORT_REFERENCE, None])
    bus = _FakeBus({descs[1]: OSError("later BlueZ read failed")})

    with pytest.raises(OSError, match="later BlueZ read failed"):
        await _find_voice_characteristic(
            bus,
            managed,
            name_regex="WiiM Remote 2",
        )
