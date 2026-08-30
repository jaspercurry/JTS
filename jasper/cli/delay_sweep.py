# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the inter-driver reverse-null delay sweep.

Two verbs, both offline:

``plan``
    Print the bounded grid a sweep would walk — the geometry seed, the
    half-period bounds either side of it, the step, and every coordinate. Run
    this before any sound: it is what tells the operator how many captures the
    sweep costs at the chosen step and crossover.

``grade``
    Read banked capture rows and print the selected delay plus the honest
    verdict. This is the number the operator hands to
    ``run-crossover-round.py --alignment-prescription``; grading is deliberately
    separate from measuring so a banked sweep can be re-graded without replaying
    a single tone.

Neither verb opens a device, a socket, or a CamillaDSP connection.

**These verbs grade a full measured sweep, which is no longer how a delay is
found.** The method of record is compute-then-confirm
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`): the coordinate is
proposed from banked transfers with no audio at all, and confirmed by three
acoustic takes staged through ``jasper-angle-capture``. These verbs remain
useful for grading a sweep somebody already banked; they are not the path a new
measurement takes.

Applying a graded delay is NOT this tool's job. The prescription door owns that,
with its own lobe gate and its own receipts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.delay_sweep import (
    DelaySweepRefused,
    rows_at_pose,
    sweep_spec,
    sweep_verdict,
)
from jasper.audio_measurement.null_walk import (
    MIN_CAPTURE_COUNT,
    BoundedNullWalkSchedule,
    NullWalkError,
    select_scheduled_delay,
)

from ._logging import configure_verbose_logging

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INPUT = 2


def _spec_from_args(args: argparse.Namespace) -> Any:
    return sweep_spec(
        crossover_fc_hz=args.fc_hz,
        upper_role=args.upper_role,
        lower_role=args.lower_role,
        signed_acoustic_path_difference_m=args.path_difference_m,
        step_us=args.step_us,
    )


def _cmd_plan(args: argparse.Namespace) -> int:
    spec = _spec_from_args(args)
    coarse = spec.coarse_candidate_delays_us()
    payload = {
        "status": "planned",
        "spec": spec.to_dict(),
        "coarse_delays_us": list(coarse),
        "coarse_candidate_count": len(coarse),
        # The refinement phase adds at most two neighbours of whichever coarse
        # coordinate measures deepest, so the cost is known before any sound.
        "maximum_captures": (len(coarse) + 2) * args.repeats * max(len(args.poses), 1),
        "repeats_per_coordinate": args.repeats,
        "poses_deg": list(args.poses),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def _rows_by_delay(document: Any) -> dict[float, list[Mapping[str, Any]]]:
    """Read banked rows, keyed by the exact coordinate each was captured at."""

    if isinstance(document, Mapping):
        document = document.get("rows", document)
    if not isinstance(document, Mapping):
        raise ValueError("capture document must map a delay coordinate to its rows")
    rows: dict[float, list[Mapping[str, Any]]] = {}
    for key, value in document.items():
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"rows for coordinate {key!r} must be a list")
        rows[float(key)] = [item for item in value if isinstance(item, Mapping)]
    return rows


def _cmd_grade(args: argparse.Namespace) -> int:
    spec = _spec_from_args(args)
    try:
        document = json.loads(Path(args.captures).read_text(encoding="utf-8"))
        rows = _rows_by_delay(document)
    except (OSError, ValueError) as exc:
        print(f"unreadable captures: {exc}", file=sys.stderr)
        return EXIT_INPUT

    axis = tuple(args.poses)[0] if args.poses else None
    on_axis = rows_at_pose(rows, axis)
    try:
        schedule = BoundedNullWalkSchedule.from_coarse_evidence(
            spec,
            {
                coordinate: on_axis[coordinate]
                for coordinate in spec.coarse_candidate_delays_us()
                if coordinate in on_axis
            },
        )
        selection = select_scheduled_delay(spec, schedule, on_axis)
    except NullWalkError as exc:
        print(
            json.dumps(
                {"status": "refused", "reason": "walk_evidence_invalid",
                 "detail": str(exc)},
                indent=2, sort_keys=True,
            )
        )
        return EXIT_REFUSED

    verdict = sweep_verdict(
        selection,
        spec=spec,
        rows_by_delay=rows,
        poses_deg=tuple(args.poses) or (None,),
    )
    print(
        json.dumps(
            {"status": "graded", "selection": selection, "verdict": verdict},
            indent=2, sort_keys=True, default=str,
        )
    )
    return EXIT_OK


def _repeats(raw: str) -> int:
    value = int(raw)
    if value < MIN_CAPTURE_COUNT:
        raise argparse.ArgumentTypeError(
            f"repeats must be at least {MIN_CAPTURE_COUNT}"
        )
    return value


def _poses(raw: str) -> tuple[int | None, ...]:
    if not raw.strip():
        return (None,)
    return tuple(int(item) for item in raw.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-delay-sweep",
        description="Plan and grade an inter-driver reverse-null delay sweep.",
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("plan", _cmd_plan), ("grade", _cmd_grade)):
        child = sub.add_parser(name)
        child.add_argument("--fc-hz", type=float, required=True,
                           help="the applied crossover corner")
        child.add_argument("--upper-role", default="tweeter")
        child.add_argument("--lower-role", default="woofer")
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
            "--repeats", type=_repeats, default=MIN_CAPTURE_COUNT,
            help=f"captures per coordinate; at least {MIN_CAPTURE_COUNT}, below "
                 "which the shared walk cannot call a coordinate repeatable",
        )
        child.add_argument("--poses", type=_poses, default=(None,),
                           help="comma-separated pose angles, e.g. 0,-7,7")
        if name == "grade":
            child.add_argument("--captures", required=True,
                               help="JSON of banked rows keyed by coordinate")
        child.set_defaults(func=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    try:
        return int(args.func(args))
    except DelaySweepRefused as exc:
        print(
            json.dumps(
                {"status": "refused", "reason": exc.reason, "detail": exc.detail},
                indent=2, sort_keys=True,
            )
        )
        return EXIT_REFUSED
    except NullWalkError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
