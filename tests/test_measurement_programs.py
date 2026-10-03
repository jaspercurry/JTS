# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The program table's behavior."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import MISSING, fields, replace
from importlib import import_module
from pathlib import Path

import pytest
from tests.program_baseline_fixtures import banked_program_baselines  # noqa: F401

from jasper.active_speaker import baseline_record
from jasper.active_speaker import measurement_programs as mp, baseline_profile as bp, commissioning_coordinator as cc
from jasper.active_speaker import measured_crossover_candidate as mc, measurement_emit as me, tuning_handoff as th
from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.capture_schedule import prepare_plan_captures
from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
from jasper.active_speaker.candidate_bank import BankedCandidate
from jasper.active_speaker.candidate_parts import compose_candidate
from jasper.active_speaker.crossover_v2 import prescription_document as pd, prescription_contract as pc
from jasper.active_speaker.design_draft import design_draft_view
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.active_speaker_fixtures import mono_output_topology, passive_stereo_output_topology
from jasper.active_speaker.round_view_artifacts import ARTIFACT_BY_VIEW, BOOKKEEPING_ORDER, bookkeeping_views
from jasper.audio_measurement.gating import NEAR_FIELD_EXEMPT
from jasper.audio_measurement.piston import NEAR_FIELD_MAX_DISTANCE_M
from jasper.cli import crossover_prescriber, round as round_cli, round_views


@pytest.mark.parametrize("actual,expected", [
    pytest.param(pd._JUDGE_ORDER, ("topology", "blend", "alignment", "bass", "room", "rear_calibration", "driver"), id="judge"),
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
    ("speaker/mark", "speaker_mark", 1, 1, 2),
    ("speaker/mark", "baseline_full", 13, 13, 16),
    ("speaker/mark", "baseline_express", 5, 5, 8),
    ("tournament/express", "tournament_full", 3, 3, 3),
    ("tournament/express", "tournament_express", 1, 1, 1),
    ("room/seat", "seat_cloud", 11, 11, 11),
    ("room/seat", "seat_cube", 7, 7, 7),
    ("room/seat", "seat_express", 3, 3, 3),
    ("room/seat", "room_quick", 3, 3, 3),
    ("rear/seat", "seat_express", 3, 3, 3),
    ("rear/pair", "speaker_mark", 1, 1, 2),
    ("drivers/each", "drivers_each", 2, 1, 2),
])
def test_shipped_rows(preset: str, layout: str, poses: int, moves: int, captures: int) -> None:
    row = mp.run_preset(preset, layout)

    assert (row.preset, row.layout) == (preset, layout)
    assert len(row.poses) == poses
    assert row.mic_move_count == moves
    assert row.capture_count == captures


@pytest.mark.parametrize("preset_id", mp.available_presets())
def test_a_preset_names_its_purposes_program_first_at_every_layout(preset_id):
    """The seat trial serves rear and room, and the in-room round room and bass, from one set
    of takes (ADR-0336, ADR-0383, ADR-0429)."""
    row = mp.preset(preset_id)
    expected = {"rear/seat": ("rear", "room"), "room/seat": ("room", "bass")}.get(preset_id, (row.purpose,))
    assert (mp.run_purposes(preset_id), mp.run_purpose(preset_id)) == (expected, expected[0])
    assert {mp.run_preset(preset_id, layout).purposes for layout in row.layouts} == {expected}


@pytest.mark.parametrize("name", ["rear", "reference", ""])
def test_run_purposes_preserves_primary_identity_without_a_registry_row(name):
    assert mp.run_purposes(name) == (mp.run_purpose(name),) == (name.partition("/")[0],)


@pytest.mark.parametrize("purpose", ["room", "bass"])
def test_summed_bookkeeping_includes_one_frequency_image(purpose):
    assert ("frequency", False, False) in bookkeeping_views((purpose,))


@pytest.mark.parametrize(("purposes", "expected"), [
    (("speaker",), (("frequency", False, False),)),
    (("room", "bass"), (("room", True, False), ("room-grade", True, True), ("bass", True, False),
                        ("frequency", False, False))),
    (("bass",), (("bass", True, False), ("frequency", False, False))),
    (("reference",), ()),
    (("rear",), (("rear", False, False), ("frequency", False, False))),
    (("rear", "room"), (("room", True, False), ("room-grade", True, True), ("rear", False, False),
                        ("frequency", False, False))),
])
def test_the_view_table_answers_every_automatic_view(purposes, expected):
    assert bookkeeping_views(purposes) == expected
    assert {name for name, row in ARTIFACT_BY_VIEW.items() if row.builder} == set(BOOKKEEPING_ORDER)
    for view, _, _ in expected:
        row = ARTIFACT_BY_VIEW[view]
        module, _, builder = row.builder.rpartition(".")
        assert callable(getattr(import_module(f".{module}", "jasper.active_speaker"), builder))


