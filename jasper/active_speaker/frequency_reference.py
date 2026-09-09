# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Valid bands and shared level references for saved frequency curves."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from .flat_spec import REFERENCE_BAND_HZ, evaluate_flat_spec
from .frequency_view import FrequencySeries, FrequencyViewError
from jasper.json_fields import finite_float


def band_limited_curve(curve: Mapping[str, Any]) -> tuple[Any, Any]:
    """Apply the producer's declared valid band before exposing a curve."""

    freqs = curve.get("freqs_hz")
    magnitude = curve.get("magnitude_db")
    band = curve.get("band_hz")
    if (
        not isinstance(freqs, Sequence)
        or isinstance(freqs, (str, bytes))
        or not isinstance(magnitude, Sequence)
        or isinstance(magnitude, (str, bytes))
        or len(freqs) != len(magnitude)
        or not isinstance(band, Sequence)
        or isinstance(band, (str, bytes))
        or len(band) != 2
    ):
        return freqs, magnitude
    try:
        lo_hz, hi_hz = (float(value) for value in band)
        numeric = tuple((float(hz), float(db)) for hz, db in zip(freqs, magnitude))
    except (TypeError, ValueError):
        return freqs, magnitude
    if (
        not math.isfinite(lo_hz)
        or not math.isfinite(hi_hz)
        or hi_hz < lo_hz
        or not all(math.isfinite(hz) and math.isfinite(db) for hz, db in numeric)
    ):
        return freqs, magnitude
    bounded = tuple((hz, db) for hz, db in numeric if lo_hz <= hz <= hi_hz)
    return tuple(hz for hz, _ in bounded), tuple(db for _, db in bounded)


def _valid_band(series: FrequencySeries) -> tuple[float | None, float | None]:
    raw = series.details.get("band_hz")
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) != 2
    ):
        return None, None
    try:
        lo_hz, hi_hz = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(lo_hz) or not math.isfinite(hi_hz) or hi_hz < lo_hz:
        return None, None
    return lo_hz, hi_hz


def _evaluated_reference_db(series: FrequencySeries) -> float | None:
    lo_hz, hi_hz = _valid_band(series)
    try:
        report = evaluate_flat_spec(
            np.asarray(series.freqs_hz, dtype=float),
            np.asarray(series.magnitude_db, dtype=float),
            smoothing_fraction=0,
            trusted_floor_hz=lo_hz,
            trusted_ceiling_hz=hi_hz,
        )
    except (OverflowError, TypeError, ValueError):
        return None
    return float(report.reference_db)


def _anchor_rank(series: FrequencySeries) -> tuple[int, float]:
    position = series.details.get("position")
    degrees = position.get("deg") if isinstance(position, Mapping) else None
    distance = (
        abs(float(degrees))
        if isinstance(degrees, (int, float)) and not isinstance(degrees, bool)
        else math.inf
    )
    role = str(series.details.get("role") or "summed")
    return (0 if role == "summed" and distance in {0.0, math.inf} else 1, distance)


def share_run_reference(
    series: Sequence[FrequencySeries],
    run_reference_db: float | None,
) -> tuple[FrequencySeries, ...]:
    """Give directly comparable curves one deterministic level reference."""

    if not series:
        return ()
    reference_db = finite_float(run_reference_db)
    if reference_db is None:
        reference_db = next(
            (item.reference_db for item in series if item.reference_db is not None),
            None,
        )
    if reference_db is None:
        lo_hz, hi_hz = REFERENCE_BAND_HZ
        candidates = sorted(
            (item for item in series if any(lo_hz <= hz < hi_hz for hz in item.freqs_hz)),
            key=_anchor_rank,
        )
        reference_db = _evaluated_reference_db(candidates[0]) if candidates else None
    if reference_db is None:
        raise FrequencyViewError(
            "frequency run has no stored reference and no curve overlaps "
            f"{REFERENCE_BAND_HZ[0]:g}-{REFERENCE_BAND_HZ[1]:g} Hz"
        )
    return tuple(replace(item, reference_db=reference_db) for item in series)


__all__ = ["band_limited_curve", "share_run_reference"]
