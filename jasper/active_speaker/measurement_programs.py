# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Named measurement programs, as plain data.

A program is a menu item the household or the LLM operator picks by name.
Free-form geometry is not a program: the only caller-supplied bearing is
:func:`spot_program`'s single pose.

This module is the code derivation of ``docs/tuning-master-plan.md``, section
"Measurement program constants (owner research, 2026-08-21)";
``tests/test_measurement_programs.py`` pins the two against each other so the
numbers cannot drift apart silently.

Poses are ABSOLUTE bearings at the mark in whole degrees — azimuth and
elevation from the on-axis anchor. Mover reach is the staging layer's to
enforce: a program says where to measure, not where the mover can go.

Deliberate omissions, so absence reads as a decision:

* The plan's nearfield captures per woofer (and port, when vented) are NOT
  poses here — the nearfield regime is not wired. They arrive with it (plan,
  "Nearfield splice v1 (Wave 4.2)").
* No ``verify`` row. The shipped verify flow owns its own pose table
  (``crossover_v2.capture_plan.CLOUD_VERIFY_POSE_PROMPTS`` and
  ``verify_pose_table``, re-exported by ``crossover_v2_flow``); a second copy
  would be a second definition of one thing.
* No absolute level. ``level_re_anchor_db`` is the only level a program
  states; :mod:`jasper.active_speaker.measurement_level` turns it into dB SPL
  against the banked seat-level anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .crossover_v2.capture_plan import MARK_DISTANCE_M

# Repeats at the on-axis anchor, per the plan's ratified position-major
# structure: x4 at the 0 deg anchor pose, x1 at every other pose.
ANCHOR_REPEATS = 4

# Seconds a single hold may wait: ten minutes covers the slower mover — a
# person walking a tape to the next bearing and posting the release. The web
# gate that spends it, ``REMOTE_POSITION_HOLD_BUDGET_S``, reads it here.
HOLD_BUDGET_S = 600


@dataclass(frozen=True)
class ProgramPose:
    """One bearing at the mark, and how many takes to capture there."""

    azimuth_deg: int
    elevation_deg: int
    repeats: int = 1


@dataclass(frozen=True)
class MeasurementProgram:
    """One named menu item: an ordered pose list, at one mark distance."""

    program_id: str
    size: str
    poses: tuple[ProgramPose, ...]
    mark_distance_m: float = MARK_DISTANCE_M
    # dB relative to the measurement anchor, never an absolute constant (plan,
    # "Drive level is anchor-relative, never an absolute program constant").
    level_re_anchor_db: float = 0.0

    @property
    def mic_move_count(self) -> int:
        """Distinct bearings — repeats stay at one bearing and move nothing."""

        return len({(p.azimuth_deg, p.elevation_deg) for p in self.poses})

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

# The candidate cycle's poses (#3498). Few and unrepeated on purpose: what a
# tournament round multiplies is the CANDIDATE list, and a candidate is only
# comparable to another measured from the same place, so poses cost travel
# where candidates cost captures.
_TOURNAMENT_EXPRESS_POSES: tuple[ProgramPose, ...] = (ProgramPose(0, 0),)

_TOURNAMENT_FULL_POSES: tuple[ProgramPose, ...] = (
    ProgramPose(0, 0),
    ProgramPose(-20, 0),
    ProgramPose(20, 0),
)

_PROGRAMS: Mapping[tuple[str, str], MeasurementProgram] = {
    (p.program_id, p.size): p
    for p in (
        MeasurementProgram("baseline", "full", _BASELINE_FULL_POSES),
        MeasurementProgram("baseline", "express", _BASELINE_EXPRESS_POSES),
        MeasurementProgram("tournament", "full", _TOURNAMENT_FULL_POSES),
        MeasurementProgram("tournament", "express", _TOURNAMENT_EXPRESS_POSES),
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
