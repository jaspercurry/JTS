# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured symptoms of a rear-stage comparison (issue #5330).

Pure arithmetic on ungated curves: no I/O, no repo state, no knowledge of
rounds, manifests, candidates or CamillaDSP. The measured view and the
no-sound preview call the SAME functions, so every input is a plain array on
the caller's own frequency grid. A SUMMED take's figures read magnitude in dB;
a PAIR take's read the complex transfer of each segment, because the trust
number and the polarity are both complex sums. A batch freezes one reference
curve per position (:func:`reference_curve_db`) and one band
(:func:`comparison_band`) before any candidate is read; each function's
docstring carries its contract. See ADR-0325 for what a comparison does and
does not claim.

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

from jasper.audio_measurement.alignment import (
    DEFAULT_CONFIDENCE_THRESHOLD, GCC_UPSAMPLE, gcc_phat,
)
from jasper.audio_measurement.analysis import (
    CANONICAL_SHOULDER_RATIOS,
    THIRD_OCTAVE_BASS_BANDS_HZ,
    band_levels_from_magnitude,
    smooth_fractional_octave,
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

#: Why a figure or a position is unavailable.
REASON_COVERAGE_SHORT = "coverage_short"
REASON_NO_REPEATS = "no_repeats"
REASON_NO_ROW = "no_row"
REASON_NO_IMPULSE = "no_impulse"
#: A gap the correlator DID read, under its confidence gate. Distinct from
#: :data:`REASON_NO_IMPULSE`: the segments were there, the answer is not
#: trustworthy enough to remove from a phase.
REASON_GAP_NOT_CONFIDENT = "gap_not_confident"
#: A batch with ONE played candidate: it reads no candidate figures, so it has
#: no repeat spread for a difference to be called real against. Its own repeat
#: evidence is per position, beside each measured figure.
REASON_NO_COMPARISON = "no_candidate_comparison"

#: Applied before the log so a bin that cancelled to exactly zero banks a
#: number instead of ``-inf``, which is not JSON — the same floor
#: :func:`~jasper.audio_measurement.deconv.magnitude_response` applies.
_MAGNITUDE_FLOOR = 1e-12

#: A woofer is radiating within this much of its own peak level. The gap needs
#: real BANDWIDTH (a GCC-PHAT main lobe is ~1/bandwidth wide, so a third-octave
#: span cannot resolve a sub-millisecond gap) and whitening a bin the woofers
#: never drove piles a peak at zero lag.
RADIATING_FLOOR_BELOW_PEAK_DB = 20.0

#: Both woofers sit in one cabinet, so their path difference to the microphone
#: is bounded by its depth (~0.3 m, ~0.9 ms); past this the peak is not the
#: pair, and the primitive's own ``at_edge`` says so.
ARRIVAL_GAP_SEARCH_MS = 2.0

#: Mean ``angle(R/F)`` with the arrival gap removed, degrees: at or below the
#: first the rear tracks the front, at or above the second it opposes it.
POLARITY_SAME_MAX_DEG = 60.0
POLARITY_INVERTED_MIN_DEG = 120.0
#: Read on the lowest bands only, where a half wavelength is long against the
#: woofers' spacing so the ratio's phase cannot have wrapped, and skipping a
#: band this far under the loudest one either woofer reached (the noise floor).
POLARITY_BAND_COUNT = 3
POLARITY_FLOOR_BELOW_PEAK_DB = 25.0

POLARITY_SAME = "same"
POLARITY_INVERTED = "inverted"
POLARITY_UNCLEAR = "unclear"

#: The scalar figures a repeat spread and a regression are read on, each with
#: the sign that makes a change from the incumbent a regression: more ripple,
#: a deeper dip and a deeper hole are worse, a lower level is worse.
FIGURE_REGRESSION_SIGN: Mapping[str, float] = {
    "ripple_db": 1.0, "dip.depth_db": 1.0, "handover.hole_db": 1.0,
    "band_level_db": -1.0, "low_bass.level_db": -1.0,
}


LEVEL_BANDS_HZ = ((30.0, 60.0), (60.0, 100.0), (90.0, 350.0), (200.0, 300.0),
                  (350.0, 700.0), (700.0, 1500.0), (1500.0, 5000.0))
LATE_ENERGY_BAND_HZ = (90.0, 250.0)
# Early/late windows of the cardioid-or-fill figure; see ADR-0325.
EARLY_WINDOW_MS = (0.0, 10.0)
LATE_WINDOW_MS = (10.0, 40.0)
CENTROID_WINDOW_MS = (-2.0, 40.0)


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
        shape = _figure_level_db(freqs, curve) - reference_curve_db(freqs, curve)
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
    in_band = np.empty(0, dtype=int) if band is None else _band(freqs, band)
    if band is None or in_band.size < 3 or freqs[0] > band[0] or freqs[-1] < band[1]:
        return {"reason": REASON_COVERAGE_SHORT, "dip": None, "dip_shift": None,
                "ripple_db": None, "handover": None, "low_bass": None, "band_level_db": None}
    level = _figure_level_db(freqs, curve)
    shape = level - reference
    shape -= float(np.mean(shape[in_band]))
    window = None if handover_hz is None else _clip(
        (handover_hz * 2.0**-HANDOVER_HALF_OCTAVES, handover_hz * 2.0**HANDOVER_HALF_OCTAVES),
        coverage_hz, coverage_hz[1])
    dip = _deepest_dip(freqs, shape, band, DIP_MIN_DEPTH_DB)
    return {
        "reason": "", "dip": dip, "dip_shift": _dip_shift(dip, incumbent),
        "ripple_db": float(np.sqrt(np.mean(shape[in_band] ** 2))),
        "handover": _handover(freqs, level, reference, shape, handover_hz, window),
        "low_bass": _low_bass(
            freqs, level,
            (float(coverage_hz[0]), band[0] if window is None else window[0]), incumbent),
        "band_level_db": _band_level_db(freqs, level, band),
    }


def repeat_spread(repeats: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Peak-to-peak spread of each figure across repeated takes of ONE
    candidate, position and level — the only thing a difference may be called
    inconclusive against. Never a difference between candidates or positions:
    fewer than two repeats is ``no_repeats``, not a substitute for one."""
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


def pair_band_levels(
    freqs_hz: Any, *, front_tf: Any, rear_tf: Any, pair_tf: Any,
    coverage_hz: Sequence[float],
) -> list[dict[str, Any]]:
    """What each woofer does ALONE and what they do together, per band.

    One row per
    :data:`~jasper.audio_measurement.analysis.THIRD_OCTAVE_BASS_BANDS_HZ` band
    inside the coverage: the two solo segments, the segment that played BOTH,
    and ``level_gap_db`` = rear minus front. Power means of the 1/6-octave
    level in the take's own dB unit; a band this grid does not reach is absent
    rather than filled in.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    bands = _third_octaves(freqs, coverage_hz)
    levels = {name: _band_levels(freqs, tf, bands) for name, tf in
              (("front_db", front_tf), ("rear_db", rear_tf), ("pair_sum_db", pair_tf))}
    return [{"band_hz": [band[0], band[1]],
             **{name: values[index] for name, values in levels.items()},
             "level_gap_db": levels["rear_db"][index] - levels["front_db"][index]}
            for index, band in enumerate(bands)]


def superposition_residual_db(
    freqs_hz: Any, *, front_tf: Any, rear_tf: Any, pair_tf: Any,
    band_hz: Sequence[float] | None,
) -> float | None:
    """How far the pair's measured sum sits from the sum of its parts, dB.

    The band rms of ``20*log10|F + R|`` minus ``20*log10|P|`` on the 1/6-octave
    level. **The trust number every prediction over this take carries**: a
    prediction IS superposition, and the dynamic bass block is not linear, so
    at a high level this can be several dB. Reported, never hidden, and not a
    pass mark. ``None`` under three in-band bins.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    inside = np.empty(0, dtype=int) if band_hz is None else _band(freqs, band_hz)
    if inside.size < 3:
        return None
    parts = _figure_level_db(freqs, magnitude_db(np.asarray(front_tf) + np.asarray(rear_tf)))
    played = _figure_level_db(freqs, magnitude_db(pair_tf))
    return float(np.sqrt(np.mean((parts[inside] - played[inside]) ** 2)))


def shared_radiating_band_hz(
    freqs_hz: Any, *, front_tf: Any, rear_tf: Any, band_hz: Sequence[float],
) -> list[float] | None:
    """The span inside ``band_hz`` where BOTH woofers radiate: lowest to
    highest bin at which each one's 1/6-octave level is within
    :data:`RADIATING_FLOOR_BELOW_PEAK_DB` of its own in-band peak. MEASURED, so
    nothing reads a crossover corner or a driver table, and it is what
    :func:`arrival_gap_ms` correlates over. ``None`` under two such bins."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    inside = _band(freqs, band_hz)
    if inside.size < 2:
        return None
    radiating = np.ones(inside.size, dtype=bool)
    for tf in (front_tf, rear_tf):
        level = _figure_level_db(freqs, magnitude_db(tf))[inside]
        radiating &= level >= float(np.max(level)) - RADIATING_FLOOR_BELOW_PEAK_DB
    both = np.flatnonzero(radiating)
    if both.size < 2:
        return None
    return [float(freqs[inside[both[0]]]), float(freqs[inside[both[-1]]])]


def arrival_gap_ms(
    repeats: Sequence[tuple[Any, Any, float]], *, sample_rate_hz: int,
    band_hz: Sequence[float] | None,
) -> dict[str, Any]:
    """Rear minus front arrival at the microphone, ms, over one position.

    Each repeat is ``(front impulse, rear impulse, rear-minus-front clock
    shift in samples)``. The two solo segments of ONE pair take share a
    recording clock and a deconvolution pre-guard, so their direct arrivals sit
    the physical gap apart; the shift divides the schedule's own drift out
    exactly as the crossover aligner does. Band-limited GCC-PHAT
    (:func:`~jasper.audio_measurement.alignment.gcc_phat`), never peak-picking:
    the rear's own wall image lands about 1.2 ms after its direct sound — the
    size of the gap itself — and a peak-picker takes whichever is taller.

    The median over repeats with the WORST repeat's ``confidence`` and
    ``at_edge``, plus their peak-to-peak spread in µs. A repeat whose impulses
    are short, mismatched in length or not finite is NOT read — the whitening
    returns a lag for a non-finite bin rather than an error — so ``ms`` is
    ``None`` with a reason when none of them could be.
    """
    band = None if band_hz is None else (float(band_hz[0]), float(band_hz[1]))
    rows = []
    for front, rear, shift in repeats:
        ahead = np.asarray(front, dtype=np.float64)
        behind = np.asarray(rear, dtype=np.float64)
        if (band is None or band[0] >= band[1] or ahead.size < 2
                or ahead.size != behind.size or finite_float(shift) is None
                or not (np.all(np.isfinite(ahead)) and np.all(np.isfinite(behind)))):
            continue
        lag, _sign, confidence, at_edge = gcc_phat(
            behind, ahead, sample_rate=sample_rate_hz, band_hz=band, upsample=GCC_UPSAMPLE,
            max_lag_samples=ARRIVAL_GAP_SEARCH_MS * 1e-3 * sample_rate_hz,
        )
        rows.append(((lag - float(shift)) / sample_rate_hz * 1e3, confidence, bool(at_edge)))
    gaps = [row[0] for row in rows]
    return {
        "ms": float(np.median(gaps)) if rows else None,
        "confidence": min((row[1] for row in rows), default=None),
        "at_edge": any(row[2] for row in rows) if rows else None,
        "band_hz": list(band) if rows and band else None, "n_repeats": len(rows),
        "repeat_spread_us": float(np.ptp(gaps)) * 1e3 if len(gaps) > 1 else None,
        "reason": "" if rows else REASON_NO_IMPULSE,
    }


def confident_arrival_gap_s(gap: Mapping[str, Any]) -> float | None:
    """An :func:`arrival_gap_ms` row in seconds when it may be BUILT ON: read,
    inside the search window, and past
    :data:`~jasper.audio_measurement.alignment.DEFAULT_CONFIDENCE_THRESHOLD`,
    the project's own GCC-PHAT gate. ``None`` otherwise, which is what leaves
    the polarity unclear and the gradient residual absent."""
    confidence = finite_float(gap.get("confidence"))
    if gap.get("ms") is None or gap.get("at_edge") or confidence is None:
        return None
    return None if confidence < DEFAULT_CONFIDENCE_THRESHOLD else float(gap["ms"]) / 1e3


def rear_polarity(
    freqs_hz: Any, *, front_tf: Any, rear_tf: Any, band_hz: Sequence[float] | None,
    arrival_gap: Mapping[str, Any],
) -> dict[str, Any]:
    """Whether the rear woofer tracks the front woofer or opposes it.

    The phase of ``R/F`` with the arrival gap divided out — the convention is
    ``positive_delay_has_negative_phase``
    (:data:`~jasper.active_speaker.rear_calibration.PHASE_CONVENTION`), so a gap
    of τ is removed by multiplying by ``exp(+jωτ)`` — averaged as a unit phasor
    over the lowest :data:`POLARITY_BAND_COUNT` bands of ``band_hz`` where both
    woofers clear :data:`POLARITY_FLOOR_BELOW_PEAK_DB`. Unclear wherever the
    mean angle falls between the two thresholds, and wherever the gap or the
    bands are unavailable — never a guess, and never a claim about the polar
    pattern.

    ``arrival_gap`` is the whole :func:`arrival_gap_ms` row, not its seconds,
    so the two ways a gap can be unusable keep their own reasons: one never
    read (:data:`REASON_NO_IMPULSE`) and one read but under the correlator's
    confidence gate (:data:`REASON_GAP_NOT_CONFIDENT`).
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    bands = [] if band_hz is None else _third_octaves(freqs, band_hz)
    row: dict[str, Any] = {"state": POLARITY_UNCLEAR, "phase_deg": None, "bands_hz": None}
    arrival_gap_s = confident_arrival_gap_s(arrival_gap)
    if arrival_gap_s is None:
        return {**row, "reason": REASON_GAP_NOT_CONFIDENT
                       if arrival_gap.get("ms") is not None else REASON_NO_IMPULSE}
    if not bands:
        return {**row, "reason": REASON_COVERAGE_SHORT}
    levels = [_band_levels(freqs, tf, bands) for tf in (front_tf, rear_tf)]
    floor = max(max(one) for one in levels) - POLARITY_FLOOR_BELOW_PEAK_DB
    loud = [band for index, band in enumerate(bands)
            if min(one[index] for one in levels) >= floor][:POLARITY_BAND_COUNT]
    if not loud:
        return {**row, "reason": REASON_COVERAGE_SHORT}
    inside = np.concatenate([_band(freqs, band) for band in loud])
    ahead = np.asarray(front_tf, dtype=np.complex128)[inside]
    aligned = (np.asarray(rear_tf, dtype=np.complex128)[inside]
               / np.where(np.abs(ahead) < _MAGNITUDE_FLOOR, _MAGNITUDE_FLOOR, ahead)
               * np.exp(2j * np.pi * freqs[inside] * float(arrival_gap_s)))
    mean = np.mean(aligned / np.maximum(np.abs(aligned), _MAGNITUDE_FLOOR))
    degrees = abs(float(np.degrees(np.angle(mean))))
    return {
        "state": POLARITY_SAME if degrees <= POLARITY_SAME_MAX_DEG
                 else POLARITY_INVERTED if degrees >= POLARITY_INVERTED_MIN_DEG
                 else POLARITY_UNCLEAR,
        "phase_deg": degrees, "bands_hz": [loud[0][0], loud[-1][1]], "reason": "",
    }


def gradient_residual_db(
    freqs_hz: Any, rear_stage_ratio: Any, arrival_gap_s: float | None,
    band_hz: Sequence[float] | None,
) -> float | None:
    """How far the APPLIED chain sits from an ideal gradient at the measured gap.

    The band mean of ``20*log10|H_rear/H_front + exp(-jωτ)|``. A first-order
    gradient feeds the rear woofer the front's own signal inverted and delayed
    by the pair's acoustic gap, so an ideal ratio is ``-exp(-jωτ)`` and the sum
    inside the log cancels. A DIAGNOSTIC of the document, not a target and not
    a measurement: it assumes the two woofers radiate the same acoustic
    response, which only matched drivers in one cabinet approximately do.
    ``None`` when the gap is unavailable or the band holds no bins.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    inside = np.empty(0, dtype=int) if band_hz is None else _band(freqs, band_hz)
    if arrival_gap_s is None or not inside.size:
        return None
    ideal = np.exp(-2j * np.pi * freqs[inside] * float(arrival_gap_s))
    return float(np.mean(magnitude_db(np.asarray(rear_stage_ratio, dtype=np.complex128)[inside] + ideal)))


def magnitude_db(values: Any) -> np.ndarray:
    """Magnitude in dB, floored by :data:`_MAGNITUDE_FLOOR`."""
    return 20.0 * np.log10(np.maximum(np.abs(np.asarray(values)), _MAGNITUDE_FLOOR))


def _third_octaves(freqs: np.ndarray,
                   band_hz: Sequence[float]) -> list[tuple[float, float]]:
    """This grid's :data:`~jasper.audio_measurement.analysis.THIRD_OCTAVE_BASS_BANDS_HZ`
    bands that lie wholly inside ``band_hz`` — the vocabulary every pair figure
    is read over."""
    return [band for band in THIRD_OCTAVE_BASS_BANDS_HZ
            if band[0] >= float(band_hz[0]) and band[1] <= float(band_hz[1])
            and _band(freqs, band).size]


def _band_levels(freqs: np.ndarray, transfer: Any,
                 bands: Sequence[tuple[float, float]]) -> list[float]:
    """One complex segment's power-mean 1/6-octave level in each band."""
    if not bands:
        return []
    return [float(level) for level in band_levels_from_magnitude(
        freqs, _figure_level_db(freqs, magnitude_db(transfer)), bands)]


def _figure_level_db(freqs: np.ndarray, curve_db: np.ndarray) -> np.ndarray:
    return smooth_fractional_octave(freqs, curve_db, fraction=FIGURE_FRACTION)


def _band(freqs: np.ndarray, band_hz: Sequence[float]) -> np.ndarray:
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
    if not _band(freqs, band_hz).size:
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
    idx = _band(freqs, band_hz)
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
    idx = _band(freqs, window) if window is not None else np.empty(0, dtype=int)
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
