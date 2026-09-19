# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from jasper.active_speaker.bass_fit import BASS_GRID_POINTS, fit_bass_shape
from jasper.active_speaker.bass_level_evidence import bass_level_evidence
from jasper.active_speaker.bass_table import fit_bass_table
from jasper.active_speaker.bass_table_report import bass_table_rows
from jasper.active_speaker.crossover_v2.round_inputs import RoundViewsError
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.active_speaker.round_packet import finish_bass_packet
from jasper.active_speaker.round_packet_report import INDEX_FILENAME, bass_table_markdown
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.wired_capture import WiredSplMonitor
from jasper.bass_extension.dynamic import DynamicBassDescriptor, expected_boost_db
from jasper.cli.round_views import main as round_views_main
from jasper.cli.round_views._bass_inputs import fit_run
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
    return fit_bass_table(pairs, candidate_id="boost", descriptor=descriptor)


def test_second_order_corners_and_downward_slope(pair):
    grid = np.asarray(pair[0]["freqs_hz"])
    for take, f0 in zip(pair, (100, 80)):
        take["fundamental_db"] = (-10 * np.log10(1 + (f0 / grid) ** 4)).tolist()
    row, = table([pair])["levels"]
    for key, f0 in (("base_response", 100), ("candidate_response", 80)):
        response = row[key]
        assert response["corner_hz"] == {str(depth): pytest.approx(
            f0 / (10 ** (depth / 10) - 1) ** .25, rel=10 ** (1 / 120) - 1) for depth in (3, 6, 10)}
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
@pytest.mark.parametrize("fader_db", [-20., -10.])
def test_prescribed_realized_and_partial_boost_band(pair, fraction, fader_db):
    descriptor = {**DESCRIPTOR, "delta_highpass_hz": 25, "detector_lowpass_hz": 90}
    settings = DynamicBassDescriptor(**descriptor)
    for take in pair:
        take["record"]["loudness_volume_db"] = fader_db
    pair[1]["fundamental_db"] = (fraction * np.asarray(expected_boost_db(
        settings, fader_db, pair[1]["freqs_hz"],
    ))).tolist()
    row, = table([pair], descriptor)["levels"]
    grid = np.geomspace(BASS_BANDS_HZ[0][0], BASS_BANDS_HZ[-1][1], BASS_GRID_POINTS)
    expected = np.asarray(expected_boost_db(settings, fader_db, grid))
    assert row["prescribed_boost_db"] == -fader_db * .4
    assert row["boost_band_hz"] == [25, 90]
    # One-third-octave power smoothing shifts band means by less than 0.15 dB.
    for band in row["realized_boost_db"]:
        lo, hi = band["band_hz"]
        mean = np.mean(expected[(grid >= lo) & (grid < hi)])
        assert band["prescribed_boost_db"] == pytest.approx(mean)
        assert band["value_db"] == pytest.approx(mean * fraction, abs=.15)
    for band in row["compression_db"]:
        lo, hi = band["band_hz"]
        mean = np.mean(expected[(grid >= lo) & (grid < hi)])
        assert band["value_db"] == pytest.approx(mean * (1 - fraction), abs=.15)
    assert row["compression_db"][0]["band_hz"] == [25, 30]
    assert row["compression_db"][-1]["band_hz"] == [80, 90]
    assert row["compression_includes"] == ["compressor", "driver", "shelf_model_error"]


def test_live_boost_readings_follow_the_prescribed_shape(pair):
    descriptor = {**DESCRIPTOR, "low_boost_db": 12, "delta_highpass_hz": 25, "detector_lowpass_hz": 125}
    for take in pair:
        take["record"].update(level_db=-16.5, loudness_volume_db=-16.5)
    aligned = fit_bass_shape([pair], candidate_id="boost")
    for (lo, hi), realized in zip(BASS_BANDS_HZ[3:7], (8.8, 6.0, 3.9, 2.8)):
        aligned["delta"][(aligned["freqs_hz"] >= lo) & (aligned["freqs_hz"] < hi)] = realized
    row = bass_level_evidence(aligned, descriptor=DynamicBassDescriptor(**descriptor), prescribed_boost_db=9.9)
    bands = [band for band in row["realized_boost_db"] if 50 <= band["band_hz"][0] <= 100]
    assert [band["prescribed_boost_db"] for band in bands] == pytest.approx([7.938, 6.167, 4.254, 2.694], abs=.001)
    compression = [band["value_db"] for band in row["compression_db"] if band["band_hz"][0] >= 50]
    assert compression == pytest.approx([-.862, .167, .354, -.106], abs=.001)


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
    for take in pair:
        del take["bands"]
    missing, = table([pair])["levels"]
    assert missing["snr_margin_db"] is None
    assert missing["base_response"]["qualified_from_hz"] is missing["candidate_response"]["qualified_from_hz"] is None


