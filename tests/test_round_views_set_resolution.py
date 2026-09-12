# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest selection, artifact isolation, and the retired CLI doors."""

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take
from jasper.active_speaker.crossover_v2 import room_views
from jasper.active_speaker.crossover_v2.room_prescription import read_room_median
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.cli import round_views
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.crossover_v2.round_inputs import default_out, round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.window_view import window_view
from jasper.active_speaker.measurement_programs import bookkeeping_views
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.cli._report import render_report
from jasper.cli.round_views import build_parser, main
from jasper.cli.round_views._common import resolve_set
from tests.crossover_v2_banked_round import bank_seat_round, SEAT_GRID_HZ
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest


@pytest.fixture
def two_sets(tmp_path):
    root = bank_seat_round(tmp_path)
    inputs = round_inputs(root)
    rows = list(measurement_documents(inputs.session_dir))
    groups = []
    for number, members in enumerate((rows[:3], rows[3:])):
        records = []
        for row, record in members:
            record["level_db"] = -30.0 + number * 10
            take_artifact_path(inputs.session_dir, row.path).write_text(json.dumps(record))
            records.append((row.path, record))
        group = manifest_set(records)
        group["takes"].append({**group["takes"][0], "take_id": f"refused-{number}",
                               "quality": {"status": "refused", "fault": "clipped"}, "selected": False})
        groups.append(group)
    manifest = write_manifest(root, program="room", groups=groups)
    return root, manifest


def artifact_answer(capsys):
    answer = json.loads(capsys.readouterr().out)
    path = Path(answer["out"])
    assert answer["bytes"] == path.stat().st_size
    return answer, json.loads(path.read_text())


@pytest.mark.parametrize("index", [0, 1])
def test_set_selects_manifest_takes_and_files_its_own_artifact(two_sets, capsys, index):
    root, manifest = two_sets
    group = manifest["sets"][index]
    selected = resolve_set(round_inputs(root), group["set_id"])
    assert selected.capture_basis == group["capture_basis"]
    assert selected.takes == tuple(group["takes"])
    assert selected.selected_ids == tuple(take["take_id"] for take in group["takes"] if take["selected"])
    assert main(["room", str(root), "--set", group["set_id"]]) == 0
    answer, document = artifact_answer(capsys)
    doc = document["median"]
    assert answer["out"] == str(root / f"room-{group['set_id'][:12]}.json")
    assert set(doc["evidence"]["take_ids"]) == set(selected.selected_ids)
    assert doc["evidence"]["basis"] == group["capture_basis"]
    assert main(["room-grade", str(root), "--set", group["set_id"]]) == 0
    _, grade = artifact_answer(capsys)
    assert grade["room"] == answer["out"]
    assert grade["evidence"] == doc["evidence"]


@pytest.mark.parametrize("case,reason", [
    ("missing", "round_manifest_missing"), ("unfinished", "round_manifest_unfinalized"),
    ("unknown", "round_set_unknown"), ("ambiguous", "round_set_unknown"),
])
def test_manifest_refusals_keep_registry_codes(two_sets, capsys, case, reason):
    root, manifest = two_sets
    directory, _ = round_artifact_dir(round_inputs(root).session_dir)
    path = directory / RUN_MANIFEST_FILENAME
    if case == "missing":
        path.unlink()
    elif case == "unfinished":
        manifest["finalized"] = False
        path.write_text(json.dumps(manifest))
    flag = [] if case == "ambiguous" else ["--set", "unknown" if case == "unknown" else manifest["sets"][0]["set_id"]]
    assert main(["room", str(root), *flag]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert (answer["status"], answer["reason"]) == ("refused", reason)
    assert reason in REASON_REGISTRY
    assert not list(root.glob("room*.json"))


@pytest.mark.parametrize("status", ["complete", "partial", "cancelled"])
def test_finalized_one_set_needs_no_selector(tmp_path, capsys, status):
    root = bank_seat_round(tmp_path)
    manifest = write_manifest(root, program="room")
    directory, _ = round_artifact_dir(round_inputs(root).session_dir)
    manifest["status"] = status
    (directory / RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    assert resolve_set(round_inputs(root)).set_id == manifest["sets"][0]["set_id"]
    assert main(["room", str(root)]) == 0
    answer, _ = artifact_answer(capsys)
    assert Path(answer["out"]).name == "room.json"


@pytest.mark.parametrize("program,first", [
    ("speaker", ("inventory", "classify-features", "distortion", "directivity", "frozen", "per-seat")),
    ("room", ("room", "room-grade")),
    ("bass", ("bass", "bass-compare")),
])
def test_inventory_groups_and_orders_the_program(tmp_path, capsys, program, first):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program=program)
    assert bookkeeping_views(program) == first
    assert main(["inventory", str(root)]) == 0
    answer, doc = artifact_answer(capsys)
    assert answer["program"] == doc["program"] == program
    assert all(row["program"] == program for row in doc["artifacts"])
    assert tuple(row["view"] for row in doc["artifacts"][:len(first)]) == first


@pytest.mark.parametrize("argv", [
    ["windows", "round"], ["gate-sweep", "round"], ["spec-sweep", "round"],
    ["sweep", "round", "--scope", "take", "--capture-id", "take"],
    ["forward-model", "round", "--capture-id", "take"],
    ["forward-model", "round", "--measured-capture-id", "take"],
    ["room", "round", "--capture-id", "take"],
    ["room-grade", "round", "--room-median", "median.json"],
    ["room-grade", "round", "--baseline-room-median", "median.json"],
    ["bass-compare", "before", "after", "--change", "candidate", "--before-take", "take"],
    ["bass-compare", "before", "after", "--change", "candidate", "--after-take", "take"],
])
def test_retired_verbs_and_selectors_are_unknown(argv):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["entry", "round"], ["per-seat", "round", "--include", "agreement"],
    ["repeat-floor", "a", "b"], ["delay-landscape", "bundle", "--fc-hz", "1800"],
    ["dsp-replay", "graph", "stimulus", "--main-db", "-30", "--bass-reference-db", "-30"],
])
def test_stdout_cannot_replace_an_artifact(argv):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([*argv, "--out", "-"])
    assert exc.value.code == 2


