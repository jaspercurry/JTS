# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room grades and comparison disclosures from the shared median fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pytest

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.room_grade import (
    ROOM_GRADE_KIND,
    ROOM_GRADE_RESOLUTION_DB,
    grade_room_median,
    read_room_median,
)
from jasper.active_speaker.crossover_v2.room_views import ROOM_BAND_SPLITS_HZ, band_edges, band_masks
from jasper.active_speaker.crossover_v2.room_prescription import ROOM_MEDIAN_UNAVAILABLE
from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_MIN_HZ
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE

from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import write_manifest
from jasper.active_speaker.crossover_v2.round_inputs import set_artifact_name

from tests.room_median_fixture import (
    BAND_BINS,
    BAND_EDGES_HZ,
    CEILING_HZ,
    DIP_DB,
    INCUMBENT,
    MODE_DB,
    N_POSITIONS,
    RIPPLE_DB,
    SPREAD_DB,
    median_document,
    room_median_document,
    write_room_median,
)

#: A second ceiling the door's reader also accepts, so an incumbent can carry
#: one that is not this round's.
OTHER_CEILING_HZ = 260.0


def _low_band_rms_db(mode_db: float = MODE_DB, dip_db: float = DIP_DB) -> float:
    """The RMS the fixture's own numbers give for the lowest band: ripple on
    every rung but the mode's and the dip's. The two bands above hold ripple
    alone, whose RMS is that ripple."""
    n_bins = BAND_BINS[0]
    return float(np.sqrt(
        ((n_bins - 2) * RIPPLE_DB[0] ** 2 + mode_db**2 + dip_db**2) / n_bins
    ))


def test_the_ceiling_tops_the_last_band():
    assert band_edges(CEILING_HZ) == BAND_EDGES_HZ


def test_a_bin_sitting_on_a_split_is_counted_once_by_the_band_above_it():
    """The masks are half-open below a split and closed at the ceiling. No
    1/12-octave grid lands on 60 or 120 Hz, so pin the seam on one that does."""
    grid = np.array([20.0, 60.0, 90.0, 120.0, 200.0, CEILING_HZ])
    masks = [mask for _, _, mask in band_masks(grid, CEILING_HZ)]

    for index, split_hz in enumerate(ROOM_BAND_SPLITS_HZ, start=1):
        on_split = grid == split_hz
        assert on_split.any()
        assert np.array_equal(masks[index] & on_split, on_split)
        assert not (masks[index - 1] & on_split).any()
    # Every bin -- the two splits and the bin on the ceiling included -- in
    # exactly one band.
    assert np.array_equal(
        sum(mask.astype(int) for mask in masks), np.ones(grid.size, dtype=int)
    )


def test_the_grade_is_the_fixture_arithmetic_below_the_ceiling():
    grade = grade_room_median(read_room_median(room_median_document()))
    artifact = grade.to_dict()

    assert artifact["kind"] == ROOM_GRADE_KIND
    assert artifact["resolution_db"] == ROOM_GRADE_RESOLUTION_DB
    assert artifact["ceiling_hz"] == CEILING_HZ
    assert artifact["ceiling_source"] == "applied_candidate"
    assert artifact["n_positions"] == N_POSITIONS
    assert artifact["incumbent"] is None
    assert artifact["regressed_bands"] == []

    assert [(row["lo_hz"], row["hi_hz"]) for row in artifact["bands"]] == list(BAND_EDGES_HZ)
    assert [row["n_bins"] for row in artifact["bands"]] == list(BAND_BINS)
    assert [row["spread_db"] for row in artifact["bands"]] == [SPREAD_DB] * 3
    assert [row["max_db"] for row in artifact["bands"]] == [
        abs(DIP_DB), RIPPLE_DB[1], RIPPLE_DB[2],
    ]
    assert [row["rms_db"] for row in artifact["bands"]] == pytest.approx([
        _low_band_rms_db(), RIPPLE_DB[1], RIPPLE_DB[2],
    ])
    assert all(row["delta_rms_db"] is None for row in artifact["bands"])
    assert all(row["regressed"] is None for row in artifact["bands"])


