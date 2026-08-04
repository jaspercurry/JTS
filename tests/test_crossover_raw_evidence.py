# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import CHAIN_GRID_HZ
from jasper.active_speaker.commissioning_evidence_store import (
    MAX_EVIDENCE_ARTIFACT_BYTES,
)
from jasper.active_speaker.crossover_raw_evidence import (
    RawEvidenceError,
    build_raw_evidence_v1,
    validate_raw_evidence_v1,
)
from jasper.active_speaker.crossover_selector_contract import (
    SELECTOR_SCALAR_KEYS,
    SelectorContractError,
    validate_selector_result_v1,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.transfer_composition import SELECTOR_RESULT_KIND
from tests.test_active_speaker_program_config import _preset, _safety_profile, _session_graph

SESSION = "cap_r15_anchor"


def _roles() -> tuple[RoleBand, RoleBand]:
    return (
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 23000.0)),
    )


def _response(role: str, segment_ids: list[str], freqs: np.ndarray):
    rows = []
    for index, segment_id in enumerate(segment_ids):
        scale = (0.8 if role == "woofer" else 0.6) * (1.0 + 0.01 * index)
        values = scale * np.exp(-1j * freqs * 1e-4)
        rows.append(
            SimpleNamespace(
                role=role,
                segment_id=segment_id,
                freqs_hz=freqs,
                magnitude_db=20.0 * np.log10(np.abs(values)),
                complex_tf=values,
                gating={"gate_window_ms": 4.0},
                snr={
                    "schema_version": 1,
                    "decision_class": "magnitude",
                    "relevant_hz": [1800.0, 2600.0],
                    "bands": [],
                    "worst_relevant": {
                        "band_id": "sweep_mid",
                        "estimated_snr_db": 25.0,
                        "verdict": "ok",
                    },
                    "verdict": "ok",
                },
                validity_floor_hz=200.0 if role == "woofer" else 1600.0,
                repeat_responses=(),
                repeat_index=index or None,
            )
        )
    rows[0].repeat_responses = tuple(rows[1:])
    return rows[0]


def _fixture(*, repeat_count: int = 3, n_source_bins: int = 480):
    program = build_measure_program(
        {"woofer": -30.0, "tweeter": -30.0},
        _roles(),
        total_channels=2,
        repeat_count=repeat_count,
    )
    sweep_ids = {
        role: [
            segment.segment_id
            for segment in program.stimulus_segments()
            if segment.kind == "sweep" and segment.role == role
        ]
        for role in ("woofer", "tweeter")
    }
    freqs = (
        np.asarray(CHAIN_GRID_HZ)
        if n_source_bins == CHAIN_GRID_HZ.size
        else np.linspace(0.0, 24000.0, n_source_bins, dtype=np.float64)
    )
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
        if segment.kind == "sweep"
    )
    analysis = SimpleNamespace(
        driver_responses=(
            _response("woofer", sweep_ids["woofer"], freqs),
            _response("tweeter", sweep_ids["tweeter"], freqs),
        ),
        transfer_composition_fingerprints={
            "woofer": {"fitter_input": "b" * 64},
            "tweeter": {"fitter_input": "c" * 64},
        },
        drift=SimpleNamespace(
            epsilon_ppm=12.0,
            per_role_epsilon_ppm={"woofer": 12.0, "tweeter": 11.8},
        ),
        alignment=SimpleNamespace(delay_us=85.0, polarity="normal"),
        candidate=SimpleNamespace(trim_db={"woofer": 0.0, "tweeter": -1.25}),
        locations=locations,
        capture_integrity=None,
        linearity_ok=True,
    )
    return program, analysis


def _build(*, repeat_count: int = 3, n_source_bins: int = 480):
    program, analysis = _fixture(
        repeat_count=repeat_count, n_source_bins=n_source_bins
    )
    graph = _session_graph(_preset(configured_fc_hz=2000.0))
    return build_raw_evidence_v1(
        session_id=SESSION,
        analysis=analysis,
        program=program,
        capture_wav=b"RIFF-r15-anchor-capture",
        attempt=2,
        session_graph=graph,
        configured_fc_hz=2000.0,
        component_safety_profile=_safety_profile(),
        calibration_identity={"applied": False, "calibration_id": None},
    )


