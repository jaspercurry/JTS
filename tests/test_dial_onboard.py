"""Tests for the Improv-over-Serial protocol bits in jasper.cli.dial_onboard.

The packet framing and SUBMIT_SETTINGS layout are easy to get wrong and
hard to debug from the dial side (the only feedback channel is the WS2812
status LED), so we lock the wire format down here.
"""
from __future__ import annotations

from jasper.cli.dial_onboard import (
    IMPROV_HEADER,
    IMPROV_PKT_CURRENT_STATE,
    IMPROV_PKT_RPC,
    IMPROV_STATE_PROVISIONED,
    _build_submit_settings,
    _improv_packet,
    _scan_packets,
)


def test_packet_starts_with_magic_header():
    pkt = _improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x04")
    assert pkt[: len(IMPROV_HEADER)] == IMPROV_HEADER


def test_packet_checksum_is_byte_sum_mod_256():
    pkt = _improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x04")
    body = pkt[:-1]
    assert pkt[-1] == sum(body) & 0xFF


def test_submit_settings_layout():
    # Per Improv spec: RPC packet, command=0x01,
    # data = ssid_len + ssid + pass_len + pass.
    pkt = _build_submit_settings("MyWifi", "secret123")
    # IMPROV<v>(7) + type(1) + outer_len(1) + cmd(1) + inner_len(1) + ssid_len(1) + ssid + pass_len(1) + pass + checksum(1)
    assert pkt[7] == IMPROV_PKT_RPC, "type byte should be RPC (0x03)"
    outer_len = pkt[8]
    expected_outer_len = 1 + 1 + 1 + len("MyWifi") + 1 + len("secret123")
    assert outer_len == expected_outer_len
    assert pkt[9] == 0x01, "command byte should be SUBMIT_SETTINGS (0x01)"
    inner_len = pkt[10]
    assert inner_len == 1 + len("MyWifi") + 1 + len("secret123")
    # ssid block
    assert pkt[11] == len("MyWifi")
    assert pkt[12 : 12 + len("MyWifi")] == b"MyWifi"
    # password block immediately after
    pwd_off = 12 + len("MyWifi")
    assert pkt[pwd_off] == len("secret123")
    assert pkt[pwd_off + 1 : pwd_off + 1 + len("secret123")] == b"secret123"


def test_scan_extracts_single_packet():
    pkt = _improv_packet(IMPROV_PKT_CURRENT_STATE, bytes([IMPROV_STATE_PROVISIONED]))
    buf = bytearray(pkt)
    found = _scan_packets(buf)
    assert len(found) == 1
    pkt_type, data = found[0]
    assert pkt_type == IMPROV_PKT_CURRENT_STATE
    assert data == bytes([IMPROV_STATE_PROVISIONED])
    assert len(buf) == 0


def test_scan_handles_partial_packet():
    pkt = _improv_packet(IMPROV_PKT_CURRENT_STATE, bytes([IMPROV_STATE_PROVISIONED]))
    # Feed first half — should yield nothing and preserve the partial
    # bytes for the next chunk.
    buf = bytearray(pkt[:7])
    assert _scan_packets(buf) == []
    # Feed the rest and we get the packet.
    buf.extend(pkt[7:])
    found = _scan_packets(buf)
    assert len(found) == 1
    assert found[0][1] == bytes([IMPROV_STATE_PROVISIONED])


def test_scan_skips_garbage_before_header():
    pkt = _improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x04")
    buf = bytearray(b"junk\x00\xff" + pkt)
    found = _scan_packets(buf)
    assert len(found) == 1
    assert found[0][1] == b"\x04"


def test_scan_drops_bad_checksum():
    pkt = bytearray(_improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x04"))
    pkt[-1] ^= 0xFF  # corrupt checksum
    buf = bytearray(pkt)
    found = _scan_packets(buf)
    assert found == []  # frame consumed but discarded


def test_scan_extracts_multiple_packets():
    p1 = _improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x05")
    p2 = _improv_packet(IMPROV_PKT_CURRENT_STATE, b"\x06")
    buf = bytearray(p1 + p2)
    found = _scan_packets(buf)
    assert len(found) == 2
    assert found[0][1] == b"\x05"
    assert found[1][1] == b"\x06"
