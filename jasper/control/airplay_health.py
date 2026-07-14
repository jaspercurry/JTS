# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import datetime
import json
import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from jasper.log_event import log_event
from jasper.music_sources import MUSIC_SOURCE_SPECS

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 5.0
JOURNAL_INTERVAL_SEC = 30.0
MPRIS_INTERVAL_SEC = 30.0
CAMILLA_INTERVAL_SEC = 30.0
BUCKET_SECONDS = 10.0
HISTORY_SECONDS = 30 * 60.0
EVENT_RING_SIZE = 20

# Boot warmup: suppress transient audio-path event RECORDING for the
# first DEFAULT_WARMUP_SEC after the sampler starts (~ jasper-control
# start ~ boot). A reboot's content-xrun + AirPlay-resync settling would
# otherwise flip the dashboard straight to "issue: recent audio-path
# recovery event". Mirrors the cold_start gate in system_supervisor
# (120 s) / shairport_supervisor (60 s). Sustained/real problems still
# surface after the window (doctor + persistent counters unaffected).
DEFAULT_WARMUP_SEC = 120.0
# Per-session grace armed when AirPlay transitions idle->active: the
# PTP-anchor settle at session establish emits expected sync-correction
# / out-of-sequence bursts. >= JOURNAL_INTERVAL_SEC so the next 30 s
# journal scan after a connect is covered.
DEFAULT_CONNECT_GRACE_SEC = 45.0

FANIN_SOCKET = "/run/jasper-fanin/control.sock"
FANIN_TIMEOUT_SEC = 1.0
SUBPROCESS_TIMEOUT_SEC = 2.0
MAINTENANCE_SUPPRESS_UNTIL_PATH = "/run/jasper-airplay-health-suppress-until"

# Fan-in's 4096-frame input buffer is load-bearing for AirPlay burst
# absorption. See docs/HANDOFF-airplay.md Pattern A3.
MIN_AIRPLAY_INPUT_BUFFER_FRAMES = 4096

SHAIRPORT_UNIT = "shairport-sync"
CAMILLA_UNIT = "jasper-camilla"
CAMILLA_SHORT_READ_RE = re.compile(
    r"Capture read (?P<read>\d+) frames instead of the requested (?P<requested>\d+)",
)

