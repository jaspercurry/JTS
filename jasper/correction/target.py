# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Target frequency-response curves for room correction.

``flat`` is 0 dB everywhere; ``harman`` is the Harman in-room target (Olive
2013, AES 8994) — roughly -1 dB/octave from 100 Hz to 20 kHz with a +4 dB
sub-bass shelf below 80 Hz; ``house_curve(warmth)`` interpolates between them
and extrapolates outside [0, 1]. Product-visible named profiles and their
warmth coefficients are owned by ``jasper.correction.strategy.TARGET_PROFILES``;
this module owns only the deterministic curve math.
"""
from __future__ import annotations

import numpy as np


def flat_target(freqs: np.ndarray) -> np.ndarray:
    """Flat target — all zeros, in dB."""
    return np.zeros_like(freqs, dtype=np.float64)


def harman_target(freqs: np.ndarray) -> np.ndarray:
    """Harman in-room target curve (Olive 2013), in dB on ``freqs``.

    Approximated as a +4 dB shelf at or below 60 Hz returning to 0 dB at
    100 Hz, then a -1 dB/octave tilt reaching about -7.6 dB at 20 kHz.
    """
    db = np.zeros_like(freqs, dtype=np.float64)

    sub_mask = freqs <= 60.0
    db[sub_mask] = 4.0

    transition_mask = (freqs > 60.0) & (freqs < 100.0)
    if transition_mask.any():
        f = freqs[transition_mask]
        x = np.log2(f / 60.0) / np.log2(100.0 / 60.0)
        db[transition_mask] = 4.0 * (1.0 - x)

    # -1 dB/octave above 100 Hz.
    above_mask = freqs >= 100.0
    db[above_mask] = -np.log2(freqs[above_mask] / 100.0)

    return db


def house_curve(freqs: np.ndarray, warmth: float = 1.0) -> np.ndarray:
    """House curve: linear interpolant between flat and Harman.

    ``warmth`` 0 = flat, 1 = full Harman; clamped to [-1, 2].
    """
    w = float(np.clip(warmth, -1.0, 2.0))
    return harman_target(freqs) * w
