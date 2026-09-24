# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.alignment_evidence import round_alignment
from jasper.active_speaker.crossover_envelope_v2 import _envelope
from jasper.active_speaker.timing_status import timing_status_lines
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.branch_chain import radiating_band_hz, sections_by_role
from jasper.active_speaker.branch_target import branch_target
from jasper.active_speaker.crossover_v2.intervention import CloudFitTerms, compose_sigma_db, fit_branches
from jasper.active_speaker.crossover_v2.driver_prescription import _check_composed
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY, exception_detail
from jasper.active_speaker.crossover_v2.round_inputs import RoundViewsError, prescription_sources, round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands, analysis_curve_records
from jasper.active_speaker.linearization_envelope import compose_envelope
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, LinearizationFilter, complex_correction_response, core_level_band_hz, fit_driver_linearization, measurement_hole_bands_hz,
)
from jasper.active_speaker.profile import CrossoverRegion
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.gating import FLOOR_SEARCH_BOUND, f_trusted_floor_hz
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.timing_verification import TIMING_RESIDUAL_FLOOR_DB, timing_verification
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK, ALIGNMENT_ESTIMATED_FLAT_SUM, ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    AlignmentEstimate, CrossoverCandidate, DriverResponse, ProgramAnalysis,
)
from jasper.cli import crossover_prescriber, round_views
from tests.crossover_v2_banked_round import bank_executor_take, bank_measure_round
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME
from jasper.active_speaker.round_packet import _fits, write_round_packet
from jasper.active_speaker.speaker_fit import _fit_vocabularies, design_clouds, fit_feature_curves, speaker_fit
from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker import candidate_parts, commissioning_experiment
from jasper.active_speaker.candidate_parts import baseline_candidate_id, candidate_from_design_draft
from jasper.active_speaker.commissioning_experiment import commissioning_candidate
from jasper.active_speaker.crossover_v2.alignment_prescription import PRESCRIPTION_OUTSIDE_DECLARED_WINDOW, PRESCRIPTION_OUT_OF_LOBE
from jasper.active_speaker.measured_crossover_candidate import effective_preset
from tests.active_speaker_fixtures import mono_output_topology, standard_design_draft
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.crossover_v2_fixtures import _fixture_applied_profile


@pytest.mark.parametrize("basis,fc_hz", [(None, 2500), (120, None)])
def test_commissioning_requires_known_alignment_lobe(tmp_path, monkeypatch, basis, fc_hz):
    topology = mono_output_topology()
    draft = standard_design_draft(topology)
    declared = candidate_from_design_draft(topology, draft)
    read_prescription = commissioning_experiment.read_alignment_prescription

    def read_at_corner(document, **kwargs):
        prescription = read_prescription(document, **{**kwargs, "fc_hz": fc_hz})
        assert prescription.out_of_lobe is None
        return prescription

    monkeypatch.setattr(commissioning_experiment, "read_alignment_prescription", read_at_corner)
    alignment = {
        "base": True, "pose": {"deg": 0, "elevation_deg": 0},
        "committed": {"delay_us": 157.5, "polarity": "inverted"},
        "seed": {"delay_us": basis}, "trim_db": {"woofer": 0, "tweeter": -3},
        "timing_verdict": "measured", "status": ALIGNMENT_OK,
        "objective": "summed_fit_committed", "record_id": "mark.json",
    }
    result = commissioning_experiment.bank_commissioning_experiment(
        tmp_path / "round", {"run_id": "test"}, {"draft": draft}, [alignment],
    )
    assert result["status"] == "alignment_unmeasured"
    assert result["reason"] == "commissioning_alignment_unavailable"
    candidate = find_banked_candidate(result["candidate_fingerprint"], root=tmp_path).candidate
    assert candidate.alignment == declared.alignment
    assert candidate.role_attenuations_db == alignment["trim_db"]


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
                                  complex_tf=10 ** (db / 20) + 0j, gating={}, snr=None, validity_floor_hz=180)
        responses.append(replace(response, repeat_responses=(response, response)))
    analysis = ProgramAnalysis(
        phase="measure", program_id=program.program_id, locations=(), driver_responses=tuple(responses),
        alignment=AlignmentEstimate(delay_us=157.5, raw_delay_us=162, parallax_us=4.5,
                                    polarity="inverted", polarity_sign=-1, confidence=0.9, seed_delay_us=-205, polarity_agrees_with_sum=False),
        candidate=CrossoverCandidate(trim_db={"woofer": 0, "tweeter": -3}, polarity="inverted",
                                     delay_us=157.5, predicted_ripple_db=1.25, confidence=0.9,
                                     seed_polarity_sign=1, alignment_seed_ripple_db=3.5, alignment_seed_delay_us=120,
                                     alignment_objective="flat_sum_estimate", flatness_improvement_db=2.25,
                                     anchor_delay_us=150, snap_delta_us=7.5),
    )
    record.update(program=program.to_dict(), program_id=program.program_id,
                  curves=analysis_curve_records(analysis, program),
                  capture_setup={"calibration": {"model": "minidsp_umik2", "calibration_id": "mic-1"}},
                  capture_calibration={"applied": True, "calibration_id": "mic-1", "curve_fingerprint": "curve-1"})
    record_path = take_artifact_path(inputs.session_dir, row.path)
    record_path.write_text(json.dumps(record))
    classes = {"woofer": "unknown", "tweeter": "soft_dome"}
    (root / "design-draft.json").write_text(json.dumps({"manual_settings": {
        "drivers": [{"role": role, "driver_class": cls} for role, cls in classes.items()],
    }}))
    region = {"id": "pair", "lower_driver": "woofer", "upper_driver": "tweeter", "fc_hz": 2400, "order": 4}
    (directory / "candidate.json").write_text(json.dumps({
        "analysis": analysis_json(analysis), "source_preset": {"crossover_regions": [region]},
    }))
    group = manifest_set([(row.path, record)], set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, groups=[group])
    return root, record, program, classes, region


