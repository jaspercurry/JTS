# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from tests.test_plan_run import banked_program_baselines  # noqa: F401

from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW, BOOKKEEPING_ORDER
from jasper.audio_measurement.gating import SEAT_EXEMPT


@pytest.mark.parametrize(
    ("program_id", "size", "poses", "moves", "captures"),
    [
        ("speaker", "mark", 1, 1, 3),
        ("baseline", "full", 13, 13, 29),
        ("baseline", "express", 5, 5, 13),
        ("tournament", "full", 3, 3, 3),
        ("tournament", "express", 1, 1, 1),
        ("seat", "cloud", 11, 11, 11),
        ("seat", "cube", 7, 7, 7),
        ("seat", "express", 3, 3, 3),
        ("room", "cloud", 11, 11, 11),
        ("room", "arm", 3, 3, 3),
        ("room", "seat", 3, 3, 3),
        ("bass", "axis", 1, 1, 1),
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
    assert row.room_sweep is (program_id in {"speaker", "baseline"})


@pytest.mark.parametrize("purpose", ["room", "bass"])
def test_summed_bookkeeping_includes_one_frequency_image(purpose):
    assert ("frequency", False, False) in mp.bookkeeping_views(purpose)


def test_speaker_bookkeeping_uses_room_views_when_the_round_holds_room_sweeps():
    assert mp.bookkeeping_views("speaker", has_room=True) == tuple(
        row for row in mp.bookkeeping_views("room") if row[0] != "frequency")


@pytest.mark.parametrize(("purpose", "has_room", "expected"), [
    ("speaker", False, (("inventory", True, False),)),
    ("speaker", True, (("room", True, False), ("room-grade", True, True), ("inventory", True, False))),
    ("room", False, (("room", True, False), ("room-grade", True, True), ("frequency", False, False),
                     ("inventory", True, False))),
    ("bass", False, (("bass", True, False), ("frequency", False, False), ("inventory", True, False))),
    ("reference", False, ()),
    ("rear", False, (("inventory", True, False),)),
])
def test_the_view_table_answers_every_automatic_view(purpose, has_room, expected):
    """One table, not four lists: each automatic view resolves to a builder."""
    assert mp.bookkeeping_views(purpose, has_room=has_room) == expected
    assert {name for name, row in ARTIFACT_BY_VIEW.items() if row.builder} == set(BOOKKEEPING_ORDER)
    for view, _, _ in expected:
        row = ARTIFACT_BY_VIEW[view]
        module, _, builder = row.builder.rpartition(".")
        assert callable(getattr(import_module(f".{module}", "jasper.active_speaker"), builder))


@pytest.mark.parametrize("reverse", [False, True])
def test_run_layout_prefers_its_program_regardless_of_registry_order(monkeypatch, reverse):
    monkeypatch.setattr(mp, "_PROGRAMS", dict(sorted(mp._PROGRAMS.items(), reverse=reverse)))
    assert mp.run_program("room", "room_quick") == mp.program("room", "arm")
    assert mp.run_program("bass", "room_quick") == mp.program("bass", "quick")


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
        ("bass", "axis"),
        ("bass", "cloud"),
        ("bass", "nearfield"),
        ("bass", "quick"),
        ("branches", "express"),
        ("close", "spot"),
        ("front_rear", "express"),
        ("rear", "express"),
        ("rear", "wide"),
        ("room", "arm"),
        ("room", "cloud"),
        ("room", "seat"),
        ("seat", "cloud"),
        ("seat", "cube"),
        ("seat", "express"),
        ("speaker", "mark"),
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
        for program_id in ("baseline", "tournament", "branches", "seat", "room", "close", "rear")
    } == {
        "baseline": "express",
        "tournament": "express",
        "branches": "express",
        "seat": "cloud",
        "room": "seat",
        "close": "spot",
        "rear": "express",
    }


@pytest.mark.parametrize("program,size,purpose", [("room", "arm", mp.PURPOSE_ROOM), ("bass", "quick", mp.PURPOSE_BASS)])
def test_room_and_bass_plans_share_poses_and_summed_regime(program, size, purpose) -> None:
    cloud = mp.program(program, "cloud")
    quick = mp.program(program, size)

    assert cloud.poses is mp.program("seat", "cloud").poses
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in quick.poses] == [
        (0, 0), (-20, 0), (20, 0),
    ]
    assert {row.purpose for row in (cloud, quick)} == {purpose}
    assert {row.regime for row in (cloud, quick)} == {mp.REGIME_SUMMED}
    assert mp.gate_exemption(cloud.purpose) == SEAT_EXEMPT


@pytest.mark.parametrize("size,poses", [
    ("express", [(0, 2), (-20, 1), (20, 1)]),
    ("wide", [(0, 2), (-20, 1), (20, 1), (-45, 1), (45, 1)]),
])
def test_rear_layouts_pin_no_mover_and_repeat_the_zero_pose(size, poses) -> None:
    """The arm is a temporary convenience; a rear layout never pins one (issue #5330)."""
    row = mp.program("rear", size)

    assert row.purpose == mp.PURPOSE_REAR and row.regime == mp.REGIME_SUMMED
    assert row.mover is None
    assert [(pose.azimuth_deg, pose.repeats) for pose in row.poses] == poses


