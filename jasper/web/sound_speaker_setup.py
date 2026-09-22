# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The speaker page's read model and draft operations; audio has its own apply door."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Mapping, TypedDict

from jasper.active_speaker import commissioning_coordinator, design_draft
from jasper.active_speaker.design_inputs import resolve_design_inputs
from jasper.active_speaker.driver_pad import PAD_KINDS
from jasper.active_speaker.driver_safety import (
    build_driver_research_context, SUPPORTED_ENCLOSURE_KINDS, DRIVER_RESEARCH_RESULT_SCHEMA_VERSION,
)
from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
from jasper.active_speaker.installation import INSTALLATION_FIELDS
from jasper.active_speaker.level_trim import declared_driver_gains
from jasper.active_speaker.measurement_programs import program_entries
from jasper.output_topology import load_output_topology
from jasper.active_speaker.layout import build_speaker_layout, layout_choices


class SpeakerSetupView(TypedDict):
    stage: str
    layout: dict[str, Any]
    draft: dict[str, Any]
    base_preview: dict[str, Any]
    applied: dict[str, Any]
    next_action: dict[str, Any]
    programs: list[dict[str, Any]]
    issues: list[dict[str, Any]]


DRIVER_FIELDS = {
    "nominal_impedance_ohm": "Nominal impedance (ohms)",
    "sensitivity_db_2v83_1m": "Sensitivity (dB at 2.83 V / 1 m)",
    "recommended_highpass_hz": "Minimum crossover (Hz)",
    "recommended_highpass_slope_db_per_octave": "Minimum slope (dB/octave)",
    "recommended_lowpass_hz": "Maximum crossover (Hz)",
    "gain_offset_db": "Custom level trim (dB)",
}
DRIVER_STYLES = (
    ("dome_tweeter", "Dome tweeter"), ("compression_driver", "Compression driver"),
    ("amt_tweeter", "AMT tweeter"), ("ribbon_tweeter", "Ribbon tweeter"),
    ("planar_tweeter", "Planar tweeter"), ("supertweeter", "Supertweeter"),
)


def _label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def layout_view(topology) -> dict[str, Any]:
    outputs = topology.hardware.to_dict().get("outputs") or []
    return {"topology": topology.to_dict(), "choices": layout_choices(topology),
            "outputs": [{"value": output["index"], "label": output.get("human_label") or f"Output {output['index'] + 1}"}
                        for output in outputs] or
                       [{"value": index, "label": f"Output {index + 1}"}
                        for index in range(topology.hardware.physical_output_count)],
            "driver_styles": [{"value": key, "label": label} for key, label in DRIVER_STYLES]}


def preview_layout(raw: Mapping[str, Any]) -> dict[str, Any]:
    return layout_view(build_speaker_layout(load_output_topology(), raw))


def load_setup_view() -> SpeakerSetupView:
    from jasper.active_speaker.baseline_profile import applied_layers, load_applied_baseline_profile_state  # lazy: graph domain
    from jasper.active_speaker.crossover_preview import build_crossover_preview  # lazy: graph domain

    topology = load_output_topology()
    draft = design_draft.load_design_draft(topology=topology)
    resolved = resolve_design_inputs(topology, draft.get("manual_settings"), draft.get("driver_research"))
    manual = resolve_design_inputs(topology, draft.get("manual_settings"), None)
    if "ambiguous" in manual["bindings"].values():
        manual = dict(draft.get("manual_settings") or {})
    manual.pop("bindings", None)
    coordinator = commissioning_coordinator.load_commissioning_view(topology)
    preview = build_crossover_preview(draft)
    applied = coordinator["applied_profile"]
    layers = applied_layers(load_applied_baseline_profile_state())
    models = (draft.get("operator_inputs") or {}).get("target_models") or {}
    facts = {driver["target_id"]: driver for driver in resolved["drivers"]}
    targets = []
    for group in topology.speaker_groups:
        rear = any(channel.output_variant == "rear" for channel in group.channels)
        for channel in group.channels:
            target_id = channel.target_id(group.id)
            values = facts.get(target_id, {})
            name = ("Rear woofer" if channel.output_variant == "rear" else "Front woofer") if rear and channel.role == "woofer" else _label(channel.role)
            targets.append({"target_id": target_id, "role": channel.role,
                            "label": f"{group.label} · {name}", "driver_style": channel.driver_style,
                            "model": models.get(target_id) or values.get("model") or
                                     (draft.get("operator_inputs") or {}).get(channel.role, ""),
                            "values": values})
    has_models = bool(targets) and all(target["model"] for target in targets)
    passive = "speaker" not in allowed
    stage = ("layout" if not topology.speaker_groups else "tune" if applied["stands"] or passive else
             "details" if not has_models else "apply" if coordinator["driver_values"]["complete"] or draft.get("driver_research") or manual.get("crossover_candidates") else "research")
    action = {"layout": ("save_layout", "Save layout"), "details": ("save_details", "Save details"),
              "research": ("copy_research", "Copy prompt"), "apply": ("apply", "Save to speaker"),
              "tune": ("tune", "Driver linearization" if not passive else "Tune the speaker")}[stage]
    prompt = ""
    if has_models:
        context = build_driver_research_context(topology, {**(draft.get("operator_inputs") or {}),
                    "target_models": {target["target_id"]: target["model"] for target in targets}}, draft.get("manual_settings"))
        prompt = build_driver_research_prompt(context)
    drivers = preview.get("drivers") or {}
    gains, provenance, _, _ = declared_driver_gains(tuple(drivers), drivers)
    return {
        "stage": stage, "layout": layout_view(topology),
        "draft": {"operator_inputs": draft.get("operator_inputs") or {},
                  "manual_settings": manual,
                  "targets": targets, "resolved": resolved, "prompt": prompt,
                  "driver_fields": DRIVER_FIELDS, "installation_fields": INSTALLATION_FIELDS,
                  "enclosures": [{"value": kind, "label": _label(kind)} for kind in sorted(SUPPORTED_ENCLOSURE_KINDS)],
                  "pads": [{"value": kind, "label": "No resistor or L-pad" if kind == "none" else _label(kind)} for kind in PAD_KINDS]},
        "base_preview": {"crossovers": [crossover for group in preview.get("groups", []) for crossover in group["crossovers"]],
                         "trims": [{"role": role, "gain_db": gain, "source": "Custom" if provenance.get(role) == "operator_pinned" else "Estimated"}
                                   for role, gain in gains.items()],
                         "rear_muted": "rear" in allowed},
        "applied": {**applied, "layers": layers}, "next_action": {"id": action[0], "label": action[1]},
        "programs": [{**entry, "applied": layers[entry["id"]]} for entry in program_entries(topology)],
        "issues": list(coordinator["review"]["issues"]) if stage == "apply" else [],
    }


