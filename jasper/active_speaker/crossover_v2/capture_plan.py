# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""The walk a session will do, decided before anything plays: where the
microphone goes, in what order, with what words on the screen, and how many
attempts each pose is allowed.

It decides; it does not act — no I/O, no session state, no fader, no graph. Mover-agnostic
(MS-17): positions are degrees and centimetres, and nothing here knows whether a
human or an arm moves the microphone.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import groupby
from typing import Any, Sequence

from jasper.audio_measurement.measurement_geometry import METERS_PER_INCH
from jasper.playback_state.capture_protocol import CapturePlan, CapturePlanEntry, MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.platform.env_load import bounded_env_float
from jasper.platform.speaker_layout import measurement_target_name
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_WALL_CLOCK_CEILING_S,
    MAX_WALL_CLOCK_CEILING_S,
)

from ..measurement_programs import (
    POSE_KIND_BEARING, POSE_KIND_BEHIND, POSE_KIND_CLOSE, POSE_KIND_SEAT, Pose,
)
from ..round_copy import millimetres
from .admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from .contracts import (
    POSITION_AXIS_HORIZONTAL,
    CrossoverV2FlowError,
)
from .measure_spec import MeasureSpec
from .spatial import (
    MARK_DISTANCE_M,
    POSITION_ROLE_ONAX,
    PositionGeometry,
)
from .sweep_spec import build_crossover_sweep_spec
from .refusal_copy import CrossoverV2Refused


def announce_run(specs: Sequence[MeasureSpec]) -> tuple[MeasureSpec, ...]:
    """A run announces itself once: its first take carries the courtesy prelude,
    after the level probe that may open it (#1677, ADR-0417)."""
    return tuple(replace(spec, courtesy_prelude=index == 0) for index, spec in enumerate(specs))


def build_inline_session_spec(
    captures: Sequence[tuple[CloudPositionPrompt, str]], *,
    acknowledgement_binding: str, **spec_kwargs: Any,
) -> Any:
    prompts = [prompt for prompt, _ in captures]
    batches = pose_batch_screens(list(range(1, len(captures) + 1)), prompts,
                                 [candidate_id for _, candidate_id in captures])
    entries = tuple(
        CapturePlanEntry(index=index - 1,
                         screen={"title": prompt.headline, "body": prompt.detail,
                                 **position_screen_keys(prompt), **batches.get(index, {})})
        for index, prompt in enumerate(prompts, 1))
    placements = sum(1 for _ in groupby(prompt.pose.place for prompt in prompts))
    attempts = len(entries) + placements * MAX_EXTRA_ATTEMPTS_PER_POSITION
    if attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CrossoverV2Refused("The prepared plan exceeds capture capacity", code="walk_over_capture_capacity")
    plan = CapturePlan(capture_target=len(entries), max_attempts=attempts, schema_version=2, entries=entries)
    return build_crossover_sweep_spec(
        driver_label="crossover", driver_role="summed", acknowledgement_binding=acknowledgement_binding,
        capture_plan=plan, **spec_kwargs,
    )


CAPTURE_PLAN_TARGET = 3


# --------------------------------------------------------------------------- #
# position prompts
# --------------------------------------------------------------------------- #


# docs/historical/linearization-campaign-2026-07.md fundamental 1: a spread of
# ~30 cm or more supports the LF edge. At or past this distance a move is
# "wide" (:attr:`CloudPositionPrompt.wide`).
WIDE_OFFSET_MIN_CM = 30.0


def format_position_distance(offset_cm: float) -> str:
    """One prompted distance, in inches with the metric value beside it.

    Both units ride every prompt, and centimetres rather than metres because
    every prompted move is between 0.1 m and 0.6 m (#1805).
    """
    inches = round(float(offset_cm) / (METERS_PER_INCH * 100.0))
    return f"{inches:g} in ({float(offset_cm):g} cm)"


