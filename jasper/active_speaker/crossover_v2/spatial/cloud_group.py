# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np

# Point limit for smoothed cloud disclosure curves.
CLOUD_CURVE_MAX_JSON_POINTS = 512


def _decimate_curve_for_json(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> dict[str, list[float]]:
    """Stride-decimate one combined curve to at most
    :data:`CLOUD_CURVE_MAX_JSON_POINTS`, for disclosure only.

    A plain stride is safe here, unlike in ``durable_state._decimate_sum``,
    because the curve is already fractional-octave smoothed; a stride over a
    raw unsmoothed prediction aliases below ~500 Hz (#1858).
    """
    n = len(freqs_hz)
    step = max(1, (n + CLOUD_CURVE_MAX_JSON_POINTS - 1) // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }
