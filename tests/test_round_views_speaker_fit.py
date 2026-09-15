# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import shlex
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.alignment_evidence import round_alignment
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.branch_chain import radiating_band_hz, sections_by_role
from jasper.active_speaker.branch_target import branch_target
from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db, decide_trim
from jasper.active_speaker.crossover_v2.driver_prescription import _check_composed
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.crossover_v2.round_inputs import prescription_sources, round_artifact_dir, round_inputs
from jasper.active_speaker.crossover_v2.round_views import response_from_banked_curve
from jasper.active_speaker.crossover_v2.spatial import _primary_sweep_bands, analysis_curve_records
from jasper.active_speaker.linearization_envelope import compose_envelope
from jasper.active_speaker.linearization_fit import (
    FitVocabulary, core_level_band_hz, fit_driver_linearization, measurement_hole_bands_hz,
)
from jasper.active_speaker.profile import CrossoverRegion
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.gating import FLOOR_SEARCH_BOUND, f_trusted_floor_hz
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR, ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    AlignmentEstimate, CrossoverCandidate, DriverResponse, ProgramAnalysis, RealizedLevelMatch,
)
from jasper.cli import crossover_prescriber, round_views
from tests.crossover_v2_banked_round import bank_executor_take, bank_measure_round
from jasper.active_speaker.round_packet import INDEX_FILENAME, _fits, write_round_packet
from jasper.active_speaker.speaker_fit import design_clouds, speaker_fit
from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker.candidate_parts import baseline_candidate_id, candidate_from_design_draft
from jasper.active_speaker.commissioning_experiment import commissioning_candidate
from jasper.active_speaker.crossover_v2.alignment_prescription import PRESCRIPTION_OUTSIDE_DECLARED_WINDOW, PRESCRIPTION_OUT_OF_LOBE
from jasper.active_speaker.measured_crossover_candidate import effective_preset
from tests.active_speaker_fixtures import mono_output_topology, standard_design_draft
from tests.run_manifest_fixture import manifest_set, write_manifest


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
    record_path = take_artifact_path(inputs.session_dir, row.path)
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


