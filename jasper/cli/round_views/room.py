# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room views over the selected manifest set, below the round's ceiling."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.crossover_v2.room_views import (
    Ceiling,
    SeatTake,
    band_masks,
    room_ceiling,
    room_median,
    room_persistence,
)
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    refused_by_name, resolve_set,
)

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"

#: A feature this fraction of positions shares is what the answer counts.
PERSISTENT_FRACTION = 0.7


def _inputs(args: argparse.Namespace) -> RoundInputs:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, Path(args.round_dir))
    if args.applied_profile:
        inputs = replace(inputs, applied_profile_path=Path(args.applied_profile))
    return inputs


def _out(args: argparse.Namespace, inputs: RoundInputs) -> Path:
    return default_out(inputs, Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact, args.set)


def _cmd_room_ceiling(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    resolve_set(inputs, args.set)
    ceiling = room_ceiling(inputs.applied_profile_path)
    doc = ceiling.to_dict()
    written = _write(doc, args.out, _out(args, inputs))
    lo, hi = doc["clamp_hz"]
    origin = (
        f"trusted floor {ceiling.trusted_floor_hz:g} Hz clamped to [{lo:g}, {hi:g}]"
        if ceiling.trusted_floor_hz is not None else ceiling.reason
    )
    return answer(
        args.command, out=written, **doc,
        line=(
            f"room-ceiling: {ceiling.ceiling_hz:g} Hz ({ceiling.source}: {origin})"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _seat_view(
    args: argparse.Namespace,
    *,
    compute: Callable[[tuple[SeatTake, ...], Ceiling], dict[str, Any]],
    answer_of: Callable[[argparse.Namespace, dict[str, Any], Path | None], int],
) -> int:
    """One view over the round's seat takes below its ceiling, written and answered."""
    inputs = _inputs(args)
    selected = resolve_set(inputs, args.set)
    try:
        selection = select_seat_takes(inputs.session_dir, take_ids=selected.selected_ids,
                                      basis=selected.capture_basis)
    except RoundCapturesRefused as exc:
        return refused_by_name(exc.reason, exc.detail)
    takes = selection.takes
    if not takes:
        return refused_by_name(
            REFUSE_NO_SEAT_TAKES,
            {"round_dir": str(inputs.session_dir), "looked_for": "lateral takes with pose_kind=seat",
             "evidence": selection.evidence},
        )
    payload = compute(takes, room_ceiling(inputs.applied_profile_path))
    payload["evidence"] = dict(selection.evidence)
    return answer_of(args, payload, _write(payload, args.out, _out(args, inputs)))


def _band_means(payload: dict[str, Any]) -> dict[str, float | None]:
    spread = None if payload["spread_db"] is None else np.asarray(payload["spread_db"])
    return {
        f"{lo:g}-{hi:g}": float(np.mean(spread[mask])) if spread is not None and np.any(mask) else None
        for lo, hi, mask in band_masks(payload["freqs_hz"], payload["ceiling_hz"])
    }


def _support_line(payload: dict[str, Any]) -> str:
    status = "sufficient" if payload["spatial_support"]["sufficient"] else "insufficient"
    return f"spatial support {status}"


def _median_answer(args: argparse.Namespace, payload: dict[str, Any], written: Path | None) -> int:
    spread = _band_means(payload)
    return answer(
        args.command, out=written, ceiling_hz=payload["ceiling_hz"],
        ceiling_source=payload["ceiling_source"], n_positions=payload["n_positions"],
        window=payload["window"], mean_spread_db=spread,
        spatial_support=payload["spatial_support"],
        evidence=payload["evidence"], coverage_hz=payload["coverage_hz"],
        line=(
            f"room-median: {payload['n_positions']} position(s), ceiling "
            f"{payload['ceiling_hz']:g} Hz ({payload['ceiling_source']}), {payload['window']}; "
            f"{_support_line(payload)}; "
            "mean spread "
            + ", ".join(
                f"{band} Hz {value:.1f} dB" if value is not None else f"{band} Hz n/a"
                for band, value in spread.items()
            )
            + (f" -> {written}" if written else "")
        ),
    )


def _persistence_answer(
    args: argparse.Namespace, payload: dict[str, Any], written: Path | None,
) -> int:
    features = payload["features"]
    persistent = sum(1 for f in features if f["presence_fraction"] >= PERSISTENT_FRACTION)
    top = [
        {k: f[k] for k in ("kind", "centre_hz", "median_depth_db", "presence_fraction")}
        for f in sorted(features, key=lambda f: -abs(f["median_depth_db"]))[:3]
    ]
    return answer(
        args.command, out=written, ceiling_hz=payload["ceiling_hz"],
        n_positions=payload["n_positions"], features=len(features),
        spatial_support=payload["spatial_support"],
        persistent=persistent, persistent_fraction=PERSISTENT_FRACTION, top=top,
        evidence=payload["evidence"], coverage_hz=payload["coverage_hz"],
        line=(
            f"room-persistence: {persistent} of {len(features)} feature(s) at >= "
            f"{PERSISTENT_FRACTION:g} presence over {payload['n_positions']} position(s)"
            f"; {_support_line(payload)}"
            + (
                "; top: " + ", ".join(
                    f"{f['kind']} {f['centre_hz']:.0f} Hz {f['median_depth_db']:+.1f} dB "
                    f"({f['presence_fraction']:.2f})"
                    for f in top
                )
                if top else ""
            )
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name, func, help_ in (
        ("room-ceiling", _cmd_room_ceiling,
         "where the room layer stops: the applied candidate's trusted floor, clamped, or the disclosed default"),
        ("room-median", partial(_seat_view, compute=room_median, answer_of=_median_answer),
         "the seat cube's median, spread and per-position deviation below the ceiling"),
        ("room-persistence", partial(_seat_view, compute=room_persistence, answer_of=_persistence_answer),
         "which peaks and dips hold across the seat cube, and at what fraction of positions"),
    ):
        parser = sub.add_parser(name, help=help_)
        parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
        parser.add_argument(
            "--applied-profile", default=None, metavar="PATH",
            help="read the ceiling from this applied profile instead of the round's own",
        )
        parser.add_argument("--set", help="set in the run manifest")
        parser.add_argument("--out", default=None, help="write the result here")
        parser.set_defaults(func=func)
