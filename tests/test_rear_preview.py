# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.crossover_v2 import rear_preview
from jasper.active_speaker.crossover_v2.pose_curve import lateral_pose_curve
from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.crossover_v2.rear_views import pair_takes
from jasper.active_speaker.crossover_v2.room_selection import purpose_take_records
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.rear_calibration import MAX_CHAIN_BOOST_DB, diagnostic_seed
from jasper.audio_measurement import rear_evidence as figures
from jasper.audio_measurement.analysis import band_levels_from_magnitude, smooth_fractional_octave
from jasper.cli import crossover_prescriber
from jasper.cli._refusal import EXIT_UNREADABLE
from tests.test_prescription_document import document
from tests.test_round_views_rear import (
    _PAIR_GAP_MS, _PAIR_LEVEL_GAP_DB, _branch_diagnostic,
    banked_candidates, packet_of, pair_round, rear_round,
)

__all__ = ["banked_candidates"]


def _preview(tmp_path, capsys, sections, root=None, extra=()):
    path = tmp_path / "document.json"
    path.write_text(json.dumps(document("saved", sections)))
    status = crossover_prescriber.main([
        "judge", "--preview", str(path), *(["--round", str(root)] if root else []), *extra,
    ])
    answer = json.loads(capsys.readouterr().out)
    assert (status == 0) == answer["ok"]
    if answer.get("code") == "evidence_unreadable":
        assert status == EXIT_UNREADABLE
    return answer


def test_grid_writes_complete_documents_and_full_previews(tmp_path, capsys):
    root = pair_round(tmp_path)
    section = diagnostic_seed(48000)
    section["rear_muted"] = False
    paths = [f"rear_calibration.rear.{branch}.gain_db" for branch in ("bass", "cancellation")]
    delay = "rear_calibration.rear.cancellation.delay_ms"
    directory = tmp_path / "proposals"
    answer = _preview(tmp_path, capsys, {"rear_calibration": section}, root, (
        "--vary", f"{','.join(paths)}=-2,-1", "--vary", f"{delay}=0,1", "--out-dir", str(directory)))
    assert (answer["section"], answer["adopted"], answer["banked"]) == ("rear_calibration", False, False)
    assert answer["axes"] == [{"paths": paths, "values": [-2, -1]}, {"paths": [delay], "values": [0, 1]}]
    assert len(answer["variants"]) == 4 and len(list(directory.iterdir())) == 8
    for index, row in enumerate(answer["variants"], 1):
        path = Path(row["out"])
        assert path == directory / f"variant-{index:02d}.json"
        variant = read_prescription_document(json.loads(path.read_text()))
        assert variant["sections"]["rear_calibration"]["assumptions"] == section["assumptions"]
        assert variant["rationale"] == document("saved")["rationale"]
        assert set(row["values"]) == {*paths, delay}
        for branch, axis_path in zip(("bass", "cancellation"), paths):
            assert variant["sections"]["rear_calibration"]["rear"][branch]["gain_db"] == row["values"][axis_path]
        full = json.loads(path.with_suffix(".preview.json").read_text())
        single = _preview(tmp_path, capsys, variant["sections"], root)
        assert full == single
        assert row["headroom_charge_db"] == full["preview"]["stage"]["headroom_charge_db"]
        for key, position in row["positions"].items():
            source = full["preview"]["positions"][key]
            assert set(position["bands"]) == {"30-60", "60-100", "90-350", "200-300", "350-700", "700-1500", "1500-5000"}
            assert position["trough_fill_db"] == source["trough_fill_db"]
            assert position["gradient_residual_db"] == source["gradient_residual"]["db"]
            for metric in ("early_late_change_db", "arrival_shift_ms"):
                assert position[metric] == source["late_energy"][metric]
            for band in source["bands"]:
                low, high = band["band_hz"]
                assert position["bands"][f"{low:g}-{high:g}"] == band["change_db"]
                if band["reason"] == figures.REASON_COVERAGE_SHORT:
                    assert position["bands"][f"{low:g}-{high:g}"] is None


