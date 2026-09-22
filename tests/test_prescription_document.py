# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A document is judged and proved as one candidate before the bank is written."""
from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

import json
from copy import deepcopy
from dataclasses import replace
from itertools import product
from math import prod

import pytest
from tests.test_prescription_contract import round_bank as round_bank
from tests.test_prescription_contract import bass_packet as bass_packet
from tests.test_active_speaker_audition import _applied_profile
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.profile import ActiveSpeakerPreset, SIDES_BY_LAYOUT
from jasper.active_speaker.preset_binding import build_passive_mains_preset
from jasper.bass_extension.dynamic_graph import validated_base_graph
from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION
from tests.test_crossover_v2_blend_prescription import _receipt, _document as blend_document
from jasper.active_speaker.crossover_v2.topology_prescription import candidate_topology
from jasper.active_speaker.measured_crossover_candidate import compile_candidate_config, prove_candidate_config
from jasper.active_speaker.branch_chain import beaming_onset_hz
from jasper.active_speaker import candidate_parts
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError,
)
from jasper.active_speaker.crossover_v2.blend_prescription import prescription_sha256
from jasper.active_speaker.crossover_v2.room_views import room_median_sha256
from jasper.active_speaker.crossover_v2 import room_prescription
import yaml

from jasper.active_speaker.candidate_bank import CandidateBankRefusal, banked_candidates, find_banked_candidate, publish_authored_candidate
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile, compose_candidate
from jasper.active_speaker.crossover_v2.prescription_contract import contract_digests, contract_json, contract_programs, prescription_contracts
from jasper.active_speaker.crossover_v2.round_inputs import prescription_sources, round_inputs
from jasper.active_speaker.crossover_v2 import prescription_document as prescription_document_mod
from jasper.active_speaker.crossover_v2.prescription_document import (
    PrescriptionDocumentRefused, PrescriptionEvidence, judge_prescription_document,
    parse_vary_axis, vary_document,
)
from jasper.active_speaker.crossover_v2.bass_prescription import BASS_PRESCRIPTION_REFUSAL_REASONS
from jasper.active_speaker.round_packet import write_round_packet
from tests.run_manifest_fixture import write_manifest
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverAlignment, driver_corrections
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.cli import crossover_prescriber
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_measured_crossover_candidate import (
    _acoustic_rear_document, _candidate, _rear_document, _room_correction,
)
from tests.test_rear_output_foundation import _rear_pair
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


@pytest.mark.parametrize("text,paths,values", [
    ("room.filters[0].gain,room.filters[1].gain=3,4", ("room.filters[0].gain", "room.filters[1].gain"), (3, 4)),
    ('room.value=-3.8,true,false,null,"label",plain,a=b', ("room.value",), (-3.8, True, False, None, "label", "plain", "a=b")),
])
def test_vary_axis_parses_coupled_paths_and_scalar_values(text, paths, values):
    parsed_paths, parsed_values = parse_vary_axis(text)
    assert parsed_paths == paths and parsed_values == values
    assert [type(value) for value in parsed_values] == [type(value) for value in values]


@pytest.mark.parametrize("text", ["room.gain", "=1", "room.gain=", "room.gain,=1", "room.gain=[]", "room.gain={}",
                                 "room.gain=1,,2", "room.gain=,"])
def test_malformed_vary_axis_is_refused(text):
    with pytest.raises(PrescriptionDocumentRefused) as caught:
        parse_vary_axis(text)
    assert (caught.value.code, caught.value.section) == ("prescription_malformed", "sections")


def test_vary_document_copies_the_seed_and_expands_axes_in_product_order():
    seed = document("saved", {"room": {"filters": [{"gain": 0}, {"gain": 0}], "enabled": False,
                                       "assumptions": ["Keep this text."]}})
    before = deepcopy(seed)
    axes = [parse_vary_axis(text) for text in (
        "room.filters[0].gain,room.filters[1].gain=1,2,3", "room.enabled=true,false")]
    variants = list(vary_document(seed, axes))
    assert len(variants) == prod(len(values) for _, values in axes)
    for (values, variant), (gain, enabled) in zip(variants, product((1, 2, 3), (True, False))):
        assert values == {"room.filters[0].gain": gain, "room.filters[1].gain": gain, "room.enabled": enabled}
        assert variant["sections"]["room"] == {**before["sections"]["room"], "filters": [{"gain": gain}] * 2, "enabled": enabled}
        assert variant["rationale"] == before["rationale"]
    variants[0][1]["sections"]["room"]["assumptions"].append("Changed")
    assert variants[1][1]["sections"]["room"]["assumptions"] == before["sections"]["room"]["assumptions"]
    assert seed == before


