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

from ._common import gate as _gate, issue as _issue
from .camilla_yaml import (
    PROTECTIVE_TWEETER_HP_MULTIPLIER,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
    emit_active_speaker_startup_config,
)
from .crossover_preview import CROSSOVER_PREVIEW_KIND
from .environment import classify_camilla_config_text
from .profile import (
    ActiveChannelMap,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    DriverSpec,
    OutputChannel,
    SafetyEnvelope,
    required_driver_roles,
)
from .playback_route import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    active_playback_route_capability,
    resolve_diagnostic_playback_device,
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

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def _channels_by_role(group: SpeakerGroup | None) -> dict[str, SpeakerChannel]:
    if group is None:
        return {}
    return {channel.role: channel for channel in group.channels}


def _software_guard_requested(group: SpeakerGroup | None) -> bool:
    return any(
        channel.role == "tweeter"
        and channel.protection_status == "software_guard_requested"
        for channel in (group.channels if group else ())
    )


def _software_guard_requested_any(groups: list[SpeakerGroup]) -> bool:
    return any(_software_guard_requested(group) for group in groups)


def _active_mode_for_way(way_count: int) -> str:
    return f"active_{way_count}_way"


def _way_count_for_mode(mode: str) -> int | None:
    if mode == "active_2_way":
        return 2
    if mode == "active_3_way":
        return 3
    return None


