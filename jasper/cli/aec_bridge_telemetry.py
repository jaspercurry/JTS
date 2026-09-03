# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC bridge telemetry — the UDP leg emitters and the bridge stats file.

Imports run one way only: nothing here reads `jasper.cli.aec_bridge`. The
capture geometry and reference endpoint the snapshot republishes belong to
the bridge, so they arrive as a `StatsIdentity`, and each emitter carries the
`_BridgeStats` it counts into rather than reaching for a module global.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import socket
import struct
import threading
import time

from jasper.aec_sweep import Aec3SweepVariant, DEFAULT_AEC3_SWEEP_VARIANTS
from jasper import wake_legs
from jasper.usb_mic import (
    USB_MIC_HEADER_STRUCT,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
)

# The bridge's own logger name: these lines reach journald beside the rest of
# jasper-aec-bridge's output, where log tooling already looks for them.
logger = logging.getLogger("jasper.aec_bridge")

# Voice consumes 1280-sample (80 ms) chunks. Aggregating four 320-sample AEC
# frames into one UDP packet keeps the bridge↔voice contract symmetric with
# MicCapture's frame size and holds the packet rate at ~12.5 pps. The AEC
# engine still works on 320-sample windows internally.
OUT_FRAME_SAMPLES = 1280
OUT_FRAME_BYTES = OUT_FRAME_SAMPLES * 2  # int16
BRIDGE_STATS_PATH = Path("/run/jasper/aec_bridge_stats.json")
BRIDGE_STATS_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class StatsIdentity:
    """Static bridge values the snapshot republishes.

    `reset` overrides the reference pair once the bridge has resolved its
    live configuration.
    """

    sample_rate_hz: int
    frame_samples: int
    reference_source: str
    reference_endpoint: str


def _zero_leg_counters(
    aec3_sweep_variants: tuple[Aec3SweepVariant, ...] = (
        DEFAULT_AEC3_SWEEP_VARIANTS
    ),
) -> dict[str, int]:
    """A fresh per-leg counter dict zeroed for every emit leg: each
    jasper.wake_legs token plus the dynamic AEC3-sweep variant legs.

    Keyed off the registry so the bridge's UDP emit tokens and the wake-event
    corpus columns stay in lockstep.
    """
    counters = {spec.token: 0 for spec in wake_legs.REGISTRY}
    counters.update({variant.leg: 0 for variant in aec3_sweep_variants})
    return counters


