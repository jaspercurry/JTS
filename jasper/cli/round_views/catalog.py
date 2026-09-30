# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tool catalog: every tool an agent can call, listed from the rows that also make the runbook's menu
(ADR-0393), and with a round, what to run next on it (#5928 TB5)."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, with_records
from jasper.active_speaker.measurement_programs import PURPOSES, available_presets, preset
from jasper.active_speaker.round_catalog import round_calls
from jasper.active_speaker.round_view_artifacts import CatalogRow
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, exit_codes_help, stage

from ._common import (
    ANSWER_SCHEMAS, CATALOG, _ROUND_DIR_HELP, _ROUND_DIR_METAVAR, _ROUND_TOOL_ERRORS, answer, default_out,
    read_run_manifest, resolve_set, round_inputs, subject,
)


def _purposes(program: str) -> set[str]:
    """The purposes whose views read a round of any preset of this program, by the
    rule the round's bookkeeping follows: a rear seat round is read by the room
    views too."""
    presets = (preset(name) for name in available_presets())
    return {program}.union(*(row.purposes for row in presets if row.purpose == program))


def _tool(command: str, row: CatalogRow) -> dict[str, Any]:
    return {"tool": command, "question": row.question, "needs": row.needs, "reads": row.reads,
            "programs": list(row.programs or PURPOSES), "argv": [*shlex.split(command), *row.argv],
            "schema": row.schema or None, "artifact": row.artifact or None, "answer_fields": list(row.answer_fields)}


def _located(inputs: RoundInputs, round_dir: Path, call: dict[str, Any]) -> dict[str, Any]:
    """One call, with where its artifact lies beside the round and whether it is there."""
    path = None
    if call["artifact"]:
        path = default_out(inputs, round_dir, call["artifact"])
        # A view run with --set on a one-set round files under the set's name.
        named = default_out(inputs, round_dir, CATALOG[call["tool"]].artifact, call["set_id"])
        path = path if path.is_file() or not named.is_file() else named
    size = path.stat().st_size if path is not None and path.is_file() else None
    return {**{key: call[key] for key in ("argv", "set_id", "take_id", "needs")},
            "out": None if path is None else str(path), "present": None if path is None else size is not None,
            "bytes": size}


def _round_tools(round_dir: Path, wanted: set[str], set_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The tools that read this round, each with its calls."""
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    manifest = with_records(inputs.session_dir, read_run_manifest(inputs), disclose=True)
    if set_id is not None:
        resolve_set(inputs, set_id, manifest=manifest)
    tools: dict[str, dict[str, Any]] = {}
    for call in round_calls(round_dir, manifest, purposes=wanted, set_id=set_id):
        tools.setdefault(call["tool"], {**_tool(call["tool"], CATALOG[call["tool"]]), "calls": []})["calls"].append(
            _located(inputs, round_dir, call))
    return subject(inputs, set_id=set_id), list(tools.values())


def _cmd_catalog(args: argparse.Namespace) -> int:
    wanted = _purposes(args.program) if args.program else set(PURPOSES)
    if args.round_dir is None:
        read: dict[str, Any] = {}
        tools = [_tool(command, row) for command, row in CATALOG.items() if wanted.intersection(row.programs or PURPOSES)]
        line = f"catalog: {len(tools)} tool(s) for {args.program or 'every program'}"
    else:
        read, tools = _round_tools(Path(args.round_dir), wanted, args.set)
        calls = [call for tool in tools for call in tool["calls"]]
        line = (f"catalog: {len(calls)} call(s) of {len(tools)} tool(s) on {args.round_dir}; "
                f"{sum(call['present'] is False for call in calls)} artifact(s) missing")
    return answer(args.command, schema=ANSWER_SCHEMAS[args.command], subject=read, parameters={"program": args.program},
                  tools=tools, line=line)


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "catalog", help="every tool an agent can call, and with a round what to run next on it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List every tuning tool an agent can call, one row each: the question it\n"
            "answers, what it needs (pose, regime, take kind), what it reads (record,\n"
            "recording or laptop), the programs whose rounds it reads, the argv that\n"
            "calls it, and its answer's schema, artifact and fields. It writes nothing.\n"
            "With a round, it lists what to run next on it: each tool that reads the\n"
            "round, with its calls filled from the round's sets and takes, the inputs\n"
            "a call still needs, and whether the artifact each call writes is there."
        ),
        epilog=(
            "EXAMPLES\n"
            "  jasper-round-views catalog --program rear\n"
            "  jasper-round-views catalog <round-dir>\n"
            "\n"
            "In argv, <this-round> is a banked round id or directory; jasper-round show\n"
            "lists the <set-id> and <take-id> values.\n"
            "\n"
            f"{exit_codes_help((EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE))}\n"
            "  an unknown --program is a usage error, which also exits 2"
        ),
    )
    parser.add_argument("round_dir", nargs="?", metavar=_ROUND_DIR_METAVAR,
                        help=f"list what to run next on this round: {_ROUND_DIR_HELP}")
    parser.add_argument("--set", help="with a round, only the calls on this set of its run manifest")
    parser.add_argument("--program", choices=PURPOSES, help="only the tools this program's rounds can use")
    parser.set_defaults(func=_cmd_catalog)
