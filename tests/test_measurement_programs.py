# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior, and its drift pin against the plan doc.

``jasper/active_speaker/measurement_programs.py`` is a code derivation of one
section of ``docs/tuning-master-plan.md``. Two copies of one set of numbers
drift, so the pin below reads the plan's own bullet and compares the numbers
with the table's. It parses NUMBERS out of a whitespace-normalised bullet, not
prose: several PRs edit that document concurrently, and reflowing or rewording
the sentence around the angles must not fail this file.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from jasper.active_speaker import measurement_programs as mp

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_DOC = REPO_ROOT / "docs" / "tuning-master-plan.md"
PLAN_SECTION = "Measurement program constants"
RECONCILE = (
    f"reconcile {PLAN_DOC.name} '{PLAN_SECTION}' with "
    "jasper/active_speaker/measurement_programs.py"
)


def _plan_baseline_bullet() -> str:
    """The plan's ``baseline`` bullet, whitespace-normalised."""

    text = PLAN_DOC.read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    matching = [s for s in sections if s.startswith(PLAN_SECTION)]
    assert len(matching) == 1, (
        f"expected exactly one '{PLAN_SECTION}' section in {PLAN_DOC.name}, "
        f"found {len(matching)}"
    )

    for bullet in re.split(r"\n(?=- )", matching[0]):
        if bullet.lstrip().startswith("- **`baseline` program:**"):
            return " ".join(bullet.split())

    pytest.fail(f"no '`baseline` program' bullet in {PLAN_DOC.name}; {RECONCILE}")


def _search(pattern: str, bullet: str) -> re.Match[str]:
    match = re.search(pattern, bullet)
    assert match, (
        f"the plan's baseline bullet no longer states {pattern!r}, so this pin "
        f"cannot read the plan's numbers; {RECONCILE}"
    )
    return match


def _angles(group: str) -> set[int]:
    """``0°, ±10°, ±20°`` -> ``{0, -10, 10, -20, 20}``."""

    angles: set[int] = set()
    for sign, value in re.findall(r"(±?)(\d+)°", group):
        angles.add(int(value))
        if sign:
            angles.add(-int(value))
    return angles


def test_full_baseline_matches_the_plans_pose_set() -> None:
    """The 13-pose set and its 1 m mark, as the plan states them."""

    bullet = _plan_baseline_bullet()
    full = mp.program("baseline", "full")

    plan_poses = int(_search(r"(\d+) poses at ", bullet).group(1))
    assert len(full.poses) == plan_poses, (
        f"plan says {plan_poses} poses, table has {len(full.poses)} — "
        f"one side moved; {RECONCILE}"
    )

    plan_distance = float(_search(r"poses at ([\d.]+) m", bullet).group(1))
    assert full.mark_distance_m == plan_distance, (
        f"plan measures at {plan_distance} m, table at {full.mark_distance_m} m — "
        f"one side moved; {RECONCILE}"
    )

    plan_horizontal = _angles(_search(r"horizontal ((?:±?\d+°,? ?)+)", bullet).group(1))
    table_horizontal = {p.azimuth_deg for p in full.poses if p.elevation_deg == 0}
    assert table_horizontal == plan_horizontal, (
        f"plan's horizontal set is {sorted(plan_horizontal)}, table's is "
        f"{sorted(table_horizontal)} — one side moved; {RECONCILE}"
    )

    plan_vertical = _angles(_search(r"vertical ((?:±?\d+°,? ?)+)", bullet).group(1))
    table_vertical = {p.elevation_deg for p in full.poses if p.azimuth_deg == 0}
    assert table_vertical == plan_vertical, (
        f"plan's vertical set is {sorted(plan_vertical)}, table's is "
        f"{sorted(table_vertical)} — one side moved (the vertical 0 is the "
        f"shared on-axis anchor, not a fourteenth pose); {RECONCILE}"
    )


def test_full_baseline_matches_the_plans_repeat_structure() -> None:
    """x4 at the 0 deg anchor, x1 at every other pose."""

    bullet = _plan_baseline_bullet()
    full = mp.program("baseline", "full")

    plan_anchor = int(_search(r"×(\d+) at the 0° anchor pose", bullet).group(1))
    plan_other = int(_search(r"×(\d+) at every other pose", bullet).group(1))

    anchors = [p for p in full.poses if (p.azimuth_deg, p.elevation_deg) == (0, 0)]
    assert len(anchors) == 1, (
        f"table has {len(anchors)} on-axis anchor poses, expected exactly one; "
        f"{RECONCILE}"
    )
    assert anchors[0].repeats == plan_anchor, (
        f"plan repeats the anchor x{plan_anchor}, table x{anchors[0].repeats} — "
        f"one side moved; {RECONCILE}"
    )

    others = {p.repeats for p in full.poses if p is not anchors[0]}
    assert others == {plan_other}, (
        f"plan repeats every other pose x{plan_other}, table uses "
        f"{sorted(others)} — one side moved; {RECONCILE}"
    )


