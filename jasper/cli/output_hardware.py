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
from dataclasses import dataclass, replace

from jasper.audio_hardware.dac import kind_for, percent_pinned_control_for
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


def _flag(value: bool | None) -> str:
    # `true`/`false`, the spelling `publish_management_transport_marker`
    # compares against; empty for no port-role record at all.
    if value is None:
        return ""
    return "true" if value else "false"


@dataclass(frozen=True)
class ObservedOutput:
    """One classification of the attached output hardware, as the DAC-role
    policy consumes it.

    :func:`env_values` is these same facts flattened for the one remaining
    SHELL reader (``deploy/bin/jasper-headphone-monitor``); the Python policy
    layer holds this instead, so nothing re-parses that flattening. The
    all-default instance is what an ABSENT record means (ADR-0235 R2).
    """

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
    #: record is not carried here: the boot-config CLI owns it and reports it
    #: on stderr as ``event=hardware.usb_role_resolved`` (ADR-0235 R4).
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


def observed_output(
    state: OutputHardwareState,
    cards: tuple[OutputCardFact, ...],
    *,
    record_changed: bool = False,
) -> ObservedOutput:
    """The classifier's verdict as the one typed observation both readers share."""
    usb = state.usb_data_role
    mapping = dual_apple_runtime_mapping(state)
    # Padded so an absent or partial composite still answers both PCM keys.
    pcms = [child.pcm or "" for child in mapping.child_devices] + ["", ""]
    return ObservedOutput(
        profile_id=state.profile_id,
        status=state.status,
        kind=kind_for(state.profile_id) or "",
        headphone_control=(
            percent_pinned_control_for(state.active_profile_id or "") or ""
        ),
        selected_card_id=state.selected_card_id or "",
        child_device_ids=tuple(child.device_id for child in state.child_devices),
        apple_card_ids=tuple(apple_output_card_ids(cards)),
        blocker_codes=tuple(
            str(issue.get("code") or "unnamed")
            for issue in state.issues
            if issue.get("severity") == "blocker"
        ),
        record_changed=record_changed,
        management_transport_available=(
            usb.management_transport_available if usb else None
        ),
        dual_mapping_ok=mapping.ok,
        dual_mapping_reason=mapping.reason,
        dual_order_source=mapping.order_source,
        dual_dac_a_pcm=pcms[0],
        dual_dac_b_pcm=pcms[1],
    )


def env_values(
    state: OutputHardwareState,
    cards: tuple[OutputCardFact, ...],
    *,
    record_changed: bool = False,
) -> dict[str, str]:
    """The whole SHELL contract as ``{KEY: value}``, unquoted."""
    observed = observed_output(state, cards, record_changed=record_changed)
    return {
        "OBSERVED_OUTPUT_PROFILE_ID": observed.profile_id,
        "OBSERVED_OUTPUT_PROFILE_STATUS": observed.status,
        "OBSERVED_OUTPUT_PROFILE_KIND": observed.kind,
        "OBSERVED_OUTPUT_HEADPHONE_CONTROL": observed.headphone_control,
        "OBSERVED_OUTPUT_SELECTED_CARD_ID": observed.selected_card_id,
        "OBSERVED_OUTPUT_CHILD_DEVICE_IDS": " ".join(observed.child_device_ids),
        "OBSERVED_OUTPUT_APPLE_CARD_IDS": " ".join(observed.apple_card_ids),
        "OBSERVED_OUTPUT_BLOCKER_CODES": ",".join(observed.blocker_codes),
        "OBSERVED_OUTPUT_RECORD_CHANGED": "1" if observed.record_changed else "0",
        "OBSERVED_OUTPUT_USB_MANAGEMENT_TRANSPORT_AVAILABLE": _flag(
            observed.management_transport_available
        ),
        "OBSERVED_OUTPUT_DUAL_MAPPING_OK": "1" if observed.dual_mapping_ok else "0",
        "OBSERVED_OUTPUT_DUAL_MAPPING_REASON": observed.dual_mapping_reason,
        "OBSERVED_OUTPUT_DUAL_ORDER_SOURCE": observed.dual_order_source,
        "OBSERVED_OUTPUT_DUAL_DAC_A_PCM": observed.dual_dac_a_pcm,
        "OBSERVED_OUTPUT_DUAL_DAC_B_PCM": observed.dual_dac_b_pcm,
    }


def env_lines(
    state: OutputHardwareState,
    cards: tuple[OutputCardFact, ...],
    *,
    record_changed: bool = False,
) -> str:
    """The whole shell contract, one shlex-quoted ``KEY=value`` line per fact."""
    return "".join(
        f"{key}={shlex.quote(value)}\n"
        for key, value in env_values(
            state, cards, record_changed=record_changed
        ).items()
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state, cards, record_changed = observe(write=args.write)
    if args.env:
        print(env_lines(state, cards, record_changed=record_changed), end="")
    else:
        print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
