# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Versioned speaker output topology contract.

The boundary between physical DAC lanes and speaker/driver roles: speaker
groups, active/passive modes, subwoofers, and assigned physical output
ownership. It has NO audio side effects — no playback, no CamillaDSP reload,
no hardware mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, cast

from .audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID as APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID as DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    by_id as _dac_by_id,
    clock_domain_label_for as _dac_clock_domain_label_for,
    label_for as _dac_label_for,
    physical_output_count_for as _dac_physical_output_count_for,
)
from .camilla_emit import (
    BASS_MANAGEMENT_CORNER_HZ_HI,
    BASS_MANAGEMENT_CORNER_HZ_LO,
)
from .fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from .json_fields import (
    CodedFieldError,
    JsonFields,
    issue as _issue,
)
from .output_hardware import (
    OutputHardwareState,
    normalize_output_device_id,
)

SCHEMA_VERSION = 1
OUTPUT_VARIANT_SCHEMA_VERSION = 2
SUPPORTED_OUTPUT_VARIANTS = {"primary", "rear"}

OUTPUT_TOPOLOGY_KIND = "jts_output_topology"

# Active-output route resolution. Owned here, not on the IO-free DAC registry,
# because resolution reads env + the topology's card identity. Re-exported from
# jasper.active_speaker.playback_route.
ACTIVE_PLAYBACK_DEVICE_ENV = "JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE"
OUTPUTD_ACTIVE_LANE_SOURCE = "outputd_active_lane"
EXPLICIT_SOURCE = "explicit"
MISSING_SOURCE = "missing"

DUAL_APPLE_ACTIVE_DEVICE_ID = DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID

# Hz; active_speaker imports this module, so shared bounds live in camilla_emit.
SUB_CROSSOVER_HZ_LO = BASS_MANAGEMENT_CORNER_HZ_LO
SUB_CROSSOVER_HZ_HI = BASS_MANAGEMENT_CORNER_HZ_HI

SUPPORTED_GROUP_KINDS = {"left", "right", "mono", "subwoofer"}
MAIN_GROUP_KINDS = frozenset(SUPPORTED_GROUP_KINDS) - {"subwoofer"}
PASSIVE_MAIN_MODE = "full_range_passive"
# Roles run low to high: the first has no lower crossover edge, consecutive
# roles cross over, and a way-count view assumes one mode per way count.
MAIN_DRIVER_ROLES_BY_MODE = {
    PASSIVE_MAIN_MODE: ("full_range",),
    "active_2_way": ("woofer", "tweeter"),
    "active_3_way": ("woofer", "mid", "tweeter"),
}
WAY_COUNT_BY_MAIN_MODE = {mode: len(roles) for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()}
ADJACENT_PAIRS_BY_MAIN_MODE = {
    mode: tuple(zip(roles, roles[1:])) for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()
}
LOWEST_DRIVER_ROLE_BY_MAIN_MODE = {mode: roles[0] for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()}
REQUIRED_ROLES_BY_MODE = {**MAIN_DRIVER_ROLES_BY_MODE, "subwoofer": ("subwoofer",)}
SUPPORTED_GROUP_MODES = set(REQUIRED_ROLES_BY_MODE)
SUPPORTED_ROLES = {
    role for roles in REQUIRED_ROLES_BY_MODE.values() for role in roles
}
OUTPUT_STATES = {"unused", "assigned", "blocked"}
# Pure-data pairing intent recorded at commission time: "is this box meant to
# run solo, become a wireless follower, or host one?" It seeds later reconciler
# defaults and carries NO behavior in this layer — nothing here reads it,
# evaluate_output_topology ignores it, and the emitted CamillaDSP config is
# unaffected. The multiroom reconciler keeps the final runtime say (mirrors
# member_camilla_kwargs). Absent == "solo", so older topology JSON loads
# unchanged.
PAIRING_INTENTS = {"solo", "will_be_follower", "has_follower"}
DEFAULT_PAIRING_INTENT = "solo"

# The stable code for "one speaker's drivers are split across two child DACs of
# a composite output device". Shared vocabulary: the /sound/ wizard keys its
# disclosure notice off this exact string. See ``cross_child_group_verdicts``.
CROSS_CHILD_GROUP_CODE = "speaker_group_spans_child_devices"


