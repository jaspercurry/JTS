# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import importlib
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from jasper.active_speaker import rear_calibration as rear_cal
from jasper.active_speaker.crossover_v2 import alignment_prescription as alignment
from jasper.active_speaker.crossover_v2 import bass_prescription as bass
from jasper.active_speaker.crossover_v2 import blend_prescription as blend
from jasper.active_speaker.crossover_v2 import driver_prescription as driver
from jasper.active_speaker.crossover_v2 import room_prescription as room
from jasper.active_speaker.crossover_v2 import topology_prescription as topology
from jasper.active_speaker.crossover_v2.evidence_packet import DERIVED_VIEWS, EVIDENCE_KEY, build_crossover_evidence_packet
from jasper.active_speaker.crossover_v2.corner_admissibility import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND, FC_REJECT_BELOW_DECLARED_FLOOR,
    fc_rejection_scenarios,
)
from jasper.active_speaker.crossover_v2.prescription_contract import (
    BASE_NOT_BANKED, CONTRACT_COMMAND, contract_digests, contract_json, contract_programs, prescription_contracts,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    contract_sources, default_out, prescription_sources, read_run_manifest, round_inputs, set_artifact_name,
)
from jasper.active_speaker import candidate_bank, candidate_parts, program_headroom
from jasper.active_speaker.camilla_yaml import ProgramHeadroomExhausted
from jasper.active_speaker.measured_crossover_candidate import compile_candidate_config
from jasper.active_speaker.speaker_fit import SpeakerFitUnreadable, _fit_vocabularies
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.active_speaker.measurement_programs import programs_for_topology
from jasper.active_speaker.round_packet import banked_evidence, store_banked_evidence
from jasper.active_speaker.bass_table_report import BASS_READOUT_FIELDS, bass_table_rows
from jasper.audio_measurement import room_limits as limits
from jasper.bass_extension import dynamic
from jasper.dsp_control.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_bass_extension_dynamic import _descriptor
from tests.test_crossover_v2_blend_prescription import _bundle
from tests.test_crossover_v2_driver_prescription import _draft, applied_profile
from tests.test_crossover_v2_room_prescription import _room_median
from tests.test_crossover_v2_harmonic_evidence import _artifact, _bundle as harmonic_bundle
from tests.run_manifest_fixture import write_manifest
from tests.active_speaker_fixtures import bind_role_rows, mono_output_topology
from tests.test_rear_output_foundation import _rear_document, _rear_pair
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.crossover_v2_fixtures import _one_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology

PLAIN_PROGRAMS = programs_for_topology(mono_output_topology())


@pytest.mark.parametrize("layout,rear,digest", [
    ("mono", False, "94c9a707d42d4883c6510654d99b94ecd0aed763083b127395fbd3c6c1d3d1bf"),
    ("mono", True, "ff6567e54a31d03bb3825a7661ecc69bbea43d5d9fe93b5135b3dc265b3e2770"),
    ("stereo", False, "8dc534ec30a06f1fc138e7f7dddb825eab38bc374b51728bd29415fe85f5c8b4"),
    ("stereo", True, "5a0806fbb5d7ba86681fdddb169eaf2965df46486ded58f71aae857c4e3637bb"),
])
def test_contracts_publish_only_the_boxes_programs(round_bank, monkeypatch, capsys, layout, rear, digest):
    preset = _rear_pair(layout)[0].to_dict() if rear else _two_way_preset(layout)
    box = _rear_pair(layout)[1] if rear else _active_topology(layout, "active_2_way")
    candidate = _candidate(preset=ActiveSpeakerPreset.from_mapping(preset)).to_dict()
    programs = programs_for_topology(box)
    for sources in ({"draft": {"topology": box.to_dict()}}, {"candidate": candidate},
                    {"applied_profile": applied_profile(preset=preset)}):
        assert contract_programs(sources) == programs
    contracts = prescription_contracts(programs=programs, candidate=candidate)
    assert ("rear" in contracts) is rear
    assert hashlib.sha256(contract_json(contracts).encode()).hexdigest() == digest
    bank, session = round_bank
    artifact = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/candidate.json"
    artifact.write_text(json.dumps(candidate))
    draft_path = bank / "design-draft.json"
    draft = json.loads(draft_path.read_text())
    draft["manual_settings"]["drivers"] = bind_role_rows(box, draft["manual_settings"]["drivers"])
    draft_path.write_text(json.dumps({**draft, "topology": box.to_dict()}))
    monkeypatch.setattr(cli, "load_output_topology", lambda: box)
    for args in ([], ["--round", str(bank)]):
        assert cli.main(["contract", *args]) == cli.EXIT_OK
        assert set(json.loads(capsys.readouterr().out)["sections"]) == set(programs)
        assert cli.main(["contract", "--section", "rear", *args]) == (cli.EXIT_OK if rear else cli.EXIT_REFUSED)
        answer = json.loads(capsys.readouterr().out)
        assert answer.get("reason") == (None if rear else "prescription_section_unavailable")
    packet = build_crossover_evidence_packet(session, driver_draft_path=draft_path)
    monkeypatch.setattr(rear_cal, "MAX_ALLPASS_Q", rear_cal.MAX_ALLPASS_Q + 1)
    changed = prescription_contracts(programs=programs, candidate=candidate)
    assert (contract_digests(changed) != contract_digests(contracts)) is rear
    after = build_crossover_evidence_packet(session, driver_draft_path=draft_path)
    assert set(packet["contracts"]) == set(programs)
    assert (packet["packet_fingerprint"] != after["packet_fingerprint"]) is rear


