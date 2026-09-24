# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Listening-position figures and their reductions. See ADR-0325.

The rule governing every figure: SHAPE figures (``dip``, ``ripple_db``,
``handover.hole_db``) are read on the in-band-mean-removed difference, so
they say nothing about output, and LEVEL figures (``band_level_db``,
``low_bass.level_db``, ``handover.level_db``) carry it. **Show a level figure
beside every shape figure**, or a merely quieter candidate reads as an
improvement. Bands are half-open, ``[lo, hi)``; missing or short data is
disclosed with a ``REASON_*`` code and a ``None`` figure, never filled in.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import (
    CANONICAL_SHOULDER_RATIOS, band_levels_from_magnitude, smooth_fractional_octave,
)
from jasper.audio_measurement.band_ladders import LATE_ENERGY_BAND_HZ, UPPER_BANDS_HZ
from jasper.audio_measurement.evidence_reasons import (
    REASON_COVERAGE_SHORT, REASON_NO_COMPARISON, REASON_NO_REPEATS, REASON_NO_ROW,
)
from jasper.json_fields import finite_float

#: Figures are read on 1/6-octave smoothed level; the frozen reference curve
#: is the one-octave trend of the batch's reference take.
FIGURE_FRACTION = 6
REFERENCE_FRACTION = 1

#: A minimum shallower than this is not called a dip.
DIP_MIN_DEPTH_DB = 1.0

#: The measured-dip search window around a geometric or declared estimate:
#: a dip can shift up by ``1 / cos(45°) ≈ 1.41`` between on-axis and a 45°
#: bearing (issue #5330), so the window multiplies and divides by a margin
#: above that.
DIP_SEARCH_WINDOW_RATIO = 1.5

#: Two dips closer than one 1/6-octave smoothing window are not independently
#: resolved by the curve, so they are the same dip and not a shift.
DIP_SHIFT_MIN_OCTAVES = 1.0 / FIGURE_FRACTION

#: The hand-over is read over this many octaves either side of its frequency.
HANDOVER_HALF_OCTAVES = 0.5

#: Where the comparison band came from.
BAND_SOURCE_MEASURED_DIP = "measured_dip"
BAND_SOURCE_DECLARED_GEOMETRY = "declared_geometry"
BAND_SOURCE_SECTION_BAND = "section_band"
BAND_SOURCE_COVERAGE = "coverage"

#: The scalar figures a repeat spread and a regression are read on, each with
#: the sign that makes a change from the incumbent a regression: more ripple,
#: a deeper dip and a deeper hole are worse, a lower level is worse.
FIGURE_REGRESSION_SIGN: Mapping[str, float] = {
    "ripple_db": 1.0, "dip.depth_db": 1.0, "handover.hole_db": 1.0,
    "band_level_db": -1.0, "low_bass.level_db": -1.0,
}


LATE_ENERGY_CHANGE_KEYS = (("early_late_change_db", "early_late_db"),
                           ("band_energy_change_db", "energy_db"), ("arrival_shift_ms", "centroid_ms"))
# Early/late windows of the cardioid-or-fill figure; see ADR-0325.
EARLY_WINDOW_MS = (0.0, 10.0)
LATE_WINDOW_MS = (10.0, 40.0)
CENTROID_WINDOW_MS = (-2.0, 40.0)
# 1.46 Hz bins keep the 1/6-octave figures honest at 30 Hz.
IMPULSE_FFT_SIZE = 32768


def band_limited_impulse(freqs_hz: Any, transfer: Any, band_hz: Sequence[float]) -> np.ndarray:
    freqs = np.asarray(freqs_hz)
    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    return np.fft.irfft(np.asarray(transfer) * mask, n=2 * (len(freqs) - 1))


