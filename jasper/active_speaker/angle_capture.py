# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture a stated set of ANGLES, in a stated stimulus regime, by a stated mover.

Composes ``{per-driver | summed} x {angles} x {arm | human-guided}`` over shipped parts.
The one new primitive is :func:`pose_at_angle`: it words a pose's bearing as the
centimetre move the evidence sidecar, the ``wide`` rule and the attribution stage already
read, and that move reads back as the same whole degree.

These poses are FORWARD-MODEL INPUT, never a pose-ratio statistic: the lateral-walk
statistic was retired as invalidated, and the P2 complex-summation
model consumes each angle's transfer function directly.

This module never constructs :data:`~.crossover_v2.journey.PHASE_LATERAL` -- it returns
poses and refusals, the session host tags indexes with a phase.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields, replace
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from jasper.platform.json_fields import finite_float
from jasper.audio_measurement.program import RoleBand

from .crossover_v2.refusal_copy import REASON_MEASUREMENT_CANDIDATE_REQUIRED, REASON_WALK_MOVER_MISMATCH
from .movers import MOVER_ARM, MOVER_HUMAN, MOVER_CONFIRMED, MOVERS
from .fader_hold import EMERGENCY_MEASUREMENT_VOLUME_DB
from .crossover_v2.admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from .crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)
from .crossover_v2.measure_spec import (
    GRAPH_SCOPE_DRIVERS, MeasureSpec, branch_target_ids_for,
)
from .measurement_programs import (
    BASE_CANDIDATE, POSE_KIND_BEARING,
    BRANCH_PAIR_DRIVERS,
    candidate_identity,
    cleared_layers,
    Pose,
    PoseLevel,
    Preset,
    plan_poses,
    pose_level,
    REGIME_PER_DRIVER,
    REGIME_SUMMED,
    REGIME_BRANCHES,
    REGIMES,
    SPOT_LEVEL,
    UnknownPresetError,
    preset,
    run_level,
    validated_branch_pair,
    validated_capture_purpose,
    validated_purposes,
    validated_angle,
    validated_stimulus,
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
    position_angle_deg,
    remote_position_prompt,
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
    "MAX_ANGLE_DEG",
    "MAX_ELEVATION_DEG",
    "ARM_ENVELOPE_DEG",
    "MOVER_MAX_ANGLE_DEG",
    "MOVER_MAX_ELEVATION_DEG",
    "LevelPolicy",
    "BASE_CANDIDATE",
    "candidate_identity",
    "AngleStop",
    "AngleCaptureRequest",
    "ResolvedStop",
    "DEFAULT_TEMPLATE",
    "TEMPLATE_SWEEP_SCOPE",
    "pose_at_angle",
    "design_axis_spec",
    "stop_specs",
    "request_for_preset",
    "resolve_request",
    "WALK_OVER_MOVER_ENVELOPE",
    "WALK_LEVEL_POLICY_INVALID",
    "WALK_SPL_CALIBRATION_REQUIRED",
    "WALK_COMMISSIONING_STOP_UNSET",
    "WALK_STIMULUS_NOT_ACCEPTED",
    "WALK_OVER_CAPTURE_CAPACITY",
    "WALK_TEMPLATE_NOT_ACCEPTED",
    "WALK_CANDIDATE_NOT_MEASURABLE",
    "WALK_REFUSAL_REASONS",
    "LateralWalkRefused",
]


