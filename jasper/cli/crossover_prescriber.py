# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Serve one round's evidence, take a prescription back, and stage it.

``packet`` writes the evidence beside the round, ``propose`` reads an answer
back through the strict gate, ``stage`` runs the SAME gate and banks it,
``status`` only reports. No model client, API key or network lives here. Emit
the packet ONCE and pass that file as ``--packet <file>``; a rebuild
fingerprints differently. Every verb answers with one JSON document on stdout
and its human line on stderr; exit codes and the failure record are
:mod:`~jasper.cli._refusal`'s, and ``status`` — which accepts nothing and
refuses nothing — always exits 0, reporting what it could not read as a field.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._logging import CLI_LOG_FORMAT
from ._refusal import (
    EXIT_OK as EXIT_OK,
    EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed,
    read_json_source, read_source_bytes,
)
# The beside-the-round output rule, reused rather than restated: a live
# session bundle is daemon-owned, so a view defaulting inside it raises
# PermissionError for the operator (#3498).
from .round_views import default_out
from .round_views._common import ARTIFACT_BY_VIEW, context_artifacts

from jasper.active_speaker.candidate_bank import (
    CandidateBankRefusal,
    banked_candidates,
    find_banked_candidate,
    publish_authored_candidate,
)
from jasper.active_speaker.baseline_profile import (
    load_applied_baseline_profile_state,
)
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile, compose_candidate
from jasper.output_topology import load_output_topology_strict
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
from jasper.audio_measurement.bundles import BundleError
from jasper.active_speaker.crossover_declaration import preset_crossover_geometry
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
    validate_packet,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    FeatureVerdict,
)
from jasper.active_speaker.crossover_v2.prescription_contract import (
    CONTRACT_COMMAND, SECTIONS, contract_json, prescription_contracts,
)
from jasper.active_speaker.crossover_v2.prescription_spool import (
    prescription_spool_path,
    stage_prescription,
    staged_prescription_pending,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    LAYOUT_UNAVAILABLE,
    ROOM_MEDIAN_UNAVAILABLE,
    ROOM_PRESCRIPTION_KIND,
    RoomMedian,
    RoomPrescription,
    RoomPrescriptionRefused,
    read_room_median,
    read_room_prescription,
    room_prescription_to_candidate_fields,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    APPLIED_PROFILE_DEFAULT_PATH,
    DECLARED_GEOMETRY_DEFAULT_PATH,
    DRIVERS_DEFAULT_PATH,
    REPEAT_FLOOR_DEFAULT_PATH,
    banked_round_of,
    recent_round_sessions,
    round_artifact_dir, round_inputs, contract_sources,
)
from jasper.active_speaker.profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    SIDES_BY_LAYOUT,
)
from jasper.active_speaker.seat_level_reference import (
    seat_level_reference_volume_db,
)
from jasper.active_speaker.state_paths import baseline_profile_state_path
# One owner for how this tool is spelled under sudo: an SSH session gets no
# EnvironmentFile and /opt/jasper/.venv is not on the default PATH.
from jasper.active_speaker.tuning_handoff import ORIENTATION_COMMAND
from jasper.identity.reader import (
    CROSSOVER_PAGE_PATH,
    SPEAKER_SETUP_PAGE_PATH,
    read_identity,
    speaker_url,
)

AUTHORITY_TIER = "advisory (`packet`/`propose`/`compose` save artifacts; `stage` writes pending state; `status` reads)"

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
#: Why a room prescription cannot be staged: the spool carries what the next
#: ROUND applies, and a room set is not that -- it becomes a candidate.
ROOM_NOT_STAGEABLE = "room_prescription_not_stageable"


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
    return validate_packet(packet)


