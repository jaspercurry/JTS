# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
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
    build_driver_research_request,
    build_driver_safety_profile,
    driver_research_targets,
    evaluate_driver_safety_profile,
    validate_driver_research_request,
    validate_driver_research_result_shape,
)
from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
from jasper.active_speaker.driver_protection import (
    LOW_LIMIT_PLAUSIBILITY_FACTOR,
    driver_low_limit_plausibility_band_hz,
)
from jasper.active_speaker.excitation_safety_plan import (
    resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology


def _blocked_codes(
    topology: OutputTopology,
    manual: dict,
    *,
    driver_research: dict | None = None,
) -> set[str]:
    """Blocking issue codes on the profile a SAVE of these values would land.

    Saving no longer refuses -- the operator keeps their work whatever they
    typed -- so "refused" now means the artifact lands ``incomplete`` and never
    reads as current, which is what every measurement gate consults. Both halves
    are asserted here so a caller cannot accidentally pin only the code.
    """

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=driver_research,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert profile["status"] == "incomplete"
    assert profile["confirmation"] is None
    assert (
        evaluate_driver_safety_profile(profile, topology).confirmed_and_current is False
    )
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


def test_the_prompt_still_recommends_the_duration_the_composer_requests() -> None:
    """#2921: the prompt's recommended sweep duration and the MEASURE
    composer's own nominal are the SAME number, and that coincidence is what
    made the admission collision fleet-wide rather than one box's bad luck.

    A sweep realizes at the nearest phase-closing length, so a 4 s request over
    a woofer band often lands just above 4 s -- above the very limit the prompt
    told the operator's assistant to declare. ``build_measure_program`` now
    fits its sweeps to that limit, which is why the prompt keeps its
    recommendation. This pins the pair rather than the prose: if either number
    moves, the paragraph in that composer's docstring explaining the collision
    stops describing this build, and this test says so.
    """
    from jasper.audio_measurement.program import DEFAULT_WOOFER_SWEEP_S

    request = build_driver_research_request(
        mono_output_topology(card_id=None), _operator_inputs(), _manual_settings(),
    )
    prompt = build_driver_research_prompt(request)

    assert "Send max_sweep_duration_s 4, max_repeat_count 3" in prompt
    assert '"max_sweep_duration_s":4' in prompt
    assert DEFAULT_WOOFER_SWEEP_S == 4.0


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
    from jasper.active_speaker.staging import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    prompt = build_driver_research_prompt(
        build_driver_research_request(
            mono_output_topology(card_id=None), _operator_inputs(), _manual_settings()
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
        build_driver_research_request(topology, _operator_inputs(), _manual_settings())
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
        build_driver_research_request(topology, _operator_inputs(), _manual_settings())
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
    prompted for.  ``horn_coverage_deg`` was researchable too, until #2872
    deleted it for never having gained a reader; see
    ``test_prompt_asks_only_for_fields_with_a_consumer``."""
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
    """The ask shrank; acceptance did not.  A reply that still carries the
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
        assert driver["gain_offset_db"] == -6.0
        assert driver["gain_offset_db_provenance"] == "research_estimate"
    for field in verbose:
        assert field in _V2_RESEARCH_DRIVER_FIELDS


def test_legacy_research_horn_coverage_deg_is_tolerated_and_dropped() -> None:
    """#2872: the retired key survives the gates but not the record.

    A v2 research result persisted before the deletion — or a chat that still
    volunteers the key — reaches the same three allowlists a stored manual
    driver does.  Refusing it there would make an existing draft unsaveable
    over a field nothing reads, so the gates tolerate it and the normalisers
    drop it.  The schema itself no longer carries the key: that is what makes
    this tolerance a migration and not a quiet second definition.
    """

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(
        topology,
        _operator_inputs(),
        _manual_settings(),
    )
    research = _research_result(request)
    for driver in research["drivers"]:
        driver["horn_coverage_deg"] = 90
    manual_settings = _manual_settings()
    for driver in manual_settings["drivers"]:
        driver["horn_coverage_deg"] = 90

    # Gate 1: the v2 result-shape allowlist.
    validate_driver_research_result_shape(research)
    # Gates 2 and 3: design_draft's manual-driver allowlist and driver_safety's
    # re-validation of the same normalised record.
    draft = build_design_draft(
        topology,
        driver_research_request=request,
        driver_research=research,
        manual_settings=manual_settings,
        operator_inputs=_operator_inputs(),
    )

    for driver in draft["driver_research"]["drivers"]:
        assert "horn_coverage_deg" not in driver
    for driver in draft["manual_settings"]["drivers"]:
        assert "horn_coverage_deg" not in driver
    assert draft["driver_safety_profile"] is not None
    assert "horn_coverage_deg" not in _V2_RESEARCH_DRIVER_FIELDS

    # build_design_draft normalises manual_settings before handing them on, so
    # the draft path alone never puts a raw legacy record in front of gate 3.
    # This exported builder does: it is the public entry point, and a caller
    # with a stored record reaches its allowlist directly.
    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual_settings,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    assert profile["issues"] == []
    assert all(
        "horn_coverage_deg" not in target for target in profile["targets"]
    )


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


def test_incomplete_values_save_as_incomplete_and_never_read_as_current() -> None:
    """A half-declared profile SAVES, and is still refused by every gate.

    The confirm ceremony is gone, so a partial declaration no longer bounces the
    save -- the operator keeps their work. What must not move is the verdict:
    ``incomplete`` still evaluates NOT confirmed_and_current, which is the one
    fail-closed half the measurement loop still relies on.
    """

    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    manual["drivers"][1].pop("required_protection_filters")

    saved = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert saved["status"] == "incomplete"
    assert saved["confirmation"] is None
    assert any(
        "required_highpass_missing" in issue["code"] for issue in saved["issues"]
    )
    evaluation = evaluate_driver_safety_profile(saved, topology)
    assert evaluation.status == "incomplete"
    assert evaluation.confirmed_and_current is False

    missing_duration = _manual_settings()
    missing_duration["drivers"][0]["level_duration_limits"].pop("max_sweep_duration_s")
    partial = build_driver_safety_profile(
        topology,
        manual_settings=missing_duration,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert partial["status"] == "incomplete"
    assert any(
        "max_sweep_duration_s_missing" in issue["code"] for issue in partial["issues"]
    )
    assert (
        evaluate_driver_safety_profile(partial, topology).confirmed_and_current is False
    )

    # The save timestamp is still required and still canonical -- it is what the
    # confirmation record dates, so an empty one would publish an undated write.
    with pytest.raises(DriverSafetyProfileError, match="confirmed_at is required"):
        build_driver_safety_profile(
            topology,
            manual_settings=_manual_settings(),
            driver_research=None,
            saved_at="",
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


def test_a_visible_edit_rotates_the_fingerprint_without_closing_the_loop() -> None:
    """The nanny loop, pinned shut.

    A safety-relevant edit still rotates the profile fingerprint -- that is how
    every downstream identity binding notices the values moved. What it must NOT
    do any more is drop the artifact into a state the measurement loop refuses:
    the rebuild re-stamps the confirmation over the NEW fingerprint, so the
    speaker is measurable the instant the edit is saved.
    """

    topology = mono_output_topology(card_id=None)
    first = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    edited = _manual_settings()
    edited["drivers"][1]["hard_excitation_band_hz"] = [4800.0, 22000.0]

    rebuilt = build_driver_safety_profile(
        topology,
        manual_settings=edited,
        driver_research=None,
        saved_at="2026-07-13T12:05:00Z",
    )

    assert rebuilt["profile_fingerprint"] != first["profile_fingerprint"]
    assert rebuilt["status"] == "confirmed"
    assert (
        rebuilt["confirmation"]["confirmed_fingerprint"]
        == rebuilt["profile_fingerprint"]
    )
    assert rebuilt["confirmation"]["confirmed_at"] == "2026-07-13T12:05:00Z"
    evaluation = evaluate_driver_safety_profile(rebuilt, topology)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True
    # Still not an audio authorization -- the physics gates are unchanged.
    assert rebuilt["authorizes_playback"] is False
    assert evaluation.to_dict()["authorizes_playback"] is False


def test_every_save_re_dates_the_declaration_and_keeps_it_current(
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
    assert first["driver_safety_profile_evaluation"]["confirmed_and_current"] is True

    edited = _manual_settings()
    edited["drivers"][1]["measurement_band_hz"] = [5000.0, 19000.0]
    changed = save_design_draft(
        topology,
        manual_settings=edited,
        operator_inputs=_operator_inputs(),
        path=path,
        created_at="2026-07-13T12:02:00Z",
    )
    assert (
        changed["driver_safety_profile"]["profile_fingerprint"]
        != first["driver_safety_profile"]["profile_fingerprint"]
    )
    assert changed["driver_safety_profile"]["status"] == "confirmed"
    assert changed["driver_safety_profile_evaluation"]["status"] == "confirmed"
    assert (
        load_design_draft(path)["driver_safety_profile"]
        == (changed["driver_safety_profile"])
    )


def test_a_profile_saved_before_the_confirm_step_was_retired_reads_as_current(
    tmp_path: Path,
) -> None:
    """Field boxes unbrick on deploy, not on the next save.

    ``needs_confirmation`` is no longer written, but boxes already carry it on
    disk -- including the one whose measured accept produced it. Reporting that
    artifact as malformed would keep the loop shut for exactly the speakers this
    change exists to reopen, so it is read under the current definition instead.
    """

    topology = mono_output_topology(card_id=None)
    legacy = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    legacy["status"] = "needs_confirmation"
    legacy["confirmation"] = None

    evaluation = evaluate_driver_safety_profile(legacy, topology)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True
    assert evaluation.reasons == ()

    # The next ordinary save collapses the stored status; no migration pass.
    path = tmp_path / "active_speaker_design_draft.json"
    saved = save_design_draft(
        topology,
        manual_settings=_manual_settings(),
        operator_inputs=_operator_inputs(),
        path=path,
        created_at="2026-07-13T12:10:00Z",
    )
    assert saved["driver_safety_profile"]["status"] == "confirmed"


def test_a_legacy_artifact_carrying_blocking_issues_never_reads_as_current() -> None:
    """The legacy read is fail-closed ONLY because of where it sits.

    ``evaluate_driver_safety_profile`` returns for ``derived_issues`` BEFORE it
    reaches the ``needs_confirmation`` compatibility branch. That ordering is
    the entire safety property: hoist the branch above the issues gate and a
    half-declared profile reads ``confirmed`` -- the measurement loop would then
    run against a declaration carrying no level or duration ceiling at all.
    Nothing in the branch itself says so, so this is the test that says it.

    The artifact is the one the hoist would wave through: a stored
    ``needs_confirmation`` status whose ``issues`` are CORRECTLY derived and
    non-empty, so it clears every earlier gate (schema, fingerprint, target
    binding, issue-payload equality) and lands on the ordering.
    """

    topology = mono_output_topology(card_id=None)
    manual = _manual_settings()
    for driver in manual["drivers"]:
        driver.pop("level_duration_limits", None)

    incomplete = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert incomplete["status"] == "incomplete"
    codes = {issue["code"] for issue in incomplete["issues"]}
    assert codes == {
        "woofer:level_duration_limits_missing",
        "tweeter:level_duration_limits_missing",
    }

    legacy = dict(incomplete)
    legacy["status"] = "needs_confirmation"
    legacy["confirmation"] = None

    evaluation = evaluate_driver_safety_profile(legacy, topology)
    # The load-bearing assertion: blocking issues outrank the compatibility
    # read, whatever the stored status says.
    assert evaluation.confirmed_and_current is False
    assert evaluation.status != "confirmed"
    assert evaluation.reasons != ()


def test_profile_refuses_stale_topology_and_fingerprint_tampering() -> None:
    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )

    moved_tweeter = mono_output_topology(card_id=None, tweeter_output=2)
    stale = evaluate_driver_safety_profile(profile, moved_tweeter)
    assert stale.status == "stale"
    assert stale.confirmed_and_current is False

    # Tampering with a value the low limit does NOT own is caught by the
    # fingerprint. The analysis window's UPPER edge is that value: since #2603
    # its LOWER edge is DERIVED from the declared low limit (so a tampered one
    # is caught earlier and by a more specific name -- pinned separately
    # below), while the ceiling stays a plain stored declaration.
    tampered = deepcopy(profile)
    tampered["targets"][1]["measurement_band_hz"][1] = 9000.0
    malformed = evaluate_driver_safety_profile(tampered, topology)
    assert malformed.status == "malformed"
    assert malformed.reasons == ("driver_safety_profile_fingerprint_mismatch",)


def test_a_profile_whose_derived_fields_left_its_own_low_limit_is_named() -> None:
    """The #2603 stale-declaration path, and the reason it is NOT the generic
    malformed answer.

    A profile carrying two disagreeing numbers for one driver's low limit is
    exactly what the one-owner ruling exists to end, and it is what every box
    confirmed before this change may be carrying. Telling that household
    "schema invalid" reads as corruption and names no remedy; the actionable
    truth is that the driver profile needs re-confirming at /sound/.

    It RETURNS rather than raises, so a box in this state reports and waits.
    Playback is untouched -- the staged CamillaDSP graph is a separate
    artifact, so the speaker keeps working while the profile waits.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )

    split = deepcopy(profile)
    # The jts3 shape: the hard floor and the protective high-pass disagree.
    split["targets"][1]["hard_excitation_band_hz"][0] = 4000.0
    evaluation = evaluate_driver_safety_profile(split, topology)
    assert evaluation.status == "malformed"
    assert evaluation.reasons == ("driver_safety_profile_low_limit_stale",)
    assert evaluation.confirmed_and_current is False


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

    profile = build_driver_safety_profile(
        topology, manual_settings=manual, driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
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
    profile = build_driver_safety_profile(
        topology, manual_settings=_manual_settings(), driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )

    for target in profile["targets"]:
        assert not [n for n in target["unknowns"] if "was replaced by" in n]


def test_a_stale_profile_whose_rebuild_would_refuse_says_so_in_its_reasons() -> None:
    """jts3's own shape, minimised: stale AND unconfirmable in one step.

    Deriving the low limit raises the hard band's lower edge to the declared
    owner, which can leave another declared value outside the band it must nest
    in -- and ``build_driver_safety_profile`` REFUSES to confirm while that
    stands. The stale name alone cannot tell /sound/ that, so the button was
    offered on a profile whose rebuild raises and the operator's first click
    came back a bare reason code.

    Verified against jts3's real stored artifacts during the fix round: its
    tweeter declares 2000 Hz while its bands were nested against the old 1600,
    so it lands exactly here. Reproduced with the shipped fixture rather than
    the box's file, which is not this repo's to carry.

    WHICH value the rebuild trips on moved with #2870 -- it was the declared
    crossover-search band, and that field is gone. See the fixture note below
    for why the shape still has to be covered against the bounds that survive.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )

    split = deepcopy(profile)
    tweeter = split["targets"][1]
    # jts3's exact shape: a stored profile target carries no owner field, so the
    # low limit is inferred from its protective high-pass -- and that cutoff
    # sits ABOVE the floor its own bands were nested against. jts3 reads 2000
    # from the filter with its bands nested at 1600; the fixture reads 21000
    # against bands nested at 5000.
    #
    # The cutoff was 6000 until #2870. Deriving 6000 used to push the declared
    # crossover-search band under its own hard band, and that was the rebuild's
    # blocker; with the search band deleted, 6000 rebuilds cleanly and the
    # fixture would have silently become a duplicate of the CONTROL below.
    # 21000 keeps the shape that has to stay covered -- a stale profile whose
    # rebuild really is blocked -- against the bounds that survive.
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 21000.0

    evaluation = evaluate_driver_safety_profile(split, topology)

    assert evaluation.status == "malformed"
    assert evaluation.confirmed_and_current is False
    # Still named first, so the "re-confirm" remedy still renders...
    assert evaluation.reasons[0] == "driver_safety_profile_low_limit_stale"
    # ...and the rebuild's own blockers ride with it, in the `<role>:<code>`
    # vocabulary the page already knows how to phrase.
    assert "tweeter:measurement_band_outside_hard_band" in evaluation.reasons

    # The claim that this REALLY is unusable, rather than a reason string
    # nobody checked: the rebuild lands the same codes as blocking issues.
    manual = deepcopy(_manual_settings())
    manual["drivers"][1]["recommended_highpass_hz"] = 21000.0
    assert _blocked_codes(topology, manual) == {
        "tweeter:measurement_band_outside_hard_band",
        "tweeter:low_limit_implausible_for_style",
    }


def test_a_stale_profile_that_would_rebuild_cleanly_offers_no_blocker() -> None:
    """The control for the pair above — otherwise the gate reads as "always off".

    A stale profile whose derivation leaves every other declaration coherent
    carries the stale name and NOTHING else, so /sound/ still offers the
    button and the household re-confirms in one click.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    split = deepcopy(profile)
    split["targets"][1]["hard_excitation_band_hz"][0] = 4000.0

    evaluation = evaluate_driver_safety_profile(split, topology)

    assert evaluation.reasons == ("driver_safety_profile_low_limit_stale",)


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


def test_a_confirmed_target_carries_the_declared_pair_beside_its_projections(
) -> None:
    """The un-fusing (#2897). Two slopes, both on the record, distinguishable.

    Before this, a confirmed target held only ``max(published, 24)`` on the
    derived protective high-pass, and no reader could recover what the
    manufacturer actually printed — which is how the topology gate came to
    refuse a household's order-2 pin against a 24 no datasheet contains.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_de250_manual(),
        driver_research=None,
        saved_at="2026-08-23T00:00:00Z",
    )
    assert profile["status"] == "confirmed"
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
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-08-23T00:00:00Z",
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


def test_a_profile_stored_before_the_declared_pair_stays_confirmed() -> None:
    """#2897's read-back tolerance, measured rather than argued.

    The confirmed fingerprint is computed from the profile's OWN stored
    targets, so adding a field to what the BUILDER writes cannot re-key an
    artifact already on disk. A speaker whose profile predates this change must
    keep playing across the deploy rather than reading ``malformed`` until
    somebody re-saves numbers that did not change.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_de250_manual(),
        driver_research=None,
        saved_at="2026-08-23T00:00:00Z",
    )
    stored_before = deepcopy(profile)
    for target in stored_before["targets"]:
        target.pop("recommended_highpass_hz", None)
        target.pop("recommended_highpass_slope_db_per_octave", None)
    # The stored digest is the pre-change one, unchanged by the pop above only
    # because it was never over these keys — so re-derive it the way a
    # pre-change build would have, and confirm the evaluation accepts it.
    stored_before["profile_fingerprint"] = driver_safety_module._fingerprint(
        {
            key: stored_before.get(key)
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
    )
    stored_before["confirmation"]["confirmed_fingerprint"] = (
        stored_before["profile_fingerprint"]
    )
    assert stored_before["profile_fingerprint"] != profile["profile_fingerprint"]

    evaluation = evaluate_driver_safety_profile(stored_before, topology)

    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True
    assert evaluation.reasons == ()


def test_a_profile_carrying_a_retired_field_is_named_not_called_corrupt() -> None:
    """#2870 hazard 1, and the migration this PR deliberately does NOT automate.

    ``crossover_search_band_hz`` was part of the hashed profile core, so every
    speaker confirmed before the ruling carries it and every one of them reads
    ``malformed`` until it is saved again. That is accepted — the fleet is lab
    boxes — but it must be LOUD and ACTIONABLE rather than silent, and it must
    not read as damage: the generic answer here is
    ``driver_safety_profile_schema_invalid``, which /sound phrases as "JTS could
    not read these limits" and which names no remedy.

    So the retired field gets its own name, exactly as the #2603 stale-low-limit
    case does, and /sound phrases it as "save them again". No auto-migration is
    written: a rebuild re-derives every target from the values the operator can
    see, and silently rewriting a confirmed safety declaration behind their back
    is the wrong direction for a declaration whose whole point is that a human
    made it.
    """

    topology = mono_output_topology(card_id=None)
    profile = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert profile["status"] == "confirmed"

    # A profile as a box confirmed it before the ruling: identical in every
    # respect except that its targets still carry the retired field.
    legacy = deepcopy(profile)
    for target in legacy["targets"]:
        target["crossover_search_band_hz"] = [1200.0, 3500.0]

    evaluation = evaluate_driver_safety_profile(legacy, topology)
    assert evaluation.status == "malformed"
    assert evaluation.confirmed_and_current is False
    assert evaluation.reasons == (
        driver_safety_module.DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON,
    )

    # The control, and the reason this is a NAME rather than a widening: an
    # unknown field that is NOT a retired one still reads as schema-invalid.
    corrupt = deepcopy(profile)
    corrupt["targets"][0]["not_a_field_this_build_ever_had"] = 1
    assert evaluate_driver_safety_profile(corrupt, topology).reasons == (
        "driver_safety_profile_schema_invalid",
    )

    # And saving really does clear it: the remedy the copy names is the remedy.
    rebuilt = build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    assert evaluate_driver_safety_profile(rebuilt, topology).confirmed_and_current


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
    (``build_driver_safety_profile``'s own manual gate, which
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
    profile = build_driver_safety_profile(
        topology,
        manual_settings=accept_manual,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    assert profile["status"] == "confirmed"
    assert all(
        "crossover_search_band_hz" not in target for target in profile["targets"]
    )
    # …and the rebuilt profile is immediately usable, which is the point of
    # tolerating rather than refusing.
    assert evaluate_driver_safety_profile(profile, topology).confirmed_and_current

    # The stored research request has its own test below: it needs a request
    # built by the PRE-CUT builder, which this file cannot construct.

    # The eval reason still fires for a profile that has NOT been re-saved --
    # tolerance at the write gates must not quietly confirm a stale artifact.
    legacy_profile = deepcopy(profile)
    for target in legacy_profile["targets"]:
        target["crossover_search_band_hz"] = [1200.0, 3500.0]
    evaluation = evaluate_driver_safety_profile(legacy_profile, topology)
    assert evaluation.confirmed_and_current is False
    assert evaluation.reasons == (
        driver_safety_module.DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON,
    )


#: A driver-research request as a PRE-#2870 build actually wrote it, frozen from
#: that build's own ``build_driver_research_request``. Its
#: ``operator_declared_context`` carries ``crossover_search_band_hz`` AND its
#: ``request_fingerprint`` was computed over a core INCLUDING that key.
#:
#: That inversion is the whole reason this is a fixture rather than something
#: built here. A request built by THIS branch and then injected with the field
#: is fingerprinted WITHOUT it, and the branch normaliser drops the injection --
#: so the recomputed core matches the original and the digest agrees by
#: accident. Such a test passes whether or not the tolerance exists. A real
#: box's request is the other way round, and only the frozen artifact has it.
_PRE_2870_REQUEST_FIXTURE = (
    Path(__file__).parent / "fixtures"
    / "driver_research_request_pre_2870_20260822" / "request.json"
)


def test_a_research_request_fingerprinted_before_the_cut_still_validates() -> None:
    """#2870's migration, one line deeper than the allowlist.

    Tolerating the retired key in ``operator_declared_context``'s allowlist is
    NECESSARY AND NOT SUFFICIENT, because that context is FINGERPRINTED. The
    build that wrote a stored request hashed a core whose context still carried
    the key; dropping it recomputes a different digest, so the request is
    refused ``fingerprint is invalid`` -- and ``design_draft.build_design_draft``
    re-validates the stored request on EVERY save, which is precisely the save
    /sound's own copy tells the household to make. The browser re-sends the
    stored request verbatim and v2 replies may not drop it, so the household
    would be left with a raw internal error and no signposted way out.

    The fix follows this PR's own doctrine one module over
    (``crossover_v2.topology_prescription._parse_prescription``): a document this
    repository already wrote must stay readable across the deploy that retired
    the field. The digest is accepted when it matches the core computed WITH the
    retired key present, and the record is RE-STAMPED to the current shape on the
    way out, so the tolerance is transitional rather than permanent.
    """

    banked = json.loads(_PRE_2870_REQUEST_FIXTURE.read_text())
    manual = banked["manual_settings"]
    inputs = banked["operator_inputs"]
    request = banked["driver_research_request"]
    topology = mono_output_topology(card_id=None)

    # The premise, asserted on BOTH halves -- the earlier version of this test
    # checked only that manual_settings carried the field, which is exactly how
    # it missed that the fingerprint is the thing that matters.
    context = request["targets"][0]["operator_declared_context"]
    assert "crossover_search_band_hz" in context
    stored_fingerprint = request["request_fingerprint"]
    assert stored_fingerprint == driver_safety_module._fingerprint(
        {k: v for k, v in request.items() if k != "request_fingerprint"}
    ), "the fixture's digest must cover the core that CARRIES the retired key"

    validated = validate_driver_research_request(request, topology, inputs, manual)

    # Tolerated on the way in...
    assert all(
        "crossover_search_band_hz" not in (
            target.get("operator_declared_context") or {}
        )
        for target in validated["targets"]
    ), "tolerated, but it must never be stored again"
    # ...and RE-STAMPED, so the draft this save writes matches on its own terms
    # next time rather than depending on the tolerance forever.
    assert validated["request_fingerprint"] != stored_fingerprint
    assert validated["request_fingerprint"] == driver_safety_module._fingerprint(
        {k: v for k, v in validated.items() if k != "request_fingerprint"}
    )

    # Re-validating the RE-STAMPED record needs no tolerance at all: that is
    # what makes this transitional rather than a permanent second shape.
    again = validate_driver_research_request(validated, topology, inputs, manual)
    assert again["request_fingerprint"] == validated["request_fingerprint"]

    # The tolerance is narrow: a digest matching NEITHER core is still refused,
    # so this did not become "any fingerprint will do".
    forged = dict(request, request_fingerprint="0" * 64)
    with pytest.raises(DriverSafetyProfileError, match="fingerprint is invalid"):
        validate_driver_research_request(forged, topology, inputs, manual)


def test_a_pre_2870_request_and_the_v2_result_bound_to_it_migrate_together() -> None:
    """The re-stamp's own second-order hazard, caught by walking the real save.

    A v2 result ECHOES its request's fingerprint, and
    ``validate_research_result_binding`` compares the two. Re-stamping the
    request therefore ORPHANS the result the same box stored beside it: the save
    stops failing at ``fingerprint is invalid`` and starts failing one gate
    later at ``does not match the current request``. Migrating one of a matched
    pair is not a migration, and the household sees the same dead end.

    ``design_draft`` owns the pair -- it is the module that refuses a v2 result
    with no request -- so ``_rebound_to_restamped_request`` carries the result
    across, and this walks ``build_design_draft`` end to end rather than either
    validator alone, because the coupling only exists between them.
    """

    banked = json.loads(_PRE_2870_REQUEST_FIXTURE.read_text())
    request = banked["driver_research_request"]
    stored_fingerprint = request["request_fingerprint"]

    # The v2 result as that same pre-cut build stored it: echoing the request's
    # own digest, the one computed WITH the retired context key.
    result = {
        "artifact_schema_version": 2,
        "kind": DRIVER_RESEARCH_KIND,
        "request_fingerprint": stored_fingerprint,
        "drivers": [
            {
                "target_id": target["target_id"],
                "target_fingerprint": target["target_fingerprint"],
                "role": target["role"],
                "model": target["manufacturer_and_model"],
            }
            for target in request["targets"]
        ],
    }

    draft = build_design_draft(
        mono_output_topology(card_id=None),
        operator_inputs=banked["operator_inputs"],
        manual_settings=banked["manual_settings"],
        driver_research=result,
        driver_research_request=request,
    )

    saved_request = draft["driver_research_request"]
    assert saved_request["request_fingerprint"] != stored_fingerprint
    # Both halves land in the NEW shape, so the next save needs no tolerance.
    assert (
        draft["driver_research"]["request_fingerprint"]
        == saved_request["request_fingerprint"]
    )

    # Narrow, not a blanket rebind: a result bound to neither digest is still
    # refused, which is the binding check's entire job.
    foreign = dict(result, request_fingerprint="0" * 64)
    with pytest.raises(
        ActiveSpeakerDesignDraftError, match="does not match the current request"
    ):
        build_design_draft(
            mono_output_topology(card_id=None),
            operator_inputs=banked["operator_inputs"],
            manual_settings=banked["manual_settings"],
            driver_research=foreign,
            driver_research_request=request,
        )


def test_the_sound_page_phrases_the_retired_field_reason_by_name() -> None:
    """The server's name and /sound's copy are one contract across two files.

    The reason exists only so the household is told a remedy, so a name the
    page cannot phrase falls back to "JTS could not read these limits" and buys
    nothing. Pinned like the low-limit-stale name it mirrors.
    """

    js = _SOUND_MAIN_JS.read_text()
    assert (
        f"'{driver_safety_module.DRIVER_SAFETY_PROFILE_RETIRED_FIELD_REASON}'"
        in js
    )
    # …and the copy says what to do, rather than only what happened.
    assert "no longer uses" in js
    assert "save them" in js


def test_evaluation_recomputes_issues_instead_of_trusting_serialized_status() -> None:
    topology = mono_output_topology(card_id=None)
    incomplete_manual = _manual_settings()
    incomplete_manual["drivers"][1].pop("required_protection_filters")
    profile = build_driver_safety_profile(
        topology,
        manual_settings=incomplete_manual,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
    assert _blocked_codes(topology, legacy).issuperset(
        {
            "left:woofer:target_specific_values_missing",
            "left:tweeter:target_specific_values_missing",
            "right:woofer:target_specific_values_missing",
            "right:tweeter:target_specific_values_missing",
        }
    )

    explicit = build_driver_safety_profile(
        topology,
        manual_settings=_stereo_manual_settings(),
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
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
    # The 2026-08-23 ruling REVERSED the first half of this test, for the same
    # reason #2603 and #2874 reversed the rest of it one layer down: a class
    # figure may not refuse a declaration. A declared peak LOUDER than the
    # tweeter class default used to land `max_effective_peak_above_code_policy`
    # and hold the profile `incomplete`; it now saves confirmed, and
    # `resolve_driver_excitation_ceilings` honours -64.0 verbatim rather than
    # clamping it back to -65.
    topology = mono_output_topology(card_id=None)
    louder_than_class_default = _manual_settings()
    louder_than_class_default["drivers"][1]["level_duration_limits"][
        "max_effective_peak_dbfs"
    ] = -64.0
    saved = build_driver_safety_profile(
        topology,
        manual_settings=louder_than_class_default,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert saved["status"] == "confirmed"
    assert _issue_codes(saved) == set()

    # And a target that declares NO level limit at all is confirmable too --
    # the ordinary shape now that the ask requests one only where a maker
    # publishes it.
    undeclared = _manual_settings()
    undeclared["drivers"][1]["level_duration_limits"].pop("max_effective_peak_dbfs")
    no_level = build_driver_safety_profile(
        topology,
        manual_settings=undeclared,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert no_level["status"] == "confirmed"
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
    accepted = build_driver_safety_profile(
        compression,
        manual_settings=below_default,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert accepted["status"] == "confirmed"
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
    warned = build_driver_safety_profile(
        compression,
        manual_settings=unsafe_highpass,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    assert warned["status"] == "confirmed"
    assert [
        issue["code"] for issue in warned["issues"]
        if issue["severity"] == "blocker"
    ] == []
    warning = next(
        issue for issue in warned["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    assert warning["severity"] == "warning"
    assert "200 Hz" in warning["message"]
    assert "500-8000 Hz" in warning["message"]
    assert "transposed digit" in warning["message"]


def test_an_implausible_low_limit_refuses_the_research_reply_and_warns_the_typist(
) -> None:
    """#2874's author split, both arms, on the same number.

    Owner ruling 2026-08-22: declared values are the only refusing authority.
    So the plausibility band -- which is anchored on the class table -- keeps
    its teeth exactly where the author is a machine, and becomes a disclosure
    where the author is the household:

    * a RESEARCH REPLY carrying 700 Hz for a tweeter whose class band is
      [1250, 20000] is refused at intake. "Ask again with the datasheet" is the
      right answer to an LLM misreading one, and refusing at the paste means
      the number never becomes a declaration anyone has to un-declare;
    * the SAME 700 Hz typed by hand SAVES, with a warning that names the value,
      the band it missed, the anchor that band came from, and the two things it
      is most likely to be. The tinker box trusts its owner and says so first.
    """

    topology = mono_output_topology(card_id=None)
    request = build_driver_research_request(topology, _operator_inputs())

    implausible_reply = _research_result(request)
    tweeter_reply = next(
        driver for driver in implausible_reply["drivers"]
        if driver["role"] == "tweeter"
    )
    tweeter_reply["recommended_highpass_hz"] = 700
    with pytest.raises(
        ActiveSpeakerDesignDraftError,
        match="not believable for its driver type",
    ) as refused:
        build_design_draft(
            topology,
            driver_research_request=request,
            driver_research=implausible_reply,
            operator_inputs=_operator_inputs(),
        )
    # The refusal teaches: the number, the band, the anchor, and the two ways
    # out (a datasheet page, or typing it yourself).
    assert "700 Hz" in str(refused.value)
    assert "1250-20000 Hz" in str(refused.value)
    assert "(class default 5000 Hz)" in str(refused.value)
    assert "Ask again with the datasheet" in str(refused.value)
    assert "by hand under Advanced" in str(refused.value)

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
    profile = build_driver_safety_profile(
        topology,
        manual_settings=typed,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    assert profile["status"] == "confirmed"
    assert profile["confirmation"] is not None
    warning = next(
        issue for issue in profile["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    assert warning["severity"] == "warning"
    assert "700 Hz" in warning["message"]
    assert "1250-20000 Hz" in warning["message"]
    assert "(class default 5000 Hz)" in warning["message"]
    assert "transposed digit" in warning["message"]

    # A warning is not a blocker: the profile it rides on is usable, and the
    # evaluation re-derives the same warning rather than reading it off disk.
    evaluation = evaluate_driver_safety_profile(profile, topology)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True

    # And a hand-edited artifact cannot quietly drop its own warning...
    tampered = deepcopy(profile)
    tampered["issues"] = []
    assert evaluate_driver_safety_profile(tampered, topology).status == "malformed"

    # ...nor re-code or downgrade one.
    recoded = deepcopy(profile)
    recoded["issues"][0]["code"] = "tweeter:something_else"
    assert evaluate_driver_safety_profile(recoded, topology).status == "malformed"
    downgraded = deepcopy(profile)
    downgraded["issues"][0]["severity"] = "info"
    assert evaluate_driver_safety_profile(downgraded, topology).status == "malformed"

    # ...but a DIFFERENT warning SENTENCE is not a mismatch, and this half is
    # deliberate. Warning prose interpolates the household's own numbers, so
    # comparing it byte-for-byte made editing the copy a breaking change: a
    # profile written one commit earlier read `malformed` and lost
    # `confirmed_and_current` although its declared values had not moved by one
    # digit. No gate reads the sentence, and the fingerprint never covered
    # `issues` at all, so excluding it loosens nothing the digest was holding.
    reworded = deepcopy(profile)
    reworded["issues"][0]["message"] = "tweeter: reworded in a later release."
    reworded_evaluation = evaluate_driver_safety_profile(reworded, topology)
    assert reworded_evaluation.status == "confirmed", reworded_evaluation.reasons
    assert reworded_evaluation.confirmed_and_current is True
    assert reworded_evaluation.profile_fingerprint == profile["profile_fingerprint"]


def test_a_low_limit_warning_message_fits_the_profile_schema_cap() -> None:
    """The warning must survive shape validation to be read at all.

    ``_validate_driver_safety_profile_shape`` caps an issue's fields, and a
    message over its cap lands the WHOLE profile ``malformed``, which makes
    ``build_driver_safety_profile`` refuse the save -- so an overrun does not
    degrade a disclosure, it REFUSES the out-of-band declaration the disclosure
    exists to permit.

    ``driver_style`` is FREE-FORM up to 80 characters (``output_topology``
    accepts any safe id), so this grids an UNREGISTERED maximum-length style,
    not just the longest registered one, and an absurd hand-edited cutoff --
    the two operator-reachable inputs that made the rendered length unbounded.

    The caps come from the module's own constant rather than being restated
    here, so raising or lowering one there cannot leave this test measuring a
    bound that is gone.
    """

    from jasper.active_speaker.driver_protection import driver_style_is_registered
    from jasper.active_speaker.driver_safety import (
        PROFILE_ISSUE_FIELD_MAX_CHARS as caps,
        _target_low_limit_warnings,
    )

    registered = [
        style for style, _ in _TWEETER_STYLE_FLOORS
        if driver_style_is_registered(style)
    ]
    assert registered, _TWEETER_STYLE_FLOORS
    # 80 chars of safe-id, which POST /output-topology accepts verbatim.
    unregistered_max = "a" + "b" * 79
    assert len(unregistered_max) == 80
    # Every shape that reaches the LONGER unknown-style branch: the sentinel
    # `_profile_core` stamps for a box whose type nobody set, a typo, an
    # oversized custom value, and the (unstorable) empty shape.
    unregistered = ["unspecified", "compresion_driver", unregistered_max, ""]
    assert not any(driver_style_is_registered(s) for s in unregistered)

    # The reviewers' worst `:g` renders -- 11 and 12 characters, against the
    # 3 an ordinary declaration spends. Both branches are gridded against them,
    # because the unknown-style branch carries the longer closing clause and is
    # now reachable from a saved profile.
    extreme_hz = (5e-324, 0.000123456, 1e-6, 1.5, 200.0, 999999.5, 1.79769e308)
    for style in [*registered, *unregistered]:
        for cutoff_hz in extreme_hz:
            target = {
                "role": "tweeter",
                "required_protection_filters": [
                    {"kind": "highpass", "cutoff_hz": cutoff_hz},
                ],
            }
            if style:
                target["driver_style"] = style
            warnings = _target_low_limit_warnings(target)
            assert warnings, (style, cutoff_hz)
            for field, cap in caps.items():
                assert len(warnings[0][field]) <= cap, (
                    field, style, cutoff_hz, len(warnings[0][field]),
                )
            # The closing instruction survives on BOTH branches, at every
            # render width. This is what the length budget is FOR -- a message
            # that fits by losing its last clause passes a cap check and still
            # fails the household.
            assert warnings[0]["message"].endswith(
                ("is right.", "cautious default.")
            ), (style, cutoff_hz, warnings[0]["message"])

    # ...and the fit is bought by ellipsizing the operator's free text, never by
    # eating the guidance. On every REGISTERED style at a realistic cutoff the
    # message renders whole: the style appears verbatim and the closing
    # instruction survives. A copy change that pushed the ordinary case into the
    # backstop would fail here rather than silently shipping a cut sentence.
    for style in registered:
        warnings = _target_low_limit_warnings({
            "role": "tweeter",
            "driver_style": style,
            "required_protection_filters": [
                {"kind": "highpass", "cutoff_hz": 1.5},
            ],
        })
        message = warnings[0]["message"]
        assert style in message, (style, message)
        assert message.endswith("is right."), message
        assert "..." not in message, message

    # ...and on the 80-character style the cut lands on the STYLE, not on the
    # sentence. Both halves matter: an ellipsis proves the free text was
    # shortened, and the surviving closing instruction proves the guidance was
    # not what got eaten. Clamping only at the end -- letting the final
    # cap-fit truncate the tail -- would keep the save working and still lose
    # the one clause that tells the household what to check.
    long_style_warning = _target_low_limit_warnings({
        "role": "tweeter",
        "driver_style": unregistered_max,
        "required_protection_filters": [
            {"kind": "highpass", "cutoff_hz": 1.5},
        ],
    })[0]["message"]
    assert "..." in long_style_warning, long_style_warning
    # The LONGER of the two tails, because an 80-character custom value is not
    # a style the table describes -- so this is the worst case for both terms
    # at once, and the instruction still survives.
    assert long_style_warning.endswith("cautious default."), long_style_warning
    assert unregistered_max not in long_style_warning, long_style_warning

    # The surface this actually broke: the SAVE. An 80-character style plus an
    # out-of-band declaration used to render a 334-char message, fail shape
    # validation, and make the builder raise "incoherent artifact" -- refusing
    # the declaration outright, and ONLY when it was out of band (the in-band
    # save on the same topology succeeded). Both must now save.
    raw = mono_output_topology(card_id=None).to_dict()
    raw["speaker_groups"][0]["channels"][1]["driver_style"] = unregistered_max
    long_style_topology = OutputTopology.from_mapping(raw)
    assert (
        long_style_topology.speaker_groups[0].channels[1].driver_style
        == unregistered_max
    ), "the topology must still accept the style this test is about"

    out_of_band = _manual_settings()
    tweeter = out_of_band["drivers"][1]
    tweeter["recommended_highpass_hz"] = 200.0
    tweeter["hard_excitation_band_hz"] = [200.0, 22000.0]
    tweeter["measurement_band_hz"] = [200.0, 20000.0]
    tweeter["required_protection_filters"][0]["cutoff_hz"] = 200.0

    for manual, expected_warning in ((out_of_band, True), (_manual_settings(), False)):
        profile = build_driver_safety_profile(
            long_style_topology,
            manual_settings=manual,
            driver_research=None,
            saved_at="2026-08-22T12:00:00Z",
        )
        assert profile["status"] == "confirmed"
        warned = any(
            issue["code"] == "tweeter:low_limit_implausible_for_style"
            for issue in profile["issues"]
        )
        assert warned is expected_warning, profile["issues"]
        assert evaluate_driver_safety_profile(
            profile, long_style_topology
        ).confirmed_and_current is True


def test_an_unknown_driver_type_is_disclosed_on_the_saved_profile() -> None:
    """The cautious-default caveat, pinned through the SHIPPED path.

    Two things are pinned here and neither survives alone.

    REACHABILITY. This builds a real profile from a topology whose tweeter has
    no ``driver_style``, and reads the copy off the SAVED artifact -- because
    that is where the caveat was dead: ``_profile_core`` stamps
    ``"unspecified"`` and the shape validator requires the field non-empty, so
    a branch keyed on an EMPTY style never fired in the product. A box whose
    driver type nobody set shipped the no-caveat sentence, and only the
    unit-test shape (key absent) reached the clause. A direct call to the
    warning builder cannot catch that class of bug; this must go through
    ``build_driver_safety_profile``.

    THE CLAUSE ITSELF. It carries the reason the band is cautious, and it has
    already vanished once -- it lived in the /sound/ page's
    ``SAFETY_RELATIONSHIP_TEXT`` map until that entry was retired with the
    blocker it phrased. Dropping it again passes every other test in this file,
    so it gets its own assertion.
    """

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

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    stored = next(t for t in profile["targets"] if t["role"] == "tweeter")
    assert stored["driver_style"] == "unspecified", (
        "the seam this pins: an unset type is STAMPED, never stored empty"
    )

    warning = next(
        issue for issue in profile["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )
    message = warning["message"]
    assert "set the driver type above" in message, message
    assert "cautious default" in message, message
    # ...and it still names the number and the band it missed.
    assert "200 Hz" in message, message
    assert "1250-20000 Hz" in message, message

    # A declared, REGISTERED type gets the other tail on the same shipped path,
    # so the caveat is a discrimination rather than boilerplate on every save.
    declared_topology = _topology_with_tweeter_style("compression_driver")
    declared_profile = build_driver_safety_profile(
        declared_topology,
        manual_settings=manual,
        driver_research=None,
        saved_at="2026-08-22T12:00:00Z",
    )
    declared_message = next(
        issue for issue in declared_profile["issues"]
        if issue["code"] == "tweeter:low_limit_implausible_for_style"
    )["message"]
    assert "cautious default" not in declared_message, declared_message
    assert declared_message.endswith("is right."), declared_message


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
    undeclared_profile = build_driver_safety_profile(
        undeclared,
        manual_settings=jts3_manual,
        driver_research=None,
        saved_at="2026-07-16T12:00:00Z",
    )
    assert undeclared_profile["status"] == "confirmed"
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
    profile = build_driver_safety_profile(
        declared,
        manual_settings=jts3_manual,
        driver_research=None,
        saved_at="2026-07-16T12:00:00Z",
    )
    assert profile["status"] == "confirmed"
    tweeter_target = next(t for t in profile["targets"] if t["role"] == "tweeter")
    assert tweeter_target["driver_style"] == "compression_driver"
    assert tweeter_target["code_owned_policy"]["min_highpass_hz"] == 2000.0
    evaluation = evaluate_driver_safety_profile(profile, declared)
    assert evaluation.status == "confirmed"
    assert evaluation.confirmed_and_current is True


def _issue_codes(profile: dict) -> set[str]:
    return {issue["code"] for issue in profile["issues"]}




_SOUND_MAIN_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "assets"
    / "sound-profile"
    / "js"
    / "main.js"
)
# ``driver_safety`` emits reason codes two structurally different ways, and a
# code escaping through EITHER of them reaches ``evaluation.reasons`` and
# therefore /sound's copy. A scan that saw one shape would go quiet exactly when
# the hole it exists to catch reopened. So each shape is matched on its own and
# each is required to have contributed: a pattern that silently stops matching
# fails loudly instead of shrinking the derived set.
#
# A third shape, a bare ``return [...]``, existed for ``_search_band_issues``
# alone and went with it when #2870 deleted the crossover search band. Restore
# it here if a checker is ever written that returns its own list again.
_REASON_CODE_SHAPES = {
    # _target_issues' per-target checks.
    "reasons.append": re.compile(r'\breasons\.append\(\s*f?"([^"]+)"'),
    # _profile_core's profile-wide issue and evaluate_driver_safety_profile's
    # re-derivation of it, whose lists are named `issues` / `derived_issues`.
    "issues.append": re.compile(r'\b\w*issues\.append\(\s*f?"([^"]+)"'),
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
    code). A NEW non-missing code with no phrase -- introduced through EITHER of
    the emission shapes ``_REASON_CODE_SHAPES`` enumerates -- would
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
        saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
            saved_at="2026-07-13T12:00:00Z",
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
            saved_at="2026-07-13T12:00:00Z",
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
        saved_at="2026-07-13T12:00:00Z",
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
            saved_at="2026-07-13T12:00:00Z",
        )

    unknown = _manual_settings()
    unknown["drivers"][0]["safe_because_ai_said_so"] = True
    with pytest.raises(DriverSafetyProfileError, match="unknown fields"):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=unknown,
            driver_research=None,
            saved_at="2026-07-13T12:00:00Z",
        )

    candidate_unknown = _manual_settings()
    candidate_unknown["crossover_candidates"] = [{"typo": True}]
    with pytest.raises(DriverSafetyProfileError, match="unknown fields"):
        build_driver_safety_profile(
            mono_output_topology(card_id=None),
            manual_settings=candidate_unknown,
            driver_research=None,
            saved_at="2026-07-13T12:00:00Z",
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

    ``driver_class``/``radiating_diameter_mm``/``pad`` must be accepted by
    every gate that re-validates the same driver record: the two save-path
    allowlists (whose drift 500s a save), the AI-research paste-back
    allowlist, and the research staleness-comparison set.  One superset
    assertion per copy so the next field lands in all four or fails loudly
    here.
    """
    from jasper.active_speaker import design_draft as dd
    from jasper.active_speaker import driver_safety as ds

    new_fields = {"driver_class", "radiating_diameter_mm", "pad"}
    assert new_fields <= set(dd._MANUAL_DRIVER_FIELDS)
    assert new_fields <= set(ds._MANUAL_DRIVER_FIELDS)
    # pad is deliberately NOT researchable; the research gates carry the
    # two researchable fields only.
    researchable = new_fields - {"pad"}
    assert researchable <= set(ds._V2_RESEARCH_DRIVER_FIELDS)
    assert researchable <= set(dd._V2_RESEARCH_COMPARABLE_FIELDS)


def test_retired_driver_fields_are_gone_from_every_schema_copy():
    """The #1665 drift guard, in the deletion direction (#2872).

    ``horn_coverage_deg`` was a fourth component-entry field.  Deleting it
    from three of the four copies and forgetting the fourth would leave a
    schema that still accepts and stores a value nothing reads — the exact
    state the deletion was for.  A retired key survives in one place only: the
    named legacy set the gates tolerate and the normalisers drop.

    Driven off ``LEGACY_DROPPED_DRIVER_FIELDS`` rather than a hard-coded name,
    because that set is append-only: the next key retired the same way
    inherits this coverage instead of needing someone to remember to add it.
    """
    from jasper.active_speaker import design_draft as dd
    from jasper.active_speaker import driver_safety as ds
    from jasper.active_speaker._common import LEGACY_DROPPED_DRIVER_FIELDS

    # A vacuous pass over an empty set would assert nothing at all.
    assert LEGACY_DROPPED_DRIVER_FIELDS
    assert "horn_coverage_deg" in LEGACY_DROPPED_DRIVER_FIELDS
    for schema in (
        dd._MANUAL_DRIVER_FIELDS,
        ds._MANUAL_DRIVER_FIELDS,
        ds._V2_RESEARCH_DRIVER_FIELDS,
        dd._V2_RESEARCH_COMPARABLE_FIELDS,
    ):
        assert not (LEGACY_DROPPED_DRIVER_FIELDS & set(schema))


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
    profile = build_driver_safety_profile(
        topology, manual_settings=manual, driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    assert profile["issues"] == [], (
        f"the worked example is refused for driver_style={style}: "
        f"{[issue['code'] for issue in profile['issues']]}"
    )
    # "no blockers" and "actually freezable" are different claims, and the
    # template has to satisfy the second one too -- which a save now settles in
    # the same step.
    assert profile["status"] == "confirmed"

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
    # the 2026-08-23 ruling -- and the profile is confirmed anyway, which is
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
    """``source`` is optional and absent means absent, never ``None``.

    A stored safety profile is re-normalised and compared as canonical JSON
    (``_validate_driver_safety_profile_shape``).  If a pre-#2195 provenance
    entry gained a ``"source": null`` key on the way through, every already-
    confirmed profile on a deployed box would read back as noncanonical and
    lose its confirmation.  This pins the omission.
    """

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
    too_long = {
        "level_duration_limits": {
            "confidence": "low",
            "basis": "estimated",
            "source": "x" * (MAX_PROVENANCE_SOURCE_CHARS + 1),
        }
    }
    with pytest.raises(DriverSafetyProfileError) as excinfo:
        _normalise_field_provenance(too_long, "driver.field_provenance")
    assert "level_duration_limits.source" in str(excinfo.value)

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
    """A saved policy echo is never read back as current policy.

    Same contract as ``driver_safety_profile_evaluation``: the value on disk
    was right for the code and topology in force when it was written, and both
    move underneath it.  A stale ``max_auto_level_dbfs`` here would mislabel the
    delegation sentinel on the /sound/ echo-back panel.

    Scoped to a topology-supplied load on purpose -- that is the only kind that
    can re-derive anything, and it is what the /sound/ endpoint always does.
    ``load_design_draft`` with no topology returns the disk copy untouched.
    """

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

    # Poison the persisted copy the way a policy change would — including with
    # a field the view no longer emits, which is exactly what every draft
    # written before the -35 dBFS ceiling was retired carries on disk today.
    raw = json.loads(path.read_text())
    raw["driver_protection_policy_view"]["hf_measurement_abs_ceiling_dbfs"] = -99.0
    raw["driver_protection_policy_view"]["targets"] = []
    path.write_text(json.dumps(raw))

    loaded = load_design_draft(path, topology=topology)
    assert loaded["driver_protection_policy_view"] == driver_protection_policy_view(
        topology, loaded["manual_settings"]
    )

    # The name is load-bearing: excitation_safety_plan already hashes a
    # DIFFERENT shape under `driver_protection_policy` inside the protection-
    # requirement fingerprint, so the draft key must not collide with it.
    assert "driver_protection_policy" not in saved


def test_provenance_has_no_second_writer_for_published_versus_estimated() -> None:
    """``state`` is derived from ``confidence``, never a second stored fact.

    The #2195 ruling lists ``state`` among the facts a value carries, and in
    the same breath defines it as a *mapping* of the existing vocabulary
    (``low -> estimated``, ``medium/high -> confirmed``).  Deriving it is
    therefore what the ruling describes; storing it as well would give one
    fact two writers that can disagree, and the reply is the untrusted side.
    ``confidence`` stays the single writer and the badge is derived at display
    time, so this key is refused by name.
    """

    from jasper.active_speaker.driver_safety import _normalise_field_provenance

    with pytest.raises(DriverSafetyProfileError) as excinfo:
        _normalise_field_provenance(
            {
                "level_duration_limits": {
                    "confidence": "high",
                    "basis": "datasheet",
                    "state": "estimated",
                }
            },
            "driver.field_provenance",
        )
    assert "state" in str(excinfo.value)


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


def test_protection_filter_without_numbers_is_refused_by_name() -> None:
    """"Required, numbers unpublished" is unstorable — and says so (#2186 leg 2).

    The old message named the two missing keys but not the fix, and the
    browser dropped the whole packet before the operator ever saw it.  Under
    the best-estimate contract (#2195) the honest answer to an unpublished
    protective cutoff is the researcher's best estimate, so the refusal says
    that out loud.
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
    assert "best engineering estimate, not null" in message
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
    assert "best engineering estimate, not null" in message


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
        created_at="2026-08-06T12:00:00Z",
    )
    profile = draft["driver_safety_profile"]
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
    undeclared, sensitivities = _cx120_confirmed_profile(tweeter_peak_dbfs=None)
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
        profile, sens = _cx120_confirmed_profile(tweeter_peak_dbfs=declared)
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

    profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    codes = [issue["code"] for issue in profile["issues"]]
    assert expected_code in codes, codes
    assert profile["status"] == "incomplete"

    # Nothing was rewritten behind the operator's back: the refused value is
    # still exactly what was entered.
    tweeter = next(t for t in profile["targets"] if t["role"] == "tweeter")
    if field == "required_protection_filters":
        assert tweeter["required_protection_filters"][0]["cutoff_hz"] == 700.0

    # And it never reads as usable: the save is allowed (the operator keeps
    # their work), the VERDICT is not.
    assert (
        evaluate_driver_safety_profile(profile, topology).confirmed_and_current
        is False
    )
