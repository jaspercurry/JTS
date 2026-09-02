# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the close reference: how much of the far read was the room.

Two verbs, both offline. **No audio plays and no device is opened.**

``distance``
    Where to put the mic for a close capture of this driver, from its diameter
    and the crossover corner, with both terms of the derivation and the
    placement tolerance printed.

``compare``
    A banked close round against a banked far round: the close IR corrected by
    1/r, delayed by the geometric excess path, sub-sample aligned to the far
    IR's own direct arrival, then gated and subtracted. Per spec band it prints
    the close-vs-far delta, the subtraction residual, and one verdict —
    ``agreement`` (the far read was speaker-dominated), ``room_dominated``, or
    ``unresolved``.

**A refusal is an output, not an error.** A round with no on-axis summed
capture, or a capture whose declared stimulus hash matches no banked program,
prints the refusal that names the missing input and exits 1. Exit 2 is a round
this cannot read at all.

Distances are DECLARED here, not read from the sidecar: today's sidecars pin
``mark_distance_m = 1.0`` for every pose (#3498). Both values are published.

Each window's gate comes from ``--close-gate-ms``/``--far-gate-ms`` when given,
else from the declared rig geometry's own first bounce at that distance
(``--geometry``, ``jasper-declare-geometry``'s file), else from the pipeline's
reflection-search ceiling. The report says which, per window.

Nothing here decides anything about a graph. A close reference is an
instrument; what a room-dominated band MEANS for a tune is the reader's
judgement.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.branch_chain import recommended_distance
from jasper.active_speaker.crossover_v2.close_reference import (
    REFUSE_UNREADABLE_ROUND,
    compare_rounds,
)
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused
from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH,
    METERS_PER_INCH,
    DeclaredGeometry,
)

from ._logging import configure_verbose_logging
from ._refusal import refused
from ._report import render_report
from ._unit_pair import MILLIMETRES, add_unit_pair, unit_pair_meters

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INPUT = 2

#: `_cmd_distance` owns this refusal: the required argparse group refuses an
#: ordinary call naming neither unit, but a hand-built Namespace (or `-O`
#: stripping an assert) must not be able to feed `None` past it.
REFUSE_NO_DRIVER_DIAMETER = "close_reference_no_driver_diameter"

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory (plays nothing)"


def _refused(reason: str, detail: Mapping[str, Any]) -> int:
    return refused(
        reason,
        json.dumps(detail, sort_keys=True, default=str),
        exit_code=EXIT_INPUT if reason == REFUSE_UNREADABLE_ROUND else EXIT_REFUSED,
    )


def _cmd_distance(args: argparse.Namespace) -> int:
    # Catches what the required argparse group cannot: a hand-built Namespace.
    diameter = unit_pair_meters(args, "driver-diameter", metric=MILLIMETRES)
    if diameter is None:
        return _refused(
            REFUSE_NO_DRIVER_DIAMETER,
            {"missing": "--driver-diameter-in or --driver-diameter-mm"},
        )
    record = recommended_distance(diameter, args.fc_hz)
    print(json.dumps({"status": "recommended", "distance": record},
                     indent=2, sort_keys=True))
    print(
        f"stand the mic {record['distance_in']:.1f} in "
        f"({record['distance_m'] * 100:.1f} cm) from the woofer: "
        f"{record['far_field_term_m'] / METERS_PER_INCH:.2f} in far-field at "
        f"{record['band_top_hz']:.0f} Hz + "
        f"{record['margin_term_m'] / METERS_PER_INCH:.2f} in margin "
        f"({record['k_margin']:g} diameters); "
        f"+/-0.5 in costs {record['placement_tolerance_db']:.2f} dB, "
        f"+/-{record['aim_tolerance_deg']:.0f} deg of aim costs nothing; "
        f"far field holds to {record['far_field_ceiling_hz']:.0f} Hz",
        file=sys.stderr,
    )
    return EXIT_OK


def _geometry(path: str) -> DeclaredGeometry | None:
    """The declared rig geometry, or ``None`` when none has been declared.

    Absent is the ordinary case, not a refusal: the caller's own
    ``--close-gate-ms`` is then the only gate there is.
    """
    return DeclaredGeometry.load(path) if Path(path).is_file() else None


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        report = compare_rounds(
            Path(args.far_round),
            Path(args.close_round),
            far_m=args.far_m,
            close_m=args.close_m,
            far_capture_id=args.far_capture,
            close_capture_id=args.close_capture,
            fc_hz=args.fc_hz,
            driver_diameter_m=unit_pair_meters(
                args, "driver-diameter", metric=MILLIMETRES
            ),
            far_gate_ms=args.far_gate_ms,
            close_gate_ms=args.close_gate_ms,
            geometry=_geometry(args.geometry),
        )
    except RoundCapturesRefused as exc:
        return _refused(exc.reason, exc.detail)
    except (ValueError, OSError) as exc:
        return _refused(REFUSE_UNREADABLE_ROUND, {"error": str(exc)})

    # Echoed always, filed only on --out: the shared serialization, not
    # `write_report`'s out-or-default.
    text = render_report({"status": "compared", "close_reference": report})
    if args.out:
        atomic_write_text(args.out, text + "\n")
    print(text)

    alignment = report["alignment"]
    band = report["validity"]["comparison_band_hz"]
    print(
        f"aligned to {alignment['residual_lag_us']:.2f} us residual "
        f"(measured {alignment['measured_shift_us']:.1f} us vs geometric "
        f"{alignment['geometric_delay_us']:.1f} us, confidence "
        f"{alignment['confidence']:.2f}); comparison band "
        f"{band[0]:.0f}-{band[1]:.0f} Hz",
        file=sys.stderr,
    )
    for window in report["windows"]:
        declared = window["declared_clean_window_ms"]
        print(
            f"  {window['name']} gated at {window['gate_ms']:.2f} ms "
            f"({window['gate_source']}); declared clean window "
            + ("undeclared" if declared is None else f"{declared:.2f} ms"),
            file=sys.stderr,
        )
        for row in window["bands"]:
            graded = row["graded_band_hz"]
            span = (
                f"{graded[0]:.0f}-{graded[1]:.0f} Hz" if graded else "not graded"
            )
            rms = row["rms_delta_db"]
            residual = row["residual_rel_direct_db"]
            print(
                f"  {window['name']} {span}: {row['verdict']}"
                + (f" ({row['unresolved_reason']})" if row["unresolved_reason"] else "")
                + (
                    ""
                    if rms is None or residual is None
                    else f", close-vs-far RMS {rms:.2f} dB "
                         f"(tolerance {row['tolerance_db']:.1f}), residual "
                         f"{residual:.1f} dB"
                ),
                file=sys.stderr,
            )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-close-reference",
        description=(
            "Correct a close capture to the far distance and say, band by "
            "band, how much of the far read was the room. Computes only; "
            "plays nothing and opens no device."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    distance = sub.add_parser(
        "distance", help="where to put the mic for this driver's close capture"
    )
    add_unit_pair(distance, "driver-diameter", required=True,
                  label="driver diameter", metric=MILLIMETRES)
    distance.add_argument(
        "--fc-hz", type=float, required=True,
        help="the crossover corner; the close capture is valid to fc/2",
    )
    distance.set_defaults(func=_cmd_distance)

    compare = sub.add_parser(
        "compare", help="a banked close round against a banked far round"
    )
    compare.add_argument("--far-round", required=True)
    compare.add_argument("--close-round", required=True)
    compare.add_argument(
        "--far-capture", default=None,
        help="take id of the far capture; default is the on-axis summed take",
    )
    compare.add_argument("--close-capture", default=None)
    compare.add_argument(
        "--close-m", type=float, required=True,
        help="DECLARED close mic distance in metres — the sidecar's "
             "mark_distance_m is published beside it but not used (#3498)",
    )
    compare.add_argument("--far-m", type=float, default=1.0)
    compare.add_argument(
        "--fc-hz", type=float, default=None,
        help="crossover corner; caps the comparison band at fc/2",
    )
    add_unit_pair(compare, "driver-diameter", required=False,
                  label="driver diameter", metric=MILLIMETRES)
    compare.add_argument(
        "--far-gate-ms", type=float, default=None,
        help="override the far window; default is the declared geometry's own "
             "first bounce at --far-m, or the pipeline's reflection ceiling",
    )
    compare.add_argument(
        "--close-gate-ms", type=float, default=None,
        help="override the close window. Derived from the declared heights by "
             "default and LONGER than the far one, because the first bounce's "
             "excess path grows as the direct path shrinks",
    )
    compare.add_argument(
        "--geometry", default=DEFAULT_PATH,
        help=f"declared rig geometry (default: {DEFAULT_PATH}); absent means "
             "no derived window",
    )
    compare.add_argument("--out", default=None, help="also write the report here")
    compare.set_defaults(func=_cmd_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
