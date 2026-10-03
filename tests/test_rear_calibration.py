# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import math
from copy import deepcopy

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import rear_stage_chain_response
from jasper.active_speaker.rear_calibration import (
    MAX_ALLPASS_Q, MAX_CHAIN_BOOST_DB, RearCalibrationError, coefficient_sha256, compile_rear_stage,
    read_rear_calibration, rear_operating_facts,
)
from jasper.active_speaker.rear_seed import REAR_SEED_GEOMETRY_UNDECLARED, rear_seed
from jasper.audio_measurement.evidence_reasons import unavailable
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from tests.active_speaker_fixtures import REAR_SEED_DRAFT, REAR_SEED_GEOMETRY, rear_seed_document


def _compile(data):
    return compile_rear_stage(read_rear_calibration(data, sample_rate=48000),
                              front_channel=4, rear_channel=6, tweeter_channel=5, channel_count=8)


def test_stage_preserves_other_outputs_and_separates_rear_branch_timing():
    data = rear_seed_document(common_delay_ms=2)
    data["rear"]["cancellation"].update(gain_db=-0.84, delay_ms=1.14)
    data["front"]["delay_ms"] = 0.5
    data["rear"]["bass"]["muted"] = True
    stage = _compile(data)
    assert set(stage) == {"filters", "mixers", "pipeline"}
    filters = stage["filters"]
    assert filters["rear_out6_front_delay"]["parameters"]["delay"] == 2.5
    assert filters["rear_out6_bass_delay"]["parameters"]["delay"] == 2.5
    assert filters["rear_out6_cancellation_delay"]["parameters"] == {"delay": 3.64, "unit": "ms"}
    assert filters["rear_out6_cancellation_gain"]["parameters"] == {"gain": -0.84, "inverted": True, "mute": False}
    assert filters["rear_out6_bass_gain"]["parameters"] == {"gain": 0, "inverted": False, "mute": True}
    assert filters["rear_out6_common_5_delay"]["parameters"]["delay"] == 2
    assert not {0, 1, 2, 3, 7} & {channel for step in stage["pipeline"] for channel in step.get("channels", [])}
    split, summed = stage["mixers"]["rear_out6_split"], stage["mixers"]["rear_out6_sum"]
    assert split["channels"] == {"in": 8, "out": 9}
    assert summed["channels"] == {"in": 9, "out": 8}
    for row in summed["mapping"]:
        assert [source["channel"] for source in row["sources"]] == ([6, 8] if row["dest"] == 6 else [row["dest"]])
    assert read_rear_calibration(data) == data


@pytest.mark.parametrize("kind,params", [
    ("Biquad", {"type": kind, "freq": 180, "q": 0.9, **({"gain": -3.0} if kind in {"Peaking", "Lowshelf", "Highshelf"} else {})})
    for kind in ("Highpass", "Lowpass", "Peaking", "Lowshelf", "Highshelf", "Allpass")
] + [("Biquad", {"type": kind, "freq": 180, "q": 0.9, "gain": 6.0})
     for kind in ("Peaking", "Lowshelf", "Highshelf")
] + [("BiquadCombo", {"type": kind, "freq": 200, "order": 4})
     for kind in ("ButterworthHighpass", "ButterworthLowpass", "LinkwitzRileyHighpass", "LinkwitzRileyLowpass")])
def test_filters_keep_all_parameters(kind, params):
    data = rear_seed_document()
    entry = {"type": kind, "parameters": params}
    data["rear"]["cancellation"]["filters"] = [entry]
    assert _compile(data)["filters"]["rear_out6_cancellation_0"] == entry


def test_fir_replaces_both_branches_without_adding_declared_latency_twice():
    data = rear_seed_document(common_delay_ms=0.0)
    coefficients = [0.0, 0.0, -0.75]
    data["rear"] = {"mode": "fir", "coefficients": coefficients, "sample_rate_hz": 48000,
                    "normalization": "as_supplied", "added_latency_ms": 2 / 48,
                    "sha256": coefficient_sha256(coefficients)}
    stage = _compile(data)
    assert not stage["mixers"]
    assert [f["type"] for f in stage["filters"].values()] == ["Gain", "Conv", "Gain"]
    assert stage["filters"]["rear_out6_fir"]["parameters"] == {"type": "Values", "values": coefficients}
    data["rear"]["coefficients"][2] = -1
    with pytest.raises(RearCalibrationError):
        _compile(data)


