# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room medians, capture selection, and persistence across seats."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import room_views
from jasper.active_speaker.round_bank import _bookkeeping
from jasper.active_speaker.round_packet import write_round_packet
from tests.test_round_views_speaker_fit import speaker_round as speaker_round
from jasper.active_speaker.crossover_v2.prescription_contract import prescription_contracts
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.measurement_analysis import MeasurementAnalysisRefused
from tests.run_manifest_fixture import manifest_set, write_manifest
from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir, round_inputs
from jasper.audio_measurement.evidence_reasons import REASON_TOO_FEW_POSITIONS
from jasper.audio_measurement.room_boundary import (
    ROOM_BOUNDARY_DEFAULT_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    room_ceiling_hz,
)
from jasper.cli import crossover_prescriber, round_views
from jasper.cli.round_views import room
from jasper.cli.round_views._common import resolve_set
from jasper.active_speaker.crossover_v2.room_selection import is_purpose_take, select_seat_takes
from jasper.active_speaker.crossover_v2.room_prescription import (
    read_room_median, read_room_prescription,
)
from jasper.active_speaker.crossover_v2.room_grade import grade_room_median
from jasper.audio_measurement import room_limits
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry, boundary_prior
from tests.test_active_speaker_audition import _applied_profile
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_active_speaker_baseline_profile import _ROOM_CORRECTION
from tests.test_crossover_v2_room_prescription import _document
from tests.crossover_v2_banked_round import SEAT_GRID_HZ, bank_measure_round, bank_seat_round
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents

_QUIET_HZ = 200.0


def _bump(centre_hz: float, depth_db: float) -> np.ndarray:
    return depth_db * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / centre_hz) / 0.12) ** 2)


def _cube() -> list[np.ndarray]:
    return [
        np.full(SEAT_GRID_HZ.shape, -30.0) + i + _bump(63.0, -8.0)
        + (_bump(100.0, 6.0) if i < 3 else 0.0)
        for i in range(7)
    ]


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict:
    assert round_views.main(argv) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def room_round(tmp_path):
    root = bank_seat_round(tmp_path, magnitudes_db=_cube())
    session = round_inputs(root).session_dir
    group = manifest_set([(row.path, record) for row, record in measurement_documents(session)])
    write_manifest(root, program="room", groups=[{**group, "base": True}])
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    profile["recomposition_snapshot"]["room_correction"] = _ROOM_CORRECTION
    (root / "applied-profile.json").write_text(json.dumps(profile))
    return root


@pytest.mark.parametrize("purposes,expected", [(("rear", "room"), True), (("room",), False)])
def test_rear_summed_take_requires_an_accepted_purpose(tmp_path, purposes, expected):
    root = bank_seat_round(tmp_path, magnitudes_db=_cube()[:1])
    row, record = next(measurement_documents(round_inputs(root).session_dir))
    record = {**record, "measurement_purpose": "rear"}
    assert is_purpose_take(row, record, purposes) is expected


@pytest.mark.parametrize("n_positions,sufficient,reason", [
    (1, False, REASON_TOO_FEW_POSITIONS), (2, True, ""), (3, True, ""),
])
def test_room_views_disclose_spatial_support(tmp_path, capsys, n_positions, sufficient, reason):
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube()[:n_positions])
    support = {"n_positions": n_positions, "sufficient": sufficient, "reason": reason}
    median = _run(capsys, ["room", str(round_dir)])
    doc = json.loads((round_dir / "room.json").read_text())["median"]
    features = json.loads((round_dir / "room.json").read_text())["persistence"]

    assert median["spatial_support"] == doc["spatial_support"] == support
    assert features["spatial_support"] == support
    assert all(feature["presence_fraction"] == 1.0 for feature in features["features"])
    at = int(np.argmin(np.abs(np.asarray(doc["freqs_hz"]) - _QUIET_HZ)))
    assert doc["median_db"][at] == pytest.approx(-30.0 + (n_positions - 1) / 2)
    if sufficient:
        assert isinstance(doc["spread_db"][at], float)
        assert doc["spread_db"][at] == pytest.approx(np.std(np.arange(n_positions)))
    else:
        assert doc["spread_db"] is None


