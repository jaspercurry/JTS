# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import nearfield_view as nv

# A banked curve's grid runs to 20 kHz; a near-field sweep stops at 2 kHz.
FREQS = np.geomspace(20.0, 20_000.0, 600)
STEP = nv.piston_step_db(0.015, 0.030, 0.057)


def _take(take_id, driver, distance_mm, level_db, *, selected=True, first_low_db=0.0, seed=0, band_hz=(20.0, 2000.0)):
    rng = np.random.default_rng(seed)
    sweeps = [np.full(FREQS.size, level_db) + rng.normal(0.0, 0.01, FREQS.size) for _ in range(3)]
    sweeps[0] = sweeps[0] + np.where(FREQS < 35.0, first_low_db, 0.0)
    curve = {"freqs_hz": FREQS.tolist(), "magnitude_db": sweeps[0].tolist(), "band_hz": list(band_hz),
             "repeat_curves": [{"freqs_hz": FREQS.tolist(), "magnitude_db": sweep.tolist()} for sweep in sweeps[1:]]}
    return {"take_id": take_id, "selected": selected, "pose": {"driver": driver, "distance_m": distance_mm / 1000},
            "quality": {"evidence": {"max_window_db_spl": 80.0}}, "curve": curve,
            "level": {"level_db": -30.0}, "artifacts": {"record_id": f"crossover_v2/run/positions/{take_id}.json"}}


def _graph(pad_db):
    """A played graph: program channel 0 to output 0 through a pad, output 1 parked."""
    return {"devices": {"samplerate": 48000, "capture": {"channels": 2}, "playback": {"channels": 2}},
            "mixers": {"route": {"channels": {"in": 2, "out": 2},
                                 "mapping": [{"dest": 0, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]}]}},
            "filters": {"pad": {"type": "Gain", "parameters": {"gain": pad_db}}},
            "pipeline": [{"type": "Mixer", "name": "route"}, {"type": "Filter", "channels": [0], "names": ["pad"]}]}


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


def test_a_driver_reads_raw_with_its_fader_and_played_graph_divided_out():
    """A placement's raw curve pools its takes' settled sweeps with the fader
    and the played graph divided out, so takes played through different pads
    read one driver on one reference, over the band they swept only. A take
    whose graph was not read back, or cannot be modelled, or whose curve sits
    on another grid stays out of it, and the rest of the view still reads (#5713)."""
    coarse = _take("coarse", "woofer", 15, 70.0, seed=3)
    coarse["curve"] = {**coarse["curve"], **{key: coarse["curve"][key][::2] for key in ("freqs_hz", "magnitude_db")},
                       "repeat_curves": [{key: value[::2] for key, value in sweep.items()}
                                         for sweep in coarse["curve"]["repeat_curves"]]}
    unmodelled = _graph(-6.0)
    unmodelled["pipeline"].append({"type": "Processor", "name": "compressor"})
    takes = [_take("a", "woofer", 15, 60.0, first_low_db=-3.0), _take("b", "woofer", 15, 54.0, seed=1),
             _take("unread", "woofer", 15, 70.0, seed=2), _take("unmodelled", "woofer", 15, 70.0, seed=4), coarse]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_role={}, played_graphs={
        "a": _graph(-6.0), "b": _graph(-12.0), "unmodelled": unmodelled, "coarse": _graph(-6.0)})

    raw = view["drivers"][0]["placements"][0]["raw"]
    assert raw["take_ids"] == ["a", "b"]
    assert raw["freqs_hz"] == pytest.approx(FREQS[FREQS <= 2000.0].tolist(), abs=1e-3)
    assert np.asarray(raw["level_db"]) == pytest.approx(96.0, abs=0.05)


def test_a_take_is_read_only_where_its_sweep_reached():
    """Outside its sweep a curve is noise: a take swept from 700 Hz reads only
    the bands it swept whole, and gives no distance step (#5684)."""
    takes = [_take("t15", "tweeter", 15, 80.0, band_hz=(700.0, 2000.0)),
             _take("t30", "tweeter", 30, 78.0, seed=1, band_hz=(700.0, 2000.0))]

    view = nv.nearfield_view(takes, radiating_diameter_mm_by_role={"tweeter": 25.0})

    assert [[band["band_hz"] for band in row["bands"]] for row in view["takes"]] == [[[800.0, 2000.0]]] * 2
    assert view["drivers"][0]["steps"] == []
