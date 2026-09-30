# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml
import pytest

from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.alignment_evidence import round_alignment
from jasper.active_speaker.crossover_envelope_v2 import _envelope
from jasper.active_speaker.timing_status import timing_status_lines
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.branch_chain import radiating_band_hz
from jasper.active_speaker.crossover_section import sections_by_role
from jasper.active_speaker.branch_target import branch_target
from jasper.active_speaker.crossover_v2.intervention import CloudFitTerms, compose_sigma_db, fit_branches
from jasper.active_speaker.crossover_v2.driver_prescription import _check_composed
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY, exception_detail
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundViewsError, prescription_sources, read_run_manifest, round_artifact_dir, round_inputs, with_records,
)
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands, analysis_curve_records
from jasper.active_speaker.linearization_envelope import compose_envelope
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, LinearizationFilter, complex_correction_response, core_level_band_hz, fit_driver_linearization, measurement_hole_bands_hz,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset, CrossoverRegion, required_driver_roles
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED
from jasper.audio_measurement.gating import FLOOR_SEARCH_BOUND, f_trusted_floor_hz
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.timing_verification import TIMING_RESIDUAL_FLOOR_DB, timing_verification
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK, ALIGNMENT_ESTIMATED_FLAT_SUM,
    AlignmentEstimate, CrossoverCandidate, DriverResponse, ProgramAnalysis,
)
from jasper.cli import crossover_prescriber, round_views
from tests.crossover_v2_banked_round import bank_executor_take, bank_measure_round
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME
from jasper.active_speaker.crossover_v2.evidence_packet import EVIDENCE_KEY
from jasper.active_speaker.round_packet import _fits, write_round_packet
from jasper.active_speaker.speaker_fit import _fit_vocabularies, design_clouds, fit_feature_curves, speaker_fit
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, compile_candidate_config
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_rear_output_foundation import _rear_document, _rear_pair
from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker import candidate_parts
from jasper.active_speaker.candidate_parts import baseline_candidate_id, candidate_from_design_draft
from tests.active_speaker_fixtures import mono_output_topology, standard_design_draft
from tests.run_manifest_fixture import manifest_set, own_record, write_manifest
from tests.crossover_v2_fixtures import _fixture_applied_profile, _one_way_preset


def _bank_candidate(directory: Path, analysis: dict, preset: ActiveSpeakerPreset | None = None, *,
                    fc_hz: float = 2400) -> dict:
    """The round's own ``candidate.json``: a candidate that reopens, on ``preset`` or else on a
    two-way preset crossed at ``fc_hz``."""
    if preset is None:
        two_way = _two_way_preset()
        two_way["crossover_regions"][0]["fc_hz"] = fc_hz
        preset = ActiveSpeakerPreset.from_mapping(two_way)
    trims = dict.fromkeys(required_driver_roles(preset.way_count), 0.0)
    candidate = replace(_candidate(preset=preset, trims=trims), analysis=analysis).to_dict()
    (directory / "candidate.json").write_text(json.dumps(candidate))
    return candidate


def _joined(inputs) -> dict:
    """The round's run manifest as speaker-fit reads it: each kept take with its record."""
    return with_records(inputs.session_dir, read_run_manifest(inputs))


@pytest.fixture
def speaker_round(tmp_path):
    root = bank_measure_round(tmp_path)
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    state = json.loads(inputs.state_path.read_text())
    state.update(session_phases=["check", "measure", "verify"])
    inputs.state_path.write_text(json.dumps(state))
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
                                  complex_tf=10 ** (db / 20) + 0j, gating={"window_ms": 50.0}, snr=None,
                                  validity_floor_hz=180)
        responses.append(replace(response, repeat_responses=(response, response)))
    analysis = ProgramAnalysis(
        phase="measure", stimulus_id=program.stimulus_id, locations=(), driver_responses=tuple(responses),
        alignment=AlignmentEstimate(delay_us=157.5, raw_delay_us=162, parallax_us=4.5,
                                    polarity="inverted", polarity_sign=-1, confidence=0.9, seed_delay_us=-205, polarity_agrees_with_sum=False),
        candidate=CrossoverCandidate(trim_db={"woofer": 0, "tweeter": -3}, polarity="inverted",
                                     delay_us=157.5, predicted_ripple_db=1.25, confidence=0.9,
                                     seed_polarity_sign=1, alignment_seed_ripple_db=3.5, alignment_seed_delay_us=120,
                                     alignment_objective="flat_sum_estimate", flatness_improvement_db=2.25,
                                     anchor_delay_us=150, snap_delta_us=7.5),
    )
    record.update(program=program.to_dict(), stimulus_id=program.stimulus_id,
                  curves=analysis_curve_records(analysis, program), analysis=analysis_json(analysis),
                  capture_setup={"calibration": {"model": "minidsp_umik2", "calibration_id": "mic-1"}},
                  capture_calibration={"applied": True, "calibration_id": "mic-1", "curve_fingerprint": "curve-1"})
    record_path = take_artifact_path(inputs.session_dir, row.path)
    record_path.write_text(json.dumps(record))
    classes = {"woofer": "unknown", "tweeter": "soft_dome"}
    (root / "design-draft.json").write_text(json.dumps({"topology": mono_output_topology().to_dict(), "manual_settings": {
        "drivers": [{"role": role, "target_id": f"mono:{role}", "driver_class": cls} for role, cls in classes.items()],
    }}))
    region, = _bank_candidate(directory, analysis_json(analysis))["source_preset"]["crossover_regions"]
    group = manifest_set([(row.path, record)], set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])
    return root, record, program, classes, region


@pytest.mark.parametrize("cloud_planned,post_apply_verifies", [(False, True), (True, True), (False, False)])
def test_speaker_fit_matches_explicit_math_and_banked_decisions(
    speaker_round, cloud_planned, post_apply_verifies, capsys,
):
    root, record, program, classes, region = speaker_round
    inputs = round_inputs(root)
    state = json.loads(inputs.state_path.read_text())
    if not post_apply_verifies:
        state["session_phases"].remove("verify")
    if cloud_planned:
        state["session_phases"].insert(1, "cloud_measure")
    inputs.state_path.write_text(json.dumps(state))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"linearization", "alignment", "trim_decision"} <= result.keys()
    assert result["boost_evidence"]["design_poses"] == 1
    bands = _primary_sweep_bands(program)
    responses = {curve["role"]: response_from_banked_curve(curve)[0] for curve in record["curves"]}
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
                                       vocabulary=FitVocabulary(allow_boost=True, per_filter_boost_cap_db=40),
                                       radiating_band_hz=radiating[role], blind_bands_hz=blind,
                                       target=branch_target(sections[role], envelopes[role].freqs_hz))
        assert result["linearization"][role]["fit"] == json.loads(json.dumps(fit.to_dict()))
        assert result["linearization"][role]["excited_band_hz"] == list(bands[role])
        assert result["linearization"][role]["envelope"]["sigma_source"] == "paired_repeats"
    alignment = result["alignment"]
    assert alignment["refinement_delta_us"] == alignment["committed"]["delay_us"] - alignment["seed"]["delay_us"]
    assert alignment["committed"] == {"delay_us": 157.5, "polarity": "inverted"}
    assert alignment["seed"] == {"delay_us": 120, "polarity": "normal"}
    assert alignment["polarity_agrees_with_sum"] is False
    assert alignment["gcc_delay_us"] == -205
    assert set(result["trim_decision"]["committed_db"]) == set(bands)
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            assert len(value) <= 16
            pending.extend(value)
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("gain_db", [-6.0, float("nan")])
@pytest.mark.parametrize("fc_hz", [2400, 400])
def test_speaker_fit_discloses_handover_level_shift(speaker_round, monkeypatch, fc_hz, gain_db):
    root, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    manifest = _joined(inputs)
    _bank_candidate(directory, json.loads((directory / "candidate.json").read_text())["analysis"], fc_hz=fc_hz)
    original = fit_branches

    def one_wide_cut(*args, **kwargs):
        branches = original(*args, **kwargs)
        fit = branches.fits["woofer"]
        return replace(branches, fits={
            **branches.fits,
            "woofer": replace(fit, filters=(LinearizationFilter("Peaking", fc_hz, 0.1, gain_db),)),
        })

    monkeypatch.setattr("jasper.active_speaker.speaker_fit.fit_branches", one_wide_cut)
    result = speaker_fit(inputs, manifest, "speaker-set")
    woofer = result["linearization"]["woofer"]
    if np.isnan(gain_db):
        assert (woofer["fit"]["reason_summary"], woofer["handover_level_shift_db"]) == (
            {"unavailable": "fit_not_finite"}, None)
        json.dumps(result["linearization"], allow_nan=False)
        return
    assert woofer["handover_level_shift_db"] == pytest.approx(-6, abs=0.5)
    if fc_hz == 400:
        assert result["trim_decision"] == {"status": "unavailable", "reason": "handover_band_unmeasured"}


