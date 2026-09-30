# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""H2/H3 out of the readings a round's MEASURE takes banked.

* ``distortion <bundle-dir>`` — H2/H3
  out of the readings a round's MEASURE and branch takes banked, relative
  to the fundamental, at the drive each take used. ``<bundle-dir>`` is a
  commissioning bundle, and ``harmonic_distortion.json`` lands beside its
  round, never inside its evidence.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.harmonic_evidence import read_round_harmonics
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _BUNDLE_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    calibration_id,
    resolved_out,
    round_inputs,
    subject,
)

def _read(session_dir: Path) -> tuple[dict[str, Any], dict[str, list[list[float]]]]:
    """The view, and each swept role's bands as its takes read them."""
    artifact = read_round_harmonics(session_dir)
    band_hz: dict[str, list[list[float]]] = {}
    for block in artifact["roles"]:
        read_bands = band_hz.setdefault(block["role"], [])
        if block["sweep"]["read_band_hz"] not in read_bands:
            read_bands.append(block["sweep"]["read_band_hz"])
    return artifact, band_hz


def _cmd_distortion(args: argparse.Namespace) -> int:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, args.bundle_dir)
    artifact, band_hz = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _read, inputs.session_dir)
    captures = artifact["captures"]
    read = subject(inputs, take_ids=[take["take_id"] for take in captures["read"]])
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(artifact, args.out, resolved_out(args.bundle_dir, spec.artifact), schema=spec.schema)
    return answer(
        args.command, schema=spec.schema, subject=read,
        parameters={"band_hz": band_hz, "calibration_id": calibration_id(artifact["calibration"])},
        out=written, orders=artifact["orders"],
        blocks=len(artifact["roles"]), captures_read=captures["n_read"],
        captures_refused=captures["n_refused"],
        line=(
            f"distortion: H{'/H'.join(str(order) for order in artifact['orders'])} "
            f"for {len(artifact['roles'])} (capture, role) block(s) from "
            f"{captures['n_read']} capture(s), {captures['n_refused']} refused"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    distortion = sub.add_parser(
        "distortion",
        help="read the H2/H3 a round's MEASURE and branch takes banked, at the drive each used",
    )
    distortion.add_argument(
        "bundle_dir", type=Path, metavar=_BUNDLE_DIR_METAVAR,
        help="banked round or its commissioning bundle",
    )
    distortion.add_argument("--out", default=None, help="write the result here")
    distortion.set_defaults(func=_cmd_distortion)
