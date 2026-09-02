# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for the round-grading comparison views (issue #2769).

One console script, eight subcommands — each a thin argparse wrapper over
:mod:`jasper.active_speaker.crossover_v2.round_views`, which owns every
number this tool prints. A round directory is EITHER a banked round tree or
a live session bundle still on the speaker
(``/var/lib/jasper/active_speaker/sessions/<id>``) — the shape is
:func:`~jasper.active_speaker.crossover_v2.round_inputs.round_inputs`'
answer, so an operator can grade the round they just ran without banking it
first (#3498). This module calls the product view and writes the result as
JSON — into a BANKED round's own directory by default, so the artifact travels
with the evidence it was computed from, and beside the CALLER for a live
session bundle, which belongs to the daemon (:func:`_default_out`). Per the
same "who prints the sentence" boundary :mod:`jasper.cli.crossover_prescriber`
already keeps for its own JSON output.

Subcommands:

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
* ``repeat <round-dir> [<round-dir> ...]`` — session-to-session spread of
  the pooled honest figures (the stop criterion). Writes
  ``repeatability.json`` for the FIRST round.
* ``repeat-floor <round-dir> <round-dir> [...] --out PATH`` — the same
  spread, banked as the durable record the evidence packet's
  ``in_capture_repeat_floor`` reads and derives the stopping plateau/benefit
  margin from. The rounds must be touched-nothing fixed-pose repeats. This
  tool only WRITES the record where ``--out`` says (required: it runs on a
  laptop over banked directories, and the speaker's own path is not a
  default it can assume); the operator then places that file at the
  on-speaker path, from which ``bank-crossover-round.sh`` pulls it beside
  every later round as ``repeat-floor.json``.
* ``agreement <round-dir>`` — per-position sign/magnitude testimony for
  every feature in the trusted sweep, built from the same per-seat curves
  ``per-seat`` computes. Writes ``agreement.json``.
* ``frequency <source-a> [<source-b>]`` — the renderer-neutral frequency view
  shared with the JTS web page. A source may be a banked round, a session
  bundle, or a JSON measurement/analysis document.
* ``co-metrics <round-dir>`` — NBD + SM (Olive 2004, ADR-0202) on the
  on-axis curve and the pooled horizontal window. Co-metrics only: they
  inform, they never gate or veto — ``entry``/``frozen``/``per-seat`` etc.
  above stay the acceptance path. Writes ``audibility_co_metrics.json``.

Every subcommand accepts ``--out PATH`` to write somewhere else instead
(``-`` for stdout, except ``repeat-floor``, whose record is published
atomically by its owning module and so requires a real path), and prints a
one-line human summary to stderr either way. Exit ``0`` on success, ``1``
when a round directory could not be read into a comparable view
(:class:`~jasper.active_speaker.crossover_v2.round_views.RoundViewsError`) or
the view could not be written where it was asked for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.crossover_v2.round_views import (
    AGREEMENT_TESTIFY_MIN,
    BankedRound,
    RoundViewsError,
    agreement_table,
    audibility_co_metrics,
    default_agreement_lo_hz,
    entry_state_grade,
    frozen_reference_grade,
    load_banked_round,
    per_seat_curves,
    repeat_floor_provenance,
    repeatability_spread,
    verify_pose_curve,
)
from jasper.active_speaker.frequency_view import build_frequency_view
from jasper.active_speaker.measurement_archive import (
    ArchivedMeasurement,
    load_measurement,
)
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor,
    stopping_thresholds,
    write_repeat_floor,
)
from jasper.active_speaker.crossover_v2.frequency_view import frequency_run

EXIT_OK = 0
EXIT_ERROR = 1

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory"

#: What every round-directory positional takes, said once. Both shapes, named
#: in the order an operator meets them: the live one is what a round leaves on
#: the speaker, the banked one is what ``bank-crossover-round.sh`` made of it.
_ROUND_DIR_HELP = "a banked round directory, or a live session bundle"

