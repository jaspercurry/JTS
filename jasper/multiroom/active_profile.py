# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Relocate the accepted speaker graph onto the grouping input."""

from pathlib import Path
from typing import Any, Callable

import yaml

from jasper.active_speaker import baseline_profile, crossover_preview, design_draft, measurement
from jasper.active_speaker.playback_route import resolve_active_playback_device
from jasper.active_speaker.runtime_contract import ACTIVE_DRIVER_DOMAIN_SOURCE
from jasper.atomic_io import atomic_write_text
from jasper.camilla_config_contract import DRIVER_DOMAIN_PAIR_TRIM_FILTER
from jasper.camilla_emit import CHANNEL_SELECT_MIXER, emit_channel_select_mixer
from jasper.dsp_apply import CamillaConfigValidationResult, validate_camilla_config
from jasper.output_topology import OutputTopology

from .grouping_ring import GROUPING_RING_FORMAT, GROUPING_RING_PCM


def build_grouped_profile(
    topology: OutputTopology, *, state_path: str, config_path: str,
    program_channel: str, trim_db: float,
    validate: Callable[[str | Path], CamillaConfigValidationResult] | None = None,
) -> dict[str, Any]:
    applied = baseline_profile.load_applied_baseline_profile_state()
    if applied is None:
        draft = design_draft.load_design_draft()
        return baseline_profile.build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=crossover_preview.load_crossover_preview(current_design_draft=draft),
            measurements=measurement.load_measurement_state(topology),
            write=True,
            state_path=state_path,
            config_path=config_path,
            capture_device=GROUPING_RING_PCM,
            capture_format=GROUPING_RING_FORMAT,
            driver_domain=True,
            program_channel=program_channel,
            driver_domain_pair_trim_db=max(0.0, -float(trim_db)),
            validate=validate or validate_camilla_config,
        )

    text, issues = baseline_profile.recompose_applied_baseline_yaml(
        topology, applied_profile=applied,
        playback_device=resolve_active_playback_device(topology)[0],
    )
    result: dict[str, Any] = {
        "status": "blocked",
        "permissions": {"may_apply": False},
        "issues": issues,
        "recomposition_snapshot": applied.get("recomposition_snapshot"),
    }
    if text is None:
        return result
    # Remove when the bond delivers canonical volume to each output endpoint.
    if baseline_profile.applied_bass_extension(applied):
        issues.append({
            "severity": "blocker",
            "code": "grouping_dynamic_bass_volume_unsupported",
            "message": "Pairing does not yet share the volume setting needed for dynamic bass extension.",
        })
        return result

    graph = yaml.safe_load(text)
    # The current driver-domain verifier admits only channel selection and pair
    # trim before the split. Never drop a saved blend or boost allowance to fit
    # that contract; remove this refusal when it can prove those stages too.
    prefix = []
    for step in graph["pipeline"]:
        if step["type"] == "Mixer":
            break
        prefix.extend(step.get("names", []))
    headroom = graph["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    if prefix != ["active_baseline_headroom"] or headroom != 0.0:
        issues.append({
            "severity": "blocker",
            "code": "grouping_applied_prefix_unsupported",
            "message": "The saved tune needs a blend or headroom stage that pairing cannot preserve yet.",
        })
        return result

    graph["devices"]["capture"].update(
        device=GROUPING_RING_PCM, format=GROUPING_RING_FORMAT,
    )
    graph["filters"].pop("active_baseline_headroom")
    graph["filters"][DRIVER_DOMAIN_PAIR_TRIM_FILTER] = {
        "type": "Gain",
        "parameters": {"gain": min(0.0, float(trim_db)), "inverted": False, "mute": False},
    }
    graph["mixers"].update(yaml.safe_load(emit_channel_select_mixer(program_channel)))
    graph["pipeline"][:1] = [
        {"type": "Mixer", "name": CHANNEL_SELECT_MIXER},
        {"type": "Filter", "channels": [0, 1], "names": [DRIVER_DOMAIN_PAIR_TRIM_FILTER]},
    ]
    atomic_write_text(
        Path(config_path),
        f"# Source: {ACTIVE_DRIVER_DOMAIN_SOURCE}\n" + yaml.safe_dump(graph, sort_keys=False),
        mode=0o640,
    )
    validation = (validate or validate_camilla_config)(config_path)
    if validation.status == "valid":
        result["status"] = "ready_to_apply"
        result["permissions"]["may_apply"] = True
    else:
        issues.append({
            "severity": "blocker", "code": "grouping_applied_config_invalid",
            "message": "CamillaDSP refused the paired speaker configuration.",
        })
    return result
