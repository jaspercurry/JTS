# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read measured evidence and write the registered round views (ADR-0237)."""

from __future__ import annotations

import argparse
import sys
from functools import partial
from importlib import import_module
from typing import Sequence

from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable
from jasper.cli._report import output_path
from jasper.cli.round import refuse_unreadable_state
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    StageFailed,
    exit_codes_help,
    failed,
    help_from_rows,
    refuse_unreadable,
)

from ._common import (
    ARTIFACT_BY_VIEW,
    AUTHORITY_TIER,
    PROG,
    ROUND_ARGUMENTS,
    RoundSetRefused,
    REASON_REFUSED,
    REASON_UNREADABLE,
    REASON_UNWRITABLE,
    _REASON_BY_CODE,
    _ROUND_TOOL_ERRORS,
    add_rungs_ms_argument,
    default_out,
    refused_by_name,
    round_ref,
    view_rows,
)
from jasper.active_speaker.round_bookkeeping import run_bookkeeping as run_bookkeeping

__all__ = [
    "ARTIFACT_BY_VIEW",
    "AUTHORITY_TIER",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNREADABLE",
    "EXIT_WRITE_FAILED",
    "PROG",
    "REASON_REFUSED",
    "REASON_UNREADABLE",
    "REASON_UNWRITABLE",
    "add_rungs_ms_argument",
    "build_parser",
    "default_out",
    "main",
]

#: The view families, in the order their subcommands are offered.
_FAMILIES = tuple(import_module(f".{name}", __name__) for name in (
    "catalog", "repeat", "candidates", "directivity", "sweeps", "impulse", "compare",
    "frequency", "distortion", "dsp_replay", "classify_features",
    "delay", "room", "room_grade", "bass", "bass_alignment", "speaker_fit", "nearfield",
    "rear_fit",
))

#: The views that read none of the speaker's local state: the catalog lists tools, and the two renders read the files they are given.
NO_STATE_VERBS = ("catalog", "dsp-replay", "dsp-levels")


def build_parser(*, resolve_rounds: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Read measured round evidence. `catalog` lists every tuning tool with the\n"
            "question it answers, and with a round, what to run next on it. Each view's\n"
            "--help adds when not to use it, an example and its exit codes. Answers use\n"
            "stdout; detailed reports use files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLES\n"
            "  jasper-round-views catalog --program rear\n"
            "  jasper-round-views catalog <round-dir>\n"
            "  jasper-round-views directivity --help\n"
            "\n"
            "ANSWER\n"
            "  One JSON document: \"view\"; \"schema\", the answer version its\n"
            "  artifact also carries; \"subject\", the round, set, take and\n"
            "  candidate ids it read (\"rounds\" lists one per round for a view\n"
            "  that compares rounds); \"parameters\", the analysis settings it\n"
            "  used; \"out\" and \"bytes\" for its artifact; then its own fields.\n"
            "  A take is named by its id: --take, or --<side>-take where a view\n"
            "  reads two (jasper-round show lists them).\n"
            "\n"
            f"{exit_codes_help()}"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for family in _FAMILIES:
        family.add_parser(sub)

    for choice in sub._choices_actions:
        if rows := view_rows(choice.dest):
            every = any(not row.programs for row in rows.values())
            programs = "all" if every else "/".join(dict.fromkeys(p for row in rows.values() for p in row.programs))
            choice.help = f"[{programs}] {choice.help}"
            writes = any(row.artifact for row in rows.values())
            help_from_rows(sub.choices[choice.dest], rows,
                           codes=(EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, *((EXIT_WRITE_FAILED,) if writes else ())))

    for child in sub.choices.values():
        child.allow_abbrev = False
        for action in child._actions:
            if "--out" in action.option_strings:
                action.type = output_path
            elif resolve_rounds and action.dest in ROUND_ARGUMENTS:
                action.type = partial(round_ref, action.type if callable(action.type) else str)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Resolving a round reads the round store, which faults for a user the
    # speaker's state is closed to: the guard runs before that parse, and
    # catalog, which the guard exempts, refuses here given a banked round id.
    args = build_parser(resolve_rounds=False).parse_args(argv)
    if (refused := refuse_unreadable_state(args.command, NO_STATE_VERBS)) is not None:
        return refused
    try:
        args = build_parser().parse_args(argv)
    except PermissionError as exc:
        return refuse_unreadable(exc)
    try:
        return int(args.func(args))
    except RoundSetRefused as refusal:
        return failed(EXIT_REFUSED, refusal.reason, refusal.detail)
    except StageFailed as staged:
        return failed(staged.code, _REASON_BY_CODE[staged.code], str(staged), code=getattr(staged.__cause__, "code", None))
    except EvidenceUnavailable as refusal:
        # Its own reason, never this tool's stage bucket.
        return refused_by_name(refusal.reason, refusal.detail)
    except _ROUND_TOOL_ERRORS as exc:
        # What no stage claimed: the round READ, and the view then declined to
        # grade it. That is the refusal exit, not an unreadable one.
        return failed(EXIT_REFUSED, REASON_REFUSED, str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
