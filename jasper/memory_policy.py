# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared, dependency-free memory-pressure policy."""
from __future__ import annotations


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