def test_rear_gate_exemption_matches_room() -> None:
    """A rear comparison reads below the gate's trusted floor, same as room (issue #5330)."""
    assert mp.gate_exemption(mp.PURPOSE_REAR) == mp.gate_exemption(mp.PURPOSE_ROOM) == SEAT_EXEMPT


def test_rear_summed_capture_is_valid_but_per_driver_is_refused() -> None:
    assert mp.validated_capture_purpose(mp.PURPOSE_REAR, mp.POSE_KIND_BEARING, mp.REGIME_SUMMED) == mp.PURPOSE_REAR
    with pytest.raises(ValueError):
        mp.validated_capture_purpose(mp.PURPOSE_REAR, mp.POSE_KIND_BEARING, mp.REGIME_PER_DRIVER)


@pytest.mark.parametrize("mover", [None, "arm", "human"])
@pytest.mark.parametrize("sections", [{"rear_calibration"}, {"rear_calibration", "room"}, {"rear_calibration", "bass", "room"}])
def test_rear_calibration_trials_the_rear_row_for_every_mover(sections, mover) -> None:
    """The rear section trials first, ahead of bass and room, with no arm-only layout."""
    selected = mp.trial_program(sections, mover)

    assert (selected.purpose, selected.layout) == (mp.PURPOSE_REAR, "rear_express")


def test_run_program_resolves_rear_layouts_and_custom_bearings() -> None:
    assert (mp.run_program("rear").program_id, mp.run_program("rear").size) == ("rear", "express")
    assert (mp.run_program("rear", "rear/wide").program_id, mp.run_program("rear", "rear/wide").size) == ("rear", "wide")
    custom = mp.run_program("rear", "0,-45,45")
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in custom.poses] == [(0, 0), (-45, 0), (45, 0)]
    assert (custom.purpose, custom.regime) == (mp.PURPOSE_REAR, mp.REGIME_SUMMED)


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
    pose = config["layouts"]["room_quick"]["poses"][0]  # type: ignore[index]
    pose.update({"headline": "Measure the main seat", "detail": "Hold the mic at ear height."})

    programs = mp.load_programs(_write_config(tmp_path, config))

    loaded = programs[("room", "arm")].poses[0]
    assert (loaded.headline, loaded.detail) == (
        "Measure the main seat", "Hold the mic at ear height.",
    )


@pytest.mark.parametrize("broken", ["empty", "repeats", "purpose", "regime", "mode", "layout_key", "mover", "room_sweep", "room_sweep_mode"])
def test_malformed_config_is_rejected(tmp_path: Path, broken: str) -> None:
    config = _bundled_config()
    if broken == "empty":
        config["layouts"]["room_quick"] = []  # type: ignore[index]
    elif broken == "repeats":
        config["layouts"]["room_quick"]["poses"][0]["repeats"] = 0  # type: ignore[index]
    elif broken == "layout_key":
        config["layouts"]["room_quick"]["moverr"] = "arm"  # type: ignore[index]
    elif broken == "mover":
        config["layouts"]["room_quick"]["mover"] = []  # type: ignore[index]
    elif broken == "room_sweep":
        config["programs"][0]["room_sweep"] = "yes"
    elif broken == "room_sweep_mode":
        config["programs"][0].update(purpose="room", regime="summed", room_sweep=True)
    elif broken == "purpose":
        config["programs"][0]["purpose"] = "other"  # type: ignore[index]
    elif broken == "regime":
        config["programs"][0]["regime"] = "other"  # type: ignore[index]
    else:
        config["programs"][0].update({"purpose": "room", "regime": "per_driver"})  # type: ignore[index]

    with pytest.raises(ValueError):
        mp.load_programs(_write_config(tmp_path, config))


@pytest.mark.parametrize("layout,ceiling", [("bass_axis", 1100), ("bass_nearfield", 1200), ("room_quick", 1100)])
def test_run_uses_the_matching_purposes_stimulus(tmp_path, monkeypatch, layout, ceiling):
    config = _bundled_config()
    config["stimuli"]["near"] = {"ceiling_hz": 1200}
    next(row for row in config["programs"] if row["id"] == "bass" and row["size"] == "nearfield")["stimulus"] = "near"
    monkeypatch.setattr(mp, "_PROGRAMS", mp.load_programs(_write_config(tmp_path, config)))
    assert mp.run_program("bass", layout).stimulus == {"ceiling_hz": ceiling}


@pytest.mark.parametrize("stimuli,reference", [
    ([], "bass"), ({"bass": []}, "bass"), ({"bass": {"band_hz": [20, 1100]}}, "bass"),
    ({"bass": {"ceiling_hz": 1100}}, "missing"), ({"bass": {"ceiling_hz": True}}, "bass"),
    ({"bass": {"ceiling_hz": 0}}, "bass"), ({"bass": {"ceiling_hz": float("inf")}}, "bass"),
])
def test_invalid_registry_stimulus_is_a_value_error(tmp_path, stimuli, reference):
    config = _bundled_config()
    config["stimuli"] = stimuli
    next(row for row in config["programs"] if row["id"] == "bass")["stimulus"] = reference
    with pytest.raises(ValueError):
        mp.load_programs(_write_config(tmp_path, config))
