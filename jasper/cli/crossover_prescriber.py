# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Serve one round's evidence, take a prescription back, and stage it.

``packet`` writes the evidence beside the round, ``propose`` reads an answer
back through the strict gate, ``stage`` runs the SAME gate and banks it,
``status`` only reports. No model client, API key or network lives here. Emit
the packet ONCE and pass that file as ``--packet <file>``; a rebuild
fingerprints differently. ``packet``, ``propose`` and ``stage`` each answer
with one JSON document on stdout and their human line on stderr; exit codes
and the failure record are :mod:`~jasper.cli._refusal`'s.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

from ._logging import CLI_LOG_FORMAT
from ._refusal import (
    EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, failed,
    read_source_bytes,
)
# The beside-the-round output rule, reused rather than restated: a live
# session bundle is daemon-owned, so a view defaulting inside it raises
# PermissionError for the operator (#3498).
from .round_views import default_out

from jasper.active_speaker.crossover_v2.blend_prescription import (
    BLEND_PRESCRIPTION_MALFORMED,
    REGION_UNAVAILABLE,
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
from jasper.active_speaker.crossover_v2.alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
)
from jasper.active_speaker.crossover_v2.topology_prescription import (
    TOPOLOGY_NO_CROSSOVER_REGION,
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
from jasper.active_speaker.crossover_v2.round_inputs import (
    APPLIED_PROFILE_DEFAULT_PATH,
    DECLARED_GEOMETRY_DEFAULT_PATH,
    DRIVERS_DEFAULT_PATH,
    REPEAT_FLOOR_DEFAULT_PATH,
    round_inputs,
)
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL,
    DEFAULT_TOLERANCE_DB,
    seat_level_reference_volume_db,
)
from jasper.active_speaker.session_volume_plan import (
    MEASUREMENT_REFERENCE_VOLUME_DB,
)
from jasper.audio_measurement.program_analysis import (
    ABSOLUTE_NO_CROSSOVER_TOPOLOGY,
)
from jasper.identity import (
    CROSSOVER_PAGE_PATH,
    SOUND_SETUP_PAGE_PATH,
    read_identity,
    speaker_url,
)

#: Authority tier for the generated tool-menu index (ADR-0204).
AUTHORITY_TIER = "advisory (`stage` mutates)"

#: This tool's console-script name, as ``pyproject.toml`` installs it: the
#: parser's own ``prog`` and the ``next`` command every answer prints.
PROG = "jasper-crossover-prescriber"

#: What happens to a document in the spool; ``stage`` and ``status`` both say it.
STAGED_LIFECYCLE_NOTE = "the next round takes it once and consumes it"

#: Why the staged section has nothing to report.
SPOOL_UNREADABLE_REASON = "permission_denied"

#: The slugs this tool publishes for its OWN failures. A gate refusal
#: publishes the gate's own reason instead, which is finer-grained than these.
REASON_EVIDENCE_SOURCE = "evidence_source"
REASON_UNREADABLE = "evidence_unreadable"
REASON_UNWRITABLE = "output_unwritable"


def _answer(document: dict[str, Any]) -> int:
    """One verb's answer, printed the one way every tuning tool prints one.

    stdout carries exactly this document and nothing else, so a reader parses
    stdout rather than scraping the human line on stderr.
    """
    print(json.dumps(document, indent=2, sort_keys=True))
    return EXIT_OK


