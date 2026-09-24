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
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from statistics import median
from typing import Any

from jasper.camilla_config_contract import DEFAULT_CAMILLA_PORT
from jasper.control._health_fields import (
    as_float,
    as_int,
    as_int_or_none,
    nonneg_delta,
    nonneg_rate,
    read_int_file,
    read_text_file,
)
from jasper.control.camilla_health import CamillaHealth
from jasper.control.fanin_view import FaninView
from jasper.service_units import SHAIRPORT_SYNC_SERVICE, JournalctlUnavailable, run_journalctl_json

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 5.0
JOURNAL_INTERVAL_SEC = 30.0
# R21 (#4416): a journalctl fork per scan adds up on an idle box with no
# AirPlay session in sight. Once no session has been active for
# JOURNAL_IDLE_THRESHOLD_SEC, SHAIRPORT's scan widens to
# JOURNAL_IDLE_INTERVAL_SEC; the connect-grace re-arm on the next
# idle->active transition (_tick) puts the 30 s cadence right back for the
# session that follows. CAMILLA's scan is unaffected — its short-read storm
# detector fires on any source's audio path, not only AirPlay's, so it
# always runs at JOURNAL_INTERVAL_SEC.
JOURNAL_IDLE_THRESHOLD_SEC = 5 * 60.0
JOURNAL_IDLE_INTERVAL_SEC = 120.0
MPRIS_INTERVAL_SEC = 30.0
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

SUBPROCESS_TIMEOUT_SEC = 2.0
MAINTENANCE_SUPPRESS_UNTIL_PATH = "/run/jasper-airplay-health-suppress-until"

# Fan-in's 4096-frame input buffer is load-bearing for AirPlay burst
# absorption.
MIN_AIRPLAY_INPUT_BUFFER_FRAMES = 4096

# AirPlay drop attribution (network vs internal:receiver — see
# jasper.control.audio_attribution.input_attribution, which owns the verdict
# thresholds). The session baseline is the median rx_bytes_per_sec of the
# last LINK_BASELINE_SAMPLES ticks where AirPlay was selected and the ring
# lane was actually receiving frames.
LINK_BASELINE_SAMPLES = 12
LINK_HEALTHY_FRAMES_PER_SEC = 1000.0

PROC_NET_WIRELESS_PATH = "/proc/net/wireless"
PROC_NET_SNMP_PATH = "/proc/net/snmp"
SYS_CLASS_NET_RX_BYTES_TMPL = "/sys/class/net/{iface}/statistics/rx_bytes"
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
except (ValueError, OSError, AttributeError):
    _CLK_TCK = 100

SHAIRPORT_UNIT = SHAIRPORT_SYNC_SERVICE.removesuffix(".service")


def _empty_bucket(t: float) -> dict[str, Any]:
    return {
        "t": t,
        "shairport_events": 0,
        "shairport_packet_drops": 0,
        "shairport_sync_errors": 0,
        "shairport_underruns": 0,
        "fanin_airplay_xruns": 0,
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
    "camilla_short_read": "camilla_short_reads",
    "camilla_playback_underrun": "camilla_playback_underruns",
}


def classify_journal_line(unit: str, line: str) -> dict[str, Any] | None:
    """Classify one shairport-sync journal line into the compact dashboard
    event shape (CamillaDSP's lines: ``camilla_health.classify_camilla_line``).

    The patterns are intentionally literal and pinned to the messages.
    Unknown log lines are ignored.
    """
    if unit == SHAIRPORT_UNIT:
        if "Dropping out of date packet" in line:
            lead_time = None
            m = re.search(r"Lead time is ([0-9.]+) seconds", line)
            if m:
                lead_time = as_float(m.group(1))
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
        # grouping doctor check). The fleet runs both shairport-sync 4.3.7
        # and 5.2.3, and this warn()'s wording differs between them (4.3.7
        # rtp.c:1822 vs. 5.2.3 rtp.c:1717) — verified against both trees at
        # their pinned commits, so the match is trimmed to the substring
        # both share rather than either version's full wording. warn()
        # never consults debuglev, so it prints at any verbosity.
        if "too short to accommodate an" in line:
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


