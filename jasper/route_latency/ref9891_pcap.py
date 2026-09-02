# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Convert a tcpdump pcap of the outputd ``:9891`` reference stream into the
route-latency harness's mic-detections JSONL (the electrical plane).

Wire truth (``rust/jasper-outputd/src/main.rs``): headerless LE interleaved
stereo int16 @ 48 kHz, one DAC period per datagram. The PERIOD is a per-box
declaration, not a constant, so callers pass it in (outputd's STATUS
``dac.period_frames``) or let the first datagram establish it.

This module owns what a ``:9891`` datagram IS for every reader of the port:
:func:`parse_udp_payload` (which packets are datagrams at all),
:func:`is_tap_period` (which datagrams are the tap), and the
``unexpected_by_length`` / ``REFUSAL_*`` vocabulary for reporting the rest
(#3509). ``scripts/jasper-pipe-probe`` consumes all three.

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
from collections import Counter
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
REFUSAL_NO_GEOMETRY = "no_geometry"

_REF_UDP_PORT = 9891
_IPPROTO_UDP = 17
_ETHERTYPE_IPV4 = 0x0800
# IPv4 flags+fragment-offset word: the low 13 bits are the offset and bit 13 is
# MF. Anything nonzero is a fragment, whose bytes are not a UDP datagram.
_IPV4_FRAG_MASK = 0x3FFF


def is_tap_period(payload_len: int, period_bytes: int) -> bool:
    """True when a ``:9891`` payload is exactly one tap datagram.

    outputd publishes one DAC period per datagram and nothing else, so the
    length IS the identity check: `period_frames * BYTES_PER_FRAME`, with
    `period_frames` read from the box rather than guessed. Every reader of
    this port shares this rule so that "not the tap" means one thing (#3509).
    """

    return payload_len > 0 and payload_len == period_bytes


CAPTURE_MANIFEST_KIND = "jts_pipe_probe_capture"
CAPTURE_MANIFEST_SCHEMA = 1

# How far below the requested window a capture may land before it is called
# short. NOT one period: a healthy 300 s jts3 capture came back 0.016 s (six
# periods) light, because the sniffer's loop stops on its own deadline
# mid-period and whatever is still in the socket buffer is never read. A
# one-period rule fires on every run and gets ignored; this catches a real
# mid-capture gap — the #3509 incident spanned a daemon restart — instead.
CAPTURE_SHORT_FRACTION = 0.01


def capture_manifest_path(raw_path: Path) -> Path:
    return raw_path.with_name(raw_path.name + ".json")


def capture_refusal(stored: int, all_zero: bool, *, period_bytes: int | None) -> str | None:
    """Why a ``:9891`` capture is worthless, when it is.

    ``period_bytes`` is None when outputd never said what a tap datagram
    looks like — a blind instrument before any byte is judged, which is not
    the same as one that looked and found nothing.
    """

    if period_bytes is None:
        return REFUSAL_NO_GEOMETRY
    if stored == 0:
        return REFUSAL_NO_PACKETS
    return REFUSAL_ALL_ZERO if all_zero else None


def build_capture_manifest(
    *,
    host: str,
    requested_seconds: float,
    period_bytes: int | None,
    records_seen: int,
    stored_payloads: int,
    unexpected_by_length: dict[int, int],
    rejected_by_header: dict[str, int],
    pairing_ok: bool,
    detail: str,
    pcm_bytes: int,
    all_zero: bool,
    start_monotonic_ns: int | None,
    start_realtime_ns: int | None,
) -> dict:
    """The sidecar manifest a ``:9891`` capture writes beside its .raw.

    Two reject buckets answer different questions: ``unexpected_by_length``
    is real UDP datagrams to the port that were not one period;
    ``rejected_by_header`` is packets that only looked like them to a parse
    checking neither protocol nor fragment. JSON object keys are strings, so
    the former's integer lengths read back as strings.

    ``pcm`` describes what was WRITTEN, not what arrived: on a pairing
    violation the payloads stay un-collapsed and a duration taken from the
    arrival count would read double.
    """

    frames = pcm_bytes // BYTES_PER_FRAME
    return {
        "schema_version": CAPTURE_MANIFEST_SCHEMA,
        "kind": CAPTURE_MANIFEST_KIND,
        "host": host,
        "requested_seconds": requested_seconds,
        "refusal": capture_refusal(stored_payloads, all_zero, period_bytes=period_bytes),
        "start_monotonic_ns": start_monotonic_ns,
        "start_realtime_ns": start_realtime_ns,
        "tap": {
            "udp_port": _REF_UDP_PORT,
            "rate_hz": RATE,
            "bytes_per_frame": BYTES_PER_FRAME,
            "period_bytes": period_bytes,
        },
        "records": {
            "seen": records_seen,
            "stored": stored_payloads,
            "unexpected_by_length": unexpected_by_length,
        },
        "wire": {"rejected_by_header": rejected_by_header},
        "pairing_ok": pairing_ok,
        "detail": detail,
        "pcm": {
            "bytes": pcm_bytes,
            "frames": frames,
            "duration_s": frames / RATE,
            "all_zero": all_zero,
            # The identity check: audio that stops mid-window still writes a
            # well-formed file, so the two durations have to be compared.
            "short_of_requested": (
                frames / RATE < requested_seconds * (1.0 - CAPTURE_SHORT_FRACTION)
            ),
        },
    }


def read_capture_manifest(raw_path: Path) -> dict | None:
    """The manifest sidecar beside a ``:9891`` capture, if it is still there.

    Colocated with :func:`build_capture_manifest` so the schema has one
    owner. Without it, re-analyzing a saved .raw silently loses every
    honesty signal the capture recorded.
    """

    path = capture_manifest_path(raw_path)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text())
    except ValueError:
        return None
    return manifest if manifest.get("kind") == CAPTURE_MANIFEST_KIND else None


