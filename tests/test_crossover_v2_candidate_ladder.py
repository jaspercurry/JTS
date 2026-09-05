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
        candidate_id="cfg-a", curves=[_summed_curve(grid, a_db)],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a01", position_deg=7,
        candidate_id="cfg-b", curves=[_summed_curve(grid, np.full_like(grid, 40.0))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a02", position_deg=7,
        candidate_id="cfg-b", curves=[_summed_curve(grid, b_db)],
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
        candidate_id="cfg-a", curves=[_summed_curve(wide, np.zeros_like(wide))],
    )
    _bank_lateral_pose(
        session_dir, take_id="lateral_01_a01", position_deg=0,
        candidate_id="cfg-b", curves=[_summed_curve(short, np.array([0.0, 12.0]))],
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
    assert refusal.value.detail["takes_naming_no_candidate"] == 1
    # The message a bare ``str()`` would show carries the same evidence, so a
    # caller that publishes neither field still says what was seen.
    assert json.loads(str(refusal.value).split(": ", 1)[1]) == refusal.value.detail