#: A round directory is operator-pulled evidence, not a validated
#: input — the documented failure shapes it can hand back are broader than
#: the product module's own typed :class:`RoundViewsError`. A malformed
#: evidence document can be missing a key (``KeyError``), hold the wrong type
#: at one (``TypeError``), or not parse at all (``ValueError``, which
#: ``json.JSONDecodeError`` subclasses); and any of the files this tool reads
#: — or the one it WRITES, where an operator can name an ``--out`` they may
#: not create — can simply not exist or not be permitted (``OSError``, which
#: ``PermissionError`` subclasses). Every one of these is "this run produced
#: no view", exactly what exit 1 already means, and :func:`main` maps the
#: whole tuple there in one place so no subcommand can grow a traceback of
#: its own.
#:
#: ``struct.error`` was here for one reader that no longer exists: a
#: header-truncated dump-ring WAV raised it out of ``scipy.io.wavfile.read``
#: while ``verify_pose_curve`` still deconvolved raw ring bytes. That view
#: reads the round's banked curve now, no code on this path opens a WAV, and
#: catching an exception nothing can raise is not how it is caught.
_ROUND_TOOL_ERRORS: tuple[type[Exception], ...] = (
    RoundViewsError, OSError, EOFError, ValueError, KeyError, TypeError,
)


def _write_json(payload: Any, out: str | None, default_path: Path) -> Path | None:
    """Write ``payload`` to ``out`` (``-`` = stdout) or ``default_path``.

    Returns the path written, or ``None`` when written to stdout — the
    caller's summary line reads differently in each case.
    """
    text = json.dumps(payload, indent=2, sort_keys=True)
    if out == "-":
        print(text)
        return None
    target = Path(out) if out else default_path
    target.write_text(text + "\n")
    return target


def _default_out(round_: BankedRound, name: str) -> Path:
    """Where a view lands when the operator named no ``--out``.

    A BANKED round tree is the operator's own directory, so its views stay
    beside the evidence they were computed from. A LIVE session bundle is the
    daemon's (``/var/lib/jasper/active_speaker/sessions/<id>``, written by the
    web host as its own user): defaulting inside it made the ordinary
    invocation — grade the round I just ran — raise ``PermissionError`` for
    the operator this door was added for (#3498). So a live round's view lands
    beside the caller instead, named by the session it came from so two
    sessions graded in one directory do not overwrite each other.
    """
    if round_.inputs.banked:
        return round_.round_dir / name
    return Path.cwd() / f"{round_.session_dir.name}-{name}"


def _frequency_default_out(source: Path) -> Path:
    """:func:`_cmd_frequency`'s own default: it takes sources the round
    resolver does not (a JSON document, a bundle that banked no round), so it
    reads the live shape directly — under the same rule as
    :func:`_default_out`, never inside a daemon-owned session bundle.
    """
    if not source.is_dir():
        return source.parent / "frequency_view.json"
    if (source / "info.json").is_file():
        return Path.cwd() / f"{source.name}-frequency_view.json"
    return source / "frequency_view.json"


