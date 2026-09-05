# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The inter-driver reverse-null delay: compute the landscape, then grade it.

* ``delay-landscape <bundle-dir> --fc-hz N`` — read a banked round's
  per-driver curves, complex-sum them across the whole delay grid, and name
  the optimum plus the two or three coordinates worth playing. No audio
  plays and no device is opened; an existing MEASURE bank answers this today.
  The grid itself stays in ``delay_landscape.json``, which it writes.
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
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG, DRIVER_ROLES
from jasper.active_speaker.crossover_v2.delay_landscape import (
    BankedLandscape,
    DelayLandscapeError,
    confirmation_stage_commands,
    confirmation_verdict,
    depth_by_coordinate,
    graded_null_rows,
    landscape_from_bank,
    optimum_line,
    verdict_line,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.cli._refusal import EXIT_UNREADABLE, StageFailed, stage

from ..null_door import NULL_RUNS_DIR
from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    refused_by_name,
    resolved_out,
)

REFUSE_NO_ROWS = "delay_confirm_no_measured_rows"


def _landscape_from_bank(args: argparse.Namespace) -> BankedLandscape:
    """The engine's read of the bank, with its exits as this family's.

    A round that could not be READ exits through the LOAD stage;
    :class:`DelayLandscapeError` is re-raised ahead of that because it
    subclasses ``ValueError`` — a bank that read and could not carry a
    landscape refuses by name, and each verb publishes that name.
    """

    try:
        return landscape_from_bank(
            Path(args.bundle_dir),
            spec=sweep_spec(
                crossover_fc_hz=args.fc_hz,
                upper_role=args.upper_role,
                lower_role=args.lower_role,
                signed_acoustic_path_difference_m=args.path_difference_m,
                step_us=args.step_us,
            ),
            inverted_role=args.inverted_role,
            phase=args.phase,
            position_deg=args.position_deg,
        )
    except DelayLandscapeError:
        raise
    except _ROUND_TOOL_ERRORS as exc:
        raise StageFailed(EXIT_UNREADABLE, exc) from exc


def _bank(payload: Any, args: argparse.Namespace) -> Path | None:
    """``--out`` names a FILE here, never ``-``: the delay grid is what the
    artifact is for. Where the default lands is :func:`resolved_out`'s.
    """

    beside = resolved_out(
        Path(args.bundle_dir), ARTIFACT_BY_VIEW[args.command].artifact
    )
    return _write(
        payload, None, Path(args.out) if args.out else beside, make_parents=True,
    )


def _cmd_delay_landscape(args: argparse.Namespace) -> int:
    try:
        landscape, take_path, composition = _landscape_from_bank(args)
    except DelayLandscapeError as exc:
        return refused_by_name(exc.refusal_reason, str(exc))

    payload = {
        "status": "proposed",
        "take_path": take_path,
        "phase": args.phase,
        "phase_composition": composition,
        "landscape": landscape.to_dict(),
        "confirm_with": confirmation_stage_commands(
            landscape, position_deg=args.position_deg,
            inverted_role=args.inverted_role,
        ),
    }
    return answer(
        args.command, out=_bank(payload, args), take_path=take_path,
        phase=args.phase, phase_composition=composition,
        best_coordinate_us=landscape.best_coordinate_us,
        confirmation_coordinates_us=list(landscape.confirmation_coordinates_us),
        next=payload["confirm_with"],
        line=optimum_line(landscape),
    )


def _cmd_delay_confirm(args: argparse.Namespace) -> int:
    try:
        landscape, take_path, composition = _landscape_from_bank(args)
    except DelayLandscapeError as exc:
        return refused_by_name(exc.refusal_reason, str(exc))

    rows_dir = Path(args.bundle_dir) / NULL_RUNS_DIR
    graded = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, graded_null_rows, rows_dir,
        fc_hz=args.fc_hz,
    )
    if not graded:
        return refused_by_name(
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
    return answer(
        args.command, out=_bank(payload, args), verdict=verdict["verdict"],
        computed_optimum_us=verdict["computed_optimum_us"],
        measured_null_depth_db=verdict["measured_null_depth_db"],
        measured_minus_predicted_db=verdict["measured_minus_predicted_db"],
        prescribable_delay_us=verdict["prescribable_delay_us"],
        graded_rows=len(graded),
        line=verdict_line(verdict, depths),
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
