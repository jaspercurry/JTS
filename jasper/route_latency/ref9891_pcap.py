# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Convert a tcpdump pcap of the outputd ``:9891`` reference stream into the
route-latency harness's mic-detections JSONL (the electrical plane).

Wire truth (``rust/jasper-outputd/src/main.rs``): headerless LE interleaved
stereo int16 @ 48 kHz, one DAC period per datagram. The PERIOD is a per-box
declaration, not a constant — this converter derives it from each datagram's
length rather than assuming one, and reports every datagram that is not a whole
number of frames instead of dropping it silently (#3509).

Pcap timestamps are ``CLOCK_REALTIME``; the harness pairs on
``CLOCK_MONOTONIC``, so a single realtime-monotonic offset sampled on this
host re-anchors them. Packet-granular peaks add a bounded bias of one period.

Exposed as the harness's ``convert-pcap`` subcommand
(:mod:`jasper.cli.route_latency_harness`).
"""
from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# The tap is S16LE stereo BY CONTRACT (outputd's `i16_bytes` guard and the
# chip-reference geometry validator both pin it), so a datagram's frame count
# is its length divided by this and nothing has to be declared twice.
BYTES_PER_FRAME = 2 * 2
RATE = 48000
DEFAULT_THRESHOLD = 0.006
REFRACTORY_S = 0.250

# Why a conversion is worthless, when it is. `ConversionResult.refusal` carries
# one of these or None, and the CLI exits nonzero on any of them.
REFUSAL_NO_PACKETS = "no_packets"
REFUSAL_ALL_ZERO = "all_zero"

_REF_UDP_PORT = 9891


def payloads(path: Path) -> Iterator[tuple[float, bytes]]:
    """Yield ``(realtime_seconds, payload_bytes)`` for each ``:9891`` datagram in a pcap.

    Parses the global pcap header to detect byte order and timestamp
    resolution (legacy microsecond or nanosecond pcapng-style), then walks
    packet records, defensively parsing Ethernet(14) + IPv4(IHL) + UDP(8).

    Every datagram on the port is yielded WHATEVER its length: a payload that
    is not a whole number of frames is the fact worth reporting, not one to
    hide, so the caller classifies. Anything else (wrong port, a record too
    short to hold the headers) is skipped, mirroring tcpdump's own port filter
    at capture time.

    A record pcap itself marks as TRUNCATED (captured length below the wire
    length — ``tcpdump -s`` short of a period) is skipped rather than yielded:
    it is a prefix of a datagram, not one, and its peak would be computed over
    only the bytes that survived. A capture snaplen'd throughout therefore
    yields nothing and is refused as ``no_packets`` instead of converting
    clean off partial periods (#3509).
    """

    with open(path, "rb") as f:
        ghdr = f.read(24)
        if len(ghdr) < 24:
            raise ValueError("not a pcap: short global header")
        magic = struct.unpack("<I", ghdr[:4])[0]
        if magic == 0xA1B2C3D4:
            endian, ts_div = "<", 1_000_000
        elif magic == 0xA1B23C4D:
            endian, ts_div = "<", 1_000_000_000
        elif magic == 0xD4C3B2A1:
            endian, ts_div = ">", 1_000_000
        else:
            raise ValueError("unrecognized pcap magic: %08x" % magic)
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                return
            ts_s, ts_frac, incl, orig_len = struct.unpack(endian + "IIII", ph)
            data = f.read(incl)
            if len(data) < incl:
                return
            if incl < orig_len:
                continue
            # Ethernet(14) + IPv4(IHL) + UDP(8); parse IHL defensively.
            if incl < 14 + 20 + 8:
                continue
            ihl = (data[14] & 0x0F) * 4
            udp_off = 14 + ihl
            if incl < udp_off + 8:
                continue
            dst_port = struct.unpack(">H", data[udp_off + 2 : udp_off + 4])[0]
            if dst_port != _REF_UDP_PORT:
                continue
            yield ts_s + ts_frac / ts_div, data[udp_off + 8 :]


@dataclass(frozen=True)
class ConversionResult:
    """Summary of one pcap-to-detections conversion run."""

    n_packets: int
    n_detections: int
    threshold: float
    rt_minus_mono: float
    frames_per_packet: int | None
    n_unexpected_size: int
    refusal: str | None

    def summary_line(self) -> str:
        """The operator-facing one-line summary."""

        return (
            "packets=%d detections=%d threshold=%.4f rt_minus_mono=%.6f "
            "frames_per_packet=%s unexpected_size=%d refusal=%s"
            % (
                self.n_packets,
                self.n_detections,
                self.threshold,
                self.rt_minus_mono,
                self.frames_per_packet,
                self.n_unexpected_size,
                self.refusal,
            )
        )


def convert_pcap_to_detections(
    pcap_path: Path,
    out_path: Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> ConversionResult:
    """Convert a ``:9891`` pcap into a mic-detections JSONL at ``out_path``.

    One realtime-to-monotonic offset is sampled once (now, on this host) and
    applied to every packet's pcap-recorded realtime timestamp — valid
    because the two clocks' offset is constant on a running system (modulo an
    NTP step). A rising edge above ``threshold`` fires one detection, then the
    detector is refractory for ``REFRACTORY_S`` — mirrors
    :class:`jasper.route_latency.impulse_detect.StreamingDetector`'s
    peak+refractory shape (this converter has no hysteresis band; see its
    module docstring for why the two aren't unified).

    A capture carrying no usable datagram, or one carrying nothing but zeros,
    is REFUSED: the tap ran and the pipeline produced an empty detections
    file, which is indistinguishable from "the speaker played nothing" unless
    the result says so (#3509). The JSONL is still written — a refusal is a
    verdict on the capture, not an I/O failure.
    """

    rt_minus_mono = time.clock_gettime(time.CLOCK_REALTIME) - time.monotonic()
    n_pkts = n_det = n_unexpected = 0
    frames_per_packet: int | None = None
    last_det_mono: float | None = None
    above = False
    all_zero = True
    with open(out_path, "w") as fo:
        for rt_ts, payload in payloads(pcap_path):
            frames, remainder = divmod(len(payload), BYTES_PER_FRAME)
            if frames == 0 or remainder:
                n_unexpected += 1
                continue
            if frames_per_packet is None:
                frames_per_packet = frames
            elif frames != frames_per_packet:
                # Still decodable, so it is converted — but a period that
                # changes mid-capture means the datagram boundaries are not
                # what the peak's ±1-period bias assumes.
                n_unexpected += 1
            n_pkts += 1
            vals = struct.unpack("<%dh" % (frames * 2), payload)
            peak = max(abs(v) for v in vals) / 32768.0
            if peak > 0.0:
                all_zero = False
            mono = rt_ts - rt_minus_mono
            if peak >= threshold:
                if not above and (
                    last_det_mono is None or mono - last_det_mono >= REFRACTORY_S
                ):
                    fo.write(
                        json.dumps({"monotonic_ns": int(mono * 1e9), "peak": peak})
                        + "\n"
                    )
                    n_det += 1
                    last_det_mono = mono
                above = True
            else:
                above = False
    if n_pkts == 0:
        refusal: str | None = REFUSAL_NO_PACKETS
    elif all_zero:
        refusal = REFUSAL_ALL_ZERO
    else:
        refusal = None
    return ConversionResult(
        n_packets=n_pkts,
        n_detections=n_det,
        threshold=threshold,
        rt_minus_mono=rt_minus_mono,
        frames_per_packet=frames_per_packet,
        n_unexpected_size=n_unexpected,
        refusal=refusal,
    )


__all__ = [
    "BYTES_PER_FRAME",
    "DEFAULT_THRESHOLD",
    "RATE",
    "REFRACTORY_S",
    "REFUSAL_ALL_ZERO",
    "REFUSAL_NO_PACKETS",
    "ConversionResult",
    "convert_pcap_to_detections",
    "payloads",
]
