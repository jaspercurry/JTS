# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which analysis artifacts a round already carries, and what produces the rest.

* ``inventory <round-dir>`` — which of the named artifacts this round already
  has, how big each one is, and the command that produces each one it is
  missing, so "was this round ever analysed for X" is read rather than re-run.
  Each ``produced_by`` is a line to RUN: this round's own paths are filled in,
  and what is left in angle brackets is what the inventory cannot know (the
  other round of a comparison, the applied corner), which
  ``producer_needs_more_than_this_round`` flags. The round is resolved, never
  graded. Presence is read at the path each producer writes with no ``--out``
  (:func:`default_out`). Writes ``inventory.json``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    INVENTORY_ARTIFACT,
    PROG,
    TAKES_THIS_BUNDLE,
    TAKES_THIS_ROUND,
    ViewArtifact,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
)


def _runnable(view: str, spec: ViewArtifact, round_dir: Path, bundle: Path) -> str:
    """This producer as a line to run, with this round's own paths in it.

    The bundle substitution goes first: every bundle placeholder starts with
    the whole-round one, and replacing the shorter token first would leave a
    path spliced into the middle of the longer one.
    """
    takes = spec.takes.replace(TAKES_THIS_BUNDLE, str(bundle)).replace(
        TAKES_THIS_ROUND, str(round_dir)
    )
    return f"{spec.producer or f'{PROG} {view}'} {takes}"


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
        stat = path.stat() if path.is_file() else None
        produced_by = _runnable(view, spec, round_dir, inputs.session_dir)
        artifacts.append({
            "artifact": spec.artifact,
            "path": str(path),
            "present": stat is not None,
            "bytes": None if stat is None else stat.st_size,
            "produced_by": produced_by,
            # What is left in angle brackets after this round's own paths went
            # in is what no inventory of ONE round can fill.
            "producer_needs_more_than_this_round": "<" in produced_by,
        })
    bytes_total = sum(row["bytes"] or 0 for row in artifacts)
    payload = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "bytes_total": bytes_total,
        "artifacts": artifacts,
    }
    written = _write(
        payload, args.out, default_out(inputs, round_dir, INVENTORY_ARTIFACT)
    )
    missing = [row["produced_by"] for row in artifacts if not row["present"]]
    return answer(
        args.command, out=written, present=len(artifacts) - len(missing),
        total=len(artifacts), bytes_total=bytes_total, missing=missing,
        line=(
            f"inventory: {len(artifacts) - len(missing)}/{len(artifacts)} "
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
    inventory.add_argument("--out", default=None, help="write the result here (- for stdout)")
    inventory.set_defaults(func=_cmd_inventory)