@pytest.mark.parametrize("floor_hz", [20.0, 30.0, 60.0, 120.0, 150.0])
@pytest.mark.parametrize("n_positions", [1, 3])
def test_room_band_follows_coverage_and_support_counts_positions(tmp_path, floor_hz, n_positions):
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube()[:n_positions])
    inputs = round_inputs(round_dir)
    selected = select_seat_takes(inputs.session_dir)
    takes = tuple(replace(take, band_hz=(floor_hz, take.band_hz[1])) for take in selected.takes)
    room = room_views.room_document(
        takes, set_id="base", evidence=selected.evidence, bundle_dir=inputs.session_dir,
        applied_profile_path=None, geometry_path=None, manifest={},
    )
    document, persistence = room["median"], room["persistence"]

    support = {"n_positions": n_positions, "sufficient": n_positions > 1,
               "reason": "" if n_positions > 1 else REASON_TOO_FEW_POSITIONS}
    assert document["freqs_hz"][0] == document["coverage_hz"][0] == persistence["coverage_hz"][0] == floor_hz
    assert document["spatial_support"] == persistence["spatial_support"] == support
    freqs = np.asarray(document["freqs_hz"])
    band = (freqs >= floor_hz) & (freqs < document["ceiling_hz"])
    assert "spread_rms_db" not in document
    assert room["spread_rms_db"] == (
        pytest.approx(np.sqrt(np.mean(np.asarray(document["spread_db"])[band] ** 2)))
        if n_positions > 1 else None)
    assert all(feature["band_hz"][0] >= floor_hz for feature in persistence["features"])
    median = read_room_median(document)
    assert median.coverage_hz == pytest.approx(document["coverage_hz"])
    bands = grade_room_median(median).bands
    assert [band.lo_hz for band in bands] == [floor_hz, *(split for split in (60.0, 120.0) if split > floor_hz)]
    assert all(band.n_bins > 0 for band in bands)


def test_room_persistence_counts_what_holds_across_the_cube(tmp_path: Path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube())

    _run(capsys, ["room", str(round_dir)])
    doc = json.loads((round_dir / "room.json").read_text())["persistence"]
    by_kind = {feature["kind"]: feature for feature in doc["features"]}
    assert set(by_kind) == {"dip", "peak"}
    assert by_kind["dip"]["centre_hz"] == pytest.approx(63.0, rel=0.05)
    assert by_kind["dip"]["median_depth_db"] == pytest.approx(-8.0, abs=1.5)
    assert (by_kind["dip"]["n_present"], by_kind["dip"]["presence_fraction"]) == (7, 1.0)
    assert by_kind["peak"]["n_present"] == 3
    assert by_kind["peak"]["presence_fraction"] == pytest.approx(3 / 7)


def test_a_round_with_no_seat_takes_is_refused_by_name(tmp_path: Path, capsys) -> None:
    round_dir = bank_measure_round(tmp_path)

    code = round_views.main(["room", str(round_dir)])

    assert code == round_views.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == room.REFUSE_NO_SEAT_TAKES


@pytest.mark.parametrize("trusted_floor_hz,ceiling_hz", [
    (None, ROOM_BOUNDARY_DEFAULT_HZ), (150.0, ROOM_BOUNDARY_MIN_HZ),
    (325.0, 325.0), (1000.0, ROOM_BOUNDARY_MAX_HZ),
])
def test_the_ceiling_is_the_trusted_floor_clamped(trusted_floor_hz, ceiling_hz) -> None:
    """ADR-0256 rule 1, at the module that owns the bounds."""
    assert room_ceiling_hz(trusted_floor_hz) == ceiling_hz


def _add_gated_take(round_dir: Path, *, take_id: str, floor_hz: float) -> None:
    bundle = round_inputs(round_dir).session_dir
    row, record = next(iter(measurement_documents(bundle)))
    gated = {
        **record,
        "take_id": take_id,
        "position_id": take_id,
        "phase": "measure",
        "measurement_purpose": "speaker",
        "gating_applied": True,
        "curves": [{**record["curves"][0], "trusted_floor_hz": floor_hz}],
    }
    take_artifact_path(bundle, row.path).with_name(f"{take_id}.json").write_text(json.dumps(gated))