# CamillaDSP logs a warning for any partial ALSA read, then immediately loops to
# read the remaining frames before emitting the chunk. Tiny recovered partials
# are normal with the plug/dsnoop/rate-adjust path and do not indicate an
# audio-path recovery event by themselves.
BENIGN_CAMILLA_SHORT_READ_DEFICIT_RATIO = 0.01


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
    "shairport_offset_too_short": "shairport_events",
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
        # shairport could not fully apply the backend latency offset — the
        # configured offset exceeds the sender's negotiated AP2 latency
        # budget, so output plays late. The classifier has no bond context,
        # so the detail states only the fact shairport reported; the expected
        # trigger is a bonded LEADER whose Snapcast round-trip pushes the
        # offset past a tight budget (the proactive, bond-aware diagnosis +
        # remediation lives in jasper/multiroom/airplay_latency.py + the
        # grouping doctor check). Matches the stable substring of shairport's
        # warning ("... it too short to accommodate an offset ..." — the "it"
        # is shairport's own typo).
        if "too short to accommodate an offset" in line:
            return {
                "type": "shairport_offset_too_short",
                "subsystem": "shairport",
                "severity": "issue",
                "title": "AirPlay latency budget too short",
                "detail": (
                    "configured offset exceeds the sender's AirPlay latency "
                    "budget — output plays late"
                ),
            }
        return None

    if unit == CAMILLA_UNIT:
        m = CAMILLA_SHORT_READ_RE.search(line)
        if m:
            frames_read = _as_int(m.group("read"))
            frames_requested = _as_int(m.group("requested"))
            deficit = max(0, frames_requested - frames_read)
            if frames_requested > 0:
                benign_deficit = math.ceil(
                    frames_requested * BENIGN_CAMILLA_SHORT_READ_DEFICIT_RATIO,
                )
                if deficit <= benign_deficit:
                    return None
            return {
                "type": "camilla_short_read",
                "subsystem": "camilla",
                "severity": "watch",
                "title": "Camilla short read",
                "detail": (
                    f"capture delivered {frames_read}/{frames_requested} "
                    "frames"
                ),
                "frames_read": frames_read,
                "frames_requested": frames_requested,
                "deficit_frames": deficit,
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


# ---------------------------------------------------------------------------
# Storm-triggered forensic capture (Tier 1 onset/offset events + Tier 2
# in-storm controller trajectory).
#
# A "storm" is a sustained run of *material* Camilla short reads — the
# rate-adjust PI loop hunting against a drifting DAC clock (the Apple USB-C
# dongle). It is INAUDIBLE: across 137k short reads over 72 h of real use it
# produced zero playback underruns, because CamillaDSP loops to fill every
# short read before emitting the chunk. But it is slow-developing (tens of
# minutes into a listening session), intermittent, and metastable (a config
# reload / restart clears it), so it cannot be reproduced on demand. These
# hooks capture the rate-controller state WHEN IT ACTUALLY HAPPENS so the
# mechanism and any future at-source tuning can be evaluated from real data
# instead of reconstructed after the fact. See docs/HANDOFF-airplay.md.
STORM_ENTER_PER_MIN = 120.0
STORM_EXIT_PER_MIN = 30.0
STORM_EXIT_DEBOUNCE_SEC = 90.0
STORM_MIN_SCAN_WINDOW_SEC = 15.0
STORM_SAMPLE_INTERVAL_SEC = 5.0
STORM_TRAJECTORY_DIR = "/var/lib/jasper/rate-storms"
STORM_TRAJECTORY_MAX_ROWS = 4000
STORM_TRAJECTORY_KEEP_FILES = 20

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
CPU_GOVERNOR_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
CPU_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"
CAMILLA_UNIT_FULL = "jasper-camilla.service"
BUILD_MARKER_PATH = "/var/lib/jasper/build.txt"


def _read_int_file(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_text_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _read_soc_temp_c() -> float | None:
    raw = _read_int_file(THERMAL_ZONE_PATH)
    return round(raw / 1000.0, 1) if raw is not None else None


def _seconds_since_camilla_restart() -> float | None:
    """Seconds since jasper-camilla last (re)started — the controller-reset age.

    A camilla restart resets the rate controller, so "did this storm start
    shortly after a restart/deploy?" is the field that settles whether
    restarts SEED storms (vs only clearing them — the open question from the
    deploy-correlation investigation). Read-only ``systemctl show`` needs no
    privilege; bounded + fail-soft.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", CAMILLA_UNIT_FULL,
             "-p", "ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    if "=" not in out:
        return None
    try:
        started_us = int(out.split("=", 1)[1])
    except ValueError:
        return None
    if started_us <= 0:
        return None
    try:
        now_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
    except (OSError, AttributeError):
        return None
    return round(max(0.0, (now_us - started_us) / 1e6), 1)


def _seconds_since_deploy(now_wall: float) -> float | None:
    """Seconds since the last install wrote the build marker — the deploy age."""
    try:
        mtime = os.stat(BUILD_MARKER_PATH).st_mtime
    except OSError:
        return None
    return round(max(0.0, now_wall - mtime), 1)


def _default_context_probe(now_wall: float) -> dict[str, Any]:
    """Cheap correlation context captured once at storm onset."""
    return {
        "soc_temp_c": _read_soc_temp_c(),
        "cpu_governor": _read_text_file(CPU_GOVERNOR_PATH),
        "cpu_freq_khz": _read_int_file(CPU_FREQ_PATH),
        "sec_since_camilla_restart": _seconds_since_camilla_restart(),
        "sec_since_deploy": _seconds_since_deploy(now_wall),
    }


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


class _StormTrajectory:
    """Bounded CSV artifact for one storm's controller trajectory (Tier 2).

    Fail-soft by construction: any directory/open/write error leaves ``path``
    None and makes every method a no-op, so a filesystem problem never
    disturbs the sampler loop or the Tier-1 onset/offset events. Capped per
    storm (``max_rows``) and across storms (``keep_files`` retained, oldest
    pruned), mirroring the wake-events ring.
    """

    _HEADER = (
        "t_sec", "rate_adjust", "capture_rate", "buffer_level",
        "soc_temp_c", "cpu_freq_khz", "material_per_min",
    )

    def __init__(
        self, dir_path: str | None, onset_stamp: str, *,
        max_rows: int, keep_files: int,
    ) -> None:
        self.path: str | None = None
        self._fh: Any = None
        self._rows = 0
        self._max_rows = max_rows
        if not dir_path:
            return
        try:
            os.makedirs(dir_path, exist_ok=True)
            self._prune(dir_path, keep_files)
            path = os.path.join(dir_path, f"storm-{onset_stamp}.csv")
            fh = open(path, "w", encoding="utf-8")
            fh.write(",".join(self._HEADER) + "\n")
            fh.flush()
            self._fh = fh
            self.path = path
        except OSError:
            logger.debug("storm trajectory open failed", exc_info=True)
            self._fh = None
            self.path = None

    @property
    def rows(self) -> int:
        return self._rows

    def append(self, row: dict[str, Any]) -> None:
        if self._fh is None or self._rows >= self._max_rows:
            return
        try:
            self._fh.write(
                ",".join(_csv_cell(row.get(k)) for k in self._HEADER) + "\n"
            )
            self._fh.flush()
            self._rows += 1
        except OSError:
            logger.debug("storm trajectory append failed", exc_info=True)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None

    @staticmethod
    def _prune(dir_path: str, keep_files: int) -> None:
        try:
            existing = [
                os.path.join(dir_path, name)
                for name in os.listdir(dir_path)
                if name.startswith("storm-") and name.endswith(".csv")
            ]
        except OSError:
            return
        existing.sort(key=_safe_mtime)
        # Keep room for the file about to be opened: retain keep_files-1.
        cutoff = max(0, len(existing) - max(0, keep_files - 1))
        for old in existing[:cutoff]:
            try:
                os.remove(old)
            except OSError:
                pass


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
        maintenance_suppress_path: str | None = MAINTENANCE_SUPPRESS_UNTIL_PATH,
        warmup_sec: float = DEFAULT_WARMUP_SEC,
        connect_grace_sec: float = DEFAULT_CONNECT_GRACE_SEC,
        storm_enter_per_min: float = STORM_ENTER_PER_MIN,
        storm_exit_per_min: float = STORM_EXIT_PER_MIN,
        storm_exit_debounce_sec: float = STORM_EXIT_DEBOUNCE_SEC,
        storm_sample_interval_sec: float = STORM_SAMPLE_INTERVAL_SEC,
        trajectory_dir: str | None = STORM_TRAJECTORY_DIR,
        trajectory_max_rows: int = STORM_TRAJECTORY_MAX_ROWS,
        trajectory_keep_files: int = STORM_TRAJECTORY_KEEP_FILES,
        context_probe: Callable[[], dict[str, Any]] | None = None,
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
        self._maintenance_suppress_path = maintenance_suppress_path
        self._time = time_fn
        # Warmup / connect-grace suppression (see DEFAULT_*_SEC above).
        self._warmup_sec = warmup_sec
        self._connect_grace_sec = connect_grace_sec
        self._started_at = time_fn()
        self._connect_grace_until: float | None = None
        self._airplay_active = False
        self._warmup_active = warmup_sec > 0.0
        self._suppressed_reason: str | None = None

        # Storm-triggered forensic capture (Tier 1/2). Config + live state.
        self._storm_enter_per_min = storm_enter_per_min
        self._storm_exit_per_min = storm_exit_per_min
        self._storm_exit_debounce_sec = storm_exit_debounce_sec
        self._storm_sample_interval = storm_sample_interval_sec
        self._trajectory_dir = trajectory_dir
        self._trajectory_max_rows = trajectory_max_rows
        self._trajectory_keep_files = trajectory_keep_files
        self._context_probe = context_probe or (
            lambda: _default_context_probe(self._time())
        )
        self._storming = False
        self._storm_started_at: float | None = None
        self._storm_peak_per_min = 0.0
        self._storm_below_exit_since: float | None = None
        self._storm_onset: dict[str, Any] | None = None
        self._storm_trajectory: _StormTrajectory | None = None
        self._storm_extent: dict[str, Any] = {}
        self._storm_samples = 0
        self._storm_count = 0
        self._last_material_per_min = 0.0

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
        self._maintenance_suppressed = False
        self._maintenance_suppressed_until: float | None = None

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

    def sample_once(self) -> None:
        """Take one sample without starting this collector's own thread.

        The speaker-wide audio-health sampler composes this AirPlay-specific
        collector and calls it from the one existing monitoring loop.
        """
        self._tick()

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
                "maintenance_suppressed": self._maintenance_suppressed,
                "maintenance_suppressed_until": self._maintenance_suppressed_until,
                "warmup_active": self._warmup_active,
                "connect_grace_until": self._connect_grace_until,
                "suppressed_reason": self._suppressed_reason,
                "status": status,
                "reason": reason,
                "current": {
                    "fanin": copy.deepcopy(self._current_fanin),
                    "mpris": copy.deepcopy(self._current_mpris),
                    "camilla": copy.deepcopy(self._current_camilla),
                },
                "summary_5m": summary_5m,
                "summary_30m": summary_30m,
                "storm": {
                    "active": self._storming,
                    "count": self._storm_count,
                    "started_at": self._storm_started_at,
                    "material_per_min": round(self._last_material_per_min, 1),
                    "peak_per_min": (
                        round(self._storm_peak_per_min, 1)
                        if self._storming else None
                    ),
                    "samples": self._storm_samples if self._storming else 0,
                    "onset": copy.deepcopy(self._storm_onset),
                },
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
        suppress_until = self._read_maintenance_suppress_until(now)
        within_warmup = (now - self._started_at) < self._warmup_sec
        in_connect_grace = (
            self._connect_grace_until is not None
            and now < self._connect_grace_until
        )
        # Base suppression gates this tick's fan-in xrun recording.
        suppress_base = (
            suppress_until is not None or within_warmup or in_connect_grace
        )
        self._ensure_bucket(now)
        self._sample_fanin(now, suppress_events=suppress_base)

        if now - self._last_mpris_sample_at >= self._mpris_interval:
            self._sample_mpris(now)
        # While storming, sample Camilla at the faster cadence and append a
        # trajectory row each time (Tier 2). Steady-state cadence is unchanged.
        camilla_interval = (
            self._storm_sample_interval if self._storming else self._camilla_interval
        )
        if now - self._last_camilla_sample_at >= camilla_interval:
            self._sample_camilla(now)
            if self._storming:
                self._append_trajectory(now)

        # Arm a per-session grace when AirPlay transitions idle->active.
        # The PTP-anchor settle at session establish emits expected
        # sync-correction / out-of-sequence bursts; suppressing event
        # *recording* (not just classification) keeps the 5m/30m windows
        # clean, same as the boot warmup. Detected after sampling so the
        # freshly-armed grace also covers THIS tick's journal scan.
        active = self._airplay_active_now()
        if active and not self._airplay_active:
            self._connect_grace_until = now + self._connect_grace_sec
        self._airplay_active = active
        in_connect_grace = (
            self._connect_grace_until is not None
            and now < self._connect_grace_until
        )
        suppress_events = suppress_base or in_connect_grace

        if suppress_events:
            self._advance_journal_cursors(now)
        elif now - self._last_journal_scan_at >= self._journal_interval:
            self._scan_journals(now)

        if suppress_until is not None:
            reason: str | None = "maintenance"
        elif within_warmup:
            reason = "warmup"
        elif in_connect_grace:
            reason = "airplay_connect"
        else:
            reason = None

        with self._lock:
            self._last_sample_at = now
            # Keep maintenance_suppressed meaning the maintenance FILE
            # only (existing consumer semantics); warmup/connect surface
            # via suppressed_reason / warmup_active below.
            self._maintenance_suppressed = suppress_until is not None
            self._maintenance_suppressed_until = suppress_until
            self._warmup_active = within_warmup
            self._suppressed_reason = reason

    def _airplay_streaming(self) -> bool | None:
        """Authoritative "is a sender streaming?" — shairport's MPRIS
        PlaybackStatus. Single source of truth shared by the dashboard
        status (`_status_locked`) and the connect-grace
        (`_airplay_active_now`).

        NOT the fan-in frame rate: the airplay input lane free-runs at
        ~48 kHz of SILENCE whenever the pipeline is up (fan-in clocks every
        lane off the always-on DAC loop), so the rate reads "active" even at
        idle. Returns ``True``/``False``, or ``None`` when the MPRIS probe is
        unavailable — so callers can tell idle from unknown. Freshness is
        bounded by the MPRIS sample interval (~30 s).
        """
        mpris = self._current_mpris if isinstance(self._current_mpris, dict) else None
        if not mpris:
            return None
        playing = mpris.get("playing")
        return playing if isinstance(playing, bool) else None

    def _airplay_active_now(self) -> bool:
        """Whether a sender is actively streaming, for arming the connect
        grace. Keyed on `_airplay_streaming()` (shairport MPRIS) — never the
        always-on silent frame rate — so the idle->active transition the
        grace watches reflects a real session start, not the pipeline simply
        coming up. Detection therefore lags up to one MPRIS sample interval;
        the boot warmup is the primary post-restart smoother, the connect
        grace a best-effort session-establish one.
        """
        return self._airplay_streaming() is True

    def _sample_fanin(self, now: float, *, suppress_events: bool = False) -> None:
        status = self._fanin_probe()
        if not isinstance(status, dict):
            with self._lock:
                self._current_fanin = None
            return

        inputs = status.get("inputs")
        if not isinstance(inputs, list):
            inputs = []
        inputs_by_label = {
            entry.get("label"): entry
            for entry in inputs
            if isinstance(entry, dict) and isinstance(entry.get("label"), str)
        }
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
        input_rates: dict[str, float | None] = {
            spec.id.value: None for spec in MUSIC_SOURCE_SPECS
        }
        input_frames = {
            spec.id.value: (
                _as_int(inputs_by_label[spec.fanin_label].get("frames_read"))
                if spec.fanin_label in inputs_by_label else 0
            )
            for spec in MUSIC_SOURCE_SPECS
        }
        if prev is not None:
            dt = max(0.001, now - float(prev.get("ts", now)))
            prev_airplay_frames = _as_int(prev.get("airplay_frames"))
            prev_output_frames = _as_int(prev.get("output_frames"))
            if airplay_frames >= prev_airplay_frames:
                airplay_rate = (airplay_frames - prev_airplay_frames) / dt
            if output_frames >= prev_output_frames:
                output_rate = (output_frames - prev_output_frames) / dt
            previous_inputs = prev.get("input_frames")
            if isinstance(previous_inputs, Mapping):
                for source_id, frames in input_frames.items():
                    previous_frames = _as_int(previous_inputs.get(source_id))
                    if frames >= previous_frames:
                        input_rates[source_id] = (frames - previous_frames) / dt

            airplay_delta = airplay_xruns - _as_int(prev.get("airplay_xruns"))
            output_delta = output_xruns - _as_int(prev.get("output_xruns"))
            if airplay_delta > 0 and not suppress_events:
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
            if output_delta > 0 and not suppress_events:
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
            "input_frames": input_frames,
        }

        input_buffer_frames = _as_int(status.get("input_buffer_frames"))
        output_buffer_frames = _as_int(output.get("buffer_frames"))
        # Fixed-shape, source-neutral observations for the outer audio-health
        # composer. Keep only what explains health; /state retains the full
        # fan-in STATUS for deep debugging. Every declared source gets a slot,
        # even when its lane is absent, so adding a source extends the existing
        # metadata seam rather than another dashboard conditional.
        input_observations: dict[str, dict[str, Any]] = {}
        for spec in MUSIC_SOURCE_SPECS:
            entry = inputs_by_label.get(spec.fanin_label)
            resampler = (
                entry.get("resampler")
                if isinstance(entry, dict)
                and isinstance(entry.get("resampler"), dict)
                else None
            )
            direct = (
                entry.get("direct")
                if isinstance(entry, dict)
                and isinstance(entry.get("direct"), dict)
                else None
            )
            input_observations[spec.id.value] = {
                "label": spec.fanin_label,
                "present": isinstance(entry, dict),
                "source": entry.get("source") if isinstance(entry, dict) else None,
                "frames_read": (
                    _as_int(entry.get("frames_read"))
                    if isinstance(entry, dict) else 0
                ),
                "frames_per_sec": (
                    round(input_rates[spec.id.value], 1)
                    if input_rates[spec.id.value] is not None else None
                ),
                "xrun_count": (
                    _as_int(entry.get("xrun_count"))
                    if isinstance(entry, dict) else 0
                ),
                "rms_dbfs": (
                    _as_float(entry.get("rms_dbfs"))
                    if isinstance(entry, dict) else None
                ),
                "muted": (
                    entry.get("muted")
                    if isinstance(entry, dict)
                    and isinstance(entry.get("muted"), bool)
                    else None
                ),
                "health": direct.get("health") if direct is not None else None,
                "resampler": (
                    {
                        "health": resampler.get("health"),
                        "locked": resampler.get("locked"),
                        "fill_frames": resampler.get("fill_frames"),
                        "target_fill_frames": resampler.get("target_fill_frames"),
                    }
                    if resampler is not None else None
                ),
            }
        current = {
            "available": True,
            "input_buffer_frames": input_buffer_frames,
            "output_buffer_frames": output_buffer_frames,
            "selected_input": status.get("selected_input"),
            "inputs": input_observations,
            "host_clock": (
                copy.deepcopy(status.get("host_clock"))
                if isinstance(status.get("host_clock"), dict)
                else None
            ),
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

    def _advance_journal_cursors(self, now: float) -> None:
        for unit in (SHAIRPORT_UNIT, CAMILLA_UNIT):
            self._journal_since[unit] = max(self._journal_since.get(unit, now), now)
        self._last_journal_scan_at = now

    def _scan_journals(self, now: float) -> None:
        scan_window = (
            now - self._last_journal_scan_at if self._last_journal_scan_at else 0.0
        )
        material_short_reads = 0
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
                    if event.get("type") == "camilla_short_read":
                        material_short_reads += 1
            self._journal_since[unit] = now
        self._last_journal_scan_at = now
        material_per_min = (
            material_short_reads / scan_window * 60.0 if scan_window > 0 else 0.0
        )
        self._update_storm_state(now, material_per_min, scan_window)

    # ---- storm-triggered forensic capture (Tier 1 + Tier 2) ----

    def _update_storm_state(
        self, now: float, material_per_min: float, scan_window: float,
    ) -> None:
        """Edge-detect the rate-loop short-read storm.

        Enter on a sustained material-short-read rate (guarded by a minimum
        scan window so a tiny-window rate spike can't false-trigger); exit on
        a debounced drop below the hysteresis floor. Fail-soft: any error here
        is observability-only and must never perturb the sampler.
        """
        self._last_material_per_min = material_per_min
        try:
            if not self._storming:
                if (
                    scan_window >= STORM_MIN_SCAN_WINDOW_SEC
                    and material_per_min >= self._storm_enter_per_min
                ):
                    self._enter_storm(now, material_per_min)
                return
            if material_per_min > self._storm_peak_per_min:
                self._storm_peak_per_min = material_per_min
            if material_per_min < self._storm_exit_per_min:
                if self._storm_below_exit_since is None:
                    self._storm_below_exit_since = now
                elif now - self._storm_below_exit_since >= self._storm_exit_debounce_sec:
                    self._exit_storm(now)
            else:
                self._storm_below_exit_since = None
        except Exception:  # noqa: BLE001
            logger.debug("storm state update failed", exc_info=True)

    def _enter_storm(self, now: float, material_per_min: float) -> None:
        # Build the onset snapshot BEFORE committing storm state, so a context
        # probe failure can't leave a half-entered storm (no onset event /
        # trajectory but `_storming` latched True). Any raise here propagates
        # to _update_storm_state's single guard before state is touched.
        context = self._safe_context()
        cam = self._current_camilla if isinstance(self._current_camilla, dict) else {}
        onset: dict[str, Any] = {
            "material_per_min": round(material_per_min, 1),
            "rate_adjust": cam.get("rate_adjust"),
            "capture_rate": cam.get("capture_rate"),
            "buffer_level": cam.get("buffer_level"),
            "active_source": self._active_source_hint(),
            **context,
        }
        self._storming = True
        self._storm_started_at = now
        self._storm_peak_per_min = material_per_min
        self._storm_below_exit_since = None
        self._storm_samples = 0
        self._storm_extent = {}
        self._storm_count += 1
        self._storm_onset = onset
        log_event(
            logger, "camilla_rate.storm_onset",
            level=logging.WARNING, fields=onset,
        )
        self._storm_trajectory = _StormTrajectory(
            self._trajectory_dir, self._storm_stamp(now),
            max_rows=self._trajectory_max_rows,
            keep_files=self._trajectory_keep_files,
        )
        # Row 0 captures the onset state itself.
        self._append_trajectory(now)

    def _exit_storm(self, now: float) -> None:
        duration = now - (self._storm_started_at or now)
        ext = self._storm_extent
        traj = self._storm_trajectory
        offset: dict[str, Any] = {
            "duration_sec": round(duration, 1),
            "peak_per_min": round(self._storm_peak_per_min, 1),
            "samples": self._storm_samples,
            "rate_adjust_min": ext.get("rate_adjust_min"),
            "rate_adjust_max": ext.get("rate_adjust_max"),
            "buffer_min": ext.get("buffer_min"),
            "buffer_max": ext.get("buffer_max"),
            "soc_temp_start_c": ext.get("soc_temp_start"),
            "soc_temp_end_c": ext.get("soc_temp_end"),
            "artifact": traj.path if traj is not None else None,
        }
        log_event(
            logger, "camilla_rate.storm_offset",
            level=logging.WARNING, fields=offset,
        )
        if traj is not None:
            traj.close()
        self._storm_trajectory = None
        self._storming = False
        self._storm_started_at = None
        self._storm_below_exit_since = None
        self._storm_onset = None

    def _append_trajectory(self, now: float) -> None:
        cam = self._current_camilla if isinstance(self._current_camilla, dict) else {}
        rate_adjust = cam.get("rate_adjust")
        buffer_level = cam.get("buffer_level")
        soc_temp = _read_soc_temp_c()
        row = {
            "t_sec": round(now - (self._storm_started_at or now), 1),
            "rate_adjust": rate_adjust,
            "capture_rate": cam.get("capture_rate"),
            "buffer_level": buffer_level,
            "soc_temp_c": soc_temp,
            "cpu_freq_khz": _read_int_file(CPU_FREQ_PATH),
            "material_per_min": round(self._last_material_per_min, 1),
        }
        ext = self._storm_extent
        if isinstance(rate_adjust, (int, float)):
            ext["rate_adjust_min"] = min(ext.get("rate_adjust_min", rate_adjust), rate_adjust)
            ext["rate_adjust_max"] = max(ext.get("rate_adjust_max", rate_adjust), rate_adjust)
        if isinstance(buffer_level, (int, float)):
            ext["buffer_min"] = min(ext.get("buffer_min", buffer_level), buffer_level)
            ext["buffer_max"] = max(ext.get("buffer_max", buffer_level), buffer_level)
        if soc_temp is not None:
            ext.setdefault("soc_temp_start", soc_temp)
            ext["soc_temp_end"] = soc_temp
        self._storm_samples += 1
        if self._storm_trajectory is not None:
            self._storm_trajectory.append(row)

    def _safe_context(self) -> dict[str, Any]:
        # The default probe is internally fail-soft (every reader returns None
        # on error) and an injected probe is test-controlled, so this need not
        # catch: any unexpected raise propagates to _update_storm_state's guard,
        # the single "forensics never break the sampler" backstop.
        ctx = self._context_probe()
        return ctx if isinstance(ctx, dict) else {}

    def _active_source_hint(self) -> str | None:
        fanin = self._current_fanin if isinstance(self._current_fanin, dict) else {}
        selected = fanin.get("selected_input")
        if selected:
            return str(selected)
        return "airplay" if self._airplay_streaming() else None

    @staticmethod
    def _storm_stamp(ts: float) -> str:
        """Filesystem-safe UTC stamp for the trajectory artifact name."""
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")

    def _read_maintenance_suppress_until(self, now: float) -> float | None:
        path = self._maintenance_suppress_path
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                suppress_until = float(f.read().strip())
        except (FileNotFoundError, OSError, ValueError):
            return None
        if suppress_until <= now:
            return None
        return suppress_until

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
            if progress_age > 5000:
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

        # Is AirPlay actually streaming? Use the authoritative MPRIS signal
        # (`_airplay_streaming`) — NOT the fan-in frame rate, which free-runs
        # ~48 kHz of silence whenever the pipeline is up and so reads
        # "active" even at idle. The frame rate is only a corroborating
        # fault check once we know audio *should* be flowing. See
        # docs/HANDOFF-airplay.md.
        mpris_playing = self._airplay_streaming()
        airplay = fanin.get("airplay", {})
        airplay_rate = (
            _as_float(airplay.get("frames_per_sec"))
            if isinstance(airplay, dict) else None
        )

        if mpris_playing is False:
            # Nothing streaming. Idle-pipeline artifacts — benign Camilla
            # short reads, content EAGAIN, the silent 48 kHz frame flow —
            # must NOT escalate to "watch"/"ok": they are the always-on
            # loopback clocking silence, not anything a listener can hear.
            # Returning here, before the non-fatal-warning branch below,
            # keeps an idle speaker reading "inactive".
            return "inactive", "AirPlay not currently streaming"
        if mpris_playing is None:
            # shairport PlaybackStatus unavailable (probe error / before the
            # first MPRIS sample). The silent free-running rate can't stand
            # in for it, so report unknown rather than guess "ok".
            return "unknown", "AirPlay playback status unavailable"

        # shairport reports it IS playing from here.
        if airplay_rate is None:
            return "unknown", "waiting for fan-in frame-rate baseline"
        if airplay_rate < 1000.0:
            return "issue", "AirPlay reports playing but fan-in is not receiving frames"

        # Streaming and receiving frames: surface recent non-fatal warnings
        # (short reads, soft events) that happened *while actively
        # streaming* — meaningful now, unlike the idle case above.
        if (
            summary_30m["shairport_events"] > 0
            or summary_30m["fanin_airplay_xruns"] > 0
            or summary_30m["fanin_output_xruns"] > 0
            or summary_5m["camilla_short_reads"] > 0
            or summary_30m["camilla_playback_underruns"] > 0
        ):
            return "watch", "recent non-fatal audio-path warning"
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
            from ..camilla import CamillaController
            from ..camilla_config_contract import read_camilla_devices_config

            async def read() -> tuple[dict[str, Any], str | None]:
                controller = CamillaController(host, port)
                try:
                    status = await controller.get_runtime_status()
                    if status is None or not all(
                        key in status
                        for key in (
                            "buffer_level", "rate_adjust", "capture_rate",
                        )
                    ):
                        raise OSError("incomplete CamillaDSP runtime status")
                    config_path = await controller.get_config_file_path(
                        best_effort=True,
                    )
                    return status, config_path
                finally:
                    await controller.close()

            out, config_path = asyncio.run(read())
            if config_path:
                out["config_path"] = config_path
                devices = read_camilla_devices_config(config_path)
                if devices:
                    out.update(devices)
            return out
        except Exception:  # noqa: BLE001
            return None
