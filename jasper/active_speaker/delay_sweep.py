# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measure inter-driver time alignment by band-limited reverse null.

The alignment estimator reads the delay off one capture and, when the branch
SNR will not carry that read, commits 0.0 microseconds by declaration
(``jasper.audio_measurement.program_analysis``'s
``ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR``). A horn and a cone do not share
an acoustic centre, so 0.0 is a placeholder rather than a measurement. The
strong instrument is the reverse null: invert one branch, sweep that branch's
delay, and keep the coordinate whose null at the crossover is DEEPEST.
Cancellation is maximally sensitive to alignment exactly where the two branches
overlap, so the null depth resolves a delay the arrival-time read cannot.

**This module measures; it does not apply.** The delay it reports is handed to
the existing ``--alignment-prescription`` door
(:mod:`jasper.active_speaker.crossover_v2.alignment_prescription`), which
applies delays with its own receipts and its own lobe gate. Nothing here writes
a profile, a preset, or a durable config.

What is reused rather than rebuilt:

* the bounded search, its scoring and its selection —
  :mod:`jasper.audio_measurement.null_walk` (spec, coarse-plus-refinement
  schedule, repeatability, plateau and tie policy). That module has been pure
  decision content with no executing host; this is the host.
* the null-depth number itself —
  :func:`jasper.active_speaker.driver_acoustics.analyze_summed_crossover`,
  whose ``to_dict()`` is already the exact capture shape the walk gates on.
* the graph-content proof and the microsecond-to-millisecond quantizer —
  :mod:`jasper.audio_measurement.delay_graph`.
* the restore guarantee — :func:`jasper.active_speaker.restore_wait.resilient_restore`
  (ADR-0179), and the runtime-only swap that leaves the persisted config path
  alone (ADR-0193).
* the "may an operator door act right now" answer —
  :func:`jasper.active_speaker.session_volume_plan.live_measurement_session`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Mapping, Sequence

from jasper.active_speaker.alignment_walk import driver_delay_walk_spec
from jasper.active_speaker.camilla_yaml import (
    driver_baseline_gain_name,
    driver_delay_name,
)
from jasper.active_speaker.restore_wait import resilient_restore
from jasper.audio_measurement.delay_graph import (
    prove_static_delay_binding,
    quantized_delay_ms,
)
from jasper.audio_measurement.null_walk import (
    MIN_CAPTURE_COUNT,
    BoundedNullWalkSchedule,
    NullWalkError,
    NullWalkSpec,
    select_scheduled_delay,
    summarize_candidate,
)

SWEEP_KIND = "jts_inter_driver_delay_sweep"
SWEEP_SCHEMA_VERSION = 1

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

REFUSE_MEASUREMENT_ACTIVE = "delay_sweep_measurement_session_active"
REFUSE_GRAPH = "delay_sweep_graph_refused"
REFUSE_BRANCH = "delay_sweep_branch_invalid"
REFUSE_PLAN = "delay_sweep_plan_invalid"


