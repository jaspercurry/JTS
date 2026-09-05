# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Versioned speaker output topology contract.

The boundary between physical DAC lanes and speaker/driver roles: speaker
groups, active/passive modes, subwoofers, and verified physical output
ownership. It has NO audio side effects — no playback, no CamillaDSP reload,
no hardware mutation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from .atomic_io import advisory_file_lock, atomic_write_text
from .json_fields import JsonFields
from .transition_log import TransitionLog
from .audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID as APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID as DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    HIFIBERRY_DAC8X_STUDIO_ID as HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,  # noqa: F401 - re-export.
    ChannelMapEntry,
    DacProfile,
    by_id as _dac_by_id,
    clock_domain_contract_for as _dac_clock_domain_contract_for,
    clock_domain_label_for as _dac_clock_domain_label_for,
    label_for as _dac_label_for,
    physical_output_count_for as _dac_physical_output_count_for,
)
from .camilla_emit import (
    BASS_MANAGEMENT_CORNER_HZ_HI,
    BASS_MANAGEMENT_CORNER_HZ_LO,
)
from .output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    detected_hardware_adoption_precondition,
    load_state as load_output_hardware_state,
    normalize_output_device_id,
    topology_hardware_from_state,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OUTPUT_TOPOLOGY_KIND = "jts_output_topology"
CHANNEL_IDENTITY_REPORT_KIND = "jts_output_channel_identity_report"
CLOCK_DOMAIN_REPORT_KIND = "jts_output_clock_domain_report"
OUTPUT_LAYOUT_KIND = "jts_output_layout"
OUTPUT_TRANSPORT_PLAN_KIND = "jts_output_transport_plan"
OUTPUT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"
OUTPUT_TOPOLOGY_LOCK_TIMEOUT_SEC = 15.0

# Active-output route resolution. Owned here, not on the IO-free DAC registry,
# because resolution reads env + the topology's card identity. Re-exported from
# jasper.active_speaker.playback_route.
ACTIVE_PLAYBACK_DEVICE_ENV = "JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE"
OUTPUTD_ACTIVE_LANE_SOURCE = "outputd_active_lane"
EXPLICIT_SOURCE = "explicit"
MISSING_SOURCE = "missing"

# The transport dispatches on clock-domain SHAPE, never a per-DAC branch:
# one sink string per shape, width + channel map carried as data.
TRANSPORT_SINK_SINGLE_ALSA = "single_alsa"
TRANSPORT_SINK_COMPOSITE = "composite"

# Every physical-DAC PCM the active path resolves MUST be a stable, name-keyed
# ALSA identifier (``hw:CARD=<name>,DEV=<n>``): a USB DAC or the Apple dongles
# re-enumerate to a different numeric ALSA index across a reboot/hotplug, so
# ``hw:<index>`` / ``plughw:`` forms are unsafe on the active path. This regex
# is the single guard that keeps them out at the type boundary. Anchored with
# \A...\Z (not ^...$) so a trailing newline cannot sneak past — Python's $
# matches before a final '\n', which would false-accept "hw:CARD=x,DEV=0\n".
_STABLE_CARD_PCM_RE = re.compile(r"\Ahw:CARD=[^,\s]+,DEV=\d+\Z")

DUAL_APPLE_ACTIVE_DEVICE_ID = DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID

# Bass-management corner bounds (Hz) for a user-settable subwoofer crossover.
# BOUND TO the one shared corner definition (jasper.camilla_emit) — a
# stdlib-only leaf this import-cheap module CAN depend on, unlike
# active_speaker, which would be circular: active_speaker imports
# output_topology.
SUB_CROSSOVER_HZ_LO = BASS_MANAGEMENT_CORNER_HZ_LO
SUB_CROSSOVER_HZ_HI = BASS_MANAGEMENT_CORNER_HZ_HI

SUPPORTED_GROUP_KINDS = {"left", "right", "mono", "subwoofer"}
SUPPORTED_GROUP_MODES = {
    "full_range_passive",
    "active_2_way",
    "active_3_way",
    "subwoofer",
}
# The listening (non-subwoofer) group kinds, and the mode that means "one
# full-range driver per side, no active crossover". MAIN_GROUP_KINDS is DERIVED,
# not re-listed: a fifth supported kind must land on one side of the main/sub
# line deliberately, not by silently dropping out of the passive predicates.
MAIN_GROUP_KINDS = frozenset(SUPPORTED_GROUP_KINDS) - {"subwoofer"}
PASSIVE_MAIN_MODE = "full_range_passive"
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


