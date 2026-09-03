# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from dbus_next import Variant

from jasper.accessories import wiim_remote_mic
from jasper.accessories.constants import WIIM_REMOTE_2_MIC_UDP_PORT
from jasper.accessories.wiim_remote_mic import (
    BLUEZ_DEVICE_IFACE,
    BLUEZ_GATT_CHARACTERISTIC_IFACE,
    BLUEZ_GATT_DESCRIPTOR_IFACE,
    BLUEZ_OBJECT_MANAGER_IFACE,
    BLUEZ_PROPERTIES_IFACE,
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
from jasper.control import restart_broker


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


async def test_find_voice_characteristic_propagates_descriptor_read_error():
    managed, _chars, descs = _managed_voice_reports([None])
    bus = _FakeBus({descs[0]: OSError("BlueZ read failed")})

    with pytest.raises(OSError, match="BlueZ read failed"):
        await _find_voice_characteristic(
            bus,
            managed,
            name_regex="WiiM Remote 2",
        )


async def test_find_voice_characteristic_scans_after_match_and_propagates_error():
    managed, _chars, descs = _managed_voice_reports([WIIM_VOICE_REPORT_REFERENCE, None])
    bus = _FakeBus({descs[1]: OSError("later BlueZ read failed")})

    with pytest.raises(OSError, match="later BlueZ read failed"):
        await _find_voice_characteristic(
            bus,
            managed,
            name_regex="WiiM Remote 2",
        )


# ---------------------------------------------------------------------------
# Per-segment delivery rate — the starved-link instrument.
# ---------------------------------------------------------------------------


def _events(caplog, name: str) -> list[dict[str, str]]:
    fields = []
    for record in caplog.records:
        message = record.getMessage()
        if message != f"event={name}" and not message.startswith(f"event={name} "):
            continue
        parsed = {}
        for token in message.split()[1:]:
            key, _, value = token.partition("=")
            parsed[key] = value
        fields.append(parsed)
    return fields


def _feed_hold(stream, *, packets: int, spacing: float, start: float = 0.0) -> None:
    for seq in range(packets):
        stream.feed_notification(_packet(seq), now=start + seq * spacing)


def test_segment_reports_the_healthy_delivery_rate_at_a_hold_boundary(caplog):
    """A released button ends a segment, and the segment reports its rate.

    63 packets at the realtime 16 ms cadence is a ~1 s push-to-talk hold; the
    rate is over the 62 observed gaps, so it lands exactly on 62.5/s.
    """
    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    stream = WiimVoicePacketStream()
    _feed_hold(stream, packets=63, spacing=0.016)
    # Button released, then pressed again well beyond the gap threshold.
    stream.feed_notification(_packet(0), now=63 * 0.016 + 5.0)

    segments = _events(caplog, "wiim_remote_mic.segment")
    assert len(segments) == 1
    assert segments[0]["packets"] == "63"
    assert segments[0]["duration_ms"] == "992"
    assert segments[0]["rate_hz"] == "62.5"


def test_segment_exposes_the_starved_rate_the_gap_detector_cannot_see(caplog):
    """Why this event has to exist at all.

    At the starved cadence — 6 Link Layer PDUs x 11.25 ms = 67.5 ms between
    notifications — the inter-packet gap sits 3.7x under `WIIM_STREAM_GAP_SEC`,
    so `stream_reset` *structurally cannot* fire. Packets still decode, so
    `bad_packets` stays 0, and the cumulative `packets` count logged at
    disconnect has no denominator. The per-segment rate is the only instrument
    on the audio path that tells a starved link from a healthy one.
    """
    starved_spacing = 6 * 0.01125
    assert starved_spacing < WIIM_STREAM_GAP_SEC, (
        "premise of this test: the starved cadence never trips the gap detector"
    )

    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    stream = WiimVoicePacketStream()
    _feed_hold(stream, packets=63, spacing=starved_spacing)
    stream.close_segment()

    assert stream.bad_packets == 0
    assert _events(caplog, "wiim_remote_mic.stream_reset") == []
    segments = _events(caplog, "wiim_remote_mic.segment")
    assert len(segments) == 1
    assert segments[0]["rate_hz"] == "14.8"
    # The same hold length reports the same packet count either way, so the
    # count alone cannot separate them — only the rate can.
    assert segments[0]["packets"] == "63"


def test_close_segment_reports_a_hold_still_running_at_disconnect(caplog):
    """A hold in progress when the link drops never sees a gap, so without an
    explicit close its rate — the sample an operator chasing a slow mic most
    wants — would be the one that is never reported."""
    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    stream = WiimVoicePacketStream()
    _feed_hold(stream, packets=10, spacing=0.016)

    assert _events(caplog, "wiim_remote_mic.segment") == []
    stream.close_segment()
    assert len(_events(caplog, "wiim_remote_mic.segment")) == 1


def test_close_segment_does_not_invent_a_rate_for_a_single_packet(caplog):
    """One packet spans no gaps, so it has no rate. Report nothing rather
    than divide by a zero span."""
    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    stream = WiimVoicePacketStream()
    stream.feed_notification(_packet(0), now=1.0)
    stream.close_segment()

    assert _events(caplog, "wiim_remote_mic.segment") == []


def test_close_segment_is_idempotent_and_does_not_repeat_a_hold(caplog):
    """Closing twice must not double-report; the second close has no segment."""
    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    stream = WiimVoicePacketStream()
    _feed_hold(stream, packets=10, spacing=0.016)
    stream.close_segment()
    stream.close_segment()

    assert len(_events(caplog, "wiim_remote_mic.segment")) == 1


# ---------------------------------------------------------------------------
# BLE connection-event reservation request (see jasper/cli/wiim_remote_ce.py).
#
# The adapter asks a root helper to reserve connection-event length on the
# live link every time notifications start. Without it BlueZ's hardcoded
# min/max CE of 0 leaves the remote's mic at ~24 % of realtime on a Pi Zero
# 2 W (measured on jts4: 196/190 packets vs 794/805 with the reservation).
# ---------------------------------------------------------------------------


async def test_ce_reservation_request_uses_the_start_only_helper(monkeypatch):
    """Exact broker call arguments. `start` is the only verb the helper is in
    the broker allowlist for, and the unit name has to match it verbatim."""
    calls = []

    def fake_manage_units(*units, **kwargs):
        calls.append((units, kwargs))
        return {"ok": True, "action": "start", "rc": 0}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)
    await wiim_remote_mic._request_ce_reservation()

    assert calls == [(
        ("jasper-wiim-remote-ce.service",),
        {
            "verb": "start",
            "reason": "wiim-remote-mic-notify-started",
            "no_block": True,
            "timeout": 5.0,
        },
    )]


