"""Runtime safety contract for roleful active-speaker CamillaDSP graphs.

This flat graph maps full-range stereo directly to DAC outputs. It is
illegal when saved output topology assigns any physical output to
tweeter/protected role.

``jasper.output_topology`` owns the declarative physical-output contract.
This module owns the runtime question that follows from it: whether a
candidate or running CamillaDSP graph is legal for that exact saved topology,
and which graph install/reconcile paths may select when they need a safe
fallback. It is deliberately file-based and side-effect-free except for the
explicit statefile writer helper at the bottom.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from jasper.output_topology import (
    OutputTopology,
    SpeakerChannel,
    SpeakerGroup,
    load_output_topology_strict,
)

from ._common import issue as _issue
from .camilla_yaml import STARTUP_LIMITER_CLIP_LIMIT_DB, STARTUP_MUTE_GAIN_DB
from .graph_evidence import (
    driver_baseline_gain_name as _baseline_gain_name,
    driver_baseline_limiter_name as _baseline_limiter_name,
    driver_limiter_name,
    filter_params as _filter_params,
    filter_type as _filter_type,
    float_value as _float_value,
    output_commission_mute_name as _commission_mute_name,
    protective_tweeter_hp_name,
    truthy_bool as _truthy_bool,
)
from .graph_safety import (
    GraphView,
    filter_param_matches,
    pipeline_contains_chain,
    tweeter_guard_present,
    view_from_yaml_text,
)
from .environment import (
    DEFAULT_CAMILLA_STATEFILE,
    classify_camilla_config_text,
    parse_camilla_statefile_config_path,
)
from .path_safety import (
    software_guard_ready_for_startup,
    staged_target_signature,
    topology_target_signature,
)
from .staging import load_staged_startup_config

DEFAULT_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover.yml")
DEFAULT_LEGACY_FLAT_CONFIG = Path("/etc/camilladsp/v1.yml")

GRAPH_FLAT_FULL_RANGE = "flat_full_range"
GRAPH_ALL_MUTED_ACTIVE_STARTUP = "all_muted_active_startup"
GRAPH_GUARDED_COMMISSIONING = "guarded_commissioning"
GRAPH_APPROVED_ACTIVE_RUNTIME = "approved_active_runtime"
GRAPH_UNKNOWN = "unknown"
GRAPH_UNSAFE = "unsafe"

ACTIVE_BASELINE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config"
)

CONTRACT_UNCONFIGURED = "unconfigured"
CONTRACT_NORMAL_STEREO_FULL_RANGE = "normal_stereo_full_range"
CONTRACT_NORMAL_MONO_FULL_RANGE = "normal_mono_full_range"
CONTRACT_ACTIVE_MONO_2WAY = "active_mono_2way"
CONTRACT_ACTIVE_MONO_3WAY = "active_mono_3way"
CONTRACT_ACTIVE_STEREO_2WAY = "active_stereo_2way"
CONTRACT_ACTIVE_STEREO_3WAY = "active_stereo_3way"
CONTRACT_SUBWOOFER_PRESENT = "subwoofer_present"
CONTRACT_PROTECTED_OUTPUTS_PRESENT = "protected_outputs_present"
CONTRACT_UNKNOWN_OR_INVALID = "unknown_or_invalid"


@dataclass(frozen=True)
class OutputAssignment:
    speaker_group_id: str
    speaker_label: str
    speaker_kind: str
    speaker_mode: str
    role: str
    physical_output_index: int | None
    identity_verified: bool
    startup_muted: bool
    protection_required: bool
    protection_status: str

    @property
    def roleful(self) -> bool:
        return self.role != "full_range"

    @property
    def protected(self) -> bool:
        return self.role == "tweeter" or self.protection_required

    @property
    def output_label(self) -> str:
        if self.physical_output_index is None:
            return "unassigned DAC output"
        return f"DAC output {self.physical_output_index + 1}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_group_id": self.speaker_group_id,
            "speaker_label": self.speaker_label,
            "speaker_kind": self.speaker_kind,
            "speaker_mode": self.speaker_mode,
            "role": self.role,
            "physical_output_index": self.physical_output_index,
            "identity_verified": self.identity_verified,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
            "protection_status": self.protection_status,
            "roleful": self.roleful,
            "protected": self.protected,
        }


@dataclass(frozen=True)
class OutputContract:
    classification: str
    topology_configured: bool
    main_layout: str
    active_modes: tuple[str, ...] = ()
    subwoofer_present: bool = False
    protected_outputs_present: bool = False
    roleful_outputs_present: bool = False
    requires_roleful_graph: bool = False
    assignments: tuple[OutputAssignment, ...] = ()
    issues: tuple[dict[str, str], ...] = ()

    @property
    def roleful_assignments(self) -> tuple[OutputAssignment, ...]:
        return tuple(item for item in self.assignments if item.roleful)

    @property
    def protected_assignments(self) -> tuple[OutputAssignment, ...]:
        return tuple(item for item in self.assignments if item.protected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "topology_configured": self.topology_configured,
            "main_layout": self.main_layout,
            "active_modes": list(self.active_modes),
            "subwoofer_present": self.subwoofer_present,
            "protected_outputs_present": self.protected_outputs_present,
            "roleful_outputs_present": self.roleful_outputs_present,
            "requires_roleful_graph": self.requires_roleful_graph,
            "assignments": [item.to_dict() for item in self.assignments],
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class GraphSafety:
    classification: str
    allowed: bool
    config_path: str | None = None
    camilla_classification: str = "missing"
    playback_device: str | None = None
    playback_channels: int | None = None
    issues: tuple[dict[str, str], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "allowed": self.allowed,
            "config_path": self.config_path,
            "camilla_classification": self.camilla_classification,
            "playback_device": self.playback_device,
            "playback_channels": self.playback_channels,
            "issues": list(self.issues),
            "details": self.details,
        }


@dataclass(frozen=True)
class SafeGraphDecision:
    status: str
    selected_config_path: str | None
    reason: str
    topology_contract: OutputContract
    current_graph: GraphSafety | None = None
    preferred_graph: GraphSafety | None = None
    fallback_graph: GraphSafety | None = None
    issues: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "blocked" and self.selected_config_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_config_path": self.selected_config_path,
            "reason": self.reason,
            "ok": self.ok,
            "topology_contract": self.topology_contract.to_dict(),
            "current_graph": (
                self.current_graph.to_dict() if self.current_graph else None
            ),
            "preferred_graph": (
                self.preferred_graph.to_dict() if self.preferred_graph else None
            ),
            "fallback_graph": (
                self.fallback_graph.to_dict() if self.fallback_graph else None
            ),
            "issues": list(self.issues),
        }


def _assignments(topology: OutputTopology) -> tuple[OutputAssignment, ...]:
    out: list[OutputAssignment] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            out.append(_assignment(group, channel))
    return tuple(out)


def _assignment(group: SpeakerGroup, channel: SpeakerChannel) -> OutputAssignment:
    return OutputAssignment(
        speaker_group_id=group.id,
        speaker_label=group.label,
        speaker_kind=group.kind,
        speaker_mode=group.mode,
        role=channel.role,
        physical_output_index=channel.physical_output_index,
        identity_verified=bool(channel.identity_verified),
        startup_muted=bool(channel.startup_muted),
        protection_required=bool(channel.protection_required),
        protection_status=channel.protection_status,
    )


def _subwoofer_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    routed = set(topology.routing.subwoofer_group_ids)
    return [
        group
        for group in topology.speaker_groups
        if group.kind == "subwoofer" or group.mode == "subwoofer" or group.id in routed
    ]


def _main_layout(groups: Iterable[SpeakerGroup]) -> str:
    kinds = {group.kind for group in groups if group.kind != "subwoofer"}
    if "mono" in kinds:
        return "mono"
    if {"left", "right"} <= kinds:
        return "stereo"
    if not kinds:
        return "unconfigured"
    return "unknown"


def classify_output_contract(topology: OutputTopology) -> OutputContract:
    """Classify the saved output topology as the runtime safety contract."""

    assignments = _assignments(topology)
    roleful = tuple(item for item in assignments if item.roleful)
    protected = tuple(item for item in assignments if item.protected)
    active_groups = tuple(
        group for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"} and group.kind != "subwoofer"
    )
    subwoofers = _subwoofer_groups(topology)
    layout = _main_layout(topology.speaker_groups)
    active_modes = tuple(sorted({group.mode for group in active_groups}))
    issues = tuple(
        _issue(
            str(item.get("severity") or "blocker"),
            str(item.get("code") or "topology_issue"),
            str(item.get("message") or item.get("code") or "topology issue"),
        )
        for item in topology.evaluation().get("blockers", [])
        if isinstance(item, dict)
    )

    if not topology.speaker_groups:
        classification = CONTRACT_UNCONFIGURED
    elif subwoofers and not active_groups:
        classification = CONTRACT_SUBWOOFER_PRESENT
    elif not active_groups and protected:
        classification = CONTRACT_PROTECTED_OUTPUTS_PRESENT
    elif not active_groups:
        classification = (
            CONTRACT_NORMAL_STEREO_FULL_RANGE
            if layout == "stereo"
            else CONTRACT_NORMAL_MONO_FULL_RANGE
            if layout == "mono"
            else CONTRACT_UNKNOWN_OR_INVALID
        )
    elif layout == "mono" and active_modes == ("active_2_way",):
        classification = CONTRACT_ACTIVE_MONO_2WAY
    elif layout == "mono" and active_modes == ("active_3_way",):
        classification = CONTRACT_ACTIVE_MONO_3WAY
    elif layout == "stereo" and active_modes == ("active_2_way",):
        classification = CONTRACT_ACTIVE_STEREO_2WAY
    elif layout == "stereo" and active_modes == ("active_3_way",):
        classification = CONTRACT_ACTIVE_STEREO_3WAY
    else:
        classification = CONTRACT_UNKNOWN_OR_INVALID

    # Subwoofers are roleful even without tweeter protection: flat stereo
    # should not be selected as their fallback unless a later runtime contract
    # explicitly teaches JTS how to drive that topology safely.
    requires_roleful_graph = bool(roleful or protected or subwoofers)
    return OutputContract(
        classification=classification,
        topology_configured=bool(topology.speaker_groups),
        main_layout=layout,
        active_modes=active_modes,
        subwoofer_present=bool(subwoofers),
        protected_outputs_present=bool(protected),
        roleful_outputs_present=bool(roleful),
        requires_roleful_graph=requires_roleful_graph,
        assignments=assignments,
        issues=issues,
    )


def active_topology_requires_roleful_graph(topology: OutputTopology) -> bool:
    return classify_output_contract(topology).requires_roleful_graph


def _statefile_config_path(statefile_path: str | Path | None) -> str | None:
    path = Path(statefile_path or os.environ.get("JASPER_CAMILLA_STATEFILE") or DEFAULT_CAMILLA_STATEFILE)
    try:
        return parse_camilla_statefile_config_path(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _read_text(path: str | Path) -> tuple[str | None, dict[str, str] | None]:
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, _issue(
            "blocker",
            "camilla_config_unreadable",
            f"could not read CamillaDSP config {path}: {type(exc).__name__}",
        )


def _path_matches(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def _protected_output_detail(contract: OutputContract) -> str:
    targets = contract.protected_assignments or contract.roleful_assignments
    labels = [
        f"{item.output_label} ({item.role}{'/protected' if item.protected else ''})"
        for item in targets
    ]
    return ", ".join(labels) or "a roleful/protected output"


def _flat_graph_allowed(
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
) -> GraphSafety:
    issues: list[dict[str, str]] = []
    allowed = not contract.requires_roleful_graph
    playback_channels = summary.get("playback_channels")
    full_range_outputs = {
        item.physical_output_index
        for item in contract.assignments
        if item.role == "full_range" and item.physical_output_index is not None
    }
    if (
        allowed
        and contract.topology_configured
        and isinstance(playback_channels, int)
        and playback_channels > len(full_range_outputs)
    ):
        allowed = False
        issues.append(_issue(
            "blocker",
            "flat_full_range_graph_wider_than_topology",
            (
                f"flat full-range graph exposes {playback_channels} output "
                f"channels, but saved full-range topology assigns only "
                f"{len(full_range_outputs)} physical output(s)"
            ),
        ))
    if not allowed:
        if contract.requires_roleful_graph:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_illegal_for_roleful_topology",
                (
                    "Active speaker topology assigns "
                    f"{_protected_output_detail(contract)} to a roleful/protected role, "
                    "but Camilla is running a flat full-range graph. Normal playback "
                    "can send full-range signal to the protected driver. Load protected "
                    "active startup or disconnect/clear the topology."
                ),
            ))
    return GraphSafety(
        classification=GRAPH_FLAT_FULL_RANGE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            "contract_requires_roleful_graph": contract.requires_roleful_graph,
            "volume_limit_ok": bool(summary.get("volume_limit_ok")),
        },
    )


def _pipeline_names_for_channels(
    payload: dict[str, Any],
    *,
    channels: set[int],
) -> tuple[str, ...]:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    out: list[str] = []
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") != "Filter":
            continue
        raw_channels = step.get("channels")
        if not isinstance(raw_channels, list):
            continue
        try:
            step_channels = {int(channel) for channel in raw_channels}
        except (TypeError, ValueError):
            continue
        # A Camilla filter step may intentionally apply one role's baseline
        # chain to multiple outputs at once, for example both stereo woofers.
        # For per-output evidence we only need to prove that the requested
        # output is covered by the chain.
        if not channels.issubset(step_channels):
            continue
        out.extend(str(name) for name in step.get("names", []) if name is not None)
    return tuple(out)


def _commission_mute_states(view: GraphView) -> dict[int, bool]:
    """Map each ``as_out{N}_commission_mute`` filter's output index to its
    ``mute`` boolean, read from the shared view's parsed filters.

    The ``as_out{N}_commission_mute`` name pattern is runtime_contract-specific
    (``graph_safety``'s predicates take a single ``mute_name``, never a pattern),
    so the scan stays here — but it now reads the already-parsed
    ``GraphView.filters`` instead of re-walking the raw config dict.
    """
    out: dict[int, bool] = {}
    for name, fdef in view.filters.items():
        if not name.startswith("as_out") or not name.endswith("_commission_mute"):
            continue
        index_s = name.removeprefix("as_out").removesuffix("_commission_mute")
        try:
            index = int(index_s)
        except ValueError:
            continue
        out[index] = bool(fdef.params.get("mute"))
    return out


def _assignment_by_output(contract: OutputContract) -> dict[int, OutputAssignment]:
    out: dict[int, OutputAssignment] = {}
    for item in contract.assignments:
        if item.physical_output_index is not None and item.roleful:
            out[item.physical_output_index] = item
    return out


def _required_roleful_indexes(contract: OutputContract) -> set[int]:
    return {
        int(item.physical_output_index)
        for item in contract.roleful_assignments
        if item.physical_output_index is not None
    }


def _active_graph_evidence(
    text: str,
    contract: OutputContract,
    summary: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    # Keep the two distinct parse-error codes that view_from_yaml_text collapses
    # into parsed_ok=False: this module's callers branch on which one fired. We
    # parse once here for the precise code (and `payload` backs the baseline
    # path's filter accessors + subset pipeline-name lookup), then take the
    # normalised view from the shared adapter for the predicate calls below.
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        issues.append(_issue(
            "blocker",
            "camilla_yaml_unparseable",
            f"could not parse CamillaDSP YAML: {type(exc).__name__}",
        ))
        return {"issues": issues, "safe": False}
    if not isinstance(payload, dict):
        issues.append(_issue(
            "blocker",
            "camilla_yaml_not_object",
            "CamillaDSP YAML did not parse to an object",
        ))
        return {"issues": issues, "safe": False}
    view = view_from_yaml_text(text)

    required_indexes = _required_roleful_indexes(contract)
    by_output = _assignment_by_output(contract)
    required_count = max(required_indexes) + 1 if required_indexes else 0
    split = summary.get("active_split") if isinstance(summary.get("active_split"), dict) else {}
    split_channels = split.get("mixer_output_channels")
    if not required_indexes:
        issues.append(_issue(
            "blocker",
            "active_graph_without_roleful_topology",
            "active-speaker graph is loaded but saved topology has no roleful outputs",
        ))
    if split_channels != required_count:
        issues.append(_issue(
            "blocker",
            "active_graph_output_count_mismatch",
            (
                f"active graph exposes {split_channels or 'unknown'} output channels; "
                f"saved roleful topology requires {required_count}"
            ),
        ))

    mutes = _commission_mute_states(view)
    graph_indexes = (
        set(range(split_channels))
        if isinstance(split_channels, int) and split_channels >= 0
        else set(required_indexes)
    )
    missing_mutes = sorted(index for index in required_indexes if index not in mutes)
    if missing_mutes:
        source = str(summary.get("source") or "")
        if source != ACTIVE_BASELINE_SOURCE:
            issues.append(_issue(
                "blocker",
                "active_graph_missing_commission_mutes",
                "active graph is missing per-output mute filters for DAC outputs "
                + ", ".join(str(index + 1) for index in missing_mutes),
            ))
    weak_mutes = sorted(
        index for index in required_indexes
        if mutes.get(index) is True
        and not filter_param_matches(
            view,
            _commission_mute_name(index),
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB},
        )
    )
    if weak_mutes:
        issues.append(_issue(
            "blocker",
            "active_graph_commission_mute_not_hard_mute",
            "active graph mute filters are not at the expected hard-mute floor for DAC outputs "
            + ", ".join(str(index + 1) for index in weak_mutes),
        ))

    unwired_mutes = sorted(
        index for index in required_indexes
        if not pipeline_contains_chain(
            view,
            channels={index},
            required_names=(_commission_mute_name(index),),
        )
    )
    if unwired_mutes:
        source = str(summary.get("source") or "")
        if source != ACTIVE_BASELINE_SOURCE:
            issues.append(_issue(
                "blocker",
                "active_graph_unwired_commission_mutes",
                "active graph does not wire per-output mutes for DAC outputs "
                + ", ".join(str(index + 1) for index in unwired_mutes),
            ))

    is_baseline = str(summary.get("source") or "") == ACTIVE_BASELINE_SOURCE
    unmuted_outputs = (
        set(graph_indexes)
        if is_baseline
        else {
            index for index in graph_indexes
            if index in mutes and mutes[index] is False
        }
    )
    muted_outputs = {
        index for index in required_indexes
        if index in mutes and mutes[index] is True
    }
    all_muted = bool(required_indexes) and muted_outputs == required_indexes

    tweeter_outputs = {
        int(item.physical_output_index)
        for item in contract.protected_assignments
        if item.physical_output_index is not None and item.role == "tweeter"
    }
    if tweeter_outputs and not is_baseline:
        if not tweeter_guard_present(
            view,
            channels=tweeter_outputs,
            hp_name=protective_tweeter_hp_name("tweeter"),
            limiter_name=driver_limiter_name("tweeter"),
            limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
        ):
            issues.append(_issue(
                "blocker",
                "active_graph_tweeter_guard_missing",
                (
                    "active graph does not prove tweeter outputs are wrapped by "
                    "the protective high-pass and limiter"
                ),
            ))

    unmuted_roles = {
        by_output[index].role
        for index in unmuted_outputs
        if index in by_output
    }
    unknown_unmuted = sorted(index for index in unmuted_outputs if index not in by_output)
    if unknown_unmuted:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_unknown_outputs",
            "active graph unmutes outputs not assigned by the saved topology: "
            + ", ".join(str(index + 1) for index in unknown_unmuted),
        ))
    if len(unmuted_roles) > 1 and not is_baseline:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_multiple_roles",
            "guarded commissioning may unmute only one driver role at a time",
        ))
    if unmuted_outputs & tweeter_outputs and any(
        issue["code"] == "active_graph_tweeter_guard_missing" for issue in issues
    ):
        issues.append(_issue(
            "blocker",
            "active_graph_unprotected_tweeter_audible",
            "active graph unmutes a tweeter output without proving software protection",
        ))

    if is_baseline:
        if not pipeline_contains_chain(
            view,
            channels={0, 1},
            required_names=("active_baseline_headroom",),
        ):
            issues.append(_issue(
                "blocker",
                "active_baseline_headroom_unwired",
                "active baseline graph does not wire the shared headroom filter",
            ))
        headroom = _float_value(
            _filter_params(payload, "active_baseline_headroom").get("gain")
        )
        if headroom is None or headroom > 0.0:
            issues.append(_issue(
                "blocker",
                "active_baseline_headroom_invalid",
                "active baseline headroom gain is missing or positive",
            ))
        unknown_baseline_outputs = sorted(graph_indexes - required_indexes)
        if unknown_baseline_outputs:
            issues.append(_issue(
                "blocker",
                "active_baseline_routes_unknown_outputs",
                "active baseline routes outputs not assigned by the saved topology: "
                + ", ".join(str(index + 1) for index in unknown_baseline_outputs),
            ))
        for index in sorted(required_indexes):
            assignment = by_output.get(index)
            if assignment is None:
                continue
            role = assignment.role
            limiter_name = _baseline_limiter_name(role)
            gain_name = _baseline_gain_name(role)
            names = _pipeline_names_for_channels(payload, channels={index})
            if limiter_name not in names or gain_name not in names:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_driver_chain_missing",
                    (
                        "active baseline graph does not wire gain and limiter "
                        f"filters for DAC output {index + 1} ({role})"
                    ),
                ))
            limiter_params = _filter_params(payload, limiter_name)
            limiter_clip = _float_value(limiter_params.get("clip_limit"))
            if (
                _filter_type(payload, limiter_name) != "Limiter"
                or limiter_clip is None
                or limiter_clip > 0.0
                or not _truthy_bool(limiter_params.get("soft_clip"))
            ):
                issues.append(_issue(
                    "blocker",
                    "active_baseline_limiter_invalid",
                    (
                        "active baseline limiter is missing or unsafe for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            gain = _float_value(_filter_params(payload, gain_name).get("gain"))
            if gain is None or gain > 0.0:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_gain_positive",
                    (
                        "active baseline driver gain is missing or positive for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            if index in tweeter_outputs:
                highpass_names = [
                    name for name in names
                    if _filter_type(payload, name) == "BiquadCombo"
                    and str(_filter_params(payload, name).get("type") or "")
                    == "LinkwitzRileyHighpass"
                    and (_float_value(_filter_params(payload, name).get("freq")) or 0.0)
                    > 0.0
                ]
                if not highpass_names:
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_tweeter_highpass_missing",
                        (
                            "active baseline tweeter output is missing a "
                            f"wired high-pass filter on DAC output {index + 1}"
                        ),
                    ))

    return {
        "safe": not issues,
        "issues": issues,
        "required_outputs": sorted(required_indexes),
        "unmuted_outputs": sorted(unmuted_outputs),
        "muted_outputs": sorted(muted_outputs),
        "all_muted": all_muted,
        "baseline_candidate": is_baseline,
        "unmuted_roles": sorted(unmuted_roles),
        "tweeter_outputs": sorted(tweeter_outputs),
        "split_channels": split_channels,
    }


def _staged_path(staged_config: dict[str, Any] | None) -> str | None:
    config = staged_config.get("config") if isinstance(staged_config, dict) else None
    if not isinstance(config, dict):
        return None
    raw = config.get("path")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _staged_matches_topology(
    staged_config: dict[str, Any] | None,
    topology: OutputTopology,
) -> bool:
    if not isinstance(staged_config, dict) or staged_config.get("status") != "staged":
        return False
    staged_topology = staged_config.get("topology")
    staged_hardware = staged_config.get("hardware")
    if not isinstance(staged_topology, dict) or not isinstance(staged_hardware, dict):
        return False
    return all((
        staged_topology.get("topology_id") == topology.topology_id,
        staged_hardware.get("device_id") == topology.hardware.device_id,
        staged_hardware.get("card_id") == topology.hardware.card_id,
        staged_hardware.get("physical_output_count")
        == topology.hardware.physical_output_count,
        staged_hardware.get("clock_domain_id") == topology.hardware.clock_domain_id,
        staged_target_signature(staged_config) == topology_target_signature(topology),
    ))


def _active_graph_allowed(
    text: str,
    topology: OutputTopology,
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
    staged_config: dict[str, Any] | None,
) -> GraphSafety:
    evidence = _active_graph_evidence(text, contract, summary)
    issues = list(evidence.get("issues") or [])
    classification = GRAPH_UNSAFE
    if evidence.get("safe"):
        if evidence.get("baseline_candidate"):
            classification = GRAPH_APPROVED_ACTIVE_RUNTIME
        elif evidence.get("all_muted"):
            classification = GRAPH_ALL_MUTED_ACTIVE_STARTUP
        elif evidence.get("unmuted_outputs"):
            classification = GRAPH_GUARDED_COMMISSIONING
        else:
            classification = GRAPH_APPROVED_ACTIVE_RUNTIME

    staged_path = _staged_path(staged_config)
    staged_match = _staged_matches_topology(staged_config, topology)
    staged_guard_ready = (
        software_guard_ready_for_startup(topology, staged_config)
        if isinstance(staged_config, dict)
        else False
    )
    if (
        classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
        and staged_path
        and config_path
        and _path_matches(config_path, staged_path)
        and not staged_match
    ):
        issues.append(_issue(
            "blocker",
            "active_staged_metadata_mismatch",
            "all-muted active startup path no longer matches saved topology metadata",
        ))
    if (
        classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
        and staged_path
        and config_path
        and _path_matches(config_path, staged_path)
        and not staged_guard_ready
    ):
        issues.append(_issue(
            "blocker",
            "active_staged_guard_not_ready",
            "staged active startup metadata does not prove software guard readiness",
        ))

    allowed = classification in {
        GRAPH_ALL_MUTED_ACTIVE_STARTUP,
        GRAPH_GUARDED_COMMISSIONING,
        GRAPH_APPROVED_ACTIVE_RUNTIME,
    } and not issues
    return GraphSafety(
        classification=classification if allowed else GRAPH_UNSAFE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            **{k: v for k, v in evidence.items() if k not in {"issues", "safe"}},
            "staged_metadata_matches_topology": staged_match,
            "staged_guard_ready": staged_guard_ready,
        },
    )


def classify_camilla_graph(
    config_path: str | Path | None = None,
    topology: OutputTopology | None = None,
    *,
    text: str | None = None,
    staged_config: dict[str, Any] | None = None,
) -> GraphSafety:
    """Return whether a CamillaDSP graph is legal for the saved topology."""

    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    issues: list[dict[str, str]] = list(contract.issues)
    path_s = str(config_path) if config_path is not None else None
    if text is None and config_path is not None:
        text, read_issue = _read_text(config_path)
        if read_issue:
            issues.append(read_issue)
    if text is None:
        return GraphSafety(
            classification=GRAPH_UNKNOWN,
            allowed=False,
            config_path=path_s,
            issues=tuple(issues) or (
                _issue("blocker", "camilla_graph_missing", "no CamillaDSP graph was provided"),
            ),
        )

    summary = classify_camilla_config_text(text)
    for issue in summary.get("issues", []):
        if isinstance(issue, dict):
            issues.append(_issue(
                str(issue.get("severity") or "blocker"),
                str(issue.get("code") or "camilla_config_issue"),
                str(issue.get("message") or issue.get("code") or "CamillaDSP issue"),
            ))

    camilla_class = str(summary.get("classification") or "unknown")
    path_name = Path(path_s).name if path_s else ""
    is_flat = (
        camilla_class in {
            "jts_outputd_stereo",
            "jts_legacy_stereo",
            "jts_generated_stereo",
        }
        or path_name in {"outputd-cutover.yml", "v1.yml"}
    )
    if is_flat:
        graph = _flat_graph_allowed(contract, config_path=path_s, summary=summary)
    elif camilla_class == "active_startup_candidate":
        graph = _active_graph_allowed(
            text,
            topology,
            contract,
            config_path=path_s,
            summary=summary,
            staged_config=staged_config,
        )
    else:
        graph = GraphSafety(
            classification=GRAPH_UNKNOWN,
            allowed=False,
            config_path=path_s,
            camilla_classification=camilla_class,
            playback_device=summary.get("playback_device"),
            playback_channels=summary.get("playback_channels"),
            issues=(
                _issue(
                    "blocker",
                    "camilla_graph_unknown_for_runtime_contract",
                    "CamillaDSP graph is not a known flat or active-speaker graph",
                ),
            ),
            details={"volume_limit_ok": bool(summary.get("volume_limit_ok"))},
        )

    if issues and graph.allowed:
        return GraphSafety(
            classification=graph.classification,
            allowed=False,
            config_path=graph.config_path,
            camilla_classification=graph.camilla_classification,
            playback_device=graph.playback_device,
            playback_channels=graph.playback_channels,
            issues=tuple(issues) + graph.issues,
            details=graph.details,
        )
    if issues and not graph.allowed:
        return GraphSafety(
            classification=graph.classification,
            allowed=False,
            config_path=graph.config_path,
            camilla_classification=graph.camilla_classification,
            playback_device=graph.playback_device,
            playback_channels=graph.playback_channels,
            issues=tuple(issues) + graph.issues,
            details=graph.details,
        )
    return graph


def running_graph_violations(
    topology: OutputTopology,
    running_config_path: str | Path | None = None,
    *,
    text: str | None = None,
    staged_config: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    graph = classify_camilla_graph(
        running_config_path,
        topology,
        text=text,
        staged_config=staged_config,
    )
    return list(graph.issues) if not graph.allowed else []


def safe_graph_for_current_topology(
    topology: OutputTopology | None = None,
    *,
    statefile_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    preferred_config_path: str | Path | None = None,
    flat_config_path: str | Path = DEFAULT_FLAT_OUTPUTD_CONFIG,
    staged_config: dict[str, Any] | None = None,
) -> SafeGraphDecision:
    """Select the only safe persisted CamillaDSP graph for this topology."""

    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    staged_config = (
        staged_config if isinstance(staged_config, dict) else load_staged_startup_config()
    )

    current_path = str(current_config_path) if current_config_path else _statefile_config_path(statefile_path)
    current_graph = (
        classify_camilla_graph(current_path, topology, staged_config=staged_config)
        if current_path
        else None
    )
    preferred_path = str(preferred_config_path) if preferred_config_path else None
    preferred_graph = (
        classify_camilla_graph(preferred_path, topology, staged_config=staged_config)
        if preferred_path and not _path_matches(preferred_path, current_path)
        else None
    )
    if current_graph and current_graph.allowed and not contract.requires_roleful_graph:
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current CamillaDSP graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current approved active-speaker runtime graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        preferred_graph
        and preferred_graph.allowed
        and preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    ):
        return SafeGraphDecision(
            status="select_active_baseline",
            selected_config_path=preferred_path,
            reason="saved applied active-speaker baseline is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current all-muted active startup graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )

    if not contract.requires_roleful_graph:
        fallback = classify_camilla_graph(flat_config_path, topology, staged_config=staged_config)
        if fallback.allowed:
            return SafeGraphDecision(
                status="select_flat",
                selected_config_path=str(flat_config_path),
                reason="saved topology has no roleful/protected outputs",
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=fallback,
            )
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason="flat outputd fallback is unavailable or invalid",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=fallback,
            issues=fallback.issues,
        )

    staged_path = _staged_path(staged_config)
    staged_graph = (
        classify_camilla_graph(staged_path, topology, staged_config=staged_config)
        if staged_path
        else None
    )
    if (
        staged_graph
        and staged_graph.allowed
        and staged_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="select_active_startup",
            selected_config_path=staged_path,
            reason="roleful/protected topology requires the all-muted active startup graph",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=staged_graph,
        )

    issues: list[dict[str, str]] = []
    if current_graph and current_graph.issues:
        issues.extend(current_graph.issues)
    elif current_graph and current_graph.allowed:
        issues.append(_issue(
            "blocker",
            "current_graph_not_persistable",
            (
                f"{current_graph.classification} is legal only for an active "
                "session, not as a deploy/restart fallback"
            ),
        ))
    if preferred_graph and preferred_graph.issues:
        issues.extend(preferred_graph.issues)
    if staged_graph and staged_graph.issues:
        issues.extend(staged_graph.issues)
    if not staged_path:
        issues.append(_issue(
            "blocker",
            "active_startup_graph_missing",
            (
                "saved topology has roleful/protected outputs but no staged "
                "all-muted active startup graph is available"
            ),
        ))
    return SafeGraphDecision(
        status="blocked",
        selected_config_path=None,
        reason=(
            "roleful/protected topology has no legal all-muted active startup graph"
        ),
        topology_contract=contract,
        current_graph=current_graph,
        preferred_graph=preferred_graph,
        fallback_graph=staged_graph,
        issues=tuple(issues),
    )


def write_camilla_statefile(
    statefile_path: str | Path,
    config_path: str | Path,
    *,
    channel_slots: int = 5,
) -> None:
    """Write CamillaDSP's persisted config path with muted volume slots."""

    target = Path(statefile_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    slots = max(1, int(channel_slots))
    payload: dict[str, Any] = {}
    try:
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        existing = None
    if isinstance(existing, dict):
        payload.update(existing)
    payload["config_path"] = str(config_path)
    if "mute" not in payload:
        payload["mute"] = [False] * slots
    if "volume" not in payload:
        payload["volume"] = [0.0] * slots
    ordered = {"config_path": payload.pop("config_path")}
    ordered.update(payload)
    target.write_text(yaml.safe_dump(ordered, sort_keys=False), encoding="utf-8")
    os.chmod(target, 0o644)


def apply_safe_graph_decision_to_statefile(
    decision: SafeGraphDecision,
    *,
    statefile_path: str | Path = DEFAULT_CAMILLA_STATEFILE,
) -> bool:
    """Persist the selected graph if the statefile is absent or needs repair."""

    if not decision.ok or not decision.selected_config_path:
        return False
    current = _statefile_config_path(statefile_path)
    if _path_matches(current, decision.selected_config_path):
        return False
    write_camilla_statefile(statefile_path, decision.selected_config_path)
    return True
