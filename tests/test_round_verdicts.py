# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.intervention import CloudFitTerms
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.repeat_floor import derive_repeat_floor, write_repeat_floor
from jasper.active_speaker.round_packet_report import INDEX_FILENAME, packet_index, series_stats
from jasper.active_speaker.round_verdicts import round_verdicts
from jasper.audio_measurement.program_analysis import DriverResponse


@pytest.mark.parametrize(
    "unit,residual,cv,count,total,gap,gain,classification",
    [
        ("db", 0.5, 2, 6, 6, 10, 7, "source_fixed"),
        ("db", 1.5, 5, 6, 6, 0, -7, "unsure"),
        ("db", 1.0, 10, 6, 6, -10, 7, "position_variant"),
        (None, 0.5, 2, 6, 6, 10, -7, "source_fixed"),
        ("db", 0.5, 5, 3, 3, 0, -7, "insufficient_positions"),
        ("db", None, 2, 6, 10, 10, 7, "source_fixed"),
        ("us", 0.5, 2, 6, 6, None, 7, "source_fixed"),
    ],
)
def test_round_verdict_numbers(
    tmp_path, unit, residual, cv, count, total, gap, gain, classification
):
    (tmp_path / "bundle" / "session").mkdir(parents=True)
    if unit:
        floor = derive_repeat_floor(rounds=[{}, {}], samples={"residual": [0, 1]}, units={"residual": unit})
        write_repeat_floor(
            {**floor, "aggregate_metric": "residual"},
            state_path=tmp_path / "repeat-floor.json",
        )
    offsets = np.linspace(-1, 1, count)
    centers = 1000 * (1 + cv / 100 * offsets / np.std(offsets, ddof=1))
    grid = np.unique(np.r_[np.geomspace(100, 10000, 1024), centers])
    responses = []
    for center in np.r_[centers, np.repeat(1000, total - count)]:
        depth = gain if len(responses) < count else 1
        magnitude = -depth * np.exp(-0.5 * (np.log2(grid / center) / 0.03) ** 2)
        magnitude -= 2 * gain * np.exp(-0.5 * (np.log2(grid / 1450) / 0.015) ** 2)
        responses.append(
            DriverResponse(
                "woofer", grid, magnitude, 10 ** (magnitude / 20) + 0j, {}, None, None
            )
        )
    cloud = CloudFitTerms(n_positions=total, boost_responses=tuple(responses))
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
    sources = {"candidate": {"source_preset": {"crossover_regions": [
        {"fc_hz": 2400, "lower_driver": "woofer", "upper_driver": "tweeter"},
    ]}}}
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
                "filters": [{"freq": 1000, "q": 2, "gain": gain}],
                "cloud": {
                    "design_poses": total,
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
        clouds={"woofer": cloud},
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
    assert fit["crossover_band_spread"] == {"2000 Hz": fit["cloud"]["band_spread"][0]}
    feature = fit["filters"][0]["position_variance"]
    assert feature["cv_percent"] == pytest.approx(cv)
    assert (
        feature["positions_deep"],
        feature["positions_total"],
        feature["classification"],
    ) == (count, total, classification)
    assert feature["frequencies_hz"] == pytest.approx(centers)
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