@pytest.mark.parametrize("cut_db", [0, 3, 6, 12])
def test_fit_resolves_trim_after_tweeter_cut(speaker_round, monkeypatch, cut_db):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    path = take_artifact_path(inputs.session_dir, read_run_manifest(inputs)["sets"][0]["takes"][0]["record_id"])
    for curve in record["curves"]:
        response = response_from_banked_curve(curve)[0]
        level = 10 if curve["role"] == "tweeter" else 0
        curve.update(magnitude_db=np.full_like(response.freqs_hz, level).tolist())
    raw_trim = {"woofer": 0, "tweeter": -10}
    banked_trim = {"woofer": min(0, 10 - cut_db), "tweeter": min(0, -10 + cut_db)}
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    _bank_candidate(directory, {**analysis, "trim_db": banked_trim})
    original = fit_branches

    def controlled_fit(drivers, **kwargs):
        branches = original(drivers, **kwargs)
        return replace(branches, fits={role: replace(fit, filters=(
            (LinearizationFilter("Peaking", 2400, 0.01, -cut_db),)
            if role == "tweeter" and cut_db else ())) for role, fit in branches.fits.items()})

    path.write_text(json.dumps(record))
    manifest = _joined(inputs)
    manifest["sets"][0]["capture_basis"].update(role="tweeter")
    monkeypatch.setattr("jasper.active_speaker.speaker_fit.fit_branches", controlled_fit)
    rows = _fits(inputs, manifest, prescription_sources(inputs), {})
    assert rows
    trims = rows[0]["resolved_trim_db"]
    assert trims["tweeter"] - trims["woofer"] == pytest.approx(raw_trim["tweeter"] + cut_db, abs=0.3)
    assert max(trims.values()) == 0
    if cut_db == 0:
        assert trims == pytest.approx(raw_trim)
    assert "prescriptions" not in rows[0]


