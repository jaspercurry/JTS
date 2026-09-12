# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
from dataclasses import asdict, replace

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import radiating_band_hz, sections_by_role
from jasper.active_speaker.branch_target import branch_target
from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db, decide_trim
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.round_views import _response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands, analysis_curve_records
from jasper.active_speaker.linearization_envelope import compose_envelope
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, core_level_band_hz, fit_driver_linearization, measurement_hole_bands_hz,
)
from jasper.active_speaker.profile import CrossoverRegion
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.program_analysis import (
    AlignmentEstimate, CrossoverCandidate, DriverResponse, ProgramAnalysis, RealizedLevelMatch,
)
from jasper.cli import round_views
from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import manifest_set, write_manifest


@pytest.fixture
def speaker_round(tmp_path):
    root = bank_measure_round(tmp_path)
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    row, record = next((row, dict(doc)) for row, doc in measurement_documents(inputs.session_dir) if row.phase == "measure")
    program = build_measure_program(
        {"woofer": -20.0, "tweeter": -24.0},
        [RoleBand("woofer", 0, FrequencyBand(150, 4000)), RoleBand("tweeter", 1, FrequencyBand(1600, 20000))],
    )
    grid = np.geomspace(100, 22000, 512)
    responses = []
    for role, center in (("woofer", 700), ("tweeter", 5000)):
        db = 7 * np.exp(-0.5 * (np.log2(grid / center) / 0.18) ** 2)
        response = DriverResponse(role=role, freqs_hz=grid, magnitude_db=db,
                                  complex_tf=10 ** (db / 20) + 0j, gating={}, snr=None, validity_floor_hz=180)
        responses.append(replace(response, repeat_responses=(response, response)))
    analysis = ProgramAnalysis(
        phase="measure", program_id=program.program_id, locations=(), driver_responses=tuple(responses),
        alignment=AlignmentEstimate(delay_us=157.5, raw_delay_us=162, parallax_us=4.5,
                                    polarity="inverted", polarity_sign=-1, confidence=0.9, seed_delay_us=120),
        candidate=CrossoverCandidate(trim_db={"woofer": 0, "tweeter": -3}, polarity="inverted",
                                     delay_us=157.5, predicted_ripple_db=1.25, confidence=0.9,
                                     seed_polarity_sign=1, alignment_seed_ripple_db=3.5,
                                     alignment_objective="flat_sum_committed", flatness_improvement_db=2.25,
                                     anchor_delay_us=150, snap_delta_us=7.5),
    )
    match = RealizedLevelMatch(0, 0.5, 0.5, 3, True, (800, 1600), (1600, 3200))
    trim = decide_trim(anchored_db={"woofer": 0, "tweeter": -3}, resolved_db={"woofer": 0, "tweeter": -13},
                       tweeter_role="tweeter", anchored_match=match, resolved_match=match, ripple_db=1.25)
    record.update(program=program.to_dict(), program_id=program.program_id,
                  curves=analysis_curve_records(analysis, program),
                  capture_setup={"calibration": {"model": "minidsp_umik2", "calibration_id": "mic-1"}},
                  capture_calibration={"applied": True, "calibration_id": "mic-1", "curve_fingerprint": "curve-1"})
    record_path = inputs.session_dir / "evidence/v1/artifacts" / row.path
    record_path.write_text(json.dumps(record))
    classes = {"woofer": "unknown", "tweeter": "soft_dome"}
    (root / "design-draft.json").write_text(json.dumps({"manual_settings": {
        "drivers": [{"role": role, "driver_class": cls} for role, cls in classes.items()],
    }}))
    region = {"id": "pair", "lower_driver": "woofer", "upper_driver": "tweeter", "fc_hz": 2400, "order": 4}
    (directory / "candidate.json").write_text(json.dumps({
        "analysis": analysis_json(analysis, trim), "source_preset": {"crossover_regions": [region]},
    }))
    group = manifest_set([(row.path, record)], set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])
    return root, record, program, classes, region, trim


@pytest.mark.parametrize("vocabulary", [None, "cut_only", "bounded_boost"])
def test_speaker_fit_matches_explicit_math_and_banked_decisions(speaker_round, vocabulary, capsys):
    root, record, program, classes, region, trim = speaker_round
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    flags = [] if vocabulary is None else ["--vocabulary", vocabulary]
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"linearization", "alignment", "trim"} <= result.keys()
    assert result["vocabulary"] == (vocabulary or "bounded_boost")
    bands = _primary_sweep_bands(program)
    responses = {curve["role"]: _response_from_banked_curve(curve)[0] for curve in record["curves"]}
    sections = sections_by_role([CrossoverRegion.from_mapping(region)])
    envelopes = {role: compose_envelope(
        role, response, excited_band_hz=bands[role], mic_tier="reference", driver_class=classes[role],
        sigma_db=compose_sigma_db(response, responses[next(r for r in responses if r != role)],
                                  tier="reference", valid_band_hz=bands[role]),
    ) for role, response in responses.items()}
    radiating = {role: radiating_band_hz(sections[role]) for role in responses}
    blind = measurement_hole_bands_hz([
        core_level_band_hz(envelopes[role], radiating_band_hz=radiating[role]) for role in responses
    ])
    for role, response in responses.items():
        fit = fit_driver_linearization(response, envelopes[role],
                                       vocabulary=FitVocabulary(allow_boost=vocabulary != "cut_only"),
                                       radiating_band_hz=radiating[role], blind_bands_hz=blind,
                                       target=branch_target(sections[role], envelopes[role].freqs_hz))
        assert result["linearization"][role]["fit"] == json.loads(json.dumps(fit.to_dict()))
        assert result["linearization"][role]["excited_band_hz"] == list(bands[role])
        assert result["linearization"][role]["envelope"]["sigma_source"] == "paired_repeats"
    alignment = result["alignment"]
    assert alignment["drift_us"] == alignment["committed"]["delay_us"] - alignment["seed"]["delay_us"]
    assert alignment["committed"] == {"delay_us": 157.5, "polarity": "inverted", "ripple_db": 1.25}
    assert alignment["seed"] == {"delay_us": 120, "polarity": "normal", "ripple_db": 3.5}
    assert result["trim"] == json.loads(json.dumps({**asdict(trim), "outcome": trim.outcome, "committed_side": trim.committed_side}))
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_unknown_set_uses_registry_refusal(speaker_round, capsys):
    root, *_ = speaker_round
    assert round_views.main(["speaker-fit", str(root), "--set", "unknown"]) == round_views.EXIT_REFUSED
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "round_set_unknown"
    assert result["reason"] in REASON_REGISTRY
    assert result["status"] == "refused"
    assert result["detail"]["set_id"] == "unknown"
