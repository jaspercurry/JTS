# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the inter-driver reverse-null delay: compute, then grade.

Two verbs, both offline:

``propose``
    Read a banked round's per-driver curves, complex-sum them across the whole
    delay grid, and print the landscape plus the two or three coordinates worth
    playing. **No audio plays and no device is opened** — an existing MEASURE
    bank answers this today.

``confirm``
    Re-compute that same landscape and grade it against the rows ``jasper-null``
    banked under ``<bundle>/null_runs/``, so the model's optimum is answered by
    the room rather than believed.

The method of record is compute-then-confirm
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`). Between the two
verbs sits the acoustic step: stage the printed coordinates with
``jasper-angle-capture stage --delayed-role R --delay-us N``, which ``propose``
prints ready to run, and play them with ``jasper-null``.

**A refusal is an output, not an error.** Banked curves whose shared band does
not bracket Fc cannot support a null there, and the sentence saying so is
printed verbatim from the module that decided it. A band that brackets Fc but
falls short of the canonical shoulders is NOT refused: it proposes on the span
it has, and the printed answer says so.

Applying a proposed delay is NOT this tool's job. The prescription door owns
that, with its own lobe gate and its own receipts.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

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
)
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import (
    read_pose_curve_pair,
    take_artifact_path,
)
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.audio_measurement.null_walk import NullWalkError

from ._logging import configure_verbose_logging
from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, failed
from ._report import file_report
from .null_door import NULL_RUNS_DIR

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (plays nothing)"

REFUSE_NO_ROUND = "delay_propose_no_round"
REFUSE_NO_CURVES = "delay_propose_no_banked_curves"
REFUSE_LANDSCAPE = "delay_propose_landscape_unsupported"
REFUSE_NO_ROWS = "delay_confirm_no_measured_rows"
REFUSE_UNWRITABLE_OUT = "delay_sweep_unwritable_out"

LANDSCAPE_OUT_NAME = "delay_landscape.json"
CONFIRMATION_OUT_NAME = "delay_confirmation.json"


def _spec_from_args(args: argparse.Namespace) -> Any:
    return sweep_spec(
        crossover_fc_hz=args.fc_hz,
        upper_role=args.upper_role,
        lower_role=args.lower_role,
        signed_acoustic_path_difference_m=args.path_difference_m,
        step_us=args.step_us,
    )


def _take_phase_composition(bundle_dir: Path, take_path: str) -> str | None:
    """Which composition the take's curves carry, or ``None`` on a legacy take.

    Read off the record (``spatial.phase_composition``), never re-derived from
    ``--phase``: which phase was commanded and whether the analysis composed
    the configured crossover in are two facts, and §4 step 1 turns on the
    second. A take that states neither — banked before the field, or captured
    with no protection to retain — reads ``None``, never one of the two.
    """

    try:
        raw = json.loads(take_artifact_path(bundle_dir, take_path).read_text())
    except (OSError, ValueError):
        return None
    stated = raw.get("phase_composition") if isinstance(raw, Mapping) else None
    return stated if isinstance(stated, str) and stated else None


def _landscape_from_bank(
    args: argparse.Namespace,
) -> tuple[DelayLandscape, str, str | None] | int:
    """The banked landscape both verbs read, or the exit code that refused it.

    ``confirm`` recomputes rather than reading ``propose``'s artifact: a
    verdict must grade the coordinates against the curves they were staged
    from, not against whatever file happens to sit beside the round.
    """

    bundle_dir = Path(args.bundle_dir)
    spec = _spec_from_args(args)
    lower_role = spec.negative_delay_target
    upper_role = spec.positive_delay_target

    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        return failed(
            EXIT_REFUSED,
            REFUSE_NO_ROUND,
            f"{bundle_dir}: {why}; bundle_dir must hold info.json beside "
            "evidence/v1/artifacts/crossover_v2/<capture>/",
        )

    found = read_pose_curve_pair(
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
        # Verbatim: a bank that cannot carry a null at Fc is a finding about
        # the bank, and the module that decided it owns the sentence.
        return failed(EXIT_REFUSED, REFUSE_LANDSCAPE, str(exc))
    return landscape, take_path, _take_phase_composition(bundle_dir, take_path)


def _bank(payload: Any, out: str | None, default_path: Path) -> Path | None | int:
    """File the artifact beside the round, or the exit code that could not.

    ``--out`` names a FILE here, never ``-``: both verbs print their summary on
    stdout, so a report written there too would interleave with it.
    """

    return file_report(
        payload,
        None,
        Path(out) if out else default_path,
        reason=REFUSE_UNWRITABLE_OUT,
        make_parents=True,
    )


def _cmd_propose(args: argparse.Namespace) -> int:
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
    out = _bank(payload, args.out, Path(args.bundle_dir) / LANDSCAPE_OUT_NAME)
    if isinstance(out, int):
        return out
    print(json.dumps({**payload, "out": str(out)}, indent=2, sort_keys=True))
    print(_optimum_line(landscape), file=sys.stderr)
    return EXIT_OK


def _cmd_confirm(args: argparse.Namespace) -> int:
    computed = _landscape_from_bank(args)
    if isinstance(computed, int):
        return computed
    landscape, take_path, composition = computed

    rows_dir = Path(args.bundle_dir) / NULL_RUNS_DIR
    graded = _graded_rows(rows_dir, fc_hz=args.fc_hz)
    if not graded:
        return failed(
            EXIT_REFUSED,
            REFUSE_NO_ROWS,
            f"{rows_dir}: no measured inverted row at fc={args.fc_hz:g} Hz; "
            "play the propose coordinates with jasper-null --bundle-dir first",
        )

    depths = _depth_by_coordinate(graded)
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
    out = _bank(payload, args.out, Path(args.bundle_dir) / CONFIRMATION_OUT_NAME)
    if isinstance(out, int):
        return out
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
    print(_verdict_line(verdict, depths), file=sys.stderr)
    return EXIT_OK


def _graded_rows(rows_dir: Path, *, fc_hz: float) -> list[dict[str, Any]]:
    """The banked rows this landscape can be graded against, in path order.

    Three filters, each because the remainder is not the same quantity: a
    refused row has no depth, an in-phase row read the summed corner rather
    than the reverse null, and a row played at another corner was read at
    other shoulders. A row that will not parse is skipped rather than fatal —
    an interrupted run leaves a half-written last row, and the coordinates
    before it are still evidence.
    """

    graded: list[dict[str, Any]] = []
    for path in sorted(rows_dir.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("status") != "measured" or row.get("polarity") != "inverted":
                continue
            if not math.isclose(float(row["fc_hz"]), fc_hz, rel_tol=1e-6):
                continue
            entry = {
                "row": path.name,
                "delay_us": float(row["delay_us"]),
                "depth_db": float(row["depth_db"]),
                "delayed_role": row.get("delayed_role"),
                "inverted_role": row.get("inverted_role"),
                "position_deg": row.get("position_deg"),
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        graded.append(entry)
    return graded


def _depth_by_coordinate(graded: Sequence[Mapping[str, Any]]) -> dict[float, float]:
    """One depth per coordinate: the deepest of a repeat.

    Near the optimum the corner level is second-order flat in delay, so the
    shallow member of a repeat pair is bounded by the run's own sigma rather
    than by the coordinate. Every row stays visible in ``graded_rows``.
    """

    depths: dict[float, float] = {}
    for row in graded:
        coordinate = float(row["delay_us"])
        depth = float(row["depth_db"])
        depths[coordinate] = max(depths.get(coordinate, depth), depth)
    return depths


def _verdict_line(verdict: Mapping[str, Any], depths: Mapping[float, float]) -> str:
    """The grade, the depth at the optimum and the deepest one, in one line."""

    at_optimum = verdict["measured_null_depth_db"]
    measured = (
        "not measured there" if at_optimum is None else
        f"{at_optimum:.1f} dB "
        f"(delta {verdict['measured_minus_predicted_db']:+.1f} dB)"
    )
    deepest_us, deepest_db = max(depths.items(), key=lambda item: item[1])
    return (
        f"{verdict['verdict']}: optimum {verdict['computed_optimum_us']:g} us "
        f"predicted {verdict['predicted_null_depth_db']:.1f} dB, measured "
        f"{measured}; deepest {deepest_db:.1f} dB at {deepest_us:g} us over "
        f"{len(depths)} coordinates"
    )


def _optimum_line(landscape: DelayLandscape) -> str:
    """The answer and the basis it was read on, in one operator line.

    Beside the optimum rather than further down the payload: a coordinate read
    on clamped shoulders is weaker evidence than the same number read on the
    canonical span, and nothing else on the line says which one this is.
    """

    span = landscape.shoulders
    clamped = [
        side for side, flag in
        (("lower", span.lower_clamped), ("upper", span.upper_clamped)) if flag
    ]
    basis = (
        f"shoulders {span.used_hz[0]:g}-{span.used_hz[1]:g} Hz "
        f"({span.used_octaves:.2f} octaves), "
    )
    basis += (
        f"{'+'.join(clamped)} clamped in from canonical "
        f"{span.canonical_hz[0]:g}-{span.canonical_hz[1]:g} Hz"
        if clamped else "canonical"
    )
    return (
        f"optimum {landscape.best_coordinate_us:g} us, predicted null "
        f"{landscape.best_predicted_null_depth_db:.1f} dB — {basis}"
    )


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
        help=f"where to bank the artifact (default: <bundle_dir>/{out_name})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-delay-sweep",
        description=(
            "Propose an inter-driver delay from banked curves, then grade the "
            "acoustic confirmation against it. Computes only; plays nothing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  propose: complex-sum a banked round's per-driver curves across\n"
            "  the delay grid and print the computed optimum plus the\n"
            "  jasper-angle-capture stage lines that confirm it.\n"
            "  confirm: grade the rows jasper-null banked under\n"
            "  <bundle_dir>/null_runs/ against that same landscape.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - to actually confirm a delay acoustically -- pipe propose's\n"
            "    \"confirm_with\" lines into jasper-angle-capture stage, then\n"
            "    run jasper-null; neither verb here plays anything\n"
            "  - on a round with no per-driver curves at the requested\n"
            "    --phase and --position-deg -- refused by name\n"
            "    (REFUSE_NO_CURVES) rather than guessed\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-delay-sweep propose captures/.../session-1/round-3 \\\n"
            "      --fc-hz 1800\n"
            "  jasper-delay-sweep confirm captures/.../session-1/round-3 \\\n"
            "      --fc-hz 1800\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- proposed or graded; the artifact was written\n"
            "  1  EXIT_REFUSED -- no round at bundle_dir, no matching\n"
            "     curves, the landscape could not carry a null at Fc, or\n"
            "     (confirm) no measured null_runs row at that corner;\n"
            "     \"refused (<reason>): <detail>\" on stderr, and as the\n"
            "     JSON \"status\": \"refused\"\n"
            "  2  EXIT_UNREADABLE -- the bundle could not be read (OSError)\n"
            "  3  EXIT_WRITE_FAILED -- computed, but --out could not be written"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    child = sub.add_parser("propose")
    _add_landscape_arguments(child, out_name=LANDSCAPE_OUT_NAME)
    child.set_defaults(func=_cmd_propose)

    child = sub.add_parser("confirm")
    _add_landscape_arguments(child, out_name=CONFIRMATION_OUT_NAME)
    child.set_defaults(func=_cmd_confirm)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    try:
        return int(args.func(args))
    except NullWalkError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"unreadable bundle: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
