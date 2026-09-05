# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared, dependency-free memory- and disk-pressure policy."""
from __future__ import annotations

import os
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
    return MemoryPressure(
        _psi_some_avg60(pressure_path), _oom_kills(vmstat_path)
    )


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


def _oom_kills(path: str) -> int | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("oom_kill "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None
