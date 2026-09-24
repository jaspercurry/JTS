# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json

import numpy as np
import pytest

from jasper.active_speaker.bass_fit import fit_bass_shape
from jasper.active_speaker.bass_comparison import compare_bass_takes
from jasper.active_speaker.bench.bass_replay import bass_replay_levels
from jasper.active_speaker.bench.replay import replay_levels
from jasper.active_speaker.bass_level_evidence import bass_level_evidence
from jasper.active_speaker.crossover_v2 import rear_preview, rear_views
from jasper.active_speaker.crossover_v2.room_grade import grade_room_median, read_room_median
from jasper.active_speaker.crossover_v2.room_selection import SeatTake
from jasper.active_speaker.linearization_envelope import DEFAULT_ENVELOPE_GRID_HZ, EnvelopeCurve
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.active_speaker.flat_spec_views import directivity_table, log_pooled_residual
from jasper.active_speaker.measurement_bass import bass_view
from jasper.active_speaker.rear_calibration import diagnostic_seed
from jasper.active_speaker.speaker_fit import _envelope_answer
from jasper.audio_measurement.band_ladders import BAND_LADDERS, CROSSOVER_SNR_BANDS_HZ, SNR_BANDS_HZ
from jasper.json_fields import sha256_file
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.snr_policy import band_snr_verdicts, framed_ambient_band_report
from jasper.cli.round_views import main
from tests.room_median_fixture import room_median_document
from tests.test_active_speaker_crossover_v2_round_views import gate_sweep_round as gate_sweep_round
from tests.test_bass_level_evidence import pair as pair
from tests.test_crossover_v2_round_frequency_view import (
    bass_fit_pairs as bass_fit_pairs,
    summed_capture_bundle as summed_capture_bundle,
)
from tests.test_cli_close_reference import _compare_argv, rounds as rounds
from tests.test_crossover_v2_gate_sweep import direct_only_report as direct_only_report
from tests.test_flat_spec_views import _position
from tests.test_round_views_rear import _branch_diagnostic, _pair_curves


def test_ladder_edges_are_frozen_at_the_measured_values():
    assert BAND_LADDERS == {
        "rear_upper": ((350.0, 700.0), (700.0, 1500.0), (1500.0, 5000.0)),
        "rear_level": ((30.0, 60.0), (60.0, 100.0), (90.0, 350.0), (200.0, 300.0),
                       (350.0, 700.0), (700.0, 1500.0), (1500.0, 5000.0)),
        "rear_late_energy": ((90.0, 250.0),),
        "rear_arrival_gap": ((90.0, 315.0),),
        "bass": ((20.0, 30.0), (30.0, 40.0), (40.0, 50.0), (50.0, 63.0),
                 (63.0, 80.0), (80.0, 100.0), (100.0, 125.0), (125.0, 160.0), (160.0, 200.0)),
        "third_octave_bass": (
            (17.817974362806787, 22.44924096618746), (22.27246795350848, 28.061551207734325),
            (28.063309621420686, 35.35755452174525), (35.635948725613574, 44.89848193237492),
            (44.54493590701696, 56.12310241546865), (56.12661924284137, 70.7151090434905),
            (71.27189745122715, 89.79696386474984), (89.08987181403393, 112.2462048309373),
            (111.36233976754241, 140.30775603867164), (142.5437949024543, 179.59392772949968),
            (178.17974362806785, 224.4924096618746),
        ),
        "octave": (
            (22.273863607376246, 44.5477272147525), (44.54772721475249, 89.095454429505),
            (88.38834764831843, 176.7766952966369), (176.77669529663686, 353.5533905932738),
            (353.5533905932737, 707.1067811865476), (707.1067811865474, 1414.213562373095),
            (1414.2135623730949, 2828.42712474619), (2828.4271247461897, 5656.85424949238),
            (5656.8542494923795, 11313.70849898476), (11313.708498984759, 22627.41699796952),
        ),
        "near_field": ((20.0, 35.0), (35.0, 50.0), (50.0, 100.0), (100.0, 200.0),
                       (200.0, 400.0), (400.0, 800.0), (800.0, 2000.0)),
        "room": (60.0, 120.0),
        "speaker_spec": ((250.0, 2000.0), (2000.0, 8000.0), (8000.0, 16000.0)),
        "snr": ((20.0, 80.0), (80.0, 160.0), (160.0, 350.0), (350.0, 1000.0)),
        "crossover_snr": ((20.0, 80.0), (80.0, 160.0), (160.0, 350.0), (350.0, 1000.0),
                          (1000.0, 4000.0), (4000.0, 12000.0)),
    }


