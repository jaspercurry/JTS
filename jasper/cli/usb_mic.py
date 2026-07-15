# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded clean-mic relay for the UAC2 Pi-to-host direction."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import fcntl
import json
import logging
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from typing import Any

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event
from jasper.usb_mic import (
    GADGET_PATH,
    INTENT_PATH,
    RELAY_STATUS_PATH,
    USB_HOST_MIC_UDP_PORT,
    usb_mic_enabled,
)

logger = logging.getLogger("jasper.usb_mic")

SOURCE_RATE = 16_000
CHANNELS = 1
SAMPLE_BYTES = 2
PERIOD_FRAMES = 320
PERIOD_BYTES = PERIOD_FRAMES * SAMPLE_BYTES
# The bridge's dedicated USB-mic leg emits one native AEC frame (20 ms).
# Accept the old 80 ms packet during rolling upgrades, but never emit it from
# current code. Both shapes split into the same bounded 20 ms sink periods.
PACKET_BYTES = PERIOD_BYTES
LEGACY_PACKET_BYTES = 1280 * SAMPLE_BYTES
ACCEPTED_PACKET_BYTES = frozenset((PACKET_BYTES, LEGACY_PACKET_BYTES))
QUEUE_PERIODS = 2
PIPE_BYTES = 4096
UAC2_DEVICE = "plughw:CARD=UAC2Gadget,DEV=0"
# The UAC2 gadget fixes the playback ring at four periods on the current Pi
# kernel. A 10 ms period is therefore the value that realizes the 40 ms
# hardware buffer target (a 20 ms period was observed as an 80 ms ring).
ALSA_PERIOD_US = 10_000
ALSA_BUFFER_US = 40_000
AUDIO_PROGRESS_FRESH_SECONDS = 2.0
DROP_STREAK_WARN_INTERVALS = 2
HOST_PCM_STATUS_PATH = Path("/proc/asound/UAC2Gadget/pcm0p/sub0/status")


class RelayError(RuntimeError):
    """An expected relay failure that systemd should restart."""


class LatestAudioQueue:
    """Two-period drop-oldest queue: bounded memory and stale latency."""

    def __init__(self, max_periods: int = QUEUE_PERIODS) -> None:
        self._items: deque[bytes] = deque()
        self._max_periods = max_periods
        self._condition = threading.Condition()
        self._closed = False
        self.dropped = 0

    def put(self, payload: bytes) -> None:
        with self._condition:
            while len(self._items) >= self._max_periods:
                self._items.popleft()
                self.dropped += 1
            self._items.append(payload)
            self._condition.notify()

    def get(self, timeout: float) -> bytes | None:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()


