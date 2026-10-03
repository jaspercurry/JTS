# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from itertools import product
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE
from jasper.audio_measurement.program import RoleBand
from jasper.bass_extension.dynamic import dynamic_bass_gain_reserve_db
from jasper.playback_state.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.platform.json_fields import finite_float

from .capture_schedule import (
    UNPROBED_TAKE_DETAIL, PlanCapture, prepare_plan_captures, run_probe_index, run_takes, unprobed_take_at_fader,
)
from .angle_capture import (
    WALK_OVER_CAPTURE_CAPACITY,
    AngleCaptureRequest, WALK_LEVEL_POLICY_INVALID,
    REGIME_BRANCHES,
)
from .crossover_v2.contracts import CrossoverV2FlowError
from .crossover_v2.measure_spec import CANDIDATE_SCOPES, branch_target_ids_for
from .crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_MEASUREMENT_OUTPUT_MUTED, REASON_MEASUREMENT_PROGRAM_NOT_OFFERED,
    REASON_WALK_BRANCH_PAIR_UNDECLARED, REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS,
    REASON_WALK_MOVER_UNAVAILABLE, REASON_WALK_RIG_CLEAR_NOT_ATTESTED,
)
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate, candidate_room_peqs,
    compile_candidate_config, plays_rear, prove_candidate_config,
)
from .movers import MOVER_ARM
from .measurement_programs import (
    BASE_CANDIDATE, BRANCH_PAIR_FRONT_REAR, PURPOSE_REAR, UnknownPresetError,
    candidate_identity, layouts_without_arm, mic_moves, run_purposes,
)
from .plan_run import preview_schedule
from .profile import DRIVER_ROLES_BY_WAY

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity
    from .crossover_v2.conductor_context import V2ConductorContext

# Rechecked at participation; a dry run reserves none of these resources.
LIVE_ADMISSION = (
    "wired_capture.require_wired_mic",
    "session_volume_plan.live_measurement_session",
    "crossover_v2.session.TuningSession.open",
    "crossover_v2.session.TuningSession._proven_level",
)


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    detail: str
    next_action: Mapping[str, Any]
    blocking: bool = True
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_code(cls, code: str, detail: str, *, blocking: bool = True) -> PreflightIssue:
        spec = REASON_REGISTRY[code]
        return cls(code, detail, spec.own_action or {
            "id": "review_plan", "label": "Review measurement settings", "href": "/sound/speaker/crossover/",
        }, blocking)


def mover_unavailable_issue(program: str) -> PreflightIssue:
    """A missing arm's refusal, naming the way on: this program's layouts that need no arm."""
    try:
        layouts = layouts_without_arm(program)
    except UnknownPresetError:
        layouts = ()
    copy = REASON_REGISTRY[REASON_WALK_MOVER_UNAVAILABLE].message
    detail = f"{copy} Or measure at a layout without the arm: {', '.join(layouts)}." if layouts else copy
    return replace(PreflightIssue.from_code(REASON_WALK_MOVER_UNAVAILABLE, detail),
                   evidence={"layouts_without_arm": list(layouts)})


@dataclass(frozen=True)
class PreflightFacts:
    candidates: Mapping[str, MeasuredCrossoverCandidate | PreflightIssue]
    mic_present: bool
    mic_identified: bool
    mic_sensitivity: MicSensitivity | None
    commissioning_stop_db_spl: float | None
    mover: str
    rig_clear_attested: bool | None = None
    mover_available: bool = True
    issues: tuple[PreflightIssue, ...] = ()
    #: The declared room's unreadable field, ``None`` when it reads (ADR-0388).
    geometry_unreadable: str | None = None
    applied_bass_extension: Mapping[str, Any] = field(default_factory=dict)
    #: Whether the run's base graph plays a rear woofer; ``None`` when it could not be read.
    applied_rear_plays: bool | None = False
    declared_target_ids: tuple[str, ...] | None = None
    #: The drivers this plan's poses may play alone here; read only for a plan naming one.
    near_field_drivers: tuple[str, ...] | None = None
    roles_bands: tuple[RoleBand, ...] = ()
    output_volume: Mapping[str, float | bool] = field(default_factory=dict)
    #: Each driver's program-path ``cap_dbfs`` and ``cap_source`` (ADR-0382).
    driver_caps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: The context these facts were read from, which composes the run's programs to price it.
    context: V2ConductorContext | None = None


@dataclass(frozen=True)
class ScheduledCapture:
    index: int
    pose: tuple[Any, ...]
    candidate_id: str
    repeat: int
    graph_scope: str | None
    regime: str


@dataclass(frozen=True)
class PreflightReport:
    plan: AngleCaptureRequest
    issues: tuple[PreflightIssue, ...]
    schedule: tuple[ScheduledCapture, ...]
    spl_ceiling_db_spl: float | None
    rung_admission: Mapping[str, Any] = field(default_factory=dict)
    driver_caps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    price: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def blocking_issue(self) -> PreflightIssue:
        return next(issue for issue in self.issues if issue.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [asdict(issue) for issue in self.issues],
            "schedule": [asdict(capture) for capture in self.schedule],
            "price": dict(self.price),
            "spl_ceiling_db_spl": self.spl_ceiling_db_spl,
            "level": self.plan.level.to_dict(),
            "live_admission": list(LIVE_ADMISSION),
            "rung_admission": dict(self.rung_admission),
            "driver_caps": {target: dict(cap) for target, cap in self.driver_caps.items()},
        }