def test_grid_continues_after_a_refused_variant_without_writing_it(tmp_path, capsys):
    section = diagnostic_seed(48000)
    section["rear"]["bass"]["filters"] = [{"type": "Biquad", "parameters": {
        "type": "Peaking", "freq": 120, "q": 1, "gain": 3}}]
    directory = tmp_path / "proposals"
    path = "rear_calibration.rear.bass.filters[0].parameters.gain"
    answer = _preview(tmp_path, capsys, {"rear_calibration": section}, pair_round(tmp_path), (
        "--vary", f"{path}=3,{MAX_CHAIN_BOOST_DB + 1},4", "--out-dir", str(directory)))
    rows = answer["variants"]
    assert len(rows) == 3 and sum(row.get("ok") is False for row in rows) == 1
    assert (rows[1]["out"], rows[1]["ok"], rows[1]["code"]) == (None, False, "rear_calibration_invalid")
    assert rows[1]["values"] == {path: MAX_CHAIN_BOOST_DB + 1}
    assert {path.name for path in directory.iterdir()} == {
        f"variant-{index:02d}{suffix}" for index in (1, 3) for suffix in (".json", ".preview.json")}
    assert all(row["positions"] for row in (rows[0], rows[2]))


def test_bad_grid_path_refuses_the_call_without_writing(tmp_path, capsys):
    directory = tmp_path / "proposals"
    answer = _preview(tmp_path, capsys, {"rear_calibration": diagnostic_seed(48000)}, extra=(
        "--vary", "rear_calibration.rear_muted=true,false", "--vary", "rear_calibration.missing=1,2",
        "--out-dir", str(directory)))
    assert (answer["ok"], answer["code"], answer["section"]) == (False, "prescription_malformed", "rear_calibration")
    assert not directory.exists()


@pytest.mark.parametrize("extra", [["--preview"], ["--out-dir", "proposals"]])
def test_grid_requires_preview_and_out_dir(extra):
    with pytest.raises(SystemExit) as caught:
        crossover_prescriber.main(["judge", "seed.json", "--vary", "room.gain=1,2", *extra])
    assert caught.value.code == 2


@pytest.mark.parametrize("front_gain,filter_gain", [(0.0, 0.0), (-0.51, -6.36), (0.0, 4.0)])
def test_muted_document_is_exactly_zero_and_needs_no_document_base(
    tmp_path, capsys, monkeypatch, banked_candidates, front_gain, filter_gain,
):
    root = pair_round(tmp_path, behind_gap_ms=-0.5)
    pair = packet_of(root)[0]["rear"][0]["pair"]

    def unavailable(*args, **kwargs):
        pytest.fail("preview resolved the document base or evidence")

    for module, name in ((crossover_prescriber, "saved_base"),
                         (crossover_prescriber, "find_banked_candidate"),
                         (crossover_prescriber, "_document_evidence")):
        monkeypatch.setattr(module, name, unavailable)
    section = diagnostic_seed(48000)
    section["front"]["gain_db"] = front_gain
    section["front"]["filters"] = [{"type": "Biquad", "parameters": {
        "type": "Peaking", "freq": 190.14, "q": 0.996, "gain": filter_gain}}]
    answer = _preview(tmp_path, capsys, {"rear_calibration": section}, root)
    assert (answer["section"], answer["adopted"], answer["banked"]) == ("rear_calibration", False, False)
    assert answer["sections"] == ["rear_calibration"]
    preview = answer["preview"]
    assert preview["stage"]["pair_candidate_id"] == pair["candidate_id"]
    assert sorted(row["repeats"] for row in preview["positions"].values()) == [1, 1, 1, 2]
    assert any(key.startswith("behind_") and row["pose_kind"] == "behind"
               for key, row in preview["positions"].items())
    for key, row in preview["positions"].items():
        assert row["trough_fill_db"] in (0, None)
        assert row["figures"]["muted"] == row["figures"]["predicted"]
        assert row["figures_band_hz"] == row["band_hz"]
        assert row["reason"] == pair["positions"][key]["reason"]
        assert row["curve"]["change_db"] == [0] * len(row["curve"]["freqs_hz"])
        assert [row["late_energy"][key] for key in (
            "early_late_change_db", "band_energy_change_db", "arrival_shift_ms",
        )] == [0, 0, 0]
        for band in row["bands"]:
            if band["reason"]:
                assert band["reason"] == figures.REASON_COVERAGE_SHORT
                assert (band["change_db"], band["muted_db"], band["predicted_db"]) == (None, None, None)
            else:
                assert band["change_db"] == 0
                assert band["muted_db"] == band["predicted_db"]
        assert any(band["front_chain_db"] != 0 for band in row["bands"]) == bool(front_gain or filter_gain)


