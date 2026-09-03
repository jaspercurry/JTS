# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Re-project a banked round's captures into the ring its readers open.

Offline and laptop-side: it reads a commissioning bundle and writes the
``sidecar/`` + ``wav/`` layout ``jasper-classify-features --dumps`` and
``jasper-read-distortion --dumps`` consume. No Pi is touched, nothing is
re-measured, and the bundle is only read.

``<bundle-dir>`` is the same directory those two take — ``info.json`` beside
``evidence/v1/artifacts/crossover_v2/<relay-session-id>/`` — and the round
inside it is found by the SAME rule they use, so a bundle carrying more than
one round is refused here rather than pooled into one ring.

The WAV is HARDLINKED, not copied: a round's captures run to tens of megabytes
and the bundle already holds the bytes. ``--copy`` forces a copy, and a link
that cannot be made (a ring on a different device from the bundle) falls back
to one on its own.

Program WAVs are NOT projected. Both consumers resolve them out of the bundle
through
:func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_program_dir`,
which already knows the two shapes they are written in.

**Exit codes are the contract**, matching the two consumers this feeds: ``0``
projected, ``1`` the instrument refused (the bundle is readable and holds no
take that can be projected), ``2`` the bundle could not be read, ``3`` the
ring could not be written. ``2`` and ``3`` send an operator to different
places: ``2`` means fix the round, ``3`` means fix the filesystem.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jasper.active_speaker.crossover_v2.ring_projection import (
    RingProjectionRefused,
    project_ring,
)
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    fail_with_payload,
)


#: Tool-menu authority tier (scripts/generate-tuning-tool-menu.py): it writes a
#: ring beside the bundle and changes nothing the speaker plays.
AUTHORITY_TIER = "mutating (projects evidence; changes nothing played)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-project-ring",
        description=(
            "Re-project a banked round into the capture ring that "
            "jasper-classify-features and jasper-read-distortion read."
        ),
    )
    parser.add_argument(
        "bundle_dir",
        type=Path,
        help="commissioning bundle: info.json beside evidence/v1/artifacts/",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="write the ring here; this is the path to hand --dumps",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy each capture WAV instead of hardlinking it",
    )
    parser.add_argument(
        "--setup-calibration-id",
        default=None,
        help=(
            "the measurement mic this round used. The bank does not carry it, "
            "and jasper-read-distortion reads it off the sidecar to choose the "
            "sign convention a --calibration file is parsed under."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable result on stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        projection = project_ring(
            args.bundle_dir,
            args.out,
            copy=args.copy,
            setup_calibration_id=args.setup_calibration_id,
        )
    except RingProjectionRefused as refusal:
        return fail_with_payload(
            f"refused: {refusal.reason}",
            {"ok": False, "reason": refusal.reason, "detail": refusal.detail},
            as_json=args.json,
            code=EXIT_REFUSED,
        )
    except (OSError, ValueError) as exc:
        # Both the read and the write raise OSError, and they are one exit
        # code here on purpose: the ring is written as it is read, so a
        # half-written ring and an unreadable bundle are the same instruction
        # to the operator — look at the filesystem, then run it again.
        code = EXIT_WRITE_FAILED if isinstance(exc, OSError) else EXIT_UNREADABLE
        return fail_with_payload(
            f"cannot project the round: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=code,
        )

    print(
        f"projected {len(projection.projected)} take(s) from "
        f"{projection.session_id} ({projection.capture_session_id}) "
        f"-> {projection.dumps_dir}",
        file=sys.stderr,
    )
    for take in projection.projected:
        how = "link" if take.linked else "copy"
        print(f"  {take.phase:<16} {take.stem}  [{how}]", file=sys.stderr)
    for skipped in projection.skipped:
        # Reported, never dropped: both readers skip a sidecar they cannot use
        # without a word, which is what makes a half-projected ring look
        # complete to whoever reads it next.
        print(f"  skipped {skipped.path}: {skipped.reason}", file=sys.stderr)

    if args.json:
        json.dump(
            {
                "ok": True,
                "dumps_dir": str(projection.dumps_dir),
                "session_id": projection.session_id,
                "capture_session_id": projection.capture_session_id,
                "projected": [
                    {
                        "take_id": take.take_id,
                        "phase": take.phase,
                        "stem": take.stem,
                        "linked": take.linked,
                    }
                    for take in projection.projected
                ],
                "skipped": [
                    {"path": take.path, "reason": take.reason}
                    for take in projection.skipped
                ],
            },
            sys.stdout,
            indent=1,
        )
        sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