@pytest.mark.parametrize("changes", [
    {"poses": 1}, {"poses": 2}, {}, {"role": "woofer"}, {"role": "main"},
    {"verifies": False}, {"verifies": False, "cloud_planned": False}, {"floor": 100.0}, {"floor": 8000.0},
    {"horn_positions": 3}, {"horn_positions": 1}, {"disagree": True}, {"stimulus": "reference_axis"}, {"basis_role": "summed"},
])
def test_design_cloud_discloses_evidence_for_each_roles_fit(speaker_round, capsys, changes):
    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    role, poses = changes.get("role", "tweeter"), changes.get("poses", 3)
    state = json.loads(inputs.state_path.read_text())
    state["session_phases"] = (["check", "measure"] + (["cloud_measure"] if changes.get("cloud_planned", True) else [])
                               + (["verify"] if changes.get("verifies", True) else []))
    inputs.state_path.write_text(json.dumps(state))
    directory, _ = round_artifact_dir(inputs.session_dir)
    candidate = json.loads((directory / "candidate.json").read_text())
    if role == "main":
        program = build_measure_program({role: -24.0}, [RoleBand(role, 0, FrequencyBand(150, 20000))])
        record.update(program=program.to_dict(), stimulus_id=program.stimulus_id)
        candidate = _bank_candidate(directory, {**candidate["analysis"], "stimulus_id": program.stimulus_id},
                                    _one_way_preset())
    if changes.get("horn_positions"):
        (root / "design-draft.json").write_text(json.dumps({"topology": mono_output_topology().to_dict(), "manual_settings": {
            "drivers": [{"role": role, "target_id": f"mono:{role}", "driver_class": "compression_horn"}],
        }}))
    response = replace(response_from_banked_curve(record["curves"][1])[0], role=role, repeat_responses=())
    rows = []
    for index, (deg, kind, phase) in enumerate([
        (0, "bearing", "measure"), (-20, "bearing", "measure"), (20, "bearing", "measure"),
        (0, "bearing", "measure"), (40, "seat", "measure"), (60, "bearing", "verify"),
        (80, "bearing", "measure"),
    ]):
        depth = -7 if changes.get("disagree") and deg else changes.get("depth", 7)
        db = -depth * np.exp(-0.5 * (np.log2(response.freqs_hz / 6000) / 0.25) ** 2)
        if changes.get("horn_positions"):
            db = -5 * np.minimum(
                np.clip(np.log2(response.freqs_hz / 10000) / np.log2(1.2), 0, 1),
                np.clip(np.log2(20000 / response.freqs_hz) / np.log2(1.25), 0, 1),
            )
            if changes["horn_positions"] == 1 and deg:
                db = np.zeros_like(db)
        measured = replace(response, magnitude_db=db, complex_tf=10 ** (db / 20) + 0j,
                           gating={**response.gating, "window_ms": 50.0})
        analysis = ProgramAnalysis(phase="measure", stimulus_id=program.stimulus_id, locations=(),
                                  driver_responses=(replace(measured, repeat_responses=(measured, measured)),))
        curves = [c for c in record["curves"] if c["role"] != role and role != "main"] + analysis_curve_records(analysis, program)
        take ={**record, "curves": curves, "take_id": f"design-{index}", "position_deg": deg, "pose_kind": kind, "phase": phase}
        path = directory / "positions" / f"{take['take_id']}.json"
        path.write_text(json.dumps(take))
        rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), take))
    group = manifest_set(rows, set_id=role, selected={r["take_id"] for _, r in rows[:poses] + rows[3:6]})
    group["capture_basis"].update(role=changes.get("basis_role", role), stimulus=changes.get("stimulus"))
    write_manifest(root, groups=[group])
    manifest = _joined(inputs)
    flags = ["--boost-floor-hz", str(changes["floor"])] if "floor" in changes else []
    assert round_views.main(["speaker-fit", str(root), "--set", role, "--take", "design-0", *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    proposal, fit = result["linearization"][role], result["linearization"][role]["fit"]
    assert result["boost_evidence"] == proposal["boost_evidence"]
    assert proposal["boost_evidence"]["design_poses"] == (0 if changes.get("basis_role") else poses)
    floor = changes.get("floor")
    assert fit["budget"]["boost_floor_hz"] == floor
    assert proposal["per_filter_boost_cap_db"] == proposal["composed_boost_cap_db"] == 40.0
    boosts = [f for f in fit["filters"] if f["gain"] > 0]
    assert all(f["freq"] >= (floor or 0) and f["gain"] <= fit["budget"]["max_gain_db"] for f in boosts)
    if role != "woofer" and floor != 8000:
        assert boosts
    peak, _ = _check_composed(tuple({"role": role, **f} for f in fit["filters"]), {role: (1600, 20000)})
    assert peak <= 39.0 + 1e-9
    if changes.get("disagree"):
        assert max(v for v in fit["position_spread_db"].values() if v is not None) > 1
    if changes.get("horn_positions"):
        correction = complex_correction_response([LinearizationFilter(**f) for f in fit["filters"]], np.array([12000, 16000]))
        assert np.all(20 * np.log10(np.abs(correction)) > 0)
        assert fit["class_prior_hz"] == {"full_to_hz": 10000, "taper_zero_hz": 20000}
        spread = fit["position_spread_db"]["16000"]
        assert spread == pytest.approx(0) if changes["horn_positions"] == 3 else spread > 1
        packet_fit = next(f for f in _fits(inputs, manifest, prescription_sources(inputs), design_clouds(manifest))
                          if f["role"] == role and f["take_id"] == "design-0")
        assert packet_fit["position_spread_db"] == fit["position_spread_db"]
        assert packet_fit["class_prior_hz"] == fit["class_prior_hz"]
    if changes.get("basis_role") or poses < 2:
        assert fit["position_spread_db"] is None


@pytest.mark.parametrize("horizontal,vertical,expected", [([-20, 20], [-10, 10], 5),
    ([-40, -30, -20, -10, 10, 20, 30, 40], [-20, -10, 10, 20], 13)])
def test_baseline_design_poses_keep_both_angles_and_the_on_axis_take(speaker_round, horizontal, vertical, expected):
    root, record, *_ = speaker_round
    curve = record["curves"][0]
    poses = [(0, 0)] * 4 + [(h, 0) for h in horizontal] + [(0, v) for v in vertical]
    takes = [{"take_id": f"baseline-{i}", "selected": True, "phase": "measure",
              "pose": {"kind": "bearing", "deg": h, "elevation_deg": v}, "captured_at": f"2026-09-29T12:00:{i:02d}Z",
              "curves": [{**curve, "magnitude_db": (np.asarray(curve["magnitude_db"]) + i).tolist()}]}
             for i, (h, v) in enumerate(poses)]
    manifest = {"sets": [{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}]}
    cloud = design_clouds(manifest)["woofer"]
    assert cloud.n_positions == len(cloud.boost_responses) == expected
    np.testing.assert_allclose(cloud.boost_responses[0].magnitude_db, takes[3]["curves"][0]["magnitude_db"])
    for response, take in zip(cloud.boost_responses[1:], takes[4:]):
        np.testing.assert_allclose(response.magnitude_db, take["curves"][0]["magnitude_db"])


@pytest.mark.parametrize("marks,pairs,spread", [(2, 1, 1), (4, 6, 3)])
def test_current_round_packet_uses_mark_pairs(speaker_round, marks, pairs, spread):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    rows = []
    for i in range(marks):
        curves = [{**curve, "magnitude_db": (np.asarray(curve["magnitude_db"]) + i).tolist()} for curve in record["curves"]]
        capture = {**record, "take_id": f"mark-{i}", "position_deg": 0, "vertical_deg": 0, "curves": curves}
        path = directory / "positions" / f"mark-{i}.json"
        path.write_text(json.dumps(capture))
        rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), capture))
    group = manifest_set(rows, set_id="woofer")
    group["capture_basis"]["role"] = "woofer"
    for take in group["takes"]:
        take.update(pose_index=0)
    write_manifest(root, program="speaker/mark", groups=[group])
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    assert len(packet["fits"]) == marks
    for fit in packet["fits"]:
        assert fit["fit_band_hz"][0] < fit["fit_band_hz"][1]
        assert fit["verdict"]["repeat_spread_db"] == pytest.approx(spread)
        assert fit["verdict"]["n_pairs"] == pairs
        assert fit["verdict"]["repeat_basis"] == "mark_pairs_max_rms"
    json.dumps(packet, allow_nan=False)


@pytest.mark.parametrize("trusted_floor_hz", [357.0, None])
def test_speaker_fit_respects_banked_trusted_floor(speaker_round, capsys, trusted_floor_hz):
    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    grid = np.geomspace(60, 4000, 1024)
    db = (-6 * np.exp(-0.5 * (np.log2(grid / 300) / 0.12) ** 2)
          + 6 * np.exp(-0.5 * (np.log2(grid / 900) / 0.18) ** 2))
    response = DriverResponse(
        role="woofer", freqs_hz=grid, magnitude_db=db, complex_tf=10 ** (db / 20) + 0j,
        gating={"f_trusted_hz": trusted_floor_hz, "window_ms": 50.0}, snr=None, validity_floor_hz=143,
    )
    analysis = ProgramAnalysis(phase="measure", stimulus_id=program.stimulus_id, locations=(),
                              driver_responses=(replace(response, repeat_responses=(response, response)),))
    curves = analysis_curve_records(analysis, program) + [record["curves"][1]]
    if trusted_floor_hz is None:
        curves[0].pop("trusted_floor_hz")
    restored = response_from_banked_curve(curves[0])[0]
    assert all(r.gating == ({} if trusted_floor_hz is None else {"f_trusted_hz": trusted_floor_hz})
               for r in (restored, *restored.repeat_responses))
    assert restored.fit_floor_hz == (trusted_floor_hz or 143)
    assert replace(restored, validity_floor_hz=None).fit_floor_hz == trusted_floor_hz
    sparse = replace(restored, freqs_hz=np.array([100., 150., 151.]), magnitude_db=np.zeros(3),
                     complex_tf=np.ones(3, dtype=complex))
    assert len(fit_feature_curves(CloudFitTerms(boost_responses=(sparse, restored)))) == 1
    rows = []
    for deg in (-20, 0, 20):
        take = {**record, "curves": curves, "take_id": f"floor-{deg}", "position_deg": deg}
        path = directory / "positions" / f"{take['take_id']}.json"
        path.write_text(json.dumps(take))
        rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), take))
    group = manifest_set(rows, set_id="speaker-set")
    group["capture_basis"]["role"] = "woofer"
    write_manifest(root, groups=[group])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", "--take", "floor-0"]) == 0
    proposal = json.loads(capsys.readouterr().out)["linearization"]["woofer"]
    fit = proposal["fit"]
    assert all(f["freq"] >= (trusted_floor_hz or 150) for f in fit["filters"])
    assert fit["reason_summary"]["250"] == ("envelope_out_of_band" if trusted_floor_hz else "envelope_fitted")
    assert any(800 < f["freq"] < 1000 and f["gain"] < -1 for f in fit["filters"])
    assert fit["fit_band_hz"][0] >= (trusted_floor_hz or 150)
    assert (250 in [b["center_hz"] for b in proposal["boost_evidence"]["band_spread"]]) == (trusted_floor_hz is None)
    if trusted_floor_hz:
        assert fit["residual_rms_db"] < 1
        assert fit["residual_max_db"] < 3


