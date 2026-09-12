# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prediction records and measured comparisons for exact capture models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

PREDICTION_KIND = "jts_forward_model_prediction"
PREDICTION_SCHEMA_VERSION = 1

REFUSAL_UNSUPPORTED = "forward_model_unsupported"

#: Nothing measured judged this prediction. What a bare prediction always is:
#: the model computed a curve and no capture ever contradicted it.
ACCEPTANCE_NOT_RUN = "not_run"

#: A banked measurement judged it, and ``judged_against`` names which one.
ACCEPTANCE_JUDGED = "judged_against_measured"


def acceptance_block(judged_against: str | None) -> dict[str, Any]:
    """Whether a measurement judged this prediction, and which one.

    ``judged_against`` is the measured comparand's own identity — the banked
    round whose VERIFY sum the prediction was deltaed against — or ``None``,
    which is what a prediction nothing measured is. Disclosure, never a gate
    and never a grade: this module ships no acceptance tolerance (#3481).
    """
    return {
        "status": ACCEPTANCE_JUDGED if judged_against else ACCEPTANCE_NOT_RUN,
        "judged_against": judged_against,
    }


class ForwardModelError(ValueError):
    """The banked curves cannot support a predicted sum.

    ``refusal_reason`` and ``detail`` are the contract; the message is operator
    copy and may be reworded freely.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = REFUSAL_UNSUPPORTED,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_reason = reason
        self.detail: dict[str, Any] = dict(detail or {})


@dataclass(frozen=True)
class PredictedSum:
    """One candidate's predicted summed magnitude, and what it was read on.

    ``predicted_db`` is on ``freqs_hz``, the banked pair's own shared grid.
    Outside :attr:`sum_band_hz` neither driver was swept and the prediction is
    the floor rather than a response — a reader compares inside that band.
    Whether a measurement judged it is the CONTAINING record's field, since
    the prediction alone cannot answer it.
    """

    freqs_hz: np.ndarray
    predicted_db: np.ndarray
    sum_band_hz: tuple[float, float]
    take_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "kind": PREDICTION_KIND,
            "freqs_hz": [float(hz) for hz in self.freqs_hz],
            "predicted_db": [float(db) for db in self.predicted_db],
            "sum_band_hz": [float(edge) for edge in self.sum_band_hz],
            "take_path": self.take_path,
        }


def predicted_minus_measured_db(
    predicted: PredictedSum,
    measured_freqs_hz: Any,
    measured_db: Any,
    *,
    band_hz: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """The predicted-vs-measured delta, as facts and no verdict.

    Both curves are level-normalised against their OWN median over the compared
    band before subtracting: a forward model over banked solos carries no
    absolute SPL reference, so the raw offset between it and a measured sum is a
    level difference rather than a shape error. The offset removed is published
    as ``level_offset_db``.

    ``band_hz`` defaults to the prediction's own
    :attr:`PredictedSum.sum_band_hz` intersected with the measured curve's
    extent, and is reported as ``compared_band_hz``. No verdict, tolerance or
    score is returned (invariant 3).
    """
    measured_grid = np.asarray(measured_freqs_hz, dtype=float)
    measured_curve = np.asarray(measured_db, dtype=float)
    if measured_grid.size != measured_curve.size or measured_grid.size == 0:
        raise ForwardModelError(
            "the measured curve and its grid disagree in length",
            detail={
                "measured_points": int(measured_curve.size),
                "grid_points": int(measured_grid.size),
            },
        )
    lo_hz = max(predicted.sum_band_hz[0], float(measured_grid.min()))
    hi_hz = min(predicted.sum_band_hz[1], float(measured_grid.max()))
    if band_hz is not None:
        lo_hz = max(lo_hz, float(band_hz[0]))
        hi_hz = min(hi_hz, float(band_hz[1]))
    grid = predicted.freqs_hz
    mask = (grid >= lo_hz) & (grid <= hi_hz)
    if not np.any(mask):
        raise ForwardModelError(
            f"no predicted bin falls in {lo_hz:g}-{hi_hz:g} Hz",
            detail={"compared_lo_hz": lo_hz, "compared_hi_hz": hi_hz},
        )
    compared_grid = grid[mask]
    predicted_curve = predicted.predicted_db[mask]
    measured_on_grid = np.interp(compared_grid, measured_grid, measured_curve)
    offset_db = float(np.median(predicted_curve) - np.median(measured_on_grid))
    delta = (predicted_curve - offset_db) - measured_on_grid
    return {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "compared_band_hz": [lo_hz, hi_hz],
        "compared_points": int(compared_grid.size),
        "level_offset_db": offset_db,
        "freqs_hz": [float(hz) for hz in compared_grid],
        "predicted_db": predicted_curve.tolist(),
        "measured_db": measured_on_grid.tolist(),
        "delta_db": [float(db) for db in delta],
        "max_abs_db": float(np.max(np.abs(delta))),
        "rms_db": float(np.sqrt(np.mean(delta**2))),
        "take_path": predicted.take_path,
    }


__all__ = [
    "ACCEPTANCE_JUDGED",
    "ACCEPTANCE_NOT_RUN",
    "PREDICTION_KIND",
    "PREDICTION_SCHEMA_VERSION",
    "REFUSAL_UNSUPPORTED",
    "ForwardModelError",
    "PredictedSum",
    "acceptance_block",
    "predicted_minus_measured_db",
]