@pytest.mark.parametrize("boost_db", [0.0, 4.0])
def test_prediction_uses_the_pair_spectra_for_bands_and_own_peak_energy(tmp_path, capsys, boost_db):
    root = pair_round(tmp_path)
    section = diagnostic_seed(48000)
    section["rear_muted"] = False
    section["front"]["gain_db"] = _PAIR_LEVEL_GAP_DB
    section["rear"]["bass"]["muted"] = True
    section["rear"]["cancellation"].update(gain_db=0.0, delay_ms=0.0)
    notch = {"type": "Biquad", "parameters": {
        "type": "Peaking", "freq": 190.0, "q": 3.0, "gain": -12.0}}
    section["front"]["filters"] = [notch]
    section["rear"]["cancellation"]["filters"] = [notch]
    if boost_db:
        section["rear"]["cancellation"]["filters"].append({"type": "Biquad", "parameters": {
            "type": "Lowshelf", "freq": 100.0, "q": 0.7, "gain": boost_db}})
    preview = _preview(tmp_path, capsys, {"rear_calibration": section}, root)["preview"]
    assert (preview["stage"]["headroom_charge_db"] > 0) == bool(boost_db)
    charge = rear_branch_sum_headroom_db(section) - rear_branch_sum_headroom_db({**section, "rear_muted": True})
    takes = pair_takes(record for _, record in purpose_take_records(round_inputs(root).session_dir, purpose="rear"))
    for take in takes:
        row = preview["positions"][take.pose_key]
        rear, front = rear_stage_response(section, take.freqs_hz)
        predicted = take.front * front + take.rear * rear
        null = np.argmin(np.abs(take.freqs_hz - 1 / (2 * _PAIR_GAP_MS / 1000)))
        if not boost_db:
            assert figures.magnitude_db(predicted)[null] < -40
        sampled = lateral_pose_curve(SimpleNamespace(
            role="summed", freqs_hz=take.freqs_hz, complex_tf=predicted,
            validity_floor_hz=None, gating=None, late_energy=None, repeat_responses=()), take.coverage_hz)
        display_db = smooth_fractional_octave(
            sampled.freqs_hz, figures.magnitude_db(sampled.complex_tf), fraction=figures.FIGURE_FRACTION)
        reference = lateral_pose_curve(SimpleNamespace(
            role="woofer", freqs_hz=take.freqs_hz, complex_tf=take.front * front,
            validity_floor_hz=None, gating=None, late_energy=None, repeat_responses=()), take.coverage_hz)
        change = display_db - smooth_fractional_octave(
            reference.freqs_hz, figures.magnitude_db(reference.complex_tf), fraction=figures.FIGURE_FRACTION)
        display = (sampled.freqs_hz >= row["coverage_hz"][0]) & (sampled.freqs_hz <= min(row["coverage_hz"][1], 5000.0))
        assert row["curve"]["freqs_hz"] == [round(float(hz), 3) for hz in sampled.freqs_hz[display]]
        assert row["curve"]["change_db"] == pytest.approx(change[display] - charge, abs=0.0005)
        dip = row["figures"]["muted"]["dip"]
        assert dip is not None
        assert row["trough_fill_db"] == pytest.approx(
            change[np.argmin(abs(sampled.freqs_hz - dip["hz"]))] - charge, abs=0.0005)
        assert row["trough_fill_db"] == row["curve"]["change_db"][row["curve"]["freqs_hz"].index(dip["hz"])]
        keep = (take.freqs_hz >= row["coverage_hz"][0]) & (take.freqs_hz <= row["coverage_hz"][1])
        expected_gradient = figures.gradient_residual_db(
            take.freqs_hz[keep], rear[keep] / front[keep],
            figures.confident_arrival_gap_s(row["arrival_gap"]), row["band_hz"])
        assert row["gradient_residual"]["db"] == pytest.approx(expected_gradient, abs=0.0005)
        assert row["gradient_residual"]["reason"] == ""
        for band in row["bands"]:
            if not band["reason"]:
                level, = band_levels_from_magnitude(sampled.freqs_hz, display_db, [band["band_hz"]])
                assert band["predicted_db"] == pytest.approx(level, abs=0.0005)
                assert band["change_db"] == pytest.approx(
                    band["predicted_db"] - band["muted_db"] - preview["stage"]["headroom_charge_db"], abs=0.001)
        muted, predicted_energy = [figures.impulse_energy_figures(
            figures.band_limited_impulse(take.freqs_hz, tf, figures.LATE_ENERGY_BAND_HZ),
            sample_rate_hz=take.sample_rate_hz,
        ) for tf in (take.front * front, predicted)]
        for output, source in (("early_late_change_db", "early_late_db"),
                               ("arrival_shift_ms", "centroid_ms"), ("band_energy_change_db", "energy_db")):
            assert row["late_energy"][output] == pytest.approx(predicted_energy[source] - muted[source], abs=0.0005)