def capture_provenance(manifest: dict | None) -> dict | None:
    """The subset of a capture manifest an analyzer should show its reader."""

    if manifest is None:
        return None
    return {
        "period_bytes": manifest.get("tap", {}).get("period_bytes"),
        "unexpected_by_length": manifest.get("records", {}).get("unexpected_by_length"),
        "rejected_by_header": manifest.get("wire", {}).get("rejected_by_header"),
        "all_zero": manifest.get("pcm", {}).get("all_zero"),
        "start_monotonic_ns": manifest.get("start_monotonic_ns"),
        "refusal": manifest.get("refusal"),
    }


def parse_udp_payload(frame: bytes, port: int) -> bytes | None:
    """The UDP payload an Ethernet frame carries to ``port``, or None.

    Every check here exists because skipping one manufactures a datagram
    that was never sent (#3509): an ICMP destination-unreachable quoting a
    loopback header is an IPv4 packet whose bytes at the UDP offsets read
    as a plausible port and payload, and a 576 B one yields exactly a
    548 B pseudo-datagram. So the protocol byte must say UDP, the packet
    must not be a fragment (a fragment's bytes are not a datagram), and the
    payload is bounded by the UDP length field rather than running to the
    end of the frame, which would append any trailer.
    """

    if len(frame) < 14 + 20 + 8:
        return None
    if struct.unpack(">H", frame[12:14])[0] != _ETHERTYPE_IPV4:
        return None
    if frame[14 + 9] != _IPPROTO_UDP:
        return None
    if struct.unpack(">H", frame[20:22])[0] & _IPV4_FRAG_MASK:
        return None
    udp_off = 14 + (frame[14] & 0x0F) * 4
    if len(frame) < udp_off + 8:
        return None
    if struct.unpack(">H", frame[udp_off + 2 : udp_off + 4])[0] != port:
        return None
    udp_len = struct.unpack(">H", frame[udp_off + 4 : udp_off + 6])[0]
    if udp_len < 8 or len(frame) < udp_off + udp_len:
        return None
    return frame[udp_off + 8 : udp_off + udp_len]