def _read_packet_file(path: Path) -> dict[str, Any]:
    """One already-emitted packet, read as the evidence rather than rebuilt.

    A rebuild on another machine resolves the flags against what THAT machine
    has and so fingerprints differently. ``OSError`` is deliberately not
    caught: the caller maps it to the unreadable-evidence exit.
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
    """The packet, as a value, separate from every verb's printing.

    ``--packet`` short-circuits the build. Which shape the positional is, and
    where the design draft and applied profile therefore live, is
    :func:`~jasper.active_speaker.crossover_v2.round_inputs.round_inputs`'
    answer, resolved there rather than at the argparse default so "the operator
    passed this flag" stays answerable.
    """
    if args.packet:
        return _read_packet_file(Path(args.packet))
    inputs = round_inputs(Path(args.session_dir))
    return build_crossover_evidence_packet(
        inputs.session_dir,
        # No default for the flow state: the web host rewrites it as a round
        # runs, so a defaulted state would move the packet's fingerprint.
        state_path=Path(args.state) if args.state else None,
        driver_draft_path=(
            Path(args.drivers) if args.drivers else inputs.design_draft_path
        ),
        applied_profile_path=(
            Path(args.applied_profile)
            if args.applied_profile
            else inputs.applied_profile_path
        ),
        repeat_floor_path=(
            Path(args.repeat_floor) if args.repeat_floor else inputs.repeat_floor_path
        ),
        declared_geometry_path=(
            Path(args.declared_geometry)
            if args.declared_geometry
            else inputs.declared_geometry_path
        ),
    )


#: Flags that exist ONLY to feed a rebuild, and are refused beside ``--packet``.
#: ``--state`` is not among them: ``stage`` reads it for the round ordinal.
_REBUILD_ONLY_FLAGS: tuple[tuple[str, str], ...] = (
    ("--drivers", "drivers"),
    ("--applied-profile", "applied_profile"),
    ("--repeat-floor", "repeat_floor"),
    ("--declared-geometry", "declared_geometry"),
)


def _evidence_source_error(args: argparse.Namespace) -> str | None:
    """ONE evidence source per invocation, or the sentence that says why not.

    ``--state`` is the exception, and only on ``stage``: that verb reads it for
    the round ordinal and hard-refuses without it.
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


#: What ``packet`` writes when no ``--out`` names somewhere else. Deliberately
#: NOT a :data:`~jasper.cli.round_views.ARTIFACT_BY_VIEW` row: that table is
#: keyed by ``jasper-round-views`` subcommand, and its ``inventory`` verb
#: renders every key as that tool's own producer.
PACKET_ARTIFACT = "packet.json"


def _cmd_packet(args: argparse.Namespace) -> int:
    """Write one round's evidence packet beside it and summarise what landed.

    The document is a file rather than a stream because every downstream verb
    takes it as ``--packet <file>``: a second build fingerprints differently,
    so the emitted copy is the evidence.
    """
    try:
        packet = _load_packet(args)
        blob = json.dumps(
            packet, indent=None if args.compact else 2, sort_keys=True
        ) + "\n"
        round_dir = Path(args.session_dir)
        out = (
            Path(args.out)
            if args.out
            else default_out(round_inputs(round_dir), round_dir, PACKET_ARTIFACT)
        )
    except (CrossoverEvidencePacketError, OSError) as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    try:
        out.write_text(blob)
        size_bytes = out.stat().st_size
    except OSError as exc:
        # The evidence READ; only the filing failed, which is a different
        # place to send the operator than an unreadable round.
        return failed(
            EXIT_WRITE_FAILED, REASON_UNWRITABLE, f"could not write {out}: {exc}"
        )
    summary = _packet_summary(packet, out, size_bytes)
    print(
        f"packet {(summary['packet_fingerprint'] or '')[:16]} "
        f"round={summary['round_id']} -> {out} ({summary['bytes']} bytes)",
        file=sys.stderr,
    )
    return _answer(summary)


def _packet_summary(
    packet: dict[str, Any], artifact: Path, size_bytes: int
) -> dict[str, Any]:
    """The document reduced to what a reader needs before opening it.

    Availability is read off the packet's own per-block ``available`` flags, so
    a block added to the builder reaches this summary with no edit here. No
    curve is ever named: the arrays live in the artifact.
    """
    return {
        "out": str(artifact),
        "bytes": size_bytes,
        "packet_fingerprint": packet.get("packet_fingerprint"),
        "round_id": (packet.get("session") or {}).get("round_id"),
        "blocks": {
            name: bool(block.get("available"))
            for name, block in sorted(packet.items())
            if isinstance(block, dict) and "available" in block
        },
        "not_evaluated": [
            entry.get("field") for entry in packet.get("not_evaluated") or []
        ],
        "trim": (packet.get("incumbent") or {}).get("trim") or {},
    }


