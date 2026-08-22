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

``status`` is the fourth verb and the odd one out: it writes nothing and gates
nothing. Sequencing in this loop is a set of artifact-dependency refusals
rather than a workflow engine, which is cheap to run and expensive to be
dropped into the middle of — so one verb says where a speaker stands, and it
derives that from the SAME builders the three doors read rather than from a
second walk of the same tree.

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
often a script: ``0`` accepted (``status``, which accepts nothing, exits ``0``
when it read the evidence), ``1`` the evidence could not be read, ``2`` the
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
    prescription_spool_path,
    stage_prescription,
    staged_prescription_pending,
)
from jasper.identity import read_identity

EXIT_OK = 0
EXIT_EVIDENCE_UNREADABLE = 1
EXIT_REFUSED = 2
EXIT_STAGE_FAILED = 3

#: Where a human goes to run, apply, or undo a crossover round.
CROSSOVER_PAGE_PATH = "/sound/crossover/"

#: Where a human DECLARES the speaker — drivers, their safety profile, the
#: corner. A second page rather than a second spelling of the first: the
#: per-driver bound comes from the design draft that page writes, and an
#: operator whose speaker has never been commissioned cannot satisfy
#: ``--drivers`` by pointing harder at a file that does not exist yet.
SOUND_SETUP_PAGE_PATH = "/sound/setup/"

#: What happens to a document sitting in the spool, said once. ``stage`` says it
#: at the moment of banking and ``status`` says it to an operator who arrived
#: later and found one waiting — the same fact at two moments, so a second
#: wording here would be a second answer to "what becomes of this file".
STAGED_LIFECYCLE_NOTE = (
    "the next round takes it once and consumes it; an Undo withdraws it unrun"
)


def _speaker_url(path: str) -> str:
    """A handoff URL for THIS speaker, from the hostname it is configured with.

    Through :func:`jasper.identity.read_identity` — the repository's single
    speaker-identity reader, which exists (its own words) "so consumers ...
    stop reconstructing identity ad-hoc and drifting from each other" — rather
    than an ``os.environ`` read of ``JASPER_HOSTNAME`` spelled a second time
    here. It is TOTAL and never raises, which is what a status verb needs:
    a speaker whose identity files are unreadable still gets a URL, at the
    documented default.

    Deliberately NOT ``Config.from_env``, whose ``hostname`` field says the
    same thing: that constructor refuses outright when no voice provider is
    configured and validates two dozen unrelated knobs on the way past, so an
    orientation verb built on it would fail on a bench speaker that has never
    been given an API key.

    Why this exists at all: speakers are ``jts1.local``, ``jts3.local``, …, and
    a printed ``http://jts.local/...`` sends its reader to a different box —
    silently, because that name usually resolves to something.
    """
    return f"http://{read_identity().hostname}{path}"


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


def _band_phrase(lo: float, hi: float) -> str:
    """One frequency span, spelled the one way this tool spells it.

    Named because ``status`` prints the crossover region and ``propose`` prints
    the band a blend prescription was bounded by, and those are the same span
    read at two moments — two format strings would drift the day one of them
    gained a decimal.
    """
    return f"{lo:.1f}-{hi:.1f} Hz"


def _passband_phrase(role: str, lo: float, hi: float) -> str:
    """One role's declared band. Same reason as :func:`_band_phrase`.

    Coarser than the region on purpose: a driver's declared band is a
    manufacturer figure rounded to whole hertz, and printing it to a tenth
    would suggest a precision the declaration does not have.
    """
    return f"{role} {lo:.0f}-{hi:.0f} Hz"


