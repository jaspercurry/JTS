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

**Emit the packet ONCE, and judge against that file.** ``propose`` and
``stage`` take ``--packet <file>`` and read it AS the evidence, which is the
intended flow: run ``packet`` on the box, hand the file to whoever writes the
prescription, then run ``propose``/``stage`` against the same file. Rebuilding
the packet a second time is what used to make staging a fingerprint dance — a
rebuild on another machine resolves ``--drivers``, ``--applied-profile`` and
``--repeat-floor`` against whatever THAT machine has, so it fingerprints
differently and the document written against the first one is refused
against the second, leaving an operator to paste a fingerprint across by
hand. Nothing here re-stamps a
fingerprint and nothing ever will: the echo is provenance, and a tool that
rewrote it would make every accepted prescription unprovable. What ``--packet``
removes is the second packet, so the echo matches by construction.

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

from jasper.active_speaker.baseline_profile import (
    DEFAULT_STATE_PATH as _APPLIED_PROFILE_DEFAULT_PATH,
)
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
)
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
    packet_incumbent_linearization,
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
from jasper.active_speaker.design_draft import (
    DEFAULT_DESIGN_DRAFT_PATH as _DRIVERS_DEFAULT_PATH,
)
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL,
    DEFAULT_TOLERANCE_DB,
    seat_level_reference_volume_db,
)
from jasper.active_speaker.session_volume_plan import (
    MEASUREMENT_REFERENCE_VOLUME_DB,
)
from jasper.identity import (
    CROSSOVER_PAGE_PATH,
    SOUND_SETUP_PAGE_PATH,
    read_identity,
    speaker_url,
)

EXIT_OK = 0
EXIT_EVIDENCE_UNREADABLE = 1
EXIT_REFUSED = 2
EXIT_STAGE_FAILED = 3

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). One tool,
#: one owner: the generator reads this rather than the runbook restating it.
#: Three of the four verbs only read; `stage` is the one that writes.
AUTHORITY_TIER = "advisory (`stage` mutates)"

#: What happens to a document sitting in the spool, said once. ``stage`` says it
#: at the moment of banking and ``status`` says it to an operator who arrived
#: later and found one waiting — the same fact at two moments, so a second
#: wording here would be a second answer to "what becomes of this file".
STAGED_LIFECYCLE_NOTE = (
    "the next round takes it once and consumes it; an Undo withdraws it unrun"
)

#: Why the staged section has nothing to report. A slug in the packet's own
#: style, so the section that reads a file the packet never sees still answers
#: in the vocabulary the other three answer in.
SPOOL_UNREADABLE_REASON = "permission_denied"


def _read_packet_file(path: Path) -> dict[str, Any]:
    """One already-emitted packet, read as the evidence rather than rebuilt.

    The whole point of the flag: a packet emitted on the speaker and a packet
    rebuilt on a laptop fingerprint differently, because the rebuild resolves
    ``--drivers``/``--applied-profile``/``--repeat-floor`` against whatever
    that machine has. So the answer to a packet was either re-fingerprinted
    by hand — provenance laundering, and the one thing the echo exists to
    prevent — or judged against evidence it was not written for. Reading the
    FILE removes the second packet entirely; the fingerprint then matches by
    construction.

    Every ``packet_*`` reader the gate uses takes the packet as a VALUE and
    tolerates any shape, so a file that parses is a usable evidence source and
    one that does not answers the tool's own "the evidence could not be read"
    exit. ``OSError`` is deliberately not caught: an unreadable path is the same
    failure as an unreadable bundle and both commands already map it to that
    exit.
    """
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CrossoverEvidencePacketError(
            f"{path} is not a readable evidence packet: {exc}"
        ) from exc
    if not isinstance(packet, dict):
        raise CrossoverEvidencePacketError(
            f"{path} must hold one evidence packet object, got "
            f"{type(packet).__name__}"
        )
    return packet


