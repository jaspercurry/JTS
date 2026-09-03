# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read one banked round's H2/H3 distortion, and file the reading.

Offline, and the sibling of ``jasper-classify-features`` in every respect: it
reads captures a round already banked, writes
``harmonic_distortion.json`` into that round's own artifact directory — where
the evidence packet looks for it — and touches no Pi, re-measures nothing, and
re-takes no capture.

``<bundle-dir>`` is a commissioning bundle: the directory holding ``info.json``
beside ``evidence/v1/artifacts/crossover_v2/<capture-session-id>/``. The round
directory inside it is found by the SAME rule the packet reader uses
(:func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_artifact_dir`),
so the artifact cannot land where the reader does not look, and a bundle
carrying more than one round exits ``2`` rather than being guessed at.

``--dumps`` is the banked capture ring, which lives outside the bundle, and
``--state`` is the round's flow state — required, because the MEASURE program is
rebuilt from its ``gain_plan_db`` and proved against its ``candidate.program_id``
before any capture is read. ``--applied-profile`` is the round's own banked
applied-baseline-profile SSOT, and is where the crossover corner is read from —
never from ``--state``, whose record of a previous apply can be stale by one
apply or arbitrarily many. ``--woofer-band`` / ``--tweeter-band``
supply the driver bands the rebuild needs; they default to the shipped MEASURE
bands and a wrong pair simply fails the program-id proof rather than producing a
wrong reading.

**Exit codes are the contract**, because the caller is often a script: ``0``
read and filed, ``1`` the instrument refused (the program did not reproduce, no
MEASURE capture was banked, or every capture failed a fidelity gate), ``2`` the
round could not be read, ``3`` the reading could not be written. ``2`` and ``3``
are separate because they send an operator to different places: ``2`` means fix
the round, ``3`` means fix the filesystem. A refusal is the instrument working —
``--json`` prints its named ``reason`` and the evidence behind it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text

from jasper.active_speaker.crossover_v2.evidence_packet import (
    HARMONICS_ARTIFACT,
    NO_ROUND_ARTIFACTS_REASON,
    round_artifact_dir,
)
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    fail_with_payload,
)
from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    HARMONIC_ORDERS,
    STATE_UNREADABLE,
    HarmonicEvidenceRefused,
    banked_roles,
    read_round_harmonics,
)

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory"

#: The shipped MEASURE driver bands for a PAIR, as the flow composes them today
#: (:data:`DEFAULT_FULL_RANGE_BAND_HZ` is the 1-way sibling).
#:
#: Defaults rather than constants: a round measured with different bands is read
#: by passing them, and a wrong pair cannot produce a wrong reading — it fails
#: the ``program_id`` proof, which is the whole point of proving the rebuild
#: instead of asserting it.
DEFAULT_BANDS_HZ: dict[str, tuple[float, float]] = {
    "woofer": (150.0, 4000.0),
    "tweeter": (1600.0, 20000.0),
}

#: The 1-way default: the whole measurable span in Hz, since a passive main's
#: own declared band is not derivable here.
DEFAULT_FULL_RANGE_BAND_HZ: tuple[float, float] = (150.0, 20000.0)


def _band(text: str) -> tuple[float, float]:
    """``"150:4000"`` as a band. Raises ``argparse``'s own error type."""
    try:
        lo, hi = (float(part) for part in str(text).split(":", 1))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected LO:HI in Hz, got {text!r}"
        ) from None
    if not 0.0 < lo < hi:
        raise argparse.ArgumentTypeError(f"band must satisfy 0 < lo < hi, got {text!r}")
    return lo, hi


def round_bands_hz(
    state: Mapping[str, Any], overrides: Mapping[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    """The band each role THIS round swept, keyed by role.

    Roles come from :func:`~...harmonic_evidence.banked_roles`, the reader
    ``rebuild_measure_program`` composes against, so the CLI cannot hand it a
    shape it will only refuse. Refuses rather than dropping a role it cannot
    place, which would compose a program silently missing a sweep.
    """
    roles = banked_roles(state)
    if not roles or not overrides.keys() >= set(roles):
        gains = state.get("gain_plan_db")
        raise HarmonicEvidenceRefused(
            STATE_UNREADABLE,
            {
                "missing": "a band for every role this round's gain plan names",
                "gain_plan_roles": sorted(gains) if isinstance(gains, Mapping) else [],
                "bands_offered": sorted(overrides),
            },
        )
    return {role: overrides[role] for role in roles}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-read-distortion",
        description=(
            "Read H2/H3 out of a banked round's MEASURE captures, relative to "
            "the fundamental, at the drive each capture used."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - with a --state from a DIFFERENT round than bundle_dir --\n"
            "    the drive level comes out wrong with NO refusal (see\n"
            "    --state's own help); always pass the state belonging to\n"
            "    THIS round\n"
            "  - without a readable --applied-profile -- the round is\n"
            "    refused rather than read, because that is where the\n"
            "    crossover corner is read from\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-read-distortion captures/.../session-1/round-3 \\\n"
            "      --dumps captures/.../round-3/dumps.json \\\n"
            "      --state captures/.../round-3/flow_state.json\n"
            "\n"
            "EXIT CODES\n"
            "  0  read; the reading is filed and a summary printed\n"
            "  1  EXIT_REFUSED -- the reading itself was refused (e.g. no\n"
            "     --applied-profile, or it named a corner this round did\n"
            "     not measure through); \"refused: <reason>\" on stderr,\n"
            "     and as JSON with --json\n"
            "  2  EXIT_UNREADABLE -- bundle_dir, info.json or --state\n"
            "     could not be read, or the bundle carries more than one\n"
            "     round and this tool will not guess which\n"
            "  3  EXIT_WRITE_FAILED -- read, but the reading could not be\n"
            "     written"
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
        "--state",
        type=Path,
        required=True,
        help=(
            "the round's flow state; its gain_plan_db and candidate.program_id "
            "are what the MEASURE program is rebuilt from and proved against. "
            "That proof is program-vs-STATE only: pass a state from a DIFFERENT "
            "round than <bundle-dir> and the drive comes out wrong with no "
            "refusal, so pass the one belonging to this round"
        ),
    )
    parser.add_argument(
        "--applied-profile",
        type=Path,
        default=None,
        help=(
            "the applied baseline profile JSON — this speaker's record of what "
            "it is PLAYING, and where the round's crossover corner is read "
            "from. NOT the flow state's record of a previous apply, which "
            "can be one apply behind or arbitrarily behind the graph the "
            "round actually measured through. Without it (or if it cannot be "
            "read) the round is refused rather than read"
        ),
    )
    parser.add_argument(
        "--woofer-band",
        type=_band,
        default=DEFAULT_BANDS_HZ["woofer"],
        metavar="LO:HI",
        help="woofer sweep band in Hz (default %(default)s)",
    )
    parser.add_argument(
        "--tweeter-band",
        type=_band,
        default=DEFAULT_BANDS_HZ["tweeter"],
        metavar="LO:HI",
        help="tweeter sweep band in Hz (default %(default)s)",
    )
    parser.add_argument(
        "--full-range-band",
        type=_band,
        default=DEFAULT_FULL_RANGE_BAND_HZ,
        metavar="LO:HI",
        help=(
            "1-way (passive full-range main) sweep band in Hz; used only when "
            "the round banked one full-range role (default %(default)s)"
        ),
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=None,
        help=(
            "microphone calibration file. Applied at each curve's OWN acoustic "
            "frequency; without one the ratios carry the mic's response"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            f"write here instead of <round-dir>/{HARMONICS_ARTIFACT}. The "
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
        message = f"cannot read the round: {why}"
        if why == NO_ROUND_ARTIFACTS_REASON:
            message += (
                " — bundle_dir must hold info.json beside "
                "evidence/v1/artifacts/crossover_v2/<capture>/"
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
        state = json.loads(args.state.read_text())
        # The file's CONTENTS, not a parsed curve: the sign convention it must
        # be read under comes from the microphone this round's own captures
        # recorded through, which only the instrument can see.
        calibration_text = (
            args.calibration.read_text() if args.calibration is not None else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        return fail_with_payload(
            f"cannot read the round: {exc}",
            {"ok": False, "error": str(exc)},
            as_json=args.json,
            code=EXIT_UNREADABLE,
        )
    if isinstance(info, dict) and isinstance(info.get("session_id"), str):
        session_id = info["session_id"]
    if not isinstance(state, dict):
        return fail_with_payload(
            f"the flow state at {args.state} is not a JSON object",
            {"ok": False, "error": "state is not a JSON object"},
            as_json=args.json,
            code=EXIT_UNREADABLE,
        )

    try:
        artifact = read_round_harmonics(
            round_dir,
            args.dumps,
            state,
            round_bands_hz(state, {
                "woofer": args.woofer_band,
                "tweeter": args.tweeter_band,
                "full_range": args.full_range_band,
            }),
            session_id=session_id,
            orders=HARMONIC_ORDERS,
            calibration_text=calibration_text,
            applied_profile_path=args.applied_profile,
        )
    except HarmonicEvidenceRefused as refusal:
        return fail_with_payload(
            f"refused: {refusal.reason}",
            {"ok": False, "reason": refusal.reason, "detail": refusal.evidence},
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

    destination = args.out or (round_dir / HARMONICS_ARTIFACT)
    try:
        # Atomic: this file is durable evidence the packet reads, and a torn
        # write would be read as a reading rather than as a broken file.
        atomic_write_text(destination, json.dumps(artifact, indent=1))
    except OSError as exc:
        return fail_with_payload(
            f"read, but could not write {destination}: {exc}",
            {"ok": False, "error": str(exc), "path": str(destination)},
            as_json=args.json,
            code=EXIT_WRITE_FAILED,
        )

    roles = artifact["roles"]
    captures = artifact["captures"]
    print(
        f"read H{'/H'.join(str(order) for order in artifact['orders'])} for "
        f"{len(roles)} (capture, role) block(s) from {captures['n_read']} "
        f"capture(s), {captures['n_refused']} refused -> {destination}",
        file=sys.stderr,
    )
    if args.json:
        json.dump(
            {"ok": True, "path": str(destination), **artifact}, sys.stdout, indent=1
        )
        sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
