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
import logging
import sys
from pathlib import Path
from typing import Any

from ._logging import CLI_LOG_FORMAT

from jasper.active_speaker.crossover_v2.blend_prescription import (
    BLEND_PRESCRIPTION_MALFORMED,
    BlendPrescription,
    BlendPrescriptionRefused,
    blend_prescription_to_candidate_fields,
    prescription_sha256,
    read_blend_prescription,
    read_prescription_bytes,
)
from jasper.active_speaker.crossover_v2.driver_prescription import (
    DRIVER_PRESCRIPTION_KIND,
    DriverPrescription,
    check_driver_document_size,
    driver_prescription_to_candidate_fields,
    read_driver_prescription,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
    packet_driver_passbands_hz,
    packet_feature_classifications,
    packet_positional_evidence,
    packet_region_band_hz,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    FeatureVerdict,
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
        driver_draft_path=Path(args.drivers) if args.drivers else None,
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
) -> tuple[
    bytes,
    BlendPrescription | DriverPrescription,
    dict[str, Any],
    tuple[FeatureVerdict, ...] | None,
]:
    """The document, the validated prescription, what it becomes, what judged it.

    Shared WHOLE by ``propose`` and ``stage``, which is the property that makes
    the first a true dry run of the second. A staging verb with its own copy of
    these calls would be a second reader of the same document, and the two would
    drift on the day one of them learns a new bound.

    The fourth value is the classification evidence read out of the packet, and
    it is returned rather than re-derived by ``stage`` for the same reason: it
    is what ``stage_prescription`` banks for the take's gate re-run, and a
    second read of the packet could hand the spool verdicts the gate above
    never saw. ``None`` for the blend class, which has no such bar.

    **ONE door for two classes.** The document names its own ``kind`` and that
    is what picks the gate — there is no ``--class`` flag and deliberately no
    inference from the shape. A flag would let an operator hand a blend document
    to the per-driver gate and be told its filters were malformed rather than
    that it was the wrong file; inference would make the answer depend on which
    optional fields happened to be present. The discriminator is the thing the
    document already asserts about itself, and a document naming neither kind is
    refused by whichever gate its ``kind`` most nearly matches — which, for a
    document naming nothing, is the blend one, exactly as before.

    ``BlendPrescriptionRefused`` for a refusal from either gate (the caller
    reports it and exits ``2``), ``CrossoverEvidencePacketError``/``OSError``
    when the inputs cannot be read at all (exit ``1``).
    """
    packet = _load_packet(args)
    payload = _read_payload(args.prescription)
    document = read_prescription_bytes(payload)
    prescription: BlendPrescription | DriverPrescription | None
    classifications: tuple[FeatureVerdict, ...] | None = None
    if document.get("kind") == DRIVER_PRESCRIPTION_KIND:
        # The class's own size bound, applied the moment the class is known.
        # `read_prescription_bytes` above has already stopped anything too large
        # to parse, under the family's ceiling and the family's slug — bytes
        # that will not parse have no class to be refused in the name of.
        check_driver_document_size(payload)
        classifications = packet_feature_classifications(packet)
        prescription = read_driver_prescription(
            document,
            packet_fingerprint=packet.get("packet_fingerprint"),
            passbands_hz=packet_driver_passbands_hz(packet),
            classifications=classifications,
        )
        # `fitted=None` and not an oversight: at propose/stage time this round
        # has not measured, so no per-driver fit exists to merge the document
        # into. The merge happens when a round builds its candidate, with the
        # fit it just produced. What this prints is therefore what the document
        # contributes, not the branch map the graph will carry.
        candidate_fields = driver_prescription_to_candidate_fields(
            prescription, fitted=None
        )
    else:
        prescription = read_blend_prescription(
            document,
            packet_fingerprint=packet.get("packet_fingerprint"),
            band_hz=packet_region_band_hz(packet),
            positional_evidence=packet_positional_evidence(packet),
        )
        candidate_fields = blend_prescription_to_candidate_fields(prescription)
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
            BLEND_PRESCRIPTION_MALFORMED, "the prescription document was empty"
        )
    # The candidate fields are computed INSIDE the gate, above, because each
    # seam re-asks its own route and can therefore refuse too. Computed by the
    # caller instead, a prescription that reached a seam by some other path
    # would crash the process instead of exiting with the contract's refusal
    # code — which would make the seam's own guard the one thing the CLI could
    # not report.
    return payload, prescription, candidate_fields, classifications


def _cmd_propose(args: argparse.Namespace) -> int:
    """Read a prescription back through the gate, and say what it becomes."""
    try:
        payload, prescription, candidate_fields, _ = _gate(args)
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
        _print_prescription(prescription, "accepted")
        print(
            f"  these become the candidate's {sorted(candidate_fields)} at build time",
            file=sys.stderr,
        )
    return EXIT_OK


def _scope(prescription: BlendPrescription | DriverPrescription) -> str:
    """What this prescription's filters were bounded BY, in one phrase.

    Two classes, two bounds, and the summary has to name the one that actually
    applied: an operator told "over 1200-2400 Hz" about a per-driver
    prescription would read the crossover region into a document that was never
    checked against it.
    """
    if isinstance(prescription, DriverPrescription):
        return ", ".join(
            f"{role} {lo:.0f}-{hi:.0f} Hz"
            for role, lo, hi in prescription.passbands_hz
            if role in prescription.roles
        )
    return f"{prescription.band_hz[0]:.1f}-{prescription.band_hz[1]:.1f} Hz"


