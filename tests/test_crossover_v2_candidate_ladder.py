# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the config ladder at one held pose reduces to honest scalars.

The numbers ``jasper-round-views candidates`` prints are this module's, so
they are pinned here rather than through the CLI. That verb's own suite keeps
only what is its: the exit code and the published record.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.candidate_ladder import (
    REFUSE_NO_LADDER,
    CandidateLadderRefused,
    candidate_ladder,
)
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.crossover_v2.round_captures import REFUSE_CAPTURE_UNREADABLE, doc_pose_key
from jasper.active_speaker.frequency_view import (
    FREQUENCY_VIEW_FILENAME, FrequencyRun, FrequencySeries, build_frequency_view,
)
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.measurement_programs import program
from jasper.audio_measurement.evidence_reasons import REASON_NO_COMPARISON

from tests.crossover_v2_banked_round import bank_measure_round
# The banked-take writer the round-views suite already owns, consumed rather
# than copied: a second idea of what a lateral take looks like would disagree
# with this reader silently.
from tests.test_active_speaker_crossover_v2_round_views import (
    _bank_lateral_pose,
    _summed_curve,
)

pytestmark = pytest.mark.usefixtures("no_real_pi_paths")


def _ladder(round_dir: Path) -> dict:
    return candidate_ladder(round_dir, round_inputs(round_dir))


def _gated_curve(freqs_hz: np.ndarray, magnitude_db: np.ndarray) -> dict:
    """A summed curve as a gated take banks it, with its gate's window."""
    return {**_summed_curve(freqs_hz, magnitude_db), "gate_window_ms": 5.0}


@pytest.mark.parametrize("source", ["records", "frequency"])
@pytest.mark.parametrize("layout", ["seat", "bearing"])
def test_candidate_rows_keep_each_declared_pose(tmp_path, layout, source):
    """Three seats at one bearing are three poses, keyed as the rear views key
    them, whichever reader supplies the curves; the view is the one the bank
    builds from the records, so no take is compared against another seat."""
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    poses = program("seat", "express").poses if layout == "seat" else program("room", "arm").poses
    grid = np.array([500.0, 1000.0, 4000.0])
    records, expected = [], {}
    for index, pose in enumerate(poses):
        metadata = {
            "position_deg": pose.azimuth_deg, "vertical_deg": pose.elevation_deg,
            "pose_kind": pose.kind, "seat_offset_m": pose.seat_offset_m,
            "mark_distance_m": pose.distance_m,
        }
        position = {
            "deg": pose.azimuth_deg, "vertical_deg": pose.elevation_deg,
            "kind": pose.kind, "seat_offset_m": list(pose.seat_offset_m) if pose.seat_offset_m else None,
            "distance_m": pose.distance_m,
        }
        expected[doc_pose_key(metadata)] = (position, float(index + 1))
        for candidate, scale in (("cfg-a", 0), ("cfg-b", index + 1)):
            take_id = f"lateral_{index:02d}_{candidate}"
            curve = _gated_curve(grid, np.array([0.0, 0.0, float(scale)]))
            _bank_lateral_pose(
                session_dir, take_id=take_id, position_deg=pose.azimuth_deg,
                vertical_deg=pose.elevation_deg, candidate_id=candidate, curves=[curve],
            )
            path, = session_dir.glob(f"evidence/v1/artifacts/crossover_v2/*/positions/{take_id}.json")
            path.write_text(json.dumps({**json.loads(path.read_text()), **metadata, "take_id": take_id}))
            records.append(json.loads(path.read_text()))
    if source == "frequency":
        view = build_frequency_view(frequency_run_from_documents(run_id="speaker", documents=records))
        (round_dir / FREQUENCY_VIEW_FILENAME).write_text(json.dumps(view))

    document = json.loads(json.dumps(_ladder(round_dir), allow_nan=False))

    assert (document["summary"]["poses"], document["summary"]["pairs"]) == (3, 3)
    assert (document["summary"]["omitted"], document["summary"]["superseded_take_ids"]) == ([], [])
    assert {row["pose_key"] for row in document["tables"]} == set(expected)
    assert [row["deg"] for row in document["tables"]] == sorted(p.azimuth_deg for p in poses)
    for row in document["tables"]:
        position, gap = expected[row["pose_key"]]
        assert {key: row[key] for key in position} == position
        assert row["played"] == ["cfg-a", "cfg-b"]
        role, = row["roles"]
        assert (role["role"], role.get("window"), role["trusted"]) == ("summed", None, True)
        assert [candidate["candidate_id"] for candidate in role["candidates"]] == ["cfg-a", "cfg-b"]
        delta, = role["deltas"]
        assert (delta["a"], delta["b"], delta["bins"]) == ("cfg-a", "cfg-b", 3)
        assert delta["max_abs_db"] == pytest.approx(gap)
        assert delta["mean_abs_db"] == pytest.approx(gap / 3)
        assert delta["level_offset_db"] == pytest.approx(0)


