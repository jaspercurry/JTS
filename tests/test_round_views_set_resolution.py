# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest selection, artifact isolation, and the retired CLI doors."""

import argparse
import json
import shlex
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take
from jasper.active_speaker.crossover_v2 import room_views
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.room_prescription import read_room_median
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.crossover_v2.round_inputs import default_out, round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.window_view import window_view
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.json_fields import sha256_file
from jasper.cli._report import render_report
from jasper.cli.round_views import ARTIFACT_BY_VIEW, _FAMILIES, build_parser, main
from jasper.cli.round_views._common import RoundSetRefused, VIEW_PURPOSES, resolve_set
from tests.crossover_v2_banked_round import bank_seat_round, SEAT_GRID_HZ
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents


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
    ("unknown", "round_set_unknown"), ("ambiguous", "set_required"),
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
    ("speaker", ("room", "room-grade")),
    ("room", ("room", "room-grade")),
    ("bass", ("bass",)),
])
def test_inventory_groups_and_orders_the_program(tmp_path, capsys, program, first):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program=program)
    assert main(["inventory", str(root)]) == 0
    answer, doc = artifact_answer(capsys)
    assert answer["program"] == doc["program"] == program
    assert all(row["program"] == program for row in doc["artifacts"])
    assert tuple(row["view"] for row in doc["artifacts"][:len(first)]) == first


@pytest.mark.parametrize("program,excluded", [
    ("speaker", {"bass", "bass-compare", "bass-fit-table"}),
    ("room", {"entry", "directivity", "delay-landscape",
              "close-reference", "distortion", "classify-features",
              "bass", "bass-compare", "bass-fit-table"}),
    ("bass", {"entry", "directivity", "delay-landscape",
              "close-reference", "distortion", "classify-features", "room", "room-grade"}),
])
def test_inventory_excludes_views_for_other_programs(tmp_path, capsys, program, excluded):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program=program)

    assert main(["inventory", str(root)]) == 0
    _, document = artifact_answer(capsys)

    assert {row["view"].split()[0] for row in document["artifacts"]}.isdisjoint(excluded)


def test_every_registered_view_family_has_purposes_and_help_tag():
    registered = set()
    scratch = argparse.ArgumentParser()
    subparsers = scratch.add_subparsers()
    for family in _FAMILIES:
        before = set(subparsers.choices)
        family.add_parser(subparsers)
        added = set(subparsers.choices) - before
        assert added <= VIEW_PURPOSES.keys()
        registered.update(added)

    parser = build_parser()
    choices = next(action for action in parser._actions if action.dest == "command")

    assert set(choices.choices) == registered
    assert all(choice.help.startswith("[") for choice in choices._choices_actions)


def _producer_argv(view, takes):
    values = {
        "<db>": "1", "<start>": "0", "<stop>": "1", "<distance-m>": "1",
        "<change>": "candidate",
    }
    return [*view.split(), *(token if token.startswith("--") else values.get(token, "value") for token in takes)]


@pytest.mark.parametrize(
    "view,takes",
    [(view, spec.takes) for view, spec in ARTIFACT_BY_VIEW.items() if spec.producer is None],
)
def test_registered_view_producer_tokens_are_accepted(view, takes):
    _, unknown = build_parser().parse_known_args(_producer_argv(view, takes))
    assert unknown == []


@pytest.mark.parametrize("argv", [
    ["windows", "round"], ["gate-sweep", "round"], ["spec-sweep", "round"],
    ["frozen", "baseline", "round"], ["per-seat", "round"], ["repeat-floor", "a", "b", "--install"],
    ["cloud-binding", "round"], ["findings", "round"], ["sweep", "round", "--scope", "verdict"],
    ["sweep", "round", "--scope", "take", "--capture-id", "take"],
    ["room", "round", "--capture-id", "take"],
    ["room-grade", "round", "--room-median", "median.json"],
    ["room-grade", "round", "--baseline-room-median", "median.json"],
])
def test_retired_verbs_and_selectors_are_unknown(argv):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["entry", "round"], ["directivity", "round"],
    ["repeat", "a", "b"], ["delay-landscape", "bundle", "--fc-hz", "1800"],
    ["dsp-replay", "graph", "stimulus", "--main-db", "-30", "--bass-reference-db", "-30"],
    ["dsp-levels", "manifest.json", "--raw", "output.f64le", "--window-s", "0", "1"],
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


