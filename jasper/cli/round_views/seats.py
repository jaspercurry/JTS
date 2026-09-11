# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Seat curves and optional related evidence, using the existing numerical owners.

Directivity is the band level difference from on-axis plus residual shape,
not sound-power DI. A shared trim leaves this difference unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.flat_spec_views import DirectivityTable

from jasper.cli._refusal import EXIT_REFUSED, EXIT_WRITE_FAILED, STATUS_BY_CODE, StageFailed, answered

from jasper.active_speaker.crossover_v2.round_views import (
    AGREEMENT_TESTIFY_MIN,
    DEFAULT_PRIMARY_ROLE,
    BankedRound,
    SeatCurve,
    VerifyPoseResult,
    agreement_table,
    audibility_co_metrics,
    default_agreement_lo_hz,
    directivity_view,
    per_seat_curves,
    verify_pose_curve,
)

from ._common import (
    ARTIFACT_BY_VIEW,
    _REASON_BY_CODE,
    _ROUND_TOOL_ERRORS,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _add_norm_band_args,
    _load_round,
    _view_out,
    _write,
    answer,
    default_out,
)

def _agreement_payload(banked: BankedRound, args: argparse.Namespace, seats: tuple[SeatCurve, ...]) -> dict[str, Any]:
    lo_hz = args.lo if args.lo is not None else default_agreement_lo_hz(banked)
    features = agreement_table(
        seats, banked.curve_grid_hz, lo_hz=lo_hz, hi_hz=args.hi,
        feature_db=args.feature_db, testify_db=args.testify_db,
    )
    return {
        "round_dir": str(banked.round_dir), "banked": banked.inputs.banked,
        "seats": [seat.position_id for seat in seats],
        "swept_band_hz": [lo_hz, args.hi],
        "feature_db": args.feature_db, "testify_db": args.testify_db,
        "features": [feature.to_dict() for feature in features],
    }

