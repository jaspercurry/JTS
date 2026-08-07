# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

import pytest

from jasper.active_speaker.design_draft import (
    ActiveSpeakerDesignDraftError,
    build_design_draft,
    load_design_draft,
    normalise_manual_settings,
    save_design_draft,
)
from jasper.active_speaker import driver_safety as driver_safety_module
from jasper.active_speaker.driver_safety import (
    DRIVER_RESEARCH_KIND,
    DRIVER_RESEARCH_REQUEST_KIND,
    DRIVER_SAFETY_PROFILE_KIND,
    SUPPORTED_PROTECTION_KINDS,
    DriverSafetyProfileError,
    _V2_RESEARCH_DRIVER_FIELDS,
    build_driver_research_prompt,
    build_driver_research_request,
    build_driver_safety_profile,
    driver_research_targets,
    evaluate_driver_safety_profile,
    validate_driver_research_request,
    validate_driver_research_result_shape,
)
from jasper.active_speaker.excitation_safety_plan import (
    resolve_driver_excitation_ceilings,
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


def _prompt_targets_block(prompt: str) -> str:
    """Return the compact request projection the prompt embeds."""

    head, _, rest = prompt.partition("\nTARGETS\n")
    assert head, "prompt has no TARGETS section"
    block, _, _ = rest.partition("\n\nACCURACY\n")
    assert block, "prompt TARGETS section is not followed by ACCURACY"
    return block


def _prompt_result_shape(prompt: str) -> str:
    """Return the fenced result-shape template the assistant must fill in."""

    _, _, rest = prompt.partition("\nRESULT SHAPE\n```json\n")
    assert rest, "prompt has no fenced RESULT SHAPE block"
    block, _, _ = rest.partition("\n```\n")
    assert block, "prompt RESULT SHAPE fence is unterminated"
    return block


def test_research_request_and_prompt_bind_exact_physical_targets() -> None:
    topology = mono_output_topology(card_id=None)
    manual_settings = _manual_settings()
    manual_settings["drivers"][0]["notes"] = (
        "Legacy per-driver note that is no longer editable"
    )

    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        manual_settings,
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
    assert "operator_notes" not in request["targets"][0]["operator_declared_context"]
    assert request["build_notes"] == "Sealed bench cabinet"
    assert "Legacy per-driver note that is no longer editable" not in prompt
    assert "Sealed bench cabinet" in prompt
    assert "hard excitation band distinct" in prompt
    assert "Never infer physical installation choices" in prompt
    assert "Treat operator_declared_context as authoritative" in prompt
    assert "preserving any operator-declared enclosure choice" in prompt

    # Binding is what the server re-checks on paste-back, so every identity the
    # result must echo has to be legible in the prompt itself.
    targets_block = _prompt_targets_block(prompt)
    assert request["request_fingerprint"] in targets_block
    for target in request["targets"]:
        assert target["target_fingerprint"] in targets_block
        assert target["target_id"] in targets_block
        assert target["manufacturer_and_model"] in targets_block
    assert "Copy target_id, target_fingerprint, and model verbatim." in prompt


def test_prompt_demands_one_fenced_json_object_and_exact_driver_count() -> None:
    """The paste box parses JSON, so the ask is one fenced object and nothing
    else — and one drivers[] entry per physical target, counted for THIS
    topology rather than left to the model to infer from an example."""

    two_way = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_request(two_way, _operator_inputs(), _manual_settings())
    )

    assert "exactly one ```json fenced code block" in prompt
    assert "No text before the fence, no text after it." in prompt
    assert "Begin the ```json block now." in prompt
    assert "nothing after the closing fence" in prompt
    assert "Do not ask clarifying questions" in prompt
    # The pasted-back position-340 failures were junk after a value (a unit
    # suffix or a trailing comment), which the old prompt never forbade.
    assert "All numbers are bare JSON numbers. No units, no comments, no text after a value." in prompt
    assert "Return exactly 2 entries in drivers[]" in prompt

    one_target = mono_output_topology(mode="full_range_passive", card_id=None)
    single = build_driver_research_prompt(
        build_driver_research_request(
            one_target,
            {"full_range": "Example FR8", "target_models": {"mono:full_range": "Example FR8"}},
            None,
        )
    )
    assert "Return exactly 1 entry in drivers[]" in single


def test_prompt_projects_targets_without_the_server_hardware_block() -> None:
    """The prompt embeds a projection, not the whole request: the hardware
    inventory and the physical output/topology labels are server bookkeeping
    the assistant cannot use, and they were most of the prompt's length. The
    request object itself is unchanged and still carries them."""

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(
        topology, _operator_inputs(), _manual_settings()
    )
    prompt = build_driver_research_prompt(request)
    targets_block = _prompt_targets_block(prompt)

    assert request["hardware"]["outputs"]
    assert request["targets"][0]["physical_output_label"]
    # Absent from the whole prompt, not merely from the projection.
    assert '"hardware"' not in prompt
    assert "clock_domain" not in prompt
    assert "physical_output_label" not in prompt
    assert "physical_output_index" not in prompt
    assert "topology_id" not in prompt
    assert request["hardware"]["device_label"] not in prompt
    assert '"state": "unused"' not in prompt
    # ...while every key the assistant is asked to reason about survives.
    assert "artifact_schema_version" not in targets_block
    for key in (
        "target_id",
        "target_fingerprint",
        "role",
        "manufacturer_and_model",
        "driver_style",
        "speaker_group_id",
        "speaker_group_mode",
        "operator_declared_context",
    ):
        assert f'"{key}"' in targets_block


def test_prompt_asks_only_for_fields_with_a_consumer() -> None:
    """The ask is a strict subset of what the parser accepts. Four fields were
    dropped: three have no computational consumer — they prefill an Advanced
    field the operator can type (recommended_lowpass_hz, horn_coverage_deg) or
    are display-only with a fallback (manufacturer) — and one asserts level
    authority that belongs to measurement and the operator (gain_offset_db).
    Acceptance is unchanged;
    ``test_dropped_ask_fields_are_still_accepted_and_normalised`` pins that."""

    topology = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_request(topology, _operator_inputs(), _manual_settings())
    )
    result_shape = _prompt_result_shape(prompt)

    for dropped in (
        "recommended_lowpass_hz",
        "horn_coverage_deg",
        "gain_offset_db",
    ):
        assert dropped not in prompt
    # "manufacturer_and_model" is the request-side key and stays; the standalone
    # result-side "manufacturer" field is what was dropped.
    assert '"manufacturer"' not in prompt
    assert '"manufacturer_and_model"' in prompt

    for kept in (
        "nominal_impedance_ohm",
        "sensitivity_db_2v83_1m",
        "usable_frequency_range_hz",
        "recommended_highpass_hz",
        "do_not_test_below_hz",
        "hard_excitation_band_hz",
        "required_protection_filters",
        "measurement_band_hz",
        "crossover_search_band_hz",
        "level_duration_limits",
        "cabinet",
        "driver_class",
        "radiating_diameter_mm",
        "unknowns",
        "field_provenance",
        "notes",
        "sources",
    ):
        assert f'"{kept}"' in result_shape
    for sub_key in (
        "max_effective_peak_dbfs",
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    ):
        assert sub_key in result_shape
    for candidate_key in (
        "between_roles",
        "frequency_hz",
        "filter_type",
        "slope_db_per_octave",
        "confidence",
        "rationale",
        "warnings",
    ):
        assert f'"{candidate_key}"' in result_shape


