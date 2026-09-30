# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sampling and storage of measured pose curves on one shared grid."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.evidence_grid import evidence_bins

__all__ = [
    "WINDOW_GATED", "WINDOW_UNGATED", "LateralPoseCurve", "lateral_pose_curve", "pose_curve_record",
]

#: The two windows a banked curve names (ADR-0383 §2).
WINDOW_GATED = "gated"
WINDOW_UNGATED = "ungated"


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
    #: The gate's validity floor for THIS occurrence, Hz. ``None`` is "no floor
    #: was resolved", never 0 Hz.
    validity_floor_hz: float | None = None
    repeat_curves: tuple["LateralPoseCurve", ...] = ()
    gate_window_ms: float | None = None
    floor_source: str | None = None
    trusted_floor_hz: float | None = None
    late_energy: Mapping[str, float] | None = None


def lateral_pose_curve(
    response: Any, band_hz: tuple[float, float], *, ungated: bool = False,
) -> LateralPoseCurve:
    """Sample one analyzed driver response onto the shared basis; ``ungated``
    reads its ``ungated_tf``, already on that basis, a reading no gate floors."""
    freqs = np.asarray(response.freqs_hz, dtype=np.float64)
    take = evidence_bins(freqs)
    tf = np.asarray(response.ungated_tf if ungated else np.asarray(response.complex_tf)[take], dtype=np.complex128)
    gating = {} if ungated else response.gating or {}
    return LateralPoseCurve(
        role=str(response.role),
        freqs_hz=freqs[take],
        complex_tf=tf,
        band_hz=(float(band_hz[0]), float(band_hz[1])),
        validity_floor_hz=None if ungated else response.validity_floor_hz,
        trusted_floor_hz=gating.get("f_trusted_hz"),
        gate_window_ms=gating.get("window_ms"),
        floor_source=gating.get("floor_source"),
        late_energy=response.late_energy,
        repeat_curves=tuple(
            lateral_pose_curve(occurrence, band_hz, ungated=ungated)
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
    take-level field of the same name a retained take can carry (one response
    for the whole take).
    """
    tf = np.asarray(curve.complex_tf, dtype=np.complex128)
    magnitude = np.maximum(np.abs(tf), _POSE_MAGNITUDE_FLOOR)
    return {
        "role": curve.role,
        "band_hz": [float(curve.band_hz[0]), float(curve.band_hz[1])],
        "freqs_hz": [float(hz) for hz in curve.freqs_hz],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(magnitude)],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
        # Ruling S3 one field further (ADR-0228 entry 2): offline fit inputs —
        # repeat_curves, validity_floor_hz, trusted_floor_hz.
        "validity_floor_hz": curve.validity_floor_hz,
        "trusted_floor_hz": curve.trusted_floor_hz,
        "window": WINDOW_UNGATED if curve.gate_window_ms is None else WINDOW_GATED,
        "gate_window_ms": curve.gate_window_ms,
        "floor_source": curve.floor_source,
        "late_energy": curve.late_energy,
        "smoothing_fractional_octave": 0,
        "repeat_curves": [
            pose_curve_record(repeat) for repeat in curve.repeat_curves
        ],
    }
