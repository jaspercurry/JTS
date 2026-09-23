# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import rear_calibration as rear_cal
from jasper.active_speaker.crossover_v2 import alignment_prescription as alignment
from jasper.active_speaker.crossover_v2 import bass_prescription as bass
from jasper.active_speaker.crossover_v2 import blend_prescription as blend
from jasper.active_speaker.crossover_v2 import driver_prescription as driver
from jasper.active_speaker.crossover_v2 import room_prescription as room
from jasper.active_speaker.crossover_v2 import topology_prescription as topology
from jasper.active_speaker.crossover_v2.evidence_packet import build_crossover_evidence_packet
from jasper.active_speaker.crossover_v2.corner_admissibility import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND, FC_REJECT_BELOW_DECLARED_FLOOR,
    fc_rejection_scenarios,
)
from jasper.active_speaker.crossover_v2.prescription_contract import (
    CONTRACT_COMMAND, contract_digests, contract_json, contract_programs, prescription_contracts,
)
from jasper.active_speaker.crossover_v2.round_inputs import contract_sources, default_out, round_inputs
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ
from jasper.active_speaker.measurement_programs import programs_for_topology
from jasper.active_speaker.bass_table_report import BASS_READOUT_FIELDS, bass_table_rows
from jasper.audio_measurement import room_limits as limits
from jasper.bass_extension import dynamic
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_bass_extension_dynamic import _descriptor
from tests.test_crossover_v2_blend_prescription import _bundle
from tests.test_crossover_v2_driver_prescription import _draft, applied_profile
from tests.test_crossover_v2_room_prescription import _room_median
from tests.test_crossover_v2_harmonic_evidence import _artifact, _bundle as harmonic_bundle
from tests.run_manifest_fixture import write_manifest
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_rear_output_foundation import _rear_pair
from tests.test_active_speaker_runtime_contract import _active_topology

PLAIN_PROGRAMS = programs_for_topology(mono_output_topology())


