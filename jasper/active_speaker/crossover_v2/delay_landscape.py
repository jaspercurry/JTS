# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Propose an inter-driver delay from banked transfers; confirm it acoustically.

PROPOSE complex-sums the two banked per-driver transfers across
``null_walk``'s whole delay grid and reads the null depth each coordinate
would produce — no audio plays, ruling S3 having banked magnitude AND phase
for every curve (:func:`~.spatial.pose_curve_record`). DISPOSE plays the
computed optimum and its neighbours and measures what cancels. Disagreement
is a banked result, not an error, and the shoulders are the canonical span
clamped into the measured overlap
(:class:`~jasper.audio_measurement.analysis.ShoulderSpan`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.active_speaker.delay_sweep import (
    ROBUST_NULL_DEPTH_DB,
    USABLE_NULL_DEPTH_DB,
    VERDICT_AXIS_LIMITED,
    VERDICT_ROBUST,
    VERDICT_WEAK,
)
from jasper.audio_measurement.analysis import (
    ShoulderSpan,
    crossover_null_depth_db,
    shoulder_span,
)
from jasper.audio_measurement.null_walk import NullWalkError, NullWalkSpec

LANDSCAPE_KIND = "jts_inter_driver_delay_landscape"
LANDSCAPE_SCHEMA_VERSION = 1

REFUSAL_UNSUPPORTED = "delay_landscape_unsupported"
REFUSAL_FC_OUTSIDE_OVERLAP = "shoulder_overlap_excludes_fc"
REFUSAL_SHOULDER_RUN_UP = "shoulder_run_up_too_short"

#: How far the measured null may sit from the computed one before the two are
#: telling different stories. Wider than the walk's own repeat spread (2 dB):
#: this compares a two-transfer model against a real acoustic sum.
MODEL_AGREEMENT_DB = 6.0

VERDICT_MODEL_BROKE = "model_break_at_alignment_band"
VERDICT_NO_EVIDENCE = "confirmation_missing"

#: Phase-overlay corridor. Convention layered on the summation math below, not
#: a derived bound: van Veen's "555" mnemonic, whose mathematically clean
#: +5 dB point sits at ~55 deg and whose 60 deg is the rounded, easily
#: visualized tolerance (research 04,
#: docs/research/2026-08-31-tuning-methodology-deep-research/
#: 04-structure-alignment-and-automation-prior-art.md).
PHASE_OVERLAY_TIGHT_DEG = 60.0

#: The summation table's OWN additive boundary, not convention: at
#: |dphi| = 120 deg two equal-level branches sum to exactly 0 dB, and past
#: it the sum falls below one branch alone (McCarthy, "Sound Systems:
#: Design and Optimization", Summation; same research citation as above).
PHASE_OVERLAY_ADDITIVE_DEG = 120.0


class DelayLandscapeError(ValueError):
    """The banked curves cannot support a delay landscape.

    ``refusal_reason`` and the numbers in ``detail`` are the contract; the
    message is operator copy and may be reworded freely.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = REFUSAL_UNSUPPORTED,
        detail: Mapping[str, float] | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_reason = reason
        self.detail: dict[str, float] = dict(detail or {})


def _curve(
    raw: Mapping[str, Any], *, field_name: str, expected_role: str
) -> tuple[Any, Any, tuple[float, float]]:
    """Reconstruct one banked curve's complex transfer, exactly as banked.

    Returns the grid, the transfer, and the driver's own SWEPT band. The band
    is not the grid: :func:`~.spatial.lateral_pose_curve` resamples every curve
    onto one shared evidence grid and keeps the swept extent in ``band_hz``, so
    an overlap derived from grid endpoints would read as canonical however
    narrow the real one was. ``expected_role`` is checked against the banked
    ``role`` because the two curves reach the caller positionally.
    """

    from .position_cycle import parse_curve_complex

    if not isinstance(raw, Mapping):
        raise DelayLandscapeError(f"{field_name} must be a banked curve mapping")
    role = raw.get("role")
    if role is not None and str(role) != expected_role:
        raise DelayLandscapeError(
            f"{field_name} is the {role!r} curve, but was passed as "
            f"{expected_role!r}"
        )
    parsed = parse_curve_complex(raw)
    if parsed is None:
        raise DelayLandscapeError(
            f"{field_name} does not parse as a banked complex curve "
            "(freqs_hz/magnitude_db/phase_deg/band_hz)"
        )
    return parsed


def _shoulders(lower_freqs, lower_band, upper_band, *, crossover_fc_hz: float):
    """The grid the sum is taken on, and the shoulders its depth is read at.

    The sum is taken on the overlap the two DECLARED bands share, never on the
    grid they were resampled onto, and the shoulders are the canonical span
    clamped into it. Refused here is only what no span can read: an overlap
    that does not bracket Fc, or one too coarse to interpolate a shoulder from.
    """

    lo = max(lower_band[0], upper_band[0])
    hi = min(lower_band[1], upper_band[1])
    freqs = lower_freqs[(lower_freqs >= lo) & (lower_freqs <= hi)]
    span = shoulder_span(freqs, crossover_fc_hz=crossover_fc_hz, overlap_hz=(lo, hi))
    if not span.used_hz[0] < crossover_fc_hz < span.used_hz[1]:
        raise DelayLandscapeError(
            f"the shared band {lo:g}-{hi:g} Hz does not bracket Fc "
            f"{crossover_fc_hz:g} Hz, so there is no null at Fc to read",
            reason=REFUSAL_FC_OUTSIDE_OVERLAP,
            detail={"crossover_fc_hz": crossover_fc_hz, "overlap_lo_hz": lo,
                    "overlap_hi_hz": hi},
        )
    if not span.usable:
        raise DelayLandscapeError(
            f"the shared band {lo:g}-{hi:g} Hz carries {span.samples_below_fc} "
            f"points below Fc {crossover_fc_hz:g} Hz and {span.samples_above_fc} "
            "above, too few to place a shoulder either side",
            reason=REFUSAL_SHOULDER_RUN_UP,
            detail={"crossover_fc_hz": crossover_fc_hz, "overlap_lo_hz": lo,
                    "overlap_hi_hz": hi,
                    "samples_below_fc": span.samples_below_fc,
                    "samples_above_fc": span.samples_above_fc},
        )
    return freqs, span


def _resample(freqs_out, freqs_in, tf):
    """One complex transfer onto another grid, magnitude and unwrapped phase."""

    import numpy as np

    magnitude = np.interp(freqs_out, freqs_in, np.abs(tf))
    phase = np.interp(freqs_out, freqs_in, np.unwrap(np.angle(tf)))
    return magnitude * np.exp(1j * phase)


def predicted_null_depth_db(
    lower_curve: Mapping[str, Any],
    upper_curve: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    relative_delay_us: float,
    inverted_role: str,
    lower_role: str,
    upper_role: str,
) -> float:
    """The null depth a coordinate would produce, from the two banked transfers.

    One branch is sign-reversed and one delayed; the sum's notch at Fc is read
    with :func:`~jasper.audio_measurement.analysis.crossover_null_depth_db` —
    the same subtraction an acoustic capture is graded with, so computed and
    measured depths are one quantity in one unit — at the shoulders
    :func:`curve_shoulder_span` derives. ``relative_delay_us`` follows
    ``null_walk``'s sign frame: positive delays the UPPER branch.
    """

    import numpy as np

    if crossover_fc_hz <= 0.0 or not math.isfinite(crossover_fc_hz):
        raise DelayLandscapeError("crossover_fc_hz must be positive and finite")
    if inverted_role not in {lower_role, upper_role}:
        raise DelayLandscapeError("the inverted role must be one of the two branches")

    lower_freqs, lower_tf, lower_band = _curve(
        lower_curve, field_name="lower curve", expected_role=lower_role
    )
    upper_freqs, upper_tf, upper_band = _curve(
        upper_curve, field_name="upper curve", expected_role=upper_role
    )

    freqs, span = _shoulders(
        lower_freqs, lower_band, upper_band, crossover_fc_hz=crossover_fc_hz
    )

    # Resampled in POLAR form: a driver transfer at ~1 m carries milliseconds of
    # acoustic delay, so its phasor turns a full circle every few hundred hertz
    # and interpolating real and imaginary parts would cut the chord, not the
    # arc. The two forms diverge only as the sum approaches perfect
    # cancellation, where the depth is ill-conditioned either way.
    lower = _resample(freqs, lower_freqs, lower_tf)
    upper = _resample(freqs, upper_freqs, upper_tf)

    delay_s = float(relative_delay_us) * 1e-6
    if delay_s >= 0.0:
        upper = upper * np.exp(-2j * np.pi * freqs * delay_s)
    else:
        lower = lower * np.exp(2j * np.pi * freqs * delay_s)
    if inverted_role == upper_role:
        upper = -upper
    else:
        lower = -lower

    summed = lower + upper
    magnitude_db = 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12))
    return crossover_null_depth_db(
        freqs, magnitude_db, crossover_fc_hz, shoulders_hz=span.used_hz
    )


def curve_shoulder_span(
    lower_curve: Mapping[str, Any],
    upper_curve: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    lower_role: str,
    upper_role: str,
) -> ShoulderSpan:
    """The span a pair of banked curves can carry a null depth over."""

    lower_freqs, _, lower_band = _curve(
        lower_curve, field_name="lower curve", expected_role=lower_role
    )
    _, _, upper_band = _curve(
        upper_curve, field_name="upper curve", expected_role=upper_role
    )
    _, span = _shoulders(
        lower_freqs, lower_band, upper_band, crossover_fc_hz=crossover_fc_hz
    )
    return span


def _phase_overlay(
    lower_curve: Mapping[str, Any],
    upper_curve: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    lower_role: str,
    upper_role: str,
) -> dict[str, Any]:
    """dphi(f) between the two branches' OWN banked curves — diagnostic only.

    No delay and no inversion applied: this is the RAW phase relationship a
    delay candidate is trying to fix, read on the shared grid across the corner
    octave (Fc/2..2*Fc) clamped into the band both drivers share.
    ``implied_summation_db`` is the two-EQUAL-LEVEL-phasor figure
    ``20*log10(2*cos(dphi/2))``, magnitude-agnostic by construction, so it does
    not restate :func:`predicted_null_depth_db`'s magnitude-aware complex sum.
    No verdict field: the two corridor constants back read facts, not a
    pass/fail.
    """

    import numpy as np

    lower_freqs, lower_tf, lower_band = _curve(
        lower_curve, field_name="lower curve", expected_role=lower_role
    )
    upper_freqs, upper_tf, upper_band = _curve(
        upper_curve, field_name="upper curve", expected_role=upper_role
    )
    freqs, span = _shoulders(
        lower_freqs, lower_band, upper_band, crossover_fc_hz=crossover_fc_hz
    )
    lower = _resample(freqs, lower_freqs, lower_tf)
    upper = _resample(freqs, upper_freqs, upper_tf)

    lo_hz, hi_hz = span.used_hz
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    band_freqs_hz = freqs[band]

    wrapped = (np.angle(upper[band]) - np.angle(lower[band]) + np.pi) % (
        2 * np.pi
    ) - np.pi
    delta_phase_deg = np.degrees(wrapped)
    # cos(wrapped/2) in [0, 1] by construction (wrapped in (-pi, pi]), so the
    # only floor needed is against the exact zero at |dphi| = 180 deg.
    implied_summation_db = 20.0 * np.log10(
        np.maximum(2.0 * np.cos(wrapped / 2.0), 1e-12)
    )

    if delta_phase_deg.size:
        max_abs_deg = float(np.max(np.abs(delta_phase_deg)))
        fraction_within_60 = float(
            np.mean(np.abs(delta_phase_deg) <= PHASE_OVERLAY_TIGHT_DEG)
        )
        fraction_within_120 = float(
            np.mean(np.abs(delta_phase_deg) <= PHASE_OVERLAY_ADDITIVE_DEG)
        )
    else:
        max_abs_deg = float("nan")
        fraction_within_60 = float("nan")
        fraction_within_120 = float("nan")

    return {
        "band_hz": [float(lo_hz), float(hi_hz)],
        "freqs_hz": [float(f) for f in band_freqs_hz],
        "delta_phase_deg": [float(v) for v in delta_phase_deg],
        "implied_summation_db": [float(v) for v in implied_summation_db],
        "max_abs_delta_phase_deg": max_abs_deg,
        "fraction_within_60deg": fraction_within_60,
        "fraction_within_120deg": fraction_within_120,
    }


@dataclass(frozen=True)
class DelayLandscape:
    """Every coordinate's predicted null, and the three worth playing."""

    spec: NullWalkSpec
    inverted_role: str
    coordinates_us: tuple[float, ...]
    predicted_null_depth_db: tuple[float, ...]
    best_coordinate_us: float
    best_predicted_null_depth_db: float
    confirmation_coordinates_us: tuple[float, ...]
    shoulders: ShoulderSpan
    #: The raw phase relationship between the two banked curves, independent of
    #: any candidate coordinate. See :func:`_phase_overlay`.
    phase_overlay: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LANDSCAPE_SCHEMA_VERSION,
            "kind": LANDSCAPE_KIND,
            "spec": self.spec.to_dict(),
            "inverted_role": self.inverted_role,
            "coordinates_us": list(self.coordinates_us),
            "predicted_null_depth_db": list(self.predicted_null_depth_db),
            "best_coordinate_us": self.best_coordinate_us,
            "best_predicted_null_depth_db": self.best_predicted_null_depth_db,
            "confirmation_coordinates_us": list(self.confirmation_coordinates_us),
            # Every depth above was read at THESE shoulders — narrower than canonical
            # where clamped, which a reader comparing against an acoustic confirm needs.
            "shoulders": self.shoulders.to_dict(),
            "phase_overlay": self.phase_overlay,
        }