class OutputTopologyError(CodedFieldError):
    """Raised when output topology JSON has an unsupported shape."""


_JSON_FIELDS = JsonFields(OutputTopologyError)
_require_mapping = _JSON_FIELDS.mapping
_sequence = _JSON_FIELDS.sequence
_require_id = _JSON_FIELDS.require_id
_optional_id = _JSON_FIELDS.optional_id
_text = _JSON_FIELDS.text
_optional_text = _JSON_FIELDS.optional_text
_int = _JSON_FIELDS.integer
_optional_int = _JSON_FIELDS.optional_integer
_bool = _JSON_FIELDS.boolean
_enum = _JSON_FIELDS.enum
_float = _JSON_FIELDS.number
_optional_float = _JSON_FIELDS.optional_number


def measurement_target_id(role: str, output_variant: str = "primary") -> str:
    """One physical driver output's identity inside a speaker group.

    A primary output's id IS its role, so every role-keyed measurement map on a
    primary-only speaker is unchanged; a rear woofer adds ``woofer:rear``
    (ADR-0316). :func:`physical_target_id` is the same id under its group.
    """
    return role if output_variant == "primary" else f"{role}:{output_variant}"


def cardioid_cabinet_channels(
    outputs: Iterable[tuple[str, str, int]],
) -> tuple[int, int, int] | None:
    """``(front woofer, rear woofer, tweeter)`` channel indexes of the one
    cabinet a rear calibration document describes, or ``None``.

    ADR-0318: exactly one rear output, one front output of the rear's role, and
    one output of the other role, over ``(role, variant, index)`` triples. The
    caller owns what else its own topology must satisfy.
    """
    items = list(outputs)
    rear = [item for item in items if item[1] == "rear"]
    if len(rear) != 1:
        return None
    role = rear[0][0]
    front = [item for item in items if item[1] != "rear" and item[0] == role]
    tweeter = [item for item in items if item[0] != role]
    if len(front) != 1 or len(tweeter) != 1:
        return None
    return front[0][2], rear[0][2], tweeter[0][2]


def physical_target_id(group_id: str, role: str, output_variant: str = "primary") -> str:
    return f"{group_id}:{measurement_target_id(role, output_variant)}"


