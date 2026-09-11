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
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.room_prescription import read_room_median
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from tests.run_manifest_fixture import write_manifest, manifest_set
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


def test_room_median_is_the_contract_a_room_candidate_reads(tmp_path: Path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube())

    _run(capsys, ["room-median", str(round_dir)])

    doc = json.loads((round_dir / "room_median.json").read_text())
    assert set(doc) == {
        "freqs_hz", "median_db", "spread_db", "n_positions", "positions",
        "ceiling_hz", "ceiling_source", "window", "coverage_hz", "evidence",
        "spatial_support",
    }
    freqs = np.asarray(doc["freqs_hz"])
    assert freqs[0] >= room_views.ROOM_FLOOR_HZ and freqs[-1] <= doc["ceiling_hz"]
    assert (doc["n_positions"], doc["window"]) == (7, "ungated")
    at = int(np.argmin(np.abs(freqs - _QUIET_HZ)))
    assert doc["median_db"][at] == pytest.approx(-27.0)
    assert doc["spread_db"][at] == pytest.approx(2.0)
    assert [p["deviation_db"][at] for p in doc["positions"]] == pytest.approx(
        [i - 3.0 for i in range(7)]
    )
    assert len({p["pose_key"] for p in doc["positions"]}) == 7


@pytest.mark.parametrize("n_positions,sufficient,reason", [
    (1, False, "too_few_positions"), (2, True, ""), (3, True, ""),
])
def test_room_views_disclose_spatial_support(tmp_path, capsys, n_positions, sufficient, reason):
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube()[:n_positions])
    support = {"n_positions": n_positions, "sufficient": sufficient, "reason": reason}
    median = _run(capsys, ["room-median", str(round_dir)])
    persistence = _run(capsys, ["room-persistence", str(round_dir)])
    doc = json.loads((round_dir / "room_median.json").read_text())
    features = json.loads((round_dir / "room_persistence.json").read_text())

    assert median["spatial_support"] == doc["spatial_support"] == support
    assert persistence["spatial_support"] == features["spatial_support"] == support
    assert all(feature["presence_fraction"] == 1.0 for feature in features["features"])
    at = int(np.argmin(np.abs(np.asarray(doc["freqs_hz"]) - _QUIET_HZ)))
    assert doc["median_db"][at] == pytest.approx(-30.0 + (n_positions - 1) / 2)
    if sufficient:
        assert isinstance(doc["spread_db"][at], float)
        assert doc["spread_db"][at] == pytest.approx(np.std(np.arange(n_positions)))
    else:
        assert doc["spread_db"] is None
        assert all(value is None for value in median["mean_spread_db"].values())


def test_room_persistence_counts_what_holds_across_the_cube(tmp_path: Path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube())

    _run(capsys, ["room-persistence", str(round_dir)])

    doc = json.loads((round_dir / "room_persistence.json").read_text())
    by_kind = {feature["kind"]: feature for feature in doc["features"]}
    assert set(by_kind) == {"dip", "peak"}
    assert by_kind["dip"]["centre_hz"] == pytest.approx(63.0, rel=0.05)
    assert by_kind["dip"]["median_depth_db"] == pytest.approx(-8.0, abs=1.5)
    assert (by_kind["dip"]["n_present"], by_kind["dip"]["presence_fraction"]) == (7, 1.0)
    assert by_kind["peak"]["n_present"] == 3
    assert by_kind["peak"]["presence_fraction"] == pytest.approx(3 / 7)


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
    assert round_views.main(["room-median", str(round_dir)]) == round_views.EXIT_REFUSED
    refused = json.loads(capsys.readouterr().out)
    assert refused["reason"] == "round_set_unknown"
    assert not (round_dir / "room_median.json").exists()

    for record, level in [(original, -30.0), (second, -20.0)]:
        set_id = "first" if record is original else "second"
        out = round_dir / f"room_median-{set_id}.json"
        legacy = select_seat_takes(root, capture_id=record["take_id"])
        answer = _run(capsys, [
            "room-median", str(round_dir), "--set", set_id, "--out", str(out),
        ])
        doc = json.loads(out.read_text())
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
        persistence = _run(capsys, [
            "room-persistence", str(round_dir), "--set", set_id,
        ])
        assert persistence["n_positions"] == 7
        assert persistence["evidence"] == doc["evidence"]
        grade = _run(capsys, ["room-grade", str(round_dir), "--set", set_id])
        assert grade["evidence"] == doc["evidence"]
        assert grade["graph_scopes"] == [record["graph_scope"]]
        if record is original:
            assert "repeated_pose" in doc["evidence"]["take_ids"]
            assert original["take_id"] in doc["evidence"]["superseded_take_ids"]
            assert [r["take_id"] for r in doc["evidence"]["omitted_takes"]] == ["bad_repeat"]