@pytest.mark.parametrize("take_id,code", [("cloud_verify_00", 0), ("unknown", 1)])
def test_take_sweep_uses_the_same_record_and_artifact_bytes(tmp_path, capsys, take_id, code):
    impulse = np.zeros(1800)
    impulse[100] = 1.0
    root = bank_capture_round(tmp_path, [impulse])
    expected = window_view(root, capture_id="cloud_verify_00", rungs_ms=[5, 20])
    assert main(["sweep", str(root), "--scope", "take", "--take", take_id, "--rungs-ms", "5", "20"]) == code
    if code:
        assert json.loads(capsys.readouterr().out)["reason"] == "round_take_unknown"
    else:
        answer, _ = artifact_answer(capsys)
        assert Path(answer["out"]).read_bytes() == (render_report(expected) + "\n").encode()


def test_bass_compare_resolves_two_sets_to_the_same_take_comparison(tmp_path, capsys):
    root = bank_seat_round(tmp_path)
    inputs = round_inputs(root)
    rows = list(measurement_documents(inputs.session_dir))[:2]
    groups = [manifest_set([(row.path, record)], set_id=f"bass-{number}") for number, (row, record) in enumerate(rows)]
    write_manifest(root, program="bass", groups=groups)
    views, paths = [], []
    for number, ((row, record), group) in enumerate(zip(rows, groups)):
        take = {"record": record, "record_path": row.path, "sweep_band_hz": [20, 200], "sweep_duration_s": 1.0,
                "calibration": {}, "freqs_hz": [30, 50, 70, 100, 150], "fundamental_db": [-30 + number] * 5,
                "fundamental_qualified": [True] * 5, "harmonics": {}}
        view = {"schema": "jts_bass_view/1", "takes": [take]}
        path = default_out(inputs, root, "bass_view.json", group["set_id"])
        path.write_text(json.dumps(view))
        views.append(view)
        paths.append(path)
    expected = compare_bass_takes(*(selected_take(view, group["takes"][0]["take_id"]) for view, group in zip(views, groups)), change="diagnostic")
    assert main(["bass-compare", str(root), str(root), "--before-set", "bass-0", "--after-set", "bass-1", "--change", "diagnostic"]) == 0
    answer, actual = artifact_answer(capsys)
    assert actual == {**expected, "source_views": list(map(str, paths))}
    assert Path(answer["out"]).name == "bass_comparison-bass-1.json"

