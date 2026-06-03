"""Stage protected active-speaker startup configs without loading hardware.

This module turns a saved physical output topology plus a designer-authored
active-speaker preset into a muted/protected CamillaDSP startup candidate. It
does not talk to CamillaDSP, does not reload a config, and does not emit sound.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from jasper.dsp_apply import CamillaConfigValidationResult, validate_camilla_config
from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

from .camilla_yaml import (
    PROTECTIVE_TWEETER_HP_MULTIPLIER,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    emit_active_speaker_startup_config,
)
from .environment import classify_camilla_config_text
from .profile import (
    ActiveChannelMap,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    OutputChannel,
    required_driver_roles,
)
from .tone_plan import load_active_speaker_preset

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STAGED_STARTUP_CONFIG_KIND = "jts_active_speaker_staged_startup_config"
DEFAULT_STAGED_CONFIG_NAME = "active_speaker_staged_startup.yml"
DEFAULT_STAGED_METADATA_PATH = Path("/var/lib/jasper/active_speaker_staged_config.json")
DEFAULT_CAMILLA_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
STAGED_CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH"
STAGED_METADATA_PATH_ENV = "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH"
ACTIVE_PLAYBACK_DEVICE_ENV = "JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE"

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _gate(
    gate_id: str,
    *,
    label: str,
    passed: bool,
    message: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


def _safe_stem(value: str) -> str:
    token = _SAFE_STEM_RE.sub("_", str(value or "").strip()).strip("_")
    return token[:80] or "active_speaker"


def staged_metadata_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get(STAGED_METADATA_PATH_ENV)
        or DEFAULT_STAGED_METADATA_PATH
    )


def staged_config_path(
    *,
    config_dir: str | Path | None = None,
    path: str | Path | None = None,
) -> Path:
    explicit = path or os.environ.get(STAGED_CONFIG_PATH_ENV)
    if explicit:
        return Path(explicit)
    return Path(config_dir or DEFAULT_CAMILLA_CONFIG_DIR) / DEFAULT_STAGED_CONFIG_NAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_name = handle.name
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp_name, 0o640)
    os.replace(tmp_name, path)


def load_staged_startup_config(
    *,
    metadata_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the latest staged-config evidence, failing soft when absent."""

    path = staged_metadata_path(metadata_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": STAGED_STARTUP_CONFIG_KIND,
            "status": "not_staged",
            "metadata_path": str(path),
            "config": None,
            "issues": [],
            "next_step": "Stage a protected startup config from the saved output setup.",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": STAGED_STARTUP_CONFIG_KIND,
            "status": "unreadable",
            "metadata_path": str(path),
            "config": None,
            "issues": [
                _issue(
                    "blocker",
                    "staged_config_metadata_unreadable",
                    f"could not read staged active-speaker metadata: {type(exc).__name__}",
                )
            ],
            "next_step": "Stage a fresh protected startup config.",
        }
    return payload if isinstance(payload, dict) else {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STAGED_STARTUP_CONFIG_KIND,
        "status": "unreadable",
        "metadata_path": str(path),
        "config": None,
        "issues": [
            _issue(
                "blocker",
                "staged_config_metadata_not_object",
                "staged active-speaker metadata is not a JSON object",
            )
        ],
        "next_step": "Stage a fresh protected startup config.",
    }


def _mono_active_group(topology: OutputTopology) -> SpeakerGroup | None:
    groups = [
        group
        for group in topology.speaker_groups
        if group.kind == "mono" and group.mode == "active_2_way"
    ]
    return groups[0] if len(groups) == 1 else None


def _channels_by_role(group: SpeakerGroup | None) -> dict[str, SpeakerChannel]:
    if group is None:
        return {}
    return {channel.role: channel for channel in group.channels}


def _resolve_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None,
) -> tuple[str | None, str]:
    explicit = playback_device or os.environ.get(ACTIVE_PLAYBACK_DEVICE_ENV)
    if explicit and explicit.strip():
        return explicit.strip(), "explicit"
    if topology.hardware.card_id:
        return f"hw:{topology.hardware.card_id},0", "topology_hardware"
    return None, "missing"


