# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Is a feature a driver defect, a cancellation, or the room?

* ``classify-features <bundle-dir> [--dumps <ring>]`` — classify one banked
  round's spectral features, known-answer controls first, and file
  ``feature_classification.json`` into the round's own artifact directory,
  where the evidence packet reads it. ``<bundle-dir>`` is a commissioning
  bundle; the round inside it and the ``<phase>_program.wav`` files its
  captures bind to are resolved by the rules the packet's own reader uses, so
  the verdict cannot land where that reader does not look. Offline: nothing is
  re-measured and no capture is re-taken.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.evidence_packet import (
    NO_ROUND_ARTIFACTS_REASON,
    round_artifact_dir,
    round_program_dir,
)
from jasper.active_speaker.crossover_v2.feature_classifier import (
    ADMISSIBLE_PHASES,
    DEFAULT_GATE_MS,
    FeatureClassificationRefused,
    classify_round,
    load_round_captures,
    load_round_pose_curves,
    summary_lines,
)
from jasper.active_speaker.crossover_v2.ring_projection import (
    RingProjectionRefused,
    bundle_session_id,
    project_ring,
)
from jasper.cli._refusal import (
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    StageFailed,
    stage,
)

from ._common import (
    ARTIFACT_BY_VIEW,
    PROG,
    _ROUND_TOOL_ERRORS,
    _write,
    add_rungs_ms_argument,
    answer,
    refused_by_name,
)

#: Where the ring lands when the operator named no ``--dumps``: inside the
#: bundle it is projected from and scoped to, so a second run re-links the same
#: takes rather than growing a second ring somewhere else.
_PROJECTED_RING = "ring"

#: Said on "no round artifacts at all" and on nothing else: a bundle stopped
#: for carrying more than one round has the right structure already, and
#: sending that operator to look for a second accepted shape is misleading.
_BOTH_SHAPES = (
    " — bundle_dir must hold info.json beside "
    "evidence/v1/artifacts/crossover_v2/<capture>/, either the "
    "campaign-receipts shape (program WAVs filed right there) or the shape "
    "bank-crossover-round.sh pulls (program WAVs in a sibling "
    "crossover_v2/<capture>/ directory instead)"
)


def _round_dir(bundle_dir: Path) -> Path:
    """This bundle's one round directory. A failure here is the ROUND."""
    round_dir, why = round_artifact_dir(bundle_dir)
    if round_dir is None:
        # Unreadable, not refused, for "more than one round" too: the fix is to
        # point at one round, and that is an input fix, not a named refusal.
        detail = why + (_BOTH_SHAPES if why == NO_ROUND_ARTIFACTS_REASON else "")
        raise StageFailed(
            EXIT_UNREADABLE, ValueError(f"cannot read the round: {detail}")
        )
    return round_dir


def _ring(args: argparse.Namespace) -> Path:
    """The capture ring: the operator's ``--dumps``, or one projected here.

    A round banked today carries no ring — the speaker-side dump ring is gone
    and the WAVs ride the bundle's take records — so the default is to project
    one. The ring is written as it is read, so an unreadable bundle and a
    half-written ring are one instruction: look at the filesystem, run it again.
    """
    if args.dumps is not None:
        return args.dumps
    projection = stage(
        EXIT_WRITE_FAILED, (OSError,), project_ring,
        args.bundle_dir, args.bundle_dir / _PROJECTED_RING,
        setup_calibration_id=args.setup_calibration_id,
    )
    print(
        f"projected {len(projection.projected)} take(s) from "
        f"{projection.session_id} -> {projection.dumps_dir}",
        file=sys.stderr,
    )
    for skipped in projection.skipped:
        # Reported, never dropped: a reader skips a sidecar it cannot use
        # without a word, which is what makes a half-projected ring look
        # complete to whoever reads it next.
        print(f"  skipped {skipped.path}: {skipped.reason}", file=sys.stderr)
    return projection.dumps_dir


def _classify(args: argparse.Namespace, programs_dir: Path) -> dict[str, Any]:
    """Everything the LOAD stage owns: the ring, the captures, the verdict."""
    captures = load_round_captures(
        programs_dir,
        _ring(args),
        session_id=bundle_session_id(args.bundle_dir),
        walk_logs=tuple(args.walk_logs),
    )
    # Best-effort and always attempted: a round with no lateral walk returns
    # empty rather than raising, and classify_round reports that as its own
    # NOT-RUN fact rather than needing a flag to ask for it.
    pose_curves = load_round_pose_curves(args.bundle_dir)
    return classify_round(
        captures,
        at=args.at,
        gate_ms=args.gate_ms,
        gates_ms=tuple(args.gates_ms) if args.gates_ms else None,
        pose_curves=pose_curves,
    )


def _cmd_classify_features(args: argparse.Namespace) -> int:
    round_dir = _round_dir(args.bundle_dir)
    programs_dir = round_program_dir(args.bundle_dir, round_dir, ADMISSIBLE_PHASES)
    try:
        artifact = stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _classify, args, programs_dir
        )
    except (FeatureClassificationRefused, RingProjectionRefused) as refusal:
        # The instrument's own reason, and the directory actually read: a
        # refusal that named neither starts a wrong-directory hunt.
        return refused_by_name(
            refusal.reason, {**refusal.detail, "programs_dir": str(programs_dir)}
        )

    written = _write(
        artifact, args.out, round_dir / ARTIFACT_BY_VIEW[args.command].artifact
    )
    summary = (
        f"classify-features: {len(artifact['rows'])} feature(s) from "
        f"{artifact['measurement']['n_captures']} capture(s)"
        f"{f' -> {written}' if written else ''}"
    )
    return answer(
        args.command, out=written, features=len(artifact["rows"]),
        captures=artifact["measurement"]["n_captures"],
        line="\n".join([summary, *summary_lines(artifact)]),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    classify = sub.add_parser(
        "classify-features",
        help="classify a round's features as driver defects, interference, or the room",
    )
    classify.add_argument(
        "bundle_dir", type=Path,
        help="commissioning bundle: info.json beside evidence/v1/artifacts/",
    )
    classify.add_argument(
        "--dumps", type=Path, default=None,
        help="a capture ring already projected (sidecar JSON beside its WAV); "
             f"without one the bundle is projected into <bundle-dir>/{_PROJECTED_RING} "
             "and that ring is read",
    )
    classify.add_argument(
        "--setup-calibration-id", default=None,
        help="the measurement mic this round used. The bank does not carry it, "
             "and it is stamped onto the ring projected here, where "
             f"`{PROG} distortion` reads it to choose the sign convention a "
             "--calibration file is parsed under. Ignored with --dumps",
    )
    classify.add_argument(
        "--walk-log", type=Path, action="append", default=[], dest="walk_logs",
        help="turntable walk trail, repeatable. Without one, captures carry no "
             "angle and the timing test reports that it did not run",
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
    classify.add_argument("--out", default=None, help="write the result here (- for stdout)")
    classify.set_defaults(func=_cmd_classify_features)
