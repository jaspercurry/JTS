# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_raw_evidence import (
    SELECTOR_SCALAR_KEYS,
    RawEvidenceError,
    build_raw_evidence_v1,
    measurement_protection_from_role_bands,
    protected_neutral_graph_identity,
    validate_protected_neutral_program_routing,
    validate_raw_evidence_v1,
    validate_selector_result_v1,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.transfer_composition import (
    RAW_EVIDENCE_KIND,
    SELECTOR_RESULT_KIND,
)


SESSION = "cap_r15_anchor"


def _response(role: str, freqs: np.ndarray, scale: float):
    def one(index: int):
        values = scale * (1.0 + 0.01 * index) * np.exp(-1j * freqs * 1e-4)
        return SimpleNamespace(
            role=role,
            freqs_hz=freqs,
            magnitude_db=20.0 * np.log10(np.abs(values)),
            complex_tf=values,
            gating={"gate_window_ms": 4.0},
            snr={"valid": True, "minimum_db": 25.0},
            validity_floor_hz=200.0 if role == "woofer" else 1600.0,
            repeat_responses=(),
            repeat_index=index,
        )

    primary = one(0)
    primary.repeat_responses = (one(1), one(2))
    return primary


def _fixture(*, linearity_ok: bool = True, gain_db: float = -30.0):
    roles = (
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 23000.0)),
    )
    program = build_measure_program(
        {"woofer": gain_db, "tweeter": gain_db}, roles, total_channels=3
    )
    freqs = np.asarray([200.0, 800.0, 1600.0, 2000.0, 4000.0, 5800.0])
    locations = tuple(
        SimpleNamespace(
            segment_id=segment.segment_id,
            scheduled_start=segment.start_sample,
            located_start=segment.start_sample + 3,
            residual_samples=3.0,
            confidence=0.95,
            clipped=False,
        )
        for segment in program.stimulus_segments()
    )
    analysis = SimpleNamespace(
        driver_responses=(
            _response("woofer", freqs, 0.8),
            _response("tweeter", freqs, 0.6),
        ),
        transfer_composition_fingerprints={
            "woofer": {"fitter_input": "b" * 64},
            "tweeter": {"fitter_input": "c" * 64},
        },
        drift=SimpleNamespace(epsilon_ppm=12.0, per_role_epsilon_ppm={"woofer": 12.0, "tweeter": 11.8}),
        alignment=SimpleNamespace(delay_us=85.0, polarity="normal"),
        candidate=SimpleNamespace(trim_db={"woofer": 0.0, "tweeter": -1.25}),
        locations=locations,
        capture_integrity=None,
        linearity_ok=linearity_ok,
    )
    protection = measurement_protection_from_role_bands(roles)
    graph = protected_neutral_graph_identity(
        "exact-yaml\n",
        protection_by_role=protection,
        role_channels={"woofer": 0, "tweeter": 1},
        summed_channel=2,
        polarity_sign_by_role={"woofer": 1, "tweeter": 1},
        limiter_threshold_dbfs=-12.0,
        volume_ceiling_db=0.0,
    )
    return roles, program, analysis, protection, graph


def _build(**kwargs):
    roles, program, analysis, protection, graph = _fixture(**kwargs)
    payload = build_raw_evidence_v1(
        session_id=SESSION,
        analysis=analysis,
        program=program,
        capture_wav=b"RIFF-r15-anchor-capture",
        attempt=2,
        roles_bands=roles,
        measurement_graph=graph,
        protection_by_role=protection,
        polarity_sign_by_role={"woofer": 1, "tweeter": 1},
        configured_fc_hz=2000.0,
        component_safety_profile={
            "profile_fingerprint": "a" * 64,
            "targets": [
                {
                    "role": "woofer",
                    "target_id": "speaker:woofer",
                    "target_fingerprint": "d" * 64,
                    "crossover_search_band_hz": [1800.0, 2600.0],
                },
                {
                    "role": "tweeter",
                    "target_id": "speaker:tweeter",
                    "target_fingerprint": "e" * 64,
                    "crossover_search_band_hz": [1900.0, 2800.0],
                },
            ],
        },
        calibration_identity={"applied": True, "calibration_id": "mic-1"},
        limiter_threshold_dbfs=-12.0,
    )
    return payload


