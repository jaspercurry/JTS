# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior."""

from __future__ import annotations

import json
import re
from dataclasses import MISSING, fields, replace
from importlib import import_module
from pathlib import Path

import pytest
from tests.test_plan_run import banked_program_baselines  # noqa: F401

from jasper.active_speaker import baseline_record
from jasper.active_speaker import measurement_programs as mp, baseline_profile as bp, commissioning_coordinator as cc
from jasper.active_speaker import measured_crossover_candidate as mc, measurement_emit as me, tuning_handoff as th
from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.capture_schedule import prepare_plan_captures
from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
from jasper.active_speaker.candidate_bank import BankedCandidate
from jasper.active_speaker.candidate_parts import compose_candidate
from jasper.active_speaker.crossover_v2 import prescription_document as pd, prescription_contract as pc
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.active_speaker_fixtures import mono_output_topology
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW, BOOKKEEPING_ORDER, bookkeeping_views
from jasper.audio_measurement.gating import NEAR_FIELD_EXEMPT, SEAT_EXEMPT
from jasper.audio_measurement.piston import NEAR_FIELD_MAX_DISTANCE_M
from jasper.cli import round as round_cli


@pytest.mark.parametrize("actual,expected", [
    pytest.param(pd._JUDGE_ORDER, ("topology", "blend", "alignment", "room", "bass", "rear_calibration", "driver"), id="judge"),
    pytest.param(tuple(pd._PREVIEW_ROWS), ("rear_calibration", "room", "emitted_graph"), id="preview"),
    pytest.param(tuple(pc.prescription_contracts()), ("speaker", "room", "bass", "rear"), id="contracts"),
    pytest.param(tuple(section.name for section in mp.PRESCRIPTION_SECTIONS if section.compose),
                 ("driver", "blend", "topology", "room", "bass", "rear_calibration"), id="compose-with-rear"),
    pytest.param(tuple(section.name for section in mp.PRESCRIPTION_SECTIONS if section.compose and section.name != "rear_calibration"),
                 ("driver", "blend", "topology", "room", "bass"), id="compose-without-rear"),
    pytest.param(mp.RUNNABLE_PROGRAMS, ("speaker", "rear", "bass", "room"), id="runnable"),
    pytest.param(tuple(pd.SECTION_KINDS.items()), (
        ("driver", "jts_crossover_driver_prescription"), ("blend", "jts_crossover_blend_prescription"),
        ("alignment", "jts_crossover_alignment_prescription"), ("topology", "jts_crossover_topology_prescription"),
        ("room", "jts_room_prescription"), ("bass", None), ("rear_calibration", "jts_rear_calibration"),
    ), id="section-kinds"),
])
def test_program_projections_preserve_document_order(actual, expected):
    assert actual == expected


@pytest.mark.parametrize("site", [
    "purposes", "runnable", "regimes", "sections", "kinds", "kind_constants", "judge", "preview",
    "contracts", "compose", "optional_types", "typed_fields", "snapshot", "applied", "applied_names",
    "handoff", "measure", "graph",
])
def test_program_table_projections(site):
    rows = mp.PROGRAM_ROWS
    ordered = sorted(rows, key=lambda row: row.purpose_order)
    sections = sorted((section for row in rows for section in row.sections), key=lambda section: section.document_order)
    candidate_fields = [field for row in ordered for field in row.candidate_fields]
    candidate = _candidate()
    composed = compose_candidate(BankedCandidate(candidate, "", "", Path("candidate.json")), base_profile={},
                                 sections={section.name: None for section in sections if section.reset})
    snapshot = baseline_record.recomposition_snapshot_for(candidate, design_draft={}, declaration=me.MeasurementGraphProfile(
        candidate.source_preset, mono_output_topology(), {}, "null"))
    snapshot_header = {"schema_version", "domain", "topology_id", "topology_fingerprint", "preset", "corrections",
                       "driver_protection", "playback_device", "measured_candidate_fingerprint"}
    actual, expected = {
        "purposes": (mp.PURPOSES, tuple(name for _, name in sorted(
            [(row.purpose_order, row.purpose) for row in rows] + [(3, mp.PURPOSE_REFERENCE)]))),
        "runnable": (mp.RUNNABLE_PROGRAMS, tuple(row.purpose for row in rows)),
        "regimes": ([(name, value) for name, value in mp._REGIMES_BY_PURPOSE.items() if name != mp.PURPOSE_REFERENCE],
                    [(row.purpose, row.regimes) for row in ordered]),
        "sections": ([mp.prescription_sections(purpose) for purpose in (None, *(row.purpose for row in rows))],
                     [tuple(section.name for row in rows if purpose is None or row.purpose == purpose
                            for section in row.sections if section.reset) for purpose in (None, *(row.purpose for row in rows))]),
        "kinds": (list(pd.SECTION_KINDS.items()), [(section.name, section.kind) for section in sections]),
        "kind_constants": ([pd.driver.DRIVER_PRESCRIPTION_KIND, pd.blend.PRESCRIPTION_KIND,
                            pd.alignment.ALIGNMENT_PRESCRIPTION_KIND, pd.topology.TOPOLOGY_PRESCRIPTION_KIND,
                            pd.room.ROOM_PRESCRIPTION_KIND, None, pd.rear_calibration.KIND], [section.kind for section in sections]),
        "judge": (pd._JUDGE_ORDER, tuple(section.name for section in sorted(sections, key=lambda section: section.judge_order))),
        "preview": (list(pd._PREVIEW_ROWS.items()), [(kind, set(names)) for _, kind, names in sorted(row.preview for row in rows if row.preview)]),
        "contracts": ((pc.SECTIONS, tuple(pc.prescription_contracts())), (tuple(row.purpose for row in ordered),) * 2),
        "compose": (list(composed.analysis["resolution"]), sorted([section.name for section in sections if section.compose] + ["alignment"])),
        "optional_types": (list(mc._OPTIONAL_FIELD_TYPES.items()), [(field.name, field.type) for field in candidate_fields]),
        "typed_fields": ([field.name for field in fields(mc.MeasuredCrossoverCandidate) if field.init and field.name != "alignment"
                          and (field.default is not MISSING or field.default_factory is not MISSING)],
                         [field.name for field in candidate_fields]),
        "snapshot": ([name for name in snapshot if name not in snapshot_header], [field.name for field in candidate_fields if field.snapshot]),
        "applied": (list(bp.applied_layers(None).items()), [(row.purpose, False) for row in ordered]),
        "applied_names": (list(bp.applied_layer_names(None).items()), [(row.applied_name, False) for row in ordered]),
        "handoff": (th.PROGRAM_ENTRIES, tuple({"id": row.purpose, "title": row.title, "description": row.description} for row in rows)),
        "measure": (list(cc._MEASURE_LABELS.items()), [(row.purpose, row.measure_label) for row in rows]),
        "graph": (list(me.measurement_graph_evidence(scope="candidate", candidate=candidate)),
                  [row.candidate_fields[0].name for row in ordered if row.graph_evidence]),
    }[site]
    assert actual == expected