LEVEL_SOURCES = ("program_default", "operator")


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
    """One stop: a pose, and what is played there.

    The pose's angles are signed WHOLE degrees (negative LEFT, positive RIGHT
    of the design axis; negative BELOW mark height), because a tenth of a
    degree claims precision the ~1 m mark placement never had; which mover may
    ask for a non-zero elevation is :data:`MOVER_MAX_ELEVATION_DEG`.
    ``candidate_id`` is the banked candidate fingerprint this stop measures
    (``""`` for the program's baseline layer). ``branch_pair`` is which two
    targets a ``branches`` stop excites
    (:data:`~.measurement_programs.BRANCH_PAIRS`). A pose that names its
    driver plays that one target alone (ADR-0366). A stop is one take of its
    pose, so its pose states no take count; the request's ``repeats`` repeats
    every stop.
    """

    pose: Pose
    regime: str
    candidate_id: str = BASE_CANDIDATE
    purpose: str | None = None
    #: Every purpose its takes serve, :attr:`purpose` first; a stop that names
    #: only its purpose serves that one (ADR-0383).
    purposes: tuple[str, ...] = ()
    stimulus: Mapping[str, Any] | None = None
    branch_pair: str = BRANCH_PAIR_DRIVERS

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", candidate_identity(self.candidate_id, for_spec=True))
        _validated_angle(self.pose.azimuth_deg)
        _validated_angle(self.pose.elevation_deg)
        try:
            purpose = validated_capture_purpose(self.purpose, self.regime)
            purposes = validated_purposes(self.purposes or (purpose,), self.regime, (self.pose,))
            if purposes[0] != purpose:
                raise ValueError(f"a stop's purposes start with its purpose {purpose!r}, got {list(purposes)}")
            object.__setattr__(self, "purposes", purposes)
            validated_branch_pair(self.branch_pair, self.regime)
            if self.pose.driver and self.candidate_id:
                raise ValueError("a driver's pose plays the neutral drivers graph; it measures no candidate")
            if self.pose.repeats != 1:
                raise ValueError("a stop is one take of its pose; the request's repeats repeat it")
        except ValueError as exc:
            raise CrossoverV2FlowError(str(exc)) from None
        if self.stimulus is not None:
            try:
                if not self.pose.driver and self.regime != REGIME_SUMMED:
                    raise ValueError(f"a {self.regime} stop plays a stimulus only on the driver its pose names")
                validated_stimulus(self.stimulus, one_driver=bool(self.pose.driver))
            except ValueError as exc:
                raise LateralWalkRefused(WALK_STIMULUS_NOT_ACCEPTED, str(exc)) from None

    @property
    def plays_summed(self) -> bool:
        """Whether this stop plays a summed graph (the scope a summed sweep
        rides); a stop naming its driver plays that driver alone instead."""
        return self.regime in (REGIME_SUMMED, REGIME_BRANCHES) and not self.pose.driver

    @property
    def level(self) -> PoseLevel | None:
        """The level rule of this stop's takes, where its pose's rule
        (:func:`~.measurement_programs.pose_level`) meets what plays there: a
        driver's pose always levels itself (ADR-0365), and so does a branch
        take at any spot, whose branches each play alone; any other driverless
        stop only when it plays the summed sweep, whose probe summed admission
        reads, and no bass stimulus, whose ladder is a deliberate series
        (ADR-0403)."""
        if self.regime == REGIME_BRANCHES and not self.pose.driver:
            return SPOT_LEVEL
        if self.pose.driver or (self.regime == REGIME_SUMMED and self.stimulus is None):
            return pose_level(self.pose)
        return None


def played_layers(stop: AngleStop) -> tuple[str, ...]:
    """The candidate layers a stop's summed take plays cleared: what its purpose
    clears on the base or on a candidate it names (ADR-0370)."""
    return cleared_layers(stop.purpose, base=not stop.candidate_id, regime=stop.regime)


def take_level(stop: AngleStop, *, scope: str, over_timing: bool) -> PoseLevel | None:
    """The level rule of a take of ``stop`` on graph ``scope``: its stop's own
    (:attr:`AngleStop.level`). In a run that takes a timing take, a summed take
    on a candidate graph levels itself too, at its pose's run level, since the
    timing take's probe reads only the timing graph (ADR-0408)."""
    if stop.level is None and over_timing and scope == TEMPLATE_SWEEP_SCOPE:
        return run_level(stop.pose.kind)
    return stop.level


