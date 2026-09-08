# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Named measurement programs, as plain data.

A program is a menu item the household or the LLM operator picks by name.
Free-form geometry is not a program: the only caller-supplied bearing is
:func:`spot_program`'s single pose.

The numbers come from ``docs/tuning-master-plan.md``, section "Measurement
program constants"; this table is where they live.

A pose is CATEGORIZED (ADR-0260): a ``bearing`` is an absolute
azimuth and elevation at the mark in whole degrees, a ``seat`` is an offset
from the listener's head centre, a ``close`` take sits on the design axis at
its own distance. Mover reach is the staging layer's to enforce: a program
says where to measure, not where the mover can go.

Deliberate omissions, so absence reads as a decision:

* No ``verify`` row: the shipped verify flow owns its own pose table
  (``crossover_v2.capture_plan.verify_pose_table``).
* No level. Every program is driven at the banked seat-level anchor's own SPL.
* No distance on a bearing: ``distance_m=None`` is the walk's own
  :data:`~jasper.active_speaker.crossover_v2_flow.MARK_DISTANCE_M`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from jasper.audio_measurement.gating import SEAT_EXEMPT

# Repeats at the on-axis anchor, per the plan's ratified position-major
# structure: x4 at the 0 deg anchor pose, x1 at every other pose.
ANCHOR_REPEATS = 4

#: The pose kinds. A ``bearing`` is gated to the direct sound; a ``seat`` take
#: is the room's own measurement and keeps its reflections
#: (docs/measurement-loop-doctrine.md 1a; ADR-0260); a ``close`` take
#: is the room-suppressed reference, gated like a bearing.
POSE_KIND_BEARING = "bearing"
POSE_KIND_SEAT = "seat"
POSE_KIND_CLOSE = "close"
POSE_KINDS = (POSE_KIND_BEARING, POSE_KIND_SEAT, POSE_KIND_CLOSE)

#: Which kinds are analyzed ungated, and the word their gating block carries.
GATE_EXEMPTION_BY_POSE_KIND: Mapping[str, str] = MappingProxyType({POSE_KIND_SEAT: SEAT_EXEMPT})


def off_the_mark(kind: str) -> bool:
    """A bearing is measured at the mark. Every other kind is stated from
    somewhere else: it plays the applied tune whole as one summed sweep, and
    a turntable at the mark cannot reach it."""

    return kind != POSE_KIND_BEARING

#: The cube's half-edge and the close reference's standoff, both in metres.
#: See ADR-0260.
SEAT_OFFSET_M = 0.30
CLOSE_DISTANCE_M = 0.30


def validated_pose(
    kind: str,
    seat_offset_m: Sequence[float] | None,
    distance_m: float | None = None,
) -> tuple[tuple[float, float, float] | None, float | None]:
    """The one rule every carrier of a pose category checks: ``kind`` is one
    of :data:`POSE_KINDS`; exactly a seat states three finite metres
    ``(right, forward, up)`` from the head; a distance, when stated, is a
    positive length. Returns the offset and distance normalized to floats;
    raises ``ValueError``."""

    if kind not in POSE_KINDS:
        raise ValueError(f"a pose kind must be one of {POSE_KINDS}, got {kind!r}")
    if (seat_offset_m is not None) != (kind == POSE_KIND_SEAT):
        raise ValueError(
            "a seat pose states its (right, forward, up) offset from the head; "
            "no other kind does"
        )
    offset = None
    if seat_offset_m is not None:
        try:
            offset = tuple(float(v) for v in seat_offset_m)
        except (TypeError, ValueError):
            offset = ()
        if len(offset) != 3 or not all(math.isfinite(v) for v in offset):
            raise ValueError(f"a seat offset is three finite metres, got {seat_offset_m!r}")
    distance = None
    if distance_m is not None:
        distance = float(distance_m) if isinstance(distance_m, (int, float)) else math.nan
        if not math.isfinite(distance) or distance <= 0:
            raise ValueError(f"a pose distance is a positive length in metres, got {distance_m!r}")
    return offset, distance  # type: ignore[return-value]


def pose_place(
    kind: str,
    azimuth_deg: int,
    elevation_deg: int,
    distance_m: float | None,
    seat_offset_m: tuple[float, float, float] | None,
) -> tuple[object, ...]:
    """What distinguishes one microphone position from another."""

    return (kind, azimuth_deg, elevation_deg, distance_m, seat_offset_m)


@dataclass(frozen=True)
class ProgramPose:
    """One place to measure from, and how many takes to capture there.

    ``seat_offset_m`` is ``(right, forward, up)`` from the head centre, seat
    kind only. ``distance_m`` is the speaker-to-microphone standoff a
    ``close`` take states; ``None`` is the mark.
    """

    azimuth_deg: int
    elevation_deg: int
    repeats: int = 1
    kind: str = POSE_KIND_BEARING
    distance_m: float | None = None
    seat_offset_m: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        offset, distance = validated_pose(self.kind, self.seat_offset_m, self.distance_m)
        object.__setattr__(self, "seat_offset_m", offset)
        object.__setattr__(self, "distance_m", distance)

    @property
    def place(self) -> tuple[object, ...]:
        return pose_place(
            self.kind, self.azimuth_deg, self.elevation_deg,
            self.distance_m, self.seat_offset_m,
        )


@dataclass(frozen=True)
class MeasurementProgram:
    """One named menu item: an ordered pose list."""

    program_id: str
    size: str
    poses: tuple[ProgramPose, ...]

    @property
    def mic_move_count(self) -> int:
        """Distinct places — repeats stay at one place and move nothing."""

        return len({p.place for p in self.poses})

    @property
    def capture_count(self) -> int:
        return sum(p.repeats for p in self.poses)