class DelaySweepRefused(RuntimeError):
    """The sweep did not start or could not be trusted. Carries a code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


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


def reverse_null_graph(
    live_graph: Mapping[str, Any],
    *,
    inverted_role: str,
    branch_roles: tuple[str, str],
    delay_role: str | None,
    delay_us: float,
    role_channels: Mapping[str, tuple[int, ...]],
) -> dict[str, Any]:
    """The live applied graph with one branch inverted and one delay applied.

    Patching the running graph is what makes "otherwise identical to the applied
    crossover and trims" structural rather than asserted: every filter, mixer
    and pipeline step that is not a branch's ``Gain.inverted`` bit or a branch's
    ``Delay`` value is carried through untouched. Re-emitting from a preset
    would reproduce a graph that merely ought to match.

    **Both branch delays are written on every coordinate**, the delayed one to
    ``delay_us`` and the other to zero. A walk coordinate is a RELATIVE delay,
    and ``null_walk``'s candidates are defined against a zero-relative
    predecessor; leaving whatever the applied profile already carries in the
    other lane would bias the whole grid by that value and make the reported
    coordinate disagree with the realized inter-driver delay. Writing both is
    also what puts every emitted graph — the zero coordinate included — through
    the proof below, so the 0 dB ceiling is never checked on some coordinates
    and skipped on others.

    The returned graph is runtime-only input: the caller loads it with
    ``set_active_config_raw`` and never writes it to disk.
    """

    if not isinstance(live_graph, Mapping) or not live_graph:
        raise DelaySweepRefused(REFUSE_GRAPH, "live graph must be a non-empty mapping")
    if len(set(branch_roles)) != 2:
        raise DelaySweepRefused(REFUSE_BRANCH, "a walk needs two distinct branches")
    if delay_role is not None and delay_role not in branch_roles:
        raise DelaySweepRefused(
            REFUSE_BRANCH, f"delay target {delay_role!r} is not one of the branches"
        )
    graph = copy.deepcopy(dict(live_graph))
    filters = graph.get("filters")
    if not isinstance(filters, dict):
        raise DelaySweepRefused(REFUSE_GRAPH, "live graph carries no filters block")

    gain_name = driver_baseline_gain_name(inverted_role)
    gain = filters.get(gain_name)
    params = gain.get("parameters") if isinstance(gain, Mapping) else None
    if not isinstance(gain, Mapping) or gain.get("type") != "Gain":
        raise DelaySweepRefused(
            REFUSE_BRANCH, f"{gain_name!r} is not a Gain filter in the live graph"
        )
    if not isinstance(params, Mapping) or type(params.get("inverted")) is not bool:
        raise DelaySweepRefused(
            REFUSE_BRANCH, f"{gain_name!r} carries no polarity to reverse"
        )
    patched_gain = dict(gain)
    patched_gain["parameters"] = {**params, "inverted": not params["inverted"]}
    filters[gain_name] = patched_gain

    for role in branch_roles:
        lane_us = delay_us if role == delay_role else 0.0
        delay_name = driver_delay_name(role)
        delay = filters.get(delay_name)
        delay_params = delay.get("parameters") if isinstance(delay, Mapping) else None
        if not isinstance(delay, Mapping) or delay.get("type") != "Delay":
            raise DelaySweepRefused(
                REFUSE_BRANCH, f"{delay_name!r} is not a Delay filter in the live graph"
            )
        if not isinstance(delay_params, Mapping) or delay_params.get("unit") != "ms":
            raise DelaySweepRefused(
                REFUSE_BRANCH, f"{delay_name!r} must carry a millisecond delay"
            )
        patched_delay = dict(delay)
        patched_delay["parameters"] = {
            **delay_params,
            # The proof below recomputes this from lane_us through the same
            # single quantizer, so the two can never disagree.
            "delay": quantized_delay_ms(lane_us),
        }
        filters[delay_name] = patched_delay

    for role in branch_roles:
        channels = tuple(role_channels.get(role, ()))
        if not channels:
            raise DelaySweepRefused(
                REFUSE_BRANCH, f"no physical channels declared for branch {role!r}"
            )
        # Refuses on a delay outside the DSP bound, a filter wired to the wrong
        # channels, and on any devices.volume_limit above the 0 dB ceiling.
        prove_static_delay_binding(
            graph,
            delay_filter_name=driver_delay_name(role),
            channels=channels,
            delay_us=delay_us if role == delay_role else 0.0,
        )
    return graph


def capture_row(
    acoustic: Mapping[str, Any],
    *,
    pose_deg: int | None,
) -> dict[str, Any]:
    """One pose-tagged capture in the shape the shared walk already reads.

    ``analyze_summed_crossover(...).to_dict()`` is passed through whole: it
    already carries every field ``null_walk`` gates on (``expect_null``,
    ``calibrated``, ``gating``, ``above_validity_floor``, the alignment-class
    ``snr`` block, ``null_depth_capped``), so there is no second vocabulary.
    The coordinate is the key the row is filed under, never a field on the row.
    """

    return {"acoustic": dict(acoustic), "pose_deg": pose_deg}


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


@dataclass(frozen=True)
class DelaySweepSeams:
    """Every side effect, injected — the shape the v2 session seams already use."""

    # Both graph seams speak the PARSED graph, not YAML text: the content proof
    # in `reverse_null_graph` runs on the structure, so serializing before the
    # proof would put an unproven string on the wire. The host owns the
    # `yaml.safe_dump` into `set_active_config_raw` and the read-back after it.
    read_live_graph: Callable[[], Awaitable[Mapping[str, Any]]]
    apply_graph: Callable[[Mapping[str, Any]], Awaitable[None]]
    # A coroutine, not any awaitable: `resilient_restore` wraps it in a task so
    # a cancel lands on the shield rather than on the put-back (ADR-0179).
    restore_graph: Callable[[], Coroutine[Any, Any, dict[str, Any]]]
    measure: Callable[..., Awaitable[Mapping[str, Any]]]
    session_claim: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class DelaySweepPlan:
    """One bounded sweep: which branch inverts, which lanes carry the delay.

    Both bounds are checked here rather than discovered by the shared walk after
    the sweep has already played: too few repeats and an empty pose list each
    cost a full audible grid before anything can refuse them.
    """

    spec: NullWalkSpec
    inverted_role: str
    role_channels: Mapping[str, tuple[int, ...]]
    poses_deg: tuple[int | None, ...] = (None,)
    repeats: int = MIN_CAPTURE_COUNT

    def __post_init__(self) -> None:
        if not self.poses_deg:
            raise DelaySweepRefused(
                REFUSE_PLAN, "a sweep needs at least one pose to measure on"
            )
        if len(set(self.poses_deg)) != len(self.poses_deg):
            raise DelaySweepRefused(REFUSE_PLAN, "poses must be distinct")
        if self.repeats < MIN_CAPTURE_COUNT:
            raise DelaySweepRefused(
                REFUSE_PLAN,
                f"repeats must be at least {MIN_CAPTURE_COUNT}; below that the "
                "shared walk cannot call a coordinate repeatable",
            )
        for role in (
            self.inverted_role,
            self.spec.positive_delay_target,
            self.spec.negative_delay_target,
        ):
            if not self.role_channels.get(role):
                raise DelaySweepRefused(
                    REFUSE_PLAN, f"no physical channels declared for role {role!r}"
                )

    @property
    def branch_roles(self) -> tuple[str, str]:
        return (self.spec.positive_delay_target, self.spec.negative_delay_target)

    @property
    def axis_pose_deg(self) -> int | None:
        """The design axis — the pose the selection is made on."""
        return self.poses_deg[0]


async def run_delay_sweep(
    plan: DelaySweepPlan,
    seams: DelaySweepSeams,
) -> dict[str, Any]:
    """Walk the bounded grid, then report. Always puts the live graph back.

    Refuses before it touches the graph when a measurement session already
    holds the box. Every coordinate's graph is runtime-only, so the durable
    CamillaDSP config is not written at any point in the sweep; the restore in
    the ``finally`` runs through :func:`resilient_restore` so a cancel lands on
    the shield rather than on the put-back.

    Evidence the walk will not grade is REPORTED, never thrown away. The shared
    schedule and selector raise on unrepeatable coarse evidence, and a sweep
    that has already played its whole audible grid must not lose that grid to an
    exception: the raise becomes an ``evidence_incomplete`` artifact carrying
    every row, so the operator can look at what was actually measured.
    """

    if seams.session_claim is not None:
        refusal = seams.session_claim()
        if refusal is not None:
            raise DelaySweepRefused(REFUSE_MEASUREMENT_ACTIVE, refusal)

    spec = plan.spec
    rows_by_delay: dict[float, list[dict[str, Any]]] = {}
    trail: list[dict[str, Any]] = []
    live_graph = await seams.read_live_graph()
    schedule: BoundedNullWalkSchedule | None = None
    refusal_detail: str | None = None
    try:
        coarse = spec.coarse_candidate_delays_us()
        for coordinate in coarse:
            await _measure_coordinate(
                plan, seams, live_graph, coordinate, rows_by_delay, trail
            )
        axis = rows_at_pose(rows_by_delay, plan.axis_pose_deg)
        try:
            schedule = BoundedNullWalkSchedule.from_coarse_evidence(
                spec,
                {value: rows for value, rows in axis.items() if value in coarse},
            )
        except NullWalkError as exc:
            refusal_detail = str(exc)
        if schedule is not None:
            for coordinate in schedule.refinement_delays_us:
                await _measure_coordinate(
                    plan, seams, live_graph, coordinate, rows_by_delay, trail
                )
    finally:
        await resilient_restore(seams.restore_graph())

    selection: dict[str, Any]
    if schedule is None:
        selection = {"status": "refused", "reason": refusal_detail}
    else:
        try:
            selection = select_scheduled_delay(
                spec, schedule, rows_at_pose(rows_by_delay, plan.axis_pose_deg)
            )
        except NullWalkError as exc:
            selection = {"status": "refused", "reason": str(exc)}
    verdict = sweep_verdict(
        selection,
        spec=spec,
        rows_by_delay=rows_by_delay,
        poses_deg=plan.poses_deg,
    )
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "kind": SWEEP_KIND,
        "inverted_role": plan.inverted_role,
        "poses_deg": list(plan.poses_deg),
        "repeats": plan.repeats,
        "selection": selection,
        "verdict": verdict,
        # Keyed by coordinate, which is exactly what `jasper-delay-sweep grade`
        # reads: a banked sweep re-grades without replaying a tone.
        "rows": {str(key): value for key, value in rows_by_delay.items()},
        "steps": trail,
    }


async def _measure_coordinate(
    plan: DelaySweepPlan,
    seams: DelaySweepSeams,
    live_graph: Mapping[str, Any],
    coordinate: float,
    rows_by_delay: dict[float, list[dict[str, Any]]],
    trail: list[dict[str, Any]],
) -> None:
    """Apply one coordinate, measure every pose and repeat, record the receipts."""

    candidate = plan.spec.dsp_candidate(coordinate)
    graph = reverse_null_graph(
        live_graph,
        inverted_role=plan.inverted_role,
        branch_roles=plan.branch_roles,
        delay_role=candidate.delay_target,
        delay_us=candidate.delay_us,
        role_channels=plan.role_channels,
    )
    await seams.apply_graph(graph)
    rows = rows_by_delay.setdefault(float(coordinate), [])
    for pose_deg in plan.poses_deg:
        for _ in range(plan.repeats):
            acoustic = await seams.measure(pose_deg=pose_deg, delay_us=candidate.delay_us)
            row = capture_row(acoustic, pose_deg=pose_deg)
            rows.append(row)
            trail.append(
                {
                    "relative_delay_us": candidate.relative_delay_us,
                    "delay_target": candidate.delay_target,
                    "delay_us": candidate.delay_us,
                    "pose_deg": pose_deg,
                    "null_depth_db": row["acoustic"].get("null_depth_db"),
                    "verdict": row["acoustic"].get("verdict"),
                }
            )


__all__ = [
    "DelaySweepPlan",
    "DelaySweepRefused",
    "DelaySweepSeams",
    "REFUSE_MEASUREMENT_ACTIVE",
    "REFUSE_PLAN",
    "ROBUST_NULL_DEPTH_DB",
    "SWEEP_KIND",
    "USABLE_NULL_DEPTH_DB",
    "VERDICT_AXIS_LIMITED",
    "VERDICT_REFUSED",
    "VERDICT_ROBUST",
    "VERDICT_WEAK",
    "capture_row",
    "reverse_null_graph",
    "rows_at_pose",
    "run_delay_sweep",
    "sweep_spec",
    "sweep_verdict",
]