@pytest.mark.parametrize("cloud_planned,cloud_present,post_apply_verifies", [
    (False, False, True), (True, False, True), (True, True, True), (False, False, False),
])
def test_speaker_fit_matches_explicit_math_and_banked_decisions(
    speaker_round, cloud_planned, cloud_present, post_apply_verifies, capsys,
):
    root, record, program, classes, region = speaker_round
    inputs = round_inputs(root)
    state = json.loads(inputs.state_path.read_text())
    if not post_apply_verifies:
        state["session_phases"].remove("verify")
    if cloud_planned:
        state["session_phases"].insert(1, "cloud_measure")
    inputs.state_path.write_text(json.dumps(state))
    directory, _ = round_artifact_dir(inputs.session_dir)
    candidate_path = directory / "candidate.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["exclusion_evidence"] = {"n_positions": 3, "excluded_bands_hz": [], "band_spread": []} if cloud_present else {}
    candidate_path.write_text(json.dumps(candidate))
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
@pytest.mark.parametrize("fc_hz", [2400, 200])
def test_speaker_fit_discloses_handover_level_shift(speaker_round, monkeypatch, fc_hz, gain_db):
    root, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    manifest = json.loads((directory / "run_manifest.json").read_text())
    candidate_path = directory / "candidate.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["source_preset"]["crossover_regions"][0]["fc_hz"] = fc_hz
    candidate_path.write_text(json.dumps(candidate))
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
    if fc_hz == 200:
        assert result["trim_decision"] == {"status": "unavailable", "reason": "handover_band_unmeasured"}


