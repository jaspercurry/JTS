# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A document is judged and proved as one candidate before the bank is written."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from tests.test_prescription_contract import round_bank as round_bank
from tests.test_active_speaker_audition import _applied_profile
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.bass_extension.dynamic_graph import validated_base_graph
from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION
from tests.test_crossover_v2_blend_prescription import _receipt, _document as blend_document
from jasper.active_speaker.crossover_v2.topology_prescription import candidate_topology
from jasper.active_speaker.measured_crossover_candidate import compile_candidate_config, prove_candidate_config
from jasper.active_speaker.branch_chain import beaming_onset_hz
from jasper.active_speaker import candidate_parts
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
from jasper.active_speaker.crossover_v2.blend_prescription import prescription_sha256
from jasper.active_speaker.crossover_v2.room_views import room_median_sha256
import yaml

from jasper.active_speaker.candidate_bank import CandidateBankRefusal, banked_candidates, find_banked_candidate, publish_authored_candidate
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile, compose_candidate
from jasper.active_speaker.crossover_v2.prescription_contract import contract_digests, contract_json, prescription_contracts
from jasper.active_speaker.crossover_v2.prescription_document import (
    PrescriptionDocumentRefused, PrescriptionEvidence, judge_prescription_document,
)
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverAlignment, driver_corrections
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.cli import crossover_prescriber
from jasper.web import correction_crossover_v2 as v2host
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_measured_crossover_candidate import _candidate, _room_correction
from tests.test_crossover_v2_candidate_republish import _publish
from tests.test_crossover_v2_driver_prescription import _draft, _document as driver_document
from tests.test_crossover_v2_room_prescription import _room_median, _document as room_document, MEDIAN_SHA256, NULL_HZ


@pytest.fixture
def bank(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: root)
    return root


def document(base, sections=None):
    return {"kind": "jts_prescription", "schema": 1, "base": base,
            "sections": sections or {}, "rationale": "Compare the resolved layers."}


@pytest.fixture
def base(bank):
    return publish_authored_candidate(replace(
        _candidate(room_correction=_room_correction()), bass_extension=BASS_EXTENSION,
        analysis={"measurement_status": "unmeasured"},
        blend_correction=[{"biquad_type": "Peaking", "freq": 1000, "q": 1, "gain": -1}],
    ), root=bank)


@pytest.mark.parametrize("section", ["driver", "blend", "alignment", "room", "bass"])
@pytest.mark.parametrize("empty", [None, {}])
def test_empty_clears_and_omitted_layers_inherit(base, section, empty):
    child = judge_prescription_document(document(base.fingerprint, {section: empty}), base=base)
    assert child.analysis["resolution"] == {
        name: "cleared" if name == section else "base"
        for name in ("driver", "blend", "alignment", "topology", "room", "bass")
    }
    fields = {"blend": "blend_correction", "alignment": "alignment", "room": "room_correction", "bass": "bass_extension"}
    for name, field in fields.items():
        value = getattr(child, field)
        if name != section:
            assert value == getattr(base.candidate, field)
        elif name == "alignment":
            assert value == MeasuredCrossoverAlignment()
        else:
            assert not value
    if section == "driver":
        assert child.linearization == {}
        assert set(child.role_attenuations_db.values()) == {0.0}
    else:
        assert child.role_attenuations_db == base.candidate.role_attenuations_db


@pytest.fixture
def evidence():
    return PrescriptionEvidence(
        {"draft": _draft(), "room_median": _room_median()},
        {"packet_fingerprint": "p" * 64}, MEDIAN_SHA256, "round-1",
    )


