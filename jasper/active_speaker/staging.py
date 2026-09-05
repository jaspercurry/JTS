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
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from jasper.atomic_io import advisory_file_lock, atomic_write_json
from jasper.camilla_config_contract import (
    DEFAULT_VOLUME_LIMIT_DB,
    read_camilla_devices_config,
)
from jasper.dsp_apply import CamillaConfigValidationResult, validate_camilla_config
from jasper.output_topology import (
    OutputTopology,
    SpeakerChannel,
    SpeakerGroup,
    main_speaker_groups,
    subwoofer_speaker_groups,
)

from ._common import gate as _gate, issue as _issue
from .camilla_yaml import (
    COMMISSIONING_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
    active_emit_devices,
    audible_outputs_for_role,
    capture_device_for_playback,
    emit_active_speaker_commissioning_config,
)
from ..fanin_coupling import RING_PCM_DEVICES, TRANSPORT_RING
from .crossover_preview import CROSSOVER_PREVIEW_KIND
from .declaration_vocabulary import (
    _normalise_filter_type,
    _slope_to_lr_order,
    supported_declaration_filter_types,
    supported_declaration_slopes_db_per_octave,
)
from .driver_protection import declared_protection_highpass_floor_hz
from .environment import classify_camilla_config_text
from .graph_evidence import (
    all_commission_mutes_engaged as _all_commission_mutes_engaged,
    driver_commission_audible_evidence,
    protective_highpass_hz as _protective_hp_hz,
    running_commission_evidence as running_commission_evidence,
    running_graph_matches_staged_anchor as running_graph_matches_staged_anchor,
    software_guard_evidence as _software_guard_evidence,
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
from .test_signal_plan import (
    declared_protection_floor_hz,
    strictest_crossover_highpass_hz,
)
from .tone_plan import load_active_speaker_preset

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
# ONE owner for the transport gate's identity (#2344). The preflight lifts this
# gate to the level consumers walk and the /sound/ renderer keys its household
# copy on it, so the id is shared rather than spelled three times.
COMMISSIONING_TRANSPORT_GATE_ID = "commissioning_transport_supported"
STAGED_CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH"
STAGED_METADATA_PATH_ENV = "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH"

# Bounded, never open-ended: this wait sits on a /sound/ web request and on the
# `baseline-reemit` CLI that deploys and operator ladder steps invoke.
# It exceeds the holder's longest bounded step -- one `camilladsp --check`
# inside :func:`~jasper.dsp_apply.validate_camilla_config`, which caps itself --
# so an ordinary overlap waits its turn instead of refusing, and a refusal
# means something genuinely abnormal is holding the pair.
STAGED_ANCHOR_LOCK_TIMEOUT_SEC = 15.0

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


class StagedAnchorLockContended(RuntimeError):
    """The staged startup anchor pair was held past the bounded wait."""


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


def staged_anchor_lock_path(config_path: str | Path) -> Path:
    """The one cross-process lock for the staged startup anchor PAIR.

    Derived from the GRAPH half's resolved path rather than read out of the
    environment a second time, so every writer that resolves the same pair
    locks the same file and a redirected pair (a test, the CLI's temp proof
    run) locks beside the artifact it is actually writing. Keyed on the graph
    half because that is the half living in the generated-config directory,
    where both writers already have write access; the lock covers BOTH halves.
    """

    path = Path(config_path)
    return path.with_name(f"{path.name}.lock")


def _staged_anchor_lock_holder(lock_path: Path) -> str:
    """Best-effort read of the stamp a holder left. Diagnostic only.

    Read without the lock (the reader is by definition the loser of the race),
    so a torn stamp is possible; it names a process for an operator, it is
    never a fact anything branches on.
    """

    try:
        with open(lock_path, "rb") as handle:
            stamp = handle.read(64).decode("utf-8", errors="replace").strip()
    except OSError:
        return "unknown"
    return stamp or "unknown"


def _stamp_staged_anchor_lock_holder(handle: Any, source: str) -> None:
    """Record who holds the lock, so a contention log can name them."""

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} {source}\n")
        handle.flush()
    except OSError:
        pass  # the stamp is diagnostic — never fail an acquired lock on it