def test_prompt_scopes_provenance_to_the_five_limit_setting_keys() -> None:
    """Per-field provenance across every field is what turned a data reply into
    an essay. Only the keys that bound what the speaker may excite carry it."""

    from jasper.active_speaker.driver_safety import _PROMPT_PROVENANCE_KEYS

    topology = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_request(topology, _operator_inputs(), _manual_settings())
    )

    assert _PROMPT_PROVENANCE_KEYS == (
        "hard_excitation_band_hz",
        "do_not_test_below_hz",
        "required_protection_filters",
        "level_duration_limits",
        "sensitivity_db_2v83_1m",
    )
    scope_line = next(
        line for line in prompt.splitlines() if line.startswith("- field_provenance")
    )
    assert "only these five keys" in scope_line
    for key in _PROMPT_PROVENANCE_KEYS:
        assert key in scope_line
    assert "at most 2 source URLs" in scope_line
    # The old ask demanded confidence + basis + URLs for every field assertion.
    assert "Every field assertion needs confidence" not in prompt
    assert "at most 3 URLs you actually consulted" in prompt
    assert "notes: one sentence, 15 words or fewer." in prompt


def test_passive_full_range_component_has_research_only_physical_target() -> None:
    topology = mono_output_topology(
        mode="full_range_passive",
        with_subwoofer=True,
        card_id=None,
    )
    operator_inputs = {
        "full_range": "Example FR8",
        "target_models": {"mono:full_range": "Example FR8"},
    }
    manual_settings = {
        "drivers": [
            {
                "target_id": "mono:full_range",
                "role": "full_range",
                "model": "Example FR8",
                "cabinet": {"enclosure_kind": "sealed"},
            }
        ],
        "crossover_candidates": [],
    }

    # Measurement remains active-only; research gets its own passive component
    # target rather than broadening the commissioning contract.
    assert active_driver_targets(topology) == []
    targets = driver_research_targets(topology)
    assert [target["target_id"] for target in targets] == ["mono:full_range"]
    assert all(target["role"] != "subwoofer" for target in targets)
    assert targets[0]["speaker_group_mode"] == "full_range_passive"
    assert len(targets[0]["target_fingerprint"]) == 64

    request = build_driver_research_request(
        topology,
        operator_inputs,
        manual_settings,
    )
    prompt = build_driver_research_prompt(request)
    assert [target["target_id"] for target in request["targets"]] == [
        "mono:full_range"
    ]
    assert request["targets"][0]["manufacturer_and_model"] == "Example FR8"
    assert "Example FR8" in prompt

    draft = build_design_draft(
        topology,
        operator_inputs=operator_inputs,
        manual_settings=manual_settings,
    )
    assert draft["summary"]["manual_driver_count"] == 1
    assert draft["summary"]["missing_driver_info_target_ids"] == []


def test_prompt_asks_for_driver_class_and_geometry_but_never_pad() -> None:
    """#1665: driver_class/radiating_diameter_mm are AI-researchable and must
    appear in the result-shape JSON; pad is operator-only and must never be
    prompted for.  ``horn_coverage_deg`` was researchable too but is no longer
    asked for — it has no computational consumer today — while staying
    accepted; see ``test_prompt_asks_only_for_fields_with_a_consumer``."""
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs(), _manual_settings())
    prompt = build_driver_research_prompt(request)
    result_shape = _prompt_result_shape(prompt)

    assert '"driver_class"' in result_shape
    assert '"radiating_diameter_mm"' in result_shape
    assert "compression_horn" in result_shape
    # Never prompted: pad is an operator-only fact (they wired the resistors),
    # never something research can discover.
    assert '"pad"' not in prompt
    assert "in-line" not in prompt.lower()
    assert "l-pad" not in prompt.lower()


def test_dropped_ask_fields_are_still_accepted_and_normalised() -> None:
    """The ask shrank; acceptance did not.  A reply that still carries the four
    fields the prompt stopped asking for — an older chat, a more thorough
    model, a hand-edited paste — must validate and normalise exactly as before,
    or slimming the prompt would have silently become a schema change."""

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
    research = _research_result(request)
    verbose = {
        "manufacturer": "Example Acoustics",
        "recommended_lowpass_hz": 3000,
        "horn_coverage_deg": 90,
        "gain_offset_db": -6,
        "gain_offset_db_provenance": "research_estimate",
    }
    for driver in research["drivers"]:
        driver.update(verbose)
    # The browser copies researched editable values into the visible record
    # before saving, and _validate_v2_research_prefill checks the two agree.
    # `manufacturer` is deliberately NOT mirrored: it is not a comparable
    # field, so it round-trips research-only.
    manual_settings = _manual_settings()
    for driver in manual_settings["drivers"]:
        driver.update({k: v for k, v in verbose.items() if k != "manufacturer"})

    # Every accept-side gate, not just the one the paste-back path happens to
    # hit first.
    validate_driver_research_result_shape(research)
    draft = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=research,
        manual_settings=manual_settings,
        operator_inputs=_operator_inputs(),
    )

    for driver in draft["driver_research"]["drivers"]:
        assert driver["manufacturer"] == "Example Acoustics"
        assert driver["recommended_lowpass_hz"] == 3000.0
        assert driver["horn_coverage_deg"] == 90.0
        assert driver["gain_offset_db"] == -6.0
        assert driver["gain_offset_db_provenance"] == "research_estimate"
    for field in verbose:
        assert field in _V2_RESEARCH_DRIVER_FIELDS


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
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
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
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
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


def test_declared_compression_driver_style_clears_jts3_shaped_plan() -> None:
    """JTS3 hardware punch #14: a B&C DE250-8 compression tweeter with a real
    ~1.8-2.5 kHz crossover plan. Before driver_style is declared the tweeter
    reads as unknown-style, the conservative 5000 Hz floor rejects the plan
    outright, and no coherent crossover exists against a woofer/tweeter pair
    that must cross in that range -- a permanent deadlock on real hardware.
    Declaring compression_driver lowers the floor to 2000 Hz, which the plan
    clears.
    """
    jts3_manual = _manual_settings()
    tweeter = jts3_manual["drivers"][1]
    tweeter["hard_excitation_band_hz"] = [1500.0, 22000.0]
    tweeter["measurement_band_hz"] = [1700.0, 20000.0]
    tweeter["crossover_search_band_hz"] = [1800.0, 2500.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 2000.0

    undeclared = mono_output_topology(card_id=None)
    with pytest.raises(
        DriverSafetyProfileError,
        match="highpass_below_code_policy",
    ):
        build_driver_safety_profile(
            undeclared,
            manual_settings=jts3_manual,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-07-16T12:00:00Z",
        )

    declared = _topology_with_tweeter_style("compression_driver")
    profile = build_driver_safety_profile(
        declared,
        manual_settings=jts3_manual,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-16T12:00:00Z",
    )
    assert profile["status"] == "confirmed"
    tweeter_target = next(t for t in profile["targets"] if t["role"] == "tweeter")
    assert tweeter_target["driver_style"] == "compression_driver"
    assert tweeter_target["code_owned_policy"]["min_highpass_hz"] == 2000.0
    evaluation = evaluate_driver_safety_profile(profile, declared)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True


# --- #2191: the search band's lower edge, and who owns it ---------------------
#
# The shipped excitation floor (#1654, resolve_driver_excitation_ceilings) drops
# measurement_band[0] for a high-frequency role on the program_admission path --
# the path every crossover search runs on. The store-time validator has to agree
# with it, or the wizard refuses to store a band MEASURE will happily sweep.


def _jts3_shaped_manual(
    *,
    tweeter_search: list[float],
    tweeter_measurement: list[float] | None = None,
    tweeter_hard: list[float] | None = None,
) -> dict:
    """The PRE-EDIT jts3 declaration shape #2191 was found on, parameterised.

    hard=[1600, 20000] / measurement=[2000, 18000] is the exact asymmetry
    ``resolve_driver_excitation_ceilings`` names in its "Low-side asymmetry"
    paragraph, and 2000 Hz is also that speaker's configured Fc.

    A fixture, not a reading: that box's declaration has since been edited and
    re-confirmed (its analysis window was widened alongside the search band,
    the interim two-field unblock #2191 describes). This shape is kept because
    it is the one the defect was measured on and the one a household that edits
    only the search band still lands in.
    """

    manual = _manual_settings()
    tweeter = manual["drivers"][1]
    tweeter["hard_excitation_band_hz"] = tweeter_hard or [1600.0, 20000.0]
    tweeter["measurement_band_hz"] = tweeter_measurement or [2000.0, 18000.0]
    tweeter["crossover_search_band_hz"] = tweeter_search
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 2000.0
    return manual


def _issue_codes(profile: dict) -> set[str]:
    return {issue["code"] for issue in profile["issues"]}


def test_the_owner_ruled_search_repair_is_storable_and_offers_confirmation() -> None:
    """#2191's headline: widening the tweeter's search band down to its declared
    hard floor -- ONE field, [2000, 2500] -> [1600, 2500], measurement window
    untouched at [2000, 18000] -- must land a profile the operator can confirm.

    Before this repair it landed ``incomplete``, which the /sound page renders
    WITHOUT a Confirm button, so the ruled one-field edit had no way to be
    applied. ``needs_confirmation`` / ``unconfirmed`` is the state whose Confirm
    control the browser does render -- pinned on the client side by
    ``testUnconfirmedSafetyProfileHoistsTheConfirmControl`` in
    tests/js/sound_profile_harness.mjs.

    The box the defect was found on took #2191's documented interim unblock
    instead (widening the analysis window too), so this is the form that stays
    blocked for any OTHER household until this validator change ships -- which
    is why the test asserts the one-field edit rather than that box's history.
    """

    topology = _topology_with_tweeter_style("compression_driver")
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_jts3_shaped_manual(tweeter_search=[1600.0, 2500.0]),
        driver_research=None,
    )

    assert profile["issues"] == []
    assert profile["status"] == "needs_confirmation"
    evaluation = evaluate_driver_safety_profile(profile, topology)
    assert evaluation.status == "unconfirmed"

    # …and it really is confirmable, not merely storable.
    confirmed = build_driver_safety_profile(
        topology,
        manual_settings=_jts3_shaped_manual(tweeter_search=[1600.0, 2500.0]),
        driver_research=None,
        confirm=True,
        confirmed_at="2026-08-06T12:00:00Z",
    )
    assert confirmed["status"] == "confirmed"