@pytest.mark.parametrize("path,section", [
    ("missing.gain", "sections"), ("room.missing", "room"), ("room.filters[1].gain", "room"),
    ("room.filters[-1].gain", "room"), ("room.filters[0].gain.value", "room"), ("room.filters.gain", "room"),
    ("room.filters[0].gain[0]", "room"), ("room..gain", "room"),
    ("room.filters[0].gain", "room"), ("room.filters,room.filters", "room"),
])
def test_vary_document_checks_all_paths_before_yielding(path, section):
    seed = document("saved", {"room": {"filters": [{"gain": 0}]}})
    variants = vary_document(seed, [parse_vary_axis("room.filters[0].gain=1,2"), parse_vary_axis(f"{path}=3")])
    with pytest.raises(PrescriptionDocumentRefused) as caught:
        list(variants)
    assert (caught.value.code, caught.value.section) == ("prescription_malformed", section)
    assert seed["sections"]["room"]["filters"] == [{"gain": 0}]


def test_room_grid_preserves_the_full_preview(base, bank, evidence, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(crossover_prescriber, "_document_evidence", lambda *args: evidence)
    seed = tmp_path / "room.json"
    prescription = document(base.fingerprint, {"room": room_document(filters=[{"freq": 277, "q": 1, "gain": -3}])})
    seed.write_text(json.dumps(prescription))
    args = ["judge", "--preview", str(seed), "--root", str(bank)]
    assert crossover_prescriber.main(args) == 0
    single = json.loads(capsys.readouterr().out)
    assert single["sections"] == ["room"]
    assert single["preview"] == room_prescription.preview_room_prescription(
        {"rationale": prescription["rationale"], **prescription["sections"]["room"]},
        room_median=room_prescription.read_room_median(evidence.sources["room_median"]),
        room_median_sha256=evidence.room_median_sha256, round_id=evidence.round_id,
        sides=SIDES_BY_LAYOUT[base.candidate.source_preset.channel_map.layout],
    )
    out_dir = tmp_path / "variants"
    assert crossover_prescriber.main([*args, "--vary", "room.sides.mono[0].gain=-3,-6", "--out-dir", str(out_dir)]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["section"] == "room" and len(answer["variants"]) == 2
    assert answer["variants"][0]["preview"] == single["preview"]
    assert all("positions" not in row for row in answer["variants"])
    assert json.loads((out_dir / "variant-01.preview.json").read_text()) == single


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
def evidence(bass_packet):
    return PrescriptionEvidence(
        {"draft": _draft(), "room_median": _room_median(), "bass_evidence": bass_packet,
         "manifest": {"sets": [{"set_id": "base", "capture_basis": {},
                                "takes": [{"selected": True, "level": {"level_db": -21.09}}]}]}},
        {"packet_fingerprint": "p" * 64}, MEDIAN_SHA256, bass_packet["round_id"],
    )


def timing_evidence(base, *, saved=None, verdict="measured", axis=0):
    return PrescriptionEvidence(sources={"applied_profile": {"timing": saved} if saved else None,
        "bass_evidence": {}, "manifest": {"run_id": "capture-run", "sets": [{
            "set_id": "base", "base": True, "capture_basis": {"candidate_id": base.fingerprint, "graph_fingerprint": "graph"},
            "takes": [{"take_id": "t2", "selected": True, "phase": "measure",
                "pose": {"kind": "bearing", "deg": axis, "elevation_deg": 0}, "artifacts": {"record_id": "read.json"},
                "analysis": {"timing_verdict": verdict, "alignment_status": "ok", "delay_us": -37.5, "polarity": "inverted",
                    "trim_db": {"woofer": 0, "tweeter": -9}, "margin_db": .6, "residual_rms_db": .2,
                    "repeat_spread_db": .1, "repeat_spread_us": 2, "repeat_count": 3}}]}]}}, round_id="r1")


@pytest.mark.parametrize("source,saved,verdict,axis,has_round,base_name", [
    ("document", True, "measured", 0, True, "banked"), ("cleared", True, "measured", 0, True, "banked"),
    ("saved", True, "measured", 0, True, "banked"), ("saved", True, "measured", 0, False, "banked"),
    ("saved", True, "measured", 0, True, "saved"),
    ("measured", False, "measured", 0, True, "banked"), ("base", False, "needs_measurement", 0, True, "banked"),
    ("base", False, "measured", 30, True, "banked"), ("base", False, "measured", 0, False, "banked"),
])
def test_document_resolves_timing_once(base, monkeypatch, source, saved, verdict, axis, has_round, base_name):
    record = {"delay_us": 22, "polarity": "normal", "provenance": "set_by_user"} if saved else None
    evidence = timing_evidence(base, saved=record, verdict=verdict, axis=axis)
    if not has_round or base_name == "saved":
        monkeypatch.setattr("jasper.active_speaker.crossover_v2.prescription_document.load_applied_baseline_profile_state",
                            lambda: {"timing": record} if record else None)
    if base_name == "saved":
        evidence = replace(evidence, sources={**evidence.sources, "applied_profile": None})
    if not has_round:
        evidence = replace(evidence, round_id="")
    sections = {"alignment": {"delay_us": 100, "basis_delay_us": 0, "basis_artifacts": ["alignment.json"]}} if source == "document" else {"alignment": {}} if source == "cleared" else {}
    child = judge_prescription_document(document("saved" if base_name == "saved" else base.fingerprint, sections), base=base, evidence=evidence)
    assert child.analysis["resolution"]["alignment"] == source
    assert child.alignment == {"document": MeasuredCrossoverAlignment(100, "tweeter", "keep"),
        "cleared": MeasuredCrossoverAlignment(), "saved": MeasuredCrossoverAlignment(22, "tweeter", "keep"),
        "measured": MeasuredCrossoverAlignment(37.5, "woofer", "invert"), "base": base.candidate.alignment}[source]
    if source == "measured":
        read = child.analysis["evidence"]["commissioning"]["alignment"]
        assert {key: read[key] for key in ("round_id", "take_id", "graph_fingerprint", "margin_db", "residual_rms_db",
                                         "repeat_spread_db", "repeat_spread_us", "repeat_count")} == {
            "round_id": "r1", "take_id": "t2", "graph_fingerprint": "graph", "margin_db": .6, "residual_rms_db": .2,
            "repeat_spread_db": .1, "repeat_spread_us": 2, "repeat_count": 3}


@pytest.mark.parametrize("source,verdict,drift", [
    ("measured", "measured", {}), ("measured", None, {}), ("base", "measured", {}),
    ("base", None, {}), ("base", "needs_measurement", {}),
    ("measured", "measured", {"delay_us": 38.5}),
    ("measured", "measured", {"delay_role": "tweeter"}),
    ("measured", "measured", {"polarity": "keep"}),
])
def test_room_document_carries_only_unchanged_measured_timing(base, bank, evidence, source, verdict, drift):
    measured = judge_prescription_document(document(base.fingerprint), base=base, evidence=timing_evidence(base))
    analysis = deepcopy(measured.analysis)
    analysis["resolution"]["alignment"] = source
    read = analysis["evidence"]["commissioning"]["alignment"]
    if verdict is None:
        read.pop("timing_verdict")
        if source == "base":
            analysis["evidence"].pop("commissioning")
    else:
        read["timing_verdict"] = verdict
    parent = publish_authored_candidate(replace(measured, analysis=analysis,
        alignment=replace(measured.alignment, **drift)), root=bank)
    before = deepcopy(parent.candidate.to_dict())
    child = judge_prescription_document(document(parent.fingerprint, {"room": room_document()}),
                                       base=parent, evidence=evidence)
    carried = not drift and (source == "measured" or verdict == "measured")
    assert child.analysis["resolution"]["alignment"] == ("measured" if carried else "base")
    assert child.alignment == parent.candidate.alignment
    assert child.analysis["measurement_status"] == "unmeasured"
    assert child.analysis["evidence"]["packet_fingerprint"] == evidence.packet["packet_fingerprint"]
    assert child.analysis["resolution"]["room"] == "document"
    if carried:
        assert child.analysis["evidence"]["commissioning"]["alignment"] == {**read, "timing_verdict": "measured"}
        child_base = publish_authored_candidate(child, root=bank)
        grandchild = judge_prescription_document(document(child_base.fingerprint, {"room": room_document()}),
                                                base=child_base, evidence=evidence)
        assert grandchild.analysis["evidence"]["commissioning"]["alignment"]["timing_verdict"] == "measured"
        assert grandchild.analysis["resolution"]["alignment"] == "measured"
    else:
        assert "commissioning" not in child.analysis["evidence"]
    assert parent.candidate.to_dict() == before


def bass_document(packet):
    return {**BASS_EXTENSION, "round_id": packet["round_id"]}


@pytest.mark.parametrize("pin", [{}, {"tweeter": -9.52}])
def test_empty_driver_chain_clears_all_roles_and_keeps_trim_context(base, bank, evidence, pin):
    base = publish_authored_candidate(replace(base.candidate, linearization={
        role: {"filters": [{"biquad_type": "Peaking", "freq": freq, "q": 1, "gain": -1}
                           for freq in freqs]}
        for role, freqs in (("woofer", (500, 700)), ("tweeter", (3000, 4000, 5000)))
    }), root=bank)
    evidence = replace(evidence, packet={**evidence.packet, "incumbent": {"linearization": {
        "from_applied_profile": {role: entry["filters"] for role, entry in base.candidate.linearization.items()},
    }}})
    raw = driver_document([], dict(evidence.packet), pinned_trim_db=pin)
    child = judge_prescription_document(document(base.fingerprint, {"driver": raw}), base=base, evidence=evidence)
    assert child.analysis["resolution"]["driver"] == "document"
    assert child.analysis["evidence"]["prescriptions"]["driver"]["displaced_filters"] == 5
    assert child.linearization == {}
    assert child.role_attenuations_db == {**base.candidate.role_attenuations_db, **pin}
    assert child.role_attenuations_db["woofer"] == 0.0


@pytest.fixture
def bass_round(round_bank, bass_packet):
    directory, _ = round_bank
    bass_packet["round_id"] = directory.name
    (directory / "packet.json").write_text(json.dumps(bass_packet))
    return directory


@pytest.mark.parametrize("section, payload, code", [
    ("room", room_document(filters=[{"freq": NULL_HZ, "q": 1, "gain": 1}]), "boost_not_admitted"),
    ("driver", driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": 50}], {"packet_fingerprint": "p" * 64}), "driver_composed_boost_exceeded"),
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


@pytest.mark.parametrize("change, code", [
    ("no_round", "bass_evidence_unavailable"),
    ("no_bass", "bass_evidence_unavailable"),
    ({"round_id": "wrong"}, "bass_round_mismatch"),
    ("packet_round_mismatch", "bass_evidence_unavailable"),
    ("no_round_id", "bass_evidence_unavailable"),
    ("missing_field", "bass_descriptor_malformed"),
    ({"unknown": 1}, "bass_descriptor_malformed"),
    ({"low_boost_db": 0}, "bass_low_boost_db_invalid"),
    ({"delta_highpass_hz": 10000}, "bass_delta_highpass_hz_invalid"),
    ({"low_boost_db": True}, "bass_low_boost_db_invalid"),
])
def test_bass_refusals_keep_the_evidence_pin_and_field_codes(base, evidence, bass_packet, round_bank, change, code):
    section = {**bass_document(bass_packet), "delta_highpass_hz": 25, "detector_lowpass_hz": 100}
    if isinstance(change, dict):
        section.update(change)
    elif change == "missing_field":
        del section["low_boost_db"]
    elif change == "packet_round_mismatch":
        (round_bank[0] / "packet.json").write_text(json.dumps({**bass_packet, "round_id": "wrong"}))
        evidence = replace(evidence, sources=prescription_sources(round_inputs(round_bank[0])))
    elif change == "no_round_id":
        del bass_packet["round_id"]
    elif change == "no_round":
        evidence = None
    elif change == "no_bass":
        evidence = replace(evidence, sources={"bass_evidence": {"round_id": bass_packet["round_id"]}})
    with pytest.raises(PrescriptionDocumentRefused) as refused:
        judge_prescription_document(document(base.fingerprint, {"bass": section}), base=base, evidence=evidence)
    answer = refused.value.to_dict()
    assert (answer["section"], answer["code"]) == ("bass", code)
    assert code in BASS_PRESCRIPTION_REFUSAL_REASONS
    assert {"ok", "code", "section", "next_action", "error", "evidence"} <= answer.keys()


@pytest.mark.parametrize("verb", ["judge", "compose"])
def test_cli_proves_without_writes_until_composition(base, bank, tmp_path, capsys, bass_round, bass_packet, verb):
    raw = document(base.fingerprint, {"bass": bass_document(bass_packet)})
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(raw))
    before = {p for p in tmp_path.rglob("*")}
    args = [verb, str(path), "--root", str(bank), "--round", str(bass_round)]
    assert crossover_prescriber.main(args) == 0
    answer = json.loads(capsys.readouterr().out)
    assert not {"status", "ok", "code", "error"} & answer.keys()
    descriptor = validate_dynamic_bass_descriptor(BASS_EXTENSION)
    receipt = {**descriptor, "round_id": bass_packet["round_id"], "evidence_status": "evaluated",
               "unqualified_boost_bands_hz": []}
    sources = {**prescription_sources(round_inputs(bass_round)), "candidate": base.candidate.to_dict()}
    contracts = prescription_contracts(programs=contract_programs(sources), **sources)
    direct = compose_candidate(base, sections={"bass": descriptor}, rationale=raw["rationale"], evidence={
        "packet_fingerprint": None, "contracts": contract_digests(contracts), "prescriptions": {"bass": receipt},
    })
    assert answer["candidate_fingerprint"] == direct.fingerprint
    assert answer["resolution"]["bass"] == "document"
    if verb == "judge":
        assert {p for p in tmp_path.rglob("*")} == before
        assert answer["sections"]["bass"] == receipt
    else:
        assert find_banked_candidate(direct.fingerprint, root=bank).candidate.to_dict() == direct.to_dict()
    assert len(banked_candidates(root=bank)) == (1 if verb == "judge" else 2)


def test_bass_below_qualified_floor_is_disclosed_by_judge_and_packet(base, bank, evidence, bass_packet, bass_round, round_bank, tmp_path, capsys):
    for band in bass_packet["bass"][0]["takes"][0]["bands"]:
        band["fundamental_qualified"] = band["band_hz"][0] >= 63
    section = {**bass_document(bass_packet), "delta_highpass_hz": 25, "detector_lowpass_hz": 100}
    raw = document(base.fingerprint, {"bass": section})
    child = judge_prescription_document(raw, base=base, evidence=evidence)
    receipt = child.analysis["evidence"]["prescriptions"]["bass"]
    assert receipt["unqualified_boost_bands_hz"] == [[20, 30], [30, 40], [40, 50], [50, 63]]
    assert receipt["evidence_status"] == "evaluated"
    (bass_round / "packet.json").write_text(json.dumps(bass_packet))
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(raw))
    assert crossover_prescriber.main(["judge", str(path), "--root", str(bank), "--round", str(bass_round)]) == 0
    assert json.loads(capsys.readouterr().out)["sections"]["bass"] == receipt
    directory = round_bank[1] / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    (directory / "candidate.json").write_text(json.dumps(child.to_dict()))
    write_manifest(bass_round, program="bass")
    packet = write_round_packet(bass_round, str(directory / "run_manifest.json"), [])
    assert packet["prescriptions"]["bass"] == receipt


@pytest.mark.parametrize("corner, highpass, unqualified", [(20, None, [[20, 30]]), (80, None, [[20, 30]]), (120, 40, [])])
def test_bass_qualification_uses_boost_bands_and_any_qualified_take(base, evidence, bass_packet, corner, highpass, unqualified):
    takes = bass_packet["bass"][0]["takes"]
    takes.append(deepcopy(takes[0]))
    for band in takes[0]["bands"]:
        band["fundamental_qualified"] = False
    for band in takes[1]["bands"]:
        if band["band_hz"][0] == 20 or band["band_hz"][0] >= max(30, corner) or band["band_hz"][1] <= (highpass or 0):
            band["fundamental_qualified"] = False
    section = {**bass_document(bass_packet), "detector_lowpass_hz": corner, "delta_highpass_hz": highpass}
    child = judge_prescription_document(document(base.fingerprint, {"bass": section}), base=base, evidence=evidence)
    assert child.analysis["evidence"]["prescriptions"]["bass"]["unqualified_boost_bands_hz"] == unqualified


@pytest.mark.parametrize("verb", ["judge", "compose"])
def test_cli_refusal_banks_nothing(base, bank, tmp_path, capsys, verb):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document(base.fingerprint, {"room": {}, "bass": {**BASS_EXTENSION, "low_boost_db": 99}})))
    args = [verb, str(path), "--root", str(bank)]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert crossover_prescriber.main(args) == 1
    answer = json.loads(capsys.readouterr().out)
    assert (answer["status"], answer["detail"]["section"], answer["code"]) == ("refused", "bass", "bass_low_boost_db_invalid")
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
@pytest.fixture
def saved_tune(bank):

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


def test_saved_candidate_refuses_unrepresentable_delay(saved_tune):
    topology, applied = saved_tune
    applied["recomposition_snapshot"]["corrections"]["tweeter"]["delay_ms"] = 0.22
    with pytest.raises(CandidateBankRefusal) as refused:
        candidate_from_applied_profile(topology, applied)
    assert refused.value.code == "composition_saved_tune_unrepresentable"


def test_saved_candidate_migration_preserves_driver_attenuations(saved_tune):
    topology, applied = saved_tune
    candidate = candidate_from_applied_profile(topology, applied)
    assert candidate.role_attenuations_db["tweeter"] == applied["recomposition_snapshot"]["corrections"]["tweeter"]["gain_db"]


@pytest.mark.parametrize("base_kind", ["saved", "banked"])
def test_bass_compose_uses_saved_layers_without_reviving_old_candidate(bank, saved_tune, tmp_path, monkeypatch, capsys, bass_round, bass_packet, base_kind):

    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    topology, applied = saved_tune
    original = deepcopy(applied)
    monkeypatch.setattr("jasper.output_topology_store.load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(prescription_document_mod, "load_applied_baseline_profile_state", lambda: applied)
    bass = tmp_path / "bass.json"
    base = "saved" if base_kind == "saved" else publish_authored_candidate(
        candidate_from_applied_profile(topology, applied), root=bank,
    ).fingerprint
    bass.write_text(json.dumps(document(base, {"bass": bass_document(bass_packet)})))
    assert crossover_prescriber.main([
        "compose", str(bass), "--root", str(bank), "--round", str(bass_round),
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
    baseline = yaml.safe_load(compile_tuning_graph(profile, candidate=candidate_from_applied_profile(topology, applied)))
    proposed = yaml.safe_load(compile_tuning_graph(profile, scope="candidate", candidate=child))
    assert validated_base_graph(proposed, child.bass_extension, (0,)) == baseline
    assert applied == original
    assert v2state.load_v2_state() is None
    v2state.set_state_path_for_tests(None)


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


@pytest.mark.parametrize("descriptor, code", [([], "prescription_malformed"), ({"low_boost_db": 4}, "bass_descriptor_malformed")])
def test_bass_compose_refuses_malformed_descriptor(bank, tmp_path, capsys, descriptor, code):
    base = _candidate()
    _publish(bank, base)
    path = tmp_path / "bass.json"
    path.write_text(json.dumps(document(base.fingerprint, {"bass": descriptor})))
    assert crossover_prescriber.main([
        "compose", str(path), "--root", str(bank),
    ]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["code"] == code
    assert len(banked_candidates(root=bank)) == 1


@pytest.mark.parametrize("delay", [-100, 100])
def test_all_sections_form_one_proved_candidate(base, evidence, bass_packet, delay):

    sources = deepcopy(dict(evidence.sources))
    sources["receipt"] = _receipt()
    evidence = replace(evidence, sources=sources)
    sections = {
        "driver": driver_document([{"role": "woofer", "biquad_type": "Peaking", "freq": 900, "q": 1, "gain": 2}], dict(evidence.packet)),
        "blend": blend_document([{"biquad_type": "Peaking", "freq": 1500, "q": 1, "gain": -1}], dict(evidence.packet)),
        "alignment": {"delay_us": delay, "basis_delay_us": 0, "basis_artifacts": ["alignment.json"]},
        "topology": {"fc_hz": 2000, "order": 4, "basis_artifacts": ["fc.json"]},
        "room": room_document(), "bass": bass_document(bass_packet),
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
    tweeter = sources["draft"]["manual_settings"]["drivers"][1]
    tweeter["recommended_highpass_hz"] = 1600
    tweeter["recommended_highpass_slope_db_per_octave"] = 24
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
    assert crossover_prescriber.main(["compose", str(path), "--root", str(bank)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "composed_graph_invalid"
    assert len(banked_candidates(root=bank)) == 1


@pytest.mark.parametrize("diameter", [None, 200.0])
def test_cli_round_evidence_judges_and_banks_one_combined_document(base, bank, tmp_path, capsys, round_bank, bass_round, bass_packet, diameter):

    round_dir, _ = round_bank
    draft_path = round_dir / "design-draft.json"
    draft = json.loads(draft_path.read_text())
    draft["manual_settings"]["drivers"][0]["radiating_diameter_mm"] = diameter
    draft_path.write_text(json.dumps(draft))
    args = crossover_prescriber.build_parser().parse_args(["status", str(round_dir)])
    packet = crossover_prescriber._load_packet(args)
    raw = document(base.fingerprint, {
        "bass": bass_document(bass_packet),
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
    assert crossover_prescriber.main(["compose", str(path), "--round", str(round_dir), "--root", str(bank)]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["candidate_fingerprint"] == preview["candidate_fingerprint"]
    child = find_banked_candidate(answer["candidate_fingerprint"], root=bank).candidate
    assert child.analysis["evidence"]["packet_fingerprint"] == packet["packet_fingerprint"]
    assert preview["sections"]["room"]["round_id"] == round_dir.name
    assert preview["sections"]["bass"]["round_id"] == bass_round.name
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
def test_saved_base_preview_migrates_once_and_invalid_composition_banks_no_child(bank, saved_tune, tmp_path, monkeypatch, capsys, base_choice):
    topology, applied = saved_tune
    monkeypatch.setattr("jasper.output_topology_store.load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(prescription_document_mod, "load_applied_baseline_profile_state", lambda: applied)
    base_name = "saved" if base_choice == "saved" else publish_authored_candidate(candidate_from_applied_profile(topology, applied), root=bank).fingerprint
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document(base_name)))
    assert crossover_prescriber.main(["judge", str(path), "--root", str(bank)]) == 0
    assert not {"status", "ok", "code", "error"} & json.loads(capsys.readouterr().out).keys()
    path.write_text(json.dumps(document(base_name, {"bass": {"low_boost_db": 99}})))
    assert crossover_prescriber.main(["compose", str(path), "--root", str(bank)]) == 1
    assert json.loads(capsys.readouterr().out)["detail"]["section"] == "bass"
    assert len(banked_candidates(root=bank)) == 1


def test_a_saved_base_judge_reads_the_applied_profile_state_once(saved_tune, monkeypatch):
    """The base candidate and the composition both need that state; one read
    serves both, so a judge cannot see two different files."""
    topology, applied = saved_tune
    reads = []
    monkeypatch.setattr("jasper.output_topology_store.load_output_topology_strict", lambda: topology)
    monkeypatch.setattr(prescription_document_mod, "load_applied_baseline_profile_state",
                        lambda: (reads.append(1), applied)[1])

    prescription_document_mod.bank_section("driver", None, rationale="Read the state once.")

    assert len(reads) == 1


@pytest.mark.parametrize("explicit_envelope", [False, True])
@pytest.mark.parametrize("passive", [False, True])
def test_room_digest_names_judged_envelope_and_inheritance_drops_stale_match(base, bank, evidence, explicit_envelope, passive):
    if passive:
        box = mono_output_topology(mode="full_range_passive")
        preset, _, _ = build_passive_mains_preset(box)
        base = publish_authored_candidate(MeasuredCrossoverCandidate(
            program_id="passive", analysis={"measurement_status": "unmeasured"},
            source_preset=preset, role_attenuations_db={"full_range": 0.0},
        ), root=bank)
        evidence = replace(evidence, sources={**evidence.sources, "draft": {"topology": box.to_dict()}})
    section = room_document()
    section.pop("rationale", None)
    if not explicit_envelope:
        for key in ("kind", "artifact_schema_version"):
            section.pop(key)
    raw = document(base.fingerprint, {"room": section})
    child = judge_prescription_document(raw, base=base, evidence=evidence)
    assert ("speaker" in child.analysis["evidence"]["contracts"]) is not passive
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


@pytest.mark.parametrize("gain,trim,expected_spend", [(6.0, -9.52, 0.0), (39.0, 0.0, 40.0)])
def test_driver_door_prices_the_resolved_program(base, bank, evidence, gain, trim, expected_spend):
    from tests.test_active_speaker_measured_crossover_candidate import _room_basis

    boosted_room = _room_correction(
        sides={"mono": [{"freq": 100.0, "q": 2.0, "gain": 2.0}]},
        basis=_room_basis(admitted_boosts_hz=[100.0]), boost_db_total=2.0, level_cost_db=2.0,
    )
    base = publish_authored_candidate(replace(base.candidate, room_correction=boosted_room), root=bank)
    section = driver_document([
        {"role": "tweeter", "biquad_type": "Peaking", "freq": 12000.0, "q": 1.0, "gain": gain},
    ], dict(evidence.packet), pinned_trim_db={"tweeter": trim})
    child = judge_prescription_document(document(base.fingerprint, {"driver": section, "room": {}}),
                                        base=base, evidence=evidence)
    text = compile_candidate_config(child, playback_device="null")
    prove_candidate_config(child, text)
    assert child.linearization["tweeter"]["filters"][0]["gain"] == gain
    spent = -yaml.safe_load(text)["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    assert spent == pytest.approx(expected_spend, abs=0.01)


@pytest.fixture
def rear_base(bank):
    return publish_authored_candidate(replace(
        _candidate(preset=_rear_pair("mono")[0], trims={"woofer": 0.0, "tweeter": -3.5}),
        analysis={"measurement_status": "unmeasured"},
    ), root=bank)


@pytest.mark.parametrize("base_fixture,code", [
    ("base", "prescription_section_unavailable"), ("rear_base", None),
])
def test_a_rear_calibration_section_composes_into_the_banked_candidate(request, base_fixture, code):
    base = request.getfixturevalue(base_fixture)
    section = _rear_document()
    raw = document(base.fingerprint, {"rear_calibration": section})
    if code is not None:
        with pytest.raises(PrescriptionDocumentRefused) as caught:
            judge_prescription_document(raw, base=base)
        assert (caught.value.code, caught.value.section) == (code, "rear_calibration")
        return

    fitted = judge_prescription_document(raw, base=base)
    inherited = judge_prescription_document(document(base.fingerprint), base=base)

    assert fitted.rear_calibration == section
    assert fitted.analysis["resolution"]["rear_calibration"] == "document"
    assert fitted.analysis["evidence"]["prescriptions"]["rear_calibration"] == section
    assert fitted.fingerprint != inherited.fingerprint
    assert not inherited.rear_calibration
    assert "rear_calibration" not in inherited.analysis["resolution"]


@pytest.mark.parametrize("empty", [None, {}])
def test_clearing_the_rear_calibration_restores_the_base_absence(bank, rear_base, empty):
    fitted = publish_authored_candidate(judge_prescription_document(
        document(rear_base.fingerprint, {"rear_calibration": _rear_document()}), base=rear_base), root=bank)

    cleared = judge_prescription_document(
        document(fitted.fingerprint, {"rear_calibration": empty}), base=fitted)
    kept = judge_prescription_document(document(fitted.fingerprint), base=fitted)

    assert not cleared.rear_calibration
    assert cleared.analysis["resolution"]["rear_calibration"] == "cleared"
    assert kept.rear_calibration == fitted.candidate.rear_calibration
    assert cleared.fingerprint != kept.fingerprint
    # The cleared core is the pre-field shape, so the absence hashes as it did
    # before the section existed.
    assert MeasuredCrossoverCandidate.from_mapping(
        {key: value for key, value in cleared.to_dict().items() if key != "rear_calibration"}
    ).fingerprint == cleared.fingerprint


@pytest.mark.parametrize("section,code", [
    (_acoustic_rear_document, "rear_calibration_case_unsupported"),
    (lambda: {**_rear_document(), "sample_rate_hz": 44100}, "rear_calibration_invalid"),
])
def test_a_refused_rear_calibration_names_its_section(rear_base, section, code):
    with pytest.raises(PrescriptionDocumentRefused) as caught:
        judge_prescription_document(
            document(rear_base.fingerprint, {"rear_calibration": section()}), base=rear_base)

    assert (caught.value.code, caught.value.section) == (code, "rear_calibration")


@pytest.mark.parametrize("verb", ["judge", "compose", "contract"])
@pytest.mark.parametrize("round_kind,expected,exit_code", [
    ("missing", "round_not_found", 2), ("empty", "round_not_found", 2),
    ("malformed", "evidence_unreadable", 2), ("io", "evidence_unreadable", 2),
])
def test_cli_names_missing_round_separately_from_read_faults(base, bank, tmp_path, capsys, monkeypatch,
                                                           verb, round_kind, expected, exit_code):
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps(document(base.fingerprint)))
    round_path = tmp_path / "round"
    if round_kind == "empty":
        round_path.mkdir()
    elif round_kind == "malformed":
        (round_path / "bundle").mkdir(parents=True)
    elif round_kind == "io":
        def unreadable(_path):
            raise PermissionError()
        monkeypatch.setattr(crossover_prescriber, "round_inputs", unreadable)
    args = [verb, "--round", str(round_path)]
    if verb != "contract":
        args += [str(path), "--root", str(bank)]
    assert crossover_prescriber.main(args) == exit_code
    answer = json.loads(capsys.readouterr().out)
    assert answer["reason" if verb == "contract" else "code"] == expected
    assert len(banked_candidates(root=bank)) == 1