class AplaySink:
    """Feed current 16 kHz frames to ALSA's proven blocking resampler."""

    def __init__(self) -> None:
        self.queue = LatestAudioQueue()
        self.frames_written = 0
        self.last_progress_epoch_sec = 0.0
        self.last_progress_monotonic = 0.0
        self.error = ""
        self._progress_lock = threading.Lock()
        self.process = subprocess.Popen(
            [
                "aplay", "-q", "-D", UAC2_DEVICE, "-t", "raw",
                "-f", "S16_LE", "-r", str(SOURCE_RATE), "-c", str(CHANNELS),
                "-F", str(ALSA_PERIOD_US), "-B", str(ALSA_BUFFER_US),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if self.process.stdin is None:
            self.process.kill()
            raise RelayError("aplay did not expose an input pipe")
        try:
            fcntl.fcntl(self.process.stdin.fileno(), fcntl.F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass
        self._thread = threading.Thread(target=self._write_loop, daemon=True)
        self._thread.start()

    def _write_loop(self) -> None:
        assert self.process.stdin is not None
        while True:
            payload = self.queue.get(timeout=0.2)
            if payload is None:
                if self.process.poll() is not None or self.queue._closed:
                    return
                continue
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                with self._progress_lock:
                    self.frames_written += len(payload) // SAMPLE_BYTES
                    self.last_progress_monotonic = time.monotonic()
                    self.last_progress_epoch_sec = time.time()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return

    def check(self) -> None:
        returncode = self.process.poll()
        if returncode is None and not self.error:
            return
        detail = self.error
        if not detail and self.process.stderr is not None:
            detail = self.process.stderr.read().decode(
                "utf-8", errors="replace",
            ).strip().replace("\n", " | ")
        raise RelayError(
            f"aplay exited unexpectedly rc={returncode}: {detail or 'no detail'}"
        )

    def progress(self) -> tuple[int, float, float]:
        """Return frames, monotonic progress time, and wall-clock progress time."""

        with self._progress_lock:
            return (
                self.frames_written,
                self.last_progress_monotonic,
                self.last_progress_epoch_sec,
            )

    def close(self) -> None:
        self.queue.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1.0)
        self._thread.join(timeout=1.0)
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass


@dataclass(frozen=True)
class HostPcmSnapshot:
    running: bool
    hw_ptr: int | None


def _read_host_pcm_status(
    path: Path = HOST_PCM_STATUS_PATH,
) -> HostPcmSnapshot:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return HostPcmSnapshot(False, None)
    match = re.search(r"^\s*hw_ptr\s*:\s*(\d+)\s*$", text, re.MULTILINE)
    return HostPcmSnapshot(
        running="state: RUNNING" in text,
        hw_ptr=int(match.group(1)) if match else None,
    )


class HostProgressTracker:
    """Turn ALSA's RUNNING label into evidence of advancing host reads."""

    def __init__(self) -> None:
        self._previous = HostPcmSnapshot(False, None)
        self._running_since_monotonic = 0.0
        self.last_progress_monotonic = 0.0
        self.last_progress_epoch_sec = 0.0

    def observe(
        self,
        snapshot: HostPcmSnapshot,
        *,
        now_monotonic: float,
        now_epoch_sec: float,
    ) -> None:
        if snapshot.running and not self._previous.running:
            self._running_since_monotonic = now_monotonic
        if (
            snapshot.running
            and self._previous.running
            and snapshot.hw_ptr is not None
            and self._previous.hw_ptr is not None
            and snapshot.hw_ptr != self._previous.hw_ptr
        ):
            self.last_progress_monotonic = now_monotonic
            self.last_progress_epoch_sec = now_epoch_sec
        if not snapshot.running:
            self._running_since_monotonic = 0.0
        self._previous = snapshot

    def progress_age(self, now_monotonic: float) -> float | None:
        if not self._previous.running:
            return None
        baseline = max(
            self.last_progress_monotonic,
            self._running_since_monotonic,
        )
        return max(0.0, now_monotonic - baseline)

    @property
    def has_progressed(self) -> bool:
        return bool(
            self._previous.running
            and self.last_progress_monotonic >= self._running_since_monotonic
            and self.last_progress_monotonic > 0.0
        )


def _audio_health_snapshot(
    *,
    now_monotonic: float,
    started_monotonic: float,
    last_packet_monotonic: float,
    last_sink_progress_monotonic: float,
    host_snapshot: HostPcmSnapshot,
    host_progress: HostProgressTracker,
    sustained_drops: bool,
) -> dict[str, bool | float | None | str]:
    """Classify source, writer, and host-clock progress without side effects."""

    source_baseline = last_packet_monotonic or started_monotonic
    source_age = max(0.0, now_monotonic - source_baseline)
    source_stalled = source_age > AUDIO_PROGRESS_FRESH_SECONDS
    host_progress_age = host_progress.progress_age(now_monotonic)
    host_was_streaming = host_progress.has_progressed
    host_progressing = bool(
        host_snapshot.running
        and host_was_streaming
        and host_progress_age is not None
        and host_progress_age <= AUDIO_PROGRESS_FRESH_SECONDS
    )
    host_stalled = bool(
        host_snapshot.running
        and host_was_streaming
        and host_progress_age is not None
        and host_progress_age > AUDIO_PROGRESS_FRESH_SECONDS
    )
    sink_baseline = last_sink_progress_monotonic or started_monotonic
    sink_progress_age = max(0.0, now_monotonic - sink_baseline)
    sink_stalled = bool(
        host_snapshot.running
        and host_was_streaming
        and sink_progress_age > AUDIO_PROGRESS_FRESH_SECONDS
    )
    # A non-advancing gadget hw_ptr is also what an idle Mac looks like, so it
    # clears Streaming but is not by itself a product fault. Drops become a
    # fault only while the independently observed host clock is advancing.
    drop_stalled = bool(host_progressing and sustained_drops)
    audio_stalled = source_stalled or drop_stalled
    reasons: list[str] = []
    if source_stalled:
        reasons.append("AEC source packets stopped")
    if drop_stalled:
        reasons.append("USB audio queue is dropping continuously")
    return {
        "audio_healthy": not audio_stalled,
        "audio_stalled": audio_stalled,
        "audio_health_detail": "; ".join(reasons),
        "source_stalled": source_stalled,
        "sink_stalled": sink_stalled,
        "host_stalled": host_stalled,
        "host_streaming": bool(
            host_progressing
            and not sink_stalled
            and not audio_stalled
        ),
        "packet_age_ms": round(source_age * 1000.0, 1),
        "sink_progress_age_ms": round(sink_progress_age * 1000.0, 1),
        "host_progress_age_ms": (
            round(host_progress_age * 1000.0, 1)
            if host_progress_age is not None
            else None
        ),
    }


def _ready(
    *,
    intent_path: str = INTENT_PATH,
    gadget_path: str = GADGET_PATH,
) -> tuple[bool, str]:
    if not usb_mic_enabled(intent_path):
        return False, "intent_off_or_invalid"
    function = Path(gadget_path) / "functions/uac2.usb0"
    try:
        p_chmask = (function / "p_chmask").read_text(encoding="utf-8").strip()
    except OSError:
        return False, "uac2_missing"
    if p_chmask != "1":
        return False, f"p_chmask_{p_chmask or 'missing'}"
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "jasper-aec-bridge.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        active = False
    return (True, "ready") if active else (False, "aec_bridge_inactive")


def _write_status(path: str, payload: dict[str, Any]) -> None:
    payload = {
        "schema_version": 2,
        "updated_epoch_sec": time.time(),
        **payload,
    }
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n", mode=0o644)


def run_relay(
    *,
    udp_port: int = USB_HOST_MIC_UDP_PORT,
    status_path: str = RELAY_STATUS_PATH,
) -> int:
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
    sock.bind(("127.0.0.1", udp_port))
    sock.settimeout(0.2)
    sink = AplaySink()
    packets = 0
    periods = 0
    malformed_packets = 0
    started_monotonic = time.monotonic()
    last_packet_monotonic = 0.0
    last_packet_epoch_sec = 0.0
    last_status = 0.0
    last_status_drops = 0
    drop_streak = 0
    host_progress = HostProgressTracker()
    last_audio_stalled: bool | None = None
    log_event(logger, "usb_mic.started", udp_port=udp_port)
    try:
        while not stop.is_set():
            sink.check()
            try:
                payload, _address = sock.recvfrom(65_536)
            except socket.timeout:
                payload = b""
            if len(payload) in ACCEPTED_PACKET_BYTES:
                packets += 1
                last_packet_monotonic = time.monotonic()
                last_packet_epoch_sec = time.time()
                for offset in range(0, len(payload), PERIOD_BYTES):
                    chunk = payload[offset : offset + PERIOD_BYTES]
                    sink.queue.put(chunk)
                    periods += 1
            elif payload:
                malformed_packets += 1
            now = time.monotonic()
            if now - last_status >= 0.5:
                now_epoch = time.time()
                host_snapshot = _read_host_pcm_status()
                host_progress.observe(
                    host_snapshot,
                    now_monotonic=now,
                    now_epoch_sec=now_epoch,
                )
                sink_frames, sink_progress_monotonic, sink_progress_epoch = (
                    sink.progress()
                )
                frames_written = sink_frames
                periods_dropped = sink.queue.dropped
                drops_since_status = max(0, periods_dropped - last_status_drops)
                status_interval = max(0.001, now - last_status) if last_status else 0.5
                current_host_progress_age = host_progress.progress_age(now)
                if (
                    host_snapshot.running
                    and host_progress.has_progressed
                    and current_host_progress_age is not None
                    and current_host_progress_age <= AUDIO_PROGRESS_FRESH_SECONDS
                    and drops_since_status
                ):
                    drop_streak += 1
                else:
                    drop_streak = 0
                sustained_drops = drop_streak >= DROP_STREAK_WARN_INTERVALS
                health = _audio_health_snapshot(
                    now_monotonic=now,
                    started_monotonic=started_monotonic,
                    last_packet_monotonic=last_packet_monotonic,
                    last_sink_progress_monotonic=sink_progress_monotonic,
                    host_snapshot=host_snapshot,
                    host_progress=host_progress,
                    sustained_drops=sustained_drops,
                )
                audio_stalled = bool(health["audio_stalled"])
                if audio_stalled != last_audio_stalled:
                    log_event(
                        logger,
                        "usb_mic.audio_health",
                        state="stalled" if audio_stalled else "healthy",
                        detail=str(health["audio_health_detail"]),
                        host_pcm_running=int(host_snapshot.running),
                        periods_dropped=periods_dropped,
                        level=logging.WARNING if audio_stalled else logging.INFO,
                    )
                    last_audio_stalled = audio_stalled
                _write_status(status_path, {
                    "state": "running",
                    "packets_received": packets,
                    "malformed_packets": malformed_packets,
                    "periods_queued": periods,
                    "periods_dropped": periods_dropped,
                    "periods_dropped_since_status": drops_since_status,
                    "drop_rate_periods_per_sec": round(
                        drops_since_status / status_interval, 1,
                    ),
                    "sustained_drops": sustained_drops,
                    "frames_written": frames_written,
                    "last_packet_epoch_sec": last_packet_epoch_sec,
                    "last_sink_progress_epoch_sec": sink_progress_epoch,
                    "last_host_progress_epoch_sec": (
                        host_progress.last_progress_epoch_sec
                    ),
                    "host_pcm_running": host_snapshot.running,
                    "host_hw_ptr": host_snapshot.hw_ptr,
                    **health,
                    "udp_port": udp_port,
                })
                last_status = now
                last_status_drops = periods_dropped
    finally:
        sink.close()
        sock.close()
        _write_status(status_path, {
            "state": "stopped",
            "packets_received": packets,
            "malformed_packets": malformed_packets,
            "periods_queued": periods,
            "periods_dropped": sink.queue.dropped,
            "frames_written": sink.progress()[0],
            "host_streaming": False,
            "audio_healthy": False,
            "audio_stalled": False,
            "udp_port": udp_port,
        })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--udp-port", type=int, default=USB_HOST_MIC_UDP_PORT)
    parser.add_argument("--status-path", default=RELAY_STATUS_PATH)
    args = parser.parse_args(argv)
    if args.check_ready:
        ready, reason = _ready()
        if not ready:
            log_event(logger, "usb_mic.skip", reason=reason)
        return 0 if ready else 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s usb-mic %(levelname)s %(message)s",
    )
    try:
        return run_relay(udp_port=args.udp_port, status_path=args.status_path)
    except (OSError, RelayError) as exc:
        logger.error("USB mic relay failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