def test_the_stored_search_floor_is_exactly_the_shipped_excitation_floor() -> None:
    """The one rule, two owners. ``driver_safety`` restates #1654's floor because
    ``excitation_safety_plan`` imports it and cannot be imported back, so this
    pins the restatement to the original by construction: the lowest search edge
    the validator ACCEPTS is exactly the floor MEASURE derives, and the largest
    float below it is refused. Either side drifting by one ULP fails this.

    Run twice, because the shipped floor is a ``max()`` of two terms and one
    fixture can only exercise whichever term wins in it. With hard=[1600, ...]
    the declared hard floor owns the edge; with hard=[10, ...] --
    physically absurd for a tweeter, which is the point -- the global
    ``MIN_DRIVER_TEST_FREQUENCY_HZ`` term owns it instead, and dropping that
    term from the restatement stops being invisible.
    """

    topology = _topology_with_tweeter_style("compression_driver")

    def shipped_floor_hz(hard: list[float], seed_search: list[float]) -> float:
        """Read MEASURE's derived floor for this declaration, then pin the
        validator's accepted edge to it from both sides."""

        def build(search: list[float], *, confirm: bool = False) -> dict:
            return build_driver_safety_profile(
                topology,
                manual_settings=_jts3_shaped_manual(
                    tweeter_search=search, tweeter_hard=hard
                ),
                driver_research=None,
                confirm=confirm,
                confirmed_at="2026-08-06T12:00:00Z" if confirm else None,
            )

        # seed_search is legal under any candidate floor, so reading the shipped
        # value never depends on the value being read.
        confirmed = build(seed_search, confirm=True)
        tweeter = next(t for t in confirmed["targets"] if t["role"] == "tweeter")
        permitted, _ = resolve_driver_excitation_ceilings(
            confirmed,
            tweeter["target_fingerprint"],
            program_admission=True,
        )
        assert build([permitted.lower_hz, 2500.0])["issues"] == [], hard
        just_below = build([math.nextafter(permitted.lower_hz, 0.0), 2500.0])
        assert _issue_codes(just_below) == {
            "tweeter:search_band_below_hard_band"
        }, hard
        return permitted.lower_hz

    # 1. The declared hard floor owns the edge, below the declared analysis
    #    window -- otherwise this test would pass without the asymmetry
    #    existing at all.
    declared_floor = shipped_floor_hz([1600.0, 20000.0], [1600.0, 2500.0])
    assert declared_floor < 2000.0

    # 2. The global test-frequency minimum owns the edge, ABOVE the declared
    #    hard floor -- the case a bare ``float(hard[0])`` restatement gets wrong.
    global_floor = shipped_floor_hz([10.0, 20000.0], [2000.0, 2500.0])
    assert global_floor > 10.0


def test_a_search_band_below_the_hard_band_is_refused_for_every_role() -> None:
    """The relationship the relaxation must never touch. The hard band is the
    operator-confirmed datasheet minimum; a search band reaching under it is
    refused whether or not the role gets the analysis-window relaxation.
    """

    topology = _topology_with_tweeter_style("compression_driver")
    tweeter_under = build_driver_safety_profile(
        topology,
        manual_settings=_jts3_shaped_manual(tweeter_search=[1599.0, 2500.0]),
        driver_research=None,
    )
    assert tweeter_under["status"] == "incomplete"
    assert "tweeter:search_band_below_hard_band" in _issue_codes(tweeter_under)

    # The woofer's hard band starts at 25 Hz and its analysis window at 35 Hz,
    # so this reaches under BOTH and the unchanged subset rule refuses it.
    woofer_under = _manual_settings()
    woofer_under["drivers"][0]["crossover_search_band_hz"] = [20.0, 3500.0]
    profile = build_driver_safety_profile(
        mono_output_topology(card_id=None),
        manual_settings=woofer_under,
        driver_research=None,
    )
    assert profile["status"] == "incomplete"
    assert "woofer:search_band_outside_measurement_band" in _issue_codes(profile)


def test_a_low_frequency_role_still_may_not_search_below_its_analysis_window(
) -> None:
    """The relaxation is high-frequency-only. For a woofer ``measurement_band[0]``
    is a real excursion hedge -- the shipped floor keeps it, so this validator
    keeps it too, even well inside the declared hard band.
    """

    manual = _manual_settings()
    # hard=[25, 5000], measurement=[35, 4500]: 30 Hz is inside the hard band and
    # below the analysis window, which is exactly what a tweeter may now declare.
    manual["drivers"][0]["crossover_search_band_hz"] = [30.0, 3500.0]
    profile = build_driver_safety_profile(
        mono_output_topology(card_id=None),
        manual_settings=manual,
        driver_research=None,
    )

    assert profile["status"] == "incomplete"
    assert _issue_codes(profile) == {"woofer:search_band_outside_measurement_band"}


def test_the_relaxation_never_reaches_the_upper_edge() -> None:
    """Scope. #1654 widened a FLOOR; nothing about it widens the top, so a
    high-frequency search band above the declared analysis window is refused
    with exactly the code it always was.
    """

    profile = build_driver_safety_profile(
        _topology_with_tweeter_style("compression_driver"),
        manual_settings=_jts3_shaped_manual(tweeter_search=[1600.0, 18001.0]),
        driver_research=None,
    )

    assert _issue_codes(profile) == {"tweeter:search_band_outside_measurement_band"}


