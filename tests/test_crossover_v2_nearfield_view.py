# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import nearfield_view as nv

FREQS = np.geomspace(20.0, 2000.0, 400)
STEP = nv.piston_step_db(0.015, 0.030, 0.057)


def _take(take_id, driver, distance_mm, level_db, *, selected=True, first_low_db=0.0, seed=0):
    rng = np.random.default_rng(seed)
    sweeps = [np.full(FREQS.size, level_db) + rng.normal(0.0, 0.01, FREQS.size) for _ in range(3)]
    sweeps[0] = sweeps[0] + np.where(FREQS < 35.0, first_low_db, 0.0)
    curve = {"freqs_hz": FREQS.tolist(), "magnitude_db": sweeps[0].tolist(),
             "repeat_curves": [{"freqs_hz": FREQS.tolist(), "magnitude_db": sweep.tolist()} for sweep in sweeps[1:]]}
    return {"take_id": take_id, "selected": selected, "pose": {"driver": driver, "distance_m": distance_mm / 1000},
            "quality": {"evidence": {"max_window_db_spl": 80.0}}, "curve": curve}


def test_a_rigid_piston_falls_2_12_db_from_15_to_30_mm_on_a_114_mm_cone():
    assert STEP == pytest.approx(-2.12, abs=0.01)


@pytest.mark.parametrize("diameters,rear_extra_db,verdicts", [
    ({"woofer": 114.0}, -0.2, ("pass", "pass")),
    ({"woofer": 114.0}, -1.0, ("pass", "fail")),
    ({}, -0.2, ("not_evaluated", "not_evaluated")),
])
def test_a_near_field_round_reads_band_by_band_and_self_tests_its_distances(diameters, rear_extra_db, verdicts):
    """Kept takes only, band by band: the first sweep against the two after it,
    and the SNR of the last two. Each driver's re-seat spread, and its level
    step between distances held to a piston of the declared cone (#5684)."""
    takes = [
        _take("w15", "woofer", 15, 90.0, first_low_db=-1.0), _take("w30", "woofer", 30, 90.15 + STEP - 0.1, seed=1),
        _take("w15again", "woofer", 15, 90.3, seed=2), _take("opener", "woofer", 15, 66.0, selected=False),
        _take("r15", "woofer:rear", 15, 84.0, seed=3), _take("r30", "woofer:rear", 30, 84.0 + STEP + rear_extra_db, seed=4),
    ]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_role=diameters)

    assert [row["take_id"] for row in view["takes"]] == ["w15", "w30", "w15again", "r15", "r30"]
    lowest = view["takes"][0]["bands"][0]
    assert (lowest["band_hz"], lowest["trusted"]) == ([20.0, 35.0], True)
    assert lowest["first_minus_rest_db"] == pytest.approx(-1.0, abs=0.05)
    woofer, rear = view["drivers"]
    assert (woofer["driver"], rear["driver"]) == ("woofer", "woofer:rear")
    assert tuple(driver["steps"][0]["verdict"] for driver in (woofer, rear)) == verdicts
    reseat = woofer["placements"][0]
    assert reseat["take_ids"] == ["w15", "w15again"]
    assert reseat["reseat_spread_db"][2] == pytest.approx(0.3, abs=0.05)
