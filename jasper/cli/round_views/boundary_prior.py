# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the declared walls alone predict at the low end, before any measurement.

``boundary-prior <round-dir>`` reads the walls the operator declared
(``jasper-declare-geometry set --front-wall-m/--side-wall-m``) out of the
round's own frozen ``declared-geometry.json`` and states
:func:`~jasper.audio_measurement.measurement_geometry.boundary_prior`'s
image-source curve over a 1/12-octave grid, so a dip a round measured can be
compared against one the room's geometry already accounts for. Advisory: it
reads no capture, grades nothing, and decides nothing about a graph.

An undeclared wall is a DISCLOSED unknown, never a refusal: the view still
answers, naming what it was not told in ``unknown`` and publishing an empty
curve. Unreadable is reserved for a file that exists and does not parse --
the declared geometry, or the ``room-median`` artifact the ceiling is read
from when a round carries one.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Mapping
from pathlib import Path

from jasper.active_speaker.crossover_v2.room_views import ROOM_FLOOR_HZ
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.attribution.position_evidence import log_grid_hz
from jasper.audio_measurement.measurement_geometry import (
    WALL_FIELD_BY_KEY,
    DeclaredGeometry,
    boundary_prior,
    load_declared_geometry,
)
from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_DEFAULT_HZ
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE, read_json_source, stage
from jasper.json_fields import finite_float

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

BOUNDARY_PRIOR_KIND = "jts_boundary_prior"

MODEL = (
    "rigid image-source pressure sum per declared wall, |1 + exp(-j 2*pi*f * "
    "2d/c)|; summing the per-wall dB curves is the rigid corner image-source "
    "sum itself, up to the per-wall null-floor clamp, and approximates equal "
    "image amplitudes (no 1/r loss) and perpendicular rigid walls"
)


def _ceiling_hz(
    args: argparse.Namespace, inputs: RoundInputs, round_dir: Path,
) -> tuple[float, str]:
    """The top of the grid and where that number came from."""
    if args.ceiling_hz is not None:
        return float(args.ceiling_hz), "argument"
    path = default_out(inputs, round_dir, ARTIFACT_BY_VIEW["room-median"].artifact)
    if path.is_file():
        document = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_json_source, str(path))
        stated = (
            finite_float(document.get("ceiling_hz"))
            if isinstance(document, Mapping) else None
        )
        if stated is not None:
            return stated, "room_median"
    return ROOM_BOUNDARY_DEFAULT_HZ, "default"


def _declared_walls(
    geometry_path: Path | None, *, named: bool,
) -> tuple[dict[str, float], list[str]]:
    """The declared wall distances, and what was not declared, by field name.

    The round's own frozen copy may be absent (nothing declared); a path the
    operator ``named`` must exist.
    """
    geometry = (
        None if geometry_path is None
        else stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS,
            DeclaredGeometry.load if named else load_declared_geometry, geometry_path,
        )
    )
    if geometry is None:
        return {}, [*WALL_FIELD_BY_KEY.values(), "geometry"]
    walls, unknown = {}, []
    for key, field in WALL_FIELD_BY_KEY.items():
        metres = getattr(geometry, field)
        if metres is None:
            unknown.append(field)
        else:
            walls[key] = metres
    return walls, unknown


def _cmd_boundary_prior(args: argparse.Namespace) -> int:
    # The round is RESOLVED, never graded: this view reads a declaration that
    # travelled with the round, not one capture.
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    walls, unknown = _declared_walls(
        Path(args.geometry) if args.geometry else inputs.declared_geometry_path,
        named=bool(args.geometry),
    )
    ceiling_hz, ceiling_source = _ceiling_hz(args, inputs, round_dir)
    if not (math.isfinite(ceiling_hz) and ceiling_hz > ROOM_FLOOR_HZ):
        return refused_by_name(
            "boundary_prior_ceiling_invalid",
            {"ceiling_hz": ceiling_hz, "ceiling_source": ceiling_source,
             "grid_lo_hz": ROOM_FLOOR_HZ},
            code=EXIT_REFUSED,
        )
    prior = boundary_prior(
        log_grid_hz(ROOM_FLOOR_HZ, ceiling_hz) if walls else (), walls=walls,
    )
    written = _write(
        {
            "kind": BOUNDARY_PRIOR_KIND,
            "model": MODEL,
            "advisory": True,
            "ceiling_hz": ceiling_hz,
            "ceiling_source": ceiling_source,
            "unknown": unknown,
            **prior,
        },
        args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    declared = [
        {"wall": key, "distance_m": wall["distance_m"], "f_null_hz": wall["f_null_hz"]}
        for key, wall in prior["walls"].items()
    ]
    stated = "; ".join(
        f"{wall['wall']} {wall['distance_m']:.2f} m null {wall['f_null_hz']:.1f} Hz"
        for wall in declared
    )
    return answer(
        args.command, out=written, walls=declared,
        unknown=unknown, ceiling_hz=ceiling_hz, ceiling_source=ceiling_source,
        line=(
            f"boundary-prior: {stated or 'no wall declared'}; "
            f"unknown: {', '.join(unknown) or 'none'}; "
            f"to {ceiling_hz:.0f} Hz ({ceiling_source})"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    prior = sub.add_parser(
        "boundary-prior",
        help="what the declared front/side wall distances alone predict at the low end",
    )
    prior.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    prior.add_argument(
        "--geometry", default=None,
        help="declared geometry to read instead of the round's own frozen copy",
    )
    prior.add_argument(
        "--ceiling-hz", type=float, default=None, metavar="HZ",
        help=f"top of the grid; default is a "
             f"{ARTIFACT_BY_VIEW['room-median'].artifact} beside the round, "
             f"else {ROOM_BOUNDARY_DEFAULT_HZ:g} Hz",
    )
    prior.add_argument("--out", default=None, help="write the result here (- for stdout)")
    prior.set_defaults(func=_cmd_boundary_prior)