def _scope(prescription: BlendPrescription | DriverPrescription) -> str:
    """What this prescription's filters were bounded BY, in one phrase.

    Two classes, two bounds, and the summary has to name the one that actually
    applied: an operator told "over 1200-2400 Hz" about a per-driver
    prescription would read the crossover region into a document that was never
    checked against it.
    """
    if isinstance(prescription, DriverPrescription):
        return ", ".join(
            _passband_phrase(role, lo, hi)
            for role, lo, hi in prescription.passbands_hz
            if role in prescription.roles
        )
    return _band_phrase(prescription.band_hz[0], prescription.band_hz[1])


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
        print(f"  {STAGED_LIFECYCLE_NOTE}", file=sys.stderr)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _block(packet: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """One of the packet's own blocks, or an empty one when there is no packet."""
    block = (packet or {}).get(name)
    return block if isinstance(block, dict) else {}


def _reason(block: dict[str, Any], packet_error: str) -> str:
    """Why a section has nothing to report, from whichever layer knows.

    The packet builder's failure wins when there is one, because then no block
    was built at all and the block's own silence would be reported as the
    round's rather than as the bundle's. Below that, the block's own
    ``_absence`` reason — ``source_absent`` and ``field_null`` are different
    facts and this verb passes both through untranslated. "not reported" only
    when a block says unavailable and names no reason; inventing one would be
    this tool asserting something the packet declined to.
    """
    if packet_error:
        return packet_error
    reason = block.get("reason")
    return str(reason) if reason else "not reported"


def _incumbent_record(value: Any, packet_error: str) -> dict[str, Any]:
    """One side of the packet's incumbent block, classified but not reconciled.

    The packet reports each side either as the correction itself or as an
    absence, and deliberately makes no judgement between its two records — so
    neither does this. A status line that preferred one would hide exactly the
    round where the receipt and the applied profile disagreed, which is the
    thing reporting them side by side exists to catch.

    An empty list is ``available`` with zero filters, and that is not pedantry:
    "the round recorded an incumbent and it was empty" and "no round receipt
    was readable" are the two facts a prescription author most needs kept
    apart, because a prescription is a TOTAL and the second means they do not
    know what they are totalling.

    **The reason is echoed only from the absence shape the packet builder
    writes.** This block is the one place a value read VERBATIM out of a
    bundle artifact reaches the report, so a receipt whose ``incumbent`` is
    some other object would otherwise print that object's ``reason`` key as
    though the builder had explained something. Checking the ``status`` field
    reduces that to the shape the builder actually authors; it does not
    eliminate it, because a receipt that mimics the shape is indistinguishable
    from one the builder wrote without the packet recording which of the two it
    is. That residue is terminal output only — this verb gates nothing — and is
    pinned below rather than left for the next reader to rediscover.
    """
    if isinstance(value, list):
        return {"available": True, "n_filters": len(value)}
    authored = (
        isinstance(value, dict) and value.get("status") == "not_evaluated"
    )
    return {
        "available": False,
        "reason": _reason(value if authored else {}, packet_error),
    }


def _incumbent_phrase(record: dict[str, Any]) -> str:
    """One classified incumbent record as the report says it."""
    return (
        f"{record['n_filters']} blend filter(s)"
        if record["available"]
        else f"none ({record['reason']})"
    )


def _declared_section(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """What this speaker says its drivers are, through the per-driver gate's reader.

    :func:`~.evidence_packet.packet_driver_passbands_hz` is what bounds a
    per-driver prescription, so asking it here is asking the question the door
    will ask. Reading ``drivers.passbands_hz`` out of the block by hand would
    be a second opinion about where in the document that bound lives.
    """
    passbands = packet_driver_passbands_hz(packet)
    roles = sorted(passbands)
    reason = None if passbands else _reason(_block(packet, "drivers"), packet_error)
    return {
        "available": bool(passbands),
        "roles": roles,
        "passbands_hz": {
            role: [lo, hi] for role, (lo, hi) in sorted(passbands.items())
        },
        "reason": reason,
        "summary": (
            ", ".join(_passband_phrase(role, *passbands[role]) for role in roles)
            if passbands
            else f"no declared driver band ({reason})"
        ),
    }


def _banked_section(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """The round, and the two banked bounds a prescription of either class needs.

    The region bounds a blend document and the classified features are what a
    per-driver filter must be shown to be aimed at, so both ride inside the
    banked section rather than beside it: they are facts about this round's
    evidence, and they are absent for the same reasons the round is.
    """
    region = packet_region_band_hz(packet)
    verdicts = packet_feature_classifications(packet)
    region_state: dict[str, Any] = {
        "available": region is not None,
        "band_hz": [region[0], region[1]] if region else None,
        "reason": (
            None
            if region
            else _reason(_block(packet, "crossover_region"), packet_error)
        ),
    }
    classification = {
        "available": bool(verdicts),
        "n_verdicts": len(verdicts) if verdicts else 0,
        "reason": (
            None
            if verdicts
            else _reason(_block(packet, "feature_classification"), packet_error)
        ),
    }
    round_block = _block(packet, "round")
    session = _block(packet, "session")
    available = bool(round_block.get("available"))
    reason = None if available else _reason(round_block, packet_error)
    summary = (
        (
            f"round {session.get('round_id')} in session "
            f"{session.get('bundle_session_id')}"
            + (
                f", region {_band_phrase(*region_state['band_hz'])}"
                if region_state["available"]
                else f", no region ({region_state['reason']})"
            )
            + (
                f", {classification['n_verdicts']} classified feature(s)"
                if classification["available"]
                else f", no readable classification ({classification['reason']})"
            )
        )
        if available
        else f"no round receipt ({reason})"
    )
    return {
        "available": available,
        "reason": reason,
        "bundle_session_id": session.get("bundle_session_id"),
        "round_id": session.get("round_id"),
        "region": region_state,
        "classification": classification,
        "summary": summary,
    }


def _staged_section() -> dict[str, Any]:
    """Whether an instruction is waiting for the next round. The stat, not a peek.

    No packet argument, and that is the point: the spool lives on the speaker
    rather than in any bundle, so a prescription waiting for the next round is
    a fact whichever directory the operator named — including a directory that
    turned out not to be a bundle at all.
    """
    pending = staged_prescription_pending()
    return {
        "pending": pending,
        "path": str(prescription_spool_path()),
        "summary": (
            f"one prescription waiting — {STAGED_LIFECYCLE_NOTE}"
            if pending
            else "nothing waiting"
        ),
    }


def _applied_section(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """What the round was measured THROUGH, from both records the packet keeps."""
    block = _block(packet, "incumbent")
    from_receipt = _incumbent_record(block.get("from_round_receipt"), packet_error)
    from_profile = _incumbent_record(block.get("from_applied_profile"), packet_error)
    return {
        "from_round_receipt": from_receipt,
        "from_applied_profile": from_profile,
        "summary": (
            f"round receipt: {_incumbent_phrase(from_receipt)}; "
            f"applied profile: {_incumbent_phrase(from_profile)}"
        ),
    }


def _status_sections(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """Declared, banked, staged, applied — through the doors' own readers.

    Every fact in these four comes from
    :func:`~.evidence_packet.build_crossover_evidence_packet` and the named
    readers the gate itself calls
    (:func:`~.evidence_packet.packet_region_band_hz`,
    :func:`~.evidence_packet.packet_driver_passbands_hz`,
    :func:`~.evidence_packet.packet_feature_classifications`), plus the spool's
    own :func:`~.prescription_spool.staged_prescription_pending`. **No second
    walk of the bundle.** A status verb with its own tree reader would answer a
    slightly different question from the door beside it, and the day they
    disagreed the operator would believe the one that was not enforcing
    anything.

    Every packet reader tolerates ``None``, so a bundle that could not be read
    at all needs no special case: each section resolves to unavailable carrying
    the builder's own error as its reason.

    Each section carries a ``summary`` sentence, and it is the SAME sentence
    the human report prints — a printer that phrased its own would be a second
    wording of each fact, and the ``--json`` reader and the terminal reader
    would end up told different things.
    """
    return {
        "declared": _declared_section(packet, packet_error),
        "banked": _banked_section(packet, packet_error),
        "staged": _staged_section(),
        "applied": _applied_section(packet, packet_error),
    }


def _next_actions(
    sections: dict[str, Any],
    *,
    state_supplied: bool,
    crossover_url: str,
    declaration_url: str,
) -> list[str]:
    """What this speaker can do next, derived from what it does and does not have.

    Artifact dependencies, not a workflow: each line is the consequence of one
    artifact being present or absent, and the tool that would refuse for want
    of it is named so an operator can tell "not yet" from "broken". Nothing
    here sequences anything — the refusals do that, and they do it whether or
    not this verb was ever run.
    """
    banked = sections["banked"]
    declared = sections["declared"]
    out: list[str] = []

    if not banked["available"]:
        out.append(
            f"no round is banked here ({banked['reason']}) — point this verb at "
            f"a commissioning bundle, or run a round at {crossover_url}"
        )
    else:
        region = banked["region"]
        classification = banked["classification"]
        prescribable = False
        if region["available"]:
            prescribable = True
            out.append(
                "a blend prescription can be written for the crossover region "
                f"{_band_phrase(*region['band_hz'])}"
            )
        else:
            out.append(
                f"no crossover region is banked ({region['reason']}), so a blend "
                "prescription has no bound and is refused by name"
            )
        if declared["available"] and classification["available"]:
            prescribable = True
            out.append(
                "a per-driver prescription can be written for "
                f"{', '.join(declared['roles'])}"
            )
        elif not declared["available"]:
            # Both halves, because the reason tells the two apart and the
            # operator may not be able to act on either alone: a draft that
            # exists is a path away, and a speaker that was never commissioned
            # has no path to point harder at yet.
            out.append(
                f"no declared driver band is available ({declared['reason']}) — "
                "pass --drivers <design draft JSON>, or declare the drivers at "
                f"{declaration_url}; without it a per-driver prescription has "
                "no bound and is refused by name"
            )
        else:
            # "readable", not "banked": this arm also covers an artifact that
            # WAS banked and whose every row the typed reader dropped. Saying
            # "not banked" there would send an operator to look for a file that
            # is sitting right in the round directory. The action is the same
            # either way — run the classifier — so one honest sentence covers
            # both, and the reason (``source_absent`` vs "not reported") is
            # what tells them apart.
            out.append(
                "no readable feature classification for this round "
                f"({classification['reason']}) — run `jasper-classify-features`; "
                "without it no per-driver filter can be shown to be aimed at a "
                "driver defect"
            )
        if prescribable:
            out.append(
                "write one against `packet`, then `propose` to see it judged and "
                "`stage` to leave it for the next round"
            )

    if not state_supplied:
        out.append(
            "pass --state <flow state JSON>: `stage` refuses without it, and the "
            "applied profile's own incumbent cannot be read"
        )

    if sections["staged"]["pending"]:
        out.append(f"a prescription is already staged — {STAGED_LIFECYCLE_NOTE}")
    out.append(f"run, apply, or undo a round at {crossover_url}")
    return out


def _print_status(payload: dict[str, Any]) -> None:
    """The report, from the section summaries rather than a second phrasing."""
    print(f"{'speaker:':9} {payload['speaker']['hostname']}")
    for name in ("declared", "banked", "staged", "applied"):
        print(f"{name + ':':9} {payload[name]['summary']}")
    print("next:")
    for action in payload["next_actions"]:
        print(f"  - {action}")


def _cmd_status(args: argparse.Namespace) -> int:
    """Where this speaker stands, and what it can do next. Writes nothing.

    **A partial answer beats no answer**, so an unreadable bundle does not stop
    the report: the packet's failure becomes every evidence section's reason,
    and the spool — which lives on the speaker, not in the bundle — is reported
    truthfully regardless. A prescription waiting for the next round is a fact
    about this speaker whichever directory the operator happened to name.

    The exit code still tells a script which of those two happened:
    :data:`EXIT_EVIDENCE_UNREADABLE` when the packet could not be built,
    matching this tool's contract that ``1`` means the evidence could not be
    read. The report prints either way.

    Unlike its three siblings the human report goes to STDOUT. For them stdout
    is reserved for a document a pipe consumes and the human gloss goes to
    stderr so it cannot contaminate it; this verb emits no document unless
    ``--json`` asks for one, and a report whose only copy went to stderr would
    be invisible to the SSH agent that is this verb's main reader.
    """
    packet: dict[str, Any] | None = None
    packet_error = ""
    try:
        packet = _load_packet(args)
    except (CrossoverEvidencePacketError, OSError) as exc:
        packet_error = str(exc)

    crossover_url = _speaker_url(CROSSOVER_PAGE_PATH)
    declaration_url = _speaker_url(SOUND_SETUP_PAGE_PATH)
    sections = _status_sections(packet, packet_error)
    payload: dict[str, Any] = {
        "speaker": {
            "hostname": read_identity().hostname,
            "crossover_url": crossover_url,
            "declaration_url": declaration_url,
        },
        "packet_fingerprint": (packet or {}).get("packet_fingerprint"),
        "packet_error": packet_error or None,
        **sections,
        "next_actions": _next_actions(
            sections,
            state_supplied=bool(args.state),
            crossover_url=crossover_url,
            declaration_url=declaration_url,
        ),
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_status(payload)
    return EXIT_EVIDENCE_UNREADABLE if packet_error else EXIT_OK


#: What ``--state`` is, said once. The verbs differ only in whether they can
#: proceed without it — ``stage`` cannot, the other three degrade and say so —
#: so the sentence that describes the FILE has one owner and each verb appends
#: its own requirement. A shared "Optional" on a verb that hard-refuses without
#: it is a `--help` that contradicts the command.
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
            "Emit one crossover round's evidence packet, read a prescription "
            "back through the strict gate, and say where this speaker stands."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # First, because it is where an operator who has just arrived starts.
    status = sub.add_parser(
        "status",
        help="print declared / banked / staged / applied state and what is next",
    )
    _add_evidence_args(status)
    status.add_argument(
        "--json", action="store_true", help="emit the report as JSON"
    )
    status.set_defaults(func=_cmd_status)

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