@pytest.mark.parametrize("run_program", ["speaker", "speaker/mark"])
def test_speaker_fit_reads_the_run_purpose_behind_a_sized_program(speaker_round, capsys, run_program):
    root, record, program, *_ = speaker_round
    group = manifest_set([(next(row.path for row, _ in measurement_documents(round_inputs(root).session_dir)
                               if row.phase == "measure"), record)], set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, program=run_program, groups=[group])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    assert json.loads(capsys.readouterr().out)["set_id"] == "speaker-set"


@pytest.mark.parametrize("trial", [False, True])
def test_speaker_fit_reads_the_rounds_candidate_or_applied_profile(speaker_round, capsys, monkeypatch, trial):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    stored = json.loads((directory / "candidate.json").read_text())
    if not trial:
        (directory / "candidate.json").unlink()
    draft_path = root / "design-draft.json"
    draft_path.write_text(json.dumps({key: value for key, value in json.loads(draft_path.read_text()).items()
                                      if key != "topology"}))
    (root / "applied-profile.json").write_text(json.dumps({
        "kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION, "status": "applied",
        "source": {"measured_candidate_fingerprint": "base-fp"},
    }))
    row_path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    measured = manifest_set([(row_path, record)], set_id="speaker-set")
    measured["capture_basis"].update(role="woofer", graph_scope="drivers")
    base = manifest_set([(row_path, record)], set_id="base-set")
    base["capture_basis"].update(graph_scope="timing", candidate_id="projected-timing-fp")
    base["takes"] = [own_record(base["takes"][0], record, phase="timing")]
    write_manifest(root, groups=[base, measured])
    looked_up = []

    def find(fingerprint):
        looked_up.append(fingerprint)
        return SimpleNamespace(candidate=MeasuredCrossoverCandidate.from_mapping(stored))

    monkeypatch.setattr(candidate_parts, "find_banked_candidate", find)
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    assert looked_up == ([] if trial else ["base-fp"])
    proposal = json.loads(capsys.readouterr().out)["linearization"]["woofer"]["fit"]
    inputs = round_inputs(root)
    fit, = _fits(inputs, _joined(inputs), prescription_sources(inputs), {})
    assert fit["filters"] == proposal["filters"] and fit["filters"]
    assert fit["residual_rms_db"] == proposal["residual_rms_db"] is not None


def test_packet_preserves_missing_round_base_error(speaker_round):
    root, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    (directory / "candidate.json").unlink()
    (root / "applied-profile.json").unlink(missing_ok=True)
    with pytest.raises(RoundViewsError) as exc:
        speaker_fit(inputs, _joined(inputs), "speaker-set")
    fit, = write_round_packet(root, str(directory / "run_manifest.json"), [])["fits"]
    assert fit["reason_summary"] == {"unavailable": exception_detail(exc.value)}
    assert fit["filters"] is fit["residual_rms_db"] is None


def test_unknown_set_uses_registry_refusal(speaker_round, capsys):
    root, *_ = speaker_round
    assert round_views.main(["speaker-fit", str(root), "--set", "unknown"]) == round_views.EXIT_REFUSED
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "round_set_unknown"
    assert result["reason"] in REASON_REGISTRY
    assert result["status"] == "refused"
    assert result["detail"]["set_id"] == "unknown"


@pytest.mark.parametrize("damaged,field", [
    ("take", "curves"), ("take", "woofer"), ("take", "validity_floor_hz"), ("take", "repeat_curves"),
    ("design_pose", "woofer"),
])
def test_a_take_banked_without_a_fit_input_refuses_by_that_field(speaker_round, capsys, damaged, field):
    """#2902: every banked take carries a curve for each role it swept, and every
    curve both fit inputs, so the fitted take or a design pose beside it without
    one refuses by that field and role; the run manifest's copy is never read."""
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    broken = json.loads(json.dumps(record))
    if field == "woofer":
        broken["curves"] = [curve for curve in broken["curves"] if curve["role"] != field]
    else:
        del (broken if field == "curves" else broken["curves"][0])[field]
    rows = [(row.path, broken if damaged == "take" else record)]
    if damaged == "design_pose":
        rows += [(str(Path(row.path).with_name(f"pose{deg}.json")), {**pose, "take_id": f"pose{deg}", "position_deg": deg})
                 for deg, pose in ((-20, record), (20, broken))]
    for path, take in rows:
        take_artifact_path(inputs.session_dir, path).write_text(json.dumps(take))
    group = manifest_set(rows, set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", "--take", record["take_id"]]) == (
        round_views.EXIT_REFUSED)
    refusal = json.loads(capsys.readouterr().out)
    detail = json.loads(refusal["detail"])
    assert (refusal["reason"], detail["field"], detail["role"]) == (
        TAKE_CURVES_NOT_BANKED, "curves" if field == "woofer" else field, "woofer")


def test_a_design_cloud_that_refuses_is_disclosed_and_its_takes_fit_nothing(speaker_round):
    """#5737 C1b: a design pose that banked no curve for its role never costs the
    round. The packet names the refusal by code for the set it serves, and that
    set's takes publish no fit, never one without its cloud."""
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    broken = {**record, "curves": [curve for curve in record["curves"] if curve["role"] != "woofer"]}
    rows = [(row.path, record)] + [(str(Path(row.path).with_name(f"pose{deg}.json")),
                                    {**pose, "take_id": f"pose{deg}", "position_deg": deg})
                                   for deg, pose in ((-20, record), (20, broken))]
    for path, take in rows:
        take_artifact_path(inputs.session_dir, path).write_text(json.dumps(take))
    group = manifest_set(rows, set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])
    directory, _ = round_artifact_dir(inputs.session_dir)

    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])

    refusal, = [entry for entry in packet["unavailable"] if entry["artifact"] == "design_clouds"]
    assert (refusal["set_id"], refusal["reason"], refusal["detail"]["take_id"], refusal["detail"]["role"]) == (
        "speaker-set", TAKE_CURVES_NOT_BANKED, "pose20", "woofer")
    assert len(packet["fits"]) == 3 and all(
        (fit["reason_summary"], fit["filters"]) == ({"unavailable": TAKE_CURVES_NOT_BANKED}, None) for fit in packet["fits"])


def test_a_named_take_the_round_did_not_keep_refuses_with_its_verdict(speaker_round, capsys):
    """#6067: a take the operator names that the run banked but did not keep
    refuses ``round_take_not_kept`` with its record's status, fault and next
    action, so the view joins every take of the set it names (ADR-0395)."""
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    refused = {**record, "take_id": "refused", "measurement_status": "captured",
               "verdict": {"ok": False, "fault": "level_off_target", "next": "retake_louder"}}
    path = str(Path(row.path).with_name("refused.json"))
    take_artifact_path(inputs.session_dir, path).write_text(json.dumps(refused))
    group = manifest_set([(row.path, record), (path, refused)], set_id="speaker-set", selected={record["take_id"]})
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])

    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", "--take", "refused"]) == (
        round_views.EXIT_REFUSED)
    refusal = json.loads(capsys.readouterr().out)
    assert (refusal["reason"], {key: refusal["detail"][key] for key in ("status", "fault", "next")}) == (
        "round_take_not_kept", {"status": "captured", "fault": "level_off_target", "next": "retake_louder"})