@dataclass(frozen=True)
class CloudPositionPrompt:
    """One prompted mic move.

    ``detail`` may be empty; ``text`` re-joins headline and detail for the
    durable evidence sidecar. ``offset_cm`` is the pose's SIDEWAYS displacement
    in the mark's own plane — the perpendicular leg of a right triangle whose
    other leg is :data:`MARK_DISTANCE_M` — so it says where a pose is relative
    to the design axis and nothing about how the microphone got there. ``wide``
    is computed from it, so the ~30 cm-class guarantee cannot be voided by
    editing copy alone. ``role`` names the question the position answers
    (:data:`POSITION_ROLES`). ``pose`` is where the move puts the microphone
    (ADR-0366 §1), stated by every builder, never defaulted; the bearings
    :func:`position_angle_deg` and :func:`position_elevation_deg` state are its own.
    """

    headline: str
    detail: str = ""
    offset_cm: float = 0.0
    role: str = POSITION_ROLE_ONAX
    #: Which side of the design axis a LATERAL row sits on: ``-1`` LEFT,
    #: ``+1`` RIGHT, ``0`` for an at-mark or vertical row.
    lateral_sign: int = 0
    #: Which side of mark HEIGHT a row sits on: ``-1`` BELOW, ``+1`` ABOVE,
    #: ``0`` for a row that asks for no raise or lower.
    vertical_sign: int = 0
    #: How far above (or below) mark height the row asks for, in centimetres.
    #: Separate from ``offset_cm`` because a compound row moves two different
    #: distances at once (the second geometry-retake rung goes 75 cm sideways
    #: AND 30 cm up). ``0`` means the row asks for no raise.
    vertical_offset_cm: float = 0.0
    pose: Pose = field(kw_only=True)

    @property
    def wide(self) -> bool:
        """Whether this move carries the plan's ~30 cm-class LF-edge offset."""
        return float(self.offset_cm) >= WIDE_OFFSET_MIN_CM

    @property
    def at_mark(self) -> bool:
        """Whether the pose asks for no move at all — on EITHER axis."""
        return (
            self.pose.kind == POSE_KIND_BEARING
            and float(self.offset_cm) == 0.0
            and float(self.vertical_offset_cm) == 0.0
        )

    @property
    def mark_distance_m(self) -> float:
        """The reference length this pose's bearings are derived against."""
        return MARK_DISTANCE_M if self.pose.distance_m is None else self.pose.distance_m

    @property
    def text(self) -> str:
        """Headline + detail as one string — the evidence sidecar's ``prompt``.

        The sidecar is the only durable statement of where a curve was
        measured, so it records the complete instruction.
        """
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


# Elevation words, from a sign: negative is BELOW mark height, positive is ABOVE.
_VERTICAL_WORDS = {-1: "BELOW", 1: "ABOVE"}


def elevation_clause(elevation_deg: int) -> str:
    """One pose's ELEVATION, worded — empty string at mark height.

    The only generator of these words: the sentence a person reads and the
    button they press to attest it are composed from this, so the two cannot
    name one pose differently.
    """
    if not elevation_deg:
        return ""
    word = _VERTICAL_WORDS[1 if elevation_deg > 0 else -1]
    return f"{abs(elevation_deg)}° {word} mark height"


def position_angle_deg(prompt: CloudPositionPrompt) -> int:
    """The signed horizontal bearing of one lateral pose, in WHOLE degrees: its
    pose's, so ``-7`` is 7° LEFT of the design axis. Whole degrees because the
    offsets are tape-measure distances to a mark placed "about" 1 m out.

    #2932 is open: a bearing is a TANGENT construction, which puts the capsule at
    ``mark / cos(θ)`` rather than a constant radius — treat the bearing as sound
    and the equidistance claim as unverified.
    """
    if float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0:
        # An off-axis pose that declared no side would multiply out to 0° —
        # "already on the design axis" — and bank an offset the microphone
        # never had.
        raise CrossoverV2FlowError(
            f"a lateral position {float(prompt.offset_cm):g} cm off the mark "
            "declares no side, so it has no signed bearing — set lateral_sign "
            "rather than letting it read as 0°"
        )
    return prompt.pose.azimuth_deg


def position_elevation_deg(prompt: CloudPositionPrompt) -> int:
    """The signed ELEVATION of one pose above mark height, in WHOLE degrees: its
    pose's. Refuses nothing: a row asking for no raise signs ``0``, which is true
    of it — an unstated elevation has an honest zero where an unstated bearing
    does not.
    """
    return prompt.pose.elevation_deg


def position_geometry(prompt: CloudPositionPrompt) -> PositionGeometry:
    """One pose's WHERE, as the four fields its retained record carries.

    TOTAL where :func:`position_angle_deg` refuses, because this runs on the
    retention path and a derivation that raised would fail a capture the
    household already gave: each refusal becomes a recorded ``degrees=None``,
    never a ``0`` that would read as "on the design axis".
    """
    elevation = position_elevation_deg(prompt)
    unsigned = float(prompt.offset_cm) != 0.0 and prompt.lateral_sign == 0
    return PositionGeometry(
        axis=POSITION_AXIS_HORIZONTAL,
        degrees=None if unsigned else position_angle_deg(prompt),
        # A seat pose is stated from the head, so no mark distance is true of it.
        mark_distance_m=None if prompt.pose.kind == POSE_KIND_SEAT else prompt.mark_distance_m,
        vertical_deg=elevation,
        kind=prompt.pose.kind,
        seat_offset_m=prompt.pose.seat_offset_m,
    )


