# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared, dependency-free memory- and disk-pressure policy."""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


# Percent-used bands for the root filesystem. The FAIL line is where writes
# start failing and an unclean power-cut risks ext4 corruption; the WARN line
# is the operator-tunable early warning below it.
DISK_WARN_PERCENT = 85
DISK_FAIL_PERCENT = 95

# Kernel pressure-stall information. `psi=1` on the cmdline (install.sh) plus
# CONFIG_PSI; without either the file is absent and the fact is "no reading",
# never a calm zero. "some avg60" is the share of the last 60 s in which at
# least one task stalled waiting on memory.
PROC_PRESSURE_MEMORY = "/proc/pressure/memory"
# Cumulative OOM kills since boot. Absent on kernels without the counter.
PROC_VMSTAT = "/proc/vmstat"
PROC_MEMINFO = "/proc/meminfo"
# Virtual (uncompressed) capacity of the zram swap device, in bytes. Absent
# where zram is not in use at all.
ZRAM_DISKSIZE_PATH = "/sys/block/zram0/disksize"

# PSI "some avg60" percent at which memory stalls stop being noise on a 1 GB
# board. Mirrors the /system dashboard's warn band —
# `toneForPercent(psi, 10, 20)` in deploy/assets/system-status/js/format.js —
# which tests/test_system_status_thresholds.py pins to this owner.
MEM_PSI_WARN_AVG60 = 10.0

# zram virtual capacity as a percent of RAM. deploy/rpi-swap/50-jts.conf sets
# `RamMultiplier=0.5` and deploy/lib/install/memory-resilience.sh sizes to the
# same number; tests/test_memory_policy.py pins both to this owner.
ZRAM_TARGET_PERCENT = 50
# Slack before the doctor calls a live zram device oversized. rpi-swap sizes
# from MemTotal, which sits a few percent below installed RAM and moves with
# the CMA/GPU split, so the live ratio never lands exactly on target; the old
# 100%-of-RAM default this check exists to catch is far past the margin.
ZRAM_OVERSIZE_MARGIN_PERCENT = 10
# The band a live device is judged against, derived here so the doctor never
# re-adds the two numbers itself.
ZRAM_WARN_PERCENT = ZRAM_TARGET_PERCENT + ZRAM_OVERSIZE_MARGIN_PERCENT


