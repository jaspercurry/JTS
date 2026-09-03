# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only output hardware profile classification.

This module is the output-side counterpart to ``audio_profile_state``:
small, import-cheap, and side-effect-free unless a caller explicitly asks
for a live probe or state-file write. It turns observed ALSA/USB facts into
the shared output-profile vocabulary used by reconcile, `/state`, doctor,
and `/sound/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic_io import atomic_write_json
from .audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    DacProfile,
    HIFIBERRY_DAC8X_ID,
    HIFIBERRY_DAC8X_STUDIO_ID,
    all_profiles as _all_dac_profiles,
    by_id as _dac_profile_by_id,
    profile_for_card_label as _dac_profile_for_card_label,
)
from .audio_hardware.hat_eeprom import HatEeprom, read_hat_eeprom
from .audio_hardware.usb_port_role import (
    UsbPortRoleState,
    resolve_system_usb_port_role,
)


SCHEMA_VERSION = 1
OUTPUT_HARDWARE_STATE_KIND = "jts_output_hardware_state"
DEFAULT_STATE_PATH = "/run/jasper-output-hardware/output_hardware.json"
DEFAULT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"

APPLE_USB_C_DONGLE_DEVICE_ID = APPLE_USB_C_DONGLE_ID
DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID = DUAL_APPLE_USB_C_DAC_4CH_ID
DUAL_APPLE_LEGACY_ACTIVE_DEVICE_ID = "dual_apple_usb_c_dac_active_2way"
HIFIBERRY_DAC8X_DEVICE_ID = HIFIBERRY_DAC8X_ID
HIFIBERRY_DAC8X_STUDIO_DEVICE_ID = HIFIBERRY_DAC8X_STUDIO_ID

APPLE_USB_VENDOR_ID, APPLE_USB_PRODUCT_ID = APPLE_USB_C_DONGLE.usb_ids[0].split(
    ":",
    1,
)

SUPPORTED_DEVICE_OUTPUT_COUNTS = {
    profile.id: profile.physical_output_count
    for profile in _all_dac_profiles()
}
SUPPORTED_DEVICE_LABELS = {
    profile.id: profile.label
    for profile in _all_dac_profiles()
}
SUPPORTED_CLOCK_DOMAIN_LABELS = {
    profile.id: profile.clock_domain_label
    for profile in _all_dac_profiles()
}

_CARD_RE = re.compile(r"^hw:CARD=([^,\s]+),DEV=(\d+)")


def normalize_output_device_id(raw: str | None) -> str:
    """Canonical device id for ``raw``. ``None`` / blank becomes ``unknown``.

    The type guard enforces the annotation for the callers that hand this raw
    artifact JSON — ``OutputHardware.from_mapping`` and
    ``OutputChildDevice.from_mapping`` — where a truthy non-string reached
    ``.strip()`` and raised ``AttributeError``, escaping the schema's typed
    contract. ``ValueError`` so the topology loaders normalise it like any
    other malformed field. This module's own callers pre-coerce with ``_text``.
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
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_HARDWARE_STATE_PATH")
        or DEFAULT_STATE_PATH
    )


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _text(value: Any) -> str | None:
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
        card_id = _text(raw.get("card_id") or raw.get("card")) or "unknown"
        return cls(
            card_id=card_id,
            card_index=_int(raw.get("card_index")),
            label=_text(raw.get("label")) or "",
            device_id=normalize_output_device_id(_text(raw.get("device_id"))),
            vendor_id=_text(raw.get("vendor_id") or raw.get("idVendor")),
            product_id=_text(raw.get("product_id") or raw.get("idProduct")),
            serial=_text(raw.get("serial")),
            pcm=_text(raw.get("pcm")) or f"hw:CARD={card_id},DEV=0",
            stable_path=_text(raw.get("stable_path")),
            usb_path=_text(raw.get("usb_path")),
            controller=_text(raw.get("controller")),
            busnum=_text(raw.get("busnum")),
            devpath=_text(raw.get("devpath")),
            endpoint_sync=_text(raw.get("endpoint_sync")),
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
        profile_id = normalize_output_device_id(_text(raw.get("profile_id")))
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
            profile_label=_text(raw.get("profile_label"))
            or SUPPORTED_DEVICE_LABELS.get(profile_id, profile_id),
            status=_text(raw.get("status")) or "unknown",
            physical_output_count=_int(raw.get("physical_output_count")) or 0,
            selected_card_id=_text(raw.get("selected_card_id")),
            selected_pcm=_text(raw.get("selected_pcm")),
            apple_dac_count=apple_dac_count
            if apple_dac_count is not None
            else _apple_card_count(children),
            child_devices=children,
            issues=issues,
            observed_at=_text(raw.get("observed_at")),
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


def detected_hardware_identity(state: OutputHardwareState | None) -> str:
    """Return a stable opaque concurrency token for detected output hardware.

    This is deliberately a snapshot *identity*, not a second hardware record.
    The output-hardware reconciler remains the only writer of observed facts;
    callers merely echo this token when an action must prove it still applies.
    ALSA card indexes and ``observed_at`` are volatile, so neither participates.
    """

    if state is None:
        payload: dict[str, Any] = {"status": "missing"}
    else:
        children = sorted(
            [
                {
                    "device_id": child.device_id,
                    "vendor_id": child.vendor_id,
                    "product_id": child.product_id,
                    "serial": child.serial,
                    "stable_path": child.stable_path,
                    "usb_path": child.usb_path,
                    "controller": child.controller,
                    "busnum": child.busnum,
                    "devpath": child.devpath,
                    "endpoint_sync": child.endpoint_sync,
                    "has_playback": child.has_playback,
                }
                for child in state.child_devices
            ],
            key=lambda child: json.dumps(child, sort_keys=True, separators=(",", ":")),
        )
        payload = {
            "profile_id": state.profile_id,
            "status": state.status,
            "physical_output_count": state.physical_output_count,
            "children": children,
            "issues": sorted(
                (str(issue.get("severity") or ""), str(issue.get("code") or ""))
                for issue in state.issues
            ),
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        "identity": detected_hardware_identity(state),
    }


@dataclass(frozen=True)
class DualAppleRuntimeMapping:
    """Runtime child-device order for the dual-Apple outputd sink."""

    ok: bool
    reason: str
    order_source: str = ""
    child_devices: tuple[OutputCardFact, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "reason": self.reason,
            "order_source": self.order_source,
            "pcms": [
                child.pcm or f"hw:CARD={child.card_id},DEV=0"
                for child in self.child_devices
            ],
            "child_devices": [child.to_dict() for child in self.child_devices],
        }
        return out


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    observed_at = observed_at or _utc_now()
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
        same_bus = _same_usb_bus(apple)
        status = "ready"
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
            profile_label=SUPPORTED_DEVICE_LABELS[DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID],
            status=status,
            physical_output_count=4,
            selected_card_id=None,
            selected_pcm=None,
            apple_dac_count=2,
            child_devices=apple,
            issues=tuple(issues),
            observed_at=observed_at,
        )

    if len(apple) == 1:
        card = apple[0]
        return OutputHardwareState(
            profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            profile_label=SUPPORTED_DEVICE_LABELS[APPLE_USB_C_DONGLE_DEVICE_ID],
            status="ready",
            physical_output_count=2,
            selected_card_id=card.card_id,
            selected_pcm=card.pcm,
            apple_dac_count=1,
            child_devices=(card,),
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


def parse_aplay_listing(
    listing: str,
    *,
    hat: HatEeprom | None = None,
) -> tuple[OutputCardFact, ...]:
    """Best-effort parser for ``aplay -L`` used by shell reconcile tests."""

    cards: list[OutputCardFact] = []
    lines = listing.splitlines()
    for index, line in enumerate(lines):
        match = _CARD_RE.match(line.strip())
        if not match:
            continue
        card_id = match.group(1)
        label = lines[index + 1].strip() if index + 1 < len(lines) else ""
        profile = _dac_profile_for_card_label(label, hat=hat)
        if profile is APPLE_USB_C_DONGLE or (
            profile is None
            and "apple" in label.lower()
            and "usb audio" in label.lower()
        ):
            device_id = APPLE_USB_C_DONGLE_DEVICE_ID
            vendor_id = APPLE_USB_VENDOR_ID
            product_id = APPLE_USB_PRODUCT_ID
        elif profile is not None:
            device_id = profile.id
            vendor_id = None
            product_id = None
        else:
            device_id = "unknown"
            vendor_id = None
            product_id = None
        cards.append(OutputCardFact(
            card_id=card_id,
            label=label,
            device_id=device_id,
            vendor_id=vendor_id,
            product_id=product_id,
            pcm=f"hw:CARD={card_id},DEV={match.group(2)}",
        ))
    return tuple(cards)


def probe_aplay_listing(aplay: str = "aplay") -> str:
    proc = subprocess.run(
        [aplay, "-L"],
        check=False,
        text=True,
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _proc_card_description(proc_root: Path, card_index: int) -> str | None:
    text = _read_text(proc_root / "cards")
    if not text:
        return None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(rf"^\s*{card_index}\s+\[", line):
            continue
        parts = [line.strip()]
        if index + 1 < len(lines) and lines[index + 1].startswith(" "):
            parts.append(lines[index + 1].strip())
        return " ".join(part for part in parts if part)
    return None


def _find_usb_device(path: Path) -> Path | None:
    current = path.resolve()
    for item in (current, *current.parents):
        if (item / "idVendor").exists() and (item / "idProduct").exists():
            return item
    return None


def _find_controller(path: Path) -> str | None:
    current = path.resolve()
    for item in (current, *current.parents):
        name = item.name
        if name.startswith("xhci-hcd."):
            return name
    return None


def _endpoint_sync_from_stream(stream: str | None) -> str | None:
    if not stream:
        return None
    if "(SYNC)" in stream:
        return "SYNC"
    if "(ASYNC)" in stream:
        return "ASYNC"
    if "(ADAPTIVE)" in stream:
        return "ADAPTIVE"
    return None


def probe_system_cards(
    *,
    sys_class_sound: str | Path = "/sys/class/sound",
    proc_asound: str | Path = "/proc/asound",
    hat: HatEeprom | None = None,
) -> tuple[OutputCardFact, ...]:
    """Probe Linux ALSA/sysfs card facts without opening audio streams."""

    sys_root = Path(sys_class_sound)
    proc_root = Path(proc_asound)
    cards: list[OutputCardFact] = []
    try:
        card_dirs = sorted(
            item for item in sys_root.glob("card[0-9]*")
            if item.name[4:].isdigit()
        )
    except OSError:
        return ()
    for card_dir in card_dirs:
        card_index = int(card_dir.name[4:])
        card_id = _read_text(proc_root / f"card{card_index}" / "id") or card_dir.name
        has_playback = (proc_root / f"card{card_index}" / "pcm0p").exists()
        real = card_dir.resolve()
        usb = _find_usb_device(real)
        vendor_id = _read_text(usb / "idVendor") if usb else None
        product_id = _read_text(usb / "idProduct") if usb else None
        serial = _read_text(usb / "serial") if usb else None
        busnum = _read_text(usb / "busnum") if usb else None
        devpath = _read_text(usb / "devpath") if usb else None
        product = _read_text(usb / "product") if usb else None
        proc_description = _proc_card_description(proc_root, card_index)
        label = product or proc_description or card_id
        stream = _read_text(proc_root / f"card{card_index}" / "stream0")
        if (
            (vendor_id or "").lower() == APPLE_USB_VENDOR_ID
            and (product_id or "").lower() == APPLE_USB_PRODUCT_ID
        ):
            device_id = APPLE_USB_C_DONGLE_DEVICE_ID
        else:
            profile = _dac_profile_for_card_label(label, hat=hat)
            device_id = profile.id if profile is not None else "unknown"
        cards.append(OutputCardFact(
            card_id=card_id,
            card_index=card_index,
            label=label,
            device_id=device_id,
            vendor_id=vendor_id,
            product_id=product_id,
            serial=serial,
            pcm=f"hw:CARD={card_id},DEV=0",
            stable_path=str(usb.resolve()) if usb else str(real),
            usb_path=(usb.name if usb else None),
            controller=_find_controller(real),
            busnum=busnum,
            devpath=devpath,
            endpoint_sync=_endpoint_sync_from_stream(stream),
            has_playback=has_playback,
        ))
    return tuple(cards)


def load_state(path: str | Path | None = None) -> OutputHardwareState | None:
    try:
        raw = json.loads(state_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("artifact_schema_version") != SCHEMA_VERSION:
        return None
    if raw.get("kind") != OUTPUT_HARDWARE_STATE_KIND:
        return None
    return OutputHardwareState.from_mapping(raw)


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


def _identity_tokens(raw: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(raw, OutputCardFact):
        values = {
            "serial": raw.serial,
            "stable_path": raw.stable_path,
            "usb_path": raw.usb_path,
        }
    elif isinstance(raw, Mapping):
        values = {
            "serial": raw.get("serial"),
            "stable_path": raw.get("stable_path"),
            "usb_path": raw.get("usb_path"),
        }
    else:
        values = {}
    tokens: list[tuple[str, str]] = []
    for key in ("serial", "stable_path", "usb_path"):
        value = _text(values.get(key))
        if value:
            tokens.append((key, value))
    return tuple(tokens)


def _runtime_identity_candidates(raw: Any) -> tuple[tuple[str, str], ...]:
    tokens = dict(_identity_tokens(raw))
    return tuple(
        (key, value)
        for key in ("usb_path", "stable_path", "serial")
        if (value := tokens.get(key))
    )


def _topology_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_TOPOLOGY_PATH")
        or DEFAULT_TOPOLOGY_PATH
    )


def _read_topology_hardware(
    path: str | Path | None = None,
) -> tuple[bool, Mapping[str, Any] | None]:
    """Read the saved speaker topology's ``hardware`` mapping.

    Returns ``(exists, hardware)``. ``exists`` is False only when no topology
    file is saved at all; ``hardware`` is None when a file is saved but carries
    no readable ``hardware`` mapping.

    That split has exactly one consumer, named here so the next reader does not
    have to hunt for it: ``_load_dual_apple_topology_children`` maps absent to
    ``None`` and corrupt to ``()``, which ``dual_apple_runtime_mapping`` turns
    into "order by observed hardware" and ``saved_topology_unreadable``
    respectively. ``apply_saved_topology_policy`` deliberately does **not** use
    it — it leaves a corrupt topology on today's behaviour rather than parking
    every box whose topology file happens to be unreadable, which would reach
    far past a half-present composite. The split also keeps a single reader of
    the file in this module.
    """

    target = _topology_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (False, None)
    except (OSError, json.JSONDecodeError):
        return (True, None)
    if not isinstance(raw, Mapping):
        return (True, None)
    hardware = raw.get("hardware")
    if not isinstance(hardware, Mapping):
        return (True, None)
    return (True, hardware)


def _load_dual_apple_topology_children(
    path: str | Path | None = None,
) -> tuple[Mapping[str, Any], ...] | None:
    exists, hardware = _read_topology_hardware(path)
    if not exists:
        return None
    if hardware is None:
        return ()
    device_id = normalize_output_device_id(_text(hardware.get("device_id")))
    if device_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return None
    children = hardware.get("child_devices") or []
    if not isinstance(children, list):
        return ()
    out = [item for item in children if isinstance(item, Mapping)]
    return tuple(sorted(out, key=_child_topology_order))


def _child_topology_order(child: Mapping[str, Any]) -> int:
    indexes = child.get("physical_output_indexes") or []
    values: list[int] = []
    if isinstance(indexes, list):
        for item in indexes:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                pass
    return min(values) if values else 999


def _declared_child_devices(
    hardware: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """The child devices the saved topology declares, if it declares any."""

    children = hardware.get("child_devices") or []
    if not isinstance(children, list):
        return ()
    return tuple(item for item in children if isinstance(item, Mapping))


def _missing_topology_child_labels(
    hardware: Mapping[str, Any],
    observed: Iterable[OutputCardFact],
) -> tuple[str, ...]:
    """Name the saved child devices that no observed card accounts for."""

    observed_tokens: set[tuple[str, str]] = set()
    for card in observed:
        observed_tokens.update(_identity_tokens(card))
    missing: list[str] = []
    for child in _declared_child_devices(hardware):
        tokens = _identity_tokens(child)
        if tokens and observed_tokens.intersection(tokens):
            continue
        missing.append(
            _text(child.get("serial"))
            or _text(child.get("child_id"))
            or _text(child.get("card_id"))
            or "unidentified child"
        )
    return tuple(missing)


def _saved_topology_requires_roleful_graph(
    path: str | Path | None = None,
) -> bool:
    """Ask the topology's own owner whether it needs per-driver DSP.

    Deferred import on purpose, twice over. ``output_topology`` imports this
    module, so the predicate is unreachable at module scope — the same
    circularity, the same predicate, and the same fix as
    ``active_speaker.playback_route``. And this module advertises itself as
    import-cheap: the import is paid only on the rare arm that reaches it (a
    declared composite that is not the observed profile), never on an ordinary
    reconcile pass.

    **A topology that parses as JSON but is invalid as a topology does not
    park, and that is the decision, not an oversight.**
    ``load_output_topology`` fails soft to an empty draft — logging
    ``event=output_topology.load_failed`` as it does — and an empty draft is
    not roleful, so such a box keeps today's behaviour. Two reasons to leave it
    there. Parking on it would mean parking every speaker whose topology file
    is malformed for any reason, which reaches far past a half-present
    composite; and the shape is already loud twice over — that WARN event, plus
    ``jasper-doctor``'s own topology reporting — where this policy would only
    add silence. It is the same call ``apply_saved_topology_policy`` makes for a
    file that is not readable JSON at all, kept deliberately consistent so the
    two malformations do not behave differently for no reason a reader could
    name.
    """

    from .active_speaker.runtime_contract import (
        active_topology_requires_roleful_graph,
    )
    from .output_topology import load_output_topology

    return active_topology_requires_roleful_graph(load_output_topology(path))


def apply_saved_topology_policy(
    state: OutputHardwareState,
    observed_cards: Iterable[OutputCardFact],
    *,
    topology_path: str | Path | None = None,
) -> OutputHardwareState:
    """Fail closed when a saved ROLEFUL composite is only partly present.

    A roleful topology pins which physical child owns which driver. Unplug one
    child and the survivor still classifies on its own as an ordinary
    full-range stereo DAC, and accepting that observation rewires the final
    output to the survivor as a plain stereo DAC.

    What that does NOT do today, stated precisely because the tempting
    shorthand is wrong: it does not put a flat full-range graph on those
    drivers. Graph selection reads the saved topology and nothing else
    (``active_speaker.runtime_contract`` names no observed-hardware reader at
    all), and a roleful topology structurally cannot select the flat graph —
    ``_flat_graph_allowed`` raises
    ``flat_full_range_graph_illegal_for_roleful_topology``. So the box already
    goes quiet rather than playing full range into a protected driver.

    Nor is the situation unreported: ``jasper-doctor``'s "active speaker output
    hardware" check already fails a roleful saved/attached mismatch. What was
    missing is that no *decision* matched that report — the box went quiet as
    the incidental sum of refusals in layers that know nothing about each
    other, while this path went on marking the survivor recognized and wiring a
    live edge onto it. This is the decision catching up with what doctor was
    already saying, taken where the single/composite policy is decided.
    Reconnecting the child re-runs the reconciler through the existing udev
    chain and un-parks with no operator step.

    **Rolefulness is the gate, not compositeness.** ``kind == "composite"`` is
    profile vocabulary; it says a device has children, not that those children
    carry declared drivers. A passive composite can legally place every
    declared speaker on one child's outputs, and unplugging the child that
    carries nothing must not park a working stereo — the same calibration
    ``jasper-doctor`` already makes when it downgrades a non-roleful mismatch
    from fail to warn. So a saved SINGLE topology, and a saved composite that
    needs no per-driver DSP, are both returned untouched.
    """

    exists, hardware = _read_topology_hardware(topology_path)
    if not exists or hardware is None:
        return state
    declared_id = normalize_output_device_id(_text(hardware.get("device_id")))
    declared = _dac_profile_by_id(declared_id)
    if declared is None or declared.kind != "composite":
        return state
    if state.profile_id == declared_id:
        return state
    # Last, because it is the only expensive check here.
    if not _saved_topology_requires_roleful_graph(topology_path):
        return state

    # Diffed against every observed CARD, not against the classified record's
    # children: a record can carry children that are not the Apple pair at all
    # (attach a registered single DAC beside both dongles and the record's
    # children are that DAC), which would report a plugged-in child missing.
    # The one surface this policy exists to produce must not misname hardware.
    # Three genuinely different situations reach this point, and they call for
    # three different sentences. An earlier cut had one `else` reading "no saved
    # child device could be matched", which fired EXACTLY when every child had
    # matched — the inverse of the truth, on the one surface this policy exists
    # to produce.
    missing = _missing_topology_child_labels(hardware, observed_cards)
    if missing:
        detail = f"missing child devices: {', '.join(missing)}"
    elif not _declared_child_devices(hardware):
        detail = "the saved topology declares no child devices to look for"
    else:
        # Every declared child IS attached. Reaching here at all means
        # classification went elsewhere, and it can only do that when other
        # output hardware is also attached — two Apple children alone classify
        # as the composite and return above. So the remediation is to remove
        # the interloper, not to reconnect anything.
        detail = (
            "every declared child device is attached; other output hardware is "
            "also present and was classified first — detach it so the declared "
            "children are recognized as one device"
        )
    return replace(
        state,
        # An already-partial/missing observation keeps its own status; only a
        # "ready" one is downgraded, because "ready" is the single word every
        # consumer reads as "safe to drive this speaker with".
        status="partial" if state.status == "ready" else state.status,
        issues=state.issues + (
            _issue(
                "blocker",
                "saved_composite_partially_present",
                f"saved topology declares the composite {declared.label} "
                f"({declared.physical_output_count} outputs) but observed output "
                f"hardware is {state.profile_id}; {detail}",
            ),
        ),
    )


def dual_apple_runtime_mapping(
    state: OutputHardwareState,
    *,
    topology_path: str | Path | None = None,
) -> DualAppleRuntimeMapping:
    """Return the child order outputd should use for the dual-Apple sink.

    A saved speaker topology pins which physical dongle owns lanes 0/1 and
    2/3. ALSA card order is only safe before that topology exists.
    """

    if state.profile_id != DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID:
        return DualAppleRuntimeMapping(False, "not_dual_apple_profile")
    if state.status != "ready":
        return DualAppleRuntimeMapping(False, f"profile_status_{state.status}")
    if len(state.child_devices) != 2:
        return DualAppleRuntimeMapping(False, "expected_two_child_devices")
    if any(not child.pcm for child in state.child_devices):
        return DualAppleRuntimeMapping(False, "missing_child_pcm")

    topology_children = _load_dual_apple_topology_children(topology_path)
    if topology_children is None:
        return DualAppleRuntimeMapping(
            True,
            "ok",
            "observed_hardware",
            state.child_devices,
        )
    if not topology_children:
        return DualAppleRuntimeMapping(False, "saved_topology_unreadable")
    if len(topology_children) != 2:
        return DualAppleRuntimeMapping(False, "saved_topology_expected_two_children")

    remaining = list(state.child_devices)
    ordered: list[OutputCardFact] = []
    for topology_child in topology_children:
        candidates = _runtime_identity_candidates(topology_child)
        if not candidates:
            return DualAppleRuntimeMapping(
                False,
                "saved_topology_child_identity_missing",
            )
        match = None
        for token in candidates:
            matches = [
                child
                for child in remaining
                if token in _identity_tokens(child)
            ]
            if len(matches) == 1:
                match = matches[0]
                break
        if match is None:
            return DualAppleRuntimeMapping(
                False,
                "saved_topology_child_identity_mismatch",
            )
        ordered.append(match)
        remaining.remove(match)

    if ordered[0].pcm == ordered[1].pcm:
        return DualAppleRuntimeMapping(False, "duplicate_child_pcm")
    return DualAppleRuntimeMapping(True, "ok", "saved_topology", tuple(ordered))


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
            "device_label": SUPPORTED_DEVICE_LABELS.get(child.device_id, child.label),
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


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    hat = read_hat_eeprom()
    cards = probe_system_cards(
        sys_class_sound=os.environ.get("JASPER_SYS_CLASS_SOUND", "/sys/class/sound"),
        proc_asound=os.environ.get("JASPER_PROC_ASOUND", "/proc/asound"),
        hat=hat,
    )
    if not cards:
        listing = probe_aplay_listing(os.environ.get("JASPER_APLAY", "aplay"))
        cards = parse_aplay_listing(listing, hat=hat)
    state = apply_saved_topology_policy(classify_output_cards(cards), cards)
    state = replace(
        state,
        hat_eeprom=hat,
        usb_data_role=resolve_system_usb_port_role(
            observed_output_profile_id=state.profile_id,
        ),
    )
    if "--write" in argv:
        write_state(state)
    print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