def test_a_high_frequency_role_without_a_hard_band_keeps_the_stricter_rule(
) -> None:
    """Fail-closed: the relaxation binds to the hard band, so a declaration with
    no hard band to bind to does not lose its floor -- it falls back to the
    unchanged subset rule (alongside the missing-band issue it already raises).
    """

    manual = _jts3_shaped_manual(tweeter_search=[1600.0, 2500.0])
    manual["drivers"][1].pop("hard_excitation_band_hz")
    profile = build_driver_safety_profile(
        _topology_with_tweeter_style("compression_driver"),
        manual_settings=manual,
        driver_research=None,
    )

    codes = _issue_codes(profile)
    assert "tweeter:hard_excitation_band_missing" in codes
    assert "tweeter:search_band_outside_measurement_band" in codes


_SOUND_MAIN_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "assets"
    / "sound-profile"
    / "js"
    / "main.js"
)
# ``driver_safety`` emits reason codes three structurally different ways, and a
# code escaping through ANY of them reaches ``evaluation.reasons`` and therefore
# /sound's copy. A scan that saw one shape would go quiet exactly when the hole
# it exists to catch reopened -- and the newest shape, a bare ``return [...]``,
# is the one that now owns band relationships. So each shape is matched on its
# own and each is required to have contributed: a pattern that silently stops
# matching fails loudly instead of shrinking the derived set.
_REASON_CODE_SHAPES = {
    # _target_issues' per-target checks, and _search_band_issues' HF branch.
    "reasons.append": re.compile(r'\breasons\.append\(\s*f?"([^"]+)"'),
    # _profile_core's profile-wide issue and evaluate_driver_safety_profile's
    # re-derivation of it, whose lists are named `issues` / `derived_issues`.
    "issues.append": re.compile(r'\b\w*issues\.append\(\s*f?"([^"]+)"'),
    # _search_band_issues returns its own list instead of appending to one.
    "return [...]": re.compile(r'\breturn\s+\[\s*f?"([^"]+)"'),
}


def _reason_code_tail(template: str) -> str:
    """The code half of a `<role>:<code>` template, interpolation stripped."""

    tail = template.rsplit("}", 1)[-1] if "}" in template else template
    return tail.rsplit(":", 1)[-1]


def _emittable_reason_codes() -> set[str]:
    """Every reason code ``driver_safety`` can emit, read from its own source.

    Derived rather than restated. Two families are templated: the
    ``{kind}_cutoff_outside_hard_band`` pair is expanded from the same
    ``SUPPORTED_PROTECTION_KINDS`` the code interpolates, and ``{field}_missing``
    collapses to the bare tail ``_missing`` -- which is enough, because the one
    caller partitions on that suffix and never needs the field names.
    """

    source = Path(driver_safety_module.__file__).read_text()
    codes: set[str] = set()
    for shape, pattern in _REASON_CODE_SHAPES.items():
        found = {_reason_code_tail(raw) for raw in pattern.findall(source)}
        assert found, f"the reason-code scan stopped seeing {shape}"
        for tail in found:
            if tail == "_cutoff_outside_hard_band":
                codes.update(f"{kind}{tail}" for kind in SUPPORTED_PROTECTION_KINDS)
            else:
                codes.add(tail)
    assert len(codes) > 8, "the reason-code scan lost most of the source"
    return codes


def test_the_sound_page_can_phrase_every_reason_that_is_not_a_missing_value():
    """#2191's regression guard, by construction rather than by restatement.

    /sound splits an ``incomplete`` profile into "a value is missing" (every
    code ending ``_missing``) and "something does not line up" (a phrase per
    code). A NEW non-missing code with no phrase -- introduced through ANY of
    the three emission shapes ``_REASON_CODE_SHAPES`` enumerates -- would
    silently fall back to "Some safety limits are still missing", reintroducing
    exactly the copy #2191 was filed about. Exact set equality both ways, so a
    dead phrase is caught too.
    """

    js = _SOUND_MAIN_JS.read_text()
    block = js.split("var SAFETY_RELATIONSHIP_TEXT = {", 1)[1].split("};", 1)[0]
    phrased = set(re.findall(r"^\s{4}([a-z0-9_]+):$", block, re.MULTILINE))

    expected = {
        code for code in _emittable_reason_codes() if not code.endswith("_missing")
    }
    assert phrased == expected


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
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
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
        match="stale for the current visible inputs",
    ):
        build_design_draft(
            topology,
            driver_research_request=request,
            manual_settings=edited,
            operator_inputs=_operator_inputs(),
        )


def test_research_request_stales_when_current_context_adds_a_safety_field() -> None:
    topology = mono_output_topology(card_id=None)
    original = _manual_settings()
    original["drivers"][0].pop("cabinet")
    request = build_driver_research_request(topology, _operator_inputs(), original)

    with pytest.raises(
        DriverSafetyProfileError,
        match="stale for the current visible inputs",
    ):
        validate_driver_research_request(
            request,
            topology,
            _operator_inputs(),
            _manual_settings(),
        )


def test_research_request_stales_when_current_build_notes_change() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
    changed_inputs = {**_operator_inputs(), "notes": "Vented production cabinet"}

    with pytest.raises(
        DriverSafetyProfileError,
        match="stale for the current visible inputs",
    ):
        validate_driver_research_request(
            request,
            topology,
            changed_inputs,
            _manual_settings(),
        )


def test_later_confirmation_records_confirmation_time_not_draft_creation(
    tmp_path: Path,
) -> None:
    topology = mono_output_topology(card_id=None)
    path = tmp_path / "active_speaker_design_draft.json"
    first = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        path=path,
        created_at="2026-07-13T12:00:00Z",
    )
    confirmed = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        confirm_safety_profile=True,
        path=path,
        created_at="2026-07-13T12:05:00Z",
    )

    assert first["created_at"] == "2026-07-13T12:00:00Z"
    assert confirmed["created_at"] == first["created_at"]
    assert confirmed["updated_at"] == "2026-07-13T12:05:00Z"
    assert confirmed["driver_safety_profile"]["confirmation"]["confirmed_at"] == (
        "2026-07-13T12:05:00Z"
    )


def test_component_entry_fields_present_in_all_four_allowlist_gates():
    """Drift guard for the four independent allowlist copies (#1665).

    ``driver_class``/``radiating_diameter_mm``/``horn_coverage_deg``/``pad``
    must be accepted by every gate that re-validates the same driver record:
    the two save-path allowlists (whose drift 500s a save), the AI-research
    paste-back allowlist, and the research staleness-comparison set.  One
    superset assertion per copy so the next field lands in all four or fails
    loudly here.
    """
    from jasper.active_speaker import design_draft as dd
    from jasper.active_speaker import driver_safety as ds

    new_fields = {"driver_class", "radiating_diameter_mm", "horn_coverage_deg", "pad"}
    assert new_fields <= set(dd._MANUAL_DRIVER_FIELDS)
    assert new_fields <= set(ds._MANUAL_DRIVER_FIELDS)
    # pad is deliberately NOT researchable; the research gates carry the
    # three researchable fields only.
    researchable = new_fields - {"pad"}
    assert researchable <= set(ds._V2_RESEARCH_DRIVER_FIELDS)
    assert researchable <= set(dd._V2_RESEARCH_COMPARABLE_FIELDS)


# --- #2186: the estimate-friendly research contract -------------------------
#
# Owner ruling 2026-08-06: "anyone can do this ... give us your best guess and
# start from there. There's some risk, but this is an experimental tinker box,
# not a Bose product."  What moved is where a proposed number may COME FROM;
# what did not move is the bound it must clear.  These tests pin both halves.


def _prompt_json_example(prompt: str) -> dict:
    """Return the RESULT SHAPE template parsed as JSON."""

    return json.loads(_prompt_result_shape(prompt))