@pytest.mark.parametrize("cut_db", [0, 3, 6, 12])
def test_fit_resolves_trim_after_tweeter_cut(speaker_round, monkeypatch, cut_db):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    manifest = json.loads((directory / "run_manifest.json").read_text())
    path = take_artifact_path(inputs.session_dir, manifest["sets"][0]["takes"][0]["artifacts"]["record_id"])
    for curve in record["curves"]:
        response = response_from_banked_curve(curve)[0]
        level = 10 if curve["role"] == "tweeter" else 0
        curve.update(magnitude_db=np.full_like(response.freqs_hz, level).tolist())
    raw_trim = {"woofer": 0, "tweeter": -10}
    banked_trim = {"woofer": min(0, 10 - cut_db), "tweeter": min(0, -10 + cut_db)}
    candidate_path = directory / "candidate.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["analysis"]["trim_db"] = banked_trim
    candidate_path.write_text(json.dumps(candidate))
    manifest["sets"][0]["capture_basis"]["gating_applied"] = True
    manifest["sets"][0]["takes"][0]["role"] = "tweeter"
    original = fit_branches

    def controlled_fit(drivers, **kwargs):
        branches = original(drivers, **kwargs)
        return replace(branches, fits={role: replace(fit, filters=(
            (LinearizationFilter("Peaking", 2400, 0.01, -cut_db),)
            if role == "tweeter" and cut_db else ())) for role, fit in branches.fits.items()})

    path.write_text(json.dumps(record))
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
    {"poses": 1}, {"poses": 2}, {}, {"role": "woofer"}, {"role": "main"}, {"exclusion": True},
    {"verifies": False}, {"verifies": False, "cloud_planned": False}, {"floor": 100.0}, {"floor": 8000.0},
    {"horn_positions": 3}, {"horn_positions": 1}, {"disagree": True}, {"stimulus": "reference_axis"}, {"basis_role": "summed"}, {"missing_curve": True},
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
    if changes.get("exclusion"):
        candidate["exclusion_evidence"] = {"n_positions": 3, "excluded_bands_hz": [], "band_spread": []}
    if role == "main":
        program = build_measure_program({role: -24.0}, [RoleBand(role, 0, FrequencyBand(150, 20000))])
        record.update(program=program.to_dict(), program_id=program.program_id)
        candidate["analysis"]["program_id"] = program.program_id
        candidate["source_preset"]["crossover_regions"] = []
    (directory / "candidate.json").write_text(json.dumps(candidate))
    if changes.get("horn_positions"):
        (root / "design-draft.json").write_text(json.dumps({"manual_settings": {
            "drivers": [{"role": role, "driver_class": "compression_horn"}],
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
        measured = replace(response, magnitude_db=db, complex_tf=10 ** (db / 20) + 0j)
        analysis = ProgramAnalysis(phase="measure", program_id=program.program_id, locations=(),
                                  driver_responses=(replace(measured, repeat_responses=(measured, measured)),))
        curves = [c for c in record["curves"] if c["role"] != role and role != "main"] + analysis_curve_records(analysis, program)
        if changes.get("missing_curve") and index == 2:
            curves = [c for c in curves if c["role"] != role]
        take = {**record, "curves": curves, "take_id": f"design-{index}", "position_deg": deg, "pose_kind": kind, "phase": phase}
        path = directory / "positions" / f"{take['take_id']}.json"
        path.write_text(json.dumps(take))
        rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), take))
    group = manifest_set(rows, set_id=role, selected={r["take_id"] for _, r in rows[:poses] + rows[3:6]})
    group["capture_basis"].update(role=changes.get("basis_role", role), stimulus=changes.get("stimulus"))
    for take in group["takes"]:
        take.update(role=changes.get("basis_role", role), analysis=candidate["analysis"])
    manifest = write_manifest(root, groups=[group])
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
        packet_fit = next(f for f in _fits(inputs, manifest, prescription_sources(inputs), design_clouds(inputs, manifest))
                          if f["role"] == role and f["take_id"] == "design-0")
        assert packet_fit["position_spread_db"] == fit["position_spread_db"]
        assert packet_fit["class_prior_hz"] == fit["class_prior_hz"]
    if changes.get("missing_curve") or changes.get("basis_role") or poses < 2:
        assert fit["position_spread_db"] is None


@pytest.mark.parametrize("horizontal,vertical,expected", [([-20, 20], [-10, 10], 5),
    ([-40, -30, -20, -10, 10, 20, 30, 40], [-20, -10, 10, 20], 13)])
def test_baseline_design_poses_keep_both_angles_and_the_on_axis_take(speaker_round, horizontal, vertical, expected):
    root, record, *_ = speaker_round
    curve = record["curves"][0]
    poses = [(0, 0)] * 4 + [(h, 0) for h in horizontal] + [(0, v) for v in vertical]
    takes = [{"take_id": f"baseline-{i}", "selected": True, "phase": "measure", "role": "woofer",
              "pose": {"kind": "bearing", "deg": h, "elevation_deg": v}, "timing": {"ended_s": i},
              "curve": {**curve, "magnitude_db": (np.asarray(curve["magnitude_db"]) + i).tolist()}}
             for i, (h, v) in enumerate(poses)]
    manifest = {"sets": [{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}]}
    cloud = design_clouds(round_inputs(root), manifest)["woofer"]
    assert cloud.n_positions == len(cloud.boost_responses) == expected
    np.testing.assert_allclose(cloud.boost_responses[0].magnitude_db, takes[3]["curve"]["magnitude_db"])
    for response, take in zip(cloud.boost_responses[1:], takes[4:]):
        np.testing.assert_allclose(response.magnitude_db, take["curve"]["magnitude_db"])


@pytest.mark.parametrize("program,marks,pairs,spread", [("speaker/mark", 2, 1, 1), ("baseline/express", 4, 6, 3)])
def test_current_round_packet_uses_mark_pairs(speaker_round, program, marks, pairs, spread):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    rows = []
    for i in range(marks):
        curves = [{**curve, "magnitude_db": (np.asarray(curve["magnitude_db"]) + i).tolist()} for curve in record["curves"]]
        capture = {**record, "take_id": f"mark-{i}", "position_deg": 0, "vertical_deg": 0, "curves": curves}
        path = directory / "positions" / f"mark-{i}.json"
        path.write_text(json.dumps(capture))
        rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), capture))
    group = manifest_set(rows, set_id="woofer")
    group["capture_basis"]["role"] = "woofer"
    for take, (_, capture) in zip(group["takes"], rows):
        take.update(role="woofer", pose_index=0, analysis=analysis, curve=capture["curves"][0])
    write_manifest(root, program=program, groups=[group])
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
        gating={"f_trusted_hz": trusted_floor_hz}, snr=None, validity_floor_hz=143,
    )
    analysis = ProgramAnalysis(phase="measure", program_id=program.program_id, locations=(),
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
    candidate = json.loads((directory / "candidate.json").read_text())
    for take in group["takes"]:
        take["analysis"] = candidate["analysis"]
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


@pytest.mark.parametrize("run_program", ["speaker", "speaker/full"])
def test_speaker_fit_reads_the_run_purpose_behind_a_sized_program(speaker_round, capsys, run_program):
    root, record, program, *_ = speaker_round
    group = manifest_set([(next(row.path for row, _ in measurement_documents(round_inputs(root).session_dir)
                               if row.phase == "measure"), record)], set_id="speaker-set")
    group["capture_basis"].update(role="woofer")
    write_manifest(root, program=run_program, groups=[group])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    assert json.loads(capsys.readouterr().out)["set_id"] == "speaker-set"


def test_speaker_fit_reads_curves_the_run_banked_on_the_manifest_rows(speaker_round, capsys):
    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    row_path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    curves = {curve["role"]: curve for curve in record.pop("curves")}
    take_artifact_path(inputs.session_dir, row_path).write_text(json.dumps(record))
    groups = []
    for role, curve in curves.items():
        group = manifest_set([(row_path, record)], set_id=f"{role}-set")
        group["capture_basis"].update(role=role)
        for take in group["takes"]:
            take.update(role=role, curve=curve)
        groups.append(group)
    write_manifest(root, groups=groups)
    assert round_views.main(["speaker-fit", str(root), "--set", "woofer-set"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert set(answer["linearization"]) == {"woofer", "tweeter"}


@pytest.mark.parametrize("trial", [False, True])
def test_speaker_fit_reads_the_rounds_candidate_or_applied_profile(speaker_round, capsys, monkeypatch, trial):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    stored = json.loads((directory / "candidate.json").read_text())
    if not trial:
        (directory / "candidate.json").unlink()
    draft_path = root / "design-draft.json"
    draft_path.write_text(json.dumps({**json.loads(draft_path.read_text()), "topology": mono_output_topology().to_dict()}))
    (root / "applied-profile.json").write_text(json.dumps({
        "kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION, "status": "applied",
        "source": {"measured_candidate_fingerprint": "base-fp"},
    }))
    row_path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    measured = manifest_set([(row_path, record)], set_id="speaker-set")
    measured["capture_basis"].update(role="woofer", graph_scope="drivers")
    measured["takes"][0].update(role="woofer", analysis=stored["analysis"])
    base = manifest_set([(row_path, record)], set_id="base-set")
    base["capture_basis"].update(graph_scope="timing", candidate_id="projected-timing-fp")
    base["takes"][0].update(phase="entry_baseline", role="summed")
    manifest = write_manifest(root, groups=[base, measured])
    looked_up = []

    def find(fingerprint):
        looked_up.append(fingerprint)
        return SimpleNamespace(candidate=SimpleNamespace(to_dict=lambda: stored))

    monkeypatch.setattr(candidate_parts, "find_banked_candidate", find)
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    assert looked_up == ([] if trial else ["base-fp"])
    proposal = json.loads(capsys.readouterr().out)["linearization"]["woofer"]["fit"]
    fit, = _fits(round_inputs(root), manifest, prescription_sources(round_inputs(root)), {})
    assert fit["filters"] == proposal["filters"] and fit["filters"]
    assert fit["residual_rms_db"] == proposal["residual_rms_db"] is not None


def test_packet_preserves_missing_round_base_error(speaker_round):
    root, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    (directory / "candidate.json").unlink()
    (root / "applied-profile.json").unlink(missing_ok=True)
    path = directory / "run_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["sets"][0]["takes"][0]["role"] = "woofer"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RoundViewsError) as exc:
        speaker_fit(round_inputs(root), manifest, "speaker-set")
    fit, = write_round_packet(root, str(path), [])["fits"]
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


def test_a_program_shared_by_takes_cannot_identify_the_banked_analysis(speaker_round, capsys):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    row = next(row for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    group = manifest_set([(row.path, record)], set_id="speaker-set")
    second = manifest_set([(row.path, {**record, "take_id": "second-take"})], set_id="second-set")
    write_manifest(root, groups=[group, second])
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == round_views.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == round_views.REASON_REFUSED


@pytest.mark.parametrize("source_preset", ["missing", None, []])
def test_unreadable_candidate_uses_registry_code(speaker_round, source_preset, capsys):
    root, *_ = speaker_round
    directory, _ = round_artifact_dir(round_inputs(root).session_dir)
    path = directory / "candidate.json"
    candidate = json.loads(path.read_text())
    if source_preset == "missing":
        del candidate["source_preset"]
    else:
        candidate["source_preset"] = source_preset
    path.write_text(json.dumps(candidate))
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == round_views.EXIT_UNREADABLE
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unreadable"
    assert result["reason"] == round_views.REASON_UNREADABLE


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
    group["takes"] = [{**original, "take_id": f"pose-{deg}", "role": "tweeter", "analysis": analysis,
                       "curve": record["curves"][1], "pose": {"kind": "bearing", "deg": deg, "elevation_deg": 0},
                       "attempt": 1, "selected": deg != -20} for deg in (-20, 0, 20)]
    latest = {**group["takes"][0], "take_id": "retake", "selected": True, "attempt": 2}
    retake = {**group, "set_id": "retaken", "takes": [latest]}
    stale = {**retake, "set_id": "older", "takes": [{**latest, "attempt": 1, "curve": {"role": "bad"}}]}
    other = {**group, "set_id": "other", "capture_basis": {**group["capture_basis"], **other_identity},
             "takes": [{**original, "role": "tweeter", "analysis": analysis, "curve": record["curves"][1],
                        "pose": {"kind": "bearing", "deg": 0, "elevation_deg": 0}}]}
    manifest = write_manifest(root, groups=[retake, group, stale, other])
    clouds = design_clouds(inputs, manifest)
    assert {key: cloud.n_positions for key, cloud in clouds.items()} == (
        {"first": 2, "retaken": 1, "older": 1, "other": 1} if anonymous else {"first": 3, "retaken": 3, "older": 3, "other": 1})
    assert len(round_alignment(manifest, prescription_sources(inputs))[0]) == (5 if anonymous else 4)
    assert len(clouds["retaken"].boost_responses) == (0 if anonymous else 3)
    for selected, expected in (("first", 2 if anonymous else 3), ("other", 1)):
        take = group["takes"][1] if selected == "first" else other["takes"][0]
        path = directory / "positions" / f"{take['take_id']}.json"
        path.write_text(json.dumps({**record, "take_id": take["take_id"]}))
        take["artifacts"] = {"record_id": str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts"))}
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
    analysis = analysis_json(ProgramAnalysis(phase="measure", program_id=program.program_id, locations=(),
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
    take = group["takes"][0]
    evidence = {f"snr.{role}.alignment.{key}": value for role, row in expected["snr"].items() for key, value in row.items()}
    levels = {"tweeter": {"alignment_level_db": -26, "alignment_snr_shortfall_db": {"before": 12.5, "after": 8.5},
                          "alignment_level_capped_by": "driver_cap", "alignment_snr_residual_shortfall_db": 8.5}}
    take.update(analysis=analysis, role="woofer", pose={"kind": "bearing", "deg": 0, "elevation_deg": 0},
                quality={"evidence": evidence}, timing={"ended_s": 2}, alignment=levels)
    refused = {**take, "take_id": "refused", "selected": False}
    if fault:
        refused.update({"quality": {"fault": fault}} if held else {"fault": fault})
    group["takes"] += [refused, {**take, "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 20},
                               "analysis": {**analysis, "delay_us": 900}, "timing": {"ended_s": 3}}]
    manifest = write_manifest(root, groups=[group, {**group, "set_id": "duplicate", "capture_basis": {
        **group["capture_basis"], "role": "tweeter"}}])
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    pair, off_axis = packet["alignment"]
    assert off_axis["pose"]["deg"] == 20
    assert off_axis["committed"]["delay_us"] == 900
    fit = speaker_fit(round_inputs(root), manifest, "timing", take["take_id"])
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
        curve = next((c for c in record["curves"] if c["role"] == role), record["curves"][0])
        group["takes"][0].update(role=role, curve={**curve, "role": role})
        if refused and role == "woofer":
            group["takes"][0]["phase"] = "entry_baseline"
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
    assert {series["role"] for series in packet["series"]} == {"summed", "woofer", "tweeter"}
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
    candidate = json.loads((directory / "candidate.json").read_text())
    curves = {curve["role"]: curve for curve in record.pop("curves")}
    groups = []
    for base in (True, False):
        rows = []
        for degrees in (0, 30, 60):
            take = {**record, "take_id": f"take-{base}-{degrees}", "position_deg": degrees}
            path = directory / "positions" / f"{take['take_id']}.json"
            path.write_text(json.dumps(take))
            rows.append((str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts")), take))
        for role, curve in curves.items():
            curve.update(gate_window_ms=7.0, validity_floor_hz=142.9, floor_source=FLOOR_SEARCH_BOUND)
            count = pose_count if base else candidate_count
            group = manifest_set(rows, set_id=f"{base}-{role}", selected={r["take_id"] for _, r in rows[:count]})
            group.update(base=base)
            group["capture_basis"].update(role=role, candidate_id="base" if base else "candidate")
            for take in group["takes"]:
                take.update(role=role, curve=curve, analysis=candidate["analysis"])
            groups.append(group)
    manifest = write_manifest(root, groups=groups)
    monkeypatch.setattr("jasper.active_speaker.measurement_analysis.analyze_measurement_bundle",
                        lambda *a, **kw: pytest.fail("speaker packet reopened WAVs"))
    applied_path = tmp_path / "applied-profile.json"
    applied_path.write_text(json.dumps({**_fixture_applied_profile(fc_hz=2400),
                                       "kind": BASELINE_PROFILE_KIND, "artifact_schema_version": SCHEMA_VERSION}))
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", applied_profile_path=applied_path,
                        view_runner=round_views.run_bookkeeping)
    packet = json.loads((banked.path / "packet.json").read_text())
    expected = {(g["set_id"], t["take_id"], t["pose"]["deg"], t["role"])
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
    assert len(packet["series"]) == len(expected)
    assert all(s["stats"]["flatness_rms_db"]["value"] is not None for s in packet["series"])
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
        count = pose_count if group["base"] else candidate_count
        command = ["jasper-round-views", "sweep", str(banked.path), "--scope", "round", "--set", group["set_id"]]
        assert (command in tools) == (count >= 2)
        for take in group["takes"]:
            assert {key: take[key] for key in ("gate_window_ms", "validity_floor_hz", "trusted_floor_hz", "floor_source")} == {
                "gate_window_ms": 7.0, "validity_floor_hz": 142.9,
                "trusted_floor_hz": f_trusted_floor_hz(.007), "floor_source": FLOOR_SEARCH_BOUND,
            }
    for series in packet["series"]:
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


@pytest.mark.parametrize("trial,delay,seed,polarity,status,objective,confidence,reason", [
    (False, 157.5, 120, "inverted", ALIGNMENT_OK, "summed_fit_committed", 0.9, ""),
    (True, -157.5, -120, "normal", ALIGNMENT_OK, "summed_fit_committed", 0.9, ""),
    (False, 1200, 1200, "inverted", ALIGNMENT_OK, "summed_fit_committed", 0.9, PRESCRIPTION_OUTSIDE_DECLARED_WINDOW),
    (False, 500, 120, "inverted", ALIGNMENT_OK, "summed_fit_committed", 0.9, PRESCRIPTION_OUT_OF_LOBE),
    (False, 157.5, 120, "inverted", ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW, "summed_fit_committed", 0.9,
     ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
    (False, 157.5, 120, "inverted", None, "summed_fit_committed", 0.9, "commissioning_alignment_unavailable"),
    (False, 0, 0, "normal", ALIGNMENT_OK, ALIGNMENT_ESTIMATED_FLAT_SUM, 0, ALIGNMENT_ESTIMATED_FLAT_SUM),
])
def test_first_speaker_experiment_banks_measured_alignment_for_apply(
    speaker_round, tmp_path, monkeypatch, trial, delay, seed, polarity, status, objective, confidence, reason,
):
    root, record, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    analysis = json.loads((directory / "candidate.json").read_text())["analysis"]
    assert analysis["alignment_status"] == ALIGNMENT_OK
    analysis.update(delay_us=delay, polarity=polarity, trim_db={"woofer": 0, "tweeter": -3},
                    alignment_seed_delay_us=seed, alignment_status=status, alignment_objective=objective,
                    alignment_confidence=confidence, timing_verdict="measured" if objective == "summed_fit_committed" else "estimate",
                    residual_rms_db=.2, margin_db=.4, repeat_spread_db=.1, repeat_spread_us=2,
                    timing_graph_fingerprint="played-timing" if objective == "summed_fit_committed" else None)
    topology = mono_output_topology()
    draft = standard_design_draft(topology)
    draft["driver_research"]["crossover_candidates"][0].update(
        delay_ms=0.4, delay_target_role="woofer", upper_polarity="inverted",
    )
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
    group["takes"][0].update(analysis=analysis, attempt=2, timing={"started_s": 190, "ended_s": 200},
                            pose={"kind": "bearing", "deg": 0, "elevation_deg": 0, "distance_m": 1})
    off_axis = {**group["takes"][0], "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 30},
                "timing": {"ended_s": 400}, "analysis": {**analysis, "delay_us": 999}}
    unselected = {**group["takes"][0], "take_id": "unselected", "selected": False, "timing": {"ended_s": 300}}
    older = {**group, "set_id": "older-set", "takes": [{**group["takes"][0], "take_id": "older", "attempt": 1, "timing": {"ended_s": 200},
             "analysis": {**analysis, "delay_us": 75, "alignment_seed_delay_us": 75, "trim_db": {"woofer": 0, "tweeter": -6}}}]}
    group["takes"].extend([off_axis, unselected, {**off_axis, "take_id": "verify", "phase": "verify",
                                                "pose": group["takes"][0]["pose"]}])
    write_manifest(root, groups=[older, group] if trial else [group, older])
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", applied_profile_path=tmp_path / "absent.json")
    packet = json.loads((banked.path / "packet.json").read_text())
    first = packet["commissioning"]
    assert first["status"] == ("alignment_unmeasured" if reason else "awaiting_apply"), first
    assert first["reason"] == reason
    assert first["alignment"]["take_id"] == group["takes"][0]["take_id"]
    pair = next(row for row in packet["alignment"] if row["take_id"] == group["takes"][0]["take_id"])
    assert {key: first["alignment"][key] for key in pair} == pair
    assert pair["committed"] == {"delay_us": delay, "polarity": polarity}
    assert pair["trim_db"] == analysis["trim_db"]
    assert pair["graph_fingerprint"] == ("played-timing" if objective == "summed_fit_committed" else group["capture_basis"]["graph_fingerprint"])
    assert group["capture_basis"]["graph_fingerprint"] != "played-timing"
    candidate = find_banked_candidate(first["candidate_fingerprint"], root=tmp_path / "bank").candidate
    assert candidate.role_attenuations_db == analysis["trim_db"]
    if reason:
        assert candidate.alignment == declared.alignment
        assert effective_preset(candidate) == effective_preset(declared)
        assert f"alignment_unmeasured: {reason}" in (banked.path / INDEX_FILENAME).read_text()
    else:
        assert candidate.alignment.delay_us == abs(delay)
        assert candidate.alignment.delay_role == ("tweeter" if delay >= 0 else "woofer")
        assert candidate.alignment.polarity == ("invert" if polarity == "inverted" else "keep")
        assert first["alignment"]["prescription"]["checked_at_fc_hz"] == 2500
    assert candidate.source_preset == declared.source_preset
    assert candidate.analysis["measurement_status"] == "unmeasured"
    assert not candidate.linearization and not candidate.room_correction
    assert commissioning_candidate(topology, draft, root=tmp_path / "bank").fingerprint == candidate.fingerprint
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize("missing,reason", [
    ("topology", "commissioning_declaration_unavailable"),
    ("take", "commissioning_alignment_unavailable"),
    ("delay_us", "commissioning_alignment_unavailable"),
    ("polarity", "commissioning_alignment_unavailable"),
    ("invalid_topology", "commissioning_candidate_unavailable"),
])
def test_first_experiment_unavailable_codes(speaker_round, tmp_path, missing, reason):
    root, *_ = speaker_round
    inputs = round_inputs(root)
    draft = standard_design_draft(mono_output_topology())
    take = {"selected": True, "pose": {"kind": "bearing", "deg": 0, "elevation_deg": 0},
            "take_id": "mark", "artifacts": {"record_id": "mark.json"},
            "analysis": {"delay_us": 150, "polarity": "normal", "trim_db": {"woofer": 0, "tweeter": -3}}}
    if missing == "topology":
        draft.pop("topology")
    elif missing == "invalid_topology":
        draft["topology"] = {"kind": "invalid"}
    else:
        take["analysis"].pop(missing, None)
    write_manifest(root, groups=[{"set_id": "mark", "base": True, "capture_basis": {},
                                  "takes": [] if missing == "take" else [take]}])
    (root / "design-draft.json").write_text(json.dumps(draft))
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", applied_profile_path=tmp_path / "absent.json")
    result = json.loads((banked.path / "packet.json").read_text())["commissioning"]
    assert (result["status"], result["reason"]) == ("unavailable", reason)
    assert "candidate_fingerprint" not in result


@pytest.mark.parametrize("incumbent_role,remaining", [("tweeter", 40.0), ("woofer", 3.0)])
def test_fit_budget_excludes_replaced_role_but_charges_other_branches(speaker_round, incumbent_role, remaining):
    *_, region = speaker_round
    vocabularies = _fit_vocabularies({
        "source_preset": {"crossover_regions": [region]},
        "linearization": {incumbent_role: {"filters": [
            {"biquad_type": "Peaking", "freq": 300.0, "q": 1.0, "gain": 36.0},
        ]}},
    }, {"tweeter": {"max_gain_db": 2.0}})
    vocabulary = vocabularies["tweeter"]
    assert vocabulary.per_filter_boost_cap_db == pytest.approx(remaining, abs=0.05)
    assert vocabulary.composed_boost_cap_db == pytest.approx(remaining, abs=0.05)
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
    group["takes"][0].update(pose={"kind": "bearing", "deg": 0, "elevation_deg": 0},
        analysis={"trim_db": {"woofer": 0, "tweeter": -3}, "delay_us": 22, "polarity": "normal",
                  "timing_saved": timing, "timing_verdict": verdict,
                  "timing_verification": verification})
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
