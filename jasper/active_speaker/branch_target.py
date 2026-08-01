# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What shape a branch SHOULD be, and where it may spend gain (#1817, #1968).

**Pipeline stage: PRESCRIBE.** In the measure → derive → diagnose → prescribe →
re-measure chain this module sits immediately before the per-branch
linearization fit. It owns the fit's *objective*: the target curve the branch
is graded against, the band a correction filter may put GAIN in, and the
branch's own contribution weight. It owns **no filters** (the fit designs
those), **no level** (``target_level_db`` stays the fit's own scalar; this
module only shapes around it), **no trim, delay, or summed model**, and no
verdict.

Why it exists
-------------
A branch is measured THROUGH its own crossover, so its measured curve carries
that crossover's rolloff. Grading it against a FLAT line reads the rolloff as a
driver deficit and attracts correction into it — the canonical instance is
#1817: a *perfectly flat* driver behind a 2 kHz LR4 attracts **+2.379 dB at
1570.6 Hz** (0.79·Fc), purely because the target was flat where the branch was
not meant to be.

#1809 bounded the *lift* band to the driver's radiating side and deliberately
stopped there, naming the remainder: "The real fix is to give the fit a
crossover-shaped TARGET rather than a flat one, so no filter fights the
crossover anywhere instead of only outside the radiating band." This module is
that target. The fit stays crossover-ignorant — it is handed a shape and a
mask, exactly as #1809 handed it a band.

The three quantities, and why one record
----------------------------------------
They are the same object — the branch's committed crossover — read three ways,
and they must not be able to disagree:

* :attr:`BranchTarget.shape_db` — **the target's shape.** The committed
  crossover's own magnitude, re-centred (below) so it adds shape without
  moving level.
* :attr:`BranchTarget.gain_permitted` — **where gain may go.** #1968's hard
  rule (below).
* :attr:`BranchTarget.contribution` — **how much this branch is actually
  worth** at each bin, as a share of full output.

A caller holding the shape but a stale mask would grade against one crossover
and guard against another, which is the drift :mod:`branch_chain` exists to
remove for the headroom charge; the same argument applies here.

Units
-----
Frequencies in hertz. ``shape_db`` in decibels relative to the branch's own
passband level (never positive before re-centring — a Butterworth pass is
monotonic and never exceeds unity). ``contribution`` is a dimensionless
amplitude fraction in ``[0, 1]``. ``gain_permitted`` is a boolean mask over the
caller's grid. Margins are in **octaves** (log2 of a frequency ratio).

What this does NOT do
---------------------
* **It never re-derives a crossover.** ``sections`` come from
  :func:`jasper.active_speaker.branch_chain.sections_by_role`, walking the same
  ``preset.crossover_regions`` the emitter builds its filters from, and the
  magnitude comes from :func:`~jasper.active_speaker.branch_chain.
  crossover_response_db` — the DIGITAL sections CamillaDSP realizes. A branch
  with no committed region gets ``None`` from :func:`branch_target`, not an
  invented crossover, for the same reason ``sections_by_role`` invents none: it
  runs full range in the emitted graph.
* **It never re-weights CUTS, and that is a deliberate departure from its own
  research input.** #1968 offers contribution weighting as an equivalent
  formulation of the hard rule — "weight the least-squares error by each
  branch's relative contribution to the sum at each frequency, so **stopband
  gain** has ~zero leverage". Read literally as a weighting of the whole
  least-squares error it would also shrink cut authority outside the passband,
  and #1809 measured that case and ruled the other way: a cut there "is
  ordinary useful work — the crossover has attenuated the band but not silenced
  it, and whatever leaks through still reaches the summed response", and it
  spends no headroom. The research is a hypothesis source, not an authority.
  So :attr:`~BranchTarget.contribution` is applied to the GAIN side only, which
  is the side the research's own stated purpose ("stopband gain has ~zero
  leverage") is about, and cuts keep reaching the fit band's edge.
* **It never gates.** Nothing here refuses a session or moves a verdict; the
  fit consumes it.
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

