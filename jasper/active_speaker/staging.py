# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import yaml

from jasper.camilla_config_contract import DEFAULT_VOLUME_LIMIT_DB
from jasper.dsp_apply import CamillaConfigValidationResult, validate_camilla_config
from jasper.output_topology import OutputTopology, SpeakerChannel, SpeakerGroup

from . import graph_safety as gs
from ._common import gate as _gate, issue as _issue
from .camilla_yaml import (
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
    audible_outputs_for_role,
    crossover_highpass_for_role,
    emit_active_speaker_commissioning_config,
    output_commission_mute_name,
)
from .crossover_preview import CROSSOVER_PREVIEW_KIND
from .environment import classify_camilla_config_text
from .graph_evidence import (
    driver_limiter_name,
    protective_tweeter_hp_name,
)
from .profile import (
    DEFAULT_SUB_CROSSOVER_HZ,
    ActiveChannelMap,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    DriverSpec,
    LocalSubwoofer,
    OutputChannel,
    SafetyEnvelope,
    required_driver_roles,
)
from .playback_route import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from .tone_plan import load_active_speaker_preset
from .test_signal_plan import protective_tweeter_highpass_frequency_hz

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STAGED_STARTUP_CONFIG_KIND = "jts_active_speaker_staged_startup_config"
DEFAULT_STAGED_CONFIG_NAME = "active_speaker_staged_startup.yml"
DEFAULT_STAGED_METADATA_PATH = Path("/var/lib/jasper/active_speaker_staged_config.json")
DEFAULT_CAMILLA_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
# A per-driver commissioning config is a TRANSIENT runtime load, never the
# durable boot config: it is written to its own path so it can never overwrite
# the all-muted staged boot config (the crash-recovery-MUTED invariant).
DEFAULT_COMMISSIONING_CONFIG_NAME = "active_speaker_commissioning.yml"
COMMISSIONING_CONFIG_KIND = "jts_active_speaker_commissioning_config"
SUMMED_COMMISSION_TARGET_ROLE = "summed"
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


def _local_subwoofer_from_topology(
    topology: OutputTopology,
    *,
    main_output_count: int,
) -> tuple[LocalSubwoofer | None, list[dict[str, str]]]:
    """Derive the local-subwoofer lane intent from a routed subwoofer group.

    Returns ``(LocalSubwoofer, [])`` when exactly one subwoofer group routes to a
    single assigned ``subwoofer`` channel pinned to the next contiguous output
    after the mains, or ``(None, issues)`` so the caller blocks fail-closed. A sub
    that cannot be resolved to a safe, contiguously-pinned output never reaches
    the emitter — a sub output must never carry a full-range / unbounded feed.

    The crossover corner is read from the resolved subwoofer channel's
    user-settable ``crossover_fc_hz`` (the ``/sound`` subwoofer card writes it
    onto the topology); it falls back to the shared bass-management default
    (:data:`DEFAULT_SUB_CROSSOVER_HZ`, 80 Hz) only when the channel leaves it
    unset. An out-of-range corner is already a fail-loud topology blocker
    (``subwoofer_crossover_out_of_range``), so a value that reaches here is
    in-range; ``LocalSubwoofer.validate`` re-checks it as defense in depth.
    """
    sub_groups = _subwoofer_groups(topology)
    if not sub_groups:
        return None, []
    issues: list[dict[str, str]] = []
    if len(sub_groups) != 1:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_single_group_required",
            "active profile supports exactly one local subwoofer group",
        ))
        return None, issues
    group = sub_groups[0]
    sub_channels = [
        channel for channel in group.channels if channel.role == "subwoofer"
    ]
    if len(sub_channels) != 1:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_channel_unresolved",
            f"{group.label} must have exactly one subwoofer channel",
        ))
        return None, issues
    sub_channel = sub_channels[0]
    output_index = sub_channel.physical_output_index
    crossover_fc_hz = (
        sub_channel.crossover_fc_hz
        if sub_channel.crossover_fc_hz is not None
        else DEFAULT_SUB_CROSSOVER_HZ
    )
    if output_index is None:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_output_unassigned",
            f"{group.label} subwoofer is not assigned to a DAC output",
        ))
        return None, issues
    # The sub output MUST be the next contiguous channel after the mains: a misrouted
    # sub index would mute the wrong output (or leave the sub channel un-band-limited).
    if output_index != main_output_count:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_output_not_contiguous",
            (
                f"{group.label} subwoofer must be on DAC output "
                f"{main_output_count + 1} (the next channel after the mains)"
            ),
        ))
        return None, issues
    try:
        sub = LocalSubwoofer(
            physical_output_index=output_index,
            label=group.label or "subwoofer",
            crossover_fc_hz=crossover_fc_hz,
        )
        sub.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "active_subwoofer_invalid",
            f"could not resolve a safe local subwoofer lane: {exc}",
        ))
        return None, issues
    return sub, issues


def _protective_hp_hz(preset: ActiveSpeakerPreset) -> float | None:
    return protective_tweeter_highpass_frequency_hz(preset, "tweeter")