def _cmd_compose(args: argparse.Namespace) -> int:
    if bool(args.room_prescription) != bool(args.room_median):
        return failed(
            EXIT_UNREADABLE,
            REASON_EVIDENCE_SOURCE,
            "--room-prescription and --room-median go together: the document "
            "is judged against the median it echoes, and neither half alone "
            "is evidence",
        )
    root = Path(args.root) if args.root else None
    try:
        bass_fields = (
            {"bass_extension": json.loads(read_source_bytes(args.bass_extension_json))}
            if args.bass_extension_json else {}
        )
        if args.base == "saved":
            saved = candidate_from_applied_profile(
                load_output_topology_strict(), load_applied_baseline_profile_state() or {},
            )
            try:
                base = publish_authored_candidate(saved, root=root)
            except (OSError, BundleError) as exc:
                return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
        else:
            base = find_banked_candidate(args.base, root=root)
        # The base's own preset is the layout: a composed child carries it,
        # so its room set must be keyed by the sides that preset declares.
        room_fields, room_sha256 = _composed_room(
            args, SIDES_BY_LAYOUT[base.candidate.source_preset.channel_map.layout],
        )
        sources = {}
        for value in args.role:
            role, separator, fingerprint = value.partition("=")
            if not separator or not role or not fingerprint or role in sources:
                raise CandidateBankRefusal("composition_role_invalid", "use one ROLE=FINGERPRINT per role")
            sources[role] = find_banked_candidate(fingerprint, root=root)
        candidate = compose_candidate(
            base, sources,
            alignment=find_banked_candidate(args.alignment, root=root) if args.alignment else None,
            blend=find_banked_candidate(args.blend, root=root) if args.blend else None,
            expected_effect=args.expected_effect,
            observation_refs=args.observation_ref,
            rationale=args.rationale,
            room_correction=room_fields.get("room_correction"),
            room_prescription_sha256=room_sha256,
            room_measured_basis=room_fields.get("measured_basis"),
            **bass_fields,
        )
    except RoomPrescriptionRefused as exc:
        return _gate_refusal(exc)
    except (CrossoverEvidencePacketError, OSError) as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        return failed(EXIT_REFUSED, exc.code, exc.detail)
    except (ValueError, TypeError, KeyError) as exc:
        return failed(EXIT_REFUSED, "composition_invalid", str(exc))
    try:
        published = publish_authored_candidate(candidate, root=root)
    except CandidateBankRefusal as exc:
        return failed(EXIT_REFUSED, exc.code, exc.detail)
    except (OSError, BundleError) as exc:
        return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
    return answered({
        "candidate_fingerprint": published.fingerprint,
        "out": str(published.path),
        "measurement_status": "unmeasured",
        "adopted": False,
        **({"room_source": candidate.analysis["room_source"]} if candidate.room_correction else {}),
    })


def _room_median(path: Path) -> tuple[RoomMedian, str]:
    """The median at ``path`` and the digest a prescription must echo.

    The digest is over the BYTES read, not a re-serialization, so it names the
    document this round was actually judged against. Every way the file can
    fail to be evidence -- absent, unreadable, not JSON, not a median -- is
    the door's own one reason.
    """
    try:
        payload = read_source_bytes(str(path))
        document = json.loads(payload)
    except (OSError, ValueError, RecursionError) as exc:
        raise RoomPrescriptionRefused(
            ROOM_MEDIAN_UNAVAILABLE, f"{path}: {exc}"
        ) from exc
    return read_room_median(document), prescription_sha256(payload)


def _room_median_path(args: argparse.Namespace, resolved: Path | None = None) -> Path:
    """``--room-median``, or the round's own copy beside the evidence.

    ``resolved`` is this invocation's own answer, threaded back from the gate
    that already read it, so a later caller does not walk the round tree again.
    """
    if resolved is not None:
        return resolved
    if args.room_median:
        return Path(args.room_median)
    if args.session_dir:
        round_dir = Path(args.session_dir)
        return default_out(round_inputs(round_dir), round_dir, ROOM_MEDIAN_ARTIFACT)
    raise RoomPrescriptionRefused(
        ROOM_MEDIAN_UNAVAILABLE,
        "a room prescription is judged against the round's spatial median: "
        f"name it with --room-median <path>, or pass the round directory "
        f"holding {ROOM_MEDIAN_ARTIFACT}",
    )


