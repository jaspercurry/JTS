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
poses and refusals, the session host tags indexes with a phase. Dependency direction: a
sibling of :mod:`jasper.active_speaker.crossover_v2_flow` that imports FROM it, not
under ``crossover_v2/`` (whose modules forbid importing the flow).
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.program import ExcitationProgram

from .crossover_v2.capture_plan import V2PlanShape, stage1_base_entries
from .crossover_v2.contracts import (
    DRIVER_ROLES,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
)
from .crossover_v2.journey import PHASE_CLOUD_VERIFY, PHASE_MEASURE
from .crossover_v2.programs import program_for_phase
from .measurement_programs import MeasurementProgram
from .crossover_v2.spatial import (
    POSITION_AXIS_HORIZONTAL,
    POSITION_AXIS_VERTICAL,
)
from .crossover_v2_flow import (
    MARK_DISTANCE_M,
    POSITION_DEG_KEY,
    POSITION_ROLE_KEY,
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    WIDE_OFFSET_MIN_CM,
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_TAP,
    CloudPositionPrompt,
    CrossoverV2FlowError,
    announced_capture_indexes,
    position_angle_deg,
    remote_position_prompt,
    stage1_plan_max_attempts,
    wall_clock_ceiling_s,
)

__all__ = [
    "REGIME_PER_DRIVER",
    "REGIME_SUMMED",
    "REGIMES",
    "MOVER_ARM",
    "MOVER_HUMAN",
    "MOVERS",
    "MAX_ANGLE_DEG",
    "MAX_ELEVATION_DEG",
    "ARM_ENVELOPE_DEG",
    "MOVER_MAX_ANGLE_DEG",
    "MOVER_MAX_ELEVATION_DEG",
    "AngleStop",
    "AngleCaptureRequest",
    "ResolvedStop",
    "pose_at_angle",
    "request_for_program",
    "walk_price",
    "candidate_measure_axes",
    "per_driver_at",
    "summed_at",
    "both_at",
    "resolve_request",
    "program_for_stop",
    "index_phase_map",
    "announced_indexes",
    "WALK_REGIME_UNSUPPORTED",
    "WALK_MOVER_MISMATCH",
    "WALK_OVER_MOVER_ENVELOPE",
    "WALK_OVER_RELAY_CAPACITY",
    "WALK_LATERAL_GROUP_ALREADY_PLANNED",
    "WALK_STOP_NO_LONGER_VALID",
    "WALK_DELAY_NOT_ACCEPTED",
    "WALK_POLARITY_NOT_ACCEPTED",
    "WALK_LEVEL_MATCH_NO_EVIDENCE",
    "WALK_CANDIDATE_NOT_MEASURABLE",
    "WALK_REFUSAL_REASONS",
    "LateralWalkRefused",
    "session_lateral_walk",
]


#: Each driver swept alone, non-overlapping inside ONE capture (regime D of the ratified
#: Measurement Program v2 schedule); yields per-driver complex transfer functions for
#: the P2 forward model.
REGIME_PER_DRIVER = "per_driver"

#: One sweep through the graph as it stands (regime S): the system response at that
#: angle, the before/after evidence.
REGIME_SUMMED = "summed"

REGIMES = (REGIME_PER_DRIVER, REGIME_SUMMED)

#: An external driver turns the microphone and reports the angle reached; the one mover
#: that auto-advances
#: (:attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.externally_positioned`),
#: holds released by the driver's own report.
MOVER_ARM = "arm"

#: A person moves the microphone and taps when there, exactly as shipped hand-walked
#: tiers do -- reading the SAME angle-stated prompt the arm is driven to
#: (:func:`pose_at_angle`); only the advance policy differs.
MOVER_HUMAN = "human"

MOVERS = (MOVER_ARM, MOVER_HUMAN)

#: How far off the design axis a stop may be asked for. :func:`pose_at_angle` is a
#: tangent, so 80 deg already puts the microphone 5.7 m off a 1 m mark -- past any room
#: this measures in. GEOMETRY's ceiling; a given mover's narrower bound is
#: :data:`MOVER_MAX_ANGLE_DEG`.
MAX_ANGLE_DEG = 80

#: How far the lab positioner can actually travel; the turntable adapter
#: (``experiments/usb-turntable/jts_turntable.py``) refuses a ``position`` outside +/-45
#: deg. Restated, not imported (``experiments/`` is not a dependency), pinned together
#: by ``tests/test_arm_walk.py``.
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
})