@pytest.mark.parametrize("applied", [False, True])
def test_mic_tier_uses_the_recorded_calibration(speaker_round, capsys, applied):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    record["capture_setup"]["calibration"]["model"] = "dayton_umm6"
    record["capture_calibration"]["applied"] = applied
    take_artifact_path(inputs.session_dir, row.path).write_text(json.dumps(record))
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {entry["fit"]["mic_tier"] for entry in result["linearization"].values()} == {"consumer" if applied else "phone"}


def test_speaker_fit_uses_executor_umik2_provenance(speaker_round, capsys, tmp_path, monkeypatch):
    root, record, program, *_ = speaker_round
    executor = bank_executor_take(tmp_path / "executor", monkeypatch, program=program)
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    for key in ("capture_setup", "capture_calibration", "gating_applied", "stimulus_dbfs", "mark_distance_m", "side"):
        record[key] = executor[key]
    take_artifact_path(inputs.session_dir, row.path).write_text(json.dumps(record))
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {entry["fit"]["mic_tier"] for entry in result["linearization"].values()} == {"reference"}


@pytest.mark.parametrize("damage,code", [
    pytest.param(lambda raw: json.dumps({**raw, "bass_extension": {
        "low_boost_db": 4.0, "reference_level_db": -10.0, "detector_lowpass_hz": 120.0, "compressor_threshold_dbfs": -12.0,
    }}), "bass_descriptor_malformed", id="adr-0352-bass"),
    pytest.param(lambda raw: json.dumps({**raw, "schema_version": 2}), "candidate_schema_unsupported", id="schema"),
    pytest.param(lambda raw: json.dumps({**raw, "source_preset": {**raw["source_preset"], "crossover_regions": [
        {**raw["source_preset"]["crossover_regions"][0], "fc_hz": -5.0}]}}), "candidate_malformed", id="region"),
    pytest.param(lambda raw: json.dumps(raw)[:-1], "candidate_malformed", id="unparseable"),
])
def test_a_base_that_does_not_reopen_refuses_by_its_code_and_the_packet_still_builds(
    speaker_round, capsys, damage, code,
):
    """#5909: a round whose banked candidate does not reopen (an ADR-0352 bass section, ADR-0381)
    is no base either judge charges; the bank still stores its packet."""
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    path = directory / "candidate.json"
    path.write_text(damage(json.loads(path.read_text())))
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    speaker = manifest_set([(row.path, record)], set_id="speaker-set")
    speaker["capture_basis"].update(role="woofer")
    speaker["takes"][0].update(role="woofer")
    write_manifest(root, groups=[speaker])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == round_views.EXIT_UNREADABLE
    fit = json.loads(capsys.readouterr().out)
    assert crossover_prescriber.main(["contract", "--round", str(root)]) == crossover_prescriber.EXIT_UNREADABLE
    contract = json.loads(capsys.readouterr().out)
    assert (fit["status"], fit["code"], contract["status"], contract["reason"]) == ("unreadable", code) * 2
    assert crossover_prescriber.main(["contract", "--round", str(root), "--section", "room"]) == crossover_prescriber.EXIT_OK
    assert "schema" in json.loads(capsys.readouterr().out)["sections"]["room"]
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    digests = packet[EVIDENCE_KEY]["contracts"]
    assert ([entry["reason_summary"] for entry in packet["fits"]], packet["limits"]["speaker-set"], digests.pop("speaker")) == (
        [{"unavailable": code}], {"status": "unavailable", "reason": code},
        {"status": "unavailable", "reason": code, "field": "candidate.json"})
    assert digests and all(isinstance(digest, str) for digest in digests.values())


@pytest.mark.parametrize("overrides", [[], ["--max-filters", "1", "--boost-floor-hz", "600", "--max-gain-db", "2", "--max-giveback-db", "1"]])
def test_speaker_fit_reads_declared_budgets_and_only_overrides_stdout(speaker_round, capsys, overrides):
    root, *_ = speaker_round
    path = root / "design-draft.json"
    draft = json.loads(path.read_text())
    draft["topology"] = mono_output_topology().to_dict()
    budget = {"max_filters": 3, "boost_floor_hz": 300, "max_gain_db": 8, "max_giveback_db": 4}
    for driver in draft["manual_settings"]["drivers"]:
        if driver["role"] == "woofer":
            driver["fit_budget"] = budget
    path.write_text(json.dumps(draft))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", *overrides]) == 0
    result = json.loads(capsys.readouterr().out)
    expected = {"max_filters": 1, "boost_floor_hz": 600, "max_gain_db": 2, "max_giveback_db": 1} if overrides else budget
    fit = result["linearization"]["woofer"]["fit"]
    assert fit["budget"] == expected
    assert len(fit["filters"]) <= expected["max_filters"]
    assert all(abs(f["gain"]) <= expected["max_gain_db"] for f in fit["filters"])
    assert fit["correction_giveback_db"] <= expected["max_giveback_db"]
    assert result["linearization"]["tweeter"]["fit"]["budget"]["max_filters"] == (1 if overrides else 8)
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_empty_manifest_has_no_packet_fits(speaker_round):
    assert _fits(round_inputs(speaker_round[0]), {}, {}, {}) == []


@pytest.mark.parametrize("other_identity", [
    {"candidate_id": "other"}, {"graph_fingerprint": "other"}, {"candidate_id": None, "graph_fingerprint": None},
])
def test_design_cloud_joins_retakes_without_borrowing_graphs(speaker_round, other_identity):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    state = json.loads(inputs.state_path.read_text())
    state["session_phases"].insert(1, "cloud_measure")
    inputs.state_path.write_text(json.dumps(state))
    directory, _ = round_artifact_dir(inputs.session_dir)
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    group = manifest_set([(path, record)], set_id="first")
    group["capture_basis"].update(role="tweeter", candidate_id="base", graph_fingerprint="graph")
    anonymous = not any(other_identity.values())
    if anonymous:
        group["capture_basis"].update(other_identity)
    original = group["takes"][0]
    group["takes"] = [own_record(original, record, take_id=f"pose-{deg}", analysis=analysis, attempt=1,
                                 pose={"kind": "bearing", "deg": deg, "elevation_deg": 0}, selected=deg != -20)
                      for deg in (-20, 0, 20)]
    latest = {**group["takes"][0], "take_id": "retake", "selected": True, "attempt": 2}
    retake = {**group, "set_id": "retaken", "takes": [latest]}
    stale = {**retake, "set_id": "older", "takes": [{**latest, "attempt": 1, "curves": [{"role": "bad"}]}]}
    other = {**group, "set_id": "other", "capture_basis": {**group["capture_basis"], **other_identity},
             "takes": [own_record(original, record, analysis=analysis, pose={"kind": "bearing", "deg": 0, "elevation_deg": 0})]}
    write_manifest(root, groups=[retake, group, stale, other])
    manifest = _joined(inputs)
    clouds = design_clouds(manifest)
    assert {key: cloud.n_positions for key, cloud in clouds.items()} == (
        {"first": 2, "retaken": 1, "older": 1, "other": 1} if anonymous else {"first": 3, "retaken": 3, "older": 3, "other": 1})
    assert len(round_alignment(manifest, prescription_sources(inputs))[0]) == (5 if anonymous else 4)
    assert len(clouds["retaken"].boost_responses) == (0 if anonymous else 3)
    for selected, expected in (("first", 2 if anonymous else 3), ("other", 1)):
        take = group["takes"][1] if selected == "first" else other["takes"][0]
        result = speaker_fit(inputs, manifest, selected, take["take_id"])
        assert result["boost_evidence"]["design_poses"] == expected
        assert result["linearization"]["tweeter"]["composed_boost_cap_db"] == 40.0


