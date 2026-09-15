# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.repeat_floor import derive_repeat_floor, write_repeat_floor
from jasper.active_speaker.round_packet_report import INDEX_FILENAME, packet_index, series_stats
from jasper.active_speaker.round_verdicts import round_verdicts
from jasper.active_speaker.speaker_fit import design_clouds
from jasper.audio_measurement.interference_nulls import feature_position_variance
from tests.crossover_v2_fixtures import _one_way_preset


@pytest.fixture
def live_round():
    packet = json.loads((Path(__file__).parent / "fixtures/round_d5dbe9ccdbd2.json").read_text())
    for fit in packet["fits"]:
        fit["boost_evidence"] = fit.pop("cloud")
    return packet


@pytest.mark.parametrize(
    "cv,count,total,gain,classification",
    [
        (2, 6, 6, 7, "source_fixed"), (5, 6, 6, -7, "unsure"),
        (10, 6, 6, 7, "position_variant"), (2, 6, 6, -7, "source_fixed"),
        (5, 3, 3, -7, "insufficient_positions"), (2, 6, 10, 7, "source_fixed"),
    ],
)
def test_feature_variance_direction(cv, count, total, gain, classification):
    offsets = np.linspace(-1, 1, count)
    centers = 1000 * (1 + cv / 100 * offsets / np.std(offsets, ddof=1))
    grid = np.geomspace(100, 10000, 1024)
    responses = []
    for center in np.r_[centers, np.repeat(1000, total - count)]:
        depth = gain if len(responses) < count else 1
        magnitude = -depth * np.exp(-0.5 * (np.log2(grid / center) / 0.03) ** 2)
        magnitude -= 2 * gain * np.exp(-0.5 * (np.log2(grid / 1450) / 0.015) ** 2)
        responses.append((grid, magnitude))
    feature = feature_position_variance(responses, freq_hz=1000, q=2, gain_db=gain, positions_total=total)
    assert feature == {"cv_percent": pytest.approx(cv, abs=0.005), "positions_deep": count, "positions_total": total,
                       "classification": classification, "frequencies_hz": pytest.approx(centers, abs=0.05)}


