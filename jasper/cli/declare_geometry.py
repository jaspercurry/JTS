# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-declare-geometry`` -- store/print the operator-declared measurement
rig geometry that :mod:`jasper.audio_measurement.measurement_geometry` derives
an ``entanglement_floor_hz`` from. See issue #3502 for why: the measured
reflection-finder structurally never fires on this rig class.

Human-only output, unlike its report-tool siblings: this is a ``set``/``show``
config door the person holding the tape measure runs under sudo, not a document
a script reads.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Sequence

from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH,
    METERS_PER_INCH,
    WALL_FIELD_BY_KEY,
    DeclaredGeometry,
    load_declared_geometry,
)

from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_WRITE_FAILED
from ._unit_pair import add_unit_pair, unit_pair_meters

#: Its own, because nothing else has one: `show` with no declaration yet is
#: not an unreadable input, it is the ordinary pre-declaration state.
EXIT_NOT_FOUND = 2

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). This CLI
#: only persists an operator-declared value -- it plays no audio, arms no
#: renderer, and touches no DSP config.
AUTHORITY_TIER = "advisory (`set` writes; `show` does not)"


def _required_meters(args: argparse.Namespace, name: str) -> float:
    # The pair's argparse group is required=True, so both-unset never reaches here.
    value = unit_pair_meters(args, name)
    assert value is not None
    return value


def _both_units(meters: float) -> str:
    return f"{meters:.4f} m ({meters / METERS_PER_INCH:.2f} in)"


#: The optional lengths, in the order both verbs print them: printed label and
#: the field it is read from. The walls come from the model's own table.
_OPTIONAL = (
    ("ceiling height", "ceiling_height_m"),
    *((f"{key} wall", field) for key, field in WALL_FIELD_BY_KEY.items()),
)


def _print_rows(rows: Sequence[tuple[str, str]]) -> None:
    width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        print(f"  {label:<{width}} {value}")


def _print_optional(
    geometry: DeclaredGeometry, *, units: Callable[[float], str], absent: str | None,
) -> None:
    """The optional lengths; an undeclared one prints ``absent`` or nothing."""
    rows = []
    for label, field in _OPTIONAL:
        metres = getattr(geometry, field)
        value = units(metres) if metres is not None else absent
        if value is not None:
            rows.append((f"{label}:", value))
    _print_rows(rows)


def _print_derived(geometry: DeclaredGeometry) -> None:
    """The two derived lines, labelled with the distance they were derived at.

    Both depend on the speaker-to-mic distance as well as the two heights, and
    a capture is evaluated at its OWN distance rather than at this one
    (``DeclaredGeometry.first_bounce_s``). Printing them bare invites a reader
    to expect these digits on every row.
    """
    at = f"at declared distance {geometry.distance_m:.4f} m; captures use their own"
    _print_rows((
        (f"first bounce ({at}):", f"{geometry.first_bounce_s() * 1000:.3f} ms"),
        (f"entanglement floor ({at}):", f"{geometry.entanglement_floor_hz():.1f} Hz"),
    ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-declare-geometry",
        description=(
            "Declare measurement rig geometry: speaker/mic heights, distance "
            "and optional ceiling, so entanglement_floor_hz has a provenance-"
            "labeled, non-measured source on rigs where the measured "
            "reflection finder structurally never fires (issue #3502); and "
            "optional front/side wall distances, which only the "
            "jasper-round-views boundary-prior model reads."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    set_parser = sub.add_parser("set", help="store the declared rig geometry")
    add_unit_pair(set_parser, "speaker-height", required=True, label="speaker acoustic-center height")
    add_unit_pair(set_parser, "mic-height", required=True, label="microphone height")
    add_unit_pair(set_parser, "distance", required=True, label="speaker-to-mic distance")
    add_unit_pair(set_parser, "ceiling-height", required=False, label="ceiling height (optional)")
    add_unit_pair(set_parser, "front-wall", required=False,
                  label="speaker baffle to the wall behind it (optional)")
    add_unit_pair(set_parser, "side-wall", required=False,
                  label="speaker to the nearest side wall (optional)")
    set_parser.add_argument(
        "--path", default=DEFAULT_PATH,
        help=f"override the stored file location (default: {DEFAULT_PATH})",
    )
    set_parser.set_defaults(func=_cmd_set)

    show_parser = sub.add_parser("show", help="print the stored declared rig geometry")
    show_parser.add_argument(
        "--path", default=DEFAULT_PATH,
        help=f"override the stored file location (default: {DEFAULT_PATH})",
    )
    show_parser.set_defaults(func=_cmd_show)
    return parser


def _cmd_set(args: argparse.Namespace) -> int:
    try:
        geometry = DeclaredGeometry(
            speaker_height_m=_required_meters(args, "speaker-height"),
            mic_height_m=_required_meters(args, "mic-height"),
            distance_m=_required_meters(args, "distance"),
            ceiling_height_m=unit_pair_meters(args, "ceiling-height"),
            front_wall_m=unit_pair_meters(args, "front-wall"),
            side_wall_m=unit_pair_meters(args, "side-wall"),
        )
    except ValueError as exc:
        print(f"jasper-declare-geometry: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        geometry.save(args.path)
    except OSError as exc:
        # /var/lib/jasper is a 0770 StateDirectory owned by the daemon's user,
        # so the login account cannot write it without sudo.
        hint = " — run with sudo" if isinstance(exc, PermissionError) else ""
        print(
            f"jasper-declare-geometry: could not write {args.path}: {exc}{hint}",
            file=sys.stderr,
        )
        return EXIT_WRITE_FAILED

    print(f"stored declared rig geometry -> {args.path}")
    print(f"  speaker height: {geometry.speaker_height_m:.4f} m")
    print(f"  mic height:     {geometry.mic_height_m:.4f} m")
    print(f"  distance:       {geometry.distance_m:.4f} m")
    _print_optional(geometry, units=lambda metres: f"{metres:.4f} m", absent=None)
    _print_derived(geometry)
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        geometry = load_declared_geometry(args.path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"jasper-declare-geometry: could not read {args.path}: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if geometry is None:
        print(f"jasper-declare-geometry: no declared geometry at {args.path}", file=sys.stderr)
        return EXIT_NOT_FOUND

    print(f"declared rig geometry <- {args.path}")
    print(f"  speaker height: {_both_units(geometry.speaker_height_m)}")
    print(f"  mic height:     {_both_units(geometry.mic_height_m)}")
    print(f"  distance:       {_both_units(geometry.distance_m)}")
    _print_optional(geometry, units=_both_units, absent="not declared")
    _print_derived(geometry)
    print("  provenance: declared by the operator, not measured -- see #3502")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