def level_sets(stops: Sequence[AngleStop], scopes: Sequence[str]) -> tuple[int | None, ...]:
    """For each take in run order, given its graph scope, the index of the take
    whose level it shares, or ``None`` when it plays at its run's fader (ADR-0366
    §2). A driver's takes share a level within their placement (ADR-0361). A
    driverless summed spot closer than the mark shares one with the next spots at
    its kind and distance -- its repeats and lateral poses -- found by the set's
    first take (ADR-0403), and so does each candidate graph's summed take at any
    spot of a run that takes a timing take (ADR-0408); a branch take of one pair
    shares one only within its placement, since each placement probes what it
    plays (ADR-0407). A summed set is one candidate graph there (its candidate and
    ``played_layers``), levelled by that graph's own first take; a branch set's
    probes play the drivers graph, so its candidates share them (ADR-0406)."""
    over_timing = "timing" in scopes

    def key(stop: AngleStop) -> tuple[object, ...]:
        if stop.pose.driver:
            return stop.pose.place
        if stop.regime == REGIME_BRANCHES:
            return (stop.regime, stop.branch_pair, stop.pose.place)
        return (stop.regime, stop.branch_pair, stop.pose.kind, stop.pose.distance_m)

    starts: list[int | None] = []
    firsts: dict[tuple[object, ...], int] = {}
    for index, (stop, scope) in enumerate(zip(stops, scopes, strict=True)):
        if take_level(stop, scope=scope, over_timing=over_timing) is None:
            starts.append(None)
            continue
        if not (index > 0 and starts[-1] is not None and key(stops[index - 1]) == key(stop)):
            firsts = {}
        summed = stop.regime == REGIME_SUMMED and not stop.pose.driver
        graph = (stop.candidate_id, played_layers(stop)) if summed else ()
        starts.append(firsts.setdefault(graph, index))
    return tuple(starts)


def _stated(record: Any, always: tuple[str, ...]) -> dict[str, Any]:
    """A record's fields that ``always`` names or that differ from their
    defaults, tuples as lists: a stop's shape in :meth:`AngleCaptureRequest.to_dict`."""
    return {f.name: list(value) if isinstance(value, tuple) else value
            for f in fields(record) for value in (getattr(record, f.name),)
            if f.name in always or value != f.default}


#: The graph scope a summed sweep plays on: the candidate graph.
TEMPLATE_SWEEP_SCOPE = "candidate"


def _states_summed_sweep(sweep_band_hz: object, sweep_s: object) -> bool:
    return bool(sweep_band_hz) or sweep_s is not None


#: What a walk that states no spec carries: the design-axis capture the host has
#: always built, with no stimulus stated.
DEFAULT_TEMPLATE = MeasureSpec(kind=MEASURE_KIND_CANDIDATE)

#: The template fields the EXECUTOR or its composition seam assigns per capture,
#: and which a walk therefore may not state: a stated one would be silently
#: replaced at every stop and silently kept on the design-axis spec.
_EXECUTOR_ASSIGNED = ("positions", "pose_prompts", "candidate_id", "branch_target_ids", "level_probe",
                      "bass_reserve_db")


@dataclass(frozen=True)
class LevelPolicy:
    """The fader every take of a run holds: the one its probe finds, capped at a
    stated ``level_db``; a run with no probe holds the stated level (ADR-0403 §4)."""

    level_db: float | None = None

    def __post_init__(self) -> None:
        if self.level_db is not None and (finite_float(self.level_db) is None
                or not EMERGENCY_MEASUREMENT_VOLUME_DB < self.level_db <= 0):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "level_db is outside the measurement fader range")

    def to_dict(self) -> dict[str, Any]:
        return {"level_db": self.level_db}