@pytest.fixture
def bass_packet():
    return {"round_id": "round-1", "packet_fingerprint": "p" * 64,
            "bass": [{"set_id": "set-1", "takes": [{"bands": [
                {"band_hz": list(band), "estimated_snr_db": 30, "fundamental_qualified": True}
                for band in BASS_BANDS_HZ]}]}],
            "bass_table": {"tables": [{"candidate_id": "candidate-1", "levels": [
                {"level_key": {"level_db": level}, "candidate_response": {"qualified_from_hz": floor}}
                for level, floor in zip((-30, -25, -20, -15), (None, 20, 40, 63))]}]}}


@pytest.fixture
def round_bank(tmp_path, request):
    bank = tmp_path / "round"
    session, _ = _bundle(bank / "bundle")
    write_manifest(bank)
    artifact = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    preset = _two_way_preset()
    draft = _draft()
    if getattr(request, "param", False):
        rear_preset, box = _rear_pair("mono")
        preset = rear_preset.to_dict()
        draft["topology"] = box.to_dict()
    (artifact / "candidate.json").write_text(json.dumps(_candidate(preset=ActiveSpeakerPreset.from_mapping(preset)).to_dict()))
    draft["manual_settings"]["drivers"][1].update(
        recommended_highpass_hz=1000.0, recommended_highpass_slope_db_per_octave=12.0,
    )
    draft = design_draft_view(draft)
    (bank / "design-draft.json").write_text(json.dumps(draft))
    median = _room_median()
    (bank / "room.json").write_text(json.dumps({
        "median": median,
        "ceiling": {"hz": median["ceiling_hz"], "provenance": {
            "ceiling_hz": median["ceiling_hz"], "ceiling_source": median["ceiling_source"],
            "trusted_floor_hz": median["ceiling_hz"],
        }},
        "persistence": {"ceiling_hz": median["ceiling_hz"], "n_positions": median["n_positions"],
                        "features": [{"kind": "dip", "centre_hz": f} for f in (45.0, 90.0)]},
    }))
    store_banked_evidence(bank)
    return bank, session


def test_room_contract_preserves_the_document_ceiling_provenance(round_bank):
    bank, _ = round_bank
    ceiling = json.loads((bank / "room.json").read_text())["ceiling"]
    provenance = _contracts(*round_bank)["room"]["bounds"]["ceiling_provenance"]
    assert provenance == ceiling
    assert provenance["hz"] == provenance["provenance"]["trusted_floor_hz"]


