# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Is a feature a driver defect, a cancellation, or the room?

* ``classify-features <bundle-dir>`` — classify one banked
  round's spectral features, known-answer controls first, and file
  ``feature_classification.json`` beside the round, never inside its
  evidence. ``<bundle-dir>`` is a commissioning bundle; each of its kept
  speaker takes is read through the impulse its analysis kept, and no
  recording is opened (ADR-0392). Offline: nothing is re-measured and no
  capture is re-taken. The answer names ``classifiable_band_hz`` — the span
  a verdict can be about the SPEAKER rather than about the band edge — on
  success as well as in the refusal.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.feature_classifier import (
    DEFAULT_GATE_MS,
    classify_round,
    load_kept_captures,
    load_round_pose_curves,
    summary_lines,
)
from jasper.active_speaker.crossover_v2.round_inputs import banked_round_of, round_inputs
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _BUNDLE_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    add_rungs_ms_argument,
    answer,
    resolved_out,
    subject,
)


def _classify(args: argparse.Namespace) -> dict[str, Any]:
    """Everything the LOAD stage owns: the kept takes, the pose curves, the verdict."""
    return classify_round(
        load_kept_captures(args.bundle_dir),
        at=args.at,
        gate_ms=args.gate_ms,
        gates_ms=tuple(args.gates_ms) if args.gates_ms else None,
        # Best-effort and always attempted: a round with no lateral walk returns
        # empty rather than raising, and classify_round reports that as its own
        # NOT-RUN fact rather than needing a flag to ask for it.
        pose_curves=load_round_pose_curves(args.bundle_dir),
    )


def _cmd_classify_features(args: argparse.Namespace) -> int:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs,
                   banked_round_of(args.bundle_dir) or args.bundle_dir)
    args.bundle_dir = inputs.session_dir
    artifact = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _classify, args)

    spec = ARTIFACT_BY_VIEW[args.command]
    written = _write(artifact, args.out, resolved_out(args.bundle_dir, spec.artifact), schema=spec.schema)
    measurement = artifact["measurement"]
    # The floor the refusal already carries, published on SUCCESS too: what
    # this instrument can be asked about is knowable before a run rather than
    # only after one declines (see ``feature_classifier.classifiable_band_hz``).
    band_hz = measurement["classifiable_band_hz"]
    summary = (
        f"classify-features: {len(artifact['rows'])} feature(s) from "
        f"{measurement['n_captures']} capture(s), classifiable over "
        f"{band_hz[0]:.0f}-{band_hz[1]:.0f} Hz"
        f"{f' -> {written}' if written else ''}"
    )
    return answer(
        args.command, schema=spec.schema, subject=subject(inputs),
        parameters={"window_ms": measurement["gate_ms_primary"], "rungs_ms": measurement["gate_ladder_ms"],
                    "smoothing_fraction": measurement["magnitude_smooth_fraction"], "at_hz": args.at},
        out=written, features=len(artifact["rows"]),
        captures=measurement["n_captures"], classifiable_band_hz=band_hz,
        line="\n".join([summary, *summary_lines(artifact)]),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    classify = sub.add_parser(
        "classify-features",
        help="classify a round's features as driver defects, interference, or the room",
    )
    classify.add_argument(
        "bundle_dir", type=Path, metavar=_BUNDLE_DIR_METAVAR,
        help="banked round or its commissioning bundle",
    )
    classify.add_argument(
        "--at", type=float, action="append", default=None, metavar="HZ",
        help="classify this frequency, repeatable. Omitted, features are "
             "detected from the round's own pooled response",
    )
    classify.add_argument(
        "--gate-ms", type=float, default=DEFAULT_GATE_MS,
        help=f"primary analysis window (default {DEFAULT_GATE_MS:g})",
    )
    add_rungs_ms_argument(classify, flag="--gates-ms", repeatable=True)
    classify.add_argument("--out", default=None, help="write the result here")
    classify.set_defaults(func=_cmd_classify_features)