def _gate(
    args: argparse.Namespace,
) -> tuple[
    bytes,
    BlendPrescription | DriverPrescription,
    dict[str, Any],
    tuple[FeatureVerdict, ...] | None,
]:
    """The document, the validated prescription, what it becomes, what judged it.

    Shared WHOLE by ``propose`` and ``stage``, which is what makes the first a
    true dry run of the second. The document's own ``kind`` picks the gate.
    Raises ``BlendPrescriptionRefused`` (``EXIT_REFUSED``) or
    ``CrossoverEvidencePacketError``/``OSError`` (``EXIT_UNREADABLE``).
    """
    packet = _load_packet(args)
    payload = read_source_bytes(args.prescription)
    document = read_prescription_bytes(payload)
    prescription: BlendPrescription | DriverPrescription | None
    classifications: tuple[FeatureVerdict, ...] | None = None
    if document.get("kind") == DRIVER_PRESCRIPTION_KIND:
        # The class's own size bound, applied the moment the class is known.
        check_driver_document_size(payload)
        classifications = packet_feature_classifications(packet)
        prescription = read_driver_prescription(
            document,
            packet_fingerprint=packet.get("packet_fingerprint"),
            passbands_hz=packet_driver_passbands_hz(packet),
            classifications=classifications,
            incumbent_filters=packet_incumbent_linearization(packet),
        )
        # `fitted=None` is deliberate: at propose/stage time no per-driver fit
        # exists yet. The merge happens when a round builds its candidate.
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
        # Unreachable today. A branch rather than an `assert` because `python -O`
        # strips asserts and a stripped narrowing would raise AttributeError.
        raise BlendPrescriptionRefused(
            BLEND_PRESCRIPTION_MALFORMED, "the prescription document was empty"
        )
    # Candidate fields are computed INSIDE the gate above, because each seam
    # re-asks its own route and can refuse with the contract's exit code.
    return payload, prescription, candidate_fields, classifications


#: What ``propose`` writes when no ``--out`` names somewhere else: the accepted
#: result, beside the packet it was judged against. NOT the prescription
#: document itself, which is the operator's own file and what
#: ``stage --prescription`` reads.
PROPOSAL_ARTIFACT = "proposal.json"


def _gate_refusal(exc: BlendPrescriptionRefused) -> int:
    """The gate's verdict as this tool's refusal, under the gate's own reason.

    The verdict alone when the gate measured nothing to show for it; the
    verdict and its evidence together when it did, because that evidence is
    what lets a prescriber correct the document rather than guess.
    """
    detail: Any = exc.detail
    if exc.evidence:
        detail = {"verdict": exc.detail, "evidence": dict(exc.evidence)}
    return failed(EXIT_REFUSED, exc.reason, detail)


def _admitted(
    prescription: BlendPrescription | DriverPrescription,
    candidate_fields: dict[str, Any],
    payload: bytes,
    out: Path,
    size_bytes: int,
) -> dict[str, Any]:
    """What was admitted and where the whole result landed. Scalars only.

    The filters, their evidence and the candidate fields' VALUES are in the
    artifact ``out`` names; a reader that needs them opens it.
    """
    return {
        "accepted": True,
        "prescription_class": prescription.prescription_class,
        "n_filters": len(prescription.filters),
        "scope": _scope(prescription),
        "candidate_fields": sorted(candidate_fields),
        "prescription_sha256": prescription_sha256(payload),
        "out": str(out),
        "bytes": size_bytes,
    }


def _stage_command(args: argparse.Namespace) -> str:
    """The ``stage`` invocation for this evidence, with the paths in hand.

    Every rebuild input comes along: a rebuild missing one resolves that flag
    against the machine instead and fingerprints differently, which is the
    mismatch the printed command exists to avoid. ``--state`` is the one input
    ``propose`` does not need and ``stage`` refuses without, so an operator who
    named none is handed the placeholder rather than a command that cannot run.
    """
    evidence = ["--packet", args.packet] if args.packet else [
        str(args.session_dir),
        *(
            word
            for flag, dest in _REBUILD_ONLY_FLAGS
            if getattr(args, dest)
            for word in (flag, getattr(args, dest))
        ),
    ]
    return shlex.join([
        PROG, "stage", *evidence,
        "--prescription", args.prescription,
        "--state", args.state or "<flow state JSON>",
    ])