@dataclass(frozen=True)
class AngleCaptureRequest:
    """One ordered walk, with adjacent candidates at each pose.

    ``program`` (the preset id) and ``layout`` (its named layout, or ``custom``)
    are provenance, and the preset's ``timing_take`` decides the timing take;
    geometry and purpose come from the stops.
    ``template`` supplies the stimulus to the two spec builders.
    """

    stops: tuple[AngleStop, ...]
    mover: str = MOVER_HUMAN
    template: MeasureSpec = DEFAULT_TEMPLATE
    program: str = ""
    layout: str = ""
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
            tuple(stop.pose.azimuth_deg for stop in self.stops),
        )
        self._refuse_beyond_reach(
            POSITION_AXIS_VERTICAL,
            MOVER_MAX_ELEVATION_DEG[self.mover],
            tuple(stop.pose.elevation_deg for stop in self.stops),
        )
        unreachable = sorted({stop.pose.kind for stop in self.stops if stop.pose.kind != POSE_KIND_BEARING})
        if unreachable and self.externally_positioned:
            raise LateralWalkRefused(
                WALK_OVER_MOVER_ENVELOPE,
                f"mover={self.mover!r} turns bearings at the mark, so it cannot "
                f"reach a {', '.join(unreachable)} pose",
            )
        self._validate_policy()
        self._refuse_bad_template()

    @property
    def takes_timing(self) -> bool:
        """Whether the run takes its preset's timing take: the base's front drivers
        summed at the mark (ADR-0319), so only with a base stop that plays every
        driver (ADR-0366). A plan naming no preset takes none."""
        try:
            timing = preset(self.program).timing_take
        except UnknownPresetError:
            return False
        return timing and any(
            candidate_identity(stop.candidate_id) == BASE_CANDIDATE and not stop.pose.driver for stop in self.stops)

    def _validate_policy(self) -> None:
        if not isinstance(self.level, LevelPolicy):
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, "level must be a LevelPolicy")
        if not self.level_source:
            object.__setattr__(self, "level_source", "operator" if self.level.level_db is not None
                               or self.levels is not None else "program_default")
        if self.level_source not in LEVEL_SOURCES:
            raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, f"level_source must be one of {LEVEL_SOURCES}")
        if self.levels is not None:
            if not isinstance(self.levels, (tuple, list)) or not self.levels or None in self.levels:
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, LADDER_STEPS_DETAIL)
            # LevelPolicy owns the fader range check for each requested level.
            for value in self.levels:
                replace(self.level, level_db=value)
            levels = tuple(float(value) for value in self.levels)
            if len(set(levels)) != len(levels):
                raise LateralWalkRefused(WALK_LEVEL_POLICY_INVALID, LADDER_STEPS_DETAIL)
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
                {**_stated(stop, ("regime", "purpose")), "candidate_id": candidate_identity(stop.candidate_id),
                 "pose": _stated(stop.pose, ("azimuth_deg", "elevation_deg"))}
                for stop in self.stops
            ],
            "candidates": list(self.candidates),
        }

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
        if self.template.level_ladder_dbfs and (self.takes_timing or any(stop.level is not None for stop in self.stops)):
            # A take that levels itself, and a timing take, which finds its run's fader, play
            # their probe first (ADR-0361 §3, ADR-0405, ADR-0408).
            raise LateralWalkRefused(
                WALK_TEMPLATE_NOT_ACCEPTED,
                "a take that levels itself finds its own level, so the template cannot state level_ladder_dbfs",
            )
        summed_stop = any(stop.plays_summed for stop in self.stops)
        if _states_summed_sweep(self.template.sweep_band_hz, self.template.sweep_s) and not summed_stop:
            raise LateralWalkRefused(
                WALK_STIMULUS_NOT_ACCEPTED,
                "sweep_band_hz/sweep_s ride summed stops; this walk names none",
            )

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
        (whether a person releases each begin by hand).
        """
        return self.mover == MOVER_ARM


# --------------------------------------------------------------------------- #
# angle -> pose: the one new primitive
# --------------------------------------------------------------------------- #


def pose_at_angle(pose: Pose) -> CloudPositionPrompt:
    """The prompt for a pose at its stated bearing -- the exact inverse of :func:`position_angle_deg`.

    The prompt carries the pose, and words its bearing as the cm move ``offset_cm``,
    the load-bearing datum of every shipped consumer
    (:attr:`CloudPositionPrompt.wide`, the evidence sidecar, the attribution stage).
    That move reads back as the pose's own bearing for every whole degree this
    accepts -- that round trip is a test, not a claim. The elevation rides the same
    construction on the orthogonal axis (``vertical_offset_cm``,
    :func:`position_elevation_deg`).

    ``role`` is DERIVED from :data:`WIDE_OFFSET_MIN_CM` and the SIDEWAYS offset alone,
    not chosen.

    The copy is stated as the ANGLE for BOTH movers: this seam asks the angle question
    of the REQUEST and the advance question of the MOVER, reusing
    :func:`remote_position_prompt` (mover-neutral: "keep it 1 m from the speaker and
    pointed at it" is what a taut string does by construction and what an arm does by
    radius), unless the pose states its own words.
    """
    degrees = _validated_angle(pose.azimuth_deg)
    elevation = _validated_angle(pose.elevation_deg)
    distance = MARK_DISTANCE_M if pose.distance_m is None else pose.distance_m
    offset_cm = _offset_cm_at(degrees, distance)
    role = POSITION_ROLE_OFFAX if offset_cm >= WIDE_OFFSET_MIN_CM else POSITION_ROLE_ONAX
    worded = remote_position_prompt(CloudPositionPrompt(
        # Placeholder, immediately replaced: copy is derived from the geometry.
        headline="",
        detail="",
        offset_cm=offset_cm,
        role=role,
        lateral_sign=_sign_of(degrees),
        vertical_sign=_sign_of(elevation),
        vertical_offset_cm=_offset_cm_at(elevation, distance),
        pose=pose,
    ))
    return replace(worded, headline=pose.headline or worded.headline, detail=pose.detail or worded.detail)


def _sign_of(degrees: int) -> int:
    return 0 if degrees == 0 else (1 if degrees > 0 else -1)


def _offset_cm_at(degrees: int, distance_m: float = MARK_DISTANCE_M) -> float:
    """The cm displacement one bearing names, in the mark's own plane."""
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
            positions=(stop.pose.azimuth_deg,),
            sweep_band_hz=() if stop.stimulus else request.template.sweep_band_hz,
            sweep_s=None if stop.stimulus else request.template.sweep_s,
            vertical_deg=stop.pose.elevation_deg,
            pose_prompts=(prompt.text,),
            candidate_id=stop.candidate_id or baseline_id,
            graph_scope="candidate_branches" if stop.regime == REGIME_BRANCHES else "candidate",
            branch_target_ids=(branch_target_ids_for(stop.branch_pair, roles_bands)
                               if stop.regime == REGIME_BRANCHES else ()),
            stimulus=stop.stimulus,
            cleared_layers=played_layers(stop),
        ))
    return tuple(spec for spec in placed for _ in range(request.repeats))


