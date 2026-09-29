# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What every view here shares: the artifact table, the round reader, the
publisher, and the flags more than one subcommand takes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from jasper.active_speaker import round_bank
from jasper.active_speaker.answer_schemas import ANSWER_SCHEMAS as ANSWER_SCHEMAS
from jasper.active_speaker.round_view_artifacts import (
    PROG as PROG,
    ARTIFACT_BY_VIEW as ARTIFACT_BY_VIEW, INVENTORY_ARTIFACT as INVENTORY_ARTIFACT,
    VIEW_PURPOSES as VIEW_PURPOSES, ViewArtifact as ViewArtifact,
    TAKES_THIS_ROUND as TAKES_THIS_ROUND, TAKES_THIS_BUNDLE as TAKES_THIS_BUNDLE,
    context_artifacts as context_artifacts,
)
from jasper.active_speaker.crossover_v2.gate_sweep import DEFAULT_RUNGS_MS
from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundSetRefused as RoundSetRefused, SetTakes as SetTakes, read_run_manifest as read_run_manifest,
    resolve_set as resolve_set, ROUND_INPUT_ERRORS as _ROUND_TOOL_ERRORS,
    default_out as default_out, set_artifact_name as set_artifact_name,
    round_artifact_dir as round_artifact_dir, subject as subject,
    RoundViewsError, round_inputs,
)
from jasper.audio_measurement.evidence_reasons import (
    REASON_REFUSED as REASON_REFUSED,
    REASON_UNREADABLE as REASON_UNREADABLE,
    REASON_UNWRITABLE as REASON_UNWRITABLE,
)
from jasper.cli._refusal import (
    answer as answer,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    failed,
    stage,
)
from jasper.cli._report import write_report

AUTHORITY_TIER = "advisory (analysis views save artifacts)"

#: What every round-directory positional takes, said once. Both shapes: the
#: live one is what a round leaves on the speaker, the banked one is what the
#: bank made of it, named by its id or its directory.
_ROUND_DIR_HELP = "a banked round id or directory, or a live session bundle"

#: What the usage line calls a directory positional, so ``--help`` reads as
#: the shape to type rather than as this module's own parameter names.
_ROUND_DIR_METAVAR = "<round-dir>"
_BUNDLE_DIR_METAVAR = "<bundle-dir>"

#: Every argument, by dest, that names a round; ``build_parser`` lets each take
#: a banked round id through :func:`round_ref`.
ROUND_ARGUMENTS = frozenset({
    "round_dir", "round_dirs", "baseline_dir", "target_dir", "bundle_dir",
    "source_a", "source_b", "before", "after", "far_round", "close_round",
})


def round_ref(convert: Callable[[str], Any], value: str) -> Any:
    """``value`` as ``convert`` reads it, a banked round id first swapped for
    its directory. A ref that names nothing passes as typed, so the view says
    what it could not read; an id that is also another path is a usage error."""
    try:
        return convert(str(round_bank.resolve_round(value)))
    except round_bank.RoundBankError as exc:
        if exc.reason == round_bank.REASON_ROUND_AMBIGUOUS:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        return convert(value)


#: The named ``reason`` each failing stage publishes. The bucket is the STAGE,
#: never the exception type — one ``RoundViewsError`` is raised both for a
#: round that could not be read and for a view that declined one, so a
#: type-based split answers the operator's "where do I go" wrong.

_REASON_BY_CODE = {
    EXIT_REFUSED: REASON_REFUSED,
    EXIT_UNREADABLE: REASON_UNREADABLE,
    EXIT_WRITE_FAILED: REASON_UNWRITABLE,
}


def _write(
    payload: Mapping[str, Any], out: str | Path | None, default_path: Path, *, schema: str,
    make_parents: bool = False,
) -> Path:
    """Publish one view under the schema its answer carries; only filesystem
    errors become write refusals."""

    return stage(
        EXIT_WRITE_FAILED, (OSError,), write_report, {**payload, "schema": schema}, out, default_path,
        make_parents=make_parents,
    )


def resolve_set_take(
    round_dir: Path, set_id: str | None, take: str | None, role: str | None,
) -> tuple[dict[str, Any], str, str]:
    """One take of a set: the subject naming it, its id, and ``role`` or else
    the response the set measured."""
    inputs = round_inputs(round_dir)
    selected = resolve_set(inputs, set_id)
    take_id = selected.take_id(take)
    return subject(inputs, selected, take_ids=[take_id]), take_id, role or selected.role


def calibration_id(calibration: Mapping[str, Any] | None) -> str | None:
    """The microphone calibration a capture was read through, when one was applied."""
    return calibration.get("calibration_id") if calibration and calibration.get("applied") else None


def omitted_note(omitted: Iterable[Mapping[str, str]]) -> str:
    """The human line's tail naming each take a view left out, when any was."""
    ids = [entry["capture_id"] for entry in omitted]
    return f" ({len(ids)} omitted: {', '.join(ids)})" if ids else ""


def refused_by_name(
    reason: str, detail: Mapping[str, Any] | str, *, code: int = EXIT_REFUSED
) -> int:
    """An instrument that refuses BY NAME publishes its own name and its
    evidence here — fields, or a sentence — never this tool's stage bucket."""
    if not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True, default=str)
    return failed(code, reason, detail, next_action=refusal_copy_for(reason)[1])


def resolved_out(round_dir: Path, artifact: str, set_id: str | None = None) -> Path:
    """Where a view lands beside a round it read WITHOUT the round resolver.

    A round the resolver cannot place still gets its artifact, beside itself.
    """
    try:
        return default_out(round_inputs(round_dir), round_dir, artifact, set_id)
    except RoundViewsError:
        return round_dir / set_artifact_name(artifact, set_id)


def add_set_argument(
    parser: argparse.ArgumentParser, *, name: str = "--set", required: bool = False,
    take: bool = False,
) -> None:
    parser.add_argument(name, required=required, help="set in the run manifest; optional for a one-set round")
    if take:
        parser.add_argument(name.removesuffix("set") + "take", help=f"selected take within {name}; defaults to the unique on-axis take")


def add_rungs_ms_argument(
    parser: argparse.ArgumentParser, *, flag: str = "--rungs-ms",
    dest: str | None = None, repeatable: bool = False,
) -> None:
    """The gate ladder flag, so the shipped ladder has one owner.

    ``repeatable=True`` defaults to ``None``, never the shipped rungs: an
    argparse ``append`` over a non-empty default ADDS to it, not replaces it.
    """
    shipped = " ".join(f"{r:g}" for r in DEFAULT_RUNGS_MS)
    if repeatable:
        parser.add_argument(
            flag, type=float, action="append", default=None, dest=dest,
            metavar="MS",
            help=f"one rung in ms, repeatable; replaces the ladder ({shipped})",
        )
        return
    parser.add_argument(
        flag, type=float, nargs="+", default=list(DEFAULT_RUNGS_MS), dest=dest,
        metavar="MS", help=f"gate ladder, in milliseconds (default: {shipped})",
    )

