# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest selection, artifact isolation, and the retired CLI doors."""

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bass_comparison import compare_bass_takes, selected_take
from jasper.active_speaker.crossover_v2 import room_views
from jasper.active_speaker.measurement_bass import BASS_VIEW_SCHEMA
from jasper.active_speaker.crossover_v2.room_prescription import read_room_median
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.crossover_v2.round_inputs import (
    COMPARAND_EARLIER_ROUND, COMPARAND_SAME_ROUND, SetTakes, default_out, round_artifact_dir, round_inputs, with_records,
)
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_BASS_COMPARAND_VIEW_NOT_FILED, REFUSE_COMPARE_NO_COMPARAND,
)
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.crossover_v2.window_view import window_view
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME, kept_measurements
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED
from jasper.cli._refusal import EXIT_REFUSED
from jasper.cli._report import render_report
from jasper.cli.round_views import build_parser, main, run_bookkeeping
from jasper.cli.round_views._common import RoundSetRefused, resolve_set
from tests.crossover_v2_banked_round import bank_seat_round, SEAT_GRID_HZ
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents
from tests.test_take_reading import _banked


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
        group["takes"].append({**group["takes"][0], "take_id": f"refused-{number}", "selected": False})
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
    if case == "ambiguous":
        for group, role in zip(manifest["sets"], ("woofer", "tweeter")):
            group["capture_basis"]["role"] = role
        path.write_text(json.dumps(manifest))
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
    if case == "ambiguous":
        assert answer["detail"]["sets"] == [
            {"set_id": group["set_id"], "candidate_id": group["capture_basis"].get("candidate_id"),
             "role": group["capture_basis"]["role"], "take_count": sum(take["selected"] for take in group["takes"])}
            for group in manifest["sets"]
        ]


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


_BASS = {"bass", "bass-compare", "bass-fit-table"}
_SPEAKER = {"directivity", "delay-landscape", "distortion", "classify-features"}


@pytest.mark.parametrize("program,listed,excluded", [
    ("speaker", _SPEAKER, _BASS | {"room", "room-grade"}),
    ("room", {"room", "room-grade"}, _SPEAKER | _BASS),
    ("bass", _BASS, _SPEAKER | {"room", "room-grade"}),
])
def test_catalog_lists_a_rounds_views_for_its_programs_only(tmp_path, capsys, program, listed, excluded):
    """A round's catalog lists the views of its own programs: a speaker round
    keeps no room sweep, so no room view reads it (ADR-0400)."""
    root = bank_seat_round(tmp_path)
    write_manifest(root, program=program)

    assert main(["catalog", str(root)]) == 0
    views = {tool["tool"].split()[1] for tool in json.loads(capsys.readouterr().out)["tools"]
             if tool["tool"].startswith("jasper-round-views ")}

    assert listed <= views and views.isdisjoint(excluded)


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
    ["directivity", "round"],
    ["repeat", "a", "b"], ["delay-landscape", "bundle", "--fc-hz", "1800"],
    ["dsp-replay", "graph", "stimulus", "--main-db", "-30"],
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


@pytest.mark.parametrize("named_sets,named_takes", [(True, False), (True, True), (False, True)],
                         ids=["sets", "sets and takes", "takes alone"])
def test_bass_compare_resolves_two_sets_to_the_same_take_comparison(tmp_path, capsys, named_sets, named_takes):
    """However its sides are named, the comparison files under the set its after side resolved
    to, as the bank files that set's views; so two comparisons never share a file."""
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
        view = {"schema": BASS_VIEW_SCHEMA, "takes": takes}
        path = default_out(inputs, root, "bass_view.json", group["set_id"])
        path.write_text(json.dumps(view))
        views.append(view)
        paths.append(path)
    ids = [group["takes"][int(named_takes)]["take_id"] for group in groups]
    expected = compare_bass_takes(*(selected_take(view, take_id) for view, take_id in zip(views, ids)), change="diagnostic")
    flags = [*(["--before-set", "bass-0", "--after-set", "bass-1"] if named_sets else []),
             *(["--before-take", ids[0], "--after-take", ids[1]] if named_takes else [])]
    assert main(["bass-compare", str(root), str(root), *flags, "--change", "diagnostic"]) == 0
    answer, actual = artifact_answer(capsys)
    assert actual == {**expected, "comparand": None, "source_views": list(map(str, paths))}
    assert Path(answer["out"]).name == "bass_comparison-bass-1.json"
    assert not (root / "bass_comparison.json").exists()


