# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The predicted-corner shortlist: one flatness figure per forecast, ranked (ADR-0401)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from jasper.audio_measurement.comparison_bands import OVERLAP_OCTAVE_RATIO
from jasper.audio_measurement.evidence_reasons import REASON_COVERAGE_SHORT, unavailable
from jasper.audio_measurement.series_stats import curve_difference, deviation_summary


def rank_corners(
    forecasts: Sequence[tuple[float, Mapping[str, Any]]],
) -> tuple[list[float] | None, list[dict[str, Any]]]:
    """``forecasts`` are ``(fc_hz, prediction)`` pairs of one take's grid. Returns the band
    every row is read over and each row's ``flatness`` and ``rank`` (1 = lowest ``rms_db``)."""
    lo = max(min(fc for fc, _ in forecasts) / OVERLAP_OCTAVE_RATIO,
             *(prediction["sum_band_hz"][0] for _, prediction in forecasts))
    hi = min(max(fc for fc, _ in forecasts) * OVERLAP_OCTAVE_RATIO,
             *(prediction["sum_band_hz"][1] for _, prediction in forecasts))
    band = [lo, hi] if lo < hi else None
    figures: list[dict[str, Any]] = []
    for _, prediction in forecasts:
        freqs = np.asarray(prediction["freqs_hz"], dtype=float)
        # ADR-0358's level rule against a flat line; once #5661 names the one flatness formula, this reads it.
        difference = None if band is None else curve_difference(
            freqs, np.asarray(prediction["predicted_db"], dtype=float), freqs, np.zeros(freqs.size),
            band_hz=(lo, hi))
        figures.append({"flatness": unavailable(REASON_COVERAGE_SHORT, {"band_hz": [lo, hi]}) if difference is None
                        else {"status": "available", **deviation_summary(difference.freqs_hz, difference.delta_db)}})
    ranked = sorted((row for row in figures if row["flatness"]["status"] == "available"),
                    key=lambda row: row["flatness"]["rms_db"])
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return band, figures