def _role_pair_key(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    lower, upper = raw
    if not isinstance(lower, str) or not isinstance(upper, str):
        return None
    return lower, upper


def _slope_to_lr_order(raw: Any) -> int | None:
    try:
        slope = float(raw)
    except (TypeError, ValueError):
        return None
    if not (slope > 0):
        return None
    order = int(round(slope / 6.0))
    return order if order in {2, 4, 8} and abs(slope - order * 6.0) < 0.01 else None


def _normalise_filter_type(raw: Any) -> str | None:
    token = str(raw or "").replace("-", "").replace(" ", "").lower()
    if token in {"linkwitzriley", "lr"}:
        return "LinkwitzRiley"
    return None


def _driver_spec_from_preview(role: str, raw: Any) -> DriverSpec:
    driver = raw if isinstance(raw, dict) else {}
    model = str(driver.get("model") or role).strip() or role
    manufacturer = str(driver.get("manufacturer") or "Operator research").strip()
    try:
        sensitivity = driver.get("sensitivity_db_2v83_1m")
        sensitivity_db = float(sensitivity) if sensitivity is not None else None
    except (TypeError, ValueError):
        sensitivity_db = None
    return DriverSpec(
        role=role,
        manufacturer=manufacturer or "Operator research",
        model=model,
        sensitivity_db=sensitivity_db,
    )


def _active_groups_for_preset(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
) -> tuple[list[SpeakerGroup], list[dict[str, str]], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    expected_mode = _active_mode_for_way(preset.way_count)
    active_groups = [
        group for group in topology.speaker_groups if group.mode == expected_mode
    ]
    if preset.channel_map.layout == "mono":
        groups = [group for group in active_groups if group.kind == "mono"]
        ok = len(groups) == 1
        gates.append(_gate(
            "active_layout_groups",
            label="Saved topology has one mono active speaker group",
            passed=ok,
            message=(
                "Mono active speaker group is present"
                if ok
                else f"Create one mono {expected_mode.replace('_', ' ')} output setup first"
            ),
        ))
        if not ok:
            issues.append(_issue(
                "blocker",
                "mono_active_group_required",
                f"stage protected config requires one mono {expected_mode} speaker group",
            ))
        return groups[:1], issues, gates

    if preset.channel_map.layout == "stereo":
        by_kind = {
            group.kind: group
            for group in active_groups
            if group.kind in {"left", "right"}
        }
        ok = set(by_kind) == {"left", "right"} and len(active_groups) == 2
        gates.append(_gate(
            "active_layout_groups",
            label="Saved topology has left and right active speaker groups",
            passed=ok,
            message=(
                "Left and right active speaker groups are present"
                if ok
                else f"Create left and right {expected_mode.replace('_', ' ')} speaker groups first"
            ),
        ))
        if not ok:
            issues.append(_issue(
                "blocker",
                "stereo_active_groups_required",
                f"stage protected config requires left and right {expected_mode} speaker groups",
            ))
        return [by_kind[kind] for kind in ("left", "right") if kind in by_kind], issues, gates

    issues.append(_issue(
        "blocker",
        "unsupported_active_layout",
        f"protected staging does not support {preset.channel_map.layout} layout",
    ))
    gates.append(_gate(
        "active_layout_groups",
        label="Saved active-speaker layout is supported",
        passed=False,
        message=f"Unsupported layout {preset.channel_map.layout}",
    ))
    return [], issues, gates


def _channels_by_side_role(
    groups: list[SpeakerGroup],
) -> dict[tuple[str, str], SpeakerChannel]:
    channels: dict[tuple[str, str], SpeakerChannel] = {}
    for group in groups:
        side = group.kind if group.kind in {"left", "right"} else "mono"
        for channel in group.channels:
            channels[(side, channel.role)] = channel
    return channels


def _target_outputs_for_groups(
    groups: list[SpeakerGroup],
) -> list[dict[str, Any]]:
    target_outputs: list[dict[str, Any]] = []
    for group in groups:
        for channel in sorted(
            group.channels,
            key=lambda item: (
                item.physical_output_index is None,
                item.physical_output_index if item.physical_output_index is not None else 999,
                item.role,
            ),
        ):
            target_outputs.append({
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "speaker_kind": group.kind,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "human_output_label": channel.human_output_label,
                "identity_verified": channel.identity_verified,
                "startup_muted": channel.startup_muted,
                "protection_required": channel.protection_required,
                "protection_status": channel.protection_status,
            })
    return target_outputs


def _subwoofer_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    routed_subwoofers = set(topology.routing.subwoofer_group_ids)
    return [
        group
        for group in topology.speaker_groups
        if (
            group.kind == "subwoofer"
            or group.mode == "subwoofer"
            or group.id in routed_subwoofers
        )
    ]


def _protective_hp_hz(preset: ActiveSpeakerPreset) -> float | None:
    fc_values = [
        region.fc_hz
        for region in preset.crossover_regions
        if region.upper_driver == "tweeter"
    ]
    if not fc_values:
        return None
    return max(fc_values) * PROTECTIVE_TWEETER_HP_MULTIPLIER


def _parse_scalar(value: str) -> Any:
    cleaned = value.split("#", 1)[0].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    if cleaned in {"true", "false"}:
        return cleaned == "true"
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except ValueError:
        return cleaned


def _parse_inline_mapping(value: str) -> dict[str, Any]:
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return {}
    out: dict[str, Any] = {}
    for item in value[1:-1].split(","):
        if ":" not in item:
            continue
        key, raw_value = item.split(":", 1)
        out[key.strip()] = _parse_scalar(raw_value)
    return out


def _parse_inline_list(value: str) -> list[Any]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return []
    return [
        _parse_scalar(item)
        for item in value[1:-1].split(",")
        if item.strip()
    ]


def _top_level_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not line.startswith(" ") and stripped.endswith(":"):
            current = stripped[:-1]
            sections[current] = []
            continue
        if current:
            sections[current].append(line)
    return sections


def _parse_generated_filters(text: str) -> dict[str, dict[str, Any]]:
    filters: dict[str, dict[str, Any]] = {}
    current_name: str | None = None
    in_parameters = False
    for line in _top_level_sections(text).get("filters", []):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 2 and stripped.endswith(":"):
            current_name = stripped[:-1]
            filters[current_name] = {"parameters": {}}
            in_parameters = False
            continue
        if not current_name or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if indent == 4 and key == "type":
            filters[current_name]["type"] = str(_parse_scalar(raw_value))
            in_parameters = False
            continue
        if indent == 4 and key == "parameters":
            filters[current_name]["parameters"].update(_parse_inline_mapping(raw_value))
            in_parameters = True
            continue
        if indent > 4 and in_parameters:
            filters[current_name]["parameters"][key] = _parse_scalar(raw_value)
    return filters


def _parse_generated_pipeline_filters(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _top_level_sections(text).get("pipeline", []):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current:
                items.append(current)
            current = {}
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("["):
            current[key] = _parse_inline_list(raw_value)
        else:
            current[key] = _parse_scalar(raw_value)
    if current:
        items.append(current)
    return [item for item in items if item.get("type") == "Filter"]


def _float_matches(value: Any, expected: float) -> bool:
    try:
        return abs(float(value) - expected) < 0.0001
    except (TypeError, ValueError):
        return False


def _filter_param_matches(
    filters: dict[str, dict[str, Any]],
    name: str,
    *,
    filter_type: str,
    params: dict[str, Any],
) -> bool:
    filter_def = filters.get(name, {})
    if filter_def.get("type") != filter_type:
        return False
    actual = filter_def.get("parameters", {})
    for key, expected in params.items():
        value = actual.get(key)
        if isinstance(expected, float):
            if not _float_matches(value, expected):
                return False
        elif value != expected:
            return False
    return True


def _pipeline_contains_chain(
    pipeline_filters: list[dict[str, Any]],
    *,
    channels: set[int],
    required_names: tuple[str, ...],
) -> bool:
    for item in pipeline_filters:
        item_channels = {
            int(channel)
            for channel in item.get("channels", [])
            if isinstance(channel, int)
        }
        item_names = tuple(str(name) for name in item.get("names", []))
        if item_channels == channels and all(name in item_names for name in required_names):
            return True
    return False


def _software_guard_evidence(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
) -> dict[str, Any]:
    protective_hp_hz = _protective_hp_hz(preset)
    filters = _parse_generated_filters(yaml)
    pipeline_filters = _parse_generated_pipeline_filters(yaml)
    tweeter_channels = {
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == "tweeter"
    }
    checks = {
        "startup_muted": _filter_param_matches(
            filters,
            "as_tweeter_startup_mute",
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB, "mute": True},
        ),
        "protective_highpass": (
            protective_hp_hz is not None
            and _filter_param_matches(
                filters,
                "as_tweeter_protective_hp",
                filter_type="BiquadCombo",
                params={
                    "type": "LinkwitzRileyHighpass",
                    "freq": protective_hp_hz,
                    "order": 4,
                },
            )
        ),
        "startup_headroom": _filter_param_matches(
            filters,
            "active_startup_headroom",
            filter_type="Gain",
            params={"gain": -STARTUP_HEADROOM_DB},
        ),
        "startup_limiter": _filter_param_matches(
            filters,
            "as_tweeter_startup_limiter",
            filter_type="Limiter",
            params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
        ),
        "tweeter_pipeline_guarded": _pipeline_contains_chain(
            pipeline_filters,
            channels=tweeter_channels,
            required_names=(
                "as_tweeter_protective_hp",
                "as_tweeter_startup_mute",
                "as_tweeter_startup_limiter",
            ),
        ),
    }
    return {
        "mode": "software_guard_requested",
        "no_load": True,
        "no_playback": True,
        "protective_highpass_hz": protective_hp_hz,
        "startup_headroom_db": STARTUP_HEADROOM_DB,
        "limiter_clip_limit_db": STARTUP_LIMITER_CLIP_LIMIT_DB,
        "tweeter_channels": sorted(tweeter_channels),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _preset_from_crossover_preview(
    topology: OutputTopology,
    preview: dict[str, Any],
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []

    preview_ready = (
        preview.get("kind") == CROSSOVER_PREVIEW_KIND
        and preview.get("status") == "ready_for_protected_staging"
        and bool(
            (preview.get("permissions") or {}).get(
                "may_prepare_protected_startup_config"
            )
        )
    )
    gates.append(_gate(
        "crossover_preview_ready",
        label="Fresh crossover preview is ready for protected staging",
        passed=preview_ready,
        message=(
            "Crossover preview can feed protected staging"
            if preview_ready
            else "Prepare a fresh ready crossover preview before staging"
        ),
    ))
    if not preview_ready:
        issues.append(_issue(
            "blocker",
            "crossover_preview_not_ready",
            "stage protected config requires a fresh ready crossover preview",
        ))
        return None, issues, gates

    source = preview.get("source") if isinstance(preview.get("source"), dict) else {}
    topology_matches = source.get("topology_id") == topology.topology_id
    gates.append(_gate(
        "crossover_preview_topology_matches",
        label="Crossover preview matches the saved output topology",
        passed=topology_matches,
        message=(
            "Preview topology matches the saved output setup"
            if topology_matches
            else "Prepare a fresh crossover preview for this output setup"
        ),
    ))
    if not topology_matches:
        issues.append(_issue(
            "blocker",
            "crossover_preview_topology_mismatch",
            "saved crossover preview was prepared for a different output topology",
        ))
        return None, issues, gates

    preview_groups = [
        group for group in preview.get("groups", []) if isinstance(group, dict)
    ]
    active_modes = {
        str(group.get("mode"))
        for group in preview_groups
        if _way_count_for_mode(str(group.get("mode"))) is not None
    }
    if len(active_modes) != 1:
        issues.append(_issue(
            "blocker",
            "crossover_preview_single_active_mode_required",
            "protected staging requires one active speaker mode per config",
        ))
        return None, issues, gates
    mode = next(iter(active_modes))
    way_count = _way_count_for_mode(mode)
    if way_count is None:
        issues.append(_issue(
            "blocker",
            "crossover_preview_mode_unsupported",
            f"protected staging does not support {mode}",
        ))
        return None, issues, gates

    kinds = {str(group.get("kind")) for group in preview_groups}
    if kinds == {"mono"} and len(preview_groups) == 1:
        layout = "mono"
    elif kinds == {"left", "right"} and len(preview_groups) == 2:
        layout = "stereo"
    else:
        issues.append(_issue(
            "blocker",
            "crossover_preview_layout_unsupported",
            "protected staging supports one mono speaker or a left/right stereo pair",
        ))
        return None, issues, gates

    roles = required_driver_roles(way_count)
    topology_groups = {
        group.id: group
        for group in topology.speaker_groups
        if group.mode == mode and group.kind in {"mono", "left", "right"}
    }
    outputs: list[OutputChannel] = []
    for preview_group in sorted(
        preview_groups,
        key=lambda item: {"mono": 0, "left": 0, "right": 1}.get(str(item.get("kind")), 9),
    ):
        group_id = str(preview_group.get("group_id") or "")
        group = topology_groups.get(group_id)
        if group is None:
            issues.append(_issue(
                "blocker",
                "crossover_preview_group_missing",
                f"preview group {group_id or '<unknown>'} is not in saved topology",
            ))
            continue
        side = group.kind if group.kind in {"left", "right"} else "mono"
        channels = _channels_by_role(group)
        for role in roles:
            channel = channels.get(role)
            if channel is None or channel.physical_output_index is None:
                issues.append(_issue(
                    "blocker",
                    "crossover_preview_channel_unassigned",
                    f"{group.label} {role} is not assigned to a DAC output",
                ))
                continue
            outputs.append(OutputChannel(
                index=channel.physical_output_index,
                side=side,
                driver_role=role,
                label=(
                    channel.human_output_label
                    or f"DAC output {channel.physical_output_index + 1}"
                ),
                startup_muted=True,
            ))

    crossover_values: dict[tuple[str, str], dict[str, Any]] = {}
    for preview_group in preview_groups:
        for crossover in preview_group.get("crossovers", []):
            if not isinstance(crossover, dict):
                continue
            key = _role_pair_key(crossover.get("between_roles"))
            if key is None:
                continue
            frequency = crossover.get("proposed_frequency_hz")
            filters = [
                item for item in crossover.get("filters", [])
                if isinstance(item, dict)
            ]
            filter_type = filters[0].get("filter_type") if filters else None
            slope = filters[0].get("slope_db_per_octave") if filters else None
            current = {
                "frequency_hz": frequency,
                "filter_type": filter_type,
                "slope_db_per_octave": slope,
            }
            previous = crossover_values.setdefault(key, current)
            if previous != current:
                issues.append(_issue(
                    "blocker",
                    "crossover_preview_stereo_values_differ",
                    f"preview crossover values differ for {key[0]}/{key[1]}",
                ))

    regions: list[CrossoverRegion] = []
    for lower_role, upper_role in (
        (("woofer", "tweeter"),)
        if way_count == 2
        else (("woofer", "mid"), ("mid", "tweeter"))
    ):
        value = crossover_values.get((lower_role, upper_role))
        if value is None:
            issues.append(_issue(
                "blocker",
                "crossover_preview_pair_missing",
                f"preview is missing {lower_role}/{upper_role} crossover",
            ))
            continue
        try:
            frequency = float(value.get("frequency_hz"))
        except (TypeError, ValueError):
            frequency = 0.0
        filter_type = _normalise_filter_type(value.get("filter_type"))
        order = _slope_to_lr_order(value.get("slope_db_per_octave"))
        if frequency <= 0 or filter_type is None or order is None:
            issues.append(_issue(
                "blocker",
                "crossover_preview_filter_unsupported",
                f"preview filter for {lower_role}/{upper_role} is not a supported Linkwitz-Riley slope",
            ))
            continue
        regions.append(CrossoverRegion(
            id=f"{lower_role}_{upper_role}_{int(round(frequency))}hz",
            lower_driver=lower_role,
            upper_driver=upper_role,
            fc_hz=frequency,
            target_type=filter_type,
            order=order,
        ))

    if issues:
        return None, issues, gates

    drivers_raw = preview.get("drivers") if isinstance(preview.get("drivers"), dict) else {}
    try:
        preset = ActiveSpeakerPreset(
            preset_id=f"preview-{_safe_stem(topology.topology_id)}-{way_count}way",
            name=f"{topology.name} preview-derived active {way_count}-way",
            way_count=way_count,
            channel_map=ActiveChannelMap(layout=layout, outputs=tuple(outputs)),
            drivers={
                role: _driver_spec_from_preview(role, drivers_raw.get(role))
                for role in roles
            },
            crossover_regions=tuple(regions),
            safety=SafetyEnvelope(
                initial_sweep_level_db_spl=55.0,
                max_commissioning_level_db_spl=80.0,
                escalation_step_db=1.0,
                require_physical_tweeter_protection=True,
                require_channel_identity_before_drivers=True,
                emergency_stop_required=True,
            ),
            notes="Derived from jts_active_speaker_crossover_preview; review before load.",
        )
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "crossover_preview_preset_invalid",
            f"could not turn crossover preview into an active preset: {exc}",
        ))
        return None, issues, gates

    gates.append(_gate(
        "crossover_preview_compiled",
        label="Crossover preview compiled to protected startup intent",
        passed=True,
        message="Preview-derived crossover can be staged through the protected emitter",
    ))
    return preset, issues, gates


def compile_preset_from_crossover_preview(
    topology: OutputTopology,
    preview: dict[str, Any],
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    """Compile a saved crossover preview into active-speaker preset intent.

    This is the shared no-side-effect bridge used by protected startup staging
    and final baseline candidate compilation. It does not write YAML, load
    CamillaDSP, or authorize playback.
    """

    return _preset_from_crossover_preview(topology, preview)


def _bind_preset_to_topology(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology,
    *,
    allow_mapped_role_order: bool = False,
) -> tuple[
    ActiveSpeakerPreset | None,
    list[dict[str, str]],
    list[dict[str, Any]],
    list[SpeakerGroup],
]:
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    active_groups, group_issues, group_gates = _active_groups_for_preset(topology, preset)
    issues.extend(group_issues)
    gates.extend(group_gates)

    software_guard_requested = _software_guard_requested_any(active_groups)
    evaluation = topology.evaluation()
    topology_blockers = [
        issue for issue in evaluation.get("blockers", [])
        if not (
            software_guard_requested
            and isinstance(issue, dict)
            and issue.get("code") == "tweeter_software_guard_requested"
        )
    ]
    topology_valid = not topology_blockers
    gates.append(_gate(
        "topology_valid",
        label="Saved output setup has no staging blockers",
        passed=topology_valid,
        message=(
            "Saved output setup can be staged for no-load review"
            if topology_valid
            else "Resolve saved output setup blockers before staging active DSP"
        ),
    ))
    for issue in topology_blockers:
        if isinstance(issue, dict):
            issues.append({
                "severity": str(issue.get("severity", "blocker")),
                "code": str(issue.get("code", "topology_blocker")),
                "message": str(issue.get("message", "output topology is blocked")),
            })

    preset_shape_ok = (
        preset.way_count in {2, 3}
        and preset.channel_map.layout in {"mono", "stereo"}
    )
    gates.append(_gate(
        "preset_shape",
        label="Preset shape is supported for protected staging",
        passed=preset_shape_ok,
        message=(
            f"Preset matches {preset.channel_map.layout} active {preset.way_count}-way staging"
            if preset_shape_ok
            else "Protected staging supports mono/stereo active 2-way or 3-way presets"
        ),
    ))
    if not preset_shape_ok:
        issues.append(_issue(
            "blocker",
            "unsupported_active_preset_shape",
            "protected staging supports mono/stereo active 2-way or 3-way presets",
        ))

    outputs: list[OutputChannel] = []
    roles = required_driver_roles(preset.way_count) if preset_shape_ok else ()
    channels_by_slot = _channels_by_side_role(active_groups)
    sides = ("mono",) if preset.channel_map.layout == "mono" else ("left", "right")
    required_slots = [(side, role) for side in sides for role in roles]
    missing_roles = [
        f"{side}/{role}"
        for side, role in required_slots
        if (side, role) not in channels_by_slot
    ]
    if missing_roles:
        issues.append(_issue(
            "blocker",
            "required_driver_role_missing",
            f"saved topology is missing driver roles: {', '.join(missing_roles)}",
        ))
    assigned_roles = [
        f"{side}/{role}"
        for side, role in required_slots
        if (
            (side, role) in channels_by_slot
            and channels_by_slot[(side, role)].physical_output_index is not None
        )
    ]
    gates.append(_gate(
        "physical_outputs_assigned",
        label="Required active-driver outputs are assigned",
        passed=bool(required_slots) and len(assigned_roles) == len(required_slots),
        message=(
            "Required driver outputs are assigned"
            if bool(required_slots) and len(assigned_roles) == len(required_slots)
            else "Assign every active driver channel to a physical DAC output"
        ),
    ))

    physical_indexes: list[int] = []
    for side, role in required_slots:
        channel = channels_by_slot.get((side, role))
        if channel is None or channel.physical_output_index is None:
            continue
        physical_indexes.append(channel.physical_output_index)
        outputs.append(OutputChannel(
            index=channel.physical_output_index,
            side=side,
            driver_role=role,
            label=channel.human_output_label or f"DAC output {channel.physical_output_index + 1}",
            startup_muted=True,
        ))
    expected_count = len(required_slots)
    contiguous = sorted(physical_indexes) == list(range(expected_count))
    gates.append(_gate(
        "contiguous_low_outputs",
        label="Assigned outputs are contiguous from DAC output 1",
        passed=bool(required_slots) and len(physical_indexes) == expected_count and contiguous,
        message=(
            "Assigned outputs map directly to the first active playback channels"
            if bool(required_slots) and len(physical_indexes) == expected_count and contiguous
            else (
                "This staging slice requires the active drivers on a contiguous "
                "block starting at DAC output 1"
            )
        ),
    ))
    if bool(required_slots) and (len(physical_indexes) != expected_count or not contiguous):
        issues.append(_issue(
            "blocker",
            "active_outputs_must_be_contiguous",
            "protected staging requires active outputs to be contiguous from DAC output 1",
        ))
    role_output_indexes = {
        (side, role): channels_by_slot[(side, role)].physical_output_index
        for side, role in required_slots
        if (
            (side, role) in channels_by_slot
            and channels_by_slot[(side, role)].physical_output_index is not None
        )
    }
    role_order_ok = (
        bool(required_slots)
        and len(role_output_indexes) == len(required_slots)
        and all(
            role_output_indexes.get((side, role)) == index
            for index, (side, role) in enumerate(required_slots)
        )
    )
    expected_role_order = ", ".join(
        f"{side} {role} on DAC output {index + 1}"
        if side != "mono"
        else f"{role} on DAC output {index + 1}"
        for index, (side, role) in enumerate(required_slots)
    )
    gates.append(_gate(
        "active_output_role_order",
        label="Assigned outputs match the protected DSP role order",
        passed=allow_mapped_role_order or role_order_ok,
        message=(
            "Preview-derived DSP will follow the saved output role mapping"
            if allow_mapped_role_order
            else (
            "Woofer and compression-driver outputs match the staged DSP order"
            if role_order_ok
            else f"This staging slice requires {expected_role_order}"
            )
        ),
    ))
    if (
        not allow_mapped_role_order
        and bool(required_slots)
        and len(role_output_indexes) == len(required_slots)
        and contiguous
        and not role_order_ok
    ):
        issues.append(_issue(
            "blocker",
            "active_outputs_must_match_role_order",
            f"first protected staging slice requires {expected_role_order}",
        ))

    tweeter_channels = [
        channel
        for (side, role), channel in channels_by_slot.items()
        if role == "tweeter" and (side, role) in required_slots
    ]
    tweeter_guard_declared = bool(tweeter_channels) and all(
        channel.startup_muted
        and channel.protection_required
        and channel.protection_status in {"present", "software_guard_requested"}
        for channel in tweeter_channels
    )
    physical_guard_present = bool(tweeter_channels) and all(
        channel.protection_status == "present" for channel in tweeter_channels
    )
    gates.append(_gate(
        "tweeter_guard_declared",
        label="High-frequency guard mode is explicit",
        passed=tweeter_guard_declared,
        message=(
            "High-frequency protection is present"
            if physical_guard_present
            else (
                "Software-only high-frequency guard was requested"
                if software_guard_requested
                else "Choose physical protection or software-guarded bring-up before staging"
            )
        ),
    ))
    if not tweeter_guard_declared:
        issues.append(_issue(
            "blocker",
            "tweeter_protection_required",
            "compression-driver guard mode must be explicit before staging",
        ))
    elif software_guard_requested:
        issues.append(_issue(
            "warning",
            "software_tweeter_guard_requested",
            (
                "software-only compression-driver guard requested; staging may "
                "write a no-load candidate but cannot authorize playback"
            ),
        ))

    if issues:
        blocker_count = sum(
            1 for issue in issues if issue.get("severity") == "blocker"
        )
        if blocker_count:
            return None, issues, gates, active_groups

    try:
        bound = replace(
            preset,
            channel_map=ActiveChannelMap(
                layout=preset.channel_map.layout,
                outputs=tuple(sorted(outputs, key=lambda item: item.index)),
            ),
        )
        bound.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "bound_active_preset_invalid",
            f"saved topology could not bind to protected DSP preset: {exc}",
        ))
        return None, issues, gates, active_groups

    return bound, issues, gates, active_groups


def stage_protected_startup_config(
    topology: OutputTopology,
    *,
    preset: ActiveSpeakerPreset | None = None,
    crossover_preview: dict[str, Any] | None = None,
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

    created_at = created_at or _utc_now()
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    source: dict[str, Any]
    allow_mapped_role_order = False
    if crossover_preview is not None:
        source_preview = (
            crossover_preview.get("source")
            if isinstance(crossover_preview.get("source"), dict)
            else {}
        )
        preset, preview_issues, preview_gates = _preset_from_crossover_preview(
            topology,
            crossover_preview,
        )
        issues.extend(preview_issues)
        gates.extend(preview_gates)
        allow_mapped_role_order = True
        source = {
            "mode": "crossover_preview",
            "preview_status": crossover_preview.get("status"),
            "preview_created_at": crossover_preview.get("created_at"),
            "preview_updated_at": crossover_preview.get("updated_at"),
            "design_draft_updated_at": source_preview.get("design_draft_updated_at"),
        }
    else:
        preset = preset or load_active_speaker_preset()
        source = {"mode": "preset_fallback"}

    active_groups: list[SpeakerGroup] = []
    bound_preset: ActiveSpeakerPreset | None = None
    if preset is not None:
        bound_preset, bind_issues, bind_gates, active_groups = _bind_preset_to_topology(
            preset,
            topology,
            allow_mapped_role_order=allow_mapped_role_order,
        )
        issues.extend(bind_issues)
        gates.extend(bind_gates)

    subwoofer_groups = _subwoofer_groups(topology)
    subwoofer_staging_supported = not subwoofer_groups
    gates.append(_gate(
        "subwoofer_startup_staging_scope",
        label="Optional subwoofer groups are included in startup staging",
        passed=subwoofer_staging_supported,
        message=(
            "No optional subwoofer groups are present"
            if subwoofer_staging_supported
            else (
                "Protected startup staging does not yet include optional "
                "subwoofer groups"
            )
        ),
    ))
    if subwoofer_groups:
        labels = ", ".join(group.label for group in subwoofer_groups)
        issues.append(_issue(
            "blocker",
            "subwoofer_staging_not_supported",
            (
                "protected startup staging does not yet include optional "
                f"subwoofer groups: {labels}"
            ),
        ))

    resolved_playback_device, playback_device_source = resolve_diagnostic_playback_device(
        topology,
        playback_device=playback_device,
    )
    route_capability = active_playback_route_capability(
        topology,
        playback_device=playback_device,
    )
    route_fits = route_capability.fits_required_outputs
    gates.append(_gate(
        "active_playback_route_capacity",
        label="Active playback route has enough output lanes",
        passed=route_fits,
        message=(
            "Active output layout fits this install's playback route"
            if route_fits
            else (
                "Choose a smaller active layout on this install, or widen "
                "the active outputd route before testing"
            )
        ),
    ))
    for issue in route_capability.issues:
        if issue.get("code") == "active_playback_route_too_narrow":
            issues.append(issue)
    playback_device_ready = bool(resolved_playback_device)
    gates.append(_gate(
        "explicit_active_playback_device",
        label="Active playback route is resolved",
        passed=playback_device_ready,
        message=(
            f"Using {resolved_playback_device} ({playback_device_source})"
            if resolved_playback_device
            else f"Set {ACTIVE_PLAYBACK_DEVICE_ENV} for this active-speaker route"
        ),
    ))
    if not playback_device_ready:
        issues.append(_issue(
            "blocker",
            "active_playback_device_required",
            "protected staging requires a resolved active playback route",
        ))

    out_path = staged_config_path(config_dir=config_dir, path=config_path)
    meta_path = staged_metadata_path(metadata_path)
    validation: dict[str, Any] = {"status": "skipped", "reason": "not_generated"}
    classification: dict[str, Any] = {}
    software_guard: dict[str, Any] = {}
    software_guard_requested = _software_guard_requested_any(active_groups)
    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")

    if blocker_count == 0 and bound_preset and resolved_playback_device:
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
            if software_guard_requested:
                software_guard = _software_guard_evidence(yaml, preset=bound_preset)
                gates.append(_gate(
                    "software_tweeter_guard_evidence",
                    label="Software compression-driver guard is present in generated config",
                    passed=bool(software_guard.get("passed")),
                    message=(
                        "Generated config keeps the compression-driver path muted, "
                        "high-passed, limited, and headroom-clamped"
                        if software_guard.get("passed")
                        else "Generated config is missing required software guard evidence"
                    ),
                ))
                if not software_guard.get("passed"):
                    missing = sorted(
                        key for key, passed in software_guard.get("checks", {}).items()
                        if not passed
                    )
                    issues.append(_issue(
                        "blocker",
                        "software_tweeter_guard_incomplete",
                        "software compression-driver guard is incomplete: "
                        + ", ".join(missing),
                    ))
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
    target_outputs = _target_outputs_for_groups(active_groups)
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STAGED_STARTUP_CONFIG_KIND,
        "status": status,
        "created_at": created_at,
        "metadata_path": str(meta_path),
        "preset": {
            "preset_id": preset.preset_id if preset else None,
            "name": preset.name if preset else None,
            "way_count": preset.way_count if preset else None,
            "layout": preset.channel_map.layout if preset else None,
            "source": source,
        },
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
            "speaker_group_id": active_groups[0].id if len(active_groups) == 1 else None,
            "speaker_label": active_groups[0].label if len(active_groups) == 1 else None,
            "speaker_group_ids": [group.id for group in active_groups],
            "speaker_labels": [group.label for group in active_groups],
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
            "tweeter_protective_highpass_hz": (
                _protective_hp_hz(preset) if preset else None
            ),
            "validation": validation,
        },
        "software_guard": software_guard,
        "load": {
            "load_allowed": False,
            "load_gate": "startup_load_preflight_required",
            "next_step": (
                "Run the guarded startup-load preflight before CamillaDSP is "
                "allowed to reload this staged graph."
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
        "source=%s config=%s blockers=%d",
        status,
        preset.preset_id if preset else None,
        topology.topology_id,
        source.get("mode"),
        out_path,
        blocker_count,
    )
    return payload
