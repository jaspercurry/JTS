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
