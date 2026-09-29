# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tool catalog: every tool an agent can call, listed from the rows that also make the runbook's menu (ADR-0393)."""

from __future__ import annotations

import argparse
import shlex

from jasper.active_speaker.measurement_programs import PURPOSES, available_presets, preset
from jasper.active_speaker.round_view_artifacts import read_purposes

from ._common import ANSWER_SCHEMAS, CATALOG, answer


def _purposes(program: str) -> set[str]:
    """The purposes whose views read a round of any preset of this program, by the
    rule the round's bookkeeping follows: a rear seat round, or a speaker round
    that keeps a room sweep, is read by the room views too."""
    presets = (preset(name) for name in available_presets())
    return {program}.union(*(read_purposes(row.purposes, has_room=row.room_sweep)
                             for row in presets if row.purpose == program))


def _cmd_catalog(args: argparse.Namespace) -> int:
    wanted = _purposes(args.program) if args.program else set(PURPOSES)
    tools = [{
        "tool": command, "question": row.question, "needs": row.needs, "reads": row.reads,
        "programs": list(row.programs or PURPOSES), "argv": [*shlex.split(command), *row.argv],
        "schema": row.schema or None, "artifact": row.artifact or None, "answer_fields": list(row.answer_fields),
    } for command, row in CATALOG.items() if wanted.intersection(row.programs or PURPOSES)]
    return answer(args.command, schema=ANSWER_SCHEMAS[args.command], subject={}, parameters={"program": args.program},
                  tools=tools, line=f"catalog: {len(tools)} tool(s) for {args.program or 'every program'}")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "catalog", help="every tool an agent can call: what each answers, needs and reads, and how to call it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "List every tuning tool an agent can call, one row each: the question it\n"
            "answers, what it needs (pose, regime, take kind), what it reads (record,\n"
            "recording or laptop), the programs whose rounds it reads, the argv that\n"
            "calls it, and its answer's schema, artifact and fields. It reads no round\n"
            "and writes nothing. Not for what one round holds: jasper-round show lists\n"
            "its sets and takes."
        ),
        epilog=(
            "EXAMPLE\n"
            "  jasper-round-views catalog --program rear\n"
            "\n"
            "In argv, <this-round> is a banked round id or directory; jasper-round show\n"
            "lists the <set-id> and <take-id> values.\n"
            "\n"
            "EXIT CODES\n"
            "  0  the answer; an unknown --program is a usage error (2)"
        ),
    )
    parser.add_argument("--program", choices=PURPOSES, help="only the tools this program's rounds can use")
    parser.set_defaults(func=_cmd_catalog)
