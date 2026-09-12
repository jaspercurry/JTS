# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room medians, capture selection, and persistence across seats."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import room_views
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from tests.run_manifest_fixture import write_manifest
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.room_boundary import (
    ROOM_BOUNDARY_DEFAULT_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    room_ceiling_hz,
)
from jasper.cli import round_views
from jasper.cli.round_views import room
from jasper.cli.round_views._common import resolve_set
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.room_prescription import read_room_median
from jasper.audio_measurement import room_limits
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry, boundary_prior
from tests.test_active_speaker_audition import _applied_profile
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_active_speaker_baseline_profile import _ROOM_CORRECTION
from tests.crossover_v2_banked_round import SEAT_GRID_HZ, bank_measure_round, bank_seat_round

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


@pytest.mark.parametrize("n_positions,sufficient,reason", [
    (1, False, "too_few_positions"), (2, True, ""), (3, True, ""),
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
    assert room_ceiling_hz(trusted_floor_hz) == ceiling_hz


def test_the_ceiling_reads_the_banked_floor_as_the_raw_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        room_views, "applied_profile_source",
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": 130.0}}, ""),
    )

    ceiling = room_views.room_ceiling(Path("applied-profile.json"))

    assert (ceiling.ceiling_hz, ceiling.source) == (
        130.0 * TRUSTED_FLOOR_MULTIPLIER, room_views.CEILING_SOURCE_APPLIED,
    )
    assert (ceiling.raw_floor_hz, ceiling.reason) == (130.0, "")


@pytest.mark.parametrize("source", [
        lambda path: (None, "unreadable"),
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": None}}, ""),
        lambda path: ({}, ""),
], ids=["no-profile", "no-floor", "no-evidence"])
def test_a_missing_floor_falls_back_and_says_so(monkeypatch: pytest.MonkeyPatch, source) -> None:
    monkeypatch.setattr(room_views, "applied_profile_source", source)

    ceiling = room_views.room_ceiling(None)

    assert (ceiling.ceiling_hz, ceiling.source) == (
        ROOM_BOUNDARY_DEFAULT_HZ, room_views.CEILING_SOURCE_FALLBACK,
    )
    assert ceiling.trusted_floor_hz is None
    assert isinstance(ceiling.reason, str) and ceiling.reason


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


@pytest.mark.parametrize("geometry", [None, {}, {"front_wall_m": 0.85}])
def test_room_document_sections_and_owners(tmp_path, capsys, geometry):
    root = bank_seat_round(tmp_path, magnitudes_db=_cube())
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    profile["recomposition_snapshot"]["room_correction"] = _ROOM_CORRECTION
    (root / "applied-profile.json").write_text(json.dumps(profile))
    if geometry is not None:
        DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                         **geometry).save(root / "declared-geometry.json")
    inputs = round_inputs(root)
    selected = resolve_set(inputs)
    result = _run(capsys, ["room", str(root), "--set", selected.set_id])
    document = json.loads(Path(result["out"]).read_text())
    assert set(document) == {"ceiling", "median", "persistence", "limits", "incumbent",
                             "boundary", "boundary_reason"}
    median = document["median"]
    selection = select_seat_takes(inputs.session_dir, take_ids=selected.selected_ids,
                                  basis=selected.capture_basis)
    assert median == {**room_views.room_median(selection.takes, room_views.room_ceiling(inputs.applied_profile_path)),
                      "set_id": selected.set_id, "evidence": selection.evidence}
    value = read_room_median(median)
    assert document["limits"] == {
        "cut_floor_db": room_limits.cut_floor_db(value.spread_db, value.freqs_hz, value.ceiling_hz).tolist(),
        "boost_cap_db": room_limits.boost_cap_db(value.freqs_hz, value.ceiling_hz).tolist(),
    }
    for feature in document["persistence"]["features"]:
        assert feature["admission"] == room_limits.admit_boost(
            feature["centre_hz"], freqs_hz=value.freqs_hz, median_db=value.median_db,
            deviations_db=value.deviations_db, n_positions=value.n_positions).to_dict()
    assert document["incumbent"]["round_id"] == _ROOM_CORRECTION["basis"]["round_id"]
    assert document["incumbent"]["room_median_sha256"] == _ROOM_CORRECTION["basis"]["room_median_sha256"]
    assert document["ceiling"]["hz"] == median["ceiling_hz"]
    if geometry:
        assert document["boundary"] == {"advisory": True, **boundary_prior(value.freqs_hz, walls={"front": 0.85})}
    else:
        assert document["boundary"] is None
        assert document["boundary_reason"] == ("geometry_undeclared" if geometry is None else "walls_undeclared")
    assert not {"freqs_hz", "median_db", "positions"} & result.keys()
    assert isinstance(result["features"], int)
    inventory = _run(capsys, ["inventory", str(root), "--set", selected.set_id])
    rows = json.loads(Path(inventory["out"]).read_text())["artifacts"]
    assert next(row for row in rows if row["view"] == "room")["present"] is True


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