def _resign(payload: dict) -> None:
    payload["evidence_set_fingerprint"] = json_fingerprint(
        {key: value for key, value in payload.items() if key != "evidence_set_fingerprint"}
    )


def _refingerprint_mapping(value: dict) -> None:
    value["fingerprint"] = json_fingerprint(
        {key: item for key, item in value.items() if key != "fingerprint"}
    )


def test_anchor_raw_v1_has_exact_three_pairs_one_ledger_and_role_specific_p():
    payload = _build()
    assert payload["kind"] == RAW_EVIDENCE_KIND
    assert payload["session_id"] == SESSION
    assert len(payload["poses"]) == 1
    pose = payload["poses"][0]
    assert pose["pose_id"] == "anchor_00"
    assert pose["take_id"] == "anchor_00_take_02"
    assert len(pose["selector_repeat_groups"]) == 3
    assert tuple(pose["drivers"]) == ("woofer", "tweeter")
    assert payload["legal_fc_bounds_hz"] == [1900.0, 2600.0]
    assert pose["drivers"]["woofer"]["driver_identity"] == {
        "role": "woofer",
        "target_id": "speaker:woofer",
        "target_fingerprint": "d" * 64,
    }
    assert pose["timing_gain_ledger"]["kind"] == "jts_crossover_timing_gain_ledger_v1"
    transfers = payload["measurement_protection_by_role"]
    assert [item["role"] for item in transfers] == ["woofer", "tweeter"]
    assert transfers[0]["filter_identity"]["sections"] != transfers[1]["filter_identity"]["sections"]
    assert pose["linearity_limiter_proof"]["limiter_engaged"] is False
    assert payload["measurement_graph"]["lifecycle"] == {
        "load": "inline_transient_under_existing_dsp_writer_lock",
        "restore": "exact_entry_config_on_success_abort_or_playback_failure",
        "durable_profile_mutated": False,
        "undo_mutated": False,
        "process_restart_target": "unchanged_persisted_entry_config",
    }
    validate_raw_evidence_v1(payload, expected_session_id=SESSION)
    validate_raw_evidence_v1(
        json.loads(json.dumps(payload, sort_keys=True)),
        expected_session_id=SESSION,
    )


def test_protected_neutral_graph_rejects_misrouted_or_wrong_width_program():
    _roles, program, _analysis, _protection, graph = _fixture()
    validate_protected_neutral_program_routing(program, graph)

    wrong_width = copy.copy(program)
    object.__setattr__(wrong_width, "channels", 2)
    with pytest.raises(RawEvidenceError, match="channel count"):
        validate_protected_neutral_program_routing(wrong_width, graph)

    segments = list(program.segments)
    target = next(
        index for index, segment in enumerate(segments) if segment.role == "tweeter"
    )
    segments[target] = copy.copy(segments[target])
    object.__setattr__(segments[target], "channel", 2)
    misrouted = copy.copy(program)
    object.__setattr__(misrouted, "segments", tuple(segments))
    with pytest.raises(RawEvidenceError, match="stimulus routing"):
        validate_protected_neutral_program_routing(misrouted, graph)


def test_raw_v1_rejects_stale_schema_and_mixed_capture_identity():
    payload = _build()
    with pytest.raises(RawEvidenceError, match="stale session"):
        validate_raw_evidence_v1(payload, expected_session_id="another-session")

    schema = copy.deepcopy(payload)
    schema["kind"] = "jts_crossover_raw_evidence_v2"
    _resign(schema)
    with pytest.raises(RawEvidenceError, match="schema or kind"):
        validate_raw_evidence_v1(schema)

    mixed = copy.deepcopy(payload)
    mixed["poses"][0]["selector_repeat_groups"][1]["woofer_capture_id"] = "other"
    _resign(mixed)
    with pytest.raises(RawEvidenceError, match="capture/take identity"):
        validate_raw_evidence_v1(mixed)