def _resign(payload: dict) -> None:
    payload["evidence_set_fingerprint"] = json_fingerprint(
        {
            key: value
            for key, value in payload.items()
            if key != "evidence_set_fingerprint"
        }
    )


def _refingerprint(value: dict) -> None:
    value["fingerprint"] = json_fingerprint(
        {key: item for key, item in value.items() if key != "fingerprint"}
    )


def test_anchor_raw_record_has_exact_groups_grid_and_role_specific_transfers() -> None:
    payload = _build()
    validate_raw_evidence_v1(payload, expected_session_id=SESSION)

    pose = payload["poses"][0]
    assert [group["accepted_order"] for group in pose["selector_repeat_groups"]] == [
        0,
        1,
        2,
    ]
    assert np.array_equal(payload["frequency_basis_hz"], CHAIN_GRID_HZ)
    assert [item["role"] for item in payload["measurement_protection_by_role"]] == [
        "woofer",
        "tweeter",
    ]
    assert all(
        driver["configured_fitter_input_S"]["fingerprint"]
        for driver in pose["drivers"].values()
    )


@pytest.mark.parametrize(
    ("field", "expected_kwarg"),
    [
        ("measurement_graph", "expected_graph_fingerprint"),
        ("component_safety_profile_identity", "expected_safety_fingerprint"),
        ("calibration_identity", "expected_calibration_fingerprint"),
        ("stimulus_identity", "expected_stimulus_fingerprint"),
    ],
)
def test_raw_expected_identity_mismatch_is_stale(field, expected_kwarg) -> None:
    payload = _build()
    assert payload[field]["fingerprint"] != "f" * 64

    with pytest.raises(RawEvidenceError, match="stale|mixed"):
        validate_raw_evidence_v1(payload, **{expected_kwarg: "f" * 64})


def test_pairing_uses_source_segments_even_when_role_observations_are_reordered() -> None:
    program, analysis = _fixture()
    for primary in analysis.driver_responses:
        primary.repeat_responses = tuple(reversed(primary.repeat_responses))
    payload = build_raw_evidence_v1(
        session_id=SESSION,
        analysis=analysis,
        program=program,
        capture_wav=b"reordered",
        attempt=1,
        session_graph=_session_graph(_preset(configured_fc_hz=2000.0)),
        configured_fc_hz=2000.0,
        component_safety_profile=_safety_profile(),
        calibration_identity={"applied": False, "calibration_id": None},
    )

    groups = payload["poses"][0]["selector_repeat_groups"]
    assert [group["woofer_segment_id"] for group in groups] == [
        "sweep_w",
        "sweep_w_rep",
        "sweep_w_rep2",
    ]
    assert [group["tweeter_segment_id"] for group in groups] == [
        "sweep_t",
        "sweep_t_rep",
        "sweep_t_rep2",
    ]


def test_extra_accepted_repeats_are_retained_but_not_selector_inputs() -> None:
    payload = _build(repeat_count=4)
    pose = payload["poses"][0]

    assert len(pose["selector_repeat_groups"]) == 3
    assert all(len(driver["responses"]) == 4 for driver in pose["drivers"].values())
    validate_raw_evidence_v1(payload)