class OutputTopologyError(ValueError):
    """Raised when output topology JSON has an unsupported shape."""


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


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
    # No defaults: callers that bypass ``from_mapping`` must make the safety
    # posture explicit instead of silently constructing an unsafe tweeter with
    # the non-tweeter values.
    protection_required: bool
    protection_status: str
    driver_style: str | None = None
    physical_output_index: int | None = None
    human_output_label: str | None = None
    identity_verified: bool = False
    startup_muted: bool = True
    # The user-settable bass-management corner for a ``subwoofer`` channel: the
    # LR4 low-pass on the sub (and the complementary high-pass on the mains) are
    # emitted at this Hz. ``None`` means "use the default corner" — the active
    # builder falls back to ``DEFAULT_SUB_CROSSOVER_HZ``. Only meaningful on a
    # subwoofer channel; ``evaluate_output_topology`` range-checks it when set.
    crossover_fc_hz: float | None = None
    # Provenance for ``identity_verified``, never serialized and never
    # compared: True only on a channel :func:`set_channel_identity_verified`
    # itself wrote. ``from_mapping`` cannot forge it, which is what lets
    # :func:`_with_server_owned_identity` tell a real audition from a save
    # payload that merely claims one.
    identity_verified_authorized: bool = field(
        default=False, compare=False, repr=False
    )

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
            driver_style=_optional_id(raw.get("driver_style")),
            physical_output_index=_optional_int(
                raw.get("physical_output_index"),
                "speaker_groups[].channels[].physical_output_index",
            ),
            # Derived from hardware.outputs after the full topology is parsed.
            # A client-provided label is a stale UI hint, never persisted truth
            # about physical wiring.
            human_output_label=None,
            identity_verified=_bool(raw.get("identity_verified"), False),
            startup_muted=_bool(raw.get("startup_muted"), True),
            protection_required=protection_required,
            protection_status=protection_status,
            crossover_fc_hz=_optional_float(
                raw.get("crossover_fc_hz"),
                "speaker_groups[].channels[].crossover_fc_hz",
            ),
        )

    def with_output_label(self, label: str | None) -> "SpeakerChannel":
        if label is None:
            return self
        return replace(self, human_output_label=label)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "physical_output_index": self.physical_output_index,
            "identity_verified": self.identity_verified,
            "startup_muted": self.startup_muted,
            "protection_required": self.protection_required,
            "protection_status": self.protection_status,
        }
        if self.driver_style:
            out["driver_style"] = self.driver_style
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
    pairing_intent: str = DEFAULT_PAIRING_INTENT

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
            pairing_intent=_enum(
                raw.get("pairing_intent", DEFAULT_PAIRING_INTENT),
                "pairing_intent",
                PAIRING_INTENTS,
            ),
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

    @property
    def status(self) -> str:
        """Current derived status; never trust the persisted status hint."""

        return cast(str, self.evaluation()["status"])

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
            "pairing_intent": self.pairing_intent,
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


def unknown_output_hardware() -> OutputHardware:
    """The inventory of a box whose reconciler record names no output DAC."""

    return OutputHardware(
        device_id="unknown",
        device_label="Unknown output device",
        physical_output_count=0,
        clock_domain_id=default_clock_domain_id("unknown", None),
    )


