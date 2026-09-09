# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Publish observed output hardware as JSON or shell assignments (ADR-0235 R2)."""

from __future__ import annotations

import argparse
import json

from jasper.output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    observe,
    observed_output,
)
from jasper.shell_env import render_shell_assignments


def _flag(value: bool | None) -> str:
    # `true`/`false`, the spelling `publish_management_transport_marker`
    # compares against; empty for no port-role record at all.
    if value is None:
        return ""
    return "true" if value else "false"


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
    return render_shell_assignments(
        env_values(state, cards, record_changed=record_changed)
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
    state, cards, record_changed = observe(write=args.write)
    if args.env:
        print(env_lines(state, cards, record_changed=record_changed), end="")
    else:
        print(json.dumps(state.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