@pytest.mark.parametrize("section, payload, code", [
    ("room", room_document(filters=[{"freq": NULL_HZ, "q": 1, "gain": 1}]), "boost_not_admitted"),
    ("driver", driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": 13}], {"packet_fingerprint": "p" * 64}), "driver_filter_boost_too_high"),
    ("bass", {**BASS_EXTENSION, "low_boost_db": 100}, "bass_extension_invalid"),
    ("topology", {}, "composition_topology_required"),
    ("blend", {"kind": "unknown"}, "prescription_kind_unknown"),
])
def test_one_invalid_section_refuses_whole_document(base, evidence, section, payload, code):
    with pytest.raises(PrescriptionDocumentRefused) as refused:
        judge_prescription_document(document(base.fingerprint, {"alignment": {}, section: payload}), base=base, evidence=evidence)
    answer = refused.value.to_dict()
    assert answer["ok"] is False
    assert (answer["code"], answer["section"]) == (code, section)
    assert {"ok", "code", "section", "next_action", "error"} <= answer.keys()


@pytest.mark.parametrize("field, value", [
    ("low_boost_db", 0), ("reference_level_db", 1), ("detector_lowpass_hz", 0),
    ("compressor_threshold_dbfs", 1), ("compressor_factor", 1),
    ("compressor_attack_s", 0), ("compressor_release_s", 0),
    ("delta_highpass_hz", 10000), ("low_boost_db", True),
])
def test_bass_contract_ranges_remain_enforced(base, field, value):
    with pytest.raises(PrescriptionDocumentRefused) as refused:
        judge_prescription_document(document(base.fingerprint, {"bass": {**BASS_EXTENSION, field: value}}), base=base)
    assert (refused.value.section, refused.value.code) == ("bass", "bass_extension_invalid")


@pytest.mark.parametrize("verb", ["judge", "compose"])
def test_cli_proves_without_writes_until_composition(base, bank, tmp_path, capsys, verb):
    raw = document(base.fingerprint, {"bass": BASS_EXTENSION})
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(raw))
    before = {p for p in tmp_path.rglob("*")}
    args = [verb, str(path), "--root", str(bank)]
    if verb == "compose":
        args += ["--base", base.fingerprint]
    assert crossover_prescriber.main(args) == 0
    answer = json.loads(capsys.readouterr().out)
    direct = compose_candidate(base, sections={"bass": validate_dynamic_bass_descriptor(BASS_EXTENSION)}, rationale=raw["rationale"], evidence={
        "packet_fingerprint": None,
        "contracts": contract_digests(prescription_contracts(candidate=base.candidate.to_dict())),
        "prescriptions": {"bass": BASS_EXTENSION},
    })
    assert answer["candidate_fingerprint"] == direct.fingerprint
    assert answer["resolution"]["bass"] == "document"
    if verb == "judge":
        assert {p for p in tmp_path.rglob("*")} == before
    else:
        assert find_banked_candidate(direct.fingerprint, root=bank).candidate.to_dict() == direct.to_dict()
    assert len(banked_candidates(root=bank)) == (1 if verb == "judge" else 2)


@pytest.mark.parametrize("verb", ["judge", "compose"])
def test_cli_refusal_banks_nothing(base, bank, tmp_path, capsys, verb):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document(base.fingerprint, {"room": {}, "bass": {**BASS_EXTENSION, "low_boost_db": 99}})))
    args = [verb, str(path), "--root", str(bank)]
    if verb == "compose":
        args += ["--base", base.fingerprint]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert crossover_prescriber.main(args) == 1
    answer = json.loads(capsys.readouterr().out)
    assert (answer["ok"], answer["section"], answer["code"]) == (False, "bass", "bass_extension_invalid")
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
@pytest.fixture
def saved_tune():

    topology = mono_output_topology()
    applied = deepcopy(_applied_profile(topology))
    snapshot = applied["recomposition_snapshot"]
    snapshot["corrections"] = {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.11, "inverted": False},
        "tweeter": {"gain_db": -10.8, "delay_ms": 0.0, "inverted": True},
    }
    snapshot["room_correction"] = _room_correction()
    snapshot["measured_candidate_fingerprint"] = None
    return topology, applied


