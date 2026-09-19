# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve plans without opening resources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from itertools import product
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.program_analysis.check import _ambient_rows_in_band, _snr_floor_ok
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.quality_model import DRIVER
from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.json_fields import finite_float

from .angle_capture import (
    WALK_OVER_CAPTURE_CAPACITY,
    BASE_CANDIDATE, AngleCaptureRequest, LateralWalkRefused, WALK_LEVEL_POLICY_INVALID,
    REGIME_BRANCHES, candidate_identity, walk_price,
)
from .crossover_v2.contracts import CrossoverV2FlowError
from .crossover_v2.measure_spec import branch_target_ids_for
from .crossover_v2.refusal_copy import REASON_REGISTRY, REASON_MEASUREMENT_OUTPUT_MUTED, REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT, REASON_WALK_BRANCH_PAIR_UNDECLARED
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate, candidate_room_peqs,
    compile_candidate_config, prove_candidate_config,
)
from .movers import MOVER_ARM
from .measurement_programs import BRANCH_PAIR_FRONT_REAR, PURPOSE_BASS, PURPOSE_REAR
from .profile import SPL_RAISE_MARGIN_DB, spl_raise_bound_db_spl
from .seat_level_reference import (
    AnchorFacts, LevelUnresolved, RungMeasurementUnavailable, SeatLevelTargetError, check_target_capture_dbfs, resolve_anchor_level,
    measured_rung_admission, rung_lift_bound_db, stimulus_mismatch, validate_commissioning_spl,
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
    rig_clear_attested: bool | None = None
    mover_available: bool = True
    issues: tuple[PreflightIssue, ...] = ()
    summed_pilot_band_hz: tuple[float, float] | None = None
    applied_bass_extension: Mapping[str, Any] | None = None
    program_ids_for: Callable[[AngleCaptureRequest], tuple[str, ...]] | None = None
    declared_target_ids: tuple[str, ...] | None = None
    roles_bands: tuple[RoleBand, ...] = ()
    output_volume: Mapping[str, float | bool] = field(default_factory=dict)


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
            "level": {"resolved": self.plan.level.resolved is not None,
                      "predicted_db_spl": self.plan.level.predicted_db_spl,
                      **{key: value for key, value in self.plan.level.to_dict().items() if key != "mode"}},
            "live_admission": list(LIVE_ADMISSION),
            "rung_admission": dict(self.rung_admission),
        }


def preflight(plan: AngleCaptureRequest, facts: PreflightFacts, *, defer_rung: bool = False,
              previous_rung: Sequence[Mapping[str, Any]] | None = None) -> PreflightReport:
    issues = list(facts.issues)
    # Remove when measurement owns an explicit household-authorized unmute.
    if facts.output_volume.get("muted") is True:
        code = REASON_MEASUREMENT_OUTPUT_MUTED
        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message), evidence=facts.output_volume))
    admission: dict[str, Any] = {"basis": "pending_measurement" if defer_rung else "anchor"}

    def add(code: str, detail: str, *, blocking: bool = True) -> None:
        issues.append(PreflightIssue.from_code(code, detail, blocking=blocking))

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

    # Remove when plans can only name declared capture targets.
    if valid_shape and facts.declared_target_ids is not None:
        pairs = {branch_target_ids_for(capture.branch_pair, facts.roles_bands)
                 for capture in plan.stops if capture.regime == REGIME_BRANCHES}
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
            return PreflightReport(plan, tuple(issues), (), {}, facts.commissioning_stop_db_spl)

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
                admission.update(requested_level_db=fader, requested_db_spl=predicted,
                                 admitted_db_spl=None if defer_rung else predicted)
                target = facts.anchor.record.get("target")
                tolerance = finite_float(target.get("tolerance_db")) if isinstance(target, Mapping) else None
                unavailable = ("applied_bass_extension" if facts.applied_bass_extension is None else
                               "anchor_tolerance_db" if tolerance is None or tolerance <= 0 else None)
                if unavailable:
                    issues.append(replace(PreflightIssue.from_code(WALK_LEVEL_POLICY_INVALID,
                                          f"Cannot derive the rung margin: {unavailable}"),
                                          evidence={"unavailable": unavailable, "level_db": fader}))
                elif previous_rung is not None and tolerance is not None:
                    try:
                        admission = {"basis": "measured_window", **measured_rung_admission(
                            fader, previous_rung, ceiling_db_spl=stop, tolerance_db=tolerance)}
                        plan = replace(plan, level=replace(level, level_db=admission["level_db"]))
                        predicted = anchor.db_spl_at(admission["level_db"])
                        admission.update(requested_db_spl=anchor.db_spl_at(fader), admitted_db_spl=predicted)
                        fader = admission["level_db"]
                    except RungMeasurementUnavailable as exc:
                        admission.update(basis="measured_window", status="blocked", admitted_db_spl=None, **exc.evidence)
                        issues.append(replace(PreflightIssue.from_code(WALK_LEVEL_POLICY_INVALID, str(exc)),
                                              evidence=dict(admission)))
                elif defer_rung and tolerance is not None:
                    margin = max(tolerance, SPL_RAISE_MARGIN_DB)
                    admission.update(bound_db_spl=spl_raise_bound_db_spl(stop, margin_db=margin),
                                     margin_db=margin, quantity="max_window_db_spl", ceiling_db_spl=stop)
                elif facts.applied_bass_extension is not None and tolerance is not None:
                    try:
                        program_ids = facts.program_ids_for(plan) if facts.program_ids_for else ()
                    except (ValueError, KeyError):
                        program_ids = ()
                    anchor_program_id = (facts.anchor.record.get("stimulus") or {}).get("program_id")
                    same_stimulus = bool(program_ids) and all(stimulus_mismatch(anchor_program_id, pid) is False for pid in program_ids)
                    admission.update(anchor_program_id=anchor_program_id, run_program_ids=program_ids,
                                     stimulus_mismatch=not same_stimulus)
                    if not same_stimulus:
                        admission.update(basis="unmeasured_stimulus_opener", bound_db_spl=anchor.anchor_db_spl)
                        if predicted > anchor.anchor_db_spl:
                            fader, predicted = anchor.reference_volume_db, anchor.anchor_db_spl
                            plan = replace(plan, level=replace(level, level_db=fader))
                            admission.update(bound_by="unmeasured_stimulus_opener", admitted_db_spl=predicted)
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
                ambient, band = facts.anchor.record.get("ambient_report"), facts.summed_pilot_band_hz
                if (isinstance(ambient, Mapping) and band is not None
                        and any(pose.plays_summed and pose.purpose != PURPOSE_BASS for pose in plan.stops)):
                    pilot_dbfs = check_target_capture_dbfs(facts.anchor.sensitivity, predicted)
                    rows = _ambient_rows_in_band(band, ambient.get("bands") or ())
                    if rows and not _snr_floor_ok(ambient, pilot_dbfs, [band]):
                        lo, hi, noise_dbfs = max(rows, key=lambda row: row[2])
                        code = REASON_RUN_LEVEL_PILOTS_UNDER_AMBIENT
                        issues.append(replace(PreflightIssue.from_code(code, REASON_REGISTRY[code].message, blocking=False), evidence={
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
        for index, (pose, repeat) in enumerate(product(plan.stops, range(1, plan.repeats + 1)))
    ) if valid_shape else ()
    price = walk_price(plan) if valid_shape else {}
    return PreflightReport(plan, tuple(issues), schedule, price, ceiling, admission)
