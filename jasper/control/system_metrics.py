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
  - Cost: ~0.05% of one core idle, plus ~35 KB RAM for the ring buffer
    (six 5-second arrays plus the slower temperature history).

What we DON'T do here:
  - No psutil dep. /proc + vcgencmd via subprocess.run is enough.
  - No persistent storage. History is in-memory; lost on restart.
    Acceptable: a recently-restarted box has no useful history anyway.
  - No on-disk caching of the latest snapshot. The /state and /system/
    snapshot HTTP handlers serialize from the live arrays directly.
"""
from __future__ import annotations

import logging
import math
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

# cgroup-v2 unified hierarchy. systemd places .service cgroups under
# /sys/fs/cgroup, including nested slices such as jts.slice/jts-audio.slice.
# The dashboard samples a curated service inventory: every jasper-* unit plus
# the audio renderers and system support daemons that explain most of the
# "non-jasper" CPU gap during audio/debugging sessions.
CGROUP_ROOT = "/sys/fs/cgroup"
SERVICE_PREFIX = "jasper-"
SERVICE_SUFFIX = ".service"

JASPER_SERVICE_GROUPS = {
    "jasper-aec-bridge.service": "Mic",
    "jasper-voice.service": "Voice",
    "jasper-camilla.service": "Audio",
    "jasper-fanin.service": "Audio",
    "jasper-mux.service": "Audio",
    "jasper-usbsink.service": "Audio",
    "jasper-control.service": "Control",
    "jasper-web.service": "Control",
    "jasper-system-web.service": "Control",
    "jasper-input.service": "Hardware",
    "jasper-headphone-monitor.service": "Hardware",
}

EXTRA_SERVICE_GROUPS = {
    "shairport-sync.service": "Audio",
    "librespot.service": "Audio",
    "bluealsa.service": "Audio",
    "bluealsa-aplay.service": "Audio",
    "nqptp.service": "Audio",
    "nginx.service": "Web",
    "avahi-daemon.service": "Network",
    "NetworkManager.service": "Network",
    "wpa_supplicant.service": "Network",
    "ssh.service": "System",
    "dbus.service": "System",
    "systemd-journald.service": "System",
    "bluetooth.service": "System",
    "bt-agent.service": "System",
}

# Memory cgroup controller probe. The Pi 5 device-tree blob injects
# `cgroup_disable=memory` into the kernel cmdline by default; install.sh
# overrides with `cgroup_enable=memory` in /boot/firmware/cmdline.txt
# (reboot required). When the override hasn't taken effect, the
# per-service memory.current files don't exist and memory reports blank.
# The dashboard surfaces this state so the next person doesn't have
# to ask why.
CGROUP_CONTROLLERS_FILE = "/sys/fs/cgroup/cgroup.controllers"

# /proc/stat per-core lines. Each "cpuN" line reports cumulative
# jiffies in user/nice/system/idle/iowait/irq/softirq/steal/guest/
# guest_nice columns; the dashboard wants active vs total to compute
# a per-core utilization percentage.
PROC_STAT = "/proc/stat"


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
        vcgencmd_interval = max(vcgencmd_interval_sec, 0.001)
        self._temp_history_points = max(
            1,
            math.ceil(
                history_points * sample_interval_sec / vcgencmd_interval,
            ),
        )
        self._lock = threading.Lock()
        # Ring buffers (5-second resolution).
        self._t = array("d")  # epoch seconds
        self._mem_available_mb = array("d")
        self._mem_used_mb = array("d")
        self._swap_used_mb = array("d")
        self._load_1m = array("d")
        self._fan_rpm = array("d")  # 0 when fan absent
        self._fan_pwm = array("d")  # 0 when fan absent
        self._temp_c_history = array("d")
        # Current snapshot values that are not stored in the main
        # 5-second ring buffers.
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
        #   _service_samples — internal: cgroup path -> (usage_usec, mono_ts)
        #     used to compute the CPU% delta on the next tick. First
        #     tick after a service appears yields cpu_pct=None because
        #     we have no baseline.
        #   _services_snapshot — public: list of dicts for snapshot().
        self._service_samples: dict[str, tuple[int, float]] = {}
        self._services_snapshot: list[dict[str, Any]] = []
        # Per-core CPU state. Same delta pattern as per-service: first
        # tick yields [] because we have no baseline.
        #   _per_core_prev — internal: list of (active_jiffies, total_jiffies)
        #     one entry per core, indexed by cpuN order in /proc/stat
        #   _per_core_pct — public: list of floats, 0-100 per core
        self._per_core_prev: list[tuple[int, int]] = []
        self._per_core_pct: list[float] = []
        # Memory-cgroup-controller state. None on non-Linux; True/False
        # on Linux based on /sys/fs/cgroup/cgroup.controllers.
        self._memory_cgroup_enabled: bool | None = None
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
                    "temp_c": list(self._temp_c_history),
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
                    # Per-core CPU utilization, one entry per logical
                    # CPU in cpuN order from /proc/stat. Each value
                    # 0-100 (%). Empty list on the first tick (no
                    # baseline yet) and on non-Linux systems.
                    "per_core_cpu_pct": list(self._per_core_pct),
                    # Memory cgroup controller availability — surfaces
                    # the "memory shows '—' for every service" case. None
                    # on non-Linux; False if the running kernel was
                    # booted with `cgroup_disable=memory` and no
                    # override; True when /sys/fs/cgroup/cgroup.controllers
                    # lists "memory".
                    "memory_cgroup_enabled": self._memory_cgroup_enabled,
                },
                # Per-service cgroup stats. List of
                #   {"name": "jasper-voice", "group": "Voice",
                #    "cpu_pct": 43.5, "memory_mb": 256.0}
                # cpu_pct is None on the first tick a service appears
                # (delta math needs two samples). memory_mb is cgroup-v2
                # memory.current, not process RSS; None means unreadable
                # (race with cgroup teardown). 100% = 1 core saturated.
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
        per_core = self._tick_per_core()
        memory_cgroup = self._read_memory_cgroup_enabled()
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
            self._per_core_pct = per_core
            self._memory_cgroup_enabled = memory_cgroup
            self._last_sample_at = time.time()

    def _tick_vcgencmd(self) -> None:
        """Expensive-metric sample — forks vcgencmd twice. Done every
        VCGENCMD_INTERVAL_SEC rather than every sample tick."""
        temp = self._read_temp_c()
        throttled_now, throttled_history = self._read_throttled()
        with self._lock:
            self._temp_c = temp
            self._append(self._temp_c_history, temp, self._temp_history_points)
            self._throttled_now = throttled_now
            self._throttled_history = throttled_history

    def _append(self, buf: array, val: float, limit: int | None = None) -> None:
        buf.append(float(val))
        max_len = limit if limit is not None else self._history_points
        if len(buf) > max_len:
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
        self, root_dir: str = CGROUP_ROOT,
    ) -> list[dict[str, Any]]:
        """Sample per-service CPU + memory from cgroup-v2.

        CPU% is computed as a delta of cpu.stat's usage_usec between
        the previous tick and this one. Per-core convention — 100% is
        one fully-saturated core, 400% the full Pi 5. New services
        yield cpu_pct=None on the first sample (no baseline).

        Services that disappeared since the last tick (one-shot
        finished, manual stop) fall out automatically — cgroup walk gives
        the live set, prev-sample entries we no longer see get dropped
        when we rebuild the dict below."""
        now_mono = time.monotonic()
        services = self._list_service_cgroups(root_dir)
        prev = self._service_samples
        new_samples: dict[str, tuple[int, float]] = {}
        out: list[dict[str, Any]] = []
        for service in services:
            unit = service["unit"]
            path = service["path"]
            sample_key = service["cgroup"]
            usec = self._read_cgroup_cpu_usec_path(path)
            memory_bytes = self._read_cgroup_memory_bytes_path(path)
            if usec is None and memory_bytes is None:
                # Cgroup vanished between listdir and read — race
                # with service teardown. Skip silently.
                continue
            memory_mb = (
                round(memory_bytes / (1024 * 1024), 1)
                if memory_bytes is not None else None
            )
            cpu_pct: float | None = None
            if usec is not None and sample_key in prev:
                prev_usec, prev_mono = prev[sample_key]
                wall_delta = now_mono - prev_mono
                if wall_delta > 0:
                    pct = (usec - prev_usec) / (wall_delta * 1e6) * 100.0
                    # Clock skew or cgroup counter reset can yield
                    # tiny negatives. Floor at 0 so the dashboard
                    # never shows a nonsense value.
                    cpu_pct = round(max(0.0, pct), 1)
            if usec is not None:
                new_samples[sample_key] = (usec, now_mono)
            out.append({
                "name": unit.removesuffix(SERVICE_SUFFIX),
                "unit": unit,
                "group": service["group"],
                "cgroup": sample_key,
                "cpu_pct": cpu_pct,
                "memory_mb": memory_mb,
            })
        self._service_samples = new_samples
        return out

    @staticmethod
    def _service_group(unit: str) -> str | None:
        """Return the dashboard group for a unit, or None to omit it."""
        if unit.startswith(SERVICE_PREFIX) and unit.endswith(SERVICE_SUFFIX):
            return JASPER_SERVICE_GROUPS.get(unit, "JTS")
        return EXTRA_SERVICE_GROUPS.get(unit)

    @classmethod
    def _list_service_cgroups(
        cls, root_dir: str = CGROUP_ROOT,
    ) -> list[dict[str, str]]:
        """Return service cgroups the dashboard should show.

        Walks the unified hierarchy recursively so JTS services moved into
        purpose-built slices (jts-audio, jts-mic, etc.) are still visible.
        """
        out: list[dict[str, str]] = []
        for dirpath, _dirnames, filenames in os.walk(
            root_dir, onerror=lambda _err: None,
        ):
            unit = os.path.basename(dirpath)
            group = cls._service_group(unit)
            if group is None:
                continue
            if "cpu.stat" not in filenames and "memory.current" not in filenames:
                continue
            cgroup = "/" + os.path.relpath(dirpath, root_dir)
            if not unit.startswith(SERVICE_PREFIX) and not (
                cgroup.startswith("/system.slice/")
                or cgroup.startswith("/jts.slice/")
            ):
                continue
            out.append({
                "unit": unit,
                "group": group,
                "path": dirpath,
                "cgroup": cgroup,
            })
        return sorted(out, key=lambda s: (s["group"], s["unit"], s["cgroup"]))

    @staticmethod
    def _read_cgroup_cpu_usec(slice_dir: str, name: str) -> int | None:
        """Total CPU time consumed by the cgroup, in microseconds, or
        None if cpu.stat is unreadable. Cumulative since cgroup
        creation — the delta over wall time is what's meaningful."""
        return SystemSampler._read_cgroup_cpu_usec_path(
            os.path.join(slice_dir, name),
        )

    @staticmethod
    def _read_cgroup_cpu_usec_path(cgroup_dir: str) -> int | None:
        path = os.path.join(cgroup_dir, "cpu.stat")
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
        return SystemSampler._read_cgroup_memory_bytes_path(
            os.path.join(slice_dir, name),
        )

    @staticmethod
    def _read_cgroup_memory_bytes_path(cgroup_dir: str) -> int | None:
        path = os.path.join(cgroup_dir, "memory.current")
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_memory_cgroup_enabled(
        controllers_file: str = CGROUP_CONTROLLERS_FILE,
    ) -> bool | None:
        """Return True if the kernel's cgroup-v2 memory controller is
        enabled, False if Linux + cgroup-v2 are present but memory is
        disabled, None if /sys/fs/cgroup isn't there (non-Linux, or
        cgroup-v1 system).

        On the Pi 5, this surfaces the install.sh ↔ reboot gap: a
        recent install.sh run will have appended `cgroup_enable=memory`
        to /boot/firmware/cmdline.txt, but the running kernel honors
        whatever it was booted with — so the dashboard can correctly
        say "reboot to apply" instead of silently rendering "—" for
        every service's memory column."""
        try:
            with open(controllers_file) as f:
                controllers = f.read().split()
        except OSError:
            return None
        return "memory" in controllers

    def _tick_per_core(
        self, stat_path: str = PROC_STAT,
    ) -> list[float]:
        """Per-core CPU utilization (%) computed as a delta of active
        vs total jiffies between the previous tick and this one. One
        entry per logical CPU in /proc/stat order; empty list on the
        first tick (no baseline) and on non-Linux systems.

        Pi 5 has 4 cores; htop-style per-core bars in the dashboard
        let the user tell whether 'load 3.6' means 'four cores ~90%
        busy' or 'one core pegged + others idle' (single-threaded
        bottleneck — common shape for jasper-voice's Python GIL)."""
        samples = self._read_per_core_jiffies(stat_path)
        if not samples:
            self._per_core_prev = []
            return []
        prev = self._per_core_prev
        out: list[float] = []
        # If core count changed (kernel hot-plug, very rare on a Pi)
        # OR we have no baseline yet, reset and yield empty — next
        # tick will produce real values.
        if len(prev) != len(samples):
            self._per_core_prev = samples
            return []
        for (a_now, t_now), (a_prev, t_prev) in zip(samples, prev):
            active_delta = a_now - a_prev
            total_delta = t_now - t_prev
            if total_delta <= 0:
                # Clock skew or counter reset — fall back to 0 rather
                # than a nonsense negative.
                out.append(0.0)
                continue
            pct = (active_delta / total_delta) * 100.0
            out.append(round(max(0.0, min(100.0, pct)), 1))
        self._per_core_prev = samples
        return out

    @staticmethod
    def _read_per_core_jiffies(
        stat_path: str = PROC_STAT,
    ) -> list[tuple[int, int]]:
        """Read /proc/stat per-core lines, return list of
        (active_jiffies, total_jiffies) per core in cpuN order.

        /proc/stat columns: user nice system idle iowait irq softirq
        steal guest guest_nice. Total = all of them. Active = total -
        idle - iowait (iowait is idle-with-pending-IO; counts as
        non-busy for utilization purposes — matches `top`'s
        convention). Returns [] on non-Linux."""
        try:
            with open(stat_path) as f:
                lines = f.readlines()
        except OSError:
            return []
        out: list[tuple[int, int]] = []
        for line in lines:
            parts = line.split()
            # Per-core lines start with "cpuN" where N is a digit.
            # The bare "cpu" aggregate line (no digit) is skipped so
            # we don't double-count.
            if not parts or not parts[0].startswith("cpu"):
                continue
            if parts[0] == "cpu":
                continue
            if not parts[0][3:].isdigit():
                continue
            try:
                fields = [int(x) for x in parts[1:11]]
            except ValueError:
                continue
            # Pad short lines (older kernels lacked guest fields).
            while len(fields) < 10:
                fields.append(0)
            user, nice, system, idle, iowait = fields[:5]
            irq, softirq, steal, guest, guest_nice = fields[5:10]
            # guest / guest_nice are ALREADY included in user / nice
            # (kernel accounting quirk — see kernel/sched/cputime.c).
            # Don't double-count.
            total = user + nice + system + idle + iowait + irq + softirq + steal
            active = total - idle - iowait
            out.append((active, total))
        return out

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