@pytest.mark.parametrize("change, code", [
    ("topology", "composition_saved_tune_unavailable"),
    ("delay", "composition_saved_tune_unrepresentable"),
    ("protection", "composition_saved_tune_unrepresentable"),
])
def test_saved_candidate_refuses_unrepresentable_upstream_tune(saved_tune, change, code):
    topology, applied = saved_tune
    snapshot = applied["recomposition_snapshot"]
    if change == "topology":
        snapshot["topology_fingerprint"] = "old-hardware"
    elif change == "delay":
        snapshot["corrections"]["tweeter"]["delay_ms"] = 0.22
    else:
        snapshot["driver_protection"] = {"targets": [
            {"role": role, "target_fingerprint": role, "required_protection_filters": ([{
                "kind": "highpass", "cutoff_hz": 40,
                "minimum_slope_db_per_octave": 24,
            }] if role == "woofer" else [])}
            for role in ("woofer", "tweeter")
        ]}
    with pytest.raises(CandidateBankRefusal) as refused:
        candidate_from_applied_profile(topology, applied)
    assert refused.value.code == code


@pytest.mark.parametrize("base_kind", ["saved", "banked"])
def test_bass_compose_uses_saved_layers_without_reviving_old_candidate(bank, saved_tune, tmp_path, monkeypatch, capsys, base_kind):

    topology, applied = saved_tune
    original = deepcopy(applied)
    monkeypatch.setattr(crossover_prescriber, "load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(crossover_prescriber, "load_applied_baseline_profile_state", lambda: applied)
    bass = tmp_path / "bass.json"
    base = "saved" if base_kind == "saved" else publish_authored_candidate(
        candidate_from_applied_profile(topology, applied), root=bank,
    ).fingerprint
    bass.write_text(json.dumps(document(base, {"bass": BASS_EXTENSION})))
    assert crossover_prescriber.main([
        "compose", str(bass), "--root", str(bank), "--base", base,
    ]) == 0
    answer = json.loads(capsys.readouterr().out)
    child = find_banked_candidate(answer["candidate_fingerprint"], root=bank).candidate
    snapshot = applied["recomposition_snapshot"]
    assert driver_corrections(child) == snapshot["corrections"]
    assert child.alignment == MeasuredCrossoverAlignment(110, "woofer", "invert")
    assert child.room_correction == snapshot["room_correction"]
    assert child.analysis["measurement_status"] == "unmeasured"
    assert child.bass_extension["low_boost_db"] == BASS_EXTENSION["low_boost_db"]
    profile = MeasurementGraphProfile(
        ActiveSpeakerPreset.from_mapping(snapshot["preset"]), topology,
        {"woofer": 0, "tweeter": 1}, "null",
    )
    baseline = yaml.safe_load(compile_tuning_graph(profile, candidate=candidate_from_applied_profile(topology, applied, purpose="bass")))
    proposed = yaml.safe_load(compile_tuning_graph(profile, scope="candidate", candidate=child))
    assert validated_base_graph(proposed, child.bass_extension, (0,)) == baseline
    assert applied == original
    assert v2host.load_v2_state() is None


@pytest.mark.parametrize("change", [None, "bass_off", "speaker", "room"])
def test_composition_inherits_downstream_layers_until_explicitly_cleared(bank, change):

    base = replace(_candidate(room_correction=_room_correction()), bass_extension=BASS_EXTENSION)
    row = publish_authored_candidate(replace(base, analysis={"measurement_status": "unmeasured"}), root=bank)
    child = compose_candidate(
        row, sections=({"driver": {"role_attenuations_db": base.role_attenuations_db,
                                  "linearization": base.linearization}} if change == "speaker" else
                       {"room": base.room_correction} if change == "room" else
                       {"bass": None} if change == "bass_off" else {}),
    )
    assert bool(child.bass_extension) is (change != "bass_off")
    assert child.room_correction == base.room_correction
    assert child.linearization == base.linearization


@pytest.mark.parametrize("descriptor", [[], {"low_boost_db": 4}, {"low_boost_db": "bad"}])
def test_bass_compose_refuses_malformed_descriptor(bank, tmp_path, capsys, descriptor):
    base = _candidate()
    _publish(bank, base)
    path = tmp_path / "bass.json"
    path.write_text(json.dumps(document(base.fingerprint, {"bass": descriptor})))
    assert crossover_prescriber.main([
        "compose", str(path), "--root", str(bank), "--base", base.fingerprint,
    ]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["code"] in {"prescription_malformed", "bass_extension_invalid"}
    assert len(banked_candidates(root=bank)) == 1


@pytest.mark.parametrize("delay", [-100, 100])
def test_all_sections_form_one_proved_candidate(base, evidence, delay):

    sources = deepcopy(dict(evidence.sources))
    for target in sources["draft"]["driver_safety_profile"]["targets"]:
        target["target_fingerprint"] = target["role"]
    sources["receipt"] = _receipt()
    evidence = replace(evidence, sources=sources)
    sections = {
        "driver": driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": 2}], dict(evidence.packet)),
        "blend": blend_document([{"biquad_type": "Peaking", "freq": 1500, "q": 1, "gain": -1}], dict(evidence.packet)),
        "alignment": {"delay_us": delay, "basis_delay_us": 0, "basis_artifacts": ["alignment.json"]},
        "topology": {"fc_hz": 2000, "order": 4, "basis_artifacts": ["fc.json"]},
        "room": room_document(), "bass": BASS_EXTENSION,
    }
    child = judge_prescription_document(document(base.fingerprint, sections), base=base, evidence=evidence)
    assert set(child.analysis["resolution"].values()) == {"document"}
    assert child.alignment == MeasuredCrossoverAlignment(abs(delay), "woofer" if delay < 0 else "tweeter", "keep")
    assert candidate_topology(child)["fc_hz"] == 2000
    assert child.room_correction and child.bass_extension
    assert child.linearization["woofer"]["headroom_cost_db"] > 0
    emitted = compile_candidate_config(child, playback_device="null")
    prove_candidate_config(child, emitted)
    assert yaml.safe_load(emitted)["devices"]["volume_limit"] == 0.0


@pytest.mark.parametrize("section, payload, code", [
    ("alignment", {"delay_us": 300, "basis_delay_us": 0, "basis_artifacts": ["alignment.json"]}, "prescription_out_of_lobe"),
    ("topology", {"fc_hz": 2000, "order": 2, "basis_artifacts": ["fc.json"]}, "topology_slope_below_declared_requirement"),
])
def test_structural_bounds_use_proposed_topology(base, evidence, section, payload, code):
    sources = deepcopy(dict(evidence.sources))
    for target in sources["draft"]["driver_safety_profile"]["targets"]:
        target["target_fingerprint"] = target["role"]
        target["recommended_highpass_slope_db_per_octave"] = 24
    sections = {"topology": {"fc_hz": 2000, "order": 4, "basis_artifacts": ["fc.json"]}, section: payload}
    with pytest.raises(PrescriptionDocumentRefused) as refused:
        judge_prescription_document(document(base.fingerprint, sections), base=base, evidence=replace(evidence, sources=sources))
    assert (refused.value.section, refused.value.code) == (section, code)


def test_whole_graph_proof_refusal_banks_nothing(base, bank, tmp_path, monkeypatch, capsys):

    def refuse(candidate, config):
        raise MeasuredCrossoverCandidateError("composed_graph_invalid", "test proof refusal")

    monkeypatch.setattr(candidate_parts, "prove_candidate_config", refuse)
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document(base.fingerprint)))
    assert crossover_prescriber.main(["compose", str(path), "--base", base.fingerprint, "--root", str(bank)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "composed_graph_invalid"
    assert len(banked_candidates(root=bank)) == 1


@pytest.mark.parametrize("diameter", [None, 200.0])
def test_cli_round_evidence_judges_and_banks_one_combined_document(base, bank, tmp_path, capsys, round_bank, diameter):

    round_dir, _ = round_bank
    draft_path = round_dir / "design-draft.json"
    draft = json.loads(draft_path.read_text())
    draft["manual_settings"] = {"drivers": [{"role": "woofer", "radiating_diameter_mm": diameter}]}
    draft_path.write_text(json.dumps(draft))
    args = crossover_prescriber.build_parser().parse_args(["status", str(round_dir)])
    packet = crossover_prescriber._load_packet(args)
    raw = document(base.fingerprint, {
        "driver": driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": -2}], packet),
        "room": room_document(sha256=room_median_sha256(json.loads((round_dir / "room.json").read_text())["median"])),
        "alignment": {"delay_us": 100, "basis_delay_us": 0, "basis_artifacts": ["alignment.json"]},
        "topology": {"fc_hz": 2000, "order": 4, "basis_artifacts": ["fc.json"]},
    })
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(raw))
    assert crossover_prescriber.main(["judge", str(path), "--round", str(round_dir), "--root", str(bank)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert set(preview["sections"]) == set(raw["sections"])
    assert "displaced_filters" in preview["sections"]["driver"]
    ceiling = preview["sections"]["topology"]["beaming_ceiling_hz"]
    assert ceiling == (None if diameter is None else pytest.approx(beaming_onset_hz(diameter)))
    assert len(banked_candidates(root=bank)) == 1
    assert crossover_prescriber.main(["compose", str(path), "--base", base.fingerprint, "--round", str(round_dir), "--root", str(bank)]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["candidate_fingerprint"] == preview["candidate_fingerprint"]
    child = find_banked_candidate(answer["candidate_fingerprint"], root=bank).candidate
    assert child.analysis["evidence"]["packet_fingerprint"] == packet["packet_fingerprint"]
    assert child.bass_extension == base.candidate.bass_extension
    assert child.analysis["evidence"]["prescriptions"] == preview["sections"]
    assert child.analysis["room_source"]["prescription_sha256"] == prescription_sha256(contract_json(preview["sections"]["room"]).encode())
    assert len(banked_candidates(root=bank)) == 2


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10 ** 400])
def test_driver_numeric_refusals_keep_the_judges_code(base, evidence, value):
    raw = driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": value}], dict(evidence.packet))
    with pytest.raises(PrescriptionDocumentRefused) as refused:
        judge_prescription_document(document(base.fingerprint, {"driver": raw}), base=base, evidence=evidence)
    assert (refused.value.section, refused.value.code) == ("driver", "driver_filter_malformed")


@pytest.mark.parametrize("base_choice", ["saved", "banked"])
def test_saved_base_preview_and_invalid_composition_never_bank_a_base(bank, saved_tune, tmp_path, monkeypatch, capsys, base_choice):
    topology, applied = saved_tune
    monkeypatch.setattr(crossover_prescriber, "load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(crossover_prescriber, "load_applied_baseline_profile_state", lambda: applied)
    base_name = "saved" if base_choice == "saved" else publish_authored_candidate(candidate_from_applied_profile(topology, applied), root=bank).fingerprint
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document(base_name)))
    count = len(banked_candidates(root=bank))
    assert crossover_prescriber.main(["judge", str(path), "--root", str(bank)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    path.write_text(json.dumps(document(base_name, {"bass": {"low_boost_db": 99}})))
    assert crossover_prescriber.main(["compose", str(path), "--base", base_name, "--root", str(bank)]) == 1
    assert json.loads(capsys.readouterr().out)["section"] == "bass"
    assert len(banked_candidates(root=bank)) == count


@pytest.mark.parametrize("explicit_envelope", [False, True])
def test_room_digest_names_judged_envelope_and_inheritance_drops_stale_match(base, bank, evidence, explicit_envelope):
    section = room_document()
    section.pop("rationale", None)
    if not explicit_envelope:
        for key in ("kind", "artifact_schema_version"):
            section.pop(key)
    raw = document(base.fingerprint, {"room": section})
    child = judge_prescription_document(raw, base=base, evidence=evidence)
    judged = child.analysis["evidence"]["prescriptions"]["room"]
    source = child.analysis["room_source"]
    assert source["prescription_sha256"] == prescription_sha256(contract_json(judged).encode())
    assert judged["rationale"] == raw["rationale"]
    assert "base_match" not in source
    changed = judge_prescription_document({**raw, "rationale": "A different reason."}, base=base, evidence=evidence)
    assert changed.analysis["room_source"]["prescription_sha256"] != source["prescription_sha256"]
    legacy = replace(child, analysis={**child.analysis, "room_source": {**source, "base_match": "match"}})
    row = publish_authored_candidate(legacy, root=bank)
    inherited = judge_prescription_document(document(row.fingerprint, {"driver": None}), base=row)
    assert inherited.analysis["resolution"]["room"] == "base"
    assert inherited.room_correction == child.room_correction
    assert inherited.analysis["room_source"] == source
