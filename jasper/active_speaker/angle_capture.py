# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture a stated set of ANGLES, in a stated stimulus regime, by a stated mover.

Composes ``{per-driver | summed} x {angles} x {arm | human-guided}`` over shipped parts.
The one new primitive is :func:`pose_at_angle`, the INVERSE of
:func:`position_angle_deg`: degrees round-trip exactly through the cm-primary
representation the evidence sidecar, the ``wide`` rule and the attribution stage already
read.

These poses are FORWARD-MODEL INPUT, never a pose-ratio statistic: the lateral-walk
statistic was retired as invalidated (PR #2717, #2711), and the P2 complex-summation
model consumes each angle's transfer function directly.

This module never constructs :data:`~.crossover_v2.journey.PHASE_LATERAL` -- it returns
poses and refusals, the session host tags indexes with a phase.
"""

from __future__ import annotations

import math
from itertools import groupby
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from jasper.json_fields import finite_float
from jasper.audio_measurement.program import ExcitationProgram, RoleBand

from .crossover_v2.refusal_copy import REASON_WALK_MOVER_MISMATCH
from .movers import MOVER_ARM, MOVER_HUMAN, MOVER_CONFIRMED, MOVERS
from .seat_level_reference import ResolvedLevel, seat_level_reference_volume_db
from .fader_hold import EMERGENCY_MEASUREMENT_VOLUME_DB
from .crossover_v2.admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from .crossover_v2.capture_plan import room_sweep_band_hz
from .crossover_v2.contracts import (
    REGIME_NEAR_FIELD as MEASURE_REGIME_NEAR_FIELD,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    POLARITY_NORMAL,
)
from .crossover_v2.journey import PHASE_CLOUD_VERIFY, PHASE_MEASURE
from .crossover_v2.measure_spec import (
    GRAPH_SCOPE_DRIVERS, MeasureSpec, branch_target_ids_for,
)
from .crossover_v2.programs import program_for_phase
from .measurement_programs import (
    BASE_CANDIDATE, POSE_KIND_BEARING, PURPOSE_ROOM, PURPOSE_SPEAKER,
    BRANCH_PAIR_DRIVERS,
    candidate_identity,
    MeasurementProgram,
    REGIME_PER_DRIVER,
    REGIME_SUMMED,
    REGIME_BRANCHES,
    REGIME_NEAR_FIELD,
    REGIMES,
    resolved_measurement_purpose,
    validated_branch_pair,
    validated_capture_purpose,
    validated_pose_driver,
    pose_place,
    validated_pose,
    validated_angle,
)
from .crossover_v2.spatial import (
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
)
from jasper.active_speaker.crossover_v2.spatial import (
    MARK_DISTANCE_M,
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    POSITION_DEG_KEY,
    POSITION_ROLE_KEY,
    WIDE_OFFSET_MIN_CM,
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_TAP,
    CloudPositionPrompt,
    announced_capture_indexes,
    position_angle_deg,
    remote_position_prompt,
    stage1_plan_max_attempts,
    wall_clock_ceiling_s,
)
from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError

__all__ = [
    "REGIME_PER_DRIVER",
    "REGIME_SUMMED",
    "REGIME_BRANCHES",
    "REGIMES",
    "MOVER_ARM",
    "MOVER_HUMAN",
    "MOVER_CONFIRMED",
    "MOVERS",
    "LEVEL_HOLD_REFERENCE",
    "MAX_ANGLE_DEG",
    "MAX_ELEVATION_DEG",
    "ARM_ENVELOPE_DEG",
    "MOVER_MAX_ANGLE_DEG",
    "MOVER_MAX_ELEVATION_DEG",
    "LevelPolicy",
    "BASE_CANDIDATE",
    "candidate_identity",
    "WALK_SCHEMA_VERSION_UNSUPPORTED",
    "AngleStop",
    "AngleCaptureRequest",
    "ResolvedStop",
    "DEFAULT_TEMPLATE",
    "TEMPLATE_SWEEP_SCOPE",
    "pose_at_angle",
    "walk_template",
    "design_axis_spec",
    "stop_specs",
    "default_run_level",
    "request_for_program",
    "walk_price",
    "per_driver_at",
    "summed_at",
    "both_at",
    "resolve_request",
    "program_for_stop",
    "index_phase_map",
    "announced_indexes",
    "WALK_REGIME_UNSUPPORTED",
    "WALK_OVER_MOVER_ENVELOPE",
    "WALK_LEVEL_POLICY_INVALID",
    "WALK_SPL_CALIBRATION_REQUIRED",
    "WALK_COMMISSIONING_STOP_UNSET",
    "WALK_STIMULUS_NOT_ACCEPTED",
    "WALK_OVER_CAPTURE_CAPACITY",
    "WALK_STOP_NO_LONGER_VALID",
    "WALK_TEMPLATE_NOT_ACCEPTED",
    "WALK_DELAY_NOT_ACCEPTED",
    "WALK_POLARITY_NOT_ACCEPTED",
    "WALK_LEVEL_MATCH_NO_EVIDENCE",
    "WALK_CANDIDATE_NOT_MEASURABLE",
    "WALK_NOTHING_PLAYABLE",
    "WALK_REFUSAL_REASONS",
    "LateralWalkRefused",
    "session_lateral_walk",
]


LEVEL_HOLD_REFERENCE = "hold_reference"
LEVEL_SOURCES = ("seat_reference", "program_default", "operator")
REQUEST_SCHEMA_VERSION = 5
REQUEST_KIND = "jts_active_speaker_angle_capture_request_staged"


#: How far off the design axis a stop may be asked for. :func:`pose_at_angle` is a
#: tangent, so 80 deg already puts the microphone 5.7 m off a 1 m mark -- past any room
#: this measures in. GEOMETRY's ceiling; a given mover's narrower bound is
#: :data:`MOVER_MAX_ANGLE_DEG`.
MAX_ANGLE_DEG = 80

#: How far the lab positioner can actually travel; the turntable adapter
#: (``jasper/turntable/jts_turntable.py``) refuses a ``position`` outside +/-45
#: deg. The adapter runs under system Python; ``tests/test_arm_walk.py`` pins
#: this bound to its subprocess contract.
ARM_ENVELOPE_DEG = 45

#: How far ABOVE or BELOW mark height a person may be asked to hold the microphone.
#: Covers the plan's baseline vertical walk (+/-20 deg) with margin, no more.
MAX_ELEVATION_DEG = 30

#: The per-mover, per-axis bound :class:`AngleCaptureRequest` enforces. Checked when the
#: walk is STATED, not when a session takes it -- a target this mover cannot reach would
#: else stall a session for the whole ``REMOTE_POSITION_HOLD_BUDGET_S`` (600 s) per
#: stop.
MOVER_MAX_ANGLE_DEG: Mapping[str, int] = MappingProxyType({
    MOVER_ARM: ARM_ENVELOPE_DEG,
    MOVER_HUMAN: MAX_ANGLE_DEG,
    MOVER_CONFIRMED: MAX_ANGLE_DEG,
})

#: Elevation half of the pair above. The arm's 0 is a rig fact: it rotates about the
#: vertical axis and cannot tilt.
MOVER_MAX_ELEVATION_DEG: Mapping[str, int] = MappingProxyType({
    MOVER_ARM: 0,
    MOVER_HUMAN: MAX_ELEVATION_DEG,
    MOVER_CONFIRMED: MAX_ELEVATION_DEG,
})

#: Which composed program object each regime plays, stated as the PHASE whose program it
#: is. Per-driver is MEASURE's interleaved object; summed is the position groups'
#: unannounced sweep.
_REGIME_PROGRAM_PHASE = {
    REGIME_PER_DRIVER: PHASE_MEASURE,
    REGIME_SUMMED: PHASE_CLOUD_VERIFY,
    REGIME_BRANCHES: PHASE_CLOUD_VERIFY,
    REGIME_NEAR_FIELD: PHASE_CLOUD_VERIFY,
}


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #


def _validated_angle(angle_deg: object) -> int:
    """Normalize a whole-degree bearing and enforce the geometry limit."""
    try:
        degrees = validated_angle(angle_deg)
    except ValueError as exc:
        raise CrossoverV2FlowError(str(exc)) from None
    if abs(degrees) > MAX_ANGLE_DEG:
        raise CrossoverV2FlowError(
            f"an angle must be within +/-{MAX_ANGLE_DEG} deg of the design "
            f"axis, got {degrees:+d} deg"
        )
    return degrees


@dataclass(frozen=True)
class AngleStop:
    """One stop: an angle, and what is played there.

    ``angle_deg`` is a signed WHOLE degree (negative LEFT, positive RIGHT of
    the design axis); whole degrees because a tenth of a degree claims
    precision the ~1 m mark placement never had. ``elevation_deg`` is the
    orthogonal bearing, signed whole degrees, 0 for a stop nobody raised;
    which mover may ask for non-zero is :data:`MOVER_MAX_ELEVATION_DEG`.
    ``candidate_id`` is the banked candidate fingerprint this stop measures
    (``""`` for the program's baseline layer). ``kind``,
    ``distance_m`` and ``seat_offset_m`` are the pose's category and where it
    is stated from (:class:`~.measurement_programs.ProgramPose`).
    ``branch_pair`` is which two targets a ``branches`` stop excites
    (:data:`~.measurement_programs.BRANCH_PAIRS`). ``driver`` is the one target
    a near-field stop plays alone (ADR-0360).
    """

    angle_deg: int
    regime: str
    elevation_deg: int = 0
    candidate_id: str = BASE_CANDIDATE
    kind: str = POSE_KIND_BEARING
    distance_m: float | None = None
    seat_offset_m: tuple[float, float, float] | None = None
    purpose: str | None = None
    headline: str = ""
    detail: str = ""
    stimulus: Mapping[str, Any] | None = None
    branch_pair: str = BRANCH_PAIR_DRIVERS
    driver: str = ""

    def __post_init__(self) -> None:
        # Normalized back onto the field, so an ``np.int64`` a caller passed
        # never reaches a record or an equality check as a numpy scalar.
        object.__setattr__(self, "candidate_id", candidate_identity(self.candidate_id, for_spec=True))
        object.__setattr__(self, "angle_deg", _validated_angle(self.angle_deg))
        object.__setattr__(
            self, "elevation_deg", _validated_angle(self.elevation_deg)
        )
        try:
            offset, distance = validated_pose(self.kind, self.seat_offset_m, self.distance_m)
            object.__setattr__(self, "purpose", validated_capture_purpose(self.purpose, self.kind, self.regime))
            validated_branch_pair(self.branch_pair, self.regime)
            validated_pose_driver(self.driver, regime=self.regime, purpose=self.purpose,
                                  kind=self.kind, distance_m=distance)
            if self.driver and self.candidate_id:
                raise ValueError("a driver's pose plays the neutral drivers graph; it measures no candidate")
        except ValueError as exc:
            raise CrossoverV2FlowError(str(exc)) from None
        object.__setattr__(self, "seat_offset_m", offset)
        object.__setattr__(self, "distance_m", distance)

    @property
    def plays_summed(self) -> bool:
        """Whether this stop plays a summed graph (the scope a summed sweep
        rides); a stop naming its driver plays that driver alone instead."""
        return self.regime in (REGIME_SUMMED, REGIME_BRANCHES, REGIME_NEAR_FIELD) and not self.driver

    @property
    def place(self) -> tuple[object, ...]:
        return pose_place(
            self.kind, self.angle_deg, self.elevation_deg,
            self.distance_m, self.seat_offset_m, self.driver,
        )


#: The scope a template naming a summed sweep is VALIDATED under. ``MeasureSpec``
#: admits ``sweep_band_hz``/``sweep_s`` only on a summed scope and the graph
#: overlays only on :data:`~.crossover_v2.measure_spec.GRAPH_SCOPE_DRIVERS`, so
#: the two statements cannot share one scope -- and nothing measures at the
#: template's, since :func:`design_axis_spec` and :func:`stop_specs` each replace
#: it with the scope their capture plays. A template's scope is therefore
#: derived from its stimulus, never stated.
TEMPLATE_SWEEP_SCOPE = "candidate"


def _states_summed_sweep(sweep_band_hz: object, sweep_s: object) -> bool:
    return bool(sweep_band_hz) or sweep_s is not None


def _states_overlay(*, polarity: object, inverted_role: object, delayed_role: object,
                    delay_us: object, level_matched: object) -> bool:
    """A graph overlay the design-axis capture rides and a summed trial cannot."""
    return bool(
        inverted_role or delayed_role or delay_us or level_matched
        or (polarity or POLARITY_NORMAL) != POLARITY_NORMAL
    )


def walk_template(**spec_fields: object) -> MeasureSpec:
    """One walk's :class:`MeasureSpec` template, refused in this module's words.

    ``MeasureSpec`` is the only judge of a spec field; this names WHICH half of
    the statement was refused, so ``reason=`` sends an operator to the flag they
    got wrong, and keeps the spec's own sentence as the detail. The scope is
    :data:`TEMPLATE_SWEEP_SCOPE`'s rule.
    """
    stated_delay = bool(spec_fields.get("delayed_role") or spec_fields.get("delay_us"))
    stated_polarity = bool(
        spec_fields.get("inverted_role")
        or spec_fields.get("polarity", POLARITY_NORMAL) != POLARITY_NORMAL
    )
    summed = _states_summed_sweep(spec_fields.get("sweep_band_hz"), spec_fields.get("sweep_s"))
    if summed and _states_overlay(
        polarity=spec_fields.get("polarity"), inverted_role=spec_fields.get("inverted_role"),
        delayed_role=spec_fields.get("delayed_role"), delay_us=spec_fields.get("delay_us"),
        level_matched=spec_fields.get("level_matched"),
    ):
        raise LateralWalkRefused(WALK_CANDIDATE_NOT_MEASURABLE, SUMMED_TRIALS_PLAY_THEIR_OWN_GRAPH)
    try:
        return MeasureSpec(
            graph_scope=TEMPLATE_SWEEP_SCOPE if summed else GRAPH_SCOPE_DRIVERS,
            candidate_id=BASE_CANDIDATE if summed else "",
            **spec_fields,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise LateralWalkRefused(
            WALK_DELAY_NOT_ACCEPTED if stated_delay
            else WALK_POLARITY_NOT_ACCEPTED if stated_polarity
            else WALK_STIMULUS_NOT_ACCEPTED,
            str(exc),
        ) from exc


#: What a walk that states no spec carries: the design-axis capture the host has
#: always built, with no overlay and no stimulus stated.
DEFAULT_TEMPLATE = MeasureSpec(kind=MEASURE_KIND_CANDIDATE)

#: The template fields the EXECUTOR assigns per capture, and which a walk
#: therefore may not state: a stated one would be silently replaced at every
#: stop and silently kept on the design-axis spec.
_EXECUTOR_ASSIGNED = ("positions", "pose_prompts", "candidate_id", "branch_target_ids")


@dataclass(frozen=True)
class LevelPolicy:
    """The banked anchor and the drive level held for every take."""

    mode: str = LEVEL_HOLD_REFERENCE
    resolved: ResolvedLevel | None = None
    level_db: float | None = None

    def __post_init__(self) -> None:
        if self.mode != LEVEL_HOLD_REFERENCE:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, f"unsupported level mode: {self.mode!r}")
        if self.level_db is not None and (finite_float(self.level_db) is None
                or not EMERGENCY_MEASUREMENT_VOLUME_DB < self.level_db <= 0):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "level_db is outside the measurement fader range")
        if self.resolved is None:
            return
        if not isinstance(self.resolved, ResolvedLevel):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "resolved must be a ResolvedLevel")
        for name in ("anchor_db_spl", "reference_volume_db"):
            if finite_float(getattr(self.resolved, name)) is None:
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, f"{name} must be finite")
        if self.resolved.reference_volume_db > 0:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "reference volume must be non-positive")
        if self.resolved.mic_serial is not None and not isinstance(self.resolved.mic_serial, str):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "mic_serial must be text")

    @property
    def volume_db(self) -> float | None:
        return self.level_db if self.level_db is not None else self.resolved.reference_volume_db if self.resolved else None

    @property
    def offset_db(self) -> float:
        if self.resolved is None or self.volume_db is None:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "The plan needs a resolved session level")
        return self.volume_db - self.resolved.reference_volume_db

    @property
    def predicted_db_spl(self) -> float | None:
        return self.resolved.db_spl_at(self.level_db if self.level_db is not None else self.resolved.reference_volume_db) if self.resolved else None

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "level_db": self.level_db, **(asdict(self.resolved) if self.resolved is not None else {
            f.name: None for f in fields(ResolvedLevel)
        })}

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any]) -> LevelPolicy:
        if set(doc) != {"mode", "level_db", *(f.name for f in fields(ResolvedLevel))}:
            raise ValueError("level must state mode, level_db and the resolved level fields")
        values = dict(doc)
        mode = values.pop("mode")
        level_db = values.pop("level_db")
        return cls(mode=mode, level_db=level_db,
                   resolved=None if all(v is None for v in values.values()) else ResolvedLevel(**values))


@dataclass(frozen=True)
class AngleCaptureRequest:
    """One ordered walk, with adjacent candidates at each pose.

    ``program`` is provenance; geometry and purpose come from the stops.
    ``template`` supplies stimulus and overlays to the two spec builders.
    """

    stops: tuple[AngleStop, ...]
    mover: str = MOVER_HUMAN
    template: MeasureSpec = DEFAULT_TEMPLATE
    program: str = ""
    candidates: tuple[str, ...] = ()
    level: LevelPolicy = LevelPolicy()
    level_source: str = ""
    levels: tuple[float, ...] | None = None
    repeats: int = 1
    retries_per_pose: int = MAX_EXTRA_ATTEMPTS_PER_POSITION

    def __post_init__(self) -> None:
        object.__setattr__(self, "stops", tuple(self.stops))
        if not self.stops:
            raise CrossoverV2FlowError("an angle capture request needs at least one stop")
        if self.mover not in MOVERS:
            raise CrossoverV2FlowError(
                f"mover must be one of {MOVERS}, got {self.mover!r}"
            )
        self._refuse_beyond_reach(
            POSITION_AXIS_HORIZONTAL,
            MOVER_MAX_ANGLE_DEG[self.mover],
            tuple(stop.angle_deg for stop in self.stops),
        )
        self._refuse_beyond_reach(
            POSITION_AXIS_VERTICAL,
            MOVER_MAX_ELEVATION_DEG[self.mover],
            tuple(stop.elevation_deg for stop in self.stops),
        )
        unreachable = sorted({stop.kind for stop in self.stops if stop.kind != POSE_KIND_BEARING})
        if unreachable and self.externally_positioned:
            raise LateralWalkRefused(
                WALK_OVER_MOVER_ENVELOPE,
                f"mover={self.mover!r} turns bearings at the mark, so it cannot "
                f"reach a {', '.join(unreachable)} pose",
            )
        self._validate_policy()
        self._refuse_bad_template()

    def _validate_policy(self) -> None:
        if not isinstance(self.level, LevelPolicy):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "level must be a LevelPolicy")
        if not self.level_source:
            object.__setattr__(self, "level_source", "operator" if self.level.level_db is not None
                               or self.levels is not None else
                               "seat_reference" if self.level.resolved is not None else "program_default")
        if self.level_source not in LEVEL_SOURCES:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, f"level_source must be one of {LEVEL_SOURCES}")
        if self.levels is not None:
            if not isinstance(self.levels, (tuple, list)) or not self.levels or None in self.levels:
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "levels must be a nonempty sequence")
            # LevelPolicy owns the fader range check for each requested level.
            for value in self.levels:
                replace(self.level, level_db=value)
            levels = tuple(float(value) for value in self.levels)
            if len(set(levels)) != len(levels):
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "levels must be distinct")
            if len(levels) == 1:
                object.__setattr__(self, "level", replace(self.level, level_db=levels[0]))
            object.__setattr__(self, "levels", levels if len(levels) > 1 else None)
        for name, minimum in (("repeats", 1), ("retries_per_pose", 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, f"{name} must be an integer >= {minimum}")
        if not isinstance(self.candidates, (tuple, list)) or any(
            not isinstance(c, str) or not c for c in self.candidates
        ):
            raise LateralWalkRefused(WALK_CANDIDATE_NOT_MEASURABLE, "candidates must be nonempty names")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        cycle = self.candidates or (BASE_CANDIDATE,)
        if set(cycle) != {
            candidate_identity(stop.candidate_id) for stop in self.stops
        }:
            raise LateralWalkRefused(WALK_CANDIDATE_NOT_MEASURABLE, "candidates must match the stop identities")

    def to_dict(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in asdict(self).items() if key != "levels" or value is not None},
            "template": self.template.to_dict(), "level": self.level.to_dict(),
            "stops": [
                {f.name: candidate_identity(stop.candidate_id) if f.name == "candidate_id" else
                 list(stop.seat_offset_m) if f.name == "seat_offset_m" and stop.seat_offset_m is not None else getattr(stop, f.name)
                 for f in fields(stop)
                 if f.name in ("angle_deg", "regime", "elevation_deg", "candidate_id", "purpose")
                 or getattr(stop, f.name) != f.default}
                for stop in self.stops
            ],
            "candidates": list(self.candidates),
            "artifact_schema_version": REQUEST_SCHEMA_VERSION, "kind": REQUEST_KIND,
        }

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any]) -> AngleCaptureRequest:
        if doc.get("artifact_schema_version") != REQUEST_SCHEMA_VERSION:
            raise LateralWalkRefused(WALK_SCHEMA_VERSION_UNSUPPORTED, "restage the request as version 5")
        if doc.get("kind") != REQUEST_KIND:
            raise ValueError("invalid angle request kind")
        unknown = set(doc) - {f.name for f in fields(cls)} - {"kind", "artifact_schema_version", "staged_at"}
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        missing = {f.name for f in fields(cls)} - set(doc) - {"levels", "level_source"}
        if missing:
            raise ValueError(f"request must state {', '.join(sorted(missing))}")
        values = {f.name: doc[f.name] for f in fields(cls) if f.name in doc}
        for name in ("mover", "program"):
            if not isinstance(values[name], str):
                raise ValueError(f"{name} must be text")
        if not isinstance(values["stops"], list) or not values["stops"]:
            raise ValueError("stops must be a nonempty list")
        for name, read in (
            ("stops", lambda entries: tuple(AngleStop(**entry) for entry in entries)),
            ("template", MeasureSpec.from_mapping), ("level", LevelPolicy.from_mapping),
        ):
            try:
                if name != "stops" and not isinstance(values[name], Mapping):
                    raise ValueError("must be an object")
                values[name] = read(values[name])
            except LateralWalkRefused:
                raise
            except CrossoverV2FlowError as exc:
                if name != "stops":
                    raise
                raise LateralWalkRefused(WALK_STOP_NO_LONGER_VALID, str(exc)) from exc
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"{name}: {exc}") from exc
        try:
            return cls(**values)
        except LateralWalkRefused:
            raise
        except CrossoverV2FlowError as exc:
            raise LateralWalkRefused(WALK_STOP_NO_LONGER_VALID, str(exc)) from exc

    def _refuse_bad_template(self) -> None:
        """The questions about a template that are the WALK's, not the spec's:
        what it may not state, and what its stops can play."""
        if not isinstance(self.template, MeasureSpec):
            raise LateralWalkRefused(
                WALK_TEMPLATE_NOT_ACCEPTED, f"template must be a MeasureSpec, got {self.template!r}",
            )
        stated = [name for name in _EXECUTOR_ASSIGNED if getattr(self.template, name)
                  and not (name == "candidate_id" and self.template.candidate_id == BASE_CANDIDATE)]
        if stated:
            raise LateralWalkRefused(
                WALK_TEMPLATE_NOT_ACCEPTED,
                f"a walk's template states what each capture is measured at, "
                f"so it cannot carry {', '.join(stated)}",
            )
        summed_stop = any(stop.plays_summed for stop in self.stops)
        if _states_summed_sweep(self.template.sweep_band_hz, self.template.sweep_s) and not summed_stop:
            raise LateralWalkRefused(
                WALK_STIMULUS_NOT_ACCEPTED,
                "sweep_band_hz/sweep_s ride summed stops; this walk names none",
            )
        if summed_stop and _states_overlay(
            polarity=self.template.polarity, inverted_role=self.template.inverted_role,
            delayed_role=self.template.delayed_role, delay_us=self.template.delay_us,
            level_matched=self.template.level_matched,
        ):
            raise LateralWalkRefused(WALK_CANDIDATE_NOT_MEASURABLE, SUMMED_TRIALS_PLAY_THEIR_OWN_GRAPH)

    def _refuse_beyond_reach(
        self, axis: str, bound: int, asked: tuple[int, ...]
    ) -> None:
        # LateralWalkRefused, not a bare CrossoverV2FlowError: decidable from
        # the request alone, with no session needed to judge it.
        outside = tuple(deg for deg in asked if abs(deg) > bound)
        if outside:
            raise LateralWalkRefused(
                WALK_OVER_MOVER_ENVELOPE,
                f"mover={self.mover!r} travels +/-{bound} deg on the {axis} "
                "axis, so it cannot reach "
                + ", ".join(f"{deg:+d}" for deg in outside)
                + " deg",
            )

    @property
    def externally_positioned(self) -> bool:
        """Whether an external driver moves the microphone between stops. The ADVANCE axis only;
        whether a session HOLDS each begin is a separate fact
        (:attr:`V2PlanShape.positions_gated`).
        """
        return self.mover == MOVER_ARM


# --------------------------------------------------------------------------- #
# angle -> pose: the one new primitive
# --------------------------------------------------------------------------- #


def pose_at_angle(
    angle_deg: int,
    elevation_deg: int = 0,
    *,
    kind: str = POSE_KIND_BEARING,
    distance_m: float | None = None,
    seat_offset_m: tuple[float, float, float] | None = None,
    driver: str = "",
) -> CloudPositionPrompt:
    """The pose at a stated bearing -- the exact inverse of :func:`position_angle_deg`.

    Returns a cm-primary pose rather than carrying the angle onward, since ``offset_cm``
    is the load-bearing datum of every shipped consumer
    (:attr:`CloudPositionPrompt.wide`, the evidence sidecar, the attribution stage).
    ``position_angle_deg(pose_at_angle(d)) == d`` for every whole degree this accepts --
    that round trip is a test, not a claim. ``elevation_deg`` rides the same
    construction on the orthogonal axis (``vertical_offset_cm``,
    :func:`position_elevation_deg`).

    ``role`` is DERIVED from :data:`WIDE_OFFSET_MIN_CM`, not chosen, reproducing the
    shipped table's own assignment (12/25 cm rows ``onax``, 40/60 cm ``offax``) from the
    SIDEWAYS offset alone.

    The copy is stated as the ANGLE for BOTH movers: this seam asks the angle question
    of the REQUEST and the advance question of the MOVER, reusing
    :func:`remote_position_prompt` (mover-neutral: "keep it 1 m from the speaker and
    pointed at it" is what a taut string does by construction and what an arm does by
    radius).
    """
    degrees = _validated_angle(angle_deg)
    elevation = _validated_angle(elevation_deg)
    distance = MARK_DISTANCE_M if distance_m is None else float(distance_m)
    offset_cm = _offset_cm_at(degrees, distance)
    role = POSITION_ROLE_OFFAX if offset_cm >= WIDE_OFFSET_MIN_CM else POSITION_ROLE_ONAX
    geometric = CloudPositionPrompt(
        # Placeholder, immediately replaced: copy is derived from the geometry.
        headline="",
        detail="",
        offset_cm=offset_cm,
        role=role,
        lateral_sign=_sign_of(degrees),
        vertical_sign=_sign_of(elevation),
        vertical_offset_cm=_offset_cm_at(elevation, distance),
        kind=kind,
        distance_m=distance_m,
        seat_offset_m=seat_offset_m,
        driver=driver,
    )
    return remote_position_prompt(geometric)


def _sign_of(degrees: int) -> int:
    return 0 if degrees == 0 else (1 if degrees > 0 else -1)


def _offset_cm_at(degrees: int, distance_m: float = MARK_DISTANCE_M) -> float:
    """The cm displacement one bearing names, in the mark's own plane. The tangent
    :func:`position_angle_deg`/:func:`position_elevation_deg` both invert, written once.
    """
    return 100.0 * distance_m * math.tan(math.radians(abs(degrees)))


# --------------------------------------------------------------------------- #
# template -> the specs that play
# --------------------------------------------------------------------------- #


def design_axis_spec(request: AngleCaptureRequest) -> MeasureSpec:
    """The spec this walk's design-axis MEASURE captures play: the template at
    :data:`~.crossover_v2.measure_spec.GRAPH_SCOPE_DRIVERS`, the band and duration
    stripped since that scope cannot play a summed sweep (:data:`TEMPLATE_SWEEP_SCOPE`)."""
    return replace(
        request.template,
        kind=MEASURE_KIND_CANDIDATE,
        graph_scope=GRAPH_SCOPE_DRIVERS, candidate_id="",
        sweep_band_hz=(),
        sweep_s=None,
    )


def stop_specs(
    request: AngleCaptureRequest,
    *,
    prompts: Sequence[CloudPositionPrompt],
    baseline_id: str,
    roles_bands: Sequence[RoleBand] = (),
) -> tuple[MeasureSpec | None, ...]:
    """Place the banked base and named candidates; ``None`` for per-driver takes."""
    placed: list[MeasureSpec | None] = []
    for stop, prompt in zip(request.stops, prompts):
        if not stop.plays_summed:
            placed.append(None)
            continue
        placed.append(replace(
            request.template,
            kind=MEASURE_KIND_CANDIDATE if stop.candidate_id else MEASURE_KIND_VERIFY,
            positions=(stop.angle_deg,),
            sweep_band_hz=() if stop.stimulus else request.template.sweep_band_hz or (
                room_sweep_band_hz(roles_bands, (prompt,))
                if roles_bands and resolved_measurement_purpose(stop.purpose, stop.kind) != PURPOSE_SPEAKER else None
            ) or (),
            sweep_s=None if stop.stimulus else request.template.sweep_s,
            vertical_deg=stop.elevation_deg,
            pose_prompts=(prompt.text,),
            candidate_id=stop.candidate_id or baseline_id,
            graph_scope="candidate_branches" if stop.regime == REGIME_BRANCHES else "candidate",
            branch_target_ids=(branch_target_ids_for(stop.branch_pair, roles_bands)
                               if stop.regime == REGIME_BRANCHES else ()),
            stimulus=stop.stimulus,
            regime=MEASURE_REGIME_NEAR_FIELD if stop.regime == REGIME_NEAR_FIELD else request.template.regime,
        ))
    return tuple(spec for spec in placed for _ in range(request.repeats))


# --------------------------------------------------------------------------- #
# the three constructors -- "per-angle, per-driver, both, whatever we want"
# --------------------------------------------------------------------------- #


def per_driver_at(
    angles_deg: Sequence[int], *, mover: str = MOVER_HUMAN,
) -> AngleCaptureRequest:
    """Per-driver captures at each angle -- the P2 forward model's input. Angles pass through
    to :class:`AngleStop` UNCOERCED (see :func:`_validated_angle`).
    """
    return AngleCaptureRequest(
        stops=tuple(AngleStop(a, REGIME_PER_DRIVER) for a in angles_deg),
        mover=mover,
    )


def summed_at(
    angles_deg: Sequence[int], *, mover: str = MOVER_HUMAN,
) -> AngleCaptureRequest:
    """Summed captures at each angle -- the system response off the axis."""
    return AngleCaptureRequest(
        stops=tuple(AngleStop(a, REGIME_SUMMED) for a in angles_deg),
        mover=mover,
    )


def both_at(
    angles_deg: Sequence[int], *, mover: str = MOVER_HUMAN,
) -> AngleCaptureRequest:
    """Both regimes at each angle, PAIRED so the microphone moves once per angle. Per-driver
    first at each stop, then summed from the same position -- the two are only
    comparable if nothing moved between them.
    """
    stops: list[AngleStop] = []
    for angle in angles_deg:
        stops.append(AngleStop(angle, REGIME_PER_DRIVER))
        stops.append(AngleStop(angle, REGIME_SUMMED))
    return AngleCaptureRequest(stops=tuple(stops), mover=mover)


def default_run_level(
    program: MeasurementProgram | AngleCaptureRequest,
    *,
    state_path: str | Path | None = None,
) -> tuple[LevelPolicy, str]:
    """Choose the scalar default only when the run has no level ladder."""
    reference_volume_db = seat_level_reference_volume_db(state_path=state_path)
    if program.levels is None and reference_volume_db is not None:
        return LevelPolicy(level_db=reference_volume_db), "seat_reference"
    return LevelPolicy(), "program_default"


def request_for_program(
    program: MeasurementProgram,
    *,
    candidates: tuple[str, ...] = (),
    mover: str = MOVER_HUMAN,
    template: MeasureSpec = DEFAULT_TEMPLATE,
    level: LevelPolicy = LevelPolicy(),
    level_source: str = "",
    levels: tuple[float, ...] | None = None,
    repeats: int = 1,
    retries_per_pose: int = MAX_EXTRA_ATTEMPTS_PER_POSITION,
) -> AngleCaptureRequest:
    """Expand poses with adjacent driver repeats, room sweeps and candidate trials."""
    if program.mover is not None and program.mover != mover:
        raise LateralWalkRefused(REASON_WALK_MOVER_MISMATCH, f"{program.program_id}/{program.size} requires mover={program.mover}")
    if program.regime == REGIME_BRANCHES and (len(candidates) != 1 or candidate_identity(candidates[0]) == BASE_CANDIDATE):
        raise CrossoverV2FlowError("branches needs one saved complete candidate fingerprint")
    room_sweep = program.room_sweep and not candidates
    return AngleCaptureRequest(
        stops=tuple(
            replace(
                stop, elevation_deg=pose.elevation_deg, candidate_id=candidate,
                kind=pose.kind,
                distance_m=pose.distance_m,
                seat_offset_m=pose.seat_offset_m,
                purpose=PURPOSE_ROOM if room_sweep and stop.plays_summed else program.purpose,
                headline=pose.headline, detail=pose.detail,
                stimulus=program.stimulus,
                branch_pair=program.branch_pair,
            )
            for pose in program.poses
            for stop in (both_at((pose.azimuth_deg,), mover=mover).stops if room_sweep else (
                AngleStop(pose.azimuth_deg, REGIME_SUMMED if candidates and program.regime == REGIME_PER_DRIVER else program.regime,
                          kind=pose.kind, distance_m=pose.distance_m, seat_offset_m=pose.seat_offset_m,
                          purpose=program.purpose, driver=pose.driver),))
            for _ in range(1 if room_sweep and stop.plays_summed else pose.repeats)
            for candidate in (candidates or (BASE_CANDIDATE,))
        ),
        mover=mover,
        template=template,
        candidates=candidates,
        level=level, level_source=level_source, levels=levels,
        repeats=repeats, retries_per_pose=retries_per_pose,
        # ``spot`` carries caller geometry rather than a registry row, so its
        # size names nothing an operator chose.
        program=(
            program.program_id
            if program.program_id == "spot"
            else f"{program.program_id}/{program.size}"
        ),
    )


def walk_price(request: AngleCaptureRequest, *, roles_bands: Sequence[RoleBand] = ()) -> dict[str, int | float | None]:
    """Price the same capture schedule shown by the page, including preparation."""
    from .plan_run import prepare_plan_captures  # lazy: plan_run imports this request model

    captures = len(prepare_plan_captures(request, roles_bands=roles_bands))
    return {
        "mic_moves": sum(1 for _place, _stops in groupby(s.place for s in request.stops)),
        "captures": captures,
        "ceiling_min": math.ceil(
            wall_clock_ceiling_s(captures) / 60
        ),
        "stimulus_s": (
            None if request.template.sweep_s is None
            else captures * request.template.sweep_s
            * max(1, len(request.template.level_ladder_dbfs))
        ),
    }


# --------------------------------------------------------------------------- #
# resolution -- request -> the shipped primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedStop:
    """One stop, resolved into what the shipped runner already consumes. ``index`` is the
    1-based capture index the capture drives (``index == accepted_count + 1``).
    ``program_phase`` names the phase whose composed program object this stop plays;
    :func:`program_for_stop` turns it into the object -- a program question, never a
    session-journey one (see the module docstring). ``candidate_id`` is the request
    stop's, carried so a resolved walk names the variant each stop measures.
    """

    index: int
    angle_deg: int
    regime: str
    elevation_deg: int
    prompt: CloudPositionPrompt
    program_phase: str
    screen: Mapping[str, str]
    candidate_id: str = ""

    def __post_init__(self) -> None:
        # Frozen means frozen: read-only view, still compares equal to a plain dict.
        object.__setattr__(self, "screen", MappingProxyType(dict(self.screen)))


def _screen_policy(request: AngleCaptureRequest, prompt: CloudPositionPrompt) -> dict[str, str]:
    """One stop's advance policy and, for an arm, its target position. A hand-guided stop
    declares no target: whether begins are HELD is the SESSION's fact. Angle is re-read
    off the POSE via :func:`position_angle_deg`, not copied from the request.
    """
    if not request.externally_positioned:
        return {"auto_advance": AUTO_ADVANCE_TAP}
    return {
        "auto_advance": AUTO_ADVANCE_COUNTDOWN,
        "countdown_s": str(AUTO_ADVANCE_COUNTDOWN_S),
        POSITION_DEG_KEY: str(position_angle_deg(prompt)),
        POSITION_ROLE_KEY: prompt.role,
    }


def resolve_request(request: AngleCaptureRequest) -> tuple[ResolvedStop, ...]:
    """The whole request, resolved into indexed stops in running order: pose from angle,
    program from regime, advance policy from mover -- three independent axes composed
    once, here.
    """
    resolved: list[ResolvedStop] = []
    for offset, stop in enumerate(request.stops):
        pose = pose_at_angle(
            stop.angle_deg, stop.elevation_deg, kind=stop.kind,
            distance_m=stop.distance_m, seat_offset_m=stop.seat_offset_m, driver=stop.driver,
        )
        pose = replace(pose, purpose=stop.purpose, preserve_text=bool(stop.headline or stop.detail),
                       headline=stop.headline or pose.headline, detail=stop.detail or pose.detail)
        resolved.append(
            ResolvedStop(
                index=offset + 1,
                angle_deg=stop.angle_deg,
                regime=stop.regime,
                elevation_deg=stop.elevation_deg,
                prompt=pose,
                program_phase=_REGIME_PROGRAM_PHASE[stop.regime],
                screen=_screen_policy(request, pose),
                candidate_id=stop.candidate_id,
            )
        )
    return tuple(resolved)


def program_for_stop(
    stop: ResolvedStop,
    *,
    check: ExcitationProgram,
    measure: ExcitationProgram | None,
    verify: ExcitationProgram,
    cloud: ExcitationProgram,
) -> ExcitationProgram:
    """The composed program this stop plays -- BY IDENTITY, through the shipped dispatcher.
    Delegates to :func:`~jasper.active_speaker.crossover_v2.programs.program_for_phase`,
    so a per-driver stop gets the very same MEASURE object the design-axis anchor played
    (a different level or sweep would make cross-angle comparison uninterpretable).
    Requesting a per-driver stop before the CHECK gain solve raises
    ``NoProgramForPhaseError``, uncaught here.
    """
    return program_for_phase(
        stop.program_phase,
        check=check,
        measure=measure,
        verify=verify,
        cloud=cloud,
    )


def index_phase_map(request: AngleCaptureRequest) -> dict[int, str]:
    """Capture index -> the phase whose program runs there. Same shape
    ``build_v2_cloud_index_phase_map`` returns, so shipped consumers
    (:func:`announced_capture_indexes`) work over an angle walk unchanged.
    """
    return {stop.index: stop.program_phase for stop in resolve_request(request)}


WALK_REGIME_UNSUPPORTED = "walk_regime_unsupported"

#: A stop is outside the stated mover's own reach on one AXIS
#: (:data:`MOVER_MAX_ANGLE_DEG`, :data:`MOVER_MAX_ELEVATION_DEG`). Decided by
#: :class:`AngleCaptureRequest` at STATEMENT time, not at a 600 s live hold.
WALK_OVER_MOVER_ENVELOPE = "walk_over_mover_envelope"

WALK_LEVEL_POLICY_INVALID = "walk_level_policy_invalid"

WALK_SCHEMA_VERSION_UNSUPPORTED = "walk_schema_version_unsupported"

#: The walk states an SPL ceiling and no microphone sensitivity resolves, so
#: nothing could turn a recording into dB SPL to watch it. Decided beside the
#: ceiling, where the watch is built (:func:`~.plan_run.spl_watch`). The value
#: is the reason ``jasper-measure`` publishes for the same refusal.
WALK_SPL_CALIBRATION_REQUIRED = "measure_spl_calibration_required"

#: The walk states an SPL ceiling and the box's own commissioning preset
#: declares no finite stop to bound it against
#: (:func:`~.commission_wiring.commissioning_spl_ceiling_db` raises
#: ``ValueError``). ``jasper-measure`` meets the same ``ValueError`` with its
#: own ``jasper.cli.measure.REFUSE_BOX_NOT_READY``.
WALK_COMMISSIONING_STOP_UNSET = "walk_commissioning_stop_unset"

#: The walk's stimulus statement is not one that can be played: a summed sweep
#: with no summed stop to ride (:class:`AngleCaptureRequest`, statement time), a
#: template field ``MeasureSpec`` refuses that is neither R-1 half
#: (:func:`walk_template`), or a stop pose it refuses when the host places the
#: template (:func:`stop_specs`) -- detail is the spec's own sentence.
WALK_STIMULUS_NOT_ACCEPTED = "walk_stimulus_not_accepted"

#: The composed session would need more capture blob indexes than exist.
WALK_OVER_CAPTURE_CAPACITY = "walk_over_capture_capacity"

#: A banked stop no longer satisfies this module's own contract (a
#: hand-edited angle, an unknown regime or mover). The spool re-raises
#: :func:`_validated_angle`'s bare :class:`CrossoverV2FlowError` under this slug.
WALK_STOP_NO_LONGER_VALID = "walk_stop_no_longer_valid"

#: The walk's template carries what the EXECUTOR assigns per capture
#: (:data:`_EXECUTOR_ASSIGNED`), so the walk would measure somewhere other than
#: the stops it states. Decided by :class:`AngleCaptureRequest` at statement
#: time, like :data:`WALK_OVER_MOVER_ENVELOPE`.
WALK_TEMPLATE_NOT_ACCEPTED = "walk_template_not_accepted"

#: The walk's ``(polarity, inverted_role)`` pair is not one
#: :class:`~.crossover_v2.measure_spec.MeasureSpec` accepts, judged by BUILDING
#: the template (:func:`walk_template`) -- detail is the spec's own sentence.
WALK_POLARITY_NOT_ACCEPTED = "walk_polarity_not_accepted"

#: The walk's ``(delayed_role, delay_us)`` pair is not one ``MeasureSpec``
#: accepts. Own slug (not :data:`WALK_POLARITY_NOT_ACCEPTED`) so ``reason=``
#: names WHICH half of R-1 was refused.
WALK_DELAY_NOT_ACCEPTED = "walk_delay_not_accepted"

#: The walk asks to level-match driver branches and this box has no measured
#: evidence to level them BY (trims resolve on-box from banked base trim or
#: guided captures). Raised by the CALLER; this module reads no box state.
WALK_LEVEL_MATCH_NO_EVIDENCE = "walk_level_match_no_evidence"

WALK_CANDIDATE_NOT_MEASURABLE = "walk_candidate_not_measurable"

#: Every stop in the walk is :data:`REGIME_PER_DRIVER`, so the playable subset
#: :func:`~.plan_run.run_plan` resolves is empty -- nothing here composes that
#: regime's phase program (the session host's job, not this loop's), and a run
#: that measured zero takes is a refusal, not an empty success.
WALK_NOTHING_PLAYABLE = "walk_nothing_playable"

SUMMED_TRIALS_PLAY_THEIR_OWN_GRAPH = "Summed trials use the selected graph's own trims and alignment."

WALK_REFUSAL_REASONS = frozenset({
    WALK_REGIME_UNSUPPORTED,
    REASON_WALK_MOVER_MISMATCH,
    WALK_OVER_MOVER_ENVELOPE,
    WALK_LEVEL_POLICY_INVALID,
    WALK_SCHEMA_VERSION_UNSUPPORTED,
    WALK_SPL_CALIBRATION_REQUIRED,
    WALK_COMMISSIONING_STOP_UNSET,
    WALK_STIMULUS_NOT_ACCEPTED,
    WALK_OVER_CAPTURE_CAPACITY,
    WALK_STOP_NO_LONGER_VALID,
    WALK_TEMPLATE_NOT_ACCEPTED,
    WALK_POLARITY_NOT_ACCEPTED,
    WALK_DELAY_NOT_ACCEPTED,
    WALK_LEVEL_MATCH_NO_EVIDENCE,
    WALK_CANDIDATE_NOT_MEASURABLE,
    WALK_NOTHING_PLAYABLE,
})


class LateralWalkRefused(CrossoverV2FlowError):
    """A walk may not run -- either as STATED, or in THIS session. Most reasons are properties
    of the pair (walk, session), judged only by :func:`session_lateral_walk`;
    :data:`WALK_OVER_MOVER_ENVELOPE` is a property of the request alone, raised by
    :class:`AngleCaptureRequest` at statement time. ``reason`` is from
    :data:`WALK_REFUSAL_REASONS`; ``detail`` is the sentence a person reads.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def session_lateral_walk(
    request: AngleCaptureRequest,
    *,
    externally_positioned: bool,
    base_entries: int,
    supported_summed_candidates: bool = False,
) -> tuple[CloudPositionPrompt, ...]:
    """The poses a measurement session should walk for this request.

    ``externally_positioned`` is the session's own ADVANCE policy
    (``V2PlanShape.externally_positioned``, never ``positions_gated``);
    ``base_entries`` is how many captures the session takes that are NOT
    this walk. Returns one pose per stop, in stop order, never a session phase.

    Raises :class:`LateralWalkRefused` with :data:`WALK_REGIME_UNSUPPORTED`,
    :data:`REASON_WALK_MOVER_MISMATCH`, or :data:`WALK_OVER_CAPTURE_CAPACITY` --
    properties of the PAIR (walk, session), so the spool's own document
    validation cannot make them. The capacity bound asks
    :func:`stage1_plan_max_attempts`, the same producer the emitted plan
    sets ``max_attempts`` from.
    """
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    off_regime = sorted({
        stop.regime for stop in request.stops if stop.regime != REGIME_PER_DRIVER
    })
    if off_regime and not (
        supported_summed_candidates
        and (all(stop.regime in (REGIME_SUMMED, REGIME_NEAR_FIELD) for stop in request.stops)
             or (len(request.stops) == 1 and request.stops[0].regime == REGIME_BRANCHES
                 and bool(request.stops[0].candidate_id)))
    ):
        raise LateralWalkRefused(
            WALK_REGIME_UNSUPPORTED,
            f"unsupported capture regimes for this session: {', '.join(off_regime)}",
        )
    if request.externally_positioned != externally_positioned:
        raise LateralWalkRefused(
            REASON_WALK_MOVER_MISMATCH,
            f"the walk states mover={request.mover!r} "
            f"(externally_positioned={request.externally_positioned}) but this "
            f"session is externally_positioned={externally_positioned}",
        )
    entries = base_entries + len(request.stops)
    attempts = stage1_plan_max_attempts(entries)
    if attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise LateralWalkRefused(
            WALK_OVER_CAPTURE_CAPACITY,
            f"{base_entries} session captures + {len(request.stops)} stops = "
            f"{entries} entries, needing {attempts} capture blob indexes over a "
            f"ceiling of {MAX_CAPTURE_PLAN_ATTEMPTS}",
        )
    return tuple(stop.prompt for stop in resolve_request(request))


def announced_indexes(request: AngleCaptureRequest) -> tuple[int, ...]:
    """Which stops of this walk play the courtesy prelude. Delegates to
    :func:`announced_capture_indexes` so "what will the household hear" keeps ONE owner
    (``courtesy_prelude_for_phase``).

    Today empty for every request -- neither regime's program phase is a session opener.
    A standalone runner still owes an opening warning: ``_courtesy_beeps_step``
    (:mod:`jasper.active_speaker.crossover_v2.sweep_spec`) refuses an empty
    ``announced_captures`` outright, so it must open on an announced capture the way
    stage 1 does, on CHECK.
    """
    return announced_capture_indexes(index_phase_map(request))
