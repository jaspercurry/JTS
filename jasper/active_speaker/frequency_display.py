# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Display coordinates and trust markings shared by browser and image charts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from jasper.json_fields import finite_float


def merge_display_intervals(intervals: Iterable[Any]) -> list[list[float]]:
    valid = []
    for interval in intervals:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            continue
        lo, hi = map(finite_float, interval)
        if lo is not None and hi is not None and lo <= hi:
            valid.append((lo, hi))
    merged: list[list[float]] = []
    for lo, hi in sorted(valid):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


def prepare_frequency_curve(
    curve: Mapping[str, Any], metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    band = merge_display_intervals([curve.get("band_hz")])
    floor = finite_float(curve.get("validity_floor_hz"))
    if floor is None:
        floor = finite_float(metadata.get("validity_floor_hz"))
    lo = max(band[0][0] if band else 0, floor or 0)
    hi = band[0][1] if band else None
    trusted = finite_float(curve.get("trusted_floor_hz"))
    if trusted is None:
        trusted = finite_float(metadata.get("trusted_floor_hz"))
    untrusted = merge_display_intervals([
        *([[0, max(lo, trusted or 0)]] if max(lo, trusted or 0) > 0 else []),
        *(metadata.get("excluded_bands_hz") or []),
        *(curve.get("excluded_intervals_hz") or []),
    ])
    reference = finite_float(curve.get("reference_db", metadata.get("reference_db")))
    deviations = []
    for raw_hz, raw_db in zip(curve["freqs_hz"], curve["magnitude_db"]):
        hz, db = finite_float(raw_hz), finite_float(raw_db)
        deviations.append(
            db - reference if reference is not None and db is not None
            and hz is not None and hz > 0 and hz >= lo and (hi is None or hz <= hi)
            else None
        )
    return {
        **curve,
        "display": {
            "deviation_db": deviations,
            "valid_band_hz": [lo, hi],
            "untrusted_intervals_hz": untrusted,
        },
    }
