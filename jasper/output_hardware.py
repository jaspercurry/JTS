# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only output hardware profile classification.

Turns observed ALSA/USB facts into the shared output-profile vocabulary used
by reconcile, `/state`, doctor, and `/sound/`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic_io import atomic_write_json, read_json_mapping
from .audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    DacProfile,
    MixerControl,
    mixer_control_groups_for,
    by_id as _dac_profile_by_id,
    label_for as _dac_label_for,
)
from .audio_hardware.hat_eeprom import HatEeprom
from .audio_hardware.usb_port_role import (
    UsbPortRoleState,
    resolve_system_usb_port_role,
)
from .json_fields import (
    issue as _issue,
    utc_now_iso,
)
from .paths import (
    OUTPUT_HARDWARE_STATE_PATH as DEFAULT_STATE_PATH,
    resolve_state_path,
)

SCHEMA_VERSION = 1
OUTPUT_HARDWARE_STATE_KIND = "jts_output_hardware_state"

APPLE_USB_C_DONGLE_DEVICE_ID = APPLE_USB_C_DONGLE_ID
DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID = DUAL_APPLE_USB_C_DAC_4CH_ID
DUAL_APPLE_LEGACY_ACTIVE_DEVICE_ID = "dual_apple_usb_c_dac_active_2way"

APPLE_USB_VENDOR_ID, APPLE_USB_PRODUCT_ID = APPLE_USB_C_DONGLE.usb_ids[0].split(
    ":",
    1,
)


def normalize_output_device_id(raw: str | None) -> str:
    """Canonical device id for ``raw``. ``None`` / blank becomes ``unknown``.

    The type guard enforces the annotation for the callers that hand this raw
    artifact JSON — ``OutputHardware.from_mapping`` and
    ``OutputChildDevice.from_mapping`` — where a truthy non-string reached
    ``.strip()`` and raised ``AttributeError``, escaping the schema's typed
    contract. ``ValueError`` so the topology loaders normalise it like any
    other malformed field. This module's own callers pre-coerce with ``text``.
    """

    if raw is not None and not isinstance(raw, str):
        raise ValueError(
            f"output device id must be a string, got {type(raw).__name__}"
        )
    value = (raw or "").strip().strip("'\"").lower().replace("-", "_")
    if value == DUAL_APPLE_LEGACY_ACTIVE_DEVICE_ID:
        return DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    return value or "unknown"


def state_path(path: str | Path | None = None) -> Path:
    return resolve_state_path(path, "JASPER_OUTPUT_HARDWARE_STATE_PATH", DEFAULT_STATE_PATH)


def degraded_marker_path(path: str | Path | None = None) -> Path:
    """The reconciler's degraded-pass marker, beside the state file it
    describes (mirrors ``RECONCILE_DEGRADED_MARKER`` in
    ``deploy/bin/jasper-audio-hardware-reconcile``). The file is a sentinel
    only — always empty; its presence, not its content, is the signal."""
    return state_path(path).parent / "reconcile.degraded"


