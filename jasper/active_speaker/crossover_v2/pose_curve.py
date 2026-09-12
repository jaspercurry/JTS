# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sampling and storage of measured pose curves on one shared grid."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "LATERAL_EVIDENCE_BAND_HZ", "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LateralPoseCurve", "lateral_evidence_grid_hz", "lateral_pose_curve", "pose_curve_record",
]

# One fixed log-spaced basis for every retained pose curve: fixed rather than
# per-role so both branches land on the SAME frequencies and a consumer can sum
# them without resampling; log-spaced because a crossover argument is a
# per-octave one. 1/12 octave is ~118 Hz at 2 kHz — a COARSE gate, never a polar
# measurement (#1968).
LATERAL_EVIDENCE_BAND_HZ = (20.0, 20_000.0)
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = 12


@dataclass(frozen=True)
class LateralPoseCurve:
    """One branch's response at one pose, on the shared log basis.

    The take's ``phase_composition`` states whether this includes a complete tune.

    Values are SAMPLED at the nearest native bin, never interpolated or
    averaged: a phase interpolated across a wrap is simply wrong. The
    frequencies actually sampled ride along. ``band_hz`` is the role's driven
    sweep band — outside it the samples are noise and a consumer must bound
    itself with this.

    ``repeat_curves`` holds this driver's other located occurrences, in
    occurrence order; a repeat's own ``repeat_curves`` is empty, mirroring
    ``DriverResponse.repeat_responses``.
    """

    role: str
    freqs_hz: np.ndarray
    complex_tf: np.ndarray
    band_hz: tuple[float, float]
    #: The gate's trusted floor for THIS occurrence, Hz. ``None`` is "no floor
    #: was resolved", never 0 Hz.
    validity_floor_hz: float | None = None
    repeat_curves: tuple["LateralPoseCurve", ...] = ()
    gate_window_ms: float | None = None


def lateral_evidence_grid_hz() -> np.ndarray:
    """The shared log basis every retained pose curve is sampled onto."""
    lo, hi = LATERAL_EVIDENCE_BAND_HZ
    octaves = math.log2(hi / lo)
    return np.geomspace(
        lo, hi, num=int(round(octaves * LATERAL_EVIDENCE_POINTS_PER_OCTAVE)) + 1,
    )


def lateral_pose_curve(
    response: Any, band_hz: tuple[float, float],
) -> LateralPoseCurve:
    """Sample one analyzed driver response onto the shared basis."""
    freqs = np.asarray(response.freqs_hz, dtype=np.float64)
    tf = np.asarray(response.complex_tf, dtype=np.complex128)
    # ``searchsorted`` + a one-step comparison is the nearest native bin on a
    # monotonically increasing rfft grid, without materialising an N x M
    # distance matrix: the analysis grid is hundreds of thousands of bins.
    grid = lateral_evidence_grid_hz()
    right = np.searchsorted(freqs, grid).clip(1, freqs.size - 1)
    left = right - 1
    take = np.where(
        np.abs(grid - freqs[left]) <= np.abs(freqs[right] - grid), left, right
    )
    return LateralPoseCurve(
        role=str(response.role),
        freqs_hz=freqs[take],
        complex_tf=tf[take],
        band_hz=(float(band_hz[0]), float(band_hz[1])),
        validity_floor_hz=response.validity_floor_hz,
        gate_window_ms=(response.gating or {}).get("window_ms"),
        repeat_curves=tuple(
            lateral_pose_curve(occurrence, band_hz)
            for occurrence in response.repeat_responses
        ),
    )


#: Deep-null floor applied before the log, so a bin that cancelled to exactly
#: zero banks a number instead of ``-inf``, which is not JSON. The same 1e-12
#: :func:`~jasper.audio_measurement.deconv.magnitude_response` applies.
_POSE_MAGNITUDE_FLOOR = 1e-12


def pose_curve_record(curve: LateralPoseCurve) -> dict[str, Any]:
    """One measured curve, banked as magnitude AND phase.

    The ONE serializer ``complex_tf`` has. The pair reconstructs the transfer
    function exactly: ``10 ** (magnitude_db / 20) * exp(1j * radians(phase_deg))``.

    ``phase_deg`` is WRAPPED to (-180, 180], the value :func:`numpy.angle`
    produces — unwrapping is a derived view with a branch choice in it, left to
    the consumer.

    Absolute phase carries the microphone's own uncorrected response, since mic
    calibration here is magnitude-only: common-mode across the roles of one
    capture, so self-cancelling for relative cross-driver work. Not a claim
    about the driver's absolute phase.

    ``repeat_curves`` carries each sibling occurrence in this same shape, so a
    reader has one thing to parse at either level. Its ``validity_floor_hz`` is
    that OCCURRENCE's own gate floor, which is a narrower quantity than the
    take-level field of the same name (:func:`~.spatial.lateral_pose_record`, one
    response for the whole take).
    """
    tf = np.asarray(curve.complex_tf, dtype=np.complex128)
    magnitude = np.maximum(np.abs(tf), _POSE_MAGNITUDE_FLOOR)
    return {
        "role": curve.role,
        "band_hz": [float(curve.band_hz[0]), float(curve.band_hz[1])],
        "freqs_hz": [float(hz) for hz in curve.freqs_hz],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(magnitude)],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
        # See ADR-0228 entry 2 for occurrence-level gates and repeats.
        "validity_floor_hz": curve.validity_floor_hz,
        "gate_window_ms": curve.gate_window_ms,
        "smoothing_fractional_octave": 0,
        "repeat_curves": [
            pose_curve_record(repeat) for repeat in curve.repeat_curves
        ],
    }
