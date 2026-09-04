# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grading verbs: the state a round entered on, a round frozen to a
baseline's per-position references, and the round's own per-seat curves.

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
* ``per-seat <round-dir>`` — every banked position plus the VERIFY pose
  (when its dump-ring capture is banked), normalised onto one comparable
  basis. Writes ``per_seat.json``.
"""

from __future__ import annotations

import argparse
import sys

from jasper.active_speaker.crossover_v2.round_views import (
    entry_state_grade,
    frozen_reference_grade,
    per_seat_curves,
    verify_pose_curve,
)
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE, stage

from ._common import (
    _ROUND_DIR_HELP,
    _add_norm_band_args,
    _load_round,
    _view_out,
    _write,
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
        print(f"entry-state: NOT GRADED — {grade.reason}", file=sys.stderr)
        return EXIT_OK
    # `is False` / `is None`, never a bare truthiness test, for exactly the
    # reason `seats._cmd_agreement` states: an UNEVALUABLE band (no
    # non-excluded bin survived) is not a failing one, and collapsing them
    # would report a band nobody could measure as one that measured badly.
    n_failed = sum(1 for band in report.bands if band.passed is False)
    n_unevaluable = sum(1 for band in report.bands if band.passed is None)
    ordinal = "?" if grade.round_ordinal is None else grade.round_ordinal
    epoch = "?" if grade.round_ordinal_epoch is None else grade.round_ordinal_epoch
    print(
        f"entry-state: {len(report.bands)} band(s), {n_failed} outside target, "
        f"{n_unevaluable} unevaluable; "
        f"overall_passed={report.overall_passed} "
        f"round={ordinal} epoch={epoch} "
        f"graph={grade.graph_fingerprint or '(not recorded)'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_frozen(args: argparse.Namespace) -> int:
    baseline = _load_round(args.baseline_dir)
    target = _load_round(args.target_dir)
    result = frozen_reference_grade(baseline, target)
    written = _write(result.to_dict(), args.out, _view_out(args, target))
    print(
        f"frozen-reference: shipped={result.shipped} frozen={result.frozen}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_per_seat(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "curve_grid_hz": banked.curve_grid_hz.tolist(),
        "norm_band_hz": [args.norm_lo, args.norm_hi],
        "verify_pose": {
            "included": verify.curve is not None,
            "reason": verify.reason,
        },
        "seats": [
            {
                "position_id": seat.position_id,
                "role": seat.role,
                "normalized_db": seat.normalized_db.tolist(),
            }
            for seat in seats
        ],
    }
    written = _write(payload, args.out, _view_out(args, banked))
    print(
        f"per-seat: {len(seats)} seat(s) ({', '.join(s.position_id for s in seats)}); "
        f"verify pose {'included' if verify.curve is not None else f'ABSENT ({verify.reason})'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def add_parser(sub: argparse._SubParsersAction) -> None:
    entry = sub.add_parser("entry", help="grade the state this round entered on, before it applied anything")
    entry.add_argument("round_dir", help=_ROUND_DIR_HELP)
    entry.add_argument("--out", default=None, help="write the result here (- for stdout)")
    entry.set_defaults(func=_cmd_entry)

    frozen = sub.add_parser("frozen", help="grade a round shipped and frozen to a baseline's reference")
    frozen.add_argument("baseline_dir", help=f"{_ROUND_DIR_HELP} to freeze the reference from")
    frozen.add_argument("target_dir", help=f"{_ROUND_DIR_HELP} to grade")
    frozen.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frozen.set_defaults(func=_cmd_frozen)

    per_seat = sub.add_parser("per-seat", help="every banked position plus the VERIFY pose, normalised")
    per_seat.add_argument("round_dir", help=_ROUND_DIR_HELP)
    _add_norm_band_args(per_seat)
    per_seat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    per_seat.set_defaults(func=_cmd_per_seat)
