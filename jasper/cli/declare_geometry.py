# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-declare-geometry`` -- store/print the operator-declared measurement
rig geometry that :mod:`jasper.audio_measurement.measurement_geometry` derives
an ``entanglement_floor_hz`` from. See issue #3502 for why: the measured
reflection-finder structurally never fires on this rig class.
"""
from __future__ import annotations

import argparse
import sys

from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH,
    DeclaredGeometry,
    load_declared_geometry,
)

#: 1 international inch, exactly (25.4 mm).
METERS_PER_INCH = 0.0254

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NOT_FOUND = 2
EXIT_WRITE_FAILED = 3

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). This CLI
#: only persists an operator-declared value -- it plays no audio, arms no
#: renderer, and touches no DSP config.
AUTHORITY_TIER = "advisory (`set` writes; `show` does not)"


def _add_unit_pair(parser: argparse.ArgumentParser, name: str, *, required: bool, label: str) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(f"--{name}-in", type=float, default=None, metavar="INCHES", help=f"{label}, in inches")
    group.add_argument(f"--{name}-m", type=float, default=None, metavar="METERS", help=f"{label}, in meters")


def _optional_meters(args: argparse.Namespace, dest_prefix: str) -> float | None:
    in_value = getattr(args, f"{dest_prefix}_in")
    if in_value is not None:
        return float(in_value) * METERS_PER_INCH
    m_value = getattr(args, f"{dest_prefix}_m")
    return float(m_value) if m_value is not None else None


def _required_meters(args: argparse.Namespace, dest_prefix: str) -> float:
    # The dest's mutually-exclusive argparse group is required=True, so
    # argparse itself refuses a call that leaves both units unset.
    value = _optional_meters(args, dest_prefix)
    assert value is not None
    return value


def _both_units(meters: float) -> str:
    return f"{meters:.4f} m ({meters / METERS_PER_INCH:.2f} in)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-declare-geometry",
        description=(
            "Declare measurement rig geometry (speaker/mic heights, distance, "
            "optional ceiling) so entanglement_floor_hz has a provenance-"
            "labeled, non-measured source on rigs where the measured "
            "reflection finder structurally never fires -- see issue #3502."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    set_parser = sub.add_parser("set", help="store the declared rig geometry")
    _add_unit_pair(set_parser, "speaker-height", required=True, label="speaker acoustic-center height")
    _add_unit_pair(set_parser, "mic-height", required=True, label="microphone height")
    _add_unit_pair(set_parser, "distance", required=True, label="speaker-to-mic distance")
    _add_unit_pair(set_parser, "ceiling-height", required=False, label="ceiling height (optional)")
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
            speaker_height_m=_required_meters(args, "speaker_height"),
            mic_height_m=_required_meters(args, "mic_height"),
            distance_m=_required_meters(args, "distance"),
            ceiling_height_m=_optional_meters(args, "ceiling_height"),
        )
    except ValueError as exc:
        print(f"jasper-declare-geometry: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        geometry.save(args.path)
    except OSError as exc:
        print(f"jasper-declare-geometry: could not write {args.path}: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED

    print(f"stored declared rig geometry -> {args.path}")
    print(f"  speaker height: {geometry.speaker_height_m:.4f} m")
    print(f"  mic height:     {geometry.mic_height_m:.4f} m")
    print(f"  distance:       {geometry.distance_m:.4f} m")
    if geometry.ceiling_height_m is not None:
        print(f"  ceiling height: {geometry.ceiling_height_m:.4f} m")
    print(f"  first bounce:       {geometry.first_bounce_s() * 1000:.3f} ms")
    print(f"  entanglement floor: {geometry.entanglement_floor_hz():.1f} Hz")
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
    if geometry.ceiling_height_m is not None:
        print(f"  ceiling height: {_both_units(geometry.ceiling_height_m)}")
    else:
        print("  ceiling height: not declared")
    print(f"  first bounce:       {geometry.first_bounce_s() * 1000:.3f} ms")
    print(f"  entanglement floor: {geometry.entanglement_floor_hz():.1f} Hz")
    print("  provenance: declared by the operator, not measured -- see #3502")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