#: How far past its acoustic passband edge a branch may still place GAIN,
#: in **octaves** — #1968's hard rule, at the strict end of its stated range.
#:
#: The rule as delivered (issue #1968, Q5 and the acceptance checklist): "no
#: branch correction filter may have significant gain more than ~1/2-1 octave
#: beyond that branch's acoustic passband edge". The range is a range because
#: the report gives no measurement that separates its ends; a GUARD picks the
#: strict end, because the two errors are not symmetric. Too strict withholds
#: gain from bins where the branch is already heavily attenuated — bins whose
#: deficit the SUM does not have and the sibling branch owns. Too loose readmits
#: exactly the pathology the rule exists to stop, at full headroom cost.
#:
#: What 1/2 octave buys, at the edge definition below (LR4, the shipped
#: default): the passband edge sits at 0.801*Fc, so the gain band ends at
#: 0.801*sqrt(2)*Fc = 1.133*Fc, where the crossover is 8.46 dB down. A branch
#: 8.46 dB below its sibling moves the summed output by 0.285 dB per 1 dB of
#: its own boost — roughly 29% leverage. At the loose end (1 octave, 1.602*Fc,
#: 17.6 dB down) that falls to 0.12 dB per dB, ~12%. So this constant is the
#: statement "a branch may not spend headroom where less than about a third of
#: it reaches the listener", and the loose end would have said one eighth.
#:
#: Corroborated independently by the report's own Key Finding 4 (audioXpress/
#: Voice Coil, Honeycutt), which gives the conventional clean-response margin
#: for a 2nd-order pass as "+40% (half-octave)" past Fc — the same half octave,
#: derived from slope order rather than from summation leverage.
STOPBAND_GAIN_MARGIN_OCTAVES: float = 0.5

#: Gain, in dB, below which a realized filter cascade is treated as putting
#: nothing into a band.
#:
#: Deliberately the fit's own :data:`~jasper.active_speaker.linearization_fit.
#: _MIN_FILTER_GAIN_DB` (0.5 dB) rather than a second, smaller number: that is
#: already the threshold below which this system declines to place a filter at
#: all, so a skirt residue beneath it is not gain any stage here would have
#: emitted deliberately. A tighter epsilon would refuse cascades over
#: differences the fit cannot express.
#:
#: Stated as its own constant, not imported, because ``linearization_fit``
#: imports THIS module; the value is pinned equal by a contract test.
SIGNIFICANT_GAIN_DB: float = 0.5