@pytest.mark.parametrize("vocabulary,cloud_planned,cloud_present,post_apply_verifies,expected", [
    (None, False, False, True, "bounded_boost"),
    (None, True, False, True, "cut_only"),
    (None, True, True, True, "bounded_boost"),
    (None, False, False, False, "cut_only"),
    ("cut_only", False, False, True, "cut_only"),
    ("bounded_boost", True, False, True, "bounded_boost"),
])
def test_speaker_fit_matches_explicit_math_and_banked_decisions(
    speaker_round, vocabulary, cloud_planned, cloud_present, post_apply_verifies, expected, capsys,
):
    root, record, program, classes, region, trim = speaker_round
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
    flags = [] if vocabulary is None else ["--vocabulary", vocabulary]
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set", *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"linearization", "alignment", "trim"} <= result.keys()
    assert result["vocabulary"] == expected
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
                                       vocabulary=FitVocabulary(allow_boost=expected == "bounded_boost"),
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
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            assert len(value) <= 16
            pending.extend(value)
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("changes,expected", [
    pytest.param({"poses": 2}, "cut_only", id="two-poses"),
    pytest.param({}, "bounded_boost", id="three-poses"),
    pytest.param({"role": "woofer"}, "cut_only", id="lowpass"),
    pytest.param({"role": "main"}, "cut_only", id="one-way"),
    pytest.param({"exclusion": True}, "bounded_boost", id="existing-exclusion"),
    pytest.param({"verifies": False}, "bounded_boost", id="no-verify"),
    pytest.param({"verifies": False, "cloud_planned": False}, "bounded_boost", id="no-cloud-or-verify"),
    pytest.param({"floor": 100.0}, "bounded_boost", id="lower-budget-floor"),
    pytest.param({"floor": 8000.0}, "bounded_boost", id="higher-budget-floor"),
    pytest.param({"override": "bounded_boost", "verifies": False}, "bounded_boost", id="operator-boost"),
    pytest.param({"override": "cut_only"}, "cut_only", id="operator-cuts"),
    pytest.param({"depth": 18.0}, "bounded_boost", id="composed-cap"),
    pytest.param({"disagree": True}, "bounded_boost", id="off-axis-contradiction"),
    pytest.param({"stimulus": "reference_axis"}, "bounded_boost", id="reference-axis-takes"),
    pytest.param({"basis_role": "summed"}, "cut_only", id="summed-set"),
    pytest.param({"missing_curve": True}, "cut_only", id="missing-cloud-curve"),
])
def test_design_cloud_bounds_each_roles_fit(speaker_round, capsys, changes, expected):
    root, record, program, _, region, _ = speaker_round
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
    response = replace(response_from_banked_curve(record["curves"][1])[0], role=role, repeat_responses=())
    rows = []
    for index, (deg, kind, phase) in enumerate([
        (0, "bearing", "measure"), (-20, "bearing", "measure"), (20, "bearing", "measure"),
        (0, "bearing", "measure"), (40, "seat", "measure"), (60, "bearing", "verify"),
        (80, "bearing", "measure"),
    ]):
        depth = -7 if changes.get("disagree") and deg else changes.get("depth", 7)
        db = -depth * np.exp(-0.5 * (np.log2(response.freqs_hz / 6000) / 0.25) ** 2)
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
    write_manifest(root, groups=[group])
    flags = [arg for key in ("floor", "override") if key in changes
             for arg in ("--boost-floor-hz" if key == "floor" else "--vocabulary", str(changes[key]))]
    assert round_views.main(["speaker-fit", str(root), "--set", role, "--take", "design-0", *flags]) == 0
    result = json.loads(capsys.readouterr().out)
    proposal, fit = result["linearization"][role], result["linearization"][role]["fit"]
    assert result["vocabulary"] == proposal["vocabulary"] == expected
    assert result["cloud"] == proposal["cloud"]
    assert proposal["cloud"]["design_poses"] == (0 if changes.get("override") or changes.get("basis_role") else poses)
    design_boost = expected == "bounded_boost" and not changes.get("override") and not changes.get("exclusion")
    floor = changes.get("floor")
    if design_boost:
        floor = max(radiating_band_hz(sections_by_role([CrossoverRegion.from_mapping(region)])[role])[0], floor or 0)
    assert fit["budget"]["boost_floor_hz"] == floor
    assert proposal["per_filter_boost_cap_db"] == (3.0 if design_boost else FitVocabulary().per_filter_boost_cap_db)
    assert proposal["composed_boost_cap_db"] == (3.0 if design_boost else None)
    boosts = [f for f in fit["filters"] if f["gain"] > 0]
    assert all(f["freq"] >= (floor or 0) for f in boosts)
    if design_boost:
        assert fit["composed_boost_cap_db"] == 3.0
        peak, _ = _check_composed(tuple({"role": role, **f} for f in fit["filters"]), {role: (1600, 20000)})
        assert peak <= 3.0 + 1e-9
        assert all(f["gain"] <= 3.0 for f in boosts)
        assert proposal["cloud"]["band_spread"]
    if expected == "cut_only" or changes.get("disagree"):
        assert not boosts
    elif floor != 8000:
        assert boosts
    if changes.get("depth") == 18:
        assert "composed_boost_cap_db" in fit["budget_binding"]
    if changes.get("disagree"):
        assert any(b["sigma_db"] > 0 for b in proposal["cloud"]["band_spread"])
        assert fit["lift_suppressed_reason"] == "boost_above_measured_target"
    if changes.get("missing_curve"):
        assert not proposal["cloud"]["band_spread"]


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


