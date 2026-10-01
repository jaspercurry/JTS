# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from itertools import product
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE
from jasper.audio_measurement.program import MEASURE_SWEEP_F_HI_HZ, RoleBand
from jasper.audio_measurement.room_boundary import ROOM_FLOOR_HZ
from jasper.bass_extension.dynamic import dynamic_bass_gain_reserve_db
from jasper.platform.biquad import PeqFilter, peaking_cascade_response_db
from jasper.playback_state.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.platform.json_fields import finite_float

from .capture_schedule import (
    UNPROBED_TAKE_DETAIL, PlanCapture, prepare_plan_captures, run_probe_index, run_takes, unprobed_take_at_fader,
    walk_price,
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
)
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate, candidate_room_peqs,
    compile_candidate_config, plays_rear, prove_candidate_config,
)
from .movers import MOVER_ARM
from .measurement_programs import (
    BASE_CANDIDATE, BRANCH_PAIR_FRONT_REAR, PURPOSE_REAR, UnknownPresetError,
    candidate_identity, run_purposes,
)
from .profile import DRIVER_ROLES_BY_WAY

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import MicSensitivity

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
    #: ``None`` when an applied profile's room layer could not be read.
    applied_room_peqs: tuple[PeqFilter, ...] | None = ()
    #: What that layer adds to the applied program charge (ADR-0385); ``None`` when unknown.
    applied_room_charge_db: float | None = None
    #: The applied tune's program charge, which the timing take's graph folds into its
    #: trims (ADR-0385); ``None`` when an applied profile could not be read.
    applied_program_charge_db: float | None = 0.0
    #: How far under unity, before that charge, the timing take plays each front driver of the
    #: run's base (``measurement_emit.timing_floor_db``); ``None`` when it could not be read.
    applied_timing_floor_db: Mapping[str, float] | None = field(default_factory=dict)
    #: Whether the run's base graph plays a rear woofer; ``None`` when it could not be read.
    applied_rear_plays: bool | None = False
    declared_target_ids: tuple[str, ...] | None = None
    #: The drivers this plan's poses may play alone here; read only for a plan naming one.
    near_field_drivers: tuple[str, ...] | None = None
    roles_bands: tuple[RoleBand, ...] = ()
    output_volume: Mapping[str, float | bool] = field(default_factory=dict)
    #: Each driver's program-path ``cap_dbfs`` and ``cap_source`` (ADR-0382).
    driver_caps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


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
    price: Mapping[str, int | float | None]
    spl_ceiling_db_spl: float | None
    rung_admission: Mapping[str, Any] = field(default_factory=dict)
    driver_caps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def blocking_issue(self) -> PreflightIssue:
        return next(issue for issue in self.issues if issue.blocking)

    @property
    def mic_moves(self) -> int:
        return int(self.price.get("mic_moves") or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [asdict(issue) for issue in self.issues],
            "schedule": [asdict(capture) for capture in self.schedule],
            "mic_moves": self.mic_moves, "price": dict(self.price),
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


def rise_without_room_db(room_peqs: Sequence[PeqFilter], band_hz: tuple[float, float], *, charge_db: float) -> float:
    """The most a graph without ``room_peqs`` plays above one with them across
    ``band_hz``: ``charge_db``, what they add to the program charge, less their
    lowest response there (ADR-0385). Never negative."""
    if not room_peqs:
        return 0.0
    return max(0.0, charge_db - min(peaking_cascade_response_db(room_peqs, *band_hz)[1]))


def run_margins(captures: Sequence[PlanCapture], facts: PreflightFacts,
                bass_extensions: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """How much louder than the take a run probes its other takes at the run's
    fader may play: the largest bass lift and the largest rise of a take that
    clears the room layer, each against the graph the probe plays, plus the most
    any driver may play over the probe's graph. Over a timing take, a candidate's
    graph may play each front driver up to unity, so that driver counts the
    applied charge the timing graph folds into its trims plus how far under unity
    the timing graph plays it. A rear woofer the probe's graph mutes and a later
    take plays adds the coherent sum of the woofers sharing its band (ADR-0403 §4,
    ADR-0370, ADR-0385). Empty when no take plays at the run's fader. Raises
    ``ValueError`` when a take clears a room layer the probe plays and that layer
    or its charge could not be read, or plays over a timing take and the applied
    tune could not be read or plays no front woofer."""
    takes = [(scope, levelled) for scope, levelled, _ in run_takes(captures)]
    probed = run_probe_index(takes)
    if probed is None:
        return {}
    at_fader = [capture for capture, (scope, levelled) in zip(captures, takes)
                if scope in CANDIDATE_SCOPES and not levelled]

    def bass(capture: Any) -> Mapping[str, Any]:
        # The timing graph plays no bass extension (#5632); a take may clear its own.
        cleared = capture.spec.graph_scope == "timing" or "bass_extension" in capture.spec.cleared_layers
        return {} if cleared else bass_extensions.get(candidate_identity(capture.stop.candidate_id), {})

    probe = captures[probed]
    lift = max(bass_lift_db(bass(capture), bass(probe)) for capture in at_fader)
    clearing = ([] if probe.spec.graph_scope == "timing" or "room_correction" in probe.spec.cleared_layers else
                [capture for capture in at_fader if "room_correction" in capture.spec.cleared_layers])
    room_peqs, charge = facts.applied_room_peqs, facts.applied_room_charge_db
    if clearing and (room_peqs is None or (room_peqs and charge is None)):
        raise ValueError("the applied room layer could not be read, so a take clearing it has no known rise")
    rise = max((rise_without_room_db(room_peqs or (), (
        ROOM_FLOOR_HZ, float((capture.stop.stimulus or {}).get("ceiling_hz") or MEASURE_SWEEP_F_HI_HZ)),
        charge_db=charge or 0.0) for capture in clearing), default=0.0)

    def graph(capture: Any) -> tuple[Any, ...]:
        return capture.spec.graph_scope, capture.stop.candidate_id, capture.spec.cleared_layers

    def rear(capture: Any, *, unread: bool) -> bool:
        # The timing graph mutes the rear woofer (#5632); only a branch take, which levels itself, clears it.
        if capture.spec.graph_scope == "timing":
            return False
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
    excess = summed
    if probe.spec.graph_scope == "timing" and others:
        charge, floors = facts.applied_program_charge_db, facts.applied_timing_floor_db
        if charge is None or floors is None:
            raise ValueError("the applied tune could not be read, so a take over the timing take has no known rise")
        gaps = {role: charge - floor for role, floor in {"woofer": 0.0, **floors}.items()}
        if not all(math.isfinite(gap) for gap in gaps.values()):
            raise ValueError("the timing take plays no front woofer, so a take over it has no known rise")
        excess = max(gap + (summed if role == "woofer" else 0.0) for role, gap in gaps.items())
    return {"lift_bound_db": lift, "room_off_rise_db": rise, "rear_sum_db": summed, "driver_excess_db": excess,
            "run_margin_db": lift + rise + excess}


def preflight(plan: AngleCaptureRequest, facts: PreflightFacts, *, finds_fader: bool = True) -> PreflightReport:
    """Whether ``plan`` may run here, its schedule and price, and its run's margins.
    A run that ``finds_fader`` with a probe (its door states driver caps) is
    refused when a take at its fader would play before that probe (ADR-0403 §4)."""
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
        for allowed, code in ((facts.rig_clear_attested is not False, "walk_rig_clear_not_attested"),
                              (facts.mover_available, "walk_mover_unavailable")):
            if not allowed:
                add(code, REASON_REGISTRY[code].message)
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
        return PreflightReport(plan, tuple(issues), (), {}, facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

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
            return PreflightReport(plan, tuple(issues), (), {}, facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

    # A stereo pair plays no driver alone until #5697 (ADR-0360).
    unoffered = tuple(sorted({stop.pose.driver for stop in plan.stops if stop.pose.driver}
                             - set(facts.near_field_drivers or ())))
    if valid_shape and facts.near_field_drivers is not None and unoffered:
        code = REASON_MEASUREMENT_PROGRAM_NOT_OFFERED
        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message), evidence={
            "unoffered_drivers": unoffered, "near_field_drivers": facts.near_field_drivers}))
        return PreflightReport(plan, tuple(issues), (), {}, facts.commissioning_stop_db_spl, driver_caps=facts.driver_caps)

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
    priceable = valid_shape and all(stop.regime != REGIME_BRANCHES or stop.pose.driver or facts.roles_bands
                                    for stop in plan.stops)
    price = walk_price(plan, roles_bands=facts.roles_bands) if priceable else {}
    prepared = prepare_plan_captures(plan, roles_bands=facts.roles_bands) if priceable else ()
    if finds_fader and unprobed_take_at_fader(run_takes(prepared)):
        admission.update(status="blocked")
        add(WALK_LEVEL_POLICY_INVALID, UNPROBED_TAKE_DETAIL)
    elif priceable and bass_extensions:
        try:
            admission.update(run_margins(prepared, facts, bass_extensions))
        except (TypeError, ValueError) as exc:
            admission.update(status="blocked")
            add(WALK_LEVEL_POLICY_INVALID, str(exc))
    return PreflightReport(plan, tuple(issues), schedule, price, ceiling, admission, facts.driver_caps)