@pytest.mark.parametrize("field,value", [
    ("gain_db", 6.0), ("gain_db", 0.1), ("gain_db", -151),
    ("gain_db", None), ("delay_ms", True), ("gain_db", "0"),
    ("gain_db", float("nan")), ("inverted", 1),
])
def test_electrical_values_are_explicit_numbers_and_booleans(field, value):
    data = rear_seed_document()
    data["rear"]["bass"][field] = value
    with pytest.raises(RearCalibrationError):
        read_rear_calibration(data)


def test_causality_and_boundary_inclusion_are_explicit():
    data = rear_seed_document(common_delay_ms=0.0)
    data["rear"]["cancellation"]["delay_ms"] = -1
    with pytest.raises(RearCalibrationError):
        _compile(data)
    data["common_delay_ms"] = 2
    assert _compile(data)["filters"]["rear_out6_cancellation_delay"]["parameters"]["delay"] == 1
    data["included_stages"]["rear"] = ["boundary_correction"]
    data["boundary"]["rear"] = [{"type": "Biquad", "parameters": {"type": "Lowshelf", "freq": 70, "q": 0.7, "gain": -3}}]
    with pytest.raises(RearCalibrationError):
        _compile(data)


def test_acoustic_targets_keep_both_sources_and_never_compile_as_electrical():
    data = rear_seed_document()
    for key in ("front", "rear", "boundary", "common_delay_ms", "rear_muted"):
        del data[key]
    data.update(case="acoustic_targets", valid_band_hz=[50, 500],
                targets={"frequency_hz": [50, 500], "front": [[1, 0], [0.9, 0.1]], "rear": [[1, 0], [0, 0]]})
    data["reference"].update(quantity="acoustic_motion", units="m", level=None)
    assert read_rear_calibration(data) == data
    assert data["geometry"]["sources"]["rear"] is None
    with pytest.raises(RearCalibrationError):
        _compile(data)
    ratio_only = deepcopy(data)
    del ratio_only["targets"]["front"]
    with pytest.raises(RearCalibrationError):
        read_rear_calibration(ratio_only)


@pytest.mark.parametrize("params,accepted", [
    ({"type": "Allpass", "q": MAX_ALLPASS_Q}, True),
    ({"type": "Allpass", "q": MAX_ALLPASS_Q + 1.0}, False),
    *[({"type": kind, "q": 0.9, "gain": 6.01}, False)
      for kind in ("Peaking", "Lowshelf", "Highshelf")],
])
def test_filter_q_and_gain_bounds(params, accepted):
    """An all-pass narrower than this rotates the branch sum faster than the
    headroom grid resolves, so the charge could miss a peak it must bound.
    """
    data = rear_seed_document()
    data["rear"]["cancellation"]["filters"] = [
        {"type": "Biquad", "parameters": {"freq": 200, **params}}
    ]
    if accepted:
        assert read_rear_calibration(data, sample_rate=48000) == data
        return
    with pytest.raises(RearCalibrationError):
        read_rear_calibration(data, sample_rate=48000)


def _geometry(back_m: float | None = 0.2, depth_m: float | None = 0.3, toe_deg: float | None = 0.0) -> DeclaredGeometry:
    return DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
                            cabinet_back_wall_m=back_m, cabinet_depth_m=depth_m, toe_in_degrees=toe_deg)