def text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    out = value.strip()
    return out or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OutputCardFact:
    """One observed ALSA playback card and its USB identity when known."""

    card_id: str
    card_index: int | None = None
    label: str = ""
    device_id: str = "unknown"
    vendor_id: str | None = None
    product_id: str | None = None
    serial: str | None = None
    pcm: str | None = None
    stable_path: str | None = None
    usb_path: str | None = None
    controller: str | None = None
    busnum: str | None = None
    devpath: str | None = None
    endpoint_sync: str | None = None
    has_playback: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OutputCardFact":
        card_id = text(raw.get("card_id") or raw.get("card")) or "unknown"
        return cls(
            card_id=card_id,
            card_index=_int(raw.get("card_index")),
            label=text(raw.get("label")) or "",
            device_id=normalize_output_device_id(text(raw.get("device_id"))),
            vendor_id=text(raw.get("vendor_id") or raw.get("idVendor")),
            product_id=text(raw.get("product_id") or raw.get("idProduct")),
            serial=text(raw.get("serial")),
            pcm=text(raw.get("pcm")) or f"hw:CARD={card_id},DEV=0",
            stable_path=text(raw.get("stable_path")),
            usb_path=text(raw.get("usb_path")),
            controller=text(raw.get("controller")),
            busnum=text(raw.get("busnum")),
            devpath=text(raw.get("devpath")),
            endpoint_sync=text(raw.get("endpoint_sync")),
            has_playback=bool(raw.get("has_playback", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "card_id": self.card_id,
            "device_id": self.device_id,
            "label": self.label,
            "has_playback": self.has_playback,
            "pcm": self.pcm or f"hw:CARD={self.card_id},DEV=0",
        }
        for key in (
            "card_index",
            "vendor_id",
            "product_id",
            "serial",
            "stable_path",
            "usb_path",
            "controller",
            "busnum",
            "devpath",
            "endpoint_sync",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def _is_apple_output_card(card: OutputCardFact) -> bool:
    return card.device_id == APPLE_USB_C_DONGLE_DEVICE_ID or (
        (card.vendor_id or "").lower() == APPLE_USB_VENDOR_ID
        and (card.product_id or "").lower() == APPLE_USB_PRODUCT_ID
    )


def _apple_card_count(cards: Iterable[OutputCardFact]) -> int:
    return sum(1 for card in cards if _is_apple_output_card(card))


def apple_output_card_ids(cards: Iterable[OutputCardFact]) -> tuple[str, ...]:
    """Card ids of the attached Apple USB-C DACs, in observation order.

    Playback cards only: the shell owner this feeds picks the Apple mixer
    helpers' card from the first id, and a capture-only card can never be
    that. The registry match already happened in the probe (ADR-0235 R2).
    """
    return tuple(
        card.card_id
        for card in cards
        if card.has_playback and _is_apple_output_card(card)
    )


@dataclass(frozen=True)
class OutputHardwareState:
    """Normalized observed final-output hardware profile."""

    profile_id: str
    profile_label: str
    status: str
    physical_output_count: int
    selected_card_id: str | None = None
    selected_pcm: str | None = None
    apple_dac_count: int = 0
    child_devices: tuple[OutputCardFact, ...] = field(default_factory=tuple)
    issues: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed_at: str | None = None
    usb_data_role: UsbPortRoleState | None = None
    hat_eeprom: HatEeprom | None = None

    @property
    def observed_profile_id(self) -> str | None:
        """The profile this record NAMES, or None if it named none.

        Answers "what hardware did the reconciler see", whatever ``status``
        says about its usability — the right question for diagnostics
        (jasper-doctor's hardware checks) and the topology wizard, both of
        which must describe attached hardware even when it is only partly
        usable. See :attr:`active_profile_id` for "what does the box drive".
        """
        return None if self.profile_id in ("", "unknown") else self.profile_id

    @property
    def active_profile_id(self) -> str | None:
        """The profile the reconciler DRIVES, or None.

        Answers "what does the box play through" — the right question for
        config emission (the conf.d floor render), never for diagnostics,
        which want :attr:`observed_profile_id` instead. Mirrors
        ``apply_observed_single_policy`` /
        ``apply_observed_composite_policy`` in
        ``deploy/bin/jasper-audio-hardware-reconcile`` bit for bit: a single
        DAC counts only while this record is ``ready`` AND names the card it
        selected; the dual-Apple composite counts as soon as it is named,
        parked or not, because its helper services are driven either way.
        Pinned against the reconciler by
        ``test_env_publication_names_the_dac_the_record_names``.
        """
        observed = self.observed_profile_id
        if observed is None:
            return None
        if observed == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
            return observed
        if self.status == "ready" and self.selected_card_id:
            return observed
        return None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OutputHardwareState":
        children = tuple(
            OutputCardFact.from_mapping(item)
            for item in raw.get("child_devices", []) or []
            if isinstance(item, Mapping)
        )
        issues = tuple(
            dict(item)
            for item in raw.get("issues", []) or []
            if isinstance(item, Mapping)
        )
        profile_id = normalize_output_device_id(text(raw.get("profile_id")))
        raw_apple_dac_count = raw.get("apple_dac_count")
        apple_dac_count = (
            _apple_card_count(children)
            if raw_apple_dac_count is None
            else _int(raw_apple_dac_count)
        )
        raw_usb_data_role = raw.get("usb_data_role")
        usb_data_role = (
            UsbPortRoleState.from_mapping(raw_usb_data_role)
            if isinstance(raw_usb_data_role, Mapping)
            else None
        )
        raw_hat_eeprom = raw.get("hat_eeprom")
        hat_eeprom = (
            HatEeprom.from_mapping(raw_hat_eeprom)
            if isinstance(raw_hat_eeprom, Mapping)
            else None
        )
        return cls(
            profile_id=profile_id,
            profile_label=text(raw.get("profile_label"))
            or _dac_label_for(profile_id) or profile_id,
            status=text(raw.get("status")) or "unknown",
            physical_output_count=_int(raw.get("physical_output_count")) or 0,
            selected_card_id=text(raw.get("selected_card_id")),
            selected_pcm=text(raw.get("selected_pcm")),
            apple_dac_count=apple_dac_count
            if apple_dac_count is not None
            else _apple_card_count(children),
            child_devices=children,
            issues=issues,
            observed_at=text(raw.get("observed_at")),
            usb_data_role=usb_data_role,
            hat_eeprom=hat_eeprom,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": OUTPUT_HARDWARE_STATE_KIND,
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "status": self.status,
            "physical_output_count": self.physical_output_count,
            "apple_dac_count": self.apple_dac_count,
            "child_devices": [child.to_dict() for child in self.child_devices],
            "issues": list(self.issues),
            "hat_eeprom": (
                self.hat_eeprom.to_dict() if self.hat_eeprom is not None else None
            ),
        }
        if self.selected_card_id:
            out["selected_card_id"] = self.selected_card_id
        if self.selected_pcm:
            out["selected_pcm"] = self.selected_pcm
        if self.observed_at:
            out["observed_at"] = self.observed_at
        if self.usb_data_role is not None:
            out["usb_data_role"] = self.usb_data_role.to_dict()
        return out


def detected_hardware_adoption_precondition(
    state: OutputHardwareState | None,
) -> dict[str, Any]:
    """Project the reconciler-owned state into the reset action precondition.

    ``allowed`` controls only the contextual "Use detected hardware" affordance.
    The lower recovery reset may still clear a speaker to silent unconfigured
    state when hardware is absent or not usable.
    """

    blockers = (
        any(issue.get("severity") == "blocker" for issue in state.issues)
        if state is not None
        else False
    )
    allowed = bool(
        state is not None
        and state.status == "ready"
        and state.profile_id != "unknown"
        and state.physical_output_count > 0
        and not blockers
    )
    return {
        "allowed": allowed,
    }


def _same_usb_bus(cards: tuple[OutputCardFact, ...]) -> bool | None:
    buses: list[tuple[str, str]] = []
    for card in cards:
        if not card.controller or not card.busnum:
            return None
        buses.append((card.controller, card.busnum))
    if not buses:
        return None
    return len(set(buses)) == 1


def _registered_single_dac_cards(
    cards: tuple[OutputCardFact, ...],
) -> tuple[tuple[OutputCardFact, DacProfile], ...]:
    out: list[tuple[OutputCardFact, DacProfile]] = []
    for card in cards:
        if _is_apple_output_card(card):
            continue
        profile = _dac_profile_by_id(card.device_id)
        if profile is not None and profile.kind == "single":
            out.append((card, profile))
    return tuple(out)


def _single_dac_state(
    card: OutputCardFact,
    profile: DacProfile,
    *,
    apple_dac_count: int,
    observed_at: str,
) -> OutputHardwareState:
    return OutputHardwareState(
        profile_id=profile.id,
        profile_label=profile.label,
        status="ready",
        physical_output_count=profile.physical_output_count,
        selected_card_id=card.card_id,
        selected_pcm=card.pcm,
        apple_dac_count=apple_dac_count,
        child_devices=(card,),
        observed_at=observed_at,
    )


def classify_output_cards(
    cards: Iterable[OutputCardFact],
    *,
    observed_at: str | None = None,
) -> OutputHardwareState:
    """Classify output hardware from already-collected card facts."""

    facts = tuple(card for card in cards if card.has_playback)
    apple = tuple(
        card for card in facts if _is_apple_output_card(card)
    )
    observed_at = observed_at or utc_now_iso()
    registered_single_dacs = _registered_single_dac_cards(facts)

    if len(registered_single_dacs) == 1:
        card, profile = registered_single_dacs[0]
        return _single_dac_state(
            card,
            profile,
            apple_dac_count=len(apple),
            observed_at=observed_at,
        )
    if len(registered_single_dacs) > 1:
        labels = ", ".join(
            f"{card.card_id}:{profile.id}"
            for card, profile in registered_single_dacs
        )
        return OutputHardwareState(
            profile_id="unknown",
            profile_label="Multiple supported output DACs",
            status="partial",
            physical_output_count=0,
            apple_dac_count=len(apple),
            child_devices=tuple(card for card, _profile in registered_single_dacs),
            issues=(
                _issue(
                    "blocker",
                    "multiple_registered_output_dacs",
                    "multiple supported output DACs are present "
                    f"({labels}); leave only the intended final-output DAC attached",
                ),
            ),
            observed_at=observed_at,
        )

    if len(apple) == 2:
        issues: list[dict[str, str]] = []
        status = "ready"
        # Only the composite's own row decides whether mismatched USB
        # buses block it (DacProfile.requires_same_usb_bus, ADR-0235 R1).
        requires_same_bus = DUAL_APPLE_USB_C_DAC_4CH.requires_same_usb_bus
        same_bus = _same_usb_bus(apple) if requires_same_bus else True
        if same_bus is False:
            status = "partial"
            issues.append(_issue(
                "blocker",
                "dual_apple_usb_topology_mismatch",
                "two Apple DACs are present but not on the same USB controller/bus",
            ))
        elif same_bus is None:
            status = "partial"
            issues.append(_issue(
                "blocker",
                "dual_apple_usb_topology_unknown",
                "two Apple DACs are present but USB controller/bus facts are unavailable",
            ))
        missing_identity = [
            card.card_id for card in apple
            if not (card.serial or card.stable_path or card.usb_path)
        ]
        if missing_identity:
            status = "partial"
            issues.append(_issue(
                "blocker",
                "dual_apple_stable_identity_missing",
                "two Apple DACs are present but at least one child lacks stable identity",
            ))
        if any(card.endpoint_sync and card.endpoint_sync.lower() != "sync" for card in apple):
            status = "partial"
            issues.append(_issue(
                "blocker",
                "dual_apple_endpoint_not_synchronous",
                "dual Apple profile requires synchronous USB Audio playback endpoints",
            ))
        return OutputHardwareState(
            profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            profile_label=DUAL_APPLE_USB_C_DAC_4CH.label,
            status=status,
            physical_output_count=DUAL_APPLE_USB_C_DAC_4CH.physical_output_count,
            selected_card_id=None,
            selected_pcm=None,
            apple_dac_count=2,
            child_devices=apple,
            issues=tuple(issues),
            observed_at=observed_at,
        )

    if len(apple) == 1:
        return _single_dac_state(
            apple[0],
            APPLE_USB_C_DONGLE,
            apple_dac_count=1,
            observed_at=observed_at,
        )

    if len(apple) > 2:
        return OutputHardwareState(
            profile_id="unknown",
            profile_label="Unsupported Apple USB-C DAC set",
            status="partial",
            physical_output_count=0,
            apple_dac_count=len(apple),
            child_devices=apple,
            issues=(
                _issue(
                    "blocker",
                    "too_many_apple_dacs",
                    "more than two Apple DACs are attached; 3-DAC/subwoofer output is not supported yet",
                ),
            ),
            observed_at=observed_at,
        )

    return OutputHardwareState(
        profile_id="unknown",
        profile_label="Unknown output device",
        status="missing",
        physical_output_count=0,
        apple_dac_count=0,
        observed_at=observed_at,
    )


@dataclass(frozen=True)
class ObservedOutput:
    """DAC-role policy facts; an all-default value means no record (ADR-0235 R2)."""

    profile_id: str = ""
    status: str = ""
    #: The registry's SHAPE for the observed profile. A composite is routed
    #: onto the paired sink through this, so no profile id is spelled there.
    kind: str = ""
    #: The mixer control ``jasper-headphone-monitor`` re-pins, taken off the
    #: profile the box DRIVES. Empty means that profile pins none, which is
    #: also how the reconciler decides not to run the monitor at all.
    headphone_control: str = ""
    selected_card_id: str = ""
    child_device_ids: tuple[str, ...] = ()
    apple_card_ids: tuple[str, ...] = ()
    blocker_codes: tuple[str, ...] = ()
    record_changed: bool = False
    #: ``None`` when there is no port-role record at all. The rest of that
    #: record is emitted by USB-role reconciliation (ADR-0235 R4).
    management_transport_available: bool | None = None
    dual_mapping_ok: bool = False
    dual_mapping_reason: str = ""
    dual_order_source: str = ""
    dual_dac_a_pcm: str = ""
    dual_dac_b_pcm: str = ""

    @property
    def valid(self) -> bool:
        """A record stating BOTH facts the whole DAC-role policy hangs off."""
        return bool(self.profile_id and self.status)


def load_state(path: str | Path | None = None) -> OutputHardwareState | None:
    raw = read_json_mapping(state_path(path))
    if raw is None:
        return None
    if raw.get("artifact_schema_version") != SCHEMA_VERSION:
        return None
    if raw.get("kind") != OUTPUT_HARDWARE_STATE_KIND:
        return None
    return OutputHardwareState.from_mapping(raw)


# `amixer cget` prints an integer control's own range, then its value, then the
# TLV it publishes:
#     ; type=INTEGER,access=rw---R--,values=1,min=0,max=254,step=1
#     : values=206
#     | dBminmax-min=-103.00dB,max=24.00dB
# Both bounds of each scale come out of ONE match: half a scale read as a whole
# one resolves a unity target to the top of the range, which on a HiFiBerry
# Studio is +24 dB.
_CGET_RANGE_RE = re.compile(
    r"^\s*;\s*type=INTEGER,[^\n]*\bmin=(-?\d+),max=(-?\d+)", re.M
)
_CGET_TLV_MINMAX_RE = re.compile(
    r"dBminmax(?:mute)?-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB"
)


def mixer_index_for_db(cget_output: str, target_db: float) -> int | None:
    """The control index ``target_db`` lands on, or None if the TLV cannot say.

    ``SNDRV_CTL_TLVT_DB_MINMAX`` maps a control's own value range linearly onto
    ``[min_db, max_db]``. The one conversion in the tree: ``jasper-dac-init``
    resolves the index it writes through it, and ``jasper-doctor`` resolves the
    index it expects to read back. Any other TLV form is unreadable to both, and
    an unreadable scale is never guessed at.
    """

    span = _CGET_RANGE_RE.search(cget_output)
    tlv = _CGET_TLV_MINMAX_RE.search(cget_output)
    if span is None or tlv is None:
        return None
    value_min, value_max = int(span.group(1)), int(span.group(2))
    db_min, db_max = float(tlv.group(1)), float(tlv.group(2))
    if value_max <= value_min or db_max <= db_min:
        return None
    scaled = value_min + (target_db - db_min) * (value_max - value_min) / (
        db_max - db_min
    )
    return math.floor(min(max(scaled, value_min), value_max) + 0.5)


def mixer_pins_for_state(
    state: OutputHardwareState | None,
) -> tuple[tuple[str, MixerControl], ...]:
    """``(card_id, control)`` for every mixer pin the OBSERVED profile declares.

    The one place the registry's per-child mixer policy is paired with the
    cards the reconciler actually saw, so ``jasper-dac-init`` applies and
    ``jasper-doctor`` verifies the same list. Keyed on
    :attr:`OutputHardwareState.observed_profile_id`, not the driven lane: a
    hardware gain stage is worth pinning whenever the board is present, and a
    partial record still names the card it saw.
    """

    if state is None:
        return ()
    profile_id = state.observed_profile_id
    if profile_id is None:
        return ()
    groups = mixer_control_groups_for(profile_id)
    if not groups:
        return ()
    if len(groups) == 1:
        cards: list[str] = [state.selected_card_id or ""]
    else:
        cards = [child.card_id for child in state.child_devices]
    # strict: a composite whose observed cards do not match its declared
    # children is a mismatch to report, never a list to silently shorten.
    return tuple(
        (card, control)
        for controls, card in zip(groups, cards, strict=True)
        if card
        for control in controls
    )


def active_dac_profile_id(path: str | Path | None = None) -> str | None:
    """The output DAC the audio-hardware reconciler DRIVES, or None.

    The ONE answer to "what does the box play through" for a live decision
    (config emission, the conf.d floor render): the record the reconciler
    writes, read through :attr:`OutputHardwareState.active_profile_id`. For
    "what hardware did the reconciler see" (doctor's hardware diagnostics,
    the topology wizard) read
    :attr:`OutputHardwareState.observed_profile_id` instead — see that
    property for the difference. ``JASPER_AUDIO_DAC_ID`` in ``jasper.env`` is
    the same driven decision republished by the same pass for consumers that
    can only read env (jasper-outputd's ExecCondition, the bash AEC
    reconciler); it persists across a reboot while this ``/run`` record does
    not, so it can name a DAC that is no longer fitted — read it through
    :func:`published_dac_id` only when the question is what that publication
    says.
    """

    state = load_state(path)
    return None if state is None else state.active_profile_id


def published_dac_id(env: Mapping[str, str]) -> str:
    """The DAC identity the reconciler published to ``env`` (``unknown`` if none).

    Reads an env SNAPSHOT — a parsed ``jasper.env`` — never the live record;
    see :func:`active_dac_profile_id` for the difference. For the AEC gate
    family this is the right question: the bash AEC reconciler derives the gate
    from this key and records the gate beside it in the same file.
    """

    return normalize_output_device_id(env.get("JASPER_AUDIO_DAC_ID"))


def current_usb_data_role(
    path: str | Path | None = None,
) -> UsbPortRoleState:
    """Return the reconciler snapshot, with a fail-closed live fallback.

    The output-hardware reconciler is the normal writer.  The fallback keeps
    early boot and recovery safe if the ephemeral ``/run`` artifact has not
    been published yet; it calls the same pure resolver rather than copying
    policy into a consumer.
    """

    state = load_state(path)
    if state is not None and state.usb_data_role is not None:
        return state.usb_data_role
    return resolve_system_usb_port_role(
        observed_output_profile_id=(state.profile_id if state is not None else "unknown")
    )


def write_state(state: OutputHardwareState, path: str | Path | None = None) -> None:
    atomic_write_json(state_path(path), state.to_dict(), mode=0o644)