@pytest.mark.parametrize("preset,pair", [("branches/express", "drivers"), ("front_rear/express", "front_rear")])
def test_a_branch_preset_is_a_speaker_run_that_keeps_its_pair(preset, pair):
    row = mp.run_preset(preset)

    assert (row.purpose, row.regime, row.branch_pair) == (mp.PURPOSE_SPEAKER, mp.REGIME_BRANCHES, pair)


@pytest.mark.parametrize("preset,layout,mover", [
    ("room/seat", "room_quick", "arm"),
    ("rear/seat", "seat_express", "human"),
    ("room/seat", "seat_express", "human"),
    ("rear/pair", "speaker_mark", None),
])
def test_a_layout_runs_under_its_preset_with_its_own_mover(preset, layout, mover):
    row, default = mp.run_preset(preset, layout), mp.run_preset(preset)
    assert (row.preset, row.layout, row.mover) == (preset, layout, mover)
    assert (row.purposes, row.regime, row.branch_pair) == (default.purposes, default.regime, default.branch_pair)


@pytest.mark.parametrize("preset,layout", [("speaker", "seat_cloud"), ("rear/express", "speaker_mark"), ("room", "speaker_mark")])
def test_a_layout_its_preset_does_not_offer_refuses_by_name(preset, layout):
    with pytest.raises(mp.LayoutNotOfferedError) as excinfo:
        mp.run_preset(preset, layout)
    default = mp.run_preset(preset)
    assert (excinfo.value.reason, excinfo.value.detail) == (mp.LAYOUT_NOT_OFFERED, {
        "preset": default.preset, "layout": layout, "offered": list(default.layouts)})


def test_a_layout_passed_as_poses_refuses_by_name():
    with pytest.raises(mp.PosesNameALayoutError) as excinfo:
        mp.run_preset("room", poses="seat_express")
    assert (excinfo.value.reason, excinfo.value.detail) == (
        mp.POSES_NAME_A_LAYOUT, {"poses": "seat_express", "use": "--layout"})


_ROUND_DIR = "/var/lib/jasper/active_speaker/campaigns/round-7"


def test_every_programs_prompt_is_one_template_that_lists_the_declared_components(monkeypatch):
    """The copied prompt comes from the rows (#5737 P11). It lists each declared output as its
    owners resolve it: the rear woofer declares no size or sensitivity, so it takes the front's
    size (ADR-0384) and its role's sensitivity (ADR-0382 §3); the tweeter's is after its pad. It
    lists the one-driver presets this speaker runs and the catalog that reads their rounds.
    Blanking the program row's own fields leaves one text for every program, and a 2-way
    cardioid's prompt, with an applied tune and a round, stays under 300 words."""
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions",
                        lambda **_kwargs: [Path(_ROUND_DIR)])
    topology = mono_output_topology(card_id=None)
    group, = topology.speaker_groups
    rear = replace(group.channels[0], output_variant="rear", physical_output_index=2)
    topology = replace(topology, speaker_groups=(replace(group, channels=(*group.channels, rear)),))
    woofer = {"role": "woofer", "model": "W", "measurement_band_hz": [40, 3000]}
    manual = {"drivers": [
        {**woofer, "target_id": "mono:woofer", "radiating_diameter_mm": 115, "sensitivity_db_2v83_1m": 86.0},
        {"target_id": "mono:tweeter", "role": "tweeter", "model": "T", "measurement_band_hz": [2000, 20000],
         "recommended_highpass_hz": 2000, "radiating_diameter_mm": 25, "sensitivity_db_2v83_1m": 94.0,
         "pad": {"kind": "direct_db", "attenuation_db": -3.0}},
        {**woofer, "target_id": "mono:woofer:rear"},
    ], "crossover_candidates": []}
    draft = design_draft_view({"revision": 3, "topology": topology.to_dict(), "manual_settings": manual},
                              topology=topology)
    view = cc.build_commissioning_view(topology, design_draft=draft, applied_profile={
        "source": {"measured_candidate_fingerprint": "a" * 64}, "config": {"sha256": "b" * 64},
        "applied_at": "2026-09-13T12:00:00Z"})
    woofer_facts = {"role": "woofer", "role_passband_hz": [40.0, 3000.0], "radiating_diameter_mm": 115,
                    "effective_sensitivity_db_2v83_1m": 86.0}
    components = [
        {"target_id": "mono:woofer", "physical_output_index": 0, **woofer_facts},
        {"target_id": "mono:tweeter", "role": "tweeter", "physical_output_index": 1,
         "role_passband_hz": [2000.0, 20000.0], "radiating_diameter_mm": 25, "effective_sensitivity_db_2v83_1m": 91.0},
        {"target_id": "mono:woofer:rear", "physical_output_index": 2, **woofer_facts},
    ]
    templates = set()
    for row in mp.PROGRAM_ROWS:
        handoff = th.build_tuning_handoff(commissioning_view=view, design_draft=draft, program_id=row.purpose)
        binding, prompt = handoff["binding"], handoff["prompt"]
        assert (binding["components"], binding["one_driver_presets"]) == (components, ["drivers/each", "nearfield/each"])
        assert {*map(json.dumps, binding["components"]), "drivers/each: " + mp.preset("drivers/each").use_when,
                "nearfield/each: " + mp.preset("nearfield/each").use_when} <= set(prompt.splitlines())
        assert th.catalog_command(mp.PURPOSE_REFERENCE) in prompt
        assert len(prompt.split()) < 300
        if note := th.PROGRAM_NOTES.get(row.purpose):
            assert note in prompt.splitlines()
            prompt = prompt.replace(f"{note}\n", "")
        run_line, = [line for line in prompt.splitlines() if line.startswith("Run: ")]
        prompt = prompt.replace(f"{run_line}\n", "")
        for value, blank in ((row.title, "<title>"), (row.description, "<description>"),
                             (f"--program {row.purpose}", "--program <p>"), (f"--section {row.purpose}", "--section <p>")):
            assert value in prompt
            prompt = prompt.replace(value, blank)
        templates.add(prompt)
    assert len(templates) == 1


