# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The inter-driver reverse-null delay: compute the landscape, then grade it.

* ``delay-landscape <bundle-dir> --fc-hz N`` — read a banked round's
  per-driver curves, complex-sum them across the whole delay grid, and print
  the landscape plus the two or three coordinates worth playing. No audio
  plays and no device is opened; an existing MEASURE bank answers this today.
  Writes ``delay_landscape.json``.
* ``delay-confirm <bundle-dir> --fc-hz N`` — recompute that same landscape
  and grade it against the rows ``jasper-null`` banked under
  ``<bundle>/null_runs/``, so the model's optimum is answered by the room
  rather than believed. Writes ``delay_confirmation.json``.

The method of record is compute-then-confirm
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`). Between the two
verbs sits the acoustic step: stage the printed coordinates with
``jasper-angle-capture stage --delayed-role R --delay-us N``, which
``delay-landscape`` prints ready to run, and play them with ``jasper-null``.

A refusal is an output, not an error, and the sentence saying so is printed
verbatim from the module that decided it. Applying a proposed delay is NOT
this tool's job — the prescription door owns that, with its own lobe gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    DRIVER_ROLES,
    POLARITY_INVERTED,
)
from jasper.active_speaker.crossover_v2.delay_landscape import (
    DelayLandscape,
    DelayLandscapeError,
    compute_landscape,
    confirmation_verdict,
    depth_by_coordinate,
    graded_null_rows,
    optimum_line,
    verdict_line,
)
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import (
    read_pose_curve_pair,
    take_phase_composition,
)
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, failed, stage

from ..null_door import NULL_RUNS_DIR
from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write

REFUSE_NO_ROUND = "delay_landscape_no_round"
REFUSE_NO_CURVES = "delay_landscape_no_banked_curves"
REFUSE_NO_ROWS = "delay_confirm_no_measured_rows"


def _landscape_from_bank(
    args: argparse.Namespace,
) -> tuple[DelayLandscape, str, str | None] | int:
    """The banked landscape both verbs read, or the exit code that refused it.

    ``delay-confirm`` recomputes rather than reading ``delay-landscape``'s
    artifact: a verdict must grade the coordinates against the curves they
    were staged from, not against whatever file happens to sit beside the
    round.
    """

    bundle_dir = Path(args.bundle_dir)
    spec = sweep_spec(
        crossover_fc_hz=args.fc_hz,
        upper_role=args.upper_role,
        lower_role=args.lower_role,
        signed_acoustic_path_difference_m=args.path_difference_m,
        step_us=args.step_us,
    )

    lower_role = spec.negative_delay_target
    upper_role = spec.positive_delay_target

    round_dir, why = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_artifact_dir, bundle_dir
    )
    if round_dir is None:
        return failed(
            EXIT_REFUSED,
            REFUSE_NO_ROUND,
            f"{bundle_dir}: {why}; bundle_dir must hold info.json beside "
            "evidence/v1/artifacts/crossover_v2/<capture>/",
        )

    found = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_pose_curve_pair,
        bundle_dir,
        phase=args.phase,
        position_deg=args.position_deg,
        roles=(lower_role, upper_role),
    )
    if found is None:
        return failed(
            EXIT_REFUSED,
            REFUSE_NO_CURVES,
            f"{bundle_dir}: no {args.phase} take at {args.position_deg} deg "
            f"carries curves for both {lower_role!r} and {upper_role!r}",
        )
    lower_curve, upper_curve, take_path = found

    try:
        landscape = compute_landscape(
            lower_curve, upper_curve, spec=spec, inverted_role=args.inverted_role,
        )
    except DelayLandscapeError as exc:
        # Verbatim, reason included: a bank that cannot carry a null at Fc is a
        # finding about the bank, and the module that decided it owns both the
        # sentence and the name it goes under.
        return failed(EXIT_REFUSED, exc.refusal_reason, str(exc))
    return landscape, take_path, take_phase_composition(bundle_dir, take_path)


def _bank(payload: Any, args: argparse.Namespace) -> Path | None:
    """File the artifact beside the round; :func:`main` owns what could not.

    ``--out`` names a FILE here, never ``-``: both verbs print their summary on
    stdout, so a report written there too would interleave with it — so the
    operator's path is resolved here and handed down as the default.
    """

    return _write(
        payload,
        None,
        Path(args.out) if args.out
        else Path(args.bundle_dir) / ARTIFACT_BY_VIEW[args.command].artifact,
        make_parents=True,
    )


def _cmd_delay_landscape(args: argparse.Namespace) -> int:
    computed = _landscape_from_bank(args)
    if isinstance(computed, int):
        return computed
    landscape, take_path, composition = computed

    payload = {
        "status": "proposed",
        "take_path": take_path,
        "phase": args.phase,
        "phase_composition": composition,
        "landscape": landscape.to_dict(),
        # The landscape's own spec, never a second build from the same flags:
        # the staged coordinate must come from the grid that proposed it.
        "confirm_with": [
            _stage_command(landscape.spec.dsp_candidate(coordinate), args)
            for coordinate in landscape.confirmation_coordinates_us
        ],
    }
    out = _bank(payload, args)
    print(json.dumps({**payload, "out": str(out)}, indent=2, sort_keys=True))
    print(optimum_line(landscape), file=sys.stderr)
    return EXIT_OK


def _cmd_delay_confirm(args: argparse.Namespace) -> int:
    computed = _landscape_from_bank(args)
    if isinstance(computed, int):
        return computed
    landscape, take_path, composition = computed

    rows_dir = Path(args.bundle_dir) / NULL_RUNS_DIR
    graded = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, graded_null_rows, rows_dir,
        fc_hz=args.fc_hz,
    )
    if not graded:
        return failed(
            EXIT_REFUSED,
            REFUSE_NO_ROWS,
            f"{rows_dir}: no measured inverted row at fc={args.fc_hz:g} Hz; "
            "play the delay-landscape coordinates with jasper-null "
            "--bundle-dir first",
        )

    depths = depth_by_coordinate(graded)
    verdict = confirmation_verdict(landscape, depths)
    payload = {
        "status": "confirmed",
        "verdict": verdict,
        "landscape": landscape.to_dict(),
        "take_path": take_path,
        "phase": args.phase,
        "phase_composition": composition,
        "position_deg": args.position_deg,
        "null_runs_dir": str(rows_dir),
        "graded_rows": graded,
    }
    out = _bank(payload, args)
    print(json.dumps({
        "status": "confirmed",
        "verdict": verdict["verdict"],
        "computed_optimum_us": verdict["computed_optimum_us"],
        "measured_null_depth_db": verdict["measured_null_depth_db"],
        "measured_minus_predicted_db": verdict["measured_minus_predicted_db"],
        "prescribable_delay_us": verdict["prescribable_delay_us"],
        "graded_rows": len(graded),
        "out": str(out),
    }, indent=2, sort_keys=True))
    print(verdict_line(verdict, depths), file=sys.stderr)
    return EXIT_OK


def _stage_command(candidate: Any, args: argparse.Namespace) -> str:
    """The ``jasper-angle-capture stage`` line that plays one coordinate.

    Built from ``dsp_candidate``, which maps a signed grid coordinate onto the
    executable (role, non-negative delay) pair — re-deriving that sign here
    would be a second opinion about which branch moves.

    The zero coordinate carries NO delay flags. ``delay_target`` is ``None``
    there because neither branch is delayed, and ``MeasureSpec`` refuses a
    half-stated pair, so a line naming a role with 0 us would be refused at the
    door it was printed for.

    Every line carries ``--level-matched``, including the zero coordinate.
    These are reverse-null confirmations, and a null between branches ~10 dB
    apart in sensitivity is bounded by that gap however well the coordinate is
    chosen — so a confirm line that did not ask for the level match would be
    printing a measurement whose answer the graph had already decided.
    """

    line = (
        f"jasper-angle-capture stage --angles {args.position_deg} "
        f"--polarity {POLARITY_INVERTED} --inverted-role {args.inverted_role} "
        "--level-matched"
    )
    if candidate.delay_target is None:
        return line
    return (
        f"{line} --delayed-role {candidate.delay_target} "
        f"--delay-us {candidate.delay_us:g}"
    )


def _add_landscape_arguments(child: argparse.ArgumentParser, *, out_name: str) -> None:
    """The bundle, the corner and the pose — the landscape both verbs compute."""

    child.add_argument(
        "bundle_dir",
        help="a commissioning bundle directory (the one holding info.json "
             "beside evidence/v1/artifacts/crossover_v2/<capture-session-id>/)",
    )
    child.add_argument("--fc-hz", type=float, required=True,
                       help="the applied crossover corner")
    child.add_argument("--upper-role", default="tweeter")
    child.add_argument("--lower-role", default="woofer")
    child.add_argument(
        "--inverted-role", default="tweeter", choices=sorted(DRIVER_ROLES),
        help="which branch the confirmation flips",
    )
    child.add_argument(
        "--path-difference-m", type=float, default=0.0,
        help="lower-driver path minus upper-driver path; 0.0 centres the "
             "half-period window on zero when geometry is undeclared",
    )
    child.add_argument(
        "--step-us", type=float, default=None,
        help="grid step in microseconds (50-100); the shared walk's own "
             "default is used when omitted",
    )
    child.add_argument(
        "--phase", default=PHASE_MEASURE, choices=(PHASE_MEASURE, PHASE_LATERAL),
        help="which banked phase carries the per-driver curves to sum",
    )
    child.add_argument(
        "--position-deg", type=int, default=DESIGN_AXIS_DEG,
        help="the bearing whose take is read; the reverse null is a "
             "design-axis act",
    )
    child.add_argument(
        "--out", default=None,
        help=f"where to bank the artifact — a real path, never - "
             f"(default: <bundle_dir>/{out_name})",
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    landscape = sub.add_parser(
        "delay-landscape",
        help="complex-sum a banked round's per-driver curves across the delay "
             "grid and print the computed optimum; plays nothing",
    )
    _add_landscape_arguments(
        landscape, out_name=ARTIFACT_BY_VIEW["delay-landscape"].artifact
    )
    landscape.set_defaults(func=_cmd_delay_landscape)

    confirm = sub.add_parser(
        "delay-confirm",
        help="grade the null_runs rows jasper-null banked against that same "
             "landscape",
    )
    _add_landscape_arguments(
        confirm, out_name=ARTIFACT_BY_VIEW["delay-confirm"].artifact
    )
    confirm.set_defaults(func=_cmd_delay_confirm)
