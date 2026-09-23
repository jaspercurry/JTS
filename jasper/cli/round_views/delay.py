# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Propose delay coordinates from banked curves."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.contracts import DRIVER_ROLES
from jasper.active_speaker.crossover_v2.commanded import profile_crossover_fc_hz
from jasper.active_speaker.crossover_v2.evidence_packet.incumbent import applied_profile_source
from jasper.active_speaker.crossover_v2.position_cycle import PoseCurvePair, select_pose_curve_pair
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, banked_round_of, round_inputs
from jasper.active_speaker.crossover_v2.delay_landscape import (
    BankedLandscape, DelayLandscapeError,
    landscape_from_bank,
    optimum_line,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, StageFailed, failed

from ._common import (
    ARTIFACT_BY_VIEW,
    _BUNDLE_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    resolved_out,
    subject,
)


def _landscape_from_bank(
    args: argparse.Namespace,
) -> tuple[BankedLandscape, PoseCurvePair, RoundInputs | None]:
    try:
        bundle, inputs = Path(args.bundle_dir), None
        if (bundle / "bundle").is_dir() or (bundle / "info.json").is_file():
            inputs = round_inputs(banked_round_of(bundle) or bundle)
            bundle = inputs.session_dir
            if args.fc_hz is None:
                args.fc_hz = profile_crossover_fc_hz(applied_profile_source(inputs.applied_profile_path)[0])
        search_detail: dict[str, Any] = {}
        pair = select_pose_curve_pair(
            bundle, phases=(PHASE_MEASURE, PHASE_LATERAL), position_deg=None,
            roles=(args.lower_role, args.upper_role), take_id=args.take, search_detail=search_detail,
        )
        args.inverted_role = args.inverted_role or (pair.document.get("inverted_role") if pair else None) or args.upper_role
        if args.fc_hz is None:
            raise DelayLandscapeError("The bank has no crossover corner; supply --fc-hz")
        banked = landscape_from_bank(
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
            search_detail=search_detail,
        )
        assert pair is not None  # landscape_from_bank refuses a missing pair
        return banked, pair, inputs
    except DelayLandscapeError as exc:
        if exc.detail:
            exc.detail["message"] = str(exc)
        raise
    except _ROUND_TOOL_ERRORS as exc:
        raise StageFailed(EXIT_UNREADABLE, exc) from exc


def _cmd_delay_landscape(args: argparse.Namespace) -> int:
    try:
        (landscape, take_path, composition), pair, inputs = _landscape_from_bank(args)
    except DelayLandscapeError as exc:
        return failed(EXIT_REFUSED, exc.refusal_reason, exc.detail or str(exc))

    payload = {
        "status": "proposed",
        "take_path": take_path,
        "phase": pair.take.phase,
        "phase_composition": composition,
        "landscape": landscape.to_dict(),
        "delay_coordinates": "residual addition to measured tune" if composition == "complete_tune_measured" else "neutral branch delay",
        "confirm_with": [
            "Author full candidate variants with these residual delay changes added to the measured tune's alignment; compare their summed captures with jasper-round trial."
            if composition == "complete_tune_measured" else
            "Author candidate variants whose branch delay is set to these coordinates; compare their summed captures with jasper-round trial."
        ],
    }
    spec = ARTIFACT_BY_VIEW[args.command]
    beside = resolved_out(Path(args.bundle_dir), spec.artifact)
    written = _write(payload, None, Path(args.out) if args.out else beside, schema=spec.schema, make_parents=True)
    take_id = pair.document.get("take_id")
    return answer(
        args.command, schema=spec.schema, subject=subject(inputs, take_ids=[take_id] if take_id else None),
        parameters={"fc_hz": landscape.spec.crossover_fc_hz, "step_us": landscape.spec.step_us,
                    "path_difference_m": args.path_difference_m, "inverted_role": landscape.inverted_role},
        out=written, take_path=take_path, phase=payload["phase"], phase_composition=composition,
        best_coordinate_us=landscape.best_coordinate_us,
        confirmation_coordinates_us=list(landscape.confirmation_coordinates_us),
        next=payload["confirm_with"],
        line=optimum_line(landscape),
    )


def _add_landscape_arguments(child: argparse.ArgumentParser, *, out_name: str) -> None:
    child.add_argument("bundle_dir", metavar=_BUNDLE_DIR_METAVAR,
                       help="banked round or its commissioning bundle")
    child.add_argument("--fc-hz", type=float,
                       help="override the banked applied crossover corner")
    child.add_argument("--upper-role", default="tweeter")
    child.add_argument("--lower-role", default="woofer")
    child.add_argument("--take", help="take ID to read; otherwise the latest take carrying both driver curves")
    child.add_argument(
        "--inverted-role", default=None, choices=sorted(DRIVER_ROLES),
        help="override the banked inverted role; otherwise the upper role",
    )
    child.add_argument("--path-difference-m", type=float, default=0.0,
                       help="lower-driver path minus upper-driver path in metres")
    child.add_argument("--step-us", type=float, default=None,
                       help="grid step in microseconds (50-100); defaults to the walk's step")
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