def test_the_ceiling_uses_the_highest_round_gate_and_discloses_its_take(tmp_path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path)
    _add_gated_take(round_dir, take_id="gate-low", floor_hz=350.0)
    _add_gated_take(round_dir, take_id="gate-source", floor_hz=357.1428571428571)

    _run(capsys, ["room", str(round_dir)])
    ceiling = json.loads((round_dir / "room.json").read_text())["ceiling"]

    assert ceiling["hz"] == pytest.approx(357.1428571428571)
    assert ceiling["provenance"] == {
        "ceiling_hz": pytest.approx(357.1428571428571),
        "ceiling_source": "round_gate",
        "trusted_floor_hz": pytest.approx(357.1428571428571),
        "source_take_id": "gate-source",
        "source_curve_role": "summed",
        "clamp_hz": [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ],
        "reason": "",
    }


def test_a_pure_room_round_uses_the_fixed_ceiling(tmp_path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path)

    _run(capsys, ["room", str(round_dir)])
    ceiling = json.loads((round_dir / "room.json").read_text())["ceiling"]

    assert ceiling == {"hz": ROOM_BOUNDARY_DEFAULT_HZ, "provenance": {
        "ceiling_hz": ROOM_BOUNDARY_DEFAULT_HZ,
        "ceiling_source": "fallback",
        "trusted_floor_hz": None,
        "source_take_id": None,
        "source_curve_role": None,
        "clamp_hz": [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ],
        "reason": "the round has no gated summed or driver take",
    }}


def test_room_views_accept_explicit_arm_positions_and_exclude_speaker_takes(tmp_path, capsys):
    round_dir = bank_seat_round(tmp_path)
    root = round_inputs(round_dir).session_dir
    seats = [(row, record) for row, record in measurement_documents(root) if record.get("pose_kind") == "seat"]
    for index, (row, record) in enumerate(seats):
        record.update(pose_kind="bearing", position_deg=[0, -20, 20][index % 3],
                      mark_distance_m=1.0, measurement_purpose="room" if index < 3 else "speaker")
        record.pop("seat_offset_m")
        take_artifact_path(root, row.path).write_text(json.dumps(record))
    write_manifest(round_dir, program="room")
    _run(capsys, ["room", str(round_dir)])
    result = json.loads((round_dir / "room.json").read_text())["median"]
    assert result["n_positions"] == 3
    assert result["window"] == "ungated"
    assert "pose_kind" not in result["evidence"]["basis"]
    assert len(set(result["evidence"]["pose_keys"])) == 3


@pytest.mark.parametrize("view,copied_calibration", [
    ("room", False), ("room", True), ("room-grade", True), ("bookkeeping", False),
])
def test_room_views_analyze_wired_takes_without_banked_curves(
    tmp_path, capsys, analyzed_room_documents, view, copied_calibration,
):
    root = bank_seat_round(tmp_path, magnitudes_db=_cube()[:3])
    bundle = round_inputs(root).session_dir
    takes = list(analyzed_room_documents.side_effect(bundle))
    for take, bearing in zip(takes, (0, -20, 20)):
        record = take.document()
        record.update(pose_kind="bearing", position_deg=bearing, mark_distance_m=1.0,
                      measurement_purpose="room")
        record.pop("seat_offset_m")
        (bundle / take.record_path).write_text(json.dumps({**record, "curves": []}))
    write_manifest(root, program="room")
    analyzed_room_documents.side_effect = lambda *args, **kwargs: iter(takes)
    calibration_root = tmp_path / "calibration" if copied_calibration else None
    if view == "bookkeeping":
        for verb in ("room", "room-grade"):
            result = round_views.run_bookkeeping(verb, root)
            assert result["status"] == "written"
            assert result["n_positions"] == 3
    else:
        flags = ["--calibration-root", str(calibration_root)] if copied_calibration else []
        assert _run(capsys, [view, str(root), *flags])["n_positions"] == 3
    median = json.loads((root / "room.json").read_text())["median"]
    assert median["n_positions"] == 3
    assert set(median["evidence"]["pose_keys"]) == {
        "az+0.00_el+0.00_d+1.00", "az-20.00_el+0.00_d+1.00", "az+20.00_el+0.00_d+1.00",
    }
    assert all(not record["curves"] for _, record in measurement_documents(bundle))


