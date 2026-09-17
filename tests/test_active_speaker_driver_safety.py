# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import re

import pytest

from jasper.active_speaker.design_draft import (
    ActiveSpeakerDesignDraftError,
    build_design_draft,
    design_draft_view,
    normalise_driver_research,
    load_design_draft,
    normalise_manual_settings,
    save_design_draft,
)
from jasper.active_speaker.driver_safety import (
    DRIVER_RESEARCH_KIND,
    DRIVER_SAFETY_PROFILE_KIND,
    DriverSafetyProfileError,
    _normalise_field_provenance,
    build_driver_research_context,
    compute_driver_safety_profile,
    driver_research_targets,
)
from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
from jasper.active_speaker.driver_protection import (
    LOW_LIMIT_PLAUSIBILITY_FACTOR,
    driver_low_limit_plausibility_band_hz,
)
from jasper.active_speaker.excitation_safety_plan import (
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    prepare_driver_excitation_plan,
    resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_emit import load_tuning_declaration, MeasurementGraphRefused
from jasper.active_speaker.test_signal_plan import DEFAULT_DRIVER_SWEEP_DURATION_S, DRIVER_SWEEP_DURATIONS_S
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_excitation_safety_plan import _requested


def _blocked_codes(
    topology: OutputTopology,
    manual: dict,
    *,
    driver_research: dict | None = None,
) -> set[str]:
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=driver_research,
    )
    assert any(i["severity"] == "blocker" for i in profile["issues"])
    return {issue["code"] for issue in profile["issues"]}


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
            "level_duration_limits",
            "cabinet",
        )
        drivers.append(
            {
                "target_id": target["target_id"],
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
                    "protection_status": "absent",
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


@pytest.mark.parametrize("role", [*DRIVER_SWEEP_DURATIONS_S, "full_range"])
def test_prompt_recommends_the_roles_protocol_sweep_ceiling(role) -> None:
    request = build_driver_research_context(
        mono_output_topology(card_id=None), _operator_inputs(),
    )
    request["targets"] = [{**request["targets"][0], "role": role}]
    prompt = build_driver_research_prompt(request)
    ceilings = {name: float(seconds) for name, seconds in re.findall(r"(\w+) (\d+(?:\.\d+)?) s", prompt)}
    expected = DRIVER_SWEEP_DURATIONS_S.get(role, DEFAULT_DRIVER_SWEEP_DURATION_S)
    assert ceilings[role if role in DRIVER_SWEEP_DURATIONS_S else "roles"] == expected
    example = json.loads(_prompt_result_shape(prompt))["drivers"][0]
    assert example["level_duration_limits"] == {
        "max_sweep_duration_s": expected, "max_repeat_count": 3, "minimum_cooldown_s": 2,
    }
    assert example["field_provenance"]["level_duration_limits"]["confidence"] == "low"


def test_prompt_contains_drivers_and_build_notes_without_typed_limits() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][0]["hard_excitation_band_hz"] = [137, 17439]
    draft = build_design_draft(
        topology, operator_inputs=_operator_inputs(), manual_settings=manual,
    )
    context = build_driver_research_context(topology, draft["operator_inputs"])
    prompt = build_driver_research_prompt(context)
    assert "137" not in prompt
    assert "17439" not in prompt
    assert "fingerprint" not in prompt
    assert "operator_declared_context" not in prompt
    assert json.loads(_prompt_targets_block(prompt)) == context
    assert context["build_notes"] == _operator_inputs()["notes"]
    assert [target["target_id"] for target in context["targets"]] == [
        "mono:woofer", "mono:tweeter",
    ]


def test_prompt_demands_one_fenced_json_object_and_exact_driver_count() -> None:
    """The paste box parses JSON, so the ask is one fenced object and nothing
    else — and one drivers[] entry per physical target, counted for THIS
    topology rather than left to the model to infer from an example."""

    two_way = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_context(two_way, _operator_inputs())
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
        build_driver_research_context(
            one_target,
            {"full_range": "Example FR8", "target_models": {"mono:full_range": "Example FR8"}},
        )
    )
    assert "Return exactly 1 entry in drivers[]" in single


def test_prompt_projects_only_driver_identity_and_build_notes() -> None:
    context = build_driver_research_context(
        mono_output_topology(card_id=None), _operator_inputs(),
    )
    projection = json.loads(_prompt_targets_block(build_driver_research_prompt(context)))
    assert set(projection) == {"targets", "build_notes"}
    assert all(set(target) == {
        "target_id", "role", "manufacturer_and_model", "driver_style",
    } for target in projection["targets"])


def test_prompt_states_the_crossover_vocabulary_the_saver_accepts() -> None:
    """The ask and the refusal read the same sets.

    ``design_draft`` refuses a ``crossover_candidates`` entry outside the
    compiler's vocabulary, and the prompt used to give only a shape example —
    so a researcher could return a filter or slope the save would reject with
    nothing in the ask having said so. Derived from the same accessors the
    entry gate and the wizard pickers read, so a widened
    ``SUPPORTED_CROSSOVER_TYPES`` / ``SUPPORTED_LR_ORDERS`` reaches this
    surface too.
    """
    from jasper.active_speaker.declaration_vocabulary import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    prompt = build_driver_research_prompt(
        build_driver_research_context(
            mono_output_topology(card_id=None), _operator_inputs()
        )
    )

    filters = ", ".join(supported_declaration_filter_types())
    slopes = ", ".join(
        f"{slope:g}" for slope in supported_declaration_slopes_db_per_octave()
    )
    assert f"crossover_candidates[].filter_type is one of: {filters}." in prompt
    assert (
        f"crossover_candidates[].slope_db_per_octave is one of: {slopes}." in prompt
    )
    # The neighbouring order-to-slope instruction teaches 18 dB/octave for a
    # DIFFERENT key (a datasheet's protection-slope condition). The new line
    # must say which question it answers, or it reads as a contradiction.
    assert "recommended_highpass_slope_db_per_octave above" in prompt