def payloads(path: Path) -> Iterator[tuple[float, bytes]]:
    """Yield ``(realtime_seconds, payload_bytes)`` for each ``:9891`` datagram in a pcap.

    Parses the global pcap header to detect byte order and timestamp
    resolution (legacy microsecond or nanosecond pcapng-style), then walks
    packet records, handing each to :func:`parse_udp_payload`.

    Every real datagram on the port is yielded WHATEVER its length: a payload
    that is not one period is the fact worth reporting, not one to hide, so
    the caller classifies. Anything that is not a UDP datagram to the port is
    skipped, mirroring tcpdump's own port filter at capture time.

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
            payload = parse_udp_payload(data, _REF_UDP_PORT)
            if payload is None:
                continue
            yield ts_s + ts_frac / ts_div, payload


@dataclass(frozen=True)
class ConversionResult:
    """Summary of one pcap-to-detections conversion run."""

    n_packets: int
    n_detections: int
    threshold: float
    rt_minus_mono: float
    frames_per_packet: int
    # Payload lengths on :9891 that were not one tap period, counted by
    # length -- the evidence #3509 asks for about what else reaches the port.
    # Shared vocabulary: every :9891 reader reports rejects under this name.
    unexpected_by_length: dict[int, int]
    refusal: str | None

    @property
    def n_unexpected_size(self) -> int:
        return sum(self.unexpected_by_length.values())

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
    period_bytes: int,
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

    ``period_bytes`` is the box's own `dac.period_frames * BYTES_PER_FRAME`
    and is REQUIRED: it is the identity check, so nothing here may infer it.
    Learning it from the capture — even from a single first datagram — is a
    vote the intruder can win, and an intruder-first pcap then converts the
    intruder, fires a detection off its bytes, and discards every real
    period as the wrong length (#3509). A datagram of any other length is
    counted and skipped rather than converted.

    A capture carrying no usable datagram, or one carrying nothing but zeros,
    is REFUSED: the tap ran and the pipeline produced an empty detections
    file, which is indistinguishable from "the speaker played nothing" unless
    the result says so (#3509). The JSONL is still written — a refusal is a
    verdict on the capture, not an I/O failure.
    """

    if period_bytes <= 0 or period_bytes % BYTES_PER_FRAME:
        # Reachable from `--period-bytes`: a bad value would otherwise reject
        # every datagram and refuse as `no_packets`, blaming the capture for
        # the caller's number.
        raise ValueError(
            f"period_bytes must be a positive multiple of {BYTES_PER_FRAME} "
            f"(one S16 stereo frame), got {period_bytes}"
        )
    rt_minus_mono = time.clock_gettime(time.CLOCK_REALTIME) - time.monotonic()
    n_pkts = n_det = 0
    unexpected: Counter[int] = Counter()
    last_det_mono: float | None = None
    above = False
    all_zero = True
    with open(out_path, "w") as fo:
        for rt_ts, payload in payloads(pcap_path):
            if not is_tap_period(len(payload), period_bytes):
                unexpected[len(payload)] += 1
                continue
            frames = len(payload) // BYTES_PER_FRAME
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
        frames_per_packet=period_bytes // BYTES_PER_FRAME,
        unexpected_by_length=dict(sorted(unexpected.items())),
        refusal=refusal,
    )


__all__ = [
    "BYTES_PER_FRAME",
    "DEFAULT_THRESHOLD",
    "RATE",
    "REFRACTORY_S",
    "REFUSAL_ALL_ZERO",
    "REFUSAL_NO_GEOMETRY",
    "REFUSAL_NO_PACKETS",
    "CAPTURE_MANIFEST_KIND",
    "CAPTURE_MANIFEST_SCHEMA",
    "ConversionResult",
    "build_capture_manifest",
    "capture_manifest_path",
    "capture_provenance",
    "capture_refusal",
    "convert_pcap_to_detections",
    "read_capture_manifest",
    "is_tap_period",
    "parse_udp_payload",
    "payloads",
]