def _read_wireless_iface() -> str | None:
    """Interface name from /proc/net/wireless — never a hardcoded wlan0."""
    text = read_text_file(PROC_NET_WIRELESS_PATH)
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("Inter-", "face")):
            continue
        iface = line.split(":", 1)[0].strip()
        if iface:
            return iface
    return None


def _read_snmp_line_fields(text: str, prefix: str) -> dict[str, str]:
    """/proc/net/snmp pairs a header line with a values line, both prefixed
    e.g. "Udp:"/"Tcp:" — zip the two into a name->value dict."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].startswith(prefix) and lines[i + 1].startswith(prefix):
            return dict(zip(lines[i].split()[1:], lines[i + 1].split()[1:]))
    return {}


def _read_link_counters() -> dict[str, Any]:
    """Pure /proc + /sys reads for AirPlay drop attribution. No subprocess,
    no privilege (ADR-0226). Every field is None when its source file is
    missing or unparseable — never 0, which would read as "no traffic"
    rather than "couldn't tell".
    """
    iface = _read_wireless_iface()
    rx_bytes = (
        read_int_file(SYS_CLASS_NET_RX_BYTES_TMPL.format(iface=iface))
        if iface else None
    )
    snmp_text = read_text_file(PROC_NET_SNMP_PATH)
    udp_fields = _read_snmp_line_fields(snmp_text, "Udp:") if snmp_text else {}
    tcp_fields = _read_snmp_line_fields(snmp_text, "Tcp:") if snmp_text else {}
    return {
        "iface": iface,
        "rx_bytes": rx_bytes,
        "udp_in_datagrams": as_int_or_none(udp_fields.get("InDatagrams")),
        "udp_rcvbuf_errors": as_int_or_none(udp_fields.get("RcvbufErrors")),
        "tcp_in_segs": as_int_or_none(tcp_fields.get("InSegs")),
    }


def _read_pid_stat_counters(pid: int) -> tuple[int, int] | None:
    """(majflt, utime+stime ticks) from /proc/<pid>/stat, or None.

    Field offsets per proc(5): majflt is field 12, utime field 14, stime
    field 15; comm (field 2) may itself contain ")", so split after the
    LAST ")" rather than by fixed position.
    """
    text = read_text_file(f"/proc/{pid}/stat")
    if text is None:
        return None
    close = text.rfind(")")
    if close == -1:
        return None
    fields = text[close + 1:].split()
    if len(fields) < 13:
        return None
    try:
        majflt = int(fields[9])
        utime = int(fields[11])
        stime = int(fields[12])
    except ValueError:
        return None
    return majflt, utime + stime


def _read_pid_state(pid: int) -> str | None:
    text = read_text_file(f"/proc/{pid}/status")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("State:"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def _read_pid_comm(pid: int) -> str | None:
    return read_text_file(f"/proc/{pid}/comm")


def _read_receiver_stat(pid: int) -> dict[str, Any]:
    """Pure /proc/<pid> reads: the receiver-process half of AirPlay drop
    attribution. `pid` is the ring's writer_pid — the ioplug inside
    shairport-sync. Includes `comm` so the caller can refuse a stale,
    recycled pid rather than trust an unrelated process's counters.
    """
    stat = _read_pid_stat_counters(pid)
    return {
        "pid": pid,
        "comm": _read_pid_comm(pid),
        "state": _read_pid_state(pid),
        "majflt": stat[0] if stat is not None else None,
        "cpu_ticks": stat[1] if stat is not None else None,
    }


class AirPlayHealthSampler:
    """Collector for recent AirPlay health.

    Tests inject probe functions and call _tick() directly. Production
    drives it via sample_once(), composed into AudioHealthSampler's loop.
    """

    def __init__(
        self,
        *,
        journal_interval_sec: float = JOURNAL_INTERVAL_SEC,
        journal_idle_threshold_sec: float = JOURNAL_IDLE_THRESHOLD_SEC,
        journal_idle_interval_sec: float = JOURNAL_IDLE_INTERVAL_SEC,
        mpris_interval_sec: float = MPRIS_INTERVAL_SEC,
        bucket_seconds: float = BUCKET_SECONDS,
        history_seconds: float = HISTORY_SECONDS,
        journal_reader: (
            Callable[[tuple[str, ...], float, float], list[tuple[str, str]]]
            | None
        ) = None,
        mpris_probe: Callable[[], dict[str, Any] | None] | None = None,
        link_probe: Callable[[], dict[str, Any]] | None = None,
        receiver_probe: Callable[[int], dict[str, Any]] | None = None,
        camilla_host: str = "127.0.0.1",
        camilla_port: int = DEFAULT_CAMILLA_PORT,
        maintenance_suppress_path: str | None = MAINTENANCE_SUPPRESS_UNTIL_PATH,
        warmup_sec: float = DEFAULT_WARMUP_SEC,
        connect_grace_sec: float = DEFAULT_CONNECT_GRACE_SEC,
        fanin_view: FaninView | None = None,
        camilla: CamillaHealth | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._journal_interval = journal_interval_sec
        self._journal_idle_threshold = journal_idle_threshold_sec
        self._journal_idle_interval = journal_idle_interval_sec
        self._mpris_interval = mpris_interval_sec
        self._bucket_seconds = bucket_seconds
        self._history_points = max(1, int(math.ceil(history_seconds / bucket_seconds)))
        self._journal_reader = journal_reader or self._read_journal_lines
        self._mpris_probe = mpris_probe or self._read_airplay_mpris
        self._link_probe = link_probe or _read_link_counters
        self._receiver_probe = receiver_probe or _read_receiver_stat
        self._maintenance_suppress_path = maintenance_suppress_path
        self._time = time_fn
        # Warmup / connect-grace suppression (see DEFAULT_*_SEC above).
        self._warmup_sec = warmup_sec
        self._connect_grace_sec = connect_grace_sec
        self._started_at = time_fn()
        self._connect_grace_until: float | None = None
        self._airplay_active = False
        # Idle-widen clock for the journal scan (R21, #4416): starts at
        # construction so a box that never sees a session widens too.
        self._last_airplay_active_at = self._started_at
        self._warmup_active = warmup_sec > 0.0
        self._suppressed_reason: str | None = None
        self._fanin = fanin_view or FaninView()
        self._camilla = camilla or CamillaHealth(
            host=camilla_host, port=camilla_port, time_fn=time_fn,
        )

        self._lock = threading.Lock()
        self._buckets: deque[dict[str, Any]] = deque(maxlen=self._history_points)
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_RING_SIZE)
        self._current_mpris: dict[str, Any] | None = None
        self._current_link: dict[str, Any] | None = None
        self._last_sample_at: float | None = None
        self._last_shairport_scan_at = 0.0
        self._shairport_journal_since = self._time()
        self._last_mpris_sample_at = 0.0
        self._last_link_counts: dict[str, Any] | None = None
        self._last_receiver_counts: dict[str, Any] | None = None
        # Per-session healthy-tick baseline (reset on lane detach, an
        # epoch_resets bump, or the active source leaving AirPlay — see
        # LINK_BASELINE_SAMPLES above).
        self._link_baseline: deque[float] = deque(maxlen=LINK_BASELINE_SAMPLES)
        self._link_baseline_epoch: int | None = None
        self._maintenance_suppressed = False
        self._maintenance_suppressed_until: float | None = None

    def sample_once(self) -> None:
        """The speaker-wide audio-health sampler composes this AirPlay-specific
        collector and calls it from the one existing monitoring loop.
        """
        self._tick()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            fanin = self._fanin.current  # read once: FaninView publishes outside self._lock
            summary_5m = self._summary_locked(5 * 60.0)
            summary_30m = self._summary_locked(30 * 60.0)
            status, reason = self._status_locked(fanin, summary_5m, summary_30m)
            return {
                "last_sample_at": self._last_sample_at,
                "maintenance_suppressed": self._maintenance_suppressed,
                "maintenance_suppressed_until": self._maintenance_suppressed_until,
                "warmup_active": self._warmup_active,
                "connect_grace_until": self._connect_grace_until,
                "suppressed_reason": self._suppressed_reason,
                "status": status,
                "reason": reason,
                "current": {
                    "fanin": copy.deepcopy(fanin),
                    "mpris": copy.deepcopy(self._current_mpris),
                    "camilla": copy.deepcopy(self._camilla.current),
                    "link": copy.deepcopy(self._current_link),
                },
                "summary_5m": summary_5m,
                "summary_30m": summary_30m,
                "storm": self._camilla.storm_snapshot(),
                "events": [dict(event) for event in self._events],
            }

    def _tick(self) -> None:
        now = self._time()
        suppress_until = self._read_maintenance_suppress_until(now)
        within_warmup = (now - self._started_at) < self._warmup_sec
        suppress_base = suppress_until is not None or within_warmup
        in_connect_grace = (
            self._connect_grace_until is not None
            and now < self._connect_grace_until
        )
        self._ensure_bucket(now)
        self._fanin.sample(
            now,
            record_event=self._record_event,
            suppress_events=(suppress_base or in_connect_grace),
        )
        self._sample_link(now)

        if now - self._last_mpris_sample_at >= self._mpris_interval:
            self._sample_mpris(now)
        self._camilla.sample(now)

        # PTP-anchor settle emits expected shairport sync-correction /
        # out-of-sequence bursts. Arm after MPRIS sampling so the grace
        # covers this tick's shairport journal scan.
        active = self._airplay_active_now()
        if active and not self._airplay_active:
            self._connect_grace_until = now + self._connect_grace_sec
        if active:
            self._last_airplay_active_at = now
        self._airplay_active = active
        in_connect_grace = (
            self._connect_grace_until is not None
            and now < self._connect_grace_until
        )
        # R21 (#4416): no AirPlay session in sight for a while widens
        # SHAIRPORT's scan cadence — the next idle->active transition
        # re-arms the connect grace above, which covers the 30 s cadence
        # resuming for it. Camilla's scan feeds the short-read storm
        # detector, which fires on any source's audio path, so it stays on
        # the base cadence regardless of AirPlay activity.
        shairport_interval = (
            self._journal_idle_interval
            if now - self._last_airplay_active_at >= self._journal_idle_threshold
            else self._journal_interval
        )
        if suppress_base or in_connect_grace:
            self._shairport_journal_since = max(self._shairport_journal_since, now)
            self._last_shairport_scan_at = now
        elif now - self._last_shairport_scan_at >= shairport_interval:
            self._scan_journal(
                SHAIRPORT_UNIT, self._shairport_journal_since, now,
                classify_journal_line,
            )
            self._shairport_journal_since = now
            self._last_shairport_scan_at = now
        self._camilla.scan_journal(
            now,
            suppress=suppress_base,  # not the connect grace: that is shairport's
            interval_sec=self._journal_interval,
            scan=self._scan_journal,
            active_source=self._active_source_hint(),
        )

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

    def airplay_streaming(self) -> bool | None:
        """Authoritative "is a sender streaming?" — shairport's MPRIS
        PlaybackStatus. Single source of truth shared by the dashboard
        status (`_status_locked`), the connect-grace (`_airplay_active_now`)
        and `/state`'s AirPlay renderer row.

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
        grace. Keyed on `airplay_streaming()` (shairport MPRIS) — never the
        always-on silent frame rate — so the idle->active transition the
        grace watches reflects a real session start, not the pipeline simply
        coming up. Detection therefore lags up to one MPRIS sample interval;
        the boot warmup is the primary post-restart smoother, the connect
        grace a best-effort session-establish one.
        """
        return self.airplay_streaming() is True

    def _sample_link(self, now: float) -> None:
        """Attribution-only signals: wireless link rate + the shairport
        receiver's own /proc counters. Pure reads, no subprocess (ADR-0226);
        fails soft to an all-None block on any probe error.
        """
        try:
            counters = self._link_probe()
        except Exception:  # noqa: BLE001
            logger.debug("link probe failed", exc_info=True)
            counters = None
        if not isinstance(counters, dict):
            counters = {}
        iface = counters.get("iface")
        rx_bytes = counters.get("rx_bytes")
        udp_in = counters.get("udp_in_datagrams")
        rcvbuf_err = counters.get("udp_rcvbuf_errors")
        tcp_in = counters.get("tcp_in_segs")

        prev = self._last_link_counts
        rx_rate: float | None = None
        udp_in_rate: float | None = None
        tcp_in_rate: float | None = None
        rcvbuf_err_delta: int | None = None
        if prev is not None and iface is not None and prev.get("iface") == iface:
            dt = max(0.001, now - float(prev.get("ts", now)))
            rx_rate = nonneg_rate(rx_bytes, prev.get("rx_bytes"), dt)
            udp_in_rate = nonneg_rate(udp_in, prev.get("udp_in_datagrams"), dt)
            tcp_in_rate = nonneg_rate(tcp_in, prev.get("tcp_in_segs"), dt)
            rcvbuf_err_delta = nonneg_delta(
                rcvbuf_err, prev.get("udp_rcvbuf_errors"),
            )
        self._last_link_counts = {
            "ts": now,
            "iface": iface,
            "rx_bytes": rx_bytes,
            "udp_in_datagrams": udp_in,
            "udp_rcvbuf_errors": rcvbuf_err,
            "tcp_in_segs": tcp_in,
        }

        fanin = self._fanin.current or {}
        selected = fanin.get("selected_input")
        inputs = fanin.get("inputs") if isinstance(fanin.get("inputs"), dict) else {}
        airplay_input = (
            inputs.get("airplay") if isinstance(inputs.get("airplay"), dict) else {}
        )
        ring = (
            airplay_input.get("ring")
            if isinstance(airplay_input.get("ring"), dict) else {}
        )
        writer_pid = ring.get("writer_pid")
        receiver = (
            self._sample_receiver(now, writer_pid)
            if isinstance(writer_pid, int) and writer_pid > 0 else None
        )

        # Baseline bookkeeping — reset on lane detach, an epoch_resets bump
        # (writer restart), or the active source leaving AirPlay. See
        # LINK_BASELINE_SAMPLES above.
        attached = ring.get("attached")
        epoch_resets = ring.get("epoch_resets")
        if selected != "airplay" or attached is False:
            self._link_baseline.clear()
            self._link_baseline_epoch = None
        elif (
            isinstance(epoch_resets, int)
            and self._link_baseline_epoch is not None
            and epoch_resets != self._link_baseline_epoch
        ):
            self._link_baseline.clear()
        if isinstance(epoch_resets, int):
            self._link_baseline_epoch = epoch_resets

        frames_per_sec = as_float(airplay_input.get("frames_per_sec"))
        if (
            selected == "airplay"
            and rx_rate is not None
            and rx_rate > 0
            and frames_per_sec is not None
            and frames_per_sec >= LINK_HEALTHY_FRAMES_PER_SEC
        ):
            self._link_baseline.append(rx_rate)
        baseline = median(self._link_baseline) if self._link_baseline else None

        current_link = {
            "iface": iface,
            "rx_bytes_per_sec": round(rx_rate, 1) if rx_rate is not None else None,
            "rx_bytes_per_sec_baseline": (
                round(baseline, 1) if baseline is not None else None
            ),
            "udp_in_datagrams_per_sec": (
                round(udp_in_rate, 1) if udp_in_rate is not None else None
            ),
            "udp_rcvbuf_errors_delta": rcvbuf_err_delta,
            "tcp_in_segs_per_sec": (
                round(tcp_in_rate, 1) if tcp_in_rate is not None else None
            ),
            "receiver": receiver,
        }
        with self._lock:
            self._current_link = current_link

    def _sample_receiver(self, now: float, pid: int) -> dict[str, Any] | None:
        """Delta shairport-sync's own /proc/<pid> counters into per-second
        rates. `pid` is the ring's writer_pid — the ioplug inside
        shairport-sync. Returns None when /proc/<pid>/comm doesn't say
        "shairport": the writer_pid is stale once the process is
        SIGKILLed, and a recycled pid's counters must never be
        misattributed to the receiver.
        """
        try:
            stat = self._receiver_probe(pid)
        except Exception:  # noqa: BLE001
            logger.debug("receiver probe failed", exc_info=True)
            stat = None
        if not isinstance(stat, dict):
            stat = {}
        comm = stat.get("comm")
        if not isinstance(comm, str) or "shairport" not in comm:
            self._last_receiver_counts = None
            return None
        majflt = stat.get("majflt")
        cpu_ticks = stat.get("cpu_ticks")
        prev = self._last_receiver_counts
        majflt_rate: float | None = None
        cpu_ms_rate: float | None = None
        if prev is not None and prev.get("pid") == pid:
            # Both-or-neither: a rate is only meaningful when BOTH counters
            # produced a valid monotonic delta this tick.
            majflt_delta = nonneg_delta(majflt, prev.get("majflt"))
            cpu_ticks_delta = nonneg_delta(cpu_ticks, prev.get("cpu_ticks"))
            if majflt_delta is not None and cpu_ticks_delta is not None:
                dt = max(0.001, now - float(prev.get("ts", now)))
                majflt_rate = majflt_delta / dt
                cpu_ms_rate = cpu_ticks_delta * 1000.0 / _CLK_TCK / dt
        self._last_receiver_counts = {
            "ts": now, "pid": pid, "majflt": majflt, "cpu_ticks": cpu_ticks,
        }
        return {
            "pid": pid,
            "state": stat.get("state"),
            "majflt_per_sec": (
                round(majflt_rate, 2) if majflt_rate is not None else None
            ),
            "cpu_ms_per_sec": (
                round(cpu_ms_rate, 1) if cpu_ms_rate is not None else None
            ),
        }

    def _sample_mpris(self, now: float) -> None:
        try:
            current = self._mpris_probe()
        except Exception:  # noqa: BLE001
            logger.debug("airplay MPRIS probe failed", exc_info=True)
            current = None
        with self._lock:
            self._current_mpris = current if isinstance(current, dict) else None
        self._last_mpris_sample_at = now

    def _scan_journal(
        self,
        unit: str,
        since: float,
        now: float,
        classify: Callable[[str, str], dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        """Read one unit's journal lines, record each event ``classify``
        finds and return those events. The shairport scan and
        :meth:`CamillaHealth.scan_journal` share it, each on its own cadence
        and cursor (R21, #4416).
        """
        try:
            entries = self._journal_reader((unit,), since, now)
        except Exception:  # noqa: BLE001
            logger.debug("journal scan failed", exc_info=True)
            entries = []
        events = []
        for scanned_unit, line in entries:
            event = classify(scanned_unit, line)
            if event is not None:
                self._record_event(now, event)
                events.append(event)
        return events

    def _active_source_hint(self) -> str | None:
        fanin = self._fanin.current or {}
        selected = fanin.get("selected_input")
        if selected:
            return str(selected)
        return "airplay" if self.airplay_streaming() else None

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
                bucket[field] = as_int(bucket.get(field)) + count
                if (
                    event.get("subsystem") == "shairport"
                    and field != "shairport_events"
                ):
                    bucket["shairport_events"] = (
                        as_int(bucket.get("shairport_events")) + count
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
            "camilla_short_reads": 0,
            "camilla_playback_underruns": 0,
        }
        for bucket in self._buckets:
            if float(bucket.get("t", 0.0)) + self._bucket_seconds < cutoff:
                continue
            for key in totals:
                totals[key] += as_int(bucket.get(key))
        return totals

    def _status_locked(
        self,
        fanin: dict[str, Any] | None,
        summary_5m: dict[str, int],
        summary_30m: dict[str, int],
    ) -> tuple[str, str]:
        if fanin is None:
            return "unknown", "fan-in status unavailable"

        if as_int(fanin.get("input_buffer_frames")) < MIN_AIRPLAY_INPUT_BUFFER_FRAMES:
            return "issue", "fan-in input buffer below 4096 frames"

        watchdog = fanin.get("watchdog", {})
        if isinstance(watchdog, dict):
            progress_age = as_int(watchdog.get("last_progress_age_ms"))
            if progress_age > 5000:
                return "issue", "fan-in watchdog stale"

        if (
            summary_5m["shairport_packet_drops"] > 0
            or summary_5m["shairport_sync_errors"] > 0
            or summary_5m["shairport_underruns"] > 0
            or summary_5m["camilla_playback_underruns"] > 0
            or summary_5m["fanin_airplay_xruns"] > 0
        ):
            return "issue", "recent audio-path recovery event"

        # Is AirPlay actually streaming? Use the authoritative MPRIS signal
        # (`airplay_streaming`) — NOT the fan-in frame rate, which free-runs
        # ~48 kHz of silence whenever the pipeline is up and so reads
        # "active" even at idle. The frame rate is only a corroborating
        # fault check once we know audio *should* be flowing.
        mpris_playing = self.airplay_streaming()
        airplay = fanin.get("airplay", {})
        airplay_rate = (
            as_float(airplay.get("frames_per_sec"))
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
            or summary_5m["camilla_short_reads"] > 0
            or summary_30m["camilla_playback_underruns"] > 0
        ):
            return "watch", "recent non-fatal audio-path warning"
        return "ok", "AirPlay path clean"

    @staticmethod
    def _read_journal_lines(
        units: tuple[str, ...], since: float, now: float,
    ) -> list[tuple[str, str]]:
        """``(unit, message)`` for every scanned unit, in ONE journalctl fork.

        ``-o json`` rather than ``-o cat`` because a merged scan has to know
        which unit emitted each line; one fork per scan instead of one per unit
        keeps this 30 s cadence off the Pi's process budget (ADR-0226).
        """
        try:
            rows = run_journalctl_json(
                units,
                since=f"@{since:.3f}",
                until=f"@{now:.3f}",
                output_fields=("_SYSTEMD_UNIT", "MESSAGE"),
                timeout=SUBPROCESS_TIMEOUT_SEC,
            )
        except JournalctlUnavailable:
            return []
        by_unit_id = {f"{unit}.service": unit for unit in units}
        entries: list[tuple[str, str]] = []
        for record in rows:
            unit = by_unit_id.get(record.get("_SYSTEMD_UNIT"))
            message = record.get("MESSAGE")
            # journald renders a non-UTF-8 MESSAGE as a list of byte values;
            # no classifier pattern can match one.
            if unit is not None and isinstance(message, str):
                entries.append((unit, message))
        return entries

    @staticmethod
    def _read_airplay_mpris() -> dict[str, Any] | None:
        try:
            from ..source_state import airplay_playing
            playing = asyncio.run(airplay_playing())
        except Exception:  # noqa: BLE001
            return None
        return {"playing": bool(playing)}