def test_the_ladder_pairs_the_configs_one_pose_played_and_locates_the_gap(tmp_path):
    """Two configs at one bearing, differing at exactly ONE bin.

    Both curves are flat but for a single +2 dB bin on B, so the median level
    each is normalised against is the same and the whole difference is shape:
    the pair's delta is 2 dB, at that bin's own frequency, and nowhere else.
    B is additionally banked with a superseded earlier attempt at a wild level,
    so no arithmetic that pooled retakes instead of superseding them could
    land on these numbers.
    """
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    grid = np.array([500.0, 1000.0, 2000.0, 4000.0, 8000.0])
    a_db = np.zeros_like(grid)
    b_db = a_db.copy()
    b_db[3] += 2.0
    _bank_lateral_pose(
        session_dir, take_id="lateral_00_a01", position_deg=7,
        candidate_id="cfg-a", curves=[_gated_curve(grid, a_db)],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a01", position_deg=7,
        candidate_id="cfg-b", curves=[_gated_curve(grid, np.full_like(grid, 40.0))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a02", position_deg=7,
        candidate_id="cfg-b", curves=[_gated_curve(grid, b_db)],
    )

    summary = (document := _ladder(round_dir))["summary"]

    assert summary["candidates"] == ["cfg-a", "cfg-b"]
    assert (summary["poses"], summary["pairs"]) == (1, 1)
    assert summary["max_abs_delta_between"] == ["cfg-a", "cfg-b"]
    assert summary["max_abs_delta_db"] == pytest.approx(2.0)
    assert summary["max_abs_delta_hz"] == pytest.approx(4000.0)
    role, = document["tables"][0]["roles"]
    delta, = role["deltas"]
    assert delta["level_offset_db"] == pytest.approx(0.0)
    assert delta["mean_abs_db"] == pytest.approx(2.0 / grid.size)
    assert [row["candidate_id"] for row in role["candidates"]] == ["cfg-a", "cfg-b"]


def test_every_take_no_table_compares_is_listed_under_why(tmp_path):
    """A retake supersedes its earlier attempt, a pose that played one
    candidate compares nothing, and a take with no readable curve cannot be
    read: each is named, so no take leaves the comparison silently."""
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    grid = np.array([500.0, 1000.0, 4000.0])
    for take_id, position_deg, candidate, curves in (
        ("lateral_00_a01", 7, "cfg-a", [_summed_curve(grid, np.zeros(3))]),
        ("lateral_01_a01", 7, "cfg-b", [_summed_curve(grid, np.full(3, 40.0))]),
        ("lateral_01_a02", 7, "cfg-b", [_summed_curve(grid, np.zeros(3))]),
        ("lateral_02_a01", 30, "cfg-a", [_summed_curve(grid, np.zeros(3))]),
        ("lateral_03_a01", 7, "cfg-c", []),
    ):
        _bank_lateral_pose(session_dir, take_id=take_id, position_deg=position_deg,
                           candidate_id=candidate, curves=curves)

    summary = _ladder(round_dir)["summary"]

    assert (summary["poses"], summary["candidates"]) == (1, ["cfg-a", "cfg-b"])
    assert summary["superseded_take_ids"] == ["lateral_01_a01"]
    assert summary["omitted"] == [
        {"capture_id": "lateral_02_a01", "reason": REASON_NO_COMPARISON},
        {"capture_id": "lateral_03_a01", "reason": REFUSE_CAPTURE_UNREADABLE},
    ]


@pytest.mark.parametrize("gate,headline", [
    pytest.param({"gate_window_ms": 5.0, "trusted_floor_hz": 357.0}, (2.0, 1000.0, [357.0, 19700.0]),
                 id="gated_from_its_trusted_floor"),
    pytest.param({"gate_window_ms": None, "trusted_floor_hz": None}, (None, None, None), id="gate_failed"),
])
def test_the_headline_is_the_widest_gap_the_gate_trusts(tmp_path, gate, headline):
    """A seat take is banked ungated and through the reference gate. The
    ungated pair keeps the room and the sweep's low edge (54 dB at 22 Hz), and
    a gated pair below its own trusted floor is the same noise (9 dB at 295
    Hz): neither headlines. A series the gate could not window is still
    labelled gated, so trust is the gate's own result, never the label."""
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    full = np.array([22.0, 200.0, 295.0, 1000.0, 4000.0, 19700.0])
    series = []
    for index, (candidate, edge_db, low_db, gap_db) in enumerate((("cfg-a", 0.0, 0.0, 0.0),
                                                                  ("cfg-b", 54.0, 9.0, 2.0))):
        take_id = f"lateral_{index:02d}_a01"
        _bank_lateral_pose(session_dir, take_id=take_id, position_deg=0, candidate_id=candidate, curves=[])
        for window, freqs, magnitude, fields in (
            ("ungated", full, [edge_db, 0.0, 0.0, 0.0, 0.0, 0.0], {"gate_window_ms": None}),
            ("gated", full[1:], [0.0, low_db, gap_db, 0.0, 0.0], gate),
        ):
            series.append(FrequencySeries(
                f"{take_id}:{window}", candidate, "measurement", tuple(freqs), tuple(magnitude),
                details={"role": "summed", "phase": "lateral", "take_id": take_id,
                         "candidate_id": candidate, "window": window, **fields},
            ))
    view = build_frequency_view(FrequencyRun("room", "room", tuple(series)))
    (round_dir / FREQUENCY_VIEW_FILENAME).write_text(json.dumps(view))

    document = _ladder(round_dir)

    summary, (db, hz, band_hz) = document["summary"], headline
    assert summary["pairs"] == 2
    assert (summary["max_abs_delta_db"], summary["max_abs_delta_hz"], summary["max_abs_delta_band_hz"]) == (
        None if db is None else pytest.approx(db), hz, band_hz)
    roles = {role["window"]: role for role in document["tables"][0]["roles"]}
    assert (roles["gated"]["trusted"], roles["ungated"]["trusted"]) == (gate["gate_window_ms"] is not None, False)
    assert roles["ungated"]["deltas"][0]["max_abs_db"] == pytest.approx(54.0)


@pytest.mark.parametrize("fields", [
    {}, {"phase": "lateral", "position": None}, {"phase": "lateral"},
    {"phase": "lateral", "position": {"deg": 7}},
], ids=["no-lateral-series", "null-position", "missing-position", "missing-take-id"])
def test_in_record_ladder_survives_a_view_without_pose_takes(tmp_path, fields):
    round_dir = tmp_path / "r1"
    grid = np.array([500.0, 1000.0, 4000.0])
    for index, candidate in enumerate(("cfg-a", "cfg-b")):
        _bank_lateral_pose(
            round_dir / "bundle" / "sess1", take_id=f"lateral_{index:02d}_a01",
            position_deg=7, candidate_id=candidate,
            curves=[_summed_curve(grid, np.array([0.0, 0.0, 2.0 * index]))],
        )
    expected = _ladder(round_dir)
    average = FrequencySeries("average", "Average", "measurement", tuple(grid), (0.0,) * 3,
                              details={"phase_deg": [0.0] * 3, **fields})
    view = build_frequency_view(FrequencyRun("speaker", "speaker", (average,)))
    (round_dir / FREQUENCY_VIEW_FILENAME).write_text(json.dumps(view))
    assert _ladder(round_dir) == expected
    assert expected["tables"][0]["played"] == ["cfg-a", "cfg-b"]


def test_explicit_base_graph_remains_in_the_candidate_comparison(tmp_path):
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    grid = np.array([500.0, 1000.0, 2000.0, 4000.0])
    for index, candidate_id in enumerate(("", "cfg-a")):
        _bank_lateral_pose(
            session_dir, take_id=f"lateral_{index:02d}_a01", position_deg=0,
            candidate_id=candidate_id, curves=[_summed_curve(grid, np.zeros_like(grid))],
        )
    path, = session_dir.glob("evidence/v1/artifacts/crossover_v2/*/positions/lateral_00_a01.json")
    record = json.loads(path.read_text())
    record.update(graph_scope="candidate", candidate_id="baseline-fp", graph_fingerprint="a" * 16)
    path.write_text(json.dumps(record))
    assert _ladder(round_dir)["summary"]["candidates"] == ["baseline-fp", "cfg-a"]


def test_the_ladder_compares_only_the_span_both_configs_actually_measured(tmp_path):
    """A config swept over less than its neighbour is compared over the OVERLAP.

    Resampling one curve onto another's grid past its own last bin holds that
    bin's value, so a band taken from the DECLARED sweep rather than from the
    bins banked would publish that endpoint's difference as a disagreement at
    frequencies the shorter config never measured.
    """
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    wide = np.array([200.0, 1000.0, 4000.0, 12000.0])
    short = np.array([200.0, 1000.0])
    _bank_lateral_pose(
        session_dir, take_id="lateral_00_a01", position_deg=0,
        candidate_id="cfg-a", curves=[_gated_curve(wide, np.zeros_like(wide))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a01", position_deg=0,
        candidate_id="cfg-b", curves=[_gated_curve(short, np.array([0.0, 12.0]))],
    )

    document = _ladder(round_dir)

    role, = document["tables"][0]["roles"]
    assert role["band_hz"] == [200.0, 1000.0]
    delta, = role["deltas"]
    assert delta["bins"] == 2
    # Both bins land 6 dB from the 6 dB median offset the pair was levelled by.
    assert document["summary"]["max_abs_delta_db"] == pytest.approx(6.0)


def test_a_cancellation_bin_costs_its_own_bin_and_not_the_round(tmp_path):
    """A level of -inf is what a perfect cancellation banks, and the strict
    writer rejects one: dropping the bin keeps the other four comparable
    instead of failing the whole document over it."""
    round_dir = tmp_path / "r1"
    session_dir = round_dir / "bundle" / "sess1"
    grid = np.array([500.0, 1000.0, 2000.0, 4000.0, 8000.0])
    holed = np.zeros_like(grid)
    holed[2] = -np.inf
    _bank_lateral_pose(
        session_dir, take_id="lateral_00_a01", position_deg=7,
        candidate_id="cfg-a", curves=[_summed_curve(grid, holed)],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a01", position_deg=7,
        candidate_id="cfg-b", curves=[_summed_curve(grid, np.zeros_like(grid))],
    )

    role, = _ladder(round_dir)["tables"][0]["roles"]

    a_row, b_row = role["candidates"]
    assert (a_row["bins"], b_row["bins"]) == (4, 5)
    delta, = role["deltas"]
    assert delta["bins"] == 4
    assert delta["max_abs_db"] == pytest.approx(0.0)
    # The whole document survives the strict writer, which is what the drop
    # buys: ``allow_nan=False`` would have refused a NaN scalar.
    json.dumps(role, allow_nan=False)


def test_the_ladder_refuses_a_round_no_pose_of_which_played_two_configs(tmp_path):
    """One config at a pose is a REPEAT, and ``repeat`` is what measures those.

    The refusal counts what it did see, so a round that walked no ladder is
    told apart from one whose takes named no config at all.
    """
    with pytest.raises(CandidateLadderRefused) as refusal:
        _ladder(bank_measure_round(tmp_path))

    assert refusal.value.reason == REFUSE_NO_LADDER
    assert refusal.value.detail["candidates_named"] == []
    assert refusal.value.detail["poses_walked"] == 1
    assert refusal.value.detail["takes_naming_no_candidate"] == ["lateral_03_a01"]
    # The message a bare ``str()`` would show carries the same evidence, so a
    # caller that publishes neither field still says what was seen.
    assert json.loads(str(refusal.value).split(": ", 1)[1]) == refusal.value.detail