#: Elevation half of the pair above. The arm's 0 is a rig fact: it rotates about the
#: vertical axis and cannot tilt.
MOVER_MAX_ELEVATION_DEG: Mapping[str, int] = MappingProxyType({
    MOVER_ARM: 0,
    MOVER_HUMAN: MAX_ELEVATION_DEG,
})

#: Which composed program object each regime plays, stated as the PHASE whose program it
#: is. Per-driver is MEASURE's interleaved object; summed is the position groups'
#: unannounced sweep.
_REGIME_PROGRAM_PHASE = {
    REGIME_PER_DRIVER: PHASE_MEASURE,
    REGIME_SUMMED: PHASE_CLOUD_VERIFY,
}


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #


def _validated_angle(angle_deg: object) -> int:
    """One bearing, checked and normalized -- the validator EVERY door shares
    (:class:`AngleStop`, :func:`pose_at_angle`, the three request constructors).

    Silent truncation never returns from here: ``0.4`` truncating to ``0``
    would make a pose just off axis an ON-AXIS capture with
    ``offset_cm=0.0``, routing around :func:`position_angle_deg`'s zero-sign
    guard. Accepts any :class:`numbers.Integral` except ``bool`` (excluded
    because ``bool`` *is* an ``Integral`` subclass, and ``True`` would
    otherwise sail through as ``+1 deg``); ``np.int64`` passes,
    ``np.float64`` does not. Returns a plain :class:`int`, converted AFTER
    the type check so it can never truncate.
    """
    if isinstance(angle_deg, bool) or not isinstance(angle_deg, numbers.Integral):
        raise CrossoverV2FlowError(
            "an angle is stated in WHOLE degrees -- no rounding, no coercion "
            f"-- got {angle_deg!r}"
        )
    degrees = int(angle_deg)
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
    (``""`` for the speaker as it stands); sits on the stop, not the walk,
    since a candidate cycle is adjacent stops at one pose.
    """

    angle_deg: int
    regime: str
    elevation_deg: int = 0
    candidate_id: str = ""

    def __post_init__(self) -> None:
        # Normalized back onto the field, so an ``np.int64`` a caller passed
        # never reaches a record or an equality check as a numpy scalar.
        object.__setattr__(self, "angle_deg", _validated_angle(self.angle_deg))
        object.__setattr__(
            self, "elevation_deg", _validated_angle(self.elevation_deg)
        )
        if self.regime not in REGIMES:
            raise CrossoverV2FlowError(
                f"stimulus regime must be one of {REGIMES}, got {self.regime!r}"
            )


@dataclass(frozen=True)
class AngleCaptureRequest:
    """What a caller is asking for: an ordered walk, and who moves the mic.

    Ordered and position-major: stops walk in order, so two regimes at the same angle
    are two ADJACENT stops (:func:`both_at`), never two walks. ``mover`` changes ONLY
    the advance policy, never the pose/prompt/program/record: a request stated in
    degrees reads back in degrees for whoever holds the microphone (see
    :func:`pose_at_angle`).

    ``delayed_role``/``delay_us`` are R-1's confirmation coordinate the DISPOSE step
    plays, walk-level like the polarity pair, never judged here. ``level_matched`` is a
    BOOLEAN: the trims belong to the speaker and resolve on-box when the host adopts
    this walk. ``program`` is PROVENANCE, not geometry -- the name of the
    :class:`~.measurement_programs.MeasurementProgram`, or ``""`` for a free-form walk;
    nothing parses it. ``polarity``/``inverted_role`` are walk-level because the
    reverse-null is ONE act at one place (``docs/REFACTOR-TUNING-2026-08.md`` §1).

    ``delayed_role`` and ``delay_us`` are R-1's other half — the confirmation
    coordinate the DISPOSE step plays. Walk-level for the same reason the
    polarity pair is, and carried the same way: never judged here.

    ``level_matched`` asks the measurement graph to carry the box's own
    per-driver level match, and it is a BOOLEAN on purpose: the trims belong
    to the speaker, not to the request, so they resolve on-box when the host
    adopts this walk. An operator who could state numbers here could measure
    one speaker through another's level match.

    ``program`` is PROVENANCE, not geometry: the name of the
    :class:`~.measurement_programs.MeasurementProgram` these stops came from
    (``"baseline/express"``, ``"spot"``), or ``""`` for a free-form walk
    nobody named. Nothing parses it -- the stops are the walk.

    ``polarity`` and ``inverted_role`` are walk-level rather than per-stop
    because the reverse-null is **one act at one place** -- design-axis-only,
    where the per-driver sweeps are per-position. They name what the session's
    design-axis MEASURE capture rides, not what happens at a stop -- which is
    also why they sit beside ``mover`` and not on :class:`AngleStop`.

    **Carried, never judged here.**
    :class:`~.crossover_v2.measure_spec.MeasureSpec` already refuses every bad
    combination of the two, including both one-sided forms, and a second copy
    of that rule is a copy that drifts. The host builds the spec when it adopts
    the walk, so a request stating one half refuses THE OPEN in the spec's own
    words -- see ADR-0006.
    """

    stops: tuple[AngleStop, ...]
    mover: str = MOVER_HUMAN
    polarity: str = POLARITY_NORMAL
    inverted_role: str = ""
    delayed_role: str = ""
    delay_us: float = 0.0
    level_matched: bool = False
    program: str = ""

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


def pose_at_angle(angle_deg: int, elevation_deg: int = 0) -> CloudPositionPrompt:
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
    offset_cm = _offset_cm_at(degrees)
    role = POSITION_ROLE_OFFAX if offset_cm >= WIDE_OFFSET_MIN_CM else POSITION_ROLE_ONAX
    geometric = CloudPositionPrompt(
        # Placeholder, immediately replaced: copy is derived from the geometry.
        headline="",
        detail="",
        offset_cm=offset_cm,
        role=role,
        lateral_sign=_sign_of(degrees),
        vertical_sign=_sign_of(elevation),
        vertical_offset_cm=_offset_cm_at(elevation),
    )
    return remote_position_prompt(geometric)


def _sign_of(degrees: int) -> int:
    return 0 if degrees == 0 else (1 if degrees > 0 else -1)


def _offset_cm_at(degrees: int) -> float:
    """The cm displacement one bearing names, in the mark's own plane. The tangent
    :func:`position_angle_deg`/:func:`position_elevation_deg` both invert, written once.
    """
    return 100.0 * MARK_DISTANCE_M * math.tan(math.radians(abs(degrees)))


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


def request_for_program(
    program: MeasurementProgram,
    *,
    candidates: tuple[str, ...] = (),
    mover: str = MOVER_HUMAN,
    polarity: str = POLARITY_NORMAL,
    inverted_role: str = "",
    delayed_role: str = "",
    delay_us: float = 0.0,
    level_matched: bool = False,
) -> AngleCaptureRequest:
    """The walk one named program asks for, in the table's own order.

    Per-driver at every pose (:data:`WALK_REGIME_UNSUPPORTED`). A pose's ``repeats``
    become that many ADJACENT identical stops, so the microphone moves once per DISTINCT
    pose. The graph flags pass through untouched -- a program states POSE geometry only.
    ``candidates`` expands POSE-MAJOR, CANDIDATE-MINOR (adjacent stops, one place);
    ``()`` measures the speaker as it stands. Reach is not re-checked:
    :class:`AngleCaptureRequest` already refuses a pose beyond the mover's envelope.
    """
    return AngleCaptureRequest(
        stops=tuple(
            AngleStop(
                pose.azimuth_deg,
                REGIME_PER_DRIVER,
                pose.elevation_deg,
                candidate,
            )
            for pose in program.poses
            for _ in range(pose.repeats)
            for candidate in (candidates or ("",))
        ),
        mover=mover,
        polarity=polarity,
        inverted_role=inverted_role,
        delayed_role=delayed_role,
        delay_us=delay_us,
        level_matched=level_matched,
        # ``spot`` carries caller geometry rather than a registry row, so its
        # size names nothing an operator chose.
        program=(
            program.program_id
            if program.program_id == "spot"
            else f"{program.program_id}/{program.size}"
        ),
    )


def walk_price(
    request: AngleCaptureRequest, *, plan_shape: V2PlanShape | None = None,
) -> dict[str, int]:
    """What this walk costs the person holding the microphone. ``ceiling_min`` prices the
    SESSION (base entries plus these stops), rounded UP to whole minutes. ``plan_shape``
    is ``None`` for a surface pricing a walk before any tier is chosen.
    """
    return {
        "mic_moves": len({(s.angle_deg, s.elevation_deg) for s in request.stops}),
        "captures": len(request.stops),
        "ceiling_min": math.ceil(
            wall_clock_ceiling_s(
                stage1_base_entries(plan_shape) + len(request.stops)
            ) / 60
        ),
    }


# --------------------------------------------------------------------------- #
# resolution -- request -> the shipped primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedStop:
    """One stop, resolved into what the shipped runner already consumes. ``index`` is the
    1-based capture index the relay drives (``index == accepted_count + 1``).
    ``program_phase`` names the phase whose composed program object this stop plays;
    :func:`program_for_stop` turns it into the object -- a program question, never a
    session-journey one (see the module docstring).
    """

    index: int
    angle_deg: int
    regime: str
    elevation_deg: int
    prompt: CloudPositionPrompt
    program_phase: str
    screen: Mapping[str, str]

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
        pose = pose_at_angle(stop.angle_deg, stop.elevation_deg)
        resolved.append(
            ResolvedStop(
                index=offset + 1,
                angle_deg=stop.angle_deg,
                regime=stop.regime,
                elevation_deg=stop.elevation_deg,
                prompt=pose,
                program_phase=_REGIME_PROGRAM_PHASE[stop.regime],
                screen=_screen_policy(request, pose),
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


#: A stop is not per-driver. A session lateral group plays MEASURE's
#: per-driver object at every pose, so a summed stop would measure wrong.
WALK_REGIME_UNSUPPORTED = "walk_regime_unsupported"

#: The walk's mover and the session's ADVANCE POLICY disagree (a countdown
#: with no hand moving, or a tap-wait from an arm with none to give). NOT a
#: comparison against the session's GATE.
WALK_MOVER_MISMATCH = "walk_mover_mismatch"

#: A stop is outside the stated mover's own reach on one AXIS
#: (:data:`MOVER_MAX_ANGLE_DEG`, :data:`MOVER_MAX_ELEVATION_DEG`). Decided by
#: :class:`AngleCaptureRequest` at STATEMENT time, not at a 600 s live hold.
WALK_OVER_MOVER_ENVELOPE = "walk_over_mover_envelope"

#: The composed session would need more relay blob indexes than exist.
WALK_OVER_RELAY_CAPACITY = "walk_over_relay_capacity"

#: The session already plans a lateral group. Raised by the CALLER; this
#: module does not read session flags.
WALK_LATERAL_GROUP_ALREADY_PLANNED = "walk_lateral_group_already_planned"

#: A banked stop no longer satisfies this module's own contract (a
#: hand-edited angle, an unknown regime or mover). The spool re-raises
#: :func:`_validated_angle`'s bare :class:`CrossoverV2FlowError` under this slug.
WALK_STOP_NO_LONGER_VALID = "walk_stop_no_longer_valid"

#: The walk's ``(polarity, inverted_role)`` pair is not one
#: :class:`~.crossover_v2.measure_spec.MeasureSpec` accepts, judged by
#: BUILDING that spec at adoption -- detail is the spec's own sentence.
WALK_POLARITY_NOT_ACCEPTED = "walk_polarity_not_accepted"

#: The walk's ``(delayed_role, delay_us)`` pair is not one ``MeasureSpec``
#: accepts. Own slug (not :data:`WALK_POLARITY_NOT_ACCEPTED`) so ``reason=``
#: names WHICH half of R-1 was refused.
WALK_DELAY_NOT_ACCEPTED = "walk_delay_not_accepted"

#: The walk asks to level-match driver branches and this box has no measured
#: evidence to level them BY (trims resolve on-box from banked base trim or
#: guided captures). Raised by the CALLER; this module reads no box state.
WALK_LEVEL_MATCH_NO_EVIDENCE = "walk_level_match_no_evidence"

#: A stop names a banked candidate this walk cannot PLAY. The per-driver
#: MEASURE graph omits crossover, linearization and applied delays by
#: contract, so only alignment axes can vary; a candidate carrying
#: linearization EQ is not measurable through this path at all.
WALK_CANDIDATE_NOT_MEASURABLE = "walk_candidate_not_measurable"

WALK_REFUSAL_REASONS = frozenset({
    WALK_REGIME_UNSUPPORTED,
    WALK_MOVER_MISMATCH,
    WALK_OVER_MOVER_ENVELOPE,
    WALK_OVER_RELAY_CAPACITY,
    WALK_LATERAL_GROUP_ALREADY_PLANNED,
    WALK_STOP_NO_LONGER_VALID,
    WALK_POLARITY_NOT_ACCEPTED,
    WALK_DELAY_NOT_ACCEPTED,
    WALK_LEVEL_MATCH_NO_EVIDENCE,
    WALK_CANDIDATE_NOT_MEASURABLE,
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


def candidate_measure_axes(candidate: Any) -> dict[str, Any]:
    """The :class:`MeasureSpec` axes a banked candidate implies, or a refusal. A stop plays the
    per-driver MEASURE graph, which omits crossover, linearization and delays, so only
    ALIGNMENT can vary; anything else refuses as :data:`WALK_CANDIDATE_NOT_MEASURABLE`.
    """
    from .crossover_alignment import POLARITY_INVERT
    from .crossover_declaration import preset_crossover_geometry

    fingerprint = str(getattr(candidate, "fingerprint", "") or "")
    if getattr(candidate, "linearization", None):
        raise LateralWalkRefused(
            WALK_CANDIDATE_NOT_MEASURABLE,
            f"banked candidate {fingerprint} carries linearization EQ, and a "
            "per-driver measurement graph plays no linearization",
        )
    minted = preset_crossover_geometry(getattr(candidate, "source_preset", None))
    if minted is None:
        raise LateralWalkRefused(
            WALK_CANDIDATE_NOT_MEASURABLE,
            f"banked candidate {fingerprint} declares no single readable "
            "crossover, so the branch its alignment flips cannot be named",
        )
    roles, _geometry = minted
    alignment = getattr(candidate, "alignment", None)
    inverted = getattr(alignment, "polarity", None) == POLARITY_INVERT
    # The candidate's convention is the region's UPPER driver relative to its
    # lower one, which is the branch named here.
    inverted_role = roles[1] if inverted else ""
    delay_us = float(getattr(alignment, "delay_us", None) or 0.0)
    # A zero delay names no branch. ``MeasuredCrossoverAlignment`` accepts the
    # pair ``(0.0, role)`` and :class:`MeasureSpec` refuses it, so an alignment
    # that delays nothing is stated here as one that names nothing rather than
    # reaching the spec as a half-stated pair.
    delayed_role = (
        str(getattr(alignment, "delay_role", None) or "") if delay_us else ""
    )
    for axis, role in (
        ("inverted_role", inverted_role), ("delayed_role", delayed_role),
    ):
        if role and role not in DRIVER_ROLES:
            raise LateralWalkRefused(
                WALK_CANDIDATE_NOT_MEASURABLE,
                f"banked candidate {fingerprint} names {role!r} as its "
                f"{axis}, and a measurement graph carries only "
                f"{', '.join(DRIVER_ROLES)}",
            )
    return {
        "polarity": POLARITY_INVERTED if inverted else POLARITY_NORMAL,
        "inverted_role": inverted_role,
        "delayed_role": delayed_role,
        "delay_us": delay_us,
    }


def session_lateral_walk(
    request: AngleCaptureRequest,
    *,
    externally_positioned: bool,
    base_entries: int,
    plans_cloud_group: bool,
) -> tuple[CloudPositionPrompt, ...]:
    """The poses a measurement session should walk for this request.

    ``externally_positioned`` is the session's own ADVANCE policy
    (``V2PlanShape.externally_positioned``, never ``positions_gated``);
    ``base_entries`` is how many captures the session takes that are NOT
    this walk; ``plans_cloud_group`` says whether it also walks a position
    cloud. Returns one pose per stop, in stop order, never a session phase.

    Raises :class:`LateralWalkRefused` with :data:`WALK_REGIME_UNSUPPORTED`,
    :data:`WALK_MOVER_MISMATCH`, or :data:`WALK_OVER_RELAY_CAPACITY` --
    properties of the PAIR (walk, session), so the spool's own document
    validation cannot make them. The capacity bound asks
    :func:`stage1_plan_max_attempts`, the same producer the emitted plan
    sets ``max_attempts`` from.
    """
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    off_regime = sorted({
        stop.regime for stop in request.stops if stop.regime != REGIME_PER_DRIVER
    })
    if off_regime:
        raise LateralWalkRefused(
            WALK_REGIME_UNSUPPORTED,
            f"a session walk plays the {REGIME_PER_DRIVER} program at every "
            f"pose, so it cannot take {', '.join(off_regime)} stops",
        )
    if request.externally_positioned != externally_positioned:
        raise LateralWalkRefused(
            WALK_MOVER_MISMATCH,
            f"the walk states mover={request.mover!r} "
            f"(externally_positioned={request.externally_positioned}) but this "
            f"session is externally_positioned={externally_positioned}",
        )
    entries = base_entries + len(request.stops)
    attempts = stage1_plan_max_attempts(
        entries, include_cloud_measure=plans_cloud_group,
    )
    if attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise LateralWalkRefused(
            WALK_OVER_RELAY_CAPACITY,
            f"{base_entries} session captures + {len(request.stops)} stops = "
            f"{entries} entries, needing {attempts} relay blob indexes over a "
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
