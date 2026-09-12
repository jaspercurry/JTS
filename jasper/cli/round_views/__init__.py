# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read banked round views and print one JSON answer per invocation (ADR-0237)."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    HarmonicEvidenceRefused,
)
from jasper.cli._report import output_path
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    StageFailed,
    failed,
)

from . import (
    bass,
    candidates,
    classify_features,
    close_reference,
    cloud_binding,
    delay,
    distortion,
    dsp_replay,
    findings,
    forward_model,
    frequency,
    grades,
    inventory,
    repeat,
    room,
    room_grade,
    seats, speaker_fit, sweeps,
)
from ._common import (
    ARTIFACT_BY_VIEW,
    AUTHORITY_TIER,
    PROG,
    RoundSetRefused,
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
    grades, repeat, candidates, seats, cloud_binding, forward_model, sweeps,
    frequency, distortion, dsp_replay, classify_features, findings, close_reference,
    delay, room, room_grade, bass, inventory, speaker_fit,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Read measured round evidence, including repeat --set spread across takes. "
            "Answers use stdout; detailed reports use files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - frozen/repeat-floor need MULTIPLE round directories\n"
            "    (a baseline plus a target, or two-or-more rounds);\n"
            "    entry/per-seat/agreement grade a single round\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-round-views frequency captures/.../session-1/round-3\n"
            "  jasper-round-views frozen captures/.../baseline captures/.../round-3\n"
            "  jasper-round-views sweep captures/.../session-1/round-3 --scope verdict\n"
            "\n"
            "OPTIONAL MODEL-ERROR FLOOR (Python)\n"
            "  jasper.active_speaker.model_error_store.adopt_floor(floor, path=...)\n"
            "  accepts FloorStats for prediction-tracking error. This is distinct\n"
            "  from the pooled-response repeat-floor metric. Use only when useful;\n"
            "  it neither installs that repeat floor nor requires another campaign.\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- result available or a coverage gap reported;\n"
            "     inspect outcome/reason and coverage in the JSON answer.\n"
            "  1  EXIT_REFUSED -- the round read, and the view itself\n"
            "     declined to grade it (a round with no cloud group, a\n"
            "     repeat floor from a single round)\n"
            "  2  EXIT_UNREADABLE -- the round or source could not be\n"
            "     read into a comparable view\n"
            "  3  EXIT_WRITE_FAILED -- graded, but the destination could\n"
            "     not be written\n"
            "  1-3 print \"<status> (<reason>): <detail>\" on stderr and the\n"
            "     same record as JSON on stdout. With --include, each result\n"
            "     keeps its own outcome/detail path; failures leave good siblings\n"
            "     visible and the command returns the highest failed stage code."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for family in _FAMILIES:
        family.add_parser(sub)

    for child in sub.choices.values():
        child.allow_abbrev = False
        for action in child._actions:
            if "--out" in action.option_strings:
                action.type = output_path
    sub.choices["inventory"].set_defaults(set_flags_by_view={
        name: child.get_default("optional_set_flags") or () for name, child in sub.choices.items()
    })
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RoundSetRefused as refusal:
        return failed(EXIT_REFUSED, refusal.reason, refusal.detail)
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