def _mono_prompt(tweeter_style: str = "dome_tweeter") -> str:
    topology = _topology_with_tweeter_style(tweeter_style)
    return build_driver_research_prompt(
        build_driver_research_request(topology, _operator_inputs(), _manual_settings())
    )


# A hand-maintained mirror of driver_protection._STYLE_HIGH_PASS_HZ, plus the
# undeclared-style case that falls back to _UNKNOWN_HF_STYLE. Kept honest by
# test_tweeter_style_floor_mirror_covers_every_registered_style below: a mirror
# that silently omits a registered style would quietly narrow every test
# parametrized over it. Each pair's floor is asserted against the real policy
# by those same parametrized tests.
_TWEETER_STYLE_FLOORS = [
    ("compression_driver", 2000.0),
    ("horn_compression_driver", 2000.0),
    ("dome_tweeter", 3000.0),
    ("amt_tweeter", 3000.0),
    ("planar_tweeter", 3500.0),
    ("ribbon_tweeter", 5000.0),
    ("supertweeter", 8000.0),
    ("unknown_high_frequency", 5000.0),
    ("unspecified", 5000.0),
]


def test_tweeter_style_floor_mirror_covers_every_registered_style() -> None:
    """The mirror above must not silently omit a style the policy registers.

    Every test that parametrizes over ``_TWEETER_STYLE_FLOORS`` is only as
    broad as this list, so an omission narrows them all without failing
    anything. Verified to bite: adding a style to ``_STYLE_HIGH_PASS_HZ``
    alone left the whole suite green before this assertion existed.

    One-directional on purpose. The reverse — a mirror entry policy does not
    know — is already caught, because an unregistered style resolves to the
    unknown-style fallback and its parametrized floor assertion then fails.
    Same shape as ``tests/test_driver_style_floor_contract.py``, which pins
    the JS display copy of this same table.
    """

    from jasper.active_speaker.driver_protection import _STYLE_HIGH_PASS_HZ

    missing = sorted(set(_STYLE_HIGH_PASS_HZ) - {style for style, _ in _TWEETER_STYLE_FLOORS})
    assert not missing, (
        "driver_protection._STYLE_HIGH_PASS_HZ registers tweeter styles this "
        f"test file's mirror does not cover: {missing}. Add them to "
        "_TWEETER_STYLE_FLOORS so the parametrized tests actually exercise them."
    )


@pytest.mark.parametrize("style,expected_floor", _TWEETER_STYLE_FLOORS)
def test_prompt_result_shape_template_is_storable_not_gate_refused(
    style: str,
    expected_floor: float,
) -> None:
    """The template taught shapes the gate then refused (#2186 leg 1).

    Two defects, both of which produced blockers from the very example the
    assistant was told to copy: ``"max_effective_peak_dbfs": null`` normalised
    away to nothing (``tweeter:max_effective_peak_dbfs_missing``), and a fixed
    high-pass cutoff that no tweeter style's floor cleared.

    Fixing the cutoff to one constant only moved the defect: 3000 clears a dome
    but is refused for planar, ribbon, supertweeter, and an undeclared tweeter.

    The durable property is not that this test catches a future style — it does
    not, and cannot: the worked example is *derived* from ``_STYLE_HIGH_PASS_HZ``,
    so a newly registered style is correct by construction and adding one leaves
    this green. What this proves is that the derivation itself is right, run
    through the REAL gate for every style the policy registers today;
    ``test_tweeter_style_floor_mirror_covers_every_registered_style`` is what
    keeps "every style" true as the registry grows.
    """

    prompt = _mono_prompt(style)
    driver = _prompt_json_example(prompt)["drivers"][0]

    # Feed the template's own values through the real normalise + gate path,
    # standing in as the tweeter of a live two-way.
    topology = _topology_with_tweeter_style(style)
    raw_drivers = [
        {
            "target_id": "mono:woofer",
            "role": "woofer",
            "model": "Example W6",
            **{
                key: deepcopy(_cx120_safety("woofer")[key])
                for key in _cx120_safety("woofer")
            },
        },
        {
            "target_id": "mono:tweeter",
            "role": "tweeter",
            "model": "Example T1",
            "hard_excitation_band_hz": driver["hard_excitation_band_hz"],
            "required_protection_filters": driver["required_protection_filters"],
            "measurement_band_hz": driver["measurement_band_hz"],
            "crossover_search_band_hz": driver["crossover_search_band_hz"],
            "level_duration_limits": driver["level_duration_limits"],
        },
    ]
    manual = normalise_manual_settings(
        {"drivers": raw_drivers, "crossover_candidates": []}
    )
    assert manual is not None
    profile = build_driver_safety_profile(
        topology, manual_settings=manual, driver_research=None, confirm=False
    )
    assert profile["issues"] == [], (
        f"the worked example is refused for driver_style={style}: "
        f"{[issue['code'] for issue in profile['issues']]}"
    )
    assert profile["status"] == "needs_confirmation"

    # And it confirms — "no blockers" and "actually freezable" are different
    # claims, and the template has to satisfy the second one too.
    confirmed = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-08-06T12:00:00Z",
    )
    assert confirmed["status"] == "confirmed"

    # The example's cutoff tracks this style's floor rather than a constant.
    cutoff = driver["required_protection_filters"][0]["cutoff_hz"]
    assert float(cutoff) >= expected_floor
    # All four limit fields survive normalisation (the original null defect).
    for field in (
        "max_effective_peak_dbfs",
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    ):
        assert driver["level_duration_limits"].get(field) is not None


def test_prompt_orders_published_first_then_conservative_estimate() -> None:
    """The ask ranks its own answers instead of stonewalling on null."""

    prompt = _mono_prompt()

    # The retired absolute. Its removal is the whole ruling; a re-added ban
    # would deadlock every driver whose protection numbers are unpublished.
    assert "Never estimate from a similar model" not in prompt
    assert "null is a correct answer, not a failure" not in prompt

    assert "give a conservative engineering estimate" in prompt
    assert 'Tag it confidence "low"' in prompt
    assert "an estimate should look like one" in prompt
    assert "Conservative means the safer side of the guess" in prompt
    assert "Use null only for a field with no engineering basis at all" in prompt

    # Operator authority over installation choices is NOT part of the ruling
    # and must survive it intact.
    assert "Never infer physical installation choices" in prompt

    # A constraint the researcher was never told about cannot be satisfied.
    assert "Nest the three bands" in prompt

    # The template teaches the contract: one estimated field, tagged low.
    provenance = _prompt_json_example(prompt)["drivers"][0]["field_provenance"]
    confidences = {entry["confidence"] for entry in provenance.values()}
    assert "low" in confidences, "template must show an estimated field"
    assert "high" in confidences, "template must show a published field too"
    assert any(
        entry["basis"].startswith("estimated:")
        for entry in provenance.values()
        if entry["confidence"] == "low"
    )


@pytest.mark.parametrize("style,expected_floor", _TWEETER_STYLE_FLOORS)
def test_prompt_limits_are_read_from_code_policy_not_restated(
    style: str,
    expected_floor: float,
) -> None:
    """The bounds in the ask come from the one owner of that policy.

    Four styles with four different floors: a prose constant cannot satisfy
    all of them, so this fails the moment someone hand-writes a number here
    instead of reading ``driver_protection_profile``.  Naming the bound is
    what lets an ESTIMATE land on the first try; the gate still refuses an
    out-of-bounds value on its own, so telling the researcher can only
    narrow the answer, never widen what is accepted.
    """

    from jasper.active_speaker.driver_protection import driver_protection_profile

    policy = driver_protection_profile("tweeter", driver_style=style)
    assert policy.min_highpass_hz == expected_floor

    prompt = _mono_prompt(style)
    limits_block, _, _ = prompt.partition("\nRESULT SHAPE\n")
    _, _, limits_block = limits_block.partition("\nLIMITS\n")
    assert limits_block, "prompt has no LIMITS section"

    assert (
        f"mono:tweeter: required high-pass cutoff_hz at or above {expected_floor:g}"
        in limits_block
    )
    assert (
        f"max_effective_peak_dbfs at or below {policy.max_auto_level_dbfs:g}"
        in limits_block
    )
    # A floor read as a target would talk a researcher DOWN from a stricter
    # published requirement.
    assert "not recommended values" in limits_block
    assert "the published one wins" in limits_block

    # The woofer has no high-pass floor, and inventing one here would be a
    # second, drifting copy of the policy.
    assert "mono:woofer: max_effective_peak_dbfs at or below 0." in limits_block


