# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mic-side (egress) audio readers for the route-latency harness.

The harness's default mic source is the AEC bridge's always-on corpus-only
"raw0" leg on localhost UDP ``:9879`` — the XVF3800's channel 2, an
unprocessed room-mic capture (no chip DSP), documented as a wake-detection
leg in ``jasper.wake_legs`` and emitted by ``jasper.cli.aec_bridge``. Reading
it here does NOT add it as a wake-detection input; this module only
*consumes* the already-emitted stream, matching AGENTS.md's rule that raw0
stays corpus/tooling-only. On a Pi with no XVF3800 (or no bridge running),
nothing feeds :9879 and ``UdpMicReader`` fails loudly on a read timeout
rather than hanging forever — see :meth:`UdpMicReader.read_chunk`.

CRITICAL CLOCK RULE — read this before touching timestamps in this module:
the mic's sample clock rides the XVF3800's USB UAC2 clock, which drifts
against ``CLOCK_MONOTONIC`` (typical USB Adaptive Mode crystal tolerances are
around 100 ppm, i.e. up to ~180 ms of accumulated drift over a 30-minute
promotion run). Both readers therefore hand back a **fresh monotonic
timestamp for every chunk** (`time.monotonic_ns()` taken immediately after
the blocking read/recv call returns) rather than a single stream-start
anchor plus a running sample count. Callers (see
``jasper.route_latency.pairing``) re-derive each impulse's arrival time from
its own chunk's timestamp — never from an extrapolated sample offset since
stream start — bounding drift error to at most one chunk's worth of
uncertainty (plus, for UDP, burst-arrival jitter — see
:class:`UdpMicReader`).
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Protocol


# Wire format pinned by jasper.cli.aec_bridge / jasper.wake_legs: raw0 is
# 1280 samples, 16 kHz mono S16_LE per UDP datagram (80 ms of audio per
# packet at ~12.5 packets/sec). Duplicated here as literal constants (not
# imported from aec_bridge) because that module pulls in numpy/sounddevice/
# scipy at import time — heavy, ALSA-adjacent dependencies this harness
# should not need just to read a UDP socket. The wire format is frozen
# (jasper/wake_legs.py docstring: "token is FROZEN... never rename"), so
# duplicating the two integer constants carries negligible drift risk;
# `tests/test_route_latency_harness.py` cross-checks them against
# `jasper.wake_legs`/`jasper.cli.aec_bridge` so a future format change would
# fail loudly here too.
RAW0_UDP_HOST = "127.0.0.1"
RAW0_UDP_PORT = 9879
RAW0_SAMPLE_RATE_HZ = 16_000
RAW0_SAMPLES_PER_PACKET = 1280
RAW0_BYTES_PER_PACKET = RAW0_SAMPLES_PER_PACKET * 2  # int16

# How long to wait for the first/any packet before concluding nothing is
# feeding the socket. Generous relative to the ~80 ms emit cadence so a
# transient scheduling hiccup doesn't misclassify a live bridge as absent,
# but bounded so a genuinely-idle socket fails fast instead of hanging the
# whole harness run.
DEFAULT_UDP_READ_TIMEOUT_SECONDS = 5.0


class MicSourceUnavailableError(RuntimeError):
    """Raised when a mic reader cannot get audio within its timeout.

    Callers must treat this as a hard, loud failure — never silently
    fall back to a different source or synthesize samples. See module
    docstring: an unfed :9879 is an expected steady state on a box with no
    XVF3800 mic present, so this is the harness's explicit "no evidence,
    refuse to certify" signal.
    """


@dataclass(frozen=True)
class MicChunk:
    """One chunk of mono int16 samples plus the monotonic time it arrived.

    ``arrival_monotonic_ns`` is ``time.monotonic_ns()`` taken immediately
    after the blocking read that produced ``samples`` returned — see the
    module docstring's clock rule. It is the anchor every detection inside
    this chunk re-derives its event time from.
    """

    samples: bytes
    arrival_monotonic_ns: int
    sample_rate_hz: int


class MicReader(Protocol):
    """Interchangeable mic-audio source: UDP raw0 leg, or an ALSA fallback."""

    def read_chunk(self) -> MicChunk:
        """Block for the next chunk. Raises MicSourceUnavailableError on
        timeout/absence, never returns an empty/synthetic chunk."""
        ...

    def close(self) -> None: ...


