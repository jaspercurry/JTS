# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The gate window ladder, and the sweep read onto the spec verdict.

* ``spec-sweep <round-dir>`` — the round's own graded spec verdict, with
  "is this band's worst bin the room or the speaker" answered AT that bin:
  the gate ladder's ``sigma_growth_ratio`` (growth with window length is what
  says room), the window's null-model-corrected contribution in dB, how many
  rungs were resolution-valid, and the frame all three are stated in.
  Disclosure only — no grade moves, and every field is a re-reading of the
  spec report the round already banked. Writes
  ``spec_gate_sensitivity.json``. Reach for ``gate-sweep --at-hz`` only
  for a bin this verdict did NOT flag.
* ``gate-sweep <round-dir>`` — the window ladder itself, over every summed
  capture the round banked: per spec band and per declared pose, what moves
  as the gate admits the room and what does not (#3495), with ``--at-hz``
  naming bins beside each band's own deepest one. What sigma growth MEANS is
  :mod:`~jasper.active_speaker.crossover_v2.gate_sweep`'s, not restated here.
  Writes ``gate_sweep.json``. Evidence for an attribution argument, never an
  EQ instruction.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.gate_sweep import summary_lines, sweep_round
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_views import spec_with_gate_sensitivity
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_TOOL_ERRORS,
    _load_round,
    _view_out,
    _write,
    add_rungs_ms_argument,
    answer,
    refused_by_name,
    resolved_out,
)

def _band_sweep_line(band: Any) -> str:
    """One band's gate read as the operator reads it: which band, then whether
    that band's own worst bin is the room or the speaker.

    ``gate_window_verdict`` is ``None`` only when the ladder never ran on this
    band; a ladder that ran and still could not call it stamps
    ``"unresolved"`` rather than nothing, and that is told apart from "never
    swept" here too.
    """
    label = f"{band.f_lo_hz:g}-{band.f_hi_hz:g} Hz"
    verdict = band.gate_window_verdict
    if verdict is None:
        return f"{label} NOT SWEPT ({band.gate_sensitivity_note})"
    if band.sigma_growth_ratio is None:
        return f"{label} {verdict.upper()} ({band.gate_sensitivity_note})"
    return (
        f"{label} @{band.max_deviation_hz:.1f} Hz {verdict.upper()} "
        f"sigma x{band.sigma_growth_ratio:.2f} over {band.n_valid_rungs} rung(s), "
        f"window {band.gate_sensitivity_db:+.2f} dB"
    )


def _cmd_spec_sweep(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    report = spec_with_gate_sensitivity(banked, rungs_ms=args.rungs_ms)
    payload = {"round_dir": str(banked.round_dir), "spec": report.to_dict()}
    written = _write(payload, args.out, _view_out(args, banked))
    return answer(
        args.command, out=written, overall_passed=report.overall_passed,
        bands=[
            {
                "band_hz": [band.f_lo_hz, band.f_hi_hz],
                "passed": band.passed,
                "gate_window_verdict": band.gate_window_verdict,
                "sigma_growth_ratio": band.sigma_growth_ratio,
                "n_valid_rungs": band.n_valid_rungs,
                "gate_sensitivity_db": band.gate_sensitivity_db,
                "max_deviation_hz": band.max_deviation_hz,
            }
            for band in report.bands
        ],
        line=(
            "spec-sweep [disclosure only, no grade moves]: "
            + "; ".join(_band_sweep_line(band) for band in report.bands)
            + (f" -> {written}" if written else "")
        ),
    )


def _cmd_gate_sweep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    try:
        report = stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, sweep_round, round_dir,
            rungs_ms=args.rungs_ms, at_hz=args.at_hz or (),
        )
    except RoundCapturesRefused as exc:
        # The ladder's own named refusal, never the resolver's coarser bucket.
        return refused_by_name(exc.reason, exc.detail)
    written = _write(
        report, args.out,
        resolved_out(round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    return answer(
        args.command, out=written, poses=len(report["poses"]),
        rungs_ms=report["frame"]["rungs_ms"],
        bands=[
            {"band_hz": band["band_hz"], "verdict": band["window_verdict"]}
            for band in report["bands"]
        ],
        features=[
            {"bin_hz": feature["bin_hz"], "verdict": feature["window_verdict"]}
            for feature in report["features"]
        ],
        line=(
            "gate-sweep [evidence only, no grade moves]: "
            + "; ".join(summary_lines(report))
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    spec_sweep = sub.add_parser(
        "spec-sweep",
        help="the round's spec verdict with room-or-speaker answered at each band's worst bin",
    )
    spec_sweep.add_argument("round_dir", help=_ROUND_DIR_HELP)
    add_rungs_ms_argument(spec_sweep)
    spec_sweep.add_argument("--out", default=None, help="write the result here (- for stdout)")
    spec_sweep.set_defaults(func=_cmd_spec_sweep)

    gate_sweep = sub.add_parser(
        "gate-sweep",
        help="sweep the gate window over this round's own captures — room or speaker, evidence only",
    )
    gate_sweep.add_argument("round_dir", help=_ROUND_DIR_HELP)
    add_rungs_ms_argument(gate_sweep)
    gate_sweep.add_argument(
        "--at-hz", type=float, nargs="+", default=None, metavar="HZ",
        help=(
            "also read these frequencies, whatever each band's deepest bin is "
            "(the spec verdict's worst bin belongs here); each is snapped to "
            "the nearest analysis-grid bin"
        ),
    )
    gate_sweep.add_argument("--out", default=None, help="write the result here (- for stdout)")
    gate_sweep.set_defaults(func=_cmd_gate_sweep)