def impulse_energy_figures(ir: Any, *, sample_rate_hz: int) -> dict[str, float]:
    impulse = np.asarray(ir, dtype=float)
    peak = int(np.argmax(np.abs(impulse)))
    time_ms = (np.arange(impulse.size) - peak) * (1000.0 / sample_rate_hz)
    energy = impulse ** 2

    def window(bounds: tuple[float, float]) -> np.ndarray:
        return (time_ms >= bounds[0]) & (time_ms < bounds[1])

    early = max(float(np.sum(energy[window(EARLY_WINDOW_MS)])), 1e-30)
    late = max(float(np.sum(energy[window(LATE_WINDOW_MS)])), 1e-30)
    held = window(CENTROID_WINDOW_MS)
    total = max(float(np.sum(energy[held])), 1e-30)
    return {"t0_ms": peak * 1000.0 / sample_rate_hz,
            "early_late_db": 10.0 * math.log10(early / late),
            "energy_db": 10.0 * math.log10(total),
            "centroid_ms": float(np.sum(time_ms[held] * energy[held])) / total}


def impulse_late_energy(ir: np.ndarray, *, sample_rate_hz: int) -> dict[str, float]:
    """Early/late figures of the 90–250 Hz band-limited impulse,
    windowed around this take's OWN peak."""
    peak = int(np.argmax(np.abs(ir)))
    # 5 ms pre / 350 ms post: the window the figure was validated on (issue #5374).
    start = max(0, peak - round(0.005 * sample_rate_hz))
    stop = min(ir.size, peak + round(0.350 * sample_rate_hz))
    spectrum = np.fft.rfft(ir[start:stop], n=IMPULSE_FFT_SIZE)
    impulse = band_limited_impulse(
        np.fft.rfftfreq(IMPULSE_FFT_SIZE, 1 / sample_rate_hz), spectrum, LATE_ENERGY_BAND_HZ,
    )
    return impulse_energy_figures(impulse, sample_rate_hz=sample_rate_hz)


def late_energy_medians(rows: Sequence[Mapping[str, float]]) -> dict[str, float] | None:
    """Each late-energy figure's median over ``rows``, on its own absolute
    scale; ``None`` for no rows. Two changes measured against different
    references compare only after both are re-based on one of these."""
    return {key: float(np.median([row[key] for row in rows])) for _, key in LATE_ENERGY_CHANGE_KEYS} if rows else None


def late_energy_change(
    candidate: Sequence[Mapping[str, float]], reference: Sequence[Mapping[str, float]],
) -> dict[str, Any]:
    """Candidate minus reference at ONE position: each side's median over takes,
    and those medians. These unpaired repeat sets may differ in count, so this
    is a difference of medians, unlike the preview's muted/predicted pairs from
    the same take. An empty side gives ``REASON_NO_COMPARISON`` and ``None``
    figures."""
    sides = {"candidate": late_energy_medians(candidate), "reference": late_energy_medians(reference)}
    compared = sides["candidate"] is not None and sides["reference"] is not None
    return {
        **{label: sides["candidate"][key] - sides["reference"][key] if compared else None
           for label, key in LATE_ENERGY_CHANGE_KEYS},
        **sides,
        "repeats": [len(candidate), len(reference)],
        "reason": "" if compared else REASON_NO_COMPARISON,
    }