@pytest.mark.parametrize(("preset", "layout", "poses", "moves", "captures"), [
    ("speaker/mark", "speaker_mark", 1, 1, 3),
    ("speaker/mark", "baseline_full", 13, 13, 29),
    ("speaker/mark", "baseline_express", 5, 5, 13),
    ("tournament/express", "tournament_full", 3, 3, 3),
    ("tournament/express", "tournament_express", 1, 1, 1),
    ("room/seat", "seat_cloud", 11, 11, 11),
    ("room/seat", "seat_cube", 7, 7, 7),
    ("room/seat", "seat_express", 3, 3, 3),
    ("room/seat", "room_quick", 3, 3, 3),
    ("rear/seat", "seat_express", 3, 3, 3),
    ("rear/pair", "speaker_mark", 1, 1, 2),
    ("bass/axis", "bass_axis", 1, 1, 1),
])
def test_shipped_rows(preset: str, layout: str, poses: int, moves: int, captures: int) -> None:
    row = mp.run_program(preset, layout)

    assert (f"{row.program_id}/{row.size}", row.layout) == (preset, layout)
    assert len(row.poses) == poses
    assert row.mic_move_count == moves
    assert row.capture_count == captures
    assert row.room_sweep is (preset == "speaker/mark")


@pytest.mark.parametrize("program_id,size", mp.available_programs())
def test_shipped_run_purposes(program_id, size):
    row = mp.program(program_id, size)
    expected = ("rear", "room") if (program_id, size) == ("rear", "seat") else (row.purpose,)
    assert mp.run_purposes(f"{program_id}/{size}") == expected
    assert mp.run_purpose(f"{program_id}/{size}") == expected[0]
    assert row.co_purposes == expected[1:]


@pytest.mark.parametrize("name", ["rear", "rear/custom", "speaker/express", "reference", "", "bass/nearfield"])
def test_run_purposes_preserves_primary_identity_without_a_registry_row(name):
    assert mp.run_purposes(name) == (mp.run_purpose(name),) == (name.partition("/")[0],)


@pytest.mark.parametrize("purpose", ["room", "bass"])
def test_summed_bookkeeping_includes_one_frequency_image(purpose):
    assert ("frequency", False, False) in bookkeeping_views(purpose)


def test_speaker_bookkeeping_uses_room_views_when_the_round_holds_room_sweeps():
    assert bookkeeping_views("speaker", has_room=True) == tuple(
        row for row in bookkeeping_views("room") if row[0] != "frequency")


