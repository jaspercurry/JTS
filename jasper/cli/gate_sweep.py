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
to make (issue #3495's 2026-09-01 amendment).

**A refusal is an output, not an error.** Every refusal names the input that
was missing — no captures, no programs, a capture whose program could not be
matched by content hash — as JSON on stdout and one sentence on stderr.

**Exit codes are the contract**: 0 swept, 1 refused, 2 the round could not be
read at all.

Applying anything this reports is NOT this tool's job. It plays nothing,
changes nothing, and its numbers are evidence for an attribution argument,
never an EQ instruction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from jasper.active_speaker.crossover_v2.gate_sweep import (
    DEFAULT_RUNGS_MS,
    GateSweepRefused,
    sweep_round,
)

from ._logging import configure_verbose_logging

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INPUT = 2

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (plays nothing)"

DEFAULT_OUT_NAME = "gate_sweep.json"


def _refused(reason: str, detail: str) -> int:
    print(
        json.dumps(
            {"status": "refused", "reason": reason, "detail": detail},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"refused ({reason}): {detail}", file=sys.stderr)
    return EXIT_REFUSED


def _cmd_sweep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    try:
        report = sweep_round(round_dir, rungs_ms=args.rungs_ms)
    except GateSweepRefused as exc:
        return _refused(exc.reason, json.dumps(exc.detail, sort_keys=True, default=str))
    out = Path(args.out) if args.out else round_dir / DEFAULT_OUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=float))
    print(json.dumps({"status": "swept", "out": str(out)}, indent=2, sort_keys=True))
    for band in report["bands"]:
        sensitivity = band["sensitivity"]
        if sensitivity is None:
            print(
                f"  {band['band_hz'][0]:g}-{band['band_hz'][1]:g} Hz: no sensitivity "
                f"({band['sensitivity_null_reason']})",
                file=sys.stderr,
            )
            continue
        print(
            f"  {band['band_hz'][0]:g}-{band['band_hz'][1]:g} Hz: worst bin "
            f"{band['worst_bin_hz']:.1f} Hz, sigma growth "
            f"{sensitivity['sigma_growth_ratio']:.2f}x over "
            f"{sensitivity['shortest_valid_rung_ms']:g}-"
            f"{sensitivity['longest_valid_rung_ms']:g} ms, corrected long-rung "
            f"delta {sensitivity['corrected_delta_db']:+.2f} dB",
            file=sys.stderr,
        )
        means = band["band_mean_sigma_db_by_rung"]
        print(
            "      band mean sigma "
            + " ".join(f"{rung}ms={value:.2f}" for rung, value in means.items()),
            file=sys.stderr,
        )
    return EXIT_OK


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
            "WHEN NOT TO USE\n"
            "  Not for a round with one pose (across-pose sigma needs two), not\n"
            "  for prescribing EQ, and not as a verdict: it publishes evidence\n"
            "  and the frame that evidence is stated in, nothing more.\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-gate-sweep captures/<campaign>/<round> --out /tmp/sweep.json\n"
            "  jasper-gate-sweep <round> --rungs-ms 3 5 7 12 20\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- swept; the report was written\n"
            "  1  EXIT_REFUSED -- a named input was missing (no captures, no\n"
            "     programs, no content-hash match for a capture's program)\n"
            "  2  EXIT_INPUT -- the round could not be read (OSError)"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "round_dir", help="banked round directory (the one holding bundle/)"
    )
    parser.add_argument(
        "--rungs-ms",
        type=float,
        nargs="+",
        default=list(DEFAULT_RUNGS_MS),
        metavar="MS",
        help=(
            "gate ladder, in milliseconds "
            f"(default: {' '.join(f'{r:g}' for r in DEFAULT_RUNGS_MS)})"
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"where to write the report (default: <round_dir>/{DEFAULT_OUT_NAME})",
    )
    parser.set_defaults(func=_cmd_sweep)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"unusable request: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except OSError as exc:
        print(f"unreadable round: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