@pytest.mark.parametrize("spacing_mm, back_m, depth_m, toe_deg, band_hz, net_delay_ms", [
    # jts3: woofers 0.33 m apart, the front panel 0.5 m from the wall.
    pytest.param(330.0, 0.2, 0.3, 0.0, (114.33, 259.85), 0.5773, id="jts3"),
    pytest.param(250.0, 0.4, 0.3, 60.0, (103.94, 343.0), 0.4373, id="toed_in"),
])
def test_the_seed_is_a_supercardioid_from_the_declared_geometry(spacing_mm, back_m, depth_m, toe_deg, band_hz,
                                                                 net_delay_ms):
    """The cancellation band runs from c/(6 x the front panel's wall distance) to c/(4 x the woofer spacing);
    that branch is inverted and its delay plus its low-pass make 0.6 x spacing / c at the band centre; the bass
    branch hands over in phase at the band's foot. The seed passes the validator and plays audible."""
    draft = {"manual_settings": {"rear_woofer_spacing_mm": spacing_mm}}
    seed = read_rear_calibration(rear_seed(48000, draft=draft, geometry=_geometry(back_m, depth_m, toe_deg), packet={}),
                                 sample_rate=48000)
    facts = rear_operating_facts(seed)
    assert facts["band_hz"] == pytest.approx(band_hz, abs=0.01)
    assert facts["bass_lowpass_hz"] == pytest.approx(band_hz[0], abs=0.01)
    bass, cancellation = seed["rear"]["bass"], seed["rear"]["cancellation"]
    assert (seed["rear_muted"], bass["inverted"], bass["delay_ms"], cancellation["inverted"]) == (False, False, 0, True)
    centre = math.sqrt(band_hz[0] * band_hz[1])
    grid = np.array([centre / 1.001, centre, centre * 1.001])
    lowpass = {**cancellation, "inverted": False,
               "filters": [item for item in cancellation["filters"] if item["parameters"]["type"].endswith("Lowpass")]}
    phase = np.unwrap(np.angle(rear_stage_chain_response(lowpass, grid, delay_ms=cancellation["delay_ms"])))
    assert -(phase[2] - phase[0]) / (2 * np.pi * (grid[2] - grid[0])) * 1e3 == pytest.approx(net_delay_ms, abs=0.001)
    assert seed["common_delay_ms"] == max(0.0, -cancellation["delay_ms"])
    assert seed["geometry"]["cabinet_back_wall_m"] == back_m
    assert seed["conditions"] == {"trim_db": 0.0, "level_gap_db": None, "pair_round": None}


def _band(centre_hz: float, gap_db: float) -> dict:
    return {"band_hz": [centre_hz / 2 ** (1 / 6), centre_hz * 2 ** (1 / 6)], "front_db": -30.0, "rear_db": -30.0 + gap_db}


@pytest.mark.parametrize("gap_db, trim_db", [(2.5, -2.5), (-3.0, 3.0), (-9.0, MAX_CHAIN_BOOST_DB)])
def test_the_seed_levels_the_rear_woofer_by_the_pair_takes_gap_at_the_mark(gap_db, trim_db):
    """One flat trim on both rear branches, read from the mark's third octaves inside the band;
    a band below it and the pose behind the cabinet are not read."""
    packet = {"round_id": "pair-round", "rear": [{"pair": {"positions": {
        "az+0.00_el+0.00_d+1.00": {"bands": [_band(hz, gap_db) for hz in (125.0, 160.0, 200.0)] + [_band(63.0, 9.0)]},
        "behind_az+0.00_el+0.00_d+0.10": {"bands": [_band(160.0, -12.0)]},
    }}}]}
    seed = rear_seed(48000, draft=REAR_SEED_DRAFT, geometry=REAR_SEED_GEOMETRY, packet=packet)
    assert [item["parameters"]["gain"] for branch in ("bass", "cancellation")
            for item in seed["rear"][branch]["filters"] if item["parameters"]["type"] == "Lowshelf"] == [trim_db] * 2
    assert seed["conditions"] == {"trim_db": trim_db, "level_gap_db": gap_db, "pair_round": "pair-round"}
    assert read_rear_calibration(seed, sample_rate=48000)


@pytest.mark.parametrize("draft, geometry, missing", [
    ({}, REAR_SEED_GEOMETRY, ["rear_woofer_spacing_mm"]),
    (REAR_SEED_DRAFT, None, ["cabinet_back_wall_m", "cabinet_depth_m", "toe_in_degrees"]),
    (REAR_SEED_DRAFT, _geometry(depth_m=None, toe_deg=None), ["cabinet_depth_m", "toe_in_degrees"]),
])
def test_the_seed_names_each_missing_declaration_instead_of_a_default(draft, geometry, missing):
    assert rear_seed(48000, draft=draft, geometry=geometry, packet={}) == unavailable(
        REAR_SEED_GEOMETRY_UNDECLARED, {"missing": missing})
