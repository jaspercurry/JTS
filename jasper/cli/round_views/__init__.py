# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for the round-grading comparison views (issue #2769).

One console script, one subcommand per view — each a thin argparse wrapper
over :mod:`jasper.active_speaker.crossover_v2.round_views`, which owns every
number this tool prints. A round directory is EITHER a banked round tree or a
live session bundle still on the speaker, whichever
:func:`~jasper.active_speaker.crossover_v2.round_inputs.round_inputs` finds,
so an operator can grade the round they just ran without banking it first
(#3498). The artifact lands beside a BANKED round, travelling with the
evidence it was computed from, and beside the CALLER for a live bundle, which
belongs to the daemon (:func:`default_out`).

Every subcommand prints its ANSWER as one JSON document on stdout and its one
human line on stderr (:func:`._common.answer`, ADR-0235); ``--out PATH``
files the artifact elsewhere, ``-`` putting the whole artifact on stdout
instead — which the two ``delay-`` verbs and ``repeat-floor``, whose record
its owning module publishes, do not take. On failure the exit code names the
STAGE that failed and it publishes the shared failure record; ``--help``'s
EXIT CODES block and docs/tuning-operator-runbook.md's "Exit codes" state the
numbers and the record's shape, so neither is repeated here.

Subcommands: one module per view family, each documenting its own verbs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    HarmonicEvidenceRefused,
)
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    StageFailed,
    failed,
)

from . import (
    classify_features,
    close_reference,
    cloud_binding,
    delay,
    distortion,
    forward_model,
    frequency,
    grades,
    inventory,
    repeat,
    seats,
    sweeps,
)
from ._common import (
    ARTIFACT_BY_VIEW,
    AUTHORITY_TIER,
    PROG,
    REASON_REFUSED,
    REASON_UNREADABLE,
    REASON_UNWRITABLE,
    _REASON_BY_CODE,
    _ROUND_TOOL_ERRORS,
    add_rungs_ms_argument,
    default_out,
    refused_by_name,
)
from .forward_model import ACCEPTANCE_RUNS

__all__ = [
    "ACCEPTANCE_RUNS",
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
_FAMILIES = (
    grades, repeat, seats, cloud_binding, forward_model, sweeps, frequency,
    distortion, classify_features, close_reference, delay, inventory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "The round-grading comparison views: entry-state grading, "
            "frozen-reference grading, per-seat curves, session-to-session "
            "repeatability and the banked repeat floor, per-seat agreement, "
            "audibility co-metrics, measured per-angle directivity, whether "
            "the cloud's null evidence bound the linearization fit, what a "
            "candidate would measure from the banked per-driver solos, the "
            "gate window ladder and the sweep read onto the spec verdict, the "
            "shared frequency view, the H2/H3 distortion reading, whether a "
            "feature is a driver defect or the room, how much of a far read "
            "was the room, the inter-driver delay landscape and its acoustic "
            "confirmation, and an inventory of which of those a round already "
            "carries — over banked rounds and live sessions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - frozen/repeat/repeat-floor need MULTIPLE round directories\n"
            "    (a baseline plus a target, or two-or-more rounds);\n"
            "    entry/per-seat/agreement grade a single round\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-round-views frequency captures/.../session-1/round-3\n"
            "  jasper-round-views frozen captures/.../baseline captures/.../round-3\n"
            "  jasper-round-views spec-sweep captures/.../session-1/round-3\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- graded; printed, or written to --out. entry can\n"
            "     print \"entry-state: NOT GRADED — <reason>\" on stderr and\n"
            "     still exit 0 -- \"not gradeable yet\" is a valid verdict,\n"
            "     not a failure, so check the printed line rather than only\n"
            "     the code if that distinction matters to your caller\n"
            "  1  EXIT_REFUSED -- the round read, and the view itself\n"
            "     declined to grade it (a round with no cloud group, a\n"
            "     repeat floor from a single round)\n"
            "  2  EXIT_UNREADABLE -- the round or source could not be\n"
            "     read into a comparable view\n"
            "  3  EXIT_WRITE_FAILED -- graded, but the destination could\n"
            "     not be written\n"
            "  1-3 print \"<status> (<reason>): <detail>\" on stderr and the\n"
            "     same record as JSON on stdout"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for family in _FAMILIES:
        family.add_parser(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except StageFailed as staged:
        return failed(staged.code, _REASON_BY_CODE[staged.code], str(staged))
    except HarmonicEvidenceRefused as refusal:
        # An instrument that refuses BY NAME publishes its own name here rather
        # than this tool's stage bucket, and its evidence as the detail.
        return refused_by_name(refusal.reason, refusal.evidence)
    except _ROUND_TOOL_ERRORS as exc:
        # What no stage claimed: the round READ, and the view then declined to
        # grade it. That is the refusal exit, not an unreadable one.
        return failed(EXIT_REFUSED, REASON_REFUSED, str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