@pytest.mark.parametrize("unit,residual,gap", [
    ("db", 0.5, 10), ("db", 1.5, 0), ("db", 1.0, -10), (None, 0.5, 10),
    ("db", None, 10), ("us", 0.5, None),
])
def test_round_verdict_numbers(tmp_path, live_round, unit, residual, gap):
    (tmp_path / "bundle" / "session").mkdir(parents=True)
    if unit:
        floor = derive_repeat_floor(rounds=[{}, {}], samples={"residual": [0, 1]}, units={"residual": unit})
        write_repeat_floor({**floor, "aggregate_metric": "residual"}, state_path=tmp_path / "repeat-floor.json")
    pose = {"kind": "bearing", "deg": 0, "elevation_deg": 0}
    groups = []
    for role, band, level in (
        ("woofer", [150, 4000], gap),
        ("tweeter", [1600, 20000], 0),
    ):
        curve = {
            "role": role,
            "freqs_hz": [100, 1600, 2400, 4000, 20000],
            "magnitude_db": [100, level, level, level, -100],
            "band_hz": band,
        }
        take = {
            "take_id": role,
            "role": role,
            "pose": pose,
            "selected": True,
            "phase": "measure",
            "curve": curve,
            "fault": None,
            "trusted_floor_hz": 500,
        }
        groups.append(
            {
                "set_id": role,
                "capture_basis": {"candidate_id": "base"},
                "takes": [
                    take,
                    {**take, "take_id": "rejected", "selected": False},
                    {**take, "take_id": "older", "timing": {"ended_s": -1}},
                ],
            }
        )
    groups.append(
        {
            "set_id": "other",
            "capture_basis": {"candidate_id": "other"},
            "takes": [groups[0]["takes"][0]],
        }
    )
    manifest = {"sets": groups}
    sources = live_round["sources"]
    sources["applied_profile"]["recomposition_snapshot"]["preset"]["crossover_regions"][0]["fc_hz"] = 2400
    stats = series_stats({"freqs_hz": [100, 1000, 10000], "deviation_db": [-2, 0, 2],
                          "rms_db": (8 / 3) ** 0.5,
                          "band_means": [{"band_hz": [80, 120], "mean_db": -2}]}, 500)
    identity = {"set_id": "woofer", "take_id": "woofer", "role": "woofer", "pose": pose}
    packet = {
        "round_id": "fixture",
        "program": "speaker",
        "result": "complete",
        "reason": None,
        "level": None,
        "applied": {"candidate": None, "record": None, "layers": {}},
        "artifacts": {"frequency_view": None},
        "limits": {},
        "packet_fingerprint": None,
        "sets": groups,
        "series": [{**identity, "stats": stats}],
        "fits": [
            {
                **identity,
                "residual_rms_db": residual,
                "residual_max_db": 2,
                "reason_summary": {},
                "filters": [],
                "boost_evidence": {
                    "design_poses": 0,
                    "band_spread": [
                        {
                            "center_hz": 2000,
                            "f_lo": 1414,
                            "f_hi": 2828,
                            "sigma_db": 0.4,
                            "max_sigma_db": 0.6,
                        },
                    ],
                },
            }
        ],
    }
    packet["verdicts"] = round_verdicts(
        packet,
        round_inputs(tmp_path),
        manifest=manifest,
        clouds={},
        sources=sources,
    )
    fit, = packet["fits"]
    verdict = fit["verdict"]
    assert verdict["repeat_spread_db"] == (1 if unit == "db" else None)
    assert verdict["residual_within_repeat_spread"] is (
        residual <= 1 if unit == "db" and residual is not None else None
    )
    assert verdict["reason"] == (
        "repeat_floor_not_banked"
        if not unit
        else "repeat_floor_unit_mismatch" if unit != "db"
        else "fit_residual_unavailable"
        if residual is None
        else None
    )
    assert fit["crossover_band_spread"] == {"2000 Hz": fit["boost_evidence"]["band_spread"][0]}
    (ceiling,) = packet["verdicts"]
    assert ceiling["branch_gap_db"] == (abs(gap) if gap is not None else None)
    assert ceiling["louder_role"] == (("woofer" if gap > 0 else "tweeter") if gap else None)
    assert ceiling["null_ceiling_db"] == (
        pytest.approx(3.302, abs=0.001) if gap else None
    )
    assert ceiling["reason"] == ("branch_response_unavailable" if gap is None else None if gap else "equal_branch_levels")
    assert ceiling["band_hz"] == (None if gap is None else [1600, 4000])
    assert ceiling["take_ids"] == ["woofer", "tweeter"]
    json.dumps(packet, allow_nan=False)
    index = packet_index(packet, tmp_path, [], manifest)
    (tmp_path / INDEX_FILENAME).write_text(index)
    for prefix in ("series woofer:", "fit woofer:", "null ceiling "):
        assert sum(line.startswith(prefix) for line in index.splitlines()) == 1


