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


def _packet(seq: int, *, predictor: int = 0, index: int = 0) -> bytes:
    """A well-formed voice report: IMA block header + 128 ADPCM bytes.

    The header is a BIG-endian int16 predictor then the step index — see the
    hardware-pinned contract in test_framing_header_is_big_endian_ima_state.
    """
    adpcm = bytes(((seq + i) & 0xFF) for i in range(WIIM_VOICE_PACKET_BYTES - 3))
    return predictor.to_bytes(2, "big", signed=True) + bytes((index,)) + adpcm


# Six consecutive reports captured off a WiiM Remote 2 on jts3 (issue #2198),
# starting at a button press — the remote restarts its encoder at predictor 0 /
# index 0 on each press, which is why the first header is all zeroes.
_HW_PACKETS = tuple(bytes.fromhex(h) for h in (
    (
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000000000000000000000000ffffffff0f0808800008800080"
        "0000000000000101111121212242223343324323253233532343334333435331"
        "812711"
    ),
    (
        "f0db1b2222522815212122334324323244312432433243038642303123242133"
        "3235151363102231321443302117302310333124324510445212332543120231"
        "150c8103430132700420b1962a15190148b920968100908e71088080d1080388"
        "2979ab3182811212109bb1461ac348a001b8310a1901012a29c3283101038d06"
        "2180aa"
    ),
    (
        "ffa50293500a24518039c3a4897290c348b4893a49f31028b13a00ff92089891"
        "18791c86889091802089000a9932e018a2801e018138f9ba811f1000b931bb96"
        "401a12c19128c4bb490ca9a017aad0380b15b9ba9adcb9b049d91a1aa1bc439a"
        "fa020998b85139ba25019da9962aa982ace239b891ff8889a1091978a380b2f4"
        "1b2309"
    ),
    (
        "fee30c1a50fb10929100818f319928b0f389852909488d20a392b3318b9a007a"
        "89c100a0bba0ea92309c013051808391d310419b1331ea93419d10901c011924"
        "f42f19898010a0a019bb9592091aa5cf80918880097b8781a1d30b242a2a1aea"
        "10a5010908af50908008f109121809199f38a0a114aa0198191131bb99b0a010"
        "a37baa"
    ),
    (
        "fe650612aa81b3b45c8932023aa0b3be321abaf188809b9f93982cbb2349e431"
        "095ba0810993a04b83ff89008008007283118ba021229ca58993701922caa010"
        "09730202289c34959119154000a990099d3132cba9b9211a90b0f11b01394b5e"
        "be860118000899100081901910b31f01ba98912039cc87ab3889951908b41932"
        "3a1197"
    ),
    (
        "fea407bfa09101009c790290a2e40b322b3b40f00a958801118f30991201f20a"
        "0508182aac38b281258b0990c9a940210eaa05902990c1192207392811e14029"
        "b233ca852bcb230171c8a012991293510113b0594089a95315b220134aff8801"
        "89819171808192d21d3388295bf928a4019002af32190098f4081010002b9f21"
        "f11128"
    ),
))


def _pcm_samples(frames: list[bytes]) -> list[int]:
    joined = b"".join(frames)
    return [
        int.from_bytes(joined[i:i + 2], "little", signed=True)
        for i in range(0, len(joined), 2)
    ]


def test_default_udp_port_matches_profile_constant():
    assert DEFAULT_UDP_PORT == WIIM_REMOTE_2_MIC_UDP_PORT


def test_wiim_packet_stream_keeps_every_packet_and_batches_80ms_frame():
    stream = WiimVoicePacketStream()
    emitted = []

    # Exactly 5 packets * 256 samples == one 1280-sample UDP frame. Nothing is
    # discarded at stream start: the old 2-packet startup drop threw away the
    # only packets whose header aligns the decoder with the encoder, and left
    # the rest of the session decoding from a predictor that never recovers
    # (issue #2198).
    for idx in range(5):
        emitted.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))

    assert len(emitted) == 1
    assert len(emitted[0]) == MANUAL_MIC_FRAME_BYTES
    assert stream.packets == 5
    assert stream.frames == 1


def test_wiim_packet_stream_gap_clears_partial_frame_and_counts_reset():
    stream = WiimVoicePacketStream()
    emitted = []
    for idx in range(7):
        emitted.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))
    assert len(emitted) == 1  # 7 * 256 = 1792 -> one frame, 512 samples held

    # A gap past the threshold drops the partial frame rather than splicing it
    # onto audio from the far side of a silence.
    after_gap = 7 * 0.016 + WIIM_STREAM_GAP_SEC + 0.010
    assert stream.feed_notification(_packet(10), now=after_gap) == []
    assert stream.resets == 1

    # The held 512 samples were discarded, so a full 5 packets are needed again.
    for step, idx in enumerate((11, 12, 13), start=1):
        assert stream.feed_notification(
            _packet(idx), now=after_gap + step * 0.016,
        ) == []
    assert stream.frames == 1
    assert stream.feed_notification(_packet(14), now=after_gap + 4 * 0.016)
    assert stream.frames == 2


def test_wiim_packet_stream_rejects_unexpected_report_lengths():
    stream = WiimVoicePacketStream()

    assert stream.feed_notification(b"\x00" * 130, now=0.0) == []
    assert stream.bad_packets == 1


