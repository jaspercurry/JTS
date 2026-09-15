# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import copy
import json
from unittest.mock import patch

import numpy as np
import pytest

from jasper.active_speaker.bass_table import fit_bass_table
from jasper.active_speaker.bass_table_report import bass_table_rows
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.active_speaker.round_packet import finish_bass_packet
from jasper.active_speaker.round_packet_report import INDEX_FILENAME, bass_table_markdown
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.wired_capture import WiredSplMonitor
from jasper.cli.round_views import main as round_views_main
from tests.test_crossover_v2_frequency_view import bass_fit_pairs as bass_fit_pairs, bass_run as bass_run


DESCRIPTOR = {"low_boost_db": 8, "reference_level_db": 0,
              "detector_lowpass_hz": 100, "compressor_threshold_dbfs": -30}


@pytest.fixture
def pair(bass_fit_pairs):
    pair = copy.deepcopy(bass_fit_pairs[0])
    grid = np.geomspace(20, 200, 1201).tolist()
    for take in pair:
        take["record"].update(level_db=-20, loudness_volume_db=-20)
        take.update(freqs_hz=grid, fundamental_db=[0.] * len(grid), fundamental_qualified=[True] * len(grid),
                    frequency_curve={"freqs_hz": [300, 500, 1000], "magnitude_db": [0, 0, 0]},
                    bands=[{"band_hz": [lo, hi], "fundamental_qualified": True, "estimated_snr_db": 32}
                           for lo, hi in BASS_BANDS_HZ],
                    harmonics={str(order): {"freqs_hz": grid, "relative_db": [-40.] * len(grid),
                                            "qualified": [True] * len(grid)} for order in (2, 3)})
    return pair


def table(pairs, descriptor=DESCRIPTOR):
    return fit_bass_table(pairs, candidate_id="boost", descriptor=descriptor,
                          target={"freqs_hz": [20, 60], "magnitude_db": [0, 0]}, tolerance_db=1)


def test_second_order_corners_and_downward_slope(pair):
    grid = np.asarray(pair[0]["freqs_hz"])
    for take, f0 in zip(pair, (100, 80)):
        take["fundamental_db"] = (-10 * np.log10(1 + (f0 / grid) ** 4)).tolist()
    row, = table([pair])["levels"]
    for key, f0 in (("base_response", 100), ("candidate_response", 80)):
        response = row[key]
        assert response["corner_hz"]["3"] == pytest.approx(f0, rel=10 ** (1 / 120) - 1)
        assert response["corner_bounded"] == {"3": False, "6": False, "10": False}
        # The -3 to -10 dB interval bends toward the -12 dB/octave asymptote.
        assert response["slope_db_per_octave"] == pytest.approx(-12, abs=3.5)
        assert response["qualified_from_hz"] == 20


@pytest.mark.parametrize("hole", [False, True])
def test_corner_stops_at_unqualified_region(pair, hole):
    grid = np.asarray(pair[0]["freqs_hz"])
    for take in pair:
        take["fundamental_db"] = (-10 * np.log10(1 + (40 / grid) ** 4)).tolist()
        take["fundamental_qualified"] = ((grid >= 100) | ((grid < 63) & hole)).tolist()
        for band in take["bands"]:
            band["fundamental_qualified"] = band["band_hz"][0] >= 100 or (hole and band["band_hz"][1] <= 63)
    row, = table([pair])["levels"]
    assert row["fit"] is None if not hole else row["fit"] is not None
    for key in ("base_response", "candidate_response"):
        response = row[key]
        assert response["qualified_from_hz"] == (20 if hole else 100)
        assert response["corner_bounded"] == {"3": True, "6": True, "10": True}
        assert set(response["corner_hz"].values()) == {response["qualified_from_hz"]}