def _load_packet(args: argparse.Namespace) -> dict[str, Any]:
    """The packet, as a value — already separate from every verb's printing.

    ``_cmd_packet``, ``_cmd_status`` and ``_gate`` each catch this call's
    errors their own way (different exception tuples, different exit codes),
    which is why the value door stops here rather than inside a shared
    try/except: one mapping could not serve three different contracts.

    ``--packet`` short-circuits the build: the two sources are exclusive, and
    :func:`_evidence_source_error` has already refused an invocation that named
    both. The on-speaker defaults are resolved HERE rather than at the argparse
    default, so "the operator passed this flag" stays answerable — which is
    what that refusal is decided on.
    """
    if args.packet:
        return _read_packet_file(Path(args.packet))
    return build_crossover_evidence_packet(
        Path(args.session_dir),
        state_path=Path(args.state) if args.state else None,
        driver_draft_path=Path(args.drivers or _DRIVERS_DEFAULT_PATH),
        applied_profile_path=Path(
            args.applied_profile or _APPLIED_PROFILE_DEFAULT_PATH
        ),
        repeat_floor_path=Path(args.repeat_floor or _REPEAT_FLOOR_DEFAULT_PATH),
    )


#: The flags that exist ONLY to feed a rebuild, and are therefore refused
#: beside ``--packet``. ``--state`` is not among them: ``stage`` reads it for
#: the round ordinal, which is not evidence, so it is refused per-verb below.
_REBUILD_ONLY_FLAGS: tuple[tuple[str, str], ...] = (
    ("--drivers", "drivers"),
    ("--applied-profile", "applied_profile"),
    ("--repeat-floor", "repeat_floor"),
)


