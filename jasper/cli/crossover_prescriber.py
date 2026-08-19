# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Serve one round's evidence, take a prescription back, and stage it.

The three parts of the prescriber loop, and deliberately nothing between them:
``packet`` emits the evidence document, the operator hands it to whatever
reader they are talking to, ``propose`` reads the answer back through the
strict gate, and ``stage`` puts an accepted one where the next round will find
it. **Who calls the model is not this tool's business** — there is no
model client, no API key, no spend cap and no network here, which is what keeps
the harness usable with a human doing the reasoning, with a laptop agent over
SSH, or with a paste into a browser.

``propose`` and ``stage`` run the SAME gate on the same document; the only
difference is that ``stage`` banks the result. That is deliberate — a staging
verb with a laxer check would be the second, weaker reader this design exists to
avoid — so ``propose`` is the dry run of ``stage`` rather than a different
question, and an operator who wants to see the answer before committing to it
runs the first and then the second.

Conventions mirror :mod:`jasper.cli.correction_bundle` and the workbench plan's
§5.0 CLI note: ``argparse`` subcommands, a per-subcommand ``--json``,
``main() -> int``, non-zero exit on failure, and ``-`` for stdin.

**Exit codes are part of the contract**, because the caller of this tool is
often a script: ``0`` accepted, ``1`` the evidence could not be read, ``2`` the
prescription was refused, ``3`` an accepted prescription could not be staged. A
refusal is not a crash — it is the loop working — so it prints the
machine-readable reason on stdout as JSON when asked, and the human sentence on
stderr either way. ``3`` is its own code rather than folded into ``1`` because
the two send an operator to different places: ``2`` means fix the prescription,
``3`` means fix the speaker's filesystem, and a script that could not tell them
apart would retry the wrong one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.blend_prescription import (
    PRESCRIPTION_MALFORMED,
    BlendPrescription,
    BlendPrescriptionRefused,
    blend_prescription_to_candidate_fields,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
    packet_positional_evidence,
    packet_region_band_hz,
)
from jasper.active_speaker.crossover_v2.prescription_spool import (
    stage_prescription,
)

EXIT_OK = 0
EXIT_EVIDENCE_UNREADABLE = 1
EXIT_REFUSED = 2
EXIT_STAGE_FAILED = 3


def _load_packet(args: argparse.Namespace) -> dict[str, Any]:
    return build_crossover_evidence_packet(
        Path(args.session_dir),
        state_path=Path(args.state) if args.state else None,
    )


