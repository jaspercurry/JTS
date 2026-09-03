# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Classify one banked round's spectral features, and file the verdict.

Offline: it reads captures a round already banked and writes
``feature_classification.json`` into that round's own artifact directory, where
the evidence packet looks for it. No Pi is touched, nothing is re-measured, and
no capture is re-taken.

``<bundle-dir>`` is a commissioning bundle — the directory holding ``info.json``
beside ``evidence/v1/artifacts/crossover_v2/<capture-session-id>/``. The round
directory inside it is found by the SAME rule the packet reader uses
(:func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_artifact_dir`),
so the artifact cannot land where the reader does not look, and a bundle
carrying more than one round exits ``2`` rather than being guessed at.

That round directory's ``<phase>_program.wav`` files are then read from
either of two places, resolved by
:func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_program_dir`
— the SAME rule :func:`~jasper.active_speaker.crossover_v2.round_views._find_program_wav`
shares, so the two readers cannot answer "where do the programs live"
differently. Beside the JSON receipts themselves is tried first; only when
neither admissible phase is there does resolution fall back to a SIBLING
``crossover_v2/<capture>/`` directory next to, not inside, ``evidence/`` — the
shape ``scripts/bank-crossover-round.sh`` actually produces, because it tars
a live Pi session bundle verbatim and that is where the product's own sole
program-WAV writer (``correction_crossover_v2.py``) has always filed them.

``--dumps`` is the banked capture ring, which lives outside the bundle. The
ring is scoped to this round by the bundle's own ``session_id``: a sidecar
stamps that id into ``jts_session_identity``, so a ring holding several rounds
needs no flag to be split correctly.

**Exit codes are the contract**, because the caller is often a script: ``0``
classified and filed, ``1`` the instrument refused (the captures are the wrong
shape, or nothing stood above the round's own scatter), ``2`` the round could
not be read, ``3`` the verdict could not be written. A round whose
known-answer controls failed exits ``0`` with an artifact: it costs the phase
class, not the round, so every row reads ``egd=ambiguous`` and the summary
carries the artifact's own ``controls_disclosure`` line.
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

from jasper.active_speaker.crossover_v2.evidence_packet import (
    CLASSIFICATION_ARTIFACT,
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
)
from jasper.cli._report import write_report
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    fail_with_payload,
)
from jasper.cli.gate_sweep import add_rungs_ms_argument

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-classify-features",
        description=(
            "Classify a banked round's features as minimum-phase driver "
            "defects, interference, or the room — controls first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - before a round has banked captures -- this reads\n"
            "    already-banked evidence, it takes no measurement itself\n"
            "  - to classify one specific dip you already suspect -- --at\n"
            "    repeats to name frequencies explicitly; omitted, features\n"
            "    are DETECTED from the round's own pooled response, which\n"
            "    may not surface a small one\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-classify-features captures/.../session-1/round-3 \\\n"
            "      --dumps captures/.../round-3/dumps.json\n"
            "\n"
            "EXIT CODES\n"
            "  0  classified; the verdict is filed and a summary printed\n"
            "  1  EXIT_REFUSED -- classification itself was refused (e.g.\n"
            "     no captures to classify); \"refused: <reason> (...)\" on\n"
            "     stderr, and as JSON with --json\n"
            "  2  EXIT_UNREADABLE -- bundle_dir, info.json, or the\n"
            "     round shape itself could not be read -- the message names\n"
            "     which, and for the commonest cause (no admissible round\n"
            "     shape) names the two directory shapes this tool accepts\n"
            "  3  EXIT_WRITE_FAILED -- classified, but the verdict could\n"
            "     not be written to --out or the default artifact path"
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
    add_rungs_ms_argument(parser, flag="--gates-ms", repeatable=True)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    round_dir, why = round_artifact_dir(args.bundle_dir)
    if round_dir is None:
        # 2, not 1, for "bundle carries more than one round" too: the fix is
        # to point at one round, and that is an input fix, not a named refusal.
        message = f"cannot read the round: {why}"
        if why == NO_ROUND_ARTIFACTS_REASON:
            # Only on THIS reason: a bundle stopped for carrying more than
            # one round has the right structure already, and telling that
            # operator to check for a second accepted shape is misleading —
            # the fix there is naming which round, not where programs live.
            message += (
                " — bundle_dir must hold info.json beside "
                "evidence/v1/artifacts/crossover_v2/<capture>/, either the "
                "campaign-receipts shape (program WAVs filed right there) or "
                "the shape bank-crossover-round.sh pulls (program WAVs in a "
                "sibling crossover_v2/<capture>/ directory instead)"
            )
        return fail_with_payload(
            message,
            {"ok": False, "error": why},
            as_json=args.json,
            code=EXIT_UNREADABLE,
        )

    session_id: str | None = None
    try:
        info = json.loads((args.bundle_dir / "info.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return fail_with_payload(
            f"cannot read the bundle's info.json: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=EXIT_UNREADABLE,
        )
    if isinstance(info, dict) and isinstance(info.get("session_id"), str):
        session_id = info["session_id"]

    try:
        programs_dir = round_program_dir(args.bundle_dir, round_dir, ADMISSIBLE_PHASES)
        captures = load_round_captures(
            programs_dir,
            args.dumps,
            session_id=session_id,
            walk_logs=tuple(args.walk_logs),
        )
        # Best-effort and always attempted: a round with no lateral walk
        # returns empty rather than raising, and classify_round reports that
        # as its own NOT-RUN fact rather than needing a flag to ask for it.
        pose_curves = load_round_pose_curves(args.bundle_dir)
        artifact = classify_round(
            captures,
            at=args.at,
            gate_ms=args.gate_ms,
            gates_ms=tuple(args.gates_ms) if args.gates_ms else None,
            pose_curves=pose_curves,
        )
    except FeatureClassificationRefused as refusal:
        # The directory actually read, named explicitly: a refusal whose
        # own detail can be misread as describing a directory with zero
        # WAVs (PROGRAM_MISSING's `programs_present` is scoped to whichever
        # directory this resolved to, not to `round_dir` by name) must not
        # start the very wrong-directory hunt this instrument exists to end.
        try:
            programs_dir_display = programs_dir.relative_to(args.bundle_dir)
        except ValueError:  # pragma: no cover - defensive, see round_program_dir
            programs_dir_display = programs_dir
        return fail_with_payload(
            f"refused: {refusal.reason} (programs read from {programs_dir_display})",
            {
                "ok": False,
                "reason": refusal.reason,
                "detail": refusal.detail,
                "programs_dir": str(programs_dir_display),
            },
            as_json=args.json,
            code=EXIT_REFUSED,
        )
    except (OSError, ValueError) as exc:
        return fail_with_payload(
            f"cannot read the round: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=EXIT_UNREADABLE,
        )

    destination = args.out or (round_dir / CLASSIFICATION_ARTIFACT)
    try:
        write_report(artifact, None, destination, make_parents=True)
    except OSError as exc:
        return fail_with_payload(
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
    # An exit-0 round whose controls failed must not read as a clean one.
    if artifact["controls_disclosure"] is not None:
        print(f"  controls: {artifact['controls_disclosure']}", file=sys.stderr)
    for row in rows:
        print(
            f"  {row['hz']:8.0f} Hz  {row['classification']:<34} "
            f"{row['confidence']:>6}  egd={row['egd_verdict']:<14} "
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
