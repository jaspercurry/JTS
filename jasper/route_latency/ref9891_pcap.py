# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Convert a tcpdump pcap of the outputd ``:9891`` reference stream into the
route-latency harness's mic-detections JSONL (the electrical plane).

Wire truth (``rust/jasper-outputd/src/main.rs``): headerless LE interleaved
stereo int16 @ 48 kHz, one 128-frame period per datagram = 512 B payload.
Pcap timestamps are ``CLOCK_REALTIME``; the harness pairs on
``CLOCK_MONOTONIC``, so a single realtime-monotonic offset sampled on this
host re-anchors them. Packet-granular peaks add a bounded 0-2.67 ms bias (one
period).

Exposed as the harness's ``convert-pcap`` subcommand
(:mod:`jasper.cli.route_latency_harness`) — see
``docs/HANDOFF-usb-latency-measurement.md`` §3 for the end-to-end procedure.
"""
from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PAYLOAD_BYTES = 512
FRAMES_PER_PKT = 128
RATE = 48000
DEFAULT_THRESHOLD = 0.006
REFRACTORY_S = 0.250

_REF_UDP_PORT = 9891


def payloads(path: Path) -> Iterator[tuple[float, bytes]]:
    """Yield ``(realtime_seconds, payload_bytes)`` for each ``:9891`` datagram in a pcap.

    Parses the global pcap header to detect byte order and timestamp
    resolution (legacy microsecond or nanosecond pcapng-style), then walks
    packet records, defensively parsing Ethernet(14) + IPv4(IHL) + UDP(8) to
    find UDP datagrams to port 9891 with exactly one period's payload.
    Anything else (wrong port, truncated record, non-512-byte payload) is
    silently skipped, mirroring tcpdump's own port filter at capture time.
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
            ts_s, ts_frac, incl, _orig = struct.unpack(endian + "IIII", ph)
            data = f.read(incl)
            if len(data) < incl:
                return
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
            payload = data[udp_off + 8 :]
            if len(payload) != PAYLOAD_BYTES:
                continue
            yield ts_s + ts_frac / ts_div, payload


@dataclass(frozen=True)
class ConversionResult:
    """Summary of one pcap-to-detections conversion run."""

    n_packets: int
    n_detections: int
    threshold: float
    rt_minus_mono: float

    def summary_line(self) -> str:
        """The operator-facing one-line summary (unchanged wording from the
        original scratch script, so existing runbooks/logs stay comparable)."""

        return "packets=%d detections=%d threshold=%.4f rt_minus_mono=%.6f" % (
            self.n_packets,
            self.n_detections,
            self.threshold,
            self.rt_minus_mono,
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
    """

    rt_minus_mono = time.clock_gettime(time.CLOCK_REALTIME) - time.monotonic()
    n_pkts = n_det = 0
    last_det_mono: float | None = None
    above = False
    with open(out_path, "w") as fo:
        for rt_ts, payload in payloads(pcap_path):
            n_pkts += 1
            vals = struct.unpack("<%dh" % (PAYLOAD_BYTES // 2), payload)
            peak = max(abs(v) for v in vals) / 32768.0
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
    return ConversionResult(
        n_packets=n_pkts,
        n_detections=n_det,
        threshold=threshold,
        rt_minus_mono=rt_minus_mono,
    )


__all__ = [
    "DEFAULT_THRESHOLD",
    "FRAMES_PER_PKT",
    "PAYLOAD_BYTES",
    "RATE",
    "REFRACTORY_S",
    "ConversionResult",
    "convert_pcap_to_detections",
    "payloads",
]
