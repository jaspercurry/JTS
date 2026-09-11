# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a measurement plan from supplied facts, without opening resources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.json_fields import finite_float

from .angle_capture import AngleCaptureRequest, LevelPolicy, LateralWalkRefused, REGIME_BRANCHES, walk_price
from .baseline_profile import applied_baseline_hardware_match
from .crossover_v2.contracts import CrossoverV2FlowError
from .crossover_v2.refusal_copy import REASON_REGISTRY
from .measured_crossover_candidate import MeasuredCrossoverCandidate, candidate_trial_scope
from .measurement_emit import candidate_layer_issues
from .measurement_programs import baseline_scope
from .plan_run import take_spl_ceiling
from .seat_level_reference import AnchorFacts, LevelUnresolved, resolve_anchor_level

# Rechecked at participation; a dry run reserves none of these resources.
LIVE_ADMISSION = (
    "wired_capture.require_wired_mic",
    "session_volume_plan.live_measurement_session",
    "crossover_v2.session.TuningSession.open",
    "crossover_v2.session.TuningSession._proven_level",
    "measurement_emit.candidate_upstream_snapshot",
)


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    detail: str
    next_action: Mapping[str, Any]
    blocking: bool = True

    @classmethod
    def from_code(cls, code: str, detail: str, *, blocking: bool = True) -> PreflightIssue:
        spec = REASON_REGISTRY[code]
        return cls(code, detail, spec.next_action or {
            "id": "review_plan", "label": "Review measurement settings", "href": "/sound/speaker/crossover/",
        }, blocking)


@dataclass(frozen=True)
class PreflightFacts:
    applied_profile: Mapping[str, Any]
    topology: Any
    candidates: Mapping[str, MeasuredCrossoverCandidate | PreflightIssue]
    mic_present: bool
    mic_identified: bool
    anchor: AnchorFacts
    commissioning_stop_db_spl: float | None
    mover: str
    issues: tuple[PreflightIssue, ...] = ()


@dataclass(frozen=True)
class ScheduledCapture:
    index: int
    pose: tuple[Any, ...]
    level_window_db: float | None
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
    candidate_scopes: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def mic_moves(self) -> int:
        return int(self.price.get("mic_moves") or 0)

    def to_dict(self) -> dict[str, Any]:
        first = next((issue for issue in self.issues if issue.blocking), None)
        return {
            "issues": [asdict(issue) for issue in self.issues],
            "schedule": [asdict(capture) for capture in self.schedule],
            "mic_moves": self.mic_moves, "price": dict(self.price),
            "baseline_graph_scope": self.plan.baseline_graph_scope,
            "spl_ceiling_db_spl": self.spl_ceiling_db_spl,
            "level": {"resolved": self.plan.level.anchor_db_spl is not None,
                      **{key: value for key, value in asdict(self.plan.level).items() if key != "mode"}},
            "live_admission": list(LIVE_ADMISSION),
            **({"status": "refused", "reason": first.code, "code": first.code,
                "detail": first.detail, "next_action": dict(first.next_action)} if first else {}),
        }


def preflight(plan: AngleCaptureRequest, facts: PreflightFacts) -> PreflightReport:
    issues = list(facts.issues)

    def add(code: str, detail: str, *, blocking: bool = True) -> None:
        issues.append(PreflightIssue.from_code(code, detail, blocking=blocking))

    valid_shape = True
    try:
        replace(plan, mover=facts.mover)
    except CrossoverV2FlowError as exc:
        add(getattr(exc, "reason", "program_plan_shape_invalid"), str(exc))
        valid_shape = False
    captures = len(plan.stops) * plan.repeats if valid_shape else 0
    if captures > MAX_CAPTURE_PLAN_ATTEMPTS or (valid_shape and plan.retries_per_pose > MAX_CAPTURE_PLAN_ATTEMPTS):
        add("walk_over_capture_capacity", f"captures={captures}, retries_per_pose={plan.retries_per_pose}; limit={MAX_CAPTURE_PLAN_ATTEMPTS}")
        valid_shape = False

    scopes: dict[str, str] = {}
    snapshot, faults = (None, []) if facts.topology is None else applied_baseline_hardware_match(
        facts.topology, applied_profile=facts.applied_profile,
    )
    if snapshot is None and any(stop.plays_summed for stop in plan.stops):
        add("measurement_profile_unavailable", str(faults))
    for name in dict.fromkeys(stop.candidate_id for stop in plan.stops if stop.candidate_id):
        candidate = facts.candidates.get(name)
        if isinstance(candidate, PreflightIssue):
            issues.append(candidate)
            continue
        if candidate is None:
            add("not_found", name)
            continue
        scopes[name] = candidate_trial_scope(candidate)
        if snapshot is not None:
            try:
                for code in candidate_layer_issues(candidate, snapshot, applied_profile=facts.applied_profile):
                    add(code, name)
            except (ValueError, TypeError, KeyError) as exc:
                add("measurement_profile_unavailable", str(exc))

    if not facts.mic_present:
        add("wired_mic_missing", "No measurement microphone is present")
    elif not facts.mic_identified:
        add("measurement_mic_unidentified", "The measurement microphone has no known identity")
    if facts.anchor.sensitivity is None:
        add("measure_spl_calibration_required", "Microphone sensitivity cannot be resolved",
            blocking=plan.spl_ceiling_db_spl is not None)
    stop = finite_float(facts.commissioning_stop_db_spl)
    ceiling = None
    if stop is None or stop <= 0:
        add("walk_commissioning_stop_unset", "The commissioning stop cannot be resolved")
    else:
        try:
            ceiling = take_spl_ceiling(plan.spl_ceiling_db_spl, commissioning_stop_db_spl=stop)
        except LateralWalkRefused as exc:
            add(exc.reason, exc.detail)
        try:
            anchor = resolve_anchor_level(facts=facts.anchor, ceiling_db_spl=stop)
            level = LevelPolicy(**asdict(anchor))
            if plan.level.anchor_db_spl is not None and plan.level != level:
                raise LevelUnresolved("seat_anchor_unusable", "The carried anchor differs from the banked anchor")
            plan = replace(plan, level=level)
        except (LevelUnresolved, LateralWalkRefused) as exc:
            add(exc.reason, exc.detail)

    schedule = tuple(
        ScheduledCapture(index + 1, pose.place, level, pose.candidate_id or "base", repeat,
                         ("candidate_branches" if pose.regime == REGIME_BRANCHES else scopes.get(pose.candidate_id))
                         if pose.candidate_id else baseline_scope(pose.purpose), pose.regime)
        for index, (pose, level, repeat) in enumerate(
            (pose, level, repeat)
            for pose in plan.stops
            for level in (plan.operating_levels_db or (None,))
            for repeat in range(1, plan.repeats + 1)
        )
    ) if valid_shape else ()
    price = walk_price(plan) if valid_shape else {}
    return PreflightReport(plan, tuple(issues), schedule, price, ceiling, scopes)