def test_protection_filter_without_numbers_is_refused_by_name() -> None:
    """"Required, numbers unpublished" is unstorable — and says so (#2186 leg 2).

    The old message named the two missing keys but not the fix, and the
    browser dropped the whole packet before the operator ever saw it.  Under
    the estimate contract the honest answer to an unpublished protective
    cutoff is a conservative estimate, so the refusal says that out loud.
    """

    from jasper.active_speaker.driver_safety import _normalise_protection_filters

    honest_null = [{
        "kind": "highpass",
        "cutoff_hz": None,
        "minimum_slope_db_per_octave": None,
        "family_or_equivalent": "equivalent_or_steeper",
    }]
    with pytest.raises(DriverSafetyProfileError) as excinfo:
        _normalise_protection_filters(honest_null, "driver.required_protection_filters")

    message = str(excinfo.value)
    assert "cutoff_hz and minimum_slope_db_per_octave" in message
    assert "conservative estimate, not null" in message
    # Naming the entry is what lets the operator find it among several drivers.
    assert "driver.required_protection_filters[0]" in message

    # Half a filter is refused for the same reason, not quietly half-stored.
    with pytest.raises(DriverSafetyProfileError):
        _normalise_protection_filters(
            [{"kind": "highpass", "cutoff_hz": 3000}],
            "driver.required_protection_filters",
        )


# --- The field case: Dayton CX120-8 on jts5, 2026-08-06 ---------------------
#
# A real coax whose datasheet publishes usable ranges (90-8500 / 4500-20000)
# and sensitivities (88.5 / 89.2) and nothing else the safety profile needs.
# Under the old ask this driver could not be commissioned without nine
# hand-typed numbers; under the estimate contract it commissions from the
# published facts plus conservative estimates the operator reviews.


def _cx120_safety(role: str, *, tweeter_peak_dbfs: float = -65) -> dict:
    """The safety block an estimating researcher returns for one CX120 section.

    ``tweeter_peak_dbfs`` is a knob only because the declared tweeter peak is a
    SENTINEL downstream — see
    ``test_cx120_declared_ceiling_delegates_but_one_db_quieter_is_literal``.
    """

    if role == "woofer":
        return {
            # Published usable range.
            "hard_excitation_band_hz": [90, 8500],
            # Estimated: no published protective low-pass for the woofer section.
            "required_protection_filters": [{
                "kind": "lowpass",
                "cutoff_hz": 3000,
                "minimum_slope_db_per_octave": 24,
            }],
            "measurement_band_hz": [100, 8000],
            "crossover_search_band_hz": [1500, 3500],
            # Protocol discipline, not a datasheet fact.
            "level_duration_limits": {
                "max_effective_peak_dbfs": -20,
                "max_sweep_duration_s": 4,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 2,
            },
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": 120,
                "baffle_width_mm": 200,
            },
        }
    return {
        "hard_excitation_band_hz": [4500, 20000],
        # Estimated from a 25 mm dome with no published Fs. Clears the
        # dome_tweeter code-policy floor of 3000 Hz with room to spare.
        "required_protection_filters": [{
            "kind": "highpass",
            "cutoff_hz": 4500,
            "minimum_slope_db_per_octave": 24,
        }],
        "measurement_band_hz": [4500, 18000],
        "crossover_search_band_hz": [4500, 6000],
        "level_duration_limits": {
            "max_effective_peak_dbfs": tweeter_peak_dbfs,
            "max_sweep_duration_s": 4,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 2,
        },
        "cabinet": {
            "enclosure_kind": "sealed",
            "radiator_count": 1,
            "effective_radiating_diameter_mm": 25,
        },
    }


_CX120_MODELS = {
    "woofer": "Dayton Audio CX120-8 (woofer section)",
    "tweeter": "Dayton Audio CX120-8 (tweeter section)",
}
# Which fields the reply sourced from the datasheet, and which it estimated.
_CX120_ESTIMATED = {"required_protection_filters", "level_duration_limits"}


def _cx120_operator_inputs() -> dict[str, str]:
    return {
        "woofer": _CX120_MODELS["woofer"],
        "tweeter": _CX120_MODELS["tweeter"],
        "notes": "Sealed coax bench cabinet",
    }


_CX120_SENSITIVITY = {"woofer": 88.5, "tweeter": 89.2}


def _cx120_manual_settings(*, tweeter_peak_dbfs: float = -65) -> dict:
    # The browser copies every researched value into the visible form on
    # import, and _validate_v2_research_prefill re-proves that match on save.
    normalised = normalise_manual_settings({
        "drivers": [
            {
                "target_id": f"mono:{role}",
                "role": role,
                "model": _CX120_MODELS[role],
                "sensitivity_db_2v83_1m": _CX120_SENSITIVITY[role],
                **_cx120_safety(role, tweeter_peak_dbfs=tweeter_peak_dbfs),
            }
            for role in ("woofer", "tweeter")
        ],
        "crossover_candidates": [],
    })
    assert normalised is not None
    return normalised


def _cx120_research(request: dict, *, estimating: bool) -> dict:
    """The reply, either estimating (new contract) or honest-null (old one)."""

    drivers = []
    for target in request["targets"]:
        role = target["role"]
        safety = deepcopy(_cx120_safety(role))
        if not estimating:
            # Exactly what the retired "null is a correct answer" rule produced:
            # the requirement is asserted, its numbers are not.
            safety["required_protection_filters"] = [{
                "kind": "lowpass" if role == "woofer" else "highpass",
                "cutoff_hz": None,
                "minimum_slope_db_per_octave": None,
                "family_or_equivalent": "equivalent_or_steeper",
            }]
            safety["level_duration_limits"] = {
                "max_effective_peak_dbfs": None,
                "max_sweep_duration_s": None,
                "max_repeat_count": None,
                "minimum_cooldown_s": None,
            }
        drivers.append({
            "target_id": target["target_id"],
            "target_fingerprint": target["target_fingerprint"],
            "role": role,
            "model": target["manufacturer_and_model"],
            "sensitivity_db_2v83_1m": _CX120_SENSITIVITY[role],
            **safety,
            "unknowns": [],
            "field_provenance": {
                field: (
                    {
                        "confidence": "low",
                        "basis": (
                            "estimated: 25 mm soft dome, Fs unpublished"
                            if field == "required_protection_filters"
                            else "estimated: protocol default, no published limit"
                        ),
                        "sources": [],
                    }
                    if field in _CX120_ESTIMATED
                    else {
                        "confidence": "high",
                        "basis": "datasheet usable range",
                        "sources": ["https://example.test/cx120"],
                    }
                )
                for field in ("hard_excitation_band_hz", *_CX120_ESTIMATED)
            },
            "sources": ["https://example.test/cx120"],
        })
    return {
        "artifact_schema_version": 2,
        "kind": DRIVER_RESEARCH_KIND,
        "request_fingerprint": request["request_fingerprint"],
        "drivers": drivers,
        "crossover_candidates": [],
    }