@pytest.mark.parametrize("case,code", [
    ("both", "prescription_malformed"), ("no_round", "evidence_unreadable"),
    ("summed", "rear_preview_needs_pair_round"), ("invalid", "rear_calibration_invalid"),
])
def test_preview_refusals(tmp_path, capsys, case, code):
    section = diagnostic_seed(48000)
    sections = {"rear_calibration": section}
    if case == "both":
        sections["room"] = {}
    if case == "invalid":
        section["sample_rate_hz"] = 44100
    root = None if case in ("both", "no_round") else (
        rear_round(tmp_path) if case == "summed" else pair_round(tmp_path))
    answer = _preview(tmp_path, capsys, sections, root)
    assert (answer["code"], answer["section"]) == (code, "rear_calibration")


def test_pair_takes_share_a_window_and_remove_each_clock_shift():
    diagnostic = _branch_diagnostic()
    for index, response in enumerate(diagnostic["responses"]):
        response["impulse"] = [0.0] * 48 + response["impulse"]
        response["pre_guard_samples"] += 48
        response["clock_shift_samples"] = index * 0.75
    take, = pair_takes([{}, {"branch_diagnostic": diagnostic}])
    assert take.freqs_hz == pytest.approx(np.fft.rfftfreq(figures.IMPULSE_FFT_SIZE, 1 / 48000))
    for response, actual in zip(diagnostic["responses"], (take.front, take.rear)):
        windowed = np.asarray(response["impulse"])[48:]
        assert take.impulses[response["role"]] == pytest.approx(windowed)
        expected = np.fft.rfft(windowed, n=figures.IMPULSE_FFT_SIZE) * np.exp(
            2j * np.pi * take.freqs_hz * response["clock_shift_samples"] / 48000)
        assert actual == pytest.approx(expected)
    del diagnostic["responses"][1]["pre_guard_samples"]
    assert pair_takes([{"branch_diagnostic": diagnostic}]) == []


def test_short_coverage_discloses_missing_acoustics(tmp_path, capsys):
    root = pair_round(tmp_path, swept_hz=(20.0, 20.1))
    preview = _preview(tmp_path, capsys, {"rear_calibration": diagnostic_seed(48000)}, root)["preview"]
    for row in preview["positions"].values():
        assert row["figures"]["predicted"]["reason"] == figures.REASON_COVERAGE_SHORT
        assert row["late_energy"]["reason"] == figures.REASON_COVERAGE_SHORT
        assert row["late_energy"]["early_late_change_db"] is None
        assert row["curve"] == {"freqs_hz": [], "change_db": []}


@pytest.mark.parametrize("declared,expected", [(None, None), ([80.0, 400.0], [80.0, 350.0]),
                                               ([400.0, 500.0], None)])
