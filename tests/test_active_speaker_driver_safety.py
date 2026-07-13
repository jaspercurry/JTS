# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from jasper.active_speaker.design_draft import (
    ActiveSpeakerDesignDraftError,
    build_design_draft,
    load_design_draft,
    normalise_manual_settings,
    save_design_draft,
)
from jasper.active_speaker.driver_safety import (
    DRIVER_RESEARCH_KIND,
    DRIVER_RESEARCH_REQUEST_KIND,
    DRIVER_SAFETY_PROFILE_KIND,
    DriverSafetyProfileError,
    build_driver_research_prompt,
    build_driver_research_request,
    build_driver_safety_profile,
    evaluate_driver_safety_profile,
    validate_driver_research_request,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


def _operator_inputs() -> dict[str, str]:
    return {
        "woofer": "Example W6",
        "tweeter": "Example T1",
        "notes": "Sealed bench cabinet",
    }


def _manual_settings() -> dict:
    raw = {
        "drivers": [
            {
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "Example W6",
                "hard_excitation_band_hz": [25, 5000],
                "required_protection_filters": [
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 3000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "measurement_band_hz": [35, 4500],
                "crossover_search_band_hz": [1200, 3500],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -24,
                    "max_sweep_duration_s": 4,
                    "max_repeat_count": 3,
                    "minimum_cooldown_s": 1,
                },
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 132,
                    "baffle_width_mm": 210,
                },
            },
            {
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "Example T1",
                "hard_excitation_band_hz": [5000, 22000],
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 5000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "measurement_band_hz": [5000, 20000],
                "crossover_search_band_hz": [5000, 8000],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -65,
                    "max_sweep_duration_s": 3,
                    "max_repeat_count": 2,
                    "minimum_cooldown_s": 0,
                },
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 25,
                },
            },
        ],
        "crossover_candidates": [],
    }
    normalised = normalise_manual_settings(raw)
    assert normalised is not None
    return normalised


def _research_result(request: dict) -> dict:
    drivers = []
    for target in request["targets"]:
        role = target["role"]
        source = f"https://example.test/{role}"
        if role == "woofer":
            safety = {
                "hard_excitation_band_hz": [25, 5000],
                "required_protection_filters": [
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 3000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "measurement_band_hz": [35, 4500],
                "crossover_search_band_hz": [1200, 3500],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -24,
                    "max_sweep_duration_s": 4,
                    "max_repeat_count": 3,
                    "minimum_cooldown_s": 1,
                },
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 132,
                    "baffle_width_mm": 210,
                },
            }
        else:
            safety = {
                "hard_excitation_band_hz": [5000, 22000],
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 5000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "measurement_band_hz": [5000, 20000],
                "crossover_search_band_hz": [5000, 8000],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -65,
                    "max_sweep_duration_s": 3,
                    "max_repeat_count": 2,
                    "minimum_cooldown_s": 0,
                },
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 25,
                },
            }
        safety_fields = (
            "hard_excitation_band_hz",
            "required_protection_filters",
            "measurement_band_hz",
            "crossover_search_band_hz",
            "level_duration_limits",
            "cabinet",
        )
        drivers.append(
            {
                "target_id": target["target_id"],
                "target_fingerprint": target["target_fingerprint"],
                "role": role,
                "model": target["manufacturer_and_model"],
                **safety,
                "unknowns": ["thermal compression limit not published"],
                "field_provenance": {
                    field: {
                        "confidence": "medium",
                        "basis": "Conservative range from the manufacturer data.",
                        "sources": [source],
                    }
                    for field in safety_fields
                },
                "sources": [source],
            }
        )
    return {
        "artifact_schema_version": 2,
        "kind": DRIVER_RESEARCH_KIND,
        "request_fingerprint": request["request_fingerprint"],
        "drivers": drivers,
        "crossover_candidates": [],
    }


def _stereo_topology() -> OutputTopology:
    raw = mono_output_topology(card_id=None).to_dict()
    raw["topology_id"] = "bench_stereo"
    raw["name"] = "Bench stereo pair"
    raw["speaker_groups"] = [
        {
            "id": group_id,
            "label": f"{group_id.title()} cabinet",
            "kind": group_id,
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": output_base,
                    "identity_verified": True,
                },
                {
                    "role": "tweeter",
                    "physical_output_index": output_base + 1,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "software_guard_requested",
                },
            ],
        }
        for group_id, output_base in (("left", 0), ("right", 2))
    ]
    raw["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right",
    }
    return OutputTopology.from_mapping(raw)


