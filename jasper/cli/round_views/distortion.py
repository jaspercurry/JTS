# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""H2/H3 out of a banked round's MEASURE captures.

* ``distortion <bundle-dir> --dumps <ring> --state <flow-state>`` — H2/H3
  out of a banked round's MEASURE captures, relative to the fundamental, at
  the drive each capture used. ``<bundle-dir>`` is a commissioning bundle,
  and its round is resolved by the rule the evidence packet's reader uses, so
  ``harmonic_distortion.json`` cannot land where that reader does not look.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    DEFAULT_BANDS_HZ,
    DEFAULT_FULL_RANGE_BAND_HZ,
    read_bundle_harmonics,
)
from jasper.cli._refusal import EXIT_OK, EXIT_UNREADABLE, stage

from ._common import ARTIFACT_BY_VIEW, _ROUND_TOOL_ERRORS, _write

def _cmd_distortion(args: argparse.Namespace) -> int:
    round_dir, artifact = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, read_bundle_harmonics,
        args.bundle_dir, args.dumps, args.state,
        {
            "woofer": args.woofer_band,
            "tweeter": args.tweeter_band,
            "full_range": args.full_range_band,
        },
        calibration_path=args.calibration,
        applied_profile_path=args.applied_profile,
    )
    # The bundle's own round directory, never `default_out`: this reading is
    # filed where the packet reader looks for it, and that is a banked tree.
    written = _write(
        artifact, args.out, round_dir / ARTIFACT_BY_VIEW[args.command].artifact
    )
    captures = artifact["captures"]
    print(
        f"distortion: H{'/H'.join(str(order) for order in artifact['orders'])} "
        f"for {len(artifact['roles'])} (capture, role) block(s) from "
        f"{captures['n_read']} capture(s), {captures['n_refused']} refused"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


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
        "bundle_dir", type=Path,
        help="commissioning bundle: info.json beside evidence/v1/artifacts/",
    )
    distortion.add_argument(
        "--dumps", type=Path, required=True,
        help="banked capture ring (sidecar JSON beside its WAV)",
    )
    distortion.add_argument(
        "--state", type=Path, required=True,
        help=(
            "THIS round's flow state; its gain_plan_db and candidate.program_id "
            "rebuild the MEASURE program and prove it. That proof is "
            "program-vs-STATE only: a state from a DIFFERENT round than "
            "bundle_dir reads the drive wrong with no refusal"
        ),
    )
    distortion.add_argument(
        "--applied-profile", type=Path, default=None,
        help=(
            "the applied baseline profile JSON, where this round's crossover "
            "corner is read from — never the flow state's record of a previous "
            "apply. Absent or unreadable, the round is refused, not read"
        ),
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
    distortion.add_argument("--out", default=None, help="write the result here (- for stdout)")
    distortion.set_defaults(func=_cmd_distortion)