def _applied_profile_path(args: argparse.Namespace) -> Path | None:
    """Where this invocation's applied-profile SSOT is.

    :func:`_load_packet`'s own resolution -- the flag, else the round's own
    copy -- with the on-box default standing in when ``--room-median`` alone
    named the evidence and there is no round to ask.
    """
    if args.applied_profile:
        return Path(args.applied_profile)
    if args.session_dir:
        return round_inputs(Path(args.session_dir)).applied_profile_path
    return baseline_profile_state_path()


def _layout_sides(args: argparse.Namespace) -> tuple[str, ...]:
    """The side names this speaker's applied preset declares.

    Read through ``load_applied_baseline_profile_state``, the same owner of
    "what is this speaker playing" the packet builder reads. A propose that
    cannot name the sides is not a dry run of the compose that will, so an
    unreadable profile refuses here rather than assuming a layout.
    """
    path = _applied_profile_path(args)
    profile = load_applied_baseline_profile_state(path) if path else None
    snapshot = (profile or {}).get("recomposition_snapshot")
    raw = snapshot.get("preset") if isinstance(snapshot, Mapping) else None
    if isinstance(raw, Mapping):
        try:
            return SIDES_BY_LAYOUT[
                ActiveSpeakerPreset.from_mapping(dict(raw)).channel_map.layout
            ]
        except (ActiveSpeakerConfigError, KeyError, TypeError, ValueError):
            pass
    raise RoomPrescriptionRefused(
        LAYOUT_UNAVAILABLE,
        "a room prescription is keyed by the sides this speaker declares, and "
        f"{path or 'no applied profile this round names'} does not declare them",
    )


def _room_gate(
    document: Mapping[str, Any],
    args: argparse.Namespace,
    path: Path,
    sides: Sequence[str],
) -> RoomPrescription:
    """The room door, against the median this invocation named.

    The round id is the directory the evidence came from -- the round tree
    when one was given, else the median's own parent -- because the candidate
    field's basis names the round, and only the caller knows which one it is.
    """
    median, sha256 = _room_median(path)
    round_id = (
        Path(args.session_dir).name
        if args.session_dir
        else path.resolve().parent.name
    )
    prescription = read_room_prescription(
        document,
        room_median=median,
        room_median_sha256=sha256,
        round_id=round_id,
        sides=sides,
    )
    if prescription is None:  # pragma: no cover - `document` is never None
        raise RoomPrescriptionRefused(
            BLEND_PRESCRIPTION_MALFORMED, "the prescription document was empty"
        )
    return prescription