def test_raw_v1_rejects_resigned_policy_or_p_not_emitted_by_bound_graph():
    policy = copy.deepcopy(_build())
    policy["total_transfer_composition_policy"]["ratio_cap_db"] = 13.0
    _refingerprint_mapping(policy["total_transfer_composition_policy"])
    _resign(policy)
    with pytest.raises(RawEvidenceError, match="composition policy"):
        validate_raw_evidence_v1(policy)

    mismatched_p = copy.deepcopy(_build())
    transfer = mismatched_p["measurement_protection_by_role"][0][
        "complex_transfer_P"
    ]
    transfer["real"][2] += 0.01
    values = np.asarray(transfer["real"]) + 1j * np.asarray(transfer["imag"])
    transfer["fingerprint"] = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_crossover_complex_response",
            "real": values.real.tolist(),
            "imag": values.imag.tolist(),
        }
    )
    _resign(mismatched_p)
    with pytest.raises(RawEvidenceError, match="not the emitted transfer"):
        validate_raw_evidence_v1(mismatched_p)


def test_raw_v1_schema_accepts_anchor_or_exact_five_pose_contract_only():
    payload = _build()
    contracts = (
        ("left_12_cm", 1, -12.0, "onax"),
        ("right_12_cm", 2, 12.0, "onax"),
        ("left_40_cm", 3, -40.0, "offax"),
        ("right_40_cm", 4, 40.0, "offax"),
    )
    anchor = payload["poses"][0]
    for pose_id, order, offset_cm, evidence_role in contracts:
        pose = copy.deepcopy(anchor)
        capture_id = hashlib.sha256(pose_id.encode()).hexdigest()
        take_id = f"{pose_id}_take_01"
        pose.update(
            {
                "pose_id": pose_id,
                "order": order,
                "lateral_offset_cm": offset_cm,
                "pose_role": "lateral",
                "evidence_role": evidence_role,
                "capture_id": capture_id,
                "take_id": take_id,
            }
        )
        for role, driver in pose["drivers"].items():
            response_ids = [
                f"{SESSION}:{capture_id}:{role}:{index}" for index in range(3)
            ]
            driver["capture_id"] = capture_id
            driver["take_id"] = take_id
            driver["ordered_response_ids"] = response_ids
            for response, response_id in zip(driver["responses"], response_ids):
                response["response_id"] = response_id
        for index, group in enumerate(pose["selector_repeat_groups"]):
            group.update(
                {
                    "woofer_response_id": pose["drivers"]["woofer"][
                        "ordered_response_ids"
                    ][index],
                    "tweeter_response_id": pose["drivers"]["tweeter"][
                        "ordered_response_ids"
                    ][index],
                    "woofer_capture_id": capture_id,
                    "tweeter_capture_id": capture_id,
                    "woofer_take_id": take_id,
                    "tweeter_take_id": take_id,
                }
            )
        payload["poses"].append(pose)
    _resign(payload)
    validate_raw_evidence_v1(payload)

    partial = copy.deepcopy(payload)
    partial["poses"] = partial["poses"][:2]
    _resign(partial)
    with pytest.raises(RawEvidenceError, match="anchor or all five"):
        validate_raw_evidence_v1(partial)


def test_raw_v1_rejects_non_linear_capture_or_limiter_engagement():
    with pytest.raises(RawEvidenceError, match="linear capture"):
        _build(linearity_ok=False)
    with pytest.raises(RawEvidenceError, match="limiter non-engagement"):
        _build(gain_db=-10.0)
    with pytest.raises(RawEvidenceError, match="limiter non-engagement"):
        _build(gain_db=-12.0)

    roles, program, analysis, protection, graph = _fixture()
    with pytest.raises(RawEvidenceError, match="canonical fingerprint"):
        build_raw_evidence_v1(
            session_id=SESSION,
            analysis=analysis,
            program=program,
            capture_wav=b"RIFF-r15-anchor-capture",
            attempt=2,
            roles_bands=roles,
            measurement_graph=graph,
            protection_by_role=protection,
            polarity_sign_by_role={"woofer": 1, "tweeter": 1},
            configured_fc_hz=2000.0,
            component_safety_profile={
                "targets": [
                    {
                        "role": role,
                        "target_id": f"speaker:{role}",
                        "target_fingerprint": fingerprint * 64,
                        "crossover_search_band_hz": [1900.0, 2500.0],
                    }
                    for role, fingerprint in (("woofer", "d"), ("tweeter", "e"))
                ]
            },
            calibration_identity={"applied": False, "calibration_id": None},
            limiter_threshold_dbfs=-12.0,
        )