class UdpMicReader:
    """Default mic source: the AEC bridge's raw0 leg on localhost :9879.

    Burst-jitter note (documented per the pinned contract): the bridge
    batches four internal 320-sample AEC frames into each 2560-byte
    datagram and ``sendto``s at that ~80 ms cadence, so packet *arrival*
    on the loopback interface is not perfectly periodic — expect residual
    jitter bounded by that emit cadence. Re-anchoring per packet (this
    reader's whole reason for existing) absorbs that jitter into each
    impulse's own uncertainty rather than letting it accumulate as drift
    across the run.
    """

    def __init__(
        self,
        *,
        host: str = RAW0_UDP_HOST,
        port: int = RAW0_UDP_PORT,
        timeout_seconds: float = DEFAULT_UDP_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout_seconds)
        self._sock.bind((host, port))
        self._timeout_seconds = timeout_seconds

    @property
    def sample_rate_hz(self) -> int:
        return RAW0_SAMPLE_RATE_HZ

    def read_chunk(self) -> MicChunk:
        try:
            data, _addr = self._sock.recvfrom(RAW0_BYTES_PER_PACKET * 2)
        except (socket.timeout, TimeoutError) as e:
            raise MicSourceUnavailableError(
                f"no raw0 packets received on :{self._sock.getsockname()[1]} "
                f"within {self._timeout_seconds:g}s — is the AEC bridge running "
                "with an XVF3800 present? (see jasper.route_latency.mic_readers "
                "module docstring)"
            ) from e
        arrival_ns = time.monotonic_ns()
        return MicChunk(
            samples=data,
            arrival_monotonic_ns=arrival_ns,
            sample_rate_hz=RAW0_SAMPLE_RATE_HZ,
        )

    def close(self) -> None:
        self._sock.close()


class AlsaMicReader:
    """Fallback mic source: a dedicated measurement mic over ALSA.

    Lazy-imports ``alsaaudio`` (mirrors ``jasper.cli.aec_bridge``'s
    ``_ref_thread`` pattern) so importing this module never requires ALSA
    to be installed — only actually opening a device does, which is fine
    since this reader is only constructed on the fallback path.
    """

    def __init__(
        self,
        device: str,
        *,
        sample_rate_hz: int = 16_000,
        period_frames: int = 1280,
    ) -> None:
        import alsaaudio  # lazy: ALSA-only dependency, fallback path only

        self._sample_rate_hz = sample_rate_hz
        self._pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NORMAL,
            device=device,
            rate=sample_rate_hz,
            channels=1,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=period_frames,
        )

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    def read_chunk(self) -> MicChunk:
        length, data = self._pcm.read()
        arrival_ns = time.monotonic_ns()
        if length <= 0:
            raise MicSourceUnavailableError(
                f"ALSA capture returned no frames (rc={length})"
            )
        return MicChunk(
            samples=data,
            arrival_monotonic_ns=arrival_ns,
            sample_rate_hz=self._sample_rate_hz,
        )

    def close(self) -> None:
        self._pcm.close()


def build_mic_reader(spec: str) -> MicReader:
    """Construct a reader from a ``--mic`` CLI spec.

    ``udp:9879`` (or bare, unset) selects :class:`UdpMicReader` on that
    port; ``alsa:<device>`` selects :class:`AlsaMicReader`.
    """

    spec = spec.strip()
    if spec in ("", "udp", f"udp:{RAW0_UDP_PORT}"):
        return UdpMicReader()
    if spec.startswith("udp:"):
        port = int(spec.split(":", 1)[1])
        return UdpMicReader(port=port)
    if spec.startswith("alsa:"):
        device = spec.split(":", 1)[1]
        if not device:
            raise ValueError("alsa: mic spec requires a device name, e.g. alsa:hw:1,0")
        return AlsaMicReader(device)
    raise ValueError(f"unrecognized --mic spec {spec!r}; use udp:<port> or alsa:<device>")


__all__ = [
    "DEFAULT_UDP_READ_TIMEOUT_SECONDS",
    "RAW0_BYTES_PER_PACKET",
    "RAW0_SAMPLES_PER_PACKET",
    "RAW0_SAMPLE_RATE_HZ",
    "RAW0_UDP_HOST",
    "RAW0_UDP_PORT",
    "AlsaMicReader",
    "MicChunk",
    "MicReader",
    "MicSourceUnavailableError",
    "UdpMicReader",
    "build_mic_reader",
]
