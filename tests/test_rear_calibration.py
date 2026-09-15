# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
import json

import pytest

from jasper.active_speaker.rear_calibration import (
    RearCalibrationError, coefficient_sha256, compile_rear_stage, diagnostic_seed, read_rear_calibration,
)
from jasper.cli.crossover_prescriber import main


def _compile(data):
    return compile_rear_stage(data, front_channel=4, rear_channel=6, tweeter_channel=5,
                              channel_count=8, sample_rate=48000)


def test_stage_preserves_other_outputs_and_separates_rear_branch_timing():
    data = diagnostic_seed(48000)
    data.update(common_delay_ms=2, rear_muted=False)
    data["front"]["delay_ms"] = 0.5
    data["rear"]["bass"]["muted"] = True
    stage = _compile(data)
    assert set(stage) == {"filters", "mixers", "pipeline"}
    filters = stage["filters"]
    assert filters["rear_out6_front_delay"]["parameters"]["delay"] == 2.5
    assert filters["rear_out6_bass_delay"]["parameters"]["delay"] == 2.5
    assert filters["rear_out6_cancellation_delay"]["parameters"] == {"delay": 3.64, "unit": "ms", "subsample": True}
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
] + [("BiquadCombo", {"type": kind, "freq": 200, "order": 4})
     for kind in ("ButterworthHighpass", "ButterworthLowpass", "LinkwitzRileyHighpass", "LinkwitzRileyLowpass")])
def test_filters_keep_all_parameters(kind, params):
    data = diagnostic_seed(48000)
    entry = {"type": kind, "parameters": params}
    data["rear"]["cancellation"]["filters"] = [entry]
    assert _compile(data)["filters"]["rear_out6_cancellation_0"] == entry


def test_fir_replaces_both_branches_without_adding_declared_latency_twice():
    data = diagnostic_seed(48000)
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


@pytest.mark.parametrize("field,value", [("gain_db", None), ("delay_ms", True), ("gain_db", "0"), ("gain_db", float("nan")), ("gain_db", 151), ("gain_db", -151), ("inverted", 1)])
def test_electrical_values_are_explicit_numbers_and_booleans(field, value):
    data = diagnostic_seed(48000)
    data["rear"]["bass"][field] = value
    with pytest.raises(RearCalibrationError):
        read_rear_calibration(data)


def test_causality_and_boundary_inclusion_are_explicit():
    data = diagnostic_seed(48000)
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
    data = diagnostic_seed(48000)
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


def test_cli_seed_and_stage_are_read_only_and_rate_bound(tmp_path, capsys):
    assert main(["rear-calibration", "--seed", "--sample-rate", "48000"]) == 0
    seed = json.loads(capsys.readouterr().out)
    assert seed["rear_muted"] is True and seed["valid_band_hz"] is None
    path = tmp_path / "rear.json"
    path.write_text(json.dumps(seed))
    args = ["rear-calibration", "--document", str(path), "--channels", "8", "--front", "4", "--rear", "6", "--tweeter", "5"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["adopted"] is False and result["requires_electrical_fitting"] is False
    assert result["stage"]["filters"]["rear_out6_output_gain"]["parameters"]["mute"] is True
    assert main([*args, "--sample-rate", "44100"]) != 0
    assert json.loads(capsys.readouterr().out)["reason"] == "rear_calibration_invalid"
