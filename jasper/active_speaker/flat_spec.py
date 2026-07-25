# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The flat-linearization spec evaluator (flat-linearization plan, stage S2).

Pure computation only: numpy plus stdlib `math`. No I/O, no logging, no
product policy, no CamillaDSP/emission imports. This module answers exactly
one question -- "does this spatially-combined, 1/3-oct-smoothed magnitude
curve meet the flat-linearization spec?" -- and nothing more. Wiring this
into `/state`, a wizard, or the conductor is later work; this module ships
as a pure, uncalled evaluator, mirroring
:mod:`jasper.active_speaker.linearization_envelope`'s shape (a pure module
with zero production callers at the time it shipped -- see that module's
docstring for the same pattern).

See docs/flat-linearization-plan.md, section "The spec -- what 'flat' means
here," for the adopted definition this module implements: reference = power
mean over 250 Hz-8 kHz (non-excluded bins only), deviation = curve -
reference, evaluated per band at the tolerances in :data:`SPEC_BANDS`, with
interference-flagged bins excluded from both the reference and every band's
deviation metric.

**The tolerances are S0-contingent, not final.** The plan doc is explicit
that this table is provisional pending the S0 validation session's hardware
data (mic-move-only captures, no DSP changes) -- see the plan's "Rationale,
briefly" paragraph and "Open questions" items 1-2, 5 for exactly what S0/S3
may revise (achievable N/spread, the 250-vs-300 Hz lower edge, and whether
8-16 kHz can tighten from +/-2.5 to +/-2.0 dB once realization headroom is
measured bounce-free). "If S0 contradicts a tolerance, the table is revised
with the S0 data attached -- the spec serves the measurement, not the
reverse" (plan wording). This module encodes whichever table is currently
adopted; it does not itself track provisional-vs-final status.

Input contract: the caller supplies an already spatially-combined, 1/3-oct-
smoothed magnitude curve (`spec_smoothed_db`) on a matching frequency axis
(`freqs_hz`), plus an optional per-bin exclusion mask (the plan's
"interference honesty screen" -- cepstral power-vs-median disagreement
flags). This module does not combine captures, smooth curves, or detect
interference; it only evaluates a curve that has already been through those
upstream steps. It deliberately consumes plain arrays rather than a
specific upstream result type, so it stays decoupled from however the curve
was produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

# The adopted spec table -- docs/flat-linearization-plan.md, "The spec --
# what 'flat' means here." Each entry is (f_lo_hz, f_hi_hz, tolerance_db);
# band membership is f_lo <= f < f_hi (inclusive-lower, exclusive-upper --
# see `evaluate_flat_spec`'s docstring for why and where this matters at
# the 2 kHz / 8 kHz seams). S0-CONTINGENT (see module docstring): revise
# only with S0/S3 data attached, per the plan's own "the spec serves the
# measurement, not the reverse" rule.
SPEC_BANDS: tuple[tuple[float, float, float], ...] = (
    (250.0, 2000.0, 1.5),
    (2000.0, 8000.0, 2.0),
    (8000.0, 16000.0, 2.5),
)

# The reference band the plan deliberately restricts to the two TIGHT
# bands (SPEC_BANDS[0] union SPEC_BANDS[1] = 250 Hz-8 kHz) "so a top-octave
# deficit cannot re-center the target" (plan wording, verbatim). Uses the
# same inclusive-lower/exclusive-upper edge rule as SPEC_BANDS, so it spans
# exactly those two bands with no gap or overlap at the 2 kHz seam.
REFERENCE_BAND_HZ: tuple[float, float] = (250.0, 8000.0)

# Above this frequency the plan's table reads "best-effort, disclosed,
# never specced" -- never evaluated against a tolerance, never counted
# toward overall_passed. A bin at exactly this frequency is best-effort,
# not the top of SPEC_BANDS[-1] (whose upper edge is exclusive at this same
# value) -- the two partitions meet with no gap or overlap.
BEST_EFFORT_ABOVE_HZ: float = 16000.0