def memory_headroom_thresholds(total_mb: int) -> tuple[int, int]:
    """Return the canonical ``(warn_mb, fail_mb)`` MemAvailable floors.

    Percentage-of-RAM thresholds with absolute floors keep the policy useful
    across Pi memory tiers: warn below ``max(100 MB, 10%)`` and fail below
    ``max(30 MB, 3%)``. The system dashboard mirrors this calculation in
    ``memoryHeadroomLimits``; ``tests/test_system_status_thresholds.py`` pins
    that JavaScript consumer to this dependency-free owner.
    """
    return (
        max(100, total_mb * 10 // 100),
        max(30, total_mb * 3 // 100),
    )


class DiskUsage(NamedTuple):
    """Fullness of one mounted filesystem.

    ``free_bytes`` is the non-root-available pool (statvfs ``f_bavail``) — what
    the daemons, none of which write as root, actually have — while
    ``percent_used`` is derived from total-vs-free so the reserved-blocks pool
    never reads as headroom. A filesystem reporting no blocks comes back with
    ``total_bytes == 0``; the caller decides whether that is a skip or a null.
    """

    path: str
    total_bytes: int
    free_bytes: int
    percent_used: float


def disk_usage(path: str = "/") -> DiskUsage | None:
    """Return fullness for ``path``, or ``None`` where ``os.statvfs`` is absent.

    ``OSError`` from the call itself propagates: an unreadable filesystem and a
    host that cannot measure one are different facts, and jasper-doctor reports
    them under different reason codes.
    """
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    st = statvfs(path)
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return DiskUsage(path, 0, 0, 0.0)
    free = st.f_bavail * st.f_frsize
    return DiskUsage(path, total, free, (total - free) * 100 / total)


class MemoryPressure(NamedTuple):
    """Live memory-stall pressure, as the kernel reports it.

    Either field is ``None`` where this kernel publishes no such counter —
    distinct from a zero, which means "measured, and there is none".
    ``oom_kills`` is cumulative since boot, so it describes history and never
    the current verdict.
    """

    psi_some_avg60: float | None
    oom_kills: int | None


def memory_pressure(
    *,
    pressure_path: str = PROC_PRESSURE_MEMORY,
    vmstat_path: str = PROC_VMSTAT,
) -> MemoryPressure:
    """Read PSI and the OOM-kill counter. Never raises.

    One reader for both consumers (ADR-0233 rule 1): jasper-control's
    system-metrics sampler feeds the dashboard tile from it, and
    jasper-doctor's ``check_memory_pressure`` verdicts against
    :data:`MEM_PSI_WARN_AVG60`.
    """
    try:
        oom_kill = proc_fields(vmstat_path, "oom_kill").get("oom_kill")
    except OSError:
        oom_kill = None
    return MemoryPressure(_psi_some_avg60(pressure_path), oom_kill)


def _psi_some_avg60(path: str) -> float | None:
    """Line shape: ``some avg10=0.00 avg60=1.23 avg300=0.41 total=12345``."""
    try:
        with open(path) as f:
            for line in f:
                if not line.startswith("some "):
                    continue
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key == "avg60":
                        return float(value)
    except (OSError, ValueError):
        return None
    return None


def proc_fields(path: str, *names: str) -> dict[str, int]:
    """Every named field in a ``key: value`` / ``key value`` ``/proc`` file,
    in ONE pass (ADR-0233 rule 1 — the one parser for this shape:
    ``/proc/vmstat``'s ``oom_kill 3`` and ``/proc/meminfo``'s
    ``MemTotal:  1014768 kB`` alike).

    Raises ``OSError`` if the file cannot be opened/read; the CALLER decides
    whether that fails soft to ``None``/absent (:func:`meminfo_kb`,
    :func:`memory_pressure`) or should propagate (jasper-control's
    ``SystemSampler._read_meminfo``, whose outer per-tick guard must drop the
    whole tick rather than record a zero-filled sample as if it were real). A
    field missing from the file, or a value that doesn't parse as an int, is
    simply absent from the returned dict.
    """
    wanted = set(names)
    out: dict[str, int] = {}
    with open(path) as f:
        for line in f:
            fields = line.split()
            if len(fields) < 2:
                continue
            key = fields[0].rstrip(":")
            if key not in wanted:
                continue
            try:
                out[key] = int(fields[1])
            except ValueError:
                continue
            if len(out) == len(wanted):
                break
    return out


def meminfo_fields(*names: str, path: str = PROC_MEMINFO) -> dict[str, int]:
    """Every named ``/proc/meminfo`` field (e.g. ``MemTotal``), in KiB, via
    :func:`proc_fields` — propagates ``OSError``; see there for who catches
    it and why."""
    return proc_fields(path, *names)


def meminfo_kb(field: str, *, path: str = PROC_MEMINFO) -> int | None:
    """One ``/proc/meminfo`` field (e.g. ``MemAvailable``), in KiB, or
    ``None`` when the file cannot be read."""
    try:
        return meminfo_fields(field, path=path).get(field)
    except OSError:
        return None


class ZramUsage(NamedTuple):
    """Virtual zram capacity against installed RAM.

    ``disksize_bytes == 0`` is a device present but not yet sized;
    ``total_bytes == 0`` is a MemTotal that could not be read. Either way
    ``percent_of_ram`` is 0 and the caller decides which fact that is.
    ``percent_of_ram`` floors to whole percent, the unit
    :data:`ZRAM_WARN_PERCENT` is stated in.
    """

    disksize_bytes: int
    total_bytes: int
    percent_of_ram: int


def zram_usage(
    *,
    disksize_path: str = ZRAM_DISKSIZE_PATH,
    meminfo_path: str = PROC_MEMINFO,
    total_kb: int | None = None,
) -> ZramUsage | None:
    """Return zram sizing, or ``None`` where there is no zram0 device at all.

    Fail-soft like :func:`disk_usage`: an older RPi OS on dphys-swapfile and a
    dev laptop both simply have no such device, which is not a fault.

    ``total_kb`` lets a caller that already read ``/proc/meminfo`` this run
    (jasper-doctor, via its per-run evidence memo) pass ``MemTotal`` straight
    in instead of causing a second read; omitted, this reads it itself.
    """
    try:
        disksize = int(Path(disksize_path).read_text().strip())
    except (OSError, ValueError):
        return None
    if total_kb is None:
        total_kb = meminfo_kb("MemTotal", path=meminfo_path) or 0
    total = total_kb * 1024
    if disksize <= 0 or total <= 0:
        return ZramUsage(max(disksize, 0), total, 0)
    return ZramUsage(disksize, total, disksize * 100 // total)
