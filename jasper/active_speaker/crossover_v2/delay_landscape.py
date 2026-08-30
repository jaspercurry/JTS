# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Propose an inter-driver delay from banked transfers; confirm it acoustically.

The method of record is compute-then-confirm, and the split is the point:

* **PROPOSE** — complex-sum the two banked per-driver transfers across
  ``null_walk``'s whole delay grid and read the null depth each coordinate
  would produce. Ruling S3 banks magnitude AND phase for every measured curve
  (:func:`~.spatial.pose_curve_record`), so the transfer functions reconstruct
  exactly and the landscape falls out of evidence that is already on disk.
  **No audio plays.** An existing MEASURE bank answers this today.
* **DISPOSE** — play the acoustic null at the computed optimum and its
  neighbours, inverted branch plus candidate delay, and measure what actually
  cancels. Three takes (two at a grid edge) rather than a blind sweep of nine
  to twenty-five.

This is not the low-SNR alignment estimator wearing a new hat. That heuristic
read a delay off one capture's arrival time and, when the branch SNR would not
carry the read, committed 0.0 microseconds by declaration. This computes a
**landscape** from measured complex transfers, states what it predicts, and
then goes and measures whether the prediction is true. Predictions propose;
measurements dispose.

**Disagreement is a result, not an error.** A deep computed optimum whose
acoustic null comes back shallow is the model breaking at this band — lobing,
or a position the two-transfer sum does not describe — and it is reported as
that. The delta between computed and measured is banked either way: it is the
controllability evidence for the alignment band.
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
from jasper.audio_measurement.analysis import crossover_null_depth_db
from jasper.audio_measurement.null_walk import NullWalkError, NullWalkSpec

LANDSCAPE_KIND = "jts_inter_driver_delay_landscape"
LANDSCAPE_SCHEMA_VERSION = 1

#: How far the measured null may sit from the computed one before the two are
#: telling different stories. Wider than the walk's own repeat spread (2 dB)
#: because this compares a two-transfer model against a real acoustic sum, not
#: one capture against another.
MODEL_AGREEMENT_DB = 6.0

VERDICT_MODEL_BROKE = "model_break_at_alignment_band"
VERDICT_NO_EVIDENCE = "confirmation_missing"


class DelayLandscapeError(ValueError):
    """The banked curves cannot support a delay landscape."""


