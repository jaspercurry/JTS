# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Pair-take levels, superposition, arrival gap and polarity. See ADR-0325."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.alignment import (
    DEFAULT_CONFIDENCE_THRESHOLD, GCC_UPSAMPLE, gcc_phat,
)
from jasper.audio_measurement.analysis import band_levels_from_magnitude
from jasper.audio_measurement.band_ladders import (
    ARRIVAL_GAP_BAND_HZ as ARRIVAL_GAP_BAND_HZ,
    THIRD_OCTAVE_BASS_BANDS_HZ,
)
from jasper.audio_measurement.evidence_reasons import (
    REASON_COVERAGE_SHORT, REASON_GAP_NOT_CONFIDENT, REASON_NO_IMPULSE,
)
from jasper.audio_measurement.seat_figures import _band, _figure_level_db
from jasper.json_fields import finite_float

# These callers are outside this move's edit scope: tests/test_rear_preview.py and tests/test_round_views_rear_cli.py.
from jasper.audio_measurement.band_ladders import (
    LATE_ENERGY_BAND_HZ as LATE_ENERGY_BAND_HZ, LEVEL_BANDS_HZ as LEVEL_BANDS_HZ,
)
from jasper.audio_measurement.evidence_reasons import (
    REASON_NO_COMPARISON as REASON_NO_COMPARISON, REASON_NO_REPEATS as REASON_NO_REPEATS,
)
from jasper.audio_measurement.seat_figures import (
    FIGURE_FRACTION as FIGURE_FRACTION, IMPULSE_FFT_SIZE as IMPULSE_FFT_SIZE,
    band_limited_impulse as band_limited_impulse, impulse_energy_figures as impulse_energy_figures,
    position_figures as position_figures, reference_curve_db as reference_curve_db,
)

#: Applied before the log so a bin that cancelled to exactly zero banks a
#: number instead of ``-inf``, which is not JSON — the same floor
#: :func:`~jasper.audio_measurement.deconv.magnitude_response` applies.
_MAGNITUDE_FLOOR = 1e-12

#: Minimum search half-width; narrow bands need room outside the main lobe.
ARRIVAL_GAP_SEARCH_MS = 2.0
#: Two drivers in one cabinet with rear-stage delay cleared for pair takes: ±10 ms leaves room for wall-image lobes.
ARRIVAL_GAP_MIN_BAND_HZ = 200.0

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
             "superposition_residual_db": superposition_residual_db(
                 freqs, front_tf=front_tf, rear_tf=rear_tf, pair_tf=pair_tf, band_hz=band),
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


def arrival_gap_ms(
    repeats: Sequence[tuple[Any, Any, float]], *, sample_rate_hz: int,
    band_hz: Sequence[float] | None,
) -> dict[str, Any]:
    """Rear minus front arrival at the microphone, ms, over one position.

    Each repeat is ``(front impulse, rear impulse, rear-minus-front clock
    shift in samples)``. The two solo segments of ONE pair take share a
    recording clock and a deconvolution pre-guard; the shift removes the
    schedule's drift. Band-limited GCC-PHAT includes the rear's wall image
    where it overlaps the direct sound in the cancellation band.

    The median over repeats with the WORST repeat's ``confidence`` and
    ``at_edge``, plus their peak-to-peak spread in µs. A repeat whose impulses
    are short, mismatched in length or not finite is NOT read — the whitening
    returns a lag for a non-finite bin rather than an error — so ``ms`` is
    ``None`` with a reason when none of them could be.
    """
    band = None if band_hz is None else (float(band_hz[0]), float(band_hz[1]))
    # Four main-lobe widths across the window leave secondary peaks unmasked.
    search_ms = (max(ARRIVAL_GAP_SEARCH_MS, 2e3 / (band[1] - band[0]))
                 if band and band[1] - band[0] >= ARRIVAL_GAP_MIN_BAND_HZ else None)
    rows = []
    for front, rear, shift in repeats:
        ahead = np.asarray(front, dtype=np.float64)
        behind = np.asarray(rear, dtype=np.float64)
        if (band is None or search_ms is None or ahead.size < 2
                or ahead.size != behind.size or finite_float(shift) is None
                or not (np.all(np.isfinite(ahead)) and np.all(np.isfinite(behind)))):
            continue
        lag, _sign, confidence, at_edge = gcc_phat(
            behind, ahead, sample_rate=sample_rate_hz, band_hz=band, upsample=GCC_UPSAMPLE,
            max_lag_samples=search_ms * 1e-3 * sample_rate_hz,
        )
        rows.append(((lag - float(shift)) / sample_rate_hz * 1e3, confidence, bool(at_edge)))
    gaps = [row[0] for row in rows]
    return {
        "ms": float(np.median(gaps)) if rows else None,
        "confidence": min((row[1] for row in rows), default=None),
        "at_edge": any(row[2] for row in rows) if rows else None,
        "search_ms": search_ms if rows else None,
        "band_hz": list(band) if rows and band else None, "n_repeats": len(rows),
        "repeat_spread_us": float(np.ptp(gaps)) * 1e3 if len(gaps) > 1 else None,
        "reason": "" if rows else REASON_COVERAGE_SHORT if search_ms is None else REASON_NO_IMPULSE,
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
    by the gap magnitude, so an ideal ratio is ``-exp(-jωτ)`` and the sum
    inside the log cancels. A DIAGNOSTIC of the document, not a target and not
    a measurement: it assumes the two woofers radiate the same acoustic
    response, which only matched drivers in one cabinet approximately do.
    The sign of a measured gap only tells which side the microphone stood on.
    ``None`` when the gap is unavailable or the band holds no bins.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    inside = np.empty(0, dtype=int) if band_hz is None else _band(freqs, band_hz)
    if arrival_gap_s is None or not inside.size:
        return None
    ideal = np.exp(-2j * np.pi * freqs[inside] * abs(float(arrival_gap_s)))
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