@pytest.mark.parametrize(("candidate", "incumbent", "regressed_lo_hz"), [
    ({}, INCUMBENT, 60.0),
    (INCUMBENT, {}, 20.0),
])
def test_a_band_that_moved_the_wrong_way_is_disclosed_both_ways(
    candidate, incumbent, regressed_lo_hz
):
    """A disclosure, never a verdict: the grade names the band and stops."""
    graded = grade_room_median(
        read_room_median(room_median_document(**candidate)),
        incumbent=read_room_median(room_median_document(**incumbent)),
    )
    artifact = graded.to_dict()

    assert artifact["regressed_bands"] == [regressed_lo_hz]
    assert artifact["incumbent"] == {
        "ceiling_hz": CEILING_HZ, "n_positions": N_POSITIONS,
    }
    for row in artifact["bands"]:
        assert row["delta_rms_db"] == pytest.approx(
            row["rms_db"] - row["incumbent_rms_db"]
        )
        assert row["regressed"] is (row["delta_rms_db"] > 0.0)
    assert artifact["bands"][2]["delta_rms_db"] == pytest.approx(0.0)
    assert artifact["bands"][2]["incumbent_spread_db"] == SPREAD_DB


def test_an_incumbent_with_another_ceiling_is_graded_on_this_rounds_bands():
    incumbent = read_room_median(room_median_document(ceiling_hz=OTHER_CEILING_HZ))
    graded = grade_room_median(read_room_median(room_median_document()), incumbent=incumbent)
    artifact = graded.to_dict()

    assert [(row["lo_hz"], row["hi_hz"]) for row in artifact["bands"]] == list(BAND_EDGES_HZ)
    assert artifact["incumbent"]["ceiling_hz"] == OTHER_CEILING_HZ
    # The top band on the incumbent's OWN grid stops at its ceiling, not this round's.
    top = incumbent.median_db[incumbent.freqs_hz >= BAND_EDGES_HZ[2][0]]
    assert artifact["bands"][2]["incumbent_rms_db"] == pytest.approx(float(np.sqrt(np.mean(top ** 2))))
    assert incumbent.freqs_hz[-1] <= OTHER_CEILING_HZ < CEILING_HZ


def test_comparison_uses_only_common_frequency_support():
    """A peak outside the trial's support cannot masquerade as improvement."""
    baseline_grid = np.asarray(room_median_document()["freqs_hz"])
    response = 6.0 * np.exp(-0.5 * (np.log2(baseline_grid / 145.0) / 0.18) ** 2)
    keep = baseline_grid >= 200.39
    candidate = read_room_median(median_document(
        baseline_grid[keep], response[keep], ceiling_hz=CEILING_HZ,
    ))
    incumbent = read_room_median(median_document(
        baseline_grid, response, ceiling_hz=CEILING_HZ,
    ))

    artifact = grade_room_median(candidate, incumbent=incumbent).to_dict()

    assert artifact["regressed_bands"] == []
    assert artifact["comparison"]["available"] is True
    assert artifact["comparison"]["common_support_hz"][0] == pytest.approx(
        candidate.freqs_hz[0]
    )
    assert artifact["comparison"]["incumbent_removed_support_hz"] == [[
        pytest.approx(float(incumbent.freqs_hz[0])),
        pytest.approx(float(candidate.freqs_hz[0])),
    ]]
    assert [row["rms_db"] for row in artifact["bands"][:2]] == [None, None]
    top = artifact["bands"][2]
    assert top["compared_hz"][0] == pytest.approx(float(candidate.freqs_hz[0]))
    assert top["delta_rms_db"] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("level,program,disclosed", [
    (-24.0, "changed-gains", "mismatched_fields"), (None, None, "unknown_fields"),
])
def test_comparison_aligns_a_whole_graph_level_shift_once(level, program, disclosed):
    document = _comparison_document(graph="candidate")
    shifted = {
        **document,
        "median_db": [value + 6.0 for value in document["median_db"]],
        "evidence": {**document["evidence"], "basis": {
            **document["evidence"]["basis"], "level_db": level, "program_id": program,
        }},
    }

    artifact = grade_room_median(
        read_room_median(shifted),
        incumbent=read_room_median(document),
    ).to_dict()

    assert artifact["comparison"]["available"] is True
    assert artifact["comparison"]["incompatible_fields"] == []
    assert set(artifact["comparison"][disclosed]) == {"level_db", "program_id"}
    assert artifact["comparison"]["level_alignment_db"] == pytest.approx(-6.0)
    assert artifact["comparison"]["level_reference_db"] == pytest.approx(
        float(np.median(document["median_db"]))
    )
    assert all(row["delta_rms_db"] == pytest.approx(0.0) for row in artifact["bands"])


