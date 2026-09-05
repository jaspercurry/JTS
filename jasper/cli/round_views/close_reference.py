# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How much of a far read was the room, from a close capture of the same driver.

* ``close-reference --far-round <r> --close-round <r> --close-m M`` — a banked
  close round against a banked far round: the close IR corrected by 1/r,
  delayed by the geometric excess path, sub-sample aligned to the far IR's own
  direct arrival, then gated and subtracted. Per spec band it reports the
  close-vs-far delta, the subtraction residual, and one verdict —
  ``agreement`` (the far read was speaker-dominated), ``room_dominated``, or
  ``unresolved``. Writes ``close_reference.json`` beside the FAR round — the
  read whose room share it explains — under :func:`default_out`'s rule.
* ``close-reference --distance --driver-diameter-in D --fc-hz FC`` — where to
  stand the mic for that close capture, from the driver's diameter and the
  crossover corner, with both terms of the derivation and the placement
  tolerance. It sizes a capture a human then takes: it reads no round and
  writes no artifact.

Both offline: no audio plays and no device is opened. Distances are DECLARED
here, not read from the sidecar, which pins ``mark_distance_m = 1.0`` for every
pose today (#3498); both values are published. What a room-dominated band MEANS
for a tune is the reader's judgement: nothing here decides anything about a
graph.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jasper.active_speaker.branch_chain import recommended_distance
from jasper.active_speaker.crossover_v2.close_reference import (
    compare_rounds,
    summary_lines,
)
from jasper.active_speaker.crossover_v2.round_captures import (
    REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND,
    RoundCapturesRefused,
)
from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH,
    METERS_PER_INCH,
    DeclaredGeometry,
)
from jasper.cli._refusal import EXIT_REFUSED, EXIT_UNREADABLE
from jasper.cli._unit_pair import MILLIMETRES, add_unit_pair, unit_pair_meters

from ._common import (
    ARTIFACT_BY_VIEW,
    _write,
    answer,
    refused_by_name,
    resolved_out,
)


def _refused(reason: str, detail: Mapping[str, Any]) -> int:
    """The STAGE this failure belongs to: a round nothing can read is not a
    decline, and publishing it as one sends a reader after a reason no tool
    ever named."""
    unreadable = reason == REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND
    return refused_by_name(
        reason, detail, code=EXIT_UNREADABLE if unreadable else EXIT_REFUSED
    )


def _geometry(path: str) -> DeclaredGeometry | None:
    """The declared rig geometry, or ``None`` when none has been declared.

    Absent is the ordinary case, not a refusal: the caller's own
    ``--close-gate-ms`` is then the only gate there is.
    """
    return DeclaredGeometry.load(path) if Path(path).is_file() else None


def _recommend_distance(args: argparse.Namespace, diameter_m: float) -> int:
    """Where to stand the mic. It writes no artifact, so the fields ARE the
    answer — the derivation's own record, published whole."""
    record = recommended_distance(diameter_m, args.fc_hz)
    return answer(
        args.command, **record,
        line=(
            f"stand the mic {record['distance_in']:.1f} in "
            f"({record['distance_m'] * 100:.1f} cm) from the woofer: "
            f"{record['far_field_term_m'] / METERS_PER_INCH:.2f} in far-field at "
            f"{record['band_top_hz']:.0f} Hz + "
            f"{record['margin_term_m'] / METERS_PER_INCH:.2f} in margin "
            f"({record['k_margin']:g} diameters); "
            f"+/-0.5 in costs {record['placement_tolerance_db']:.2f} dB, "
            f"+/-{record['aim_tolerance_deg']:.0f} deg of aim costs nothing; "
            f"far field holds to {record['far_field_ceiling_hz']:.0f} Hz"
        ),
    )