@pytest.mark.parametrize("held,declared,fault", [(False, False, "capture_clipped"), (True, True, "capture_clipped"), (True, False, None)])
def test_packet_and_speaker_fit_keep_saved_timing(speaker_round, held, declared, fault):
    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    candidate = CrossoverCandidate(trim_db={"woofer": 0, "tweeter": -3}, delay_us=120 if held else 191,
        polarity="normal" if held else "inverted", predicted_ripple_db=0.348, confidence=0 if held else 0.9,
        alignment_objective="saved_timing" if held else "summed_fit_committed",
        residual_rms_db=0.348, margin_db=1.1 if held else 2.12,
        timing_verdict="saved" if held else "measured", repeat_spread_db=.1, repeat_spread_us=2, repeat_count=3, seed_polarity_sign=1, alignment_seed_delay_us=120)
    analysis = analysis_json(ProgramAnalysis(phase="measure", stimulus_id=program.stimulus_id, locations=(),
        candidate=candidate, alignment=AlignmentEstimate(delay_us=candidate.delay_us, raw_delay_us=candidate.delay_us,
            parallax_us=4.5 if declared else 0, polarity=candidate.polarity, polarity_sign=1 if held else -1,
            confidence=candidate.confidence, seed_delay_us=120)))
    expected = {"objective": candidate.alignment_objective,
                "committed": {"delay_us": candidate.delay_us, "polarity": candidate.polarity},
                "seed": {"delay_us": 120, "polarity": "normal"},
                "confidence": candidate.confidence, "residual_rms_db": 0.348, "margin_db": candidate.margin_db,
                "repeat_spread_db": .1, "repeat_spread_us": 2, "repeat_count": 3, "timing_verdict": candidate.timing_verdict,
                "parallax_us": 4.5 if declared else 0, "driver_spacing_source": "declared" if declared else "unknown",
                "snr": {"woofer": {"verdict": "ok", "shortfall_db": 0},
                        "tweeter": {"verdict": "insufficient", "shortfall_db": 8.5}}}
    profile = {"kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION, "status": "applied",
               "source": {"measured_candidate_fingerprint": "applied-candidate"}, "config": {"sha256": "a" * 64},
               "corrections": {"woofer": {"delay_ms": 0, "inverted": False}, "tweeter": {"delay_ms": 0.12, "inverted": False}},
               "corrections_provenance": {role: {"delay_ms": "manual", "inverted": "manual"} for role in expected["snr"]}}
    corrections, provenance = profile["corrections"], profile["corrections_provenance"]
    if held:
        profile["recomposition_snapshot"] = {"corrections": corrections if declared else {"tweeter": {"delay_ms": True}},
                                              "corrections_provenance": provenance}
        profile.update(corrections={"tweeter": {"delay_ms": 999, "inverted": True}},
                       corrections_provenance={"tweeter": {"delay_ms": "measured", "inverted": "measured"}})
        if not declared:
            corrections = provenance = {role: dict.fromkeys(("delay_ms", "inverted")) for role in expected["snr"]}
    (root / "applied-profile.json").write_text(json.dumps(profile))
    if declared:
        draft_path = root / "design-draft.json"
        draft = json.loads(draft_path.read_text())
        draft["manual_settings"]["driver_spacing_mm"] = 150
        draft_path.write_text(json.dumps(draft))
    path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    group = manifest_set([(path, record)], set_id="timing")
    group["capture_basis"].update(role="woofer")
    evidence = {f"snr.{role}.alignment.{key}": value for role, row in expected["snr"].items() for key, value in row.items()}
    levels = {"tweeter": {"alignment_level_db": -26, "alignment_snr_shortfall_db": {"before": 12.5, "after": 8.5},
                          "alignment_level_capped_by": "driver_cap", "alignment_snr_residual_shortfall_db": 8.5}}
    take = own_record(group["takes"][0], record, analysis=analysis, pose={"kind": "bearing", "deg": 0, "elevation_deg": 0},
                      verdict={"evidence": evidence}, level={**record["level"], "alignment": levels})
    group["takes"] = [take]
    refused = {**take, "take_id": "refused", "selected": False}
    if fault:
        refused.update({"verdict": {**take["verdict"], "fault": fault}} if held else {"incident": fault})
    group["takes"] += [refused, {**take, "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 20},
                               "analysis": {**analysis, "delay_us": 900}}]
    write_manifest(root, groups=[group, {**group, "set_id": "duplicate", "capture_basis": {
        **group["capture_basis"], "role": "tweeter"}}])
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    pair, off_axis = packet["alignment"]
    assert off_axis["pose"]["deg"] == 20
    assert off_axis["committed"]["delay_us"] == 900
    inputs = round_inputs(root)
    fit = speaker_fit(inputs, _joined(inputs), "timing", take["take_id"])
    for answer in (pair, json.loads(json.dumps(fit["alignment"]))):
        assert {key: answer[key] for key in expected} == expected
        assert answer["applied"]["candidate"] == "applied-candidate"
        assert answer["applied"]["record"] == "a" * 12
        assert answer["applied"]["corrections"] == corrections
        assert answer["levels"] == levels
    assert all(t["alignment"] == levels for group in packet["sets"] for t in group["takes"])
    assert pair["take_id"] == take["take_id"]
    lines = (root / INDEX_FILENAME).read_text().splitlines()
    assert len([line for line in lines if line.startswith("timing:")]) == 2
    start = next(i for i, line in enumerate(lines) if line.startswith("timing:"))
    assert max(map(len, lines[start:lines.index("## Decisions")])) <= 160
    assert {t["fault"] for g in packet["sets"] for t in g["takes"] if not t["selected"]} == {fault}
    assert [line for line in lines if line.startswith("retakes:")] == ([f"retakes: refused {fault}"] if fault else [])


@pytest.mark.parametrize("refused", [False, True])
def test_packet_fits_only_drivers_and_keeps_refusal_codes(speaker_round, tmp_path, refused):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    groups = []
    for role in ("summed", "woofer", "tweeter"):
        group = manifest_set([(row.path, record)], set_id=role)
        group["capture_basis"].update(role=role)
        if refused and role == "woofer":
            group["takes"] = [own_record(group["takes"][0], record, phase="timing")]
        groups.append(group)
    write_manifest(root, groups=groups)
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json")
    packet = json.loads((banked.path / "packet.json").read_text())
    assert len(packet["fits"]) == 2
    fits = {fit["role"]: fit for fit in packet["fits"]}
    assert set(fits) == {"woofer", "tweeter"}
    if refused:
        assert fits["woofer"]["reason_summary"] == {"unavailable": "round_take_unknown"}
    else:
        assert isinstance(fits["woofer"]["filters"], list)
    assert isinstance(fits["tweeter"]["filters"], list)
    assert {take["role"] for group in packet["sets"] for take in group["takes"]} == {"summed", "woofer", "tweeter"}