async def test_ce_reservation_request_does_not_block_the_event_loop(monkeypatch):
    """The 3c trap. `manage_units` is a blocking socket call and this loop is
    also delivering the mic notifications; running it inline stalls the loop
    and drops exactly the packets the reservation exists to save. If someone
    replaces the thread offload with a direct call, the ticker below never
    runs and the fake broker's wait times out."""
    released = threading.Event()
    ticks = 0

    def fake_manage_units(*_units, **_kwargs):
        assert released.wait(timeout=5.0), (
            "the event loop did not run while the broker call was blocking — "
            "_request_ce_reservation must offload it to a thread"
        )
        return {"ok": True}

    async def ticker():
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0)
            ticks += 1
        released.set()

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)
    task = asyncio.ensure_future(ticker())
    await wiim_remote_mic._request_ce_reservation()
    await task
    assert ticks == 3


async def test_ce_reservation_request_is_fail_soft_on_a_broker_error(monkeypatch):
    """Degraded audio beats no adapter: a broker that answers `ok: false` is
    logged and the subscription carries on."""
    monkeypatch.setattr(
        restart_broker, "manage_units",
        lambda *a, **k: {"ok": False, "error": "unit(s) not in allowlist"},
    )
    await wiim_remote_mic._request_ce_reservation()


