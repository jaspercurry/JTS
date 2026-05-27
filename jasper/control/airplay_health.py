"""Lightweight AirPlay health sampler for the /system dashboard.

The sampler runs inside jasper-control, next to SystemSampler, but is
kept in its own module because the domain is audio-path observability
rather than generic host metrics.

Design constraints:
  - Keep the hot loop cheap. Fan-in STATUS is a local UDS read with a
    short timeout; journal and DBus/Camilla probes run less often.
  - Keep history in memory. /system wants a recent operator view, not
    a long-term metrics database.
  - Fail soft. Observability must never break /system/snapshot or the
    audio path.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 5.0
JOURNAL_INTERVAL_SEC = 30.0
MPRIS_INTERVAL_SEC = 30.0
CAMILLA_INTERVAL_SEC = 30.0
BUCKET_SECONDS = 10.0
HISTORY_SECONDS = 30 * 60.0
EVENT_RING_SIZE = 20

FANIN_SOCKET = "/run/jasper-fanin/control.sock"
FANIN_TIMEOUT_SEC = 1.0
SUBPROCESS_TIMEOUT_SEC = 2.0

# Fan-in's 4096-frame input buffer is load-bearing for AirPlay burst
# absorption. See docs/HANDOFF-airplay.md Pattern A3.
MIN_AIRPLAY_INPUT_BUFFER_FRAMES = 4096

SHAIRPORT_UNIT = "shairport-sync"
CAMILLA_UNIT = "jasper-camilla"


def _empty_bucket(t: float) -> dict[str, Any]:
    return {
        "t": t,
        "shairport_events": 0,
        "shairport_packet_drops": 0,
        "shairport_sync_errors": 0,
        "shairport_underruns": 0,
        "fanin_airplay_xruns": 0,
        "fanin_output_xruns": 0,
        "camilla_short_reads": 0,
        "camilla_playback_underruns": 0,
    }


EVENT_BUCKET_FIELD = {
    "shairport_packet_drop": "shairport_packet_drops",
    "shairport_oos": "shairport_events",
    "shairport_sync_positive": "shairport_sync_errors",
    "shairport_sync_negative": "shairport_sync_errors",
    "shairport_underrun": "shairport_underruns",
    "shairport_broken_pipe": "shairport_events",
    "fanin_airplay_xrun": "fanin_airplay_xruns",
    "fanin_output_xrun": "fanin_output_xruns",
    "camilla_short_read": "camilla_short_reads",
    "camilla_playback_underrun": "camilla_playback_underruns",
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_journal_line(unit: str, line: str) -> dict[str, Any] | None:
    """Classify one journal line into the compact dashboard event shape.

    The patterns are intentionally literal and pinned to the messages
    documented in docs/HANDOFF-airplay.md. Unknown log lines are ignored.
    """
    if unit == SHAIRPORT_UNIT:
        if "Dropping out of date packet" in line:
            lead_time = None
            m = re.search(r"Lead time is ([0-9.]+) seconds", line)
            if m:
                lead_time = _as_float(m.group(1))
            detail = (
                f"lead time {lead_time:.3f}s"
                if lead_time is not None else "out-of-date packet"
            )
            return {
                "type": "shairport_packet_drop",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay packet drop",
                "detail": detail,
                "lead_time_sec": lead_time,
            }
        if "Player: packets out of sequence" in line:
            return {
                "type": "shairport_oos",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay packet order",
                "detail": "packets out of sequence",
            }
        if "Large positive" in line:
            return {
                "type": "shairport_sync_positive",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay sync correction",
                "detail": "large positive sync error",
            }
        if "Large negative" in line:
            return {
                "type": "shairport_sync_negative",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay sync correction",
                "detail": "large negative sync error",
            }
        if "recovering from a previous underrun" in line:
            return {
                "type": "shairport_underrun",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay ALSA underrun",
                "detail": "shairport recovered an underrun",
            }
        if "Broken pipe" in line or "Too much" in line:
            return {
                "type": "shairport_broken_pipe",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay output error",
                "detail": "shairport output transport error",
            }
        return None

    if unit == CAMILLA_UNIT:
        if re.search(r"Capture read \d+ frames instead of", line):
            return {
                "type": "camilla_short_read",
                "subsystem": "camilla",
                "severity": "watch",
                "title": "Camilla short read",
                "detail": "capture delivered fewer frames than requested",
            }
        if (
            "Prepare playback after buffer underrun" in line
            or "playback_underrun" in line
            or "Could not write" in line
            or "Broken pipe" in line
        ):
            return {
                "type": "camilla_playback_underrun",
                "subsystem": "camilla",
                "severity": "issue",
                "title": "Camilla playback underrun",
                "detail": "playback buffer underrun",
            }
    return None


class AirPlayHealthSampler:
    """Background sampler for recent AirPlay health.

    Tests inject probe functions and call _tick() directly. Production
    starts the daemon thread via start().
    """

    def __init__(
        self,
        *,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
        journal_interval_sec: float = JOURNAL_INTERVAL_SEC,
        mpris_interval_sec: float = MPRIS_INTERVAL_SEC,
        camilla_interval_sec: float = CAMILLA_INTERVAL_SEC,
        bucket_seconds: float = BUCKET_SECONDS,
        history_seconds: float = HISTORY_SECONDS,
        fanin_probe: Callable[[], dict[str, Any] | None] | None = None,
        journal_reader: (
            Callable[[str, float, float], list[str]] | None
        ) = None,
        mpris_probe: Callable[[], dict[str, Any] | None] | None = None,
        camilla_probe: Callable[[], dict[str, Any] | None] | None = None,
        camilla_host: str = "127.0.0.1",
        camilla_port: int = 1234,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._sample_interval = sample_interval_sec
        self._journal_interval = journal_interval_sec
        self._mpris_interval = mpris_interval_sec
        self._camilla_interval = camilla_interval_sec
        self._bucket_seconds = bucket_seconds
        self._history_points = max(1, int(math.ceil(history_seconds / bucket_seconds)))
        self._fanin_probe = fanin_probe or self._read_fanin_status
        self._journal_reader = journal_reader or self._read_journal_lines
        self._mpris_probe = mpris_probe or self._read_airplay_mpris
        self._camilla_probe = camilla_probe or (
            lambda: self._read_camilla_state(camilla_host, camilla_port)
        )
        self._time = time_fn

        self._lock = threading.Lock()
        self._buckets: deque[dict[str, Any]] = deque(maxlen=self._history_points)
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_RING_SIZE)
        self._current_fanin: dict[str, Any] | None = None
        self._current_mpris: dict[str, Any] | None = None
        self._current_camilla: dict[str, Any] | None = None
        self._last_sample_at: float | None = None
        self._last_journal_scan_at = 0.0
        self._last_mpris_sample_at = 0.0
        self._last_camilla_sample_at = 0.0
        self._journal_since = {
            SHAIRPORT_UNIT: self._time(),
            CAMILLA_UNIT: self._time(),
        }
        self._last_fanin_counts: dict[str, Any] | None = None

        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="jasper-airplay-health-sampler",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        """For tests; production runs the daemon thread to process exit."""
        self._stopped = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            summary_5m = self._summary_locked(5 * 60.0)
            summary_30m = self._summary_locked(30 * 60.0)
            status, reason = self._status_locked(summary_5m, summary_30m)
            return {
                "sample_interval_sec": self._sample_interval,
                "journal_interval_sec": self._journal_interval,
                "bucket_seconds": self._bucket_seconds,
                "history_points": self._history_points,
                "last_sample_at": self._last_sample_at,
                "status": status,
                "reason": reason,
                "current": {
                    "fanin": copy.deepcopy(self._current_fanin),
                    "mpris": copy.deepcopy(self._current_mpris),
                    "camilla": copy.deepcopy(self._current_camilla),
                },
                "summary_5m": summary_5m,
                "summary_30m": summary_30m,
                "events": [dict(event) for event in self._events],
                "history": self._history_locked(),
            }

    def _run(self) -> None:
        while not self._stopped:
            sample_start = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("airplay health sampler tick failed")
            elapsed = time.monotonic() - sample_start
            time.sleep(max(0.1, self._sample_interval - elapsed))

    def _tick(self) -> None:
        now = self._time()
        self._ensure_bucket(now)
        self._sample_fanin(now)

        if now - self._last_mpris_sample_at >= self._mpris_interval:
            self._sample_mpris(now)
        if now - self._last_camilla_sample_at >= self._camilla_interval:
            self._sample_camilla(now)
        if now - self._last_journal_scan_at >= self._journal_interval:
            self._scan_journals(now)

        with self._lock:
            self._last_sample_at = now

    def _sample_fanin(self, now: float) -> None:
        status = self._fanin_probe()
        if not isinstance(status, dict):
            with self._lock:
                self._current_fanin = None
            return

        inputs = status.get("inputs")
        if not isinstance(inputs, list):
            inputs = []
        airplay = next(
            (inp for inp in inputs if inp.get("label") == "airplay"),
            None,
        )
        output = status.get("output") if isinstance(status.get("output"), dict) else {}
        watchdog = (
            status.get("watchdog")
            if isinstance(status.get("watchdog"), dict) else {}
        )

        airplay_frames = _as_int(airplay.get("frames_read")) if airplay else 0
        airplay_xruns = _as_int(airplay.get("xrun_count")) if airplay else 0
        output_frames = _as_int(output.get("frames_written"))
        output_xruns = _as_int(output.get("xrun_count"))

        prev = self._last_fanin_counts
        airplay_rate: float | None = None
        output_rate: float | None = None
        if prev is not None:
            dt = max(0.001, now - float(prev.get("ts", now)))
            prev_airplay_frames = _as_int(prev.get("airplay_frames"))
            prev_output_frames = _as_int(prev.get("output_frames"))
            if airplay_frames >= prev_airplay_frames:
                airplay_rate = (airplay_frames - prev_airplay_frames) / dt
            if output_frames >= prev_output_frames:
                output_rate = (output_frames - prev_output_frames) / dt

            airplay_delta = airplay_xruns - _as_int(prev.get("airplay_xruns"))
            output_delta = output_xruns - _as_int(prev.get("output_xruns"))
            if airplay_delta > 0:
                self._record_event(
                    now,
                    {
                        "type": "fanin_airplay_xrun",
                        "subsystem": "fanin",
                        "severity": "issue",
                        "title": "AirPlay fan-in xrun",
                        "detail": f"input recovered {airplay_delta} xrun(s)",
                    },
                    count=airplay_delta,
                )
            if output_delta > 0:
                self._record_event(
                    now,
                    {
                        "type": "fanin_output_xrun",
                        "subsystem": "fanin",
                        "severity": "issue",
                        "title": "Fan-in output xrun",
                        "detail": f"output recovered {output_delta} xrun(s)",
                    },
                    count=output_delta,
                )

        self._last_fanin_counts = {
            "ts": now,
            "airplay_frames": airplay_frames,
            "airplay_xruns": airplay_xruns,
            "output_frames": output_frames,
            "output_xruns": output_xruns,
        }

        input_buffer_frames = _as_int(status.get("input_buffer_frames"))
        output_buffer_frames = _as_int(output.get("buffer_frames"))
        current = {
            "available": True,
            "input_buffer_frames": input_buffer_frames,
            "output_buffer_frames": output_buffer_frames,
            "selected_input": status.get("selected_input"),
            "airplay": {
                "present": airplay is not None,
                "frames_read": airplay_frames,
                "frames_per_sec": (
                    round(airplay_rate, 1)
                    if airplay_rate is not None else None
                ),
                "xrun_count": airplay_xruns,
            },
            "output": {
                "frames_written": output_frames,
                "frames_per_sec": (
                    round(output_rate, 1)
                    if output_rate is not None else None
                ),
                "xrun_count": output_xruns,
                "sample_rate": _as_int(output.get("sample_rate")),
                "period_frames": _as_int(output.get("period_frames")),
                "buffer_frames": _as_int(output.get("buffer_frames")),
            },
            "watchdog": {
                "last_progress_age_ms": _as_int(
                    watchdog.get("last_progress_age_ms"),
                ),
                "pings_skipped": _as_int(watchdog.get("pings_skipped")),
            },
        }
        with self._lock:
            self._current_fanin = current

    def _sample_mpris(self, now: float) -> None:
        try:
            current = self._mpris_probe()
        except Exception:  # noqa: BLE001
            logger.debug("airplay MPRIS probe failed", exc_info=True)
            current = None
        with self._lock:
            self._current_mpris = current if isinstance(current, dict) else None
        self._last_mpris_sample_at = now

    def _sample_camilla(self, now: float) -> None:
        try:
            current = self._camilla_probe()
        except Exception:  # noqa: BLE001
            logger.debug("camilla state probe failed", exc_info=True)
            current = None
        with self._lock:
            self._current_camilla = current if isinstance(current, dict) else None
        self._last_camilla_sample_at = now

    def _scan_journals(self, now: float) -> None:
        for unit in (SHAIRPORT_UNIT, CAMILLA_UNIT):
            since = self._journal_since.get(unit, now)
            try:
                lines = self._journal_reader(unit, since, now)
            except Exception:  # noqa: BLE001
                logger.debug("journal scan failed for %s", unit, exc_info=True)
                lines = []
            for line in lines:
                event = classify_journal_line(unit, line)
                if event is not None:
                    self._record_event(now, event)
            self._journal_since[unit] = now
        self._last_journal_scan_at = now

    def _record_event(
        self,
        ts: float,
        event: dict[str, Any],
        *,
        count: int = 1,
    ) -> None:
        bucket = self._ensure_bucket(ts)
        event_type = str(event.get("type", "unknown"))
        field = EVENT_BUCKET_FIELD.get(event_type)
        with self._lock:
            if field:
                bucket[field] = _as_int(bucket.get(field)) + count
                if (
                    event.get("subsystem") == "shairport"
                    and field != "shairport_events"
                ):
                    bucket["shairport_events"] = (
                        _as_int(bucket.get("shairport_events")) + count
                    )
            item = {
                "ts": ts,
                "type": event_type,
                "subsystem": event.get("subsystem", "unknown"),
                "severity": event.get("severity", "watch"),
                "title": event.get("title", event_type),
                "detail": event.get("detail", ""),
                "count": count,
            }
            if event.get("lead_time_sec") is not None:
                item["lead_time_sec"] = event["lead_time_sec"]
            self._events.append(item)

    def _ensure_bucket(self, ts: float) -> dict[str, Any]:
        bucket_t = math.floor(ts / self._bucket_seconds) * self._bucket_seconds
        with self._lock:
            if not self._buckets or self._buckets[-1]["t"] != bucket_t:
                self._buckets.append(_empty_bucket(bucket_t))
            return self._buckets[-1]

    def _summary_locked(self, window_sec: float) -> dict[str, int]:
        cutoff = self._time() - window_sec
        totals = {
            "shairport_events": 0,
            "shairport_packet_drops": 0,
            "shairport_sync_errors": 0,
            "shairport_underruns": 0,
            "fanin_airplay_xruns": 0,
            "fanin_output_xruns": 0,
            "camilla_short_reads": 0,
            "camilla_playback_underruns": 0,
        }
        for bucket in self._buckets:
            if float(bucket.get("t", 0.0)) + self._bucket_seconds < cutoff:
                continue
            for key in totals:
                totals[key] += _as_int(bucket.get(key))
        return totals

    def _status_locked(
        self,
        summary_5m: dict[str, int],
        summary_30m: dict[str, int],
    ) -> tuple[str, str]:
        fanin = self._current_fanin
        if fanin is None:
            return "unknown", "fan-in status unavailable"

        if _as_int(fanin.get("input_buffer_frames")) < MIN_AIRPLAY_INPUT_BUFFER_FRAMES:
            return "issue", "fan-in input buffer below 4096 frames"

        watchdog = fanin.get("watchdog", {})
        if isinstance(watchdog, dict):
            progress_age = _as_int(watchdog.get("last_progress_age_ms"))
            skipped = _as_int(watchdog.get("pings_skipped"))
            if progress_age > 3000 or skipped > 0:
                return "issue", "fan-in watchdog stale"

        if (
            summary_5m["shairport_packet_drops"] > 0
            or summary_5m["shairport_sync_errors"] > 0
            or summary_5m["shairport_underruns"] > 0
            or summary_5m["camilla_playback_underruns"] > 0
            or summary_5m["fanin_airplay_xruns"] > 0
            or summary_5m["fanin_output_xruns"] > 0
        ):
            return "issue", "recent audio-path recovery event"

        mpris = self._current_mpris or {}
        airplay = fanin.get("airplay", {})
        airplay_rate = (
            _as_float(airplay.get("frames_per_sec"))
            if isinstance(airplay, dict) else None
        )
        if mpris.get("playing") is True and airplay_rate is not None:
            if airplay_rate < 1000.0:
                return "issue", "MPRIS playing but fan-in is not receiving frames"
        if mpris.get("playing") is True and airplay_rate is None:
            return "unknown", "waiting for fan-in frame-rate baseline"

        if (
            summary_30m["shairport_events"] > 0
            or summary_30m["fanin_airplay_xruns"] > 0
            or summary_30m["fanin_output_xruns"] > 0
            or summary_5m["camilla_short_reads"] > 0
            or summary_30m["camilla_playback_underruns"] > 0
        ):
            return "watch", "recent non-fatal audio-path warning"

        if mpris.get("playing") is False and (
            airplay_rate is None or airplay_rate < 1000.0
        ):
            return "inactive", "AirPlay not currently streaming"
        if airplay_rate is None:
            return "unknown", "waiting for fan-in frame-rate baseline"
        if airplay_rate is not None and airplay_rate < 1000.0:
            return "inactive", "AirPlay not currently streaming"
        return "ok", "AirPlay path clean"

    def _history_locked(self) -> dict[str, list[Any]]:
        keys = list(_empty_bucket(0.0).keys())
        return {
            key: [bucket.get(key, 0) for bucket in self._buckets]
            for key in keys
        }

    @staticmethod
    def _read_fanin_status(
        socket_path: str = FANIN_SOCKET,
        timeout_sec: float = FANIN_TIMEOUT_SEC,
    ) -> dict[str, Any] | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_sec)
                sock.connect(socket_path)
                sock.sendall(b"STATUS\n")
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
            return None
        try:
            return json.loads(b"".join(chunks).decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _read_journal_lines(unit: str, since: float, now: float) -> list[str]:
        try:
            proc = subprocess.run(
                [
                    "journalctl",
                    "-u", unit,
                    "--since", f"@{since:.3f}",
                    "--until", f"@{now:.3f}",
                    "--no-pager",
                    "-o", "cat",
                ],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode not in (0, 1):
            return []
        return proc.stdout.splitlines()

    @staticmethod
    def _read_airplay_mpris() -> dict[str, Any] | None:
        try:
            from ..source_state import airplay_playing
            playing = asyncio.run(airplay_playing())
        except Exception:  # noqa: BLE001
            return None
        return {"playing": bool(playing)}

    @staticmethod
    def _read_camilla_state(host: str, port: int) -> dict[str, Any] | None:
        try:
            from camilladsp import CamillaClient

            client = CamillaClient(host, port)
            client.connect()
            try:
                return {
                    "buffer_level": _as_int(client.query("GetBufferLevel")),
                    "rate_adjust": _as_float(client.query("GetRateAdjust")),
                    "capture_rate": _as_int(client.query("GetCaptureRate")),
                }
            finally:
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            return None