def _composed_room(
    args: argparse.Namespace, sides: Sequence[str]
) -> tuple[dict[str, Any], str]:
    """The candidate fields ``compose`` contributes from a room prescription."""
    if not args.room_prescription:
        return {}, ""
    payload = read_source_bytes(args.room_prescription)
    prescription = _room_gate(
        read_prescription_bytes(payload), args, _room_median_path(args), sides
    )
    return (
        {**room_prescription_to_candidate_fields(prescription), "measured_basis": prescription.measured_basis},
        prescription_sha256(payload),
    )


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
    if args.session_dir is None:
        # Reachable only from the room class's own evidence flags: every other
        # path resolved this in `_evidence_source_error`, which cannot know
        # the document's kind because it runs before the document is read.
        raise CrossoverEvidencePacketError(
            "this prescription is judged against an evidence packet, so name "
            "the round it answers: a session_dir, or --packet <packet JSON>"
        )
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
        # No default, same reason as ``state_path``: the CamillaDSP statefile
        # is live, mutable system state, and a defaulted read would make two
        # honest rebuilds of the same round disagree on the packet's
        # fingerprint depending purely on when each ran (#3316).
        statefile_path=None,
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
    the round ordinal and hard-refuses without it. ``--room-median`` is an
    evidence source in its own right -- a room prescription is judged against
    the round's spatial median and needs no packet at all.
    """
    if not args.packet:
        if args.session_dir is None and not args.room_median:
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


PACKET_ARTIFACT = ARTIFACT_BY_VIEW["packet"].artifact
#: The seat cube's median, written by ``jasper-round-views room-median``.
ROOM_MEDIAN_ARTIFACT = ARTIFACT_BY_VIEW["room-median"].artifact


def _cmd_contract(args: argparse.Namespace) -> int:
    try:
        sources = {}
        if args.round:
            inputs = round_inputs(Path(args.round))
            sources = contract_sources(inputs.session_dir)
            artifact_dir, _ = round_artifact_dir(inputs.session_dir)
            for name, path in (
                ("draft", inputs.design_draft_path),
                ("receipt", artifact_dir / "round_receipt.json" if artifact_dir else None),
            ):
                try:
                    raw = read_json_source(str(path)) if path is not None else None
                except ValueError:
                    raw = None
                sources[name] = raw if isinstance(raw, dict) else {}
            sources["applied_profile"] = (load_applied_baseline_profile_state(inputs.applied_profile_path)
                                          if inputs.applied_profile_path else None)
        contracts = prescription_contracts(**sources)
        document = contracts if args.section == "all" else contracts[args.section]
        payload = contract_json(document)
    except (CrossoverEvidencePacketError, OSError, ValueError) as exc:
        return failed(EXIT_UNREADABLE, REASON_UNREADABLE, str(exc))
    if args.out:
        try:
            Path(args.out).write_text(payload, encoding="utf-8")
        except OSError as exc:
            return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
    print(payload)
    return EXIT_OK


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
    summary["rebuild_status_command"] = shlex.join([
        PROG, "status", *_evidence_words(args),
        *(["--state", args.state] if args.state else []),
    ])
    print(
        f"packet {(summary['packet_fingerprint'] or '')[:16]} "
        f"round={summary['round_id']} -> {out} ({summary['bytes']} bytes)",
        file=sys.stderr,
    )
    return answered(summary)


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
    BlendPrescription | DriverPrescription | RoomPrescription,
    dict[str, Any],
    tuple[FeatureVerdict, ...] | None,
    Path | None,
]:
    """The document, the prescription, what it becomes, what judged it, where.

    Shared WHOLE by ``propose`` and ``stage``, which is what makes the first a
    true dry run of the second. The document's own ``kind`` picks the gate --
    and, for the room class, the EVIDENCE too, which is why the document is
    read before anything is built: a room prescription is measured against the
    round's spatial median and a packet it never answered must not be built,
    let alone required. Raises ``BlendPrescriptionRefused`` (``EXIT_REFUSED``)
    or ``CrossoverEvidencePacketError``/``OSError`` (``EXIT_UNREADABLE``).

    The last member is the room median this invocation resolved, handed back so
    the receipt's home and the printed next command do not resolve it a second
    time; ``None`` for every other class, which is judged against a packet.
    """
    payload = read_source_bytes(args.prescription)
    document = read_prescription_bytes(payload)
    if document.get("kind") == ROOM_PRESCRIPTION_KIND:
        median_path = _room_median_path(args)
        room = _room_gate(document, args, median_path, _layout_sides(args))
        return (
            payload,
            room,
            room_prescription_to_candidate_fields(room),
            None,
            median_path,
        )
    packet = _load_packet(args)
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
    return payload, prescription, candidate_fields, classifications, None


#: What ``propose`` writes when no ``--out`` names somewhere else: the accepted
#: result, beside the packet it was judged against. NOT the prescription
#: document itself, which is the operator's own file and what
#: ``stage --prescription`` reads — and NOT ``proposal.json``, which is the
#: apply-time candidate mirror :func:`~jasper.active_speaker.bundles` writes
#: into the bundle inside this same round tree.
PROPOSAL_RECEIPT_ARTIFACT = "proposal_receipt.json"


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
    prescription: BlendPrescription | DriverPrescription | RoomPrescription,
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


def _evidence_words(args: argparse.Namespace) -> list[str]:
    """The words that named this invocation's evidence, ready to re-run.

    One owner for what a printed command must carry: a rebuild missing one of
    these flags resolves it against the machine instead and fingerprints
    differently, which is the mismatch a printed command must not walk into.
    """
    if args.packet:
        return ["--packet", args.packet]
    return [
        *([str(args.session_dir)] if args.session_dir else []),
        *(
            word
            for flag, dest in _REBUILD_ONLY_FLAGS
            if getattr(args, dest)
            for word in (flag, getattr(args, dest))
        ),
    ]


def _compose_command(args: argparse.Namespace, median_path: Path | None) -> str:
    """The ``compose`` invocation a room prescription becomes.

    A room set is not an instruction for the next round: it is a candidate
    field, so what follows ``propose`` is a composition against a banked base
    rather than a staging. The base is the operator's choice and is left as a
    placeholder.
    """
    return shlex.join([
        PROG, "compose", "--base", "<base candidate fingerprint>",
        "--room-prescription", args.prescription,
        "--room-median", str(_room_median_path(args, median_path)),
    ])


def _stage_command(args: argparse.Namespace) -> str:
    """The ``stage`` invocation for this evidence, with the paths in hand.

    ``--state`` is the one input ``propose`` does not need and ``stage``
    refuses without, so an operator who named none is handed the placeholder
    rather than a command that cannot run.
    """
    return shlex.join([
        PROG, "stage", *_evidence_words(args),
        "--prescription", args.prescription,
        "--state", args.state or "<flow state JSON>",
    ])


def _cmd_propose(args: argparse.Namespace) -> int:
    """Read a prescription back through the gate, and say what it becomes."""
    source_error = _evidence_source_error(args)
    if source_error is not None:
        return failed(EXIT_UNREADABLE, REASON_EVIDENCE_SOURCE, source_error)
    try:
        payload, prescription, candidate_fields, _, median_path = _gate(args)
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
    out = Path(args.out) if args.out else _proposal_out(args, median_path)
    try:
        out.write_text(blob)
        size_bytes = out.stat().st_size
    except OSError as exc:
        return failed(
            EXIT_WRITE_FAILED, REASON_UNWRITABLE, f"could not write {out}: {exc}"
        )
    _print_prescription(prescription, "accepted")
    return answered({
        **_admitted(prescription, candidate_fields, payload, out, size_bytes),
        "next": (
            _compose_command(args, median_path)
            if isinstance(prescription, RoomPrescription)
            else _stage_command(args)
        ),
    })


def _proposal_out(args: argparse.Namespace, median_path: Path | None) -> Path:
    """Beside the packet this document was judged against.

    A ``--packet`` file is already somewhere the operator can write; a rebuild
    lands where ``packet`` itself would have, so the round's own artifacts stay
    together and a live daemon-owned bundle is not written into.
    """
    if args.packet:
        return Path(args.packet).parent / PROPOSAL_RECEIPT_ARTIFACT
    if args.session_dir is None:
        # The room class's evidence is a file rather than a round, so the
        # receipt lands beside the median it was judged against. Reached only
        # after that gate accepted, which is what resolved ``median_path``.
        return _room_median_path(args, median_path).parent / PROPOSAL_RECEIPT_ARTIFACT
    round_dir = Path(args.session_dir)
    return default_out(round_inputs(round_dir), round_dir, PROPOSAL_RECEIPT_ARTIFACT)


def _band_phrase(lo: float, hi: float) -> str:
    """One frequency span, spelled the one way this tool spells it."""
    return f"{lo:.1f}-{hi:.1f} Hz"


def _passband_phrase(role: str, lo: float, hi: float) -> str:
    """One role's declared band, to whole hertz.

    A manufacturer figure; a tenth would suggest precision it does not have.
    """
    return f"{role} {lo:.0f}-{hi:.0f} Hz"


def _scope(
    prescription: BlendPrescription | DriverPrescription | RoomPrescription,
) -> str:
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
    prescription: BlendPrescription | DriverPrescription | RoomPrescription,
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
        # The per-driver class names a role and the room class a side; the
        # summed blend class names neither, because it IS the sum.
        named = entry.get("role") or entry.get("side") or ""
        role = f"{named} " if named else ""
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
        payload, prescription, candidate_fields, classifications, median_path = (
            _gate(args)
        )
        if isinstance(prescription, RoomPrescription):
            # BEFORE the ordinal: what refuses is the CLASS, not the round it
            # would have been staged for.
            raise RoomPrescriptionRefused(
                ROOM_NOT_STAGEABLE,
                "the spool carries what the next ROUND applies, and a room "
                "correction is not that: it becomes a candidate through "
                f"`{_compose_command(args, median_path)}`",
            )
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
    return answered({
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


def _candidate_records() -> list[dict[str, Any]]:
    """The validated candidate artifacts available for a summed trial."""
    records: list[dict[str, Any]] = []
    for banked in banked_candidates():
        candidate = banked.candidate
        minted = preset_crossover_geometry(candidate.source_preset)
        corner: dict[str, Any] | None = None
        if minted is not None:
            roles, geometry = minted
            corner = {
                "between_roles": list(roles),
                "fc_hz": geometry.fc_hz,
                "filter_type": geometry.filter_type,
                "slope_db_per_octave": geometry.slope_db_per_octave,
            }
        alignment = candidate.alignment
        records.append({
            "fingerprint": banked.fingerprint,
            "measurement_status": candidate.analysis.get("measurement_status", "not_reported"),
            "bundle_session_id": banked.bundle_session_id,
            "capture_session_id": banked.capture_session_id,
            "corner": corner,
            "alignment": {
                "polarity": alignment.polarity,
                "delay_us": alignment.delay_us,
                "delay_role": alignment.delay_role,
            },
        })
    return records


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
    candidates = _candidate_records()
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
    ) + (
        f"; {len(candidates)} banked candidate artifact(s)"
        if candidates
        else "; no banked candidate"
    )
    return {
        "available": available,
        "reason": reason,
        "bundle_session_id": session.get("bundle_session_id"),
        "round_id": session.get("round_id"),
        "region": region_state,
        "classification": classification,
        "walk": walk,
        "candidates": candidates,
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


def _next_commands(
    sections: dict[str, Any],
    *,
    packet_error: str,
    seat_level_db: float | None,
    session_dir: str | None,
    evidence: list[str],
    state: str | None,
) -> list[str]:
    """What to RUN next, with the paths already resolved.

    Artifact dependencies, not a workflow: each command is the consequence of
    one artifact being present or absent. Why it is offered is never restated
    here — the section that found the gap carries the reason, and a gap no
    command closes (no round banked, no declared driver band) is answered by
    ``speaker``'s two URLs.
    """
    commands: list[str] = []
    # Nothing that would fail for the reason already reported: these two read
    # the same evidence this verb just could not.
    if session_dir and not packet_error:
        commands.append(shlex.join(["jasper-round-views", "inventory", session_dir]))
        banked = sections["banked"]
        if banked["available"] and not banked["classification"]["available"]:
            try:
                bundle = str(round_inputs(Path(session_dir)).session_dir)
            except (CrossoverEvidencePacketError, OSError):
                bundle = session_dir
            commands.append(
                shlex.join(["jasper-round-views", "classify-features", bundle])
            )
        commands.append(shlex.join([
            PROG, "packet", *evidence, *(["--state", state] if state else []),
        ]))
    # Status discovers candidates; the LLM chooses a compatible shortlist.
    # Staging every retained artifact would turn discovery into an experiment.
    if len(sections["banked"]["candidates"]) > 1:
        commands.append("jasper-angle-capture plan --help")
    if not sections["staged"]["available"]:
        commands.append(
            " ".join([ORIENTATION_COMMAND, *(shlex.quote(w) for w in evidence)])
        )
    if seat_level_db is None:
        commands.append("jasper-seat-level")
    return commands


_READING_ORDER: tuple[tuple[str, str, str], ...] = (
    ("entry and tool menu", "tuning-operator-runbook.md",
     "short entry contract, tool discovery and optional examples"),
    ("optional methodology", "tuning-methodology.md",
     "measurement science and traps"),
    ("optional doctrine", "measurement-loop-doctrine.md",
     "roles and physical constraints"),
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


def _reading_order() -> list[dict[str, Any]]:
    """Entry contract, then optional references, with each document's size."""
    order: list[dict[str, Any]] = []
    for label, filename, gives in _READING_ORDER:
        path = _doc_path(filename)
        try:
            blob = Path(path).read_bytes()
        except OSError:
            size, lines = None, None
        else:
            size, lines = len(blob), blob.count(b"\n")
        order.append({
            "label": label, "path": path, "gives": gives,
            "bytes": size, "lines": lines,
        })
    return order