def _compare(args: argparse.Namespace, diameter_m: float | None) -> int:
    far_dir = Path(args.far_round)
    try:
        report = compare_rounds(
            far_dir,
            Path(args.close_round),
            far_m=args.far_m,
            close_m=args.close_m,
            far_capture_id=args.far_capture,
            close_capture_id=args.close_capture,
            fc_hz=args.fc_hz,
            driver_diameter_m=diameter_m,
            far_gate_ms=args.far_gate_ms,
            close_gate_ms=args.close_gate_ms,
            geometry=_geometry(args.geometry),
        )
    except RoundCapturesRefused as exc:
        return _refused(exc.reason, exc.detail)
    except (ValueError, OSError) as exc:
        return _refused(REFUSE_CLOSE_REFERENCE_UNREADABLE_ROUND, {"error": str(exc)})

    # Read before filed: a comparison an operator paid a capture for survives a
    # destination that could not be written.
    for line in summary_lines(report):
        print(line, file=sys.stderr)
    written = _write(
        {"status": "compared", "close_reference": report},
        args.out,
        resolved_out(far_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    alignment = report["alignment"]
    return answer(
        args.command, out=written,
        comparison_band_hz=report["validity"]["comparison_band_hz"],
        residual_lag_us=alignment["residual_lag_us"],
        alignment_confidence=alignment["confidence"],
        alignment_trusted=alignment["trusted"],
        bands=[
            {
                "window": window["name"],
                "graded_band_hz": row["graded_band_hz"],
                "verdict": row["verdict"],
                "rms_delta_db": row["rms_delta_db"],
                "residual_rel_direct_db": row["residual_rel_direct_db"],
            }
            for window in report["windows"] for row in window["bands"]
        ],
        line=f"close-reference -> {written or 'stdout'}",
    )


def _cmd_close_reference(args: argparse.Namespace) -> int:
    diameter_m = unit_pair_meters(args, "driver-diameter", metric=MILLIMETRES)
    # argparse cannot mark either mode's inputs required — the mode is a flag,
    # and each mode takes none of the other's — so the parser refuses a missing
    # one as the usage error it would otherwise have been.
    parser: argparse.ArgumentParser = args.parser
    if args.distance:
        if diameter_m is None or args.fc_hz is None:
            parser.error(
                "--distance needs --driver-diameter-in/--driver-diameter-mm "
                "and --fc-hz"
            )
        # It compares nothing and writes nothing, so a comparison's own inputs
        # named beside it are a line that would exit 0 having done neither.
        comparing = [
            flag for flag, value in (
                ("--far-round", args.far_round),
                ("--close-round", args.close_round),
                ("--close-m", args.close_m),
                ("--out", args.out),
            ) if value is not None
        ]
        if comparing:
            parser.error(f"--distance takes none of {', '.join(comparing)}")
        return _recommend_distance(args, diameter_m)
    if args.far_round is None or args.close_round is None or args.close_m is None:
        parser.error(
            "a comparison needs --far-round, --close-round and --close-m; "
            "pass --distance for the mic placement instead"
        )
    return _compare(args, diameter_m)


def add_parser(sub: argparse._SubParsersAction) -> None:
    close = sub.add_parser(
        "close-reference",
        help="correct a close capture to the far distance and say, band by band, how much of the far read was the room",
    )
    close.add_argument(
        "--distance", action="store_true",
        help="instead of comparing, print where to stand the mic for the close "
             "capture (needs a driver diameter and --fc-hz, no rounds)",
    )
    close.add_argument("--far-round", default=None)
    close.add_argument("--close-round", default=None)
    close.add_argument(
        "--far-capture", default=None,
        help="take id of the far capture; default is the on-axis summed take",
    )
    close.add_argument("--close-capture", default=None)
    close.add_argument(
        "--close-m", type=float, default=None,
        help="DECLARED close mic distance in metres — the sidecar's "
             "mark_distance_m is published beside it but not used (#3498)",
    )
    close.add_argument("--far-m", type=float, default=1.0)
    close.add_argument(
        "--fc-hz", type=float, default=None,
        help="crossover corner; caps the comparison band at fc/2, and with "
             "--distance it is what the close capture stays valid to (fc/2)",
    )
    add_unit_pair(close, "driver-diameter", required=False,
                  label="driver diameter", metric=MILLIMETRES)
    close.add_argument(
        "--far-gate-ms", type=float, default=None,
        help="override the far window; default is the declared geometry's own "
             "first bounce at --far-m, or the pipeline's reflection ceiling",
    )
    close.add_argument(
        "--close-gate-ms", type=float, default=None,
        help="override the close window. Derived from the declared heights by "
             "default and LONGER than the far one, because the first bounce's "
             "excess path grows as the direct path shrinks",
    )
    close.add_argument(
        "--geometry", default=DEFAULT_PATH,
        help=f"declared rig geometry (default: {DEFAULT_PATH}); absent means "
             "no derived window",
    )
    close.add_argument("--out", default=None, help="write the result here (- for stdout)")
    close.set_defaults(func=_cmd_close_reference, parser=close)