@pytest.mark.parametrize("builder,ladder,rows_key,edge_keys", [
    ("rear_upper", "rear_upper", "upper_bands", ("band_hz",)),
    ("rear_level", "rear_level", "bands", ("band_hz",)),
    ("rear_pair", "third_octave_bass", "bands", ("band_hz",)),
    ("bass_take", "bass", "bands", ("band_hz",)),
    ("bass_level", "bass", "realized_boost_db", ("band_hz",)),
    ("room_grade", "room", "bands", ("lo_hz", "hi_hz")),
    ("rear_preview", "rear_level", "bands", ("band_hz",)),
    ("bass_comparison", "bass", "bands", ("band_hz",)),
    ("flat_spec", "speaker_spec", "bands", ("f_lo_hz", "f_hi_hz")),
    ("log_pooled", "speaker_spec", "bands", ("f_lo_hz", "f_hi_hz")),
    ("directivity", "speaker_spec", "bands", ("f_lo_hz", "f_hi_hz")),
    ("gate_sweep", "speaker_spec", "bands", ("band_hz",)),
    ("close_reference", "speaker_spec", "bands", ("nominal_band_hz",)),
    ("ambient", "snr", "bands", ("band_hz",)),
    ("ambient", "crossover_snr", "bands", ("band_hz",)),
    ("snr_verdict", "snr", "bands", ("band_hz",)),
    ("snr_verdict", "crossover_snr", "bands", ("band_hz",)),
    ("speaker_envelope", "octave", "bands", ("band_hz",)),
    ("replay", "bass", "bands", ("band_hz",)),
    ("bass_replay", "bass", "bands", ("band_hz",)),
    ("gate_sweep_cli", "speaker_spec", "bands", ("band_hz",)),
])
def test_band_payloads_name_the_registry_edges(builder, ladder, rows_key, edge_keys, request, tmp_path, capsys):
    expected = BAND_LADDERS[ladder]
    if builder in ("rear_upper", "rear_level"):
        grid = np.geomspace(20, 5000, 600)
        zero = np.zeros_like(grid)
        take = SeatTake("take", "pose", grid, zero, False, (20, 5000))
        payload = rear_views._position_rows(
            {"pose": [take]}, {"pose": (grid, zero)}, {}, {"pose": zero},
            band_hz=(90, 250), coverage_hz=(20, 5000), handover_hz=None,
            swept_hz=(20, 5000), bearing={"pose"} if builder == "rear_upper" else set(),
        )["pose"]
    elif builder in ("rear_pair", "rear_preview"):
        result = rear_views._pair_position([{"curves": _pair_curves()}], {}, ceiling_hz=500)
        assert result is not None
        payload, _ = result
        if builder == "rear_preview":
            takes = rear_views.pair_takes([{"branch_diagnostic": _branch_diagnostic()}])
            payload = rear_preview._position(takes, payload, diagnostic_seed(48000), 0)
        else:
            low, high = payload["coverage_hz"]
            expected = tuple((lo, hi) for lo, hi in expected if lo >= low and hi <= high)
    elif builder == "bass_take":
        bundle, calibration, _, bank = request.getfixturevalue("summed_capture_bundle")
        asyncio.run(bank("baseline"))
        payload, = bass_view(bundle, take_ids=("baseline",), calibration_root=calibration)["takes"]
        # This 1.5 s sweep has no FFT bin in the 50–63 or 63–80 Hz dwells.
        expected = tuple(expected[index] for index in (0, 1, 2, 5, 6, 7, 8))
    elif builder == "bass_level":
        aligned = fit_bass_shape([request.getfixturevalue("pair")], candidate_id="boost")
        payload = bass_level_evidence(aligned, descriptor=None, prescribed_boost_db=None)
    elif builder == "bass_comparison":
        payload = compare_bass_takes(*request.getfixturevalue("pair"), change="candidate")
    elif builder == "speaker_envelope":
        payload = _envelope_answer(EnvelopeCurve(
            "woofer", DEFAULT_ENVELOPE_GRID_HZ, np.ones_like(DEFAULT_ENVELOPE_GRID_HZ),
            (), {}, None, 1, "reference", "unknown"))
        low, high = DEFAULT_ENVELOPE_GRID_HZ[[0, -1]]
        expected = tuple((max(lo, low), min(hi, high)) for lo, hi in expected if lo < high and hi > low)
    elif builder == "room_grade":
        median = read_room_median(room_median_document())
        payload = grade_room_median(median).to_dict()
        bounds = (float(median.freqs_hz[0]), *expected, median.ceiling_hz)
        expected = tuple(zip(bounds, bounds[1:]))
    elif builder in ("flat_spec", "log_pooled", "directivity"):
        grid = np.geomspace(250, 20000, 600)
        zero = np.zeros_like(grid)
        report = evaluate_flat_spec(grid, zero, np.zeros(grid.shape, dtype=bool))
        if builder == "flat_spec":
            payload = report.to_dict()
        elif builder == "log_pooled":
            payload = log_pooled_residual(report).to_dict()
        else:
            payload = directivity_table(report, (_position("axis", "axis", grid, zero),),
                                        reference_role="axis").rows[0].to_dict()
    elif builder == "gate_sweep":
        payload = request.getfixturevalue("direct_only_report")
    elif builder == "gate_sweep_cli":
        root = request.getfixturevalue("gate_sweep_round")
        assert main(["sweep", "--scope", "round", str(root), "--rungs-ms", "5", "20"]) == 0
        payload = json.loads(capsys.readouterr().out)
    elif builder == "close_reference":
        out = tmp_path / "close.json"
        assert main(_compare_argv(request.getfixturevalue("rounds"), out)) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["ladder"] == ladder
        windows = json.loads(out.read_text())["close_reference"]["windows"]
        assert all(window["ladder"] == ladder for window in windows)
        payload = windows[0]
    elif builder in ("replay", "bass_replay"):
        manifests = {}
        for stage in ("baseline", "full_boost", "volume_taper", "delivered"):
            directory = tmp_path if stage == "delivered" else tmp_path / stage
            directory.mkdir(exist_ok=True)
            raw = directory / "output.f64le"
            np.zeros(48000, dtype="<f8").tofile(raw)
            manifests[stage] = {
                "schema": "jts_dsp_replay/1", "render": {"output_sha256": sha256_file(raw)},
                "sample_rate_hz": 48000, "channels": 1, "graph_sha256": stage,
                "stimulus_sha256": "stimulus", "main_db": -20, "bass_reference_db": -20,
            }
        manifest = manifests.pop("delivered")
        manifest["bass_attribution"] = {"stages": manifests, "channels": [0], "descriptor": {}, "scope": "output"}
        read = replay_levels if builder == "replay" else bass_replay_levels
        payload, = read(manifest, raw, (0, 1))["channels"]
    else:
        table = SNR_BANDS_HZ if ladder == "snr" else CROSSOVER_SNR_BANDS_HZ
        payload = framed_ambient_band_report(np.zeros(48000), 48000, table, percentile=95)
        if builder == "snr_verdict":
            payload = band_snr_verdicts(decision_class="magnitude", capture_bands=payload["bands"],
                                       noise_bands=None, noise_floor_dbfs_scalar=None,
                                       relevant_hz=(20, 12000), model=DRIVER)
    assert payload["ladder"] == ladder
    edges = tuple(tuple(row[edge_keys[0]]) if len(edge_keys) == 1 else tuple(row[key] for key in edge_keys)
                  for row in payload[rows_key])
    assert edges == expected
