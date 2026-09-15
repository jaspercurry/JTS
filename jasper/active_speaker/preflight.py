# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a measurement plan from supplied facts, without opening resources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from jasper.audio_measurement.program_analysis.check import _ambient_rows_in_band, _snr_floor_ok
from jasper.audio_measurement.quality_model import DRIVER
from jasper.bass_extension.measurement import target_band_hz
from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.json_fields import finite_float

from .angle_capture import (
    WALK_OVER_CAPTURE_CAPACITY,
    BASE_CANDIDATE, AngleCaptureRequest, LateralWalkRefused, WALK_LEVEL_POLICY_INVALID,
    REGIME_BRANCHES, candidate_identity, walk_price,
)
from .crossover_v2.contracts import CrossoverV2FlowError
from .crossover_v2.refusal_copy import REASON_REGISTRY, REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate, candidate_room_peqs,
    compile_candidate_config, prove_candidate_config,
)
from .measurement_programs import PURPOSE_BASS, pilot_floor_blocking
from .seat_level_reference import (
    AnchorFacts, LevelUnresolved, SeatLevelTargetError, check_target_capture_dbfs, resolve_anchor_level,
    rung_lift_bound_db, validate_commissioning_spl,
)

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
        return cls(code, detail, spec.next_action or {
            "id": "review_plan", "label": "Review measurement settings", "href": "/sound/speaker/crossover/",
        }, blocking)