def test_realized_boost_uses_the_same_pose_medians_as_the_fit(pair):
    pairs = [copy.deepcopy(pair) for _ in range(3)]
    for repeat, levels in zip(pairs, ((0, 20), (10, 10), (20, 20))):
        for take, level in zip(repeat, levels):
            take["fundamental_db"] = [level] * len(take["freqs_hz"])
    row, = table(pairs)["levels"]
    assert [band["value_db"] for band in row["realized_boost_db"]] == pytest.approx([10] * len(BASS_BANDS_HZ))


def test_realized_boost_smooths_each_qualified_run_once(pair):
    grid = np.geomspace(20, 200, 121)
    shared = np.ones(grid.size, dtype=bool)
    shared[61] = False
    base = -6 + 5 * np.log2(grid / 100)
    candidate = base + 6 + 4 * np.tanh(20 * np.log2(grid / grid[61]))
    for take, curve in zip(pair, (base, candidate)):
        take.update(freqs_hz=grid.tolist(), fundamental_db=curve.tolist(), fundamental_qualified=shared.tolist())
    expected = np.full(grid.size, np.nan)
    for section in (slice(0, 61), slice(62, None)):
        baseline, treated = [smooth_fractional_octave(grid[section], curve[section], fraction=3)
                             for curve in (base, candidate)]
        expected[section] = treated - baseline
    row, = table([pair])["levels"]
    assert [band["value_db"] for band in row["realized_boost_db"]] == pytest.approx([
        np.nanmean(expected[(grid >= lo) & (grid < hi)]) for lo, hi in BASS_BANDS_HZ])


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


@pytest.mark.parametrize("coverage", [20, 40, 63, 201])
def test_partial_and_empty_coverage_keep_one_row_shape(pair, coverage):
    complete, = table([pair])["levels"]
    for index, take in enumerate(pair):
        take["fundamental_db"] = [-6 + index * 10] * len(take["freqs_hz"])
        take["fundamental_qualified"] = [f >= coverage for f in take["freqs_hz"]]
    row, = table([pair])["levels"]
    assert set(row) == set(complete)
    assert row["candidate_response"]["qualified_from_hz"] == (coverage if coverage < 200 else None)
    assert row["realized_boost_db"][-1]["value_db"] == (pytest.approx(10) if coverage < 200 else None)
    assert all((band["prescribed_boost_db"] is None) is (band["value_db"] is None) for band in row["realized_boost_db"])
    json.dumps(row, allow_nan=False)


@pytest.fixture
def ladder(pair):
    def build(*, knee=True, repeats=2, spread=0., snr=21):
        pairs = []
        for spl in (72, 75, 78, 81):
            for repeat in range(repeats):
                rung = copy.deepcopy(pair)
                for side, take in enumerate(rung):
                    take["record"].update(level_db=spl - 100, loudness_volume_db=spl - 100,
                                          capture_integrity={"spl": {"loudest_half_second_db_spl": spl + side - 1}})
                    take["frequency_curve"]["magnitude_db"] = [spl - 100] * 3
                    take["fundamental_db"] = [spl - 100 + side] * len(take["freqs_hz"])
                    for band in take["bands"]:
                        band["estimated_snr_db"] = snr
                    take["harmonics"]["3"]["relative_db"] = [
                        -40 + (2 * max(0, spl - 75) if knee and side and 40 <= f < 50 else 0)
                        + (spread if repeat else -spread) for f in take["freqs_hz"]]
                    for harmonic in take["harmonics"].values():
                        harmonic["floor_relative_db"] = [value - snr for value in harmonic["relative_db"]]
                pairs.append(rung)
        return pairs
    return build


