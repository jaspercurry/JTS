# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Classify one banked round's spectral features, and file the verdict.

Offline: it reads captures a round already banked and writes
``feature_classification.json`` into that round's own artifact directory, where
the evidence packet looks for it. No Pi is touched, nothing is re-measured, and
no capture is re-taken.

``<bundle-dir>`` is a commissioning bundle — the directory holding ``info.json``
beside ``evidence/v1/artifacts/crossover_v2/<relay-session-id>/``. The round
directory inside it is found by the SAME rule the packet reader uses
(:func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_artifact_dir`),
so the artifact cannot land where the reader does not look, and a bundle
carrying more than one round is refused rather than guessed at.

``--dumps`` is the banked capture ring, which lives outside the bundle. The
ring is scoped to this round by the bundle's own ``session_id``: a sidecar
stamps that id into ``jts_session_identity``, so a ring holding several rounds
needs no flag to be split correctly.

**Exit codes are the contract**, because the caller is often a script: ``0``
classified and filed, ``1`` the round could not be read, ``2`` the instrument
refused (its controls failed, the captures are the wrong shape, or nothing
stood above the round's own scatter), ``3`` the verdict could not be written.
``2`` and ``3`` are separate because they send an operator to different places:
``2`` means fix the round, ``3`` means fix the filesystem. A refusal is the
instrument working — ``--json`` prints its named ``reason`` and the evidence
behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_text

from jasper.active_speaker.crossover_v2.evidence_packet import (
    CLASSIFICATION_ARTIFACT,
    round_artifact_dir,
)
from jasper.active_speaker.crossover_v2.feature_classifier import (
    DEFAULT_GATE_MS,
    FeatureClassificationRefused,
    classify_round,
    load_round_captures,
)

EXIT_OK = 0
EXIT_ROUND_UNREADABLE = 1
EXIT_REFUSED = 2
EXIT_WRITE_FAILED = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-classify-features",
        description=(
            "Classify a banked round's features as minimum-phase driver "
            "defects, interference, or the room — controls first."
        ),
    )
    parser.add_argument(
        "bundle_dir",
        type=Path,
        help="commissioning bundle: info.json beside evidence/v1/artifacts/",
    )
    parser.add_argument(
        "--dumps",
        type=Path,
        required=True,
        help="banked capture ring (sidecar JSON beside its WAV)",
    )
    parser.add_argument(
        "--walk-log",
        type=Path,
        action="append",
        default=[],
        dest="walk_logs",
        help=(
            "turntable walk trail, repeatable. Without one, captures carry no "
            "angle and the timing test reports that it did not run."
        ),
    )
    parser.add_argument(
        "--at",
        type=float,
        action="append",
        default=None,
        metavar="HZ",
        help=(
            "classify this frequency, repeatable. Omitted, features are "
            "detected from the round's own pooled response."
        ),
    )
    parser.add_argument(
        "--gate-ms",
        type=float,
        default=DEFAULT_GATE_MS,
        help=f"primary analysis window (default {DEFAULT_GATE_MS:g})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            f"write here instead of <round-dir>/{CLASSIFICATION_ARTIFACT}. The "
            "default is the only path the packet reader looks at."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable result on stdout"
    )
    return parser


def _fail(message: str, payload: dict[str, Any], *, as_json: bool, code: int) -> int:
    print(message, file=sys.stderr)
    if as_json:
        json.dump(payload, sys.stdout, indent=1)
        sys.stdout.write("\n")
    return code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    round_dir, why = round_artifact_dir(args.bundle_dir)
    if round_dir is None:
        return _fail(
            f"cannot read the round: {why}",
            {"ok": False, "error": why},
            as_json=args.json,
            code=EXIT_ROUND_UNREADABLE,
        )

    session_id: str | None = None
    try:
        info = json.loads((args.bundle_dir / "info.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(
            f"cannot read the bundle's info.json: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=EXIT_ROUND_UNREADABLE,
        )
    if isinstance(info, dict) and isinstance(info.get("session_id"), str):
        session_id = info["session_id"]

    try:
        captures = load_round_captures(
            round_dir,
            args.dumps,
            session_id=session_id,
            walk_logs=tuple(args.walk_logs),
        )
        artifact = classify_round(captures, at=args.at, gate_ms=args.gate_ms)
    except FeatureClassificationRefused as refusal:
        return _fail(
            f"refused: {refusal.reason}",
            {"ok": False, "reason": refusal.reason, "detail": refusal.detail},
            as_json=args.json,
            code=EXIT_REFUSED,
        )
    except (OSError, ValueError) as exc:
        return _fail(
            f"cannot read the round: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=EXIT_ROUND_UNREADABLE,
        )

    destination = args.out or (round_dir / CLASSIFICATION_ARTIFACT)
    try:
        # Atomic: this file is durable evidence a later round reads, and a
        # torn write would be read as a verdict rather than as a broken file.
        atomic_write_text(destination, json.dumps(artifact, indent=1))
    except OSError as exc:
        return _fail(
            f"classified, but could not write {destination}: {exc}",
            {"ok": False, "error": str(exc), "path": str(destination)},
            as_json=args.json,
            code=EXIT_WRITE_FAILED,
        )

    rows = artifact["rows"]
    print(
        f"classified {len(rows)} feature(s) from {len(captures)} capture(s) "
        f"-> {destination}",
        file=sys.stderr,
    )
    for row in rows:
        print(
            f"  {row['hz']:8.0f} Hz  {row['classification']:<34} "
            f"{row['confidence']:>4}  egd={row['egd_verdict']:<14} "
            f"gate={row['gate_verdict']:<7} depth={row['depth_db']:.2f} dB",
            file=sys.stderr,
        )
    if args.json:
        json.dump(
            {"ok": True, "path": str(destination), "rows": rows},
            sys.stdout,
            indent=1,
        )
        sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
