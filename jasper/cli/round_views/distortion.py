# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""H2/H3 out of a banked round's MEASURE captures.

* ``distortion <bundle-dir>`` — H2/H3
  out of a banked round's MEASURE captures, relative to the fundamental, at
  the drive each capture used. ``<bundle-dir>`` is a commissioning bundle,
  and ``harmonic_distortion.json`` lands beside its round, never inside its
  evidence.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    DEFAULT_BANDS_HZ,
    DEFAULT_FULL_RANGE_BAND_HZ,
    read_bundle_harmonics,
)
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _BUNDLE_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    resolved_out,
    round_inputs,
    subject,
)

def _cmd_distortion(args: argparse.Namespace) -> int:
    artifact = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_bundle_harmonics,
        args.bundle_dir,
        {
            "woofer": args.woofer_band,
            "tweeter": args.tweeter_band,
            "full_range": args.full_range_band,
        },
        calibration_path=args.calibration,
    )
    captures = artifact["captures"]
    read = subject(round_inputs(args.bundle_dir), take_ids=[take["take_id"] for take in captures["read"]])
    # Only the swept roles, each over the band it was read: a candidate-branch
    # capture reads its own recorded program's band, not the flag's.
    band_hz: dict[str, list[list[float]]] = {}
    for block in artifact["roles"]:
        read_bands = band_hz.setdefault(block["role"], [])
        if block["sweep"]["read_band_hz"] not in read_bands:
            read_bands.append(block["sweep"]["read_band_hz"])
    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(artifact, args.out, resolved_out(args.bundle_dir, spec.artifact), schema=spec.schema)
    return answer(
        args.command, schema=spec.schema, subject=read,
        parameters={"band_hz": band_hz,
                    "setup_calibration_id": artifact["calibration"].get("setup_calibration_id") or None},
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


def _band(text: str) -> tuple[float, float]:
    """``"150:4000"`` as a band. Raises ``argparse``'s own error type."""
    try:
        lo, hi = (float(part) for part in str(text).split(":", 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected LO:HI in Hz, got {text!r}") from None
    if not 0.0 < lo < hi:
        raise argparse.ArgumentTypeError(f"band must satisfy 0 < lo < hi, got {text!r}")
    return lo, hi


def add_parser(sub: argparse._SubParsersAction) -> None:
    distortion = sub.add_parser(
        "distortion",
        help="read H2/H3 out of a banked round's MEASURE captures, at the drive each used",
    )
    distortion.add_argument(
        "bundle_dir", type=Path, metavar=_BUNDLE_DIR_METAVAR,
        help="banked round or its commissioning bundle",
    )
    distortion.add_argument(
        "--woofer-band", type=_band, default=DEFAULT_BANDS_HZ["woofer"],
        metavar="LO:HI", help="woofer sweep band in Hz (default %(default)s)",
    )
    distortion.add_argument(
        "--tweeter-band", type=_band, default=DEFAULT_BANDS_HZ["tweeter"],
        metavar="LO:HI", help="tweeter sweep band in Hz (default %(default)s)",
    )
    distortion.add_argument(
        "--full-range-band", type=_band, default=DEFAULT_FULL_RANGE_BAND_HZ,
        metavar="LO:HI",
        help="1-way (passive full-range main) sweep band in Hz, used only "
             "when the round banked one full-range role (default %(default)s)",
    )
    distortion.add_argument(
        "--calibration", type=Path, default=None,
        help="microphone calibration file, applied at each curve's OWN "
             "acoustic frequency; without one the ratios carry the mic's response",
    )
    distortion.add_argument("--out", default=None, help="write the result here")
    distortion.set_defaults(func=_cmd_distortion)
