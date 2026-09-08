# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room, read off a round's seat-cube takes below the ceiling.

* ``room-ceiling <round-dir>`` — where the room layer stops. Writes
  ``room_ceiling.json``.
* ``room-median <round-dir>`` — the cube's median, spread and per-position
  deviation, 20 Hz to the ceiling. Writes ``room_median.json``.
* ``room-persistence <round-dir>`` — which peaks and dips hold across the
  cube, and at what fraction of positions. Writes ``room_persistence.json``.

The reading is :mod:`jasper.active_speaker.crossover_v2.room_views`'; this is
its door. The ceiling is read from the round's own applied profile (the banked
copy for a banked round, the box's for a live bundle) unless
``--applied-profile`` names another.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.crossover_v2.room_views import (
    ROOM_FLOOR_HZ,
    Ceiling,
    SeatTake,
    room_ceiling,
    room_median,
    room_persistence,
    seat_takes,
)
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
    refused_by_name,
)

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"

#: The answer's spread summary bands; the last runs to the ceiling.
SPREAD_BANDS_HZ: tuple[tuple[float, float | None], ...] = (
    (ROOM_FLOOR_HZ, 60.0), (60.0, 120.0), (120.0, None),
)

#: A feature this fraction of positions shares is what the answer counts.
PERSISTENT_FRACTION = 0.7


def _inputs(args: argparse.Namespace) -> RoundInputs:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, Path(args.round_dir))
    if args.applied_profile:
        inputs = replace(inputs, applied_profile_path=Path(args.applied_profile))
    return inputs


def _out(args: argparse.Namespace, inputs: RoundInputs) -> Path:
    return default_out(inputs, Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact)


def _cmd_room_ceiling(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    ceiling = room_ceiling(inputs.applied_profile_path)
    written = _write(ceiling.to_dict(), args.out, _out(args, inputs))
    origin = (
        f"trusted floor {ceiling.trusted_floor_hz:g} Hz clamped to "
        f"[{ceiling.to_dict()['clamp_hz'][0]:g}, {ceiling.to_dict()['clamp_hz'][1]:g}]"
        if ceiling.trusted_floor_hz is not None else ceiling.reason
    )
    return answer(
        args.command, out=written, **ceiling.to_dict(),
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
    takes = seat_takes(inputs.session_dir)
    if not takes:
        return refused_by_name(
            REFUSE_NO_SEAT_TAKES,
            {"round_dir": str(inputs.session_dir), "looked_for": "lateral takes with pose_kind=seat"},
        )
    payload = compute(takes, room_ceiling(inputs.applied_profile_path))
    return answer_of(args, payload, _write(payload, args.out, _out(args, inputs)))


def _band_means(payload: dict[str, Any]) -> dict[str, float | None]:
    freqs = np.asarray(payload["freqs_hz"])
    spread = np.asarray(payload["spread_db"])
    out: dict[str, float | None] = {}
    for lo, hi in SPREAD_BANDS_HZ:
        top = payload["ceiling_hz"] if hi is None else min(hi, payload["ceiling_hz"])
        mask = (freqs >= lo) & (freqs <= top)
        out[f"{lo:g}-{top:g}"] = float(np.mean(spread[mask])) if np.any(mask) else None
    return out


def _median_answer(args: argparse.Namespace, payload: dict[str, Any], written: Path | None) -> int:
    spread = _band_means(payload)
    return answer(
        args.command, out=written, ceiling_hz=payload["ceiling_hz"],
        ceiling_source=payload["ceiling_source"], n_positions=payload["n_positions"],
        window=payload["window"], mean_spread_db=spread,
        line=(
            f"room-median: {payload['n_positions']} position(s), ceiling "
            f"{payload['ceiling_hz']:g} Hz ({payload['ceiling_source']}), {payload['window']}; "
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
        persistent=persistent, persistent_fraction=PERSISTENT_FRACTION, top=top,
        line=(
            f"room-persistence: {persistent} of {len(features)} feature(s) at >= "
            f"{PERSISTENT_FRACTION:g} presence over {payload['n_positions']} position(s)"
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
        parser.add_argument("--out", default=None, help="write the result here (- for stdout)")
        parser.set_defaults(func=func)