def test_speaker_fit_falls_back_to_the_rounds_banked_base(speaker_round, capsys, monkeypatch):
    from types import SimpleNamespace
    from jasper.active_speaker import candidate_bank
    from jasper.cli.round_views import speaker_fit as view

    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    stored = json.loads((directory / "candidate.json").read_text())
    (directory / "candidate.json").unlink()
    manifest = json.loads((root / "run_manifest.json").read_text()) if (root / "run_manifest.json").is_file() else None
    row_path = next(row.path for row, _ in measurement_documents(inputs.session_dir) if row.phase == "measure")
    measured = manifest_set([(row_path, record)], set_id="speaker-set")
    measured["capture_basis"].update(role="woofer")
    base = manifest_set([(row_path, record)], set_id="base-set")
    base["capture_basis"].update(graph_scope="candidate", candidate_id="base-fp")
    write_manifest(root, groups=[base, measured])
    looked_up = []

    def find(fingerprint):
        looked_up.append(fingerprint)
        return SimpleNamespace(candidate=SimpleNamespace(to_dict=lambda: stored))

    monkeypatch.setattr(candidate_bank, "find_banked_candidate", find)
    assert round_views.main(["speaker-fit", str(root), "--set", "speaker-set"]) == 0
    assert looked_up == ["base-fp"]
    del manifest, view


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
    budget = {"max_filters": 3, "boost_floor_hz": 300, "max_gain_db": 8, "max_giveback_db": 4}
    draft["driver_safety_profile"] = {"targets": [{"role": "woofer", "fit_budget": budget}]}
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
    assert len(round_alignment(manifest, prescription_sources(inputs))) == 2
    assert len(clouds["retaken"].boost_responses) == (0 if anonymous else 3)
    for selected, expected in (("first", 2 if anonymous else 3), ("other", 1)):
        take = group["takes"][1] if selected == "first" else other["takes"][0]
        path = directory / "positions" / f"{take['take_id']}.json"
        path.write_text(json.dumps({**record, "take_id": take["take_id"]}))
        take["artifacts"] = {"record_id": str(path.relative_to(inputs.session_dir / "evidence/v1/artifacts"))}
        result = speaker_fit(inputs, manifest, selected, take["take_id"])
        assert result["cloud"]["design_poses"] == expected
        assert result["vocabulary"] == ("bounded_boost" if expected == 3 else "cut_only")