def _topology_with_tweeter_style(style: str) -> OutputTopology:
    raw = mono_output_topology(card_id=None).to_dict()
    raw["speaker_groups"][0]["channels"][1]["driver_style"] = style
    return OutputTopology.from_mapping(raw)


def _stereo_manual_settings() -> dict:
    mono = _manual_settings()
    drivers = []
    for group_id in ("left", "right"):
        for original in mono["drivers"]:
            driver = deepcopy(original)
            driver["target_id"] = f"{group_id}:{driver['role']}"
            driver["model"] = f"{group_id.title()} {driver['model']}"
            driver.pop("source", None)
            drivers.append(driver)
    normalised = normalise_manual_settings(
        {"drivers": drivers, "crossover_candidates": []}
    )
    assert normalised is not None
    return normalised


def _stereo_operator_inputs() -> dict:
    return {
        "target_models": {
            "left:woofer": "Left Example W6",
            "left:tweeter": "Left Example T1",
            "right:woofer": "Right Example W6",
            "right:tweeter": "Right Example T1",
        }
    }


def _refingerprint_profile(profile: dict) -> None:
    core = {
        key: profile[key]
        for key in (
            "artifact_schema_version",
            "kind",
            "topology_id",
            "targets",
            "research",
            "authority",
            "authorizes_playback",
        )
    }
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    profile["profile_fingerprint"] = fingerprint
    if isinstance(profile.get("confirmation"), dict):
        profile["confirmation"]["confirmed_fingerprint"] = fingerprint


def test_research_request_and_prompt_bind_exact_physical_targets() -> None:
    topology = mono_output_topology(card_id=None)

    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
    prompt = build_driver_research_prompt(request)

    assert request["kind"] == DRIVER_RESEARCH_REQUEST_KIND
    assert request["artifact_schema_version"] == 1
    assert len(request["request_fingerprint"]) == 64
    assert [target["target_id"] for target in request["targets"]] == [
        "mono:woofer",
        "mono:tweeter",
    ]
    assert all(len(target["target_fingerprint"]) == 64 for target in request["targets"])
    assert request["targets"][1]["physical_output_index"] == 1
    assert request["targets"][0]["operator_declared_context"]["cabinet"] == {
        "enclosure_kind": "sealed",
        "lf_reconstruction_capability": "sealed_single_radiator_supported",
        "radiator_count": 1,
        "effective_radiating_diameter_mm": 132.0,
        "baffle_width_mm": 210.0,
    }
    assert "hard excitation band distinct" in prompt
    assert "Echo request_fingerprint" in prompt
    assert request["request_fingerprint"] in prompt


def test_v2_research_refuses_stale_request_or_target_binding() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())
    research = _research_result(request)

    stale_request = deepcopy(research)
    stale_request["request_fingerprint"] = "0" * 64
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="request_fingerprint does not match",
    ):
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=stale_request,
            operator_inputs=_operator_inputs(),
        )

    wrong_target = deepcopy(research)
    wrong_target["drivers"][1]["target_fingerprint"] = "f" * 64
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="targets do not exactly match",
    ):
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=wrong_target,
            operator_inputs=_operator_inputs(),
        )


def test_confirmed_profile_uses_visible_values_and_never_authorizes_audio() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())
    research = _research_result(request)

    draft = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=research,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        confirm_safety_profile=True,
        created_at="2026-07-13T12:00:00Z",
    )

    profile = draft["driver_safety_profile"]
    assert profile["kind"] == DRIVER_SAFETY_PROFILE_KIND
    assert profile["status"] == "confirmed"
    assert profile["authority"] == "operator_visible_values"
    assert profile["authorizes_playback"] is False
    assert profile["targets"][1]["hard_excitation_band_hz"] == [5000.0, 22000.0]
    assert profile["targets"][1]["unknowns"] == [
        "thermal compression limit not published"
    ]
    assert profile["confirmation"] == {
        "confirmed_fingerprint": profile["profile_fingerprint"],
        "confirmed_at": "2026-07-13T12:00:00Z",
        "method": "operator_reviewed_visible_values",
    }
    assert draft["driver_safety_profile_evaluation"] == {
        "status": "confirmed",
        "confirmed_and_current": True,
        "profile_fingerprint": profile["profile_fingerprint"],
        "reasons": [],
        "authorizes_playback": False,
    }
    assert draft["permissions"]["may_not_emit_audio"] is True
    assert draft["safety"]["driver_safety_profile_authorizes_playback"] is False