@pytest.mark.parametrize("scope", ["round", "take"])
def test_sweep_reads_manifest_curves_when_position_sidecars_have_none(tmp_path, capsys, scope):
    impulse = np.zeros(1800)
    impulse[100] = 1.0
    root = bank_capture_round(tmp_path, [impulse] * 3)
    session = round_inputs(root).session_dir
    directory, _ = round_artifact_dir(session)
    positions = directory / "positions"
    positions.mkdir()
    rows, curves = [], {}
    for path in sorted(session.glob("summed/*.json")):
        record = json.loads(path.read_text())
        record.update(kind=POSITION_EVIDENCE_KIND, take_id=record.pop("position_id"),
                      wav_sha256=sha256_file(session / record["wav_path"]))
        curves[record["take_id"]], = record.pop("curves")
        destination = positions / path.name
        destination.write_text(json.dumps(record))
        path.unlink()
        rows.append((str(destination.relative_to(session / "evidence/v1/artifacts")), record))
    group = manifest_set(rows)
    for take in group["takes"]:
        take.update(role="summed", curve=curves[take["take_id"]])
    write_manifest(root, groups=[group])
    flags = ["--take", group["takes"][0]["take_id"]] if scope == "take" else []
    assert main(["sweep", str(root), "--scope", scope, "--set", group["set_id"], *flags]) == 0
    answer, report = artifact_answer(capsys)
    assert answer["scope"] == scope
    if scope == "round":
        assert {pose["capture_id"] for pose in report["poses"]} == set(curves)
    else:
        assert answer["capture_id"] == group["takes"][0]["take_id"]