def remote_position_prompt(prompt: CloudPositionPrompt) -> CloudPositionPrompt:
    """One hand-walked pose, restated as the ANGLE a positioner turns to.

    Same pose, same ``offset_cm``, same role — only the copy changes, so
    everything downstream reads exactly what Full's walk records. A raise also
    states its LENGTH, because the person holding the microphone has a tape
    measure and no protractor. It names the mark distance beside it since that
    is the standoff the height is derived at
    (#2932: a bearing puts the capsule further out than that).
    """
    pose = prompt.pose
    if pose.kind == POSE_KIND_SEAT:
        return replace(prompt, headline=_seat_headline(pose.seat_offset_m), detail=_SEAT_DETAIL)
    distance = prompt.mark_distance_m
    if pose.kind == POSE_KIND_CLOSE and pose.driver:
        return replace(
            prompt,
            headline=(f"Put the microphone {millimetres(distance)} from the centre of the "
                      f"{measurement_target_name(pose.driver)}, on its axis."),
            detail="Measured from the dust cap, pointed straight at it.",
        )
    if pose.kind == POSE_KIND_CLOSE:
        return replace(
            prompt,
            headline=f"Put the microphone {distance:g} m from the baffle on the design axis.",
            detail="Close enough that the room drops out of the read; pointed at the speaker.",
        )
    if pose.kind == POSE_KIND_BEHIND:
        return replace(
            prompt,
            headline=f"Put the microphone {distance:g} m behind the cabinet, on the axis.",
            detail="Between the back panel and the wall, at woofer height, pointed at the back panel.",
        )
    degrees_ = position_angle_deg(prompt)
    elevation = position_elevation_deg(prompt)
    if degrees_ == 0:
        verb = "Leave" if elevation == 0 else "Keep"
        bearing = f"{verb} the microphone on the design axis (0°)"
        detail = f"On the mark, {distance:g} m out, pointed at the speaker."
    else:
        side = "LEFT" if degrees_ < 0 else "RIGHT"
        bearing = (
            f"Turn the microphone to {degrees_:+d}° "
            f"({abs(degrees_)}° {side} of the design axis)"
        )
        detail = f"Keep it {distance:g} m from the speaker and pointed at it."
    clause = elevation_clause(elevation)
    if not clause:
        return replace(prompt, headline=f"{bearing}.", detail=detail)
    height = format_position_distance(round(prompt.vertical_offset_cm))
    return replace(
        prompt,
        headline=(
            f"{bearing}, and {clause} — that is {height} "
            f"at the declared {distance:g} m."
        ),
        detail=detail,
    )


_SEAT_DETAIL = "At the listening position, pointed at the speaker."

#: The cube's three axes, worded from the listener's head: ``(sign, word)``.
_SEAT_AXIS_WORDS = (
    {1: "to the RIGHT of", -1: "to the LEFT of"},
    {1: "FORWARD of", -1: "BEHIND"},
    {1: "ABOVE", -1: "BELOW"},
)


def _seat_headline(offset_m: tuple[float, float, float] | None) -> str:
    """One seat pose in plain words, stated from the head centre at ear height."""
    right, forward, up = offset_m or (0.0, 0.0, 0.0)
    moves = [
        f"{format_position_distance(round(abs(v) * 100.0))} {words[1 if v > 0 else -1]}"
        for v, words in zip((right, forward, up), _SEAT_AXIS_WORDS)
        if v
    ]
    if not moves:
        return "Hold the microphone at the head centre of the listening position, at ear height."
    height = "" if up else ", at ear height"
    return f"Move the microphone {' and '.join(moves)} the head centre{height}."


# --------------------------------------------------------------------------- #
# capture plan + session spec
# --------------------------------------------------------------------------- #

# The cancelable auto-advance countdown between an accepted CHECK and MEASURE.
AUTO_ADVANCE_COUNTDOWN_S = 5

# Auto-advance policy vocabulary carried in the per-entry ``screen`` field,
# which is opaque to the schema.
AUTO_ADVANCE_TAP = "tap"            # requires the user's tap (first capture)
AUTO_ADVANCE_COUNTDOWN = "countdown"  # auto-begins behind a cancelable countdown

# Phone-inactivity budget for the very FIRST begin of a v2 session, before any
# capture: the microphone-check screen's placement instructions take longer to
# read than the general 120 s ``DEFAULT_TIMEOUT_S``. Every later window keeps
# the tight per-phase arm/upload backstop.
V2_FIRST_BEGIN_TIMEOUT_S = 300.0