def test_the_answers_summarize_without_the_curves(tmp_path: Path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube())

    median = _run(capsys, ["room-median", str(round_dir)])
    persistence = _run(capsys, ["room-persistence", str(round_dir)])

    assert median["mean_spread_db"]["20-60"] == pytest.approx(2.0)
    assert "freqs_hz" not in median and "median_db" not in median
    assert (persistence["persistent"], persistence["top"][0]["kind"]) == (1, "dip")
    assert "features" in persistence and isinstance(persistence["features"], int)


def test_a_round_with_no_seat_takes_is_refused_by_name(tmp_path: Path, capsys) -> None:
    round_dir = bank_measure_round(tmp_path)

    code = round_views.main(["room-median", str(round_dir)])

    assert code == round_views.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == room.REFUSE_NO_SEAT_TAKES


@pytest.mark.parametrize(
    ("trusted_floor_hz", "ceiling_hz"),
    [
        (None, ROOM_BOUNDARY_DEFAULT_HZ),
        (150.0, ROOM_BOUNDARY_MIN_HZ),
        (325.0, 325.0),
        (1000.0, ROOM_BOUNDARY_MAX_HZ),
    ],
)
def test_the_ceiling_is_the_trusted_floor_clamped(trusted_floor_hz, ceiling_hz) -> None:
    """ADR-0256 rule 1, at the module that owns the bounds."""
    assert room_ceiling_hz(trusted_floor_hz) == ceiling_hz


def test_the_ceiling_reads_the_banked_floor_as_the_raw_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """``validity_floor_hz`` is the cloud's ``1/T`` floor; the trusted floor
    is ``TRUSTED_FLOOR_MULTIPLIER`` times it, and that is what is clamped."""
    monkeypatch.setattr(
        room_views, "applied_profile_source",
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": 130.0}}, ""),
    )

    ceiling = room_views.room_ceiling(Path("applied-profile.json"))

    assert (ceiling.ceiling_hz, ceiling.source) == (
        130.0 * TRUSTED_FLOOR_MULTIPLIER, room_views.CEILING_SOURCE_APPLIED,
    )
    assert (ceiling.raw_floor_hz, ceiling.reason) == (130.0, "")


@pytest.mark.parametrize(
    "source",
    [
        lambda path: (None, "unreadable"),
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": None}}, ""),
        lambda path: ({}, ""),
    ],
    ids=["no-profile", "no-floor", "no-evidence"],
)
def test_a_missing_floor_falls_back_and_says_so(monkeypatch: pytest.MonkeyPatch, source) -> None:
    monkeypatch.setattr(room_views, "applied_profile_source", source)

    ceiling = room_views.room_ceiling(None)

    assert (ceiling.ceiling_hz, ceiling.source) == (
        ROOM_BOUNDARY_DEFAULT_HZ, room_views.CEILING_SOURCE_FALLBACK,
    )
    assert ceiling.trusted_floor_hz is None
    assert isinstance(ceiling.reason, str) and ceiling.reason


def test_room_ceiling_writes_the_disclosed_fallback_and_inventory_lists_the_room(
    tmp_path: Path, capsys,
) -> None:
    round_dir = bank_seat_round(tmp_path)

    answer = _run(capsys, ["room-ceiling", str(round_dir)])

    doc = json.loads((round_dir / "room_ceiling.json").read_text())
    assert doc["ceiling_source"] == answer["ceiling_source"] == room_views.CEILING_SOURCE_FALLBACK
    assert doc["clamp_hz"] == [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]
    _run(capsys, ["inventory", str(round_dir)])
    rows = {
        row["artifact"]: row["present"]
        for row in json.loads((round_dir / "inventory.json").read_text())["artifacts"]
    }
    assert rows["room_ceiling.json"] is True
    assert {"room_median.json", "room_persistence.json"} <= set(rows)


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
    _run(capsys, ["room-median", str(round_dir)])
    result = json.loads((round_dir / "room_median.json").read_text())
    assert result["n_positions"] == 3
    assert result["window"] == "ungated"
    assert "pose_kind" not in result["evidence"]["basis"]
    assert len(set(result["evidence"]["pose_keys"])) == 3