@pytest.mark.parametrize("override", [False, True])
def test_bass_compare_resolves_two_sets_to_the_same_take_comparison(tmp_path, capsys, override):
    root = bank_seat_round(tmp_path)
    inputs = round_inputs(root)
    rows = list(measurement_documents(inputs.session_dir))[:4]
    for index, (_, record) in enumerate(rows):
        record.update(pose_kind="bearing", position_deg=15.0 if index % 2 else 0.0, vertical_deg=0.0)
    groups = [manifest_set([(row.path, record) for row, record in rows[start:start + 2]],
                           set_id=f"bass-{number}") for number, start in enumerate((0, 2))]
    write_manifest(root, program="bass", groups=groups)
    views, paths = [], []
    for number, group in enumerate(groups):
        takes = [{"record": record, "record_path": row.path, "sweep_band_hz": [20, 200], "sweep_duration_s": 1.0,
                  "calibration": {}, "freqs_hz": [30, 50, 70, 100, 150], "fundamental_db": [-30 + number + i] * 5,
                  "fundamental_qualified": [True] * 5, "harmonics": {}}
                 for i, (row, record) in enumerate(rows[number * 2:number * 2 + 2])]
        view = {"schema": "jts_bass_view/1", "takes": takes}
        path = default_out(inputs, root, "bass_view.json", group["set_id"])
        path.write_text(json.dumps(view))
        views.append(view)
        paths.append(path)
    ids = [group["takes"][int(override)]["take_id"] for group in groups]
    expected = compare_bass_takes(*(selected_take(view, take_id) for view, take_id in zip(views, ids)), change="diagnostic")
    flags = ["--before-take", ids[0], "--after-take", ids[1]] if override else []
    assert main(["bass-compare", str(root), str(root), "--before-set", "bass-0", "--after-set", "bass-1", "--change", "diagnostic", *flags]) == 0
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
        original.update(candidate_id="first", graph_scope="candidate", program={"program_id": "program"}, loudness_volume_db=-30.0)
        original["curves"][0]["band_hz"] = [30.0, 200.0]
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
    assert main(["room", str(round_dir)]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["reason"] == "set_required"
    assert not (round_dir / "room.json").exists()

    for record, level in [(original, -30.0), (second, -20.0)]:
        set_id = "first" if record is original else "second"
        out = round_dir / f"room-{set_id}.json"
        legacy = select_seat_takes(root, capture_id=record["take_id"])
        answer = _run(capsys, [
            "room", str(round_dir), "--set", set_id,
        ])
        document = json.loads(out.read_text())
        doc = document["median"]
        assert document["persistence"]["n_positions"] == doc["n_positions"]
        assert doc["median_db"] == room_views.room_median(
            legacy.takes, room_views.room_ceiling(root),
        )["median_db"]
        assert answer["n_positions"] == doc["n_positions"] == 7
        assert len({p["pose_key"] for p in doc["positions"]}) == 7
        assert doc["coverage_hz"] == [doc["freqs_hz"][0], doc["freqs_hz"][-1]] == [30.0, 200.0]
        assert np.allclose(doc["median_db"], level)
        median = read_room_median(doc)
        assert median.band_hz == (30.0, 200.0)
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


def test_single_take_views_refuse_an_ambiguous_set(two_sets, capsys):
    root, manifest = two_sets
    first, second = (group["set_id"] for group in manifest["sets"])
    argv = ["bass-compare", str(root), str(root), "--before-set", first, "--after-set", second, "--change", "candidate"]
    assert main(argv) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["reason"] == "round_take_selection_required"
    assert answer["detail"]["set_id"] == first
    assert answer["reason"] in REASON_REGISTRY


@pytest.mark.parametrize("named", [False, True])
def test_inventory_reads_one_manifest_and_uses_optional_set_arguments(two_sets, capsys, monkeypatch, named):
    root, manifest = two_sets
    if not named:
        write_manifest(root, groups=manifest["sets"][:1])
    reads = []
    read_text = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == RUN_MANIFEST_FILENAME:
            reads.append(path)
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert main(["inventory", str(root)]) == 0
    _, doc = artifact_answer(capsys)
    assert len(reads) == 1
    for row in doc["artifacts"]:
        if row["view"] == "bass-compare":
            assert row["required_inputs"] == ["<before-round>", "<change>"]
            tokens = shlex.split(row["next_command"])
            assert "--before-set" not in tokens
            assert ("--after-set" in tokens) == named


@pytest.mark.parametrize("poses,selected,requested,expected", [
    ([(0, 0), (15, 0)], [True, True], None, "take-0"),
    ([(15, 0), (0, 0)], [True, True], None, "take-1"),
    ([(0, 0), (15, 0)], [True, True], "take-1", "take-1"),
    ([(0, 10), (15, 0)], [True, True], None, "round_take_selection_required"),
    ([(0, 0), (0, 0)], [True, True], None, "round_take_selection_required"),
    ([(None, None), (None, None)], [True, True], None, "round_take_selection_required"),
    ([(0, 0), (15, 0), (30, 0)], [False, True, True], None, "round_take_selection_required"),
    ([(0, 0), (15, 0)], [False, True], "take-0", "round_take_unknown"),
    ([(0, 0), (15, 0)], [True, True], "missing", "round_take_unknown"),
])
def test_single_take_defaults_and_overrides(two_sets, poses, selected, requested, expected):
    root, manifest = two_sets
    group = manifest["sets"][0]
    group["takes"] = [{**group["takes"][0], "take_id": f"take-{i}", "selected": keep,
                       "pose": {"kind": "bearing", "deg": deg, "elevation_deg": elevation}}
                      for i, ((deg, elevation), keep) in enumerate(zip(poses, selected))]
    resolved = resolve_set(round_inputs(root), group["set_id"], manifest=manifest)
    if expected.startswith("round_"):
        with pytest.raises(RoundSetRefused) as refused:
            resolved.take_id(requested)
        assert refused.value.reason == expected
        assert refused.value.detail["take_ids"] == tuple(f"take-{i}" for i, keep in enumerate(selected) if keep)
    else:
        assert resolved.take_id(requested) == expected


def test_set_selection_excludes_entry_baseline_takes(two_sets):
    root, manifest = two_sets
    group = manifest["sets"][0]
    group["takes"][0]["phase"] = "entry_baseline"

    selected = resolve_set(round_inputs(root), group["set_id"], manifest=manifest)

    assert group["takes"][0]["take_id"] not in selected.selected_ids