def test_a_stereo_pairs_outputs_are_named_apart_and_it_offers_no_one_driver_preset(monkeypatch):
    """A component is named by its speaker group too, so a pair's two drivers of one role are two
    components; a pair plays no driver alone (ADR-0360), so no one-driver preset is listed."""
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.round_inputs.recent_round_sessions", lambda **_kwargs: [])
    topology = passive_stereo_output_topology()
    draft = design_draft_view({"revision": 1, "topology": topology.to_dict(), "manual_settings": None},
                              topology=topology)
    binding = th.build_tuning_handoff_binding(draft, cc.build_commissioning_view(topology, design_draft=draft))
    assert ([component["target_id"] for component in binding["components"]], binding["one_driver_presets"]) == (
        ["left:full_range", "right:full_range"], [])


@pytest.mark.parametrize("round_dir", [None, _ROUND_DIR])
@pytest.mark.parametrize("program", mp.RUNNABLE_PROGRAMS)
def test_the_prompt_points_at_status_the_catalog_and_the_contract(program, round_dir):
    """Where tuning stands, what the agent can ask and what a document may write (#5928 TB6), each
    a call its tool's own parser accepts; the contract evaluates its bounds on the latest round."""
    parsers = {module.PROG: module.build_parser() for module in (crossover_prescriber, round_views)}
    prompt = th.build_tuning_handoff_prompt({"latest_round_dir": round_dir}, program)
    calls = []
    for command in th.pointer_commands(program, round_dir):
        assert command in prompt
        _sudo, path, *argv = shlex.split(command)
        args = vars(parsers[Path(path).name].parse_args(argv))
        calls.append((Path(path).name, args["command"], args.get("program") or args.get("section"), args.get("round")))
    assert calls == [(crossover_prescriber.PROG, "status", None, None), (round_views.PROG, "catalog", program, None),
                     (crossover_prescriber.PROG, "contract", program, round_dir)]


def test_run_help_names_every_registry_pose_set(capsys):
    """``jasper-round run --help`` names every preset its ``--program`` takes (#5632 F11)."""
    with pytest.raises(SystemExit):
        round_cli.main(["run", "--help"])
    words = set(re.split(r"[\s,()]+", capsys.readouterr().out))
    assert set(mp.available_presets()) <= words


def test_express_geometry() -> None:
    row = mp.run_preset("speaker", "baseline_express")

    assert {p.azimuth_deg for p in row.poses} == {0, -20, 20}
    assert {p.elevation_deg for p in row.poses} == {0, -10, 10}
    assert [
        p.repeats for p in row.poses if (p.azimuth_deg, p.elevation_deg) == (0, 0)
    ] == [mp.run_preset("speaker", "baseline_full").poses[0].repeats]


@pytest.mark.parametrize("name", ["baseline/medium", "tournament/medium", "spot/express", "spot", ""])
def test_unknown_lookup_names_the_valid_choices(name: str) -> None:
    lookups = [lambda: mp.preset(name)]
    if name:
        lookups.append(lambda: mp.run_purposes(name))
    for lookup in lookups:
        with pytest.raises(mp.UnknownPresetError) as excinfo:
            lookup()
        assert (excinfo.value.preset, excinfo.value.choices) == (name, mp.available_presets())


def test_available_presets_is_the_sorted_registry() -> None:
    choices = mp.available_presets()

    assert choices == (
        "branches/express", "drivers/each", "front_rear/express", "nearfield/each", "rear/express",
        "rear/pair", "rear/seat", "room/seat", "speaker/mark", "tournament/express",
    )
    rows = [mp.preset(preset_id) for preset_id in choices]
    assert tuple(row.preset for row in rows) == choices
    assert {(row.preset, row.branch_pair) for row in rows if row.regime == mp.REGIME_BRANCHES} == {
        ("branches/express", mp.BRANCH_PAIR_DRIVERS), ("front_rear/express", mp.BRANCH_PAIR_FRONT_REAR),
        ("rear/pair", mp.BRANCH_PAIR_FRONT_REAR),
    }