class UnknownProgramError(ValueError):
    """No such ``(program_id, size)``. ``choices`` carries the valid pairs."""

    def __init__(
        self,
        program_id: str,
        size: str,
        choices: tuple[tuple[str, str], ...],
    ) -> None:
        self.program_id = program_id
        self.size = size
        self.choices = choices
        offered = ", ".join(f"{pid}/{sz}" for pid, sz in choices) or "(none)"
        super().__init__(
            f"no measurement program {program_id}/{size}; choose one of: {offered}"
        )


_BASELINE_FULL_POSES: tuple[ProgramPose, ...] = (
    ProgramPose(0, 0, ANCHOR_REPEATS),
    ProgramPose(-10, 0),
    ProgramPose(10, 0),
    ProgramPose(-20, 0),
    ProgramPose(20, 0),
    ProgramPose(-30, 0),
    ProgramPose(30, 0),
    ProgramPose(-40, 0),
    ProgramPose(40, 0),
    ProgramPose(0, -10),
    ProgramPose(0, 10),
    ProgramPose(0, -20),
    ProgramPose(0, 20),
)

# On-axis plus one horizontal pair and one vertical pair: the owner's quick
# tier, same anchor repeats as full.
_BASELINE_EXPRESS_POSES: tuple[ProgramPose, ...] = (
    ProgramPose(0, 0, ANCHOR_REPEATS),
    ProgramPose(-20, 0),
    ProgramPose(20, 0),
    ProgramPose(0, -10),
    ProgramPose(0, 10),
)

# The candidate cycle's poses (#3498). Few and unrepeated on purpose: a
# tournament round multiplies the CANDIDATE list, and a candidate is only
# comparable to another measured from the same place.
_TOURNAMENT_EXPRESS_POSES: tuple[ProgramPose, ...] = (ProgramPose(0, 0),)

_TOURNAMENT_FULL_POSES: tuple[ProgramPose, ...] = (
    ProgramPose(0, 0),
    ProgramPose(-20, 0),
    ProgramPose(20, 0),
)


def _seat(right_m: float, forward_m: float, up_m: float) -> ProgramPose:
    return ProgramPose(
        0, 0, kind=POSE_KIND_SEAT, seat_offset_m=(right_m, forward_m, up_m),
    )


# The seat cube: the head centre and the six face centres around it, each
# a SUMMED sweep through the applied tune (the VERIFY shape), because the
# room is measured through the speaker stage it sits on.
_SEAT_CUBE_POSES: tuple[ProgramPose, ...] = (
    _seat(0.0, 0.0, 0.0),
    _seat(SEAT_OFFSET_M, 0.0, 0.0),
    _seat(-SEAT_OFFSET_M, 0.0, 0.0),
    _seat(0.0, SEAT_OFFSET_M, 0.0),
    _seat(0.0, -SEAT_OFFSET_M, 0.0),
    _seat(0.0, 0.0, SEAT_OFFSET_M),
    _seat(0.0, 0.0, -SEAT_OFFSET_M),
)

_SEAT_EXPRESS_POSES: tuple[ProgramPose, ...] = (
    _seat(0.0, 0.0, 0.0),
    _seat(SEAT_OFFSET_M, 0.0, 0.0),
    _seat(0.0, SEAT_OFFSET_M, 0.0),
)

# One summed take on the design axis, close enough that the room is
# suppressed: what ``round-views close-reference`` reads.
_CLOSE_SPOT_POSES: tuple[ProgramPose, ...] = (
    ProgramPose(0, 0, kind=POSE_KIND_CLOSE, distance_m=CLOSE_DISTANCE_M),
)

_PROGRAMS: Mapping[tuple[str, str], MeasurementProgram] = {
    (p.program_id, p.size): p
    for p in (
        MeasurementProgram("baseline", "full", _BASELINE_FULL_POSES),
        MeasurementProgram("baseline", "express", _BASELINE_EXPRESS_POSES),
        MeasurementProgram("tournament", "full", _TOURNAMENT_FULL_POSES),
        MeasurementProgram("tournament", "express", _TOURNAMENT_EXPRESS_POSES),
        MeasurementProgram("seat", "cube", _SEAT_CUBE_POSES),
        MeasurementProgram("seat", "express", _SEAT_EXPRESS_POSES),
        MeasurementProgram("close", "spot", _CLOSE_SPOT_POSES),
    )
}


def available_programs() -> tuple[tuple[str, str], ...]:
    """The ``(program_id, size)`` pairs a menu may offer, sorted.

    ``spot`` is absent on purpose: it carries caller geometry, so it is reached
    through :func:`spot_program` rather than looked up by name.
    """

    return tuple(sorted(_PROGRAMS))


def program(program_id: str, size: str) -> MeasurementProgram:
    """The named program, or :class:`UnknownProgramError` listing the choices."""

    try:
        return _PROGRAMS[(program_id, size)]
    except KeyError:
        raise UnknownProgramError(program_id, size, available_programs()) from None


def spot_program(azimuth_deg: int, elevation_deg: int) -> MeasurementProgram:
    """One take at one caller-supplied bearing.

    Not a registry row, and it enforces no reach bounds — the staging layer
    owns what the mover can reach.
    """

    return MeasurementProgram(
        "spot", "express", (ProgramPose(azimuth_deg, elevation_deg),)
    )