@pytest.mark.parametrize("held,declared,fault", [(False, False, "capture_clipped"), (True, True, "capture_clipped"), (True, False, None)])
def test_packet_and_speaker_fit_keep_saved_timing(speaker_round, held, declared, fault):
    root, record, program, *_ = speaker_round
    inputs = round_inputs(root)
    directory, _ = round_artifact_dir(inputs.session_dir)
    candidate = CrossoverCandidate(trim_db={"woofer": 0, "tweeter": -3}, delay_us=120 if held else 191,
        polarity="normal" if held else "inverted", predicted_ripple_db=0.348, confidence=0 if held else 0.9,
        alignment_objective=ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR if held else "summed_fit_committed",
        summed_fit_rms_db=0.348, summed_fit_margin=1.1 if held else 2.12,
        summed_fit_verdict="ambiguous" if held else "committed", delay_interval_us=(181, 201), seed_polarity_sign=1)
    analysis = analysis_json(ProgramAnalysis(phase="measure", program_id=program.program_id, locations=(),
        candidate=candidate, alignment=AlignmentEstimate(delay_us=candidate.delay_us, raw_delay_us=candidate.delay_us,
            parallax_us=4.5 if declared else 0, polarity=candidate.polarity, polarity_sign=1 if held else -1,
            confidence=candidate.confidence, seed_delay_us=120)))
    expected = {"objective": candidate.alignment_objective,
                "committed": {"delay_us": candidate.delay_us, "polarity": candidate.polarity, "ripple_db": 0.348},
                "seed": {"delay_us": 120, "polarity": "normal", "ripple_db": None},
                "confidence": candidate.confidence, "summed_fit_rms_db": 0.348, "summed_fit_margin": candidate.summed_fit_margin,
                "delay_interval_us": [181, 201], "summed_fit_verdict": candidate.summed_fit_verdict,
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
    take.update(analysis=analysis, role="woofer", pose={"kind": "bearing", "deg": 0, "elevation_deg": 0},
                quality={"evidence": evidence}, timing={"ended_s": 2})
    refused = {**take, "take_id": "refused", "selected": False}
    if fault:
        refused.update({"quality": {"fault": fault}} if held else {"fault": fault})
    group["takes"] += [refused, {**take, "take_id": "off-axis", "pose": {"kind": "bearing", "deg": 20},
                               "analysis": {**analysis, "delay_us": 900}, "timing": {"ended_s": 3}}]
    manifest = write_manifest(root, groups=[group, {**group, "set_id": "duplicate", "capture_basis": {
        **group["capture_basis"], "role": "tweeter"}}])
    packet = write_round_packet(root, str(directory / "run_manifest.json"), [])
    pair, = packet["alignment"]
    fit = speaker_fit(round_inputs(root), manifest, "timing", take["take_id"])
    for answer in (pair, json.loads(json.dumps(fit["alignment"]))):
        assert {key: answer[key] for key in expected} == expected
        assert answer["applied"]["candidate"] == "applied-candidate"
        assert answer["applied"]["record"] == "a" * 12
        assert answer["applied"]["corrections"] == corrections
        assert answer["applied"]["corrections_provenance"] == provenance
    assert pair["take_id"] == take["take_id"]
    lines = (root / INDEX_FILENAME).read_text().splitlines()
    assert len([line for line in lines if line.startswith("timing:")]) == 1
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
    mark_state(inputs.session_dir, "applied")
    banked = bank_round(inputs.session_dir, campaign_root=tmp_path / "bank", state_path=inputs.state_path,
                        design_draft_path=root / "design-draft.json", view_runner=round_views.run_bookkeeping)
    packet = json.loads((banked.path / "packet.json").read_text())
    expected = {(g["set_id"], t["take_id"], t["pose"]["deg"], t["role"])
                for g in manifest["sets"] for t in g["takes"] if t["selected"]}
    assert len(packet["fits"]) == len(expected) == 2 * (pose_count + candidate_count)
    assert len(packet["verdicts"]) == pose_count + candidate_count
    for fit in packet["fits"]:
        features = [feature["position_variance"] for feature in fit["filters"]]
        count = pose_count if fit["set_id"].startswith("True-") else candidate_count
        deep = count if count >= 3 else 0
        assert any(feature["positions_deep"] == deep for feature in features)
        assert all(feature["cv_percent"] == (pytest.approx(0) if deep else None)
                   for feature in features if feature["positions_deep"] == deep)
    assert {(f["set_id"], f["take_id"], f["pose"]["deg"], f["role"]) for f in packet["fits"]} == expected
    assert all(f["mic_tier"] == "reference" and isinstance(f["filters"], list)
               and f["residual_rms_db"] is not None and f["budget"] for f in packet["fits"])
    for fit in packet["fits"]:
        count = pose_count if fit["set_id"].startswith("True-") else candidate_count
        bounded = fit["role"] == "tweeter" and count == 3
        assert fit["vocabulary"] == ("bounded_boost" if bounded else "cut_only")
        assert fit["cloud"]["design_poses"] == count
        assert fit["composed_boost_cap_db"] == (3.0 if bounded else None)
    assert len(packet["series"]) == len(expected)
    assert all(s["stats"]["rms_100_10k_db"]["value"] is not None for s in packet["series"])
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
        assert stats["rms_100_10k_db"]["below_trusted_floor"] is True
        assert stats["tilt_db_per_decade"]["below_trusted_floor"] is True
        assert all(row["below_trusted_floor"] == (row["value"] is not None) for row in stats["low_end_means_db"].values())
    assert crossover_prescriber.main(["status", str(banked.path)]) == 0
    assert packet["packet_fingerprint"] == json.loads(capsys.readouterr().out)["packet_fingerprint"]


@pytest.mark.parametrize("trial,delay,seed,polarity,status,objective,confidence,reason", [
    (False, 157.5, 120, "inverted", ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9, ""),
    (True, -157.5, -120, "normal", ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9, ""),
    (False, 0, 0, "normal", ALIGNMENT_OK, ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR, 0,
     ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR),
    (False, 400, 120, "normal", ALIGNMENT_OK, ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR, 0,
     ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR),
    (False, 1200, 1200, "inverted", ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9, PRESCRIPTION_OUTSIDE_DECLARED_WINDOW),
    (False, 500, 120, "inverted", ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9, PRESCRIPTION_OUT_OF_LOBE),
    (False, 157.5, 120, "inverted", ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9,
     ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW),
    (False, 157.5, 120, "inverted", None, ALIGNMENT_COMMITTED_FLAT_SUM, 0.9, "commissioning_alignment_unavailable"),
    (False, 0, 0, "normal", ALIGNMENT_OK, ALIGNMENT_COMMITTED_FLAT_SUM, 0, "commissioning_alignment_unavailable"),
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
                    alignment_confidence=confidence)
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
    pair, = packet["alignment"]
    assert {key: first["alignment"][key] for key in pair} == pair
    assert pair["committed"] == {"delay_us": delay, "polarity": polarity, "ripple_db": analysis["predicted_ripple_db"]}
    assert pair["trim_db"] == analysis["trim_db"]
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