def band_level_changes(
    freqs_hz: Any, curve_db: Any, *, reference_db: Any, coverage_hz: Sequence[float],
    bands_hz: Sequence[tuple[float, float]] = UPPER_BANDS_HZ,
) -> list[dict[str, Any]]:
    """Candidate levels against REAR-MUTED on the same grid;
    power means of the 1/6-octave level. ``coverage_hz`` is the swept band,
    not the room-clamped coverage; a band not wholly inside it or without
    bins on this grid is absent."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    bands = [band for band in bands_hz
             if band[0] >= coverage_hz[0] and band[1] <= coverage_hz[1]
             and band_indices(freqs, band).size]
    if not bands:
        return []
    levels, reference = [band_levels_from_magnitude(
        freqs, figure_level_db(freqs, np.asarray(curve, dtype=np.float64)), bands,
    ) for curve in (curve_db, reference_db)]
    return [{"band_hz": [lo, hi], "level_db": float(level), "reference_db": float(zero),
             "change_db": float(level - zero)}
            for (lo, hi), level, zero in zip(bands, levels, reference)]


def reference_curve_db(freqs_hz: Any, curve_db: Any) -> np.ndarray:
    """The frozen zero for one microphone position: the one-octave trend of
    the batch's reference take there. It only sets the zero, so
    candidate-versus-candidate differences do not depend on it, and no
    candidate is measured against its own trend."""
    return smooth_fractional_octave(np.asarray(freqs_hz, dtype=np.float64),
                                    np.asarray(curve_db, dtype=np.float64),
                                    fraction=REFERENCE_FRACTION)


def comparison_band(
    *,
    coverage_hz: Sequence[float],
    ceiling_hz: float,
    reference_take: tuple[Any, Any] | None = None,
    geometric_dip_hz: float | None = None,
    section_band_hz: Sequence[float] | None = None,
    handover_hz: float | None = None,
) -> dict[str, Any]:
    """The ONE band this batch is compared over, chosen once and then held
    for every candidate and position, with its source reported.

    In order: the MEASURED wall dip of ``reference_take`` (its own
    ``(freqs_hz, curve_db)``, read against its own trend because a reference
    take has no other zero — that dip only WINDOWS the comparison and is
    never a reported figure); a supplied geometric dip estimate; the
    incumbent's rear section band; the coverage. A dip's band is
    :data:`~jasper.audio_measurement.analysis.CANONICAL_SHOULDER_RATIOS`
    times its frequency. Always clipped to the coverage and the ceiling; a
    band left with nothing is ``band_hz`` ``None`` with ``coverage_short``,
    never an inverted range.

    The measured-dip search is itself WINDOWED, so a deeper room mode
    elsewhere in the coverage is never mistaken for the wall dip (issue
    #5330): inside ``geometric_dip_hz``'s :data:`DIP_SEARCH_WINDOW_RATIO`
    margin when given, else inside ``section_band_hz`` when given, else
    above ``handover_hz`` when given, else the whole coverage. The window
    searched is reported as ``search_hz``, ``None`` when it misses the
    coverage entirely — a miss falls through to the next source, same as no
    dip found there.
    """
    coverage_clip = _clip(coverage_hz, coverage_hz, ceiling_hz)
    if geometric_dip_hz is not None:
        search_hz = _clip(
            (float(geometric_dip_hz) / DIP_SEARCH_WINDOW_RATIO,
             float(geometric_dip_hz) * DIP_SEARCH_WINDOW_RATIO),
            coverage_hz, ceiling_hz)
    elif section_band_hz is not None:
        search_hz = _clip(section_band_hz, coverage_hz, ceiling_hz)
    elif handover_hz is not None:
        search_hz = _clip((float(handover_hz), coverage_hz[1]), coverage_hz, ceiling_hz)
    else:
        search_hz = coverage_clip
    dip = None
    if search_hz is not None and reference_take is not None:
        freqs = np.asarray(reference_take[0], dtype=np.float64)
        curve = np.asarray(reference_take[1], dtype=np.float64)
        shape = figure_level_db(freqs, curve) - reference_curve_db(freqs, curve)
        dip = _deepest_dip(freqs, shape, search_hz, DIP_MIN_DEPTH_DB)
    lo_ratio, hi_ratio = CANONICAL_SHOULDER_RATIOS
    source: str
    band: tuple[float, float] | None
    if dip is not None:
        source, band = BAND_SOURCE_MEASURED_DIP, (dip["hz"] * lo_ratio, dip["hz"] * hi_ratio)
    elif geometric_dip_hz is not None:
        source, band = BAND_SOURCE_DECLARED_GEOMETRY, (
            float(geometric_dip_hz) * lo_ratio, float(geometric_dip_hz) * hi_ratio)
    elif section_band_hz is not None:
        source, band = BAND_SOURCE_SECTION_BAND, (
            float(section_band_hz[0]), float(section_band_hz[1]))
    else:
        source, band = BAND_SOURCE_COVERAGE, coverage_clip
    clipped = None if band is None else _clip(band, coverage_hz, ceiling_hz)
    return {"band_hz": None if clipped is None else list(clipped), "source": source,
            "dip_hz": dip["hz"] if dip is not None else None,
            "search_hz": None if search_hz is None else list(search_hz),
            "reason": "" if clipped is not None else REASON_COVERAGE_SHORT}


def position_figures(
    freqs_hz: Any,
    curve_db: Any,
    *,
    reference_db: Any,
    band_hz: Sequence[float] | None,
    coverage_hz: Sequence[float],
    handover_hz: float | None = None,
    incumbent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One candidate's symptoms at ONE microphone position.

    ``reference_db`` is that position's frozen :func:`reference_curve_db`, on
    the same grid as ``curve_db``; ``band_hz`` is :func:`comparison_band`'s
    frozen band, and ``None`` discloses every figure as unavailable;
    ``handover_hz`` is where the rear's bass branch hands over to its
    inverted cancellation branch, or ``None`` when the caller cannot name it;
    ``incumbent`` is this same result for the incumbent at the SAME position
    and adds the two relative readings (against itself: no shift, 0.0 dB).
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    curve = np.asarray(curve_db, dtype=np.float64)
    reference = np.asarray(reference_db, dtype=np.float64)
    if freqs.shape != curve.shape or freqs.shape != reference.shape:
        raise ValueError("rear_evidence_curves_unmatched")
    band = None if band_hz is None else (float(band_hz[0]), float(band_hz[1]))
    in_band = np.empty(0, dtype=int) if band is None else band_indices(freqs, band)
    if band is None or in_band.size < 3 or freqs[0] > band[0] or freqs[-1] < band[1]:
        return {"reason": REASON_COVERAGE_SHORT, "dip": None, "dip_shift": None,
                "ripple_db": None, "own_trend_ripple_db": None,
                "handover": None, "low_bass": None, "band_level_db": None}
    level = figure_level_db(freqs, curve)
    shape = level - reference
    shape -= float(np.mean(shape[in_band]))
    window = None if handover_hz is None else _clip(
        (handover_hz * 2.0**-HANDOVER_HALF_OCTAVES, handover_hz * 2.0**HANDOVER_HALF_OCTAVES),
        coverage_hz, coverage_hz[1])
    dip = _deepest_dip(freqs, shape, band, DIP_MIN_DEPTH_DB)
    return {
        "reason": "", "dip": dip, "dip_shift": _dip_shift(dip, incumbent),
        "ripple_db": float(np.sqrt(np.mean(shape[in_band] ** 2))),
        "own_trend_ripple_db": own_trend_ripple_db(freqs, curve, band_hz=band),
        "handover": _handover(freqs, level, reference, shape, handover_hz, window),
        "low_bass": _low_bass(
            freqs, level,
            (float(coverage_hz[0]), band[0] if window is None else window[0]), incumbent),
        "band_level_db": _band_level_db(freqs, level, band),
    }


def own_trend_ripple_db(freqs_hz: Any, curve_db: Any, *, band_hz: Sequence[float]) -> float:
    """Mean-removed RMS against the candidate's own one-octave trend, dB."""
    freqs, curve = np.asarray(freqs_hz, dtype=np.float64), np.asarray(curve_db, dtype=np.float64)
    shape = (figure_level_db(freqs, curve) - reference_curve_db(freqs, curve))[band_indices(freqs, band_hz)]
    return float(np.sqrt(np.mean((shape - np.mean(shape)) ** 2)))


def spread_rms_db(spread_db: Any, freqs_hz: Any, *, band_hz: Sequence[float]) -> float | None:
    """RMS of per-bin cross-position standard deviations over the band, dB."""
    if spread_db is None:
        return None
    spread = np.asarray(spread_db, dtype=np.float64)[band_indices(np.asarray(freqs_hz), band_hz)]
    return float(np.sqrt(np.mean(spread ** 2)))


def repeat_spread(repeats: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Peak-to-peak spread of each figure across repeated takes of ONE
    candidate, position and level — the only thing a difference may be called
    inconclusive against. Never a difference between candidates or positions:
    fewer than two repeats is ``too_few_repeats``, not a substitute for one."""
    enough = len(repeats) > 1
    spread: dict[str, float | None] = {}
    for figure in FIGURE_REGRESSION_SIGN:
        values = [_figure(row, figure) for row in repeats]
        held = [value for value in values if value is not None]
        spread[figure] = max(held) - min(held) if enough and len(held) == len(values) else None
    return {"n_repeats": len(repeats), "reason": "" if enough else REASON_NO_REPEATS,
            "spread_db": spread}


def across_positions(
    rows: Mapping[str, Mapping[str, Any]],
    *,
    incumbent_rows: Mapping[str, Mapping[str, Any]],
    spread_db: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """One candidate's figures pooled over microphone positions, and its
    WORST regression against the incumbent.

    Every figure carries its worst position beside a median, never an average
    that hides a bad position. ``change_db`` is signed by
    :data:`FIGURE_REGRESSION_SIGN`, so a negative worst regression means this
    candidate beat the incumbent everywhere, and ``exceeds_repeat_spread`` is
    ``None`` when the batch measured no spread. ``positions`` counts the
    BATCH's positions — this candidate's keys union the incumbent's — and
    ``positions_unavailable`` maps each that answered nothing to its reason,
    so a position never measured (``no_row``) cannot pass as a smaller batch.
    """
    batch = set(rows) | set(incumbent_rows)
    figures: dict[str, Any] = {}
    worst: dict[str, Any] | None = None
    for figure, sign in FIGURE_REGRESSION_SIGN.items():
        held = {key: value for key, row in rows.items()
                if (value := _figure(row, figure)) is not None}
        figures[figure] = {
            "positions": len(held),
            "worst_db": max(held.values(), key=lambda value: sign * value) if held else None,
            "median_db": float(np.median(list(held.values()))) if held else None,
        }
        for key, value in held.items():
            prior = _figure(incumbent_rows.get(key), figure)
            if prior is None:
                continue
            change = sign * (value - prior)
            if worst is None or change > worst["change_db"]:
                limit = None if spread_db is None else spread_db.get(figure)
                worst = {"position": key, "figure": figure, "change_db": change,
                         "exceeds_repeat_spread": None if limit is None else change > limit}
    return {
        "positions": len(batch),
        "positions_unavailable": {
            key: rows[key].get("reason") if key in rows else REASON_NO_ROW
            for key in sorted(batch) if key not in rows or rows[key].get("reason")
        },
        "figures": figures, "worst_regression": worst,
    }


def figure_level_db(freqs: np.ndarray, curve_db: np.ndarray) -> np.ndarray:
    return smooth_fractional_octave(freqs, curve_db, fraction=FIGURE_FRACTION)


def band_indices(freqs: np.ndarray, band_hz: Sequence[float]) -> np.ndarray:
    """This grid's bins inside the half-open band — the edge rule
    :func:`~jasper.audio_measurement.analysis.band_levels_from_magnitude`
    applies, so a bin counts toward coverage exactly when it counts toward
    the level figures."""
    return np.flatnonzero((freqs >= float(band_hz[0])) & (freqs < float(band_hz[1])))


def _clip(band_hz: Sequence[float], coverage_hz: Sequence[float],
          ceiling_hz: float) -> tuple[float, float] | None:
    """``band_hz`` inside the coverage and the ceiling, or ``None`` when that
    leaves nothing: the edges clamp independently and would otherwise cross
    into an inverted range."""
    lo = max(float(band_hz[0]), float(coverage_hz[0]))
    hi = min(float(band_hz[1]), float(coverage_hz[1]), float(ceiling_hz))
    return (lo, hi) if lo < hi else None


def _band_level_db(freqs: np.ndarray, level: np.ndarray,
                   band_hz: Sequence[float]) -> float | None:
    """Power-mean level over the band in the caller's own dB unit, or ``None``
    when the band holds none of this grid's bins."""
    if not band_indices(freqs, band_hz).size:
        return None
    return float(band_levels_from_magnitude(
        freqs, level, ((float(band_hz[0]), float(band_hz[1])),))[0])


def _deepest_dip(freqs: np.ndarray, shape: np.ndarray, band_hz: Sequence[float],
                 min_depth_db: float) -> dict[str, float] | None:
    """The deepest local minimum at least ``min_depth_db`` below the zero
    inside the band, with its half-depth width. Every in-band sample is a
    candidate — a dip ON the band's own edge is still a dip, and the sample
    beside it is real data one bin outside the band — so only the ARRAY's two
    end samples, which have no neighbour to compare, are skipped."""
    idx = band_indices(freqs, band_hz)
    if idx.size < 3:
        return None
    best: dict[str, float] | None = None
    for position in idx:
        i = int(position)
        if i == 0 or i == freqs.size - 1:
            continue
        depth = -float(shape[i])
        if depth < min_depth_db or (best is not None and depth <= best["depth_db"]):
            continue
        # `<=` left and `<` right keeps one sample of a flat bottom, not both.
        if not (shape[i] <= shape[i - 1] and shape[i] < shape[i + 1]):
            continue
        half = -0.5 * depth
        lo_edge = hi_edge = i
        while lo_edge > idx[0] and shape[lo_edge - 1] <= half:
            lo_edge -= 1
        while hi_edge < idx[-1] and shape[hi_edge + 1] <= half:
            hi_edge += 1
        best = {"hz": float(freqs[i]), "depth_db": depth,
                "width_octaves": float(math.log2(freqs[hi_edge] / freqs[lo_edge]))}
    return best


def _dip_shift(dip: Mapping[str, float] | None,
               incumbent: Mapping[str, Any] | None) -> dict[str, float] | None:
    """This candidate's dip when it is NEW or SHIFTED against the incumbent's
    dip at the same position, else ``None``."""
    if dip is None or incumbent is None:
        return None
    prior = incumbent.get("dip")
    if prior is not None and abs(math.log2(dip["hz"] / prior["hz"])) < DIP_SHIFT_MIN_OCTAVES:
        return None
    return {"hz": dip["hz"], "depth_db": dip["depth_db"]}


def _handover(freqs: np.ndarray, level: np.ndarray, reference: np.ndarray, shape: np.ndarray,
              handover_hz: float | None,
              window: tuple[float, float] | None) -> dict[str, Any] | None:
    """The broad level and the deepest hole in the hand-over window, both
    against the reference. A positive ``hole_db`` is below the reference."""
    if handover_hz is None:
        return None
    idx = band_indices(freqs, window) if window is not None else np.empty(0, dtype=int)
    row: dict[str, Any] = {"hz": float(handover_hz),
                           "window_hz": None if window is None else list(window)}
    if window is None or not idx.size:
        return {**row, "reason": REASON_COVERAGE_SHORT, "level_db": None,
                "hole_db": None, "hole_hz": None}
    broad = _band_level_db(freqs, level, window)
    zero = _band_level_db(freqs, reference, window)
    deepest = int(idx[int(np.argmin(shape[idx]))])
    return {**row, "reason": "",
            "level_db": None if broad is None or zero is None else broad - zero,
            "hole_db": -float(shape[deepest]), "hole_hz": float(freqs[deepest])}


def _low_bass(freqs: np.ndarray, level: np.ndarray, band_hz: tuple[float, float],
              incumbent: Mapping[str, Any] | None) -> dict[str, Any]:
    """Power-mean level from the coverage floor up to the hand-over window —
    up to the comparison band when no hand-over is named — absolute and
    against the incumbent."""
    lo, hi = float(band_hz[0]), float(band_hz[1])
    row: dict[str, Any] = {"band_hz": [lo, hi]}
    absolute = _band_level_db(freqs, level, (lo, hi)) if hi > lo else None
    if absolute is None:
        return {**row, "reason": REASON_COVERAGE_SHORT, "level_db": None, "change_db": None}
    prior = _figure(incumbent, "low_bass.level_db")
    return {**row, "reason": "", "level_db": absolute,
            "change_db": None if prior is None else absolute - prior}


def _figure(row: Mapping[str, Any] | None, path: str) -> float | None:
    """One dotted scalar figure out of a :func:`position_figures` row."""
    node: Any = row
    for key in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return finite_float(node)