@dataclass(frozen=True)
class BandResult:
    """One :data:`SPEC_BANDS` entry's evaluation outcome.

    ``n_bins`` is the total number of ``freqs_hz`` bins landing in
    ``[f_lo_hz, f_hi_hz)``, regardless of exclusion; ``n_excluded`` is how
    many of those were interference-flagged and therefore excluded from
    ``max_deviation_db`` / ``rms_db``. ``n_bins - n_excluded`` is the
    number of bins the deviation metrics were actually computed from.
    """

    f_lo_hz: float
    f_hi_hz: float
    tolerance_db: float
    max_deviation_db: float
    rms_db: float
    n_bins: int
    n_excluded: int
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "tolerance_db": self.tolerance_db,
            "max_deviation_db": self.max_deviation_db,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class FlatSpecReport:
    """The full flat-spec evaluation for one combined+smoothed curve.

    ``excluded_intervals`` collapses contiguous (by array index -- see
    :func:`evaluate_flat_spec`'s docstring for the ascending-``freqs_hz``
    assumption this relies on) runs of the exclusion mask into merged
    ``(f_lo_hz, f_hi_hz)`` tuples spanning each run's first and last
    excluded bin. Diagnostic disclosure only -- pass/fail itself reads the
    exclusion mask directly, not this derived field. ``best_effort_above_hz``
    echoes :data:`BEST_EFFORT_ABOVE_HZ` so a consumer rendering this report
    doesn't need a second import to know where the disclosed-only region
    begins.
    """

    reference_db: float
    bands: tuple[BandResult, ...]
    overall_passed: bool
    excluded_intervals: tuple[tuple[float, float], ...]
    best_effort_above_hz: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_db": self.reference_db,
            "bands": [band.to_dict() for band in self.bands],
            "overall_passed": self.overall_passed,
            "excluded_intervals": [list(interval) for interval in self.excluded_intervals],
            "best_effort_above_hz": self.best_effort_above_hz,
        }


