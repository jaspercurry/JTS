# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-branch fit objective: target curve, gain-permitted band, contribution
weight (#1817, #1968). Contribution weights GAIN only (#1809)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_db,
    radiating_band_hz,
)

#: Octaves past the passband edge a branch may still place GAIN (#1968: ~1/2-1).
STOPBAND_GAIN_MARGIN_OCTAVES: float = 0.5

#: dB floor for "puts something into a band"; ==
#: ``linearization_fit._MIN_FILTER_GAIN_DB``, pinned by contract test.
SIGNIFICANT_GAIN_DB: float = 0.5


@dataclass(frozen=True)
class BranchTarget:
    """Arrays are bin-aligned to :func:`branch_target`'s grid, non-writeable."""

    shape_db: np.ndarray  #: SHAPE dB rel. passband level (raw; see centred_on)
    gain_permitted: np.ndarray  #: GAIN allowed (passband + STOPBAND_GAIN_MARGIN_OCTAVES)
    contribution: np.ndarray  #: fraction of own full output, [0, 1]
    passband_hz: tuple[float, float]  #: -3 dB acoustic edge
    gain_band_hz: tuple[float, float]  #: outer bound of gain_permitted, Hz

    def target_curve_db(self, target_level_db: float) -> np.ndarray:
        """The per-bin target: the fit's scalar level, in this branch's shape."""
        return float(target_level_db) + self.shape_db

    def centred_on(self, level_mask: np.ndarray) -> "BranchTarget":
        """Shape re-centred to add no LEVEL over ``level_mask``; contribution unchanged."""
        mask = np.asarray(level_mask, dtype=bool)
        if not mask.any():
            return self
        shape_db = self.shape_db - float(np.median(self.shape_db[mask]))
        shape_db.flags.writeable = False
        return BranchTarget(
            shape_db=shape_db,
            gain_permitted=self.gain_permitted,
            contribution=self.contribution,
            passband_hz=self.passband_hz,
            gain_band_hz=self.gain_band_hz,
        )


def octave_scaled(hz: float, octaves: float) -> float:
    """``hz`` moved ``octaves`` octaves (signed); 0 and inf are fixed points. Public: reused by
    :mod:`linearization_fit` (#2523).
    """
    if hz <= 0.0 or math.isinf(hz):
        return hz
    return hz * (2.0 ** octaves)


def branch_target(
    sections: Sequence[CrossoverSection],
    freqs_hz: np.ndarray,
    *,
    level_mask: np.ndarray | None = None,
    gain_margin_octaves: float = STOPBAND_GAIN_MARGIN_OCTAVES,
) -> BranchTarget | None:
    """This branch's fit objective, or ``None`` when ``sections`` is empty (role runs FULL
    RANGE). ``level_mask``, if given, re-centres the shape.
    """
    if not sections:
        return None
    if gain_margin_octaves < 0.0:
        raise ValueError(
            "gain_margin_octaves must be non-negative "
            f"(got {gain_margin_octaves})"
        )

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    raw_shape_db = crossover_response_db(freqs, sections)

    # RAW shape: contribution must be 1.0 in the passband, not median-shifted.
    contribution = np.clip(10.0 ** (raw_shape_db / 20.0), 0.0, 1.0)

    shape_db = raw_shape_db
    if level_mask is not None:
        mask = np.asarray(level_mask, dtype=bool)
        if mask.any():
            shape_db = raw_shape_db - float(np.median(raw_shape_db[mask]))

    lo_hz, hi_hz = radiating_band_hz(sections)
    gain_lo_hz = octave_scaled(lo_hz, -gain_margin_octaves)
    gain_hi_hz = octave_scaled(hi_hz, gain_margin_octaves)
    gain_permitted = (freqs >= gain_lo_hz) & (freqs <= gain_hi_hz)

    for array in (shape_db, contribution, gain_permitted):
        array.flags.writeable = False
    return BranchTarget(
        shape_db=shape_db,
        gain_permitted=gain_permitted,
        contribution=contribution,
        passband_hz=(lo_hz, hi_hz),
        gain_band_hz=(gain_lo_hz, gain_hi_hz),
    )
