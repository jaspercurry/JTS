# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for the round-grading comparison views (issue #2769).

One console script, four subcommands — each a thin argparse wrapper over
:mod:`jasper.active_speaker.crossover_v2.round_views`, which owns every
number this tool prints. This module reads banked round directories (the
tree ``scripts/bank-crossover-round.sh`` produces), calls the product view,
and writes the result as JSON — into the round directory by default, so the
artifact travels with the evidence it was computed from, per the same
"who prints the sentence" boundary :mod:`jasper.cli.crossover_prescriber`
already keeps for its own JSON output.

Subcommands:

* ``frozen <baseline-dir> <target-dir>`` — grade ``target`` shipped AND
  frozen to ``baseline``'s per-position reference levels. Writes
  ``<target-dir>/frozen_reference.json``.
* ``per-seat <round-dir>`` — every banked position plus the VERIFY pose
  (when its dump-ring capture is banked), normalised onto one comparable
  basis. Writes ``<round-dir>/per_seat.json``.
* ``repeat <round-dir> [<round-dir> ...]`` — session-to-session spread of
  the pooled honest figures (the stop criterion). Writes
  ``<first-round-dir>/repeatability.json``.
* ``agreement <round-dir>`` — per-position sign/magnitude testimony for
  every feature in the trusted sweep, built from the same per-seat curves
  ``per-seat`` computes. Writes ``<round-dir>/agreement.json``.

Every subcommand accepts ``--out PATH`` to write somewhere else instead
(``-`` for stdout), and prints a one-line human summary to stderr either
way. Exit ``0`` on success, ``1`` when a round directory could not be read
into a comparable view (:class:`~jasper.active_speaker.crossover_v2.round_views.RoundViewsError`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.crossover_v2.round_views import (
    AGREEMENT_TESTIFY_MIN,
    RoundViewsError,
    agreement_table,
    default_agreement_lo_hz,
    frozen_reference_grade,
    load_banked_round,
    per_seat_curves,
    repeatability_spread,
    verify_pose_curve,
)

EXIT_OK = 0
EXIT_ERROR = 1

#: A banked round directory is operator-pulled evidence, not a validated
#: input — the documented failure shapes it can hand back are broader than
#: the product module's own typed :class:`RoundViewsError`. A malformed
#: evidence document can be missing a key (``KeyError``), hold the wrong type
#: at one (``TypeError``), or not parse at all (``ValueError``, which
#: ``json.JSONDecodeError`` subclasses); and any of the files this tool reads
#: can simply not exist or not be readable (``OSError``). Every one of these
#: is "the round is unreadable", exactly what exit 1 already means — this
#: tuple is what makes that the ACTUAL behaviour instead of an unhandled
#: traceback the first time a real, imperfect round hits it.
#:
#: ``struct.error`` was here for one reader that no longer exists: a
#: header-truncated dump-ring WAV raised it out of ``scipy.io.wavfile.read``
#: while ``verify_pose_curve`` still deconvolved raw ring bytes. That view
#: reads the round's banked curve now, no code on this path opens a WAV, and
#: catching an exception nothing can raise is not how it is caught.
_ROUND_READ_ERRORS: tuple[type[Exception], ...] = (
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


def _cmd_frozen(args: argparse.Namespace) -> int:
    try:
        baseline = load_banked_round(Path(args.baseline_dir))
        target = load_banked_round(Path(args.target_dir))
        result = frozen_reference_grade(baseline, target)
    except _ROUND_READ_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    written = _write_json(
        result.to_dict(), args.out, Path(args.target_dir) / "frozen_reference.json"
    )
    print(
        f"frozen-reference: shipped={result.shipped} frozen={result.frozen}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_per_seat(args: argparse.Namespace) -> int:
    try:
        banked = load_banked_round(Path(args.round_dir))
        verify = verify_pose_curve(banked)
        seats = per_seat_curves(
            banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
        )
    except _ROUND_READ_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    payload = {
        "round_dir": str(banked.round_dir),
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
    written = _write_json(payload, args.out, Path(args.round_dir) / "per_seat.json")
    print(
        f"per-seat: {len(seats)} seat(s) ({', '.join(s.position_id for s in seats)}); "
        f"verify pose {'included' if verify.curve is not None else f'ABSENT ({verify.reason})'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_repeat(args: argparse.Namespace) -> int:
    try:
        rounds = [(round_dir, load_banked_round(Path(round_dir))) for round_dir in args.round_dirs]
        result = repeatability_spread(rounds)
    except _ROUND_READ_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    default_path = Path(args.round_dirs[0]) / "repeatability.json"
    written = _write_json(result.to_dict(), args.out, default_path)
    shipped = next((m for m in result.metrics if m.name == "shipped_linear_pool_db"), None)
    spread = shipped.spread() if shipped else None
    print(
        f"repeatability: {len(result.round_labels)} round(s); "
        f"shipped_linear_pool_db spread={spread}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_agreement(args: argparse.Namespace) -> int:
    try:
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
    except _ROUND_READ_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    payload = {
        "round_dir": str(banked.round_dir),
        "seats": [seat.position_id for seat in seats],
        "swept_band_hz": [lo_hz, args.hi],
        "feature_db": args.feature_db,
        "testify_db": args.testify_db,
        "features": [feature.to_dict() for feature in features],
    }
    written = _write_json(payload, args.out, Path(args.round_dir) / "agreement.json")
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


def _add_norm_band_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--norm-lo", type=float, default=400.0, help="normalisation band low edge, Hz (default 400)")
    parser.add_argument("--norm-hi", type=float, default=8000.0, help="normalisation band high edge, Hz (default 8000)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-round-views",
        description=(
            "The round-grading comparison views: frozen-reference grading, "
            "per-seat curves, session-to-session repeatability, and per-seat "
            "agreement — over a banked round directory."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    frozen = sub.add_parser("frozen", help="grade a round shipped and frozen to a baseline's reference")
    frozen.add_argument("baseline_dir", help="banked round directory to freeze the reference from")
    frozen.add_argument("target_dir", help="banked round directory to grade")
    frozen.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frozen.set_defaults(func=_cmd_frozen)

    per_seat = sub.add_parser("per-seat", help="every banked position plus the VERIFY pose, normalised")
    per_seat.add_argument("round_dir", help="banked round directory")
    _add_norm_band_args(per_seat)
    per_seat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    per_seat.set_defaults(func=_cmd_per_seat)

    repeat = sub.add_parser("repeat", help="session-to-session spread of the pooled honest figures")
    repeat.add_argument("round_dirs", nargs="+", help="two or more banked round directories")
    repeat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    repeat.set_defaults(func=_cmd_repeat)

    agreement = sub.add_parser("agreement", help="per-seat sign/magnitude testimony for every feature")
    agreement.add_argument("round_dir", help="banked round directory")
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