def _comparison_document(*, graph: str, side: str = "left") -> dict[str, Any]:
    document = room_median_document()
    pose_keys = [f"seat-{index}" for index in range(document["n_positions"])]
    document["evidence"] = {
        "basis": {
            "candidate_id": graph,
            "submitted_graph_fingerprint": graph,
            "graph_fingerprint": graph,
            "graph_scope": "candidate" if graph == "candidate" else "candidate",
            "side": side,
            "capture_device": {"usb_id": "mic-1", "channel_selected": 0},
            "level_db": -30.0,
            "loudness_volume_db": -30.0, "program_id": "program",
            "stimulus_dbfs": -12.0,
            "stimulus_wav_sha256": "program",
            "stimulus_peak_dbfs": -12.0,
            "gating_applied": False,
            "calibration_reference": "cal-1",
            "calibration_applied": True,
            "capture_calibration": {
                "applied": True,
                "calibration_id": "cal-1",
                "curve_fingerprint": "curve-1",
                "model_key": "umik-2",
                "tier": "reference",
            },
        },
        "take_ids": [entry["id"] for entry in document["positions"]],
        "pose_keys": pose_keys,
    }
    return document


def test_graph_change_is_the_intervention_not_an_incompatible_basis():
    artifact = grade_room_median(
        read_room_median(_comparison_document(graph="candidate")),
        incumbent=read_room_median(_comparison_document(graph="incumbent")),
    ).to_dict()

    comparison = artifact["comparison"]
    assert comparison["available"] is True
    assert comparison["basis_status"] == "compatible"
    assert comparison["incompatible_fields"] == []
    assert comparison["intervention_fields"] == [
        "candidate_id", "graph_fingerprint",
        "submitted_graph_fingerprint",
    ]


def test_known_capture_basis_mismatch_withholds_the_comparison():
    artifact = grade_room_median(
        read_room_median(_comparison_document(graph="candidate", side="right")),
        incumbent=read_room_median(_comparison_document(graph="incumbent")),
    ).to_dict()

    assert artifact["comparison"]["available"] is False
    assert artifact["comparison"]["basis_status"] == "incompatible"
    assert artifact["comparison"]["incompatible_fields"] == ["side"]
    assert artifact["comparison"]["unavailable_reason"] == "incompatible_measurement_basis"
    assert all(row["delta_rms_db"] is None for row in artifact["bands"])


@pytest.mark.parametrize(("changed_field", "change"), [
    ("loudness_volume_db", lambda document: document["evidence"]["basis"].update(loudness_volume_db=-24)),
    ("pose_keys", lambda document: document["evidence"].update(
        pose_keys=["different", *document["evidence"]["pose_keys"][1:]],
    )),
    ("capture_calibration", lambda document: document["evidence"]["basis"][
        "capture_calibration"
    ].update(curve_fingerprint="curve-2")),
    ("calibration_reference", lambda document: document["evidence"]["basis"].update(
        capture_calibration=None,
        calibration_reference="cal-2",
        calibration_applied=True,
    )),
])
def test_pose_or_calibration_change_withholds_comparison(changed_field, change):
    candidate = _comparison_document(graph="candidate")
    change(candidate)

    artifact = grade_room_median(
        read_room_median(candidate),
        incumbent=read_room_median(_comparison_document(graph="incumbent")),
    ).to_dict()

    assert artifact["comparison"]["available"] is False
    assert artifact["comparison"]["incompatible_fields"] == [changed_field]
    assert all(row["delta_rms_db"] is None for row in artifact["bands"])


def test_legacy_unknown_basis_is_disclosed_without_blocking_comparison():
    artifact = grade_room_median(
        read_room_median(room_median_document()),
        incumbent=read_room_median(room_median_document(**INCUMBENT)),
    ).to_dict()

    assert artifact["comparison"]["available"] is True
    assert artifact["comparison"]["basis_status"] == "unknown"
    assert "capture_calibration" in artifact["comparison"]["unknown_fields"]
    assert "calibration_applied" in artifact["comparison"]["unknown_fields"]


def _grid_cropped_below(document: dict[str, Any], hi_hz: float) -> dict[str, Any]:
    """``document`` with its grid cropped below ``hi_hz``. The door checks that a
    median's grid stays inside the room band, not that it spans it, so this is
    still a median it reads."""
    keep = [index for index, freq in enumerate(document["freqs_hz"]) if freq < hi_hz]

    def cropped(values: Sequence[float]) -> list[float]:
        return [values[index] for index in keep]

    return {
        **document,
        "freqs_hz": cropped(document["freqs_hz"]),
        "median_db": cropped(document["median_db"]),
        "spread_db": cropped(document["spread_db"]),
        "positions": [
            {**row, "deviation_db": cropped(row["deviation_db"])}
            for row in document["positions"]
        ],
        "ceiling_hz": ROOM_BOUNDARY_MIN_HZ,
    }


