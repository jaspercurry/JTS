# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grading verbs: the state a round entered on, a round frozen to a
baseline's per-position references, and comparison to a frozen baseline.

* ``entry <round-dir>`` — grade the state the round ENTERED on, from the
  entry-baseline take it banked, through the shipped flat-spec evaluator.
  The one table nothing else prints: a fresh box's declarations-derived
  config is the first round's entry state, and until this door it could only
  be graded by hand. Writes ``entry_state_grade.json``. A round that banked
  no gradeable take says so with a named reason and still exits ``0`` — that
  is an answer, not an unreadable round.
* ``frozen <baseline-dir> <target-dir>`` — grade ``target`` shipped AND
  frozen to ``baseline``'s per-position reference levels. Writes
  ``frozen_reference.json`` for the TARGET round.
"""

from __future__ import annotations

import argparse

from jasper.active_speaker.crossover_v2.round_views import (
    entry_state_grade,
    frozen_reference_grade,
)
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _load_round,
    _view_out,
    _write,
    answer,
)

def _cmd_entry(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    # A packet missing `entry_baseline` is a corrupt packet, which this grade's
    # own docstring puts in the unreadable arm — not a view declining a round.
    grade = stage(EXIT_UNREADABLE, (KeyError, TypeError), entry_state_grade, banked)
    written = _write(grade.to_dict(), args.out, _view_out(args, banked))
    report = grade.report
    # ``report is None`` IS ``not available`` — the two move together on
    # ``EntryStateGrade`` — and testing the report narrows it for the summary
    # below without a second, unfalsifiable assertion that they agree.
    if report is None:
        # Exit 0, not 1: "this round banked no gradeable entry baseline" is an
        # ANSWER — the one this door exists to give instead of an operator's
        # hand-rolled evaluation — not a failure to read the round, which is
        # what the unreadable exit is for.
        return answer(
            args.command, out=written, graded=False, reason=grade.reason,
            line=f"entry-state: NOT GRADED — {grade.reason}",
        )
    # `is False` / `is None`, never a bare truthiness test, for exactly the
    # reason `seats._cmd_agreement` states: an UNEVALUABLE band (no
    # non-excluded bin survived) is not a failing one, and collapsing them
    # would report a band nobody could measure as one that measured badly.
    n_failed = sum(1 for band in report.bands if band.within_target is False)
    n_unevaluable = sum(1 for band in report.bands if band.within_target is None)
    ordinal = "?" if grade.round_ordinal is None else grade.round_ordinal
    epoch = "?" if grade.round_ordinal_epoch is None else grade.round_ordinal_epoch
    return answer(
        args.command, out=written, graded=True, bands=len(report.bands),
        outside_target=n_failed, unevaluable=n_unevaluable,
        overall_within_target=report.overall_within_target, round_ordinal=grade.round_ordinal,
        round_ordinal_epoch=grade.round_ordinal_epoch,
        graph_fingerprint=grade.graph_fingerprint or None,
        line=(
            f"entry-state: {len(report.bands)} band(s), {n_failed} outside target, "
            f"{n_unevaluable} unevaluable; "
            f"overall_within_target={report.overall_within_target} "
            f"round={ordinal} epoch={epoch} "
            f"graph={grade.graph_fingerprint or '(not recorded)'}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _cmd_frozen(args: argparse.Namespace) -> int:
    baseline = _load_round(args.baseline_dir)
    target = _load_round(args.target_dir)
    result = frozen_reference_grade(baseline, target)
    written = _write(result.to_dict(), args.out, _view_out(args, target))
    return answer(
        args.command, out=written, shipped=result.shipped, frozen=result.frozen,
        line=(
            f"frozen-reference: shipped={result.shipped} frozen={result.frozen}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    entry = sub.add_parser("entry", help="grade the state this round entered on, before it applied anything")
    entry.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    entry.add_argument("--out", default=None, help="write the result here (- for stdout)")
    entry.set_defaults(func=_cmd_entry)

    frozen = sub.add_parser("frozen", help="grade a round shipped and frozen to a baseline's reference")
    frozen.add_argument(
        "baseline_dir", metavar="<baseline-round-dir>",
        help=f"{_ROUND_DIR_HELP} to freeze the reference from",
    )
    frozen.add_argument(
        "target_dir", metavar="<target-round-dir>",
        help=f"{_ROUND_DIR_HELP} to grade",
    )
    frozen.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frozen.set_defaults(func=_cmd_frozen)