@dataclass(frozen=True)
class BranchTarget:
    """One branch's fit objective, on the caller's frequency grid.

    Every array is the length of the grid handed to :func:`branch_target` and
    is aligned to it bin for bin. Immutable and read-only (the arrays are
    marked non-writeable) because four consumers inside one fit read them and a
    stage that scaled one in place would silently re-grade the ones after it.
    """

    #: The target's SHAPE, dB. As built by :func:`branch_target` this is the
    #: raw crossover magnitude; :meth:`centred_on` is what re-centres it to add
    #: no level, and the fit calls that before using it. Add to the fit's
    #: scalar ``target_level_db`` to get the per-bin target curve.
    shape_db: np.ndarray
    #: True where a correction filter may put GAIN — the passband widened by
    #: :data:`STOPBAND_GAIN_MARGIN_OCTAVES`.
    gain_permitted: np.ndarray
    #: This branch's output as a fraction of its own full output, ``[0, 1]``.
    #: 1.0 deep in the passband, falling through the crossover.
    #:
    #: The contribution weight #1968 asks for, on the gain side only. **A
    #: heuristic weighting, not a physical identity**: the consumer multiplies
    #: a dB deficit by this amplitude fraction, which is monotone
    #: de-emphasis in the right direction rather than a derived quantity. It is
    #: honest as a weight and would not be honest as a prediction of how much
    #: of a boost reaches the sum.
    contribution: np.ndarray
    #: The acoustic passband edge, Hz — :func:`~jasper.active_speaker.
    #: branch_chain.radiating_band_hz`, the -3 dB span.
    passband_hz: tuple[float, float]
    #: The outer bound of :attr:`gain_permitted`, Hz.
    gain_band_hz: tuple[float, float]

    def target_curve_db(self, target_level_db: float) -> np.ndarray:
        """The per-bin target: the fit's scalar level, in this branch's shape.

        The scalar keeps its exact meaning — "where this driver's passband
        SITS" — and this adds only "what shape it should be". Separating them
        is what lets ``target_level_db`` stay the number every level consumer
        (the residual claim, the VERIFY/OBSERVE ladders,
        ``driver_core_level_db``, ``correction_giveback_db``) already reads.
        """
        return float(target_level_db) + self.shape_db

    def centred_on(self, level_mask: np.ndarray) -> "BranchTarget":
        """This target with its shape re-centred to add no LEVEL over
        ``level_mask`` — the band the fit reads ``target_level_db`` over.

        ``shape_db -= median(shape_db over level_mask)``. Without it the target
        drifts DOWN relative to the measurement it grades: the level band
        reaches to the -3 dB edge, so the raw crossover magnitude's median over
        it sits a few tenths of a dB below zero, and the fit would read the
        whole passband as that much too loud and cut it. Re-centring makes the
        residual's median over the level band zero — exactly what a flat target
        at the same scalar does — so the shape carries only the DIFFERENCE from
        flat, which is the whole quantity #1817 is about.

        **Here and not in :func:`branch_target` because the mask is the FIT's.**
        The level mask is solved inside ``fit_driver_linearization`` from the
        envelope, well after the composer has built this record, so the
        composer cannot supply it. Putting the formula on the record keeps one
        owner for it rather than one copy at each call site.

        An empty (or all-False) mask returns ``self`` unchanged: there is no
        band to centre on, and a median over nothing is not a level.
        :attr:`contribution` is never re-centred — it is a physical amplitude
        fraction of this branch's full output, not a shape relative to a level.
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


def _octave_scaled(hz: float, octaves: float) -> float:
    """``hz`` moved ``octaves`` octaves (signed). 0 and inf are fixed points —
    a branch with no low-pass has no upper gain bound to widen, and one with no
    high-pass has no lower bound."""
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

    ``sections`` are the branch's committed Linkwitz-Riley sections
    (:func:`~jasper.active_speaker.branch_chain.sections_by_role`). **Empty
    means ``None``**, not a flat target object: a role with no committed region
    runs FULL RANGE in the emitted graph, so it has no shape to hold it to and
    no stopband to guard — and returning ``None`` gives the fit its documented
    pre-#1817 path byte for byte, which is what keeps a one-way box and the
    room-correction lane unchanged.

    ``level_mask`` is the band the fit reads its ``target_level_db`` median
    over. The shape is **re-centred on it**: ``shape_db -= median(shape_db over
    level_mask)``. Without that the target would drift DOWN relative to the
    measurement it is graded against — the level band reaches to the -3 dB
    edge, so the raw shape's median over it is a few tenths of a dB below zero,
    and the fit would read the whole passband as that much too loud and cut it.
    Re-centring makes the residual's median over the level band zero, exactly
    as a flat target at the same scalar does; the shape then carries only the
    DIFFERENCE from flat, which is the whole quantity #1817 is about. ``None``
    (or an empty mask) leaves the raw crossover magnitude uncentred.

    ``gain_margin_octaves`` is #1968's rule; see
    :data:`STOPBAND_GAIN_MARGIN_OCTAVES` for the choice of its value. A
    negative margin is a caller bug — it would narrow the gain band INSIDE the
    passband the fit is entitled to correct — and raises rather than quietly
    refusing the fit most of its own band.
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

    # Contribution reads the RAW shape: it is a physical amplitude fraction of
    # this branch's own full output, so it must be 1.0 in the passband and fall
    # with the real crossover — not with a curve that has been shifted to make
    # a median land on zero.
    contribution = np.clip(10.0 ** (raw_shape_db / 20.0), 0.0, 1.0)

    shape_db = raw_shape_db
    if level_mask is not None:
        mask = np.asarray(level_mask, dtype=bool)
        if mask.any():
            shape_db = raw_shape_db - float(np.median(raw_shape_db[mask]))

    lo_hz, hi_hz = radiating_band_hz(sections)
    gain_lo_hz = _octave_scaled(lo_hz, -gain_margin_octaves)
    gain_hi_hz = _octave_scaled(hi_hz, gain_margin_octaves)
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