def test_a_band_the_incumbent_never_measured_grades_as_unknown():
    """Zero bins is no evidence, not a flat incumbent: the band it cannot see
    reads null rather than grading this round's own RMS as a regression."""
    graded = grade_room_median(
        read_room_median(room_median_document()),
        incumbent=read_room_median(
            _grid_cropped_below(room_median_document(), BAND_EDGES_HZ[2][0])
        ),
    )
    artifact = graded.to_dict()

    top = artifact["bands"][2]
    assert top["n_bins"] == 0
    assert top["rms_db"] is None
    assert top["max_db"] is None
    assert top["spread_db"] is None
    assert top["incumbent_n_bins"] == 0
    assert top["incumbent_rms_db"] is None
    assert top["delta_rms_db"] is None
    assert top["regressed"] is None
    assert BAND_EDGES_HZ[2][0] not in artifact["regressed_bands"]
    # The bands its grid does cover are graded as usual.
    assert [row["incumbent_n_bins"] for row in artifact["bands"][:2]] == list(
        BAND_BINS[:2]
    )


def test_the_view_grades_the_median_beside_the_round(tmp_path, capsys):
    round_dir = bank_measure_round(tmp_path)
    write_room_median(round_dir)

    assert round_views.main(["room-grade", str(round_dir)]) == EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    artifact = json.loads((round_dir / "room_grade.json").read_text())
    assert artifact["kind"] == ROOM_GRADE_KIND
    assert answer["out"] == str(round_dir / "room_grade.json")
    assert answer["bytes"] == (round_dir / "room_grade.json").stat().st_size
    assert answer["ceiling_hz"] == CEILING_HZ
    assert answer["regressed_bands"] == []
    assert answer["incumbent"] is None
    assert [row["n_bins"] for row in answer["bands"]] == list(BAND_BINS)
    # No take on this round carries a scope, and that is a disclosure of
    # nothing rather than a missing key.
    assert answer["graph_scopes"] == []
    assert artifact["graph_scopes"] == []


@pytest.mark.parametrize("n_positions,with_baseline,unknown", [
    (1, False, "both"), (1, True, "both"),
    (N_POSITIONS, True, "candidate"), (N_POSITIONS, True, "incumbent"),
])
def test_the_view_keeps_response_grades_when_spread_is_unknown(
    tmp_path, capsys, n_positions, with_baseline, unknown,
):
    round_dir = bank_measure_round(tmp_path)
    document = room_median_document(n_positions=n_positions)
    if unknown != "incumbent":
        document["spread_db"] = None
    (round_dir / "room.json").write_text(json.dumps({"median": document}))
    args = ["room-grade", str(round_dir)]
    if with_baseline:
        baseline = room_median_document(n_positions=n_positions, **INCUMBENT)
        if unknown != "candidate":
            baseline["spread_db"] = None
        _write_room_sets(round_dir, candidate=document, incumbent=baseline)
        args += ["--set", "candidate"]

    assert round_views.main(args) == EXIT_OK
    answer = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(answer["out"]).read_text())
    assert answer["spatial_support"] == artifact["spatial_support"] == document["spatial_support"]
    assert artifact["spatial_support"]["sufficient"] is (n_positions != 1)
    assert answer["bands"] == artifact["bands"]
    assert [row["spread_db"] for row in answer["bands"]] == [
        SPREAD_DB if unknown == "incumbent" else None,
    ] * 3
    assert all(isinstance(row["rms_db"], float) and isinstance(row["max_db"], float) for row in answer["bands"])
    if with_baseline:
        assert answer["comparison"]["available"] is True
        assert answer["regressed_bands"] == [60.0]
        assert [row["incumbent_spread_db"] for row in answer["bands"]] == [
            SPREAD_DB if unknown == "candidate" else None,
        ] * 3
        assert all(row["delta_rms_db"] == pytest.approx(
            row["rms_db"] - row["incumbent_rms_db"],
        ) for row in answer["bands"])