def test_figures_band_and_gradient_reason_are_independent(tmp_path, capsys, monkeypatch, declared, expected):
    root = pair_round(tmp_path)
    section = diagnostic_seed(48000)
    if declared:
        section["rear"]["cancellation"]["filters"] = [
            {"type": "BiquadCombo", "parameters": {"type": kind, "freq": hz, "order": 2}}
            for kind, hz in zip(("ButterworthHighpass", "ButterworthLowpass"), declared)
        ]
    monkeypatch.setattr(figures, "confident_arrival_gap_s", lambda gap: None)
    preview = _preview(tmp_path, capsys, {"rear_calibration": section}, root)["preview"]
    for row in preview["positions"].values():
        assert row["figures_band_hz"] == (row["band_hz"] if declared is None else expected)
        assert row["reason"] == ""
        assert row["gradient_residual"] == {"db": None, "reason": figures.REASON_GAP_NOT_CONFIDENT}
        assert "gradient_residual_db" not in row
        assert row["figures"]["predicted"]["reason"] == ("" if row["figures_band_hz"] else figures.REASON_COVERAGE_SHORT)


def test_repeats_use_mean_magnitudes_and_median_per_take_energy(tmp_path, capsys, monkeypatch):
    root = pair_round(tmp_path)
    original = pair_takes(record for _, record in purpose_take_records(round_inputs(root).session_dir, purpose="rear"))[0]
    takes = [replace(original, front=sign * original.front, rear=sign * gain * original.rear)
             for sign, gain in ((1, 0.2), (-1, 1), (1, 4))]
    section = diagnostic_seed(48000)
    section["rear_muted"] = False
    section["rear"]["bass"]["muted"] = True
    singles = []
    for take in takes:
        monkeypatch.setattr(rear_preview, "pair_takes", lambda records, take=take: [take])
        singles.append(_preview(tmp_path, capsys, {"rear_calibration": section}, root)["preview"]["positions"][take.pose_key])
    monkeypatch.setattr(rear_preview, "pair_takes", lambda records: takes)
    row = _preview(tmp_path, capsys, {"rear_calibration": section}, root)["preview"]["positions"][original.pose_key]
    assert row["repeats"] == 3
    for key in ("early_late_change_db", "band_energy_change_db", "arrival_shift_ms"):
        values = [single["late_energy"][key] for single in singles]
        assert row["late_energy"][key] == pytest.approx(np.median(values), abs=0.001)
        assert abs(np.mean(values) - np.median(values)) > 0.01
    rear, front = rear_stage_response(section, original.freqs_hz)
    curves = [lateral_pose_curve(SimpleNamespace(
        role="summed", freqs_hz=take.freqs_hz, complex_tf=take.front * front + take.rear * rear,
        validity_floor_hz=None, gating=None, late_energy=None, repeat_responses=()), take.coverage_hz) for take in takes]
    freqs = curves[0].freqs_hz
    mean_db = np.mean([figures.magnitude_db(curve.complex_tf) for curve in curves], axis=0)
    sampled_front = lateral_pose_curve(SimpleNamespace(
        role="woofer", freqs_hz=original.freqs_hz, complex_tf=original.front * front,
        validity_floor_hz=None, gating=None, late_energy=None, repeat_responses=()), original.coverage_hz)
    expected = figures.position_figures(
        freqs, mean_db, reference_db=figures.reference_curve_db(freqs, figures.magnitude_db(sampled_front.complex_tf)),
        band_hz=row["figures_band_hz"], coverage_hz=row["coverage_hz"])
    assert row["figures"]["predicted"]["ripple_db"] == pytest.approx(expected["ripple_db"], abs=0.0005)
    levels = band_levels_from_magnitude(freqs, smooth_fractional_octave(
        freqs, mean_db, fraction=figures.FIGURE_FRACTION), figures.LEVEL_BANDS_HZ)
    for band, level in zip(row["bands"], levels):
        if not band["reason"]:
            assert band["predicted_db"] == pytest.approx(level, abs=0.0005)