def test_coverage_changes_cannot_create_boost_or_a_false_corner(pair):
    grid = np.asarray(pair[0]["freqs_hz"])
    for take in pair:
        take["fundamental_db"] = (-10 * np.log10(1 + (300 / grid) ** 4)).tolist()
    pair[1]["fundamental_qualified"] = ((grid < 40) | (grid >= 50)).tolist()
    row, = table([pair])["levels"]
    assert row["candidate_response"]["corner_hz"]["3"] is None
    assert row["candidate_response"]["corner_bounded"]["3"] is None
    assert row["candidate_response"]["slope_db_per_octave"] is None
    assert row["realized_boost_db"][2]["value_db"] is None
    assert [band["value_db"] for band in row["realized_boost_db"] if band["value_db"] is not None] == pytest.approx([0] * 8)


@pytest.mark.parametrize("fraction", [1., .5])
def test_prescribed_realized_and_partial_boost_band(pair, fraction):
    pair[1]["fundamental_db"] = [8 * fraction] * len(pair[1]["freqs_hz"])
    row, = table([pair], {**DESCRIPTOR, "delta_highpass_hz": 25, "detector_lowpass_hz": 90})["levels"]
    assert row["prescribed_boost_db"] == 8
    assert row["boost_band_hz"] == [25, 90]
    assert [band["value_db"] for band in row["realized_boost_db"]] == pytest.approx([8 * fraction] * len(BASS_BANDS_HZ))
    assert [band["value_db"] for band in row["compression_db"]] == pytest.approx([8 * (1 - fraction)] * 6)
    assert row["compression_db"][0]["band_hz"] == [25, 30]
    assert row["compression_db"][-1]["band_hz"] == [80, 90]
    assert row["compression_includes"] == ["compressor", "driver"]


@pytest.mark.parametrize("change,verdict", [(0, "harmonics_flat"), (6, "harmonics_rose"), (None, "unknown")])
def test_harmonics_readout(pair, change, verdict):
    if change is None:
        pair[1]["harmonics"] = {}
    else:
        h3 = pair[1]["harmonics"]["3"]
        h3["relative_db"] = [value + (change if 40 <= f < 50 else 0) for f, value in zip(h3["freqs_hz"], h3["relative_db"])]
    row, = table([pair])["levels"]
    assert row["headroom_verdict"] == verdict
    h3 = next(band["orders"]["3"] for band in row["harmonics_delta_db"] if band["band_hz"] == [40, 50])
    assert h3["delta_db"] == change
    assert h3["evidence_floor_db"] == (None if change is None else 1)
    if change == 6:
        rise, = row["headroom_rises"]
        assert (rise["order"], rise["band_hz"], rise["delta_db"]) == (3, [40, 50], 6)
    else:
        assert row["headroom_rises"] == []


def test_confidence_uses_repeats_within_pose(pair):
    one, = table([pair])["levels"]
    assert (one["snr_margin_db"], one["repeat_spread_db"], one["position_spread_db"]) == (12, None, None)
    repeated = copy.deepcopy(pair)
    for side, take in enumerate(repeated):
        take["fundamental_db"] = [2.] * len(take["freqs_hz"])
        take["bands"][2]["estimated_snr_db"] = 27
        for harmonic in take["harmonics"].values():
            harmonic["relative_db"] = [-34 + side * 2.] * len(harmonic["freqs_hz"])
    row, = table([pair, repeated])["levels"]
    assert row["snr_margin_db"] == 7
    assert row["repeat_spread_db"] == pytest.approx(np.sqrt(2))
    assert row["headroom_verdict"] == "harmonics_flat"
    assert row["harmonics_delta_db"][0]["orders"]["3"]["repeat_spread_db"] == pytest.approx(np.sqrt(50))
    for take in repeated:
        take["record"]["position_deg"] = 20
    other_pose, = table([pair, repeated])["levels"]
    assert other_pose["repeat_spread_db"] is None
    assert other_pose["headroom_verdict"] == "harmonics_rose"


