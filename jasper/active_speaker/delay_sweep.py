# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bound and grade a band-limited reverse-null delay walk.

Decision content only: this module states the grid a walk may cover and grades
the evidence a walk produced. It plays nothing, mutates no graph, and applies
no delay.

**The method of record is compute-then-confirm**
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`): the coordinate is
proposed from banked complex transfers with no audio, then confirmed by three
staged acoustic takes. The full measured sweep this module once hosted was
retired with it; what remains is the grading half, which the landscape's own
verdict reads its bars from and which ``jasper-delay-sweep`` exposes.

The bounded search, its scoring and its selection stay in
:mod:`jasper.audio_measurement.null_walk`; the null-depth number is
:func:`jasper.active_speaker.driver_acoustics.analyze_summed_crossover`'s.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jasper.active_speaker.alignment_walk import driver_delay_walk_spec
from jasper.audio_measurement.null_walk import (
    NullWalkError,
    NullWalkSpec,
    summarize_candidate,
)

# Both bars are docs/tuning-methodology.md's, and both are depths in dB re the
# Fc/2 and 2*Fc shoulder mean — the one null-depth definition
# `analyze_summed_crossover` already emits. They answer a different question
# from `driver_acoustics.DEFAULT_NULL_THRESHOLD_DB` (6.0), which asks whether a
# null formed AT ALL for the polarity call; these ask whether the null that
# formed is deep enough to trust the DELAY it implies.
ROBUST_NULL_DEPTH_DB = 20.0
# When NO coordinate the sweep measured reaches this, the residual at Fc is not
# a timing residual at all — it is axis or lobing. Disclosed as a verdict,
# never raised as an error.
USABLE_NULL_DEPTH_DB = 15.0

VERDICT_ROBUST = "delay_resolved_robust"
VERDICT_WEAK = "delay_resolved_weak"
VERDICT_AXIS_LIMITED = "axis_or_lobing_limited"
VERDICT_REFUSED = "evidence_incomplete"


def sweep_spec(
    *,
    crossover_fc_hz: float,
    upper_role: str,
    lower_role: str,
    signed_acoustic_path_difference_m: float,
    step_us: float | None = None,
) -> NullWalkSpec:
    """Bound one sweep from the crossover corner and the declared geometry.

    Every bound is derived, never per-speaker: the grid spans one half period at
    ``crossover_fc_hz`` either side of the geometry seed, because beyond that a
    reverse null repeats into the next cycle and the deepest point stops being
    unique. ``signed_acoustic_path_difference_m`` is lower-driver path minus
    upper-driver path; pass 0.0 when the geometry is undeclared, which centres
    the same half-period window on zero.
    """

    kwargs: dict[str, Any] = {
        "crossover_fc_hz": crossover_fc_hz,
        "positive_delay_target_role": upper_role,
        "negative_delay_target_role": lower_role,
        "signed_acoustic_path_difference_m": signed_acoustic_path_difference_m,
    }
    if step_us is not None:
        kwargs["step_us"] = step_us
    return driver_delay_walk_spec(**kwargs)


def rows_at_pose(
    rows_by_delay: Mapping[float, Sequence[Mapping[str, Any]]],
    pose_deg: int | None,
) -> dict[float, list[Mapping[str, Any]]]:
    """The evidence for one pose only — what the shared selector may be handed.

    ``summarize_candidate`` reads the spread across a coordinate's captures as
    repeatability, so pooling two poses into one coordinate makes a real
    pose-to-pose difference look like a noisy microphone and refuses the whole
    walk. The selection therefore runs on the design axis, and the other poses
    are graded separately as a stability check.
    """

    return {
        float(delay_us): [row for row in rows if row.get("pose_deg") == pose_deg]
        for delay_us, rows in rows_by_delay.items()
    }


def _pose_best(
    spec: NullWalkSpec,
    rows_by_delay: Mapping[float, Sequence[Mapping[str, Any]]],
    pose_deg: int | None,
) -> float | None:
    """The deepest REPEATABLE coordinate this one pose saw, or None.

    Graded through ``summarize_candidate`` rather than by averaging
    ``null_depth_db`` directly, so an off-axis pose is held to the same bar the
    selection was: the same median statistic, the same clipping / calibration /
    gating / validity-floor / alignment-SNR gates, and the same
    smallest-move-from-the-seed tie break the anchor chooser uses. A private
    statistic here would let a clipped off-axis pose downgrade a sound axis
    result, or agree with it for the wrong reason.
    """

    depths: dict[float, float] = {}
    for delay_us, rows in rows_by_delay.items():
        pose_rows = [row for row in rows if row.get("pose_deg") == pose_deg]
        if not pose_rows:
            continue
        try:
            summary = summarize_candidate(spec, float(delay_us), pose_rows)
        except NullWalkError:
            continue
        if summary["repeatable"] and summary["median_null_depth_db"] is not None:
            depths[float(delay_us)] = float(summary["median_null_depth_db"])
    if not depths:
        return None
    return min(
        depths,
        key=lambda coordinate: (
            -depths[coordinate],
            abs(coordinate - spec.geometry_seed_us),
            coordinate,
        ),
    )


def sweep_verdict(
    selection: Mapping[str, Any],
    *,
    spec: NullWalkSpec,
    rows_by_delay: Mapping[float, Sequence[Mapping[str, Any]]],
    poses_deg: Sequence[int | None] = (None,),
) -> dict[str, Any]:
    """Grade one finished selection, and disclose when no delay explains Fc.

    Three honest outcomes and one refusal:

    * ``delay_resolved_robust`` — the selected coordinate nulls at or past
      :data:`ROBUST_NULL_DEPTH_DB` and every declared pose agrees on it.
    * ``delay_resolved_weak`` — a delay was selected, but either the null is
      shallower than the robustness bar, or a declared pose disagrees about
      where it sits, or a declared pose produced no gradeable evidence at all.
      Prescribe it only with that stated.
    * ``axis_or_lobing_limited`` — no coordinate the sweep actually MEASURED
      (the coarse schedule plus its refinement, not the whole fine grid)
      reached :data:`USABLE_NULL_DEPTH_DB` **on the design axis**. The residual
      at Fc is not a delay residual on that axis; prescribing a delay here would
      chase geometry with timing.
    * ``evidence_incomplete`` — the evidence was refused (missing or
      unrepeatable captures). The refusing ``reason`` is carried through rather
      than re-spelled.
    """

    if selection.get("status") != "selected":
        return {
            "verdict": VERDICT_REFUSED,
            "reason": selection.get("reason"),
            "selected_delay_us": None,
            "selected_relative_delay_us": None,
            "selected_delay_target": None,
            "best_measured_null_depth_db": None,
            "meets_robustness_bar": False,
            "robustness_bar_db": ROBUST_NULL_DEPTH_DB,
            "usable_floor_db": USABLE_NULL_DEPTH_DB,
            "pose_best_delays_us": {},
            "unmeasured_poses_deg": [str(pose) for pose in poses_deg],
            "poses_agree": False,
        }

    best_depth = float(selection["best_measured_null_depth_db"])
    plateau = [float(item) for item in selection.get("indistinguishable_delays_us", ())]
    pose_best = {str(pose): _pose_best(spec, rows_by_delay, pose) for pose in poses_deg}
    unmeasured = [name for name, value in pose_best.items() if value is None]
    # Every declared pose must have produced gradeable evidence AND landed on
    # the plateau. A pose that yielded nothing is not agreement by silence:
    # angle robustness is the claim, so an unmeasured angle cannot support it.
    poses_agree = not unmeasured and all(
        any(abs(value - coordinate) <= 1e-6 for coordinate in plateau)
        for value in pose_best.values()
        if value is not None
    )
    meets_bar = best_depth >= ROBUST_NULL_DEPTH_DB

    if best_depth < USABLE_NULL_DEPTH_DB:
        verdict = VERDICT_AXIS_LIMITED
    elif meets_bar and poses_agree:
        verdict = VERDICT_ROBUST
    else:
        verdict = VERDICT_WEAK

    return {
        "verdict": verdict,
        "reason": None,
        "selected_delay_us": selection["selected_delay_us"],
        "selected_delay_target": selection["selected_delay_target"],
        "selected_relative_delay_us": selection["selected_relative_delay_us"],
        "best_measured_null_depth_db": best_depth,
        "meets_robustness_bar": meets_bar,
        "robustness_bar_db": ROBUST_NULL_DEPTH_DB,
        "usable_floor_db": USABLE_NULL_DEPTH_DB,
        "pose_best_delays_us": pose_best,
        "unmeasured_poses_deg": unmeasured,
        "poses_agree": poses_agree,
    }


__all__ = [
    "ROBUST_NULL_DEPTH_DB",
    "USABLE_NULL_DEPTH_DB",
    "VERDICT_AXIS_LIMITED",
    "VERDICT_REFUSED",
    "VERDICT_ROBUST",
    "VERDICT_WEAK",
    "rows_at_pose",
    "sweep_spec",
    "sweep_verdict",
]