def test_confirmation_requires_complete_bands_filters_and_timestamp() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][1].pop("required_protection_filters")

    with pytest.raises(DriverSafetyProfileError, match="required_highpass_missing"):
        build_driver_safety_profile(
            topology,
            manual_settings=manual,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )

    with pytest.raises(DriverSafetyProfileError, match="confirmed_at is required"):
        build_driver_safety_profile(
            topology,
            manual_settings=_manual_settings(),
            driver_research=None,
            confirm=True,
        )

    missing_duration = _manual_settings()
    missing_duration["drivers"][0]["level_duration_limits"].pop("max_sweep_duration_s")
    with pytest.raises(DriverSafetyProfileError, match="max_sweep_duration_s_missing"):
        build_driver_safety_profile(
            topology,
            manual_settings=missing_duration,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )


def test_v2_contracts_reject_boolean_versions_values_and_unknown_fields() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())

    bool_version = deepcopy(request)
    bool_version["artifact_schema_version"] = True
    with pytest.raises(DriverSafetyProfileError, match="schema or kind"):
        validate_driver_research_request(bool_version, topology, _operator_inputs())

    research = _research_result(request)
    research["typo_field"] = "must not disappear silently"
    with pytest.raises(ActiveSpeakerDesignDraftError, match="unknown fields"):
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=research,
            operator_inputs=_operator_inputs(),
        )

    bool_value = _research_result(request)
    bool_value["drivers"][0]["hard_excitation_band_hz"][0] = True
    with pytest.raises(ActiveSpeakerDesignDraftError, match="must not be boolean"):
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=bool_value,
            operator_inputs=_operator_inputs(),
        )


def test_profile_confirmation_is_invalidated_by_visible_edit() -> None:
    topology = mono_output_topology(card_id=None)
    confirmed = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    edited = _manual_settings()
    edited["drivers"][1]["hard_excitation_band_hz"] = [4800.0, 22000.0]

    rebuilt = build_driver_safety_profile(
        topology,
        manual_settings=edited,
        driver_research=None,
        prior_profile=confirmed,
    )

    assert rebuilt["profile_fingerprint"] != confirmed["profile_fingerprint"]
    assert rebuilt["confirmation"] is None
    assert evaluate_driver_safety_profile(rebuilt, topology).status == "unconfirmed"


def test_saved_confirmation_is_preserved_only_while_visible_values_match(
    tmp_path: Path,
) -> None:
    topology = mono_output_topology(card_id=None)
    path = tmp_path / "active_speaker_design_draft.json"
    first = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        confirm_safety_profile=True,
        path=path,
        created_at="2026-07-13T12:00:00Z",
    )

    unchanged = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        path=path,
        created_at="2026-07-13T12:01:00Z",
    )
    assert (
        unchanged["driver_safety_profile"]["confirmation"]
        == (first["driver_safety_profile"]["confirmation"])
    )

    edited = _manual_settings()
    edited["drivers"][1]["measurement_band_hz"] = [5000.0, 19000.0]
    changed = save_design_draft(
        topology,
        manual_settings=edited,
        operator_inputs=_operator_inputs(),
        path=path,
        created_at="2026-07-13T12:02:00Z",
    )
    assert changed["driver_safety_profile"]["confirmation"] is None
    assert changed["driver_safety_profile_evaluation"]["status"] == "unconfirmed"
    assert (
        load_design_draft(path)["driver_safety_profile"]
        == (changed["driver_safety_profile"])
    )