@pytest.mark.parametrize("changed", [
    {"candidate_id": "second"},
    {"graph_fingerprint": "other-applied"},
    {"provenance": {"graph": {"fingerprint": "other-played"}}},
    {"graph_scope": "room"},
    {"side": "right"},
    {"capture_setup": {"calibration": {"calibration_id": "other-mic"}}},
    {"capture_calibration": {"applied": True, "calibration_id": "same-mic", "curve_fingerprint": "changed-curve"}},
    {"capture_device": {"card": "other-card"}},
    {"provenance": {"stimulus": {"wav_sha256": "other-program"}}},
    {"level_db": -35.0},
    {"stimulus_dbfs": -20.0},
    {"program": {"program_id": "changed-gains"}}, {"loudness_volume_db": -23.0}, {"program_id": "stamped"}, {"program_id": None},
])
def test_room_views_select_one_measured_set_and_count_physical_poses(tmp_path, capsys, changed):
    round_dir = bank_seat_round(tmp_path)
    root = round_inputs(round_dir).session_dir
    captures = []
    for row, original in list(measurement_documents(root)):
        if original.get("pose_kind") != "seat":
            continue
        path = take_artifact_path(root, row.path)
        original.update(candidate_id="first", graph_scope="speaker_tune", program={"program_id": "program"}, loudness_volume_db=-30.0)
        original["curves"][0]["band_hz"] = [50.0, 200.0]
        path.write_text(json.dumps(original))
        second = json.loads(json.dumps(original))
        second.update(changed)
        second["pose_id"] += "_second"
        second["take_id"] += "_second"
        second["curves"][0]["magnitude_db"] = [-20.0] * len(SEAT_GRID_HZ)
        path.with_stem(path.stem + "_second").write_text(json.dumps(second))
        captures.append((original, second))
    original, second = captures[0]
    repeat = dict(original, pose_id="another_stop", take_id="repeated_pose", attempt=10)
    path.with_stem("repeated_pose").write_text(json.dumps(repeat))
    invalid = dict(repeat, take_id="bad_repeat", attempt=11, curves=[])
    path.with_stem("bad_repeat").write_text(json.dumps(invalid))

    groups = [manifest_set([(row.path, record) for row, record in measurement_documents(root)
                            if record.get("candidate_id") == candidate["candidate_id"] and
                            (record.get("take_id", "").endswith("_second") == (candidate is second))],
                           set_id="second" if candidate is second else "first") for candidate in (original, second)]
    write_manifest(round_dir, program="room", groups=groups)
    assert round_views.main(["room", str(round_dir)]) == round_views.EXIT_REFUSED
    refused = json.loads(capsys.readouterr().out)
    assert refused["reason"] == "round_set_unknown"
    assert not (round_dir / "room.json").exists()

    for record, level in [(original, -30.0), (second, -20.0)]:
        set_id = "first" if record is original else "second"
        out = round_dir / f"room-{set_id}.json"
        legacy = select_seat_takes(root, capture_id=record["take_id"])
        answer = _run(capsys, [
            "room", str(round_dir), "--set", set_id,
        ])
        doc = json.loads(out.read_text())["median"]
        assert doc["median_db"] == room_views.room_median(legacy.takes, room_views.room_ceiling(None))["median_db"]
        assert answer["n_positions"] == doc["n_positions"] == 7
        assert len({p["pose_key"] for p in doc["positions"]}) == 7
        assert doc["coverage_hz"] == [50.0, 200.0]
        assert min(doc["freqs_hz"]) >= 50.0 and max(doc["freqs_hz"]) <= 200.0
        assert np.allclose(doc["median_db"], level)
        median = read_room_median(doc)
        assert median.band_hz == (50.0, 200.0)
        assert median.evidence == doc["evidence"]
        expected_program = changed["program_id"] if record is second and "program_id" in changed else record["program"]["program_id"]
        assert [median.evidence["basis"][key] for key in ("program_id", "loudness_volume_db")] == [expected_program, record["loudness_volume_db"]]
        grade = _run(capsys, ["room-grade", str(round_dir), "--set", set_id])
        assert grade["evidence"] == doc["evidence"]
        assert grade["graph_scopes"] == [record["graph_scope"]]
        if record is original:
            assert "repeated_pose" in doc["evidence"]["take_ids"]
            assert original["take_id"] in doc["evidence"]["superseded_take_ids"]
            assert [r["take_id"] for r in doc["evidence"]["omitted_takes"]] == ["bad_repeat"]


def _run(capsys, argv):
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("view", ["forward-model", "bass-compare"])
def test_single_take_views_refuse_an_ambiguous_set(two_sets, capsys, view):
    root, manifest = two_sets
    first, second = (group["set_id"] for group in manifest["sets"])
    argv = ([view, str(root), "--set", first] if view == "forward-model" else
            [view, str(root), str(root), "--before-set", first, "--after-set", second, "--change", "candidate"])
    assert main(argv) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["reason"] == "round_take_selection_required"
    assert answer["detail"]["set_id"] == first
    assert answer["reason"] in REASON_REGISTRY