def _all_commission_mutes_engaged(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
) -> bool:
    """Every per-output commission mute is muted AND wired — the crash-recovery boot state.

    The single-audio-path commissioning config isolates drivers with a
    per-physical-output mute mask. The *staged* candidate is the muted boot
    config (``audible_outputs=frozenset()``): a crash or power loss partway
    through commissioning must reboot into everything-muted, never a driver left
    unmuted at level with no protection. Per-driver unmute is a transient runtime
    load, never the frozen boot config (HANDOFF-active-speaker-dsp.md "Resolved
    decisions").

    This is the *only* mute assertion that runs on every staged config (the
    software guard runs solely in software-protection mode), so it verifies each
    physical output from the preset rather than trusting the emitter to keep its
    filter-definition and pipeline loops in lockstep: for every output index the
    ``as_out{idx}_commission_mute`` filter must be a -120 dB hard mute **and** be
    applied to channel ``{idx}`` in the pipeline. A muted-but-unwired (or
    wired-but-unmuted) output fails closed. Mirrors the per-index rigor of
    :func:`_software_guard_evidence`.
    """
    view = gs.view_from_emitted_text(yaml)
    output_count = max((o.index for o in preset.channel_map.outputs), default=-1) + 1
    if output_count <= 0:
        return False
    return all(
        gs.output_hard_muted_and_wired(
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
        for index in range(output_count)
    )


def _software_guard_evidence(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
) -> dict[str, Any]:
    protective_hp_hz = _protective_hp_hz(preset)
    view = gs.view_from_emitted_text(yaml)
    tweeter_channels = audible_outputs_for_role(preset, "tweeter")
    # Commissioning isolates per *physical output*, so the tweeter is muted iff
    # every physical output carrying it has its as_out{idx}_commission_mute layer
    # engaged. There is no per-role startup mute to check anymore.
    tweeter_outputs_muted = bool(tweeter_channels) and all(
        gs.filter_param_matches(
            view,
            output_commission_mute_name(index),
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB, "mute": True},
        )
        for index in tweeter_channels
    )
    # The protective high-pass + startup limiter still wrap the tweeter channel
    # in the running pipeline, and every tweeter output keeps its commission-mute
    # layer — so an unmuted tweeter cannot reach the amp without its protection.
    tweeter_pipeline_guarded = (
        bool(tweeter_channels)
        and gs.pipeline_contains_chain(
            view,
            channels=set(tweeter_channels),
            required_names=(
                protective_tweeter_hp_name("tweeter"),
                driver_limiter_name("tweeter"),
            ),
        )
        and all(
            gs.pipeline_contains_chain(
                view,
                channels={index},
                required_names=(output_commission_mute_name(index),),
            )
            for index in tweeter_channels
        )
    )
    checks = {
        "startup_muted": tweeter_outputs_muted,
        "protective_highpass": (
            protective_hp_hz is not None
            and gs.filter_param_matches(
                view,
                protective_tweeter_hp_name("tweeter"),
                filter_type="BiquadCombo",
                params={
                    "type": "LinkwitzRileyHighpass",
                    "freq": protective_hp_hz,
                    "order": 4,
                },
            )
        ),
        "startup_headroom": gs.filter_param_matches(
            view,
            "active_startup_headroom",
            filter_type="Gain",
            params={"gain": -STARTUP_HEADROOM_DB},
        ),
        "startup_limiter": gs.filter_param_matches(
            view,
            driver_limiter_name("tweeter"),
            filter_type="Limiter",
            params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
        ),
        "tweeter_pipeline_guarded": tweeter_pipeline_guarded,
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


def driver_commission_audible_evidence(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
    audible_outputs: frozenset[int] | set[int],
    expected_headroom_db: float = STARTUP_HEADROOM_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
) -> dict[str, Any]:
    """Per-driver commissioning safety: ONLY the target is audible, and an
    audible tweeter still carries its protective high-pass + limiter.

    The single-audio-path commissioning loads, one driver at a time, a config
    where exactly the target driver's physical outputs are unmuted and every
    other output is hard-muted. This is the *config-level* form of the Stage-5
    safety rule "assert the high-pass is present before the tweeter is unmuted"
    (HANDOFF-active-speaker-dsp.md). It verifies, against the emitted YAML:

    1. **Audible mask is exactly ``audible_outputs``** — each listed output's
       ``as_out{idx}_commission_mute`` is un-muted AND wired to its channel;
       every OTHER output is a -120 dB hard mute wired to its channel. A muted
       output that is silently un-wired (or vice versa) fails closed. Mirrors
       :func:`_all_commission_mutes_engaged`'s per-index rigor.
    2. **Protection-while-audible** — every AUDIBLE tweeter output keeps the
       ``as_tweeter_protective_hp`` Linkwitz-Riley high-pass (at the correct
       ``_protective_hp_hz``) and the ``as_tweeter_startup_limiter`` wrapping its
       channel in the running pipeline. A woofer-only target has no audible
       tweeter, so this check is vacuously satisfied while the tweeter stays
       muted.

    Pure analysis of the emitted YAML — opens nothing, loads nothing. The
    assertion against the LIVE CamillaDSP graph (not just the file) before any
    tweeter is unmuted on hardware remains the on-device Stage-5 gate; this is
    the off-device half that gates whether the config is even allowed to load.
    """
    audible = {int(i) for i in audible_outputs}
    view = gs.view_from_emitted_text(yaml)
    output_count = max((o.index for o in preset.channel_map.outputs), default=-1) + 1

    # (1) Audible mask: listed outputs un-muted, all others -120 dB hard-muted,
    # every commission-mute filter wired to its own channel. Fail closed.
    mask_correct = output_count > 0 and bool(audible) and audible <= set(
        range(output_count)
    )
    muted_outputs: list[int] = []
    for index in range(output_count):
        name = output_commission_mute_name(index)
        if index in audible:
            ok = gs.output_unmuted_and_wired(view, index, mute_name=name)
        else:
            muted_outputs.append(index)
            ok = gs.output_hard_muted_and_wired(
                view, index, mute_name=name, mute_gain_db=STARTUP_MUTE_GAIN_DB
            )
        if not ok:
            mask_correct = False

    # (2) Protection-while-audible for an audible tweeter.
    tweeter_outputs = audible_outputs_for_role(preset, "tweeter")
    audible_tweeter = audible & set(tweeter_outputs)
    if filter_mode == APPLIED_RESPONSE_FILTER_MODE:
        highpass = crossover_highpass_for_role(preset, "tweeter")
    else:
        protective_hz = _protective_hp_hz(preset)
        highpass = (
            (
                protective_tweeter_hp_name("tweeter"),
                protective_hz,
                4,
            )
            if protective_hz is not None
            else None
        )
    highpass_name = highpass[0] if highpass is not None else ""
    protective_hp_hz = highpass[1] if highpass is not None else None
    highpass_order = highpass[2] if highpass is not None else None
    if not audible_tweeter:
        tweeter_protected = True  # vacuous: the tweeter stays muted
    else:
        hp_defined = protective_hp_hz is not None and gs.filter_param_matches(
            view,
            highpass_name,
            filter_type="BiquadCombo",
            params={
                "type": "LinkwitzRileyHighpass",
                "freq": protective_hp_hz,
                "order": highpass_order,
            },
        )
        limiter_defined = gs.filter_param_matches(
            view,
            driver_limiter_name("tweeter"),
            filter_type="Limiter",
            params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
        )
        hp_limiter_wired = gs.pipeline_contains_chain(
            view,
            channels=audible_tweeter,
            required_names=(
                highpass_name,
                driver_limiter_name("tweeter"),
            ),
        )
        tweeter_protected = bool(hp_defined and limiter_defined and hp_limiter_wired)

    headroom = gs.filter_param_matches(
        view,
        "active_startup_headroom",
        filter_type="Gain",
        params={"gain": -expected_headroom_db},
    )
    checks = {
        "audible_mask_correct": mask_correct,
        "tweeter_protected_while_audible": tweeter_protected,
        "startup_headroom": headroom,
    }
    return {
        "audible_outputs": sorted(audible),
        "muted_outputs": muted_outputs,
        "tweeter_outputs": sorted(int(i) for i in tweeter_outputs),
        "audible_tweeter_outputs": sorted(audible_tweeter),
        "protective_highpass_hz": protective_hp_hz,
        "tweeter_highpass_name": highpass_name,
        "tweeter_highpass_order": highpass_order,
        "startup_headroom_db": expected_headroom_db,
        "limiter_clip_limit_db": STARTUP_LIMITER_CLIP_LIMIT_DB,
        "checks": checks,
        "passed": all(checks.values()),
    }


# --- live (running-graph) read-back evidence ---------------------------------
#
# `driver_commission_audible_evidence` (above) proves a config is safe BEFORE it
# loads, parsing the emitted YAML *text*. The guarded commissioning load
# (`startup_load.load_driver_commissioning_config`) needs the same proof AFTER
# the load, against the config CamillaDSP is ACTUALLY running — read back over
# the websocket (`CamillaController.get_active_config_raw`), not the file on
# disk. CamillaDSP re-serializes the running graph in its own YAML dialect
# (block-style lists, defaults filled, keys reordered, scalar `channel: N`
# sugar) that the emitted-text parser does not handle, so
# `running_commission_evidence` parses the read-back with a real YAML loader
# and runs the SAME shared invariant predicates on it via
# `graph_safety.view_from_camilla_dict` (see graph_safety.py for why the three
# parse dialects are kept separate while the predicates are shared).


def running_commission_evidence(
    running_config_raw: str | None,
    *,
    audible_outputs: Iterable[int],
    muted_outputs: Iterable[int],
    tweeter_outputs: Iterable[int],
    protective_hp_hz: float | None,
    tweeter_highpass_name: str = "",
    tweeter_highpass_order: int = 4,
    expected_headroom_db: float = STARTUP_HEADROOM_DB,
) -> dict[str, Any]:
    """Per-driver commissioning safety, asserted on the RUNNING CamillaDSP graph.

    The live counterpart of :func:`driver_commission_audible_evidence`: given the
    config CamillaDSP is actually running (``CamillaController.get_active_config_raw``)
    and the INTENDED mask the off-device gate already validated, prove the live
    graph still matches — exactly ``audible_outputs`` un-muted (every other
    output a -120 dB hard mute, all wired) and every audible tweeter still wrapped
    by its protective high-pass + startup limiter. This is the "assert the
    high-pass is present in the RUNNING pipeline, not just the config file" gate
    that guards the per-driver tweeter unmute (HANDOFF-active-speaker-dsp.md
    Stage 5). Fails closed: an unparseable read-back, a missing filter, or a mask
    that drifted from intent all return ``passed=False``.
    """
    audible = {int(i) for i in audible_outputs}
    muted = {int(i) for i in muted_outputs}
    tweeters = {int(i) for i in tweeter_outputs}
    declared = audible | muted
    audible_tweeter = audible & tweeters

    config: Any = None
    if isinstance(running_config_raw, str) and running_config_raw.strip():
        try:
            config = yaml.safe_load(running_config_raw)
        except yaml.YAMLError:
            config = None
    parse_ok = isinstance(config, dict)
    view = gs.view_from_camilla_dict(config if parse_ok else None)

    # (1) Audible mask: each declared output un-muted iff in `audible`, every
    # other declared output -120 dB hard-muted, and each mute wired to its own
    # channel. Fail closed on an empty declared set or any drift.
    mask_correct = parse_ok and bool(declared) and audible <= declared
    for index in sorted(declared):
        name = output_commission_mute_name(index)
        if index in audible:
            ok = gs.output_unmuted_and_wired(view, index, mute_name=name)
        else:
            ok = gs.output_hard_muted_and_wired(
                view, index, mute_name=name, mute_gain_db=STARTUP_MUTE_GAIN_DB
            )
        if not ok:
            mask_correct = False

    # (2) Protection-while-audible: an audible tweeter keeps its protective HP +
    # limiter, wired. A muted tweeter is vacuously safe — independent of parse
    # health, which the dedicated ``running_config_parsed`` check already gates.
    if not audible_tweeter:
        tweeter_protected = True
    else:
        highpass_name = tweeter_highpass_name or protective_tweeter_hp_name(
            "tweeter"
        )
        hp_defined = protective_hp_hz is not None and gs.filter_param_matches(
            view,
            highpass_name,
            filter_type="BiquadCombo",
            params={
                "type": "LinkwitzRileyHighpass",
                "freq": protective_hp_hz,
                "order": tweeter_highpass_order,
            },
        )
        limiter_defined = gs.filter_param_matches(
            view,
            driver_limiter_name("tweeter"),
            filter_type="Limiter",
            params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
        )
        hp_limiter_wired = gs.pipeline_contains_chain(
            view,
            channels=audible_tweeter,
            required_names=(
                highpass_name,
                driver_limiter_name("tweeter"),
            ),
        )
        tweeter_protected = bool(hp_defined and limiter_defined and hp_limiter_wired)

    headroom = gs.filter_param_matches(
        view,
        "active_startup_headroom",
        filter_type="Gain",
        params={"gain": -expected_headroom_db},
    )
    checks = {
        "running_config_parsed": parse_ok,
        "audible_mask_correct": mask_correct,
        "tweeter_protected_while_audible": tweeter_protected,
        "startup_headroom": headroom,
    }
    return {
        "audible_outputs": sorted(audible),
        "muted_outputs": sorted(muted),
        "audible_tweeter_outputs": sorted(audible_tweeter),
        "protective_highpass_hz": protective_hp_hz,
        "startup_headroom_db": expected_headroom_db,
        "checks": checks,
        "passed": all(checks.values()),
    }


def running_graph_matches_staged_anchor(
    running_config_raw: str | None,
    *,
    audible_outputs: Iterable[int],
) -> bool:
    """True when the RUNNING readback still shows the all-muted staged anchor.

    The convergence discriminator for the transient commissioning load
    (``startup_load.load_driver_commissioning_config``): CamillaDSP acks the
    inline ``SetConfig`` before its readback side reflects the new graph, so a
    read taken immediately after the load can still return the staged all-muted
    anchor (hardware-reproduced 2026-07-15, ~22 ms after the apply). The
    intended commission graph un-mutes exactly ``audible_outputs``; the staged
    anchor hard-mutes every output. So "every intended-audible output is still
    hard-muted AND wired" is the cheapest reliable "the switch has not landed
    yet" signal — robust to CamillaDSP's own YAML dialect, unlike raw-text
    comparison against the staged file.

    Fail direction: an unparseable/empty readback or an empty
    ``audible_outputs`` returns False — the caller cannot positively prove the
    graph is still the anchor, so the (failing) live evidence decides
    immediately rather than waiting out the convergence budget on a readback
    that will never discriminate.
    """
    audible = {int(i) for i in audible_outputs}
    if not audible:
        return False
    config: Any = None
    if isinstance(running_config_raw, str) and running_config_raw.strip():
        try:
            config = yaml.safe_load(running_config_raw)
        except yaml.YAMLError:
            config = None
    if not isinstance(config, dict):
        return False
    view = gs.view_from_camilla_dict(config)
    return all(
        gs.output_hard_muted_and_wired(
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
        for index in audible
    )


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
                # Persisted working-crossover values (Slice 0): a per-side
                # mismatch here trips the SAME stereo-consistency blocker below
                # as a frequency/slope mismatch — a preview only stages when
                # both sides agree.
                "lower_polarity": crossover.get("lower_polarity"),
                "upper_polarity": crossover.get("upper_polarity"),
                "delay_ms": crossover.get("delay_ms"),
                "delay_target_role": crossover.get("delay_target_role"),
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
            lower_polarity=value.get("lower_polarity") or "non-inverted",
            upper_polarity=value.get("upper_polarity") or "non-inverted",
            delay_ms=value.get("delay_ms"),
            delay_target_driver=value.get("delay_target_role"),
        ))

    # A routed local subwoofer is the lower half of the bass-management crossover;
    # the mains' lowest driver carries the complementary high-pass. Resolve it here
    # (fail-closed) so both the candidate compile and protected staging emit the SAME
    # sub-bearing graph through the one multi-output emitter. The sub output pins to
    # the next contiguous channel after the mains (validated against main_output_count).
    local_subwoofer, sub_issues = _local_subwoofer_from_topology(
        topology, main_output_count=len(outputs)
    )
    issues.extend(sub_issues)

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
            local_subwoofer=local_subwoofer,
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


