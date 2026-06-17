"""Shared spatial-spread helpers for multi-position measurements."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np


SpatialConfidence = Literal["high", "medium", "low"]

HIGH_CONFIDENCE_STD_DB = 4.0
MEDIUM_CONFIDENCE_STD_DB = 6.0

# A per-frequency std from only two seats is too few samples to call
# repeatability "high": two measurements that happen to agree can't be
# distinguished from genuine seat-to-seat stability. Require at least
# this many positions before "high" is allowed.
MIN_POSITIONS_FOR_HIGH = 3


@dataclass(frozen=True)
class SpatialMatrix:
    freqs_hz: np.ndarray
    magnitudes_db: np.ndarray
    std_db: np.ndarray
    range_db: np.ndarray

    @property
    def position_count(self) -> int:
        return int(self.magnitudes_db.shape[0])


def confidence_for_std(
    std_db: float, n_positions: int | None = None,
) -> SpatialConfidence:
    """Classify repeatability from per-frequency standard deviation.

    With fewer than MIN_POSITIONS_FOR_HIGH positions, "high" is capped to
    "medium": a 2-seat std is from too few samples to trust. Pass
    n_positions to apply the gate; None keeps the legacy std-only
    classification.
    """
    if std_db <= HIGH_CONFIDENCE_STD_DB:
        level: SpatialConfidence = "high"
    elif std_db <= MEDIUM_CONFIDENCE_STD_DB:
        level = "medium"
    else:
        level = "low"
    if (
        level == "high"
        and n_positions is not None
        and n_positions < MIN_POSITIONS_FOR_HIGH
    ):
        return "medium"
    return level


def build_spatial_matrix(
    position_magnitudes: list[np.ndarray],
    freqs_hz: np.ndarray | None,
) -> tuple[SpatialMatrix | None, str | None]:
    """Validate and stack per-position magnitude curves.

    One position is valid for artifact export, but any confidence
    claim based on seat-to-seat variation must still require at least
    two positions.
    """
    if freqs_hz is None or not position_magnitudes:
        return None, "need at least one completed position"

    curves = [np.asarray(m, dtype=float) for m in position_magnitudes]
    freqs = np.asarray(freqs_hz, dtype=float)
    if freqs.ndim != 1 or freqs.shape[0] == 0:
        return None, "freqs must be non-empty 1-D"
    if any(curve.ndim != 1 for curve in curves):
        return None, "position curves must be 1-D"
    if any(curve.shape[0] != freqs.shape[0] for curve in curves):
        return None, "position curve shapes differ"
    if not np.all(np.isfinite(freqs)) or any(
        not np.all(np.isfinite(curve)) for curve in curves
    ):
        return None, "position curves contain non-finite values"

    matrix = np.vstack(curves)
    return SpatialMatrix(
        freqs_hz=freqs,
        magnitudes_db=matrix,
        std_db=np.std(matrix, axis=0),
        range_db=np.ptp(matrix, axis=0),
    ), None


def band_summary(
    matrix: SpatialMatrix,
    *,
    band_hz: tuple[float, float],
    band_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Return JSON-safe spatial spread metrics for one frequency band."""
    freqs = matrix.freqs_hz
    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    out: dict[str, Any] = {
        "band_hz": [band_hz[0], band_hz[1]],
        "position_count": matrix.position_count,
    }
    if band_id:
        out["band_id"] = band_id
    if label:
        out["label"] = label
    if not np.any(mask):
        out.update({
            "available": False,
            "reason": "no points in band",
            "n_points": 0,
        })
        return out

    out["n_points"] = int(mask.sum())
    if matrix.position_count < 2:
        out.update({
            "available": False,
            "reason": "need at least two completed positions",
        })
        return out

    band_freqs = freqs[mask]
    band_std = matrix.std_db[mask]
    band_range = matrix.range_db[mask]
    p90_std = float(np.percentile(band_std, 90))
    worst_idx = int(np.argmax(band_range))
    out.update({
        "available": True,
        "confidence_level": confidence_for_std(
            p90_std, n_positions=matrix.position_count,
        ),
        "median_std_db": round(float(np.median(band_std)), 2),
        "p90_std_db": round(p90_std, 2),
        "max_range_db": round(float(np.max(band_range)), 2),
        "worst_freq_hz": round(float(band_freqs[worst_idx]), 2),
    })
    return out


def point_summary(
    matrix: SpatialMatrix,
    *,
    freq_hz: float,
) -> dict[str, Any]:
    """Return spatial spread at the nearest measured frequency."""
    idx = int(np.argmin(np.abs(matrix.freqs_hz - freq_hz)))
    out: dict[str, Any] = {
        "available": matrix.position_count >= 2,
        "position_count": matrix.position_count,
        "freq_hz": round(float(matrix.freqs_hz[idx]), 2),
    }
    if matrix.position_count < 2:
        out["reason"] = "need at least two completed positions"
        return out
    std_db = float(matrix.std_db[idx])
    out.update({
        "confidence_level": confidence_for_std(
            std_db, n_positions=matrix.position_count,
        ),
        "std_db": round(std_db, 2),
        "range_db": round(float(matrix.range_db[idx]), 2),
    })
    return out