def test_profile_refuses_stale_topology_and_fingerprint_tampering() -> None:
    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )

    moved_tweeter = mono_output_topology(card_id=None, tweeter_output=2)
    stale = evaluate_driver_safety_profile(profile, moved_tweeter)
    assert stale.status == "stale"
    assert stale.confirmed_and_current is False

    tampered = deepcopy(profile)
    tampered["targets"][1]["hard_excitation_band_hz"][0] = 200.0
    malformed = evaluate_driver_safety_profile(tampered, topology)
    assert malformed.status == "malformed"
    assert malformed.reasons == ("driver_safety_profile_fingerprint_mismatch",)


def test_evaluation_recomputes_issues_instead_of_trusting_serialized_status() -> None:
    topology = mono_output_topology(card_id=None)
    incomplete_manual = _manual_settings()
    incomplete_manual["drivers"][1].pop("required_protection_filters")
    profile = build_driver_safety_profile(
        topology,
        manual_settings=incomplete_manual,
        driver_research=None,
    )
    assert profile["status"] == "incomplete"

    profile["issues"] = []
    profile["status"] = "confirmed"
    profile["confirmation"] = {
        "confirmed_fingerprint": profile["profile_fingerprint"],
        "confirmed_at": "2026-07-13T12:00:00Z",
        "method": "operator_reviewed_visible_values",
    }

    evaluation = evaluate_driver_safety_profile(profile, topology)
    assert evaluation.status == "malformed"
    assert evaluation.confirmed_and_current is False
    assert evaluation.reasons == ("driver_safety_profile_derived_state_mismatch",)


def test_refingerprinted_noncanonical_target_fields_cannot_be_confirmed() -> None:
    topology = mono_output_topology(card_id=None)
    canonical = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    variants = []

    string_band = deepcopy(canonical)
    string_band["targets"][1]["hard_excitation_band_hz"][0] = "5000"
    variants.append(string_band)

    noncanonical_group = deepcopy(canonical)
    noncanonical_group["targets"][1]["speaker_group_id"] = " mono "
    variants.append(noncanonical_group)

    float_output = deepcopy(canonical)
    float_output["targets"][1]["physical_output_index"] = 1.0
    variants.append(float_output)

    noncanonical_provenance = deepcopy(canonical)
    noncanonical_provenance["targets"][1]["field_provenance"] = {
        "hard_excitation_band_hz": {
            "confidence": "medium",
            "basis": "  padded evidence  ",
            "sources": [],
        }
    }
    variants.append(noncanonical_provenance)

    for profile in variants:
        _refingerprint_profile(profile)
        evaluation = evaluate_driver_safety_profile(profile, topology)
        assert evaluation.status == "malformed"
        assert evaluation.confirmed_and_current is False
        assert evaluation.reasons == ("driver_safety_profile_schema_invalid",)


def test_cabinet_reconstruction_is_explicit_and_fail_closed() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][0]["cabinet"] = {
        "enclosure_kind": "vented",
        "radiator_count": 2,
        "lf_reconstruction_capability": "refused_multi_radiator_contract_missing",
    }

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )

    woofer = profile["targets"][0]
    assert woofer["cabinet"]["enclosure_kind"] == "vented"
    assert woofer["cabinet"]["lf_reconstruction_capability"] == (
        "refused_multi_radiator_contract_missing"
    )
    assert profile["authorizes_playback"] is False


def test_legacy_research_remains_readable_but_advisory() -> None:
    topology = mono_output_topology(card_id=None)
    legacy = {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {"role": "woofer", "model": "Legacy W6"},
            {"role": "tweeter", "model": "Legacy T1"},
        ],
        "crossover_candidates": [],
    }

    draft = build_design_draft(topology, driver_research=legacy)

    assert draft["driver_research"]["artifact_schema_version"] == 1
    assert draft["driver_safety_profile"]["status"] == "incomplete"
    assert draft["driver_safety_profile_evaluation"]["confirmed_and_current"] is False
    assert draft["safety"]["research_is_advisory"] is True