@pytest.mark.parametrize("view", ["room", "room-grade", "bookkeeping"])
@pytest.mark.parametrize("code", [
    "measurement_capture_identity_mismatch", "measurement_program_manifest_missing",
    "measurement_analysis_program_unsupported",
])
def test_room_analysis_refusals_are_unreadable(tmp_path, capsys, analyzed_room_documents, view, code):
    root = bank_seat_round(tmp_path)
    analyzed_room_documents.side_effect = MeasurementAnalysisRefused(code)
    if view == "bookkeeping":
        # run_bookkeeping names the analyzer's own code; the CLI verb below
        # still buckets it under the generic unreadable-round reason.
        result = round_views.run_bookkeeping("room", root)
        assert (result["status"], result["reason"]) == ("unavailable", code)
    else:
        assert round_views.main([view, str(root), "--calibration-root", str(tmp_path)]) == round_views.EXIT_UNREADABLE
        result = json.loads(capsys.readouterr().out)
        assert (result["status"], result["reason"]) == ("unreadable", round_views.REASON_UNREADABLE)
    assert not (root / "room.json").exists()


@pytest.mark.parametrize("geometry,walls,boundary_reason", [
    (None, {}, "geometry_undeclared"),
    ({}, {}, "walls_undeclared"),
    ({"front_wall_m": 0.85}, {"front": 0.85}, ""),
    ({"cabinet_back_wall_m": 0.2}, {}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "side_wall_m": 1.4}, {"side": 1.4}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, {"front": 0.5}, ""),
])
def test_room_document_sections_and_owners(room_round, capsys, geometry, walls, boundary_reason):
    root = room_round
    if geometry is not None:
        DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                         **geometry).save(root / "declared-geometry.json")
    inputs = round_inputs(root)
    selected = resolve_set(inputs)
    with patch.object(room_views, "applied_profile_source", wraps=room_views.applied_profile_source) as source:
        result = _run(capsys, ["room", str(root), "--set", selected.set_id])
    source.assert_called_once_with(inputs.applied_profile_path)
    document = json.loads(Path(result["out"]).read_text())
    assert set(document) == {"ceiling", "median", "spread_rms_db", "persistence", "limits", "incumbent",
                             "boundary", "boundary_reason", "incumbent_reason", "room_median_sha256", "admit_boost"}
    median = document["median"]
    selection = select_seat_takes(inputs.session_dir, take_ids=selected.selected_ids,
                                  basis=selected.capture_basis)
    assert median == {**room_views.room_median(selection.takes, room_views.room_ceiling(inputs.session_dir)),
                      "set_id": selected.set_id, "evidence": selection.evidence}
    value = read_room_median(median)
    assert document["limits"] == {
        "cut_floor_db": room_limits.cut_floor_db(value.spread_db, value.freqs_hz, value.ceiling_hz).tolist(),
        "boost_cap_db": room_limits.boost_cap_db(value.freqs_hz, value.ceiling_hz).tolist(),
    }
    bounds = prescription_contracts(room_median=median, room_persistence=document["persistence"])["room"]["bounds"]
    assert document["admit_boost"] == bounds["admit_boost"]
    assert [finding["feature"] for finding in document["admit_boost"]] == document["persistence"]["features"]
    assert {key: bounds[key] for key in document["limits"]} == document["limits"]
    assert document["incumbent"]["round_id"] == _ROOM_CORRECTION["basis"]["round_id"]
    assert document["incumbent"]["room_median_sha256"] == _ROOM_CORRECTION["basis"]["room_median_sha256"]
    assert document["incumbent_reason"] == result["incumbent_reason"] == ""
    assert document["ceiling"]["hz"] == median["ceiling_hz"]
    if walls:
        expected = {"advisory": True, **boundary_prior(value.freqs_hz, walls=walls)}
        if "cabinet_back_wall_m" in geometry:
            expected["geometry"] = {"speaker_height_m": 0.84, "mic_height_m": 0.84, "distance_m": 1.0, **geometry}
            expected["front_reference"] = "front_panel_centre" if "front" in walls else None
        assert document["boundary"] == expected
    else:
        assert document["boundary"] is None
    assert document["boundary_reason"] == boundary_reason
    assert not {"freqs_hz", "median_db", "positions"} & result.keys()
    assert isinstance(result["features"], int)
    inventory = _run(capsys, ["inventory", str(root), "--set", selected.set_id])
    rows = json.loads(Path(inventory["out"]).read_text())["artifacts"]
    assert next(row for row in rows if row["view"] == "room")["present"] is True