def test_prompt_asks_only_for_fields_with_a_consumer() -> None:
    """The ask is a strict subset of what the parser accepts. Three fields were
    dropped from it: two have no computational consumer — one prefills an
    Advanced field the operator can type (recommended_lowpass_hz), the other is
    display-only with a fallback (manufacturer) — and one asserts level
    authority that belongs to measurement and the operator (gain_offset_db).
    Acceptance is unchanged for those three;
    ``test_dropped_ask_fields_are_still_accepted_and_normalised`` pins it. The
    tuple below carries two more keys that are absent from the ask for their
    own reasons, each noted inline."""

    topology = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_context(topology, _operator_inputs())
    )
    result_shape = _prompt_result_shape(prompt)

    for dropped in (
        "recommended_lowpass_hz",
        "gain_offset_db",
        # #2872: DELETED, not merely unasked or retired. It was a horn's
        # nominal coverage angle, collected for a Bessel beamwidth matcher
        # that was never built; nothing ever read it. Tolerated on load and
        # dropped, never stored --
        # test_legacy_research_horn_coverage_deg_is_tolerated_and_dropped.
        "horn_coverage_deg",
        # #2603: retired, not merely unasked. It was an optional SECOND
        # declaration of the driver's low limit; the owner
        # (recommended_highpass_hz) is what the ask carries now, and nothing
        # reads this key. Still accepted so older drafts load, which
        # test_dropped_ask_fields_are_still_accepted_and_normalised pins.
        "do_not_test_below_hz",
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
        # #2603: the owner's slope condition, asked separately because that is
        # how manufacturers publish it -- a footnote to the frequency, and not
        # universal.
        "recommended_highpass_slope_db_per_octave",
        "hard_excitation_band_hz",
        "measurement_band_hz",
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
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    ):
        assert sub_key in result_shape
    # `max_effective_peak_dbfs` is deliberately NOT in the result shape. It is
    # the one datasheet fact in that object, and the template's own number was
    # the class default -- so the shape taught the assistant to send a figure
    # this file had injected, which everything downstream then read as a
    # declaration (owner ruling, 2026-08-23). It is still ACCEPTED, and the
    # prose above the shape still asks for it where a maker publishes one.
    assert "max_effective_peak_dbfs" not in result_shape
    assert "max_effective_peak_dbfs is the one key" in prompt
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

    from jasper.active_speaker.driver_safety_prompt import _PROMPT_PROVENANCE_KEYS

    topology = mono_output_topology(card_id=None)
    prompt = build_driver_research_prompt(
        build_driver_research_context(topology, _operator_inputs())
    )

    assert _PROMPT_PROVENANCE_KEYS == (
        "hard_excitation_band_hz",
        # #2603: the owner replaced the retired do_not_test_below_hz here. It
        # is the field whose value most directly bounds what the speaker may
        # excite, so it is exactly the kind of key this scope exists for.
        "recommended_highpass_hz",
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

    request = build_driver_research_context(
        topology,
        operator_inputs,
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
    prompted for.  ``horn_coverage_deg`` was researchable too, until #2872
    deleted it for never having gained a reader; see
    ``test_prompt_asks_only_for_fields_with_a_consumer``."""
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(topology, _operator_inputs())
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
    """The ask shrank; acceptance did not.  A reply that still carries the
    fields the prompt stopped asking for — an older chat, a more thorough
    model, a hand-edited paste — must validate and normalise exactly as before,
    or slimming the prompt would have silently become a schema change."""

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(
        topology,
        _operator_inputs(),
    )
    research = _research_result(request)
    verbose = {
        "manufacturer": "Example Acoustics",
        "recommended_lowpass_hz": 3000,
        "gain_offset_db": -6,
        "gain_offset_db_provenance": "research_estimate",
    }
    for driver in research["drivers"]:
        driver.update(verbose)
    manual_settings = _manual_settings()
    for driver in manual_settings["drivers"]:
        driver.update({k: v for k, v in verbose.items() if k != "manufacturer"})

    draft = build_design_draft(
        topology,
        driver_research=research,
        manual_settings=manual_settings,
        operator_inputs=_operator_inputs(),
    )

    for driver in draft["driver_research"]["drivers"]:
        assert driver["manufacturer"] == "Example Acoustics"
        assert driver["recommended_lowpass_hz"] == 3000.0
        assert driver["gain_offset_db"] == -6.0
        assert driver["gain_offset_db_provenance"] == "research_estimate"


def test_legacy_research_horn_coverage_deg_is_tolerated_and_dropped() -> None:

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(
        topology,
        _operator_inputs(),
    )
    research = _research_result(request)
    for driver in research["drivers"]:
        driver["horn_coverage_deg"] = 90
    manual_settings = _manual_settings()
    for driver in manual_settings["drivers"]:
        driver["horn_coverage_deg"] = 90

    draft = build_design_draft(
        topology,
        driver_research=research,
        manual_settings=manual_settings,
        operator_inputs=_operator_inputs(),
    )

    for driver in draft["driver_research"]["drivers"]:
        assert "horn_coverage_deg" not in driver
    for driver in draft["manual_settings"]["drivers"]:
        assert "horn_coverage_deg" not in driver
    assert design_draft_view(draft)["driver_safety_profile"] is not None

    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual_settings,
        driver_research=None,
    )
    assert profile["issues"] == []
    assert all(
        "horn_coverage_deg" not in target for target in profile["targets"]
    )


def test_pasted_reply_and_edited_visible_value_both_survive_save(tmp_path: Path) -> None:
    topology = mono_output_topology(card_id=None)
    research = _research_result(build_driver_research_context(topology, _operator_inputs()))
    research["drivers"][1]["recommended_highpass_hz"] = 3000
    manual = _manual_settings()
    manual["drivers"][1]["recommended_highpass_hz"] = 3500
    draft = save_design_draft(
        topology, driver_research=research, manual_settings=manual,
        operator_inputs=_operator_inputs(), path=tmp_path / "draft.json",
    )
    assert draft["driver_research"]["drivers"][1]["recommended_highpass_hz"] == 3000
    assert draft["manual_settings"]["drivers"][1]["recommended_highpass_hz"] == 3500
    assert load_design_draft(tmp_path / "draft.json") == draft


def test_computed_profile_uses_visible_values_and_never_authorizes_audio() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(
        topology,
        _operator_inputs(),
    )
    research = _research_result(request)

    draft = build_design_draft(
        topology,
        driver_research=research,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        created_at="2026-07-13T12:00:00Z",
    )

    profile = design_draft_view(draft)["driver_safety_profile"]
    assert profile["kind"] == DRIVER_SAFETY_PROFILE_KIND
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
    assert profile["authority"] == "operator_visible_values"
    assert profile["targets"][1]["hard_excitation_band_hz"] == [5000.0, 22000.0]
    assert profile["targets"][1]["unknowns"] == [
        "thermal compression limit not published"
    ]
    assert draft["permissions"]["may_not_emit_audio"] is True


def test_missing_floor_and_duration_are_computed_issues() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][1].pop("required_protection_filters")

    saved = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )
    assert any(i["severity"] == "blocker" for i in saved["issues"])
    assert any(
        "required_highpass_missing" in issue["code"] for issue in saved["issues"]
    )

    missing_duration = _manual_settings()
    missing_duration["drivers"][0]["level_duration_limits"].pop("max_sweep_duration_s")
    partial = compute_driver_safety_profile(
        topology,
        manual_settings=missing_duration,
        driver_research=None,
    )
    assert any(i["severity"] == "blocker" for i in partial["issues"])
    assert any(
        "max_sweep_duration_s_missing" in issue["code"] for issue in partial["issues"]
    )

@pytest.mark.parametrize("patch,code", [
    ({"artifact_schema_version": True}, "invalid_design_draft"),
    ({"kind": "other"}, "invalid_design_draft"),
    ({"drivers": [False]}, "invalid_design_draft"),
    ({"drivers": {}}, "invalid_design_draft"),
    ({"drivers": [{"role": "woofer", "model": "W", "hard_excitation_band_hz": [True, 20000]}]}, "invalid_design_draft"),
    ({"crossover_candidates": [False]}, "invalid_design_draft"),
    ({"crossover_candidates": {}}, "invalid_design_draft"),
    ({"crossover_candidates": [{"between_roles": ["woofer", "tweeter"]}] * 9}, "invalid_design_draft"),
    ({"crossover_candidates": [{"between_roles": ["woofer", "tweeter"], "source": {"value": True}}]}, "invalid_design_draft"),
    ({"typo": True}, "unknown_driver_fields"),
])
def test_v2_result_rejects_invalid_shapes(patch, code):
    request = build_driver_research_context(mono_output_topology(card_id=None), _operator_inputs())
    with pytest.raises(ActiveSpeakerDesignDraftError) as caught:
        normalise_driver_research(dict(_research_result(request), **patch))
    assert caught.value.code == code


def test_a_typed_protection_value_the_derivation_replaced_is_disclosed() -> None:
    """"Derived" alone hid the case that costs the operator something.

    /sound/ renders an editable high-pass cutoff and slope, and the projection
    overwrites both from the declared low limit. A household that deliberately
    typed a STRICTER number was told only that the field was derived — never
    that their own entry had been superseded, or by what. The unknowns are what
    the confirm gate shows before anything is frozen, so that is where the
    replacement has to be named.
    """

    topology = mono_output_topology(card_id=None)
    manual = deepcopy(_manual_settings())
    tweeter = manual["drivers"][1]
    tweeter["recommended_highpass_hz"] = 5000.0
    # Typed TIGHTER than the declaration on both fields.
    for entry in tweeter["required_protection_filters"]:
        if entry.get("kind") == "highpass":
            entry["cutoff_hz"] = 6500.0
            entry["minimum_slope_db_per_octave"] = 48.0

    profile = compute_driver_safety_profile(
        topology, manual_settings=manual, driver_research=None,
    )
    unknowns = profile["targets"][1]["unknowns"]

    assert any(
        "the typed high-pass cutoff 6500 was replaced by the derived 5000" in note
        for note in unknowns
    ), unknowns
    assert any(
        "the typed high-pass slope 48 was replaced by the derived 24" in note
        for note in unknowns
    ), unknowns


def test_an_untouched_typed_high_pass_discloses_no_replacement() -> None:
    """The control: the disclosure is a signal, not a line on every save.

    A declaration whose typed high-pass already equals its derivation — the
    ordinary case, including every profile whose low limit was INFERRED from
    that same filter — must not claim anything was replaced.
    """

    topology = mono_output_topology(card_id=None)
    profile = compute_driver_safety_profile(
        topology, manual_settings=_manual_settings(), driver_research=None,
    )

    for target in profile["targets"]:
        assert not [n for n in target["unknowns"] if "was replaced by" in n]


def _de250_manual() -> dict:
    """``_manual_settings`` with the tweeter declaring B&C's real DE250 pair.

    "Recommended Crossover 1.6 kHz — 12 dB/oct. or higher slope high-pass
    filter", which is the declaration the 2026-08-23 owner ruling was made on.
    """

    manual = deepcopy(_manual_settings())
    tweeter = manual["drivers"][1]
    tweeter["recommended_highpass_hz"] = 1600.0
    tweeter["recommended_highpass_slope_db_per_octave"] = 12.0
    tweeter["hard_excitation_band_hz"] = [1600.0, 22000.0]
    tweeter["measurement_band_hz"] = [1600.0, 20000.0]
    tweeter["required_protection_filters"] = [
        {"kind": "highpass", "cutoff_hz": 1600.0,
         "minimum_slope_db_per_octave": 24.0},
    ]
    return manual


def test_a_computed_target_carries_the_declared_pair_beside_its_projections(
) -> None:
    """The un-fusing (#2897). Two slopes, both on the record, distinguishable.

    Before this, a confirmed target held only ``max(published, 24)`` on the
    derived protective high-pass, and no reader could recover what the
    manufacturer actually printed — which is how the topology gate came to
    refuse a household's order-2 pin against a 24 no datasheet contains.
    """

    topology = mono_output_topology(card_id=None)
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=_de250_manual(),
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
    tweeter = profile["targets"][1]
    assert tweeter["recommended_highpass_hz"] == 1600.0
    assert tweeter["recommended_highpass_slope_db_per_octave"] == 12.0
    highpass = next(
        item
        for item in tweeter["required_protection_filters"]
        if item["kind"] == "highpass"
    )
    # The derived figure is UNCHANGED: it is still what this build emits and
    # later proves it emitted, so every already-legal graph stays legal.
    assert highpass["cutoff_hz"] == 1600.0
    assert highpass["minimum_slope_db_per_octave"] == 24.0


def test_an_inferred_low_limit_stores_no_declared_pair() -> None:
    """Provenance is not laundered by persistence.

    ``_manual_settings``'s tweeter declares a protective high-pass and no owner
    field, so its limit is INFERRED. ``apply_driver_low_limit`` fills the owner
    pair on that projection too — storing it would turn "we read this off your
    filter" into "the manufacturer published this", on the one field whose
    entire meaning is the second sentence.
    """

    topology = mono_output_topology(card_id=None)
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
    )
    tweeter = profile["targets"][1]
    assert "recommended_highpass_hz" not in tweeter
    assert "recommended_highpass_slope_db_per_octave" not in tweeter
    # …and the projection it WAS inferred from is untouched.
    highpass = next(
        item
        for item in tweeter["required_protection_filters"]
        if item["kind"] == "highpass"
    )
    assert highpass["cutoff_hz"] == 5000.0


#: A REAL pre-#2870 box's saved draft, kept verbatim under
#: ``tests/fixtures/active_speaker_protection_floor_20260814/``. All four of its
#: drivers carry ``crossover_search_band_hz``, because origin/main REQUIRED the
#: field -- ``crossover_search_band_missing`` blocked confirmation -- so this is
#: what every box confirmed before the ruling actually looks like on disk. It is
#: the specimen, not a hand-built approximation of one.
_PRE_2870_REAL_BOX_DRAFT = (
    Path(__file__).parent / "fixtures" / "active_speaker_protection_floor_20260814"
    / "design-draft-2000hz-below-floor.json"
)


def test_a_pre_2870_box_can_still_save_and_accept_its_stored_declaration() -> None:
    """#2870 hazard 1's other half, and the one that would have bricked boxes.

    Deleting the field from ``_MANUAL_DRIVER_FIELDS`` made every gate that
    RE-VALIDATES a stored driver record raise on it. Two of those gates sit on
    paths a household cannot avoid: the crossover-preview SAVE
    (``design_draft.normalise_manual_settings``) and the crossover ACCEPT
    (``compute_driver_safety_profile``'s own manual gate, which
    ``apply_measured_crossover_geometry`` runs with ``durable=True`` --
    mid-measurement, after the round has already been paid for).

    So the field joins :data:`LEGACY_DROPPED_DRIVER_FIELDS`: TOLERATED at every
    re-validating gate and DROPPED by every normaliser, exactly as
    ``horn_coverage_deg`` is (#2872/#2877). One vocabulary, one set -- a second
    tolerance list would be a second answer to "which keys may a stored record
    still carry".

    Tolerated is not stored: the normalisers' explicit output dicts never emit
    it again, so a box that saves once is clean afterwards. That is what makes
    the re-save the whole migration.
    """

    draft = json.loads(_PRE_2870_REAL_BOX_DRAFT.read_text())
    manual = draft["manual_settings"]
    carriers = [
        driver for driver in manual["drivers"]
        if "crossover_search_band_hz" in driver
    ]
    # The premise, asserted rather than assumed: if the specimen ever stops
    # carrying the field this test would silently prove nothing.
    assert carriers, "the specimen no longer carries the retired field"

    # SAVE: the crossover-preview seam.
    saved = normalise_manual_settings(manual)
    assert saved is not None
    assert all(
        "crossover_search_band_hz" not in driver for driver in saved["drivers"]
    ), "tolerated on the way in, but it must never be stored again"

    # ACCEPT: the seam a measured crossover adopts through.
    topology = mono_output_topology(card_id=None)
    accept_manual = deepcopy(_manual_settings())
    for driver in accept_manual["drivers"]:
        driver["crossover_search_band_hz"] = [1200.0, 3500.0]
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=accept_manual,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
    assert all(
        "crossover_search_band_hz" not in target for target in profile["targets"]
    )
    # …and the rebuilt profile is immediately usable, which is the point of
    # tolerating rather than refusing.

def test_cabinet_reconstruction_is_explicit_and_fail_closed() -> None:
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][0]["cabinet"] = {
        "enclosure_kind": "vented",
        "radiator_count": 2,
        "lf_reconstruction_capability": "refused_multi_radiator_contract_missing",
    }

    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )

    woofer = profile["targets"][0]
    assert woofer["cabinet"]["enclosure_kind"] == "vented"
    assert woofer["cabinet"]["lf_reconstruction_capability"] == (
        "refused_multi_radiator_contract_missing"
    )


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
    assert any(i["severity"] == "blocker" for i in design_draft_view(draft)["driver_safety_profile"]["issues"])
    assert draft["safety"]["research_is_advisory"] is True


def test_stereo_targets_require_physical_target_values_and_preserve_asymmetry() -> None:
    topology = _stereo_topology()
    legacy = _manual_settings()
    for driver in legacy["drivers"]:
        driver.pop("target_id", None)
        driver.pop("source", None)

    incomplete = compute_driver_safety_profile(
        topology,
        manual_settings=legacy,
        driver_research=None,
    )
    assert any(i["severity"] == "blocker" for i in incomplete["issues"])
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
    assert _blocked_codes(topology, legacy).issuperset(
        {
            "left:woofer:target_specific_values_missing",
            "left:tweeter:target_specific_values_missing",
            "right:woofer:target_specific_values_missing",
            "right:tweeter:target_specific_values_missing",
        }
    )

    explicit = compute_driver_safety_profile(
        topology,
        manual_settings=_stereo_manual_settings(),
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in explicit["issues"])
    assert {target["target_id"]: target["model"] for target in explicit["targets"]} == {
        "left:woofer": "Left Example W6",
        "left:tweeter": "Left Example T1",
        "right:woofer": "Right Example W6",
        "right:tweeter": "Right Example T1",
    }


def test_stereo_research_request_uses_exact_target_models() -> None:
    request = build_driver_research_context(
        _stereo_topology(),
        _stereo_operator_inputs(),
    )

    assert {
        target["target_id"]: target["manufacturer_and_model"]
        for target in request["targets"]
    } == _stereo_operator_inputs()["target_models"]


@pytest.mark.parametrize(
    ("field", "value"),
    (("target_id", "unknown:tweeter"), ("model", "Wrong T1")),
)
def test_v2_research_refuses_unknown_target_or_model_mismatch(field: str, value: str) -> None:
    topology = mono_output_topology(card_id=None)
    inputs = {**_operator_inputs(), "tweeter": "  EXAMPLE   T1  "}
    research = _research_result(build_driver_research_context(topology, inputs))
    research["drivers"][1][field] = value
    with pytest.raises(ActiveSpeakerDesignDraftError):
        build_design_draft(
            topology, driver_research=research, manual_settings=_manual_settings(),
            operator_inputs=inputs,
        )


def test_v2_research_accepts_model_case_and_spacing() -> None:
    topology = mono_output_topology(card_id=None)
    inputs = {**_operator_inputs(), "tweeter": "  EXAMPLE   T1  "}
    research = _research_result(build_driver_research_context(topology, inputs))
    research["drivers"][1]["model"] = "example\t t1"
    draft = build_design_draft(
        topology, driver_research=research, manual_settings=_manual_settings(),
        operator_inputs=inputs,
    )
    assert draft["driver_research"]["drivers"][1]["model"] == "example t1"


def test_v2_research_refuses_a_missing_target() -> None:
    topology = mono_output_topology(mode="active_3_way", card_id=None)
    inputs = {**_operator_inputs(), "mid": "Example M3"}
    research = _research_result(build_driver_research_context(topology, inputs))
    research["drivers"] = [driver for driver in research["drivers"] if driver["role"] != "mid"]
    with pytest.raises(ActiveSpeakerDesignDraftError):
        build_design_draft(topology, driver_research=research, operator_inputs=inputs)


def test_code_policy_refuses_unsafe_peak_and_highpass() -> None:
    # The 2026-08-23 ruling REVERSED the first half of this test, for the same
    # reason #2603 and #2874 reversed the rest of it one layer down: a class
    # figure may not refuse a declaration. A declared peak LOUDER than the
    # tweeter class default used to land `max_effective_peak_above_code_policy`
    # and hold the profile `incomplete`; it now has no blocker, and
    # `resolve_driver_excitation_ceilings` honours -64.0 verbatim rather than
    # clamping it back to -65.
    topology = mono_output_topology(card_id=None)
    louder_than_class_default = _manual_settings()
    louder_than_class_default["drivers"][1]["level_duration_limits"][
        "max_effective_peak_dbfs"
    ] = -64.0
    saved = compute_driver_safety_profile(
        topology,
        manual_settings=louder_than_class_default,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in saved["issues"])
    assert _issue_codes(saved) == set()

    # And a target that declares NO level limit at all is confirmable too --
    # the ordinary shape now that the ask requests one only where a maker
    # publishes it.
    undeclared = _manual_settings()
    undeclared["drivers"][1]["level_duration_limits"].pop("max_effective_peak_dbfs")
    no_level = compute_driver_safety_profile(
        topology,
        manual_settings=undeclared,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in no_level["issues"])
    assert _issue_codes(no_level) == set()

    # #2603 REVERSED the second half of this test. A declared low limit below
    # the class default used to be refused (`highpass_below_code_policy`);
    # since the 2026-08-17 ruling a sourced manufacturer figure WINS, because
    # the 2 kHz compression-driver default was rejecting B&C's own published
    # 1.6 kHz for the DE250. 1800 Hz is now accepted...
    compression = _topology_with_tweeter_style("compression_driver")
    below_default = _manual_settings()
    tweeter = below_default["drivers"][1]
    tweeter["recommended_highpass_hz"] = 1800.0
    tweeter["hard_excitation_band_hz"] = [1800.0, 22000.0]
    tweeter["measurement_band_hz"] = [1800.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 1800.0
    accepted = compute_driver_safety_profile(
        compression,
        manual_settings=below_default,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in accepted["issues"])
    assert _issue_codes(accepted) == set()

    # ...and #2874 reversed what stood in its place. A plausibility BLOCKER
    # over a saved declaration is the same class-over-declaration inversion one
    # layer down, so 200 Hz on a compression driver now SAVES with a loud
    # warning naming the number, the band it missed and the anchor. The
    # refusing arm moved to the research-reply intake.
    unsafe_highpass = _manual_settings()
    tweeter = unsafe_highpass["drivers"][1]
    tweeter["recommended_highpass_hz"] = 200.0
    tweeter["hard_excitation_band_hz"] = [200.0, 22000.0]
    tweeter["measurement_band_hz"] = [200.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 200.0
    warned = compute_driver_safety_profile(
        compression,
        manual_settings=unsafe_highpass,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in warned["issues"])
    assert [
        issue["code"] for issue in warned["issues"]
        if issue["severity"] == "blocker"
    ] == []
    warning = next(
        issue for issue in warned["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    assert warning["severity"] == "warning"


def test_an_implausible_low_limit_refuses_the_research_reply_and_warns_the_typist(
) -> None:
    """#2874's author split, both arms, on the same number.

    Owner ruling 2026-08-22: declared values are the only refusing authority.
    So the plausibility band -- which is anchored on the class table -- keeps
    its teeth exactly where the author is a machine, and becomes a disclosure
    where the author is the household:

    * a RESEARCH REPLY carrying 700 Hz for a tweeter whose class band is
      [1250, 20000] is refused at intake, so the number never becomes a
      declaration anyone has to un-declare;
    * the SAME 700 Hz typed by hand SAVES, with a warning. The tinker box
      trusts its owner and says so first.
    """

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(topology, _operator_inputs())

    implausible_reply = _research_result(request)
    tweeter_reply = next(
        driver for driver in implausible_reply["drivers"]
        if driver["role"] == "tweeter"
    )
    tweeter_reply["recommended_highpass_hz"] = 700
    with pytest.raises(ActiveSpeakerDesignDraftError) as refused:
        build_design_draft(
            topology,
            driver_research=implausible_reply,
            operator_inputs=_operator_inputs(),
        )
    # The intake code survives the safety-profile -> design-draft wrap.
    assert refused.value.code == "research_low_limit_implausible"

    # A published figure INSIDE the band passes the intake screen untouched,
    # including one below the class default -- that is the #2603 ruling and it
    # is not reopened here.
    from jasper.active_speaker.driver_safety import (
        validate_research_low_limit_plausibility,
    )

    plausible_reply = _research_result(request)
    next(
        driver for driver in plausible_reply["drivers"]
        if driver["role"] == "tweeter"
    )["recommended_highpass_hz"] = 1600
    validate_research_low_limit_plausibility(plausible_reply, request)
    # ...and a reply that declares nothing is not judged at all.
    validate_research_low_limit_plausibility(_research_result(request), request)

    # The other arm: the same 700 Hz, typed.
    typed = _manual_settings()
    tweeter = typed["drivers"][1]
    tweeter["recommended_highpass_hz"] = 700.0
    tweeter["hard_excitation_band_hz"] = [700.0, 22000.0]
    tweeter["measurement_band_hz"] = [700.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 700.0
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=typed,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
    warning = next(
        issue for issue in profile["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    assert warning["severity"] == "warning"

def test_an_unknown_driver_type_is_disclosed_on_the_computed_profile() -> None:
    topology = mono_output_topology(card_id=None)
    assert topology.speaker_groups[0].channels[1].driver_style is None, (
        "this test is about a box whose driver type nobody set"
    )

    manual = _manual_settings()
    tweeter = manual["drivers"][1]
    tweeter["recommended_highpass_hz"] = 200.0
    tweeter["hard_excitation_band_hz"] = [200.0, 22000.0]
    tweeter["measurement_band_hz"] = [200.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 200.0

    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )
    stored = next(t for t in profile["targets"] if t["role"] == "tweeter")
    assert stored["driver_style"] == "unspecified", (
        "the seam this pins: an unset type is STAMPED, never stored empty"
    )

    warning = next(
        issue for issue in profile["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    assert warning["severity"] == "warning"
    assert warning["target_id"] == stored["target_id"]


def test_declared_compression_driver_style_clears_jts3_shaped_plan() -> None:
    """JTS3 hardware punch #14: a B&C DE250-8 compression tweeter with a real
    ~1.8-2.5 kHz crossover plan.

    WHAT CLEARS THE PLAN MOVED with #2603. It used to be the driver_style
    declaration: an unknown-style tweeter got the conservative 5000 Hz class
    floor, that floor VETOED the plan, and declaring compression_driver lowered
    the veto to 2000 Hz. Since the 2026-08-17 ruling the class figure is not a
    veto at all -- what clears the plan is declaring the driver's own published
    minimum recommended crossover, which for the DE250 is B&C's 1.6 kHz. That
    is the collapse: one declared number, and both the protective high-pass
    and the hard band's floor follow it. (A third follower, the crossover
    search band's floor, went with the field in #2870.)

    driver_style still matters, and this test still pins it -- as the
    plausibility anchor and as the ``code_owned_policy`` the profile freezes.
    """
    jts3_manual = _manual_settings()
    tweeter = jts3_manual["drivers"][1]
    tweeter["recommended_highpass_hz"] = 1600.0
    tweeter["hard_excitation_band_hz"] = [1500.0, 22000.0]
    tweeter["measurement_band_hz"] = [1700.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 2000.0

    # Undeclared style is no longer a deadlock: the plausibility band for an
    # unknown-style tweeter is [1250, 20000], and a published 1600 sits inside
    # it, so the plan is believed rather than vetoed.
    undeclared = mono_output_topology(card_id=None)
    undeclared_profile = compute_driver_safety_profile(
        undeclared,
        manual_settings=jts3_manual,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in undeclared_profile["issues"])
    undeclared_tweeter = next(
        t for t in undeclared_profile["targets"] if t["role"] == "tweeter"
    )
    # The stale second declaration (2000) lost; the owner (1600) won, and every
    # derived field followed it.
    assert undeclared_tweeter["hard_excitation_band_hz"] == [1600.0, 22000.0]
    assert (
        undeclared_tweeter["required_protection_filters"][0]["cutoff_hz"] == 1600.0
    )
    # ...while the class default still describes the UNDECLARED style.
    assert undeclared_tweeter["code_owned_policy"]["min_highpass_hz"] == 5000.0

    declared = _topology_with_tweeter_style("compression_driver")
    profile = compute_driver_safety_profile(
        declared,
        manual_settings=jts3_manual,
        driver_research=None,
    )
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
    tweeter_target = next(t for t in profile["targets"] if t["role"] == "tweeter")
    assert tweeter_target["driver_style"] == "compression_driver"
    assert tweeter_target["code_owned_policy"]["min_highpass_hz"] == 2000.0


def _issue_codes(profile: dict) -> set[str]:
    return {issue["code"] for issue in profile["issues"]}


def test_driver_style_changes_computed_policy_not_measurement_identity() -> None:
    compression = _topology_with_tweeter_style("compression_driver")
    ribbon = _topology_with_tweeter_style("ribbon_tweeter")
    compression_targets = active_driver_targets(compression)
    ribbon_targets = active_driver_targets(ribbon)
    assert [target["target_fingerprint"] for target in compression_targets] == [
        target["target_fingerprint"] for target in ribbon_targets
    ]

    profile = compute_driver_safety_profile(
        compression,
        manual_settings=_manual_settings(),
        driver_research=None,
    )
    assert profile["targets"][1]["driver_style"] == "compression_driver"
    recomputed = compute_driver_safety_profile(ribbon, manual_settings=_manual_settings(), driver_research=None)
    assert recomputed["targets"][1]["driver_style"] == "ribbon_tweeter"


def test_sealed_cabinet_without_baffle_width_has_typed_refusal() -> None:
    manual = _manual_settings()
    manual["drivers"][0]["cabinet"].pop("baffle_width_mm")
    manual = normalise_manual_settings(manual)
    assert manual is not None

    profile = compute_driver_safety_profile(
        mono_output_topology(card_id=None),
        manual_settings=manual,
        driver_research=None,
    )

    assert profile["targets"][0]["cabinet"]["lf_reconstruction_capability"] == (
        "refused_geometry_incomplete"
    )


def test_operator_override_drops_research_provenance_for_changed_field() -> None:
    topology = mono_output_topology(card_id=None)
    request = build_driver_research_context(
        topology,
        _operator_inputs(),
    )
    imported = build_design_draft(
        topology,
        driver_research=_research_result(request),
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
    )
    edited = _manual_settings()
    edited["drivers"][1]["cabinet"]["baffle_width_mm"] = 150.0

    profile = compute_driver_safety_profile(
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


def _legacy_duplicate_role(manual: dict) -> None:
    for driver in manual["drivers"]:
        driver.pop("target_id", None)
    manual["drivers"].append(deepcopy(manual["drivers"][0]))


_mono_topology = partial(mono_output_topology, card_id=None)


@pytest.mark.parametrize(
    ("mutate", "topology", "code"),
    [
        pytest.param(
            lambda manual: manual["drivers"][1].update({"role": "woofer"}),
            _mono_topology,
            "manual_target_role_mismatch",
            id="role_contradicts_target_id",
        ),
        pytest.param(
            lambda manual: manual["drivers"][1].update(
                {"target_id": "missing:tweeter"}
            ),
            _mono_topology,
            "manual_target_unknown",
            id="target_id_not_in_topology",
        ),
        pytest.param(
            lambda manual: manual["drivers"].append(
                {**deepcopy(manual["drivers"][1]), "target_id": None}
            ),
            _mono_topology,
            "manual_target_bound_twice",
            id="legacy_role_row_rebinds_a_bound_target",
        ),
        pytest.param(
            _legacy_duplicate_role,
            _stereo_topology,
            "manual_duplicate_legacy_role",
            id="two_legacy_rows_for_one_role",
        ),
    ],
)
def test_manual_target_binding_refuses_contradictions(mutate, topology, code: str) -> None:
    manual = _manual_settings()
    mutate(manual)

    with pytest.raises(DriverSafetyProfileError) as caught:
        compute_driver_safety_profile(
            topology(), manual_settings=manual, driver_research=None,
        )
    assert caught.value.code == code


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
    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )

    assert profile["targets"][0]["hard_excitation_band_hz"] == [25.0, 5000.0]
    assert profile["targets"][0]["required_protection_filters"][0][
        "family_or_equivalent"
    ] == "equivalent_or_steeper"
    assert profile["targets"][0]["cabinet"]["lf_reconstruction_capability"] == (
        "refused_geometry_incomplete"
    )


def test_direct_builder_rejects_boolean_and_unknown_manual_fields() -> None:
    boolean = _manual_settings()
    boolean["drivers"][1]["hard_excitation_band_hz"][0] = True
    with pytest.raises(DriverSafetyProfileError):
        compute_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=boolean,
            driver_research=None,
        )

    candidate_unknown = _manual_settings()
    candidate_unknown["crossover_candidates"] = [{"typo": True}]
    with pytest.raises(DriverSafetyProfileError) as caught:
        compute_driver_safety_profile(
            mono_output_topology(card_id=None), manual_settings=candidate_unknown,
            driver_research=None,
        )
    assert caught.value.code == "unknown_driver_fields"


def test_provenance_has_no_second_writer_for_published_versus_estimated() -> None:
    with pytest.raises(DriverSafetyProfileError) as caught:
        _normalise_field_provenance({
            "level_duration_limits": {
                "confidence": "high", "basis": "datasheet", "state": "estimated",
            },
        }, "driver.field_provenance")
    assert caught.value.code == "unknown_driver_fields"


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
        build_driver_research_context(topology, _operator_inputs())
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
            # #2603: the template no longer STATES a protective high-pass for a
            # tweeter -- it declares the driver's minimum recommended crossover
            # and the requirement derives from it. Copying the owner is what
            # makes this test prove the template is storable.
            "recommended_highpass_hz": driver["recommended_highpass_hz"],
            "recommended_highpass_slope_db_per_octave": driver[
                "recommended_highpass_slope_db_per_octave"
            ],
            "measurement_band_hz": driver["measurement_band_hz"],
            "level_duration_limits": driver["level_duration_limits"],
        },
    ]
    manual = normalise_manual_settings(
        {"drivers": raw_drivers, "crossover_candidates": []}
    )
    assert manual is not None
    profile = compute_driver_safety_profile(
        topology, manual_settings=manual, driver_research=None,
    )
    assert profile["issues"] == [], (
        f"the worked example is refused for driver_style={style}: "
        f"{[issue['code'] for issue in profile['issues']]}"
    )
    # The example's low limit tracks this style's figure rather than a
    # constant, and the protective high-pass it DERIVES lands on the same
    # number -- the template teaches one declaration, not two (#2603).
    assert float(driver["recommended_highpass_hz"]) >= expected_floor
    tweeter_target = next(
        t for t in profile["targets"] if t["role"] == "tweeter"
    )
    highpass = next(
        item
        for item in tweeter_target["required_protection_filters"]
        if item["kind"] == "highpass"
    )
    assert highpass["cutoff_hz"] == float(driver["recommended_highpass_hz"])
    # The three protocol limit fields survive normalisation (the original null
    # defect). The fourth, `max_effective_peak_dbfs`, is absent by design since
    # the 2026-08-23 ruling -- and the computed profile has no blocker, which is
    # the half of that ruling this test is the guard for.
    for field in (
        "max_sweep_duration_s",
        "max_repeat_count",
        "minimum_cooldown_s",
    ):
        assert driver["level_duration_limits"].get(field) is not None
    assert "max_effective_peak_dbfs" not in driver["level_duration_limits"]


def test_prompt_asks_for_a_best_estimate_declared_with_a_source() -> None:
    """The ask ranks its own answers instead of stonewalling on null.

    #2195 changed the *kind* of estimate asked for.  "Conservative" is gone:
    safety lives in the clamps below, and prompt-level lowballing only costs
    performance.  What replaces it is best-number-plus-declaration-plus-source,
    so the operator can arbitrate rather than inherit a timid guess.
    """

    prompt = _mono_prompt()

    # The retired absolute. Its removal is the whole ruling; a re-added ban
    # would deadlock every driver whose protection numbers are unpublished.
    assert "Never estimate from a similar model" not in prompt
    assert "null is a correct answer, not a failure" not in prompt

    # The retired #2186 posture. Both sentences are gone, not reworded:
    # "conservative" must not survive anywhere in the ask.
    assert "conservative" not in prompt.lower()

    assert "best reality-grounded engineering estimate" in prompt
    assert "from the driver's published facts and physics" in prompt
    assert 'Tag it confidence "low"' in prompt
    assert "an estimate should look like one" in prompt
    assert "Declare every estimate as an estimate and name one source" in prompt
    assert "Use null only for a field with no engineering basis at all" in prompt

    # Operator authority over installation choices is NOT part of the ruling
    # and must survive it intact.
    assert "Never infer physical installation choices" in prompt

    # A constraint the researcher was never told about cannot be satisfied.
    assert "Nest the bands" in prompt

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
    # Both eras of entry carry the citation -- the ask says "either way", and a
    # template that only sourced the published field would teach otherwise.
    assert all(entry.get("source") for entry in provenance.values()), provenance


def test_prompt_template_provenance_is_a_subset_of_what_the_parser_accepts() -> None:
    """ask ⊂ accept: every key the template teaches must normalise.

    The #2186 postmortem's leg 1 was exactly this — the RESULT SHAPE taught a
    shape the gate refused — so the new ``source`` key gets the same mechanical
    check rather than an argument that it is fine.
    """

    from jasper.active_speaker.driver_safety import _normalise_field_provenance

    provenance = _prompt_json_example(_mono_prompt())["drivers"][0][
        "field_provenance"
    ]
    normalised = _normalise_field_provenance(provenance, "driver.field_provenance")

    assert set(normalised) == set(provenance)
    for key, entry in normalised.items():
        assert entry["source"] == provenance[key]["source"]
        assert entry["confidence"] == provenance[key]["confidence"]


def test_provenance_source_is_additive_and_old_entries_are_byte_identical() -> None:
    """An absent source stays absent in research provenance."""

    from jasper.active_speaker.driver_safety import (
        MAX_PROVENANCE_SOURCE_CHARS,
        _canonical_json,
        _normalise_field_provenance,
    )

    legacy = {
        "do_not_test_below_hz": {
            "confidence": "high",
            "basis": "datasheet minimum crossover",
            "sources": ["https://example.test/t1"],
        }
    }
    normalised_legacy = _normalise_field_provenance(legacy, "driver.field_provenance")
    assert _canonical_json(normalised_legacy) == _canonical_json(legacy)
    assert "source" not in normalised_legacy["do_not_test_below_hz"]

    sourced = {
        "level_duration_limits": {
            "confidence": "low",
            "basis": "estimated: protocol default",
            "source": "  Dayton   CX120-8 datasheet, p.2 ",
            "sources": [],
        }
    }
    entry = _normalise_field_provenance(sourced, "driver.field_provenance")[
        "level_duration_limits"
    ]
    # Whitespace-collapsed like every other free string on this contract.
    assert entry["source"] == "Dayton CX120-8 datasheet, p.2"

    # Length-capped, and the cap names the field so an operator can find it.
    at_max = {
        "level_duration_limits": {
            "confidence": "low",
            "basis": "estimated",
            "source": "x" * MAX_PROVENANCE_SOURCE_CHARS,
        }
    }
    accepted = _normalise_field_provenance(at_max, "driver.field_provenance")
    assert len(accepted["level_duration_limits"]["source"]) == MAX_PROVENANCE_SOURCE_CHARS

    too_long = {
        "level_duration_limits": {
            "confidence": "low",
            "basis": "estimated",
            "source": "x" * (MAX_PROVENANCE_SOURCE_CHARS + 1),
        }
    }
    with pytest.raises(DriverSafetyProfileError):
        _normalise_field_provenance(too_long, "driver.field_provenance")

    # The citation slot must hold any URL the `sources` list holds. They are
    # separate budgets, but a datasheet URL is a legal citation, so a cap that
    # accepted a URL in the list and refused the SAME URL in `source` would
    # reject a correct reply for a reason no researcher could have anticipated.
    # Pinned behaviourally in both slots rather than by comparing constants,
    # because what matters is that the promotion works, not how it is spelled.
    long_url = "https://example.test/datasheets/" + "d" * 280
    both_slots = {
        "hard_excitation_band_hz": {
            "confidence": "high",
            "basis": "datasheet usable range",
            "source": long_url,
            "sources": [long_url],
        }
    }
    promoted = _normalise_field_provenance(both_slots, "driver.field_provenance")[
        "hard_excitation_band_hz"
    ]
    assert promoted["sources"] == [long_url]
    assert promoted["source"] == long_url, (
        "a URL the sources list accepts must be promotable verbatim into the "
        "single citation slot"
    )

    # And the pre-#2233 cap is genuinely gone: 161 characters used to raise.
    formerly_refused = {
        "level_duration_limits": {
            "confidence": "low",
            "basis": "estimated",
            "source": "y" * 161,
        }
    }
    assert (
        _normalise_field_provenance(formerly_refused, "driver.field_provenance")[
            "level_duration_limits"
        ]["source"]
        == "y" * 161
    )


@pytest.mark.parametrize("style,expected_floor", _TWEETER_STYLE_FLOORS)
def test_protection_policy_view_reads_policy_never_restates_it(
    style: str,
    expected_floor: float,
) -> None:
    """The page's policy echo is derived, per target, from the one owner.

    Nine registered styles with five distinct floors: a hand-written constant
    cannot satisfy all of them, so this fails the moment the view stops calling
    ``driver_protection_profile``.  ``role_class`` travels too, so /sound/ never
    keeps its own copy of which roles are high-frequency.

    With no visible declaration the resolved low limit IS the class figure --
    and says so.  The declared case is
    ``test_the_policy_view_publishes_the_resolved_floor_with_its_provenance``.
    """

    from jasper.active_speaker.driver_protection import driver_protection_profile
    from jasper.active_speaker.driver_safety import driver_protection_policy_view

    view = driver_protection_policy_view(_topology_with_tweeter_style(style))

    by_target = {entry["target_id"]: entry for entry in view["targets"]}
    assert set(by_target) == {"mono:woofer", "mono:tweeter"}

    tweeter = by_target["mono:tweeter"]
    policy = driver_protection_profile("tweeter", driver_style=style)
    assert tweeter["role_class"] == "high_frequency"
    assert tweeter["low_limit_hz"] == expected_floor == policy.min_highpass_hz
    assert tweeter["low_limit_provenance"] == "style_default"
    assert tweeter["low_limit_summary"] == (
        f"{expected_floor:g} Hz (class fallback; nothing declared)"
    )
    assert tweeter["max_auto_level_dbfs"] == policy.max_auto_level_dbfs

    woofer = by_target["mono:woofer"]
    assert woofer["role_class"] == "low_frequency"
    assert woofer["low_limit_hz"] is None
    assert woofer["low_limit_provenance"] is None
    assert woofer["low_limit_summary"] is None

    # The emitted per-target shape, pinned. `role` is deliberately absent --
    # role_class answers every question the page asks, and a field with no
    # reader is a field that drifts unnoticed. The raw `min_highpass_hz` is
    # absent since #2874: it discriminated between HF styles, but so does the
    # resolved trio that replaced it, and printing the class figure unlabelled
    # beside a declared one is the ambiguity that ticket removed.
    assert set(tweeter) == {
        "target_id",
        "role_class",
        "max_auto_level_dbfs",
        "low_limit_hz",
        "low_limit_provenance",
        "low_limit_summary",
    }
    assert "min_highpass_hz" not in tweeter
    # `hf_measurement_abs_ceiling_dbfs` is deliberately absent: the provisional
    # -35 dBFS constant it published was retired 2026-08-20, and the bound that
    # replaced it (the per-driver sensitivity derivation) is not computable from
    # a topology alone. Pinned as an exact key set so re-adding it — or
    # restating the global test ceiling in its place — fails here.
    assert set(view) == {"policy_version", "targets"}


def test_the_policy_view_publishes_the_resolved_floor_with_its_provenance() -> None:
    """#2874's confusion surface, closed on the view the draft carries.

    The draft on jts3 showed ``recommended_highpass_hz: 1600`` beside an
    unlabelled ``min_highpass_hz: 2000`` with nothing saying which bounds the
    corner, and two readers independently took the 2000 for a second floor.
    The view now answers that question in the same document, in words.
    """

    from jasper.active_speaker.driver_safety import driver_protection_policy_view

    topology = _topology_with_tweeter_style("compression_driver")
    manual = _manual_settings()
    manual["drivers"][1]["recommended_highpass_hz"] = 1600.0

    view = driver_protection_policy_view(topology, manual)
    tweeter = next(
        entry for entry in view["targets"] if entry["target_id"] == "mono:tweeter"
    )

    assert tweeter["low_limit_hz"] == 1600.0
    assert tweeter["low_limit_provenance"] == "declared"
    assert tweeter["low_limit_summary"] == "1600 Hz (manufacturer declared)"
    # The class figure is not republished beside it, unlabelled or otherwise.
    assert "min_highpass_hz" not in tweeter
    assert 2000.0 not in tweeter.values()

    # Same topology, nothing declared: the class figure IS the answer, and says
    # so rather than passing itself off as a datasheet number.
    undeclared = driver_protection_policy_view(topology)
    undeclared_tweeter = next(
        entry for entry in undeclared["targets"]
        if entry["target_id"] == "mono:tweeter"
    )
    assert undeclared_tweeter["low_limit_hz"] == 2000.0
    assert undeclared_tweeter["low_limit_summary"] == (
        "2000 Hz (class fallback; nothing declared)"
    )


def test_design_draft_restamps_the_protection_policy_on_every_topology_load(
    tmp_path: Path,
) -> None:
    """The current topology and declaration determine the policy view."""

    from jasper.active_speaker.driver_safety import driver_protection_policy_view

    topology = _topology_with_tweeter_style("dome_tweeter")
    path = tmp_path / "design_draft.json"
    saved = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        path=path,
    )
    assert saved["driver_protection_policy_view"] == driver_protection_policy_view(
        topology, saved["manual_settings"]
    )

    raw = json.loads(path.read_text())
    assert "driver_protection_policy_view" not in raw
    raw["driver_protection_policy_view"] = {"hf_measurement_abs_ceiling_dbfs": -99.0, "targets": []}
    path.write_text(json.dumps(raw))

    loaded = load_design_draft(path, topology=topology)
    assert loaded["driver_protection_policy_view"] == driver_protection_policy_view(
        topology, loaded["manual_settings"]
    )

    # The name is load-bearing: excitation_safety_plan already hashes a
    # DIFFERENT shape under `driver_protection_policy` inside the protection-
    # requirement fingerprint, so the draft key must not collide with it.
    assert "driver_protection_policy" not in saved


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

    # #2603: the style figure is no longer stated as a required MINIMUM -- a
    # published manufacturer number below it now wins. What the ask states is
    # the PLAUSIBILITY band derived from that same figure, so the bound still
    # cannot drift from the policy it is read out of.
    band = driver_low_limit_plausibility_band_hz("tweeter", driver_style=style)
    assert band == (
        expected_floor / LOW_LIMIT_PLAUSIBILITY_FACTOR,
        expected_floor * LOW_LIMIT_PLAUSIBILITY_FACTOR,
    )
    assert (
        f"mono:tweeter: recommended_highpass_hz between {band[0]:g} and "
        f"{band[1]:g} if published, else null"
        in limits_block
    )
    # A floor read as a target would talk a researcher DOWN from a stricter
    # published requirement.
    assert "not recommended values" in limits_block
    assert "the published one wins" in limits_block

    # No level bound is stated, for either driver. The section carried
    # `max_effective_peak_dbfs at or below <class default>` until 2026-08-23 --
    # a code figure a researcher had to clear, whose reply this build then read
    # as a declaration. The owner struck it.
    assert "max_effective_peak_dbfs" not in limits_block
    assert f"at or below {policy.max_auto_level_dbfs:g}" not in limits_block
    # The woofer has no high-pass floor and no level bound, so it contributes
    # no LIMITS line at all rather than an invented one.
    assert "mono:woofer:" not in limits_block


@pytest.mark.parametrize("declared", [
    # "Required, numbers unpublished" is unstorable (#2186 leg 2): under the
    # best-estimate contract (#2195) the honest answer to an unpublished
    # protective cutoff is the researcher's best estimate, never null.
    [{
        "kind": "highpass",
        "cutoff_hz": None,
        "minimum_slope_db_per_octave": None,
        "family_or_equivalent": "equivalent_or_steeper",
    }],
    # Half a filter is refused for the same reason, not quietly half-stored.
    [{"kind": "highpass", "cutoff_hz": 3000}],
])
def test_protection_filter_without_numbers_is_refused(declared) -> None:
    from jasper.active_speaker.driver_safety import _normalise_protection_filters

    with pytest.raises(DriverSafetyProfileError) as excinfo:
        _normalise_protection_filters(declared, "driver.required_protection_filters")
    assert excinfo.value.code == "protection_filter_numbers_missing"


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
        "level_duration_limits": {
            # ``None`` omits the key entirely -- the ordinary reply since the
            # 2026-08-23 ruling, since Dayton publish no level limit.
            **(
                {}
                if tweeter_peak_dbfs is None
                else {"max_effective_peak_dbfs": tweeter_peak_dbfs}
            ),
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
        "drivers": drivers,
        "crossover_candidates": [],
    }


def _cx120_setup() -> tuple[OutputTopology, dict, dict]:
    topology = _topology_with_tweeter_style("dome_tweeter")
    manual = _cx120_manual_settings()
    request = build_driver_research_context(
        topology, _cx120_operator_inputs()
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
            driver_research=_cx120_research(request, estimating=False),
            manual_settings=manual,
            operator_inputs=_cx120_operator_inputs(),
        )

    assert excinfo.value.code == "protection_filter_numbers_missing"


def test_cx120_estimating_reply_prefills_and_confirms_with_no_issues() -> None:
    topology, manual, request = _cx120_setup()
    research = _cx120_research(request, estimating=True)

    draft = build_design_draft(
        topology,
        driver_research=research,
        manual_settings=manual,
        operator_inputs=_cx120_operator_inputs(),
        created_at="2026-08-06T12:00:00Z",
    )

    profile = design_draft_view(draft)["driver_safety_profile"]
    assert profile["issues"] == []
    assert not any(i["severity"] == "blocker" for i in profile["issues"])
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


def _cx120_profile(*, tweeter_peak_dbfs: float = -65) -> tuple[dict, dict]:
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
    )

    topology = _topology_with_tweeter_style("dome_tweeter")
    manual = _cx120_manual_settings(tweeter_peak_dbfs=tweeter_peak_dbfs)
    draft = build_design_draft(
        topology,
        driver_research=None,
        manual_settings=manual,
        operator_inputs=_cx120_operator_inputs(),
        created_at="2026-08-06T12:00:00Z",
    )
    profile = design_draft_view(draft)["driver_safety_profile"]
    assert profile["issues"] == []
    return profile, declared_effective_driver_sensitivities(draft)


def test_the_ask_no_longer_writes_a_level_ceiling_it_will_read_back() -> None:
    """The retired round trip, pinned as its own absence.

    Until 2026-08-23 the ask emitted a class-default tweeter peak in RESULT
    SHAPE and named the same figure under LIMITS, then
    ``resolve_driver_excitation_ceilings`` read a declared value EQUAL to that
    figure as "no driver-specific level intent". Every link of that chain was
    this file writing a number and this file reading it back as a declaration.

    What replaces it: the ask emits no level ceiling anywhere, and the
    delegation is carried by the field's ABSENCE — a provenance fact rather
    than a magic value.
    """

    from jasper.active_speaker.driver_protection import driver_protection_profile

    policy = driver_protection_profile("tweeter", driver_style="dome_tweeter")
    prompt = _mono_prompt()

    # No injected number, in the template or in the prose.
    assert (
        "max_effective_peak_dbfs"
        not in _prompt_json_example(prompt)["drivers"][0]["level_duration_limits"]
    )
    assert f"at or below {policy.max_auto_level_dbfs:g}" not in prompt
    assert "send exactly the ceiling" not in prompt

    # Absence delegates, through the real resolver.
    undeclared, sensitivities = _cx120_profile(tweeter_peak_dbfs=None)
    tweeter_fp = next(
        t["target_fingerprint"] for t in undeclared["targets"] if t["role"] == "tweeter"
    )
    _band, derived = resolve_driver_excitation_ceilings(
        undeclared,
        tweeter_fp,
        program_admission=True,
        declared_sensitivities=sensitivities,
    )
    assert derived == pytest.approx(-20.7)
    assert derived != policy.max_auto_level_dbfs

    # A declared value is honoured literally, in BOTH directions from the class
    # figure. Louder used to be clamped back to it (and refused at save); that
    # was a code figure overruling a declaration, and it is gone.
    for declared in (policy.max_auto_level_dbfs - 1, policy.max_auto_level_dbfs + 1):
        profile, sens = _cx120_profile(tweeter_peak_dbfs=declared)
        _band, literal = resolve_driver_excitation_ceilings(
            profile,
            next(
                t["target_fingerprint"]
                for t in profile["targets"]
                if t["role"] == "tweeter"
            ),
            program_admission=True,
            declared_sensitivities=sens,
        )
        assert literal == pytest.approx(declared)


def test_cx120_declared_ceiling_delegates_but_one_db_quieter_is_literal() -> None:
    """The field case, through the real resolver, with the numbers named.

    The CX120 reply declares the tweeter at -65 because it has no published
    level limit. On the proven-high-pass path that delegates the choice: the
    derived ceiling is -20.7 dBFS, forty-four decibels louder than the declared
    number. Declaring -66 instead — a deliberate quieter limit — is honoured
    literally. Both are intended; the discontinuity is documented at the
    equality site in excitation_safety_plan and in the research ask itself.

    This fixture is also the second real-hardware case that motivated retiring
    the provisional -35 dBFS hedge on 2026-08-20: a 0.7 dB sensitivity delta
    puts the honest ceiling at -20.7, so the constant bound 14.3 dB below the
    physics on an ordinary coax.
    """

    profile, sensitivities = _cx120_profile()
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
    # woofer cap -20, sensitivity delta 0.7 dB -> -20.7: the sensitivity
    # arithmetic IS the ceiling. Mutation guard: restore the -35 hedge and this
    # fails, because -35 would clamp a real coax 14.3 dB below its own physics.
    assert ceiling == pytest.approx(-20.7)

    # Without the proven-high-pass path the declared number stands. Delegation
    # is what the protective high-pass buys; it is not unconditional.
    _band, naked = resolve_driver_excitation_ceilings(
        profile, tweeter_fp, declared_sensitivities=sensitivities
    )
    assert naked == pytest.approx(-65.0)

    # One dB quieter is a deliberate choice and is never raised.
    quieter, quieter_sens = _cx120_profile(tweeter_peak_dbfs=-66)
    _band, quieter_ceiling = resolve_driver_excitation_ceilings(
        quieter,
        next(
            t["target_fingerprint"] for t in quieter["targets"] if t["role"] == "tweeter"
        ),
        program_admission=True,
        declared_sensitivities=quieter_sens,
    )
    assert quieter_ceiling == pytest.approx(-66.0)


def test_prompt_asks_for_a_published_level_limit_or_none_at_all() -> None:
    """The guidance has to survive being read literally.

    Two earlier drafts failed that. One said "use the ceiling listed under
    LIMITS" and then "a ceiling is not a recommendation" two clauses later.
    The one after it steered a reply onto a class figure this file had
    written — honest about the delegation, but still a round trip through the
    researcher. The ask now states one rule with no number in it.
    """

    prompt = _mono_prompt()
    assert "a ceiling is not a recommendation" not in prompt
    assert "send exactly the ceiling" not in prompt
    assert "use -20 for a woofer" not in prompt
    # One rule: publish it or omit it.
    assert "ONLY when the manufacturer publishes a level limit" in prompt
    assert "Omit the key entirely when they publish none" in prompt
    # Omission is not a gap to be filled with an estimate, which is exactly
    # what the general estimate contract would otherwise tell it to do.
    assert "Never estimate it, and never send a protocol default" in prompt
    # And what fills the silence is named, so the omission does not read as a
    # missing safety bound.
    assert (
        "declared sensitivity against its low-frequency sibling's own limit"
        in prompt
    )


@pytest.mark.parametrize(
    "field,mutation,expected_code",
    [
        # An implausible low limit used to be a case here. It moved out with
        # #2874: a SAVED declaration is operator-authored, and refusing one on
        # a class anchor is the class-over-declaration inversion that ruling
        # ends. It is now a warning that saves, and the refusing arm sits at
        # the research-reply intake -- both pinned in
        # ``test_an_implausible_low_limit_refuses_the_research_reply_and_warns_the_typist``.
        # An estimate louder than the high-frequency class default used to be
        # the level case here. It left with the 2026-08-23 ruling for the same
        # reason the low limit left with #2874: a class figure is not entitled
        # to refuse a declaration. A declared peak is now honoured verbatim,
        # and the only bound left on it is digital full scale -- a real
        # no-headroom bound, refused at the parse
        # (``_normalise_level_duration_limits``: "must be <= 0"), which is why
        # it cannot be expressed as an issue code here. What remains in this
        # object are the three protocol numbers, and a reply that omits one is
        # still incomplete however well sourced it is.
        (
            "level_duration_limits",
            {
                "max_repeat_count": 3,
                "minimum_cooldown_s": 2,
            },
            "tweeter:max_sweep_duration_s_missing",
        ),
        # Nesting: a measurement band reaching outside the hard excitation
        # band. Since #2870 deleted the crossover search band this is the ONLY
        # nesting relationship left, so it carries the whole clamp on its own.
        #
        # The UPPER edge, since #2603: the analysis window's LOWER edge is now
        # DERIVED (clamped up into the allowed band by
        # ``apply_driver_low_limit``), so a below-the-floor analysis window is
        # structurally impossible rather than merely refused. The upper edge is
        # still declared, still unnested-able, and still refused by name.
        # Deleting this clamp left every test in the suite green before #2186,
        # so it is pinned here rather than assumed.
        (
            "measurement_band_hz",
            [4500, 25000],
            "tweeter:measurement_band_outside_hard_band",
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

    profile = compute_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
    )
    codes = [issue["code"] for issue in profile["issues"]]
    assert expected_code in codes, codes
    assert any(i["severity"] == "blocker" for i in profile["issues"])

    # Nothing was rewritten behind the operator's back: the refused value is
    # still exactly what was entered.
    tweeter = next(t for t in profile["targets"] if t["role"] == "tweeter")
    if field == "required_protection_filters":
        assert tweeter["required_protection_filters"][0]["cutoff_hz"] == 700.0


@pytest.mark.parametrize("budget,accepted", [
    ({}, True), ({"max_filters": 3, "boost_floor_hz": 300, "max_gain_db": 8, "max_giveback_db": 4}, True),
    ({"max_filters": 0}, False), ({"max_filters": 9}, False), ({"max_filters": 1.5}, False),
    ({"max_filters": True}, False), ({"boost_floor_hz": 0}, False), ({"boost_floor_hz": float("nan")}, False),
    ({"max_gain_db": 0}, False), ({"max_giveback_db": 0}, True),
    ({"max_gain_db": -1}, False), ({"max_gain_db": 13}, False), ({"max_gain_db": True}, False),
    ({"max_giveback_db": -1}, False), ({"max_giveback_db": 19}, False),
    ({"max_giveback_db": float("inf")}, False), ({"extra": 1}, False), (None, False),
])
def test_declared_target_fit_budget_round_trip_and_refusal(budget, accepted):
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][0]["fit_budget"] = budget
    if not accepted:
        with pytest.raises(DriverSafetyProfileError):
            compute_driver_safety_profile(topology, manual_settings=manual, driver_research=None)
        return
    draft = build_design_draft(topology, manual_settings=manual, created_at="2026-09-12T00:00:00Z")
    profile = design_draft_view(draft)["driver_safety_profile"]
    assert profile["targets"][0].get("fit_budget", {}) == budget


@pytest.mark.parametrize("missing", ["level_duration_limits", "measurement_band_hz", "hard_excitation_band_hz"])
def test_apply_requires_only_the_floor_but_measurement_requires_its_inputs(missing):
    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["crossover_candidates"] = [{"between_roles": ["woofer", "tweeter"],
        "frequency_hz": 5500, "filter_type": "Linkwitz-Riley", "slope_db_per_octave": 24}]
    manual["drivers"][1].pop("required_protection_filters")
    manual["drivers"][1].pop(missing)
    draft = build_design_draft(topology, manual_settings=manual)
    with pytest.raises(MeasurementGraphRefused) as refused:
        load_tuning_declaration(topology, design_draft=draft)
    assert refused.value.code == "tweeter:required_highpass_missing"
    assert refused.value.detail["target_id"] == "mono:tweeter"
    manual["drivers"][1]["recommended_highpass_hz"] = 5000
    draft = build_design_draft(topology, manual_settings=manual)
    applied = load_tuning_declaration(topology, design_draft=draft)
    assert applied.protection_sections_by_role["tweeter"]
    profile = design_draft_view(draft)["driver_safety_profile"]
    tweeter = next(t for t in profile["targets"] if t["role"] == "tweeter")
    with pytest.raises(ExcitationSafetyPlanError) as refused_measurement:
        prepare_driver_excitation_plan(topology, profile, _requested(tweeter["target_fingerprint"]))
    assert refused_measurement.value.code == ExcitationSafetyPlanRefusal.MEASUREMENT_INPUTS_INVALID.value
    assert refused_measurement.value.detail["target_id"] == "mono:tweeter"
    assert refused_measurement.value.detail["code"] == "tweeter:" + missing.replace("_hz", "") + "_missing"


@pytest.mark.parametrize("missing", ["highpass", "lowpass"])
def test_apply_names_the_mid_target_when_a_required_corner_is_missing(missing):

    topology = mono_output_topology(mode="active_3_way", card_id=None)
    manual = _manual_settings()
    manual["drivers"].append({"target_id": "mono:mid", "role": "mid", "model": "Mid",
        "required_protection_filters": [{"kind": kind, "cutoff_hz": cutoff,
            "minimum_slope_db_per_octave": 24}
            for kind, cutoff in (("highpass", 300), ("lowpass", 5000)) if kind != missing]})
    with pytest.raises(MeasurementGraphRefused) as refused:
        load_tuning_declaration(topology, design_draft=build_design_draft(topology, manual_settings=manual))
    assert refused.value.code == f"mid:required_{missing}_missing"
    assert refused.value.detail["target_id"] == "mono:mid"
