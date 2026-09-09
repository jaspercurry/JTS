# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room grade: a seat-cube median read into bands, and drift disclosed.

The median document comes from ``tests/room_median_fixture.py`` — it is the
seat-cube view's artifact, not this one's — and is read through the room
door's own reader, so what this suite grades is what that door prescribes
against.
"""

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
from jasper.active_speaker.crossover_v2.room_views import (
    ROOM_BAND_SPLITS_HZ,
    band_edges,
    band_masks,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    ROOM_MEDIAN_UNAVAILABLE,
)
from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_MIN_HZ
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE

from tests.crossover_v2_banked_round import bank_measure_round
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
    # The dip is the largest excursion in the lowest band; the two bands above
    # it hold ripple alone, which is what the rungs above the ceiling being
    # excluded looks like from here.
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
    # The band above both splits is untouched between the two documents, so it
    # is the one that must read as neither improved nor regressed.
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
    _stamp_graph_scopes(round_dir, ("speaker_tune", "base"))

    assert round_views.main(["room-grade", str(round_dir)]) == EXIT_OK

    assert json.loads(capsys.readouterr().out)["graph_scopes"] == [
        "base", "speaker_tune",
    ]


def test_the_baseline_round_names_the_regressed_band(tmp_path, capsys):
    round_dir = bank_measure_round(tmp_path)
    baseline_dir = bank_measure_round(tmp_path, name="r0-baseline")
    write_room_median(round_dir)
    write_room_median(baseline_dir, **INCUMBENT)

    assert round_views.main([
        "room-grade", str(round_dir), "--baseline", str(baseline_dir),
    ]) == EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    assert answer["regressed_bands"] == [60.0]
    assert answer["incumbent"]["n_positions"] == N_POSITIONS
    assert answer["bands"][1]["incumbent_rms_db"] == pytest.approx(
        INCUMBENT["ripple_db"][1]
    )


def test_both_medians_can_be_named_away_from_their_rounds(tmp_path, capsys):
    round_dir = bank_measure_round(tmp_path)
    candidate = tmp_path / "candidate-median.json"
    candidate.write_text(json.dumps(room_median_document()))
    incumbent = tmp_path / "incumbent-median.json"
    incumbent.write_text(json.dumps(room_median_document(**INCUMBENT)))

    assert round_views.main([
        "room-grade", str(round_dir),
        "--room-median", str(candidate),
        "--baseline-room-median", str(incumbent),
    ]) == EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    assert answer["regressed_bands"] == [60.0]
    assert answer["out"] == str(round_dir / "room_grade.json")


@pytest.mark.parametrize(("write_median", "reason"), [
    (None, round_views.REASON_UNREADABLE),
    (lambda path: path.write_text(json.dumps({"freqs_hz": []})), ROOM_MEDIAN_UNAVAILABLE),
])
def test_a_median_the_view_cannot_read_is_unreadable(
    tmp_path, capsys, write_median, reason
):
    round_dir = bank_measure_round(tmp_path)
    if write_median is not None:
        write_median(round_dir / "room_median.json")

    code = round_views.main(["room-grade", str(round_dir)])

    assert code == EXIT_UNREADABLE
    document = json.loads(capsys.readouterr().out)
    assert document["reason"] == reason
    assert document["status"] == "unreadable"
