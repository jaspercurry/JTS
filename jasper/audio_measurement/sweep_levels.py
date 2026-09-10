# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Received sweep-band power versus equal-duration quiet windows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from .snr_policy import band_levels_dbfs
from .sweep import SweepMeta


def sweep_band_levels(
    capture: np.ndarray, quiet: np.ndarray, sample_rate: int, sweep: SweepMeta,
    anchor: int, bands: Sequence[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Raw power units on both sides; short dwell and changing noise limit precision."""
    rows = []
    for lo, hi in bands:
        if not 0 < lo < hi or not all(math.isfinite(v) for v in (lo, hi)):
            raise ValueError("sweep_band_invalid")
        if lo < sweep.f1 or hi > sweep.f2:
            continue
        start, stop = (
            round(sweep.duration_s * sample_rate * math.log(f / sweep.f1) / math.log(sweep.f2 / sweep.f1))
            for f in (lo, hi)
        )
        size = stop - start
        if size < 8 or anchor + start < 0 or anchor + stop > capture.size:
            continue
        band = [("sweep", lo, hi)]
        signal = band_levels_dbfs(capture[anchor + start:anchor + stop], sample_rate, band, window="rectangular")
        noise = [
            value[0]["level_dbfs"] for offset in range(0, quiet.size - size + 1, size)
            if (value := band_levels_dbfs(quiet[offset:offset + size], sample_rate, band, window="rectangular"))
        ]
        if not signal:
            continue
        observed = signal[0]["level_dbfs"]
        percentiles = np.percentile(noise, [10, 50, 90]).tolist() if noise else None
        excess = 10 ** ((observed - percentiles[2]) / 10) - 1 if percentiles else None
        rows.append({
            "band_hz": [lo, hi], "signal_plus_noise_dbfs": observed,
            "noise_p10_p50_p90_dbfs": percentiles, "quiet_windows": len(noise),
            "estimated_snr_db": 10 * math.log10(excess) if excess is not None and excess > 0 else None,
            "window_ms": 1000 * size / sample_rate, "resolution_hz": sample_rate / size,
        })
    return rows