_WOOFER_STEP = (("woofer", 0.015), ("woofer", 0.03))
_TWO_WAY, _CARDIOID = ("tweeter", "woofer"), ("tweeter", "woofer", "woofer:rear")
_NEARFIELD, _DRIVERS = mp.run_preset("nearfield"), mp.run_preset("drivers")


@pytest.mark.parametrize("row,targets,driver,walked", [
    (_NEARFIELD, _TWO_WAY, "", _WOOFER_STEP),
    (_NEARFIELD, _CARDIOID, "", (*_WOOFER_STEP, *(("woofer:rear", distance) for _, distance in _WOOFER_STEP))),
    (_NEARFIELD, (), "", _WOOFER_STEP),
    (_NEARFIELD, _CARDIOID, "woofer:rear", tuple(("woofer:rear", distance) for _, distance in _WOOFER_STEP)),
    (_DRIVERS, _CARDIOID, "", (("woofer", None), ("woofer:rear", None), ("tweeter", None))),
    (_DRIVERS, _CARDIOID, "tweeter", (("tweeter", None),)),
    (replace(_DRIVERS, poses=tuple(mp.Pose(0, 0, driver=driver) for driver in ("woofer:rear", "tweeter"))),
     _CARDIOID, "", (("woofer:rear", None), ("tweeter", None))),
    (mp.run_preset("nearfield", poses='[{"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", '
                                       '"distance_m": 0.02, "driver": "woofer"}]'), _CARDIOID, "", (("woofer", 0.02),)),
])
def test_a_presets_driver_role_plays_each_declared_output(row, targets, driver, walked) -> None:
    """A named layout's pose that names a bare driver role plays each declared output of
    it, one output's placements after the other's; a pose naming one output, or an inline
    pose, plays what it names; an undeclared role keeps its name for preflight to refuse;
    --driver narrows to one output (ADR-0366 §6)."""
    request = ac.request_for_preset(row, targets=targets, driver=driver)
    assert tuple((stop.pose.driver, stop.pose.distance_m) for stop in request.stops) == walked


@pytest.mark.parametrize("row,targets,driver,offered", [
    (_NEARFIELD, _TWO_WAY, "woofer:rear", ["woofer"]),
    (_NEARFIELD, _CARDIOID, "tweeter", ["woofer", "woofer:rear"]),
    (mp.run_preset("speaker"), _CARDIOID, "woofer", []),
])
def test_a_driver_the_preset_does_not_play_alone_refuses_naming_the_ones_it_does(row, targets, driver, offered) -> None:
    with pytest.raises(mp.DriverNotOfferedError) as excinfo:
        mp.plan_poses(row, targets, driver)
    assert (excinfo.value.reason, excinfo.value.detail) == (
        mp.DRIVER_NOT_OFFERED, {"preset": row.preset, "driver": driver, "offered": offered})


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
            mp.run_preset(program_id, layout, poses)
        return
    row = mp.run_preset(program_id, layout, poses)
    assert (row.layout, tuple((pose.driver, pose.distance_m) for pose in row.poses)) == resolved
    assert (row.purpose, row.regime, row.stimulus, mp.run_purpose(row.preset)) == (
        mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, _bundled_config()["stimuli"]["near_field"], mp.PURPOSE_REFERENCE)


def _seat(right_m: float, forward_m: float, up_m: float, repeats: int = 1):
    return mp.Pose(
        0, 0, repeats,
        kind=mp.POSE_KIND_SEAT, seat_offset_m=(right_m, forward_m, up_m),
    )


@pytest.mark.parametrize(
    ("poses", "moves", "captures"),
    [
        (
            (mp.Pose(0, 0, 1), mp.Pose(0, 0, 1), mp.Pose(10, 0, 1)),
            2,
            3,
        ),
        (
            (mp.Pose(0, 0, 4), mp.Pose(0, 0, 1), mp.Pose(10, 0, 2)),
            2,
            7,
        ),
        # Two seat poses share the (0, 0) bearing and are two different places.
        ((_seat(0.0, 0.0, 0.0), _seat(0.30, 0.0, 0.0, 2)), 2, 3),
        # A close pose is stated from its driver's cone, so each driver is its own place.
        ((mp.Pose(0, 0, kind=mp.POSE_KIND_CLOSE, distance_m=0.3, driver="woofer"),
          mp.Pose(0, 0, kind=mp.POSE_KIND_CLOSE, distance_m=0.3, driver="woofer:rear")), 2, 2),
        # Going back to a spot moves the microphone again.
        ((mp.Pose(0, 0), mp.Pose(10, 0), mp.Pose(0, 0)), 3, 3),
    ],
)
def test_counts_split_moves_from_captures(
    poses: tuple[object, ...], moves: int, captures: int
) -> None:
    row = mp.Preset(preset="t/t", poses=poses, purposes=(mp.PURPOSE_SPEAKER,))

    assert row.mic_move_count == moves
    assert row.capture_count == captures