def _safe_id_fragment(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
    return out[:64] or "unknown"


def default_clock_domain_id(device_id: str, card_id: str | None = None) -> str:
    """Return the default clock-domain id for an output device.

    The measured dual-Apple pair is the one composite that gets a profile-keyed
    id, because its two children share no single ALSA card. Generic multi-DAC
    ALSA aggregation stays unsupported: ``clock_domain_report`` reports
    ``multi_device_aggregate_supported`` false on every path.
    """

    device_id = normalize_output_device_id(device_id)
    if device_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return "profile:dual-apple-usb-c-dac-4ch"
    if card_id:
        return f"alsa:{_safe_id_fragment(card_id)}"
    if device_id:
        return f"device:{_safe_id_fragment(device_id)}"
    return "unknown"


def default_clock_domain_label(device_id: str) -> str:
    device_id = normalize_output_device_id(device_id)
    return _dac_clock_domain_label_for(device_id) or "Single output device clock"


@dataclass(frozen=True)
class OutputChildDevice:
    """One serial-pinned member of a measured composite output device."""

    child_id: str
    device_id: str
    device_label: str
    physical_output_indexes: tuple[int, ...] = field(default_factory=tuple)
    serial: str | None = None
    card_id: str | None = None
    stable_path: str | None = None
    usb_path: str | None = None
    controller: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputChildDevice":
        raw = _require_mapping(raw, "hardware.child_devices[]")
        indexes = tuple(
            _int(item, "hardware.child_devices[].physical_output_indexes[]")
            for item in _sequence(
                raw.get("physical_output_indexes", []),
                "hardware.child_devices[].physical_output_indexes",
            )
        )
        child_device_id = normalize_output_device_id(
            raw.get("device_id", APPLE_USB_C_DONGLE_DEVICE_ID)
        )
        return cls(
            child_id=_require_id(
                raw.get("child_id"),
                "hardware.child_devices[].child_id",
            ),
            device_id=_require_id(child_device_id, "hardware.child_devices[].device_id"),
            device_label=_text(
                raw.get("device_label"),
                "hardware.child_devices[].device_label",
                default="Apple USB-C audio adapter",
            ),
            physical_output_indexes=indexes,
            serial=_optional_text(
                raw.get("serial"),
                "hardware.child_devices[].serial",
                max_length=120,
            ),
            card_id=_optional_id(raw.get("card_id")),
            stable_path=_optional_text(
                raw.get("stable_path"),
                "hardware.child_devices[].stable_path",
                max_length=320,
            ),
            usb_path=_optional_text(
                raw.get("usb_path"),
                "hardware.child_devices[].usb_path",
                max_length=120,
            ),
            controller=_optional_text(
                raw.get("controller"),
                "hardware.child_devices[].controller",
                max_length=120,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "child_id": self.child_id,
            "device_id": self.device_id,
            "device_label": self.device_label,
            "physical_output_indexes": list(self.physical_output_indexes),
        }
        if self.serial:
            out["serial"] = self.serial
        if self.card_id:
            out["card_id"] = self.card_id
        if self.stable_path:
            out["stable_path"] = self.stable_path
        if self.usb_path:
            out["usb_path"] = self.usb_path
        if self.controller:
            out["controller"] = self.controller
        return out


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
    clock_domain_id: str = "unknown"
    clock_domain_label: str = "Single output device clock"
    outputs: tuple[PhysicalOutput, ...] = field(default_factory=tuple)
    child_devices: tuple[OutputChildDevice, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputHardware":
        raw = _require_mapping(raw, "hardware")
        count = _int(
            raw.get("physical_output_count"),
            "hardware.physical_output_count",
        )
        if count < 0 or count > 64:
            raise OutputTopologyError("physical_output_count must be 0-64")
        device_id = _require_id(
            normalize_output_device_id(raw.get("device_id")),
            "hardware.device_id",
        )
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
        child_devices = tuple(
            OutputChildDevice.from_mapping(item)
            for item in _sequence(
                raw.get("child_devices", []),
                "hardware.child_devices",
            )
        )
        hardware = cls(
            device_id=device_id,
            device_label=_text(
                raw.get("device_label"),
                "hardware.device_label",
                default=_dac_label_for(device_id) or device_id,
            ),
            physical_output_count=count,
            card_id=card_id,
            clock_domain_id=clock_domain_id,
            clock_domain_label=_text(
                raw.get("clock_domain_label"),
                "hardware.clock_domain_label",
                default=default_clock_domain_label(device_id),
            ),
            outputs=outputs,
            child_devices=child_devices,
        )
        hardware.validate()
        return hardware

    def validate(self) -> None:
        expected_count = _dac_physical_output_count_for(self.device_id)
        if expected_count is not None and self.physical_output_count != expected_count:
            raise OutputTopologyError(
                f"{self.device_id} requires exactly {expected_count} physical outputs"
            )
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
        child_seen: set[int] = set()
        for child in self.child_devices:
            for index in child.physical_output_indexes:
                if index < 0 or index >= self.physical_output_count:
                    raise OutputTopologyError(
                        f"child device output {index} is outside hardware range"
                    )
                if index in child_seen:
                    raise OutputTopologyError(
                        f"child device output {index} is mapped more than once"
                    )
                child_seen.add(index)

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
        if self.child_devices:
            out["child_devices"] = [child.to_dict() for child in self.child_devices]
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
    # No default: callers that bypass ``from_mapping`` must make the safety
    # posture explicit instead of silently constructing an unsafe tweeter with
    # the non-tweeter value.
    protection_required: bool
    driver_style: str | None = None
    physical_output_index: int | None = None
    human_output_label: str | None = None
    startup_muted: bool = True
    # The user-settable bass-management corner for a ``subwoofer`` channel: the
    # LR4 low-pass on the sub (and the complementary high-pass on the mains) are
    # emitted at this Hz. ``None`` means "use the default corner" — the active
    # builder falls back to ``DEFAULT_SUB_CROSSOVER_HZ``. Only meaningful on a
    # subwoofer channel; ``evaluate_output_topology`` range-checks it when set.
    crossover_fc_hz: float | None = None
    output_variant: str = "primary"

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
        return cls(
            role=role,
            output_variant=_enum(raw.get("output_variant", "primary"), "output_variant", SUPPORTED_OUTPUT_VARIANTS),
            driver_style=_optional_id(raw.get("driver_style")),
            physical_output_index=_optional_int(
                raw.get("physical_output_index"),
                "speaker_groups[].channels[].physical_output_index",
            ),
            # Derived from hardware.outputs after the full topology is parsed.
            # A client-provided label is a stale UI hint, never persisted truth
            # about physical wiring.
            human_output_label=None,
            startup_muted=_bool(raw.get("startup_muted"), True),
            protection_required=protection_required,
            crossover_fc_hz=_optional_float(
                raw.get("crossover_fc_hz"),
                "speaker_groups[].channels[].crossover_fc_hz",
            ),
        )

    def with_output_label(self, label: str | None) -> "SpeakerChannel":
        if label is None:
            return self
        return replace(self, human_output_label=label)

    def target_id(self, group_id: str) -> str:
        return physical_target_id(group_id, self.role, self.output_variant)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "physical_output_index": self.physical_output_index,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
        }
        if self.driver_style:
            out["driver_style"] = self.driver_style
        if self.output_variant != "primary":
            out["output_variant"] = self.output_variant
        if self.human_output_label:
            out["human_output_label"] = self.human_output_label
        if self.crossover_fc_hz is not None:
            out["crossover_fc_hz"] = self.crossover_fc_hz
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
        raw = _require_mapping(raw, "routing")
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
    """Persisted speaker topology."""

    topology_id: str
    name: str
    hardware: OutputHardware
    speaker_groups: tuple[SpeakerGroup, ...] = field(default_factory=tuple)
    routing: TopologyRouting = field(default_factory=TopologyRouting)
    pairing_intent: str = DEFAULT_PAIRING_INTENT

    @classmethod
    def from_mapping(cls, raw: Any) -> "OutputTopology":
        raw = _require_mapping(raw, "output_topology")
        if raw.get("artifact_schema_version") not in (SCHEMA_VERSION, OUTPUT_VARIANT_SCHEMA_VERSION):
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
            routing=TopologyRouting.from_mapping(raw.get("routing", {})),
            pairing_intent=_enum(
                raw.get("pairing_intent", DEFAULT_PAIRING_INTENT),
                "pairing_intent",
                PAIRING_INTENTS,
            ),
        )
        topology._validate_references()
        if raw["artifact_schema_version"] < topology.schema_version:
            raise OutputTopologyError("rear output variants require schema version 2")
        return topology

    @property
    def schema_version(self) -> int:
        return OUTPUT_VARIANT_SCHEMA_VERSION if any(
            channel.output_variant != "primary"
            for group in self.speaker_groups for channel in group.channels
        ) else SCHEMA_VERSION

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

    @property
    def status(self) -> str:
        """Current derived status; never trust the persisted status hint."""

        return cast(str, self.evaluation()["status"])

    def to_dict(self, *, include_evaluation: bool = False) -> dict[str, Any]:
        evaluation = self.evaluation()
        out: dict[str, Any] = {
            "artifact_schema_version": self.schema_version,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": self.topology_id,
            "name": self.name,
            "status": evaluation["status"],
            "hardware": self.hardware.to_dict(),
            "speaker_groups": [group.to_dict() for group in self.speaker_groups],
            "routing": self.routing.to_dict(),
            "pairing_intent": self.pairing_intent,
            "safety": evaluation["safety"],
        }
        if include_evaluation:
            out["evaluation"] = evaluation
        return out


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """SHA-256 over one canonically serialised payload.

    Public because ``active_speaker.baseline_profile`` consumes it: the two
    modules fingerprint the same artifacts and a second copy of the
    serialisation would let their digests drift apart silently.
    """

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def topology_config_fingerprint(topology: OutputTopology) -> str:
    """Fingerprint only topology fields that determine emitted DSP config.

    ``hardware``/``speaker_groups``/``routing`` and nothing else. ``status`` and
    ``safety`` are the evaluation's own output — ``safety.warnings`` is prose
    that determines no filter — and hashing them made every warning reword move
    every persisted anchor (#2500). ``topology_id`` and ``name`` are identity
    and label; each anchor site compares ``topology_id`` separately.
    ``pairing_intent`` stays out because it drives no config either: the
    multiroom reconciler resolves the runtime role from ``grouping.env``. That
    exclusion is pinned by
    ``test_pairing_intent_change_does_not_invalidate_baseline_cache``.

    Reading only the dataclass fields also means this never runs
    :meth:`OutputTopology.evaluation`, so the gate that compares two of these
    at CamillaDSP start costs one parse.
    """

    return canonical_fingerprint({
        "hardware": topology.hardware.to_dict(),
        "speaker_groups": [group.to_dict() for group in topology.speaker_groups],
        "routing": topology.routing.to_dict(),
    })


def default_physical_outputs(count: int) -> tuple[PhysicalOutput, ...]:
    return tuple(
        PhysicalOutput(
            index=index,
            human_label=f"DAC output {index + 1}",
            terminal_label=str(index + 1),
        )
        for index in range(count)
    )


def unknown_output_hardware() -> OutputHardware:
    """The inventory of a box whose reconciler record names no output DAC."""

    return OutputHardware(
        device_id="unknown",
        device_label="Unknown output device",
        physical_output_count=0,
        clock_domain_id=default_clock_domain_id("unknown", None),
    )


def cross_child_group_verdicts(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return one verdict per speaker group whose drivers span two child DACs.

    A composite output device (``hardware.child_devices``) is two or more
    physically separate DACs driven from one process. Their clocks are NOT
    corrected against each other: the composite clock contract is
    ``measured_sync_required``, and ``PairedCompositeSink`` detects divergence
    and fails closed rather than resampling it away. A speaker group whose
    woofer sits on one child and whose tweeter on another puts that uncorrected
    seam INSIDE a crossover, where inter-driver drift walks the crossover null.
    The supported shape is one child DAC per speaker.

    This is a FIDELITY verdict, not a hearing-safety one: every lane still
    drives, nothing is at risk of damage, and the household may have a reason.
    So it is reported at ``warning`` severity — it never joins ``blockers`` and
    therefore never refuses the save or moves the topology to ``blocked``.

    ``child_ids`` is sorted so it is comparable regardless of the order the
    group happens to list its channels in.
    """

    children = topology.hardware.child_devices
    if len(children) < 2:
        return []
    # Safe as a flat map: OutputHardware.validate() already refuses a physical
    # output claimed by more than one child.
    owner_by_index: dict[int, str] = {
        index: child.child_id
        for child in children
        for index in child.physical_output_indexes
    }
    verdicts: list[dict[str, Any]] = []
    for group in topology.speaker_groups:
        owners: set[str] = set()
        for channel in group.channels:
            index = channel.physical_output_index
            if index is None:
                continue
            owner = owner_by_index.get(index)
            # An index no child claims is a DIFFERENT defect, owned by the
            # composite's own output-map check.
            if owner is not None:
                owners.add(owner)
        if len(owners) < 2:
            continue
        child_ids = sorted(owners)
        verdicts.append({
            "severity": "warning",
            "code": CROSS_CHILD_GROUP_CODE,
            "message": (
                f"{group.label} is split across DACs {', '.join(child_ids)}; "
                "keep every driver of one speaker on one DAC so its crossover "
                "does not straddle two uncorrected clocks"
            ),
            "group_id": group.id,
            "group_label": group.label,
            "child_ids": child_ids,
        })
    return verdicts


def evaluate_output_topology(topology: OutputTopology) -> dict[str, Any]:
    """Return deterministic safety/validity evidence for a topology."""

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    assigned: dict[int, tuple[str, str]] = {}

    if not topology.speaker_groups:
        warnings.append(
            _issue("warning", "no_speaker_groups", "no speaker groups are configured")
        )

    for group in topology.speaker_groups:
        required_roles = set(REQUIRED_ROLES_BY_MODE[group.mode])
        actual_roles = [channel.role for channel in group.channels if channel.output_variant == "primary"]
        actual_role_set = set(actual_roles)
        slots = [(channel.role, channel.output_variant) for channel in group.channels]
        if (actual_role_set != required_roles or len(slots) != len(set(slots)) or any(
            channel.output_variant not in SUPPORTED_OUTPUT_VARIANTS
            or (channel.output_variant == "rear" and (channel.role != "woofer" or "woofer" not in required_roles))
            for channel in group.channels
        )):
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
            if channel.output_variant == "rear" and not channel.startup_muted:
                blockers.append(_issue("blocker", "rear_must_start_muted", f"{group.label} rear woofer must start muted"))
            fc = channel.crossover_fc_hz
            if fc is not None and not (
                SUB_CROSSOVER_HZ_LO <= fc <= SUB_CROSSOVER_HZ_HI
            ):
                # Fail LOUD: an out-of-range bass-management corner would emit
                # an unsafe (or non-band-limiting) crossover, so it is a
                # blocker, never a silent clamp.
                blockers.append(
                    _issue(
                        "blocker",
                        "subwoofer_crossover_out_of_range",
                        (
                            f"{group.label} {channel.role} crossover {fc:g} Hz "
                            f"must be between {SUB_CROSSOVER_HZ_LO:g} and "
                            f"{SUB_CROSSOVER_HZ_HI:g} Hz"
                        ),
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

    warnings.extend(cross_child_group_verdicts(topology))

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

    status = "blocked" if blockers else "valid"
    if not topology.speaker_groups:
        status = "draft"
    if status == "draft":
        next_step = "Create speaker groups and assign physical outputs."
    elif blockers:
        next_step = "Resolve blockers before any sound test can be prepared."
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


def main_speaker_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    """Return the listening (left / right / mono) groups of ``topology``."""

    return [
        group for group in topology.speaker_groups
        if group.kind in MAIN_GROUP_KINDS
    ]


def subwoofer_speaker_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    """Return every group that is a subwoofer by kind, mode, or routing."""

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


def topology_is_passive_mains(topology: OutputTopology) -> bool:
    """True iff mains exist and EVERY main is a full-range passive speaker.

    The one owner of "this speaker's mains carry no inter-driver crossover";
    callers compose it rather than restating the kind/mode vocabulary.
    """

    mains = main_speaker_groups(topology)
    if not mains:
        return False
    return all(group.mode == PASSIVE_MAIN_MODE for group in mains)


def topology_is_subless_passive_mains(topology: OutputTopology) -> bool:
    """True iff the topology is full-range passive mains with NO subwoofer.

    The shape that takes the flat program lane: no inter-driver crossover (the
    mains are passive) and no bass-management split (no sub), so it needs no
    active-crossover commissioning and the setup flow terminates after output
    identity. Passive mains PLUS a sub are a DIFFERENT shape: they still ride
    the roleful multi-output emitter for bass management.
    """

    return topology_is_passive_mains(topology) and not subwoofer_speaker_groups(
        topology
    )


@dataclass(frozen=True)
class OutputLayout:
    """Resolved active-output route for a saved topology.

    Computed FRESH from the ``OutputTopology`` on every call, never cached
    against a numeric card index, so a boot/udev topology recompute flows
    straight through to the resolved route. ``playback_device`` is where the
    active path hands audio off: the production outputd active lane or an
    explicit lab PCM.
    """

    device_id: str
    card_id: str | None
    playback_device: str | None
    playback_device_source: str
    transport_channel_count: int
    subwoofer_supported: bool


def resolve_output_layout(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
    env: Mapping[str, str] | None = None,
) -> OutputLayout:
    """Resolve the active-output route for ``topology`` with stable card identity.

    Resolution order:

    1. An explicit lab/CI device (``playback_device`` arg or
       ``JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE``).
    2. The production outputd active lane, when the resolved ``DacProfile``
       declares one. This is the durable path.
    3. Otherwise the route is missing (no width, no subwoofer support).

    Case 2 has ONE transport, and this is where a FRESH emit names it: the
    active lane is reached over the ACTIVE RING, unconditionally. This chooser
    does not read the reconciler's endpoint marker (see the branch below).
    ``playback_device_source`` stays ``OUTPUTD_ACTIVE_LANE_SOURCE``: it names
    the lane ROLE, not the transport, so nothing keyed on the SOURCE knows
    about the ring.

    """

    env = env if env is not None else os.environ
    hardware = topology.hardware
    profile = _dac_by_id(hardware.device_id)
    physical_width = max(0, int(hardware.physical_output_count or 0))

    explicit = playback_device or env.get(ACTIVE_PLAYBACK_DEVICE_ENV)
    if explicit and explicit.strip():
        return OutputLayout(
            device_id=hardware.device_id,
            card_id=hardware.card_id,
            playback_device=explicit.strip(),
            playback_device_source=EXPLICIT_SOURCE,
            transport_channel_count=physical_width,
            subwoofer_supported=True,
        )

    if (
        profile is not None
        and profile.supports_active_outputd_lane
        and profile.active_outputd_lane_channels
    ):
        # The ACTIVE ring, unconditionally — there is no second legal endpoint
        # to choose between (OUTPUTD_LEGAL_ENDPOINT_DEVICES is one member).
        #
        # Reading `ring_active_endpoint_armed()` here would make this chooser a
        # FIXED POINT: the marker derives from the loaded graph and the graph's
        # device would derive from the marker, so no automated pass could move
        # a box between transports — only a human passing `--endpoint`. Not
        # reading the marker is what makes the roleful path convergent.
        active_device = RING_ACTIVE_PLAYBACK_DEVICE
        return OutputLayout(
            device_id=hardware.device_id,
            card_id=hardware.card_id,
            playback_device=active_device,
            playback_device_source=OUTPUTD_ACTIVE_LANE_SOURCE,
            transport_channel_count=profile.active_outputd_lane_channels,
            subwoofer_supported=True,
        )

    return OutputLayout(
        device_id=hardware.device_id,
        card_id=hardware.card_id,
        playback_device=None,
        playback_device_source=MISSING_SOURCE,
        transport_channel_count=0,
        subwoofer_supported=False,
    )


def topology_hardware_from_state(state: OutputHardwareState) -> dict[str, Any]:
    """Convert observed state into an ``OutputHardware`` JSON mapping."""

    outputs = []
    if state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        labels = (
            ("Apple DAC A left", "A-L"),
            ("Apple DAC A right", "A-R"),
            ("Apple DAC B left", "B-L"),
            ("Apple DAC B right", "B-R"),
        )
    else:
        labels = tuple(
            (f"DAC output {index + 1}", str(index + 1))
            for index in range(state.physical_output_count)
        )
    for index in range(state.physical_output_count):
        human_label, terminal_label = labels[index]
        outputs.append({
            "index": index,
            "human_label": human_label,
            "terminal_label": terminal_label,
        })

    child_devices = []
    for idx, child in enumerate(state.child_devices):
        physical = (
            [idx * 2, idx * 2 + 1]
            if state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
            and idx < 2
            else list(range(state.physical_output_count))
        )
        child_devices.append({
            "child_id": f"apple_dac_{idx + 1}"
            if child.device_id == APPLE_USB_C_DONGLE_DEVICE_ID
            else child.card_id,
            "device_id": child.device_id,
            "device_label": _dac_label_for(child.device_id) or child.label,
            "physical_output_indexes": physical,
            **({"serial": child.serial} if child.serial else {}),
            **({"card_id": child.card_id} if child.card_id else {}),
            **({"stable_path": child.stable_path} if child.stable_path else {}),
            **({"usb_path": child.usb_path} if child.usb_path else {}),
            **({"controller": child.controller} if child.controller else {}),
        })

    out: dict[str, Any] = {
        "device_id": state.profile_id,
        "device_label": state.profile_label,
        "physical_output_count": state.physical_output_count,
        "outputs": outputs,
    }
    if state.selected_card_id:
        out["card_id"] = state.selected_card_id
    if child_devices:
        out["child_devices"] = child_devices
    return out
