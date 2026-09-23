# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The full-IR-then-arrival-window chain the deconvolution tests drive."""

from __future__ import annotations

import numpy as np

from jasper.audio_measurement.deconv import (
    DEFAULT_EPSILON_RELATIVE,
    DEFAULT_POST_ARRIVAL_MS,
    DEFAULT_PRE_ARRIVAL_MS,
    apply_arrival_window,
    direct_arrival_window,
    regularized_deconvolution_full,
)


def deconvolve(
    captured: np.ndarray,
    sweep: np.ndarray,
    sample_rate: int,
    *,
    pre_arrival_ms: float = DEFAULT_PRE_ARRIVAL_MS,
    post_arrival_ms: float = DEFAULT_POST_ARRIVAL_MS,
    epsilon_relative: float = DEFAULT_EPSILON_RELATIVE,
    max_capture_seconds: float | None = None,
) -> np.ndarray:
    """Recover h(t) from y(t) ≈ (h * x)(t) via regularized FFT.

    ``sweep`` must be the EXACT signal played, or the math is wrong by an
    unknown filter. ``epsilon_relative`` is the denominator floor as a fraction
    of peak |X(f)|². ``post_arrival_ms`` of
    500 covers a living room (RT60 < 1 s). ``max_capture_seconds=None`` reads
    :data:`DEFAULT_MAX_CAPTURE_SECONDS` at call time; <= 0 disables.
    """
    full_ir = regularized_deconvolution_full(
        captured,
        sweep,
        sample_rate,
        epsilon_relative=epsilon_relative,
        max_capture_seconds=max_capture_seconds,
    )
    window = direct_arrival_window(
        full_ir,
        sample_rate,
        pre_arrival_ms=pre_arrival_ms,
        post_arrival_ms=post_arrival_ms,
    )
    return apply_arrival_window(full_ir, window)