def test_the_seat_cube_is_the_head_and_six_face_centres() -> None:
    cube = mp.run_preset("room", "seat_cube")
    express = mp.run_preset("room", "seat_express")

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
    cloud = mp.run_preset("room", "seat_cloud")

    assert {p.kind for p in cloud.poses} == {mp.POSE_KIND_SEAT}
    assert {(p.azimuth_deg, p.elevation_deg, p.repeats) for p in cloud.poses} == {(0, 0, 1)}
    assert [p.seat_offset_m for p in cloud.poses] == [
        (-0.30, 0.30, 0.0), (0.0, 0.30, 0.0), (0.30, 0.30, 0.0),
        (-0.30, 0.0, 0.0), (0.0, 0.0, 0.0), (0.30, 0.0, 0.0),
        (-0.30, -0.30, 0.0), (0.0, -0.30, 0.0), (0.30, -0.30, 0.0),
        (0.0, 0.0, 0.30), (0.0, 0.0, -0.30),
    ]


@pytest.mark.parametrize("retired", [
    "baseline/full", "seat/cube", "rear/pair_mark", "rear/custom", "bass/nearfield", "nearfield/woofer",
    "speaker/express"])
def test_a_retired_preset_id_refuses_as_an_unknown_preset(retired: str) -> None:
    """No retired id is kept for a new run or a banked round (ADR-0377)."""
    for resolve in (mp.run_preset, mp.run_purpose):
        with pytest.raises(mp.UnknownPresetError) as excinfo:
            resolve(retired)
        assert excinfo.value.preset == retired


def test_a_program_name_resolves_to_its_first_preset() -> None:
    assert {name: mp.preset(name).preset for name in ("tournament", "branches", "room", "rear", "nearfield")} == {
        "tournament": "tournament/express", "branches": "branches/express", "room": "room/seat",
        "rear": "rear/express", "nearfield": "nearfield/each"}


@pytest.mark.parametrize("program", mp.RUNNABLE_PROGRAMS)
def test_a_programs_first_plan_serves_it_at_a_layout_its_preset_offers(program) -> None:
    """Bass starts on the in-room round, which serves room and bass (ADR-0429)."""
    first = mp.first_plan(program)

    assert program in first.purposes and first.layout in mp.preset(first.preset).layouts
    if program not in (mp.PURPOSE_REAR, mp.PURPOSE_BASS):
        assert first == mp.preset(program)


def test_a_bare_bass_names_no_preset() -> None:
    """Bass has no preset of its own: the in-room round measures it, so a bass run refuses as an
    unknown preset that names the presets there are (ADR-0429, ADR-0431)."""
    with pytest.raises(mp.UnknownPresetError) as excinfo:
        mp.run_preset("bass")
    assert (excinfo.value.preset, excinfo.value.choices) == ("bass", mp.available_presets())


def test_the_rear_program_starts_with_the_pair_model_at_the_mark() -> None:
    """The playbook's Seat loop banks the pair model first; the rear program's default preset
    stays the summed one a trial of candidates walks."""
    first = mp.first_plan("rear")

    assert (first.preset, first.layout, first.regime) == ("rear/pair", "speaker_mark", mp.REGIME_BRANCHES)
    assert mp.preset("rear").preset == "rear/express"


@pytest.mark.parametrize("program,purpose", [("room", mp.PURPOSE_ROOM)])
def test_room_and_bass_plans_share_poses_and_summed_regime(program, purpose) -> None:
    cloud = mp.run_preset(program, "seat_cloud")
    quick = mp.run_preset(program, "room_quick")

    assert cloud.poses is mp.run_preset("room", "seat_cloud").poses
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in quick.poses] == [
        (0, 0), (-20, 0), (20, 0),
    ]
    assert {row.purpose for row in (cloud, quick)} == {purpose}
    assert {row.regime for row in (cloud, quick)} == {mp.REGIME_SUMMED}
    assert [{mp.gate_exemption(pose.kind) for pose in row.poses} for row in (cloud, quick)] == [
        {mp.POSE_KIND_SEAT}, {None}]


@pytest.mark.parametrize("layout,poses", [
    ("rear_express", [(0, 2), (-20, 1), (20, 1)]),
    ("rear_wide", [(0, 2), (-20, 1), (20, 1), (-45, 1), (45, 1)]),
])
def test_rear_layouts_pin_no_mover_and_repeat_the_zero_pose(layout, poses) -> None:
    row = mp.run_preset("rear", layout)

    assert row.purpose == mp.PURPOSE_REAR and row.regime == mp.REGIME_SUMMED
    assert row.mover is None
    assert [(pose.azimuth_deg, pose.repeats) for pose in row.poses] == poses