def _cmd_agreement(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi))
    payload = _agreement_payload(banked, args, seats)
    written = _write(payload, args.out, _view_out(args, banked))
    features = payload["features"]
    return answer(
        args.command, out=written, features=len(features),
        common_mode=sum(f["common_mode"] is True for f in features),
        not_evaluable=sum(f["common_mode"] is None for f in features),
        testify_min_seats=AGREEMENT_TESTIFY_MIN,
        line=f"agreement: {len(features)} feature(s)",
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

def _directivity_payload(banked: BankedRound, table: DirectivityTable) -> dict[str, Any]:
    return {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "directivity": table.to_dict(),
    }

def _cmd_directivity(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    table = directivity_view(banked)
    payload = _directivity_payload(banked, table)
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


def _per_seat_payload(
    banked: BankedRound, args: argparse.Namespace, verify: VerifyPoseResult, seats: tuple[SeatCurve, ...],
) -> dict[str, Any]:
    return {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "curve_grid_hz": banked.curve_grid_hz.tolist(),
        "norm_band_hz": [args.norm_lo, args.norm_hi],
        "verify_pose": {
            "included": verify.curve is not None,
            "reason": verify.reason,
        },
        "seats": [
            {
                "position_id": seat.position_id,
                "role": seat.role,
                "normalized_db": seat.normalized_db.tolist(),
            }
            for seat in seats
        ],
    }

def _cmd_per_seat(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    if args.include:
        return _compose_seats(args, banked)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    payload = _per_seat_payload(banked, args, verify, seats)
    written = _write(payload, args.out, _view_out(args, banked))
    return answer(
        args.command, out=written,
        seats=[seat.position_id for seat in seats],
        verify_pose_included=verify.curve is not None,
        verify_pose_reason=verify.reason,
        line=(
            f"per-seat: {len(seats)} seat(s) ({', '.join(s.position_id for s in seats)}); "
            f"verify pose {'included' if verify.curve is not None else f'ABSENT ({verify.reason})'}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _compose_seats(args: argparse.Namespace, banked: BankedRound) -> int:
    results = {}
    exit_code = 0
    prepared: tuple[VerifyPoseResult, tuple[SeatCurve, ...]]
    preparation_error = None
    view_errors = (StageFailed,) + _ROUND_TOOL_ERRORS
    try:
        verify = verify_pose_curve(banked)
        prepared = (verify, per_seat_curves(
            banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi),
        ))
    except _ROUND_TOOL_ERRORS as exc:
        preparation_error = exc
    for view in dict.fromkeys(("per-seat", *args.include)):
        path = default_out(banked.inputs, banked.round_dir, ARTIFACT_BY_VIEW[view].artifact)
        if args.out:
            base = Path(args.out)
            path = base if view == "per-seat" else base.with_name(f"{base.stem}-{path.name}")
        result: dict[str, Any] = {
            "sources": {
                "bundle": str(banked.session_dir),
                "session": banked.packet.get("session", {}),
                "packet_fingerprint": banked.packet.get("packet_fingerprint"),
                "positions": [
                    {"position_id": p.position_id, "take_id": p.take_id}
                    for p in banked.positions
                ],
            },
            "extra_sources": {}, "parameters": {}, "units": {"frequency": "Hz", "level": "dB"},
            "coverage": {}, "outcome": "available", "reason": None,
            "out": None, "bytes": None,
        }
        results[view] = result
        try:
            if view in ("per-seat", "agreement"):
                result["extra_sources"] = {
                    "verify_state": str(banked.inputs.state_path) if banked.inputs.state_path else None,
                    "verify_state_reason": banked.inputs.state_reason or None,
                }
                result["parameters"] = {"norm_band_hz": [args.norm_lo, args.norm_hi]}
                if view == "agreement":
                    result["parameters"].update(
                        swept_band_hz=[args.lo if args.lo is not None else default_agreement_lo_hz(banked), args.hi],
                        feature_db=args.feature_db, testify_db=args.testify_db,
                    )
                if preparation_error is not None:
                    raise preparation_error
                verify, seats = prepared
                result["coverage"] = {
                    "seats": [seat.position_id for seat in seats],
                    "verify_pose_included": verify.curve is not None,
                    "verify_pose_reason": verify.reason,
                }
                if verify.curve is None:
                    result.update(outcome="partial", reason=verify.reason)
                if view == "per-seat":
                    payload = _per_seat_payload(banked, args, verify, seats)
                else:
                    payload = _agreement_payload(banked, args, seats)
                    result["coverage"]["testify_min_seats"] = AGREEMENT_TESTIFY_MIN
                    result["coverage"].update(
                        features=len(payload["features"]),
                        common_mode=sum(f["common_mode"] is True for f in payload["features"]),
                        not_evaluable=sum(f["common_mode"] is None for f in payload["features"]),
                    )
                    if len(seats) < AGREEMENT_TESTIFY_MIN:
                        result.update(outcome="unavailable", reason="insufficient_agreement_seats")
            elif view == "directivity":
                result["parameters"] = {
                    "reference_role": DEFAULT_PRIMARY_ROLE,
                    "graded_band_hz": list(banked.report.graded_band_hz) if banked.report else None,
                }
                table = directivity_view(banked)
                payload = _directivity_payload(banked, table)
                result["coverage"] = {
                    "reference_position_ids": list(table.reference_position_ids),
                    "angles_recorded": table.angles_recorded if table.evaluable else None,
                    "seats": len(table.rows),
                    "not_evaluable": sum(not row.evaluable for row in table.rows),
                }
                if not table.evaluable:
                    result.update(outcome="unavailable", reason=table.not_evaluated_reason)
                elif any(not row.evaluable for row in table.rows):
                    result.update(outcome="partial", reason="unevaluable_seat_rows")
            else:
                result["extra_sources"] = {"lateral_pose_bundle": str(banked.session_dir)}
                result["parameters"] = {"band_hz": list(banked.report.graded_band_hz) if banked.report else None}
                result["units"]["sm_r2"] = "dimensionless"
                metrics = audibility_co_metrics(banked)
                payload = metrics.to_dict()
                result["summary"] = payload
                result["coverage"] = {
                    "on_axis": metrics.on_axis is not None,
                    "on_axis_reason": metrics.on_axis_reason,
                    "pooled_window_bearings_deg": list(metrics.pooled_window_bearings_deg),
                    "pooled_window_reason": metrics.pooled_window_reason,
                }
                if metrics.on_axis is None or metrics.pooled_window is None:
                    result.update(
                        outcome="unavailable" if metrics.on_axis is None and metrics.pooled_window is None else "partial",
                        reason=metrics.on_axis_reason or metrics.pooled_window_reason,
                    )
            _write(payload, str(path), path)
            result.update(out=str(path), bytes=path.stat().st_size)
        except view_errors as exc:
            code = exc.code if isinstance(exc, StageFailed) else EXIT_REFUSED
            exit_code = max(exit_code, code)
            if code != EXIT_WRITE_FAILED:
                result.pop("summary", None)
            result.update(outcome=STATUS_BY_CODE[code], reason=_REASON_BY_CODE[code], detail=str(exc))
    answered(
        {"view": "per-seat", "round_dir": str(banked.round_dir), "results": results},
        f"per-seat: {len(results)} selected result(s); {sum(r['out'] is not None for r in results.values())} detail file(s)",
    )
    return exit_code

def _add_agreement_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lo", type=float, default=None,
        help="trusted sweep low edge, Hz (default: this round's own trusted_floor_hz)",
    )
    parser.add_argument("--hi", type=float, default=16000.0, help="trusted sweep high edge, Hz")
    parser.add_argument("--feature-db", type=float, default=0.4, help="minimum |pooled dB| to count as a feature")
    parser.add_argument("--testify-db", type=float, default=0.4, help="minimum |seat dB| to testify or dissent")

def add_parser(sub: argparse._SubParsersAction) -> None:
    agreement = sub.add_parser("agreement", help="per-seat sign/magnitude testimony for every feature")
    agreement.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    _add_norm_band_args(agreement)
    _add_agreement_args(agreement)
    agreement.add_argument("--out", default=None, help="write the result here")
    agreement.set_defaults(func=_cmd_agreement)

    co_metrics = sub.add_parser(
        "co-metrics", help="NBD + SM (Olive 2004) on the on-axis and pooled-window curves — informational only",
    )
    co_metrics.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    co_metrics.add_argument("--out", default=None, help="write the result here")
    co_metrics.set_defaults(func=_cmd_co_metrics)

    directivity = sub.add_parser(
        "directivity",
        help="every cloud seat's departure from on-axis, split per band into level and shape — observed only",
    )
    directivity.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    directivity.add_argument("--out", default=None, help="write the result here")
    directivity.set_defaults(func=_cmd_directivity)

    per_seat = sub.add_parser("per-seat", help="normalised seats with optional related evidence")
    per_seat.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    _add_norm_band_args(per_seat)
    _add_agreement_args(per_seat)
    per_seat.add_argument(
        "--include", nargs="+", choices=("agreement", "directivity", "co-metrics"),
        default=[], help="selected extra views; share one round read and seat preparation",
    )
    per_seat.add_argument(
        "--out", default=None,
        help="per-seat detail path; included views use prefixed sibling filenames",
    )
    per_seat.set_defaults(func=_cmd_per_seat)