def compute_landscape(
    lower_curve: Mapping[str, Any],
    upper_curve: Mapping[str, Any],
    *,
    spec: NullWalkSpec,
    inverted_role: str,
) -> DelayLandscape:
    """Walk the WHOLE fine grid offline and pick the three coordinates to play.

    The fine grid is enumerated directly rather than through
    ``candidate_delays_us``: that helper's 25-point cap is a budget on AUDIBLE
    candidates, and nothing here plays. Every coordinate still passes
    ``fine_grid_coordinate``'s own physical bound and the 20 ms DSP ceiling.
    """

    if not isinstance(spec, NullWalkSpec):
        raise DelayLandscapeError("spec must be NullWalkSpec")
    try:
        coordinates = tuple(
            spec.fine_grid_coordinate(index)
            for index in range(spec.fine_grid_index_min, spec.fine_grid_index_max + 1)
        )
    except NullWalkError as exc:
        raise DelayLandscapeError(str(exc)) from exc

    span = curve_shoulder_span(
        lower_curve,
        upper_curve,
        crossover_fc_hz=spec.crossover_fc_hz,
        lower_role=spec.negative_delay_target,
        upper_role=spec.positive_delay_target,
    )
    overlay = _phase_overlay(
        lower_curve,
        upper_curve,
        crossover_fc_hz=spec.crossover_fc_hz,
        lower_role=spec.negative_delay_target,
        upper_role=spec.positive_delay_target,
    )
    depths = tuple(
        predicted_null_depth_db(
            lower_curve,
            upper_curve,
            crossover_fc_hz=spec.crossover_fc_hz,
            relative_delay_us=coordinate,
            inverted_role=inverted_role,
            lower_role=spec.negative_delay_target,
            upper_role=spec.positive_delay_target,
        )
        for coordinate in coordinates
    )
    best_index = max(
        range(len(coordinates)),
        key=lambda i: (depths[i], -abs(coordinates[i] - spec.geometry_seed_us)),
    )
    # The optimum plus its immediate neighbours — two takes when the optimum
    # lands on a grid edge. A single take cannot tell a real null from a
    # lucky level.
    neighbours = tuple(
        coordinates[i]
        for i in (best_index - 1, best_index, best_index + 1)
        if 0 <= i < len(coordinates)
    )
    return DelayLandscape(
        spec=spec,
        inverted_role=inverted_role,
        coordinates_us=coordinates,
        predicted_null_depth_db=depths,
        best_coordinate_us=coordinates[best_index],
        best_predicted_null_depth_db=depths[best_index],
        confirmation_coordinates_us=neighbours,
        shoulders=span,
        phase_overlay=overlay,
    )