@pytest.mark.parametrize("kind,driver,distance_m,reason", [
    (mp.POSE_KIND_SEAT, "", None, mp.POSE_KIND_SEAT),
    (mp.POSE_KIND_BEARING, "", 1.0, None),
    (mp.POSE_KIND_BEHIND, "", 0.1, None),
    (mp.POSE_KIND_CLOSE, "woofer:rear", 0.015, NEAR_FIELD_EXEMPT),
    (mp.POSE_KIND_CLOSE, "woofer", NEAR_FIELD_MAX_DISTANCE_M, NEAR_FIELD_EXEMPT),
    (mp.POSE_KIND_BEARING, "woofer", 0.5, None),
    (mp.POSE_KIND_CLOSE, "", 0.03, None),
])
def test_a_take_reads_ungated_only_at_a_seat_or_within_one_drivers_near_field(kind, driver, distance_m, reason) -> None:
    """A seat take is the room's own measurement; a pose at one driver reads
    ungated only within the near-field distance, so a far-field one-driver
    take is gated. A room, bass or rear take at a bearing or behind the
    cabinet is gated too: a purpose never exempts a take (ADR-0400)."""
    assert mp.gate_exemption(kind, driver=driver, distance_m=distance_m) == reason


@pytest.mark.parametrize("purpose,regime,supported", [
    (mp.PURPOSE_REAR, mp.REGIME_SUMMED, True),
    (mp.PURPOSE_REAR, mp.REGIME_BRANCHES, True),
    (mp.PURPOSE_REAR, mp.REGIME_PER_DRIVER, False),
    (mp.PURPOSE_ROOM, mp.REGIME_BRANCHES, False),
    (mp.PURPOSE_SPEAKER, mp.REGIME_BRANCHES, True),
])
def test_only_rear_joins_speaker_in_the_branches_regime(purpose, regime, supported) -> None:
    if supported:
        assert mp.validated_capture_purpose(purpose, regime) == purpose
    else:
        with pytest.raises(ValueError):
            mp.validated_capture_purpose(purpose, regime)


def _pose_with(kind, distance_m, driver):
    seat_offset_m = (0.0, 0.0, 0.0) if kind == mp.POSE_KIND_SEAT else None
    return mp.Pose(0, 0, kind=kind, distance_m=distance_m, driver=driver, seat_offset_m=seat_offset_m)


def _program_with(purpose, regime, kind, distance_m, driver):
    return mp.Preset("t/t", (_pose_with(kind, distance_m, driver),), purposes=(purpose,), regime=regime)


def _stop_with(purpose, regime, kind, distance_m, driver):
    return ac.AngleStop(_pose_with(kind, distance_m, driver), regime, purpose=purpose)


@pytest.mark.parametrize("door,refusal", [(_program_with, ValueError), (_stop_with, CrossoverV2FlowError)])
@pytest.mark.parametrize("purpose,regime,kind,distance_m,driver,accepted", [
    (mp.PURPOSE_SPEAKER, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "woofer", True),
    (mp.PURPOSE_SPEAKER, mp.REGIME_SUMMED, mp.POSE_KIND_BEARING, 2.0, "tweeter", True),
    (mp.PURPOSE_SPEAKER, mp.REGIME_BRANCHES, mp.POSE_KIND_CLOSE, 0.15, "woofer", True),
    (mp.PURPOSE_REAR, mp.REGIME_SUMMED, mp.POSE_KIND_BEHIND, 0.5, "woofer:rear", True),
    (mp.PURPOSE_ROOM, mp.REGIME_SUMMED, mp.POSE_KIND_SEAT, 0.05, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, mp.POSE_KIND_CLOSE, 0.015, "woofer:rear", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "tweeter", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.015, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_BRANCHES, mp.POSE_KIND_BEARING, None, "woofer", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.03, "", True),
    (mp.PURPOSE_REFERENCE, mp.REGIME_PER_DRIVER, mp.POSE_KIND_BEARING, None, "", False),
    (mp.PURPOSE_SPEAKER, mp.REGIME_PER_DRIVER, mp.POSE_KIND_CLOSE, 0.015, "woofer", False),
    (mp.PURPOSE_ROOM, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, NEAR_FIELD_MAX_DISTANCE_M, "woofer", False),
    (mp.PURPOSE_BASS, mp.REGIME_SUMMED, mp.POSE_KIND_CLOSE, 0.03, "woofer", False),
    (mp.PURPOSE_BASS, mp.REGIME_SUMMED, mp.POSE_KIND_BEARING, None, "woofer", False),
])
def test_a_pose_of_any_purpose_names_its_driver_and_a_near_field_one_is_reference(
    door, refusal, purpose, regime, kind, distance_m, driver, accepted,
) -> None:
    """Every door a pose enters through judges its driver the same way: a
    program row or layout, and a stop in a hand-written plan. A pose of any
    purpose but bass names the driver it plays alone, at any regime, kind and
    distance (ADR-0366 §1); one within that driver's near-field distance, never
    at a seat (ADR-0400 §1), is reference evidence (ADR-0360 §2), and a
    reference pose on a regime that plays drivers names one."""
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
        row = mp.preset("nearfield")
        assert len(ac.request_for_preset(row, candidates=candidates).stops) == len(row.poses)
    else:
        with pytest.raises(CrossoverV2FlowError):
            ac.request_for_preset(mp.preset("nearfield"), candidates=candidates)


