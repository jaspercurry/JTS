"""Target frequency-response curves for room correction.

Built-in targets (returned as dB arrays evaluated on a frequency grid):

  - flat — 0 dB everywhere. The "do nothing above the modal range"
    target for power users.

  - harman — Harman in-room target (Olive 2013, AES 8994). Roughly
    -1 dB/octave from 100 Hz to 20 kHz, with a +4 dB sub-bass shelf
    below 80 Hz. The published research target most closely
    matching what listeners prefer in a typical living room.

  - house_curve(warmth) — interpolant between flat and harman.
    warmth=0 returns flat; warmth=1 returns full Harman; warmth>1
    extrapolates (more bass, more downward tilt) and warmth<0
    extrapolates the other way (brighter / less bass). The Phase 2
    UI exposes this as a 3-position slider, not a continuous knob.

We deliberately do NOT ship the B&K-1974 / EBU Tech 3276 / JBL-Synthesis
curves in V1. They're presets a power user might want, but the four
canonical targets — flat, Harman, plus warm/bright tilts of Harman —
cover the discriminating axis (overall tilt + sub-bass emphasis).
Adding more is Phase 2 polish.
"""
from __future__ import annotations

import numpy as np


def flat_target(freqs: np.ndarray) -> np.ndarray:
    """Flat target — all zeros, in dB."""
    return np.zeros_like(freqs, dtype=np.float64)


def harman_target(freqs: np.ndarray) -> np.ndarray:
    """Harman in-room target curve (Olive 2013).

    Approximation:
      - Sub-bass shelf: +4 dB at f ≤ 60 Hz, smoothly returning to
        0 dB at 100 Hz (cosine-like crossfade in log-frequency).
      - Above 100 Hz: -1 dB/octave tilt, ending at ~ -7.6 dB at
        20 kHz.

    Returns dB array on the supplied frequency grid.
    """
    db = np.zeros_like(freqs, dtype=np.float64)

    # Sub-bass shelf
    sub_mask = freqs <= 60.0
    db[sub_mask] = 4.0

    # Smooth shoulder 60 → 100 Hz.
    transition_mask = (freqs > 60.0) & (freqs < 100.0)
    if transition_mask.any():
        f = freqs[transition_mask]
        # Linear in log-frequency: at 60 Hz output 4 dB, at 100 Hz output 0.
        x = np.log2(f / 60.0) / np.log2(100.0 / 60.0)
        db[transition_mask] = 4.0 * (1.0 - x)

    # -1 dB/octave above 100 Hz. Octave ratio = log2(f / 100).
    above_mask = freqs >= 100.0
    db[above_mask] = -np.log2(freqs[above_mask] / 100.0)

    return db


def house_curve(freqs: np.ndarray, warmth: float = 1.0) -> np.ndarray:
    """House curve: linear interpolant between flat and Harman.

    Args:
      warmth: 0 = flat, 1 = full Harman, 0.5 = halfway. Clamped to
        [-1, 2] to avoid pathological extrapolations from a UI bug.
    """
    w = float(np.clip(warmth, -1.0, 2.0))
    return harman_target(freqs) * w


# Phase 2 UI surfaces these three named presets in a 3-position
# slider. Keeping the names + warmth coefficients as a single source
# of truth so the UI label and the math line up.
HOUSE_CURVE_PRESETS = {
    "neutral": 0.0,    # = flat target
    "warm":    0.7,    # mostly Harman, slightly less sub-bass / tilt
    "bright": -0.3,    # negative tilt — more energy in the highs
}
