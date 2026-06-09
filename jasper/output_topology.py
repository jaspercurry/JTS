"""Versioned speaker output topology contract.

This module is the product-grade boundary between physical DAC lanes and the
speaker/driver roles JTS will eventually feed through CamillaDSP. It is not an
ALSA route renderer and it has no audio side effects: no playback, no CamillaDSP
reload, and no hardware mutation. The current DAC8x ``JASPER_OUTPUT_DAC_ROUTE``
knob remains a narrow final-output alias; this model is where speaker groups,
active/passive modes, subwoofers, and verified physical output ownership live.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_hardware.dac import all_profiles, by_id

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OUTPUT_TOPOLOGY_KIND = "jts_output_topology"
CHANNEL_IDENTITY_REPORT_KIND = "jts_output_channel_identity_report"
CLOCK_DOMAIN_REPORT_KIND = "jts_output_clock_domain_report"
OUTPUT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"

SUPPORTED_DEVICE_OUTPUT_COUNTS = {
    profile.id: profile.physical_output_count for profile in all_profiles()
}
SUPPORTED_GROUP_KINDS = {"left", "right", "mono", "subwoofer"}
SUPPORTED_GROUP_MODES = {
    "full_range_passive",
    "active_2_way",
    "active_3_way",
    "subwoofer",
}
REQUIRED_ROLES_BY_MODE = {
    "full_range_passive": ("full_range",),
    "active_2_way": ("woofer", "tweeter"),
    "active_3_way": ("woofer", "mid", "tweeter"),
    "subwoofer": ("subwoofer",),
}
SUPPORTED_ROLES = {
    role for roles in REQUIRED_ROLES_BY_MODE.values() for role in roles
}
PROTECTION_STATUSES = {
    "not_required",
    "required_missing",
    "present",
    "software_guard_requested",
    "unknown",
}
OUTPUT_STATES = {"unused", "assigned", "verified", "blocked"}
TOPOLOGY_STATUSES = {"draft", "valid", "blocked", "verified"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class OutputTopologyError(ValueError):
    """Raised when output topology JSON has an unsupported shape."""


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _require_mapping(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise OutputTopologyError(f"{field_name} must be an object")
    return raw


def _sequence(raw: Any, field_name: str) -> list[Any]:
    if not isinstance(raw, list):
        raise OutputTopologyError(f"{field_name} must be a list")
    return raw


def _require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutputTopologyError(f"{field_name} is required")
    out = value.strip()
    if not _ID_RE.match(out):
        raise OutputTopologyError(
            f"{field_name} must be <=80 chars and contain only safe id chars"
        )
    return out


def _optional_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _require_id(value, "optional id")


def _text(value: Any, field_name: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise OutputTopologyError(f"{field_name} is required")
    out = " ".join(value.split())
    if len(out) > 120:
        raise OutputTopologyError(f"{field_name} must be <=120 chars")
    return out


def _int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise OutputTopologyError(f"{field_name} must be an integer") from e


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    return _int(value, field_name)


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _enum(value: Any, field_name: str, supported: set[str]) -> str:
    if not isinstance(value, str):
        raise OutputTopologyError(f"{field_name} must be a string")
    token = value.strip()
    if token not in supported:
        raise OutputTopologyError(f"{field_name} is unsupported: {token}")
    return token


def _float(value: Any, field_name: str, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError) as e:
        raise OutputTopologyError(f"{field_name} must be numeric") from e
    if not math.isfinite(out):
        raise OutputTopologyError(f"{field_name} must be finite")
    return out


def _safe_id_fragment(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
    return out[:64] or "unknown"


def default_clock_domain_id(device_id: str, card_id: str | None = None) -> str:
    """Return the current single-device clock-domain id.

    JTS does not yet aggregate multiple output DACs. This id records the
    current assumption explicitly so future multi-device work has a contract
    to replace rather than reverse-engineer from labels.
    """

    if card_id:
        return f"alsa:{_safe_id_fragment(card_id)}"
    if device_id:
        return f"device:{_safe_id_fragment(device_id)}"
    return "unknown"


def default_clock_domain_label(device_id: str) -> str:
    profile = by_id(device_id)
    return profile.clock_domain_label if profile else "Single output device clock"


@dataclass(frozen=True)
class PhysicalOutput:
    """One physical DAC lane visible to the user."""

    index: int
    human_label: str
    terminal_label: str
    state: str = "unused"

    @classmethod
    def from_mapping(cls, raw: Any) -> "PhysicalOutput":
        raw = _require_mapping(raw, "hardware.outputs[]")
        index = _int(raw.get("index"), "hardware.outputs[].index")
        if index < 0:
            raise OutputTopologyError("physical output index must be >= 0")
        return cls(
            index=index,
            human_label=_text(
                raw.get("human_label"),
                "hardware.outputs[].human_label",
                default=f"Output {index + 1}",
            ),
            terminal_label=_text(
                raw.get("terminal_label"),
                "hardware.outputs[].terminal_label",
                default=str(index + 1),
            ),
            state=_enum(
                raw.get("state", "unused"),
                "hardware.outputs[].state",
                OUTPUT_STATES,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "human_label": self.human_label,
            "terminal_label": self.terminal_label,
            "state": self.state,
        }


@dataclass(frozen=True)
class OutputHardware:
    """Detected or selected output device and its physical lanes."""

    device_id: str
    device_label: str
    physical_output_count: int
    card_id: str | None = None
    route: str | None = None
    clock_domain_id: str = "unknown"
    clock_domain_label: str = "Single output device clock"
    outputs: tuple[PhysicalOutput, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputHardware":
        raw = _require_mapping(raw, "hardware")
        count = _int(
            raw.get("physical_output_count"),
            "hardware.physical_output_count",
        )
        if count < 0 or count > 64:
            raise OutputTopologyError("physical_output_count must be 0-64")
        device_id = _require_id(raw.get("device_id"), "hardware.device_id")
        card_id = _optional_id(raw.get("card_id"))
        clock_domain_id = _require_id(
            raw.get("clock_domain_id")
            or default_clock_domain_id(device_id, card_id),
            "hardware.clock_domain_id",
        )
        outputs_raw = raw.get("outputs")
        outputs = (
            tuple(
                PhysicalOutput.from_mapping(item)
                for item in _sequence(outputs_raw, "hardware.outputs")
            )
            if outputs_raw is not None
            else default_physical_outputs(count)
        )
        hardware = cls(
            device_id=device_id,
            device_label=_text(raw.get("device_label"), "hardware.device_label"),
            physical_output_count=count,
            card_id=card_id,
            route=raw.get("route") if isinstance(raw.get("route"), str) else None,
            clock_domain_id=clock_domain_id,
            clock_domain_label=_text(
                raw.get("clock_domain_label"),
                "hardware.clock_domain_label",
                default=default_clock_domain_label(device_id),
            ),
            outputs=outputs,
        )
        hardware.validate()
        return hardware

    def validate(self) -> None:
        seen: set[int] = set()
        for output in self.outputs:
            if output.index in seen:
                raise OutputTopologyError(f"duplicate physical output {output.index}")
            seen.add(output.index)
            if output.index >= self.physical_output_count:
                raise OutputTopologyError(
                    f"physical output {output.index} exceeds device output count"
                )
        expected = set(range(self.physical_output_count))
        if seen != expected:
            raise OutputTopologyError("hardware outputs must cover every physical lane")

    def output_label(self, index: int | None) -> str | None:
        if index is None:
            return None
        for output in self.outputs:
            if output.index == index:
                return output.human_label
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "device_id": self.device_id,
            "device_label": self.device_label,
            "physical_output_count": self.physical_output_count,
            "clock_domain_id": self.clock_domain_id,
            "clock_domain_label": self.clock_domain_label,
            "outputs": [output.to_dict() for output in self.outputs],
        }
        if self.card_id:
            out["card_id"] = self.card_id
        if self.route:
            out["route"] = self.route
        return out


@dataclass(frozen=True)
class SpeakerPosition:
    """Approximate user-facing speaker placement in a top-down layout."""

    x: float = 0.0
    y: float = 0.0
    rotation_degrees: float = 0.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "SpeakerPosition":
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            x=_float(raw.get("x"), "position.x"),
            y=_float(raw.get("y"), "position.y"),
            rotation_degrees=_float(
                raw.get("rotation_degrees"), "position.rotation_degrees"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "rotation_degrees": round(self.rotation_degrees, 2),
        }


@dataclass(frozen=True)
class SpeakerChannel:
    """One speaker role assigned to an optional physical output."""

    role: str
    physical_output_index: int | None = None
    human_output_label: str | None = None
    identity_verified: bool = False
    startup_muted: bool = True
    protection_required: bool = False
    protection_status: str = "not_required"

    @classmethod
    def from_mapping(cls, raw: Any) -> "SpeakerChannel":
        raw = _require_mapping(raw, "speaker_groups[].channels[]")
        role = _enum(
            raw.get("role"),
            "speaker_groups[].channels[].role",
            SUPPORTED_ROLES,
        )
        protection_required = _bool(
            raw.get("protection_required"),
            role == "tweeter",
        )
        protection_status = _enum(
            raw.get(
                "protection_status",
                "required_missing" if protection_required else "not_required",
            ),
            "speaker_groups[].channels[].protection_status",
            PROTECTION_STATUSES,
        )
        return cls(
            role=role,
            physical_output_index=_optional_int(
                raw.get("physical_output_index"),
                "speaker_groups[].channels[].physical_output_index",
            ),
            # Display labels are derived from hardware.outputs after the full
            # topology is parsed. Treat client-provided labels as stale UI
            # hints, never as persisted truth about physical wiring.
            human_output_label=None,
            identity_verified=_bool(raw.get("identity_verified"), False),
            startup_muted=_bool(raw.get("startup_muted"), True),
            protection_required=protection_required,
            protection_status=protection_status,
        )

    def with_output_label(self, label: str | None) -> "SpeakerChannel":
        if label is None:
            return self
        return SpeakerChannel(
            role=self.role,
            physical_output_index=self.physical_output_index,
            human_output_label=label,
            identity_verified=self.identity_verified,
            startup_muted=self.startup_muted,
            protection_required=self.protection_required,
            protection_status=self.protection_status,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "physical_output_index": self.physical_output_index,
            "identity_verified": self.identity_verified,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
            "protection_status": self.protection_status,
        }
        if self.human_output_label:
            out["human_output_label"] = self.human_output_label
        return out


@dataclass(frozen=True)
class SpeakerGroup:
    """One logical speaker or subwoofer group."""

    id: str
    label: str
    kind: str
    mode: str
    position: SpeakerPosition = field(default_factory=SpeakerPosition)
    channels: tuple[SpeakerChannel, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, raw: Any) -> "SpeakerGroup":
        raw = _require_mapping(raw, "speaker_groups[]")
        return cls(
            id=_require_id(raw.get("id"), "speaker_groups[].id"),
            label=_text(raw.get("label"), "speaker_groups[].label"),
            kind=_enum(
                raw.get("kind"),
                "speaker_groups[].kind",
                SUPPORTED_GROUP_KINDS,
            ),
            mode=_enum(
                raw.get("mode"),
                "speaker_groups[].mode",
                SUPPORTED_GROUP_MODES,
            ),
            position=SpeakerPosition.from_mapping(raw.get("position")),
            channels=tuple(
                SpeakerChannel.from_mapping(item)
                for item in _sequence(
                    raw.get("channels", []),
                    "speaker_groups[].channels",
                )
            ),
        )

    def channels_with_output_labels(self, hardware: OutputHardware) -> "SpeakerGroup":
        return SpeakerGroup(
            id=self.id,
            label=self.label,
            kind=self.kind,
            mode=self.mode,
            position=self.position,
            channels=tuple(
                channel.with_output_label(
                    hardware.output_label(channel.physical_output_index)
                )
                for channel in self.channels
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "mode": self.mode,
            "position": self.position.to_dict(),
            "channels": [channel.to_dict() for channel in self.channels],
        }


@dataclass(frozen=True)
class TopologyRouting:
    """Main listening groups and optional subwoofer groups."""

    main_left_group_id: str | None = None
    main_right_group_id: str | None = None
    mono_group_id: str | None = None
    subwoofer_group_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, raw: Any) -> "TopologyRouting":
        raw = raw if isinstance(raw, Mapping) else {}
        subs = raw.get("subwoofer_group_ids", [])
        return cls(
            main_left_group_id=_optional_id(raw.get("main_left_group_id")),
            main_right_group_id=_optional_id(raw.get("main_right_group_id")),
            mono_group_id=_optional_id(raw.get("mono_group_id")),
            subwoofer_group_ids=tuple(
                _require_id(item, "subwoofer_group_ids[]")
                for item in _sequence(subs, "subwoofer_group_ids")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "main_left_group_id": self.main_left_group_id,
            "main_right_group_id": self.main_right_group_id,
            "mono_group_id": self.mono_group_id,
            "subwoofer_group_ids": list(self.subwoofer_group_ids),
        }


@dataclass(frozen=True)
class OutputTopology:
    """Persisted speaker topology draft or verified configuration."""

    topology_id: str
    name: str
    hardware: OutputHardware
    speaker_groups: tuple[SpeakerGroup, ...] = field(default_factory=tuple)
    routing: TopologyRouting = field(default_factory=TopologyRouting)
    status: str = "draft"

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputTopology":
        raw = _require_mapping(raw, "output_topology")
        if raw.get("artifact_schema_version") != SCHEMA_VERSION:
            raise OutputTopologyError("unsupported output topology schema version")
        if raw.get("kind") != OUTPUT_TOPOLOGY_KIND:
            raise OutputTopologyError("unsupported output topology kind")
        hardware = OutputHardware.from_mapping(raw.get("hardware"))
        topology = cls(
            topology_id=_require_id(raw.get("topology_id"), "topology_id"),
            name=_text(raw.get("name"), "name"),
            hardware=hardware,
            speaker_groups=tuple(
                SpeakerGroup.from_mapping(item).channels_with_output_labels(hardware)
                for item in _sequence(raw.get("speaker_groups", []), "speaker_groups")
            ),
            routing=TopologyRouting.from_mapping(raw.get("routing")),
            status=_enum(raw.get("status", "draft"), "status", TOPOLOGY_STATUSES),
        )
        topology._validate_references()
        return topology

    def _validate_references(self) -> None:
        group_ids: set[str] = set()
        for group in self.speaker_groups:
            if group.id in group_ids:
                raise OutputTopologyError(f"duplicate speaker group id {group.id}")
            group_ids.add(group.id)
            for channel in group.channels:
                index = channel.physical_output_index
                if index is not None and (
                    index < 0 or index >= self.hardware.physical_output_count
                ):
                    raise OutputTopologyError(
                        f"physical output {index} is outside hardware range"
                    )
        for field_name, group_id in (
            ("main_left_group_id", self.routing.main_left_group_id),
            ("main_right_group_id", self.routing.main_right_group_id),
            ("mono_group_id", self.routing.mono_group_id),
        ):
            if group_id and group_id not in group_ids:
                raise OutputTopologyError(
                    f"routing.{field_name} references unknown group"
                )
        for group_id in self.routing.subwoofer_group_ids:
            if group_id not in group_ids:
                raise OutputTopologyError(
                    "routing.subwoofer_group_ids references unknown group"
                )

    def evaluation(self) -> dict[str, Any]:
        return evaluate_output_topology(self)

    def to_dict(self, *, include_evaluation: bool = False) -> dict[str, Any]:
        evaluation = self.evaluation()
        out: dict[str, Any] = {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": self.topology_id,
            "name": self.name,
            "status": evaluation["status"],
            "hardware": self.hardware.to_dict(),
            "speaker_groups": [group.to_dict() for group in self.speaker_groups],
            "routing": self.routing.to_dict(),
            "safety": evaluation["safety"],
        }
        if include_evaluation:
            out["evaluation"] = evaluation
        return out


def default_physical_outputs(count: int) -> tuple[PhysicalOutput, ...]:
    return tuple(
        PhysicalOutput(
            index=index,
            human_label=f"DAC output {index + 1}",
            terminal_label=str(index + 1),
        )
        for index in range(count)
    )


def hardware_from_env(env: Mapping[str, str] | None = None) -> OutputHardware:
    """Build read-only hardware inventory from reconciler-owned env facts."""

    env = env or os.environ
    device_id = env.get("JASPER_AUDIO_DAC_ID") or "unknown"
    card_id = env.get("JASPER_AUDIO_DAC_CARD") or None
    route = env.get("JASPER_OUTPUT_DAC_ROUTE") or None
    profile = by_id(device_id)
    output_count = profile.physical_output_count if profile else 0
    if output_count <= 0:
        output_count = 2 if card_id else 0
    device_label = (
        profile.label
        if profile
        else device_id if device_id != "unknown" else "Unknown output device"
    )
    return OutputHardware(
        device_id=device_id,
        device_label=device_label,
        physical_output_count=output_count,
        card_id=card_id,
        route=route,
        clock_domain_id=default_clock_domain_id(device_id, card_id),
        clock_domain_label=default_clock_domain_label(device_id),
        outputs=default_physical_outputs(output_count),
    )


def new_topology_draft(
    *,
    topology_id: str = "default",
    name: str = "Speaker outputs",
    hardware: OutputHardware | None = None,
) -> OutputTopology:
    return OutputTopology(
        topology_id=topology_id,
        name=name,
        hardware=hardware or hardware_from_env(),
        speaker_groups=(),
        routing=TopologyRouting(),
        status="draft",
    )


def evaluate_output_topology(topology: OutputTopology) -> dict[str, Any]:
    """Return deterministic safety/validity evidence for a topology."""

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    assigned: dict[int, tuple[str, str]] = {}

    if not topology.speaker_groups:
        warnings.append(
            _issue("warning", "no_speaker_groups", "no speaker groups are configured")
        )

    for group in topology.speaker_groups:
        required_roles = set(REQUIRED_ROLES_BY_MODE[group.mode])
        actual_roles = [channel.role for channel in group.channels]
        actual_role_set = set(actual_roles)
        if actual_role_set != required_roles or len(actual_roles) != len(actual_role_set):
            blockers.append(
                _issue(
                    "blocker",
                    "mode_role_mismatch",
                    f"{group.label} must have exactly {sorted(required_roles)}",
                )
            )
        if group.kind == "subwoofer" and group.mode != "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_mode_mismatch",
                    f"{group.label} is a subwoofer group but mode is {group.mode}",
                )
            )
        if group.kind != "subwoofer" and group.mode == "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_group_required",
                    f"{group.label} uses subwoofer mode but is not a subwoofer group",
                )
            )
        for channel in group.channels:
            output_index = channel.physical_output_index
            if output_index is None:
                blockers.append(
                    _issue(
                        "blocker",
                        "physical_output_unassigned",
                        f"{group.label} {channel.role} is not assigned to a DAC output",
                    )
                )
                continue
            previous = assigned.get(output_index)
            if previous:
                blockers.append(
                    _issue(
                        "blocker",
                        "duplicate_physical_output",
                        f"DAC output {output_index + 1} is assigned to both "
                        f"{previous[0]}/{previous[1]} and {group.id}/{channel.role}",
                    )
                )
            else:
                assigned[output_index] = (group.id, channel.role)
            if not channel.identity_verified:
                warnings.append(
                    _issue(
                        "warning",
                        "identity_unverified",
                        f"{group.label} {channel.role} output identity is not verified",
                    )
                )
            if channel.role == "tweeter":
                if not channel.startup_muted:
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_must_start_muted",
                            f"{group.label} tweeter must start muted",
                        )
                    )
                if not channel.protection_required:
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_protection_not_required",
                            f"{group.label} tweeter must require protection",
                        )
                    )
                if channel.protection_status == "software_guard_requested":
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_software_guard_requested",
                            (
                                f"{group.label} tweeter software guard is requested; "
                                "stage and review a protected startup config before "
                                "any load or playback"
                            ),
                        )
                    )
                elif channel.protection_status != "present":
                    blockers.append(
                        _issue(
                            "blocker",
                            "tweeter_protection_unverified",
                            f"{group.label} tweeter protection must be marked present",
                        )
                    )

    group_ids = {group.id for group in topology.speaker_groups}
    if topology.routing.main_left_group_id and topology.routing.main_left_group_id not in group_ids:
        blockers.append(_issue("blocker", "left_group_missing", "left routing group is missing"))
    if topology.routing.main_right_group_id and topology.routing.main_right_group_id not in group_ids:
        blockers.append(_issue("blocker", "right_group_missing", "right routing group is missing"))
    for sub_id in topology.routing.subwoofer_group_ids:
        group = next((item for item in topology.speaker_groups if item.id == sub_id), None)
        if group and group.kind != "subwoofer":
            blockers.append(
                _issue(
                    "blocker",
                    "subwoofer_route_kind_mismatch",
                    f"routing subwoofer {sub_id} is not a subwoofer group",
                )
            )

    verified = bool(topology.speaker_groups) and not blockers and all(
        channel.identity_verified
        for group in topology.speaker_groups
        for channel in group.channels
    )
    status = "blocked" if blockers else ("verified" if verified else "valid")
    if not topology.speaker_groups:
        status = "draft"
    warning_codes = {issue["code"] for issue in warnings}
    if status == "draft":
        next_step = "Create speaker groups and verify physical output identity."
    elif blockers:
        next_step = "Resolve blockers before any sound test can be prepared."
    elif "identity_unverified" in warning_codes:
        next_step = "Verify physical output identity before preparing sound tests."
    else:
        next_step = "Topology is saved; sound tests still require a separate safe session."

    return {
        "status": status,
        "assigned_output_count": len(assigned),
        "unused_output_count": max(
            0,
            topology.hardware.physical_output_count - len(assigned),
        ),
        "blockers": blockers,
        "warnings": warnings,
        "safety": {
            "sound_tests_allowed": False,
            "requires_identity_verification": True,
            "requires_tweeter_protection": any(
                channel.role == "tweeter"
                for group in topology.speaker_groups
                for channel in group.channels
            ),
            "blockers": blockers,
            "warnings": warnings,
            "next_step": next_step,
        },
    }


def channel_identity_report(topology: OutputTopology) -> dict[str, Any]:
    """Return user-confirmed physical channel identity progress.

    This report is deliberately narrower than ``evaluate_output_topology``:
    it answers "which assigned DAC lane does the operator still need to
    physically verify?" It does not authorize playback or infer that tweeter
    protection is safe merely because identity was confirmed.
    """

    targets: list[dict[str, Any]] = []
    verified_count = 0
    assigned_count = 0
    for group in topology.speaker_groups:
        for channel in group.channels:
            assigned = channel.physical_output_index is not None
            if assigned:
                assigned_count += 1
            if assigned and channel.identity_verified:
                verified_count += 1
            protection_blocker = None
            if channel.protection_required and channel.protection_status != "present":
                protection_blocker = (
                    "tweeter_software_guard_requested"
                    if channel.protection_status == "software_guard_requested"
                    else "tweeter_protection_unverified"
                )
            targets.append({
                "id": f"{group.id}:{channel.role}",
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "speaker_kind": group.kind,
                "speaker_mode": group.mode,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "human_output_label": channel.human_output_label,
                "assigned": assigned,
                "identity_verified": channel.identity_verified,
                "startup_muted": channel.startup_muted,
                "protection_required": channel.protection_required,
                "protection_status": channel.protection_status,
                "sound_test_blockers": [
                    code for code, blocked in (
                        ("physical_output_unassigned", not assigned),
                        ("identity_unverified", not channel.identity_verified),
                        (protection_blocker, protection_blocker is not None),
                        (
                            "tweeter_must_start_muted",
                            channel.role == "tweeter" and not channel.startup_muted,
                        ),
                    )
                    if blocked
                ],
            })

    evaluation = topology.evaluation()
    unverified_count = sum(
        1 for target in targets
        if target["assigned"] and not target["identity_verified"]
    )
    if not topology.speaker_groups:
        status = "draft"
        next_step = "Create a speaker map before verifying physical outputs."
    elif evaluation["blockers"]:
        status = "blocked"
        next_step = "Resolve topology blockers before channel identity can be trusted."
    elif unverified_count:
        status = "needs_verification"
        next_step = "Verify each assigned physical output before sound tests."
    else:
        status = "verified"
        next_step = "Channel identity is verified; path safety still gates playback."

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CHANNEL_IDENTITY_REPORT_KIND,
        "status": status,
        "topology_status": evaluation["status"],
        "assigned_channel_count": assigned_count,
        "verified_channel_count": verified_count,
        "unverified_channel_count": unverified_count,
        "sound_tests_allowed": False,
        "targets": targets,
        "next_step": next_step,
    }


def clock_domain_report(topology: OutputTopology) -> dict[str, Any]:
    """Return read-only output clocking evidence for the topology.

    This intentionally does not authorize multi-DAC playback. It names the
    active profile's declared clocking shape and preserves the product
    boundary: topology evidence records the hardware clock contract, while
    runtime owners must still prove graph safety, xrun behavior, and any
    inter-device skew/drift constraints before playback.
    """

    hardware = topology.hardware
    profile = by_id(hardware.device_id)
    profile_known = profile is not None
    profile_kind = profile.kind if profile else "unknown"
    profile_is_composite = bool(profile and profile.kind == "composite")
    aggregate_output_runtime_enabled = False
    issues: list[dict[str, str]] = []
    notes: list[str] = []
    if hardware.physical_output_count <= 0:
        status = "missing_hardware"
        issues.append(
            _issue(
                "blocker",
                "no_output_hardware",
                "no recognized output hardware is available",
            )
        )
        clock_domain_count = 0
        coherent_physical_output_count = 0
        notes.append("No final-output hardware is currently available.")
    elif profile is None:
        status = "unknown_device_clock"
        issues.append(
            _issue(
                "warning",
                "unknown_clock_domain",
                "output hardware clocking is not recognized by JTS",
            )
        )
        clock_domain_count = 1
        coherent_physical_output_count = 0
        notes.extend([
            "The active output device is not in the DAC profile registry.",
            "Topology can record wiring, but clock coherence is unknown.",
        ])
    elif profile.coherent_clock_domain:
        status = "single_device_clock"
        clock_domain_count = 1
        coherent_physical_output_count = hardware.physical_output_count
        notes.extend([
            f"{profile.label} is a known coherent output profile.",
            "All declared physical outputs share one hardware clock domain.",
        ])
    else:
        status = "known_independent_clocks"
        clock_domain_count = max(1, len(profile.child_profile_ids))
        coherent_physical_output_count = 0
        issues.append(
            _issue(
                "warning",
                "independent_output_clocks",
                "known output profile does not declare one coherent clock domain",
            )
        )
        notes.extend([
            f"{profile.label} is a known output profile with independent clocks.",
            "Topology can expose the physical output shape, but runtime validation must prove aggregate-output safety.",
        ])
    if profile_is_composite:
        notes.append(
            "Composite output ordering and runtime readiness are owned by "
            "hardware reconcile/outputd, not by this topology parser."
        )

    recommendation = (
        "Use one coherent multi-output DAC/interface for active crossover."
        if status in {"single_device_clock", "unknown_device_clock", "missing_hardware"}
        else (
            "Use this aggregate profile only after the output runtime has matching "
            "graph, channel-identity, and clock-drift validation evidence."
        )
    )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CLOCK_DOMAIN_REPORT_KIND,
        "status": status,
        "clock_domain_id": hardware.clock_domain_id,
        "clock_domain_label": hardware.clock_domain_label,
        "clock_domain_count": clock_domain_count,
        "coherent_physical_output_count": coherent_physical_output_count,
        "profile_id": profile.id if profile else hardware.device_id,
        "profile_known": profile_known,
        "profile_kind": profile_kind,
        "profile_is_composite_output": profile_is_composite,
        "aggregate_output_runtime_enabled": aggregate_output_runtime_enabled,
        "multi_device_aggregate_supported": False,
        "future_multi_device_lab_path": True,
        "sound_tests_allowed": False,
        "issues": issues,
        "notes": notes,
        "recommendation": recommendation,
    }


def set_channel_identity_verified(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    identity_verified: bool,
) -> OutputTopology:
    """Return a copy with one channel's physical identity evidence updated."""

    group_id = _require_id(speaker_group_id, "speaker_group_id")
    role_id = _enum(role, "role", SUPPORTED_ROLES)
    matches = [
        (group, channel)
        for group in topology.speaker_groups
        for channel in group.channels
        if group.id == group_id and channel.role == role_id
    ]
    if not matches:
        raise OutputTopologyError("speaker channel not found")
    if len(matches) > 1:
        raise OutputTopologyError("speaker channel identity is ambiguous")
    matched_channel = matches[0][1]
    if matched_channel.physical_output_index is None and identity_verified:
        raise OutputTopologyError("cannot verify an unassigned physical output")

    groups: list[SpeakerGroup] = []
    for group in topology.speaker_groups:
        if group.id != group_id:
            groups.append(group)
            continue
        channels = []
        for channel in group.channels:
            if channel.role != role_id:
                channels.append(channel)
                continue
            channels.append(SpeakerChannel(
                role=channel.role,
                physical_output_index=channel.physical_output_index,
                human_output_label=channel.human_output_label,
                identity_verified=bool(identity_verified),
                startup_muted=channel.startup_muted,
                protection_required=channel.protection_required,
                protection_status=channel.protection_status,
            ))
        groups.append(SpeakerGroup(
            id=group.id,
            label=group.label,
            kind=group.kind,
            mode=group.mode,
            position=group.position,
            channels=tuple(channels),
        ))
    return OutputTopology(
        topology_id=topology.topology_id,
        name=topology.name,
        hardware=topology.hardware,
        speaker_groups=tuple(groups),
        routing=topology.routing,
        status="draft",
    )