# Passive mains (full_range_passive) carry NO inter-driver crossover, so they
# produce no active crossover preview — the preview-driven compile path above has
# nothing to feed it. A passive speaker is only ever split onto the roleful
# multi-output emitter when a LOCAL SUBWOOFER is present (bass management: the sub
# gets an LR4 low-pass, each main its complementary high-pass). That degenerate
# 1-way preset is built directly from the saved topology here, NOT from a preview.
_PASSIVE_MAIN_MODE = "full_range_passive"
_PASSIVE_MAIN_ROLE = "full_range"


def topology_is_passive_mains_with_sub(topology: OutputTopology) -> bool:
    """True iff the saved topology is full-range passive mains PLUS a sub group.

    This is the single predicate the build path uses to route a passive+sub
    topology through the active multi-output emitter (the degenerate 1-way bass-
    management path) instead of the flat ``emit_sound_config`` lane. A SUBLESS
    passive speaker returns False and stays on the flat path (byte-identical).
    """
    mains = [
        group
        for group in topology.speaker_groups
        if group.kind in {"left", "right", "mono"}
    ]
    if not mains or any(group.mode != _PASSIVE_MAIN_MODE for group in mains):
        return False
    return bool(_subwoofer_groups(topology))


def _passive_mains_with_sub_preset(
    topology: OutputTopology,
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    """Build the degenerate 1-way (passive full-range + local sub) preset.

    The passive analogue of :func:`_preset_from_crossover_preview`: a passive
    speaker has no active crossover preview to compile, but a routed local
    subwoofer still needs the roleful emitter (bass management). This resolves the
    full-range mains + the sub lane directly from the saved topology, fail-closed
    (an unresolvable sub returns ``(None, issues, gates)`` — never a mains-only
    graph that leaves the sub un-band-limited or full-range). It does not write
    YAML, load CamillaDSP, or authorize playback.
    """
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []

    mains = [
        group
        for group in topology.speaker_groups
        if group.kind in {"left", "right", "mono"}
    ]
    by_kind = {group.kind: group for group in mains}
    if set(by_kind) == {"mono"} and len(mains) == 1:
        layout = "mono"
        ordered = [by_kind["mono"]]
    elif set(by_kind) == {"left", "right"} and len(mains) == 2:
        layout = "stereo"
        ordered = [by_kind["left"], by_kind["right"]]
    else:
        issues.append(_issue(
            "blocker",
            "passive_sub_layout_unsupported",
            "bass management supports one mono passive speaker or a left/right pair",
        ))
        gates.append(_gate(
            "passive_sub_layout",
            label="Passive mains layout is supported for bass management",
            passed=False,
            message="Bass management supports one mono passive speaker or a left/right pair",
        ))
        return None, issues, gates
    gates.append(_gate(
        "passive_sub_layout",
        label="Passive mains layout is supported for bass management",
        passed=True,
        message=f"Passive {layout} mains can be bass-managed with a local subwoofer",
    ))

    outputs: list[OutputChannel] = []
    for group in ordered:
        side = group.kind if group.kind in {"left", "right"} else "mono"
        channel = next(
            (c for c in group.channels if c.role == _PASSIVE_MAIN_ROLE), None
        )
        if channel is None or channel.physical_output_index is None:
            issues.append(_issue(
                "blocker",
                "passive_main_output_unassigned",
                f"{group.label} full-range driver is not assigned to a DAC output",
            ))
            continue
        outputs.append(OutputChannel(
            index=channel.physical_output_index,
            side=side,
            driver_role=_PASSIVE_MAIN_ROLE,
            label=(
                channel.human_output_label
                or f"DAC output {channel.physical_output_index + 1}"
            ),
            startup_muted=True,
        ))

    # The sub pins to the next contiguous channel after the mains (validated
    # against main_output_count) and carries the user-settable bass-mgmt corner.
    local_subwoofer, sub_issues = _local_subwoofer_from_topology(
        topology, main_output_count=len(outputs)
    )
    issues.extend(sub_issues)
    if local_subwoofer is None:
        # A passive topology routed here ALWAYS has a sub group (the caller gates on
        # topology_is_passive_mains_with_sub); a None sub means the fail-closed
        # resolution rejected it. Its blocker is already in `issues`; never emit a
        # mains-only graph that drops the sub.
        if not any(i.get("severity") == "blocker" for i in issues):
            issues.append(_issue(
                "blocker",
                "passive_sub_unresolved",
                "routed subwoofer could not be resolved for the passive mains",
            ))
        return None, issues, gates

    if any(i.get("severity") == "blocker" for i in issues):
        return None, issues, gates

    try:
        preset = ActiveSpeakerPreset(
            preset_id=f"passive-sub-{_safe_stem(topology.topology_id)}",
            name=f"{topology.name} passive full-range + local sub",
            way_count=1,
            channel_map=ActiveChannelMap(
                layout=layout,
                outputs=tuple(sorted(outputs, key=lambda item: item.index)),
            ),
            drivers={
                _PASSIVE_MAIN_ROLE: DriverSpec(
                    role=_PASSIVE_MAIN_ROLE,
                    manufacturer="Operator research",
                    model=_PASSIVE_MAIN_ROLE,
                ),
            },
            crossover_regions=(),
            local_subwoofer=local_subwoofer,
            safety=SafetyEnvelope(
                initial_sweep_level_db_spl=55.0,
                max_commissioning_level_db_spl=80.0,
                escalation_step_db=1.0,
                require_physical_tweeter_protection=True,
                require_channel_identity_before_drivers=True,
                emergency_stop_required=True,
            ),
            notes="Derived from a passive-mains + local-subwoofer topology; bass management only.",
        )
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "passive_sub_preset_invalid",
            f"could not build a passive bass-management preset: {exc}",
        ))
        return None, issues, gates

    gates.append(_gate(
        "passive_sub_compiled",
        label="Passive mains + local subwoofer compiled to bass-management intent",
        passed=True,
        message="Passive bass-management preset can be staged through the active emitter",
    ))
    return preset, issues, gates


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