@pytest.mark.parametrize("band_lo,contains_crossover", [(1615.118381017347, True), (2500.01, False)])
def test_live_round_verdicts(tmp_path, live_round, band_lo, contains_crossover):
    fixture = live_round
    manifest, sources = fixture["manifest"], fixture["sources"]
    sources["candidate"] = {"source_preset": {"crossover_regions": [
        {**sources["applied_profile"]["recomposition_snapshot"]["preset"]["crossover_regions"][0], "fc_hz": 8000},
    ]}}
    (tmp_path / "bundle/session").mkdir(parents=True)
    inputs = round_inputs(tmp_path)
    clouds = design_clouds(inputs, manifest)
    for fit in fixture["fits"]:
        fit.update(residual_rms_db=2.4, residual_max_db=6.1, reason_summary={})
        fit["boost_evidence"]["band_spread"][0]["f_lo"] = band_lo
    packet = {"round_id": "d5dbe9ccdbd2", "program": manifest["program"], "result": "complete", "reason": None,
              "level": None, "applied": {"candidate": None, "record": None, "layers": {}},
              "artifacts": {"frequency_view": None}, "limits": {}, "packet_fingerprint": None,
              "sets": [{**group, "takes": [{**take, "fault": None} for take in group["takes"]]}
                       for group in manifest["sets"]], "series": [], "fits": fixture["fits"]}
    packet["verdicts"] = round_verdicts(packet, inputs, manifest=manifest, sources=sources, clouds=clouds)
    assert len(packet["verdicts"]) == 3
    ceiling = packet["verdicts"][0]
    assert ceiling["pose"]["deg"] == 0
    assert ceiling["take_ids"] == ["wired-dd7bfccfb97c6d66_take_0003"] * 2
    assert (ceiling["branch_gap_db"], ceiling["null_ceiling_db"]) == pytest.approx((13.060389458919104, 2.1839927942315023))
    assert ceiling["louder_role"] == "tweeter"
    assert ceiling["band_hz"] == [1600, 4000]
    assert ceiling["capture_graph"] == manifest["sets"][2]["capture_basis"]["graph_fingerprint"]
    for fit in packet["fits"]:
        expected = {"2000 Hz": fit["boost_evidence"]["band_spread"][0]} if contains_crossover else {}
        assert fit["crossover_band_spread"] == expected
        if fit["role"] != "tweeter":
            continue
        band, = fit["boost_evidence"]["band_spread"]
        assert (band["center_hz"], band["sigma_db"], band["max_sigma_db"]) == pytest.approx((2000, 0.815763999253154, 1.103016043550224))
        measured = next(band for band in clouds[fit["set_id"]].band_spread if band.center_hz == 2000)
        assert measured.sigma_db == pytest.approx(band["sigma_db"])
        assert len(fit["filters"]) == 2
        for feature, count, frequencies, cv in zip(fit["filters"], (0, 3), ([], [3937.0, 3935.1, 3936.9]), (None, 0.027)):
            assert feature["position_variance"] == {
                "positions_deep": count, "positions_total": 3, "cv_percent": pytest.approx(cv, abs=0.001) if cv else None,
                "frequencies_hz": pytest.approx(frequencies, abs=0.1), "classification": "insufficient_positions",
            }
        packet["series"].append({**fit, "stats": {"rms_100_10k_db": {"value": 2.4, "below_trusted_floor": False}}})
    packet["series"].append({**packet["series"][0], "pose": {"kind": "seat", "deg": 0, "name": "sofa", "seat_offset_m": [0, 0, 0]}})
    index = packet_index(packet, tmp_path, [], manifest)
    assert any(line.startswith("series tweeter: pose sofa;") for line in index.splitlines())
    for fit, token in zip((f for f in packet["fits"] if f["role"] == "tweeter"), ("0°", "-20°", "+20°")):
        for kind in ("fit", "series"):
            line = next(line for line in index.splitlines() if line.startswith(f"{kind} tweeter: pose {token};"))
            assert fit["take_id"] in line and fit["set_id"] in line
            if kind == "fit" and contains_crossover:
                assert "center_hz=2000 sigma_db=0.8158 max_sigma_db=1.103" in line[:160]
            else:
                assert "2.4" in line[:160]
    json.dumps(packet, allow_nan=False)


@pytest.mark.parametrize("profile", [None, {}, {"recomposition_snapshot": {"preset": {}}}, "passive"])
def test_no_applied_crossover_is_disclosed(tmp_path, live_round, profile):
    if profile == "passive":
        profile = {"recomposition_snapshot": {"preset": _one_way_preset().to_dict()}}
    sources = {"applied_profile": profile, "candidate": {"source_preset": live_round["sources"]["applied_profile"]["recomposition_snapshot"]["preset"]}}
    (tmp_path / "bundle/session").mkdir(parents=True)
    fits = [{**fit, "residual_rms_db": None, "residual_max_db": None, "reason_summary": {}} for fit in live_round["fits"]]
    packet = {"round_id": "passive", "program": "speaker", "result": "complete", "reason": None, "level": None,
              "applied": {"candidate": None, "record": None, "layers": {}}, "artifacts": {"frequency_view": None},
              "limits": {}, "packet_fingerprint": None, "sets": [], "series": [], "fits": fits}
    assert round_verdicts(packet, round_inputs(tmp_path), manifest=live_round["manifest"], sources=sources, clouds={}) == []
    assert all(fit["crossover_band_spread"] is None and fit["crossover_band_spread_reason"] == "no_applied_crossover" for fit in fits)
    index = packet_index(packet, tmp_path, [], {})
    assert index.splitlines().count("crossover_band_spread=null; reason=no_applied_crossover") == 1