def _stamp_graph_scopes(round_dir: Path, scopes: Sequence[str]) -> None:
    """Give this round's takes a graph scope, cycling through ``scopes``.

    The take file is what ``bundle_measurements`` reads, and the spatial
    writers this fixture goes through stamp no scope of their own.
    """
    bundle, = (round_dir / "bundle").iterdir()
    artifacts = bundle / EVIDENCE_ROOT / "artifacts"
    for index, row in enumerate(bundle_measurements(bundle)):
        path = artifacts / row.path
        document = json.loads(path.read_text())
        path.write_text(json.dumps({
            **document, "graph_scope": scopes[index % len(scopes)],
        }))


def test_the_view_discloses_the_scopes_the_round_played_through(tmp_path, capsys):
    round_dir = bank_measure_round(tmp_path)
    write_room_median(round_dir)
    _stamp_graph_scopes(round_dir, ("candidate", "base"))

    assert round_views.main(["room-grade", str(round_dir)]) == EXIT_OK

    assert json.loads(capsys.readouterr().out)["graph_scopes"] == [
        "base", "candidate",
    ]


def _write_room_sets(round_dir, **medians):
    groups = [{"set_id": key, "capture_basis": value.get("evidence", {}).get("basis", {}), "takes": []}
              for key, value in medians.items()]
    write_manifest(round_dir, program="room", groups=groups)
    for key, value in medians.items():
        (round_dir / set_artifact_name("room.json", key)).write_text(json.dumps({
            "median": {**value, "set_id": key}, "incumbent": {"set_id": "incumbent"},
        }))


@pytest.mark.parametrize("named", [False, True])
def test_the_incumbent_set_names_the_regressed_band(tmp_path, capsys, named):
    round_dir = bank_measure_round(tmp_path)
    _write_room_sets(round_dir, candidate=room_median_document(),
                    incumbent=room_median_document(**INCUMBENT))
    assert round_views.main(["room-grade", str(round_dir), "--set", "candidate",
                             *(["--incumbent", "incumbent"] if named else [])]) == EXIT_OK
    answer = json.loads(capsys.readouterr().out)
    assert answer["regressed_bands"] == [60.0]
    assert answer["incumbent_set_id"] == "incumbent"
    assert answer["bands"][1]["incumbent_rms_db"] == pytest.approx(INCUMBENT["ripple_db"][1])


@pytest.mark.parametrize("flag", ["--room-median", "--baseline-room-median", "--baseline", "--baseline-set"])
def test_room_grade_rejects_median_path_flags(flag):
    with pytest.raises(SystemExit) as exc:
        round_views.build_parser().parse_args(["room-grade", "round", flag, "median.json"])
    assert exc.value.code == 2


@pytest.mark.parametrize(("write_median", "reason"), [
    (None, round_views.REASON_UNREADABLE),
    (lambda path: path.write_text("[]"), round_views.REASON_UNREADABLE),
    (lambda path: path.write_text("{}"), ROOM_MEDIAN_UNAVAILABLE),
    (lambda path: path.write_text(json.dumps({"median": {"freqs_hz": []}})), ROOM_MEDIAN_UNAVAILABLE),
])
def test_a_median_the_view_cannot_read_is_unreadable(
    tmp_path, capsys, write_median, reason
):
    round_dir = bank_measure_round(tmp_path)
    if write_median is not None:
        write_median(round_dir / "room.json")

    code = round_views.main(["room-grade", str(round_dir)])

    assert code == EXIT_UNREADABLE
    document = json.loads(capsys.readouterr().out)
    assert document["reason"] == reason
    assert document["status"] == "unreadable"


@pytest.mark.parametrize("named", [False, True])
def test_campaign_room_sets_preserve_the_60_to_120_hz_grade(tmp_path, capsys, named):
    fixture = Path(__file__).parent / "fixtures/room_campaign"
    medians = {key: json.loads((fixture / filename).read_text()) for key, filename in (
        ("incumbent", "0c3d5db4fb3e.json"), ("candidate", "a5688899542a.json"),
    )}
    root = bank_measure_round(tmp_path)
    _write_room_sets(root, **medians)
    assert round_views.main(["room-grade", str(root), "--set", "candidate",
                             *(["--incumbent", "incumbent"] if named else [])]) == EXIT_OK
    answer = json.loads(capsys.readouterr().out)
    band = next(row for row in answer["bands"] if row["lo_hz"] == 60.0)
    assert band["rms_db"] == pytest.approx(5.123918344665255)
    assert band["incumbent_rms_db"] == pytest.approx(7.9267101468919945)
    assert band["regressed"] is False
    assert band["delta_rms_db"] == pytest.approx(-2.8027918022267393)
    assert answer["comparison"]["available"] is True