@pytest.mark.parametrize("knee,repeats,spread,snr,expected_knee,allowance", [
    (True, 2, .25, 32, 78, 1 / 6),
    (False, 2, .25, 32, None, 1 / 6),
    (True, 2, 3., 32, None, 2.),
])
def test_ladder_knee_and_measured_headroom(ladder, knee, repeats, spread, snr, expected_knee, allowance):
    result = table(ladder(knee=knee, repeats=repeats, spread=spread, snr=snr))
    levels = result["levels"]
    bands = [row["headroom"]["candidate"][2] for row in levels]
    assert bands[0]["growth_db_per_db"] == {"fundamental": None, "2": None, "3": None}
    for band, h3 in zip(bands[1:], (1, 3 if knee else 1, 3 if knee else 1)):
        assert band["growth_db_per_db"] == pytest.approx({"fundamental": 1, "2": 1, "3": h3})
        assert band["allowance_db_per_db"]["3"] == pytest.approx(allowance)
        assert band["allowance_basis"]["3"] == "repeat_spread_db"
    for spl, band in zip((72, 75, 78, 81), bands):
        assert band["band_hz"] == [40, 50]
        assert band["knee_level_db_spl"] == expected_knee
        assert band["knee_bounded"] == ("above_top_rung" if expected_knee is None else None)
        assert band["top_clean_level_db_spl"] == (81 if expected_knee is None else 75)
        assert band["headroom_remaining_db"] == (81 if expected_knee is None else 75) - spl
        assert band["extrapolated"] is (expected_knee is None)
        assert "basis" not in band
    for row in levels:
        assert all(band["knee_level_db_spl"] is None for band in row["headroom"]["base"])
        assert row["headroom"]["candidate"][0]["knee_bounded"] == "above_top_rung"
    assert bass_table_rows({"tables": [result]})[0]["headroom"] == levels[0]["headroom"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("weak_reading,rung,snr,expected_knee", [
    ("fundamental", 1, 80, 78), ("fundamental", 1, 20, None), ("fundamental", 2, 20, None),
    ("3", 1, 20, None), ("3", 2, 20, None),
])
def test_single_repeat_knee_uses_worst_reading_noise(ladder, weak_reading, rung, snr, expected_knee):
    pairs = ladder(repeats=1, knee=False, snr=80)
    for i, (_, take) in enumerate(pairs):
        take["harmonics"]["3"]["relative_db"] = [-40 + (1 if i >= 2 and 40 <= f < 50 else 0)
                                                    for f in take["freqs_hz"]]
    take = pairs[rung][1]
    if weak_reading == "fundamental":
        take["bands"][2]["estimated_snr_db"] = snr
    else:
        harmonic = take["harmonics"][weak_reading]
        harmonic["floor_relative_db"] = [value - snr for value in harmonic["relative_db"]]
    band = table(pairs)["levels"][2]["headroom"]["candidate"][2]
    assert band["growth_db_per_db"] == pytest.approx({"fundamental": 1, "2": 1, "3": 4 / 3})
    assert band["allowance_db_per_db"]["3"] == pytest.approx(80 * np.log10(1 + 10 ** (-snr / 20)) / 3)
    assert band["allowance_basis"]["3"] == "snr_uncertainty_db"
    assert band["knee_level_db_spl"] == expected_knee


@pytest.mark.parametrize("gap,top,unmeasured", [(0, 81, [1]), (1, 81, [1, 2]), (3, 78, [3])])
def test_ladder_bound_uses_top_measurable_rung_and_lists_gaps(ladder, gap, top, unmeasured):
    pairs = ladder(repeats=1, knee=False)
    for harmonic in pairs[gap][1]["harmonics"].values():
        harmonic["qualified"] = [False] * len(harmonic["freqs_hz"])
    levels = table(pairs)["levels"]
    for row in levels:
        band = row["headroom"]["candidate"][2]
        assert band["knee_level_db_spl"] is None
        assert band["knee_bounded"] == "above_top_rung"
        assert band["top_clean_level_db_spl"] == top
        assert band["headroom_remaining_db"] == top - row["candidate_db_spl_at_mark"]
        assert band["unmeasured_level_keys"] == [levels[i]["level_key"] for i in unmeasured]
        assert band["extrapolated"] is True


def test_ladder_h2_knee_uses_fader_steps_and_stops_at_the_first_knee(ladder):
    pairs = ladder()
    for pair in pairs:
        for take in pair:
            take["record"]["level_db"] *= 2
            take["record"]["loudness_volume_db"] *= 2
            take["harmonics"]["2"], take["harmonics"]["3"] = take["harmonics"]["3"], take["harmonics"]["2"]
            if take["record"]["level_db"] == -38:
                take["harmonics"]["2"]["relative_db"] = [-40.] * len(take["freqs_hz"])
    levels = table(pairs)["levels"]
    knee = levels[2]["headroom"]["candidate"][2]
    assert knee["growth_db_per_db"] == pytest.approx({"fundamental": .5, "2": 1.5, "3": .5})
    assert knee["knee_orders"] == [2]
    assert levels[-1]["headroom"]["candidate"][2]["knee_level_db_spl"] == 78
    assert levels[-1]["headroom"]["candidate"][2]["top_clean_level_db_spl"] == 75


@pytest.mark.parametrize("fault", ["harmonics", "fundamental", "pose", "spl", "snr", "harmonic_snr", "coverage"])
def test_ladder_missing_evidence_does_not_create_a_knee_or_headroom(ladder, fault):
    pairs = ladder(repeats=1, knee=fault != "coverage")
    for i, pair in enumerate(pairs):
        for take in pair:
            if fault == "harmonics":
                for harmonic in take["harmonics"].values():
                    harmonic["qualified"] = [False] * len(harmonic["freqs_hz"])
            elif fault == "fundamental":
                take["fundamental_qualified"] = [False] * len(take["freqs_hz"])
            elif fault == "pose":
                take["record"]["position_deg"] = i * 10
            elif fault == "spl":
                del take["record"]["capture_integrity"]
            elif fault == "snr":
                for band in take["bands"]:
                    del band["estimated_snr_db"]
            elif fault == "harmonic_snr":
                for harmonic in take["harmonics"].values():
                    del harmonic["floor_relative_db"]
            else:
                for harmonic in take["harmonics"].values():
                    harmonic["qualified"] = [f < 45 if i % 2 else f >= 45 for f in harmonic["freqs_hz"]]
    levels = table(pairs)["levels"]
    for row in levels:
        band = row["headroom"]["candidate"][2]
        assert band["knee_level_db_spl"] is None
        assert band["knee_bounded"] is None
        assert band["headroom_remaining_db"] is None
        if fault in {"harmonics", "fundamental", "pose", "coverage"}:
            assert band["growth_db_per_db"]["2"] is band["growth_db_per_db"]["3"] is None
    json.dumps(levels, allow_nan=False)


@pytest.mark.parametrize("baseline_only", [False, True])
def test_packet_index_and_cli_share_the_level_report(bass_run, capsys, tmp_path, monkeypatch, baseline_only):
    bass_run.write(takes=bass_run.takes[::2] if baseline_only else bass_run.takes)
    if baseline_only:
        monkeypatch.setattr("jasper.cli.round_views._bass_inputs.fit_run",
                            lambda args: fit_run(SimpleNamespace(**{**vars(args), "candidate": []})))
    assert round_views_main(bass_run.argv) == 0
    output = capsys.readouterr()
    payload = json.loads(bass_run.out.read_text())
    rows = bass_table_rows(payload)
    assert json.loads(output.out)["levels"] == rows
    assert len(rows) == 3
    levels = payload["tables"][0]["levels"]
    assert [row["realized_boost_db"] for row in rows] == [level["realized_boost_db"] for level in levels]
    assert [row["headroom"] for row in rows] == [level["headroom"] for level in levels]
    assert [row["compression_includes"] for row in rows] == [level["compression_includes"] for level in levels]
    assert rows[0]["realized_boost_db"][3]["value_db"] == pytest.approx(0 if baseline_only else 6)
    assert (levels[0]["prescribed_boost_db"] is None) is baseline_only
    assert all((band["prescribed_boost_db"] is None) is (baseline_only or band["value_db"] is None)
               for row in rows for band in row["realized_boost_db"])
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
    assert all(len(row) == 11 for row in cells)
    for cells, row in zip(cells, rows):
        values = [band.split(":")[1].strip().split(" / ") for band in cells[4].split(";")]
        assert [[float(value) if value != "null" else None for value in band] for band in values] == [
            [round(band[key], 1) if band[key] is not None else None for key in ("prescribed_boost_db", "value_db")]
            for band in row["realized_boost_db"]]
    assert json.loads((tmp_path / "packet.json").read_text())["bass_table"] == payload
    finish_bass_packet(tmp_path, manifest, join_levels=Mock(side_effect=RoundViewsError("missing inputs")))
    assert json.loads((tmp_path / "packet.json").read_text())["bass_table"] == {
        "status": "unavailable", "code": "bass_fit_inputs_missing", "error_type": "RoundViewsError",
    }
