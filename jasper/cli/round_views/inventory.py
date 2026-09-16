# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Artifact presence, producer provenance and usable next commands for one round."""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.round_inventory import inventory_payload, inventory_summary
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    INVENTORY_ARTIFACT,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_set_argument, answer,
    default_out,
)


def _cmd_inventory(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    payload = inventory_payload(inputs, round_dir, args.set)
    summary = inventory_summary(payload)
    written = _write(
        payload, args.out, default_out(inputs, round_dir, INVENTORY_ARTIFACT, args.set)
    )
    missing = summary["missing"]
    return answer(
        args.command, out=written, **summary,
        line=(
            f"inventory: {summary['present']}/{summary['total']} "
            f"artifact(s) present"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    inventory = sub.add_parser(
        "inventory",
        help="which analysis artifacts this round has, and the command that produces each missing one",
    )
    inventory.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    add_set_argument(inventory)
    inventory.add_argument("--out", default=None, help="write the result here")
    inventory.set_defaults(func=_cmd_inventory)
