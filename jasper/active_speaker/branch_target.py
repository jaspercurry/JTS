# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What shape a branch SHOULD be, and where it may spend gain (#1817, #1968).

Owns the per-branch fit objective: the target curve, the band a correction
filter may put gain in, and the branch's contribution weight. It owns no
filters, no level, no trim/delay/summed model and no verdict; it never
re-derives a crossover (sections come from :mod:`branch_chain`) and never
gates. Contribution weights the GAIN side only — a cut outside the passband
is ordinary useful work and spends no headroom (#1809).
"""
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

# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #

#: How far past its acoustic passband edge a branch may still place GAIN, in
#: **octaves**. #1968's hard rule states ~1/2-1 octave; this is the strict end.
STOPBAND_GAIN_MARGIN_OCTAVES: float = 0.5

#: Gain, in dB, below which a realized filter cascade is treated as putting
#: nothing into a band. Equal to ``linearization_fit._MIN_FILTER_GAIN_DB``;
#: stated rather than imported because that module imports this one, and pinned
#: equal by a contract test.
SIGNIFICANT_GAIN_DB: float = 0.5


@dataclass(frozen=True)
class BranchTarget:
    """One branch's fit objective, on the caller's frequency grid.

    Every array is the length of the grid handed to :func:`branch_target`,
    aligned to it bin for bin, and marked non-writeable.
    """

    #: The target's SHAPE, dB relative to the branch's own passband level. Raw
    #: crossover magnitude as built; :meth:`centred_on` re-centres it.
    shape_db: np.ndarray
    #: True where a correction filter may put GAIN — the passband widened by
    #: :data:`STOPBAND_GAIN_MARGIN_OCTAVES`.
    gain_permitted: np.ndarray
    #: This branch's output as a fraction of its own full output, ``[0, 1]``.
    #: A heuristic de-emphasis weight, not a prediction of how much of a boost
    #: reaches the sum.
    contribution: np.ndarray
    #: The acoustic passband edge, Hz — the -3 dB span.
    passband_hz: tuple[float, float]
    #: The outer bound of :attr:`gain_permitted`, Hz.
    gain_band_hz: tuple[float, float]

    def target_curve_db(self, target_level_db: float) -> np.ndarray:
        """The per-bin target: the fit's scalar level, in this branch's shape."""
        return float(target_level_db) + self.shape_db

    def centred_on(self, level_mask: np.ndarray) -> "BranchTarget":
        """This target with its shape re-centred to add no LEVEL over
        ``level_mask`` — the band the fit reads ``target_level_db`` over.

        Lives on the record because the mask is solved inside the fit, long
        after the composer built this. :attr:`contribution` is never re-centred:
        it is an amplitude fraction, not a shape relative to a level.
        """
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
    """``hz`` moved ``octaves`` octaves (signed); 0 and inf are fixed points.

    Public because :mod:`jasper.active_speaker.linearization_fit` widens the
    same radiating band by the same margin to bound its solve (#2523).
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
    """This branch's fit objective, or ``None`` when it has no crossover.

    Empty ``sections`` means ``None`` rather than a flat target: a role with no
    committed region runs FULL RANGE in the emitted graph, so it has no shape to
    hold it to and no stopband to guard. ``level_mask`` is the band the fit
    reads ``target_level_db`` over; the shape is re-centred on it, and ``None``
    leaves the raw crossover magnitude uncentred.
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

    # Reads the RAW shape: an amplitude fraction of this branch's own full
    # output must be 1.0 in the passband, not shifted to re-centre a median.
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