@pytest.mark.parametrize(("purpose", "has_room", "expected"), [
    ("speaker", False, (("inventory", True, False),)),
    ("speaker", True, (("room", True, False), ("room-grade", True, True), ("inventory", True, False))),
    ("room", False, (("room", True, False), ("room-grade", True, True), ("frequency", False, False),
                     ("inventory", True, False))),
    ("bass", False, (("bass", True, False), ("frequency", False, False), ("inventory", True, False))),
    ("reference", False, ()),
    ("rear", False, (("rear", False, False), ("frequency", False, False),
                     ("inventory", True, False))),
])
def test_the_view_table_answers_every_automatic_view(purpose, has_room, expected):
    assert bookkeeping_views(purpose, has_room=has_room) == expected
    assert {name for name, row in ARTIFACT_BY_VIEW.items() if row.builder} == set(BOOKKEEPING_ORDER)
    for view, _, _ in expected:
        row = ARTIFACT_BY_VIEW[view]
        module, _, builder = row.builder.rpartition(".")
        assert callable(getattr(import_module(f".{module}", "jasper.active_speaker"), builder))


def test_rear_co_purpose_banks_the_room_views_in_order():
    assert bookkeeping_views("rear", co_purposes=("room",)) == tuple(
        (name, row.per_set, row.grades_against_base) for name in BOOKKEEPING_ORDER
        if {"room", "rear"}.intersection((row := ARTIFACT_BY_VIEW[name]).bookkeeping))


@pytest.mark.parametrize("preset,expected", [("rear/seat", ("room",)), ("room/seat", ()), ("bass/axis", ())])
def test_a_seat_layout_carries_its_presets_co_purposes(preset, expected):
    assert mp.run_program(preset, "seat_express").co_purposes == expected


@pytest.mark.parametrize("preset,pair", [("branches/express", "drivers"), ("front_rear/express", "front_rear")])
def test_a_branch_preset_is_a_speaker_run_that_keeps_its_pair(preset, pair):
    row = mp.run_program(preset)

    assert (row.purpose, row.regime, row.branch_pair, row.room_sweep) == (
        mp.PURPOSE_SPEAKER, mp.REGIME_BRANCHES, pair, False)


@pytest.mark.parametrize("preset,layout,mover", [
    ("room/seat", "room_quick", "arm"),
    ("bass/axis", "room_quick", "arm"),
    ("rear/seat", "seat_express", "human"),
    ("room/seat", "seat_express", "human"),
    ("rear/pair", "speaker_mark", None),
])
def test_a_layout_runs_under_its_preset_with_its_own_mover(preset, layout, mover):
    row, default = mp.run_program(preset, layout), mp.run_program(preset)
    assert (f"{row.program_id}/{row.size}", row.layout, row.mover) == (preset, layout, mover)
    assert (row.purpose, row.regime, row.branch_pair, row.co_purposes) == (
        default.purpose, default.regime, default.branch_pair, default.co_purposes)


@pytest.mark.parametrize("preset,layout", [("speaker", "seat_cloud"), ("rear/express", "speaker_mark"), ("room", "bass_axis")])
def test_a_layout_its_preset_does_not_offer_refuses_by_name(preset, layout):
    with pytest.raises(mp.LayoutNotOfferedError) as excinfo:
        mp.run_program(preset, layout)
    default = mp.run_program(preset)
    assert (excinfo.value.reason, excinfo.value.detail) == (mp.LAYOUT_NOT_OFFERED, {
        "preset": f"{default.program_id}/{default.size}", "layout": layout, "offered": list(default.layouts)})


def test_a_layout_passed_as_poses_refuses_by_name():
    with pytest.raises(mp.PosesNameALayoutError) as excinfo:
        mp.run_program("room", poses="seat_express")
    assert (excinfo.value.reason, excinfo.value.detail) == (
        mp.POSES_NAME_A_LAYOUT, {"poses": "seat_express", "use": "--layout"})


def test_the_bass_handoff_names_a_layout_a_person_can_walk():
    """The default bass layout pins the arm, so the prompt names the hand one (#5632 F4)."""
    preset, layout, mover = re.search(r"--program (\S+) --layout (\S+) --mover (\S+)",
                                      th.build_tuning_handoff_prompt({}, "bass")).groups()
    assert (mp.run_program(preset).mover, mp.run_program(preset, layout).mover, mover) == ("arm", "human", "human")


def test_the_rear_handoff_names_the_pair_model_its_previews_read():
    """The seat loop previews rear documents against the front/rear pair take (#5632 F11)."""
    preset, layout = re.search(r"--program (\S+) --layout (\S+) --wait", th.build_tuning_handoff_prompt({}, "rear")).groups()
    row = mp.run_program(preset, layout)
    assert (row.regime, row.branch_pair) == (mp.REGIME_BRANCHES, mp.BRANCH_PAIR_FRONT_REAR)


def test_run_help_names_every_registry_pose_set(capsys):
    """The hand-off sends the agent to ``jasper-round run --help`` for the plans (#5632 F11)."""
    with pytest.raises(SystemExit):
        round_cli.main(["run", "--help"])
    words = set(re.split(r"[\s,()]+", capsys.readouterr().out))
    assert {f"{name}/{size}" for name, size in mp.available_programs()} <= words