@dataclass(frozen=True)
class PreflightFacts:
    candidates: Mapping[str, MeasuredCrossoverCandidate | PreflightIssue]
    mic_present: bool
    mic_identified: bool
    anchor: AnchorFacts
    commissioning_stop_db_spl: float | None
    mover: str
    issues: tuple[PreflightIssue, ...] = ()
    summed_pilot_band_hz: tuple[float, float] | None = None
    applied_bass_extension: Mapping[str, Any] | None = None


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

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def mic_moves(self) -> int:
        return int(self.price.get("mic_moves") or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [asdict(issue) for issue in self.issues],
            "schedule": [asdict(capture) for capture in self.schedule],
            "mic_moves": self.mic_moves, "price": dict(self.price),
            "spl_ceiling_db_spl": self.spl_ceiling_db_spl,
            "level": {"resolved": self.plan.level.resolved is not None,
                      "predicted_db_spl": self.plan.level.predicted_db_spl,
                      **{key: value for key, value in self.plan.level.to_dict().items() if key != "mode"}},
            "live_admission": list(LIVE_ADMISSION),
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
        add(WALK_OVER_CAPTURE_CAPACITY, f"captures={captures}, retries_per_pose={plan.retries_per_pose}; limit={MAX_CAPTURE_PLAN_ATTEMPTS}")
        valid_shape = False

    scopes: dict[str, str] = {}
    bass_extensions: dict[str, Mapping[str, Any]] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        candidate = facts.candidates.get(name)
        if name == BASE_CANDIDATE and candidate is None:
            if facts.applied_bass_extension is not None:
                bass_extensions[name] = facts.applied_bass_extension
            continue
        if isinstance(candidate, PreflightIssue):
            issues.append(candidate)
            continue
        if candidate is None:
            add("not_found", name)
            continue
        try:
            graph = compile_candidate_config(
                candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate),
            )
            prove_candidate_config(candidate, graph)
            scopes[name] = "candidate"
            bass_extensions[name] = candidate.bass_extension
        except ValueError as exc:
            add("measurement_candidate_invalid", f"{name}: {exc}")

    if not facts.mic_present:
        add("wired_mic_missing", "No measurement microphone is present")
    elif not facts.mic_identified:
        add("measurement_mic_unidentified", "The measurement microphone has no known identity")
    if facts.anchor.sensitivity is None:
        add("measure_spl_calibration_required", "Microphone sensitivity cannot be resolved")
    stop = finite_float(facts.commissioning_stop_db_spl)
    ceiling = None
    if stop is None or stop <= 0:
        add("walk_commissioning_stop_unset", "The commissioning stop cannot be resolved")
    else:
        ceiling = stop
        if facts.anchor.sensitivity is not None:
            try:
                anchor = resolve_anchor_level(facts=facts.anchor)
                level = replace(plan.level, resolved=anchor)
                if plan.level.resolved is not None and plan.level != level:
                    raise LevelUnresolved("seat_anchor_unusable", "The carried anchor differs from the banked anchor")
                plan = replace(plan, level=level)
                fader = level.level_db if level.level_db is not None else anchor.reference_volume_db
                predicted = anchor.db_spl_at(fader)
                target = facts.anchor.record.get("target")
                tolerance = finite_float(target.get("tolerance_db")) if isinstance(target, Mapping) else None
                unavailable = ("applied_bass_extension" if facts.applied_bass_extension is None else
                               "anchor_tolerance_db" if tolerance is None or tolerance <= 0 else None)
                if unavailable:
                    issues.append(replace(PreflightIssue.from_code(WALK_LEVEL_POLICY_INVALID,
                                          f"Cannot derive the rung margin: {unavailable}"),
                                          evidence={"unavailable": unavailable, "level_db": fader}))
                elif facts.applied_bass_extension is not None and tolerance is not None:
                    for name, descriptor in bass_extensions.items():
                        try:
                            lift = rung_lift_bound_db(descriptor, facts.applied_bass_extension, fader)
                        except (TypeError, ValueError) as exc:
                            add(WALK_LEVEL_POLICY_INVALID, f"Cannot derive the bass lift for {name}: {exc}")
                            continue
                        margin = tolerance + lift
                        try:
                            validate_commissioning_spl(predicted, ceiling_db_spl=stop, margin_db=margin)
                        except SeatLevelTargetError as exc:
                            detail = f"{exc}; margin = anchor tolerance {tolerance:g} + bass lift bound {lift:g} dB"
                            issues.append(replace(PreflightIssue.from_code(WALK_LEVEL_POLICY_INVALID, detail), evidence={
                                "level_db": fader, "predicted_db_spl": predicted, "ceiling_db_spl": stop,
                                "candidate_id": name, "anchor_tolerance_db": tolerance, "lift_bound_db": lift,
                                "margin_db": margin, "bound_db_spl": stop - margin,
                            }))
                ambient = facts.anchor.record.get("ambient_report")
                if isinstance(ambient, Mapping):
                    pilot_dbfs = check_target_capture_dbfs(facts.anchor.sensitivity, predicted)
                    for purpose in dict.fromkeys(pose.purpose for pose in plan.stops if pose.plays_summed):
                        band = target_band_hz() if purpose == PURPOSE_BASS else facts.summed_pilot_band_hz
                        if band is None:
                            continue
                        rows = _ambient_rows_in_band(band, ambient.get("bands") or ())
                        # Remove when measured programs no longer require pilot SNR admission.
                        if rows and not _snr_floor_ok(ambient, pilot_dbfs, [band]):
                            lo, hi, noise_dbfs = max(rows, key=lambda row: row[2])
                            code = REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT
                            issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message,
                                                  blocking=pilot_floor_blocking(purpose)), evidence={
                                "level_db": fader, "predicted_pilot_capture_dbfs": pilot_dbfs,
                                "pilot_band_hz": band, "ambient_row": {"band_hz": (lo, hi), "level_dbfs": noise_dbfs},
                                "floor_dbfs": noise_dbfs + DRIVER.snr_ok_db,
                            }))
            except (LevelUnresolved, LateralWalkRefused) as exc:
                add(exc.reason, exc.detail)

    schedule = tuple(
        ScheduledCapture(index + 1, pose.place,
                         candidate_identity(pose.candidate_id), repeat,
                         ("candidate_branches" if pose.regime == REGIME_BRANCHES else
                          scopes.get(pose.candidate_id) if pose.candidate_id else
                          "candidate" if pose.plays_summed else "drivers"), pose.regime)
        for index, (pose, repeat) in enumerate(
            (pose, repeat) for pose in plan.stops
            for repeat in range(1, plan.repeats + 1)
        )
    ) if valid_shape else ()
    price = walk_price(plan) if valid_shape else {}
    return PreflightReport(plan, tuple(issues), schedule, price, ceiling)