@pytest.mark.parametrize("program,roles_fitted", [("speaker", {"woofer"}), ("rear/pair", set())])
def test_only_a_speaker_purpose_take_earns_a_packet_fit(speaker_round, program, roles_fitted):
    """A fit is gated speaker evidence. The same branch pair measured for a rear
    comparison is read ungated and proposes no driver filters (issue #5330).
    """
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    groups = []
    for role in ("woofer", "woofer:rear", "summed"):
        group = manifest_set([(row.path, record)], set_id=role)
        group["capture_basis"].update(role=role)
        group["takes"][0].update(role=role)
        groups.append(group)
    write_manifest(root, program=program, groups=groups)

    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])

    assert {fit["role"] for fit in packet["fits"]} == roles_fitted


@pytest.mark.parametrize("pose_count,candidate_count,cloud_planned,verifies", [
    (1, 1, True, True), (2, 2, True, True), (3, 3, True, True), (3, 1, True, True), (3, 3, False, False),
])
def test_banked_speaker_packet_fits_every_selected_pose_and_role(
    speaker_round, tmp_path, monkeypatch, capsys, pose_count, candidate_count, cloud_planned, verifies,
):

    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    state = json.loads(inputs.state_path.read_text())
    if cloud_planned:
        state["session_phases"].insert(1, "cloud_measure")
    if not verifies:
        state["session_phases"].remove("verify")
    inputs.state_path.write_text(json.dumps(state))
    directory, _ = round_artifact_dir(inputs.session_dir)
    for curve in record["curves"]:
        curve.update(gate_window_ms=7.0, validity_floor_hz=142.9, floor_source=FLOOR_SEARCH_BOUND)
    groups = []
    for base in (True, False):
        rows = []
        for degrees in (0, 30, 60):
            take = {**record, "take_id": f"take-{base}-{degrees}", "position_deg": degrees}
            path = directory / "positions" / f"{take['take_id']}.json"
            path.write_text(json.dumps(take))
            rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), take))
        for role in ("woofer", "tweeter"):
            count = pose_count if base else candidate_count
            group = manifest_set(rows, set_id=f"{base}-{role}", selected={r["take_id"] for _, r in rows[:count]})
            group.update(base=base)
            group["capture_basis"].update(role=role, candidate_id="base" if base else "candidate")
            groups.append(group)
    write_manifest(root, groups=groups)
    manifest = _joined(inputs)
    applied_path = tmp_path / "applied-profile.json"
    applied_path.write_text(json.dumps({**_fixture_applied_profile(fc_hz=2400),
                                       "kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION}))
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", applied_profile_path=applied_path,
                        view_runner=round_views.run_bookkeeping)
    packet = json.loads((banked.path / "packet.json").read_text())
    expected = {(g["set_id"], t["take_id"], t["pose"]["deg"], g["capture_basis"]["role"])
                for g in manifest["sets"] for t in g["takes"] if t["selected"]}
    assert len(packet["fits"]) == len(expected) == 2 * (pose_count + candidate_count)
    assert all(fit["handover_level_shift_db"] is not None for fit in packet["fits"])
    assert len(packet["verdicts"]) == pose_count + candidate_count
    for fit in packet["fits"]:
        features = [feature["position_variance"] for feature in fit["filters"]]
        count = pose_count if fit["set_id"].startswith("True-") else candidate_count
        deep = count if count >= 2 else 0
        assert any(feature["positions_deep"] == deep for feature in features)
        assert all(feature["cv_percent"] == (pytest.approx(0) if deep else None)
                   for feature in features if feature["positions_deep"] == deep)
    assert {(f["set_id"], f["take_id"], f["pose"]["deg"], f["role"]) for f in packet["fits"]} == expected
    assert all(f["mic_tier"] == "reference" and isinstance(f["filters"], list)
               and f["residual_rms_db"] is not None and f["budget"] for f in packet["fits"])
    for fit in packet["fits"]:
        count = pose_count if fit["set_id"].startswith("True-") else candidate_count
        assert fit["boost_evidence"]["design_poses"] == count
        assert fit["composed_boost_cap_db"] == 40.0
    # Every banked take is drawn from its record, tagged with the set its run kept it in (#5737 C1b).
    drawn = [series for series in packet["series"] if series["set_id"]]
    assert expected <= {(s["set_id"], s["take_id"], s["pose"]["deg"], s["role"]) for s in drawn}
    assert all(s["stats"]["flatness_rms_db"]["value"] is not None for s in drawn)
    assert packet["result"] == "complete" and set(packet["limits"]) == {g["set_id"] for g in groups}
    assert Path(packet["artifacts"]["frequency_png"]).read_bytes().startswith(b"\x89PNG")
    index = (banked.path / INDEX_FILENAME).read_text().splitlines()
    assert f"Fingerprint: {packet['packet_fingerprint']}" in index
    heads = ("Measured:", "Applied:", "Result:", "## Decisions", "driver:", "blend:", "alignment:", "topology:",
             "gate ", "series ", "fit ", "null ceiling ", "## Artifacts", "## Tools", "Fingerprint:")
    positions = [next(i for i, line in enumerate(index) if line.startswith(head)) for head in heads]
    assert positions == sorted(positions)
    tools = [shlex.split(line[3:-1]) for line in index if line.startswith("- `")]
    for group in packet["sets"]:
        first = next(take["take_id"] for take in group["takes"] if take["selected"])
        assert ["jasper-round-views", "speaker-fit", str(banked.path), "--set", group["set_id"], "--take", first] in tools
        for take in group["takes"]:  # a deselected take's record is never read, so it states no gate
            gates = {"gate_window_ms": 7.0, "validity_floor_hz": 142.9,
                     "trusted_floor_hz": f_trusted_floor_hz(.007), "floor_source": FLOOR_SEARCH_BOUND}
            assert {key: take[key] for key in gates} == (gates if take["selected"] else dict.fromkeys(gates))
    for series in drawn:
        stats = series["stats"]
        if series["role"] == "woofer":
            assert stats["band_means_db"]["250"]["below_trusted_floor"] is True
            assert stats["band_means_db"]["500"]["below_trusted_floor"] is True
        assert stats["band_means_db"]["1000"]["below_trusted_floor"] is False
        assert stats["flatness_rms_db"]["band_hz"] == [f_trusted_floor_hz(.007), 10000]
        assert stats["tilt_db_per_decade"]["below_trusted_floor"] is False
        assert all(row["below_trusted_floor"] == (row["value"] is not None) for row in stats["low_end_means_db"].values())
    assert crossover_prescriber.main(["status", str(banked.path)]) == 0
    assert packet["packet_fingerprint"] == json.loads(capsys.readouterr().out)["packet_fingerprint"]


@pytest.mark.parametrize("trial,delay,seed,polarity,objective,confidence", [
    (False, 157.5, 120, "inverted", "summed_fit_committed", 0.9),
    (True, -157.5, -120, "normal", "summed_fit_committed", 0.9),
    (False, 0, 0, "normal", ALIGNMENT_ESTIMATED_FLAT_SUM, 0),
])
def test_first_speaker_round_banks_its_timing_read_and_no_candidate(
    speaker_round, tmp_path, monkeypatch, trial, delay, seed, polarity, objective, confidence,
):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    assert analysis["alignment_status"] == ALIGNMENT_OK
    analysis.update(delay_us=delay, polarity=polarity, trim_db={"woofer": 0, "tweeter": -3},
                    alignment_seed_delay_us=seed, alignment_status=ALIGNMENT_OK, alignment_objective=objective,
                    alignment_confidence=confidence, timing_verdict="measured" if objective == "summed_fit_committed" else "estimate",
                    residual_rms_db=.2, margin_db=.4, repeat_spread_db=.1, repeat_spread_us=2,
                    timing_graph_fingerprint="played-timing" if objective == "summed_fit_committed" else None)
    topology = mono_output_topology()
    draft = standard_design_draft(topology)
    declared = candidate_from_design_draft(topology, draft)
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.load_output_topology_strict", lambda: topology)
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.load_applied_baseline_profile_state", lambda: None)
    monkeypatch.setattr("jasper.active_speaker.design_draft.load_design_draft", lambda **kw: draft)
    assert baseline_candidate_id() == declared.fingerprint
    assert find_banked_candidate(declared.fingerprint).candidate == declared
    (root / "design-draft.json").write_text(json.dumps(draft))
    (directory / "candidate.json").unlink()
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    group = manifest_set([(row.path, record)], set_id="design-mark")
    group.update(base=not trial)
    group["capture_basis"].update(candidate_id=declared.fingerprint if trial else None)
    group["takes"] = [own_record(group["takes"][0], record, analysis=analysis, attempt=2,
                                 captured_at="2026-09-29T12:00:02Z",
                                 pose={"kind": "bearing", "deg": 0, "elevation_deg": 0, "distance_m": 1})]
    off_axis = {**group["takes"][0], "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 30},
                "captured_at": "2026-09-29T12:00:04Z", "analysis": {**analysis, "delay_us": 999}}
    unselected = {**group["takes"][0], "take_id": "unselected", "selected": False, "captured_at": "2026-09-29T12:00:03Z"}
    older = {**group, "set_id": "older-set", "takes": [{**group["takes"][0], "take_id": "older", "attempt": 1,
                                                       "captured_at": "2026-09-29T12:00:02Z",
             "analysis": {**analysis, "delay_us": 75, "alignment_seed_delay_us": 75, "trim_db": {"woofer": 0, "tweeter": -6}}}]}
    group["takes"].extend([off_axis, unselected, {**off_axis, "take_id": "verify", "phase": "verify",
                                                "pose": group["takes"][0]["pose"]}])
    write_manifest(root, groups=[older, group] if trial else [group, older])
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", applied_profile_path=tmp_path / "absent.json")
    packet = json.loads((banked.path / "packet.json").read_text())
    assert "commissioning" not in packet
    assert not (banked.path.parent / "commissioning.json").exists()
    pair = next(row for row in packet["alignment"] if row["take_id"] == group["takes"][0]["take_id"])
    assert pair["committed"] == {"delay_us": delay, "polarity": polarity}
    assert pair["trim_db"] == analysis["trim_db"]
    assert pair["graph_fingerprint"] == ("played-timing" if objective == "summed_fit_committed" else group["capture_basis"]["graph_fingerprint"])
    assert group["capture_basis"]["graph_fingerprint"] != "played-timing"
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize("incumbent_role,rear", [("tweeter", False), ("woofer", False), ("woofer", True)])
def test_fit_budget_excludes_replaced_role_but_charges_other_branches(incumbent_role, rear):
    """#5909: the tweeter's cap is what the emitted graph charges without its own chain."""
    speaker = {"preset": _rear_pair("mono")[0], "rear_calibration": _rear_document()} if rear else {}
    chain = {"filters": [{"biquad_type": "Peaking", "freq": 300.0, "q": 1.0, "gain": 30.0}]}
    others = _candidate(linearization={} if incumbent_role == "tweeter" else {"woofer": chain}, **speaker)
    graph = yaml.safe_load(compile_candidate_config(others, playback_device="null"))
    remaining = 40.0 + graph["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    candidate = _candidate(linearization={incumbent_role: chain}, **speaker)
    vocabulary = _fit_vocabularies(candidate, {"tweeter": {"max_gain_db": 2.0}})["tweeter"]
    assert vocabulary.per_filter_boost_cap_db == vocabulary.composed_boost_cap_db == pytest.approx(remaining)
    assert vocabulary.max_gain_db == 2.0


@pytest.mark.parametrize("verdict,residual,noise,reasons,action", [
    ("saved", .15, .01, {}, None),
    ("saved", TIMING_RESIDUAL_FLOOR_DB + .1, .01, {}, "reset_timing"),
    ("saved", 10.6, .43, {"snr_short": ("tweeter", "woofer")}, "measure_timing"),
    ("saved", 10.6, .43, {"graph_mismatch": ("woofer:rear",)}, "remeasure_timing"),
    ("saved", 10.6, .43, None, "measure_timing"),
    ("saved", TIMING_RESIDUAL_FLOOR_DB + .1, TIMING_RESIDUAL_FLOOR_DB, {}, None),
    ("saved", 2, None, {}, None),
    ("measured", None, None, {}, "apply_timing"), ("needs_measurement", None, None, {}, "measure_timing"),
])
def test_packet_timing_verification_and_next_action(speaker_round, verdict, residual, noise, reasons, action):
    """#5632 F3: a reading that is not comparable never asks for a reset; the page and envelope say what the packet says."""
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    timing = {"delay_us": 22.0, "polarity": "normal", "provenance": "measured", "measured": {"take_id": "original"}} if residual is not None else None
    verification = timing_verification(residual, noise, **(reasons or {})) if timing else None
    if reasons is None:  # stored before ADR-0345: no status, so comparability is unknown
        verification = {key: verification[key] for key in ("residual_rms_db", "repeat_noise_db", "residual_floor_db")}
    profile_path = root / "applied-profile.json"
    profile = {"kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION, "status": "applied"}
    profile_path.write_text(json.dumps({**profile, **({"timing": timing} if timing else {})}))
    group = manifest_set([(path, record)], set_id="timing")
    group["takes"] = [own_record(group["takes"][0], record, pose={"kind": "bearing", "deg": 0, "elevation_deg": 0},
        analysis={"trim_db": {"woofer": 0, "tweeter": -3}, "delay_us": 22, "polarity": "normal",
                  "timing_saved": timing, "timing_verdict": verdict,
                  "timing_verification": verification})]
    group["takes"].append({**group["takes"][0], "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 20, "elevation_deg": 0},
                          "analysis": {**group["takes"][0]["analysis"], "timing_verdict": "needs_measurement"}})
    write_manifest(root, groups=[group])
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    assert packet["alignment_verdict"] == {"saved": timing, "verification": verification}
    assert (packet["next_action"] or {}).get("id") == action
    lines = timing_status_lines(json.loads(profile_path.read_text()), {key: packet[key] for key in ("alignment_verdict", "next_action")})
    assert lines["next_action"] == packet["next_action"]
    previous = {"id": "continue"}
    envelope = _envelope(screen="finished", active_step="measure", verdict="", next_action=previous, alternate_actions=[{"id": "stop"}],
                         status={"timing": lines, "crossover_v2": {"candidate": {"timing_saved": timing, "timing_verification": {
                             "residual_rms_db": 10.6, "repeat_noise_db": .43}, "timing_verdict": verdict}}})
    assert envelope["timing"] == lines
    assert envelope["next_action"]["id"] == ("reset_timing" if action == "reset_timing" else "continue")
    assert envelope["alternate_actions"] == ([previous, {"id": "stop"}] if action == "reset_timing" else [{"id": "stop"}])
    assert json.loads(profile_path.read_text()).get("timing") == timing
