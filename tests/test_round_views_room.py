# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room views: the median contract, what persists across the cube, and
where the ceiling comes from."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.room_boundary import (
    ROOM_BOUNDARY_DEFAULT_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
)
from jasper.cli import round_views
from jasper.cli.round_views import room
from tests.crossover_v2_banked_round import SEAT_GRID_HZ, bank_seat_round

#: Away from every feature below, where the ladder alone sets the numbers.
_QUIET_HZ = 200.0


def _bump(centre_hz: float, depth_db: float) -> np.ndarray:
    return depth_db * np.exp(-0.5 * (np.log2(SEAT_GRID_HZ / centre_hz) / 0.12) ** 2)


def _cube() -> list[np.ndarray]:
    """Seven positions: a 1 dB level ladder (median -27 dB, sigma 2 dB), a dip
    at 63 Hz at every position, a peak at 100 Hz at three of them."""
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

    answer = _run(capsys, ["room-median", str(round_dir)])

    doc = json.loads((round_dir / "room_median.json").read_text())
    assert set(doc) == {
        "freqs_hz", "median_db", "spread_db", "n_positions", "positions",
        "ceiling_hz", "ceiling_source", "window",
    }
    freqs = np.asarray(doc["freqs_hz"])
    assert freqs[0] >= room.ROOM_FLOOR_HZ and freqs[-1] <= doc["ceiling_hz"]
    assert (doc["ceiling_hz"], doc["ceiling_source"]) == (ROOM_BOUNDARY_DEFAULT_HZ, "fallback")
    assert (doc["n_positions"], doc["window"]) == (7, "ungated")
    at = int(np.argmin(np.abs(freqs - _QUIET_HZ)))
    assert doc["median_db"][at] == pytest.approx(-27.0)
    assert doc["spread_db"][at] == pytest.approx(2.0)
    assert [p["deviation_db"][at] for p in doc["positions"]] == pytest.approx(
        [i - 3.0 for i in range(7)]
    )
    assert len({p["pose_key"] for p in doc["positions"]}) == 7
    assert answer["mean_spread_db"]["20-60"] == pytest.approx(2.0)


def test_room_persistence_counts_what_holds_across_the_cube(tmp_path: Path, capsys) -> None:
    round_dir = bank_seat_round(tmp_path, magnitudes_db=_cube())

    answer = _run(capsys, ["room-persistence", str(round_dir)])

    doc = json.loads((round_dir / "room_persistence.json").read_text())
    by_kind = {feature["kind"]: feature for feature in doc["features"]}
    assert set(by_kind) == {"dip", "peak"}
    assert by_kind["dip"]["centre_hz"] == pytest.approx(63.0, rel=0.05)
    assert by_kind["dip"]["median_depth_db"] == pytest.approx(-8.0, abs=1.5)
    assert (by_kind["dip"]["n_present"], by_kind["dip"]["presence_fraction"]) == (7, 1.0)
    assert by_kind["peak"]["n_present"] == 3
    assert by_kind["peak"]["presence_fraction"] == pytest.approx(3 / 7)
    assert (answer["persistent"], answer["top"][0]["kind"]) == (1, "dip")


def test_a_round_with_no_seat_takes_is_refused_by_name(tmp_path: Path, capsys) -> None:
    from tests.crossover_v2_banked_round import bank_measure_round

    round_dir = bank_measure_round(tmp_path)

    code = round_views.main(["room-median", str(round_dir)])

    assert code == round_views.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == room.REFUSE_NO_SEAT_TAKES


@pytest.mark.parametrize(
    ("raw_floor_hz", "ceiling_hz"),
    [
        (60.0, ROOM_BOUNDARY_MIN_HZ),
        (130.0, 130.0 * TRUSTED_FLOOR_MULTIPLIER),
        (400.0, ROOM_BOUNDARY_MAX_HZ),
    ],
)
def test_the_ceiling_is_the_applied_trusted_floor_clamped(
    monkeypatch: pytest.MonkeyPatch, raw_floor_hz: float, ceiling_hz: float,
) -> None:
    """``validity_floor_hz`` is the cloud's ``1/T`` floor; the ceiling is its
    trusted ``2.5/T``, inside the room boundary's bounds (ADR-0256 rule 1)."""
    monkeypatch.setattr(
        room, "applied_profile_source",
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": raw_floor_hz}}, ""),
    )

    ceiling = room.room_ceiling(Path("applied-profile.json"))

    assert (ceiling.ceiling_hz, ceiling.source) == (ceiling_hz, "applied_candidate")
    assert ceiling.trusted_floor_hz == pytest.approx(raw_floor_hz * TRUSTED_FLOOR_MULTIPLIER)
    assert ceiling.raw_floor_hz == raw_floor_hz


@pytest.mark.parametrize(
    "source",
    [
        lambda path: (None, "unreadable"),
        lambda path: ({"exclusion_evidence": {"validity_floor_hz": None}}, ""),
        lambda path: ({}, ""),
    ],
)
def test_a_missing_floor_falls_back_and_says_so(monkeypatch: pytest.MonkeyPatch, source) -> None:
    monkeypatch.setattr(room, "applied_profile_source", source)

    ceiling = room.room_ceiling(None)

    assert (ceiling.ceiling_hz, ceiling.source) == (ROOM_BOUNDARY_DEFAULT_HZ, "fallback")
    assert ceiling.trusted_floor_hz is None
    assert isinstance(ceiling.reason, str) and ceiling.reason


def test_room_ceiling_writes_the_disclosed_fallback_for_a_round_with_no_profile(
    tmp_path: Path, capsys,
) -> None:
    round_dir = bank_seat_round(tmp_path)

    answer = _run(capsys, ["room-ceiling", str(round_dir)])

    doc = json.loads((round_dir / "room_ceiling.json").read_text())
    assert doc["ceiling_source"] == answer["ceiling_source"] == "fallback"
    assert doc["ceiling_hz"] == ROOM_BOUNDARY_DEFAULT_HZ
    assert doc["clamp_hz"] == [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ]
    inventory = _run(capsys, ["inventory", str(round_dir)])
    assert inventory["present"] >= 2
    rows = {
        row["artifact"]: row["present"]
        for row in json.loads((round_dir / "inventory.json").read_text())["artifacts"]
    }
    assert rows["room_ceiling.json"] is True
    assert {"room_median.json", "room_persistence.json"} <= set(rows)