def test_express_geometry() -> None:
    row = mp.run_program("speaker", "baseline_express")

    assert {p.azimuth_deg for p in row.poses} == {0, -20, 20}
    assert {p.elevation_deg for p in row.poses} == {0, -10, 10}
    assert [
        p.repeats for p in row.poses if (p.azimuth_deg, p.elevation_deg) == (0, 0)
    ] == [mp.run_program("speaker", "baseline_full").poses[0].repeats]


@pytest.mark.parametrize("program_id,size", [("baseline", "medium"), ("tournament", "medium"), ("spot", "express"), ("", "")])
def test_unknown_lookup_names_the_valid_choices(program_id: str, size: str) -> None:
    lookups = [lambda: mp.program(program_id, size)]
    if program_id:
        lookups.append(lambda: mp.run_purposes(f"{program_id}/{size}"))
    for lookup in lookups:
        with pytest.raises(mp.UnknownProgramError) as excinfo:
            lookup()
        assert excinfo.value.choices == mp.available_programs()
        assert (excinfo.value.program_id, excinfo.value.size) == (program_id, size)


def test_available_programs_is_the_sorted_registry() -> None:
    choices = mp.available_programs()

    assert choices == (
        ("bass", "axis"), ("branches", "express"), ("drivers", "each"), ("front_rear", "express"),
        ("nearfield", "each"), ("rear", "express"), ("rear", "pair"), ("rear", "seat"), ("room", "seat"),
        ("speaker", "mark"), ("tournament", "express"),
    )
    rows = [mp.program(program_id, size) for program_id, size in choices]
    assert tuple((row.program_id, row.size) for row in rows) == choices
    assert {(row.program_id, row.branch_pair) for row in rows
            if row.regime == mp.REGIME_BRANCHES} == {
        ("branches", mp.BRANCH_PAIR_DRIVERS), ("front_rear", mp.BRANCH_PAIR_FRONT_REAR),
        ("rear", mp.BRANCH_PAIR_FRONT_REAR),
    }



_WOOFER_STEP = (("woofer", 0.015), ("woofer", 0.03))
_TWO_WAY, _CARDIOID = ("tweeter", "woofer"), ("tweeter", "woofer", "woofer:rear")
_NEARFIELD, _DRIVERS = mp.run_program("nearfield"), mp.run_program("drivers")


@pytest.mark.parametrize("row,targets,driver,walked", [
    (_NEARFIELD, _TWO_WAY, "", _WOOFER_STEP),
    (_NEARFIELD, _CARDIOID, "", (*_WOOFER_STEP, *(("woofer:rear", distance) for _, distance in _WOOFER_STEP))),
    (_NEARFIELD, (), "", _WOOFER_STEP),
    (_NEARFIELD, _CARDIOID, "woofer:rear", tuple(("woofer:rear", distance) for _, distance in _WOOFER_STEP)),
    (_DRIVERS, _CARDIOID, "", (("woofer", None), ("woofer:rear", None), ("tweeter", None))),
    (_DRIVERS, _CARDIOID, "tweeter", (("tweeter", None),)),
    (replace(_DRIVERS, poses=tuple(mp.ProgramPose(0, 0, driver=driver) for driver in ("woofer:rear", "tweeter"))),
     _CARDIOID, "", (("woofer:rear", None), ("tweeter", None))),
    (mp.run_program("nearfield", poses='[{"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", '
                                       '"distance_m": 0.02, "driver": "woofer"}]'), _CARDIOID, "", (("woofer", 0.02),)),
])
def test_a_presets_driver_role_plays_each_declared_output(row, targets, driver, walked) -> None:
    """A named layout's pose that names a bare driver role plays each declared output of
    it, one output's placements after the other's; a pose naming one output, or an inline
    pose, plays what it names; an undeclared role keeps its name for preflight to refuse;
    --driver narrows to one output (ADR-0366 §6)."""
    request = ac.request_for_program(row, targets=targets, driver=driver)
    assert tuple((stop.driver, stop.distance_m) for stop in request.stops) == walked


@pytest.mark.parametrize("row,targets,driver,offered", [
    (_NEARFIELD, _TWO_WAY, "woofer:rear", ["woofer"]),
    (_NEARFIELD, _CARDIOID, "tweeter", ["woofer", "woofer:rear"]),
    (mp.run_program("speaker"), _CARDIOID, "woofer", []),
])
def test_a_driver_the_preset_does_not_play_alone_refuses_naming_the_ones_it_does(row, targets, driver, offered) -> None:
    with pytest.raises(mp.DriverNotOfferedError) as excinfo:
        mp.plan_poses(row, targets, driver)
    assert (excinfo.value.reason, excinfo.value.detail) == (
        mp.DRIVER_NOT_OFFERED, {"preset": row.preset_id, "driver": driver, "offered": offered})


