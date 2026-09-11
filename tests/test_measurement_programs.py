# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.test_crossover_v2_tuning_scope import banked_program_baseline  # noqa: F401

from jasper.active_speaker import measurement_programs as mp


@pytest.mark.parametrize(
    ("program_id", "size", "poses", "moves", "captures"),
    [
        ("baseline", "full", 13, 13, 16),
        ("baseline", "express", 5, 5, 8),
        ("tournament", "full", 3, 3, 3),
        ("tournament", "express", 1, 1, 1),
        ("seat", "cloud", 11, 11, 11),
        ("seat", "cube", 7, 7, 7),
        ("seat", "express", 3, 3, 3),
        ("room", "cloud", 11, 11, 11),
        ("room", "quick", 3, 3, 3),
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
        ("bass", "cloud"),
        ("bass", "quick"),
        ("branches", "express"),
        ("close", "spot"),
        ("room", "cloud"),
        ("room", "quick"),
        ("seat", "cloud"),
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
    assert [p.seat_offset_m for p in cube.poses] == [
        (0.0, 0.0, 0.0),
        (0.30, 0.0, 0.0), (-0.30, 0.0, 0.0),
        (0.0, 0.30, 0.0), (0.0, -0.30, 0.0),
        (0.0, 0.0, 0.30), (0.0, 0.0, -0.30),
    ]
    assert [p.seat_offset_m for p in express.poses] == [
        (0.0, 0.0, 0.0), (mp.SEAT_OFFSET_M, 0.0, 0.0), (0.0, mp.SEAT_OFFSET_M, 0.0),
    ]
    assert {p.seat_offset_m for p in express.poses} <= {
        p.seat_offset_m for p in cube.poses
    }


def test_seat_cloud_walks_three_rows_then_above_and_below_the_head() -> None:
    cloud = mp.program("seat", "cloud")

    assert {p.kind for p in cloud.poses} == {mp.POSE_KIND_SEAT}
    assert {(p.azimuth_deg, p.elevation_deg, p.repeats) for p in cloud.poses} == {(0, 0, 1)}
    assert [p.seat_offset_m for p in cloud.poses] == [
        (-0.30, 0.30, 0.0), (0.0, 0.30, 0.0), (0.30, 0.30, 0.0),
        (-0.30, 0.0, 0.0), (0.0, 0.0, 0.0), (0.30, 0.0, 0.0),
        (-0.30, -0.30, 0.0), (0.0, -0.30, 0.0), (0.30, -0.30, 0.0),
        (0.0, 0.0, 0.30), (0.0, 0.0, -0.30),
    ]


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


def test_configured_defaults_preserve_existing_cli_choices_and_add_room() -> None:
    assert {
        program_id: mp.program(program_id).size
        for program_id in ("baseline", "tournament", "branches", "seat", "room", "close")
    } == {
        "baseline": "express",
        "tournament": "express",
        "branches": "express",
        "seat": "cloud",
        "room": "cloud",
        "close": "spot",
    }


@pytest.mark.parametrize("program,purpose,scope", [("room", mp.PURPOSE_ROOM, "speaker"), ("bass", mp.PURPOSE_BASS, "room")])
def test_room_and_bass_plans_preserve_their_upstream_layers(program, purpose, scope) -> None:
    cloud = mp.program(program, "cloud")
    quick = mp.program(program, "quick")

    assert cloud.poses is mp.program("seat", "cloud").poses
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in quick.poses] == [
        (0, 0), (-20, 0), (20, 0),
    ]
    assert {row.purpose for row in (cloud, quick)} == {purpose}
    assert {row.regime for row in (cloud, quick)} == {mp.REGIME_SUMMED}
    assert mp.gate_exemption(cloud.purpose) == mp.SEAT_EXEMPT
    assert mp.baseline_scope(cloud.purpose) == scope


@pytest.mark.parametrize(
    ("purpose", "kind", "expected"),
    [
        (None, mp.POSE_KIND_BEARING, mp.PURPOSE_SPEAKER),
        (None, mp.POSE_KIND_SEAT, mp.PURPOSE_ROOM),
        (None, mp.POSE_KIND_CLOSE, mp.PURPOSE_REFERENCE),
        (mp.PURPOSE_ROOM, mp.POSE_KIND_BEARING, mp.PURPOSE_ROOM),
    ],
)
def test_measurement_purpose_resolves_explicit_and_legacy_rows(
    purpose: str | None, kind: str, expected: str
) -> None:
    assert mp.resolved_measurement_purpose(purpose, kind) == expected


def _bundled_config() -> dict[str, object]:
    path = Path(mp.__file__).with_name("measurement_plans.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, config: dict[str, object]) -> Path:
    path = tmp_path / "plans.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_shared_layout_can_change_to_five_positions_without_code(tmp_path: Path) -> None:
    config = _bundled_config()
    config["layouts"]["seat_cloud"] = config["layouts"]["seat_cloud"][:5]  # type: ignore[index]

    programs = mp.load_programs(_write_config(tmp_path, config))

    assert len(programs[("seat", "cloud")].poses) == 5
    assert programs[("room", "cloud")].poses is programs[("seat", "cloud")].poses


def test_config_can_supply_future_prompt_text(tmp_path: Path) -> None:
    config = _bundled_config()
    pose = config["layouts"]["room_quick"][0]  # type: ignore[index]
    pose.update({"headline": "Measure the main seat", "detail": "Hold the mic at ear height."})

    programs = mp.load_programs(_write_config(tmp_path, config))

    loaded = programs[("room", "quick")].poses[0]
    assert (loaded.headline, loaded.detail) == (
        "Measure the main seat", "Hold the mic at ear height.",
    )


@pytest.mark.parametrize("broken", ["empty", "repeats", "purpose", "regime", "mode"])
def test_malformed_config_is_rejected(tmp_path: Path, broken: str) -> None:
    config = _bundled_config()
    if broken == "empty":
        config["layouts"]["room_quick"] = []  # type: ignore[index]
    elif broken == "repeats":
        config["layouts"]["room_quick"][0]["repeats"] = 0  # type: ignore[index]
    elif broken == "purpose":
        config["programs"][0]["purpose"] = "other"  # type: ignore[index]
    elif broken == "regime":
        config["programs"][0]["regime"] = "other"  # type: ignore[index]
    else:
        config["programs"][0].update({"purpose": "room", "regime": "per_driver"})  # type: ignore[index]

    with pytest.raises(ValueError):
        mp.load_programs(_write_config(tmp_path, config))
