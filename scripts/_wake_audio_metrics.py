# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared numeric metrics for standalone wake-analysis scripts."""
from __future__ import annotations

import numpy as np


def rms_amplitude(samples: np.ndarray) -> float:
    """Root-mean-square amplitude using float64 accumulation.

    Float conversion happens before squaring so integer inputs cannot overflow.
    Empty arrays retain the wake tools' established ``0.0`` result; NumPy's
    ordinary NaN/inf propagation remains unchanged for non-finite inputs.
    """
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