@pytest.mark.parametrize("stops,expected", [
    (mp.run_preset("drivers"), [("lateral", ("woofer",)), ("lateral", ("woofer:rear",)), ("lateral", ("tweeter",))]),
    ((ac.AngleStop(mp.Pose(0, 0), mp.REGIME_PER_DRIVER, purpose=mp.PURPOSE_SPEAKER),
      ac.AngleStop(mp.Pose(0, 0, driver="woofer"), mp.REGIME_PER_DRIVER, purpose=mp.PURPOSE_REFERENCE)),
     [("check", ()), ("timing", ()), ("measure", ()), ("lateral", ("woofer",))]),
], ids=["one-driver-preset", "beside-a-two-driver-stop"])
def test_a_stop_naming_its_driver_skips_what_plays_every_driver(stops, expected) -> None:
    """A stop naming its driver plays it alone in the far field on MEASURE's
    sweep, with no CHECK or timing take for it; a stop that plays every driver
    keeps both (#5696, ADR-0366)."""
    request = (ac.request_for_preset(stops, targets=_CARDIOID) if isinstance(stops, mp.Preset)
               else ac.AngleCaptureRequest(stops=stops, program="speaker/mark"))
    captures = prepare_plan_captures(request)
    assert [(capture.spec.program_phase, capture.spec.branch_target_ids) for capture in captures] == expected
    assert all(capture.spec.stimulus is None for capture in captures)


@pytest.mark.parametrize("purpose,base,regime,cleared", [
    (mp.PURPOSE_REAR, True, mp.REGIME_BRANCHES, ("rear_calibration",)),
    (mp.PURPOSE_REAR, False, mp.REGIME_BRANCHES, ("rear_calibration",)),
    (mp.PURPOSE_REAR, True, mp.REGIME_SUMMED, ()), (mp.PURPOSE_SPEAKER, True, mp.REGIME_BRANCHES, ()),
    (mp.PURPOSE_ROOM, True, mp.REGIME_SUMMED, ("room_correction", "bass_extension")),
    (mp.PURPOSE_ROOM, False, mp.REGIME_SUMMED, ()), (mp.PURPOSE_REFERENCE, True, mp.REGIME_SUMMED, ()),
    (None, True, mp.REGIME_SUMMED, ()),
])
def test_a_purpose_row_declares_the_applied_layers_its_takes_clear(purpose, base, regime, cleared) -> None:
    """The in-room base plays bass and room off; a rear pair take plays its
    parent with the rear stage off; every other take plays its layers as
    composed (ADR-0370, ADR-0386, ADR-0429)."""
    assert mp.cleared_layers(purpose, base=base, regime=regime) == cleared


def test_the_rear_pair_row_reuses_the_express_layout_and_the_proven_front_rear_pair() -> None:
    """The pair take is the rear express geometry, played as two branches
    (issue #5330). Naming it leaves the default rear size alone."""
    row = mp.run_preset("rear/pair")

    assert (row.purpose, row.regime, row.branch_pair) == (
        mp.PURPOSE_REAR, mp.REGIME_BRANCHES, mp.BRANCH_PAIR_FRONT_REAR)
    assert row.poses is mp.preset("rear/express").poses
    assert row.mover is None
    assert mp.preset("rear").preset == "rear/express"


@pytest.mark.parametrize("preset,regime,pair", [
    ("rear/pair", mp.REGIME_BRANCHES, mp.BRANCH_PAIR_FRONT_REAR),
    ("rear/express", mp.REGIME_SUMMED, mp.BRANCH_PAIR_DRIVERS),
])
def test_rear_behind_places_the_microphone_behind_the_cabinet(preset, regime, pair) -> None:
    row = mp.run_preset(preset, "rear_behind")
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
    pose = mp.Pose(0, 0, kind=mp.POSE_KIND_BEHIND, distance_m=0.1)
    assert (pose.seat_offset_m, pose.distance_m) == (None, 0.1)


def test_run_preset_resolves_rear_layouts_and_custom_bearings() -> None:
    assert mp.run_preset("rear").preset == "rear/express"
    wide = mp.run_preset("rear", "rear_wide")
    assert (wide.preset, wide.layout) == ("rear/express", "rear_wide")
    custom = mp.run_preset("rear", poses="0,-45,45")
    assert [(pose.azimuth_deg, pose.elevation_deg) for pose in custom.poses] == [(0, 0), (-45, 0), (45, 0)]
    assert (custom.preset, custom.layout, custom.purpose, custom.regime) == (
        "rear/express", mp.CUSTOM_LAYOUT, mp.PURPOSE_REAR, mp.REGIME_SUMMED)


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

    presets = mp._load_presets(_write_config(tmp_path, config))[0]

    assert len(presets["room/seat"].poses) == 2
    assert presets["rear/seat"].poses is presets["room/seat"].poses


def test_config_can_supply_future_prompt_text(tmp_path: Path) -> None:
    config = _bundled_config()
    pose = config["layouts"]["seat_express"]["poses"][0]  # type: ignore[index]
    pose.update({"headline": "Measure the main seat", "detail": "Hold the mic at ear height."})

    loaded = mp._load_presets(_write_config(tmp_path, config))[0]["room/seat"].poses[0]
    assert (loaded.headline, loaded.detail) == (
        "Measure the main seat", "Hold the mic at ear height.",
    )