@pytest.mark.parametrize("matches,reason", [
    (0, "room_incumbent_set_unavailable"), (2, "room_incumbent_set_ambiguous"),
])
def test_room_without_an_incumbent_discloses_null_and_reason(room_round, capsys, matches, reason):
    groups = [{"set_id": str(index), "base": True, "capture_basis": {}, "takes": []}
              for index in range(matches)]
    inputs = round_inputs(room_round)
    selection = select_seat_takes(inputs.session_dir)
    document = room_views.room_document(
        selection.takes, set_id="candidate", evidence=selection.evidence,
        bundle_dir=inputs.session_dir,
        applied_profile_path=inputs.applied_profile_path, geometry_path=None, manifest={"sets": groups},
    )
    assert document["incumbent"] is document["boundary"] is None
    assert document["incumbent_reason"] == reason
    assert document["boundary_reason"] == "geometry_undeclared"
    (room_round / "room.json").write_text(json.dumps(document))
    answer = _run(capsys, ["room-grade", str(room_round)])
    assert answer["incumbent"] is None
    assert answer["incumbent_reason"] == reason


@pytest.mark.parametrize("bases", [1, 2])
@pytest.mark.parametrize("timing", [False, True])
def test_incumbent_room_requires_one_base(bases, timing):
    groups = [{"set_id": f"base-{i}", "base": True, "capture_basis": {"candidate_id": "38395a08"}} for i in range(bases)]
    if timing:
        groups.append({"set_id": "timing", "base": True, "capture_basis": {"graph_scope": "timing"}})
    groups.append({"set_id": "trial", "base": False, "capture_basis": {"candidate_id": "trial"}})
    incumbent, reason = room_views.incumbent_room(
        {"source": {"measured_candidate_fingerprint": "another-fingerprint"}}, {"sets": groups}, set_id="trial")
    assert incumbent == ({"set_id": "base-0", "round_id": None, "room_median_sha256": None} if bases == 1 else None)
    assert reason == ("" if bases == 1 else "room_incumbent_set_ambiguous")


def test_room_grade_never_grades_a_set_against_itself(room_round, capsys):
    inputs = round_inputs(room_round)
    own = resolve_set(inputs, None).set_id
    selection = select_seat_takes(inputs.session_dir)
    document = room_views.room_document(
        selection.takes, set_id=own, evidence=selection.evidence,
        bundle_dir=inputs.session_dir,
        applied_profile_path=inputs.applied_profile_path, geometry_path=None,
        manifest={"sets": [{"set_id": own, "base": True, "capture_basis": {}, "takes": []}]},
    )
    assert document["incumbent"]["set_id"] == own
    (room_round / "room.json").write_text(json.dumps(document))
    answer = _run(capsys, ["room-grade", str(room_round)])
    assert answer["incumbent"] is answer["incumbent_set_id"] is None
    assert answer["incumbent_reason"] == "room_incumbent_set_unavailable"


@pytest.mark.parametrize("change", ["incumbent", "median", "spread_rms_db"])
def test_room_median_digest_tracks_only_the_measured_section(room_round, capsys, monkeypatch, change):
    inputs = round_inputs(room_round)
    selected = resolve_set(inputs)
    argv = ["room", str(room_round), "--set", selected.set_id]
    path = Path(_run(capsys, argv)["out"])
    before = json.loads(path.read_text())
    profile_path = room_round / "applied-profile.json"
    profile = json.loads(profile_path.read_text())
    profile["recomposition_snapshot"]["room_correction"]["basis"].update(
        round_id="another-round", room_median_sha256="b" * 64,
    )
    profile_path.write_text(json.dumps(profile))
    if change == "spread_rms_db":
        monkeypatch.setattr(room_views, "spread_rms_db", lambda *a, **kw: before["spread_rms_db"] + 1.0)
    _run(capsys, argv)
    after = json.loads(path.read_text())
    if change == "spread_rms_db":
        assert after["spread_rms_db"] == before["spread_rms_db"] + 1.0
    assert before["incumbent"] != after["incumbent"]
    assert before["median"] == after["median"]
    digest = before["room_median_sha256"]
    assert digest == after["room_median_sha256"]
    if change == "median":
        after["median"]["median_db"][0] += 1.0
        path.write_text(json.dumps(after))
    median, read_digest = crossover_prescriber._room_median(path)
    assert (read_digest == digest) is (change != "median")
    prescription = _document(sha256=digest, filters=[{"freq": 33.0, "q": 3.0, "gain": -1.0}])
    kwargs = dict(room_median=median, room_median_sha256=read_digest, round_id="round-7", sides=("mono",))
    receipt = read_room_prescription(prescription, **kwargs).to_dict()
    assert receipt["answers_median"] is (change != "median")
    assert receipt["room_median_sha256"] == read_digest


