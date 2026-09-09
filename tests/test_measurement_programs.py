# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior."""

from __future__ import annotations

import pytest

from jasper.active_speaker import measurement_programs as mp


@pytest.mark.parametrize(
    ("program_id", "size", "poses", "moves", "captures"),
    [
        ("baseline", "full", 13, 13, 16),
        ("baseline", "express", 5, 5, 8),
        ("tournament", "full", 3, 3, 3),
        ("tournament", "express", 1, 1, 1),
        ("seat", "cube", 7, 7, 7),
        ("seat", "express", 3, 3, 3),
        ("close", "spot", 1, 1, 1),
    ],
)
def test_shipped_rows(
    program_id: str, size: str, poses: int, moves: int, captures: int
) -> None:
    """The shipped numbers."""

    row = mp.program(program_id, size)

    assert (row.program_id, row.size) == (program_id, size)
    assert len(row.poses) == poses
    assert row.mic_move_count == moves
    assert row.capture_count == captures


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
    [("baseline", "medium"), ("tournament", "medium"), ("spot", "express"), ("", "")],
)
def test_unknown_lookup_names_the_valid_choices(program_id: str, size: str) -> None:
    """A miss carries the menu as a field, not only in its message."""

    with pytest.raises(mp.UnknownProgramError) as excinfo:
        mp.program(program_id, size)

    assert excinfo.value.choices == mp.available_programs()
    assert (excinfo.value.program_id, excinfo.value.size) == (program_id, size)


def test_available_programs_is_the_sorted_registry() -> None:
    choices = mp.available_programs()

    assert choices == (
        ("baseline", "express"),
        ("baseline", "full"),
        ("branches", "express"),
        ("close", "spot"),
        ("seat", "cube"),
        ("seat", "express"),
        ("tournament", "express"),
        ("tournament", "full"),
    )
    rows = [mp.program(program_id, size) for program_id, size in choices]
    assert tuple((row.program_id, row.size) for row in rows) == choices


def _seat(right_m: float, forward_m: float, up_m: float, repeats: int = 1):
    return mp.ProgramPose(
        0, 0, repeats,
        kind=mp.POSE_KIND_SEAT, seat_offset_m=(right_m, forward_m, up_m),
    )


@pytest.mark.parametrize(
    ("poses", "moves", "captures"),
    [
        (
            (mp.ProgramPose(0, 0, 1), mp.ProgramPose(0, 0, 1), mp.ProgramPose(10, 0, 1)),
            2,
            3,
        ),
        (
            (mp.ProgramPose(0, 0, 4), mp.ProgramPose(0, 0, 1), mp.ProgramPose(10, 0, 2)),
            2,
            7,
        ),
        # Two seat poses share the (0, 0) bearing and are two different places.
        ((_seat(0.0, 0.0, 0.0), _seat(mp.SEAT_OFFSET_M, 0.0, 0.0, 2)), 2, 3),
    ],
)
def test_counts_split_moves_from_captures(
    poses: tuple[object, ...], moves: int, captures: int
) -> None:
    """Repeats add captures at a place already reached, never a mic move."""

    row = mp.MeasurementProgram(program_id="t", size="t", poses=poses)

    assert row.mic_move_count == moves
    assert row.capture_count == captures


def test_the_seat_cube_is_the_head_and_six_face_centres() -> None:
    """The listener's head and the six faces one offset away, express a subset."""

    cube = mp.program("seat", "cube")
    express = mp.program("seat", "express")

    assert {p.kind for p in cube.poses} == {mp.POSE_KIND_SEAT}
    assert {(p.azimuth_deg, p.elevation_deg, p.repeats) for p in cube.poses} == {(0, 0, 1)}
    assert {p.seat_offset_m for p in cube.poses} == {
        (0.0, 0.0, 0.0),
        (mp.SEAT_OFFSET_M, 0.0, 0.0), (-mp.SEAT_OFFSET_M, 0.0, 0.0),
        (0.0, mp.SEAT_OFFSET_M, 0.0), (0.0, -mp.SEAT_OFFSET_M, 0.0),
        (0.0, 0.0, mp.SEAT_OFFSET_M), (0.0, 0.0, -mp.SEAT_OFFSET_M),
    }
    assert [p.seat_offset_m for p in express.poses] == [
        (0.0, 0.0, 0.0), (mp.SEAT_OFFSET_M, 0.0, 0.0), (0.0, mp.SEAT_OFFSET_M, 0.0),
    ]
    assert {p.seat_offset_m for p in express.poses} <= {
        p.seat_offset_m for p in cube.poses
    }


def test_close_spot_is_one_close_pose_at_its_own_distance() -> None:
    """The room-suppressed reference states a standoff, and no head offset."""

    pose, = mp.program("close", "spot").poses

    assert pose.kind == mp.POSE_KIND_CLOSE
    assert pose.distance_m == mp.CLOSE_DISTANCE_M
    assert pose.seat_offset_m is None


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