def confirmation_verdict(
    landscape: DelayLandscape,
    measured_null_depth_db: Mapping[float, float],
) -> dict[str, Any]:
    """Grade the acoustic confirmation against what the model predicted.

    None of the five verdicts is an error. Depth is not compared directly: a
    modelled cancellation can be arbitrarily deep while a measured one floors
    on noise and room, so "measured shallower than predicted" is the ordinary
    case and what is checked is WHERE the null is. The delta is banked either
    way, as this band's controllability evidence.
    """

    measured = {float(k): float(v) for k, v in measured_null_depth_db.items()}
    at_optimum = next(
        (
            depth
            for coordinate, depth in measured.items()
            if math.isclose(coordinate, landscape.best_coordinate_us, abs_tol=1e-6)
        ),
        None,
    )
    predicted = landscape.best_predicted_null_depth_db
    base = {
        "schema_version": LANDSCAPE_SCHEMA_VERSION,
        "computed_optimum_us": landscape.best_coordinate_us,
        "predicted_null_depth_db": predicted,
        "measured_null_depth_db": at_optimum,
        "measured_minus_predicted_db": (
            None if at_optimum is None else at_optimum - predicted
        ),
        "agreement_tolerance_db": MODEL_AGREEMENT_DB,
        "robustness_bar_db": ROBUST_NULL_DEPTH_DB,
        "usable_floor_db": USABLE_NULL_DEPTH_DB,
        "confirmed_coordinates_us": sorted(measured),
    }
    if at_optimum is None:
        return {**base, "verdict": VERDICT_NO_EVIDENCE, "model_agrees": False,
                "prescribable_delay_us": None}

    deepest_measured = max(measured.values())
    # Read against the coordinates that were CONFIRMED — the optimum and its
    # neighbours, not the whole grid — so this claims only "no confirmed
    # neighbour beat the optimum". A null living somewhere else entirely is
    # caught by `promised_unkept` below.
    located = at_optimum >= deepest_measured - MODEL_AGREEMENT_DB
    # The one depth claim that IS comparable: the model said this coordinate
    # would give a usable null, and the room did not.
    promised_unkept = (
        predicted >= USABLE_NULL_DEPTH_DB and at_optimum < USABLE_NULL_DEPTH_DB
    )
    model_agrees = located and not promised_unkept

    if not model_agrees:
        verdict = VERDICT_MODEL_BROKE
    elif at_optimum < USABLE_NULL_DEPTH_DB:
        verdict = VERDICT_AXIS_LIMITED
    elif at_optimum >= ROBUST_NULL_DEPTH_DB:
        verdict = VERDICT_ROBUST
    else:
        verdict = VERDICT_WEAK
    return {
        **base,
        "verdict": verdict,
        "model_agrees": model_agrees,
        # Only a verdict the measurement actually supports hands out a number.
        "prescribable_delay_us": (
            landscape.best_coordinate_us
            if verdict in {VERDICT_ROBUST, VERDICT_WEAK}
            else None
        ),
    }


__all__ = [
    "LANDSCAPE_KIND",
    "MODEL_AGREEMENT_DB",
    "PHASE_OVERLAY_ADDITIVE_DEG",
    "PHASE_OVERLAY_TIGHT_DEG",
    "REFUSAL_FC_OUTSIDE_OVERLAP",
    "REFUSAL_SHOULDER_RUN_UP",
    "REFUSAL_UNSUPPORTED",
    "VERDICT_MODEL_BROKE",
    "VERDICT_NO_EVIDENCE",
    "DelayLandscape",
    "DelayLandscapeError",
    "compute_landscape",
    "confirmation_verdict",
    "curve_shoulder_span",
    "predicted_null_depth_db",
]