def test_stereo_targets_require_physical_target_values_and_preserve_asymmetry() -> None:
    topology = _stereo_topology()
    legacy = _manual_settings()
    for driver in legacy["drivers"]:
        driver.pop("target_id", None)
        driver.pop("source", None)

    incomplete = build_driver_safety_profile(
        topology,
        manual_settings=legacy,
        driver_research=None,
    )
    assert incomplete["status"] == "incomplete"
    assert [target["target_values_binding"] for target in incomplete["targets"]] == [
        "missing",
        "missing",
        "missing",
        "missing",
    ]
    assert {issue["code"] for issue in incomplete["issues"]}.issuperset(
        {
            "left:woofer:target_specific_values_missing",
            "left:tweeter:target_specific_values_missing",
            "right:woofer:target_specific_values_missing",
            "right:tweeter:target_specific_values_missing",
        }
    )
    with pytest.raises(
        DriverSafetyProfileError,
        match="target_specific_values_missing",
    ):
        build_driver_safety_profile(
            topology,
            manual_settings=legacy,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )

    explicit = build_driver_safety_profile(
        topology,
        manual_settings=_stereo_manual_settings(),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    assert explicit["status"] == "confirmed"
    assert {target["target_id"]: target["model"] for target in explicit["targets"]} == {
        "left:woofer": "Left Example W6",
        "left:tweeter": "Left Example T1",
        "right:woofer": "Right Example W6",
        "right:tweeter": "Right Example T1",
    }


def test_stereo_research_request_uses_exact_target_models() -> None:
    request = build_driver_research_request(
        _stereo_topology(),
        _stereo_operator_inputs(),
        _stereo_manual_settings(),
    )

    assert {
        target["target_id"]: target["manufacturer_and_model"]
        for target in request["targets"]
    } == _stereo_operator_inputs()["target_models"]


@pytest.mark.parametrize(
    ("field", "value"),
    (("role", "woofer"), ("model", "Wrong T1")),
)
def test_v2_research_refuses_role_or_model_mismatch(field: str, value: str) -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())
    research = _research_result(request)
    research["drivers"][1][field] = value

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="targets do not exactly match",
    ):
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=research,
            manual_settings=_manual_settings(),
            operator_inputs=_operator_inputs(),
        )


def test_code_policy_refuses_unsafe_peak_and_highpass() -> None:
    topology = mono_output_topology(card_id=None)
    unsafe_peak = _manual_settings()
    unsafe_peak["drivers"][1]["level_duration_limits"][
        "max_effective_peak_dbfs"
    ] = -64.0
    with pytest.raises(
        DriverSafetyProfileError,
        match="max_effective_peak_above_code_policy",
    ):
        build_driver_safety_profile(
            topology,
            manual_settings=unsafe_peak,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )

    compression = _topology_with_tweeter_style("compression_driver")
    unsafe_highpass = _manual_settings()
    tweeter = unsafe_highpass["drivers"][1]
    tweeter["hard_excitation_band_hz"] = [1800.0, 22000.0]
    tweeter["measurement_band_hz"] = [1800.0, 20000.0]
    tweeter["crossover_search_band_hz"] = [2000.0, 8000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 1800.0
    with pytest.raises(
        DriverSafetyProfileError,
        match="highpass_below_code_policy",
    ):
        build_driver_safety_profile(
            compression,
            manual_settings=unsafe_highpass,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )


def test_driver_style_stales_only_safety_binding_not_measurement_identity() -> None:
    compression = _topology_with_tweeter_style("compression_driver")
    ribbon = _topology_with_tweeter_style("ribbon_tweeter")
    compression_targets = active_driver_targets(compression)
    ribbon_targets = active_driver_targets(ribbon)
    assert [target["target_fingerprint"] for target in compression_targets] == [
        target["target_fingerprint"] for target in ribbon_targets
    ]

    profile = build_driver_safety_profile(
        compression,
        manual_settings=_manual_settings(),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    assert profile["targets"][1]["driver_style"] == "compression_driver"
    evaluation = evaluate_driver_safety_profile(profile, ribbon)
    assert evaluation.status == "stale"
    assert evaluation.reasons == ("driver_safety_profile_target_mismatch",)


def test_sealed_cabinet_without_baffle_width_has_typed_refusal() -> None:
    manual = _manual_settings()
    manual["drivers"][0]["cabinet"].pop("baffle_width_mm")
    manual = normalise_manual_settings(manual)
    assert manual is not None

    profile = build_driver_safety_profile(
        mono_output_topology(card_id=None),
        manual_settings=manual,
        driver_research=None,
    )

    assert profile["targets"][0]["cabinet"]["lf_reconstruction_capability"] == (
        "refused_geometry_incomplete"
    )


def test_operator_override_drops_research_provenance_for_changed_field() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())
    imported = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=_research_result(request),
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
    )
    edited = _manual_settings()
    edited["drivers"][1]["cabinet"]["baffle_width_mm"] = 150.0

    profile = build_driver_safety_profile(
        topology,
        manual_settings=edited,
        driver_research=imported["driver_research"],
    )
    tweeter = profile["targets"][1]
    assert tweeter["field_provenance"]["cabinet"] == {
        "confidence": "unknown",
        "basis": (
            "Operator-entered visible value; no matching research assertion "
            "is authoritative."
        ),
        "sources": [],
    }
    assert (
        "cabinet: operator override has no matching research source"
        in tweeter["unknowns"]
    )


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda manual: manual["drivers"][1].update({"role": "woofer"}),
            "role does not match target_id",
        ),
        (
            lambda manual: manual["drivers"][1].update(
                {"target_id": "missing:tweeter"}
            ),
            "not a current physical target",
        ),
        (
            lambda manual: manual["drivers"].append(
                {**deepcopy(manual["drivers"][1]), "target_id": None}
            ),
            "resolves target mono:tweeter more than once",
        ),
    ],
)
def test_manual_target_binding_refuses_contradictions(mutate, match: str) -> None:
    manual = _manual_settings()
    mutate(manual)

    with pytest.raises(DriverSafetyProfileError, match=match):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=manual,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-13T12:00:00Z",
        )

    with pytest.raises(DriverSafetyProfileError, match=match):
        build_driver_research_request(
            mono_output_topology(card_id=None),
            _operator_inputs(),
            manual,
        )


