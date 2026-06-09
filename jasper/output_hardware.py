"""Observed-vs-active final-output hardware state.

The DAC profile registry is static. This module is the small runtime
classification layer around it: callers pass already-observed ALSA/sysfs facts
and get a JSON-able state object that status surfaces, doctor, and reconcile
can share. Importing this module does not open audio devices, restart services,
or read hardware unless a caller explicitly invokes a probe.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X_ID,
    DacProfile,
    by_id,
)


SCHEMA_VERSION = 1
OUTPUT_HARDWARE_STATE_KIND = "jts_output_hardware_state"
DEFAULT_STATE_PATH = "/run/jasper/output_hardware.json"

APPLE_USB_ID = "05ac:110a"
_CARD_RE = re.compile(r"^hw:CARD=([^,\s]+),DEV=(\d+)")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str | None:
    if value is None:
        return None
    out = str(value).strip().strip("'\"")
    return out or None


def normalize_profile_id(raw: str | None) -> str:
    value = (raw or "").strip().strip("'\"").lower().replace("-", "_")
    return value or "unknown"


def _profile(profile_id: str | None) -> DacProfile | None:
    return by_id(normalize_profile_id(profile_id))


def _profile_label(profile_id: str | None) -> str:
    normalized = normalize_profile_id(profile_id)
    profile = by_id(normalized)
    return profile.label if profile else normalized


def _profile_output_count(profile_id: str | None) -> int:
    profile = _profile(profile_id)
    return profile.physical_output_count if profile else 0


def _profile_outputd_sink(profile_id: str | None) -> str:
    profile = _profile(profile_id)
    return profile.outputd_sink if profile else "unknown"


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


@dataclass(frozen=True)
class OutputCardFact:
    """One observed playback card, with USB identity when available."""

    card_id: str
    label: str = ""
    profile_id: str = "unknown"
    card_index: int | None = None
    pcm: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    serial: str | None = None
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
        index_raw = raw.get("card_index")
        try:
            card_index = None if index_raw is None else int(index_raw)
        except (TypeError, ValueError):
            card_index = None
        return cls(
            card_id=card_id,
            label=_text(raw.get("label")) or "",
            profile_id=normalize_profile_id(_text(raw.get("profile_id"))),
            card_index=card_index,
            pcm=_text(raw.get("pcm")) or f"hw:CARD={card_id},DEV=0",
            vendor_id=_text(raw.get("vendor_id")),
            product_id=_text(raw.get("product_id")),
            serial=_text(raw.get("serial")),
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
            "profile_id": self.profile_id,
            "profile_label": _profile_label(self.profile_id),
            "label": self.label,
            "pcm": self.pcm or f"hw:CARD={self.card_id},DEV=0",
            "has_playback": self.has_playback,
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


def state_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_OUTPUT_HARDWARE_STATE_PATH")
        or DEFAULT_STATE_PATH
    )


def _same_usb_bus(cards: Sequence[OutputCardFact]) -> bool | None:
    busnums = [card.busnum for card in cards]
    if not busnums or any(busnum is None for busnum in busnums):
        return None
    controllers = [card.controller for card in cards]
    if all(controller is not None for controller in controllers):
        return len(set(zip(controllers, busnums))) == 1
    return len(set(busnums)) == 1


def _is_apple(card: OutputCardFact) -> bool:
    usb_id = f"{(card.vendor_id or '').lower()}:{(card.product_id or '').lower()}"
    return card.profile_id == APPLE_USB_C_DONGLE_ID or usb_id == APPLE_USB_ID


def _is_dac8x(card: OutputCardFact) -> bool:
    return card.profile_id == HIFIBERRY_DAC8X_ID


def _snapshot(
    *,
    profile_id: str,
    status: str,
    card_id: str | None = None,
    recognized: bool = False,
    runtime_ready: bool = False,
    physical_output_count: int | None = None,
    apple_dac_count: int = 0,
    same_usb_bus: bool | None = None,
) -> dict[str, Any]:
    normalized = normalize_profile_id(profile_id)
    out: dict[str, Any] = {
        "profile_id": normalized,
        "profile_label": _profile_label(normalized),
        "status": status,
        "recognized": recognized,
        "runtime_ready": runtime_ready,
        "physical_output_count": (
            _profile_output_count(normalized)
            if physical_output_count is None else physical_output_count
        ),
        "outputd_sink": _profile_outputd_sink(normalized),
    }
    if card_id:
        out["card_id"] = card_id
    if apple_dac_count:
        out["apple_dac_count"] = apple_dac_count
    if same_usb_bus is not None:
        out["same_usb_bus"] = same_usb_bus
    return out


def classify_output_cards(
    cards: Iterable[OutputCardFact],
    *,
    active_profile_id: str,
    active_card_id: str | None,
    active_recognized: bool,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build the shared output-hardware state from card facts.

    ``active_*`` is the reconciler-owned runtime role. ``observed`` is the
    best currently attached hardware shape. Keeping both in one state file is
    what lets UI/doctor say "two Apple adapters are present" without implying
    outputd has already switched to a dual-sink graph.
    """

    facts = tuple(card for card in cards if card.has_playback)
    active_normalized = normalize_profile_id(
        active_profile_id if active_recognized else "unknown"
    )
    if (
        active_recognized
        and active_card_id
        and by_id(active_normalized) is not None
        and not any(card.profile_id == active_normalized for card in facts)
    ):
        # Some non-USB cards, notably the I2S DAC8x path, are best identified
        # by the reconciler's ALSA listing. Preserve that recognized active
        # role in the state artifact even when sysfs alone is sparse.
        facts = facts + (
            OutputCardFact(
                card_id=active_card_id,
                label=_profile_label(active_normalized),
                profile_id=active_normalized,
            ),
        )
    apple = tuple(card for card in facts if _is_apple(card))
    dac8x = next((card for card in facts if _is_dac8x(card)), None)
    issues: list[dict[str, str]] = []
    observed_at = observed_at or _utc_now()

    if dac8x is not None:
        observed = _snapshot(
            profile_id=HIFIBERRY_DAC8X_ID,
            status="ready",
            card_id=dac8x.card_id,
            recognized=True,
            runtime_ready=True,
            apple_dac_count=len(apple),
        )
        children = (dac8x,)
    elif len(apple) == 2:
        same_bus = _same_usb_bus(apple)
        status = "ready"
        if same_bus is False:
            status = "blocked"
            issues.append(issue(
                "blocker",
                "dual_apple_usb_topology_mismatch",
                "two Apple DACs are present but are not on the same USB controller/bus",
            ))
        elif same_bus is None:
            status = "blocked"
            issues.append(issue(
                "blocker",
                "dual_apple_usb_topology_unknown",
                "two Apple DACs are present but USB controller/bus facts are unavailable",
            ))
        missing_identity = [
            card.card_id for card in apple
            if not (card.serial or card.stable_path or card.usb_path)
        ]
        if missing_identity:
            status = "blocked"
            issues.append(issue(
                "blocker",
                "dual_apple_stable_identity_missing",
                "two Apple DACs are present but at least one child lacks stable identity",
            ))
        observed = _snapshot(
            profile_id=DUAL_APPLE_USB_C_DAC_4CH_ID,
            status=status,
            recognized=status == "ready",
            runtime_ready=False,
            apple_dac_count=2,
            same_usb_bus=same_bus,
        )
        children = apple
    elif len(apple) == 1:
        observed = _snapshot(
            profile_id=APPLE_USB_C_DONGLE_ID,
            status="ready",
            card_id=apple[0].card_id,
            recognized=True,
            runtime_ready=True,
            apple_dac_count=1,
        )
        children = apple
    elif len(apple) > 2:
        observed = _snapshot(
            profile_id="unknown",
            status="unsupported",
            recognized=False,
            runtime_ready=False,
            physical_output_count=0,
            apple_dac_count=len(apple),
        )
        children = apple
        issues.append(issue(
            "blocker",
            "too_many_apple_dacs",
            "more than two Apple DACs are attached; 3-DAC/subwoofer output is not supported yet",
        ))
    else:
        observed = _snapshot(
            profile_id="unknown",
            status="missing",
            recognized=False,
            runtime_ready=False,
            physical_output_count=0,
        )
        children = ()

    active_profile = _profile(active_normalized)
    active_runtime_ready = bool(
        active_recognized
        and active_profile is not None
        and active_profile.outputd_sink == "alsa"
    )
    active = _snapshot(
        profile_id=active_normalized,
        status="active" if active_recognized else "parked",
        card_id=active_card_id,
        recognized=active_recognized,
        runtime_ready=active_runtime_ready,
    )

    if observed["profile_id"] != active["profile_id"]:
        issues.append(issue(
            "warning",
            "observed_active_profile_mismatch",
            (
                f"observed output hardware is {observed['profile_id']} but the "
                f"active runtime profile is {active['profile_id']}"
            ),
        ))
    if (
        observed["profile_id"] == DUAL_APPLE_USB_C_DAC_4CH_ID
        and active_normalized != DUAL_APPLE_USB_C_DAC_4CH_ID
    ):
        issues.append(issue(
            "warning",
            "dual_apple_runtime_handoff_pending",
            "dual Apple hardware is observed, but outputd has not activated a dual-Apple runtime sink",
        ))

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": OUTPUT_HARDWARE_STATE_KIND,
        "observed_at": observed_at,
        "active": active,
        "observed": observed,
        "child_devices": [child.to_dict() for child in children],
        "issues": issues,
    }


