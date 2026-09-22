# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Acquire Linux ALSA/USB facts and publish the classified output hardware."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    APPLE_USB_VENDOR_ID,
    APPLE_USB_PRODUCT_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    load_state,
    write_state,
)
from jasper.output_topology_observation import apply_saved_topology_policy
from .dac import APPLE_USB_C_DONGLE, profile_for_card_label as _dac_profile_for_card_label
from .hat_eeprom import HatEeprom, read_hat_eeprom
from .usb_port_role import resolve_system_usb_port_role


DEFAULT_PROC_ASOUND_PATH = "/proc/asound"
_CARD_RE = re.compile(r"^hw:CARD=([^,\s]+),DEV=(\d+)")


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


def observe(
    *, write: bool = False
) -> tuple[OutputHardwareState, tuple[OutputCardFact, ...], bool]:
    """Classify the attached output hardware: ``(state, cards, record_changed)``.

    ``write`` publishes the JSON record; ``record_changed`` then says whether
    the record it replaced named a different profile or card.
    """
    hat = read_hat_eeprom()
    cards = probe_system_cards(
        sys_class_sound=os.environ.get("JASPER_SYS_CLASS_SOUND", "/sys/class/sound"),
        proc_asound=os.environ.get("JASPER_PROC_ASOUND", DEFAULT_PROC_ASOUND_PATH),
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
    record_changed = False
    if write:
        # Read before the write replaces it: the identity the mixer pin
        # depends on (which profile, on which card). An absent or unreadable
        # record reads as no identity, so a first write counts as a change.
        previous = load_state()
        record_changed = previous is None or (
            previous.profile_id != state.profile_id
            or previous.selected_card_id != state.selected_card_id
        )
        write_state(state)
    return state, cards, record_changed


# aplay -L enumerates in tens of ms; 2 s is far past a hung USB stack, well
# under the unit's 50 s TimeoutStartSec.
_APLAY_LISTING_TIMEOUT_SEC = 2.0


def probe_aplay_listing(aplay: str = "aplay") -> str:
    try:
        proc = subprocess.run(
            [aplay, "-L"],
            check=False,
            text=True,
            capture_output=True,
            timeout=_APLAY_LISTING_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return ""
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