@pytest.mark.parametrize("broken", ["empty", "repeats", "regime", "mode", "layout_key",
                                    "mover", "offers_unknown", "offers_without_default",
                                    "branch_pair", "branch_pair_regime", "purposes_missing", "purposes_unknown",
                                    "purposes_none", "purposes_regime", "purposes_not_list", "purposes_duplicate",
                                    "purposes_not_text", "driver_purposes", "driver_purposes_reversed",
                                    "preset_description", "layout_use_when", "layout_list", "timing_take"])
def test_malformed_config_is_rejected(tmp_path: Path, broken: str) -> None:
    config = _bundled_config()
    if broken == "purposes_missing":
        del config["presets"][0]["purposes"]  # type: ignore[index]
    elif broken == "preset_description":
        del config["presets"][0]["description"]  # type: ignore[index]
    elif broken == "layout_use_when":
        del config["layouts"]["room_quick"]["use_when"]  # type: ignore[index]
    elif broken == "layout_list":
        config["layouts"]["room_quick"] = config["layouts"]["room_quick"]["poses"]  # type: ignore[index]
    elif broken.startswith("driver_purposes"):
        # Every purpose, not only the first, admits each driver pose, so a near-field one stays reference.
        next(row for row in config["presets"] if row["preset"] == "nearfield/each")["purposes"] = (
            ["speaker", "reference"] if broken.endswith("reversed") else ["reference", "speaker"])
    elif broken.startswith("purposes_"):
        config["presets"][0].update(regime="summed", purposes={
            "purposes_unknown": ["other"], "purposes_none": [], "purposes_regime": ["rear", "room"],
            "purposes_not_list": "rear", "purposes_duplicate": ["rear", "rear"], "purposes_not_text": [None],
        }[broken])
        if broken == "purposes_regime":
            config["presets"][0]["regime"] = "branches"
    elif broken == "offers_unknown":
        config["presets"][0]["layouts"].append("no_such_layout")  # type: ignore[index]
    elif broken == "offers_without_default":
        config["presets"][0]["layouts"] = ["baseline_full"]  # type: ignore[index]
    elif broken == "branch_pair":
        config["presets"][0].update(regime="branches", branch_pair="both")
    elif broken == "branch_pair_regime":
        config["presets"][0]["branch_pair"] = "front_rear"
    elif broken == "empty":
        config["layouts"]["room_quick"] = []  # type: ignore[index]
    elif broken == "repeats":
        config["layouts"]["room_quick"]["poses"][0]["repeats"] = 0  # type: ignore[index]
    elif broken == "layout_key":
        config["layouts"]["room_quick"]["moverr"] = "arm"  # type: ignore[index]
    elif broken == "mover":
        config["layouts"]["room_quick"]["mover"] = []  # type: ignore[index]
    elif broken == "timing_take":
        config["presets"][0][broken] = "yes"
    elif broken == "regime":
        config["presets"][0]["regime"] = "other"  # type: ignore[index]
    else:
        config["presets"][0].update({"purposes": ["room"], "regime": "per_driver"})  # type: ignore[index]

    with pytest.raises(ValueError):
        mp._load_presets(_write_config(tmp_path, config))


@pytest.mark.parametrize("stimuli,reference", [
    ([], "bass"), ({"bass": []}, "bass"), ({"bass": {"band_hz": [20, 1100]}}, "bass"),
    ({"bass": {"ceiling_hz": 1100}}, "missing"), ({"bass": {"ceiling_hz": True}}, "bass"),
    ({"bass": {"ceiling_hz": 0}}, "bass"), ({"bass": {"ceiling_hz": float("inf")}}, "bass"),
    *(({"near_field": row}, "bass") for row in (
        {"band_hz": [20.0, 2000.0], "sweep_s": 8.0}, {"band_hz": [2000.0, 20.0], "sweep_s": 8.0, "gap_s": 0.5},
        {"band_hz": [20.0], "sweep_s": 8.0, "gap_s": 0.5}, {"band_hz": 20.0, "sweep_s": 8.0, "gap_s": 0.5},
        {"band_hz": [20.0, 2000.0], "sweep_s": 8.0, "gap_s": 0},
        {"band_hz": [20.0, 2000.0], "sweep_s": 8.0, "gap_s": 3.0},
        {"band_hz": [20.0, 2000.0], "sweep_s": 8.0, "gap_s": 0.2},
        {"band_hz": [20.0, 2000.0], "sweep_s": 8.0, "gap_s": 0.5, "ceiling_hz": 1100.0})),
])
def test_invalid_registry_stimulus_is_a_value_error(tmp_path, stimuli, reference):
    config = _bundled_config()
    config["stimuli"] = {**config["stimuli"], **stimuli} if isinstance(stimuli, dict) else stimuli
    next(row for row in config["presets"] if row["preset"] == "nearfield/each")["stimulus"] = reference
    with pytest.raises(ValueError):
        mp._load_presets(_write_config(tmp_path, config))