def _contracts(bank: Path, session: Path):
    sources = dict(
        **contract_sources(session),
        draft=json.loads((bank / "design-draft.json").read_text()),
        receipt=json.loads((session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/round_receipt.json").read_text()),
    )
    return prescription_contracts(programs=contract_programs(sources), **sources)


@pytest.mark.parametrize("section,door,codes", [
    ("speaker", "driver", driver.DRIVER_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "blend", blend.BLEND_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "alignment", alignment.ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "topology", topology.TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS),
    ("room", None, room.ROOM_PRESCRIPTION_REFUSAL_REASONS),
    ("bass", None, dynamic.DYNAMIC_BASS_REFUSAL_REASONS),
])
def test_each_door_serves_an_authoring_schema_and_the_judges_codes(round_bank, section, door, codes):
    contracts = _contracts(*round_bank)
    contract = contracts[section][door] if door else contracts[section]
    schema = contract["schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) <= set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert set(contract["refusal_codes"]) == codes
    assert isinstance(contract["bounds"], dict)
    assert json.loads(contract_json(contract)) == contract


@pytest.mark.parametrize("positions", [1, 7])
def test_room_curves_and_feature_admission_use_the_medians_grid_and_the_judge(positions):
    raw = _room_median()
    raw.update(positions=raw["positions"][:positions], n_positions=positions)
    median = room.read_room_median(raw)
    persistence = {"features": [{"kind": "dip", "centre_hz": f} for f in (45.0, 90.0)]}
    bounds = prescription_contracts(room_median=raw, room_persistence=persistence)["room"]["bounds"]
    np.testing.assert_array_equal(bounds["freqs_hz"], median.freqs_hz)
    np.testing.assert_array_equal(bounds["cut_floor_db"], limits.cut_floor_db(
        median.spread_db, median.freqs_hz, median.ceiling_hz,
    ))
    np.testing.assert_array_equal(bounds["boost_cap_db"], limits.boost_cap_db(median.freqs_hz, median.ceiling_hz))
    assert bounds["taper_knee_hz"] == limits.taper_knee_hz(median.ceiling_hz)
    assert bounds["q_range"] == [limits.ROOM_PEQ_Q_MIN, limits.ROOM_PEQ_Q_MAX]
    assert bounds["spatial_support"] == limits.spatial_support(positions)
    for finding in bounds["admit_boost"]:
        expected = limits.admit_boost(finding["freq_hz"], freqs_hz=median.freqs_hz,
                                     median_db=median.median_db, deviations_db=median.deviations_db,
                                     n_positions=positions).to_dict()
        assert {key: finding[key] for key in expected} == expected


def test_speaker_limits_come_from_the_declared_hardware_and_round(round_bank):
    bank, _ = round_bank
    speaker = _contracts(*round_bank)["speaker"]
    draft = json.loads((bank / "design-draft.json").read_text())
    expected = driver.driver_passbands_from_safety_profile(draft["driver_safety_profile"])
    assert speaker["driver"]["bounds"]["passbands_hz"] == {role: list(band) for role, band in expected.items()}
    bounds = speaker["driver"]["bounds"]
    assert set(bounds["boost_headroom"]) == set(expected)
    route = speaker["blend"]["bounds"]["boost_route"]
    assert (route["status"], route["reason"]) == ("unavailable", blend.BOOST_ROUTE_UNAVAILABLE)
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    assert speaker["alignment"]["bounds"]["declared_delay_magnitude_us"] == list(alignment.alignment_delay_search_bounds_us(preset))
    corner = preset.crossover_regions[0].fc_hz
    assert speaker["alignment"]["bounds"]["lobe_us"] == alignment.half_period_us(corner)
    assert speaker["topology"]["bounds"]["fc_hz"] == [1000.0, 4000.0]
    assert speaker["topology"]["bounds"]["supported_orders"] == sorted(topology.SUPPORTED_LR_ORDERS)
    assert speaker["topology"]["bounds"]["declared_fc_refusal"] is None
    assert speaker["topology"]["bounds"]["fc_rejection"] == {
        "below_minimum": FC_REJECT_BELOW_DECLARED_FLOOR,
        "above_maximum": FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
        "at_minimum": None, "at_maximum": None,
    }


@pytest.mark.parametrize("corner,refusal", [
    (None, None), (999.0, FC_REJECT_BELOW_DECLARED_FLOOR),
    (1000.0, None), (4000.0, None), (4001.0, FC_REJECT_ABOVE_LOWER_DRIVER_BAND),
])
def test_declared_corner_uses_the_same_rejection_rule_as_the_bounds(corner, refusal):
    result = fc_rejection_scenarios(1000.0, 4000.0, declared_fc_hz=corner)
    assert result["declared_fc_refusal"] == refusal


@pytest.mark.parametrize("surface", ["contract", "packet"])
def test_round_context_is_read_once(round_bank, monkeypatch, capsys, surface):
    bank, session = round_bank
    profile = bank / "applied-profile.json"
    profile.write_text(json.dumps(applied_profile(preset=_two_way_preset())))
    reads = dict.fromkeys((
        bank / "design-draft.json", profile,
        session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/round_receipt.json",
    ), 0)
    path_open = Path.open

    def counted_open(path, *args, **kwargs):
        if path in reads:
            reads[path] += 1
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    if surface == "contract":
        assert cli.main(["contract", "--round", str(bank)]) == 0
        assert set(json.loads(capsys.readouterr().out)["sections"]) == set(PLAIN_PROGRAMS)
    else:
        packet = build_crossover_evidence_packet(
            session, driver_draft_path=bank / "design-draft.json", applied_profile_path=profile,
        )
        assert set(packet["contracts"]) == set(PLAIN_PROGRAMS)
    assert list(reads.values()) == [1, 1, 1]


@pytest.mark.parametrize("round_bank,section", [
    *[(False, section) for section in PLAIN_PROGRAMS],
    pytest.param(True, "rear", id="cardioid-rear"),
], indirect=["round_bank"])
def test_served_bytes_digest_matches_packet_and_status(round_bank, tmp_path, capsys, section):
    bank, session = round_bank
    output = tmp_path / f"{section}.json"
    assert cli.main(["contract", "--round", str(bank), "--section", section, "--out", str(output)]) == 0
    answer = json.loads(capsys.readouterr().out)
    served = output.read_bytes()
    assert (answer["out"], answer["bytes"], "sections" in answer) == (str(output), len(served), False)
    packet = build_crossover_evidence_packet(session, driver_draft_path=bank / "design-draft.json")
    assert packet["contracts"][section] == answer["sha256"] == hashlib.sha256(served).hexdigest()
    assert packet["contracts"] == contract_digests(_contracts(*round_bank))
    assert {"response_format", "driver_response_format"}.isdisjoint(packet)
    assert packet["capture_snr"]["uncertainty"] == CONTRACT_COMMAND
    assert cli.main(["status", str(bank)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["contracts"] == packet["contracts"]


def test_contract_without_round_discloses_missing_evidence_and_bass_defaults(capsys):
    assert cli.main(["contract"]) == 0
    printed = capsys.readouterr().out
    answer = json.loads(printed)
    # Served in the contracts' own compact serialization, so the envelope costs bytes, not a multiple.
    assert printed == contract_json(answer) + "\n"
    contracts = answer["sections"]
    assert set(contracts) == set(PLAIN_PROGRAMS)
    assert (contracts["room"]["status"], contracts["room"]["reason"]) == ("unavailable", room.ROOM_MEDIAN_UNAVAILABLE)
    assert contracts["room"]["bounds"]["cut_floor_db"] is None
    contract = contracts["bass"]
    assert set(contract["schema"]["properties"]) == dynamic.REQUIRED_FIELDS | dynamic.OPTIONAL_FIELDS | {"round_id"}
    assert set(contract["refusal_codes"]) == {
        "bass_descriptor_malformed", "bass_linkwitz_transform_invalid",
        "bass_delta_highpass_hz_invalid", "bass_detector_lowpass_hz_invalid", "bass_compressor_threshold_dbfs_invalid",
        "bass_compressor_factor_invalid", "bass_compressor_attack_s_invalid", "bass_compressor_release_s_invalid",
    }
    assert contract["schema"]["required"] == sorted(dynamic.REQUIRED_FIELDS)
    assert {name: contract["schema"]["properties"][name]["default"] for name in dynamic.OPTIONAL_FIELDS} == {
        "compressor_factor": 10.0, "compressor_attack_s": 0.01, "compressor_release_s": 0.25}
    assert (contract["status"], contract["reason"]) == ("unavailable", bass.BASS_EVIDENCE_UNAVAILABLE)


def test_the_layers_the_bass_contract_calls_uncharged_leave_the_charge_unmoved():
    """ADR-0385, ADR-0359: the block names the one charge and the bass reserve, and a bass
    boost leaves the emitted graph's charge where it was."""
    block = prescription_contracts(programs=("bass",))["bass"]["shared_headroom"]
    named = [getattr(importlib.import_module(module), name) for module, _, name in
             (block[field].rpartition(".") for field in ("charge_function", "bass_reserve_function"))]
    assert named == [program_headroom.charge_db, dynamic.dynamic_bass_gain_reserve_db]
    assert "bass_extension" in set(block["uncharged_layers"]) - set(block["charged_layers"])
    candidate = _candidate(linearization={"woofer": {"filters": [
        {"biquad_type": "Peaking", "freq": 100.0, "q": 1.0, "gain": 4.0}]}})
    boosted = replace(candidate, bass_extension=_descriptor().payload())
    assert candidate_parts.program_charge_db(boosted) == candidate_parts.program_charge_db(candidate) > 0.0


def test_bass_contract_reads_saved_packet_and_discloses_every_level(round_bank, bass_packet, capsys):
    bank, _ = round_bank
    bass_packet["round_id"] = bank.name
    for level in bass_packet["bass_table"]["tables"][0]["levels"]:
        level.update(sources=[{"before": "/tmp/base.json", "after": r"C:\captures\after.json"}],
                     records=["/tmp/record.json"], compression_includes=["compressor", "driver"], harmonics_delta_db=[])
    (bank / "packet.json").write_text(json.dumps(bass_packet))
    assert cli.main(["contract", "--round", str(bank), "--section", "bass"]) == 0
    contract = json.loads(capsys.readouterr().out)["sections"]["bass"]
    assert contract["status"] == "available"
    assert set(contract["refusal_codes"]) == dynamic.DYNAMIC_BASS_REFUSAL_REASONS
    levels = contract["detail"]["levels"]
    assert levels == bass_table_rows(bass_packet["bass_table"])
    for level in levels:
        assert set(level) == set(BASS_READOUT_FIELDS)
        assert not any(separator in json.dumps(level) for separator in ("/", "\\"))


def test_evidence_declarations_are_served_as_templates_and_cannot_be_mutated():
    first = prescription_contracts()["speaker"]["evidence_declarations"]
    assert first["capture_snr"]["fields"] == {}
    field = "h{order}_repeat_spread_db"
    assert first["harmonics"]["fields"][field]["kind"] == "random"
    first["harmonics"]["fields"][field]["kind"] = "changed"
    assert prescription_contracts()["speaker"]["evidence_declarations"]["harmonics"]["fields"][field]["kind"] == "random"


@pytest.mark.parametrize("name", sorted(dynamic.REQUIRED_FIELDS | dynamic.OPTIONAL_FIELDS))
def test_bass_schema_edges_match_the_unchanged_validator(name):
    contract = prescription_contracts()["bass"]
    prop = contract["schema"]["properties"][name]
    # The lowest high-pass keeps the detector's lowest edge above it.
    baseline = {**_descriptor().payload(), "delta_highpass_hz": 10.0}
    for bound, direction in (("minimum", -math.inf), ("exclusiveMinimum", -math.inf),
                             ("maximum", math.inf)):
        if bound not in prop:
            continue
        edge = prop[bound]
        accepted = math.nextafter(edge, math.inf) if bound == "exclusiveMinimum" else edge
        assert dynamic.validate_dynamic_bass_descriptor({**baseline, name: accepted})[name] == accepted
        refused = edge if bound == "exclusiveMinimum" else math.nextafter(edge, direction)
        with pytest.raises(ValueError):
            dynamic.validate_dynamic_bass_descriptor({**baseline, name: refused})
    if name == "delta_highpass_hz":
        upper = baseline[contract["bounds"]["delta_highpass_hz_exclusive_upper_field"]]
        with pytest.raises(ValueError):
            dynamic.validate_dynamic_bass_descriptor({**baseline, name: upper})
        assert dynamic.validate_dynamic_bass_descriptor({**baseline, name: math.nextafter(upper, -math.inf)})[name] < upper


@pytest.mark.parametrize("name", ["source_hz", "source_q", "target_hz", "target_q"])
def test_linkwitz_schema_edges_match_the_validator(name):
    contract = prescription_contracts()["bass"]
    prop = contract["schema"]["properties"]["linkwitz_transform"]["properties"][name]
    rules = contract["bounds"]["linkwitz_transform"]
    # A low target keeps every single-field edge inside the cross-field rules.
    shape = {"source_hz": 90.0, "source_q": 0.6, "target_hz": 12.0, "target_q": 0.707}
    baseline = {**_descriptor().payload(), "detector_lowpass_hz": 125.0, "delta_highpass_hz": 15.0}

    def validate(value, **changes):
        return dynamic.validate_dynamic_bass_descriptor({**baseline, **changes, "linkwitz_transform": {**shape, name: value}})

    for bound, direction in (("minimum", -math.inf), ("maximum", math.inf)):
        if bound in prop:
            assert validate(prop[bound])["linkwitz_transform"][name] == prop[bound]
            with pytest.raises(ValueError):
                validate(math.nextafter(prop[bound], direction))
    if name == "target_hz":
        with pytest.raises(ValueError):
            validate(shape[name], delta_highpass_hz=None)
        with pytest.raises(ValueError):
            validate(shape[rules["target_hz_exclusive_upper_field"]])


@pytest.mark.parametrize("named_set", [False, True])
def test_a_banked_round_serves_the_room_its_bank_stored(round_bank, capsys, named_set):
    """A re-run room view is a view (ADR-0371)."""
    bank, _ = round_bank
    set_id = read_run_manifest(round_inputs(bank))["sets"][0]["set_id"] if named_set else None
    view = bank / set_artifact_name("room.json", set_id)
    stored = json.loads((bank / "room.json").read_text())
    (bank / "packet.json").write_text(json.dumps({"room": [{**stored, "out": str(view)}]}))
    view.write_text(json.dumps({}))
    assert cli.main(["contract", "--round", str(bank), "--section", "room",
                     *(["--set", set_id] if set_id else [])]) == cli.EXIT_OK
    served = json.loads(capsys.readouterr().out)["sections"]["room"]
    assert served["status"] == "available"
    assert served["bounds"]["freqs_hz"] == stored["median"]["freqs_hz"]


@pytest.mark.parametrize("section,code", [("speaker", cli.EXIT_UNREADABLE), ("room", cli.EXIT_OK)])
def test_a_named_section_is_built_alone(round_bank, capsys, section, code):
    """A declared woofer diameter of 0 cannot be built into the speaker section; a room request
    never builds that section, so it still answers."""
    bank, _ = round_bank
    draft = json.loads((bank / "design-draft.json").read_text())
    next(row for row in draft["manual_settings"]["drivers"] if row["role"] == "woofer")["radiating_diameter_mm"] = 0.0
    (bank / "design-draft.json").write_text(json.dumps(draft))
    assert cli.main(["contract", "--round", str(bank), "--section", section]) == code
    assert set(json.loads(capsys.readouterr().out).get("sections", ())) == ({section} if code == cli.EXIT_OK else set())


def test_live_contract_reads_the_view_writers_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    session, _ = _bundle(tmp_path)
    output = default_out(round_inputs(session), session, "room.json")
    output.write_text(json.dumps({"median": _room_median()}))
    assert cli.main(["contract", "--round", str(session), "--section", "room"]) == 0
    served = json.loads(capsys.readouterr().out)["sections"]
    assert served["room"]["status"] == "available"
    assert served["room"]["bounds"]["freqs_hz"] == _room_median()["freqs_hz"]
    packet = build_crossover_evidence_packet(session)
    assert packet["contracts"]["room"] == contract_digests(served)["room"]


def test_harmonic_templates_cover_the_published_rows(tmp_path):
    packet = build_crossover_evidence_packet(harmonic_bundle(tmp_path, harmonics=_artifact()))
    block = packet[DERIVED_VIEWS]["harmonics"]
    declarations = prescription_contracts()["speaker"]["evidence_declarations"]["harmonics"]
    declared = {name.format(order=order)
                for group in ("fields", "not_uncertainties")
                for name in declarations[group] for order in block["orders"]}
    assert block["uncertainty"] == CONTRACT_COMMAND
    assert block["roles"]
    for role in block["roles"]:
        assert role["rows"]
        for row in role["rows"]:
            assert set(row) <= declared


@pytest.mark.parametrize("valid", [True, False])
def test_applied_preset_fallback_matches_the_packets_reader(round_bank, capsys, valid):
    bank, session = round_bank
    (session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/candidate.json").unlink()
    profile = applied_profile(preset=_two_way_preset())
    if not valid:
        profile["kind"] = "unknown"
    path = bank / "applied-profile.json"
    path.write_text(json.dumps(profile))
    assert cli.main(["contract", "--round", str(bank)]) == 0
    contracts = json.loads(capsys.readouterr().out)["sections"]
    packet = build_crossover_evidence_packet(
        session, driver_draft_path=bank / "design-draft.json", applied_profile_path=path,
    )
    assert contract_digests(contracts) == packet["contracts"]
    assert (contracts["speaker"]["alignment"]["bounds"]["fc_hz"] is not None) is valid


@pytest.mark.parametrize("manifest", [
    {"sets": [{"takes": [{"selected": True, "level": {
        "level_db": -21.09, "loudest_half_second_db_spl": 40.0, "stimulus_dbfs": -6.0,
    }}]}]}, {}, {"sets": [None, {"takes": [None, {}]}, {}]}, {"sets": None},
])
def test_speaker_contract_publishes_playback_cost_with_unreadable_measurements(round_bank, manifest):
    bank, session = round_bank
    sources = contract_sources(session)
    sources["candidate"] = _candidate(
        preset=ActiveSpeakerPreset.from_mapping(sources["candidate"]["source_preset"]),
        trims={"woofer": 0.0, "tweeter": -9.52},
        linearization={"tweeter": {"filters": [{"biquad_type": "Peaking", "freq": 12000.0, "gain": 6.0, "q": 1.0}]}},
    ).to_dict()
    sources["manifest"] = manifest
    draft = json.loads((bank / "design-draft.json").read_text())
    for target in draft["driver_safety_profile"]["targets"]:
        target["level_duration_limits"] = {"max_effective_peak_dbfs": -8.0 if target["role"] == "woofer" else -33.2}
    bounds = prescription_contracts(**sources, draft=draft)["speaker"]["driver"]["bounds"]["boost_headroom"]
    assert bounds["tweeter"]["composed_boost_db"] == pytest.approx(6.0)
    assert bounds["woofer"]["composed_boost_db"] == 0.0
    for row in bounds.values():
        assert row["program_headroom_spent_db"] == 0.0
        assert row["program_headroom_remaining_db"] == 40.0
        assert row["max_program_headroom_db"] == 40.0
        assert row["binding"] is None
        assert row["session_volume_db"] == (-21.09 if manifest.get("sets") and manifest["sets"][0] else None)
        assert row["spl_headroom_db"] == (42.0 if row["session_volume_db"] is not None else None)


@pytest.mark.parametrize("rear", [False, True])
def test_the_contract_spends_what_the_emitted_graph_attenuates(rear):
    """#5909 D2: the base's spend is its emitted charge, rear stage included."""
    candidate = _candidate(
        preset=_rear_pair("mono")[0] if rear else None, rear_calibration=_rear_document() if rear else None,
        linearization={"woofer": {"filters": [{"biquad_type": "Peaking", "freq": 100.0, "q": 1.0, "gain": 4.0}]}},
    )
    graph = yaml.safe_load(compile_candidate_config(candidate, playback_device="null"))
    charge = -graph["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    rows = prescription_contracts(candidate=candidate.to_dict())["speaker"]["driver"]["bounds"]["boost_headroom"]
    assert [row["program_headroom_spent_db"] for row in rows.values()] == [pytest.approx(charge)] * 2
    assert [row["program_headroom_remaining_db"] for row in rows.values()] == [pytest.approx(40.0 - charge)] * 2


@pytest.mark.parametrize("trim_db, charge_db", [(0.0, 5.0), (-2.0, 3.0), (-6.0, 0.0)])
def test_a_one_way_trim_nets_the_boost_it_follows(trim_db, charge_db):
    """#5909: a one-way +4 dB boost charges 5.0 / 3.0 / 0.0 dB at trims 0 / -2 / -6 dB in the
    emitted graph, in the contract and in composition's judgment (ADR-0385)."""
    candidate = _candidate(preset=_one_way_preset(), trims={"full_range": trim_db}, linearization={
        "full_range": {"filters": [{"biquad_type": "Peaking", "freq": 1000.0, "q": 1.0, "gain": 4.0}]}})
    graph = yaml.safe_load(compile_candidate_config(candidate, playback_device="null"))
    row = prescription_contracts(candidate=candidate.to_dict())["speaker"]["driver"]["bounds"]["boost_headroom"]["full_range"]
    assert (-graph["filters"]["active_baseline_headroom"]["parameters"]["gain"], row["program_headroom_spent_db"],
            candidate_parts.program_charge_db(candidate)) == pytest.approx((charge_db,) * 3, abs=1e-4)


def test_an_exhausted_base_spends_the_charge_the_emitter_refused():
    """#5909: past the ceiling, the contract and the fit read the charge the emitter refused."""
    candidate = _candidate(linearization={"woofer": {"filters": [
        {"biquad_type": "Peaking", "freq": 900.0, "q": 1.0, "gain": 45.0}]}})
    with pytest.raises(ProgramHeadroomExhausted) as refused:
        compile_candidate_config(candidate, playback_device="null")
    rows = prescription_contracts(candidate=candidate.to_dict())["speaker"]["driver"]["bounds"]["boost_headroom"]
    assert {role: (row["program_headroom_spent_db"], row["program_headroom_remaining_db"], row["binding"], row["reason"])
            for role, row in rows.items()} == {role: (refused.value.charge_db, 0.0, "program_headroom", None)
                                               for role in ("woofer", "tweeter")}
    caps = _fit_vocabularies(candidate, {"woofer": {}, "tweeter": {}})
    assert {role: vocabulary.composed_boost_cap_db for role, vocabulary in caps.items()} == {
        "woofer": 40.0, "tweeter": 0.0}


def test_a_round_without_a_candidate_charges_no_base_and_never_touches_the_bank(round_bank, monkeypatch):
    """#5909: the packet is built from the round's banked inputs alone (ADR-0371), even when
    the applied tune names a banked candidate."""
    bank, session = round_bank
    (session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/candidate.json").unlink()
    (bank / "applied-profile.json").write_text(json.dumps({
        **applied_profile(preset=_two_way_preset()), "candidate_artifact_path": str(bank / "elsewhere.json"),
        "source": {"measured_candidate_fingerprint": "f" * 64},
    }))

    def touched(*args, **kwargs):
        raise AssertionError("the candidate bank was touched")

    for module in (candidate_bank, candidate_parts):
        for name in ("find_banked_candidate", "load_applied_candidate", "publish_authored_candidate"):
            monkeypatch.setattr(module, name, touched)
    stored, error = banked_evidence(round_inputs(bank))
    assert error is None and stored[EVIDENCE_KEY] is not None
    rows = prescription_contracts(**prescription_sources(round_inputs(bank)))["speaker"]["driver"]["bounds"]
    assert {role: (row["program_headroom_spent_db"], row["reason"]) for role, row in rows["boost_headroom"].items()} == {
        role: (None, BASE_NOT_BANKED) for role in ("woofer", "tweeter")}


def test_a_base_the_emitter_refuses_names_its_code_and_the_packet_still_builds(round_bank):
    """#5909: the contract publishes no charge for a base the emitter refuses, with the refusal's
    code, and the round's packet builds; the fit refuses with that code."""
    bank, session = round_bank
    cut = {"biquad_type": "Peaking", "freq": 1000.0, "q": 1.0, "gain": -1.0}
    candidate = replace(_candidate(), blend_correction=[cut] * 3)
    (session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/candidate.json").write_text(
        json.dumps(candidate.to_dict()))
    stored, error = banked_evidence(round_inputs(bank))
    assert error is None and stored[EVIDENCE_KEY] is not None
    rows = prescription_contracts(candidate=candidate.to_dict())["speaker"]["driver"]["bounds"]["boost_headroom"]
    assert {role: (row["program_headroom_spent_db"], row["reason"]) for role, row in rows.items()} == {
        role: (None, candidate_parts.COMPOSITION_INVALID) for role in ("woofer", "tweeter")}
    with pytest.raises(SpeakerFitUnreadable) as refused:
        _fit_vocabularies(candidate, {"woofer": {}})
    assert refused.value.code == candidate_parts.COMPOSITION_INVALID


def test_rear_contract_bounds_equal_the_rear_calibration_constants():
    contract = prescription_contracts()["rear"]
    assert contract["document_section"] == "rear_calibration"
    assert contract["case"] == "electrical_dsp"
    assert contract["mode"] == "branches"
    schema = contract["schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    bounds = contract["bounds"]
    assert bounds["max_filters_per_chain"] == rear_cal.MAX_FILTERS_PER_CHAIN
    assert bounds["chain_gain_db"] == [rear_cal.MIN_CHAIN_GAIN_DB, 0.0]
    front = schema["properties"]["front"]
    rear = schema["properties"]["rear"]["properties"]
    for chain in (front, rear["bass"], rear["cancellation"]):
        gain = chain["properties"]["gain_db"]
        assert [gain["minimum"], gain["maximum"]] == bounds["chain_gain_db"]
    assert bounds["resonant_q_max"] == rear_cal.MAX_RESONANT_Q
    assert bounds["allpass_q_max"] == rear_cal.MAX_ALLPASS_Q
    assert bounds["combo_order_max"] == rear_cal.MAX_COMBO_ORDER
    assert set(bounds["biquad_kinds"]) == rear_cal.BIQUADS
    assert set(bounds["combo_kinds"]) == rear_cal.COMBOS
    assert set(bounds["gain_kinds"]) == rear_cal.SHELVING
    assert set(bounds["stage_kinds"]) == rear_cal.STAGES
    assert json.loads(contract_json(contract)) == contract


def test_rear_schema_filter_shapes_match_the_validators_per_kind_caps():
    filter_schema = prescription_contracts()["rear"]["schema"]["properties"]["front"]["properties"]["filters"]["items"]
    q_max_by_kind: dict[str, float | None] = {}
    gain_required_kinds: set[str] = set()
    order_kinds_seen: set[str] = set()
    for branch in filter_schema["oneOf"]:
        parameters = branch["properties"]["parameters"]
        kinds = set(parameters["properties"]["type"]["enum"])
        assert parameters["additionalProperties"] is False
        if "gain" in parameters["properties"]:
            assert "gain" in parameters["required"]
            assert parameters["properties"]["gain"]["maximum"] == rear_cal.MAX_CHAIN_BOOST_DB
            gain_required_kinds |= kinds
        if "q" in parameters["properties"]:
            q = parameters["properties"]["q"]
            assert q["exclusiveMinimum"] == 0
            for kind in kinds:
                q_max_by_kind[kind] = q.get("maximum")
        if "order" in parameters["properties"]:
            order = parameters["properties"]["order"]
            assert order["minimum"] == 1
            assert order["maximum"] == rear_cal.MAX_COMBO_ORDER
            even = order.get("multipleOf") == 2
            assert even == all(kind.startswith("LinkwitzRiley") for kind in kinds)
            order_kinds_seen |= kinds

    assert gain_required_kinds == rear_cal.SHELVING
    assert order_kinds_seen == rear_cal.COMBOS
    for kind in rear_cal.BIQUADS - rear_cal.SHELVING - {"Allpass"}:
        assert q_max_by_kind[kind] == rear_cal.MAX_RESONANT_Q
    for kind in rear_cal.SHELVING - {"Peaking"}:
        assert q_max_by_kind[kind] == rear_cal.MAX_RESONANT_Q
    assert q_max_by_kind["Allpass"] == rear_cal.MAX_ALLPASS_Q
    assert q_max_by_kind["Peaking"] is None


def _biquad(kind, *, freq=100.0, q, gain=None):
    params = {"type": kind, "freq": freq, "q": q}
    if gain is not None:
        params["gain"] = gain
    return {"type": "Biquad", "parameters": params}


def _combo(kind, *, freq=100.0, order):
    return {"type": "BiquadCombo", "parameters": {"type": kind, "freq": freq, "order": order}}


def _delay_edge(document, *, common_delay_ms, cancellation_delay_ms):
    document["common_delay_ms"] = common_delay_ms
    document["rear"]["cancellation"]["delay_ms"] = cancellation_delay_ms


def _boundary_conflict(document):
    document["boundary"]["front"] = [_biquad("Highpass", q=0.7)]
    document["included_stages"]["front"] = ["boundary_correction"]


# Nyquist at diagnostic_seed(48000)'s sample rate; freq must stay strictly below it.
_NYQUIST_HZ = 24000.0


@pytest.mark.parametrize(("mutate", "expect_pass"), [
    pytest.param(lambda d: None, True, id="obeys_unmodified_seed"),

    pytest.param(lambda d: d["front"].update(gain_db=rear_cal.MIN_CHAIN_GAIN_DB), True, id="chain_gain_floor_inside"),
    pytest.param(lambda d: d["front"].update(gain_db=rear_cal.MIN_CHAIN_GAIN_DB - 0.01), False, id="chain_gain_floor_outside"),
    pytest.param(lambda d: d["rear"]["bass"].update(gain_db=0.0), True, id="chain_gain_ceiling_inside"),
    pytest.param(lambda d: d["rear"]["bass"].update(gain_db=0.01), False, id="chain_gain_ceiling_outside"),

    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", q=rear_cal.MAX_RESONANT_Q)]),
                 True, id="resonant_q_max_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", q=rear_cal.MAX_RESONANT_Q + 0.01)]),
                 False, id="resonant_q_max_outside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Lowshelf", q=rear_cal.MAX_RESONANT_Q, gain=-1.0)]),
                 True, id="shelf_resonant_q_max_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Lowshelf", q=rear_cal.MAX_RESONANT_Q + 0.01, gain=-1.0)]),
                 False, id="shelf_resonant_q_max_outside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Allpass", q=rear_cal.MAX_ALLPASS_Q)]),
                 True, id="allpass_q_max_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Allpass", q=rear_cal.MAX_ALLPASS_Q + 0.01)]),
                 False, id="allpass_q_max_outside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Peaking", q=1_000_000.0, gain=-1.0)]),
                 True, id="peaking_q_uncapped"),

    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", q=0.7, gain=-3.0)]),
                 False, id="gain_key_forbidden_on_non_shelving_kind"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Lowshelf", q=0.7, gain=rear_cal.MAX_CHAIN_BOOST_DB)]),
                 True, id="filter_gain_ceiling_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Lowshelf", q=0.7, gain=rear_cal.MAX_CHAIN_BOOST_DB + 0.01)]),
                 False, id="filter_gain_ceiling_outside"),

    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", freq=1e-3, q=0.7)]),
                 True, id="freq_lower_bound_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", freq=0.0, q=0.7)]),
                 False, id="freq_lower_bound_outside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", freq=_NYQUIST_HZ - 0.01, q=0.7)]),
                 True, id="freq_nyquist_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_biquad("Highpass", freq=_NYQUIST_HZ, q=0.7)]),
                 False, id="freq_nyquist_outside"),

    pytest.param(lambda d: d["front"].update(filters=[_combo("ButterworthLowpass", order=rear_cal.MAX_COMBO_ORDER)]),
                 True, id="combo_order_max_inside"),
    pytest.param(lambda d: d["front"].update(filters=[_combo("ButterworthLowpass", order=rear_cal.MAX_COMBO_ORDER + 1)]),
                 False, id="combo_order_max_outside"),
    pytest.param(lambda d: d["front"].update(filters=[_combo("ButterworthLowpass", order=rear_cal.MAX_COMBO_ORDER - 1)]),
                 True, id="butterworth_odd_order_allowed"),
    pytest.param(lambda d: d["front"].update(filters=[_combo("LinkwitzRileyLowpass", order=rear_cal.MAX_COMBO_ORDER)]),
                 True, id="linkwitz_riley_order_max_inside_and_even"),
    pytest.param(lambda d: d["front"].update(filters=[_combo("LinkwitzRileyLowpass", order=rear_cal.MAX_COMBO_ORDER - 1)]),
                 False, id="linkwitz_riley_odd_order_refused"),

    pytest.param(lambda d: d["front"].update(filters=[_biquad("Peaking", q=1.0, gain=-1.0)] * rear_cal.MAX_FILTERS_PER_CHAIN),
                 True, id="max_filters_per_chain_inside"),
    pytest.param(lambda d: d["front"].update(
        filters=[_biquad("Peaking", q=1.0, gain=-1.0)] * (rear_cal.MAX_FILTERS_PER_CHAIN + 1)),
                 False, id="max_filters_per_chain_outside"),

    pytest.param(lambda d: _delay_edge(d, common_delay_ms=10.0, cancellation_delay_ms=-10.0),
                 True, id="emitted_delay_rule_inside"),
    pytest.param(lambda d: _delay_edge(d, common_delay_ms=10.0, cancellation_delay_ms=-10.01),
                 False, id="emitted_delay_rule_outside"),

    pytest.param(_boundary_conflict, False, id="boundary_correction_rule_outside"),

    pytest.param(lambda d: d.update(valid_band_hz=[0.001, 100.0]), True, id="valid_band_hz_lower_bound_inside"),
    pytest.param(lambda d: d.update(valid_band_hz=[0.0, 100.0]), False, id="valid_band_hz_lower_bound_outside"),
])
def test_rear_document_agrees_with_the_validator_at_each_bound_edge(mutate, expect_pass):
    document = rear_cal.diagnostic_seed(48000)
    mutate(document)
    if expect_pass:
        assert rear_cal.read_rear_calibration(document, sample_rate=48000)["case"] == "electrical_dsp"
    else:
        with pytest.raises(rear_cal.RearCalibrationError):
            rear_cal.read_rear_calibration(document, sample_rate=48000)


def test_contract_cli_rear_shares_rooms_top_level_shape(capsys, monkeypatch):
    monkeypatch.setattr(cli, "load_output_topology", lambda: _rear_pair("mono")[1])
    assert cli.main(["contract", "--section", "rear"]) == 0
    rear = json.loads(capsys.readouterr().out)["sections"]["rear"]
    assert cli.main(["contract", "--section", "room"]) == 0
    room_contract = json.loads(capsys.readouterr().out)["sections"]["room"]
    assert {"schema", "bounds"} <= set(rear) & set(room_contract)
    # The starting document the rear door admits as written: untuned and muted.
    seed = rear_cal.read_rear_calibration(rear["seed"], sample_rate=DEFAULT_SAMPLE_RATE)
    assert (seed["rear_muted"], seed["valid_band_hz"]) == (True, None)
