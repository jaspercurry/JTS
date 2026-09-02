# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round-trip pin for the `:9891` pcap-to-detections converter.

Synthesizes a tiny pcap (global header + Ethernet/IPv4/UDP packet records
carrying 512-byte S16LE stereo payloads) so the test exercises the real
byte-parsing path rather than a mocked one. The realtime<->monotonic offset
is mocked to a fixed value so the JSONL output is exactly predictable
instead of merely bounded.
"""
from __future__ import annotations

import json
import struct

import pytest

from jasper.route_latency import ref9891_pcap

_DST_PORT = 9891
_FRAMES_PER_PKT = 128


def _payload(peak_sample: int, *, frames: int = _FRAMES_PER_PKT) -> bytes:
    """One S16LE stereo payload of `frames` frames, loudest sample `peak_sample`."""

    samples = [0] * (frames * 2)
    samples[0] = peak_sample
    return struct.pack(f"<{frames * 2}h", *samples)


def _udp_packet(
    payload: bytes, *, dst_port: int = _DST_PORT, proto: int = 17, frag: int = 0
) -> bytes:
    eth = b"\x00" * 12 + b"\x08\x00"  # dst/src MAC (unchecked) + ethertype IPv4
    # version/IHL=5, then total length, id, flags+fragment offset, TTL and the
    # PROTOCOL byte -- 17 is what makes this a UDP datagram rather than merely
    # a packet whose bytes sit where UDP's would (#3509).
    ip = (
        bytes([0x45, 0x00])
        + struct.pack(">HHH", 20 + 8 + len(payload), 0, frag)
        + bytes([64, proto])
        + b"\x00" * 10  # checksum + src/dst addresses: 20-byte header total
    )
    udp = struct.pack(">HHHH", 55555, dst_port, 8 + len(payload), 0)
    return eth + ip + udp + payload


def _record(ts_s: int, ts_us: int, data: bytes) -> bytes:
    return struct.pack("<IIII", ts_s, ts_us, len(data), len(data)) + data


def _truncated_record(ts_s: int, ts_us: int, data: bytes, *, snaplen: int) -> bytes:
    """A record tcpdump captured under a snaplen: incl_len < orig_len."""

    return struct.pack("<IIII", ts_s, ts_us, snaplen, len(data)) + data[:snaplen]


def _write_pcap(path, records: list[bytes]) -> None:
    # Legacy microsecond-resolution global header (magic 0xA1B2C3D4).
    global_hdr = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    path.write_bytes(global_hdr + b"".join(records))


def test_convert_pcap_to_detections_round_trip(tmp_path, monkeypatch):
    quiet = _payload(10)  # peak ~0.0003 -- below the default 0.006 threshold
    loud = _payload(20000)  # peak 0.6103515625 -- above threshold

    base = 1_700_000_000
    records = [
        _record(base, 0, _udp_packet(quiet)),
        _record(base, 100_000, _udp_packet(loud)),  # rising edge -> detection #1 @ +0.100s
        _record(base, 150_000, _udp_packet(quiet)),  # falling edge
        _record(base, 200_000, _udp_packet(loud)),  # +0.100s since det #1 -> refractory-suppressed
        _record(base, 250_000, _udp_packet(quiet)),  # falling edge
        _record(base, 500_000, _udp_packet(loud)),  # +0.400s since det #1 -> detection #2 @ +0.500s
        _record(base, 500_000, _udp_packet(quiet, dst_port=9999)),  # wrong port -- ignored entirely
    ]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)
    out_path = tmp_path / "detections.jsonl"

    # Fix rt_minus_mono = REALTIME_NOW - MONOTONIC_NOW to exactly `base` so
    # every yielded packet's re-anchored monotonic timestamp is exact.
    monkeypatch.setattr(ref9891_pcap.time, "clock_gettime", lambda clk: float(base) + 1000.0)
    monkeypatch.setattr(ref9891_pcap.time, "monotonic", lambda: 1000.0)

    result = ref9891_pcap.convert_pcap_to_detections(
        pcap_path, out_path, period_bytes=_FRAMES_PER_PKT * ref9891_pcap.BYTES_PER_FRAME
    )

    assert result.n_packets == 6  # the wrong-port record never reaches the counter
    assert result.n_detections == 2
    assert result.threshold == ref9891_pcap.DEFAULT_THRESHOLD
    assert result.rt_minus_mono == float(base)
    assert result.frames_per_packet == _FRAMES_PER_PKT
    assert result.n_unexpected_size == 0
    assert result.refusal is None
    assert result.summary_line() == (
        "packets=6 detections=2 threshold=0.0060 rt_minus_mono=1700000000.000000 "
        "frames_per_packet=128 unexpected_size=0 refusal=None"
    )

    # rt_ts and rt_minus_mono are both epoch-scale (~1.7e9); float64's ~15-17
    # significant digits gives the subtraction to mono (~0.1-0.5s) at most
    # ~100ns of slop -- inherent to the algorithm (see the module docstring's
    # documented 0-2.67ms packet-granularity bias), not a porting bug. Bound
    # it instead of asserting exact equality.
    detections = [json.loads(line) for line in out_path.read_text().splitlines()]
    expected_monotonic_ns = [100_000_000, 500_000_000]
    assert len(detections) == len(expected_monotonic_ns)
    for detection, expected_ns in zip(detections, expected_monotonic_ns):
        assert abs(detection["monotonic_ns"] - expected_ns) < 1_000  # < 1 us
        assert detection["peak"] == 20000 / 32768.0


def test_payloads_skips_non_9891_and_short_records(tmp_path):
    """#3509: a snaplen-truncated record is a PREFIX of a datagram, not one.

    Every other length is now the caller's to classify, so this is the last
    length check left — without it `tcpdump -s` short of a period converts
    clean off partial periods, with a peak taken over only the surviving
    bytes and nothing in the result saying so.
    """
    loud = _payload(20000)
    full = _udp_packet(loud)
    records = [
        _record(1, 0, _udp_packet(loud, dst_port=9999)),  # wrong port
        _record(1, 0, b"\x00" * 10),  # shorter than Ethernet+IPv4+UDP headers
        _truncated_record(1, 0, full, snaplen=96),  # pcap-marked truncation
        _record(1, 0, full),  # the only packet on the port
    ]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)

    yielded = list(ref9891_pcap.payloads(pcap_path))

    assert len(yielded) == 1
    _ts, payload = yielded[0]
    assert len(payload) == _FRAMES_PER_PKT * ref9891_pcap.BYTES_PER_FRAME


def test_a_wider_period_is_read_as_frames_and_a_ragged_one_is_counted(tmp_path):
    """#3509: the datagram's LENGTH is the period, not a 512-byte constant.

    A box whose DAC runs a 1024-frame period publishes 4096-byte datagrams;
    a hard-coded 512 dropped every one of them and reported a silent capture.
    The period is declared by the caller, and anything that is not it is
    counted rather than dropped in silence.
    """
    wide = _payload(20000, frames=1024)
    records = [
        _record(1, 0, _udp_packet(wide)),
        _record(1, 100_000, _udp_packet(_payload(0, frames=1024))),
        _record(1, 200_000, _udp_packet(wide[:-1])),  # 4095 B: not whole frames
    ]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)

    result = ref9891_pcap.convert_pcap_to_detections(
        pcap_path, tmp_path / "detections.jsonl", period_bytes=len(wide)
    )

    assert result.frames_per_packet == 1024
    assert result.n_packets == 2
    assert result.n_detections == 1
    assert result.n_unexpected_size == 1
    assert result.refusal is None


def test_a_foreign_datagram_neither_converts_nor_fires_a_detection(tmp_path):
    """#3509: what reaches :9891 is not automatically the tap.

    An ICMP error quoting a loopback header has a plausible port and payload
    at the UDP offsets but is not a datagram at all; a 548 B one decodes as
    137 frames whose peak (0.0078) clears the default 0.006 threshold. Both
    the protocol check and the period check must hold, or the harness pairs
    a mic detection against an impulse that was never played.
    """
    period = _payload(20000)
    intruder = bytes([127, 1, 0, 1]) * 137  # 548 B, peak 0.0078 > threshold
    records = [
        _record(1, 0, _udp_packet(period)),
        _record(1, 100_000, _udp_packet(intruder)),
        _record(1, 200_000, _udp_packet(period, proto=1)),  # ICMP, not a datagram
        _record(1, 300_000, _udp_packet(period, frag=0x0100)),  # a fragment
    ]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)

    result = ref9891_pcap.convert_pcap_to_detections(
        pcap_path, tmp_path / "detections.jsonl", period_bytes=len(period)
    )

    # Only the real datagram converted, and only it could fire.
    assert result.n_packets == 1
    assert result.n_detections == 1
    # The non-datagrams never reached the size check at all; the wrong-length
    # datagram did, and is reported by length rather than silently dropped.
    assert result.unexpected_by_length == {len(intruder): 1}


def test_an_intruder_first_pcap_cannot_define_the_period(tmp_path):
    """#3509: learning the period from the capture is a vote the intruder
    can win, and arriving first is enough to win a 1-of-1.

    With the period declared, the intruder is the outlier it is: no
    detection fires off its bytes, and the twenty real periods behind it are
    still the tap rather than the wrong length.
    """
    period = _payload(20000)
    intruder = bytes([127, 1, 0, 1]) * 137  # 548 B, peak 0.0078 > threshold
    records = [_record(1, 0, _udp_packet(intruder))]
    records += [
        _record(1, 100_000 + i * 1000, _udp_packet(period)) for i in range(20)
    ]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)

    result = ref9891_pcap.convert_pcap_to_detections(
        pcap_path, tmp_path / "detections.jsonl", period_bytes=len(period)
    )

    assert result.n_packets == 20
    assert result.unexpected_by_length == {len(intruder): 1}
    # One rising edge across twenty identical loud periods, and it is the
    # tap's -- not the intruder's, which never reached the detector.
    assert result.n_detections == 1
    first = json.loads((tmp_path / "detections.jsonl").read_text().splitlines()[0])
    assert first["peak"] == 20000 / 32768.0


@pytest.mark.parametrize("bad", [0, -512, 513])
def test_a_nonsense_period_is_refused_rather_than_blamed_on_the_capture(tmp_path, bad):
    """`--period-bytes` is operator input. A value that is not a whole
    number of S16 stereo frames would reject every datagram and refuse as
    `no_packets`, which reads as a fault in the capture."""
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, [_record(1, 0, _udp_packet(_payload(20000)))])

    with pytest.raises(ValueError):
        ref9891_pcap.convert_pcap_to_detections(
            pcap_path, tmp_path / "detections.jsonl", period_bytes=bad
        )


def test_an_all_zero_capture_is_refused_with_its_own_code(tmp_path):
    """A 150 s capture came back all zeros while music played (#3509).

    Every downstream surface saw an empty detections file, which is what a
    genuinely quiet room produces too — so the converter's own result has to
    carry the difference.
    """
    silence = _payload(0)
    records = [_record(1, us, _udp_packet(silence)) for us in (0, 100_000)]
    pcap_path = tmp_path / "ref9891.pcap"
    _write_pcap(pcap_path, records)

    result = ref9891_pcap.convert_pcap_to_detections(
        pcap_path, tmp_path / "detections.jsonl", period_bytes=len(silence)
    )

    assert result.refusal == ref9891_pcap.REFUSAL_ALL_ZERO
    assert result.n_packets == 2

    empty = tmp_path / "empty.pcap"
    _write_pcap(empty, [])
    no_packets = ref9891_pcap.convert_pcap_to_detections(
        empty, tmp_path / "none.jsonl", period_bytes=_FRAMES_PER_PKT * ref9891_pcap.BYTES_PER_FRAME
    )
    assert no_packets.refusal == ref9891_pcap.REFUSAL_NO_PACKETS