def _protective_hp_hz(preset: ActiveSpeakerPreset) -> float | None:
    fc_values = [
        region.fc_hz
        for region in preset.crossover_regions
        if region.upper_driver == "tweeter"
    ]
    if not fc_values:
        return None
    return max(fc_values) * PROTECTIVE_TWEETER_HP_MULTIPLIER


def _bind_preset_to_topology(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology,
    group: SpeakerGroup | None,
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    evaluation = topology.evaluation()
    topology_valid = not evaluation.get("blockers")
    gates.append(_gate(
        "topology_valid",
        label="Saved output setup has no backend blockers",
        passed=topology_valid,
        message=(
            "Saved output setup is valid"
            if topology_valid
            else "Resolve saved output setup blockers before staging active DSP"
        ),
    ))
    for issue in evaluation.get("blockers", []):
        if isinstance(issue, dict):
            issues.append({
                "severity": str(issue.get("severity", "blocker")),
                "code": str(issue.get("code", "topology_blocker")),
                "message": str(issue.get("message", "output topology is blocked")),
            })

    preset_shape_ok = preset.way_count == 2 and preset.channel_map.layout == "mono"
    gates.append(_gate(
        "preset_shape",
        label="Preset is a mono active 2-way bring-up profile",
        passed=preset_shape_ok,
        message=(
            "Preset matches the mono active 2-way staging slice"
            if preset_shape_ok
            else "This slice only stages mono active 2-way presets"
        ),
    ))
    if not preset_shape_ok:
        issues.append(_issue(
            "blocker",
            "unsupported_active_preset_shape",
            "protected staging currently supports mono active 2-way presets only",
        ))

    group_present = group is not None
    gates.append(_gate(
        "mono_active_group",
        label="Saved topology has exactly one mono active 2-way speaker group",
        passed=group_present,
        message=(
            "Mono active 2-way group is present"
            if group_present
            else "Create one mono active 2-way output setup first"
        ),
    ))
    if not group_present:
        issues.append(_issue(
            "blocker",
            "mono_active_group_required",
            "stage protected config requires one saved mono active 2-way speaker group",
        ))

    channels = _channels_by_role(group)
    outputs: list[OutputChannel] = []
    roles = required_driver_roles(preset.way_count) if preset_shape_ok else ()
    missing_roles = [role for role in roles if role not in channels]
    if missing_roles:
        issues.append(_issue(
            "blocker",
            "required_driver_role_missing",
            f"saved topology is missing driver roles: {', '.join(missing_roles)}",
        ))
    assigned_roles = [
        role
        for role in roles
        if role in channels and channels[role].physical_output_index is not None
    ]
    gates.append(_gate(
        "physical_outputs_assigned",
        label="Woofer and compression-driver outputs are assigned",
        passed=bool(roles) and len(assigned_roles) == len(roles),
        message=(
            "Required driver outputs are assigned"
            if bool(roles) and len(assigned_roles) == len(roles)
            else "Assign woofer and compression-driver channels to physical DAC outputs"
        ),
    ))

    physical_indexes: list[int] = []
    for role in roles:
        channel = channels.get(role)
        if channel is None or channel.physical_output_index is None:
            continue
        physical_indexes.append(channel.physical_output_index)
        outputs.append(OutputChannel(
            index=channel.physical_output_index,
            side="mono",
            driver_role=role,
            label=channel.human_output_label or f"DAC output {channel.physical_output_index + 1}",
            startup_muted=True,
        ))
    contiguous = sorted(physical_indexes) == list(range(len(physical_indexes)))
    gates.append(_gate(
        "contiguous_low_outputs",
        label="Assigned outputs are contiguous from DAC output 1",
        passed=bool(roles) and len(physical_indexes) == len(roles) and contiguous,
        message=(
            "Assigned outputs map directly to the first active playback channels"
            if bool(roles) and len(physical_indexes) == len(roles) and contiguous
            else "This first staging slice needs woofer on DAC output 1 and high driver on DAC output 2"
        ),
    ))
    if bool(roles) and (len(physical_indexes) != len(roles) or not contiguous):
        issues.append(_issue(
            "blocker",
            "active_outputs_must_be_contiguous",
            "first protected staging slice supports DAC outputs 1 and 2 only",
        ))
    role_output_indexes = {
        role: channels[role].physical_output_index
        for role in roles
        if role in channels and channels[role].physical_output_index is not None
    }
    role_order_ok = (
        bool(roles)
        and len(role_output_indexes) == len(roles)
        and all(role_output_indexes.get(role) == index for index, role in enumerate(roles))
    )
    expected_role_order = ", ".join(
        f"{role} on DAC output {index + 1}"
        for index, role in enumerate(roles)
    )
    gates.append(_gate(
        "active_output_role_order",
        label="Assigned outputs match the protected DSP role order",
        passed=role_order_ok,
        message=(
            "Woofer and compression-driver outputs match the staged DSP order"
            if role_order_ok
            else f"This staging slice requires {expected_role_order}"
        ),
    ))
    if (
        bool(roles)
        and len(role_output_indexes) == len(roles)
        and contiguous
        and not role_order_ok
    ):
        issues.append(_issue(
            "blocker",
            "active_outputs_must_match_role_order",
            f"first protected staging slice requires {expected_role_order}",
        ))

    tweeter = channels.get("tweeter")
    tweeter_protected = bool(
        tweeter
        and tweeter.startup_muted
        and tweeter.protection_required
        and tweeter.protection_status == "present"
    )
    gates.append(_gate(
        "tweeter_protection_present",
        label="Compression-driver protection is marked present",
        passed=tweeter_protected,
        message=(
            "Compression-driver protection is present"
            if tweeter_protected
            else "Mark the F110M/compression-driver protection present before staging"
        ),
    ))
    if not tweeter_protected:
        issues.append(_issue(
            "blocker",
            "tweeter_protection_required",
            "compression-driver protection must be marked present before staging",
        ))

    if issues:
        return None, issues, gates

    return replace(
        preset,
        channel_map=ActiveChannelMap(layout="mono", outputs=tuple(outputs)),
    ), issues, gates


def stage_protected_startup_config(
    topology: OutputTopology,
    *,
    preset: ActiveSpeakerPreset | None = None,
    playback_device: str | None = None,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
    run_config_check: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Stage a muted/protected startup YAML and return versioned evidence."""

    preset = preset or load_active_speaker_preset()
    created_at = created_at or _utc_now()
    group = _mono_active_group(topology)
    bound_preset, issues, gates = _bind_preset_to_topology(preset, topology, group)
    resolved_playback_device, playback_device_source = _resolve_playback_device(
        topology,
        playback_device=playback_device,
    )
    playback_device_ready = bool(resolved_playback_device)
    gates.append(_gate(
        "explicit_active_playback_device",
        label="Active playback device is explicit hardware, not the JTS stereo lane",
        passed=playback_device_ready,
        message=(
            f"Using {resolved_playback_device}"
            if resolved_playback_device
            else f"Set {ACTIVE_PLAYBACK_DEVICE_ENV} or save detected hardware with a card id"
        ),
    ))
    if not playback_device_ready:
        issues.append(_issue(
            "blocker",
            "active_playback_device_required",
            "protected staging requires an explicit active hardware playback device",
        ))

    out_path = staged_config_path(config_dir=config_dir, path=config_path)
    meta_path = staged_metadata_path(metadata_path)
    validation: dict[str, Any] = {"status": "skipped", "reason": "not_generated"}
    classification: dict[str, Any] = {}

    if not issues and bound_preset and resolved_playback_device:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            yaml = emit_active_speaker_startup_config(
                bound_preset,
                playback_device=resolved_playback_device,
                out_path=out_path,
                baseline_id=f"staged-{_safe_stem(topology.topology_id)}",
            )
            classification = classify_camilla_config_text(yaml)
            gates.append(_gate(
                "generated_active_startup_candidate",
                label="Generated config is classified as active-speaker startup",
                passed=classification.get("classification") == "active_startup_candidate",
                message=classification.get("label", "classified generated config"),
            ))
            gates.append(_gate(
                "volume_ceiling_preserved",
                label="CamillaDSP volume ceiling is <= 0 dB",
                passed=bool(classification.get("volume_limit_ok")),
                message=(
                    "Volume ceiling is preserved"
                    if classification.get("volume_limit_ok")
                    else "Generated config did not preserve the volume ceiling"
                ),
            ))
            for issue in classification.get("issues", []):
                if isinstance(issue, dict):
                    issues.append({
                        "severity": str(issue.get("severity", "blocker")),
                        "code": str(issue.get("code", "config_issue")),
                        "message": str(issue.get("message", "generated config issue")),
                    })
            validation = (
                validate(out_path).to_dict()
                if run_config_check
                else {"status": "skipped", "reason": "disabled"}
            )
        except (ActiveSpeakerConfigError, OSError) as exc:
            issues.append(_issue(
                "blocker",
                "staged_config_generation_failed",
                f"could not generate protected startup config: {type(exc).__name__}",
            ))

    validation_status = str(validation.get("status") or "unknown")
    validation_ok = validation_status in {"valid", "missing"}
    if validation_status not in {"skipped", "not_generated"}:
        gates.append(_gate(
            "camilla_syntax_preflight",
            label="Generated config passed CamillaDSP syntax preflight",
            passed=validation_ok,
            message=(
                f"Validation status is {validation_status}"
                if validation_ok
                else "CamillaDSP validation blocked the staged config"
            ),
        ))
    if validation_status not in {"valid", "missing", "skipped", "not_generated"}:
        issues.append(_issue(
            "blocker",
            "staged_config_validation_failed",
            f"CamillaDSP validation status is {validation_status}",
        ))

    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")
    status = "staged" if blocker_count == 0 and out_path.exists() else "blocked"
    target_outputs = []
    if group:
        for channel in sorted(
            group.channels,
            key=lambda item: (item.physical_output_index is None, item.role),
        ):
            target_outputs.append({
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "human_output_label": channel.human_output_label,
                "identity_verified": channel.identity_verified,
                "startup_muted": channel.startup_muted,
                "protection_required": channel.protection_required,
                "protection_status": channel.protection_status,
            })
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STAGED_STARTUP_CONFIG_KIND,
        "status": status,
        "created_at": created_at,
        "metadata_path": str(meta_path),
        "preset": {
            "preset_id": preset.preset_id,
            "name": preset.name,
            "way_count": preset.way_count,
            "layout": preset.channel_map.layout,
        },
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
            "speaker_group_id": group.id if group else None,
            "speaker_label": group.label if group else None,
        },
        "hardware": {
            "device_id": topology.hardware.device_id,
            "device_label": topology.hardware.device_label,
            "card_id": topology.hardware.card_id,
            "physical_output_count": topology.hardware.physical_output_count,
            "clock_domain_id": topology.hardware.clock_domain_id,
        },
        "targets": target_outputs,
        "config": {
            "path": str(out_path),
            "basename": out_path.name,
            "exists": out_path.exists(),
            "playback_device": resolved_playback_device,
            "playback_device_source": playback_device_source,
            "playback_channels": (
                classification.get("playback_channels")
                if classification else None
            ),
            "classification": classification.get("classification"),
            "volume_limit_db": classification.get("volume_limit_db"),
            "volume_limit_ok": classification.get("volume_limit_ok"),
            "startup_headroom_db": STARTUP_HEADROOM_DB,
            "limiter_clip_limit_db": STARTUP_LIMITER_CLIP_LIMIT_DB,
            "tweeter_protective_highpass_hz": _protective_hp_hz(preset),
            "validation": validation,
        },
        "load": {
            "load_allowed": False,
            "load_gate": "not_implemented",
            "next_step": (
                "Loading the protected graph remains a separate lab-gated slice; "
                "this step only stages and validates the candidate."
            ),
        },
        "required_gates": gates,
        "issues": issues,
        "next_step": (
            "Protected startup config staged. Inspect the evidence before any lab load."
            if status == "staged"
            else "Resolve staging blockers before loading or playing active-speaker audio."
        ),
    }
    try:
        _atomic_write_json(meta_path, payload)
    except OSError as exc:
        logger.warning(
            "event=active_speaker.staged_config_metadata_write_failed path=%s error=%s",
            meta_path,
            type(exc).__name__,
        )
    logger.info(
        "event=active_speaker.staged_config status=%s preset_id=%s topology_id=%s "
        "config=%s blockers=%d",
        status,
        preset.preset_id,
        topology.topology_id,
        out_path,
        blocker_count,
    )
    return payload