# --------------------------------------------------------------------------- #
# the constructor -- a preset's poses
# --------------------------------------------------------------------------- #


def request_for_preset(
    preset: Preset,
    *,
    candidates: tuple[str, ...] = (),
    mover: str = MOVER_HUMAN,
    level: LevelPolicy = LevelPolicy(),
    level_source: str = "",
    repeats: int = 1,
    retries_per_pose: int = MAX_EXTRA_ATTEMPTS_PER_POSITION,
    targets: Sequence[str] = (),
    driver: str = "",
) -> AngleCaptureRequest:
    """Expand poses with adjacent driver repeats and candidate trials.

    ``targets`` are the outputs this speaker declares for a pose to play alone; a
    preset's driver role expands to them, and ``driver`` narrows the run to one
    (:func:`~.measurement_programs.plan_poses`)."""
    if preset.mover is not None and preset.mover != mover:
        raise LateralWalkRefused(REASON_WALK_MOVER_MISMATCH, f"{preset.preset} requires mover={preset.mover}")
    # A pair whose takes clear a layer reads the drivers raw, so the applied base
    # may be its one candidate (ADR-0386).
    saved = bool(candidates) and candidate_identity(candidates[0]) != BASE_CANDIDATE
    if preset.regime == REGIME_BRANCHES and (len(candidates) > 1 or not saved and not cleared_layers(
            preset.purpose, base=True, regime=REGIME_BRANCHES)):
        raise LateralWalkRefused(REASON_MEASUREMENT_CANDIDATE_REQUIRED,
                                 f"{preset.preset} plays one candidate: name one saved fingerprint")
    return AngleCaptureRequest(
        stops=tuple(
            AngleStop(
                replace(pose, repeats=1),
                REGIME_SUMMED if candidates and preset.regime == REGIME_PER_DRIVER else preset.regime,
                candidate_id=candidate, purpose=preset.purpose, purposes=preset.purposes,
                stimulus=preset.stimulus, branch_pair=preset.branch_pair,
            )
            for pose in plan_poses(preset, targets, driver)
            for _ in range(pose.repeats)
            for candidate in (candidates or (BASE_CANDIDATE,))
        ),
        mover=mover,
        candidates=candidates,
        level=level, level_source=level_source,
        repeats=repeats, retries_per_pose=retries_per_pose,
        program=preset.preset,
        layout=preset.layout,
    )


