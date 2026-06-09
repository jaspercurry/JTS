"""Read-only output hardware profile classification.

This module is the output-side counterpart to ``audio_profile_state``:
small, import-cheap, and side-effect-free unless a caller explicitly asks
for a live probe or state-file write. It turns observed ALSA/USB facts into
the shared output-profile vocabulary used by reconcile, `/state`, doctor,
and `/sound/`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X_ID,
    HIFIBERRY_DAC8X_STUDIO_ID,
    all_profiles as _all_dac_profiles,
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
        return cls(
            profile_id=profile_id,
            profile_label=_text(raw.get("profile_label"))
            or SUPPORTED_DEVICE_LABELS.get(profile_id, profile_id),
            status=_text(raw.get("status")) or "unknown",
            physical_output_count=int(raw.get("physical_output_count") or 0),
            selected_card_id=_text(raw.get("selected_card_id")),
            selected_pcm=_text(raw.get("selected_pcm")),
            apple_dac_count=int(raw.get("apple_dac_count") or len(children)),
            child_devices=children,
            issues=issues,
            observed_at=_text(raw.get("observed_at")),
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
        }
        if self.selected_card_id:
            out["selected_card_id"] = self.selected_card_id
        if self.selected_pcm:
            out["selected_pcm"] = self.selected_pcm
        if self.observed_at:
            out["observed_at"] = self.observed_at
        return out


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


def classify_output_cards(
    cards: Iterable[OutputCardFact],
    *,
    observed_at: str | None = None,
) -> OutputHardwareState:
    """Classify output hardware from already-collected card facts."""

    facts = tuple(card for card in cards if card.has_playback)
    apple = tuple(
        card for card in facts
        if card.device_id == APPLE_USB_C_DONGLE_DEVICE_ID
        or (
            (card.vendor_id or "").lower() == APPLE_USB_VENDOR_ID
            and (card.product_id or "").lower() == APPLE_USB_PRODUCT_ID
        )
    )
    dac8x_studio = next(
        (card for card in facts if card.device_id == HIFIBERRY_DAC8X_STUDIO_DEVICE_ID),
        None,
    )
    dac8x = next(
        (card for card in facts if card.device_id == HIFIBERRY_DAC8X_DEVICE_ID),
        None,
    )
    observed_at = observed_at or _utc_now()

    if dac8x_studio:
        return OutputHardwareState(
            profile_id=HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
            profile_label=SUPPORTED_DEVICE_LABELS[HIFIBERRY_DAC8X_STUDIO_DEVICE_ID],
            status="ready",
            physical_output_count=8,
            selected_card_id=dac8x_studio.card_id,
            selected_pcm=dac8x_studio.pcm,
            apple_dac_count=len(apple),
            child_devices=(dac8x_studio,),
            observed_at=observed_at,
        )
    if dac8x:
        return OutputHardwareState(
            profile_id=HIFIBERRY_DAC8X_DEVICE_ID,
            profile_label=SUPPORTED_DEVICE_LABELS[HIFIBERRY_DAC8X_DEVICE_ID],
            status="ready",
            physical_output_count=8,
            selected_card_id=dac8x.card_id,
            selected_pcm=dac8x.pcm,
            apple_dac_count=len(apple),
            child_devices=(dac8x,),
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


def parse_aplay_listing(listing: str) -> tuple[OutputCardFact, ...]:
    """Best-effort parser for ``aplay -L`` used by shell reconcile tests."""

    cards: list[OutputCardFact] = []
    lines = listing.splitlines()
    for index, line in enumerate(lines):
        match = _CARD_RE.match(line.strip())
        if not match:
            continue
        card_id = match.group(1)
        label = lines[index + 1].strip() if index + 1 < len(lines) else ""
        lower = label.lower()
        if "usb-c to 3.5mm" in lower or (
            "apple" in lower and "usb audio" in lower
        ):
            device_id = APPLE_USB_C_DONGLE_DEVICE_ID
            vendor_id = APPLE_USB_VENDOR_ID
            product_id = APPLE_USB_PRODUCT_ID
        elif "dac8x" in lower and "studio" in lower:
            device_id = HIFIBERRY_DAC8X_STUDIO_DEVICE_ID
            vendor_id = None
            product_id = None
        elif "snd_rpi_hifiberry_dac8x" in lower or "dac8x" in lower:
            device_id = HIFIBERRY_DAC8X_DEVICE_ID
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
        if name.startswith("xhci-hcd.") or name.startswith("usb"):
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
        stream = _read_text(proc_root / f"card{card_index}" / "stream0")
        if (
            (vendor_id or "").lower() == APPLE_USB_VENDOR_ID
            and (product_id or "").lower() == APPLE_USB_PRODUCT_ID
        ):
            device_id = APPLE_USB_C_DONGLE_DEVICE_ID
        elif product and "dac8x" in product.lower() and "studio" in product.lower():
            device_id = HIFIBERRY_DAC8X_STUDIO_DEVICE_ID
        elif product and "dac8x" in product.lower():
            device_id = HIFIBERRY_DAC8X_DEVICE_ID
        else:
            device_id = "unknown"
        cards.append(OutputCardFact(
            card_id=card_id,
            card_index=card_index,
            label=product or card_id,
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


def classify_aplay_listing(listing: str) -> OutputHardwareState:
    return classify_output_cards(parse_aplay_listing(listing))


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


def write_state(state: OutputHardwareState, path: str | Path | None = None) -> None:
    target = state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_name = handle.name
        handle.write(data)
    os.chmod(tmp_name, 0o644)
    os.replace(tmp_name, target)


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


def _load_dual_apple_topology_children(
    path: str | Path | None = None,
) -> tuple[Mapping[str, Any], ...] | None:
    target = _topology_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, Mapping):
        return ()
    hardware = raw.get("hardware")
    if not isinstance(hardware, Mapping):
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
    cards = probe_system_cards(
        sys_class_sound=os.environ.get("JASPER_SYS_CLASS_SOUND", "/sys/class/sound"),
        proc_asound=os.environ.get("JASPER_PROC_ASOUND", "/proc/asound"),
    )
    if not cards:
        listing = probe_aplay_listing(os.environ.get("JASPER_APLAY", "aplay"))
        cards = parse_aplay_listing(listing)
    state = classify_output_cards(cards)
    if "--write" in argv:
        write_state(state)
    print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