def test_production_size_fft_is_bounded_below_the_real_store_limit() -> None:
    payload = _build(n_source_bins=524_288 // 2 + 1)
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    assert len(payload["frequency_basis_hz"]) == CHAIN_GRID_HZ.size
    assert len(encoded) < MAX_EVIDENCE_ARTIFACT_BYTES


def test_hf_declared_floor_remains_in_the_bounded_raw_mask() -> None:
    payload = _build()
    hf = payload["poses"][0]["drivers"]["tweeter"]
    first_valid = np.asarray(payload["frequency_basis_hz"])[hf["valid_mask"]][0]
    assert first_valid >= 1600.0
    assert first_valid < 1700.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["poses"][0]["timing_gain_ledger"].__setitem__(
            "kind", "wrong_ledger"
        ),
        lambda raw: raw["poses"][0]["drivers"]["woofer"][
            "repeat_uncertainty_db"
        ].__setitem__(0, 999.0),
        lambda raw: raw["calibration_identity"].__setitem__("arbitrary", True),
        lambda raw: raw["stimulus_identity"].__setitem__("take_id", "wrong_take"),
        lambda raw: raw.__setitem__("legal_fc_bounds_hz", [1000.0, 3000.0]),
        lambda raw: raw["poses"][0]["timing_gain_ledger"][
            "stimulus_gain_db_by_segment"
        ].__setitem__(
            raw["poses"][0]["selector_repeat_groups"][0]["woofer_segment_id"],
            -11.0,
        ),
    ],
)
def test_refingerprinted_semantic_mutations_are_rejected(mutate) -> None:
    payload = _build()
    mutate(payload)
    for value in (
        payload.get("calibration_identity"),
        payload.get("stimulus_identity"),
        payload["poses"][0].get("timing_gain_ledger"),
    ):
        if isinstance(value, dict) and "fingerprint" in value:
            _refingerprint(value)
    _resign(payload)

    with pytest.raises(RawEvidenceError):
        validate_raw_evidence_v1(payload)


def test_refingerprinted_graph_spec_and_P_mismatch_is_rejected() -> None:
    payload = _build()
    graph = payload["measurement_graph"]
    graph["canonical_spec"]["roles"][1]["required_protection_filters"][0][
        "cutoff_hz"
    ] = 5000.0
    graph["canonical_spec_fingerprint"] = json_fingerprint(graph["canonical_spec"])
    _refingerprint(graph)
    component = payload["component_safety_profile_identity"]
    component["targets_by_role"]["tweeter"]["required_protection_filters"][0][
        "cutoff_hz"
    ] = 5000.0
    _refingerprint(component)
    payload["session_fingerprint"] = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_crossover_raw_session_identity",
            "session_id": payload["session_id"],
            "measurement_graph_fingerprint": graph["fingerprint"],
            "component_safety_identity_fingerprint": component["fingerprint"],
            "calibration_fingerprint": payload["calibration_identity"]["fingerprint"],
            "stimulus_program_id": payload["stimulus_identity"]["program_id"],
        }
    )
    _resign(payload)

    with pytest.raises(RawEvidenceError, match="measurement P"):
        validate_raw_evidence_v1(payload)


def test_five_pose_record_rejects_reused_capture_and_take_identities() -> None:
    payload = _build()
    contracts = [
        ("anchor_00", 0, 0.0, "anchor", "design_axis"),
        ("left_12_cm", 1, -12.0, "lateral", "onax"),
        ("right_12_cm", 2, 12.0, "lateral", "onax"),
        ("left_40_cm", 3, -40.0, "lateral", "offax"),
        ("right_40_cm", 4, 40.0, "lateral", "offax"),
    ]
    anchor = payload["poses"][0]
    payload["poses"] = []
    for pose_id, order, offset, pose_role, evidence_role in contracts:
        pose = copy.deepcopy(anchor)
        pose.update(
            pose_id=pose_id,
            order=order,
            lateral_offset_cm=offset,
            pose_role=pose_role,
            evidence_role=evidence_role,
        )
        payload["poses"].append(pose)
    _resign(payload)

    with pytest.raises(RawEvidenceError, match="distinct"):
        validate_raw_evidence_v1(payload)


def _metrics(first_value: float) -> dict:
    return {
        key: {
            "value": (
                int(first_value)
                if key == "filter_biquad_count"
                else first_value
                if key == SELECTOR_SCALAR_KEYS[0]
                else 1.0
            ),
            "uncertainty_half_width": 0.0,
        }
        for key in SELECTOR_SCALAR_KEYS
    }


