# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the room-vs-speaker gate sweep: read, never play.

Points this at a banked round directory and it deconvolves every summed
capture, gates each one at a ladder of window lengths, and reports — per
spec band, per declared pose — what moves with the window and what does
not. A feature whose across-pose spread GROWS as the window admits the room
is the room's; one whose spread is large but window-invariant is the
speaker's directivity, and the two are the discrimination this tool exists
to make (#3495).

**A refusal is an output, not an error.** Every refusal names the input that
was missing: no captures, no programs, a capture whose program could not be
matched by content hash. It publishes the shared failure record, whose shape
docs/tuning-operator-runbook.md's "Exit codes" states once.

Applying anything this reports is NOT this tool's job. It plays nothing,
changes nothing, and its numbers are evidence for an attribution argument,
never an EQ instruction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.gate_sweep import (
    DEFAULT_RUNGS_MS,
    sweep_round,
)
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused

from ._logging import configure_verbose_logging
from ._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    StageFailed,
    failed,
    stage,
)
from ._report import write_report

#: Named like every other refusal here: the input that was missing.
REFUSE_UNUSABLE_REQUEST = "gate_sweep_unusable_request"
REFUSE_UNREADABLE_ROUND = "gate_sweep_unreadable_round"
REFUSE_UNWRITABLE_OUT = "gate_sweep_unwritable_out"

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (plays nothing)"

DEFAULT_OUT_NAME = "gate_sweep.json"


def _sensitivity_line(label: str, payload: Mapping[str, Any]) -> str:
    """One report entry as the operator reads it: label, numbers, verdict.

    The verdict is the engine's own word, and the routes that produced it are
    printed with it — a reader who sees ``moved`` without them cannot tell the
    room-owned features apart from the ones only one route flagged.
    """
    verdict = payload["window_verdict"]
    reasons = ", ".join(payload["window_verdict_reasons"])
    sensitivity = payload["sensitivity"]
    if sensitivity is None:
        return f"{label}: no sensitivity ({reasons}) -> {verdict}"
    return (
        f"{label}: sigma growth "
        f"{sensitivity['sigma_growth_ratio']:.2f}x over "
        f"{sensitivity['shortest_valid_rung_ms']:g}-"
        f"{sensitivity['longest_valid_rung_ms']:g} ms, corrected long-rung "
        f"delta {sensitivity['corrected_delta_db']:+.2f} dB -> {verdict}"
        + (f" ({reasons})" if reasons else "")
    )


def _cmd_sweep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    try:
        report = sweep_round(round_dir, rungs_ms=args.rungs_ms, at_hz=args.at_hz or ())
    except RoundCapturesRefused as exc:
        return failed(
            EXIT_REFUSED, exc.reason,
            json.dumps(exc.detail, sort_keys=True, default=str),
        )
    out = stage(
        EXIT_WRITE_FAILED, (OSError,), write_report,
        report, args.out, round_dir / DEFAULT_OUT_NAME, make_parents=True,
    )
    print(json.dumps({"status": "swept", "out": str(out or "-")}, indent=2, sort_keys=True))
    for band in report["bands"]:
        label = f"  {band['band_hz'][0]:g}-{band['band_hz'][1]:g} Hz"
        worst_bin_hz = band.get("worst_bin_hz")
        if worst_bin_hz is not None:
            label += f" (worst bin {worst_bin_hz:.1f} Hz)"
        print(_sensitivity_line(label, band), file=sys.stderr)
        means = band.get("band_mean_sigma_db_by_rung")
        if means:
            print(
                "      band mean sigma "
                + " ".join(f"{rung}ms={value:.2f}" for rung, value in means.items()),
                file=sys.stderr,
            )
    for feature in report["features"]:
        label = f"  at {feature['requested_hz']:g} Hz (bin {feature['bin_hz']:.1f} Hz)"
        print(_sensitivity_line(label, feature), file=sys.stderr)
    return EXIT_OK


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-gate-sweep",
        description=(
            "Sweep a banked round's gate window and report, per spec band and "
            "declared pose, what moves with the window. Computes only; plays "
            "nothing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  Discriminate a room feature from a loudspeaker one. Across-pose\n"
            "  sigma that GROWS with window length is the room; sigma that is\n"
            "  large but window-invariant is directivity. Long-rung deltas are\n"
            "  null-model corrected, because the window's own bias on a narrow\n"
            "  feature is large and never vanishes.\n"
            "\n"
            "ANCHORING\n"
            "  Per band the report anchors on that band's deepest bin, which is\n"
            "  not in general its most window-divergent one. --at-hz names the\n"
            "  bin you already care about -- the spec verdict's worst bin -- and\n"
            "  reads it the same way, null model included.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  Not for a round with one pose (across-pose sigma needs two), not\n"
            "  for prescribing EQ, and not as a verdict: it publishes evidence\n"
            "  and the frame that evidence is stated in, nothing more.\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-gate-sweep captures/<campaign>/<round> --out /tmp/sweep.json\n"
            "  jasper-gate-sweep <round> --rungs-ms 3 5 7 12 20\n"
            "  jasper-gate-sweep <round> --at-hz 441.6 358\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- swept; the report was written\n"
            "  1  EXIT_REFUSED -- a named input was missing (no captures, no\n"
            "     programs, no content-hash match for a capture's program)\n"
            "  2  EXIT_UNREADABLE -- the round could not be read\n"
            "  3  EXIT_WRITE_FAILED -- swept, but --out could not be written"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "round_dir", help="banked round directory (the one holding bundle/)"
    )
    add_rungs_ms_argument(parser)
    parser.add_argument(
        "--at-hz",
        type=float,
        nargs="+",
        default=None,
        metavar="HZ",
        help=(
            "also read these frequencies, whatever each band's deepest bin is "
            "(the spec verdict's worst bin belongs here); each is snapped to "
            "the nearest analysis-grid bin"
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"where to write the report (- for stdout; default: <round_dir>/{DEFAULT_OUT_NAME})",
    )
    parser.set_defaults(func=_cmd_sweep)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    try:
        return int(args.func(args))
    except StageFailed as staged:
        return failed(staged.code, REFUSE_UNWRITABLE_OUT, str(staged))
    except ValueError as exc:
        return failed(EXIT_UNREADABLE, REFUSE_UNUSABLE_REQUEST, str(exc))
    except OSError as exc:
        return failed(EXIT_UNREADABLE, REFUSE_UNREADABLE_ROUND, str(exc))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