def test_room_with_unreadable_geometry(tmp_path, capsys):
    root = bank_seat_round(tmp_path)
    (root / "declared-geometry.json").write_text("{not json")
    assert round_views.main(["room", str(root)]) == round_views.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


@pytest.mark.parametrize("verb", ["room-ceiling", "room-median", "room-persistence", "boundary-prior"])
def test_retired_room_verbs(verb):
    with pytest.raises(SystemExit) as exc:
        round_views.build_parser().parse_args([verb, "round"])
    assert exc.value.code == 2


@pytest.mark.parametrize("purpose,floor_hz", [("speaker", 20.0), ("room", 30.0)])
def test_speaker_packet_holds_driver_fits_and_room_evidence_at_three_poses(speaker_round, tmp_path, purpose, floor_hz):
    root, driver, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    seat_root = bank_seat_round(tmp_path / "room", magnitudes_db=_cube()[:3])
    summed = [record for _, record in measurement_documents(round_inputs(seat_root).session_dir)]
    for record in summed:
        record["curves"][0]["band_hz"][0] = floor_hz
    groups = []
    for role in ("woofer", "tweeter", "summed"):
        rows = []
        for index, degrees in enumerate((0, -20, 20)):
            source = summed[index] if role == "summed" else driver
            record = {**source, "take_id": f"{role}-{index}", "pose_kind": "bearing",
                      "position_deg": degrees, "vertical_deg": 0, "mark_distance_m": 1.0,
                      "gating_applied": role != "summed", "graph_scope": "candidate" if role == "summed" else "drivers",
                      "measurement_purpose": "room" if role == "summed" else "speaker"}
            record.pop("seat_offset_m", None)
            path = directory / "positions" / f"{record['take_id']}.json"
            path.write_text(json.dumps(record))
            rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), record))
        group = manifest_set(rows, set_id=role)
        group.update(base=True)
        group["capture_basis"]["role"] = role
        for take, (_, record) in zip(group["takes"], rows):
            take.update(role=role, analysis=analysis,
                        curve=next(curve for curve in record["curves"] if curve["role"] == role))
        groups.append(group)
    groups.append({**groups[-1], "set_id": "timing", "takes": [{**groups[-1]["takes"][0], "phase": "entry_baseline"}],
                   "capture_basis": {**groups[-1]["capture_basis"], "graph_scope": "timing", "candidate_id": "projected-timing"}})
    write_manifest(root, program=f"{purpose}/express", groups=groups)
    manifest_path, views = _bookkeeping(root, inputs.session_dir, round_views.run_bookkeeping)
    packet = write_round_packet(root, manifest_path, views)
    assert {(fit["pose"]["deg"], fit["role"]) for fit in packet["fits"]} == {
        (degrees, role) for degrees in (0, -20, 20) for role in ("woofer", "tweeter")}
    assert all(isinstance(fit["filters"], list) and fit["residual_rms_db"] is not None for fit in packet["fits"])
    room, = packet["room"]
    assert room["set_id"] == room["incumbent"]["set_id"] == "summed"
    assert room["median"]["n_positions"] == room["persistence"]["spatial_support"]["n_positions"] == 3
    assert room["median"]["window"] == "ungated"
    assert set(room) == {"ceiling", "median", "spread_rms_db", "persistence", "incumbent", "boundary", "boundary_reason",
                         "incumbent_reason", "room_median_sha256", "admit_boost", "out", "set_id"}
    assert packet["limits"]["summed"]["bounds"]["admit_boost"] == room["admit_boost"]
    limits = packet["limits"]["summed"]
    assert limits["evidence_status"] == "evaluated"
    assert limits["bounds"]["band_hz"][0] == room["median"]["coverage_hz"][0] == floor_hz
    assert limits["bounds"]["freqs_hz"] == room["median"]["freqs_hz"]
    assert len(limits["bounds"]["cut_floor_db"]) == len(room["median"]["freqs_hz"])
    assert {row["set_id"] for row in views if row["view"] == "room" and row["status"] == "written"} == {"summed"}
    inventories = [json.loads(Path(row["out"]).read_text()) for row in views if row["view"] == "inventory"]
    assert any(row["view"] == "room" and row["present"] for inventory in inventories for row in inventory["artifacts"])
