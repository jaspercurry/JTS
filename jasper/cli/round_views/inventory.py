# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which view artifacts a round already carries, and what produces the rest.

* ``inventory <round-dir>`` — which of the named view artifacts this round
  already has, and the subcommand that produces each one it is missing, so
  "was this round ever analysed for X" is read rather than re-run. The round
  is resolved, never graded, and each ``produced_by`` places it in the slot
  that writes the artifact beside it. Presence is read at the path each view
  writes with no ``--out``, so a LIVE session bundle's artifacts are looked
  for beside the caller, not in the bundle (:func:`default_out`). Writes
  ``inventory.json``.

"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    INVENTORY_ARTIFACT,
    PROG,
    TAKES_THIS_ROUND,
    _ROUND_DIR_HELP,
    _ROUND_TOOL_ERRORS,
    _write,
    default_out,
)

def _cmd_inventory(args: argparse.Namespace) -> int:
    # The round is RESOLVED, never graded: which artifacts sit beside a round
    # is a directory question, and building the evidence packet to answer it
    # would make the cheapest verb here cost the most (415 MB target, ADR-0226).
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    # ``None`` only for a directory holding no round artifacts at all, where
    # every row below is missing whichever path it is read at.
    artifact_dir, _why = round_artifact_dir(inputs.session_dir)
    artifacts: list[dict[str, Any]] = []
    for view, spec in ARTIFACT_BY_VIEW.items():
        path = (
            artifact_dir / spec.artifact
            if spec.in_artifact_dir and artifact_dir is not None
            else default_out(inputs, round_dir, spec.artifact)
        )
        artifacts.append({
            "artifact": spec.artifact,
            "path": str(path),
            "present": path.is_file(),
            "produced_by": f"{PROG} {view} {spec.takes}",
            "producer_needs_more_than_this_round": spec.takes != TAKES_THIS_ROUND,
        })
    payload = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "artifacts": artifacts,
    }
    written = _write(
        payload, args.out, default_out(inputs, round_dir, INVENTORY_ARTIFACT)
    )
    missing = [row["produced_by"] for row in artifacts if not row["present"]]
    print(
        f"inventory: {len(artifacts) - len(missing)}/{len(artifacts)} "
        f"artifact(s) present"
        + (f"; missing: {', '.join(missing)}" if missing else "")
        + (f" -> {written}" if written else ""),
        file=sys.stderr,
    )
    return EXIT_OK


def add_parser(sub: argparse._SubParsersAction) -> None:
    inventory = sub.add_parser(
        "inventory",
        help="which view artifacts this round has, and what produces each missing one",
    )
    inventory.add_argument("round_dir", help=_ROUND_DIR_HELP)
    inventory.add_argument("--out", default=None, help="write the result here (- for stdout)")
    inventory.set_defaults(func=_cmd_inventory)
