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

# Where to look for the PWM fan. The `pwmfan` hwmon name is exposed by
# the Pi 5's pwm-fan kernel driver when the official Active Cooler (or
# any 4-pin PWM+tach fan wired to the fan header) is attached. Index
# (hwmon0/1/2/…) isn't stable across boots; we scan by name.
HWMON_DIR = "/sys/class/hwmon"
PWMFAN_HWMON_NAME = "pwmfan"
# The pwm-fan driver expresses duty as 0-255. There is no `pwm1_max`
# attribute to read, so hardcode the well-known max.
PWMFAN_DUTY_MAX = 255

# cgroup-v2 unified hierarchy. systemd places .service units under
# /sys/fs/cgroup/system.slice/<unit>.service/. We sample only our own
# (jasper-* prefix) to keep the table focused on what the user can
# act on — the audio renderers (shairport-sync, librespot, etc.) have
# their own units that don't share this prefix and aren't included.
SYS_SLICE_CGROUP = "/sys/fs/cgroup/system.slice"
SERVICE_PREFIX = "jasper-"
SERVICE_SUFFIX = ".service"


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
        self._fan_rpm = array("d")  # 0 when fan absent
        self._fan_pwm = array("d")  # 0 when fan absent
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
        self._fan_present = False
        # Per-service cgroup state.
        #   _service_samples — internal: name -> (usage_usec, mono_ts)
        #     used to compute the CPU% delta on the next tick. First
        #     tick after a service appears yields cpu_pct=None because
        #     we have no baseline.
        #   _services_snapshot — public: list of dicts for snapshot().
        self._service_samples: dict[str, tuple[int, float]] = {}
        self._services_snapshot: list[dict[str, Any]] = []
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
                    "fan_rpm": list(self._fan_rpm),
                    "fan_pwm": list(self._fan_pwm),
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
                    "fan_present": self._fan_present,
                    # None (not 0) when absent — lets the dashboard hide
                    # the tile cleanly instead of showing "0 RPM".
                    "fan_rpm": (
                        int(self._fan_rpm[-1])
                        if self._fan_present and self._fan_rpm
                        else None
                    ),
                    "fan_pwm": (
                        int(self._fan_pwm[-1])
                        if self._fan_present and self._fan_pwm
                        else None
                    ),
                    "fan_pwm_max": PWMFAN_DUTY_MAX,
                },
                # Per-service cgroup stats. List of
                #   {"name": "jasper-voice", "cpu_pct": 43.5, "rss_mb": 256.0}
                # cpu_pct is None on the first tick a service appears
                # (delta math needs two samples). rss_mb is None only
                # if memory.current was unreadable (race with cgroup
                # teardown). 100% = 1 core saturated (top convention).
                "services": list(self._services_snapshot),
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
        """Cheap-metric sample — /proc reads + statvfs + sysfs only."""
        mem = self._read_meminfo()
        load = self._read_loadavg_1m()
        net = self._read_net_dev()
        disk_used_pct, disk_total_gb = self._read_disk()
        uptime = self._read_uptime()
        # _read_fan returns None when no pwm-fan hwmon device exists
        # (dev machines, Pi without an Active Cooler attached).
        fan = self._read_fan()
        services = self._tick_services()
        with self._lock:
            self._append(self._t, time.time())
            self._append(self._mem_available_mb, mem["available_mb"])
            self._append(self._mem_used_mb, mem["used_mb"])
            self._append(self._swap_used_mb, mem["swap_used_mb"])
            self._append(self._load_1m, load)
            if fan is not None:
                self._fan_present = True
                self._append(self._fan_rpm, fan["rpm"])
                self._append(self._fan_pwm, fan["pwm"])
            else:
                self._fan_present = False
                # Still append (zeros) so history arrays stay aligned
                # with `t` — the dashboard hides the tile via
                # fan_present anyway.
                self._append(self._fan_rpm, 0.0)
                self._append(self._fan_pwm, 0.0)
            self._mem_total_mb = mem["total_mb"]
            self._net_rx_bytes = net["rx_bytes"]
            self._net_tx_bytes = net["tx_bytes"]
            self._disk_used_pct = disk_used_pct
            self._disk_total_gb = disk_total_gb
            self._uptime_sec = uptime
            self._services_snapshot = services
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
    def _read_fan(hwmon_dir: str = HWMON_DIR) -> dict[str, int] | None:
        """Returns {'rpm': N, 'pwm': N} for the Pi's PWM fan, or None
        if no `pwmfan` hwmon device is present (dev box, no Active
        Cooler attached, etc.).

        hwmon indices aren't stable across boots, so scan by name. Cost
        is one listdir + one open of each hwmon's name file (typically
        4-6 entries on a Pi 5) per 5-second tick — negligible."""
        try:
            entries = os.listdir(hwmon_dir)
        except OSError:
            return None
        for entry in entries:
            base = os.path.join(hwmon_dir, entry)
            try:
                with open(os.path.join(base, "name")) as f:
                    if f.read().strip() != PWMFAN_HWMON_NAME:
                        continue
                with open(os.path.join(base, "fan1_input")) as f:
                    rpm = int(f.read().strip())
                with open(os.path.join(base, "pwm1")) as f:
                    pwm = int(f.read().strip())
            except (OSError, ValueError):
                continue
            return {"rpm": rpm, "pwm": pwm}
        return None

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

    def _tick_services(
        self, slice_dir: str = SYS_SLICE_CGROUP,
    ) -> list[dict[str, Any]]:
        """Sample per-service CPU + memory from cgroup-v2.

        CPU% is computed as a delta of cpu.stat's usage_usec between
        the previous tick and this one. Per-core convention — 100% is
        one fully-saturated core, 400% the full Pi 5. New services
        yield cpu_pct=None on the first sample (no baseline).

        Services that disappeared since the last tick (one-shot
        finished, manual stop) fall out automatically — listdir gives
        the live set, prev-sample entries we no longer see get dropped
        when we rebuild the dict below."""
        now_mono = time.monotonic()
        names = self._list_jasper_cgroups(slice_dir)
        prev = self._service_samples
        new_samples: dict[str, tuple[int, float]] = {}
        out: list[dict[str, Any]] = []
        for name in names:
            usec = self._read_cgroup_cpu_usec(slice_dir, name)
            rss_bytes = self._read_cgroup_memory_bytes(slice_dir, name)
            if usec is None and rss_bytes is None:
                # Cgroup vanished between listdir and read — race
                # with service teardown. Skip silently.
                continue
            rss_mb = (
                round(rss_bytes / (1024 * 1024), 1)
                if rss_bytes is not None else None
            )
            cpu_pct: float | None = None
            if usec is not None and name in prev:
                prev_usec, prev_mono = prev[name]
                wall_delta = now_mono - prev_mono
                if wall_delta > 0:
                    pct = (usec - prev_usec) / (wall_delta * 1e6) * 100.0
                    # Clock skew or cgroup counter reset can yield
                    # tiny negatives. Floor at 0 so the dashboard
                    # never shows a nonsense value.
                    cpu_pct = round(max(0.0, pct), 1)
            if usec is not None:
                new_samples[name] = (usec, now_mono)
            out.append({
                "name": name.removesuffix(SERVICE_SUFFIX),
                "cpu_pct": cpu_pct,
                "rss_mb": rss_mb,
            })
        self._service_samples = new_samples
        return out

    @staticmethod
    def _list_jasper_cgroups(slice_dir: str = SYS_SLICE_CGROUP) -> list[str]:
        """Return jasper-*.service cgroup directory names. Empty list
        when /sys/fs/cgroup/system.slice is absent (dev box on macOS,
        or cgroup-v1 system — Trixie uses v2 by default)."""
        try:
            entries = os.listdir(slice_dir)
        except OSError:
            return []
        return sorted(
            e for e in entries
            if e.startswith(SERVICE_PREFIX) and e.endswith(SERVICE_SUFFIX)
        )

    @staticmethod
    def _read_cgroup_cpu_usec(slice_dir: str, name: str) -> int | None:
        """Total CPU time consumed by the cgroup, in microseconds, or
        None if cpu.stat is unreadable. Cumulative since cgroup
        creation — the delta over wall time is what's meaningful."""
        path = os.path.join(slice_dir, name, "cpu.stat")
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    @staticmethod
    def _read_cgroup_memory_bytes(slice_dir: str, name: str) -> int | None:
        """Resident memory of the cgroup in bytes (memory.current).
        None if unreadable (race with cgroup teardown)."""
        path = os.path.join(slice_dir, name, "memory.current")
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

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