@contextmanager
def staged_anchor_lock(
    config_path: str | Path,
    *,
    source: str,
    timeout_sec: float | None = None,
):
    """Serialize every writer of the staged startup anchor pair (#2518).

    The pair — the all-muted startup graph and the staged metadata that LOCATES
    it — has two writers: :func:`stage_protected_startup_config` (which every
    wizard route reaches) and the publish block of ``jasper-active-speaker
    baseline-reemit``. Neither writes the two halves in one filesystem
    operation, so interleaved runs could leave one graph's metadata over
    another's bytes. This lock is what makes each writer's pair atomic with
    respect to the other's.

    LOCK ORDERING: innermost, always. The driver-capture route already holds
    :func:`~jasper.dsp_apply.dsp_writer_lock` when it reaches the stager, so
    the only nesting is ``dsp writer -> staged anchor``. Nothing may acquire
    the DSP writer lock while holding this one, and nothing may re-enter this
    lock: ``flock`` is per open file description, so a second acquisition in
    the same process would block against itself rather than recurse.

    CRASH RELEASE IS THE POINT OF USING ``flock``. Both writers run in
    short-lived contexts that can be killed mid-write — a CLI invocation inside
    a deploy, a web request, either of them OOM-killed on a 1 GB Pi. The kernel
    drops an ``flock`` when the holding descriptor closes, including on process
    death, so a killed writer can never strand the anchor behind a lock nobody
    will release. A status file would need a liveness check to say the same
    thing.

    FAIL-OPEN ON AN UNOPENABLE LOCK FILE, at WARNING, mirroring
    ``jasper.fanin.coupling_reconcile._acquire_entry_lock``. The consequence of
    proceeding unserialized is bounded (stale evidence over a same-topology
    all-muted graph, never an audible change); the consequence of refusing is
    that a box cannot stage its boot anchor at all. The asymmetry decides it.

    Contention past the bounded wait raises :class:`StagedAnchorLockContended`;
    each writer translates that into its own refusal contract, and neither
    writes a byte of the pair.
    """

    # Opened by root (the `jasper-active-speaker` CLI) and by `jasper-web` (the
    # /sound/ wizard), so it takes the shared group-writable lock mode. Unlike
    # `.dsp_apply.lock` this lock never existed pre-upgrade, so it needs no
    # install-time ownership heal.
    lock_path = staged_anchor_lock_path(config_path)
    timeout = (
        STAGED_ANCHOR_LOCK_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    )
    started = time.monotonic()
    with ExitStack() as stack:
        handle = None
        try:
            handle = stack.enter_context(advisory_file_lock(
                lock_path,
                timeout_sec=timeout,
            ))
        # `TimeoutError` IS an `OSError` subclass, so the bounded-wait refusal
        # has to be caught FIRST — otherwise the fail-open arm below would
        # swallow a genuine contention and let both writers run at once.
        except TimeoutError:
            waited_ms = round((time.monotonic() - started) * 1000)
            holder = _staged_anchor_lock_holder(lock_path)
            logger.warning(
                "event=active_speaker.staged_anchor_lock_contended path=%s "
                "source=%s holder=%s waited_ms=%d timeout_ms=%d",
                lock_path,
                source,
                holder,
                waited_ms,
                round(timeout * 1000),
            )
            raise StagedAnchorLockContended(
                f"the staged startup anchor is held by {holder}; waited "
                f"{waited_ms} ms"
            ) from None
        except OSError as exc:
            logger.warning(
                "event=active_speaker.staged_anchor_lock_unavailable path=%s "
                "source=%s error=%s",
                lock_path,
                source,
                type(exc).__name__,
            )
        if handle is not None:
            _stamp_staged_anchor_lock_holder(handle, source)
        yield


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