@pytest.mark.parametrize("layout,rear,digest", [
    ("mono", False, "7aea4d5c3787cdb255c260ccc6770bd2f38285534fac7f97da11a1e28a374d45"),
    ("mono", True, "0d8da016b5dd05cdca8618d274266c1880187d3e4e28c06286d88c9b49c18fe3"),
    ("stereo", False, "01720a6a0617389a2bdf0e09c0dbba195be4b7eca3c7e944be0064bc852de388"),
    ("stereo", True, "f341b70888e1aa8f39d0673d3d4ea3dcc8559b7f01c4971426b028d60f240778"),
])
def test_contracts_publish_only_the_boxes_programs(round_bank, monkeypatch, capsys, layout, rear, digest):
    preset = _rear_pair(layout)[0].to_dict() if rear else _two_way_preset(layout)
    box = _rear_pair(layout)[1] if rear else _active_topology(layout, "active_2_way")
    candidate = {"source_preset": preset}
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
    draft_path.write_text(json.dumps({**json.loads(draft_path.read_text()), "topology": box.to_dict()}))
    monkeypatch.setattr(cli, "load_output_topology", lambda: box)
    for args in ([], ["--round", str(bank)]):
        assert cli.main(["contract", *args]) == cli.EXIT_OK
        assert set(json.loads(capsys.readouterr().out)) == set(programs)
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
    (artifact / "candidate.json").write_text(json.dumps({"source_preset": preset}))
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
    ("bass", None, bass.BASS_PRESCRIPTION_REFUSAL_REASONS),
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
    assert all(row["program_headroom_remaining_db"] == 40.0 for row in bounds["boost_headroom"].values())
    assert speaker["blend"]["bounds"]["boost_route"]["available"] is False
    assert speaker["blend"]["bounds"]["boost_route"]["reason"] == blend.BOOST_ROUTE_UNAVAILABLE
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
        assert set(json.loads(capsys.readouterr().out)) == set(PLAIN_PROGRAMS)
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
    served = capsys.readouterr().out.rstrip("\n").encode()
    assert output.read_bytes() == served
    packet = build_crossover_evidence_packet(session, driver_draft_path=bank / "design-draft.json")
    assert packet["contracts"][section] == hashlib.sha256(served).hexdigest()
    assert packet["contracts"] == contract_digests(_contracts(*round_bank))
    assert {"response_format", "driver_response_format"}.isdisjoint(packet)
    assert packet["capture_snr"]["uncertainty"] == CONTRACT_COMMAND
    assert packet["reflections"]["uncertainty"] == CONTRACT_COMMAND
    assert cli.main(["status", str(bank)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["contracts"] == packet["contracts"]


def test_contract_without_round_discloses_missing_evidence_and_bass_defaults(capsys):
    assert cli.main(["contract"]) == 0
    contracts = json.loads(capsys.readouterr().out)
    assert set(contracts) == set(PLAIN_PROGRAMS)
    assert contracts["room"]["evidence_status"] == room.ROOM_MEDIAN_UNAVAILABLE
    assert contracts["room"]["bounds"]["cut_floor_db"] is None
    contract = contracts["bass"]
    assert set(contract["schema"]["properties"]) == dynamic._REQUIRED_FIELDS | dynamic._OPTIONAL_FIELDS | {"round_id"}
    assert set(contract["refusal_codes"]) == {
        "bass_evidence_unavailable", "bass_descriptor_malformed",
    } | {f"bass_{name}_invalid" for name in dynamic._REQUIRED_FIELDS | dynamic._OPTIONAL_FIELDS}
    assert contract["schema"]["properties"]["low_boost_db"]["maximum"] == dynamic.NATIVE_LOUDNESS_BOOST_MAX_DB
    assert contract["evidence_status"] == bass.BASS_EVIDENCE_UNAVAILABLE
    assert contract["shared_headroom"]["adr"] == "ADR-0257"


def test_bass_contract_reads_saved_packet_and_discloses_every_level(round_bank, bass_packet, capsys):
    bank, _ = round_bank
    bass_packet["round_id"] = bank.name
    for level in bass_packet["bass_table"]["tables"][0]["levels"]:
        level.update(sources=[{"before": "/tmp/base.json", "after": r"C:\captures\after.json"}],
                     records=["/tmp/record.json"], compression_includes=["compressor", "driver"], harmonics_delta_db=[])
    (bank / "packet.json").write_text(json.dumps(bass_packet))
    assert cli.main(["contract", "--round", str(bank), "--section", "bass"]) == 0
    contract = json.loads(capsys.readouterr().out)
    assert contract["evidence_status"] == "evaluated"
    assert set(contract["refusal_codes"]) == bass.BASS_PRESCRIPTION_REFUSAL_REASONS
    levels = contract["evidence_status_detail"]["levels"]
    assert levels == bass_table_rows(bass_packet["bass_table"])
    for level in levels:
        assert set(level) == set(BASS_READOUT_FIELDS)
        assert not any(separator in json.dumps(level) for separator in ("/", "\\"))


def test_evidence_declarations_are_served_as_templates_and_cannot_be_mutated():
    first = prescription_contracts()["speaker"]["evidence_declarations"]
    assert first["capture_snr"]["fields"] == first["reflections"]["fields"] == {}
    field = "h{order}_repeat_spread_db"
    assert first["harmonics"]["fields"][field]["kind"] == "random"
    first["harmonics"]["fields"][field]["kind"] = "changed"
    assert prescription_contracts()["speaker"]["evidence_declarations"]["harmonics"]["fields"][field]["kind"] == "random"


@pytest.mark.parametrize("name", sorted(dynamic._REQUIRED_FIELDS | dynamic._OPTIONAL_FIELDS))
def test_bass_schema_edges_match_the_unchanged_validator(name):
    contract = prescription_contracts()["bass"]
    prop = contract["schema"]["properties"][name]
    baseline = asdict(_descriptor())
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


def test_live_contract_reads_the_view_writers_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    session, _ = _bundle(tmp_path)
    output = default_out(round_inputs(session), session, "room.json")
    output.write_text(json.dumps({"median": _room_median()}))
    assert cli.main(["contract", "--round", str(session), "--section", "room"]) == 0
    payload = capsys.readouterr().out.rstrip("\n")
    served = json.loads(payload)
    assert served["evidence_status"] == "evaluated"
    assert served["bounds"]["freqs_hz"] == _room_median()["freqs_hz"]
    packet = build_crossover_evidence_packet(session)
    assert packet["contracts"]["room"] == hashlib.sha256(payload.encode()).hexdigest()


def test_harmonic_templates_cover_the_published_rows(tmp_path):
    packet = build_crossover_evidence_packet(harmonic_bundle(tmp_path, harmonics=_artifact()))
    block = packet["harmonics"]
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
    contracts = json.loads(capsys.readouterr().out)
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
    sources["candidate"]["role_attenuations_db"] = {"woofer": 0.0, "tweeter": -9.52}
    sources["candidate"]["linearization"] = {"tweeter": {"filters": [
        {"biquad_type": "Peaking", "freq": 12000.0, "gain": 6.0, "q": 1.0},
    ]}}
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
    rear = json.loads(capsys.readouterr().out)
    assert cli.main(["contract", "--section", "room"]) == 0
    room_contract = json.loads(capsys.readouterr().out)
    assert {"schema", "bounds"} <= set(rear) & set(room_contract)