class _BridgeStats:
    """Low-cost monotonic counters for capture provenance.

    The wake-corpus recorder snapshots this JSON file at clip start/stop and
    stores counter deltas in clip metadata. Counters are monotonic for the
    lifetime of one bridge process; the PID + start epoch let consumers
    reject deltas that span a restart.
    """

    def __init__(self, identity: StatsIdentity) -> None:
        self._identity = identity
        self._lock = threading.Lock()
        self._started_epoch_sec = time.time()
        self.reset()

    def reset(
        self,
        aec3_sweep_variants: tuple[Aec3SweepVariant, ...] = (
            DEFAULT_AEC3_SWEEP_VARIANTS
        ),
        *,
        reference_source: str | None = None,
        reference_endpoint: str | None = None,
    ) -> None:
        with self._lock:
            self._started_epoch_sec = time.time()
            self._started_monotonic = time.monotonic()
            self._leg_engines: dict[str, dict[str, object]] = {}
            self._active_capture_plan: dict[str, object] = {}
            self._capture_stream: dict[str, object] = {}
            self._reference_source = (
                self._identity.reference_source
                if reference_source is None
                else reference_source
            )
            self._reference_endpoint = (
                self._identity.reference_endpoint
                if reference_endpoint is None
                else reference_endpoint
            )
            self._reference_frames_enqueued = 0
            self._reference_last_frame_monotonic: float | None = None
            self._counters: dict[str, int] = {
                "frames_processed": 0,
                "ref_starved_frames": 0,
                "usb_mic_source_fallback_frames": 0,
            }
            self._grouped_counters: dict[str, dict[str, int]] = {
                "queue_drops": {
                    "mic": 0,
                    "chip": 0,
                    "raw0": 0,
                    "usb": 0,
                    "ref": 0,
                },
                "udp_send_drops_by_leg": _zero_leg_counters(aec3_sweep_variants),
                "packets_sent_by_leg": _zero_leg_counters(aec3_sweep_variants),
            }

    def record_reference_frames(self, count: int) -> None:
        """Record complete 20 ms reference frames accepted by ``ref_q``.

        Conversion alone is not receiver progress: a full bounded queue means
        the AEC loop cannot consume the new frame, so callers report only
        successful ``put_nowait`` operations here. The AEC loop's reuse of
        ``last_ref_bytes`` deliberately never reaches this method — a
        carried-forward frame must not refresh receiver health.
        """

        if count <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._reference_frames_enqueued += count
            self._reference_last_frame_monotonic = now

    def set_capture_stream(
        self,
        *,
        sample_rate_hz: int,
        block_frames: int,
        input_latency_seconds: float,
    ) -> None:
        """Publish the PortAudio geometry negotiated by the live XVF stream."""

        with self._lock:
            self._capture_stream = {
                "sample_rate_hz": sample_rate_hz,
                "block_frames": block_frames,
                "input_latency_seconds": input_latency_seconds,
                "input_latency_frames": round(
                    input_latency_seconds * sample_rate_hz
                ),
            }

    def set_leg_engine(
        self,
        leg: str,
        *,
        enabled: bool,
        loaded: bool,
        error: str | None = None,
    ) -> None:
        """Record an optional engine leg's current runtime availability.

        Gives the bridge-stats file a journal-independent answer to "is this
        leg actually running?" across both initialization and later inference
        failures; jasper-doctor's `check_aec_bridge_dtln_engine` reads it.
        """
        with self._lock:
            self._leg_engines[leg] = {
                "enabled": enabled,
                "loaded": loaded,
                "error": error,
            }

    def set_active_capture_plan(
        self,
        *,
        wake_corpus_plan_id: str,
        expected_legs: tuple[str, ...],
        emitted_legs: list[str],
        corpus_flags: dict[str, object],
        beam_plan: dict[str, object],
        ports: dict[str, int],
        mic_reference_identity: dict[str, object],
        usb_mic_source: dict[str, object] | None = None,
        mic_fingerprint: str = "",
        dac_reference_fingerprint: str = "",
    ) -> None:
        with self._lock:
            self._active_capture_plan = {
                "wake_corpus_plan_id": wake_corpus_plan_id,
                "expected_legs": list(expected_legs),
                "emitted_legs": list(emitted_legs),
                "enabled_corpus_flags": dict(corpus_flags),
                "beam_plan": dict(beam_plan),
                "ports": dict(ports),
                "mic_reference_identity": dict(mic_reference_identity),
                "usb_mic_source": dict(usb_mic_source or {}),
                "mic_fingerprint": mic_fingerprint,
                "dac_reference_fingerprint": dac_reference_fingerprint,
            }

    def set_usb_mic_effective_source(
        self,
        *,
        mode: str,
        leg: str,
        fallback_active: bool,
    ) -> None:
        """Publish the source actually exported, not just configured intent."""

        with self._lock:
            source = self._active_capture_plan.get("usb_mic_source")
            if not isinstance(source, dict):
                return
            source["mode"] = mode
            source["leg"] = leg
            source["fallback_active"] = fallback_active

    def mark_leg_unavailable(self, leg: str, *, error: str) -> None:
        """Atomically withdraw a failed runtime leg from live bridge truth.

        Keep ``expected_legs`` intact so capture-plan validation reports the
        promised leg as missing, while ``emitted_legs`` and ``ports`` describe
        only outputs the bridge can still feed.
        """
        with self._lock:
            self._leg_engines[leg] = {
                "enabled": True,
                "loaded": False,
                "error": error,
            }
            emitted = self._active_capture_plan.get("emitted_legs")
            if isinstance(emitted, list):
                self._active_capture_plan["emitted_legs"] = [
                    emitted_leg for emitted_leg in emitted
                    if emitted_leg != leg
                ]
            ports = self._active_capture_plan.get("ports")
            if isinstance(ports, dict):
                ports.pop(leg, None)

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def inc_nested(self, group: str, key: str, amount: int = 1) -> None:
        with self._lock:
            values = self._grouped_counters.get(group)
            if values is None:
                return
            values[key] = values.get(key, 0) + amount

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters: dict[str, object] = dict(self._counters)
            counters.update({
                group: dict(values)
                for group, values in self._grouped_counters.items()
            })
            leg_engines = json.loads(json.dumps(self._leg_engines))
            active_capture_plan = json.loads(json.dumps(self._active_capture_plan))
            capture_stream = json.loads(json.dumps(self._capture_stream))
            reference_source = self._reference_source
            reference_endpoint = self._reference_endpoint
            reference_frames_enqueued = self._reference_frames_enqueued
            reference_last_frame_monotonic = self._reference_last_frame_monotonic
            started = self._started_epoch_sec
            started_monotonic = self._started_monotonic
            now_monotonic = time.monotonic()
        last_frame_age_ms = (
            None
            if reference_last_frame_monotonic is None
            else max(
                0,
                int(
                    (now_monotonic - reference_last_frame_monotonic) * 1000
                ),
            )
        )
        return {
            "schema_version": BRIDGE_STATS_SCHEMA_VERSION,
            "pid": os.getpid(),
            "started_epoch_sec": started,
            "updated_epoch_sec": time.time(),
            "sample_rate_hz": self._identity.sample_rate_hz,
            "frame_samples": self._identity.frame_samples,
            "out_frame_samples": OUT_FRAME_SAMPLES,
            "counters": counters,
            "leg_engines": leg_engines,
            "capture_stream": capture_stream,
            "reference_input": {
                "source": reference_source,
                "endpoint": reference_endpoint,
                "frames_enqueued": reference_frames_enqueued,
                "last_frame_age_ms": last_frame_age_ms,
                "snapshot_monotonic_ms": max(0, int(now_monotonic * 1000)),
                "process_age_ms": max(
                    0,
                    int((now_monotonic - started_monotonic) * 1000),
                ),
            },
            "active_capture_plan": active_capture_plan,
            "wake_corpus_plan_id": active_capture_plan.get(
                "wake_corpus_plan_id", "",
            ),
            "emitted_legs": active_capture_plan.get("emitted_legs", []),
        }

    def write_snapshot(self, path: Path = BRIDGE_STATS_PATH) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(), sort_keys=True))
            tmp.replace(path)
        except OSError as e:
            logger.debug("bridge stats snapshot write failed: %s", e)