def _driver_spec_from_preview(role: str, raw: Any) -> DriverSpec:
    driver = raw if isinstance(raw, dict) else {}
    model = str(driver.get("model") or role).strip() or role
    manufacturer = str(driver.get("manufacturer") or "Operator research").strip()
    try:
        # #1665: DriverSpec.sensitivity_db is a descriptive label (the staged
        # config's operator-facing card) with no level-affecting consumer --
        # confirmed by inspection, nothing downstream reads it. Left naked
        # (not folded through driver_pad.effective_sensitivity_db) on
        # purpose: baseline_profile._derive_corrections is the one place a
        # pad's attenuation must reach the actual trim math.
        sensitivity = driver.get("sensitivity_db_2v83_1m")
        sensitivity_db = float(sensitivity) if sensitivity is not None else None
    except (TypeError, ValueError):
        sensitivity_db = None
    return DriverSpec(
        role=role,
        manufacturer=manufacturer or "Operator research",
        model=model,
        sensitivity_db=sensitivity_db,
        # #2491: the preview driver payload already carries the confirmed
        # ``required_protection_filters``. This is the ONE point that reads it
        # into the preset, so the emitted graph and every verifier of that
        # graph clamp the derived tweeter protection to the same declared floor.
        protection_highpass_floor_hz=declared_protection_highpass_floor_hz(driver),
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
    sub_groups = subwoofer_speaker_groups(topology)
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
            # The vocabulary is READ, not spelled: this message names the same
            # sets the entry gate offers and refuses against, so 4.1 widens one
            # place and no reader is told the old answer here.
            issues.append(_issue(
                "blocker",
                "crossover_preview_filter_unsupported",
                f"preview filter for {lower_role}/{upper_role} is not one of: "
                + ", ".join(supported_declaration_filter_types())
                + " at "
                + ", ".join(
                    f"{slope:g}"
                    for slope in supported_declaration_slopes_db_per_octave()
                )
                + " dB/octave",
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
                escalation_step_db=1.0,
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
# nothing to feed it. Their 1-way preset is built directly from the saved
# topology below, NOT from a preview.
_PASSIVE_MAIN_ROLE = "full_range"


def build_passive_mains_preset(
    topology: OutputTopology,
) -> tuple[ActiveSpeakerPreset | None, list[dict[str, str]], list[dict[str, Any]]]:
    """Build the 1-way (passive full-range mains, optional local sub) preset.

    The passive analogue of :func:`_preset_from_crossover_preview`: a passive
    speaker has no active crossover preview to compile, so the mains — and the
    sub lane when the topology routes one — resolve directly from the saved
    topology. Fail-closed where a sub IS declared (an unresolvable one returns
    ``(None, issues, gates)``, never a mains-only graph that leaves the sub
    un-band-limited). A subless topology yields ``crossover_regions=()`` and
    ``local_subwoofer=None``. It does not write YAML, load CamillaDSP, or
    authorize playback.
    """
    issues: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []

    mains = main_speaker_groups(topology)
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
            "passive_mains_layout_unsupported",
            "passive mains must be one mono speaker or a left/right pair",
        ))
        gates.append(_gate(
            "passive_mains_layout",
            label="Passive mains layout is supported",
            passed=False,
            message="Passive mains must be one mono speaker or a left/right pair",
        ))
        return None, issues, gates
    gates.append(_gate(
        "passive_mains_layout",
        label="Passive mains layout is supported",
        passed=True,
        message=f"Passive {layout} mains can be routed through the roleful emitter",
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

    local_subwoofer = None
    if subwoofer_speaker_groups(topology):
        # The sub pins to the next contiguous channel after the mains (validated
        # against main_output_count) and carries the user-settable bass-mgmt corner.
        local_subwoofer, sub_issues = _local_subwoofer_from_topology(
            topology, main_output_count=len(outputs)
        )
        issues.extend(sub_issues)
        if local_subwoofer is None:
            # The topology DECLARES a sub, so a None here is the fail-closed
            # resolution rejecting it. Never emit a mains-only graph that drops a
            # declared sub — that leaves it un-band-limited or full-range.
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
            # The with-sub id keeps its historical spelling: it is banked on
            # candidates already measured, and a rename would read as a preset
            # mismatch at apply time.
            preset_id=(
                f"passive-sub-{_safe_stem(topology.topology_id)}"
                if local_subwoofer is not None
                else f"passive-{_safe_stem(topology.topology_id)}"
            ),
            name=(
                f"{topology.name} passive full-range"
                + (" + local sub" if local_subwoofer is not None else "")
            ),
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
                escalation_step_db=1.0,
            ),
            notes="Derived from a passive-mains topology; no inter-driver crossover.",
        )
        preset.validate()
    except ActiveSpeakerConfigError as exc:
        issues.append(_issue(
            "blocker",
            "passive_mains_preset_invalid",
            f"could not build a passive-mains preset: {exc}",
        ))
        return None, issues, gates

    gates.append(_gate(
        "passive_mains_compiled",
        label="Passive mains compiled to a routable intent",
        passed=True,
        message="Passive preset can be staged through the roleful emitter",
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
    subwoofer_groups = subwoofer_speaker_groups(topology)
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


def _record_generated_config_classification(
    yaml: str,
    *,
    candidate_gate_id: str,
    gates: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify one generated active graph and record its shared safety gates."""

    classification = classify_camilla_config_text(yaml)
    gates.append(_gate(
        candidate_gate_id,
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
    return classification


def _record_camilla_validation(
    validation: dict[str, Any],
    *,
    blocked_subject: str,
    failure_code: str,
    gates: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    """Fold CamillaDSP validation into the shared gate/issue vocabulary."""

    status = str(validation.get("status") or "unknown")
    validation_ok = status in {"valid", "missing"}
    if status not in {"skipped", "not_generated"}:
        gates.append(_gate(
            "camilla_syntax_preflight",
            label="Generated config passed CamillaDSP syntax preflight",
            passed=validation_ok,
            message=(
                f"Validation status is {status}"
                if validation_ok
                else f"CamillaDSP validation blocked the {blocked_subject}"
            ),
        ))
    if status not in {"valid", "missing", "skipped", "not_generated"}:
        issues.append(_issue(
            "blocker",
            failure_code,
            f"CamillaDSP validation status is {status}",
        ))


def _anchor_lock_contended_payload(
    topology: OutputTopology,
    *,
    out_path: Path,
    meta_path: Path,
    created_at: str,
    detail: str,
) -> dict[str, Any]:
    """The staging refusal for a pair another writer is publishing.

    Same envelope as an ordinary staged payload — ``status`` plus a blocker in
    ``issues`` — so every existing consumer's "did this stage?" branch already
    covers it and no caller learns a new failure vocabulary. Nothing on this
    path is written: a refusal that overwrote the metadata would be the very
    corruption the lock exists to prevent.
    """

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STAGED_STARTUP_CONFIG_KIND,
        "status": "blocked",
        "created_at": created_at,
        "metadata_path": str(meta_path),
        "preset": {
            "preset_id": None,
            "name": None,
            "way_count": None,
            "layout": None,
            "source": None,
        },
        "topology": {
            "topology_id": topology.topology_id,
            "name": topology.name,
            "speaker_group_id": None,
            "speaker_label": None,
            "speaker_group_ids": [],
            "speaker_labels": [],
        },
        "hardware": {
            "device_id": topology.hardware.device_id,
            "device_label": topology.hardware.device_label,
            "card_id": topology.hardware.card_id,
            "physical_output_count": topology.hardware.physical_output_count,
            "clock_domain_id": topology.hardware.clock_domain_id,
        },
        "targets": [],
        "config": {
            "path": str(out_path),
            "basename": out_path.name,
            "exists": out_path.exists(),
            "validation": {"status": "skipped", "reason": "not_generated"},
        },
        "software_guard": {},
        "load": {
            "load_allowed": False,
            "load_gate": "startup_load_preflight_required",
            "next_step": (
                "Run the guarded startup-load preflight before CamillaDSP is "
                "allowed to reload this staged graph."
            ),
        },
        "required_gates": [],
        "issues": [_issue("blocker", "staged_config_anchor_lock_contended", detail)],
        "next_step": (
            "Another writer is publishing the protected startup config. Wait "
            "for it to finish, then stage again."
        ),
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
    """Stage a muted/protected startup YAML and return versioned evidence.

    Every wizard route that stages the anchor arrives here, so this is where
    the pair's :func:`staged_anchor_lock` is taken for this writer: the graph
    and the metadata that locates it are published under one hold, and a
    contending run refuses without writing either half (#2518).
    """

    created_at = created_at or _utc_now()
    out_path = staged_config_path(config_dir=config_dir, path=config_path)
    meta_path = staged_metadata_path(metadata_path)
    try:
        with staged_anchor_lock(out_path, source="stage_protected_startup_config"):
            return _stage_protected_startup_config_locked(
                topology,
                preset=preset,
                crossover_preview=crossover_preview,
                playback_device=playback_device,
                out_path=out_path,
                meta_path=meta_path,
                run_config_check=run_config_check,
                validate=validate,
                created_at=created_at,
            )
    except StagedAnchorLockContended as exc:
        return _anchor_lock_contended_payload(
            topology,
            out_path=out_path,
            meta_path=meta_path,
            created_at=created_at,
            detail=str(exc),
        )


def _stage_protected_startup_config_locked(
    topology: OutputTopology,
    *,
    preset: ActiveSpeakerPreset | None,
    crossover_preview: dict[str, Any] | None,
    playback_device: str | None,
    out_path: Path,
    meta_path: Path,
    run_config_check: bool,
    validate: Callable[[str | Path], CamillaConfigValidationResult],
    created_at: str,
) -> dict[str, Any]:
    """Stage the anchor pair. Call only while holding :func:`staged_anchor_lock`."""

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

    validation: dict[str, Any] = {"status": "skipped", "reason": "not_generated"}
    classification: dict[str, Any] = {}
    software_guard: dict[str, Any] = {}
    software_guard_requested = _software_guard_requested_any(active_groups)
    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")

    # THE ANCHOR'S DEVICE BLOCK IS DERIVED, NOT DEFAULTED (#2364). This graph is
    # the box's durable BOOT anchor, so every half of its device contract has to
    # match the endpoint it names. Forwarding only the device NAME left the other
    # halves at the emitter's snd-aloop defaults, which on the ACTIVE ring is a
    # sink of `jts_ring_active_playback` over a capture of `plug:jasper_capture`
    # — the tap fan-in STOPS feeding under `shm_ring` — plus the program-lane
    # format and the loopback chunk/target/queue geometry. That is a graph that
    # names the right device and behaves like the wrong one: silence with every
    # daemon healthy, and quiet, because nothing downstream inspects transport
    # coherence (`build_startup_load_preflight`'s gates are about staging,
    # identity, protection and level, never the transport).
    #
    # `active_emit_devices` is the ONE derivation for "what does an emit against
    # THIS device have to declare", the same one `recompose_applied_baseline_yaml`
    # reads — so the anchor and the applied baseline now answer the endpoint
    # question in the same place instead of two. Non-ring devices get the
    # emitter's own defaults back, so this is byte-identical on every box that is
    # not armed.
    #
    # CROSS-BOOT SEMANTICS, stated because #2364 asked for them: the anchor names
    # whichever endpoint the box currently resolves, exactly as the applied
    # baseline does. `baseline-reemit --endpoint ring` moves it to the ring —
    # the ONE legal ACTIVE endpoint, and so the arm's only `--endpoint` choice,
    # with no "back" to move it to — and a re-stage in between re-derives from
    # the live marker rather than freezing a stale answer.
    devices = None
    if blocker_count == 0 and bound_preset and resolved_playback_device:
        # A ring wire token neither jasper-fanin nor JTS can resolve must reach
        # the operator as this function's ordinary blocker, not as a traceback
        # out of a wizard or the CLI. Mirrors the applied path's refusal, code
        # included, so one bad token reads the same wherever it surfaces.
        try:
            devices = active_emit_devices(resolved_playback_device, topology=topology)
        except ValueError as exc:
            issues.append(_issue(
                "blocker",
                "ring_wire_declaration_invalid",
                f"this box declares a ring wire neither jasper-fanin nor JTS can "
                f"resolve, so there is no wire to emit against: {exc}",
            ))
            blocker_count += 1

    if (
        blocker_count == 0
        and bound_preset
        and resolved_playback_device
        and devices is not None
    ):
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # Stage the production graph with an all-muted per-output mask
            # (audible_outputs=frozenset()). Validation now happens through the
            # real path: this same config is what later freezes as the durable
            # profile. Per-driver unmute is a transient runtime load, never the
            # frozen boot config — so the staged candidate is fully muted.
            #
            # Every device field is named EXPLICITLY, like the applied path's
            # emit: a field added to `ActiveEmitDevices` and not added here is
            # the subset-forwarding defect this block exists to close.
            emitted_config = emit_active_speaker_commissioning_config(
                bound_preset,
                playback_device=resolved_playback_device,
                capture_device=devices.capture_device,
                capture_format=devices.capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize,
                target_level=devices.target_level,
                queuelimit=devices.queuelimit,
                enable_rate_adjust=devices.enable_rate_adjust,
                audible_outputs=frozenset(),
                out_path=out_path,
                baseline_id=f"staged-{_safe_stem(topology.topology_id)}",
            )
            classification = _record_generated_config_classification(
                emitted_config,
                candidate_gate_id="generated_active_startup_candidate",
                gates=gates,
                issues=issues,
            )
            # Crash-recovery invariant: the staged boot config must start with
            # every active output muted. A reboot partway through commissioning
            # has to come up everything-muted, never a tweeter unmuted at level.
            fully_muted = _all_commission_mutes_engaged(
                emitted_config, preset=bound_preset
            )
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
                software_guard = _software_guard_evidence(
                    emitted_config, preset=bound_preset
                )
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

    _record_camilla_validation(
        validation,
        blocked_subject="staged config",
        failure_code="staged_config_validation_failed",
        gates=gates,
        issues=issues,
    )

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
            # #2491: the two facts the startup-load gate compares. Published by
            # the producer of the graph rather than re-derived by the gate, so
            # the refusal is about the config that was actually staged.
            "tweeter_crossover_highpass_hz": (
                strictest_crossover_highpass_hz(preset, "tweeter") if preset else None
            ),
            "tweeter_protection_floor_hz": (
                declared_protection_floor_hz(preset, "tweeter") if preset else None
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
        atomic_write_json(
            meta_path,
            payload,
            mode=0o640,
        )
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

    # THIS EMIT'S DEVICE BLOCK IS DERIVED, NOT DEFAULTED (#2412). The emitter
    # takes seven device fields beyond the sink NAME, and forwarding only the
    # name leaves all seven at its snd-aloop defaults: the `plug:jasper_capture`
    # tap, the program-lane formats, and the loopback chunk/target/queue
    # geometry. That is the subset-forwarding shape #2364 closed at the boot
    # anchor above, on the SAME emitter with the SAME contract, so this reads
    # the same one derivation — `active_emit_devices` owns "what does an emit
    # against THIS device have to declare" for every device, ring or not.
    #
    # OFF THE RING THE BYTES DO NOT MOVE. `active_emit_devices` hands back the
    # emitter's own defaults for every non-ring device, so every box that is not
    # armed emits what it emitted before. On a ring device it answers the ring
    # capture lane, the resolved wire format and the certified ring geometry —
    # which is the whole of what makes a ring emit carryable, and what the
    # transport gate below then proves over the artifact.
    devices = None
    emitted_config: str | None = None
    if blocker_count == 0 and bound_preset is not None and resolved_playback_device:
        # A ring wire token neither jasper-fanin nor JTS can resolve must reach
        # the operator as this function's ordinary blocker, not as a traceback
        # out of a wizard or the CLI. Mirrors the anchor's refusal, code
        # included, so one bad token reads the same wherever it surfaces.
        try:
            devices = active_emit_devices(resolved_playback_device, topology=topology)
        except ValueError as exc:
            issues.append(_issue(
                "blocker",
                "ring_wire_declaration_invalid",
                f"this box declares a ring wire neither jasper-fanin nor JTS can "
                f"resolve, so there is no wire to emit against: {exc}",
            ))
            blocker_count += 1

    if (
        blocker_count == 0
        and bound_preset is not None
        and resolved_playback_device
        and audible_outputs
        and devices is not None
    ):
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # Every device field is named EXPLICITLY, like the anchor's emit: a
            # field added to `ActiveEmitDevices` and not added here is the
            # subset-forwarding defect this block exists to close.
            emitted_config = emit_active_speaker_commissioning_config(
                bound_preset,
                playback_device=resolved_playback_device,
                capture_device=devices.capture_device,
                capture_format=devices.capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize,
                target_level=devices.target_level,
                queuelimit=devices.queuelimit,
                enable_rate_adjust=devices.enable_rate_adjust,
                audible_outputs=audible_outputs,
                audible_gain_db=audible_gain_db,
                volume_limit_db=volume_limit_db,
                startup_headroom_db=COMMISSIONING_HEADROOM_DB,
                out_path=out_path,
                baseline_id=f"commission-{_safe_stem(topology.topology_id)}-{role}",
                filter_mode=filter_mode,
            )
            classification = _record_generated_config_classification(
                emitted_config,
                candidate_gate_id="generated_active_commissioning_candidate",
                gates=gates,
                issues=issues,
            )
            # The per-driver protection-while-audible gate (the config-level
            # form of the Stage-5 "HP present before the tweeter is unmuted").
            audible_evidence = driver_commission_audible_evidence(
                emitted_config,
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

    # BOTH ENDS OF THIS GRAPH NAME ONE TRANSPORT (#2412). The gate id and its
    # label are unchanged, and so is the invariant they state; the predicate
    # that was supposed to test it is what changes. It used to be
    # `resolved_playback_device not in RING_PCM_DEVICES` — a refusal of the ring
    # outright, shipped by #2344 as a permanent contract on the owner's
    # 2026-08-12 #2254 ruling, and superseded by the owner's re-opening in
    # #2412. That predicate never tested the property its label names, and the
    # property is the one whose absence is the hazard: a graph whose SINK is the
    # ring while its SOURCE is still the snd-aloop tap. Under `shm_ring` fan-in
    # stops feeding that tap, so such a graph sweeps a device nobody writes and
    # the measurement records silence with every daemon healthy — "everything
    # green" being exactly what an operator cannot tell from success, which is
    # why this is a blocker and not a warning.
    #
    # A RE-READ PROOF, not a restatement. Both device fields came from ONE
    # `active_emit_devices` call above, so agreement is true by construction and
    # re-deriving it here would be a tautology. Reading the FILE back is what
    # makes it a proof about the artifact the loader will open, and it
    # complements — never replaces — `tests/test_ring_active_endpoint.py`'s
    # `dataclasses.fields(ActiveEmitDevices)` walk: the walk catches a call site
    # that DROPS a field, this catches one that forwards six of seven. Neither
    # implies the other, so both are required.
    #
    # THIS PROVES COHERENCE, NOT LIVENESS, and the gap is owned one altitude up.
    # A ring/ring graph on a loopback-coupled or unarmed box is self-consistent
    # and passes here. This function is a PURE BUILDER — it reads no daemon env,
    # and teaching it to would put a reconciler read inside a builder — while
    # `commission_load.build_driver_commission_load_preflight`'s
    # `commissioning_transport_armed` gate reads the live coupling and marker.
    # A config prepared on an unarmed box is harmless; a LOAD on one is the
    # silent sweep, so the live half stands where the load does.
    #
    # NO GRAPH, NO ENDS TO DISAGREE. When an earlier blocker stopped the emit
    # this passes rather than inventing a transport failure for a box no owner
    # refused — the same rule the preflight's mirror follows for an absent gate.
    # It cannot make an unproven graph loadable: that earlier blocker already
    # fails `status`, which fails the preflight's own `prepared` gate.
    if emitted_config is None:
        transport_ends_agree = True
        transport_message = "No commissioning graph was generated"
    else:
        emitted_devices = read_camilla_devices_config(out_path) or {}
        emitted_playback = emitted_devices.get("playback_device")
        emitted_capture = emitted_devices.get("capture_device")
        expected_capture = (
            capture_device_for_playback(emitted_playback)
            if isinstance(emitted_playback, str) and emitted_playback
            else None
        )
        transport_ends_agree = (
            expected_capture is not None and emitted_capture == expected_capture
        )
        transport_message = (
            f"Commissioning captures {emitted_capture} into {emitted_playback}"
            if transport_ends_agree
            else (
                f"This graph plays out of {emitted_playback} while capturing "
                f"{emitted_capture}; that output is carried by {expected_capture}"
            )
        )
    gates.append(_gate(
        COMMISSIONING_TRANSPORT_GATE_ID,
        label="Commissioning emits on a transport this graph can carry",
        passed=transport_ends_agree,
        message=transport_message,
    ))
    if not transport_ends_agree:
        issues.append(_issue(
            "blocker",
            "commissioning_transport_ends_disagree",
            "the commissioning graph names one transport where it plays out and "
            "a different one where it captures, so the sweep would excite a "
            "device nothing reads; no operator setting fixes this",
        ))

    _record_camilla_validation(
        validation,
        blocked_subject="commissioning config",
        failure_code="commissioning_config_validation_failed",
        gates=gates,
        issues=issues,
    )

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
    # THE TRANSPORT, ON THE LINE THAT NAMES THE ROLE (#2412). This line
    # already carried the role and the outputs and never the transport, so
    # Finding (C) — a commissioning graph whose sink was the ring while its
    # source was still the snd-aloop tap — was invisible in the journal even
    # though every fact needed to see it was in scope here. Four fields make it
    # one grep. No new event name: the commissioning vocabulary is stable and
    # fragmenting it would cost a line per transition.
    #
    # `wire` is read off the emitted block rather than re-deriving it from
    # `resolve_ring_wire(topology)`. Identical by construction — `active_emit_devices`
    # sets BOTH ring formats from that one call — but re-deriving would be a
    # second call that can raise `ValueError` on a bad wire token, which is the
    # blocker path this line has to stay readable on. The literal `-` (never an
    # empty value, which reads as "unknown") covers a non-ring emit, which has
    # no ring wire, and a blocked prepare, which has no emitted block at all.
    transport_is_ring = resolved_playback_device in RING_PCM_DEVICES
    logger.info(
        "event=active_speaker.driver_commission_prepared status=%s group=%s role=%s "
        "outputs=%s blockers=%d transport=%s capture=%s playback=%s wire=%s",
        status,
        group_id,
        role,
        sorted(audible_outputs),
        blocker_count,
        TRANSPORT_RING if transport_is_ring else "-",
        devices.capture_device if devices is not None else "-",
        resolved_playback_device or "-",
        (
            devices.capture_format
            if devices is not None and transport_is_ring
            else "-"
        ),
    )
    return payload