@pytest.mark.parametrize("program_id,layout,poses,resolved", [
    ("nearfield", None, None, ("nearfield_woofer", _WOOFER_STEP)),
    ("nearfield", None, '[{"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.012, "driver": "woofer:rear"}]',
     ("custom", (("woofer:rear", 0.012),))),
    ("speaker", "nearfield_woofer", None, None),
    ("nearfield", None, "0,10", None),
])
def test_a_near_field_run_resolves_as_reference_evidence(program_id, layout, poses, resolved):
    """A near-field run names its program: a bundled row or an inline pose list
    resolves as reference near-field evidence and banks under that purpose;
    a driver's pose under another program, or a bearing under this one, is
    refused (ADR-0360)."""
    if resolved is None:
        with pytest.raises(ValueError):
            mp.run_program(program_id, layout, poses)
        return
    row = mp.run_program(program_id, layout, poses)
    assert (row.layout, tuple((pose.driver, pose.distance_m) for pose in row.poses)) == resolved
    assert (row.purpose, row.regime, mp.run_purpose(f"{row.program_id}/{row.size}")) == (
        mp.PURPOSE_REFERENCE, mp.REGIME_NEAR_FIELD, mp.PURPOSE_REFERENCE)


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
        ((_seat(0.0, 0.0, 0.0), _seat(0.30, 0.0, 0.0, 2)), 2, 3),
    ],
)
def test_counts_split_moves_from_captures(
    poses: tuple[object, ...], moves: int, captures: int
) -> None:
    row = mp.MeasurementProgram(program_id="t", size="t", poses=poses)

    assert row.mic_move_count == moves
    assert row.capture_count == captures


def test_the_seat_cube_is_the_head_and_six_face_centres() -> None:
    cube = mp.run_program("room", "seat_cube")
    express = mp.run_program("room", "seat_express")

    assert {p.kind for p in cube.poses} == {mp.POSE_KIND_SEAT}
    assert {(p.azimuth_deg, p.elevation_deg, p.repeats) for p in cube.poses} == {(0, 0, 1)}
    assert [p.seat_offset_m for p in cube.poses] == [
        (0.0, 0.0, 0.0),
        (0.30, 0.0, 0.0), (-0.30, 0.0, 0.0),
        (0.0, 0.30, 0.0), (0.0, -0.30, 0.0),
        (0.0, 0.0, 0.30), (0.0, 0.0, -0.30),
    ]
    assert [p.seat_offset_m for p in express.poses] == [
        (0.0, 0.0, 0.0), (0.30, 0.0, 0.0), (0.0, 0.30, 0.0),
    ]
    assert {p.seat_offset_m for p in express.poses} <= {
        p.seat_offset_m for p in cube.poses
    }


def test_seat_cloud_walks_three_rows_then_above_and_below_the_head() -> None:
    cloud = mp.run_program("room", "seat_cloud")

    assert {p.kind for p in cloud.poses} == {mp.POSE_KIND_SEAT}
    assert {(p.azimuth_deg, p.elevation_deg, p.repeats) for p in cloud.poses} == {(0, 0, 1)}
    assert [p.seat_offset_m for p in cloud.poses] == [
        (-0.30, 0.30, 0.0), (0.0, 0.30, 0.0), (0.30, 0.30, 0.0),
        (-0.30, 0.0, 0.0), (0.0, 0.0, 0.0), (0.30, 0.0, 0.0),
        (-0.30, -0.30, 0.0), (0.0, -0.30, 0.0), (0.30, -0.30, 0.0),
        (0.0, 0.0, 0.30), (0.0, 0.0, -0.30),
    ]


#: The registry ids at ``2eeeaf4be``, before the fold, that a banked round still reads
#: as its purpose (ADR-0366 §6); the folded near-field rows no longer read (#2902).
_BANKED_BEFORE_THE_FOLD = {
    "speaker/mark": "speaker", "baseline/full": "speaker", "baseline/express": "speaker",
    "tournament/full": "speaker", "tournament/express": "speaker", "branches/express": "speaker",
    "front_rear/express": "speaker", "rear/express": "rear", "rear/seat": "rear", "rear/wide": "rear",
    "rear/behind": "rear", "rear/pair": "rear", "rear/pair_mark": "rear", "rear/pair_behind": "rear",
    "seat/cloud": "room", "seat/cube": "room", "seat/express": "room", "room/cloud": "room",
    "room/arm": "room", "room/seat": "room", "bass/axis": "bass", "bass/cloud": "bass", "bass/quick": "bass",
    "bass/nearfield": "bass", "close/spot": "reference",
}


@pytest.mark.parametrize("banked,purpose", [
    *_BANKED_BEFORE_THE_FOLD.items(),
    *sorted({(f"{banked.partition('/')[0]}/custom", purpose) for banked, purpose in _BANKED_BEFORE_THE_FOLD.items()}),
])
def test_every_id_banked_before_the_fold_still_reads_as_its_purpose(banked: str, purpose: str) -> None:
    assert mp.run_purpose(banked) == purpose


def test_no_preset_id_repeats_or_reuses_a_retired_id() -> None:
    ids = [f"{row['id']}/{row['size']}" for row in _bundled_config()["programs"]]  # type: ignore[index]
    assert len(ids) == len(set(ids))
    assert not set(ids) & set(mp.RETIRED_PROGRAMS)