@dataclass
class DropLogDebouncer:
    interval_sec: float = 1.0
    drops_in_window: int = 0
    last_log: float = 0.0

    def record(self, now: float) -> tuple[int, float] | None:
        return self.record_many(now, 1)

    def record_many(self, now: float, drops: int) -> tuple[int, float] | None:
        self.drops_in_window += max(0, drops)
        return self.flush(now)

    def flush(self, now: float) -> tuple[int, float] | None:
        if self.drops_in_window <= 0:
            return None
        if self.last_log and now - self.last_log < self.interval_sec:
            return None
        window_sec = now - self.last_log if self.last_log else self.interval_sec
        drops = self.drops_in_window
        self.drops_in_window = 0
        self.last_log = now
        return drops, window_sec


def _send_packet(
    *,
    stats: _BridgeStats,
    sock: socket.socket,
    dest: tuple[str, int],
    packet: bytes,
    leg: str,
) -> None:
    """Send one non-blocking leg packet and preserve drop-newest stats."""

    try:
        sock.sendto(packet, dest)
        stats.inc_nested("packets_sent_by_leg", leg)
    except BlockingIOError:
        stats.inc_nested("udp_send_drops_by_leg", leg)
        logger.warning("udp %s sendto would block, dropping frame", leg)


def emit_packet(
    *,
    stats: _BridgeStats,
    sock: socket.socket,
    dest: tuple[str, int],
    batch: bytearray,
    pcm: bytes,
    leg: str,
    frame_bytes: int = OUT_FRAME_BYTES,
) -> None:
    batch.extend(pcm)
    if len(batch) < frame_bytes:
        return
    _send_packet(
        stats=stats,
        sock=sock,
        dest=dest,
        packet=bytes(batch[:frame_bytes]),
        leg=leg,
    )
    del batch[:frame_bytes]


@dataclass
class LegEmitter:
    sock: socket.socket
    dest: tuple[str, int]
    batch: bytearray
    stats_key: str
    stats: _BridgeStats
    frame_samples: int = OUT_FRAME_SAMPLES

    def emit(self, pcm: bytes) -> None:
        emit_packet(
            stats=self.stats,
            sock=self.sock,
            dest=self.dest,
            batch=self.batch,
            pcm=pcm,
            leg=self.stats_key,
            frame_bytes=self.frame_samples * 2,
        )

    def close(self) -> None:
        self.sock.close()


@dataclass
class TimestampedLegEmitter(LegEmitter):
    """Packetize the isolated USB-host mic leg with emit-time metadata.

    The wire header's ``t_capture_mono_ns`` is deliberately a bridge-emit
    timestamp: it measures bridge emit -> relay sink. PortAudio's input
    latency is observed separately, when the capture stream opens.
    """

    _seq: int = field(default=0, init=False, repr=False)

    def emit(self, pcm: bytes) -> None:
        frame_bytes = self.frame_samples * 2
        self.batch.extend(pcm)
        if len(self.batch) < frame_bytes:
            return
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        header = struct.pack(
            USB_MIC_HEADER_STRUCT,
            USB_MIC_PACKET_MAGIC,
            USB_MIC_PACKET_VERSION,
            0,
            seq,
            time.clock_gettime_ns(time.CLOCK_MONOTONIC),
        )
        _send_packet(
            stats=self.stats,
            sock=self.sock,
            dest=self.dest,
            packet=header + bytes(self.batch[:frame_bytes]),
            leg=self.stats_key,
        )
        del self.batch[:frame_bytes]
