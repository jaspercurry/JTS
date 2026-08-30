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

``grade``
    Read the banked confirmation takes back, key each measured null depth by
    the delay coordinate its graph actually carried, and put the pair to
    :func:`~jasper.active_speaker.crossover_v2.delay_landscape.confirmation_verdict`.
    Prints the verdict, the prescribable delay when there is one, and the
    ready-to-run alignment-prescription line. **No audio plays here either.**

The method of record is compute-then-confirm
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`). ``propose`` is its
first step and prints the ``jasper-angle-capture stage`` lines that play the
coordinates; ``grade`` is the third, after those takes are banked.

**A refusal is an output, not an error.** Banked curves that do not span both
crossover shoulders cannot support a null at Fc, and the sentence saying so is
printed verbatim from the module that decided it.

Applying a proposed delay is NOT this tool's job. The prescription door owns
that, with its own lobe gate and its own receipts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.angle_capture import REGIME_NULL_CONFIRM
from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    DRIVER_ROLES,
    POLARITY_INVERTED,
)
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_NULL_CONFIRM
from jasper.active_speaker.crossover_v2.delay_landscape import (
    DelayLandscapeError,
    compute_landscape,
    confirmation_verdict,
)
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import read_take_curves
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.audio_measurement.null_walk import NullWalkError

from ._logging import configure_verbose_logging

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INPUT = 2

REFUSE_NO_ROUND = "delay_propose_no_round"
REFUSE_NO_CURVES = "delay_propose_no_banked_curves"
REFUSE_LANDSCAPE = "delay_propose_landscape_unsupported"
REFUSE_NO_CONFIRMATIONS = "delay_grade_no_confirmation_takes"


def _spec_from_args(args: argparse.Namespace) -> Any:
    return sweep_spec(
        crossover_fc_hz=args.fc_hz,
        upper_role=args.upper_role,
        lower_role=args.lower_role,
        signed_acoustic_path_difference_m=args.path_difference_m,
        step_us=args.step_us,
    )


def _curve_pair(
    bundle_dir: Path, *, phase: str, position_deg: int, roles: tuple[str, str],
) -> tuple[Mapping[str, Any], Mapping[str, Any], str] | None:
    """The latest banked take carrying BOTH roles, and the take it came from.

    Selected through the measurement index, the shape
    :func:`~jasper.active_speaker.crossover_v2.position_cycle._banked_take_records`
    established: the rows narrow the candidates and the take file decides.

    Both roles must ride ONE take: the two transfers are summed against each
    other, so curves from two different captures would be summed across
    whatever moved between them.

    **Latest attempt wins.** A superseded take stays on disk as the honest walk
    record, and ``take_id`` is ``{position}_a{attempt:02d}`` zero-padded so the
    index's ``ORDER BY path`` is also chronological -- so the FIRST match is
    the take a retake replaced.
    """

    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    for row in reversed(bundle_measurements(
        bundle_dir, phase=phase, position_deg=position_deg
    )):
        curves = read_take_curves(artifacts / row.path, phase=phase)
        if curves is None:
            continue
        by_role = {str(curve.get("role")): curve for curve in curves}
        if roles[0] in by_role and roles[1] in by_role:
            return by_role[roles[0]], by_role[roles[1]], row.path
    return None


def _refused(reason: str, detail: str) -> int:
    print(
        json.dumps(
            {"status": "refused", "reason": reason, "detail": detail},
            indent=2, sort_keys=True,
        )
    )
    print(f"refused ({reason}): {detail}", file=sys.stderr)
    return EXIT_REFUSED


def _cmd_propose(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle_dir)
    spec = _spec_from_args(args)
    lower_role = spec.negative_delay_target
    upper_role = spec.positive_delay_target

    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        return _refused(
            REFUSE_NO_ROUND,
            f"{bundle_dir}: {why}; bundle_dir must hold info.json beside "
            "evidence/v1/artifacts/crossover_v2/<relay>/",
        )

    found = _curve_pair(
        bundle_dir,
        phase=args.phase,
        position_deg=args.position_deg,
        roles=(lower_role, upper_role),
    )
    if found is None:
        return _refused(
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
        return _refused(REFUSE_LANDSCAPE, str(exc))

    print(json.dumps({
        "status": "proposed",
        "take_path": take_path,
        "landscape": landscape.to_dict(),
        "confirm_with": [
            _stage_command(spec.dsp_candidate(coordinate), args)
            for coordinate in landscape.confirmation_coordinates_us
        ],
    }, indent=2, sort_keys=True))
    return EXIT_OK


def _confirmed_depths(
    bundle_dir: Path, spec: Any, *, position_deg: int,
) -> tuple[dict[float, float], list[dict[str, Any]]]:
    """Every banked confirmation take, keyed by the coordinate it PLAYED.

    The key is re-signed here, through ``spec``'s own frame: a take banks the
    executable ``(delayed_role, played_delay_us)`` pair its graph installed,
    because which branch counts as "positive" is the walk spec's question and a
    record that pre-signed the number would be a second opinion about it. This
    inverts ``dsp_candidate`` using the same spec that produced it.

    A take carrying no ``played_delay_us`` is skipped, not defaulted to zero: a
    record banked before the coordinate was stamped does not describe a zero
    delay, it describes an unknown one, and grading it as 0 us would put a
    measurement at a coordinate nothing played.
    """
    artifacts = Path(bundle_dir) / EVIDENCE_ROOT / "artifacts"
    depths: dict[float, float] = {}
    read: list[dict[str, Any]] = []
    for row in bundle_measurements(
        bundle_dir, kind=MEASURE_KIND_NULL_CONFIRM, position_deg=position_deg,
    ):
        try:
            record = json.loads((artifacts / row.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        depth = record.get("null_depth_db")
        played = record.get("played_delay_us")
        role = record.get("delayed_role")
        if depth is None or played is None:
            continue
        signed = float(played)
        if role and role == spec.negative_delay_target:
            signed = -signed
        depths[signed] = float(depth)
        read.append({
            "take": row.path,
            "coordinate_us": signed,
            "null_depth_db": float(depth),
            "shoulder_summed": record.get("shoulder_summed"),
            "phase_provenance": record.get("phase_provenance"),
        })
    return depths, read


def _cmd_grade(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle_dir)
    spec = _spec_from_args(args)
    lower_role = spec.negative_delay_target
    upper_role = spec.positive_delay_target

    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        return _refused(REFUSE_NO_ROUND, f"{bundle_dir}: {why}")

    found = _curve_pair(
        bundle_dir, phase=args.phase, position_deg=args.position_deg,
        roles=(lower_role, upper_role),
    )
    if found is None:
        return _refused(
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
        # Verbatim, like propose: the module that decided owns the sentence.
        return _refused(REFUSE_LANDSCAPE, str(exc))

    measured, read = _confirmed_depths(
        bundle_dir, spec, position_deg=args.position_deg,
    )
    if not measured:
        return _refused(
            REFUSE_NO_CONFIRMATIONS,
            f"{bundle_dir}: no banked {MEASURE_KIND_NULL_CONFIRM} take at "
            f"{args.position_deg} deg records both a null depth and the delay "
            "coordinate it was played at; stage the lines `propose` printed",
        )

    verdict = confirmation_verdict(landscape, measured)
    prescribable = verdict.get("prescribable_delay_us")
    print(json.dumps({
        "status": "graded",
        "take_path": take_path,
        "confirmations": sorted(read, key=lambda r: r["coordinate_us"]),
        "verdict": verdict,
        # Only a verdict the measurement supports hands out a line to run.
        "apply_with": (
            None if prescribable is None
            else _prescription_command(landscape, prescribable, args)
        ),
    }, indent=2, sort_keys=True))
    return EXIT_OK


def _prescription_command(
    landscape: Any, delay_us: float, args: argparse.Namespace,
) -> str:
    """The alignment-prescription key that APPLIES a confirmed delay.

    A session-open key rather than a prescriber verb, and its fields are the
    ones the door declares (`docs/tuning-methodology.md` §4): the sign frame is
    ``(D_woofer - D_tweeter)``, so a positive number delays the tweeter, and
    ``basis_delay_us`` is what the computation proposed before the room
    answered.
    """
    basis = landscape.best_coordinate_us
    return (
        "--alignment-prescription "
        f'\'{{"delay_us": {delay_us:g}, "basis_delay_us": {basis:g}, '
        f'"basis_artifacts": ["{args.bundle_dir}"], '
        f'"basis_note": "confirmed acoustically at {args.position_deg} deg"}}\''
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

    Every line carries ``--regime null_confirm``. That regime is what makes the
    printed line a confirmation rather than a system response: it plays the
    band-limited summed sweep THROUGH the measurement graph, so the inversion
    and the coordinate named on the same line are actually in the signal path
    when the null is read. The ordinary ``summed`` regime would restore the
    production graph first and measure a speaker with neither.

    Every line carries ``--level-matched``, including the zero coordinate.
    These are reverse-null confirmations, and a null between branches ~10 dB
    apart in sensitivity is bounded by that gap however well the coordinate is
    chosen — so a confirm line that did not ask for the level match would be
    printing a measurement whose answer the graph had already decided.
    """

    line = (
        f"jasper-angle-capture stage --angles {args.position_deg} "
        f"--regime {REGIME_NULL_CONFIRM} "
        f"--polarity {POLARITY_INVERTED} --inverted-role {args.inverted_role} "
        "--level-matched"
    )
    if candidate.delay_target is None:
        return line
    return (
        f"{line} --delayed-role {candidate.delay_target} "
        f"--delay-us {candidate.delay_us:g}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-delay-sweep",
        description=(
            "Propose an inter-driver delay from banked curves. Computes only; "
            "plays nothing."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for verb, func in (("propose", _cmd_propose), ("grade", _cmd_grade)):
        child = _add_selector_parser(sub, verb)
        child.set_defaults(func=func)
    return parser


def _add_selector_parser(sub: Any, verb: str) -> argparse.ArgumentParser:
    """The selectors both verbs share, declared once.

    ``grade`` reads the SAME round, corner and bearing ``propose`` computed
    from -- it has to, because it grades a measurement against that
    computation's landscape. Two copies of this block would let the two verbs
    describe different speakers.
    """
    child = sub.add_parser(verb)
    child.add_argument(
        "bundle_dir",
        help="a commissioning bundle directory (the one holding info.json "
             "beside evidence/v1/artifacts/crossover_v2/<relay-session-id>/)",
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
    return child


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
        return EXIT_INPUT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