@pytest.mark.parametrize("via", ["program", "poses"])
@pytest.mark.parametrize("retired", sorted(mp.RETIRED_PROGRAMS))
def test_a_retired_id_refuses_a_new_run_by_name_and_names_a_live_replacement(retired: str, via: str) -> None:
    """A retired id leaves the registry; a round banked under it still reads, and a
    new run naming it is told what replaces it (ADR-0366 §6)."""
    with pytest.raises(mp.RetiredProgramError) as excinfo:
        mp.run_program(retired) if via == "program" else mp.run_program("speaker", poses=retired)
    replacement = mp.RETIRED_PROGRAMS[retired]
    assert (excinfo.value.reason, excinfo.value.detail) == (mp.PROGRAM_RETIRED, {"retired": retired, **replacement._asdict()})
    assert replacement.purpose == mp.run_purpose(retired)
    if replacement.preset:
        run = mp.run_program(replacement.preset, None if replacement.layout == mp.CUSTOM_SIZE else replacement.layout)
        assert run.purpose == replacement.purpose


def test_configured_defaults_preserve_existing_cli_choices_and_add_room() -> None:
    assert {
        program_id: mp.program(program_id).size
        for program_id in ("tournament", "branches", "room", "rear")
    } == {
        "tournament": "express",
        "branches": "express",
        "room": "seat",
        "rear": "express",
    }


@pytest.mark.parametrize("program,purpose", [("room", mp.PURPOSE_ROOM), ("bass", mp.PURPOSE_BASS)])
def test_room_and_bass_plans_share_poses_and_summed_regime(program, purpose) -> None:
    cloud = mp.run_program(program, "seat_cloud")
    quick = mp.run_program(program, "room_quick")

    assert cloud.poses is mp.run_program("room", "seat_cloud").poses
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in quick.poses] == [
        (0, 0), (-20, 0), (20, 0),
    ]
    assert {row.purpose for row in (cloud, quick)} == {purpose}
    assert {row.regime for row in (cloud, quick)} == {mp.REGIME_SUMMED}
    assert mp.gate_exemption(cloud.purpose) == SEAT_EXEMPT


@pytest.mark.parametrize("layout,poses", [
    ("rear_express", [(0, 2), (-20, 1), (20, 1)]),
    ("rear_wide", [(0, 2), (-20, 1), (20, 1), (-45, 1), (45, 1)]),
])
def test_rear_layouts_pin_no_mover_and_repeat_the_zero_pose(layout, poses) -> None:
    row = mp.run_program("rear", layout)

    assert row.purpose == mp.PURPOSE_REAR and row.regime == mp.REGIME_SUMMED
    assert row.mover is None
    assert [(pose.azimuth_deg, pose.repeats) for pose in row.poses] == poses


@pytest.mark.parametrize("purpose,driver,distance_m,reason", [
    (mp.PURPOSE_REAR, "", None, SEAT_EXEMPT),
    (mp.PURPOSE_ROOM, "", None, SEAT_EXEMPT),
    (mp.PURPOSE_REFERENCE, "woofer:rear", 0.015, NEAR_FIELD_EXEMPT),
    (mp.PURPOSE_REFERENCE, "woofer", NEAR_FIELD_MAX_DISTANCE_M, NEAR_FIELD_EXEMPT),
    (mp.PURPOSE_REFERENCE, "woofer", 0.5, None),
    (mp.PURPOSE_REFERENCE, "", 0.03, None),
])
def test_a_take_reads_ungated_for_the_room_or_within_one_drivers_near_field(purpose, driver, distance_m, reason) -> None:
    """A rear comparison reads below the gate's trusted floor, same as room
    (issue #5330); a pose at one driver reads ungated only within the
    near-field distance, so a far-field one-driver take is gated (ADR-0366)."""
    assert mp.gate_exemption(purpose, driver=driver, distance_m=distance_m) == reason


@pytest.mark.parametrize("purpose,regime,supported", [
    (mp.PURPOSE_REAR, mp.REGIME_SUMMED, True),
    (mp.PURPOSE_REAR, mp.REGIME_BRANCHES, True),
    (mp.PURPOSE_REAR, mp.REGIME_PER_DRIVER, False),
    (mp.PURPOSE_REAR, mp.REGIME_NEAR_FIELD, False),
    (mp.PURPOSE_ROOM, mp.REGIME_BRANCHES, False),
    (mp.PURPOSE_SPEAKER, mp.REGIME_BRANCHES, True),
])
def test_only_rear_joins_speaker_in_the_branches_regime(purpose, regime, supported) -> None:
    if supported:
        assert mp.validated_capture_purpose(purpose, mp.POSE_KIND_BEARING, regime) == purpose
    else:
        with pytest.raises(ValueError):
            mp.validated_capture_purpose(purpose, mp.POSE_KIND_BEARING, regime)


def _program_with(purpose, regime, kind, distance_m, driver):
    return mp.MeasurementProgram("t", "t", (mp.ProgramPose(0, 0, kind=kind, distance_m=distance_m, driver=driver),),
                                 purpose=purpose, regime=regime)