def _power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` -- the power (energy) mean the plan's
    combiner and this evaluator both use, NOT a linear average of dB
    values. This exact linear-dB-mean-vs-power-mean confusion has shipped
    wrong at least three times in this repo, so this helper is the single
    place the conversion happens; :func:`evaluate_flat_spec` never inlines
    it.
    """
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


def _merged_excluded_intervals(
    freqs_hz: np.ndarray, exclusion_mask: np.ndarray
) -> tuple[tuple[float, float], ...]:
    """Collapse contiguous (by array index) runs of ``True`` in
    ``exclusion_mask`` into merged ``(f_lo_hz, f_hi_hz)`` tuples spanning
    each run's first and last excluded bin's frequency.

    Assumes ``freqs_hz`` is ascending: index-adjacency is used as a proxy
    for frequency-adjacency, which only merges genuinely adjacent
    frequency bins when the axis is sorted. Every caller in this codebase
    passes an ascending frequency axis (the upstream combiner/smoother
    produces one), so this is a documented assumption, not a validated
    one -- see :func:`evaluate_flat_spec`'s docstring.
    """
    if not exclusion_mask.any():
        return ()
    excluded_idx = np.flatnonzero(exclusion_mask)
    run_breaks = np.flatnonzero(np.diff(excluded_idx) > 1) + 1
    runs = np.split(excluded_idx, run_breaks)
    return tuple((float(freqs_hz[run[0]]), float(freqs_hz[run[-1]])) for run in runs)


def evaluate_flat_spec(
    freqs_hz: np.ndarray,
    spec_smoothed_db: np.ndarray,
    exclusion_mask: np.ndarray | None = None,
) -> FlatSpecReport:
    """Evaluate the flat-linearization spec against one combined, 1/3-oct-
    smoothed magnitude curve (docs/flat-linearization-plan.md, "The spec --
    what 'flat' means here").

    Args:
        freqs_hz: 1-D ascending frequency axis, Hz.
        spec_smoothed_db: 1-D magnitude curve, dB, same length as
            ``freqs_hz`` -- the spatially-combined, 1/3-oct-smoothed curve
            the plan's Instrument stage (S1) produces. This module does
            not smooth or combine; it consumes the result verbatim.
        exclusion_mask: optional 1-D bool array, same length as
            ``freqs_hz``. ``True`` marks an interference-flagged bin (the
            plan's cepstral power-vs-median disagreement screen) --
            excluded from the reference-level computation AND from every
            band's deviation metrics. ``None`` (the default) excludes
            nothing.

    Reference level: the power mean (:func:`_power_mean_db`) over
    non-excluded bins inside :data:`REFERENCE_BAND_HZ`. Deliberately spans
    only the two tight-tolerance bands so a top-octave deficit cannot
    re-center the target (plan wording, verbatim).

    Deviation: ``spec_smoothed_db - reference_db``, evaluated per
    :data:`SPEC_BANDS` entry over that band's non-excluded bins:
    ``max_deviation_db`` is the max absolute deviation, ``rms_db`` is the
    RMS deviation, and ``passed`` is ``max_deviation_db <= tolerance_db``.
    :attr:`FlatSpecReport.overall_passed` is the AND of every band's
    ``passed``.

    Band membership is ``f_lo <= f < f_hi`` -- inclusive-lower,
    exclusive-upper -- for both :data:`SPEC_BANDS` entries and
    :data:`REFERENCE_BAND_HZ` (the same rule, applied uniformly, is what
    keeps the reference band's span exactly equal to
    ``SPEC_BANDS[0] union SPEC_BANDS[1]`` with no gap or overlap at the
    2 kHz seam, and keeps :data:`BEST_EFFORT_ABOVE_HZ` exactly adjacent to
    ``SPEC_BANDS[-1]``'s exclusive upper edge). A bin at exactly 2000 Hz
    therefore lands in the 2-8 kHz band, not 250 Hz-2 kHz; a bin at exactly
    8000 Hz lands in 8-16 kHz; a bin at exactly 16000 Hz is best-effort,
    not the top of 8-16 kHz.

    Bins at or above :data:`BEST_EFFORT_ABOVE_HZ` are never evaluated and
    never fail -- they simply do not appear in any :class:`BandResult`.
    The plan calls this region "best-effort, disclosed, never specced,"
    which this module satisfies by omission (plus naming where the region
    starts via :attr:`FlatSpecReport.best_effort_above_hz`), not by
    computing anything for it.

    Raises:
        ValueError: for any degenerate input -- empty or non-1-D arrays,
            mismatched array lengths, a :data:`SPEC_BANDS` band or
            :data:`REFERENCE_BAND_HZ` left with zero non-excluded bins (no
            evidence to evaluate, never silently skipped), or any
            non-finite (NaN/Inf) value in ``freqs_hz`` or
            ``spec_smoothed_db``.
    """
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    spec_smoothed_db = np.asarray(spec_smoothed_db, dtype=np.float64)

    if freqs_hz.ndim != 1 or spec_smoothed_db.ndim != 1:
        raise ValueError(
            "freqs_hz and spec_smoothed_db must be 1-D arrays "
            f"(got ndim={freqs_hz.ndim} and ndim={spec_smoothed_db.ndim})"
        )
    if freqs_hz.size == 0 or spec_smoothed_db.size == 0:
        raise ValueError("freqs_hz and spec_smoothed_db must not be empty")
    if freqs_hz.shape != spec_smoothed_db.shape:
        raise ValueError(
            f"freqs_hz shape {freqs_hz.shape} does not match "
            f"spec_smoothed_db shape {spec_smoothed_db.shape}"
        )

    if exclusion_mask is None:
        resolved_exclusion_mask = np.zeros_like(freqs_hz, dtype=bool)
    else:
        resolved_exclusion_mask = np.asarray(exclusion_mask, dtype=bool)
        if resolved_exclusion_mask.shape != freqs_hz.shape:
            raise ValueError(
                f"exclusion_mask shape {resolved_exclusion_mask.shape} does not "
                f"match freqs_hz shape {freqs_hz.shape}"
            )

    if not np.all(np.isfinite(freqs_hz)) or not np.all(np.isfinite(spec_smoothed_db)):
        raise ValueError(
            "freqs_hz and spec_smoothed_db must contain only finite values "
            "(found NaN or Inf)"
        )

    included_mask = ~resolved_exclusion_mask

    ref_lo_hz, ref_hi_hz = REFERENCE_BAND_HZ
    ref_band_mask = (freqs_hz >= ref_lo_hz) & (freqs_hz < ref_hi_hz) & included_mask
    if not ref_band_mask.any():
        raise ValueError(
            f"reference band {ref_lo_hz}-{ref_hi_hz} Hz has zero non-excluded "
            "bins; cannot compute reference level"
        )
    reference_db = _power_mean_db(spec_smoothed_db[ref_band_mask])

    deviation_db = spec_smoothed_db - reference_db

    band_results: list[BandResult] = []
    for f_lo_hz, f_hi_hz, tolerance_db in SPEC_BANDS:
        band_mask = (freqs_hz >= f_lo_hz) & (freqs_hz < f_hi_hz)
        included_band_mask = band_mask & included_mask
        if not included_band_mask.any():
            raise ValueError(
                f"band {f_lo_hz}-{f_hi_hz} Hz has zero non-excluded bins; "
                "cannot evaluate flat spec"
            )
        band_deviation_db = deviation_db[included_band_mask]
        max_deviation_db = float(np.max(np.abs(band_deviation_db)))
        rms_db = float(np.sqrt(np.mean(np.square(band_deviation_db))))
        band_results.append(
            BandResult(
                f_lo_hz=float(f_lo_hz),
                f_hi_hz=float(f_hi_hz),
                tolerance_db=float(tolerance_db),
                max_deviation_db=max_deviation_db,
                rms_db=rms_db,
                n_bins=int(band_mask.sum()),
                n_excluded=int((band_mask & resolved_exclusion_mask).sum()),
                passed=bool(max_deviation_db <= tolerance_db),
            )
        )

    overall_passed = all(band.passed for band in band_results)
    excluded_intervals = _merged_excluded_intervals(freqs_hz, resolved_exclusion_mask)

    return FlatSpecReport(
        reference_db=reference_db,
        bands=tuple(band_results),
        overall_passed=overall_passed,
        excluded_intervals=excluded_intervals,
        best_effort_above_hz=float(BEST_EFFORT_ABOVE_HZ),
    )