def new_topology_draft(
    *,
    topology_id: str = "default",
    name: str = "Speaker outputs",
    hardware: OutputHardware | None = None,
) -> OutputTopology:
    if hardware is None:
        observed = load_output_hardware_state()
        if observed is not None and observed.physical_output_count > 0:
            try:
                hardware = OutputHardware.from_mapping(
                    topology_hardware_from_state(observed)
                )
            except OutputTopologyError:
                logger.warning(
                    "event=output_topology.observed_hardware_invalid profile_id=%s",
                    observed.profile_id,
                )
    return OutputTopology(
        topology_id=topology_id,
        name=name,
        hardware=hardware or unknown_output_hardware(),
        speaker_groups=(),
        routing=TopologyRouting(),
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
                    warnings.append(
                        _issue(
                            "warning",
                            "tweeter_software_guard_requested",
                            (
                                f"{group.label} tweeter software guard is requested; "
                                "protected startup DSP, floor confirmation, and "
                                "driver-aware level caps are required before playback"
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


def channel_identity_report(topology: OutputTopology) -> dict[str, Any]:
    """Return user-confirmed physical channel identity progress.

    Deliberately narrower than ``evaluate_output_topology``: it answers "which
    assigned DAC lane does the operator still need to physically verify?" It
    does not authorize playback, and confirmed identity never implies tweeter
    protection is safe.
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
                if channel.protection_status != "software_guard_requested":
                    protection_blocker = "tweeter_protection_unverified"
            targets.append({
                "id": f"{group.id}:{channel.role}",
                "speaker_group_id": group.id,
                "speaker_label": group.label,
                "speaker_kind": group.kind,
                "speaker_mode": group.mode,
                "role": channel.role,
                "driver_style": channel.driver_style,
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


def _dual_apple_clock_issues(
    hardware: OutputHardware,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    child_devices = hardware.child_devices
    if len(child_devices) != 2:
        issues.append(_issue(
            "blocker",
            "dual_apple_children_required",
            "measured dual-Apple topology requires exactly two child DACs",
        ))
    child_serials: list[str] = []
    mapped_outputs: list[int] = []
    for child in child_devices:
        if child.device_id != APPLE_USB_C_DONGLE_DEVICE_ID:
            issues.append(_issue(
                "blocker",
                "dual_apple_child_device_required",
                f"{child.child_id} is {child.device_id}, not "
                f"{APPLE_USB_C_DONGLE_DEVICE_ID}",
            ))
        if not child.serial:
            issues.append(_issue(
                "blocker",
                "dual_apple_child_serial_required",
                f"{child.child_id} is missing a serial for stable pinning",
            ))
        else:
            child_serials.append(child.serial)
        if len(child.physical_output_indexes) != 2:
            issues.append(_issue(
                "blocker",
                "dual_apple_child_output_pair_required",
                f"{child.child_id} must own exactly two physical outputs",
            ))
        mapped_outputs.extend(child.physical_output_indexes)
    if len(child_serials) == 2 and len(set(child_serials)) != 2:
        issues.append(_issue(
            "blocker",
            "dual_apple_child_serials_not_unique",
            "measured dual-Apple topology requires two unique child DAC serials",
        ))
    if child_devices and sorted(mapped_outputs) != list(range(4)):
        issues.append(_issue(
            "blocker",
            "dual_apple_output_map_invalid",
            "dual-Apple child outputs must cover physical outputs 1-4 exactly once",
        ))
    if hardware.physical_output_count != 4:
        issues.append(_issue(
            "blocker",
            "dual_apple_physical_output_count",
            "dual-Apple topology requires exactly four physical outputs",
        ))

    return issues


def _observed_dual_apple_hardware_issues(
    hardware: OutputHardware,
    observed: OutputHardwareState | None,
) -> list[dict[str, str]]:
    """Return blockers/warnings from current runtime hardware observation."""

    if observed is None:
        return [
            _issue(
                "blocker",
                "dual_apple_observation_missing",
                "current dual-Apple output hardware state has not been observed",
            )
        ]
    if observed.profile_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return [
            _issue(
                "blocker",
                "dual_apple_observed_profile_mismatch",
                f"current output hardware is {observed.profile_id}, not "
                f"{DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}",
            )
        ]

    issues: list[dict[str, str]] = []
    for raw_issue in observed.issues:
        severity = str(raw_issue.get("severity") or "warning")
        if severity not in {"blocker", "warning"}:
            severity = "warning"
        code = str(raw_issue.get("code") or "dual_apple_observed_issue")
        message = str(raw_issue.get("message") or "observed dual-Apple hardware issue")
        issues.append(_issue(severity, code, message))

    if observed.status != "ready" and not any(
        issue.get("severity") == "blocker" for issue in issues
    ):
        issues.append(_issue(
            "blocker",
            "dual_apple_observed_hardware_not_ready",
            f"current dual-Apple output hardware state is {observed.status}",
        ))

    topology_serials = {
        child.serial for child in hardware.child_devices if child.serial
    }
    observed_serials = {
        child.serial for child in observed.child_devices
        if child.device_id == APPLE_USB_C_DONGLE_DEVICE_ID and child.serial
    }
    if topology_serials:
        if len(observed_serials) != 2:
            issues.append(_issue(
                "blocker",
                "dual_apple_observed_serials_missing",
                "current dual-Apple hardware observation lacks two DAC serials",
            ))
        elif observed_serials != topology_serials:
            issues.append(_issue(
                "blocker",
                "dual_apple_observed_serial_mismatch",
                "current dual-Apple DAC serials do not match the saved topology",
            ))

    return issues


def clock_domain_report(topology: OutputTopology) -> dict[str, Any]:
    """Return read-only output clocking evidence for the topology.

    This does not implement multi-DAC aggregation. It names the single-device
    clock-domain assumption and holds the boundary: active-crossover playback
    uses one coherent multi-output device until a lab path proves multi-device
    skew/drift.
    """

    hardware = topology.hardware
    issues: list[dict[str, str]] = []
    notes: list[str]
    clock_contract = _dac_clock_domain_contract_for(hardware.device_id)
    if (
        clock_contract == "measured_sync_required"
        and hardware.device_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    ):
        issues = _dual_apple_clock_issues(hardware)
        observed = load_output_hardware_state()
        issues.extend(_observed_dual_apple_hardware_issues(hardware, observed))
        passed = not any(issue.get("severity") == "blocker" for issue in issues)
        status = (
            "dual_apple_composite_clock"
            if passed
            else "dual_apple_composite_clock_blocked"
        )
        notes = [
            "This is a constrained dual-DAC output profile, not generic ALSA aggregation.",
            "Each Apple DAC remains one speaker-local stereo device; JTS must own both sinks in one process.",
        ]
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": CLOCK_DOMAIN_REPORT_KIND,
            "status": status,
            "clock_domain_id": hardware.clock_domain_id,
            "clock_domain_label": hardware.clock_domain_label,
            "clock_domain_count": len(hardware.child_devices) or 2,
            "coherent_physical_output_count": (
                hardware.physical_output_count if passed else 0
            ),
            "multi_device_aggregate_supported": False,
            "composite_clock_supported": passed,
            "future_multi_device_lab_path": not passed,
            "sound_tests_allowed": False,
            "issues": issues,
            "notes": notes,
            "child_devices": [
                child.to_dict() for child in hardware.child_devices
            ],
            "observed_hardware": (
                observed.to_dict()
                if observed is not None else None
            ),
            "recommendation": (
                "Proceed only through the measured dual-Apple active-output "
                "owner: one process opens both serial-pinned DACs, writes "
                "silence first, monitors xruns/delay/frame counts, and aborts "
                "both sinks on mismatch."
            ),
        }

    notes = [
        "All current physical outputs are assumed to belong to one output device clock domain.",
        "Multiple independent USB DACs are not aggregated by this topology contract.",
    ]
    if hardware.physical_output_count <= 0:
        status = "missing_hardware"
        issues.append(
            _issue(
                "blocker",
                "no_output_hardware",
                "no recognized output hardware is available",
            )
        )
    elif clock_contract is None:
        status = "unknown_device_clock"
        issues.append(
            _issue(
                "warning",
                "unknown_clock_domain",
                "output hardware clocking is not recognized by JTS",
            )
        )
    elif clock_contract == "single_device":
        status = "single_device_clock"
    elif clock_contract in {"independent", "measured_sync_required"}:
        status = "unsupported_clock_contract"
        issues.append(
            _issue(
                "warning",
                "unsupported_clock_contract",
                f"output hardware clock contract {clock_contract} is not supported",
            )
        )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CLOCK_DOMAIN_REPORT_KIND,
        "status": status,
        "clock_domain_id": hardware.clock_domain_id,
        "clock_domain_label": hardware.clock_domain_label,
        "clock_domain_count": 1 if hardware.physical_output_count > 0 else 0,
        "coherent_physical_output_count": hardware.physical_output_count
        if status == "single_device_clock"
        else 0,
        "multi_device_aggregate_supported": False,
        "future_multi_device_lab_path": True,
        "sound_tests_allowed": False,
        "issues": issues,
        "notes": notes,
        "recommendation": (
            "Use one coherent multi-output DAC/interface for active crossover. "
            "Treat multiple USB DACs as future lab work until JTS can measure "
            "and compensate inter-device skew and drift."
        ),
    }


def stable_card_pcm(card_id: str | None) -> str | None:
    """Return a STABLE, name-keyed ALSA PCM for an output card, or ``None``.

    The one chokepoint every physical-DAC PCM on the active-crossover path goes
    through, so the path is keyed on the card's stable name rather than a
    drift-prone numeric index. ``card_id`` is the ALSA card *name*
    (``/proc/asound/card<N>/id``, e.g. ``DAC8``/``Array``), the same name the
    DAC profile matches on; the ``CARD=`` form forces name lookup even if that
    id happens to be numeric.
    """

    card = (card_id or "").strip()
    if not card:
        return None
    return f"hw:CARD={card},DEV=0"


def is_stable_card_pcm(pcm: str | None) -> bool:
    """Return True only for a stable ``hw:CARD=<name>,DEV=<n>`` identifier."""

    return bool(_STABLE_CARD_PCM_RE.match(pcm or ""))


def _transport_sink_for_kind(kind: str) -> str:
    """Map a DAC clock-domain shape (``kind``) to its transport sink string.

    The whole of the per-shape dispatch: a new DAC of an established shape adds
    no code here, because width and channel map ride as data.
    """

    return TRANSPORT_SINK_COMPOSITE if kind == "composite" else TRANSPORT_SINK_SINGLE_ALSA


def _resolve_transport_dac_pcms(
    hardware: OutputHardware,
    profile: DacProfile,
) -> tuple[str, ...]:
    """Resolve the physical DAC PCM(s) the transport writes to (stable identity).

    Child/card identity may be incomplete in a draft topology (observed
    hardware not yet recorded), so this is best-effort: it returns only the
    stable PCMs it can resolve, and never a numeric form.
    """

    if profile.kind == "composite":
        pcms: list[str] = []
        for child in hardware.child_devices:
            pcm = stable_card_pcm(child.card_id)
            if pcm:
                pcms.append(pcm)
        return tuple(pcms)
    pcm = stable_card_pcm(hardware.card_id)
    return (pcm,) if pcm else ()


@dataclass(frozen=True)
class OutputTransportPlan:
    """DAC-agnostic active-output transport truth for a resolved DAC.

    The single source of truth the reconciler emits to env and
    ``jasper-outputd`` consumes; here it is pure data with no env or ALSA I/O.
    ``dac_pcms`` are always stable ``hw:CARD=`` identifiers — the invariant
    that survives card-index drift is enforced at this boundary.
    """

    sink: str
    transport_channels: int
    channel_map: tuple[ChannelMapEntry, ...]
    dac_pcms: tuple[str, ...]
    clock_domain_contract: str

    def __post_init__(self) -> None:
        if self.sink not in (TRANSPORT_SINK_SINGLE_ALSA, TRANSPORT_SINK_COMPOSITE):
            raise OutputTopologyError(f"unsupported transport sink {self.sink!r}")
        if self.transport_channels <= 0:
            raise OutputTopologyError("transport_channels must be > 0")
        if len(self.channel_map) != self.transport_channels:
            raise OutputTopologyError(
                "channel_map must carry one entry per transport channel"
            )
        camilla_indexes = sorted(e.camilla_out_index for e in self.channel_map)
        if camilla_indexes != list(range(self.transport_channels)):
            raise OutputTopologyError(
                "channel_map camilla_out_index values must be exactly "
                f"0..{self.transport_channels - 1}"
            )
        physical = [e.physical_dac_channel for e in self.channel_map]
        if len(set(physical)) != len(physical):
            raise OutputTopologyError(
                "channel_map maps two lanes to the same physical_dac_channel"
            )
        for pcm in self.dac_pcms:
            if not is_stable_card_pcm(pcm):
                raise OutputTopologyError(
                    "dac_pcms must be stable hw:CARD= identifiers (no numeric "
                    f"index, no plug/plughw), got {pcm!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": OUTPUT_TRANSPORT_PLAN_KIND,
            "sink": self.sink,
            "transport_channels": self.transport_channels,
            "channel_map": [
                {
                    "camilla_out_index": entry.camilla_out_index,
                    "physical_dac_channel": entry.physical_dac_channel,
                }
                for entry in self.channel_map
            ],
            "dac_pcms": list(self.dac_pcms),
            "clock_domain_contract": self.clock_domain_contract,
        }


@dataclass(frozen=True)
class OutputLayout:
    """Resolved active-output route for a saved topology.

    Computed FRESH from the ``OutputTopology`` on every call, never cached
    against a numeric card index, so a boot/udev topology recompute flows
    straight through to the resolved route. ``playback_device`` is where the
    active path hands audio off (the production outputd active lane or an
    explicit lab PCM); ``transport_plan`` is present only when a production
    outputd active lane exists.
    """

    device_id: str
    card_id: str | None
    playback_device: str | None
    playback_device_source: str
    transport_channel_count: int
    subwoofer_supported: bool
    transport_plan: OutputTransportPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": OUTPUT_LAYOUT_KIND,
            "device_id": self.device_id,
            "card_id": self.card_id,
            "playback_device": self.playback_device,
            "playback_device_source": self.playback_device_source,
            "transport_channel_count": self.transport_channel_count,
            "subwoofer_supported": self.subwoofer_supported,
            "transport_plan": (
                self.transport_plan.to_dict() if self.transport_plan else None
            ),
        }


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
       declares one. This is the durable path and the only one carrying an
       ``OutputTransportPlan``.
    3. Otherwise the route is missing (no width, no subwoofer support).

    Case 2 has ONE transport, and this is where a FRESH emit names it: the
    active lane is reached over the ACTIVE RING, unconditionally. This chooser
    does not read the reconciler's endpoint marker (see the branch below).
    ``playback_device_source`` stays ``OUTPUTD_ACTIVE_LANE_SOURCE``: it names
    the lane ROLE, not the transport, so nothing keyed on the SOURCE knows
    about the ring.

    A RE-EMIT of a graph the box is already running asks a different question
    and does not come through here first. It reads
    :func:`jasper.active_speaker.playback_route.resolve_live_active_endpoint`,
    which prefers the statefile-pointed GRAPH — the marker is derived from that
    graph, so a marker read would be a rung behind mid-ladder — and falls back
    to this resolution when the graph does not answer.
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
            transport_plan=None,
        )

    if (
        profile is not None
        and profile.supports_active_outputd_lane
        and profile.active_outputd_lane_channels
    ):
        # Lazy import: fanin_coupling is import-cheap but this module is on the
        # topology layer.
        from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

        # The ACTIVE ring, unconditionally — there is no second legal endpoint
        # to choose between (OUTPUTD_LEGAL_ENDPOINT_DEVICES is one member).
        #
        # Reading `ring_active_endpoint_armed()` here would make this chooser a
        # FIXED POINT: the marker derives from the loaded graph and the graph's
        # device would derive from the marker, so no automated pass could move
        # a box between transports — only a human passing `--endpoint`. Not
        # reading the marker is what makes the roleful path convergent.
        #
        # The marker is unaffected: it is outputd's own
        # JASPER_OUTPUTD_ACTIVE_LANE biconditional, read by the Rust daemon and
        # by `active_ring_endpoint_proof`.
        active_device = RING_ACTIVE_PLAYBACK_DEVICE
        return OutputLayout(
            device_id=hardware.device_id,
            card_id=hardware.card_id,
            playback_device=active_device,
            playback_device_source=OUTPUTD_ACTIVE_LANE_SOURCE,
            transport_channel_count=profile.active_outputd_lane_channels,
            subwoofer_supported=True,
            transport_plan=_build_outputd_transport_plan(hardware, profile),
        )

    return OutputLayout(
        device_id=hardware.device_id,
        card_id=hardware.card_id,
        playback_device=None,
        playback_device_source=MISSING_SOURCE,
        transport_channel_count=0,
        subwoofer_supported=False,
        transport_plan=None,
    )


def _build_outputd_transport_plan(
    hardware: OutputHardware,
    profile: DacProfile,
) -> OutputTransportPlan:
    width = int(profile.active_outputd_lane_channels or 0)
    if profile.dac_channel_map is not None:
        channel_map = profile.dac_channel_map
    else:
        channel_map = tuple(ChannelMapEntry(index, index) for index in range(width))
    return OutputTransportPlan(
        sink=_transport_sink_for_kind(profile.kind),
        transport_channels=width,
        channel_map=channel_map,
        dac_pcms=_resolve_transport_dac_pcms(hardware, profile),
        clock_domain_contract=profile.clock_domain_contract,
    )


def _update_speaker_channel(
    topology: OutputTopology,
    *,
    group_id: str,
    role: str,
    ambiguity_subject: str,
    update: Callable[[SpeakerChannel], SpeakerChannel],
) -> OutputTopology:
    """Return a topology with one unambiguous speaker channel transformed."""

    matches = [
        channel
        for group in topology.speaker_groups
        for channel in group.channels
        if group.id == group_id and channel.role == role
    ]
    if not matches:
        raise OutputTopologyError("speaker channel not found")
    if len(matches) > 1:
        raise OutputTopologyError(
            f"speaker channel {ambiguity_subject} is ambiguous"
        )

    groups = tuple(
        replace(
            group,
            channels=tuple(
                update(channel) if channel.role == role else channel
                for channel in group.channels
            ),
        )
        if group.id == group_id
        else group
        for group in topology.speaker_groups
    )
    return replace(topology, speaker_groups=groups)


def set_channel_identity_verified(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    identity_verified: bool,
) -> OutputTopology:
    """Return a copy with one channel's physical identity evidence updated.

    The sole writer of ``identity_verified``: only what this function marks
    survives :func:`_with_server_owned_identity` on the way to disk.
    """

    group_id = _require_id(speaker_group_id, "speaker_group_id")
    role_id = _enum(role, "role", SUPPORTED_ROLES)

    def update(channel: SpeakerChannel) -> SpeakerChannel:
        if channel.physical_output_index is None and identity_verified:
            raise OutputTopologyError("cannot verify an unassigned physical output")
        return replace(
            channel,
            identity_verified=bool(identity_verified),
            identity_verified_authorized=bool(identity_verified),
        )

    return _update_speaker_channel(
        topology,
        group_id=group_id,
        role=role_id,
        ambiguity_subject="identity",
        update=update,
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
    if role_id != "tweeter" and status != "not_required":
        raise OutputTopologyError("only tweeter channels can require protection")

    def update(channel: SpeakerChannel) -> SpeakerChannel:
        return replace(
            channel,
            startup_muted=True if role_id == "tweeter" else channel.startup_muted,
            protection_required=channel.protection_required or role_id == "tweeter",
            protection_status=status,
        )

    return _update_speaker_channel(
        topology,
        group_id=group_id,
        role=role_id,
        ambiguity_subject="protection",
        update=update,
    )


@dataclass(frozen=True)
class CompositeRepinPlan:
    """What a same-shape composite re-pin reuses, and what it re-verifies."""

    child_count: int
    replaced_child_count: int
    reverify_output_indexes: tuple[int, ...]
    reverify_output_labels: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_count": self.child_count,
            "replaced_child_count": self.replaced_child_count,
            "reverify_output_indexes": list(self.reverify_output_indexes),
            "reverify_output_labels": list(self.reverify_output_labels),
        }


def _composite_repin_pairs(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> tuple[tuple[str, OutputChildDevice, OutputCardFact], ...] | None:
    """Pair each saved composite child with the DAC now in its USB port.

    Returns ``None`` unless the attached hardware is the SAME SHAPE as the
    saved declaration — same profile, same physical output count, same child
    count, one child of the same kind per saved USB port — so the only thing
    that can differ is WHICH physical unit is plugged into each port. Whether
    the attached hardware is usable at all is
    ``detected_hardware_adoption_precondition``'s verdict, not this one's.

    The pairing anchor is ``usb_path`` — the sysfs port topology path, which
    survives a unit swap in the same port and is the first identity token the
    runtime child-order matcher tries
    (``jasper.output_hardware.dual_apple_runtime_mapping``). Serial cannot be
    the anchor: it is the thing that changed. A DAC moved to a DIFFERENT port
    is therefore not a re-pin — nothing then says which physical unit landed on
    which lanes, and the saved speaker/role assignment has no anchor to keep.

    ``physical_output_indexes`` are deliberately NOT compared against the
    observed projection: observed lane order follows ALSA enumeration, which is
    precisely what a saved topology exists to override. Those indexes are
    declaration, and a re-pin preserves them.
    """

    if observed is None:
        return None
    if not detected_hardware_adoption_precondition(observed)["allowed"]:
        return None
    hardware = topology.hardware
    # Two or more child DACs is what "composite" means in a saved topology
    # (mirrors ``active_speaker.runtime_contract.topology_sink_is_composite``);
    # a single-child DAC has no serial-keyed pairing contract to repair.
    if len(hardware.child_devices) < 2:
        return None
    if normalize_output_device_id(observed.profile_id) != hardware.device_id:
        return None
    if observed.physical_output_count != hardware.physical_output_count:
        return None
    if len(observed.child_devices) != len(hardware.child_devices):
        return None

    saved_by_port: dict[str, OutputChildDevice] = {}
    for child in hardware.child_devices:
        if not child.usb_path or child.usb_path in saved_by_port:
            return None
        saved_by_port[child.usb_path] = child
    attached_by_port: dict[str, OutputCardFact] = {}
    for card in observed.child_devices:
        if not card.usb_path:
            return None
        attached_by_port[card.usb_path] = card
    if set(saved_by_port) != set(attached_by_port):
        return None

    pairs: list[tuple[str, OutputChildDevice, OutputCardFact]] = []
    # Walks the saved child order (dict insertion order).
    for port, child in saved_by_port.items():
        card = attached_by_port[port]
        if card.device_id != child.device_id or not card.serial:
            return None
        pairs.append((port, child, card))
    return tuple(pairs)


def _composite_repin_plan(
    topology: OutputTopology,
    pairs: tuple[tuple[str, OutputChildDevice, OutputCardFact], ...],
) -> CompositeRepinPlan | None:
    """Project paired children into a plan, or ``None`` when nothing changed."""

    replaced = [
        child for _port, child, card in pairs if child.serial != card.serial
    ]
    if not replaced:
        return None
    indexes = tuple(sorted({
        index for child in replaced for index in child.physical_output_indexes
    }))
    return CompositeRepinPlan(
        child_count=len(pairs),
        replaced_child_count=len(replaced),
        reverify_output_indexes=indexes,
        reverify_output_labels=tuple(
            topology.hardware.output_label(index) or f"Output {index + 1}"
            for index in indexes
        ),
    )


def composite_serial_repin_plan(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> CompositeRepinPlan | None:
    """Return the same-shape re-pin available for ``topology``, if any.

    ``None`` means "not offerable": the attached hardware is a different shape,
    is not usable, or is the very same pair of units already pinned.
    """

    pairs = _composite_repin_pairs(topology, observed)
    if pairs is None:
        return None
    return _composite_repin_plan(topology, pairs)


_OBSERVED_HARDWARE_CLOCK_ISSUE_CODES = frozenset({
    "dual_apple_observation_missing",
    "dual_apple_usb_topology_mismatch",
    "dual_apple_usb_topology_unknown",
    "dual_apple_stable_identity_missing",
    "dual_apple_endpoint_not_synchronous",
})


def _is_observed_hardware_clock_issue(code: str) -> bool:
    return (
        code.startswith("dual_apple_observed_")
        or code in _OBSERVED_HARDWARE_CLOCK_ISSUE_CODES
    )


def declared_hardware_mismatch(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> dict[str, Any] | None:
    """Compare the topology's DECLARED hardware against what's attached now.

    The ONE implementation of this rule.
    ``deploy/assets/sound-profile/js/main.js``'s ``outputHardwareMismatch()``
    reads this result as ``payload.hardware_mismatch`` (published by
    ``jasper.web.sound_setup._output_topology_payload``) rather than
    recomputing it. ``jasper.control.audio_health`` calls this directly — it
    runs in a different daemon and cannot read the wizard's HTTP response — as
    the "outer conjunct" alongside
    ``jasper.output_hardware.detected_hardware_adoption_precondition``'s inner
    one: adoption being allowed only says the DETECTED hardware is usable,
    never that it differs from what is already declared, so without this check
    an already-armed box hitting an ordinary outputd hiccup is told to "finish
    setup" for a setup that already happened (#2812).

    This function CANNOT by itself distinguish "genuinely never declared" from
    "already matches". ``topology`` has no ``None`` state for "undeclared" — a
    caller reading a missing topology file gets a ``new_topology_draft`` back
    regardless, and that draft's ``hardware`` is auto-seeded FROM the observed
    record whenever it has outputs. So on a fresh box whose detected hardware
    is ready, the auto-seeded draft and the observed record match by
    construction and this reports no mismatch. Callers that need "was anything
    ever actually persisted?" must ask ``load_output_topology_snapshot``
    (``snapshot.revision == "missing"``); see
    ``jasper.control.audio_health._undeclared_hardware_signal``.
    """
    clock_blockers = [
        issue
        for issue in clock_domain_report(topology).get("issues", [])
        if issue.get("severity") == "blocker"
        and _is_observed_hardware_clock_issue(str(issue.get("code") or ""))
    ]
    if observed is None and not clock_blockers:
        return None
    saved = topology.hardware
    saved_id = saved.device_id
    current_id = observed.profile_id if observed is not None else ""
    saved_count = saved.physical_output_count
    current_count = observed.physical_output_count if observed is not None else 0
    id_mismatch = bool(saved_id and current_id and saved_id != current_id)
    count_mismatch = saved_count != current_count
    if not id_mismatch and not count_mismatch and not clock_blockers:
        return None
    saved_label = saved.device_label or saved.device_id or "Saved hardware"
    current_label = (
        (observed.profile_label or observed.profile_id)
        if observed is not None
        else "Attached hardware"
    )
    current_summary = (
        f"currently attached hardware is {current_label} "
        f"({current_count} physical output{'' if current_count == 1 else 's'})"
        if observed is not None
        else "current output hardware has not been observed"
    )
    blocker_messages = [
        str(issue.get("message") or "")
        for issue in clock_blockers
        if issue.get("message")
    ]
    message = (
        f"Saved topology expects {saved_label} "
        f"({saved_count} physical output{'' if saved_count == 1 else 's'}), "
        f"but {current_summary}."
    )
    if blocker_messages:
        message = f"{message} {' '.join(blocker_messages)}"
    return {
        "saved_label": saved_label,
        "current_label": current_label,
        "saved_count": saved_count,
        "current_count": current_count,
        "clock_blockers": clock_blockers,
        "message": message,
    }


def repin_composite_child_serials(
    topology: OutputTopology,
    observed: OutputHardwareState | None,
) -> OutputTopology:
    """Return a copy pinned to the units now attached, keeping the design.

    The NARROW counterpart to :func:`new_topology_draft`'s wipe: the design a
    swapped dongle cannot invalidate survives, and only each child's observed
    physical identity is rewritten.

    ``identity_verified`` is the one thing it must NOT carry over — it is
    cleared for every channel on a REPLACED child's lanes, and only the
    per-lane tone check (:func:`channel_identity_report`) re-establishes it.
    The clear RE-ARMS real refusals: ``startup_load``'s
    ``physical_identity_verified`` and ``staged_topology_matches_current``
    gates, and ``path_safety``'s ``route_verified`` / evidence-binding checks
    all fail until the household re-confirms, so a re-pinned speaker cannot
    ARM on trust.

    Those gates guard the next arm, not the graph a box is ALREADY playing, so
    an armed box would otherwise keep playing through unconfirmed DACs — on a
    roleful topology, the full-range-into-a-tweeter hazard class. Silencing
    that takes TWO halves, neither sufficient alone, and BOTH belong to this
    function's callers (it is pure — it only clears the evidence):

    1. Immediate, same request:
       ``runtime_convergence.park_and_commit_topology(stay_parked=True)`` skips
       graph selection and makes the park durable.
    2. Durable, every later pass:
       ``runtime_contract.roleful_identity_confirmed`` gates the two
       approved-active-runtime rungs of the graph selector. Without it the park
       is LIVE-ONLY: the reconcile fired by the same request re-selects the
       applied baseline, and audio returns at the next ``jasper-camilla``
       bounce, which every deploy and reboot performs.

    Raises ``OutputTopologyError`` when the attached hardware is not a
    same-shape re-pin — callers offer this only after
    :func:`composite_serial_repin_plan` returns a plan.
    """

    pairs = _composite_repin_pairs(topology, observed)
    plan = _composite_repin_plan(topology, pairs) if pairs is not None else None
    if pairs is None or plan is None:
        raise OutputTopologyError(
            "attached output hardware is not a same-shape re-pin of the saved "
            "speaker setup"
        )
    reverify = set(plan.reverify_output_indexes)
    composed = replace(
        topology.hardware,
        child_devices=tuple(
            replace(
                child,
                serial=card.serial,
                card_id=card.card_id,
                stable_path=card.stable_path,
                controller=card.controller,
            )
            for _port, child, card in pairs
        ),
    )
    # Round-trip through the artifact contract before it can be persisted: the
    # rewritten fields are observed strings from the reconciler, and `replace`
    # bypasses every check `from_mapping` owns (id shape, length caps, lane
    # coverage).
    hardware = OutputHardware.from_mapping(composed.to_dict())
    return replace(
        topology,
        hardware=hardware,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, identity_verified=False)
                    if channel.physical_output_index in reverify
                    else channel
                    for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )


def topology_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_TOPOLOGY_PATH")
        or OUTPUT_TOPOLOGY_PATH
    )


def topology_lock_path(path: str | Path | None = None) -> Path:
    """Return the process-shared writer lock next to the topology artifact."""

    target = topology_path(path)
    return target.with_name(f".{target.name}.lock")


@dataclass(frozen=True)
class OutputTopologySnapshot:
    """One topology and the revision of the exact bytes that produced it."""

    topology: OutputTopology
    revision: str


def load_output_topology_snapshot(
    path: str | Path | None = None,
) -> OutputTopologySnapshot:
    """Load topology and revision from one immutable byte snapshot."""

    target = topology_path(path)
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return OutputTopologySnapshot(new_topology_draft(), "missing")
    except OSError as exc:
        raise OutputTopologyError(
            f"could not read output topology {target}: {exc}"
        ) from exc
    revision = "sha256:" + hashlib.sha256(data).hexdigest()
    try:
        raw = json.loads(data.decode("utf-8"))
        topology = OutputTopology.from_mapping(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutputTopologyError(
            f"output topology {target} is not valid JSON: {exc}"
        ) from exc
    except ValueError as exc:
        raise OutputTopologyError(
            f"output topology {target} is invalid: {exc}"
        ) from exc
    return OutputTopologySnapshot(topology, revision)


def _with_server_owned_identity(
    topology: OutputTopology, recorded: OutputTopology, target: Path
) -> OutputTopology:
    """Return `topology` carrying only identity evidence the server holds.

    ``identity_verified`` says a household member heard the right driver on
    that lane, so a writer that is not :func:`set_channel_identity_verified`
    may only carry the persisted record forward: its ``True`` survives where
    ``recorded`` already holds one for the same group, role, physical output,
    AND declared hardware device. Re-pinning a lane, or swapping the declared
    device under an unchanged group/role/output key, retires the audition that
    named it, because the audition named that hardware, not the key. Clearing
    stays open to every writer.
    """

    verified = {
        (
            group.id,
            channel.role,
            channel.physical_output_index,
            recorded.hardware.device_id,
        )
        for group in recorded.speaker_groups
        for channel in group.channels
        if channel.identity_verified
    }
    refused = 0

    def admit(group_id: str, channel: SpeakerChannel) -> SpeakerChannel:
        nonlocal refused
        if not channel.identity_verified or channel.identity_verified_authorized:
            return channel
        key = (
            group_id,
            channel.role,
            channel.physical_output_index,
            topology.hardware.device_id,
        )
        if key in verified:
            return channel
        refused += 1
        return replace(channel, identity_verified=False)

    admitted = replace(
        topology,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    admit(group.id, channel) for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )
    if refused:
        logger.warning(
            "event=output_topology.identity_claim_refused path=%s lanes=%d",
            target,
            refused,
        )
    return admitted


class OutputTopologyMutation:
    """One admitted read-modify-write transaction for saved topology intent."""

    def __init__(self, target: Path) -> None:
        self.target = target

    def snapshot(self) -> OutputTopologySnapshot:
        """Read topology and revision from one immutable byte snapshot."""

        return load_output_topology_snapshot(self.target)

    def save(self, topology: OutputTopology) -> str:
        """Publish one topology and return its precomputed byte revision."""

        return save_output_topology(
            _with_server_owned_identity(topology, self._recorded(), self.target),
            self.target,
        )

    def _recorded(self) -> OutputTopology:
        """The identity evidence this transaction may still carry forward."""

        try:
            return load_output_topology_strict(self.target)
        except OutputTopologyError:
            # Unreadable bytes prove no audition; the empty draft holds none.
            return new_topology_draft()


@contextmanager
def output_topology_mutation(
    path: str | Path | None = None,
    *,
    timeout_sec: float = OUTPUT_TOPOLOGY_LOCK_TIMEOUT_SEC,
):
    """Serialize a bounded topology mutation across threads and processes."""

    target = topology_path(path)
    with advisory_file_lock(
        topology_lock_path(target),
        mode=0o660,
        timeout_sec=timeout_sec,
    ):
        yield OutputTopologyMutation(target)


def load_output_topology_strict(path: str | Path | None = None) -> OutputTopology:
    """Load persisted topology for safety-authorizing paths.

    A missing topology means "not configured yet" and remains an empty draft.
    A corrupt or unreadable topology is different: callers that may authorize a
    runtime graph must fail closed instead of silently treating it as no saved
    roleful/protected outputs.
    """
    target = topology_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return OutputTopology.from_mapping(raw)
    except FileNotFoundError:
        return new_topology_draft()
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is NOT an OSError, and read_text() raises it before
        # the JSON limb below can see it; SD-card bit rot produces exactly that,
        # and uncaught it escapes every caller's fail-closed handling.
        raise OutputTopologyError(
            f"could not read output topology {target}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OutputTopologyError(
            f"output topology {target} is not valid JSON: {exc}"
        ) from exc
    except ValueError as exc:
        # `OutputTopologyError` IS a ValueError, so the domain error still lands
        # here unchanged. Catching the BASE is deliberate: not every field
        # validator raises the subclass, and only the base makes "every
        # malformed artifact leaves as one typed error" true of the whole parse
        # — which every caller that authorizes a runtime graph fails closed on.
        # The snapshot loader above widens for the same reason.
        raise OutputTopologyError(
            f"output topology {target} is invalid: {exc}"
        ) from exc


# A corrupt or unreadable topology is a persistent *state*, not a per-call
# event, and `load_output_topology` is called on a steady cadence inside
# long-lived daemons (jasper-control's audio_health route sampler, every 60 s).
# Unguarded that is ~1,440 identical WARN lines/day drowning the journal in an
# already-degraded state (#2140). So log the transitions — into failure and
# back out — plus a reminder slow enough to stay readable and frequent enough
# that a persistent failure is never silent. A short-lived process (doctor, a
# wizard request) starts with empty state and always logs its first failure.
LOAD_FAILURE_REMINDER_SEC = 3600.0
# Bounded because this lives for the process's lifetime. Production resolves
# one path; the cap only matters for callers that pass explicit paths.
_LOAD_FAILURE_STATE_MAX = 8


def _now() -> float:
    """Wrapped `time.monotonic` so tests can drive the reminder window."""
    return time.monotonic()


# The shared transition-or-reminder gate (jasper.transition_log), also consumed
# by the crossover level-run poller. The clock is looked up through this module
# so a test may monkeypatch `_now`.
_load_failures = TransitionLog(
    reminder_sec=LOAD_FAILURE_REMINDER_SEC,
    max_keys=_LOAD_FAILURE_STATE_MAX,
    clock=lambda: _now(),
)


def _load_failure_is_loggable(target: Path, signature: str) -> bool:
    """Whether this failure is a transition or a due reminder, not a repeat."""

    return _load_failures.should_log(str(target), signature)


def _load_failure_cleared(target: Path) -> bool:
    """Whether ``target`` just recovered from a failure that was logged."""

    return _load_failures.cleared(str(target))


def load_output_topology(path: str | Path | None = None) -> OutputTopology:
    """Load persisted topology, failing soft to a detected empty draft."""

    target = topology_path(path)
    try:
        topology = load_output_topology_strict(target)
    except OutputTopologyError as exc:
        if _load_failure_is_loggable(target, f"{type(exc).__name__}:{exc}"):
            logger.warning(
                "event=output_topology.load_failed path=%s error=%s detail=%s "
                "repeat_suppression_sec=%d",
                target,
                type(exc).__name__,
                exc,
                int(LOAD_FAILURE_REMINDER_SEC),
            )
        return new_topology_draft()
    if _load_failure_cleared(target):
        logger.info("event=output_topology.load_recovered path=%s", target)
    return topology


def save_output_topology(
    topology: OutputTopology,
    path: str | Path | None = None,
) -> str:
    """Persist topology atomically and return the exact published revision."""

    target = topology_path(path)
    data = json.dumps(topology.to_dict(), indent=2, sort_keys=True) + "\n"
    # /var/lib/jasper is group jasper but NOT setgid, so a root-run recovery
    # write must publish under the directory's group for the non-root management
    # daemons.
    atomic_write_text(target, data, mode=0o640, durable=True)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()