def set_channel_protection_status(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    protection_status: str,
) -> OutputTopology:
    """Return a copy with one channel's protection evidence updated."""

    group_id = _require_id(speaker_group_id, "speaker_group_id")
    role_id = _enum(role, "role", SUPPORTED_ROLES)
    status = _enum(protection_status, "protection_status", PROTECTION_STATUSES)
    matches = [
        (group, channel)
        for group in topology.speaker_groups
        for channel in group.channels
        if group.id == group_id and channel.role == role_id
    ]
    if not matches:
        raise OutputTopologyError("speaker channel not found")
    if len(matches) > 1:
        raise OutputTopologyError("speaker channel protection is ambiguous")
    if role_id != "tweeter" and status != "not_required":
        raise OutputTopologyError("only tweeter channels can require protection")

    groups: list[SpeakerGroup] = []
    for group in topology.speaker_groups:
        if group.id != group_id:
            groups.append(group)
            continue
        channels = []
        for channel in group.channels:
            if channel.role != role_id:
                channels.append(channel)
                continue
            protection_required = channel.protection_required or role_id == "tweeter"
            channels.append(SpeakerChannel(
                role=channel.role,
                physical_output_index=channel.physical_output_index,
                human_output_label=channel.human_output_label,
                identity_verified=channel.identity_verified,
                startup_muted=True if role_id == "tweeter" else channel.startup_muted,
                protection_required=protection_required,
                protection_status=status,
            ))
        groups.append(SpeakerGroup(
            id=group.id,
            label=group.label,
            kind=group.kind,
            mode=group.mode,
            position=group.position,
            channels=tuple(channels),
        ))
    return OutputTopology(
        topology_id=topology.topology_id,
        name=topology.name,
        hardware=topology.hardware,
        speaker_groups=tuple(groups),
        routing=topology.routing,
        status="draft",
    )


def topology_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_TOPOLOGY_PATH")
        or OUTPUT_TOPOLOGY_PATH
    )


def load_output_topology(path: str | Path | None = None) -> OutputTopology:
    """Load persisted topology, failing soft to a detected empty draft."""

    target = topology_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return OutputTopology.from_mapping(raw)
    except FileNotFoundError:
        return new_topology_draft()
    except (OSError, json.JSONDecodeError, OutputTopologyError) as exc:
        logger.warning(
            "event=output_topology.load_failed path=%s error=%s",
            target,
            type(exc).__name__,
        )
        return new_topology_draft()


def save_output_topology(
    topology: OutputTopology,
    path: str | Path | None = None,
) -> None:
    """Persist a topology atomically. This still does not authorize playback."""

    target = topology_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(topology.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(data)
        os.chmod(tmp_name, 0o640)
        os.replace(tmp_name, target)
    except Exception:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "event=output_topology.temp_cleanup_failed path=%s",
                    tmp_name,
                )
        raise