def test_adpcm_decode_packet_shape_is_16khz_16ms_chunk():
    stream = WiimVoicePacketStream()
    # One decoded WiiM notification is 256 samples, still below the 1280-sample
    # UDP frame threshold. The private byte buffer length is pinned indirectly
    # by feeding five packets and expecting exactly one frame.
    out = []
    for idx in range(4):
        assert stream.feed_notification(_packet(idx), now=idx * 0.016) == []
    for idx in range(4, 5):
        out.extend(stream.feed_notification(_packet(idx), now=idx * 0.016))
    assert len(out) == 1
    assert len(out[0]) == WIIM_VOICE_PACKET_SAMPLES * 5 * 2


def test_framing_header_is_big_endian_ima_state():
    """The 3 framing bytes are the encoder's own predictor + step index.

    Pinned against hardware: for consecutive real reports, the next packet's
    header must equal the state the decoder holds after the current one. The
    byte order is load-bearing — little-endian is the WAV IMA convention and
    is wrong for this device, so both directions are asserted.
    """
    from jasper.accessories.wiim_remote_mic import ImaAdpcmDecoder

    decoder = ImaAdpcmDecoder()
    matched_be = matched_le = 0
    for current, following in zip(_HW_PACKETS, _HW_PACKETS[1:]):
        tail = decoder.decode(current[3:])[-1]
        if int.from_bytes(following[0:2], "big", signed=True) == tail:
            matched_be += 1
        if int.from_bytes(following[0:2], "little", signed=True) == tail:
            matched_le += 1
        assert following[2] == decoder.index

    assert matched_be == len(_HW_PACKETS) - 1
    assert matched_le == 0


def test_resync_changes_nothing_while_the_stream_is_intact():
    """Adopting the header state is a no-op on an unbroken stream.

    This is what makes the fix safe: it only ever acts after a real break.
    """
    from jasper.accessories.wiim_remote_mic import ImaAdpcmDecoder

    stream = WiimVoicePacketStream()
    got: list[bytes] = []
    for idx, packet in enumerate(_HW_PACKETS):
        got.extend(stream.feed_notification(packet, now=idx * 0.016))
    got.append(bytes(stream._pcm))

    decoder = ImaAdpcmDecoder()
    expected: list[int] = []
    for packet in _HW_PACKETS:
        expected.extend(decoder.decode(packet[3:]))

    assert _pcm_samples(got) == expected


def test_lost_packet_costs_only_that_packet():
    """A dropped report must not corrupt every report after it.

    IMA ADPCM is a pure integrator with no leakage, so before the fix a single
    loss offset the predictor for the rest of the session. With the header
    adopted per packet, audio either side of the loss decodes bit-exactly.
    """
    from jasper.accessories.wiim_remote_mic import ImaAdpcmDecoder

    def reference(packet: bytes) -> list[int]:
        decoder = ImaAdpcmDecoder()
        decoder.resync(int.from_bytes(packet[0:2], "big", signed=True), packet[2])
        return decoder.decode(packet[3:])

    survivors = [p for i, p in enumerate(_HW_PACKETS) if i != 2]
    stream = WiimVoicePacketStream()
    got: list[bytes] = []
    for idx, packet in enumerate(survivors):
        got.extend(stream.feed_notification(packet, now=idx * 0.016))
    got.append(bytes(stream._pcm))

    expected: list[int] = []
    for packet in survivors:
        expected.extend(reference(packet))
    assert _pcm_samples(got) == expected


def test_first_packet_after_a_gap_decodes_from_its_own_header():
    """A gap needs no decoder reset — the next header supplies the state.

    This is the invariant that lets ``WiimVoicePacketStream.reset`` leave the
    decoder alone: whatever the previous burst left behind is overwritten
    before anything is decoded from the next packet.
    """
    from jasper.accessories.wiim_remote_mic import ImaAdpcmDecoder

    # The packet after the gap must NOT be the natural continuation of the
    # burst before it — consecutive packets share state by construction, so
    # they cannot tell "adopted the header" from "carried state across".
    stream = WiimVoicePacketStream()
    for idx in range(2):
        stream.feed_notification(_HW_PACKETS[idx], now=idx * 0.016)

    after_gap = 2 * 0.016 + WIIM_STREAM_GAP_SEC + 0.010
    assert stream.feed_notification(_HW_PACKETS[4], now=after_gap) == []
    assert stream.resets == 1

    expected = ImaAdpcmDecoder()
    expected.resync(
        int.from_bytes(_HW_PACKETS[4][0:2], "big", signed=True),
        _HW_PACKETS[4][2],
    )
    assert _pcm_samples([bytes(stream._pcm)]) == expected.decode(_HW_PACKETS[4][3:])


def test_resync_clamps_a_corrupt_step_index():
    """A malformed header must not index past the step table and crash."""
    from jasper.accessories.wiim_remote_mic import ImaAdpcmDecoder

    decoder = ImaAdpcmDecoder()
    decoder.resync(999999, 255)
    assert decoder.predictor == 32767
    assert decoder.index == 88

    stream = WiimVoicePacketStream()
    # 0xFF is not a valid step index; decoding must still produce a full packet.
    assert stream.feed_notification(_packet(0, index=255), now=0.0) == []
    assert stream.packets == 1
    assert stream.bad_packets == 0


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