def test_selector_v1_contract_is_strict_without_implementing_selection():
    grid = [1800.0, 2000.0, 2200.0]
    candidates = []
    for fc in grid:
        candidates.append(
            {
                "fc_hz": fc,
                "protection_transfer_fingerprint": "1" * 64,
                "configured_transfer_fingerprint": "2" * 64,
                "composition_policy_fingerprint": "3" * 64,
                "required_mask_fingerprint": "4" * 64,
                "transfer_ratio_fingerprint": "5" * 64,
                "fitter_input_fingerprint": "6" * 64,
                "complete_prescription_fingerprint": "7" * 64,
                "scalar_metrics": {
                    key: {
                        "value": 0 if key == "filter_biquad_count" else 0.0,
                        "uncertainty_half_width": 0.1 if index < 3 else 0.0,
                    }
                    for index, key in enumerate(SELECTOR_SCALAR_KEYS)
                },
                "admissible": True,
                "inadmissibility_reason_codes": [],
            }
        )
    core = {
        "schema_version": 1,
        "kind": SELECTOR_RESULT_KIND,
        "evidence_set_id": f"{SESSION}:anchor_00",
        "evidence_set_fingerprint": "8" * 64,
        "grid_policy_fingerprint": "9" * 64,
        "candidate_interval_bound_fingerprints": {
            "component_policy": "a" * 64,
            "measurement_protection": "b" * 64,
            "measured_trust": "3" * 64,
            "meaningful_contribution": "4" * 64,
            "fit_authority": "5" * 64,
        },
        "candidate_interval_hz": [1800.0, 2200.0],
        "grid_fc": grid,
        "grid_fingerprint": json_fingerprint(
            {
                "schema_version": 1,
                "kind": "jts_crossover_selector_grid_v1",
                "grid_fc": grid,
            }
        ),
        "metric_policy_fingerprint": "d" * 64,
        "common_mask_fingerprint": "e" * 64,
        "role_mask_fingerprints": {"woofer": "f" * 64, "tweeter": "0" * 64},
        "candidate_mask_fingerprints": {},
        "mask_bin_counts": {},
        "repeat_realization_policy_fingerprint": "1" * 64,
        "comparison_policy_fingerprint": "2" * 64,
        "candidate_records": candidates,
        "comparison_trace": [],
        "sensitivity_check_results": [],
        "outcome": {
            "status": "abstained",
            "selected_fc_hz": None,
            "retained_fc_hz": 2000.0,
            "reason_codes": ["metric_uncertainty_tie"],
        },
    }
    payload = {**core, "fingerprint": json_fingerprint(core)}
    validate_selector_result_v1(payload)
    payload["candidate_records"][0]["partial_filter"] = True
    core = {key: value for key, value in payload.items() if key != "fingerprint"}
    payload["fingerprint"] = json_fingerprint(core)
    with pytest.raises(RawEvidenceError, match="candidate schema"):
        validate_selector_result_v1(payload)

    invalid_reason = copy.deepcopy({**core, "fingerprint": json_fingerprint(core)})
    del invalid_reason["candidate_records"][0]["partial_filter"]
    invalid_reason["outcome"]["reason_codes"] = ["no_unique_winner"]
    _refingerprint_mapping(invalid_reason)
    with pytest.raises(RawEvidenceError, match="selector outcome"):
        validate_selector_result_v1(invalid_reason)
