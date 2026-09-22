# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read the input owners once before a reconcile pass changes runtime state."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Mapping

from jasper.accessories.mic_env import read_accessory_mic_sources
from jasper.audio_measurement.mic_identity import measurement_mic_usb_ids, read_card_usb_id
from jasper.chip_aec.policy import (
    ChipAecGate, gate_from_runtime_env, normalize_dac_id, resolve_chip_aec_dac_gate,
)
from jasper.mics import xvf3800
from jasper.platform.status_socket import read_status_socket


@dataclass(frozen=True)
class Observation:
    mic: xvf3800.RuntimeProfile | None
    channels: Mapping[str, int | None]
    measurement_cards: frozenset[str]
    accessory_sources: tuple[str, ...]
    accessory_status: str
    dac_gate: ChipAecGate


def card_id(spec: str) -> str:
    match = re.search(r"CARD=([^,]+)", spec) or re.match(r"(?:hw|plughw):([^,]+)", spec)
    return match.group(1) if match else spec


def observe(values: Mapping[str, str], asound_root: Path, outputd_socket: str, log) -> Observation:
    mic = None
    try:
        mic = xvf3800.detect_runtime_profile(asound_root=asound_root)
    except (OSError, ValueError) as exc:
        log(f"event=aec_reconcile.mic_profile status=failed error={exc}")
    candidates = values.get("JASPER_MIC_DEVICE_CANDIDATES", "")
    cards = dict.fromkeys((
        *xvf3800.ALSA_CARD_NAMES,
        *(card_id(value) for value in candidates.replace(",", " ").split()),
        card_id(values.get("JASPER_AEC_MIC_DEVICE", "")),
        mic.alsa_card_name if mic else "",
    ))
    channels = {}
    measurement = set()
    registered = None
    for card in cards:
        if not card:
            continue
        stream = asound_root / card / "stream0"
        if not stream.is_file():
            continue
        channels[card] = (
            mic.capture_channels if mic and card == mic.alsa_card_name
            else xvf3800._capture_channels_for_card(card, asound_root=asound_root)
        )
        usb_id = read_card_usb_id(asound_root / card)
        if usb_id:
            if registered is None:
                try:
                    registered = set(measurement_mic_usb_ids())
                except (OSError, ValueError) as exc:
                    registered = set()
                    log(f"event=aec_reconcile.measurement_registry status=failed error={exc}")
            if usb_id in registered:
                measurement.add(card)
    try:
        sources = read_accessory_mic_sources()
        accessory_status = "resolved"
    except (OSError, UnicodeError, ValueError) as exc:
        sources, accessory_status = (), "failed"
        log(f"event=aec_reconcile.accessory_mic status=failed error={exc}")
    status, error = None, ""
    try:
        status = read_status_socket(outputd_socket)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)
    dac_id = normalize_dac_id(values.get("JASPER_AUDIO_DAC_ID", "unknown"))
    try:
        gate = resolve_chip_aec_dac_gate(
            dac_id, outputd_status=status, outputd_error=error,
            testing_requested=values.get("JASPER_AUDIO_INPUT_PROFILE") == "xvf_chip_aec_testing",
        )
    except (OSError, ValueError) as exc:
        gate = gate_from_runtime_env(values)
        if gate is None:
            gate = ChipAecGate(dac_id, "needs_calibration", "policy_unavailable", str(exc), False)
        else:
            note = "chip-AEC DAC gate could not be evaluated; carrying last verdict"
            gate = replace(gate, source="runtime_env_carried", detail=f"{gate.detail.removesuffix('; ' + note)}; {note}".lstrip('; '))
        log(f"event=aec_reconcile.dac_gate status=failed error={exc}")
    return Observation(mic, channels, frozenset(measurement), sources, accessory_status, gate)