def bass_lift_db(bass: Mapping[str, Any], under: Mapping[str, Any]) -> float:
    """How much more one graph's dynamic bass can lift than another's: the
    difference of their reserves, never negative (ADR-0370)."""
    return max(0.0, dynamic_bass_gain_reserve_db(bass) - dynamic_bass_gain_reserve_db(under))


def run_margins(captures: Sequence[PlanCapture], facts: PreflightFacts,
                bass_extensions: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """How much louder than the take a run probes its other takes at the run's
    fader may play: the largest bass lift against the graph the probe plays and,
    for a rear woofer the probe's graph mutes and a later take plays, the coherent
    sum of the woofers sharing its band (ADR-0403 §4, ADR-0370). Empty when no
    take plays at the run's fader. Clearing the room layer adds nothing: of the
    takes at a fader only a bass run's clear it, its probe too (ADR-0413 §3)."""
    takes = [(scope, levelled) for scope, levelled, _ in run_takes(captures)]
    probed = run_probe_index(takes)
    if probed is None:
        return {}
    at_fader = [capture for capture, (scope, levelled) in zip(captures, takes)
                if scope in CANDIDATE_SCOPES and not levelled]

    def bass(capture: Any) -> Mapping[str, Any]:
        cleared = "bass_extension" in capture.spec.cleared_layers
        return {} if cleared else bass_extensions.get(candidate_identity(capture.stop.candidate_id), {})

    probe = captures[probed]
    lift = max(bass_lift_db(bass(capture), bass(probe)) for capture in at_fader)

    def graph(capture: Any) -> tuple[Any, ...]:
        return capture.spec.graph_scope, capture.stop.candidate_id, capture.spec.cleared_layers

    def rear(capture: Any, *, unread: bool) -> bool:
        name = candidate_identity(capture.stop.candidate_id)
        candidate = facts.candidates.get(name)
        known = (plays_rear(candidate) if isinstance(candidate, MeasuredCrossoverCandidate) else
                 facts.applied_rear_plays if name == BASE_CANDIDATE else None)
        return unread if known is None else known

    # Woofers sharing a band add in phase at worst: 20·log10(N_take / N_probe).
    others = [capture for capture in at_fader if graph(capture) != graph(probe)]
    summed = max((20 * math.log10((1 + rear(capture, unread=True)) / (1 + rear(probe, unread=False)))
                  for capture in others), default=0.0)
    summed = max(0.0, summed)
    return {"lift_bound_db": lift, "rear_sum_db": summed, "run_margin_db": lift + summed}


def preflight(plan: AngleCaptureRequest, facts: PreflightFacts) -> PreflightReport:
    """Whether ``plan`` may run here, its schedule, margins and, with this speaker's context, price: its
    captures, mic moves and seconds. A take at the run's fader before the run's probe refuses (ADR-0403 §4)."""
    issues = list(facts.issues)
    # Remove when measurement owns an explicit household-authorized unmute.
    if facts.output_volume.get("muted") is True:
        code = REASON_MEASUREMENT_OUTPUT_MUTED
        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message), evidence=facts.output_volume))
    admission: dict[str, Any] = {}

    def add(code: str, detail: str, *, blocking: bool = True) -> None:
        issues.append(PreflightIssue.from_code(code, detail, blocking=blocking))

    # A round banks under its program id (ADR-0277).
    try:
        run_purposes(plan.program)
    except UnknownPresetError as exc:
        add(REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, str(exc))
    valid_shape = True
    try:
        replace(plan, mover=facts.mover)
    except CrossoverV2FlowError as exc:
        add(getattr(exc, "reason", "program_plan_shape_invalid"), str(exc))
        valid_shape = False
    if facts.mover == MOVER_ARM:
        if facts.rig_clear_attested is False:
            add(REASON_WALK_RIG_CLEAR_NOT_ATTESTED, REASON_REGISTRY[REASON_WALK_RIG_CLEAR_NOT_ATTESTED].message)
        if not facts.mover_available:
            issues.append(mover_unavailable_issue(plan.program))
    captures = len(plan.stops) * plan.repeats if valid_shape else 0
    if captures > MAX_CAPTURE_PLAN_ATTEMPTS or (valid_shape and plan.retries_per_pose > MAX_CAPTURE_PLAN_ATTEMPTS):
        add(WALK_OVER_CAPTURE_CAPACITY, f"captures={captures}, retries_per_pose={plan.retries_per_pose}; limit={MAX_CAPTURE_PLAN_ATTEMPTS}")
        valid_shape = False

    # Remove once three-way CHECK graphs and MEASURE are supported (#5396).
    if (valid_shape and {role.role for role in facts.roles_bands} == set(DRIVER_ROLES_BY_WAY[3])
            and any(not stop.plays_summed for stop in plan.stops)):
        code = REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS
        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message),
                              evidence={"driver_roles": DRIVER_ROLES_BY_WAY[3]}))
        return PreflightReport(plan, tuple(issues), (), facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

    # Remove when plans can only name declared capture targets.
    if valid_shape and facts.declared_target_ids is not None:
        pairs = {branch_target_ids_for(capture.branch_pair, facts.roles_bands)
                 for capture in plan.stops if capture.regime == REGIME_BRANCHES and not capture.pose.driver}
        if any(capture.purpose == PURPOSE_REAR for capture in plan.stops):
            pairs.add(branch_target_ids_for(BRANCH_PAIR_FRONT_REAR, facts.roles_bands))
        missing = tuple(sorted({target for pair in pairs for target in pair} - set(facts.declared_target_ids)))
        invalid_pairs = tuple(sorted(pair for pair in pairs if len(pair) != 2 or len(set(pair)) != 2 or not all(pair)))
        if missing or invalid_pairs:
            code = REASON_WALK_BRANCH_PAIR_UNDECLARED
            issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message), evidence={
                "missing_target_ids": missing, "declared_target_ids": facts.declared_target_ids,
                "invalid_branch_target_ids": invalid_pairs,
            }))
            return PreflightReport(plan, tuple(issues), (), facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

    # A stereo pair plays no driver alone until #5697 (ADR-0360).
    unoffered = tuple(sorted({stop.pose.driver for stop in plan.stops if stop.pose.driver}
                             - set(facts.near_field_drivers or ())))
    if valid_shape and facts.near_field_drivers is not None and unoffered:
        code = REASON_MEASUREMENT_PROGRAM_NOT_OFFERED
        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message), evidence={
            "unoffered_drivers": unoffered, "near_field_drivers": facts.near_field_drivers}))
        return PreflightReport(plan, tuple(issues), (), facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

    scopes: dict[str, str] = {}
    bass_extensions: dict[str, Mapping[str, Any]] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        candidate = facts.candidates.get(name)
        if name == BASE_CANDIDATE and candidate is None:
            bass_extensions[name] = facts.applied_bass_extension
            continue
        if isinstance(candidate, PreflightIssue):
            issues.append(candidate)
            continue
        if candidate is None:
            add("not_found", name)
            continue
        try:
            graph = compile_candidate_config(candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate))
            prove_candidate_config(candidate, graph)
            scopes[name] = "candidate"
            bass_extensions[name] = candidate.bass_extension
        except ValueError as exc:
            add("measurement_candidate_invalid", f"{name}: {exc}")

    if not facts.mic_present:
        add("wired_mic_missing", "No measurement microphone is present")
    elif not facts.mic_identified:
        add("measurement_mic_unidentified", "The measurement microphone has no known identity")
    if facts.mic_sensitivity is None:
        add("measure_spl_calibration_required", "Microphone sensitivity cannot be resolved")
    # Every take gates to the declared room and banks its band from it, so an unreadable one refuses the run (ADR-0388).
    if unreadable := facts.geometry_unreadable:
        issues.append(replace(PreflightIssue.from_code(
            DECLARED_GEOMETRY_UNREADABLE, REASON_REGISTRY[DECLARED_GEOMETRY_UNREADABLE].message),
            evidence={"field": unreadable}))
    stop = finite_float(facts.commissioning_stop_db_spl)
    ceiling = None
    if stop is None or stop <= 0:
        if not facts.issues:
            add("walk_commissioning_stop_unset", "The commissioning stop cannot be resolved")
    else:
        ceiling = stop

    schedule = tuple(
        ScheduledCapture(index + 1, stop.pose.place,
                         candidate_identity(stop.candidate_id), repeat,
                         ("candidate_branches" if stop.regime == REGIME_BRANCHES and not stop.pose.driver else
                          scopes.get(stop.candidate_id) if stop.candidate_id else
                          "candidate" if stop.plays_summed else "drivers"), stop.regime)
        for index, (stop, repeat) in enumerate(product(plan.stops, range(1, plan.repeats + 1)))
    ) if valid_shape else ()
    preparable = valid_shape and all(stop.regime != REGIME_BRANCHES or stop.pose.driver or facts.roles_bands
                                     for stop in plan.stops)
    prepared = prepare_plan_captures(plan, roles_bands=facts.roles_bands) if preparable else ()
    if unprobed_take_at_fader(run_takes(prepared)):
        admission.update(status="blocked")
        add(WALK_LEVEL_POLICY_INVALID, UNPROBED_TAKE_DETAIL)
    elif preparable and bass_extensions:
        try:
            admission.update(run_margins(prepared, facts, bass_extensions))
        except (TypeError, ValueError) as exc:
            admission.update(status="blocked")
            add(WALK_LEVEL_POLICY_INVALID, str(exc))
    price = {"captures": len(prepared), "seconds": round(preview_schedule(plan, prepared, facts.context)["estimated_seconds"]),
             "mic_moves": mic_moves(capture.stop.pose for capture in prepared)} if facts.context and schedule else {}
    return PreflightReport(plan, tuple(issues), schedule, ceiling, admission, facts.driver_caps, price)