def _print_prescription(
    prescription: BlendPrescription | DriverPrescription,
    verb: str,
    *,
    qualifier: str = "",
) -> None:
    """The human summary, shared by both verbs and both classes.

    Extracted rather than copied when the second class arrived, and the exact
    wording of both lines is preserved: ``stage``'s "for round N" sits where it
    always did, because a test in a real subprocess reads that sentence to prove
    the CLI's logging configuration did not swallow the operator's own output.
    """
    print(
        f"{verb} {prescription.prescription_class} prescription{qualifier}: "
        f"{len(prescription.filters)} filter(s) over {_scope(prescription)}",
        file=sys.stderr,
    )
    for entry in prescription.filters:
        role = f"{entry['role']} " if "role" in entry else ""
        print(
            f"  {role}Peaking {entry['freq']:.1f} Hz Q{entry['q']:g} "
            f"{entry['gain']:+.2f} dB",
            file=sys.stderr,
        )


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
        payload, prescription, candidate_fields, classifications = _gate(args)
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
        path = stage_prescription(
            payload,
            prescription,
            for_round_ordinal=ordinal,
            classifications=classifications,
        )
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
        _print_prescription(prescription, "staged", qualifier=f" for round {ordinal}")
        print(f"  {path}", file=sys.stderr)
        print(
            "  the next round takes it once and consumes it; an Undo withdraws "
            "it unrun",
            file=sys.stderr,
        )
    return EXIT_OK


#: What ``--state`` is, said once. The two verbs differ only in whether they
#: can proceed without it, so the sentence that describes the FILE has one owner
#: and each verb appends its own requirement — a shared "Optional" on a verb
#: that hard-refuses without it is a `--help` that contradicts the command.
_STATE_HELP = (
    "the crossover-v2 flow state JSON, banked separately from the bundle"
)
_STATE_HELP_OPTIONAL = (
    f"{_STATE_HELP}. Optional; without it the packet cannot carry the "
    "per-claim verify verdicts, the Fc selection, or the applied profile's "
    "incumbent, and says so"
)
_STATE_HELP_REQUIRED = (
    f"{_STATE_HELP}. REQUIRED for this verb: the round a prescription becomes "
    "an instruction for is read from its round receipt, and staging without "
    "one would file the prescription against a series this command cannot see"
)


#: What ``--drivers`` is. Optional for BOTH verbs, unlike ``--state``: a blend
#: prescription never needs it, and a per-driver one is refused by name without
#: it — which is the packet's own honesty rule applied to a second evidence
#: source, not a reason to make every operator pass a path they do not use.
_DRIVERS_HELP = (
    "the active-speaker design draft JSON, which carries the confirmed "
    "driver-safety profile (default location on a speaker: "
    "/var/lib/jasper/active_speaker_design_draft.json). Optional; without it "
    "the packet cannot say where each driver's own band starts and ends, and "
    "a per-driver prescription has no bound to be checked against"
)


def _add_evidence_args(
    parser: argparse.ArgumentParser, *, state_help: str = _STATE_HELP_OPTIONAL
) -> None:
    parser.add_argument(
        "session_dir",
        help=(
            "a commissioning bundle directory (the one holding info.json and "
            "evidence/v1/artifacts/crossover_v2/<relay-session-id>/)"
        ),
    )
    # Not `required=True` even for ``stage``: argparse would refuse before the
    # two speaker-level questions are asked, and the command owns a sentence
    # that says WHY the flag matters here. The check lives in `_cmd_stage`.
    parser.add_argument("--state", default=None, help=state_help)
    parser.add_argument("--drivers", default=None, help=_DRIVERS_HELP)


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
    _add_evidence_args(stage, state_help=_STATE_HELP_REQUIRED)
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
    # A10. Without this the tool's structured events have no handler at all:
    # ``logging.lastResort`` emits WARNING and above, so
    # ``event=crossover_v2.prescription_staged`` — written by
    # ``stage_prescription`` at INFO, right after the atomic write — reached
    # neither an operator's terminal nor the journal. This CLI is the only
    # supported staging path, so that made the one state transition it performs
    # unobservable: a prescription could be banked, or silently REPLACE another,
    # with nothing anywhere saying so.
    #
    # ``basicConfig`` at INFO in ``main``, which is what the seven sibling CLIs
    # that emit ``event=`` lines do (``sound``, ``aec_commission``,
    # ``aec_init``, …) rather than something new. Deliberately NOT
    # ``_logging.configure_verbose_logging``: that helper is for CLIs with a
    # ``--verbose`` flag and floors at WARNING without one, which is exactly the
    # level that hid this event. Its FORMAT is reused, so the one place the
    # shared shape is written down stays the only place.
    #
    # In ``main`` rather than at import, because a module that configures the
    # root logger on import imposes its choice on every importer — including
    # the test suite and any tool that reaches in for ``read_prescription_bytes``.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