def _cx120_setup() -> tuple[OutputTopology, dict, dict]:
    topology = _topology_with_tweeter_style("dome_tweeter")
    manual = _cx120_manual_settings()
    request = build_driver_research_request(
        topology, _cx120_operator_inputs(), manual
    )
    return topology, manual, request


def test_cx120_honest_null_reply_is_refused_loudly_never_dropped() -> None:
    """Direction 1: the artifact that deadlocked jts5 fails LOUDLY.

    It stays unstorable — a declared-but-numberless protective filter is not a
    thing this contract can freeze — but the refusal names the entry and the
    fix instead of the packet vanishing with its provenance and sources.
    """

    topology, manual, request = _cx120_setup()

    with pytest.raises(ActiveSpeakerDesignDraftError) as excinfo:
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=_cx120_research(request, estimating=False),
            manual_settings=manual,
            operator_inputs=_cx120_operator_inputs(),
        )

    message = str(excinfo.value)
    assert "required_protection_filters" in message
    assert "conservative estimate, not null" in message


def test_cx120_estimating_reply_prefills_and_confirms_with_no_issues() -> None:
    """Direction 2: the same driver, answered under the estimate contract.

    Published bands and sensitivities where the datasheet has them, conservative
    estimates where it does not — and that is enough to confirm.  This is the
    end of the two-leg deadlock: no honest-null wall, no nine manual fields.
    """

    topology, manual, request = _cx120_setup()
    research = _cx120_research(request, estimating=True)

    draft = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=research,
        manual_settings=manual,
        operator_inputs=_cx120_operator_inputs(),
        confirm_safety_profile=True,
        created_at="2026-08-06T12:00:00Z",
    )

    profile = draft["driver_safety_profile"]
    assert profile["issues"] == []
    assert profile["status"] == "confirmed"
    assert draft["driver_safety_profile_evaluation"]["confirmed_and_current"] is True
    # Advice never becomes permission, whatever its provenance.
    assert profile["authorizes_playback"] is False
    assert profile["research"]["advisory_only"] is True
    assert profile["authority"] == "operator_visible_values"

    tweeter = next(t for t in profile["targets"] if t["role"] == "tweeter")
    # The estimate survived as an estimate: low confidence, derivation stated.
    filter_provenance = tweeter["field_provenance"]["required_protection_filters"]
    assert filter_provenance["confidence"] == "low"
    assert filter_provenance["basis"].startswith("estimated:")
    # A published field is still distinguishable from an estimated one.
    assert tweeter["field_provenance"]["hard_excitation_band_hz"]["confidence"] == "high"
    # The code-owned policy snapshot is frozen alongside, not replaced by advice.
    assert tweeter["code_owned_policy"]["min_highpass_hz"] == 3000.0
    assert tweeter["code_owned_policy"]["max_auto_level_dbfs"] == -65.0


def _cx120_confirmed_profile(*, tweeter_peak_dbfs: float = -65) -> tuple[dict, dict]:
    """Return (confirmed safety profile, pad-folded declared sensitivities)."""

    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
    )

    topology = _topology_with_tweeter_style("dome_tweeter")
    manual = _cx120_manual_settings(tweeter_peak_dbfs=tweeter_peak_dbfs)
    request = build_driver_research_request(topology, _cx120_operator_inputs(), manual)
    draft = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=None,
        manual_settings=manual,
        operator_inputs=_cx120_operator_inputs(),
        confirm_safety_profile=True,
        created_at="2026-08-06T12:00:00Z",
    )
    profile = draft["driver_safety_profile"]
    assert profile["issues"] == []
    return profile, declared_effective_driver_sensitivities(draft)


def test_template_tweeter_peak_lands_on_the_derivation_sentinel() -> None:
    """The template's tweeter peak is a SENTINEL, and the chain is pinned.

    ``max_effective_peak_dbfs`` is not merely a ceiling for a high-frequency
    role. ``resolve_driver_excitation_ceilings`` treats a declared value that
    EQUALS the class default as "no driver-specific level intent was
    expressed" and replaces it with a sensitivity-derived ceiling; a value one
    dB either side is honoured literally. #2186 made the research ask an
    automated writer of that value, and it deliberately lands on the sentinel.

    Three links, so any one drifting breaks loudly here rather than silently
    moving a real measurement level by up to 30 dB:
      1. the RESULT SHAPE template's tweeter peak,
      2. ``driver_protection_profile(...).max_auto_level_dbfs``,
      3. the value the excitation plan actually compares against.
    """

    from jasper.active_speaker.driver_protection import driver_protection_profile

    policy = driver_protection_profile("tweeter", driver_style="dome_tweeter")

    # Link 1 == link 2.
    template_peak = _prompt_json_example(_mono_prompt())["drivers"][0][
        "level_duration_limits"
    ]["max_effective_peak_dbfs"]
    assert float(template_peak) == policy.max_auto_level_dbfs

    # ... and the prose that steers a reply onto it points at the same number.
    limits_block, _, _ = _mono_prompt().partition("\nRESULT SHAPE\n")
    _, _, limits_block = limits_block.partition("\nLIMITS\n")
    assert (
        f"max_effective_peak_dbfs at or below {policy.max_auto_level_dbfs:g}"
        in limits_block
    )

    # Link 2 == link 3, proved behaviourally rather than by reading a constant:
    # the policy value triggers derivation, one dB quieter does not.
    on_sentinel, sensitivities = _cx120_confirmed_profile(
        tweeter_peak_dbfs=policy.max_auto_level_dbfs
    )
    off_sentinel, _ = _cx120_confirmed_profile(
        tweeter_peak_dbfs=policy.max_auto_level_dbfs - 1
    )
    tweeter_fp = next(
        t["target_fingerprint"] for t in on_sentinel["targets"] if t["role"] == "tweeter"
    )
    _band, derived = resolve_driver_excitation_ceilings(
        on_sentinel,
        tweeter_fp,
        program_admission=True,
        declared_sensitivities=sensitivities,
    )
    _band, literal = resolve_driver_excitation_ceilings(
        off_sentinel,
        next(
            t["target_fingerprint"]
            for t in off_sentinel["targets"]
            if t["role"] == "tweeter"
        ),
        program_admission=True,
        declared_sensitivities=sensitivities,
    )
    assert derived != policy.max_auto_level_dbfs, (
        "the declared class default must be superseded on the proven-HP path"
    )
    assert literal == pytest.approx(policy.max_auto_level_dbfs - 1)


def test_cx120_declared_ceiling_delegates_but_one_db_quieter_is_literal() -> None:
    """The field case, through the real resolver, with the numbers named.

    The CX120 reply declares the tweeter at -65 because it has no published
    level limit. On the proven-high-pass path that delegates the choice: the
    derived ceiling is -35 dBFS, thirty decibels louder than the declared
    number. Declaring -66 instead — a deliberate quieter limit — is honoured
    literally. Both are intended; the discontinuity is documented at the
    equality site in excitation_safety_plan and in the research ask itself.
    """

    from jasper.active_speaker.driver_protection import (
        HF_MEASUREMENT_ABS_CEILING_DBFS,
    )

    profile, sensitivities = _cx120_confirmed_profile()
    # Pad-free declaration, so the effective sensitivities are the datasheet
    # ones the reply reported.
    assert sensitivities == pytest.approx({"woofer": 88.5, "tweeter": 89.2})

    tweeter_fp = next(
        t["target_fingerprint"] for t in profile["targets"] if t["role"] == "tweeter"
    )
    _band, ceiling = resolve_driver_excitation_ceilings(
        profile,
        tweeter_fp,
        program_admission=True,
        declared_sensitivities=sensitivities,
    )
    # woofer cap -20, sensitivity delta 0.7 dB -> min(-20.7, -35) = -35: the
    # absolute hearing-safety ceiling binds, not the sensitivity arithmetic.
    assert ceiling == pytest.approx(HF_MEASUREMENT_ABS_CEILING_DBFS)
    assert ceiling == pytest.approx(-35.0)

    # Without the proven-high-pass path the declared number stands. Delegation
    # is what the protective high-pass buys; it is not unconditional.
    _band, naked = resolve_driver_excitation_ceilings(
        profile, tweeter_fp, declared_sensitivities=sensitivities
    )
    assert naked == pytest.approx(-65.0)

    # One dB quieter is a deliberate choice and is never raised.
    quieter, quieter_sens = _cx120_confirmed_profile(tweeter_peak_dbfs=-66)
    _band, quieter_ceiling = resolve_driver_excitation_ceilings(
        quieter,
        next(
            t["target_fingerprint"] for t in quieter["targets"] if t["role"] == "tweeter"
        ),
        program_admission=True,
        declared_sensitivities=quieter_sens,
    )
    assert quieter_ceiling == pytest.approx(-66.0)