def status_document(
    packet: dict[str, Any] | None,
    packet_error: str,
    *,
    session_dir: str | None,
    evidence: list[str],
    state: str | None,
) -> dict[str, Any]:
    """Read status without rebuilding a supplied packet or consuming the spool."""
    sections = _status_sections(packet, packet_error)
    context: dict[str, Any] = {
        "frozen_packet": None, "latest_agent_note": None, "context_error": None,
    }
    recent = []
    try:
        if session_dir:
            context.update(context_artifacts(round_inputs(Path(session_dir)), Path(session_dir)))
            frozen = context["frozen_packet"]
            if frozen["present"]:
                frozen_packet = _read_packet_file(Path(frozen["path"]))
                fingerprint = frozen_packet.get("packet_fingerprint")
                frozen["contracts"] = frozen_packet.get("contracts")
                frozen["packet_fingerprint"] = fingerprint
                frozen["matches_current_evidence"] = (
                    fingerprint == packet.get("packet_fingerprint") if packet else None
                )
        else:
            for bundle in recent_round_sessions():
                path = str(banked_round_of(bundle) or bundle)
                recent.append({
                    "path": path,
                    "bundle_session_dir": str(bundle),
                    "next": [
                        shlex.join([PROG, "status", path]),
                        shlex.join(["jasper-round-views", "inventory", path]),
                    ],
                })
    except (CrossoverEvidencePacketError, OSError) as exc:
        context["context_error"] = str(exc)
    # A level nobody measured is what a session rides without one, so the
    # banked value itself is published rather than a warning about its absence.
    seat_level_db = seat_level_reference_volume_db()
    return {
        "speaker": {
            "hostname": read_identity().hostname,
            "crossover_url": speaker_url(CROSSOVER_PAGE_PATH),
            "declaration_url": speaker_url(SPEAKER_SETUP_PAGE_PATH),
        },
        "packet_fingerprint": (packet or {}).get("packet_fingerprint"),
        "contracts": (packet or {}).get("contracts"),
        "packet_error": packet_error or None,
        "selected_round": session_dir,
        "recent_rounds": recent,
        **sections,
        **context,
        "seat_level_reference_volume_db": seat_level_db,
        "reading_order": _reading_order(),
        "next": _next_commands(
            sections, packet_error=packet_error, seat_level_db=seat_level_db,
            session_dir=session_dir, evidence=evidence, state=state,
        ),
    }