def save_details(raw: Mapping[str, Any]) -> None:
    topology = load_output_topology()
    prior = design_draft.load_design_draft(topology=topology)
    inputs = design_draft.normalise_operator_inputs(raw.get("operator_inputs"))
    def models(values):
        return {(group.id, channel.role, channel.output_variant):
                (values.get("target_models") or {}).get(channel.target_id(group.id)) or values.get(channel.role)
                for group in topology.speaker_groups for channel in group.channels}
    research = prior.get("driver_research") if models(inputs) == models(prior.get("operator_inputs") or {}) else None
    design_draft.save_design_draft(topology, driver_research=research,
                                  manual_settings=raw.get("manual_settings"), operator_inputs=raw.get("operator_inputs"))


def import_research(raw: Mapping[str, Any]) -> None:
    text = raw.get("text")
    if not isinstance(text, str):
        raise ValueError("Paste the research result first.")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    try:
        research = json.loads(fence[1] if fence else text)
    except json.JSONDecodeError as exc:
        raise ValueError("The result is not valid JSON. Paste the complete result and try again.") from exc
    if not isinstance(research, dict) or research.get("artifact_schema_version") != DRIVER_RESEARCH_RESULT_SCHEMA_VERSION:
        raise ValueError("Use the current research prompt so the result matches each driver in this speaker.")
    topology = load_output_topology()
    prior = design_draft.load_design_draft(topology=topology)
    design_draft.save_design_draft(topology, driver_research=research,
                                  manual_settings=prior.get("manual_settings"), operator_inputs=prior.get("operator_inputs"))


def update_setup(path: str, raw: Mapping[str, Any], *, camilla_factory) -> dict[str, Any]:
    from .sound_active_speaker import (  # lazy: HTTP adapters share existing operation owners
        _save_output_topology_payload, _reset_output_topology_payload,
        _active_speaker_finish_commissioning_payload,
    )

    result: dict[str, Any] = {}
    if path == "/setup/layout":
        return {"layout": preview_layout(raw)}
    if path == "/setup/save-layout":
        result = _save_output_topology_payload(dict(raw))
    elif path == "/setup/details":
        save_details(raw)
    elif path == "/setup/research":
        import_research(raw)
    elif path == "/setup/apply":
        from jasper.active_speaker.candidate_parts import candidate_from_design_draft  # lazy: graph compilation

        topology = load_output_topology()
        candidate = candidate_from_design_draft(topology, design_draft.load_design_draft(topology=topology))
        result = asyncio.run(_active_speaker_finish_commissioning_payload(candidate=candidate, camilla_factory=camilla_factory))
    elif path == "/setup/reset":
        result = _reset_output_topology_payload(raw)
    else:
        raise ValueError("Unknown setup operation")
    return {"result": result, "setup": load_setup_view()}