@pytest.mark.parametrize(
    ("program_id", "size", "poses", "moves", "captures", "ceiling_s"),
    [
        ("baseline", "full", 13, 13, 16, 3360),
        ("baseline", "express", 5, 5, 8, 2400),
    ],
)
def test_shipped_rows(
    program_id: str,
    size: str,
    poses: int,
    moves: int,
    captures: int,
    ceiling_s: int,
) -> None:
    """The shipped numbers, including the ceilings the module's prose states."""

    row = mp.program(program_id, size)

    assert (row.program_id, row.size) == (program_id, size)
    assert len(row.poses) == poses
    assert row.mic_move_count == moves
    assert row.capture_count == captures
    assert row.mark_distance_m == 1.0
    assert (row.hold_budget_s, row.session_ceiling_s) == (600, ceiling_s)


def test_express_geometry() -> None:
    """The quick tier: on-axis plus one horizontal pair and one vertical pair."""

    row = mp.program("baseline", "express")

    assert {p.azimuth_deg for p in row.poses} == {0, -20, 20}
    assert {p.elevation_deg for p in row.poses} == {0, -10, 10}
    assert [
        p.repeats for p in row.poses if (p.azimuth_deg, p.elevation_deg) == (0, 0)
    ] == [mp.ANCHOR_REPEATS]


@pytest.mark.parametrize(
    ("program_id", "size"),
    [("baseline", "medium"), ("tournament", "full"), ("spot", "express"), ("", "")],
)
def test_unknown_lookup_names_the_valid_choices(program_id: str, size: str) -> None:
    """A miss carries the menu as a field, not only in its message."""

    with pytest.raises(mp.UnknownProgramError) as excinfo:
        mp.program(program_id, size)

    assert excinfo.value.choices == mp.available_programs()
    assert (excinfo.value.program_id, excinfo.value.size) == (program_id, size)


def test_available_programs_is_the_sorted_registry() -> None:
    choices = mp.available_programs()

    assert choices == (("baseline", "express"), ("baseline", "full"))
    rows = [mp.program(program_id, size) for program_id, size in choices]
    assert tuple((row.program_id, row.size) for row in rows) == choices


@pytest.mark.parametrize(
    ("repeats", "moves", "captures"),
    [
        ((1, 1, 1), 2, 3),
        ((4, 1, 2), 2, 7),
    ],
)
def test_counts_split_moves_from_captures(
    repeats: tuple[int, int, int], moves: int, captures: int
) -> None:
    """Repeats add captures at a bearing already reached, never a mic move."""

    row = mp.MeasurementProgram(
        program_id="t",
        size="t",
        poses=(
            mp.ProgramPose(0, 0, repeats[0]),
            mp.ProgramPose(0, 0, repeats[1]),
            mp.ProgramPose(10, 0, repeats[2]),
        ),
        hold_budget_s=mp.HOLD_BUDGET_S,
        session_ceiling_s=0,
    )

    assert row.mic_move_count == moves
    assert row.capture_count == captures


@pytest.mark.parametrize(
    ("azimuth", "elevation"),
    [(0, 0), (-35, 10), (400, -400)],
)
def test_spot_is_one_take_at_the_callers_bearing(azimuth: int, elevation: int) -> None:
    """Out-of-reach geometry is the staging layer's refusal, not this table's."""

    row = mp.spot_program(azimuth, elevation)

    assert row.poses == (mp.ProgramPose(azimuth, elevation, 1),)
    assert (row.mic_move_count, row.capture_count) == (1, 1)
    assert (row.program_id, row.size) == ("spot", "express")
    assert row.mark_distance_m == 1.0


@pytest.mark.parametrize(
    "row",
    [
        mp.program("baseline", "full"),
        mp.program("baseline", "express"),
        mp.spot_program(0, 0),
    ],
    ids=["full", "express", "spot"],
)
def test_clocks_are_program_owned(row: mp.MeasurementProgram) -> None:
    """Every program's ceiling scales with its own capture count."""

    assert row.hold_budget_s == mp.HOLD_BUDGET_S
    assert row.session_ceiling_s == 1800 + max(0, row.capture_count - 3) * (
        mp.CAPTURE_SETTLE_ALLOWANCE_S
    )


def test_table_imports_nothing_but_stdlib() -> None:
    """The table is plain data: importing it must not pull the audio stack."""

    source = Path(mp.__file__).read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    assert roots <= {"__future__", "dataclasses", "typing"}, (
        f"measurement_programs.py imports {sorted(roots)} — it is a plain-data "
        "table and stays import-cheap"
    )