def _cmd_status(args: argparse.Namespace) -> int:
    """Where this speaker stands, and what to run next. Writes nothing.

    Exit 0 whatever it found: this verb accepts nothing and refuses nothing, so
    an unreadable bundle is a FACT it reports — ``packet_fingerprint: null``
    beside the sentence in ``packet_error`` — rather than a failure that would
    have to publish a refusal record instead of the orientation the caller ran
    it for.
    """
    packet: dict[str, Any] | None = None
    packet_error = ""
    if args.session_dir is None:
        packet_error = "round_not_selected"
    else:
        try:
            packet = _load_packet(args)
        except (CrossoverEvidencePacketError, OSError) as exc:
            packet_error = str(exc)

    return answered(status_document(
        packet, packet_error,
        session_dir=args.session_dir,
        evidence=_evidence_words(args), state=args.state,
    ))


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


#: What ``--room-median`` is, and why the room class takes it instead of a
#: packet: the summed packet answers questions about the crossover region,
#: and a room prescription is bounded by the spatial median's own per-bin
#: spread and ceiling.
_ROOM_MEDIAN_HELP = (
    "the round's room median JSON -- the per-bin median, spread and seat "
    "deviations a room prescription is judged against. Required for a "
    f"{ROOM_PRESCRIPTION_KIND} document; defaults to {ROOM_MEDIAN_ARTIFACT} "
    "beside the round when a round directory was named. The document echoes "
    "this file's sha256, so one answering a different median is refused"
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
        metavar="<round-dir>",
        nargs="?" if optional_positional else None,
        help=(
            "a commissioning bundle directory (the one holding info.json and "
            "evidence/v1/artifacts/crossover_v2/<capture-session-id>/), or a "
            "banked round tree holding one"
            + (
                ". Omit to list recent retained rounds and commands to select "
                "one. Status leaves evidence unselected until you name a path"
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
        parser.add_argument("--room-median", default=None, help=_ROOM_MEDIAN_HELP)
    else:
        # So every verb's namespace answers the questions `_load_packet` and
        # the room door ask.
        parser.set_defaults(packet=None, room_median=None)


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
            "  0  accepted -- status (which accepts nothing) always exits 0;\n"
            "     what it could not read is a field in its document, not a\n"
            "     code\n"
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

    contract = sub.add_parser("contract", help="schemas and bounds evaluated on a round")
    contract.add_argument("--round", metavar="DIR")
    contract.add_argument("--section", choices=(*SECTIONS, "all"), default="all")
    contract.add_argument("--out", metavar="FILE", help="save the served JSON document")
    contract.set_defaults(func=_cmd_contract)

    compose = sub.add_parser("compose", help="combine banked candidate parts into an unmeasured candidate")
    compose.add_argument("--base", required=True, metavar="FINGERPRINT|saved", help="banked candidate or the applied speaker tune")
    compose.add_argument("--bass-extension-json", metavar="FILE", help="dynamic bass descriptor; an empty object removes extension")
    compose.add_argument("--role", action="append", default=[], metavar="ROLE=FINGERPRINT")
    compose.add_argument("--alignment", metavar="FINGERPRINT", help="alignment source; defaults to base")
    compose.add_argument("--blend", metavar="FINGERPRINT", help="blend source; defaults to base")
    compose.add_argument("--expected-effect", default="", help="expected change; no measurement claim")
    compose.add_argument("--observation-ref", action="append", default=[], help="path or take reference to existing observations")
    compose.add_argument("--rationale", default="")
    compose.add_argument(
        "--room-prescription",
        default=None,
        help=(
            f"a {ROOM_PRESCRIPTION_KIND} JSON document, or - for stdin. It is "
            "re-judged here by the same gate `propose` ran and lands on the "
            "composed candidate's room_correction field"
        ),
    )
    compose.add_argument("--room-median", default=None, help=_ROOM_MEDIAN_HELP)
    compose.add_argument("--root", help="candidate source and output bank; defaults to the speaker's bank")
    # `session_dir` so the room helpers can ask this namespace the same
    # question they ask propose's and stage's.
    compose.set_defaults(func=_cmd_compose, session_dir=None)

    status = sub.add_parser(
        "status",
        help="print declared / banked / staged / applied state and what is next",
    )
    _add_evidence_args(status, session_dir_optional=True)
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Run {CONTRACT_COMMAND} --round <dir> for the contracts.",
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
            f"a PATH for the accepted result instead of {PROPOSAL_RECEIPT_ARTIFACT} "
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