def _stop_with(purpose, regime, kind, distance_m, driver):
    return ac.AngleStop(0, regime, kind=kind, distance_m=distance_m, purpose=purpose, driver=driver)


@pytest.mark.parametrize("door,refusal", [(_program_with, ValueError), (_stop_with, CrossoverV2FlowError)])
@pytest.mark.parametrize("purpose,regime,kind,distance_m,driver,accepted", [
    (mp.PURPOSE_REFERENCE, mp.REGIME_NEAR_FIELD, mp.POSE_KIND_CLOSE, 0.015, "woofer:rear", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_NEAR_FIELD, mp.POSE_KIND_CLOSE, 0.15, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_NEAR_FIELD, mp.POSE_KIND_BEHIND, 0.5, "woofer:rear", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "tweeter", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.015, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_BRANCHES, mp.POSE_KIND_BEARING, None, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.3, "", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_NEAR_FIELD, mp.POSE_KIND_CLOSE, 0.015, "", False),
    (mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "", False),
    (mp.PURPOSE_SPEAKER, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "woofer", False),
    (mp.PURPOSE_BASS, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.03, "woofer", False),
])
def test_only_a_reference_pose_names_its_driver_on_any_regime_kind_or_distance(
    door, refusal, purpose, regime, kind, distance_m, driver, accepted,
) -> None:
    """Every door a pose enters through judges its driver the same way: a
    program row or layout, and a stop in a hand-written plan. A reference pose
    names one at any regime, kind and distance, and must on a regime that plays
    drivers; no tuning purpose names one yet (ADR-0366)."""
    if accepted:
        door(purpose, regime, kind, distance_m, driver)
    else:
        with pytest.raises(refusal):
            door(purpose, regime, kind, distance_m, driver)


@pytest.mark.parametrize("candidates,accepted", [((), True), (("base",), True), (("base", "fp-a"), False)])
def test_a_near_field_run_measures_no_candidate(candidates, accepted) -> None:
    """A driver's pose plays the neutral drivers graph, so ``--candidates``
    cannot stamp its takes with a candidate or repeat its poses (ADR-0360)."""
    if accepted:
        row = mp.program("nearfield")
        assert len(ac.request_for_program(row, candidates=candidates).stops) == len(row.poses)
    else:
        with pytest.raises(CrossoverV2FlowError):
            ac.request_for_program(mp.program("nearfield"), candidates=candidates)


@pytest.mark.parametrize("stops,expected", [
    (mp.run_program("drivers"), [("lateral", ("woofer",)), ("lateral", ("woofer:rear",)), ("lateral", ("tweeter",))]),
    ((ac.AngleStop(0, mp.REGIME_PER_DRIVER, purpose=mp.PURPOSE_SPEAKER),
      ac.AngleStop(0, mp.REGIME_PER_DRIVER, purpose=mp.PURPOSE_REFERENCE, driver="woofer")),
     [("check", ()), ("entry_baseline", ()), ("measure", ()), ("lateral", ("woofer",))]),
], ids=["one-driver-preset", "beside-a-two-driver-stop"])
def test_a_stop_naming_its_driver_skips_what_plays_every_driver(stops, expected) -> None:
    """A stop naming its driver plays it alone in the far field on MEASURE's
    sweep, with no CHECK or timing take for it; a stop that plays every driver
    keeps both (#5696, ADR-0366)."""
    request = (ac.request_for_program(stops, targets=_CARDIOID) if isinstance(stops, mp.MeasurementProgram)
               else ac.AngleCaptureRequest(stops=stops))
    captures = prepare_plan_captures(request)
    assert [(capture.spec.program_phase, capture.spec.branch_target_ids) for capture in captures] == expected
    assert {capture.spec.regime for capture in captures if capture.stop.driver} == {"reference_axis"}


@pytest.mark.parametrize("purpose,base,cleared", [
    (mp.PURPOSE_BASS, True, ("room_correction", "bass_extension")),
    (mp.PURPOSE_BASS, False, ("room_correction",)),
    (mp.PURPOSE_SPEAKER, True, ()), (mp.PURPOSE_ROOM, True, ()), (mp.PURPOSE_REAR, False, ()),
    (mp.PURPOSE_REFERENCE, True, ()), (None, True, ()),
])
def test_a_purpose_row_declares_the_applied_layers_its_takes_clear(purpose, base, cleared) -> None:
    """A bass take plays the applied speaker layer with room off, and its base
    plays bass off too; every other purpose plays its layers as composed (ADR-0370)."""
    assert mp.cleared_layers(purpose, base=base) == cleared


def test_the_rear_pair_row_reuses_the_express_layout_and_the_proven_front_rear_pair() -> None:
    """The pair take is the rear express geometry, played as two branches
    (issue #5330). Naming it leaves the default rear size alone."""
    row = mp.run_program("rear/pair")

    assert (row.purpose, row.regime, row.branch_pair) == (
        mp.PURPOSE_REAR, mp.REGIME_BRANCHES, mp.BRANCH_PAIR_FRONT_REAR)
    assert row.poses is mp.program("rear", "express").poses
    assert row.mover is None and row.room_sweep is False
    assert mp.program("rear").size == "express"


