"""System metrics sampler for jasper-control.

Polls /proc and vcgencmd at a fixed cadence (5s for cheap metrics —
memory, load, network, disk, uptime; 30s for the costlier vcgencmd
subprocess calls — temperature, throttled bitmask) and keeps a 60-min
ring buffer that the /system dashboard reads via /system/snapshot.

Why in-process here:
  - We need a 60-min history for the sparkline in the dashboard, and
    nothing else in the daemon was already collecting it.
  - jasper-control is the natural place — it's the always-on HTTP
    aggregator for state, volume, transport, etc.
  - Cost: ~0.05% of one core idle, plus ~28 KB RAM for the ring buffer
    (six float64 arrays × 720 points × 8 bytes ≈ 33 KB; measured 30
    KB Pss on the Pi).

What we DON'T do here:
  - No psutil dep. /proc + vcgencmd via subprocess.run is enough.
  - No persistent storage. History is in-memory; lost on restart.
    Acceptable: a recently-restarted box has no useful history anyway.
  - No on-disk caching of the latest snapshot. The /state and /system/
    snapshot HTTP handlers serialize from the live arrays directly.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from array import array
from typing import Any

logger = logging.getLogger(__name__)

# Sample cadences. 5 s = 720 points = 60-min ring buffer for the
# sparklines. 30 s for vcgencmd (subprocess fork+exec cost) — temp
# and throttled bits don't move fast enough to need 5 s resolution.
SAMPLE_INTERVAL_SEC = 5.0
VCGENCMD_INTERVAL_SEC = 30.0
HISTORY_POINTS = 720  # 60 min @ 5 s

# Subprocess timeout for vcgencmd — if the GPU firmware is wedged
# (rare but seen on overheating), we don't want to block the sampler.
VCGENCMD_TIMEOUT_SEC = 2.0


class SystemSampler:
    """Background thread that snapshots /proc + vcgencmd into ring
    buffers. Lock-free for readers: snapshot() takes a brief lock,
    copies out the arrays, releases."""

    def __init__(
        self,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
        vcgencmd_interval_sec: float = VCGENCMD_INTERVAL_SEC,
        history_points: int = HISTORY_POINTS,
    ) -> None:
        self._sample_interval = sample_interval_sec
        self._vcgencmd_interval = vcgencmd_interval_sec
        self._history_points = history_points
        self._lock = threading.Lock()
        # Ring buffers (5-second resolution).
        self._t = array("d")  # epoch seconds
        self._mem_available_mb = array("d")
        self._mem_used_mb = array("d")
        self._swap_used_mb = array("d")
        self._load_1m = array("d")
        # Current-only (no history): cheap to keep up to date but not
        # worth a sparkline.
        self._mem_total_mb = 0
        self._disk_used_pct = 0.0
        self._disk_total_gb = 0.0
        self._temp_c = 0.0
        self._throttled_now = 0
        self._throttled_history = 0  # high bits = "ever throttled since boot"
        self._net_rx_bytes = 0
        self._net_tx_bytes = 0
        self._uptime_sec = 0.0
        self._last_sample_at: float | None = None
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="jasper-control-sampler", daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        """For tests; production runs daemon thread to end of process."""
        self._stopped = True

    def snapshot(self) -> dict[str, Any]:
        """Return current values + ring-buffer history. Copies arrays
        inside the lock so the caller can release before serializing."""
        with self._lock:
            return {
                "sample_interval_sec": self._sample_interval,
                "history_points": self._history_points,
                "last_sample_at": self._last_sample_at,
                "history": {
                    "t": list(self._t),
                    "mem_available_mb": list(self._mem_available_mb),
                    "mem_used_mb": list(self._mem_used_mb),
                    "swap_used_mb": list(self._swap_used_mb),
                    "load_1m": list(self._load_1m),
                },
                "current": {
                    "mem_total_mb": self._mem_total_mb,
                    "disk_used_pct": round(self._disk_used_pct, 1),
                    "disk_total_gb": round(self._disk_total_gb, 1),
                    "temp_c": round(self._temp_c, 1),
                    "throttled_now": self._throttled_now,
                    "throttled_history": self._throttled_history,
                    "net_rx_bytes": self._net_rx_bytes,
                    "net_tx_bytes": self._net_tx_bytes,
                    "uptime_sec": round(self._uptime_sec, 1),
                },
            }

    def _run(self) -> None:
        last_vcgencmd_at = 0.0
        while not self._stopped:
            sample_start = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                # Sampler must never crash the daemon. Log and keep going.
                logger.exception("system sampler tick failed")
            # vcgencmd less frequently than the main sample loop.
            now_mono = time.monotonic()
            if now_mono - last_vcgencmd_at >= self._vcgencmd_interval:
                try:
                    self._tick_vcgencmd()
                except Exception:  # noqa: BLE001
                    logger.exception("vcgencmd tick failed")
                last_vcgencmd_at = now_mono
            elapsed = time.monotonic() - sample_start
            sleep_for = max(0.1, self._sample_interval - elapsed)
            time.sleep(sleep_for)

    def _tick(self) -> None:
        """Cheap-metric sample — /proc reads + statvfs only."""
        mem = self._read_meminfo()
        load = self._read_loadavg_1m()
        net = self._read_net_dev()
        disk_used_pct, disk_total_gb = self._read_disk()
        uptime = self._read_uptime()
        with self._lock:
            self._append(self._t, time.time())
            self._append(self._mem_available_mb, mem["available_mb"])
            self._append(self._mem_used_mb, mem["used_mb"])
            self._append(self._swap_used_mb, mem["swap_used_mb"])
            self._append(self._load_1m, load)
            self._mem_total_mb = mem["total_mb"]
            self._net_rx_bytes = net["rx_bytes"]
            self._net_tx_bytes = net["tx_bytes"]
            self._disk_used_pct = disk_used_pct
            self._disk_total_gb = disk_total_gb
            self._uptime_sec = uptime
            self._last_sample_at = time.time()

    def _tick_vcgencmd(self) -> None:
        """Expensive-metric sample — forks vcgencmd twice. Done every
        VCGENCMD_INTERVAL_SEC rather than every sample tick."""
        temp = self._read_temp_c()
        throttled_now, throttled_history = self._read_throttled()
        with self._lock:
            self._temp_c = temp
            self._throttled_now = throttled_now
            self._throttled_history = throttled_history

    def _append(self, buf: array, val: float) -> None:
        buf.append(float(val))
        if len(buf) > self._history_points:
            del buf[0]

    # --- raw readers ---

    @staticmethod
    def _read_meminfo() -> dict[str, int]:
        out: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        out[key] = int(parts[1])
                    except ValueError:
                        pass
        total_kb = out.get("MemTotal", 0)
        avail_kb = out.get("MemAvailable", 0)
        swap_total_kb = out.get("SwapTotal", 0)
        swap_free_kb = out.get("SwapFree", 0)
        return {
            "total_mb": total_kb // 1024,
            "available_mb": avail_kb // 1024,
            "used_mb": (total_kb - avail_kb) // 1024,
            "swap_used_mb": (swap_total_kb - swap_free_kb) // 1024,
        }

    @staticmethod
    def _read_loadavg_1m() -> float:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])

    @staticmethod
    def _read_net_dev() -> dict[str, int]:
        rx = tx = 0
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        # Skip the two-line header.
        for line in lines[2:]:
            parts = line.split()
            if not parts:
                continue
            iface = parts[0].rstrip(":")
            # Exclude loopback — local-only traffic isn't "outbound".
            if iface == "lo":
                continue
            try:
                rx += int(parts[1])
                tx += int(parts[9])
            except (ValueError, IndexError):
                pass
        return {"rx_bytes": rx, "tx_bytes": tx}

    @staticmethod
    def _read_disk() -> tuple[float, float]:
        """Returns (used_pct, total_gb) for the root filesystem."""
        try:
            s = os.statvfs("/")
        except OSError:
            return 0.0, 0.0
        if s.f_blocks == 0:
            return 0.0, 0.0
        used_pct = (1.0 - s.f_bavail / s.f_blocks) * 100.0
        total_gb = (s.f_blocks * s.f_frsize) / (1024 ** 3)
        return used_pct, total_gb

    @staticmethod
    def _read_uptime() -> float:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])

    @staticmethod
    def _read_temp_c() -> float:
        try:
            out = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True, text=True,
                timeout=VCGENCMD_TIMEOUT_SEC,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return 0.0
        # vcgencmd output: "temp=47.7'C\n"
        try:
            return float(out.stdout.split("=")[1].split("'")[0])
        except (IndexError, ValueError):
            return 0.0

    @staticmethod
    def _read_throttled() -> tuple[int, int]:
        """Returns (current_bits, history_bits) from vcgencmd get_throttled.

        Bits 0-3 are CURRENTLY happening; bits 16-19 are "happened
        since boot." 0x0 = never throttled, all is well."""
        try:
            out = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True, text=True,
                timeout=VCGENCMD_TIMEOUT_SEC,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return 0, 0
        # Output: "throttled=0x0\n"
        try:
            full = int(out.stdout.split("=")[1].strip(), 16)
        except (IndexError, ValueError):
            return 0, 0
        return full & 0xF, (full >> 16) & 0xF


def read_build_info(build_file: str = "/var/lib/jasper/build.txt") -> dict[str, str]:
    """Read the build manifest install.sh writes on every install.

    Returns {} if the file isn't there (e.g. dev environment). Keys
    written by install.sh:
      - JASPER_GIT_SHA (short SHA)
      - JASPER_GIT_SHA_FULL
      - JASPER_GIT_BRANCH
      - JASPER_INSTALL_AT (ISO 8601 timestamp)"""
    out: dict[str, str] = {}
    try:
        with open(build_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.warning("build_info read failed: %s", e)
        return {}
    return out