def _cmd_entry(args: argparse.Namespace) -> int:
    banked = load_banked_round(Path(args.round_dir))
    grade = entry_state_grade(banked)
    written = _write_json(
        grade.to_dict(), args.out, _default_out(banked, "entry_state_grade.json")
    )
    report = grade.report
    # ``report is None`` IS ``not available`` — the two move together on
    # ``EntryStateGrade`` — and testing the report narrows it for the summary
    # below without a second, unfalsifiable assertion that they agree.
    if report is None:
        # Exit 0, not 1: "this round banked no gradeable entry baseline" is an
        # ANSWER — the one this door exists to give instead of an operator's
        # hand-rolled evaluation — not a failure to read the round. Exit 1 is
        # reserved for a round directory that could not be read at all.
        print(f"entry-state: NOT GRADED — {grade.reason}", file=sys.stderr)
        return EXIT_OK
    # `is False` / `is None`, never a bare truthiness test, for exactly the
    # reason `_cmd_agreement` states below: an UNEVALUABLE band (no
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
    baseline = load_banked_round(Path(args.baseline_dir))
    target = load_banked_round(Path(args.target_dir))
    result = frozen_reference_grade(baseline, target)
    written = _write_json(
        result.to_dict(), args.out, _default_out(target, "frozen_reference.json")
    )
    print(
        f"frozen-reference: shipped={result.shipped} frozen={result.frozen}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_per_seat(args: argparse.Namespace) -> int:
    banked = load_banked_round(Path(args.round_dir))
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
    written = _write_json(payload, args.out, _default_out(banked, "per_seat.json"))
    print(
        f"per-seat: {len(seats)} seat(s) ({', '.join(s.position_id for s in seats)}); "
        f"verify pose {'included' if verify.curve is not None else f'ABSENT ({verify.reason})'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _load_rounds(round_dirs: Sequence[str]) -> list[tuple[str, BankedRound]]:
    """The (label, round) pairs both repeat verbs grade, labelled by the
    directory the operator named."""
    return [(round_dir, load_banked_round(Path(round_dir))) for round_dir in round_dirs]


def _cmd_repeat(args: argparse.Namespace) -> int:
    rounds = _load_rounds(args.round_dirs)
    result = repeatability_spread(rounds)
    written = _write_json(
        result.to_dict(), args.out, _default_out(rounds[0][1], "repeatability.json")
    )
    shipped = next((m for m in result.metrics if m.name == SHIPPED_POOL_METRIC), None)
    spread = shipped.spread() if shipped else None
    print(
        f"repeatability: {len(result.round_labels)} round(s); "
        f"{SHIPPED_POOL_METRIC} spread={spread}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _record_path(value: str) -> Path:
    """``--out`` for a verb that publishes a file: ``-`` is not a path."""
    if value == "-":
        raise argparse.ArgumentTypeError("repeat-floor publishes a file; '-' is not a path")
    return Path(value)


def _cmd_repeat_floor(args: argparse.Namespace) -> int:
    path = args.out
    rounds = _load_rounds(args.round_dirs)
    payload = derive_repeat_floor(
        repeatability_spread(rounds),
        rounds=[repeat_floor_provenance(round_dir, banked) for round_dir, banked in rounds],
    )
    record = write_repeat_floor(payload, state_path=path)
    thresholds = stopping_thresholds(record)
    aggregate = record["metrics"][SHIPPED_POOL_METRIC]
    print(
        f"repeat-floor: {record['n_repeats']} round(s); {SHIPPED_POOL_METRIC} "
        f"sd={aggregate['sd_db']:.4g} dB; thresholds={thresholds} -> {path}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_agreement(args: argparse.Namespace) -> int:
    banked = load_banked_round(Path(args.round_dir))
    lo_hz = args.lo if args.lo is not None else default_agreement_lo_hz(banked)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    features = agreement_table(
        seats,
        banked.curve_grid_hz,
        lo_hz=lo_hz,
        hi_hz=args.hi,
        feature_db=args.feature_db,
        testify_db=args.testify_db,
    )
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "seats": [seat.position_id for seat in seats],
        "swept_band_hz": [lo_hz, args.hi],
        "feature_db": args.feature_db,
        "testify_db": args.testify_db,
        "features": [feature.to_dict() for feature in features],
    }
    written = _write_json(payload, args.out, _default_out(banked, "agreement.json"))
    # `common_mode is True`, never a bare truthiness test: `None` (not
    # evaluable, below AGREEMENT_TESTIFY_MIN seats) must not be silently
    # counted alongside `False` (evaluated and failed the bar).
    n_common = sum(1 for f in features if f.common_mode is True)
    n_not_evaluable = sum(1 for f in features if f.common_mode is None)
    print(
        f"agreement: {len(features)} feature(s), {n_common} common-mode, "
        f"{n_not_evaluable} not-evaluable (< {AGREEMENT_TESTIFY_MIN} seats)"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_co_metrics(args: argparse.Namespace) -> int:
    banked = load_banked_round(Path(args.round_dir))
    result = audibility_co_metrics(banked)
    written = _write_json(
        result.to_dict(), args.out, _default_out(banked, "audibility_co_metrics.json")
    )
    on_axis = (
        f"NBD={result.on_axis.nbd_db:.3f} dB SM={result.on_axis.sm_r2:.3f}"
        if result.on_axis is not None else f"NOT AVAILABLE ({result.on_axis_reason})"
    )
    pooled = (
        f"NBD={result.pooled_window.nbd_db:.3f} dB SM={result.pooled_window.sm_r2:.3f} "
        f"({len(result.pooled_window_bearings_deg)} bearing(s))"
        if result.pooled_window is not None
        else f"NOT AVAILABLE ({result.pooled_window_reason})"
    )
    print(
        f"co-metrics [informational only, never a grade input]: "
        f"on-axis {on_axis}; pooled-window {pooled}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _frequency_source(path: Path):
    """One round, bundle, or JSON document as a neutral frequency run."""

    if path.is_file():
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError(f"{path}: expected one JSON object")
        run = frequency_run_from_documents(
            run_id=path.stem, documents=(document,),
        )
    elif (path / "info.json").is_file():
        info = json.loads((path / "info.json").read_text())
        if not isinstance(info, dict):
            raise ValueError(f"{path / 'info.json'}: expected one JSON object")
        run = load_measurement(ArchivedMeasurement(
            id=str(info.get("session_id") or path.name),
            bundle_dir=path,
            started_at=info.get("started_at"),
            state=str(info.get("state") or "") or None,
        ))
    else:
        run = frequency_run(load_banked_round(path).packet)
    if not run.series:
        raise ValueError(f"{path}: no usable frequency-response curves")
    return run


def _cmd_frequency(args: argparse.Namespace) -> int:
    source_a = Path(args.source_a)
    run_a = _frequency_source(source_a)
    run_b = _frequency_source(Path(args.source_b)) if args.source_b else None
    payload = build_frequency_view(run_a, run_b)
    written = _write_json(payload, args.out, _frequency_default_out(source_a))
    print(
        f"frequency: {len(payload['runs'])} run(s)"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _add_norm_band_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--norm-lo", type=float, default=400.0, help="normalisation band low edge, Hz (default 400)")
    parser.add_argument("--norm-hi", type=float, default=8000.0, help="normalisation band high edge, Hz (default 8000)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-round-views",
        description=(
            "The round-grading comparison views: entry-state grading, "
            "frozen-reference grading, per-seat curves, session-to-session "
            "repeatability and the banked repeat floor, per-seat agreement, "
            "audibility co-metrics, and the shared frequency view — over "
            "banked rounds and live sessions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - frozen/repeat/repeat-floor need MULTIPLE round directories\n"
            "    (a baseline plus a target, or two-or-more rounds);\n"
            "    entry/per-seat/agreement grade a single round\n"
            "  - to classify a feature's likely CAUSE -- that is\n"
            "    jasper-classify-features; this tool grades curves, not\n"
            "    defects\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-round-views frequency captures/.../session-1/round-3\n"
            "  jasper-round-views frozen captures/.../baseline captures/.../round-3\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- graded; printed, or written to --out. entry can\n"
            "     print \"entry-state: NOT GRADED — <reason>\" on stderr and\n"
            "     still exit 0 -- \"not gradeable yet\" is a valid verdict,\n"
            "     not a failure, so check the printed line rather than only\n"
            "     the code if that distinction matters to your caller\n"
            "  1  EXIT_ERROR -- the round or session source could not be\n"
            "     read or built into a view, or the view could not be\n"
            "     written; \"error: <detail>\" on stderr"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

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

    repeat = sub.add_parser("repeat", help="session-to-session spread of the pooled honest figures")
    repeat.add_argument("round_dirs", nargs="+", help=f"two or more of: {_ROUND_DIR_HELP}")
    repeat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    repeat.set_defaults(func=_cmd_repeat)

    repeat_floor = sub.add_parser(
        "repeat-floor", help="bank the repeat spread as the floor the evidence packet reads",
    )
    repeat_floor.add_argument(
        "round_dirs", nargs="+",
        help="two or more TOUCHED-NOTHING fixed-pose repeat round directories",
    )
    repeat_floor.add_argument(
        "--out", required=True, type=_record_path,
        help=(
            "where to write the record; place it on the speaker at "
            f"{_REPEAT_FLOOR_DEFAULT_PATH} so bank-crossover-round.sh pulls it "
            "beside every later round, or beside a banked round as "
            "repeat-floor.json"
        ),
    )
    repeat_floor.set_defaults(func=_cmd_repeat_floor)

    agreement = sub.add_parser("agreement", help="per-seat sign/magnitude testimony for every feature")
    agreement.add_argument("round_dir", help=_ROUND_DIR_HELP)
    _add_norm_band_args(agreement)
    agreement.add_argument(
        "--lo", type=float, default=None,
        help="trusted sweep low edge, Hz (default: this round's own trusted_floor_hz)",
    )
    agreement.add_argument("--hi", type=float, default=16000.0, help="trusted sweep high edge, Hz")
    agreement.add_argument("--feature-db", type=float, default=0.4, help="minimum |pooled dB| to count as a feature")
    agreement.add_argument("--testify-db", type=float, default=0.4, help="minimum |seat dB| to testify or dissent")
    agreement.add_argument("--out", default=None, help="write the result here (- for stdout)")
    agreement.set_defaults(func=_cmd_agreement)

    co_metrics = sub.add_parser(
        "co-metrics", help="NBD + SM (Olive 2004) on the on-axis and pooled-window curves — informational only",
    )
    co_metrics.add_argument("round_dir", help=_ROUND_DIR_HELP)
    co_metrics.add_argument("--out", default=None, help="write the result here (- for stdout)")
    co_metrics.set_defaults(func=_cmd_co_metrics)

    frequency = sub.add_parser("frequency", help="build the shared frequency-response view")
    frequency.add_argument(
        "source_a", help="banked round, session bundle, or JSON document for A",
    )
    frequency.add_argument(
        "source_b", nargs="?",
        help="optional banked round, session bundle, or JSON document for B",
    )
    frequency.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frequency.set_defaults(func=_cmd_frequency)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except _ROUND_TOOL_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
