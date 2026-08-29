# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Capture a stated set of ANGLES, in a stated stimulus regime, by a stated mover.

The composition this module owns is
``{per-driver | summed} x {angles} x {arm | human-guided}``, and it exists
because two of those three axes were already clean while the third had no input
port at all:

* **The program axis was clean.**
  :func:`~jasper.active_speaker.crossover_v2.programs.program_for_phase`
  dispatches by object identity, and the per-driver program is one composed
  object -- ``build_measure_program``'s interleaved woofer/tweeter schedule, both
  drivers in ONE capture on channels 0 and 1, so they share an exact common time
  origin. This module selects that same object; it does not compose a second
  per-driver shape.
* **The mover axis was clean.** The flow states an advance policy
  (:attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.externally_positioned`
  -- a machine advances, so every entry auto-begins) separately from a pose
  statement
  (:attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.positions_gated`
  -- poses are stated as bearings and every begin is held until somebody reports
  the microphone in place), and ``_positioned_prompt`` restates a pose's copy as
  that angle. Both a driver and a person can hold the second without the first.
* **The angle axis was welded.** Degrees were DERIVED and never REQUESTED:
  :func:`position_angle_deg` reads a bearing off a pose's ``offset_cm``, and the
  only pose table that reaches the per-driver program off the design axis is
  ``LATERAL_POSE_PROMPTS`` -- a fixed six-row tuple derived from two hard-coded
  cm offsets, behind an import-time length guard. There was no way to ask for
  45 deg, and no way to ask for a summed capture at an angle at all.

**So the one new primitive here is the INVERSE of the existing derivation**
(:func:`pose_at_angle`), and everything else is composition over shipped parts.
Degrees become an input that round-trips exactly through the cm-primary
representation the evidence sidecar, the ``wide`` rule and the attribution stage
already read, so an angle-requested capture banks in the same shape as a
hand-walked one and stays comparable with it.

**What this is NOT, stated because the distinction is load-bearing.**  These
poses are FORWARD-MODEL INPUT and never a pose-ratio statistic.  The two are
different consumers with different validity arguments:

* The **lateral-walk STATISTIC** was paused on 2026-08-18
  ([PR #2717](https://github.com/jaspercurry/JTS/pull/2717)) because the
  statistic itself was invalidated -- a max-over-poses reduction that ranked
  below its own repeat noise, and whose argmax pose is frequently the zero-offset
  closing repeat.  It was retired with the corner hunt it fed; a redesign would
  have to clear that four-point bar
  ([#2711](https://github.com/jaspercurry/JTS/issues/2711)).
* **Per-driver responses at angles** -- the P2 crossover search's
  complex-summation model, which predicts a candidate's summed response from the
  per-driver complex responses -- need no pose-ratio statistic at all.  The model
  consumes each angle's complex transfer function directly.  The pose-ratio
  cancellation that invalidated the statistic is a property of the REDUCTION,
  not of the captures, and PR #2717's own evidence says so in as many words:
  "The poses are clean; it is the max-over-poses reduction into one scalar that
  cannot separate candidates."

This module therefore **never constructs**
:data:`~jasper.active_speaker.crossover_v2.journey.PHASE_LATERAL`: it returns
poses and refusals, and the session host is what tags indexes with a phase.  A
taken walk runs as the session's lateral group and so does reach
``_close_lateral_walk``, whose close publishes nothing.

Dependency direction: a sibling of :mod:`jasper.active_speaker.crossover_v2_flow`
that imports FROM it, the same direction
:mod:`jasper.active_speaker.crossover_envelope_v2` already takes.  It is
deliberately NOT under ``jasper/active_speaker/crossover_v2/``, whose modules
forbid importing the flow.
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from jasper.audio_measurement.program import ExcitationProgram

from .crossover_v2.contracts import POLARITY_NORMAL
from .crossover_v2.journey import PHASE_CLOUD_VERIFY, PHASE_MEASURE
from .crossover_v2.programs import program_for_phase
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
)

__all__ = [
    "REGIME_PER_DRIVER",
    "REGIME_SUMMED",
    "REGIMES",
    "MOVER_ARM",
    "MOVER_HUMAN",
    "MOVERS",
    "MAX_ANGLE_DEG",
    "ARM_ENVELOPE_DEG",
    "MOVER_MAX_ANGLE_DEG",
    "AngleStop",
    "AngleCaptureRequest",
    "ResolvedStop",
    "pose_at_angle",
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
    "WALK_POLARITY_NOT_ACCEPTED",
    "WALK_REFUSAL_REASONS",
    "LateralWalkRefused",
    "session_lateral_walk",
]


# --------------------------------------------------------------------------- #
# the two vocabularies a request is stated in
# --------------------------------------------------------------------------- #

#: Each driver swept alone, non-overlapping inside ONE capture (regime D of the
#: ratified Measurement Program v2 schedule). Yields that angle's per-driver
#: complex transfer functions, which is what the P2 forward model consumes.
REGIME_PER_DRIVER = "per_driver"

#: One sweep through the graph as it stands (regime S). Yields the system
#: response at that angle -- the before/after evidence.
REGIME_SUMMED = "summed"

REGIMES = (REGIME_PER_DRIVER, REGIME_SUMMED)

#: An external driver turns the microphone and reports the angle reached. It is
#: the one mover that auto-advances -- there is no hand to tap -- which is what
#: :attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.externally_positioned`
#: names; its holds are released by the driver's own report.
MOVER_ARM = "arm"

#: A person moves the microphone -- the owner's string-and-protractor technique
#: -- and taps when they are there. The tap IS the settle signal, exactly as the
#: shipped hand-walked tiers use it.
#:
#: They read the SAME angle-stated prompt the arm is driven to
#: (:func:`pose_at_angle`); only the advance policy differs. That combination --
#: degrees plus a tap -- is the one the two shape facts exist to express, and
#: the reason this seam separates the two questions.
MOVER_HUMAN = "human"

MOVERS = (MOVER_ARM, MOVER_HUMAN)

#: How far off the design axis a stop may be asked for. The bound is the
#: geometry's own: :func:`pose_at_angle` is a tangent, so 90 deg is a pose an
#: infinite distance away and everything near it turns a rounding error into
#: metres of offset. 80 deg already puts the microphone 5.7 m off a 1 m mark,
#: which is past any room this measures in -- so this refuses an unmeasurable
#: request rather than silently banking an absurd ``offset_cm``.
#:
#: This is the GEOMETRY's ceiling, which every angle passes. A given mover may
#: reach less far; that second, narrower bound is :data:`MOVER_MAX_ANGLE_DEG`.
MAX_ANGLE_DEG = 80

#: How far the lab positioner can actually travel. The turntable adapter
#: (``experiments/usb-turntable/jts_turntable.py``) refuses a ``position``
#: outside +/-45 deg, so a stop past it is a request the arm can never serve.
#: Restated here rather than imported because ``experiments/`` is not a package
#: this one may depend on; ``tests/test_arm_walk.py`` pins the two together.
ARM_ENVELOPE_DEG = 45

#: The per-mover bound :class:`AngleCaptureRequest` enforces -- the only place
#: that knows BOTH the mover and its stops. A person can stand anywhere the
#: geometry stays honest (:data:`MAX_ANGLE_DEG`); the arm is bounded by its own
#: travel. Checked when the walk is STATED rather than when a
#: session takes it, because the alternative is a session that stalls: the
#: position gate publishes a target this mover cannot reach and then spends its
#: whole ``REMOTE_POSITION_HOLD_BUDGET_S`` (600 s) per stop waiting for a report
#: that cannot come.
MOVER_MAX_ANGLE_DEG: Mapping[str, int] = MappingProxyType({
    MOVER_ARM: ARM_ENVELOPE_DEG,
    MOVER_HUMAN: MAX_ANGLE_DEG,
})

#: Which composed program object each regime plays -- stated as the PHASE whose
#: program it is, so :func:`program_for_stop` can delegate to the shipped
#: identity dispatcher instead of minting a second answer to "what does this
#: play". Per-driver is MEASURE's interleaved object; summed is the position
#: groups' unannounced sweep (the same object and the same min-cap clamp as
#: VERIFY's, minus the courtesy prelude no position carries).
_REGIME_PROGRAM_PHASE = {
    REGIME_PER_DRIVER: PHASE_MEASURE,
    REGIME_SUMMED: PHASE_CLOUD_VERIFY,
}


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #


def _validated_angle(angle_deg: object) -> int:
    """One bearing, checked and normalized -- the validator EVERY door shares.

    :class:`AngleStop`, :func:`pose_at_angle` and the three request
    constructors are all reachable directly, so the bounds live here rather
    than on whichever one a caller happened to use first.

    **Silent truncation never returns from this function, and that is the point
    rather than a style preference.** The constructors used to coerce with
    ``int(a)`` before this ran, which made the whole-degree contract bind two of
    the three doors. The sharp row is ``0.4``: it truncates to ``0``, and a
    request for a pose just off the axis becomes an ON-AXIS capture with
    ``offset_cm=0.0`` -- routing straight around :func:`position_angle_deg`'s
    zero-sign guard, whose whole job is to refuse a pose that "would record an
    offset the microphone never had". ``7.9 -> 7`` and ``'45' -> 45`` are the
    same defect wearing quieter clothes. A caller who means 7 degrees can say
    ``7``; nothing here guesses.

    **Accepts any integer type except ``bool``.** Arm tooling and
    numpy-derived schedules naturally hand over ``np.int64``, and refusing that
    would push every such caller into exactly the ``int()`` coercion this
    function exists to remove. So the test is :class:`numbers.Integral`, which
    ``np.int64`` satisfies and ``np.float64`` does not. ``bool`` is excluded
    explicitly because it *is* an ``Integral`` subclass in Python: ``True``
    would otherwise sail through as ``+1 deg``, which is a real bearing and an
    obvious caller error at the same time.

    Returns a plain :class:`int` so nothing downstream carries a numpy scalar
    into a record, a journal line or an equality check. The conversion happens
    AFTER the type check, so it can never truncate.
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

    ``angle_deg`` is a signed WHOLE degree -- negative LEFT of the design axis,
    positive RIGHT, the sign convention ``_LATERAL_SIGNS`` states once for the
    whole flow. Whole degrees because that is the resolution the number is
    honest at (:func:`position_angle_deg`'s own reasoning: the mark is placed
    "about" 1 m out, and a tenth of a degree claims a precision the placement
    never had).
    """

    angle_deg: int
    regime: str

    def __post_init__(self) -> None:
        # Normalized back onto the field, so an ``np.int64`` a caller passed
        # never reaches a record or an equality check as a numpy scalar.
        object.__setattr__(self, "angle_deg", _validated_angle(self.angle_deg))
        if self.regime not in REGIMES:
            raise CrossoverV2FlowError(
                f"stimulus regime must be one of {REGIMES}, got {self.regime!r}"
            )


@dataclass(frozen=True)
class AngleCaptureRequest:
    """What a caller is asking for: an ordered walk, and who moves the mic.

    **Ordered, and position-major.** The stops are walked in the order given and
    every stop is one microphone position, so two regimes at the same angle are
    two ADJACENT stops rather than two walks -- the microphone moves once per
    angle, which is the ratified schedule's own property and the reason
    :func:`both_at` emits them paired.

    ``mover`` is the one axis that is not about the measurement: the same stops
    measured by an arm and by a person are the same measurement, which is why it
    changes **only the advance policy** -- never the pose, the prompt, the
    program, or the banked record. That is the rule ``_positioned_prompt``
    already states for the shipped walk ("the remote tier is a different
    OPERATOR, not a different measurement"), taken one step further: the shipped
    walk still changes the COPY with the operator, and this does not, because a
    request stated in degrees reads back in degrees for whoever is holding the
    microphone (see :func:`pose_at_angle`).

    ``polarity`` and ``inverted_role`` are walk-level rather than per-stop
    because the reverse-null is **one act at one place**
    (``docs/REFACTOR-TUNING-2026-08.md`` §1: design-axis-only, where the
    per-driver sweeps are per-position). They name what the session's
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "stops", tuple(self.stops))
        if not self.stops:
            raise CrossoverV2FlowError("an angle capture request needs at least one stop")
        if self.mover not in MOVERS:
            raise CrossoverV2FlowError(
                f"mover must be one of {MOVERS}, got {self.mover!r}"
            )
        bound = MOVER_MAX_ANGLE_DEG[self.mover]
        outside = tuple(
            stop.angle_deg for stop in self.stops if abs(stop.angle_deg) > bound
        )
        if outside:
            # A LateralWalkRefused rather than a bare CrossoverV2FlowError so the
            # refusal carries the walk vocabulary's own machine slug: this IS a
            # walk refusal, it is simply the one decidable from the request alone
            # (mover plus angles), with no session needed to judge it.
            raise LateralWalkRefused(
                WALK_OVER_MOVER_ENVELOPE,
                f"mover={self.mover!r} travels +/-{bound} deg, so it cannot "
                "reach "
                + ", ".join(f"{deg:+d}" for deg in outside)
                + " deg",
            )

    @property
    def externally_positioned(self) -> bool:
        """Whether an external driver moves the microphone between stops.

        The same question :attr:`V2PlanShape.externally_positioned` answers for
        a tiered session, asked of a mover instead of a tier -- the ADVANCE
        axis, and only that. Whether a session HOLDS each begin is a separate
        fact (:attr:`V2PlanShape.positions_gated`) that a person can satisfy
        too, which is why :func:`session_lateral_walk` compares this one and
        not that one.
        """
        return self.mover == MOVER_ARM


# --------------------------------------------------------------------------- #
# angle -> pose: the one new primitive
# --------------------------------------------------------------------------- #


def pose_at_angle(angle_deg: int) -> CloudPositionPrompt:
    """The pose at a stated bearing -- the exact inverse of
    :func:`position_angle_deg`.

    **Why this returns a cm-primary pose rather than carrying the angle
    onward.** ``offset_cm`` is the load-bearing datum of every shipped consumer:
    :attr:`CloudPositionPrompt.wide` derives the ~30 cm LF-edge class from it,
    the evidence sidecar records it as the durable statement of WHERE a curve was
    measured, and the attribution stage compares positions by it. Minting an
    angle-only pose would have created a second, parallel statement of the same
    geometry -- and the flow's own reason for deriving angles instead of
    tabulating them ("an edit to the offsets must move the angles with them
    instead of leaving a second, silently-stale table of numbers") applies with
    equal force in this direction. So the request is stated in degrees, the pose
    is stored in centimetres, and ``position_angle_deg(pose_at_angle(d)) == d``
    for every whole degree this accepts. That round trip is a test, not a claim.

    ``role`` is DERIVED from :data:`WIDE_OFFSET_MIN_CM`, not chosen: a stop past
    the wide class answers the off-axis question and one inside it answers the
    on-axis one, which reproduces the shipped table's own assignment exactly
    (its 12 and 25 cm rows are ``onax``, its 40 and 60 cm rows are ``offax``)
    without a second table to drift from it. A vertical
    (:data:`POSITION_ROLE_XOVR`) pose is unreachable here by construction --
    this seam states a horizontal bearing and elevation is the ratified
    deferred axis -- which is also what keeps :func:`position_angle_deg` from
    ever being handed a row it must refuse.

    **The copy is stated as the ANGLE for BOTH movers.** A request stated in
    degrees reads back in degrees whoever is holding the microphone, so this
    seam asks the angle question of the REQUEST and the advance question of the
    MOVER. The flow separates the same two questions on its own side --
    ``_positioned_prompt`` reads
    :attr:`~jasper.active_speaker.crossover_v2_flow.V2PlanShape.positions_gated`
    while ``_entry_advance`` reads ``externally_positioned`` -- so
    ``(centimetres, tap)``, ``(degrees, gate)`` and the ratified household
    technique's ``(degrees, tap)``, a string swung to a protractor angle, are
    all sayable.

    It reuses the shipped :func:`remote_position_prompt` to say it rather than
    writing a second angle sentence: that function already restates a pose as
    its bearing and leaves ``offset_cm`` and ``role`` untouched, which is
    exactly the "same pose, different vocabulary" contract wanted here. Its copy
    is mover-neutral in fact -- "keep it 1 m from the speaker and pointed at it"
    is what a taut string does by construction and what an arm does by radius.
    """
    degrees = _validated_angle(angle_deg)
    offset_cm = 100.0 * MARK_DISTANCE_M * math.tan(math.radians(abs(degrees)))
    sign = 0 if degrees == 0 else (1 if degrees > 0 else -1)
    role = POSITION_ROLE_OFFAX if offset_cm >= WIDE_OFFSET_MIN_CM else POSITION_ROLE_ONAX
    geometric = CloudPositionPrompt(
        # A placeholder sentence, immediately replaced: the pose's identity is
        # its geometry, and the copy below is derived from that geometry rather
        # than written beside it.
        headline="",
        detail="",
        offset_cm=offset_cm,
        role=role,
        lateral_sign=sign,
    )
    return remote_position_prompt(geometric)


# --------------------------------------------------------------------------- #
# the three constructors -- "per-angle, per-driver, both, whatever we want"
# --------------------------------------------------------------------------- #


def per_driver_at(
    angles_deg: Sequence[int], *, mover: str = MOVER_HUMAN,
) -> AngleCaptureRequest:
    """Per-driver captures at each angle -- the P2 forward model's input.

    Angles are passed through to :class:`AngleStop` UNCOERCED, so this door
    enforces the same whole-degree contract as the other two -- see
    :func:`_validated_angle` for why an ``int()`` here was a hole rather than a
    convenience.
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
    """Both regimes at each angle, PAIRED so the microphone moves once per angle.

    Per-driver first at each stop, then summed from the same position. The order
    is the measurement's, not a preference: the two are only comparable at an
    angle if nothing moved between them, and the pairing is what makes "the
    microphone moves once per stop" true of a two-regime walk.
    """
    stops: list[AngleStop] = []
    for angle in angles_deg:
        stops.append(AngleStop(angle, REGIME_PER_DRIVER))
        stops.append(AngleStop(angle, REGIME_SUMMED))
    return AngleCaptureRequest(stops=tuple(stops), mover=mover)


# --------------------------------------------------------------------------- #
# resolution -- request -> the shipped primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedStop:
    """One stop, resolved into what the shipped runner already consumes.

    ``index`` is the 1-based capture index the relay drives (``index ==
    accepted_count + 1``), so this tuple IS the running order and the same index
    space :func:`index_phase_map` and the plan entries are built in.

    ``program_phase`` names the phase whose composed program object this stop
    plays; :func:`program_for_stop` turns it into the object. It is deliberately
    a program question and not a session-journey one -- see the module docstring
    on why no stop here is ever tagged ``PHASE_LATERAL``.
    """

    index: int
    angle_deg: int
    regime: str
    prompt: CloudPositionPrompt
    program_phase: str
    screen: Mapping[str, str]

    def __post_init__(self) -> None:
        # Frozen means frozen: the screen bag is handed out behind a read-only
        # view, the same way ``SessionExcitation`` guards ``caps_dbfs``. It
        # still compares equal to a plain dict, so callers and tests read it
        # exactly as before.
        object.__setattr__(self, "screen", MappingProxyType(dict(self.screen)))


def _screen_policy(request: AngleCaptureRequest, prompt: CloudPositionPrompt) -> dict[str, str]:
    """One stop's advance policy and, for an arm, its target position.

    Mirrors ``_entry_advance`` / ``_entry_policy`` for a request-shaped walk:
    a hand-guided stop is a tap (the person's own settle signal), an arm-driven
    one auto-begins behind the cancelable countdown AND declares the angle the
    position gate must wait for. The two arm behaviours are emitted together
    because a countdown without the gate fires into an arm still in motion.

    **This is the REQUEST's own preview, which is why a hand-guided stop
    declares no target here.** Whether a walk's begins are HELD is the
    SESSION's fact, not the request's -- a person's walk is gated when the
    session hosting it is (:attr:`V2PlanShape.positions_gated`), and that
    session builds its own entries through ``_entry_policy`` off its own shape.
    Guessing a target from the mover alone would be a second answer to a
    question this seam cannot see. The one consumer of this bag is
    ``jasper-angle-capture plan``'s dry run.

    The angle is re-read off the POSE through the shipped
    :func:`position_angle_deg` rather than copied from the request, so the
    number the gate acts on is the number the banked pose carries. A copy taken
    from the request would be a second source for one fact, and the round trip
    is exactly what would hide a defect in it.
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
    """The whole request, resolved into indexed stops in running order.

    This is the seam: everything above is a statement of intent and everything
    below consumes shipped machinery. Each stop resolves to the pose its angle
    names, the program object its regime names, and the advance policy its mover
    names -- three independent axes composed once, here, so no caller has to
    know how any of the three is implemented.
    """
    resolved: list[ResolvedStop] = []
    for offset, stop in enumerate(request.stops):
        pose = pose_at_angle(stop.angle_deg)
        resolved.append(
            ResolvedStop(
                index=offset + 1,
                angle_deg=stop.angle_deg,
                regime=stop.regime,
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
    """The composed program this stop plays -- BY IDENTITY, through the shipped
    dispatcher.

    Delegates to
    :func:`~jasper.active_speaker.crossover_v2.programs.program_for_phase` rather
    than branching on the regime here, so a per-driver stop is handed the very
    same MEASURE object the design-axis anchor played. That identity is the whole
    point: a pose measured at a different level or with a different sweep would
    make every cross-angle comparison uninterpretable, which is the same argument
    ``program_for_phase`` already records for the lateral poses it serves.

    Requesting a per-driver stop before the CHECK gain solve has produced a
    MEASURE program raises ``NoProgramForPhaseError`` from the dispatcher --
    unchanged, and deliberately not caught here: composing something at a guessed
    level is exactly what that error exists to prevent.
    """
    return program_for_phase(
        stop.program_phase,
        check=check,
        measure=measure,
        verify=verify,
        cloud=cloud,
    )


def index_phase_map(request: AngleCaptureRequest) -> dict[int, str]:
    """Capture index -> the phase whose program runs there.

    The same ``index -> phase`` shape ``build_v2_cloud_index_phase_map`` returns,
    so the shipped consumers that read one (notably
    :func:`announced_capture_indexes`) work over an angle walk unchanged.
    """
    return {stop.index: stop.program_phase for stop in resolve_request(request)}


#: A stop is not per-driver. A session lateral group plays MEASURE's per-driver
#: object at every pose, so a summed stop would be measured as something else.
WALK_REGIME_UNSUPPORTED = "walk_regime_unsupported"

#: The walk's mover and the session's ADVANCE POLICY disagree. Either way the
#: session stalls: a countdown firing into a microphone no hand is moving, or a
#: session waiting for a tap from an arm that has none to give. Deliberately
#: NOT a comparison against the session's GATE: a gated session's holds are
#: released by whichever mover it was opened for, so a person's walk inside a
#: hand-released session is exactly the combination this vocabulary must let
#: through.
WALK_MOVER_MISMATCH = "walk_mover_mismatch"

#: A stop is outside the stated mover's own reach (:data:`MOVER_MAX_ANGLE_DEG`).
#: Decided by :class:`AngleCaptureRequest` itself, so a walk the arm cannot serve
#: is refused where it is STATED -- at ``jasper-angle-capture plan``/``stage`` --
#: rather than stalling a live session one 600 s hold at a time.
WALK_OVER_MOVER_ENVELOPE = "walk_over_mover_envelope"

#: The composed session would need more relay blob indexes than exist.
WALK_OVER_RELAY_CAPACITY = "walk_over_relay_capacity"

#: The session already plans a lateral group. Declared here so the vocabulary
#: has one home; raised by the CALLER, since this module does not read session
#: flags (see the module docstring).
WALK_LATERAL_GROUP_ALREADY_PLANNED = "walk_lateral_group_already_planned"

#: A banked stop no longer satisfies this module's own contract -- a hand-edited
#: angle, an unknown regime or mover. The spool re-raises the bare
#: :class:`CrossoverV2FlowError` for these rather than re-wrapping, because
#: :func:`_validated_angle`'s sentence beats a second vocabulary; the take gives
#: that exception this slug and keeps the sentence as the detail.
WALK_STOP_NO_LONGER_VALID = "walk_stop_no_longer_valid"

#: The walk's ``(polarity, inverted_role)`` pair is not one
#: :class:`~.crossover_v2.measure_spec.MeasureSpec` accepts. Judged by BUILDING
#: that spec at adoption, never by a copy of its rule here -- so the detail is
#: the spec's own sentence, the same way :data:`WALK_STOP_NO_LONGER_VALID`
#: keeps ``_validated_angle``'s.
WALK_POLARITY_NOT_ACCEPTED = "walk_polarity_not_accepted"

WALK_REFUSAL_REASONS = frozenset({
    WALK_REGIME_UNSUPPORTED,
    WALK_MOVER_MISMATCH,
    WALK_OVER_MOVER_ENVELOPE,
    WALK_OVER_RELAY_CAPACITY,
    WALK_LATERAL_GROUP_ALREADY_PLANNED,
    WALK_STOP_NO_LONGER_VALID,
    WALK_POLARITY_NOT_ACCEPTED,
})


class LateralWalkRefused(CrossoverV2FlowError):
    """A walk may not run -- either as STATED, or in THIS session.

    Most of its reasons are properties of the pair (walk, session) and can only
    be judged by :func:`session_lateral_walk`. One,
    :data:`WALK_OVER_MOVER_ENVELOPE`, is a property of the request alone and is
    therefore raised by :class:`AngleCaptureRequest` at statement time -- same
    vocabulary, decided as early as it can be.

    ``reason`` is from :data:`WALK_REFUSAL_REASONS`; ``detail`` is the sentence
    a person reads. Same shape as ``angle_capture_spool.AngleRequestRefused``,
    which answers the earlier question ("may this document be read"); separate
    because that module imports this one.
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
    plans_cloud_group: bool,
) -> tuple[CloudPositionPrompt, ...]:
    """The poses a measurement session should walk for this request.

    Takes: the staged ``request``; ``externally_positioned``, the session's own
    ADVANCE policy (``V2PlanShape.externally_positioned``, never its
    ``positions_gated`` — see :data:`WALK_MOVER_MISMATCH` for why a gated
    hand-walked session answers ``False`` here and takes a person's walk);
    ``base_entries``, how many captures that session takes that are NOT this
    walk; ``plans_cloud_group``, whether it also walks a position cloud.

    Returns: one pose per stop, in stop order, in the ``CloudPositionPrompt``
    vocabulary every shipped prompted walk uses. Never a session phase -- the
    caller tags indexes.

    Raises :class:`LateralWalkRefused` with :data:`WALK_REGIME_UNSUPPORTED`,
    :data:`WALK_MOVER_MISMATCH`, or :data:`WALK_OVER_RELAY_CAPACITY`. All three
    are properties of the PAIR (this walk, this session), which is why the
    spool's own document validation cannot make them.

    The capacity bound asks :func:`stage1_plan_max_attempts`, the same producer
    the emitted plan sets ``max_attempts`` from. A second copy of that
    arithmetic is a gate that refuses walks the relay would have taken, which is
    what the first one did.
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
    """Which stops of this walk play the courtesy prelude.

    Delegates to the shipped :func:`announced_capture_indexes` over this walk's
    own map, so "what will the household hear" keeps ONE owner
    (``courtesy_prelude_for_phase``) rather than gaining a second answer here.

    **Today this is empty for every request, and that is correct rather than an
    oversight -- but it is a boundary a standalone runner must respect, and it
    fails loudly rather than quietly.** The prelude announces a SESSION, not a
    capture, and neither regime's program phase is a session opener: per-driver
    plays MEASURE's object and summed plays the position groups' deliberately
    unannounced sweep. An angle walk run INSIDE an already-announced session
    therefore announces nothing, which is the shipped behaviour for every capture
    after the first.

    A walk run as its OWN session still owes the household an opening warning,
    and the consent builder enforces that: ``_courtesy_beeps_step``
    (:mod:`jasper.capture_relay.spec`) refuses an empty ``announced_captures``
    outright -- "must be a non-empty subset of this plan's 1..N captures". So a
    standalone runner cannot ship a silent opener by accident; it has to open on
    an announced capture the way stage 1 does, on CHECK. Minting a prelude
    policy here instead would give the question a second owner, which is exactly
    what ``courtesy_prelude_for_phase`` exists to prevent.
    """
    return announced_capture_indexes(index_phase_map(request))