# --------------------------------------------------------------------------- #
# resolution -- request -> the shipped primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedStop:
    """One stop, resolved into what the shipped runner already consumes. ``index`` is the
    1-based capture index the capture drives (``index == accepted_count + 1``).
    ``candidate_id`` is the request stop's, carried so a resolved walk names the
    variant each stop measures; its pose is its prompt's.
    """

    index: int
    regime: str
    prompt: CloudPositionPrompt
    screen: Mapping[str, str]
    candidate_id: str = ""

    def __post_init__(self) -> None:
        # Frozen means frozen: read-only view, still compares equal to a plain dict.
        object.__setattr__(self, "screen", MappingProxyType(dict(self.screen)))


def _screen_policy(request: AngleCaptureRequest, prompt: CloudPositionPrompt) -> dict[str, str]:
    """One stop's advance policy and, for an arm, its target position. A hand-guided stop
    declares no target: whether begins are HELD is the SESSION's fact. The angle is the
    prompt's pose's, read through :func:`position_angle_deg`, which refuses a vertical row.
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
    """The whole request, resolved into indexed stops in running order: the prompt from
    the pose, the advance policy from the mover -- two independent axes composed once,
    here.
    """
    resolved: list[ResolvedStop] = []
    for offset, stop in enumerate(request.stops):
        prompt = pose_at_angle(stop.pose)
        resolved.append(
            ResolvedStop(
                index=offset + 1,
                regime=stop.regime,
                prompt=prompt,
                screen=_screen_policy(request, prompt),
                candidate_id=stop.candidate_id,
            )
        )
    return tuple(resolved)


#: A stop is outside the stated mover's own reach on one AXIS
#: (:data:`MOVER_MAX_ANGLE_DEG`, :data:`MOVER_MAX_ELEVATION_DEG`). Decided by
#: :class:`AngleCaptureRequest` at STATEMENT time, not at a 600 s live hold.
WALK_OVER_MOVER_ENVELOPE = "walk_over_mover_envelope"

WALK_LEVEL_POLICY_INVALID = "walk_level_policy_invalid"

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
#: stop's declared stimulus its pose and regime cannot play (:class:`AngleStop`), or
#: a stop pose it refuses when the host places the template (:func:`stop_specs`) --
#: detail is the spec's own sentence.
WALK_STIMULUS_NOT_ACCEPTED = "walk_stimulus_not_accepted"

#: The composed session would need more capture blob indexes than exist.
WALK_OVER_CAPTURE_CAPACITY = "walk_over_capture_capacity"

#: The walk's template carries what the EXECUTOR assigns per capture
#: (:data:`_EXECUTOR_ASSIGNED`), so the walk would measure somewhere other than
#: the stops it states. Decided by :class:`AngleCaptureRequest` at statement
#: time, like :data:`WALK_OVER_MOVER_ENVELOPE`.
WALK_TEMPLATE_NOT_ACCEPTED = "walk_template_not_accepted"

WALK_CANDIDATE_NOT_MEASURABLE = "walk_candidate_not_measurable"

#: A ladder's levels are steps, not faders: the loudest plays at the level the first rung's probe finds
#: (ADR-0403 §4).
LADDER_STEPS_DETAIL = ("levels are distinct steps in dB: the loudest plays at the level the first rung's probe "
                       "finds, and each other one as far under it as it is under the loudest")

WALK_REFUSAL_REASONS = frozenset({
    REASON_WALK_MOVER_MISMATCH,
    REASON_MEASUREMENT_CANDIDATE_REQUIRED,
    WALK_OVER_MOVER_ENVELOPE,
    WALK_LEVEL_POLICY_INVALID,
    WALK_SPL_CALIBRATION_REQUIRED,
    WALK_COMMISSIONING_STOP_UNSET,
    WALK_STIMULUS_NOT_ACCEPTED,
    WALK_OVER_CAPTURE_CAPACITY,
    WALK_TEMPLATE_NOT_ACCEPTED,
    WALK_CANDIDATE_NOT_MEASURABLE,
})


class LateralWalkRefused(CrossoverV2FlowError):
    """A walk may not run -- either as STATED, or in THIS session. ``reason`` is from
    :data:`WALK_REFUSAL_REASONS`; ``detail`` is the sentence a person reads.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