def parse_aplay_listing(listing: str) -> tuple[OutputCardFact, ...]:
    """Best-effort parser for ``aplay -L`` fixtures and fallback probes."""

    cards: list[OutputCardFact] = []
    lines = listing.splitlines()
    for index, line in enumerate(lines):
        match = _CARD_RE.match(line.strip())
        if not match:
            continue
        card_id = match.group(1)
        label = lines[index + 1].strip() if index + 1 < len(lines) else ""
        lower = label.lower()
        if "usb-c to 3.5mm" in lower or ("apple" in lower and "usb audio" in lower):
            profile_id = APPLE_USB_C_DONGLE_ID
            vendor_id, product_id = APPLE_USB_ID.split(":", 1)
        elif (
            "snd_rpi_hifiberry_dac8x" in lower
            or "hifiberry" in lower
            or "dac8x" in lower
        ):
            profile_id = HIFIBERRY_DAC8X_ID
            vendor_id = product_id = None
        else:
            profile_id = "unknown"
            vendor_id = product_id = None
        cards.append(OutputCardFact(
            card_id=card_id,
            label=label,
            profile_id=profile_id,
            pcm=f"hw:CARD={card_id},DEV={match.group(2)}",
            vendor_id=vendor_id,
            product_id=product_id,
        ))
    return tuple(cards)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _find_usb_device(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except OSError:
        return None
    for item in (current, *current.parents):
        if (item / "idVendor").exists() and (item / "idProduct").exists():
            return item
    return None


def _find_controller(path: Path) -> str | None:
    try:
        current = path.resolve()
    except OSError:
        return None
    for item in (current, *current.parents):
        if item.name.startswith("xhci-hcd."):
            return item.name
    return None


def _endpoint_sync_from_stream(stream: str | None) -> str | None:
    if not stream:
        return None
    for token in ("SYNC", "ASYNC", "ADAPTIVE"):
        if f"({token})" in stream:
            return token
    return None


def _profile_from_sysfs(
    *,
    vendor_id: str | None,
    product_id: str | None,
    product: str | None,
    fallback_label: str,
) -> str:
    usb_id = f"{(vendor_id or '').lower()}:{(product_id or '').lower()}"
    if usb_id == APPLE_USB_ID:
        return APPLE_USB_C_DONGLE_ID
    haystack = " ".join(part for part in (product, fallback_label) if part).lower()
    if "dac8x" in haystack:
        return HIFIBERRY_DAC8X_ID
    return "unknown"


def probe_system_cards(
    *,
    sys_class_sound: str | Path = "/sys/class/sound",
    proc_asound: str | Path = "/proc/asound",
) -> tuple[OutputCardFact, ...]:
    """Probe ALSA/sysfs card facts without opening PCM streams."""

    sys_root = Path(sys_class_sound)
    proc_root = Path(proc_asound)
    try:
        card_dirs = sorted(
            item for item in sys_root.glob("card[0-9]*")
            if item.name[4:].isdigit()
        )
    except OSError:
        return ()

    cards: list[OutputCardFact] = []
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
        profile_id = _profile_from_sysfs(
            vendor_id=vendor_id,
            product_id=product_id,
            product=product,
            fallback_label=card_id,
        )
        cards.append(OutputCardFact(
            card_id=card_id,
            label=product or card_id,
            profile_id=profile_id,
            card_index=card_index,
            pcm=f"hw:CARD={card_id},DEV=0",
            vendor_id=vendor_id,
            product_id=product_id,
            serial=serial,
            stable_path=str(real),
            usb_path=usb.name if usb else None,
            controller=_find_controller(real),
            busnum=busnum,
            devpath=devpath,
            endpoint_sync=_endpoint_sync_from_stream(stream),
            has_playback=has_playback,
        ))
    return tuple(cards)


def load_state(path: str | Path | None = None) -> dict[str, Any] | None:
    try:
        raw = json.loads(state_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("artifact_schema_version") != SCHEMA_VERSION:
        return None
    if raw.get("kind") != OUTPUT_HARDWARE_STATE_KIND:
        return None
    return raw


def write_state(state: Mapping[str, Any], path: str | Path | None = None) -> None:
    target = state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as handle:
        tmp_name = handle.name
        handle.write(data)
    os.chmod(tmp_name, 0o644)
    os.replace(tmp_name, target)


def topology_hardware_mapping(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an ``OutputHardware`` mapping for the best observed hardware.

    This is used only for a new draft when no saved output topology exists.
    It does not authorize playback; clock-domain/readiness checks still gate
    sound separately.
    """

    observed = state.get("observed") if isinstance(state, Mapping) else None
    if not isinstance(observed, Mapping):
        return None
    if observed.get("status") != "ready":
        return None
    profile_id = normalize_profile_id(_text(observed.get("profile_id")))
    if profile_id == "unknown":
        return None
    count = int(observed.get("physical_output_count") or 0)
    if count <= 0:
        return None
    outputs = [
        {
            "index": index,
            "human_label": f"DAC output {index + 1}",
            "terminal_label": str(index + 1),
        }
        for index in range(count)
    ]
    if profile_id == DUAL_APPLE_USB_C_DAC_4CH_ID:
        labels = (
            ("Apple DAC 1 left", "A-L"),
            ("Apple DAC 1 right", "A-R"),
            ("Apple DAC 2 left", "B-L"),
            ("Apple DAC 2 right", "B-R"),
        )
        outputs = [
            {"index": index, "human_label": label, "terminal_label": terminal}
            for index, (label, terminal) in enumerate(labels)
        ]
    out: dict[str, Any] = {
        "device_id": profile_id,
        "device_label": _profile_label(profile_id),
        "physical_output_count": count,
        "outputs": outputs,
    }
    card_id = _text(observed.get("card_id"))
    if card_id:
        out["card_id"] = card_id
    children = state.get("child_devices")
    if isinstance(children, list):
        out["child_devices"] = children
    return out


def _cards_from_cli(args: argparse.Namespace) -> tuple[OutputCardFact, ...]:
    cards = probe_system_cards(
        sys_class_sound=args.sys_class_sound,
        proc_asound=args.proc_asound,
    )
    if cards:
        return cards
    if args.aplay_listing:
        return parse_aplay_listing(Path(args.aplay_listing).read_text(encoding="utf-8"))
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify JTS output hardware")
    parser.add_argument(
        "--active-profile-id",
        default=os.environ.get("JASPER_AUDIO_DAC_ID", "unknown"),
    )
    parser.add_argument(
        "--active-card-id",
        default=os.environ.get("JASPER_AUDIO_DAC_CARD"),
    )
    parser.add_argument("--active-recognized", action="store_true")
    parser.add_argument("--state-path", default=None)
    parser.add_argument(
        "--sys-class-sound",
        default=os.environ.get("JASPER_SYS_CLASS_SOUND", "/sys/class/sound"),
    )
    parser.add_argument(
        "--proc-asound",
        default=os.environ.get("JASPER_PROC_ASOUND", "/proc/asound"),
    )
    parser.add_argument("--aplay-listing", default=None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    state = classify_output_cards(
        _cards_from_cli(args),
        active_profile_id=args.active_profile_id,
        active_card_id=args.active_card_id,
        active_recognized=args.active_recognized,
    )
    if args.write:
        write_state(state, args.state_path)
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