@pytest.mark.parametrize("rounds,flags,expected", [
    ({"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}}, ["--after-set", "cand"], (0, COMPARAND_SAME_ROUND, "r", "r0")),
    ({"e": {"base": [("e0", 0)]}, "r": {"cand": [("c0", 0)]}}, [], (0, COMPARAND_EARLIER_ROUND, "e", "e0")),
    ({"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}}, ["--before-set", "base", "--after-set", "cand"],
     (0, None, "r", "r0")),
    ({"e": {"base": [("e30", 30)]}, "r": {"cand": [("c0", 0)]}}, [], (EXIT_REFUSED, REFUSE_COMPARE_NO_COMPARAND, None, "c0")),
    ({"room": {"base": [("s0", 0)]}, "r": {"cand": [("c0", 0)]}}, [],
     (EXIT_REFUSED, REFUSE_BASS_COMPARAND_VIEW_NOT_FILED, "room", "s0")),
], ids=["same-round-base", "earlier-round", "before-side-named", "none", "comparand-files-no-bass-view"])
@pytest.mark.parametrize("probe", [False, True], ids=["", "probed"])
def test_bass_compare_with_no_before_side_reads_the_after_takes_comparand(tmp_path, capsys, rounds, flags, expected,
                                                                          probe):
    """ADR-0391: one round named with no --before-* flag is the after take's, and
    the before take is its comparand, read from the bass view its round filed;
    the answer says how it was found. With none, or with a comparand whose round
    filed no bass view, bass-compare refuses by name. A round's run probe files
    no view, so its one measured set's view keeps no set name (ADR-0403 §4)."""
    store = tmp_path / "campaigns"
    paths = {}
    for day, (name, sets) in enumerate(rounds.items()):
        root = paths[name] = _banked(store, name, f"2026-09-{20 + day}T12:00:00Z", sets, probe=probe)
        if name == "room":
            continue  # A room round files no bass view.
        inputs = round_inputs(root)
        records = {record["take_id"]: (row.path, record) for row, record in measurement_documents(inputs.session_dir)}
        for set_id, takes in sets.items():
            view = {"schema": BASS_VIEW_SCHEMA, "takes": [
                {"record": {**records[take_id][1], "pose_kind": "bearing", "vertical_deg": 0},
                 "record_path": records[take_id][0], "sweep_band_hz": [20, 200], "sweep_duration_s": 1.0,
                 "calibration": {}, "freqs_hz": [30, 50, 70, 100, 150], "fundamental_db": [-30.0] * 5,
                 "fundamental_qualified": [True] * 5, "harmonics": {}} for take_id, *_ in takes]}
            # The bank files a set's view under the set's name only in a round of more than one set.
            default_out(inputs, root, "bass_view.json", set_id if len(sets) > 1 else None).write_text(json.dumps(view))

    code = main(["bass-compare", str(paths["r"]), *flags, "--change", "diagnostic"])
    answer = json.loads(capsys.readouterr().out)

    if code:
        detail = answer["detail"]
        assert (code, answer["reason"], detail.get("round_id"), detail["take_id"]) == expected
    else:
        before, _after = answer["subject"]["rounds"]
        assert (code, answer["comparand"], before["round_id"], *before["take_ids"]) == expected


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
    {"program": {"stimulus_id": "changed-gains"}}, {"stimulus_id": "stamped"}, {"stimulus_id": None},
])
def test_room_views_select_one_measured_set_and_count_physical_poses(tmp_path, capsys, changed):
    round_dir = bank_seat_round(tmp_path)
    root = round_inputs(round_dir).session_dir
    captures = []
    for row, original in list(measurement_documents(root)):
        if original.get("pose_kind") != "seat":
            continue
        path = take_artifact_path(root, row.path)
        original.update(candidate_id="first", graph_scope="candidate", program={"stimulus_id": "program"})
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
            legacy.takes, room_views.room_ceiling(),
        )["median_db"]
        assert answer["n_positions"] == doc["n_positions"] == 7
        assert len({p["pose_key"] for p in doc["positions"]}) == 7
        assert doc["coverage_hz"] == [doc["freqs_hz"][0], doc["freqs_hz"][-1]] == [30.0, 200.0]
        assert np.allclose(doc["median_db"], level)
        median = read_room_median(doc)
        assert median.band_hz == (30.0, 200.0)
        assert median.evidence == doc["evidence"]
        expected_program = changed["stimulus_id"] if record is second and "stimulus_id" in changed else record["program"]["stimulus_id"]
        assert median.evidence["basis"]["stimulus_id"] == expected_program
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


def test_a_take_names_the_set_that_holds_it_kept_or_not(two_sets):
    root, manifest = two_sets
    inputs = round_inputs(root)
    for group in manifest["sets"]:
        for take in group["takes"]:
            assert resolve_set(inputs, take=take["take_id"]).set_id == group["set_id"]


def test_a_take_two_sets_hold_needs_the_set_named_among_those_that_hold_it(two_sets):
    """A take that recorded two roles is a row in the set of each, so it cannot say which."""
    root, manifest = two_sets
    first, second = manifest["sets"]
    shared = first["takes"][0]
    third = {**first, "set_id": "third", "takes": [{**take, "take_id": f"third-{take['take_id']}"} for take in first["takes"]]}
    sets = [first, {**second, "takes": [*second["takes"], shared]}, third]

    with pytest.raises(RoundSetRefused) as refused:
        resolve_set(round_inputs(root), take=shared["take_id"], manifest={**manifest, "sets": sets})

    assert refused.value.reason == "set_required"
    assert [row["set_id"] for row in refused.value.detail["sets"]] == [first["set_id"], second["set_id"]]


