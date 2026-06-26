# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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
    WiimVoicePacketStream,
    voice_characteristic_candidates,
)


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