def _cmd_packet(args: argparse.Namespace) -> int:
    """Emit one round's evidence packet."""
    try:
        packet = _load_packet(args)
    except CrossoverEvidencePacketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EVIDENCE_UNREADABLE
    blob = json.dumps(packet, indent=None if args.compact else 2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(blob + "\n")
        print(f"wrote {args.out} ({len(blob)} bytes)", file=sys.stderr)
    else:
        print(blob)
    if not args.json:
        _print_packet_summary(packet)
    return EXIT_OK


def _print_packet_summary(packet: dict[str, Any]) -> None:
    """The three things a reader should see before trusting the document.

    Printed to stderr so it never contaminates a piped packet: the fingerprint
    a prescription must echo, the region a proposal must sit inside, and the
    count of questions this round cannot answer — which is the number most
    worth noticing and the easiest to skip past in 48 KB of JSON.
    """
    region = packet.get("crossover_region") or {}
    print(
        f"packet {packet.get('packet_fingerprint', '')[:16]} "
        f"session={(packet.get('session') or {}).get('bundle_session_id')}",
        file=sys.stderr,
    )
    print(
        "  region: "
        + (
            f"{region.get('band_hz')}"
            if region.get("available")
            else f"unavailable ({region.get('reason')})"
        ),
        file=sys.stderr,
    )
    for entry in packet.get("not_evaluated") or []:
        print(f"  not evaluated: {entry.get('field')} — {entry.get('reason')}", file=sys.stderr)


def _read_payload(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _gate(
    args: argparse.Namespace,
) -> tuple[bytes, BlendPrescription, dict[str, Any]]:
    """The document, the validated prescription, and what it becomes — or a raise.

    Shared WHOLE by ``propose`` and ``stage``, which is the property that makes
    the first a true dry run of the second. A staging verb with its own copy of
    these four calls would be a second reader of the same document, and the two
    would drift on the day one of them learns a new bound.

    ``BlendPrescriptionRefused`` for a refusal (the caller reports it and exits
    ``2``), ``CrossoverEvidencePacketError``/``OSError`` when the inputs cannot
    be read at all (exit ``1``).
    """
    packet = _load_packet(args)
    payload = _read_payload(args.prescription)
    document = read_prescription_bytes(payload)
    prescription = read_blend_prescription(
        document,
        packet_fingerprint=packet.get("packet_fingerprint"),
        band_hz=packet_region_band_hz(packet),
        positional_evidence=packet_positional_evidence(packet),
    )
    if prescription is None:
        # Unreachable today — `read_blend_prescription` returns None only for a
        # null document, and `read_prescription_bytes` has already refused one.
        # Written as a branch rather than an `assert` because `python -O`
        # strips asserts, and a stripped narrowing would turn an impossible
        # state into an AttributeError three lines down instead of a named
        # exit. Same reason `linearization_fit`'s cut-only invariant raises.
        # Raised into the refusal vocabulary rather than returned as a special
        # case, so both commands have exactly one arm that handles "this is not
        # a prescription we can use".
        raise BlendPrescriptionRefused(
            PRESCRIPTION_MALFORMED, "the prescription document was empty"
        )
    # Inside the gate, because the seam re-asks the route and can therefore
    # refuse too. Computed by the caller instead, a prescription that reached
    # the seam by some other path would crash the process instead of exiting
    # with the contract's refusal code — which would make the seam's own guard
    # the one thing the CLI could not report.
    return payload, prescription, blend_prescription_to_candidate_fields(prescription)


def _cmd_propose(args: argparse.Namespace) -> int:
    """Read a prescription back through the gate, and say what it becomes."""
    try:
        payload, prescription, candidate_fields = _gate(args)
    except (CrossoverEvidencePacketError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EVIDENCE_UNREADABLE
    except BlendPrescriptionRefused as exc:
        if args.json:
            print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        print(f"refused ({exc.reason}): {exc.detail}", file=sys.stderr)
        return EXIT_REFUSED

    result: dict[str, Any] = {
        "accepted": True,
        "prescription": prescription.to_dict(),
        "prescription_sha256": prescription_sha256(payload),
        "candidate_fields": candidate_fields,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"accepted {prescription.prescription_class} prescription: "
            f"{len(prescription.filters)} filter(s) over "
            f"{prescription.band_hz[0]:.1f}-{prescription.band_hz[1]:.1f} Hz",
            file=sys.stderr,
        )
        for entry in prescription.filters:
            print(
                f"  Peaking {entry['freq']:.1f} Hz Q{entry['q']:g} "
                f"{entry['gain']:+.2f} dB",
                file=sys.stderr,
            )
        print(
            f"  these become the candidate's {sorted(candidate_fields)} at build time",
            file=sys.stderr,
        )
    return EXIT_OK


def _next_round_ordinal(state_path: str | None) -> int:
    """Which round a prescription staged now would be the instruction for.

    Through ``series_position_from_state`` — the reader that lives beside the
    writer of the receipt it parses — so the ordinal this stamps and the ordinal
    the round checks it against are the same function reading the same key. A
    second derivation here (``round_receipt.round_ordinal + 1`` spelled by
    hand) would be a second owner of the series' own arithmetic, and it would
    drift the first time that reader learns a new shape to refuse.

    ``--state`` is REQUIRED for this, and the caller enforces it: without the
    state file that reader resolves every unreadable shape to the first round,
    which is a real answer for a round that is really starting over and a
    fabricated one for a prescription being filed against a series it cannot
    see.
    """
    from jasper.active_speaker.crossover_v2.coordinator import (
        series_position_from_state,
    )

    raw = json.loads(Path(str(state_path)).read_text())
    return series_position_from_state(raw).ordinal


def _cmd_stage(args: argparse.Namespace) -> int:
    """Accept a prescription and leave it where the next round will take it."""
    if not args.state:
        print(
            "error: --state is required to stage a prescription; the round it "
            "becomes an instruction for is read from the flow state's round "
            "receipt, and staging without one would file it against a series "
            "this command cannot see",
            file=sys.stderr,
        )
        return EXIT_EVIDENCE_UNREADABLE
    try:
        payload, prescription, candidate_fields = _gate(args)
        ordinal = _next_round_ordinal(args.state)
    except BlendPrescriptionRefused as exc:
        # FIRST. Every exception this handler names is a ``ValueError``
        # subclass — the refusal, the packet error, and ``JSONDecodeError`` —
        # and today they are siblings, so neither arm can swallow the other
        # whichever way round they are written. The order is here for the edit
        # that stops that being true: an arm widened to ``except ValueError``
        # below would report every refused prescription as an unreadable input,
        # handing a prescriber exit ``1`` with no reason slug, which is the one
        # outcome the closed refusal vocabulary exists to prevent.
        if args.json:
            print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        print(f"refused ({exc.reason}): {exc.detail}", file=sys.stderr)
        return EXIT_REFUSED
    except (
        CrossoverEvidencePacketError, OSError, UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        # The last two are the state file's own failure modes: it is read here
        # rather than by the packet builder, so its unreadability is this
        # command's to report.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EVIDENCE_UNREADABLE

    try:
        path = stage_prescription(payload, prescription, for_round_ordinal=ordinal)
    except OSError as exc:
        print(f"error: could not stage the prescription: {exc}", file=sys.stderr)
        return EXIT_STAGE_FAILED

    result: dict[str, Any] = {
        "accepted": True,
        "staged": True,
        "staged_at_path": str(path),
        "for_round_ordinal": ordinal,
        "prescription": prescription.to_dict(),
        "prescription_sha256": prescription_sha256(payload),
        "candidate_fields": candidate_fields,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"staged {prescription.prescription_class} prescription for round "
            f"{ordinal}: {len(prescription.filters)} filter(s) over "
            f"{prescription.band_hz[0]:.1f}-{prescription.band_hz[1]:.1f} Hz",
            file=sys.stderr,
        )
        print(f"  {path}", file=sys.stderr)
        print(
            "  the next round takes it once and consumes it; an Undo withdraws "
            "it unrun",
            file=sys.stderr,
        )
    return EXIT_OK


def _add_evidence_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "session_dir",
        help=(
            "a commissioning bundle directory (the one holding info.json and "
            "evidence/v1/artifacts/crossover_v2/<relay-session-id>/)"
        ),
    )
    parser.add_argument(
        "--state",
        default=None,
        help=(
            "the crossover-v2 flow state JSON, banked separately from the "
            "bundle. Optional; without it the packet cannot carry the "
            "per-claim verify verdicts, the Fc selection, or the applied "
            "profile's incumbent, and says so"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-crossover-prescriber",
        description=(
            "Emit one crossover round's evidence packet, and read a "
            "prescription back through the strict gate."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    packet = sub.add_parser(
        "packet",
        help="emit the evidence packet for one banked round",
    )
    _add_evidence_args(packet)
    packet.add_argument("--out", default=None, help="write the packet here instead of stdout")
    packet.add_argument(
        "--compact", action="store_true", help="emit the packet without indentation"
    )
    packet.add_argument(
        "--json",
        action="store_true",
        help="suppress the human summary on stderr",
    )
    packet.set_defaults(func=_cmd_packet)

    propose = sub.add_parser(
        "propose",
        help="validate a prescription against the round it answers",
    )
    _add_evidence_args(propose)
    propose.add_argument(
        "--prescription",
        required=True,
        help="the prescription JSON document, or - for stdin",
    )
    propose.add_argument("--out", default=None, help="write the accepted result here")
    propose.add_argument(
        "--json", action="store_true", help="emit the result (or refusal) as JSON"
    )
    propose.set_defaults(func=_cmd_propose)

    stage = sub.add_parser(
        "stage",
        help=(
            "validate a prescription and leave it for the next round to apply"
        ),
    )
    _add_evidence_args(stage)
    stage.add_argument(
        "--prescription",
        required=True,
        help="the prescription JSON document, or - for stdin",
    )
    stage.add_argument(
        "--json", action="store_true", help="emit the result (or refusal) as JSON"
    )
    stage.set_defaults(func=_cmd_stage)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