def test_a_take_no_set_holds_is_unknown_and_the_refusal_lists_the_kept_takes(two_sets):
    root, manifest = two_sets

    with pytest.raises(RoundSetRefused) as refused:
        resolve_set(round_inputs(root), take="no-such-take")

    assert refused.value.reason == "round_take_unknown"
    assert refused.value.detail["take_ids"] == tuple(
        take["take_id"] for group in manifest["sets"] for take in group["takes"] if take["selected"])


@pytest.mark.parametrize("poses,selected,requested,expected", [
    ([(0, 0), (15, 0)], [True, True], None, "take-0"),
    ([(15, 0), (0, 0)], [True, True], None, "take-1"),
    ([(0, 0), (15, 0)], [True, True], "take-1", "take-1"),
    ([(0, 10), (15, 0)], [True, True], None, "round_take_selection_required"),
    ([(0, 0), (0, 0)], [True, True], None, "round_take_selection_required"),
    ([(None, None), (None, None)], [True, True], None, "round_take_selection_required"),
    ([(0, 0), (15, 0), (30, 0)], [False, True, True], None, "round_take_selection_required"),
    ([(0, 0), (15, 0)], [False, True], "take-0", "round_take_not_kept"),
    ([(0, 0), (15, 0)], [True, True], "missing", "round_take_unknown"),
])
def test_single_take_defaults_and_overrides(two_sets, poses, selected, requested, expected):
    root, manifest = two_sets
    group = manifest["sets"][0]
    unkept = {"measurement_status": "captured", "verdict": {"ok": False, "fault": "level_off_target", "next": "retake_louder"}}
    group["takes"] = [{**group["takes"][0], "take_id": f"take-{i}", "selected": keep,
                       "pose": {"kind": "bearing", "azimuth_deg": deg, "elevation_deg": elevation}, **({} if keep else unkept)}
                      for i, ((deg, elevation), keep) in enumerate(zip(poses, selected))]
    resolved = resolve_set(round_inputs(root), group["set_id"], manifest=manifest)
    if expected.startswith("round_"):
        with pytest.raises(RoundSetRefused) as refused:
            resolved.take_id(requested)
        assert refused.value.reason == expected
        assert refused.value.detail["take_ids"] == tuple(f"take-{i}" for i, keep in enumerate(selected) if keep)
        if expected == "round_take_not_kept":
            assert {key: refused.value.detail[key] for key in ("status", "fault", "next")} == {
                "status": "captured", "fault": "level_off_target", "next": "retake_louder"}
    else:
        assert resolved.take_id(requested) == expected


def test_a_joined_timing_take_is_no_take_of_its_set(two_sets):
    """The packet index walks every set of its joined manifest, so a joined take
    of the timing phase (ADR-0319) is no take of its set."""
    root, manifest = two_sets
    group = manifest["sets"][0]
    session = round_inputs(root).session_dir
    record = take_artifact_path(session, group["takes"][0]["record_id"])
    record.write_text(json.dumps({**json.loads(record.read_text()), "phase": "timing"}))

    joined = SetTakes.from_row(with_records(session, manifest)["sets"][0])

    assert group["takes"][0]["take_id"] not in joined.selected_ids


@pytest.mark.parametrize("reader", ["kept_measurements", "bank", "room", "catalog"])
def test_a_round_banked_before_pointer_rows_refuses_by_that_field(tmp_path, capsys, reader):
    """A manifest banked before the rows became pointers names its preset
    ``program`` and each record under ``artifacts``, and no reader reads that
    shape (#2902): the round's kept takes, its bank and its views refuse
    ``take_curves_not_banked`` by the rows' missing ``record_id`` (ADR-0395)."""
    root = bank_seat_round(tmp_path)
    session = round_inputs(root).session_dir
    directory, _ = round_artifact_dir(session)
    path = directory / RUN_MANIFEST_FILENAME
    manifest = json.loads(path.read_text())
    for group in manifest["sets"]:
        group["takes"] = [{"take_id": take["take_id"], "selected": take["selected"],
                           "artifacts": {"record_id": take["record_id"]}} for take in group["takes"]]
    old = {"program" if key == "preset" else key: value for key, value in manifest.items()}
    path.write_text(json.dumps({**old, "schema_version": 2}))

    if reader in ("room", "catalog"):
        assert main([reader, str(root)]) == EXIT_REFUSED
        answer = json.loads(capsys.readouterr().out)
        refusal = answer["reason"], answer["detail"]["field"]
    else:
        with pytest.raises(RoundSetRefused) as refused:
            if reader == "bank":
                mark_state(session, "applied")
                bank_round(session, campaign_root=tmp_path / "bank", view_runner=run_bookkeeping)
            else:
                list(kept_measurements(session, phases=("lateral",), purposes=("room",)))
        refusal = refused.value.reason, refused.value.detail["field"]
    assert refusal == (TAKE_CURVES_NOT_BANKED, "record_id")