def _build_active_commissioning_context(
    topology: OutputTopology,
    *,
    preset: ActiveSpeakerPreset | None,
    crossover_preview: dict[str, Any] | None,
    playback_device: str | None,
) -> dict[str, Any]:
    """Compile + bind + resolve the shared active-commissioning context.

    Both the all-muted staged boot config
    (:func:`stage_protected_startup_config`) and a per-driver commissioning
    config (:func:`prepare_driver_commissioning_config`) do exactly this before
    emitting their YAML: resolve the preset (from a crossover preview or the
    bundled fallback), bind it to the topology, reject not-yet-staged subwoofer
    groups, and resolve + capacity-check the active playback route. Returns the
    bound preset, active groups, source, resolved device, and the accumulated
    gates/issues, so each caller only adds its own emit + per-config safety gate
    (the all-muted crash-recovery gate vs the per-driver protection-while-audible
    gate) rather than duplicating this ~100-line sequence.
    """
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

    # A routed local subwoofer is armed into the protected-startup graph exactly like
    # the other drivers: the preset builder pins it onto bound_preset, and the
    # commissioning emitter wires its output MUTED at startup (the same per-output
    # crash-recovery mask the woofer/tweeter get). The gate passes when a routed sub
    # was actually resolved onto the staged preset; a sub group present in the topology
    # but absent from the bound preset means the fail-closed resolution rejected it
    # (its blocker is already in `issues`), so staging stays blocked.
    subwoofer_groups = _subwoofer_groups(topology)
    sub_armed = bool(bound_preset and bound_preset.local_subwoofer is not None)
    subwoofer_staging_supported = (not subwoofer_groups) or sub_armed
    gates.append(_gate(
        "subwoofer_startup_staging_scope",
        label="Routed subwoofer groups are armed (muted) in startup staging",
        passed=subwoofer_staging_supported,
        message=(
            "No optional subwoofer groups are present"
            if not subwoofer_groups
            else (
                "Local subwoofer output is staged muted with the other drivers"
                if sub_armed
                else "Could not arm the routed subwoofer into the protected startup graph"
            )
        ),
    ))
    if subwoofer_groups and not sub_armed:
        # Fail closed: a routed sub that did not make it onto the staged preset (e.g.
        # the preset-fallback path, or a resolution the builder rejected) must block
        # staging — never silently drop the sub and stage a mains-only graph.
        labels = ", ".join(group.label for group in subwoofer_groups)
        issues.append(_issue(
            "blocker",
            "subwoofer_staging_unresolved",
            (
                "routed subwoofer could not be armed into the protected startup "
                f"graph: {labels}"
            ),
        ))

    resolved_playback_device, playback_device_source = resolve_active_playback_device(
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
    return {
        "preset": preset,
        "bound_preset": bound_preset,
        "active_groups": active_groups,
        "source": source,
        "resolved_playback_device": resolved_playback_device,
        "playback_device_source": playback_device_source,
        "gates": gates,
        "issues": issues,
    }


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
    ctx = _build_active_commissioning_context(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
    )
    preset = ctx["preset"]
    bound_preset = ctx["bound_preset"]
    active_groups = ctx["active_groups"]
    source = ctx["source"]
    resolved_playback_device = ctx["resolved_playback_device"]
    playback_device_source = ctx["playback_device_source"]
    gates = ctx["gates"]
    issues = ctx["issues"]

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
            # Stage the production graph with an all-muted per-output mask
            # (audible_outputs=frozenset()). Validation now happens through the
            # real path: this same config is what later freezes as the durable
            # profile. Per-driver unmute is a transient runtime load, never the
            # frozen boot config — so the staged candidate is fully muted.
            yaml = emit_active_speaker_commissioning_config(
                bound_preset,
                playback_device=resolved_playback_device,
                audible_outputs=frozenset(),
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
            # Crash-recovery invariant: the staged boot config must start with
            # every active output muted. A reboot partway through commissioning
            # has to come up everything-muted, never a tweeter unmuted at level.
            fully_muted = _all_commission_mutes_engaged(yaml, preset=bound_preset)
            gates.append(_gate(
                "staged_candidate_fully_muted",
                label="Staged boot config starts with every output muted",
                passed=fully_muted,
                message=(
                    "Every active output is muted at startup (crash-recovery safe)"
                    if fully_muted
                    else "Staged active-speaker config is not fully muted at startup"
                ),
            ))
            if not fully_muted:
                issues.append(_issue(
                    "blocker",
                    "staged_config_not_fully_muted",
                    "staged active-speaker boot config must start with every "
                    "output muted",
                ))
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


def commissioning_config_path(
    *, config_dir: str | Path | None = None, path: str | Path | None = None
) -> Path:
    """Path of the TRANSIENT per-driver commissioning config (never the boot config)."""
    if path:
        return Path(path)
    return Path(config_dir or DEFAULT_CAMILLA_CONFIG_DIR) / DEFAULT_COMMISSIONING_CONFIG_NAME


def prepare_driver_commissioning_config(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    preset: ActiveSpeakerPreset | None = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    run_config_check: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Emit + safety-assert (NO load) the per-driver commissioning config.

    Emits the production graph with ``audible_outputs`` = the ``(speaker_group_id,
    role)`` target's physical outputs (every other output hard-muted), asserts
    the protection-while-audible invariant
    (:func:`driver_commission_audible_evidence`) + the CamillaDSP volume ceiling,
    validates syntax, and returns evidence with ``load_allowed`` gated behind the
    guarded runtime load. Shares the compile/bind/resolve work with
    :func:`stage_protected_startup_config` via
    :func:`_build_active_commissioning_context`.

    Requires exactly ONE active speaker group and unmutes the target's whole
    role (a mono cabinet's woofer is one output; a stereo group's woofer is both
    sides). A multi-group topology fails closed; per-SIDE isolation is a future
    selector.

    Per-driver unmute is a TRANSIENT runtime load: this config is never the
    durable boot config (which ``stage_protected_startup_config`` keeps all-muted
    for crash recovery), so it is written to its own commissioning path. The
    actual CamillaDSP reload + rollback is the separate guarded load step; this
    function opens nothing and loads nothing.
    """
    created_at = created_at or _utc_now()
    role = (role or "").strip().lower()
    group_id = (speaker_group_id or "").strip()
    ctx = _build_active_commissioning_context(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
    )
    bound_preset = ctx["bound_preset"]
    active_groups = ctx["active_groups"]
    resolved_playback_device = ctx["resolved_playback_device"]
    playback_device_source = ctx["playback_device_source"]
    gates = ctx["gates"]
    issues = ctx["issues"]

    # Resolve the target's audible outputs. `_build_active_commissioning_context`
    # -> `_bind_preset_to_topology` already enforces a SINGLE active speaker group
    # (one bound preset == one speaker; a multi-group topology fails closed there
    # with `mono_active_group_required`), so the bound preset's role outputs ARE
    # the target group's outputs -- there is no cross-group mis-scope to guard
    # against here. `speaker_group_id` is load-bearing: it must name that one
    # active group. The audible set is the whole role -- a mono cabinet's woofer
    # is one output, a stereo group's woofer is both sides; per-SIDE isolation
    # (driving L or R alone) is a future selector, not this.
    #
    # The same transient commissioning graph is also the intended summed-check
    # boundary: SUMMED_COMMISSION_TARGET_ROLE is a named internal target, not an
    # ordinary driver role. It means the target active group's full driver set is
    # live through the real crossover/limiter path for one bounded validation
    # tone. The single-active-group gate above keeps that deliberately narrow.
    audible_outputs: frozenset[int] = frozenset()
    active_group_id = active_groups[0].id if active_groups else None
    if group_id and group_id != active_group_id:
        issues.append(_issue(
            "blocker",
            "commissioning_target_group_unknown",
            "driver commissioning target group is not the active speaker group",
        ))
    if active_group_id is not None and bound_preset is not None and role:
        if role == SUMMED_COMMISSION_TARGET_ROLE:
            audible_outputs = frozenset(
                output.index for output in bound_preset.channel_map.outputs
            )
        else:
            audible_outputs = audible_outputs_for_role(bound_preset, role)
    if not audible_outputs:
        issues.append(_issue(
            "blocker",
            "commissioning_target_role_unknown",
            f"no active outputs carry the role {role!r}",
        ))
    gates.append(_gate(
        "commissioning_target_resolved",
        label="Per-driver commissioning target resolves to physical outputs",
        passed=bool(audible_outputs),
        message=(
            f"Target {group_id}/{role} -> outputs {sorted(audible_outputs)}"
            if audible_outputs
            else f"No active outputs carry the role {role!r}"
        ),
    ))

    out_path = commissioning_config_path(config_dir=config_dir, path=config_path)
    validation: dict[str, Any] = {"status": "skipped", "reason": "not_generated"}
    classification: dict[str, Any] = {}
    audible_evidence: dict[str, Any] = {}
    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")

    if (
        blocker_count == 0
        and bound_preset is not None
        and resolved_playback_device
        and audible_outputs
    ):
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            yaml = emit_active_speaker_commissioning_config(
                bound_preset,
                playback_device=resolved_playback_device,
                audible_outputs=audible_outputs,
                audible_gain_db=audible_gain_db,
                volume_limit_db=volume_limit_db,
                startup_headroom_db=COMMISSIONING_HEADROOM_DB,
                out_path=out_path,
                baseline_id=f"commission-{_safe_stem(topology.topology_id)}-{role}",
                filter_mode=filter_mode,
            )
            classification = classify_camilla_config_text(yaml)
            gates.append(_gate(
                "generated_active_commissioning_candidate",
                label="Generated config is classified as active-speaker startup",
                passed=(
                    classification.get("classification") == "active_startup_candidate"
                ),
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
            # The per-driver protection-while-audible gate (the config-level
            # form of the Stage-5 "HP present before the tweeter is unmuted").
            audible_evidence = driver_commission_audible_evidence(
                yaml,
                preset=bound_preset,
                audible_outputs=audible_outputs,
                expected_headroom_db=COMMISSIONING_HEADROOM_DB,
                filter_mode=filter_mode,
            )
            gates.append(_gate(
                "driver_protection_while_audible",
                label="Only the target is audible; an audible tweeter keeps its protection",
                passed=bool(audible_evidence.get("passed")),
                message=(
                    "Audible mask is exactly the target and tweeter protection is intact"
                    if audible_evidence.get("passed")
                    else "Config failed the per-driver protection-while-audible gate"
                ),
            ))
            if not audible_evidence.get("passed"):
                missing = sorted(
                    key
                    for key, passed in audible_evidence.get("checks", {}).items()
                    if not passed
                )
                issues.append(_issue(
                    "blocker",
                    "driver_protection_while_audible_incomplete",
                    "per-driver commissioning config failed protection-while-audible: "
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
                "commissioning_config_generation_failed",
                f"could not generate commissioning config: {type(exc).__name__}",
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
                else "CamillaDSP validation blocked the commissioning config"
            ),
        ))
    if validation_status not in {"valid", "missing", "skipped", "not_generated"}:
        issues.append(_issue(
            "blocker",
            "commissioning_config_validation_failed",
            f"CamillaDSP validation status is {validation_status}",
        ))

    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")
    status = "prepared" if blocker_count == 0 and out_path.exists() else "blocked"
    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": COMMISSIONING_CONFIG_KIND,
        "status": status,
        "created_at": created_at,
        # The speaker's way count, so a caller (the Stage-5 ramp ordering gate)
        # knows which driver roles exist in this cabinet without re-binding the
        # preset itself.
        "way_count": bound_preset.way_count if bound_preset is not None else None,
        "target": {
            "speaker_group_id": group_id,
            "role": role,
            "audible_outputs": sorted(audible_outputs),
            "audible_gain_db": audible_gain_db,
            "filter_mode": filter_mode,
        },
        "config": {
            "path": str(out_path),
            "basename": out_path.name,
            "exists": out_path.exists(),
            "playback_device": resolved_playback_device,
            "playback_device_source": playback_device_source,
            "classification": classification.get("classification"),
            "volume_limit_db": classification.get("volume_limit_db"),
            "volume_limit_ok": classification.get("volume_limit_ok"),
            "validation": validation,
        },
        "audible_evidence": audible_evidence,
        "load": {
            "load_allowed": False,
            "load_gate": "driver_commissioning_load_preflight_required",
            "next_step": (
                "Run the guarded per-driver commissioning load before CamillaDSP "
                "reloads this transient graph."
            ),
        },
        "required_gates": gates,
        "issues": issues,
    }
    logger.info(
        "event=active_speaker.driver_commission_prepared status=%s group=%s role=%s "
        "outputs=%s blockers=%d",
        status,
        group_id,
        role,
        sorted(audible_outputs),
        blocker_count,
    )
    return payload


def prepare_summed_commissioning_config(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    preset: ActiveSpeakerPreset | None = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    run_config_check: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Emit + safety-assert the combined-driver commissioning config.

    This is the first-class product boundary for the validation card's summed
    check: the active speaker's full driver set is audible through the same
    crossover, limiter, headroom, and protection graph used by per-driver
    commissioning. It deliberately does not load CamillaDSP; the guarded runtime
    load remains a separate startup-load operation.
    """

    return prepare_driver_commissioning_config(
        topology,
        speaker_group_id=speaker_group_id,
        role=SUMMED_COMMISSION_TARGET_ROLE,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=audible_gain_db,
        config_dir=config_dir,
        config_path=config_path,
        run_config_check=run_config_check,
        validate=validate,
        created_at=created_at,
    )
