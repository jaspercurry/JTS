# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit the observed final-output hardware profile.

The bridge between the Python classifier in ``jasper.output_hardware`` and
the shell-only policy layer ``jasper-audio-hardware-reconcile``, the same
shape ``jasper.cli.xvf_profile`` is for the input side. One spawn publishes
the JSON record and prints the ``KEY=value`` lines the shell evals, so the
shell parses no JSON and holds no hardware label (ADR-0235 R2).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import replace

from jasper.audio_hardware.hat_eeprom import read_hat_eeprom
from jasper.audio_hardware.usb_port_role import resolve_system_usb_port_role
from jasper.output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    apple_output_card_ids,
    apply_saved_topology_policy,
    classify_output_cards,
    dual_apple_runtime_mapping,
    load_state,
    parse_aplay_listing,
    probe_aplay_listing,
    probe_system_cards,
    write_state,
)


def _flag(value: bool) -> str:
    # `true`/`false`, the spelling `publish_management_transport_marker`
    # compares against.
    return "true" if value else "false"


def env_lines(
    state: OutputHardwareState,
    cards: tuple[OutputCardFact, ...],
    *,
    record_changed: bool = False,
) -> str:
    """The whole shell contract, one shlex-quoted ``KEY=value`` line per fact."""
    usb = state.usb_data_role
    mapping = dual_apple_runtime_mapping(state)
    # Padded so an absent or partial composite still answers both PCM keys.
    pcms = [child.pcm or "" for child in mapping.child_devices] + ["", ""]
    values = {
        "OBSERVED_OUTPUT_PROFILE_ID": state.profile_id,
        "OBSERVED_OUTPUT_PROFILE_STATUS": state.status,
        "OBSERVED_OUTPUT_SELECTED_CARD_ID": state.selected_card_id or "",
        "OBSERVED_OUTPUT_CHILD_DEVICE_IDS": " ".join(
            child.device_id for child in state.child_devices
        ),
        "OBSERVED_OUTPUT_APPLE_CARD_IDS": " ".join(apple_output_card_ids(cards)),
        "OBSERVED_OUTPUT_BLOCKER_CODES": ",".join(
            str(issue.get("code") or "unnamed")
            for issue in state.issues
            if issue.get("severity") == "blocker"
        ),
        "OBSERVED_OUTPUT_RECORD_CHANGED": "1" if record_changed else "0",
        # The rest of the port-role record is not re-emitted here: the
        # boot-config CLI owns it and reports it on stderr as
        # `event=hardware.usb_role_resolved` (ADR-0235 R4).
        "OBSERVED_OUTPUT_USB_MANAGEMENT_TRANSPORT_AVAILABLE": (
            _flag(usb.management_transport_available) if usb else ""
        ),
        "OBSERVED_OUTPUT_DUAL_MAPPING_OK": "1" if mapping.ok else "0",
        "OBSERVED_OUTPUT_DUAL_MAPPING_REASON": mapping.reason,
        "OBSERVED_OUTPUT_DUAL_ORDER_SOURCE": mapping.order_source,
        "OBSERVED_OUTPUT_DUAL_DAC_A_PCM": pcms[0],
        "OBSERVED_OUTPUT_DUAL_DAC_B_PCM": pcms[1],
    }
    return "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically publish the observed record as the JSON state artifact",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print shell-safe environment assignments instead of JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    record_changed = False
    if args.write:
        # Read before the write replaces it: the identity the mixer pin
        # depends on (which profile, on which card). An absent or unreadable
        # record reads as no identity, so a first write counts as a change.
        previous = load_state()
        record_changed = previous is None or (
            previous.profile_id != state.profile_id
            or previous.selected_card_id != state.selected_card_id
        )
        write_state(state)
    if args.env:
        print(env_lines(state, cards, record_changed=record_changed), end="")
    else:
        print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