def test_stereo_duplicate_legacy_role_rows_are_rejected() -> None:
    legacy = _manual_settings()
    for driver in legacy["drivers"]:
        driver.pop("target_id", None)
    legacy["drivers"].append(deepcopy(legacy["drivers"][0]))

    with pytest.raises(DriverSafetyProfileError, match="duplicate legacy role woofer"):
        build_driver_safety_profile(
            _stereo_topology(),
            manual_settings=legacy,
            driver_research=None,
        )


def test_direct_builder_canonicalizes_manual_values_and_forged_cabinet_claim() -> None:
    manual = _manual_settings()
    woofer = manual["drivers"][0]
    woofer["cabinet"].pop("baffle_width_mm")
    woofer["cabinet"][
        "lf_reconstruction_capability"
    ] = "sealed_single_radiator_supported"
    woofer["hard_excitation_band_hz"] = [25, 5000]
    woofer["required_protection_filters"][0].pop("family_or_equivalent")

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )

    assert profile["targets"][0]["hard_excitation_band_hz"] == [25.0, 5000.0]
    assert profile["targets"][0]["required_protection_filters"][0][
        "family_or_equivalent"
    ] == "equivalent_or_steeper"
    assert profile["targets"][0]["cabinet"]["lf_reconstruction_capability"] == (
        "refused_geometry_incomplete"
    )
    evaluation = evaluate_driver_safety_profile(profile, topology)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True


def test_direct_builder_rejects_boolean_and_unknown_manual_fields() -> None:
    boolean = _manual_settings()
    boolean["drivers"][1]["hard_excitation_band_hz"][0] = True
    with pytest.raises(DriverSafetyProfileError, match="must not be boolean"):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=boolean,
            driver_research=None,
        )

    unknown = _manual_settings()
    unknown["drivers"][0]["safe_because_ai_said_so"] = True
    with pytest.raises(DriverSafetyProfileError, match="unknown fields"):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=unknown,
            driver_research=None,
        )

    candidate_unknown = _manual_settings()
    candidate_unknown["crossover_candidates"] = [{"typo": True}]
    with pytest.raises(DriverSafetyProfileError, match="unknown fields"):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=candidate_unknown,
            driver_research=None,
        )


def test_research_request_operator_context_stales_after_visible_edit() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    request = build_driver_research_request(topology, _operator_inputs(), manual)
    edited = _manual_settings()
    edited["drivers"][0]["cabinet"]["baffle_width_mm"] = 240.0

    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="operator-declared context is stale",
    ):
        build_design_draft(
            topology,
            driver_research_request=request,
            manual_settings=edited,
            operator_inputs=_operator_inputs(),
        )