def _trace(left_fc: float, right_fc: float, left: dict, right: dict) -> dict:
    key = SELECTOR_SCALAR_KEYS[0]
    return {
        "left_fc_hz": left_fc,
        "right_fc_hz": right_fc,
        "examined_keys": [
            {
                "key": key,
                "left": left[key],
                "right": right[key],
                "verdict": "left",
            }
        ],
        "first_deciding_key": key,
        "decision": "left",
    }


def _tie_trace(left_fc: float, right_fc: float, left: dict, right: dict) -> dict:
    return {
        "left_fc_hz": left_fc,
        "right_fc_hz": right_fc,
        "examined_keys": [
            {
                "key": key,
                "left": left[key],
                "right": right[key],
                "verdict": "tied",
            }
            for key in SELECTOR_SCALAR_KEYS
        ],
        "first_deciding_key": None,
        "decision": "tie",
    }


def _selector() -> dict:
    grid = [1900.0, 2100.0]
    keys = [format(value, ".17g") for value in grid]
    metrics = [_metrics(1.0), _metrics(2.0)]
    candidate_masks = {key: ("a" if index == 0 else "b") * 64 for index, key in enumerate(keys)}
    candidates = []
    for index, fc in enumerate(grid):
        candidates.append(
            {
                "fc_hz": fc,
                "protection_transfer_fingerprint": "1" * 64,
                "configured_transfer_fingerprint": "2" * 64,
                "composition_policy_fingerprint": "3" * 64,
                "required_mask_fingerprint": candidate_masks[keys[index]],
                "transfer_ratio_fingerprint": "4" * 64,
                "fitter_input_fingerprint": "5" * 64,
                "complete_prescription_fingerprint": "6" * 64,
                "scalar_metrics": metrics[index],
                "admissible": True,
                "inadmissibility_reason_codes": [],
            }
        )
    trace = [_trace(grid[0], grid[1], metrics[0], metrics[1])]
    pose_sets = {
        "left_only": ["anchor_00", "left_12_cm", "left_40_cm"],
        "right_only": ["anchor_00", "right_12_cm", "right_40_cm"],
        "omit_left_40_cm": [
            "anchor_00",
            "left_12_cm",
            "right_12_cm",
            "right_40_cm",
        ],
        "omit_right_40_cm": [
            "anchor_00",
            "left_12_cm",
            "right_12_cm",
            "left_40_cm",
        ],
    }
    core = {
        "schema_version": 1,
        "kind": SELECTOR_RESULT_KIND,
        "evidence_set_id": "session:anchor_00",
        "evidence_set_fingerprint": "7" * 64,
        "grid_policy_fingerprint": "8" * 64,
        "candidate_interval_bound_fingerprints": {
            key: "9" * 64
            for key in (
                "component_policy",
                "measurement_protection",
                "measured_trust",
                "meaningful_contribution",
                "fit_authority",
            )
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
        "metric_policy_fingerprint": "a" * 64,
        "common_mask_fingerprint": "b" * 64,
        "role_mask_fingerprints": {"woofer": "c" * 64, "tweeter": "d" * 64},
        "candidate_mask_fingerprints": candidate_masks,
        "mask_bin_counts": {
            "common": 10,
            "roles": {"woofer": 8, "tweeter": 9},
            "candidates": {keys[0]: 7, keys[1]: 6},
        },
        "repeat_realization_policy_fingerprint": "e" * 64,
        "comparison_policy_fingerprint": "f" * 64,
        "candidate_records": candidates,
        "comparison_trace": trace,
        "sensitivity_check_results": [
            {
                "check_id": check_id,
                "included_pose_ids": poses,
                "candidate_scalar_metrics": {
                    keys[0]: copy.deepcopy(metrics[0]),
                    keys[1]: copy.deepcopy(metrics[1]),
                },
                "comparison_trace": copy.deepcopy(trace),
                "unbeaten_fc_hz": [1900.0],
                "subreason": "unique",
            }
            for check_id, poses in pose_sets.items()
        ],
        "outcome": {
            "status": "selected",
            "selected_fc_hz": 1900.0,
            "retained_fc_hz": None,
            "reason_codes": ["unique_lexicographic_dominance"],
        },
    }
    return {**core, "fingerprint": json_fingerprint(core)}


def _resign_selector(raw: dict) -> None:
    raw["fingerprint"] = json_fingerprint(
        {key: value for key, value in raw.items() if key != "fingerprint"}
    )


def test_complete_minimal_selector_envelope_is_valid() -> None:
    validate_selector_result_v1(_selector(), configured_fc_hz=1900.0)


def test_selector_abstention_requires_the_exact_admissible_configured_fallback() -> None:
    value = _selector()
    same_metrics = copy.deepcopy(value["candidate_records"][0]["scalar_metrics"])
    value["candidate_records"][1]["scalar_metrics"] = same_metrics
    value["comparison_trace"] = [
        _tie_trace(1900.0, 2100.0, same_metrics, same_metrics)
    ]
    for sensitivity in value["sensitivity_check_results"]:
        sensitivity["candidate_scalar_metrics"]["2100"] = copy.deepcopy(
            same_metrics
        )
        sensitivity["comparison_trace"] = [
            _tie_trace(1900.0, 2100.0, same_metrics, same_metrics)
        ]
        sensitivity["unbeaten_fc_hz"] = [1900.0, 2100.0]
        sensitivity["subreason"] = "metric_uncertainty_tie"
    value["outcome"] = {
        "status": "abstained",
        "selected_fc_hz": None,
        "retained_fc_hz": 1900.0,
        "reason_codes": ["metric_uncertainty_tie"],
    }
    _resign_selector(value)

    validate_selector_result_v1(value, configured_fc_hz=1900.0)
    with pytest.raises(SelectorContractError, match="fallback"):
        validate_selector_result_v1(value, configured_fc_hz=2100.0)


def test_selector_no_admissible_refusal_requires_its_exact_reason() -> None:
    value = _selector()
    for candidate in value["candidate_records"]:
        candidate["admissible"] = False
        candidate["inadmissibility_reason_codes"] = ["fit_failed"]
    value["comparison_trace"] = []
    for sensitivity in value["sensitivity_check_results"]:
        sensitivity["candidate_scalar_metrics"] = {}
        sensitivity["comparison_trace"] = []
        sensitivity["unbeaten_fc_hz"] = []
        sensitivity["subreason"] = "cyclic_comparison"
    value["outcome"] = {
        "status": "refused",
        "selected_fc_hz": None,
        "retained_fc_hz": None,
        "reason_codes": ["no_admissible_candidate"],
    }
    _resign_selector(value)

    validate_selector_result_v1(value, configured_fc_hz=1900.0)
    value["outcome"]["reason_codes"] = [
        "configured_fc_inadmissible_for_abstention"
    ]
    _resign_selector(value)
    with pytest.raises(SelectorContractError, match="no-admissible"):
        validate_selector_result_v1(value, configured_fc_hz=1900.0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("candidate_mask_fingerprints", {}),
        lambda value: value["mask_bin_counts"].__setitem__("common", 0),
        lambda value: value.__setitem__("comparison_trace", []),
        lambda value: value.__setitem__("sensitivity_check_results", []),
        lambda value: value["candidate_records"][0].__setitem__(
            "inadmissibility_reason_codes", ["fit_failed"]
        ),
        lambda value: value["outcome"].__setitem__("selected_fc_hz", 2100.0),
    ],
)
def test_selector_mandatory_meaning_mutations_are_rejected(mutate) -> None:
    value = _selector()
    mutate(value)
    _resign_selector(value)
    with pytest.raises(SelectorContractError):
        validate_selector_result_v1(value, configured_fc_hz=1900.0)