def v2_first_begin_timeout_s() -> float:
    """The first-begin budget in force — the constant above, env-overridable.

    Out-of-range or unparseable ``JASPER_V2_FIRST_BEGIN_TIMEOUT_S`` values fall
    back to the default. The ceiling is derived from
    ``capture_protocol.MAX_TTL_S``: nothing outliving that sanity ceiling can be
    honoured, whatever this knob says.
    """

    from jasper.playback_state.capture_protocol import MAX_TTL_S  # lazy: test_correction_crossover_v2_endpoints patches capture_protocol.MAX_TTL_S

    return bounded_env_float(
        "JASPER_V2_FIRST_BEGIN_TIMEOUT_S", V2_FIRST_BEGIN_TIMEOUT_S,
        lo=30.0, hi=float(MAX_TTL_S),
    )


#: The per-entry screen keys that state an entry's TARGET POSITION in machine
#: terms. The plan is the source of that pose; the position gate reads it back
#: off the entry. The vertical key rides only a pose that LEAVES mark height —
#: absent IS the mark — because a walk's vertical stops sit at 0° bearing and
#: would otherwise publish as design-axis captures.
POSITION_DEG_KEY = "position_deg"
POSITION_KIND_KEY = "position_kind"
POSITION_VERTICAL_DEG_KEY = "position_vertical_deg"
POSITION_ROLE_KEY = "position_role"
POSITION_BATCH_START_KEY = "position_batch_start"
POSITION_BATCH_SIZE_KEY = "position_batch_size"
POSITION_BATCH_CONFIG_KEY = "position_batch_config"


def pose_batch_screens(
    indexes: Sequence[int], prompts: Sequence[CloudPositionPrompt],
    candidate_ids: Sequence[str],
) -> dict[int, dict[str, str]]:
    """Capture index -> the batch identity every capture of one POSE declares.

    Consecutive prompts at one ``place`` are one batch: the gate grants the
    batch's first capture and carries that grant to the rest
    (:meth:`~.position_gate.PositionGate.gate`), so the microphone moves once
    per pose however many configs play there.
    """
    if len(candidate_ids) != len(indexes) or any(not isinstance(cid, str) for cid in candidate_ids):
        raise CrossoverV2FlowError("candidate ids must name every lateral capture")
    if not indexes:
        return {}
    screens = {}
    for _pose, group in groupby(
        enumerate(prompts), key=lambda row: row[1].pose.place,
    ):
        offsets = [offset for offset, _prompt in group]
        size = len(offsets)
        for ordinal, offset in enumerate(offsets, 1):
            screens[indexes[offset]] = {
                "candidate_id": candidate_ids[offset],
                POSITION_BATCH_START_KEY: str(indexes[offsets[0]]),
                POSITION_BATCH_SIZE_KEY: str(size),
                POSITION_BATCH_CONFIG_KEY: str(ordinal),
                "progress": f"Measurement {ordinal} of {size} at this position — keep the mic still.",
            }
    return screens


def position_screen_keys(
    prompt: CloudPositionPrompt | None,
) -> dict[str, str]:
    """One pose as the TARGET the gate reads back off an entry.

    Shares :data:`POSITION_DEG_KEY` and :data:`POSITION_ROLE_KEY` with ``angle_capture._screen_policy``; ``None`` is the design axis.
    """
    degrees = position_angle_deg(prompt) if prompt is not None else 0
    vertical = position_elevation_deg(prompt) if prompt is not None else 0
    role = prompt.role if prompt is not None else POSITION_ROLE_ONAX
    return {
        POSITION_DEG_KEY: str(degrees),
        **({POSITION_VERTICAL_DEG_KEY: str(vertical)} if vertical else {}),
        POSITION_ROLE_KEY: role,
        **({POSITION_KIND_KEY: prompt.pose.kind} if prompt is not None and prompt.pose.kind != POSE_KIND_BEARING
           else {}),
    }


def wall_clock_ceiling_s(capture_target: int) -> float:
    """The walked-away volume ceiling for a plan of ``capture_target`` captures.

    Grows by :data:`WALL_CLOCK_CEILING_PER_ENTRY_S` for every capture beyond the
    3-entry baseline; ``session_volume_plan.MAX_WALL_CLOCK_CEILING_S`` owns the
    hard cap.
    """

    extra = max(0, capture_target - CAPTURE_PLAN_TARGET)
    return min(
        MAX_WALL_CLOCK_CEILING_S,
        DEFAULT_WALL_CLOCK_CEILING_S + extra * WALL_CLOCK_CEILING_PER_ENTRY_S,
    )


# Per accepted capture beyond the 3-entry baseline: covers a prompt read, a
# deliberate mic move, a tap, the ~16 s sweep entry and the upload. A budget
# allowance, deliberately generous — never a measured position time.
WALL_CLOCK_CEILING_PER_ENTRY_S = 120.0