async def test_ce_reservation_request_is_fail_soft_when_the_broker_raises(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("broker socket vanished")

    monkeypatch.setattr(restart_broker, "manage_units", boom)
    await wiim_remote_mic._request_ce_reservation()


# ---------------------------------------------------------------------------
# `_run_subscription` — the wiring that makes the reservation actually happen.
#
# The four tests above cover `_request_ce_reservation` itself, and the CE
# constants, HCI bytes, unit hardening, polkit rule and broker allowlist are
# each pinned elsewhere. None of that pins the single `await` that causes any
# of it to run. The fakes below exist so it can be.
# ---------------------------------------------------------------------------


class _FakeSink:
    def __init__(self, host: str, port: int) -> None:
        self.addr = (host, port)
        self.frames: list[bytes] = []
        self.closed = False

    def send(self, frame: bytes) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


class _FakeVoiceCharacteristic:
    def __init__(self, journal: list[str]) -> None:
        self._journal = journal

    async def call_start_notify(self) -> None:
        self._journal.append("start_notify")

    async def call_stop_notify(self) -> None:
        self._journal.append("stop_notify")


class _FakeObjectManager:
    def __init__(self, managed: dict) -> None:
        self._managed = managed

    async def call_get_managed_objects(self) -> dict:
        return self._managed


class _FakePropertiesInterface:
    def __init__(self, bus: _SubscriptionBus, path: str) -> None:
        self._bus = bus
        self._path = path

    def on_properties_changed(self, handler) -> None:
        self._bus.handlers[self._path] = handler


class _SubscriptionProxy:
    def __init__(self, bus: _SubscriptionBus, path: str) -> None:
        self._bus = bus
        self._path = path

    def get_interface(self, interface: str):
        if interface == BLUEZ_OBJECT_MANAGER_IFACE:
            return _FakeObjectManager(self._bus.managed)
        if interface == BLUEZ_GATT_CHARACTERISTIC_IFACE:
            return _FakeVoiceCharacteristic(self._bus.journal)
        if interface == BLUEZ_PROPERTIES_IFACE:
            return _FakePropertiesInterface(self._bus, self._path)
        raise AssertionError(f"unexpected interface {interface}")


class _SubscriptionBus:
    """A BlueZ bus fake complete enough to drive `_run_subscription`.

    `_FakeBus` above is a descriptor-read fake for `_find_voice_characteristic`
    and deliberately hard-asserts that one interface; the subscription path
    needs the ObjectManager, the characteristic, and two Properties interfaces,
    so it gets its own object graph rather than loosening that assertion.
    """

    def __init__(self, managed: dict, char_path: str, device_path: str) -> None:
        self.managed = managed
        self.char_path = char_path
        self.device_path = device_path
        self.journal: list[str] = []
        self.handlers: dict = {}
        self._gone: asyncio.Future = asyncio.get_running_loop().create_future()

    async def wait_for_disconnect(self) -> None:
        await self._gone

    async def introspect(self, service: str, path: str) -> str:
        assert service == "org.bluez"
        return path

    def get_proxy_object(
        self, service: str, path: str, intro: str
    ) -> _SubscriptionProxy:
        assert service == "org.bluez"
        assert intro == path
        return _SubscriptionProxy(self, path)

    def disconnect(self) -> None:
        self.journal.append("bus_disconnect")

    def push_voice_packet(self, payload: bytes) -> None:
        self.handlers[self.char_path](
            BLUEZ_GATT_CHARACTERISTIC_IFACE,
            {"Value": Variant("ay", payload)},
            [],
        )

    def push_disconnect(self) -> None:
        self.handlers[self.device_path](
            BLUEZ_DEVICE_IFACE,
            {"Connected": Variant("b", False)},
            [],
        )

    def push_bus_loss(self) -> None:
        if not self._gone.done():
            self._gone.set_result(None)


async def _wait_until(predicate, *, what: str, timeout: float = 5.0) -> None:
    """Bounded wait — never spin forever if the subscription wedges."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.001)


def _subscription_harness(monkeypatch) -> tuple[_SubscriptionBus, _FakeSink]:
    managed, chars, _descs = _managed_voice_reports([WIIM_VOICE_REPORT_REFERENCE])
    device = next(
        path for path, ifaces in managed.items() if BLUEZ_DEVICE_IFACE in ifaces
    )
    bus = _SubscriptionBus(managed, chars[0], device)
    sink = _FakeSink("127.0.0.1", DEFAULT_UDP_PORT)

    async def fake_connect():
        return bus

    monkeypatch.setattr(wiim_remote_mic, "_connect_bluez", fake_connect)
    monkeypatch.setattr(wiim_remote_mic, "UdpPcmSink", lambda host, port: sink)
    return bus, sink


async def test_run_subscription_requests_the_ce_reservation_after_notify_starts(
    monkeypatch,
):
    """The wiring guard for the whole feature.

    Everything *around* the reservation is pinned hard, but the one line that
    makes any of it run — `await _request_ce_reservation()` in
    `_run_subscription` — was pinned by nothing: deleting it left the complete
    candidate test set (53 tests) green while the mic silently returned to
    ~24 % of realtime on a Pi Zero 2 W, which is the exact bug this feature
    exists to fix.

    Ordering is load-bearing, not decoration. The reservation acts on a live
    link, so it has to follow `call_start_notify()`; asking before there is a
    notifying connection reserves event time for nothing.
    """
    bus, sink = _subscription_harness(monkeypatch)

    def fake_manage_units(*units, **kwargs):
        bus.journal.append("ce_request")
        assert units == (wiim_remote_mic.CE_HELPER_UNIT,)
        assert kwargs["verb"] == "start"
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)

    async def drive():
        await _wait_until(
            lambda: "ce_request" in bus.journal,
            what="the CE reservation request",
        )
        for seq in range(5):
            bus.push_voice_packet(_packet(seq))
        bus.push_disconnect()

    await asyncio.wait_for(
        asyncio.gather(
            wiim_remote_mic._run_subscription(
                wiim_remote_mic.MicAdapterConfig()
            ),
            drive(),
        ),
        timeout=20,
    )

    assert bus.journal == [
        "start_notify",
        "ce_request",
        "stop_notify",
        "bus_disconnect",
    ]
    # Not just the ordering: the notification path really is live, so this
    # would also catch a subscription that requested the reservation but
    # never wired the decoder to the sink.
    assert len(sink.frames) == 1
    assert sink.closed


async def test_run_subscription_reports_the_final_hold_when_the_link_drops(
    monkeypatch, caplog
):
    """The teardown wiring for the segment rate — pinned separately from the
    method, because they are two different promises and only one of them was
    guarded.

    `close_segment` on a bare stream is covered above. This pins the *call* in
    `_run_subscription`'s teardown, which is the only path that reports a hold
    still running when the connection ends — a dying remote battery, a user
    walking out of range, or simply the last hold before disconnect. Nothing
    else reaches it: no >250 ms gap ever arrives to trigger `reset()`, so
    without this call that hold is silently never reported, and the failure is
    invisible by construction — which is the whole reason the rate line exists.

    Deliberately asserts the packet count and not `rate_hz`: these packets
    arrive at real `time.monotonic()` spacing, so the rate here is meaningless.
    The rate arithmetic is pinned deterministically by the bare-stream tests
    (62.5/s healthy, 14.8/s starved); this test's job is the wiring.
    """
    caplog.set_level(logging.DEBUG, logger="jasper.accessories.wiim_remote_mic")
    bus, _sink = _subscription_harness(monkeypatch)

    def fake_manage_units(*_units, **_kwargs):
        bus.journal.append("ce_request")
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage_units)

    async def drive():
        await _wait_until(
            lambda: "ce_request" in bus.journal,
            what="the CE reservation request",
        )
        # A hold in progress: no gap, so `reset()` never fires and the only
        # thing that can report this hold is the teardown.
        for seq in range(5):
            bus.push_voice_packet(_packet(seq))
        bus.push_disconnect()

    await asyncio.wait_for(
        asyncio.gather(
            wiim_remote_mic._run_subscription(
                wiim_remote_mic.MicAdapterConfig()
            ),
            drive(),
        ),
        timeout=20,
    )

    assert _events(caplog, "wiim_remote_mic.stream_reset") == [], (
        "premise: no gap fires during a hold, so teardown is the only reporter"
    )
    segments = _events(caplog, "wiim_remote_mic.segment")
    assert len(segments) == 1, (
        "the hold running when the link dropped was never reported — "
        "`_run_subscription` must close the segment in its teardown"
    )
    assert segments[0]["packets"] == "5"


async def test_run_subscription_survives_a_broker_that_refuses_the_reservation(
    monkeypatch,
):
    """Degraded audio beats a dead adapter, end to end rather than in
    isolation: a broker that rejects the request must not stop the mic from
    streaming or the subscription from shutting down cleanly."""
    bus, sink = _subscription_harness(monkeypatch)

    def refuse(*_units, **_kwargs):
        bus.journal.append("ce_request")
        return {"ok": False, "error": "unit(s) not in allowlist"}

    monkeypatch.setattr(restart_broker, "manage_units", refuse)

    async def drive():
        await _wait_until(
            lambda: "ce_request" in bus.journal,
            what="the CE reservation request",
        )
        for seq in range(5):
            bus.push_voice_packet(_packet(seq))
        bus.push_disconnect()

    await asyncio.wait_for(
        asyncio.gather(
            wiim_remote_mic._run_subscription(
                wiim_remote_mic.MicAdapterConfig()
            ),
            drive(),
        ),
        timeout=20,
    )

    assert len(sink.frames) == 1
    assert sink.closed


async def test_a_dead_bus_ends_the_subscription_rather_than_wedging(monkeypatch):
    """bluetoothd restarting takes the bus with it, and no Connected property
    change can arrive over a bus that is gone. Waiting on the device alone
    would leave this task parked forever with nothing raised — invisible to the
    supervisor, which sees a task that is still running."""
    bus, sink = _subscription_harness(monkeypatch)
    monkeypatch.setattr(restart_broker, "manage_units", lambda *a, **k: {"ok": True})

    async def drive():
        await _wait_until(
            lambda: "start_notify" in bus.journal, what="the notify subscription",
        )
        bus.push_bus_loss()

    await asyncio.wait_for(
        asyncio.gather(
            wiim_remote_mic._run_subscription(wiim_remote_mic.MicAdapterConfig()),
            drive(),
        ),
        timeout=5,
    )

    assert sink.closed