def test_realized_boost_uses_the_same_pose_medians_as_the_fit(pair):
    pairs = [copy.deepcopy(pair) for _ in range(3)]
    for repeat, levels in zip(pairs, ((0, 20), (10, 10), (20, 20))):
        for take, level in zip(repeat, levels):
            take["fundamental_db"] = [level] * len(take["freqs_hz"])
    row, = table(pairs)["levels"]
    assert [band["value_db"] for band in row["realized_boost_db"]] == pytest.approx([10] * len(BASS_BANDS_HZ))
    assert row["fit"]["choices"][-1]["realized_boost_db"] == pytest.approx([10] * len(row["fit"]["freqs_hz"]))


@pytest.mark.parametrize("calibrated", [False, True])
def test_spl_uses_each_takes_calibrated_capture_statistic(pair, calibrated):
    for take, sensitivity in zip(pair, (-10, -12)):
        if calibrated:
            monitor = WiredSplMonitor(MicSensitivity(sensitivity), ceiling_db_spl=85, channel=0)
            samples = np.full(1000, round(.01 * np.iinfo(np.int32).max), dtype="<i4")
            monitor.observe(samples.tobytes(), len(samples), 1, sample_rate_hz=1000)
            take["record"]["capture_integrity"] = {"spl": {"loudest_half_second_db_spl": monitor.loudest_half_second_db_spl}}
    row, = table([pair])["levels"]
    assert row["base_db_spl_at_mark"] == (pytest.approx(64) if calibrated else None)
    assert row["candidate_db_spl_at_mark"] == (pytest.approx(66) if calibrated else None)


@pytest.mark.parametrize("gain,coverage,outcome", [
    (6, 20, "target_met"), (3, 20, "target_not_met"), (10, 20, "measurement_required"),
    (6, 40, "insufficient_evidence"), (6, 63, "insufficient_evidence"),
])
def test_all_outcomes_keep_one_row_shape(pair, gain, coverage, outcome):
    passing, = table([pair])["levels"]
    for index, take in enumerate(pair):
        take["fundamental_db"] = [-6 + index * gain] * len(take["freqs_hz"])
        take["fundamental_qualified"] = [f >= coverage for f in take["freqs_hz"]]
    row, = table([pair])["levels"]
    assert set(row) == set(passing)
    assert row["outcome"] == outcome
    assert (row["fit"] is None) == (coverage == 63)
    assert row["code"] == ("bass_fit_common_coverage_unavailable" if coverage == 63 else None)
    assert row["candidate_response"]["qualified_from_hz"] == coverage
    assert row["realized_boost_db"][-1]["value_db"] == pytest.approx(gain)
    json.dumps(row, allow_nan=False)


def test_packet_index_and_cli_share_the_level_report(bass_run, capsys, tmp_path):
    bass_run.write()
    assert round_views_main(bass_run.argv) == 0
    output = capsys.readouterr()
    payload = json.loads(bass_run.out.read_text())
    rows = bass_table_rows(payload)
    assert json.loads(output.out)["levels"] == rows
    assert len(rows) == 3
    assert rows[0]["realized_boost_db"][3]["value_db"] == pytest.approx(6)
    packet = {"round_id": "bass", "program": "bass", "result": "complete", "reason": None, "level": None,
              "applied": {"candidate": None, "record": None, "layers": {}},
              "artifacts": {"frequency_view": None, "bass_views": []}, "limits": {},
              "sets": [], "fits": [], "series": [], "packet_fingerprint": None}
    (tmp_path / "packet.json").write_text(json.dumps(packet))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"runs": [{"level": {"run": {"level_db": level}}} for level in (-30, -20, -10)], "sets": []}))
    with patch("jasper.active_speaker.round_packet_report.bass_table_markdown", wraps=bass_table_markdown) as render:
        finish_bass_packet(tmp_path, manifest, join_levels=lambda *a, **kw: bass_run.out)
    assert render.call_args.args[0] == rows
    cells = [line.strip("|").split("|") for line in (tmp_path / INDEX_FILENAME).read_text().splitlines() if line.startswith("|")][2:]
    assert [float(cells[1]) for cells in cells] == [row["level_key"]["level_db"] for row in rows]
    assert json.loads((tmp_path / "packet.json").read_text())["bass_table"] == payload
