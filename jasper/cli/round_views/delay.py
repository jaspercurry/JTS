# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compute delay proposals and compare banked acoustic confirmations."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.contracts import DRIVER_ROLES
from jasper.active_speaker.crossover_v2.commanded import profile_crossover_fc_hz
from jasper.active_speaker.crossover_v2.evidence_packet import applied_profile_source
from jasper.active_speaker.crossover_v2.position_cycle import select_pose_curve_pair
from jasper.active_speaker.crossover_v2.round_inputs import banked_round_of, round_inputs
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
    _BUNDLE_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    refused_by_name,
    resolved_out,
)

REFUSE_NO_ROWS = "delay_confirm_no_measured_rows"


def _landscape_from_bank(args: argparse.Namespace) -> BankedLandscape:
    try:
        bundle = Path(args.bundle_dir)
        if (bundle / "bundle").is_dir() or (bundle / "info.json").is_file():
            inputs = round_inputs(banked_round_of(bundle) or bundle)
            bundle = inputs.session_dir
            if args.fc_hz is None:
                args.fc_hz = profile_crossover_fc_hz(applied_profile_source(inputs.applied_profile_path)[0])
        pair = select_pose_curve_pair(
            bundle, phases=(args.phase,) if args.phase else (PHASE_MEASURE, PHASE_LATERAL),
            position_deg=args.position_deg, roles=(args.lower_role, args.upper_role),
            take_path=args.take_path,
        )
        if pair is not None:
            args.phase, args.position_deg = pair.take.phase, pair.take.position_deg
        args.inverted_role = args.inverted_role or (pair.document.get("inverted_role") if pair else None) or args.upper_role
        if args.fc_hz is None:
            raise DelayLandscapeError("The bank has no crossover corner; supply --fc-hz")
        return landscape_from_bank(
            bundle,
            spec=sweep_spec(
                crossover_fc_hz=args.fc_hz,
                upper_role=args.upper_role,
                lower_role=args.lower_role,
                signed_acoustic_path_difference_m=args.path_difference_m,
                step_us=args.step_us,
            ),
            inverted_role=args.inverted_role,
            pair=pair,
        )
    except DelayLandscapeError:
        raise
    except _ROUND_TOOL_ERRORS as exc:
        raise StageFailed(EXIT_UNREADABLE, exc) from exc


def _bank(payload: Any, args: argparse.Namespace) -> Path | None:
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
        "delay_coordinates": "residual addition to measured tune" if composition == "complete_tune_measured" else "neutral branch delay",
        "confirm_with": [
            "Author full candidate variants with these residual changes added to the measured tune's alignment; compare their summed captures with tournament. jasper-null uses neutral branches and cannot confirm this tune."
        ] if composition == "complete_tune_measured" else confirmation_stage_commands(
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

    if composition == "complete_tune_measured":
        return refused_by_name("delay_confirm_graph_mismatch", "These curves include the complete tune. Compare complete candidate variants with tournament; neutral jasper-null rows cannot confirm residual tune changes.")

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
        "bundle_dir", metavar=_BUNDLE_DIR_METAVAR,
        help="banked round or its commissioning bundle",
    )
    child.add_argument("--fc-hz", type=float,
                       help="override the banked applied crossover corner")
    child.add_argument("--upper-role", default="tweeter")
    child.add_argument("--lower-role", default="woofer")
    child.add_argument("--take-path", help="exact indexed take path; otherwise the latest matching pose is read")
    child.add_argument(
        "--inverted-role", default=None, choices=sorted(DRIVER_ROLES),
        help="override the banked inverted role; otherwise the upper role",
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
        "--phase", default=None, choices=(PHASE_MEASURE, PHASE_LATERAL),
        help="override the selected take phase",
    )
    child.add_argument(
        "--position-deg", type=int, default=None,
        help="override the selected take bearing",
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