def _evidence_source_error(args: argparse.Namespace) -> str | None:
    """ONE evidence source per invocation, or the sentence that says why not.

    Two sources cannot both be the evidence, and the failure of letting them
    try is silent: the rebuild wins, the document echoes the file's fingerprint,
    and the operator is told their prescription answers the wrong round. So a
    rebuild input named beside ``--packet`` is refused rather than ignored.

    ``--state`` is the exception, and only on ``stage``: that verb reads it for
    the round ordinal — a fact about the SERIES, not about the round's
    evidence — and hard-refuses without it, so refusing it here would make
    ``stage --packet`` unreachable. On ``propose`` it feeds nothing but the
    rebuild, so it is refused with the rest.
    """
    if not args.packet:
        if args.session_dir is None:
            return (
                "name the evidence: a session_dir to build the packet from, or "
                "--packet <packet JSON> to judge against one already emitted"
            )
        return None
    named = ["the session_dir positional"] if args.session_dir else []
    named += [flag for flag, dest in _REBUILD_ONLY_FLAGS if getattr(args, dest)]
    if args.command != "stage" and args.state:
        named.append("--state")
    if not named:
        return None
    return (
        f"--packet is the evidence, so {', '.join(named)} cannot be given "
        "beside it: those inputs only feed a rebuild, and a rebuild "
        "fingerprints differently from the file it was rebuilt beside — which "
        "is the mismatch --packet exists to remove"
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
    """The four things a reader should see before trusting the document.

    Printed to stderr so it never contaminates a piped packet: the fingerprint
    a prescription must echo, the region a proposal must sit inside, the
    count of questions this round cannot answer — which is the number most
    worth noticing and the easiest to skip past in 48 KB of JSON — and, per
    role, the trim this round's own measurement resolved beside the trim
    already applied, so a re-solve is visible before a prescriber decides
    whether to pin it rather than only after (on the receipt's ``delta_db``).
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
    trim = (packet.get("incumbent") or {}).get("trim") or {}
    for role, numbers in sorted(trim.items()):
        applied_db, resolved_db = numbers.get("applied_db"), numbers.get("round_resolved_db")
        if applied_db is None or resolved_db is None:
            continue
        pinned = " (pinned this round)" if numbers.get("pinned_this_round") else ""
        print(
            f"  trim {role}: applied {applied_db:+.2f} dB, round resolved "
            f"{resolved_db:+.2f} dB (Δ {numbers['delta_db']:+.2f} dB){pinned}",
            file=sys.stderr,
        )


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
            incumbent_filters=packet_incumbent_linearization(packet),
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
    source_error = _evidence_source_error(args)
    if source_error is not None:
        print(f"error: {source_error}", file=sys.stderr)
        return EXIT_EVIDENCE_UNREADABLE
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


def _displaced_phrase(prescription: DriverPrescription) -> str:
    """What staging this document deletes, in one line, or that nobody knows.

    Three answers, kept apart because they send an operator somewhere
    different: the evidence carried no incumbent (go and find out what the
    speaker is playing before you total a role); it carried one and this
    document replaces nothing; it replaces filters, and here is how far above
    them the prescribed cascade ends up. Only the last is the 2026-08-22 shape.

    It reports and never refuses — see
    :func:`~.driver_prescription._check_displaced` for the mechanism test that
    decision rests on.
    """
    count = prescription.displaced_filters
    if count is None:
        return (
            "displaces: unknown — this packet carries no incumbent "
            "linearization, so what these filters replace cannot be named"
        )
    if not count:
        return "displaces: nothing (the named role(s) carry no filters today)"
    boost = prescription.displaced_boost_db or 0.0
    where = (
        f", peaking on the {prescription.displaced_boost_role}"
        if prescription.displaced_boost_role
        else ""
    )
    return (
        f"displaces: {count} incumbent filter(s); net {boost:+.2f} dB against "
        f"the graph now playing{where}"
    )


def _vouch_phrase(prescription: DriverPrescription) -> str:
    """Which filters a banked verdict backs, in one line, or that nobody knows.

    The three answers ``_displaced_phrase`` draws, for the same reason —
    "nobody read the evidence", "it was read and everything is backed", and
    "it was read and these are not" send an operator somewhere different. Only
    the third asks for a judgement. A fourth line covers the empty document,
    which has no filters to say either about.

    It reports and never refuses. Until 2026-08-23 the unvouched filters were
    refused instead, which meant a role could never keep an incumbent shelf: the
    fit engine placed it and no verdict vouches for it, so naming the role
    deleted it (#2863). See
    :func:`~.driver_prescription._check_classification` for the ruling.
    """
    unvouched = prescription.unvouched_filters
    total = len(prescription.filters)
    if unvouched is None:
        return (
            "vouched: unknown — no banked classification was read for this "
            "document, so which filters the evidence backs cannot be named"
        )
    if not total:
        return "vouched: no filters to vouch for"
    if not unvouched:
        return f"vouched: all {total} filter(s) sit on a banked defect verdict"
    # By ``(role, freq)`` rather than by position, so a basis shorter than the
    # filter list names the RIGHT filters — the same match the receipt's own
    # basis is keyed on.
    backed = {
        (basis.role, basis.filter_freq_hz)
        for basis in prescription.classification_basis
    }
    named = ", ".join(
        f"{entry['role']} @ {float(entry['freq']):.0f} Hz"
        for entry in prescription.filters
        if (str(entry["role"]), float(entry["freq"])) not in backed
    )
    return (
        f"vouched: {total - unvouched} of {total} filter(s); {unvouched} carry "
        f"no banked verdict ({named}) — disclosed, not refused; the round "
        "measures whether they helped"
    )


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

    The per-driver class gets two more lines, after the filters, and both are
    disclosures the gate makes rather than bounds it applies. What the document
    DELETES, because a document of that class is a TOTAL for every role it names
    and the filters printed above are therefore also a deletion of whatever
    those roles carry — the fact the 2026-08-22 round had no way to see (#2863).
    And which of those filters a banked verdict BACKS, which stopped being a
    refusal on 2026-08-23 and became this line.
    """
    print(
        f"{verb} {prescription.prescription_class} prescription{qualifier}: "
        f"{len(prescription.filters)} filter(s) over {_scope(prescription)}",
        file=sys.stderr,
    )
    for entry in prescription.filters:
        role = f"{entry['role']} " if "role" in entry else ""
        # The entry's OWN type, and its Q only when the type has one. The line
        # spelled a literal "Peaking" while that was the only type either class
        # admitted; the driver door now takes the emitter's whole set, and a
        # Lowshelf printed as a Peaking at a Q the emitter drops would be an
        # operator report disagreeing with the graph it describes.
        biquad_type = str(entry.get("biquad_type") or "Peaking")
        q = f"Q{entry['q']:g} " if biquad_type == "Peaking" else ""
        print(
            f"  {role}{biquad_type} {entry['freq']:.1f} Hz {q}"
            f"{entry['gain']:+.2f} dB",
            file=sys.stderr,
        )
    if isinstance(prescription, DriverPrescription):
        # Only when there IS one, unlike the two lines below — those always have
        # something to say and a pin is rare. Printed first because it is the
        # one thing here that moves a LEVEL rather than a shape: an operator
        # reading a filter list has to be told the round will not solve the trim
        # underneath it.
        if prescription.pinned_trim_db:
            pins = ", ".join(
                f"{role} {db:+.2f} dB" for role, db in prescription.pinned_trim_db
            )
            print(
                f"  pins: {pins} — carried, not re-solved by the round",
                file=sys.stderr,
            )
        print(f"  {_displaced_phrase(prescription)}", file=sys.stderr)
        print(f"  {_vouch_phrase(prescription)}", file=sys.stderr)


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
    source_error = _evidence_source_error(args)
    if source_error is not None:
        print(f"error: {source_error}", file=sys.stderr)
        return EXIT_EVIDENCE_UNREADABLE
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


def _degree_list(block: dict[str, Any], key: str) -> list[int]:
    """One of the packet's whole-degree lists, or empty when it published none."""

    value = block.get(key)
    return list(value) if isinstance(value, list) else []


def _banked_section(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """The round, and the two banked bounds a prescription of either class needs.

    The region bounds a blend document and the classified features are what a
    per-driver filter must be shown to be aimed at, so both ride inside the
    banked section rather than beside it: they are facts about this round's
    evidence, and they are absent for the same reasons the round is.

    ``walk`` is the exception to "absent for the same reasons": the packet's
    ``lateral_poses`` block is filled by ACCEPTED takes, while ``available``
    above needs a ``round_receipt.json``, which is only written once a graded
    post-apply VERIFY completes. A measurement-only angle walk therefore banks
    poses and no receipt, and without this sub-block the operator who staged it
    — or the driver polling this verb for it — would read the round's silence
    as the walk's.
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
    lateral = _block(packet, "lateral_poses")
    walk: dict[str, Any] = {
        "available": bool(lateral.get("available")),
        "n_takes": lateral.get("n_takes") or 0,
        "angles_deg": _degree_list(lateral, "angles_deg"),
        "elevations_deg": _degree_list(lateral, "elevations_deg"),
        "reason": (
            None if lateral.get("available")
            else _reason(lateral, packet_error)
        ),
    }
    # A walk at mark height reads exactly the sentence it read before elevation
    # was sayable: "0 deg" is not a raise worth a clause.
    raised = [deg for deg in walk["elevations_deg"] if deg]
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
    ) + (
        f"; {walk['n_takes']} walk take(s) at "
        f"{', '.join(str(deg) for deg in walk['angles_deg'])} deg"
        + (
            f", elevations {', '.join(str(deg) for deg in walk['elevations_deg'])}"
            " deg"
            if raised
            else ""
        )
        if walk["available"]
        else f"; no walk takes ({walk['reason']})"
    )
    return {
        "available": available,
        "reason": reason,
        "bundle_session_id": session.get("bundle_session_id"),
        "round_id": session.get("round_id"),
        "region": region_state,
        "classification": classification,
        "walk": walk,
        "summary": summary,
    }


def _staged_section() -> dict[str, Any]:
    """Whether an instruction is waiting for the next round. The stat, not a peek.

    No packet argument, and that is the point: the spool lives on the speaker
    rather than in any bundle, so a prescription waiting for the next round is
    a fact whichever directory the operator named — including a directory that
    turned out not to be a bundle at all.

    The spool sits under ``/var/lib/jasper/``, which the installer owns
    ``root:jasper`` at 0770, so an operator running this verb as a login user
    outside that group cannot even traverse to the file and gets
    ``PermissionError`` out of the stat — which used to be a traceback and is
    now the fourth section reporting unavailable with its reason, the shape
    the three evidence sections beside it already use. ``pending`` is ``None``
    rather than ``False`` there: "no document is waiting" and "nobody could
    look" are different facts, and a prescriber told the first would stage over a
    document it never saw. The refusal is NOT swallowed at the spool — ``stage``
    still needs the real error — so the catch lives here, in the one verb whose
    contract is a partial answer.
    """
    path = str(prescription_spool_path())
    try:
        pending = staged_prescription_pending()
    except PermissionError:
        return {
            "available": False,
            "pending": None,
            "path": path,
            "reason": SPOOL_UNREADABLE_REASON,
            "summary": (
                f"whether one is waiting is unknown ({SPOOL_UNREADABLE_REASON})"
            ),
        }
    return {
        "available": True,
        "pending": pending,
        "path": path,
        "reason": None,
        "summary": (
            f"one prescription waiting — {STAGED_LIFECYCLE_NOTE}"
            if pending
            else "nothing waiting"
        ),
    }


def _applied_section(
    packet: dict[str, Any] | None, packet_error: str
) -> dict[str, Any]:
    """The packet's two BLEND records — and they answer different questions.

    ``from_round_receipt`` is what the round said it derived from;
    ``from_applied_profile`` is what the speaker is playing now, read from the
    applied-profile SSOT. They should agree, and the packet reports both rather
    than reconciling them.

    The packet keeps a third under ``incumbent.linearization``, the per-driver
    correction each branch carries, and this verb does not surface it yet
    (#2863's follow-up). Named here rather than left as a silent omission: a
    reader would otherwise take these two lines for the whole answer.
    """
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
    readers the gate itself calls — three of them today
    (:func:`~.evidence_packet.packet_region_band_hz`,
    :func:`~.evidence_packet.packet_driver_passbands_hz`,
    :func:`~.evidence_packet.packet_feature_classifications`), with the gate's
    fourth (:func:`~.evidence_packet.packet_incumbent_linearization`) not yet
    called here — plus the spool's own
    :func:`~.prescription_spool.staged_prescription_pending`. **No second
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

    applied_profile = sections["applied"]["from_applied_profile"]
    if not applied_profile["available"]:
        # Keyed on the packet's answer and carrying its reason, on the
        # ``--drivers`` line's rule above rather than the ``--state`` line's
        # below: this is optional evidence whose absence has more than one
        # cause, and "the flag was passed and the file was unreadable" and
        # "the bundle itself could not be read" send an operator somewhere
        # different from "you did not pass it".
        out.append(
            f"no applied profile is available ({applied_profile['reason']}) — "
            "pass --applied-profile <applied baseline profile JSON>; without it "
            "this packet cannot name the correction the graph already carries, "
            "and a per-driver prescription's displacement is unknown"
        )

    if not state_supplied:
        out.append("pass --state <flow state JSON>: `stage` refuses without it")

    staged = sections["staged"]
    if not staged["available"]:
        out.append(
            f"the spool could not be read ({staged['reason']}) — run with sudo "
            "for the full report"
        )
    elif staged["pending"]:
        out.append(f"a prescription is already staged — {STAGED_LIFECYCLE_NOTE}")

    # `seat_level_reference_volume_db()` already fails soft to `None` both
    # when this box never ran the leveling step and when /var/lib/jasper does
    # not exist at all (a laptop checkout) -- the same shape
    # `staged_prescription_pending()` above relies on -- so no on-box guard
    # is needed here. A banked reference adds NO line: the absence of this
    # warning is itself the signal, so a converged box stays uncluttered.
    if seat_level_reference_volume_db() is None:
        out.append(
            "no seat-level measurement reference is banked — measurement "
            f"sessions ride the {MEASUREMENT_REFERENCE_VOLUME_DB:g} dB "
            "main-volume fallback; `jasper-seat-level` sets the seat to the "
            f"default {DEFAULT_TARGET_DB_SPL - DEFAULT_TOLERANCE_DB:g}-"
            f"{DEFAULT_TARGET_DB_SPL + DEFAULT_TOLERANCE_DB:g} dB SPL target "
            "(--target-db-spl states another) and banks the reference"
        )

    out.append(f"run, apply, or undo a round at {crossover_url}")
    return out


#: Tier 0's front door (ADR-0204): the reading order an SSH-only agent lands
#: on before any of the three operator docs. Methodology answers HOW
#: (sequence, traps, thresholds); the runbook answers WHICH TOOL AND HOW TO
#: RUN IT; the doctrine answers WHAT IS ALLOWED and binds the other two. Names
#: only -- `_doc_path` resolves each to wherever it actually is on this box.
_READING_ORDER: tuple[tuple[str, str, str], ...] = (
    ("methodology guide", "tuning-methodology.md",
     "sequence, traps, adjudicated thresholds"),
    ("runbook, per tool", "tuning-operator-runbook.md",
     "tool mechanics, contracts, exit codes"),
    ("doctrine", "measurement-loop-doctrine.md",
     "binds everything: what is allowed, who decides"),
)

#: Where deploy/lib/install/python-runtime.sh's install_jasper() copies the
#: three operator docs. Existence is checked rather than assumed, so a
#: laptop checkout that was never deployed falls back to the repo path
#: instead of pointing an operator at a file that is not there.
_INSTALLED_DOCS_DIR = Path("/opt/jasper/docs")
#: The checkout's own docs/, anchored to this package (the repo root is
#: three parents above this file), never the CWD — status runs from
#: anywhere. Resolves to a nonexistent site-packages sibling under a venv
#: install, which the existence check below treats as any other absence.
_REPO_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def _doc_path(filename: str) -> str:
    """The first of (installed, checkout) that exists, else the bare repo name.

    The last fallback is an identifier, not a location: a box with neither
    directory still gets the doc named in repo-relative spelling rather
    than a path fabricated to look present.
    """
    for candidate in (_INSTALLED_DOCS_DIR / filename, _REPO_DOCS_DIR / filename):
        if candidate.exists():
            return str(candidate)
    return f"docs/{filename}"


def _print_reading_order() -> None:
    """The cold-start front door, printed before anything this verb measures.

    Orientation only -- the doctrine's hard stops are enforced in code
    regardless of whether anyone reads this line (ADR-0204 point 3), so
    there is nothing here to gate and nothing to get wrong by skipping it.
    """
    print("read in order:")
    for n, (label, filename, gives) in enumerate(_READING_ORDER, start=1):
        print(f"  {n}. {label:<18} {_doc_path(filename)}  ({gives})")
    print()


def _print_status(payload: dict[str, Any]) -> None:
    """The front-door reading order, then the report from the section
    summaries rather than a second phrasing of them."""
    _print_reading_order()
    print(f"{'speaker:':9} {payload['speaker']['hostname']}")
    for name in ("declared", "banked", "staged", "applied"):
        print(f"{name + ':':9} {payload[name]['summary']}")
    print("next:")
    for action in payload["next_actions"]:
        print(f"  - {action}")


def status_document(
    packet: dict[str, Any] | None, packet_error: str, *, state_supplied: bool
) -> dict[str, Any]:
    """Where this speaker stands, and what it can do next, as a value.

    Exactly what :func:`_print_status` prints and what ``status --json``
    dumps. The packet is a parameter rather than ``argparse.Namespace`` so a
    caller that already built one can hand it in directly instead of walking
    the bundle a second time.

    **A partial answer beats no answer**, so an unreadable bundle does not stop
    the report: the packet's failure becomes every evidence section's reason,
    and the spool — which lives on the speaker, not in the bundle — is reported
    truthfully regardless. A prescription waiting for the next round is a fact
    about this speaker whichever directory the operator happened to name.
    """
    crossover_url = speaker_url(CROSSOVER_PAGE_PATH)
    declaration_url = speaker_url(SOUND_SETUP_PAGE_PATH)
    sections = _status_sections(packet, packet_error)
    return {
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
            state_supplied=state_supplied,
            crossover_url=crossover_url,
            declaration_url=declaration_url,
        ),
    }


def _cmd_status(args: argparse.Namespace) -> int:
    """Where this speaker stands, and what it can do next. Writes nothing.

    Loads the packet from ``args`` and hands it to :func:`status_document`,
    then prints the result — the exit code still tells a script which of two
    things happened: :data:`EXIT_EVIDENCE_UNREADABLE` when the packet could
    not be built, matching this tool's contract that ``1`` means the evidence
    could not be read. The report prints either way.

    Unlike its three siblings the human report goes to STDOUT. For them stdout
    is reserved for a document a pipe consumes and the human gloss goes to
    stderr so it cannot contaminate it; this verb emits no document unless
    ``--json`` asks for one, and a report whose only copy went to stderr would
    be invisible to the SSH agent that is this verb's main reader.
    """
    packet: dict[str, Any] | None = None
    packet_error = ""
    if args.session_dir is None:
        # The runbook's own step 1 ("Orient"): on a virgin speaker no session
        # dir exists yet, so there is nothing to point this verb at. Every
        # section below already tolerates ``packet=None`` (its own contract,
        # stated above), so this reuses that path rather than inventing a
        # second, partial report shape for the no-session case.
        packet_error = (
            "no session_dir given -- this speaker has no crossover-v2 "
            "session yet"
        )
    else:
        try:
            packet = _load_packet(args)
        except (CrossoverEvidencePacketError, OSError) as exc:
            packet_error = str(exc)

    payload = status_document(packet, packet_error, state_supplied=bool(args.state))

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
    "per-claim verify verdicts or the Fc selection, and says so"
)
_STATE_HELP_REQUIRED = (
    f"{_STATE_HELP}. REQUIRED for this verb: the round a prescription becomes "
    "an instruction for is read from its round receipt, and staging without "
    "one would file the prescription against a series this command cannot see"
)


#: What ``--drivers`` is, and where it points when not given. A blend
#: prescription never needs it, and a per-driver one is refused by name
#: without a readable file here — which is the packet's own honesty rule
#: applied to a second evidence source. Defaulted to the on-speaker path
#: (rather than left ``None``) so an operator running this CLI on the speaker
#: itself does not have to name a file that is already sitting there; a
#: laptop or a speaker that was never commissioned reads it as unavailable,
#: same as before.
_DRIVERS_HELP = (
    "the active-speaker design draft JSON, which carries the confirmed "
    f"driver-safety profile. Defaults to {_DRIVERS_DEFAULT_PATH}. Without a "
    "readable file there, the packet cannot say where each driver's own band "
    "starts and ends, and a per-driver prescription has no bound to be "
    "checked against"
)


#: What ``--applied-profile`` is, defaulted on the same terms as ``--drivers``.
#: NOT interchangeable with ``--state``: what the flow state records about a
#: previous apply is one apply behind the graph whenever a round has applied
#: and arbitrarily behind it whenever a graph was applied through a door that
#: never touches v2 state.
_APPLIED_PROFILE_HELP = (
    "the applied baseline profile JSON — this speaker's record of what it is "
    f"PLAYING. Defaults to {_APPLIED_PROFILE_DEFAULT_PATH}. Without a "
    "readable file there, the packet cannot name the correction the graph "
    "already carries, so a per-driver prescription's displacement is "
    "reported unknown rather than guessed"
)


#: What ``--repeat-floor`` is, defaulted on the same terms as the two above.
#: Without it the accuracy budget reports the repeat floor unmeasured and the
#: stopping thresholds fall back to the codified assumptions, saying so.
_REPEAT_FLOOR_HELP = (
    "the banked repeat floor JSON — this rig's measured touched-nothing "
    f"repeat spread. Defaults to {_REPEAT_FLOOR_DEFAULT_PATH}. Without a "
    "readable file there, the packet's in_capture_repeat_floor reads "
    "unavailable and its plateau/margin are the codified assumptions"
)


#: What ``--packet`` is, and why it exists. The packet a laptop rebuilds is not
#: the packet the speaker emitted — the rebuild resolves ``--drivers``,
#: ``--applied-profile`` and ``--repeat-floor`` against whatever THAT machine
#: has — so the two fingerprint differently and a document answering one is
#: refused against the other. Emitting once and judging against the file
#: removes the second packet rather than teaching anything to re-stamp a
#: fingerprint, which would make the echo worthless as provenance.
_PACKET_HELP = (
    "an evidence packet JSON file (what `packet` emitted), used AS this "
    "round's evidence instead of rebuilding one. Emit the packet ONCE on the "
    "speaker, hand that file to whoever writes the prescription, then judge "
    "the answer against the SAME file: the fingerprint the document echoes "
    "matches by construction and nobody copies one by hand. The rebuild inputs "
    "(the session_dir positional, --drivers, --applied-profile, "
    "--repeat-floor) are refused beside it; `stage` still takes --state, "
    "which it reads for the round ordinal rather than as evidence"
)


def _add_evidence_args(
    parser: argparse.ArgumentParser,
    *,
    state_help: str = _STATE_HELP_OPTIONAL,
    session_dir_optional: bool = False,
    packet_source: bool = False,
) -> None:
    optional_positional = session_dir_optional or packet_source
    parser.add_argument(
        "session_dir",
        nargs="?" if optional_positional else None,
        help=(
            "a commissioning bundle directory (the one holding info.json and "
            "evidence/v1/artifacts/crossover_v2/<relay-session-id>/)"
            + (
                ". Omit on a virgin speaker with no session yet -- status "
                "reports what it can (declared state lives at --drivers / "
                "the design draft) and names the gap"
                if session_dir_optional
                else ""
            )
            + (
                ". Omit it when --packet names the evidence; the two are "
                "exclusive and naming both is refused"
                if packet_source
                else ""
            )
        ),
    )
    # Not `required=True` even for ``stage``: argparse would refuse before the
    # two speaker-level questions are asked, and the command owns a sentence
    # that says WHY the flag matters here. The check lives in `_cmd_stage`.
    parser.add_argument("--state", default=None, help=state_help)
    # `None` at the parser, resolved to the on-Pi path in `_load_packet`: the
    # default is still TRUE (omitting the flag reads it), and keeping it out of
    # the namespace is what lets `_evidence_source_error` tell an operator who
    # named the flag from one who did not.
    parser.add_argument("--drivers", default=None, help=_DRIVERS_HELP)
    parser.add_argument("--applied-profile", default=None, help=_APPLIED_PROFILE_HELP)
    parser.add_argument("--repeat-floor", default=None, help=_REPEAT_FLOOR_HELP)
    if packet_source:
        parser.add_argument("--packet", default=None, help=_PACKET_HELP)
    else:
        # So every verb's namespace answers the question `_load_packet` asks,
        # rather than the loader reaching for an attribute only two verbs have.
        parser.set_defaults(packet=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-crossover-prescriber",
        description=(
            "Emit one crossover round's evidence packet, read a prescription "
            "back through the strict gate, and say where this speaker stands."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - to actually MEASURE anything -- this tool never opens a\n"
            "    session or plays a sound; scripts/run-crossover-round.py or\n"
            "    the guided web flow does that\n"
            "  - to skip propose and go straight to stage -- stage runs the\n"
            "    SAME gate propose does, so skipping propose only delays\n"
            "    finding out about a refusal, it does not avoid the gate\n"
            "\n"
            "EXAMPLE -- emit the packet ONCE, then judge against that file\n"
            "  jasper-crossover-prescriber packet captures/.../session-1 \\\n"
            "      --out packet.json\n"
            "  jasper-crossover-prescriber propose --packet packet.json \\\n"
            "      --prescription my_prescription.json\n"
            "  jasper-crossover-prescriber stage --packet packet.json \\\n"
            "      --prescription my_prescription.json --state flow_state.json\n"
            "\n"
            "  The fingerprint the document echoes is the file's, so it\n"
            "  matches by construction. Rebuilding the packet on another\n"
            "  machine resolves --drivers/--applied-profile/--repeat-floor\n"
            "  against THAT machine and fingerprints differently, which is\n"
            "  what used to send an operator copying a fingerprint across\n"
            "  by hand.\n"
            "\n"
            "EXIT CODES\n"
            "  0  accepted -- status (which accepts nothing) exits 0 once it\n"
            "     read the evidence, even a partial one\n"
            "  1  EXIT_EVIDENCE_UNREADABLE -- the bundle, --state,\n"
            "     --drivers, --applied-profile, or --repeat-floor could not\n"
            "     be read\n"
            "  2  EXIT_REFUSED -- propose's or stage's gate refused the\n"
            "     prescription; \"refused (<reason>): <detail>\" on stderr,\n"
            "     and as JSON with --json\n"
            "  3  EXIT_STAGE_FAILED -- stage's own write to the spool\n"
            "     failed -- a filesystem problem, distinct from a refused\n"
            "     prescription: 2 means fix the prescription, 3 means fix\n"
            "     the speaker's filesystem"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # First, because it is where an operator who has just arrived starts.
    status = sub.add_parser(
        "status",
        help="print declared / banked / staged / applied state and what is next",
    )
    _add_evidence_args(status, session_dir_optional=True)
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
    _add_evidence_args(propose, packet_source=True)
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
    _add_evidence_args(stage, state_help=_STATE_HELP_REQUIRED, packet_source=True)
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
