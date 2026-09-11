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

from jasper.active_speaker.crossover_v2 import alignment_prescription as alignment
from jasper.active_speaker.crossover_v2 import blend_prescription as blend
from jasper.active_speaker.crossover_v2 import driver_prescription as driver
from jasper.active_speaker.crossover_v2 import room_prescription as room
from jasper.active_speaker.crossover_v2 import topology_prescription as topology
from jasper.active_speaker.crossover_v2.evidence_packet import (
    PACKET_SCHEMA_VERSION, PacketSchemaUnsupported,
    build_crossover_evidence_packet, validate_packet,
)
from jasper.active_speaker.crossover_v2.fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND, FC_REJECT_BELOW_DECLARED_FLOOR,
    fc_rejection_scenarios,
)
from jasper.active_speaker.crossover_v2.prescription_contract import (
    CONTRACT_COMMAND, SECTIONS, contract_digests, contract_json, prescription_contracts,
)
from jasper.active_speaker.crossover_v2.round_inputs import contract_sources, default_out, round_inputs
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import room_limits as limits
from jasper.bass_extension import dynamic
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_bass_extension_dynamic import _descriptor
from tests.test_crossover_v2_blend_prescription import _bundle
from tests.test_crossover_v2_driver_prescription import _draft, applied_profile
from tests.test_crossover_v2_room_prescription import _room_median
from tests.test_crossover_v2_harmonic_evidence import _artifact, _bundle as harmonic_bundle


@pytest.fixture
def round_bank(tmp_path):
    bank = tmp_path / "round"
    session, _ = _bundle(bank / "bundle")
    artifact = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    preset = _two_way_preset()
    (artifact / "candidate.json").write_text(json.dumps({"source_preset": preset}))
    draft = _draft()
    for target in draft["driver_safety_profile"]["targets"]:
        target["target_fingerprint"] = target["role"]
        target["recommended_highpass_slope_db_per_octave"] = 12.0
    (bank / "design-draft.json").write_text(json.dumps(draft))
    median = _room_median()
    (bank / "room_median.json").write_text(json.dumps(median))
    (bank / "room_ceiling.json").write_text(json.dumps({
        "ceiling_hz": median["ceiling_hz"], "ceiling_source": median["ceiling_source"],
        "trusted_floor_hz": median["ceiling_hz"],
    }))
    (bank / "room_persistence.json").write_text(json.dumps({
        "ceiling_hz": median["ceiling_hz"], "n_positions": median["n_positions"],
        "features": [{"kind": "dip", "centre_hz": f} for f in (45.0, 90.0)],
    }))
    return bank, session


def _contracts(bank: Path, session: Path):
    return prescription_contracts(
        **contract_sources(session),
        draft=json.loads((bank / "design-draft.json").read_text()),
        receipt=json.loads((session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY/round_receipt.json").read_text()),
    )


@pytest.mark.parametrize("section,door,codes", [
    ("speaker", "driver", driver.DRIVER_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "blend", blend.BLEND_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "alignment", alignment.ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
    ("speaker", "topology", topology.TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS),
    ("room", None, room.ROOM_PRESCRIPTION_REFUSAL_REASONS),
    ("bass", None, frozenset()),
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
    assert bounds["max_composed_boost_db"] == driver.DRIVER_MAX_COMPOSED_BOOST_DB
    assert bounds["max_spl_spend_bound_db"] == driver.MAX_SPL_SPEND_BOUND_DB
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
        assert set(json.loads(capsys.readouterr().out)) == set(SECTIONS)
    else:
        packet = build_crossover_evidence_packet(
            session, driver_draft_path=bank / "design-draft.json", applied_profile_path=profile,
        )
        assert set(packet["contracts"]) == set(SECTIONS)
    assert list(reads.values()) == [1, 1, 1]


@pytest.mark.parametrize("section", SECTIONS)
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
    frozen = bank / "packet.json"
    frozen.write_text(json.dumps(packet))
    assert cli.main(["status", str(bank)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["contracts"] == packet["contracts"]
    assert status["frozen_packet"]["contracts"] == packet["contracts"]


def test_packet_reader_refuses_the_previous_schema_by_name(round_bank):
    packet = build_crossover_evidence_packet(round_bank[1])
    assert PACKET_SCHEMA_VERSION == 2
    packet["artifact_schema_version"] = 1
    with pytest.raises(PacketSchemaUnsupported) as caught:
        validate_packet(packet)
    assert caught.value.reason == "packet_schema_unsupported"


def test_contract_without_round_discloses_missing_evidence_and_bass_defaults(capsys):
    assert cli.main(["contract"]) == 0
    contracts = json.loads(capsys.readouterr().out)
    assert set(contracts) == set(SECTIONS)
    assert contracts["room"]["evidence_status"] == room.ROOM_MEDIAN_UNAVAILABLE
    assert contracts["room"]["bounds"]["cut_floor_db"] is None
    bass = contracts["bass"]
    assert set(bass["schema"]["properties"]) == dynamic._REQUIRED_FIELDS | dynamic._OPTIONAL_FIELDS
    assert bass["schema"]["properties"]["low_boost_db"]["maximum"] == dynamic.NATIVE_LOUDNESS_BOOST_MAX_DB
    assert bass["refusal_type"] == "ValueError"
    assert bass["shared_headroom"]["adr"] == "ADR-0257"


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
    output = default_out(round_inputs(session), session, "room_median.json")
    output.write_text(json.dumps(_room_median()))
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