@pytest.mark.parametrize("preset,regime,pair", [
    ("rear/pair", mp.REGIME_BRANCHES, mp.BRANCH_PAIR_FRONT_REAR),
    ("rear/express", mp.REGIME_SUMMED, mp.BRANCH_PAIR_DRIVERS),
])
def test_rear_behind_places_the_microphone_behind_the_cabinet(preset, regime, pair) -> None:
    row = mp.run_program(preset, "rear_behind")
    assert (row.layout, row.purpose, row.regime, row.branch_pair, row.mover) == (
        "rear_behind", mp.PURPOSE_REAR, regime, pair, "human")
    assert [(pose.azimuth_deg, pose.elevation_deg, pose.kind, pose.distance_m, pose.repeats)
            for pose in row.poses] == [
        (0, 0, mp.POSE_KIND_BEARING, None, 1),
        (0, 0, mp.POSE_KIND_BEHIND, 0.1, 1),
    ]


def test_a_behind_pose_states_its_own_distance_from_the_back_panel() -> None:
    """A behind pose carries no seat offset; its distance validates like a
    close pose's (issue #5330)."""
    assert mp.validated_pose(mp.POSE_KIND_BEHIND, None, 0.1) == (None, 0.1)


def test_run_program_resolves_rear_layouts_and_custom_bearings() -> None:
    assert (mp.run_program("rear").program_id, mp.run_program("rear").size) == ("rear", "express")
    wide = mp.run_program("rear", "rear_wide")
    assert (wide.program_id, wide.size, wide.layout) == ("rear", "express", "rear_wide")
    custom = mp.run_program("rear", poses="0,-45,45")
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in custom.poses] == [(0, 0), (-45, 0), (45, 0)]
    assert (custom.size, custom.layout, custom.purpose, custom.regime) == (
        "express", mp.CUSTOM_SIZE, mp.PURPOSE_REAR, mp.REGIME_SUMMED)


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


def test_shared_layout_can_change_to_two_positions_without_code(tmp_path: Path) -> None:
    config = _bundled_config()
    config["layouts"]["seat_express"]["poses"] = config["layouts"]["seat_express"]["poses"][:2]  # type: ignore[index]

    programs = mp.load_programs(_write_config(tmp_path, config))

    assert len(programs[("room", "seat")].poses) == 2
    assert programs[("rear", "seat")].poses is programs[("room", "seat")].poses


def test_config_can_supply_future_prompt_text(tmp_path: Path) -> None:
    config = _bundled_config()
    pose = config["layouts"]["bass_axis"]["poses"][0]  # type: ignore[index]
    pose.update({"headline": "Measure the main seat", "detail": "Hold the mic at ear height."})

    programs = mp.load_programs(_write_config(tmp_path, config))

    loaded = programs[("bass", "axis")].poses[0]
    assert (loaded.headline, loaded.detail) == (
        "Measure the main seat", "Hold the mic at ear height.",
    )


@pytest.mark.parametrize("broken", ["empty", "repeats", "purpose", "regime", "mode", "layout_key",
                                    "mover", "room_sweep", "room_sweep_mode", "offers_unknown", "offers_without_default",
                                    "branch_pair", "branch_pair_regime", "co_unknown", "co_primary",
                                    "co_regime", "co_not_list", "co_duplicate", "co_not_text", "levels"])
def test_malformed_config_is_rejected(tmp_path: Path, broken: str) -> None:
    config = _bundled_config()
    if broken.startswith("co_"):
        config["programs"][0].update(purpose="rear", regime="summed", room_sweep=False, co_purposes={
            "co_unknown": ["unknown"], "co_primary": ["rear"], "co_regime": ["room"],
            "co_not_list": "room", "co_duplicate": ["room", "room"], "co_not_text": [None],
        }[broken])
        if broken == "co_regime":
            config["programs"][0]["regime"] = "branches"
    elif broken == "levels":
        config["programs"][0]["levels"] = "-28,-18"
    elif broken == "offers_unknown":
        config["programs"][0]["layouts"].append("no_such_layout")  # type: ignore[index]
    elif broken == "offers_without_default":
        config["programs"][0]["layouts"] = ["baseline_full"]  # type: ignore[index]
    elif broken == "branch_pair":
        config["programs"][0].update(regime="branches", room_sweep=False, branch_pair="both")
    elif broken == "branch_pair_regime":
        config["programs"][0]["branch_pair"] = "front_rear"
    elif broken == "empty":
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


@pytest.mark.parametrize("layout", ["bass_axis", "seat_cloud", "room_quick", "seat_express"])
def test_a_bass_run_keeps_its_stimulus_and_ladder_on_any_layout(layout):
    run = mp.run_program("bass", layout)
    assert (run.stimulus, run.levels) == (mp.program("bass").stimulus, "auto")


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