def _cmd_propose(args: argparse.Namespace) -> int:
    """Read a prescription back through the gate, and say what it becomes."""
    source_error = _evidence_source_error(args)
    if source_error is not None:
        return failed(EXIT_UNREADABLE, REASON_EVIDENCE_SOURCE, source_error)
    try:
        payload, prescription, candidate_fields, _ = _gate(args)
    except (CrossoverEvidencePacketError, OSError) as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    except BlendPrescriptionRefused as exc:
        return _gate_refusal(exc)

    blob = json.dumps(
        {
            "accepted": True,
            "prescription": prescription.to_dict(),
            "prescription_sha256": prescription_sha256(payload),
            "candidate_fields": candidate_fields,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    out = Path(args.out) if args.out else _proposal_out(args)
    try:
        out.write_text(blob)
        size_bytes = out.stat().st_size
    except OSError as exc:
        return failed(
            EXIT_WRITE_FAILED, REASON_UNWRITABLE, f"could not write {out}: {exc}"
        )
    _print_prescription(prescription, "accepted")
    return _answer({
        **_admitted(prescription, candidate_fields, payload, out, size_bytes),
        "next": _stage_command(args),
    })


def _proposal_out(args: argparse.Namespace) -> Path:
    """Beside the packet this document was judged against.

    A ``--packet`` file is already somewhere the operator can write; a rebuild
    lands where ``packet`` itself would have, so the round's own artifacts stay
    together and a live daemon-owned bundle is not written into.
    """
    if args.packet:
        return Path(args.packet).parent / PROPOSAL_ARTIFACT
    round_dir = Path(args.session_dir)
    return default_out(round_inputs(round_dir), round_dir, PROPOSAL_ARTIFACT)


def _band_phrase(lo: float, hi: float) -> str:
    """One frequency span, spelled the one way this tool spells it."""
    return f"{lo:.1f}-{hi:.1f} Hz"


def _passband_phrase(role: str, lo: float, hi: float) -> str:
    """One role's declared band, to whole hertz.

    A manufacturer figure; a tenth would suggest precision it does not have.
    """
    return f"{role} {lo:.0f}-{hi:.0f} Hz"


def _scope(prescription: BlendPrescription | DriverPrescription) -> str:
    """What this prescription's filters were bounded BY, in one phrase."""
    if isinstance(prescription, DriverPrescription):
        return ", ".join(
            _passband_phrase(role, lo, hi)
            for role, lo, hi in prescription.passbands_hz
            if role in prescription.roles
        )
    return _band_phrase(prescription.band_hz[0], prescription.band_hz[1])


def _displaced_phrase(prescription: DriverPrescription) -> str:
    """What staging this document deletes, in one line, or that nobody knows.

    Reports and never refuses.
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

    Reports and never refuses: see
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
    # filter list names the RIGHT filters — the receipt's own basis key.
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

    ``stage``'s "for round N" wording is read by a real-subprocess test proving
    the CLI's logging configuration did not swallow the operator's output. The
    per-driver class gets two more lines after the filters — what the document
    deletes, and which filters a banked verdict backs — both disclosures the
    gate makes rather than bounds it applies.
    """
    print(
        f"{verb} {prescription.prescription_class} prescription{qualifier}: "
        f"{len(prescription.filters)} filter(s) over {_scope(prescription)}",
        file=sys.stderr,
    )
    for entry in prescription.filters:
        role = f"{entry['role']} " if "role" in entry else ""
        # The entry's OWN type, and its Q only when the type has one: a
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
        # Only when there IS one, unlike the two lines below. Printed first
        # because it is the one thing here that moves a LEVEL rather than a
        # shape.
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

    Through ``series_position_from_state``, so the ordinal this stamps and the
    ordinal the round checks it against are one function reading one key.
    ``--state`` is REQUIRED and the caller enforces it: without it that reader
    resolves every unreadable shape to the first round.
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
        return failed(EXIT_UNREADABLE, REASON_EVIDENCE_SOURCE, source_error)
    if not args.state:
        return failed(
            EXIT_UNREADABLE,
            REASON_EVIDENCE_SOURCE,
            "--state is required to stage a prescription; the round it becomes "
            "an instruction for is read from the flow state's round receipt, "
            "and staging without one would file it against a series this "
            "command cannot see",
        )
    try:
        payload, prescription, candidate_fields, classifications = _gate(args)
        ordinal = _next_round_ordinal(args.state)
    except BlendPrescriptionRefused as exc:
        # FIRST. Every exception this handler names is a ``ValueError``
        # subclass, so an arm widened to ``except ValueError`` below would
        # report every refused prescription as an unreadable input — exit 2
        # with no reason slug.
        return _gate_refusal(exc)
    except (
        CrossoverEvidencePacketError, OSError, UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        # The last two are the state file's own failure modes: it is read here
        # rather than by the packet builder.
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))

    try:
        path = stage_prescription(
            payload,
            prescription,
            for_round_ordinal=ordinal,
            classifications=classifications,
        )
        size_bytes = path.stat().st_size
    except OSError as exc:
        return failed(
            EXIT_WRITE_FAILED,
            REASON_UNWRITABLE,
            f"could not stage the prescription: {exc}",
        )

    _print_prescription(prescription, "staged", qualifier=f" for round {ordinal}")
    print(f"  {path}", file=sys.stderr)
    print(f"  {STAGED_LIFECYCLE_NOTE}", file=sys.stderr)
    return _answer({
        **_admitted(prescription, candidate_fields, payload, path, size_bytes),
        "staged": True,
        "for_round_ordinal": ordinal,
    })


def _block(packet: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """One of the packet's own blocks, or an empty one when there is no packet."""
    block = (packet or {}).get(name)
    return block if isinstance(block, dict) else {}


def _reason(block: dict[str, Any], packet_error: str) -> str:
    """Why a section has nothing to report, from whichever layer knows.

    The packet builder's failure wins when there is one; below that, the
    block's own ``_absence`` reason, passed through untranslated. "not
    reported" only when a block says unavailable and names no reason.
    """
    if packet_error:
        return packet_error
    reason = block.get("reason")
    return str(reason) if reason else "not reported"


def _incumbent_record(value: Any, packet_error: str) -> dict[str, Any]:
    """One side of the packet's incumbent block, classified but not reconciled.

    The packet makes no judgement between its two records, so neither does
    this. An empty list is ``available`` with zero filters: "the round recorded
    an empty incumbent" and "no receipt was readable" are the two facts a
    prescription author most needs kept apart, because a prescription is a
    TOTAL. The reason is echoed only from the absence shape the packet builder
    writes, so a receipt whose ``incumbent`` is some other object cannot print
    that object's ``reason`` key as though the builder had explained something.
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
    per-driver prescription, so asking it here asks the question the door will.
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

    The region and the classified features ride inside the banked section: they
    are facts about this round's evidence and absent for the same reasons the
    round is. ``walk`` is the exception — ``lateral_poses`` is filled by
    ACCEPTED takes while ``available`` needs a ``round_receipt.json``, so a
    measurement-only angle walk banks poses and no receipt.
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
    # "0 deg" is not a raise worth a clause.
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

    No packet argument: the spool lives on the speaker rather than in any
    bundle. The spool sits under ``/var/lib/jasper/`` at root:jasper 0770, so
    an operator outside that group gets ``PermissionError`` from the stat and
    this section reports unavailable with that reason. ``pending`` is ``None``
    rather than ``False`` there — "no document is waiting" and "nobody could
    look" are different facts. ``stage`` still sees the real error.
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
    ``from_applied_profile`` is what the speaker is playing now. They should
    agree, and the packet reports both rather than reconciling them. The third,
    ``incumbent.linearization``, is not surfaced here yet (#2863 follow-up).
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

    Every fact comes from
    :func:`~.evidence_packet.build_crossover_evidence_packet` and the named
    readers the gate itself calls, plus the spool's own
    :func:`~.prescription_spool.staged_prescription_pending`. No second walk of
    the bundle. Every packet reader tolerates ``None``, so an unreadable bundle
    needs no special case. Each section's ``summary`` is the SAME sentence the
    human report prints, so ``--json`` and terminal readers agree.
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
    """What this speaker can do next, derived from what it has and has not.

    Artifact dependencies, not a workflow: each line is the consequence of one
    artifact being present or absent, and names the tool that would refuse for
    want of it. Nothing here sequences anything — the refusals do that.
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
        elif region["reason"] == ABSOLUTE_NO_CROSSOVER_TOPOLOGY:
            out.append(
                "this speaker has no crossover region, so the blend, alignment, "
                "topology doors do not apply and refuse by name "
                f"({REGION_UNAVAILABLE}, {ALIGNMENT_NO_CROSSOVER_REGION}, "
                f"{TOPOLOGY_NO_CROSSOVER_REGION}) — the per-driver door below "
                "is the whole loop here"
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
            # operator may not be able to act on either alone.
            out.append(
                f"no declared driver band is available ({declared['reason']}) — "
                "pass --drivers <design draft JSON>, or declare the drivers at "
                f"{declaration_url}; without it a per-driver prescription has "
                "no bound and is refused by name"
            )
        else:
            # "readable", not "banked": this arm also covers an artifact that
            # WAS banked and whose every row the typed reader dropped. The
            # action is the same either way, and the reason tells them apart.
            out.append(
                "no readable feature classification for this round "
                f"({classification['reason']}) — run "
                "`jasper-round-views classify-features`; "
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
        # Keyed on the packet's answer and carrying its reason: this is optional
        # evidence whose absence has more than one cause, and "unreadable file"
        # sends an operator somewhere different from "you did not pass it".
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

    # `seat_level_reference_volume_db()` already fails soft to `None` both when
    # this box never ran the leveling step and when /var/lib/jasper does not
    # exist at all. A banked reference adds NO line: the absence of this warning
    # is itself the signal.
    if seat_level_reference_volume_db() is None:
        out.append(
            "no seat-level measurement reference is banked — measurement "
            f"sessions ride the {MEASUREMENT_REFERENCE_VOLUME_DB:g} dB "
            "main-volume fallback; `jasper-seat-level` sets the seat to the "
            f"default {DEFAULT_TARGET_DB_SPL - DEFAULT_TOLERANCE_DB:g}-"
            f"{DEFAULT_TARGET_DB_SPL + DEFAULT_TOLERANCE_DB:g} dB SPL target "
            "(--target-db-spl states another) and banks the reference"
        )

    out.append(f"run or apply a round at {crossover_url}")
    return out


#: Tier 0's front door (ADR-0204): the reading order an SSH-only agent lands on
#: before any of the three operator docs. Names only — `_doc_path` resolves
#: each to wherever it actually is on this box.
_READING_ORDER: tuple[tuple[str, str, str], ...] = (
    ("methodology guide", "tuning-methodology.md",
     "sequence, traps, adjudicated thresholds"),
    ("runbook, per tool", "tuning-operator-runbook.md",
     "tool mechanics, contracts, exit codes"),
    ("doctrine", "measurement-loop-doctrine.md",
     "binds everything: what is allowed, who decides"),
)

#: Where deploy/lib/install/python-runtime.sh's install_jasper() copies the
#: three operator docs. Existence is checked rather than assumed.
_INSTALLED_DOCS_DIR = Path("/opt/jasper/docs")
#: The checkout's own docs/, anchored to this package rather than the CWD.
#: Resolves to a nonexistent site-packages sibling under a venv install, which
#: the existence check below treats as any other absence.
_REPO_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def _doc_path(filename: str) -> str:
    """The first of (installed, checkout) that exists, else the bare repo name.

    The last fallback is an identifier, not a location.
    """
    for candidate in (_INSTALLED_DOCS_DIR / filename, _REPO_DOCS_DIR / filename):
        if candidate.exists():
            return str(candidate)
    return f"docs/{filename}"


def _print_reading_order() -> None:
    """The cold-start front door, printed before anything this verb measures.

    Orientation only — the doctrine's hard stops are enforced in code
    regardless of whether anyone reads this line (ADR-0204 point 3).
    """
    print("read in order:")
    for n, (label, filename, gives) in enumerate(_READING_ORDER, start=1):
        print(f"  {n}. {label:<18} {_doc_path(filename)}  ({gives})")
    print()


def _print_status(payload: dict[str, Any]) -> None:
    """The reading order, then the report from the section summaries."""
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

    Exactly what :func:`_print_status` prints and what ``status --json`` dumps.
    The packet is a parameter rather than an ``argparse.Namespace`` so a caller
    that already built one need not walk the bundle again. An unreadable bundle
    does not stop the report: the packet's failure becomes every evidence
    section's reason, and the spool is reported truthfully regardless.
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

    The report prints either way; the exit code still says which of two things
    happened, :data:`EXIT_UNREADABLE` when the packet could not be
    built. Unlike its three siblings the human report goes to STDOUT: this verb
    emits no document unless ``--json`` asks for one, and a report whose only
    copy went to stderr would be invisible to the SSH agent reading it.
    """
    packet: dict[str, Any] | None = None
    packet_error = ""
    if args.session_dir is None:
        # On a virgin speaker no session dir exists yet. Every section below
        # already tolerates ``packet=None``, so this reuses that path rather
        # than inventing a second report shape.
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
    return EXIT_UNREADABLE if packet_error else EXIT_OK


#: What ``--state`` is, said once. The verbs differ only in whether they can
#: proceed without it — ``stage`` cannot, the other three degrade and say so —
#: so each verb appends its own requirement to this sentence.
_STATE_HELP = (
    "the crossover-v2 flow state JSON, banked separately from the bundle"
)
_STATE_HELP_OPTIONAL = (
    f"{_STATE_HELP}. Optional and NOT defaulted; without it the packet cannot "
    "carry the per-claim verify verdicts or the Fc selection, and says so"
)
_STATE_HELP_REQUIRED = (
    f"{_STATE_HELP}. REQUIRED for this verb: the round a prescription becomes "
    "an instruction for is read from its round receipt, and staging without "
    "one would file the prescription against a series this command cannot see"
)


#: What ``--drivers`` is, and where it points when not given. Defaulted rather
#: than left ``None`` so an operator on the speaker itself need not name a file
#: already sitting there; a laptop reads it as unavailable.
_DRIVERS_HELP = (
    "the active-speaker design draft JSON, which carries the confirmed "
    "driver-safety profile. Defaults to the round's own banked copy, or "
    f"{DRIVERS_DEFAULT_PATH} for a live session directory. Without a "
    "readable file there, the packet cannot say where each driver's own band "
    "starts and ends, and a per-driver prescription has no bound to be "
    "checked against"
)


#: What ``--applied-profile`` is, defaulted on the same terms as ``--drivers``.
#: NOT interchangeable with ``--state``: what the flow state records about a
#: previous apply is at least one apply behind the graph.
_APPLIED_PROFILE_HELP = (
    "the applied baseline profile JSON — this speaker's record of what it is "
    "PLAYING. Defaults to the round's own banked copy, or "
    f"{APPLIED_PROFILE_DEFAULT_PATH} for a live session directory. Without a "
    "readable file there, the packet cannot name the correction the graph "
    "already carries, so a per-driver prescription's displacement is "
    "reported unknown rather than guessed"
)


#: What ``--repeat-floor`` is, defaulted on the same terms as the two above.
#: Without it the accuracy budget reports the repeat floor unmeasured.
_REPEAT_FLOOR_HELP = (
    "the banked repeat floor JSON — this rig's measured touched-nothing "
    "repeat spread. Defaults to the round's own banked copy, or "
    f"{REPEAT_FLOOR_DEFAULT_PATH} for a live session directory. Without a "
    "readable file there, the packet's in_capture_repeat_floor reads "
    "unavailable and its plateau/margin are the codified assumptions"
)


#: What ``--declared-geometry`` is, defaulted on the same terms as the three
#: above. Without it the packet reports no room.
_DECLARED_GEOMETRY_HELP = (
    "the household's declared rig geometry JSON — the speaker/mic heights and "
    "distance `jasper-declare-geometry set` stores. Defaults to the round's "
    f"own banked copy, or {DECLARED_GEOMETRY_DEFAULT_PATH} for a live session "
    "directory. Without a readable file there, the packet's "
    "session.declared_geometry names the absence and the room's entanglement "
    "floor stays unknown"
)


#: What ``--packet`` is, and why it exists: a packet a laptop rebuilds resolves
#: the four evidence flags against whatever THAT machine has, so the two
#: fingerprint differently and a document answering one is refused against the
#: other. Nothing here re-stamps a fingerprint.
_PACKET_HELP = (
    "an evidence packet JSON file (what `packet` emitted), used AS this "
    "round's evidence instead of rebuilding one. Emit the packet ONCE on the "
    "speaker, hand that file to whoever writes the prescription, then judge "
    "the answer against the SAME file: the fingerprint the document echoes "
    "matches by construction and nobody copies one by hand. The rebuild inputs "
    "(the session_dir positional, --drivers, --applied-profile, "
    "--repeat-floor, --declared-geometry) are refused beside it; `stage` "
    "still takes --state, "
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
            "evidence/v1/artifacts/crossover_v2/<capture-session-id>/), or a "
            "banked round tree holding one"
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
    # two speaker-level questions are asked. The check lives in `_cmd_stage`.
    parser.add_argument("--state", default=None, help=state_help)
    # `None` at the parser, resolved to the on-Pi path in `_load_packet`:
    # keeping it out of the namespace is what lets `_evidence_source_error`
    # tell an operator who named the flag from one who did not.
    parser.add_argument("--drivers", default=None, help=_DRIVERS_HELP)
    parser.add_argument("--applied-profile", default=None, help=_APPLIED_PROFILE_HELP)
    parser.add_argument("--repeat-floor", default=None, help=_REPEAT_FLOOR_HELP)
    parser.add_argument(
        "--declared-geometry", default=None, help=_DECLARED_GEOMETRY_HELP
    )
    if packet_source:
        parser.add_argument("--packet", default=None, help=_PACKET_HELP)
    else:
        # So every verb's namespace answers the question `_load_packet` asks.
        parser.set_defaults(packet=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
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
            "  jasper-crossover-prescriber packet rounds/round-3\n"
            "      # writes rounds/round-3/packet.json and prints the path\n"
            "  jasper-crossover-prescriber propose \\\n"
            "      --packet rounds/round-3/packet.json \\\n"
            "      --prescription my_prescription.json\n"
            "  jasper-crossover-prescriber stage \\\n"
            "      --packet rounds/round-3/packet.json \\\n"
            "      --prescription my_prescription.json --state flow_state.json\n"
            "\n"
            "  The fingerprint the document echoes is the file's, so it\n"
            "  matches by construction. Rebuilding the packet on another\n"
            "  machine resolves --drivers/--applied-profile/--repeat-floor/\n"
            "  --declared-geometry\n"
            "  against THAT machine and fingerprints differently, which is\n"
            "  what used to send an operator copying a fingerprint across\n"
            "  by hand.\n"
            "\n"
            "EXIT CODES\n"
            "  0  accepted -- status (which accepts nothing) exits 0 once it\n"
            "     read the evidence, even a partial one\n"
            "  1  EXIT_REFUSED -- propose's or stage's gate refused the\n"
            "     prescription; \"refused (<reason>): <detail>\" on stderr,\n"
            "     and the same record as JSON on stdout\n"
            "  2  EXIT_UNREADABLE -- the bundle, --state, --drivers,\n"
            "     --applied-profile, --repeat-floor or --declared-geometry\n"
            "     could not be read\n"
            "  3  EXIT_WRITE_FAILED -- packet's or stage's own write failed\n"
            "     -- a filesystem problem, distinct from a refused\n"
            "     prescription: 1 means fix the prescription, 3 means fix\n"
            "     the speaker's filesystem"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
        help=(
            f"write one round's evidence packet to {PACKET_ARTIFACT} beside "
            "it and summarise what landed"
        ),
    )
    _add_evidence_args(packet)
    packet.add_argument(
        "--out",
        default=None,
        help=(
            f"a PATH to write the packet to instead of {PACKET_ARTIFACT} "
            "beside the round (a live session bundle is daemon-owned, so its "
            "default lands in the current directory instead). No `-` stdout "
            "shorthand: a whole packet on a terminal is what the default "
            "artifact exists to stop"
        ),
    )
    packet.add_argument(
        "--compact", action="store_true", help="emit the packet without indentation"
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
    propose.add_argument(
        "--out",
        default=None,
        help=(
            f"a PATH for the accepted result instead of {PROPOSAL_ARTIFACT} "
            "beside the packet it was judged against"
        ),
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
    stage.set_defaults(func=_cmd_stage)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Without this the tool's structured events have no handler at all:
    # ``logging.lastResort`` emits WARNING and above, so
    # ``event=crossover_v2.prescription_staged`` (INFO) reached neither an
    # operator's terminal nor the journal, leaving the one state transition
    # this CLI performs unobservable. Deliberately NOT
    # ``_logging.configure_verbose_logging``, which floors at WARNING without a
    # ``--verbose`` flag; its FORMAT is reused. In ``main`` rather than at
    # import, because configuring the root logger on import imposes that choice
    # on every importer.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