def test_prompt_explains_the_tweeter_peak_delegation_without_contradicting_itself(
) -> None:
    """The guidance has to survive being read literally.

    An earlier draft said "use the ceiling listed under LIMITS" and then "a
    ceiling is not a recommendation" two clauses later, which is a
    contradiction and hid the sentinel entirely.
    """

    prompt = _mono_prompt()
    assert "a ceiling is not a recommendation" not in prompt
    assert "send exactly the ceiling listed under LIMITS" in prompt
    assert "hands the level choice to this build's own protection logic" in prompt
    # The condition on delegation is stated, not implied.
    assert "only once a protective high-pass is proven in the signal path" in prompt
    # And the other branch: quieter is literal.
    assert "taken literally and is never raised" in prompt
    assert "Never send a value above the ceiling." in prompt


@pytest.mark.parametrize(
    "field,mutation,expected_code",
    [
        # An estimate below the dome_tweeter floor: refused BY NAME, not
        # quietly raised to 3000. The operator sees the bound and decides.
        (
            "required_protection_filters",
            [{"kind": "highpass", "cutoff_hz": 2500, "minimum_slope_db_per_octave": 24}],
            "tweeter:highpass_below_code_policy",
        ),
        # An estimate louder than the high-frequency ceiling.
        (
            "level_duration_limits",
            {
                "max_effective_peak_dbfs": -60,
                "max_sweep_duration_s": 4,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 2,
            },
            "tweeter:max_effective_peak_above_code_policy",
        ),
        # Nesting: a measurement band reaching below the hard excitation floor.
        (
            "measurement_band_hz",
            [3000, 18000],
            "tweeter:measurement_band_outside_hard_band",
        ),
        # Nesting, inner half: a crossover-search band escaping its declared
        # bounds. Deleting this clamp left every test in the suite green before
        # #2186, so it is pinned here rather than assumed.
        #
        # WHICH bound this mutation escapes changed with #2194 (the #1654/#2191
        # HF-floor asymmetry), and the expected code moved with it. A tweeter's
        # two search-band edges now answer to different authorities: the LOWER
        # edge to ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0])`` -- here
        # 4500 Hz -- and the UPPER edge still to ``measurement_band[1]``. So
        # 4000 Hz is refused for reaching under the declared hard floor, while
        # 6000 Hz sits legally inside the 18000 Hz analysis ceiling. The clamp
        # is intact; only the name it is refused by moved.
        (
            "crossover_search_band_hz",
            [4000, 6000],
            "tweeter:search_band_below_hard_band",
        ),
    ],
)
def test_estimate_provenance_never_buys_past_a_code_policy_clamp(
    field: str,
    mutation: object,
    expected_code: str,
) -> None:
    """The non-negotiable half of the ruling.

    Widening where a number may come from is not widening what it may be. An
    out-of-bounds value carrying impeccable research provenance is refused
    exactly as an out-of-bounds hand-typed one is — and refused by name rather
    than silently clamped, so the operator can raise the bound deliberately or
    fix the value.
    """

    topology = _topology_with_tweeter_style("dome_tweeter")
    raw_drivers = [
        {
            "target_id": f"mono:{role}",
            "role": role,
            "model": _CX120_MODELS[role],
            **_cx120_safety(role),
        }
        for role in ("woofer", "tweeter")
    ]
    tweeter_raw = next(d for d in raw_drivers if d["role"] == "tweeter")
    tweeter_raw[field] = mutation
    manual = normalise_manual_settings(
        {"drivers": raw_drivers, "crossover_candidates": []}
    )
    assert manual is not None

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=False,
    )
    codes = [issue["code"] for issue in profile["issues"]]
    assert expected_code in codes, codes
    assert profile["status"] == "incomplete"

    # Nothing was rewritten behind the operator's back: the refused value is
    # still exactly what was entered.
    tweeter = next(t for t in profile["targets"] if t["role"] == "tweeter")
    if field == "required_protection_filters":
        assert tweeter["required_protection_filters"][0]["cutoff_hz"] == 2500.0

    with pytest.raises(DriverSafetyProfileError, match="cannot be confirmed"):
        build_driver_safety_profile(
            topology,
            manual_settings=manual,
            driver_research=None,
            confirm=True,
            confirmed_at="2026-08-06T12:00:00Z",
        )


def test_both_search_band_edges_are_reported_when_both_are_bad() -> None:
    """The two search-band checks are INDEPENDENT: neither may suppress the other.

    Since #2194 (the #1654 / #2191 HF-floor asymmetry) a high-frequency role's
    two search-band edges answer to DIFFERENT authorities -- the LOWER edge to
    ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0])``, the UPPER edge still to
    ``measurement_band[1]``. ``_search_band_issues`` therefore evaluates them as
    two unconditional ``append``s into one list rather than as a chain, so a
    declaration that breaks both bounds is told about both.

    Pinned because the property was doubted rather than measured. #2199 and
    #2194 landed within a day of each other and ``main`` went red on one stale
    assertion; the circulating diagnosis was that #2194's new floor check "fires
    first and shadows" the older ceiling check. It does not, and it never did --
    but nothing in the suite would have caught it if it had. Rewriting the second
    ``if`` as an ``elif`` is precisely the regression this test exists to fail
    on, and the cost of that regression is paid by the operator: they would fix
    the one edge they were told about, re-save, and be refused a second time by
    the edge that had been hidden.

    ``[4000, 19000]`` breaks both bounds of the CX120 dome tweeter at once --
    4000 Hz reaches under its declared hard floor of 4500 Hz, and 19000 Hz
    escapes its 18000 Hz analysis ceiling. Each edge alone is already covered
    (``test_the_relaxation_never_reaches_the_upper_edge`` for the ceiling, the
    ``crossover_search_band_hz`` case above for the floor); the conjunction is
    what was untested.
    """

    topology = _topology_with_tweeter_style("dome_tweeter")
    raw_drivers = [
        {
            "target_id": f"mono:{role}",
            "role": role,
            "model": _CX120_MODELS[role],
            **_cx120_safety(role),
        }
        for role in ("woofer", "tweeter")
    ]
    tweeter_raw = next(d for d in raw_drivers if d["role"] == "tweeter")
    tweeter_raw["crossover_search_band_hz"] = [4000, 19000]
    manual = normalise_manual_settings(
        {"drivers": raw_drivers, "crossover_candidates": []}
    )
    assert manual is not None

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=False,
    )

    codes = _issue_codes(profile)
    # Ordered so an ``elif`` regression -- which suppresses the floor code,
    # because the ceiling check runs first -- fails on the edge it hid.
    assert "tweeter:search_band_below_hard_band" in codes, codes
    assert "tweeter:search_band_outside_measurement_band" in codes, codes
    assert profile["status"] == "incomplete"