def _curve(
    raw: Mapping[str, Any], *, field_name: str, expected_role: str
) -> tuple[Any, Any, tuple[float, float]]:
    """Reconstruct one banked curve's complex transfer, exactly as banked.

    Returns the grid, the transfer, and the driver's own SWEPT band. The band is
    not the grid: :func:`~.spatial.lateral_pose_curve` resamples every curve onto
    one shared evidence grid and keeps the band it actually swept in
    ``band_hz``. Deriving the overlap from grid endpoints would therefore find
    the same extent for both drivers on real bank data — the shoulder-span
    refusal could never fire, and the sum would include bins neither driver was
    swept over.

    ``expected_role`` is checked against the banked ``role`` because the two
    curves reach the caller positionally: swapped, the model would delay and
    invert the wrong branches and say nothing.
    """

    import numpy as np

    if not isinstance(raw, Mapping):
        raise DelayLandscapeError(f"{field_name} must be a banked curve mapping")
    role = raw.get("role")
    if role is not None and str(role) != expected_role:
        raise DelayLandscapeError(
            f"{field_name} is the {role!r} curve, but was passed as "
            f"{expected_role!r}"
        )
    try:
        freqs = np.asarray([float(hz) for hz in raw["freqs_hz"]], dtype=float)
        magnitude_db = np.asarray(
            [float(db) for db in raw["magnitude_db"]], dtype=float
        )
        phase_deg = np.asarray([float(deg) for deg in raw["phase_deg"]], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise DelayLandscapeError(
            f"{field_name} must carry freqs_hz, magnitude_db and phase_deg"
        ) from exc
    if not (freqs.size and freqs.size == magnitude_db.size == phase_deg.size):
        raise DelayLandscapeError(f"{field_name} curve arrays disagree in length")
    if not np.all(np.isfinite(freqs)):
        raise DelayLandscapeError(f"{field_name} carries a non-finite frequency")
    # The exact inverse of pose_curve_record's serialization (ruling S3).
    band = raw.get("band_hz")
    if (
        isinstance(band, (list, tuple))
        and len(band) == 2
        and all(isinstance(edge, (int, float)) for edge in band)
    ):
        swept = (float(band[0]), float(band[1]))
    else:
        # A curve that does not declare its band is taken at its grid extent.
        swept = (float(freqs[0]), float(freqs[-1]))
    if not swept[0] < swept[1]:
        raise DelayLandscapeError(f"{field_name} declares an empty band")
    tf = 10.0 ** (magnitude_db / 20.0) * np.exp(1j * np.radians(phase_deg))
    return freqs, tf, swept


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

    One branch is sign-reversed and one is delayed, then they are summed and the
    notch at Fc is read with :func:`~jasper.audio_measurement.analysis.crossover_null_depth_db`
    — the same subtraction an acoustic capture is graded with, so a computed
    depth and a measured one are the same quantity in the same units.

    ``relative_delay_us`` follows ``null_walk``'s sign frame: positive delays
    the UPPER branch, negative delays the lower.
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

    # The two drivers sweep their own bands, so the sum is taken on the overlap
    # they SHARE — read from each curve's declared band, never from the grid it
    # was resampled onto. A crossover null lives in exactly that overlap.
    lo = max(lower_band[0], upper_band[0])
    hi = min(lower_band[1], upper_band[1])
    if not lo < crossover_fc_hz / 2.0 or not crossover_fc_hz * 2.0 < hi:
        raise DelayLandscapeError(
            "the banked curves do not span both crossover shoulders"
        )
    freqs = lower_freqs[(lower_freqs >= lo) & (lower_freqs <= hi)]
    if freqs.size < 2:
        raise DelayLandscapeError("the shared band carries too few points")

    # Resampled in POLAR form. A driver transfer at ~1 m carries milliseconds
    # of acoustic delay, so its phasor turns a full circle every few hundred
    # hertz; interpolating real and imaginary parts separately cuts the chord
    # instead of following the arc and shrinks the magnitude between grid
    # points. On a realistic null — one floored by level or driver mismatch —
    # the two forms agree to a fraction of a dB, and they diverge only as the
    # sum approaches a perfect cancellation, where the depth is numerically
    # ill-conditioned in either form. Polar is kept because it is what the
    # physics says and it costs nothing, not because a measured result hangs
    # on it.
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
    return crossover_null_depth_db(freqs, magnitude_db, crossover_fc_hz)


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
    # The optimum plus its immediate neighbours -- three takes, or two when the
    # optimum lands on a grid edge. They show whether the measured null sits
    # where the model put it AND falls away either side of it; a single take
    # cannot tell a real null from a lucky level.
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
    )


def confirmation_verdict(
    landscape: DelayLandscape,
    measured_null_depth_db: Mapping[float, float],
) -> dict[str, Any]:
    """Grade the acoustic confirmation against what the model predicted.

    Outcomes, and none of them is an error:

    * ``delay_resolved_robust`` — the measured null sits at the computed
      optimum and reaches :data:`ROBUST_NULL_DEPTH_DB`.
    * ``delay_resolved_weak`` — it sits there and is usable, but short of the
      bar. Prescribe with that stated.
    * ``model_break_at_alignment_band`` — either the deepest measured null is
      NOT at the computed optimum, or the model promised a usable null there
      and the room refused it. The two-transfer sum does not describe this
      position — lobing, or somewhere the model does not reach — so no delay is
      prescribed on the strength of the computation.
    * ``axis_or_lobing_limited`` — the model agreed, and there is simply no
      usable null to be had on this axis.
    * ``confirmation_missing`` — no take covered the computed optimum.

    **Depth is not compared directly.** A modelled cancellation can be
    arbitrarily deep while a measured one floors on noise and room, so
    "measured shallower than predicted" is the ordinary case. What the model
    claims, and what is therefore checked, is WHERE the null is. The delta is
    banked either way — it is the controllability evidence for this band.
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
    # What the model actually claims is WHERE the null is. Depth is not
    # comparable: a modelled cancellation can be arbitrarily deep, while a
    # measured one floors on noise and room, so "measured shallower than
    # predicted" is the normal case and not evidence of anything. The delta is
    # banked as evidence; it does not decide agreement.
    # Read against the coordinates that were CONFIRMED, which is the optimum and
    # its neighbours — not the whole grid. So this says "no confirmed neighbour
    # beat the optimum", a narrower claim than "the null is here"; a null living
    # somewhere else entirely is caught by `promised_unkept` below, when the
    # model predicted a usable one and the room did not produce it.
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
    "VERDICT_MODEL_BROKE",
    "VERDICT_NO_EVIDENCE",
    "DelayLandscape",
    "DelayLandscapeError",
    "compute_landscape",
    "confirmation_verdict",
    "predicted_null_depth_db",
]
