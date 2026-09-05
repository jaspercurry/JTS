# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The seat verbs: what each position testifies about a feature, the
audibility co-metrics, and how the cloud departs from on-axis.

* ``agreement <round-dir>`` — per-position sign/magnitude testimony for
  every feature in the trusted sweep, built from the same per-seat curves
  ``per-seat`` computes. Writes ``agreement.json``.
* ``co-metrics <round-dir>`` — NBD + SM (Olive 2004, ADR-0202) on the
  on-axis curve and the pooled horizontal window. Co-metrics only: they
  inform, they never gate or veto — ``entry``/``frozen``/``per-seat`` etc.
  stay the acceptance path. Writes ``audibility_co_metrics.json``.
* ``directivity <round-dir>`` — every cloud seat as its departure from the
  on-axis reference, split per graded band into a level offset (that band's
  directivity index, which a trim can remove) and the shape residual it
  cannot. Observed only: no grade moves. Writes ``directivity.json``. A round
  banked before the seat bearings were written still answers, with
  ``angles_recorded`` false — read it as role-labelled, never as 0°.
"""

from __future__ import annotations

import argparse

from jasper.active_speaker.crossover_v2.round_views import (
    AGREEMENT_TESTIFY_MIN,
    agreement_table,
    audibility_co_metrics,
    default_agreement_lo_hz,
    directivity_view,
    per_seat_curves,
    verify_pose_curve,
)

from ._common import (
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _add_norm_band_args,
    _load_round,
    _view_out,
    _write,
    answer,
)

def _cmd_agreement(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    lo_hz = args.lo if args.lo is not None else default_agreement_lo_hz(banked)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    features = agreement_table(
        seats,
        banked.curve_grid_hz,
        lo_hz=lo_hz,
        hi_hz=args.hi,
        feature_db=args.feature_db,
        testify_db=args.testify_db,
    )
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "seats": [seat.position_id for seat in seats],
        "swept_band_hz": [lo_hz, args.hi],
        "feature_db": args.feature_db,
        "testify_db": args.testify_db,
        "features": [feature.to_dict() for feature in features],
    }
    written = _write(payload, args.out, _view_out(args, banked))
    # `common_mode is True`, never a bare truthiness test: `None` (not
    # evaluable, below AGREEMENT_TESTIFY_MIN seats) must not be silently
    # counted alongside `False` (evaluated and failed the bar).
    n_common = sum(1 for f in features if f.common_mode is True)
    n_not_evaluable = sum(1 for f in features if f.common_mode is None)
    return answer(
        args.command, out=written, features=len(features), common_mode=n_common,
        not_evaluable=n_not_evaluable, testify_min_seats=AGREEMENT_TESTIFY_MIN,
        line=(
            f"agreement: {len(features)} feature(s), {n_common} common-mode, "
            f"{n_not_evaluable} not-evaluable (< {AGREEMENT_TESTIFY_MIN} seats)"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _cmd_co_metrics(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    result = audibility_co_metrics(banked)
    written = _write(result.to_dict(), args.out, _view_out(args, banked))
    on_axis = (
        f"NBD={result.on_axis.nbd_db:.3f} dB SM={result.on_axis.sm_r2:.3f}"
        if result.on_axis is not None else f"NOT AVAILABLE ({result.on_axis_reason})"
    )
    pooled = (
        f"NBD={result.pooled_window.nbd_db:.3f} dB SM={result.pooled_window.sm_r2:.3f} "
        f"({len(result.pooled_window_bearings_deg)} bearing(s))"
        if result.pooled_window is not None
        else f"NOT AVAILABLE ({result.pooled_window_reason})"
    )
    return answer(
        args.command, out=written,
        on_axis_nbd_db=None if result.on_axis is None else result.on_axis.nbd_db,
        on_axis_sm_r2=None if result.on_axis is None else result.on_axis.sm_r2,
        on_axis_reason=result.on_axis_reason,
        pooled_nbd_db=(
            None if result.pooled_window is None else result.pooled_window.nbd_db
        ),
        pooled_sm_r2=(
            None if result.pooled_window is None else result.pooled_window.sm_r2
        ),
        pooled_bearings_deg=list(result.pooled_window_bearings_deg),
        pooled_reason=result.pooled_window_reason,
        line=(
            f"co-metrics [informational only, never a grade input]: "
            f"on-axis {on_axis}; pooled-window {pooled}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _cmd_directivity(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    table = directivity_view(banked)
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "directivity": table.to_dict(),
    }
    written = _write(payload, args.out, _view_out(args, banked))
    # Both clauses — and the two answer fields below — only inside the
    # evaluable arm: an absent reference forces `angles_recorded` false and
    # the reference empty whatever the round banked, so reading either out
    # there would tell a caller their bearings are missing when they are not.
    if table.evaluable:
        n_not_evaluable = sum(1 for row in table.rows if not row.evaluable)
        summary = (
            f"{len(table.rows)} seat(s), {n_not_evaluable} not-evaluable, against "
            f"{len(table.reference_position_ids)} {table.reference_role} seat(s); "
            + (
                "angles recorded" if table.angles_recorded
                else "angles NOT recorded (role-labelled only)"
            )
        )
    else:
        summary = f"NOT AVAILABLE ({table.not_evaluated_reason})"
    return answer(
        args.command, out=written, evaluable=table.evaluable,
        seats=len(table.rows),
        not_evaluable=sum(1 for row in table.rows if not row.evaluable),
        reference_seats=(
            len(table.reference_position_ids) if table.evaluable else None
        ),
        reference_role=table.reference_role,
        angles_recorded=table.angles_recorded if table.evaluable else None,
        not_evaluated_reason=table.not_evaluated_reason,
        line=(
            f"directivity [observed only, no grade moves]: {summary}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    agreement = sub.add_parser("agreement", help="per-seat sign/magnitude testimony for every feature")
    agreement.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    _add_norm_band_args(agreement)
    agreement.add_argument(
        "--lo", type=float, default=None,
        help="trusted sweep low edge, Hz (default: this round's own trusted_floor_hz)",
    )
    agreement.add_argument("--hi", type=float, default=16000.0, help="trusted sweep high edge, Hz")
    agreement.add_argument("--feature-db", type=float, default=0.4, help="minimum |pooled dB| to count as a feature")
    agreement.add_argument("--testify-db", type=float, default=0.4, help="minimum |seat dB| to testify or dissent")
    agreement.add_argument("--out", default=None, help="write the result here (- for stdout)")
    agreement.set_defaults(func=_cmd_agreement)

    co_metrics = sub.add_parser(
        "co-metrics", help="NBD + SM (Olive 2004) on the on-axis and pooled-window curves — informational only",
    )
    co_metrics.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    co_metrics.add_argument("--out", default=None, help="write the result here (- for stdout)")
    co_metrics.set_defaults(func=_cmd_co_metrics)

    directivity = sub.add_parser(
        "directivity",
        help="every cloud seat's departure from on-axis, split per band into level and shape — observed only",
    )
    directivity.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    directivity.add_argument("--out", default=None, help="write the result here (- for stdout)")
    directivity.set_defaults(func=_cmd_directivity)
