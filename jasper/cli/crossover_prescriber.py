# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Judge and compose prescription documents; serve contracts and report applied layers, last banked rounds and the next program."""
from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from ._refusal import (
    EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answer, envelope, failed, help_from_rows,
    read_source_bytes,
)
from .round_views._common import (
    _ROUND_DIR_HELP, RoundSetRefused, add_set_argument, context_artifacts, round_ref,
)
from jasper.active_speaker.answer_schemas import ANSWER_SCHEMAS
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.round_catalog import catalog_command
from jasper.active_speaker.round_view_artifacts import TAKES_THIS_ROUND, CatalogRow, view_rows
from jasper.active_speaker.baseline_profile import applied_layer_names, load_applied_baseline_profile_state
from jasper.active_speaker.commissioning_coordinator import next_program_action, programs_for_topology
from jasper.active_speaker.candidate_bank import BankedCandidate, CandidateBankRefusal, banked_candidates, find_banked_candidate, publish_authored_candidate
from jasper.active_speaker.candidate_parts import program_charge_db
from jasper.active_speaker.crossover_declaration import preset_crossover_geometry
from jasper.active_speaker.design_draft import ActiveSpeakerDesignDraftError, load_design_draft
from jasper.active_speaker.crossover_v2.conductor_context import published_driver_caps
from jasper.active_speaker.crossover_v2.blend_prescription import BlendPrescriptionRefused, read_prescription_bytes
from jasper.active_speaker.crossover_v2.room_views import room_median_sha256
from jasper.active_speaker.crossover_v2.room_prescription import ROOM_MEDIAN_UNAVAILABLE, RoomMedian, RoomPrescriptionRefused, read_room_median
from jasper.active_speaker.crossover_v2.evidence_packet import (
    DERIVED_VIEWS, CrossoverEvidencePacketError, build_round_evidence, contract_currency,
    packet_driver_passbands_hz, packet_feature_classifications, round_evidence,
)
from jasper.active_speaker.crossover_v2.prescription_contract import SECTIONS, contract_json, contract_programs, prescription_contracts
from jasper.active_speaker.crossover_v2.prescription_document import (
    DOCUMENT_KIND, REASON_EVIDENCE_UNREADABLE, SECTION_KINDS, PrescriptionDocumentRefused, PrescriptionEvidence, blamed_section,
    judge_prescription_document, preview_prescription_document, parse_vary_axis, preview_kind,
    read_prescription_document, saved_base, vary_document,
)
from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
from jasper.active_speaker.crossover_v2.rear_preview import summary_rows
from jasper.active_speaker.crossover_v2.round_inputs import (
    banked_round_of, latest_banked_rounds, recent_round_sessions, round_inputs, prescription_sources, resolve_set, RoundInputs,
    SetTakes, subject,
)
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
from jasper.active_speaker.seat_level_reference import seat_level_reference_status
from jasper.active_speaker.output_contract import classify_output_contract, rear_cabinet_channels
from jasper.active_speaker.tuning_docs import reading_order
from jasper.audio_measurement.bundles import BundleError
from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable, unavailable
from jasper.platform.atomic_io import atomic_write_json
from jasper.platform.json_fields import sha256_file
from jasper.audio_routes.output_topology_store import load_output_topology
from jasper.identity.reader import CROSSOVER_PAGE_PATH, SPEAKER_SETUP_PAGE_PATH, read_identity, speaker_url

PROG = "jasper-crossover-prescriber"
AUTHORITY_TIER = "advisory (judge, contract and status read; compose banks a candidate)"
REASON_UNWRITABLE = "output_unwritable"


#: A preview's shape, answered or written to a file (ADR-0344 §4).
_PREVIEW_SCHEMA = ANSWER_SCHEMAS[f"{PROG} judge --preview"]


def _answer(args: argparse.Namespace, row: str, read: Mapping[str, Any],
            parameters: Mapping[str, Any] | None = None, **fields: Any) -> int:
    """This verb's answer under the envelope every analysis answer shares (ADR-0387).

    ``read`` is the :func:`subject` of the round, set and take the verb resolved.
    """
    return answer(args.command, schema=ANSWER_SCHEMAS[f"{PROG} {row}"], subject=read,
                  parameters=parameters or {}, line="", **fields)


def _document_set(args: argparse.Namespace, document: Mapping[str, Any], inputs: RoundInputs | None) -> SetTakes | None:
    """The set a document reads: the one ``--set`` names, else a room section's only set."""
    if inputs is None or not (args.set or document["sections"].get("room")):
        return None
    return resolve_set(inputs, args.set)


def _document_evidence(
    args: argparse.Namespace, document: Mapping[str, Any], inputs: RoundInputs | None,
) -> PrescriptionEvidence:
    sections = document["sections"]
    sources = prescription_sources(inputs, set_id=args.set)
    packet: dict[str, Any] = {}
    if inputs is not None and (sections.get("driver") or sections.get("blend")):
        packet = _load_packet(args, inputs=inputs)
    try:
        sha = _room_median(sources.get("room_median", {}))[1] if sections.get("room") else ""
    except RoomPrescriptionRefused as exc:
        raise PrescriptionDocumentRefused(exc.reason, "room", exc.detail, evidence=exc.evidence) from exc
    return PrescriptionEvidence(sources, packet, sha, Path(args.round).name if args.round else "")


def _room_median(source: Path | Mapping[str, Any]) -> tuple[RoomMedian, str]:
    """Bind the canonical median independently of the document's incumbent."""
    try:
        document = json.loads(read_source_bytes(str(source))) if isinstance(source, Path) else source
        median = document.get("median", document) if isinstance(document, Mapping) else document
        return read_room_median(median), room_median_sha256(median)
    except RoomPrescriptionRefused:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise RoomPrescriptionRefused(ROOM_MEDIAN_UNAVAILABLE, str(exc)) from exc


def _document_base(document: Mapping[str, Any], root: Path | None) -> tuple[BankedCandidate, Mapping[str, Any] | None]:
    return saved_base() if document["base"] == "saved" else (find_banked_candidate(document["base"], root=root), None)


def _preview_document(
    args: argparse.Namespace, document: Mapping[str, Any], inputs: RoundInputs | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The preview, and the :func:`subject` of the round, set and take it read."""
    kind = preview_kind(document)
    selected, capture_id, cabinet = None, None, None
    try:
        base, _ = _document_base(document, Path(args.root) if args.root else None)
        if kind == "rear_calibration":
            cabinet = rear_cabinet_channels(classify_output_contract(load_output_topology()))
        else:
            selected = _document_set(args, document, inputs)
        evidence = _document_evidence(args, document, inputs)
        if kind == "emitted_graph" and inputs is not None:
            selected = selected or resolve_set(inputs, args.set)
            capture_id = selected.with_records(inputs.session_dir, every_take=args.take is not None).take_id(args.take)
        result = preview_prescription_document(document, round_dir=Path(args.round) if args.round else None,
                                               base=base, evidence=evidence, capture_id=capture_id, cabinet=cabinet)
    except (RoundSetRefused, EvidenceUnavailable) as exc:
        raise PrescriptionDocumentRefused(exc.reason, blamed_section(document["sections"]), str(exc), evidence=exc.detail) from exc
    return result, subject(inputs, selected, take_ids=None if capture_id is None else [capture_id])


def _preview_parameters(result: Mapping[str, Any]) -> dict[str, Any]:
    """The gate and band the preview's engine recorded; null where its kind records none (ADR-0344 §3)."""
    summary = result["preview"].get("summary") or {}
    if result["section"] == "emitted_graph":
        return {"window_ms": summary["window"]["window_ms"], "band_hz": summary["reconstruction"]["compared_band_hz"]}
    return {"window_ms": None, "band_hz": summary.get("band_hz")}


def _cmd_vary_document(args: argparse.Namespace, document: Mapping[str, Any], inputs: RoundInputs | None) -> int:
    kind = preview_kind(document)
    axes = [parse_vary_axis(text) for text in args.vary]
    directory = Path(args.out_dir)
    rows = []
    read = subject(inputs)
    for index, (values, variant) in enumerate(vary_document(document, axes), 1):
        try:
            result, read = _preview_document(args, variant, inputs)
        except PrescriptionDocumentRefused as exc:
            rows.append({"out": None, "values": values, "reason": exc.code, "error": exc.error})
            continue
        path = directory / f"variant-{index:02d}.json"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, variant)
            atomic_write_json(path.with_suffix(".preview.json"), {**result, "schema": _PREVIEW_SCHEMA})
        except OSError as exc:
            return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
        rows.append({"out": str(path), "values": values,
                     **({"program_charge_db": result["program_charge_db"], **summary_rows(result["preview"])}
                        if result["section"] == "rear_calibration"
                        else {"summary": result["preview"]["summary"]} if result["section"] == "emitted_graph"
                        else {"preview": result["preview"]})})
    return _answer(args, "judge --preview --vary", read,
                   {"axes": [{"paths": paths, "values": values} for paths, values in axes]},
                   section=kind, variants=rows, adopted=False, banked=False)


def _preview_out(args: argparse.Namespace, result: Mapping[str, Any], read: Mapping[str, Any]) -> int:
    """The whole preview to ``--out`` under its answer's schema; the answer names
    it and keeps the forecast's summary."""
    out = Path(args.out)
    try:
        atomic_write_json(out, {**result, "schema": _PREVIEW_SCHEMA})
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
    return _answer(
        args, "judge --preview", read, _preview_parameters(result), out=out,
        section=result["section"], sections=result["sections"],
        **({"summary": result["preview"]["summary"]} if result["section"] == "emitted_graph" else {}),
        adopted=False, banked=False,
    )


def _evidence_code(exc: Exception) -> str:
    """The code an evidence error carries, else ``evidence_unreadable``."""
    return str(getattr(exc, "code", REASON_EVIDENCE_UNREADABLE))


def _document_failure(refusal: PrescriptionDocumentRefused, exit_code: int | None = None) -> int:
    if exit_code is None:
        exit_code = {REASON_EVIDENCE_UNREADABLE: EXIT_UNREADABLE, REASON_UNWRITABLE: EXIT_WRITE_FAILED}.get(refusal.code, EXIT_REFUSED)
    return failed(exit_code, refusal.code, refusal.failure_detail(), code=refusal.code,
                  next_action=refusal_copy_for(refusal.code)[1])


def _cmd_document(args: argparse.Namespace) -> int:
    try:
        try:
            raw = read_prescription_bytes(read_source_bytes(args.document))
        except BlendPrescriptionRefused as exc:
            raise PrescriptionDocumentRefused(exc.reason, None, exc.detail, evidence=exc.evidence) from exc
        document = read_prescription_document(raw)
        root = Path(args.root) if args.root else None
        inputs = round_inputs(Path(args.round)) if args.round else None
        if args.command == "judge" and args.preview:
            if args.vary:
                return _cmd_vary_document(args, document, inputs)
            result, read = _preview_document(args, document, inputs)
            if args.out:
                return _preview_out(args, result, read)
            return _answer(args, "judge --preview", read, _preview_parameters(result), **result)
        base, base_profile = _document_base(document, root)
        selected = _document_set(args, document, inputs)
        evidence = _document_evidence(args, document, inputs)
        candidate = judge_prescription_document(document, base=base, evidence=evidence,
                                                 base_profile=base_profile)
        fields: dict[str, Any] = {
            "candidate_fingerprint": candidate.fingerprint, "resolution": candidate.analysis["resolution"],
            "program_charge_db": program_charge_db(candidate), "measurement_status": "unmeasured", "adopted": False,
            "packet_contracts": contract_currency(inputs) if inputs is not None else None}
        if args.command == "judge":
            fields["sections"] = candidate.analysis["evidence"]["prescriptions"]
        else:
            try:
                fields["out"] = publish_authored_candidate(candidate, root=root).path
            except (OSError, BundleError) as exc:
                raise PrescriptionDocumentRefused(REASON_UNWRITABLE, None, str(exc)) from exc
    except PrescriptionDocumentRefused as exc:
        return _document_failure(exc)
    except RoundSetRefused as exc:
        return _document_failure(PrescriptionDocumentRefused(exc.reason, blamed_section(document["sections"]), str(exc), evidence=exc.detail))
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        return _document_failure(PrescriptionDocumentRefused(exc.code, None, exc.detail))
    except (CrossoverEvidencePacketError, OSError, ValueError) as exc:
        refusal = PrescriptionDocumentRefused(_evidence_code(exc), None, str(exc))
        return _document_failure(refusal, EXIT_UNREADABLE)
    return _answer(args, args.command, subject(inputs, selected), **fields)


def _load_packet(args: argparse.Namespace, *, inputs: RoundInputs | None = None) -> dict[str, Any]:
    if args.session_dir is None:
        raise CrossoverEvidencePacketError("name a round directory")
    inputs = inputs or round_inputs(Path(args.session_dir))
    if not any((args.state, args.drivers, args.applied_profile, args.repeat_floor, args.declared_geometry)):
        return round_evidence(inputs)
    # A status what-if: built from the inputs named, fingerprinted as built.
    named = replace(
        inputs,
        design_draft_path=Path(args.drivers) if args.drivers else inputs.design_draft_path,
        applied_profile_path=Path(args.applied_profile) if args.applied_profile else inputs.applied_profile_path,
        repeat_floor_path=Path(args.repeat_floor) if args.repeat_floor else inputs.repeat_floor_path,
        declared_geometry_path=Path(args.declared_geometry) if args.declared_geometry else inputs.declared_geometry_path,
    )
    return build_round_evidence(named, state_path=Path(args.state) if args.state else None)



def _cmd_contract(args: argparse.Namespace) -> int:
    try:
        inputs = round_inputs(Path(args.round)) if args.round else None
        selected = resolve_set(inputs, args.set) if inputs is not None and args.set else None
        sources = prescription_sources(inputs, set_id=args.set)
        programs = contract_programs(sources) if inputs is not None else programs_for_topology(load_output_topology())
        if args.section != "all" and args.section not in programs:
            return failed(EXIT_REFUSED, "prescription_section_unavailable", args.section)
        # A named section is built alone, so a section it never reads cannot refuse it.
        contracts = prescription_contracts(programs=programs if args.section == "all" else (args.section,), **sources)
        document = contracts if args.section == "all" else contracts[args.section]
        payload = contract_json(document)
    except RoundSetRefused as exc:
        return failed(EXIT_REFUSED, exc.reason, exc.detail)
    except (CrossoverEvidencePacketError, OSError, ValueError) as exc:
        return failed(EXIT_UNREADABLE, _evidence_code(exc), str(exc))
    read, parameters = subject(inputs, selected), {"section": args.section}
    if not args.out:
        # The contracts' own compact serialization keeps the answer the size of what it serves.
        print(contract_json(envelope(
            args.command, schema=ANSWER_SCHEMAS[f"{PROG} contract"], subject=read, parameters=parameters,
            sections=contracts,
        )))
        return EXIT_OK
    out = Path(args.out)
    try:
        out.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return failed(EXIT_WRITE_FAILED, REASON_UNWRITABLE, str(exc))
    return _answer(args, "contract", read, parameters, out=out, sha256=sha256_file(out))



def _passband_phrase(role: str, lo: float, hi: float) -> str:
    """One role's declared band, to whole hertz.

    A manufacturer figure; a tenth would suggest precision it does not have.
    """
    return f"{role} {lo:.0f}-{hi:.0f} Hz"



def _block(packet: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """One of the packet's own blocks, or an empty one when there is no packet."""
    block = (packet or {}).get(name)
    return block if isinstance(block, dict) else {}



def _gap(block: dict[str, Any], packet_gap: dict[str, Any] | None) -> dict[str, Any]:
    """Why a section has nothing to report, as the one gap shape (#5928 TB2), from whichever layer knows.

    The packet builder's failure wins when there is one, its code and its
    sentence; below that, the block's own ``absence`` code and detail, passed
    through untranslated. A block that names no code was never supplied.
    """
    if packet_gap:
        return dict(packet_gap)
    return unavailable(str(block.get("reason") or "source_absent"), block.get("detail"))



def _incumbent_record(value: Any, packet_gap: dict[str, Any] | None) -> dict[str, Any]:
    """The packet's applied incumbent, classified.

    An empty list is ``available`` with zero filters: "the profile applied an
    empty correction" and "no profile was readable" are the two facts a
    prescription author most needs kept apart, because a prescription is a
    TOTAL.
    """
    if isinstance(value, list):
        return {"status": "available", "n_filters": len(value)}
    return _gap(value if isinstance(value, dict) else {}, packet_gap)



def _declared_section(
    packet: dict[str, Any] | None, packet_gap: dict[str, Any] | None
) -> dict[str, Any]:
    """What this speaker says its drivers are, through the per-driver gate's reader.

    :func:`~.evidence_packet.packet_driver_passbands_hz` is what bounds a
    per-driver prescription, so asking it here asks the question the door will.
    """
    passbands = packet_driver_passbands_hz(packet)
    roles = sorted(passbands)
    gap = {} if passbands else _gap(_block(packet, "drivers"), packet_gap)
    return {
        **(gap or {"status": "available"}),
        "roles": roles,
        "passbands_hz": {
            role: [lo, hi] for role, (lo, hi) in sorted(passbands.items())
        },
        "summary": (
            ", ".join(_passband_phrase(role, *passbands[role]) for role in roles)
            if passbands
            else f"no declared driver band ({gap['reason']})"
        ),
    }



def _live_driver_caps() -> dict[str, Any]:
    """Each driver's program-path cap and its source from today's declaration, whichever
    round is named, or the code refusing that declaration (ADR-0382)."""
    try:
        profile = load_design_draft().get("driver_safety_profile") or {}
    except ValueError as exc:  # /sound/speaker/ opens a refused declaration and names its fix
        return {"caps": {}, "reason": getattr(exc, "code", ActiveSpeakerDesignDraftError.code)}
    targets = {target["target_id"]: target["target_fingerprint"] for target in profile.get("targets", [])}
    return {"caps": published_driver_caps(profile, targets), "reason": None}


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
    packet: dict[str, Any] | None, packet_gap: dict[str, Any] | None
) -> dict[str, Any]:
    """The round, its classified features (the classification view filed
    beside it) and its walk (the ACCEPTED lateral takes)."""
    verdicts = packet_feature_classifications(packet)
    candidates = _candidate_records()
    classification = {
        **({"status": "available"} if verdicts else _gap(
            _block(_block(packet, DERIVED_VIEWS), "feature_classification"), packet_gap)),
        "n_verdicts": len(verdicts) if verdicts else 0,
    }
    lateral = _block(packet, "lateral_poses")
    walked = lateral.get("status") == "available"
    walk: dict[str, Any] = {
        **({"status": "available"} if walked else _gap(lateral, packet_gap)),
        "n_takes": lateral.get("n_takes") or 0,
        "angles_deg": _degree_list(lateral, "angles_deg"),
        "elevations_deg": _degree_list(lateral, "elevations_deg"),
    }
    # "0 deg" is not a raise worth a clause.
    raised = [deg for deg in walk["elevations_deg"] if deg]
    session = _block(packet, "session")
    gap = {} if session else _gap(session, packet_gap)
    summary = (
        (
            f"round in session {session.get('bundle_session_id')}"
            + (
                f", {classification['n_verdicts']} classified feature(s)"
                if verdicts
                else f", no readable classification ({classification['reason']})"
            )
        )
        if session
        else f"no round ({gap['reason']})"
    ) + (
        f"; {walk['n_takes']} walk take(s) at "
        f"{', '.join(str(deg) for deg in walk['angles_deg'])} deg"
        + (
            f", elevations {', '.join(str(deg) for deg in walk['elevations_deg'])}"
            " deg"
            if raised
            else ""
        )
        if walked
        else f"; no walk takes ({walk['reason']})"
    ) + (
        f"; {len(candidates)} banked candidate artifact(s)"
        if candidates
        else "; no banked candidate"
    )
    return {
        **(gap or {"status": "available"}),
        "bundle_session_id": session.get("bundle_session_id"),
        "classification": classification,
        "walk": walk,
        "candidates": candidates,
        "summary": summary,
    }



def _applied_section(
    packet: dict[str, Any] | None, packet_gap: dict[str, Any] | None
) -> dict[str, Any]:
    block = _block(packet, "incumbent")
    return {"from_applied_profile": _incumbent_record(block.get("from_applied_profile"), packet_gap)}



def _status_sections(
    packet: dict[str, Any] | None, packet_gap: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "declared": _declared_section(packet, packet_gap),
        "banked": _banked_section(packet, packet_gap),
        "applied": _applied_section(packet, packet_gap),
    }



def _next_commands(
    sections: dict[str, Any],
    *,
    packet_gap: dict[str, Any] | None,
    seat_level_db: float | None,
    session_dir: str | None,
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
    if session_dir and not packet_gap:
        commands.append(catalog_command(session_dir))
        commands.append(shlex.join([
            PROG, "contract", "--round", session_dir,
        ]))
    # Status discovers candidates; the LLM chooses a compatible shortlist.
    # Staging every retained artifact would turn discovery into an experiment.
    if len(sections["banked"]["candidates"]) > 1:
        commands.append("jasper-round run --help")
    if seat_level_db is None:
        commands.append("jasper-seat-level")
    return commands



def status_document(
    packet: dict[str, Any] | None,
    packet_gap: dict[str, Any] | None,
    *,
    session_dir: str | None,
    inputs: RoundInputs | None = None,
    applied_profile_path: Path | None = None,
) -> dict[str, Any]:
    """Read retained evidence and candidate status; ``inputs`` is ``session_dir`` already read.

    ``packet_gap`` is why the packet did not build, as a gap: every section it
    feeds reads it.
    """
    sections = _status_sections(packet, packet_gap)
    context: dict[str, Any] = {
        "latest_agent_note": None, "context_error": None,
    }
    recent = []
    currency = None
    try:
        if session_dir:
            inputs = inputs or round_inputs(Path(session_dir))
            context.update(context_artifacts(inputs, Path(session_dir)))
            currency = contract_currency(inputs)
        else:
            for bundle in recent_round_sessions():
                path = str(banked_round_of(bundle) or bundle)
                recent.append({
                    "path": path,
                    "bundle_session_dir": str(bundle),
                    "next": [
                        shlex.join([PROG, "status", path]),
                        catalog_command(path),
                    ],
                })
    except (CrossoverEvidencePacketError, OSError) as exc:
        context["context_error"] = unavailable(_evidence_code(exc), str(exc))
    # A level nobody measured is what a session rides without one, so the banked value
    # itself is published rather than a warning about its absence.
    level = seat_level_reference_status() or {}
    seat_level_db = level.get("seat_level_reference_volume_db")
    profile = load_applied_baseline_profile_state(applied_profile_path)
    identity = applied_identity(profile) or {}
    programs = programs_for_topology(load_output_topology())
    action = next_program_action(profile, identity, latest_banked_rounds(identity, programs=programs),
                                 programs=programs)
    banked = latest_banked_rounds(identity, programs=programs, include_stale=True)
    layers = applied_layer_names(profile)
    sections["applied"].update(
        layers=layers, candidate_fingerprint=identity.get("candidate"),
        summary="applied layers: " + (", ".join(name for name, applied in layers.items() if applied) or "none"),
        reference_volume_db=seat_level_db, leveled_db_spl=level.get("leveled_db_spl"),
        anchor_graph_mismatch=level.get("anchor_graph_mismatch"), anchor_pose_mismatch=level.get("anchor_pose_mismatch"),
    )
    return {
        "speaker": {
            "hostname": read_identity().hostname,
            "crossover_url": speaker_url(CROSSOVER_PAGE_PATH),
            "declaration_url": speaker_url(SPEAKER_SETUP_PAGE_PATH),
        },
        "packet_fingerprint": (packet or {}).get("packet_fingerprint"),
        "contracts": (packet or {}).get("contracts"),
        "packet_contracts": currency,
        "selected_round": session_dir,
        "recent_rounds": recent,
        **sections,
        **context,
        "seat_level_reference_volume_db": seat_level_db,
        "driver_caps_live": _live_driver_caps(),
        "reading_order": [{key: value for key, value in entry.items() if key != "name"}
                          for entry in reading_order()],
        "last_banked": {name: {key: banked[name][key] for key in ("round_id", "round_dir", "banked_at", "status", "stale")}
                        if name in banked else None for name in programs},
        "next": {"program": None if action["reason_code"] == "complete" else action["program"],
                 "reason_code": action["reason_code"]},
        "next_commands": _next_commands(
            sections, packet_gap=packet_gap, seat_level_db=seat_level_db,
            session_dir=session_dir,
        ),
    }



def _cmd_status(args: argparse.Namespace) -> int:
    """Where this speaker stands, and what to run next. Writes nothing.

    Exit 0 whatever it found: this verb accepts nothing and refuses nothing, so
    an unreadable bundle is a FACT it reports — ``packet_fingerprint: null``
    beside a gap in each section the packet feeds, its code in ``reason`` and
    its evidence in ``detail`` — rather than a failure that would have to
    publish a refusal record instead of the orientation the caller ran it for.
    """
    inputs: RoundInputs | None = None
    packet: dict[str, Any] | None = None
    packet_gap: dict[str, Any] | None = None
    if args.session_dir is not None:
        try:
            inputs = round_inputs(Path(args.session_dir))
            packet = _load_packet(args, inputs=inputs)
        except (CrossoverEvidencePacketError, OSError) as exc:
            packet_gap = unavailable(_evidence_code(exc), str(exc))
        except EvidenceUnavailable as exc:
            packet_gap = unavailable(exc.reason, exc.detail)

    return _answer(args, "status", subject(inputs), **status_document(
        packet, packet_gap,
        session_dir=args.session_dir, inputs=inputs,
        applied_profile_path=Path(args.applied_profile) if args.applied_profile else None,
    ))



#: A round argument takes an id as a view's does; round_ref swaps it for the banked round's directory.
_ROUND = partial(round_ref, str)

#: The modes no catalog row covers: judge and compose answer no analysis question (ADR-0393), so their
#: help rows live with the verbs.
_DOCUMENT_ARGV = ("<document.json>", "--round", TAKES_THIS_ROUND, "--set", "<set-id>")
_UNCATALOGUED = {
    "judge": {f"{PROG} judge": CatalogRow(
        argv=_DOCUMENT_ARGV, question="Does a prescription document pass every gate, and what candidate would it make?",
        avoid="the predicted response; judge --preview predicts it")},
    "compose": {f"{PROG} compose": CatalogRow(
        argv=_DOCUMENT_ARGV, question="Which candidate does a judged document make, banked under its fingerprint?",
        avoid="checking a document, which judge does without banking, or applying one; jasper-round apply does that")},
}

#: What the shared exit words leave out: judge and compose refuse a malformed document (1) where the
#: vocabulary calls malformed input unreadable (2), and status refuses nothing (see _cmd_status).
_DOCUMENT_EXITS: dict[str, Any] = {"note": "A document that is not valid JSON, or does not fit the document schema, exits 1 "
                                            "as a refusal. Code 2 means the document file or the round cannot be read."}
_EXITS: dict[str, dict[str, Any]] = {
    "judge": _DOCUMENT_EXITS, "compose": _DOCUMENT_EXITS,
    "status": {"codes": (EXIT_OK,), "note": "It refuses nothing. A round it cannot read shows as an unavailable gap, "
                                            "with a reason code and its detail, in each section it feeds."},
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    contract = sub.add_parser("contract", help="schemas and bounds evaluated on a round")
    contract.add_argument("--round", metavar="DIR", type=_ROUND, help=f"evaluate the bounds on this round: {_ROUND_DIR_HELP}")
    add_set_argument(contract)
    contract.add_argument("--section", choices=(*SECTIONS, "all"), default="all",
                          help="the program whose contract to serve, or all (default: %(default)s)")
    contract.add_argument("--out", metavar="FILE", help="write the contracts here; the answer then names the file and its sha256")
    contract.set_defaults(func=_cmd_contract)
    for verb in ("judge", "compose"):
        command = sub.add_parser(verb, help="judge every section and preview resolution" if verb == "judge" else "judge, prove and bank one candidate")
        command.add_argument("document", metavar="DOC", help=(
            f'a file, or - for stdin: {{"kind": "{DOCUMENT_KIND}", "schema": 1, "base": "saved" or a banked '
            f'fingerprint, "sections": {{name: {{...}} or null}}, "rationale": text}}; a section left out '
            f'keeps the base\'s, null or {{}} clears it; sections: {", ".join(SECTION_KINDS)}'))
        command.add_argument("--round", dest="round", metavar="DIR", type=_ROUND,
                             help=f"the round the document reads its evidence from: {_ROUND_DIR_HELP}")
        add_set_argument(command, take=verb == "judge")
        if verb == "judge":
            command.add_argument("--preview", action="store_true", help="predict driver, blend or topology with --round <branch diagnostic round>, room with --round <room round>, or rear_calibration with --round <pair round>, compiling its stage at the declared cabinet's outputs; banks nothing")
            command.add_argument("--vary", action="append", metavar="AXIS", help="PATH[,PATH...]=VALUE[,VALUE...] axis; repeat for a Cartesian grid")
            command.add_argument("--out-dir", metavar="DIR", help="write grid documents and full previews")
            command.add_argument("--out", metavar="FILE", help="write the full preview here and answer with its summary; "
                                 "jasper-round-views compare --a-preview reads it")
        command.add_argument("--root", help="candidate bank root")
        command.set_defaults(func=_cmd_document)
    status = sub.add_parser("status", help="read applied layers, last banked rounds and the next program; optionally inspect a round")
    status.add_argument("session_dir", nargs="?", metavar="DIR", type=_ROUND,
                        help=f"read this round's evidence packet: {_ROUND_DIR_HELP}; without one, list the recent rounds")
    for name, help_text in (
        ("state", "with a round, read this flow state file in place of the round's own"),
        ("drivers", "with a round, read this driver declaration (design draft) in place of the round's own"),
        ("applied-profile", "read this applied baseline profile in place of the round's or the speaker's own"),
        ("repeat-floor", "with a round, read this repeat floor in place of the round's own"),
        ("declared-geometry", "with a round, read this declared rig geometry in place of the round's own"),
    ):
        status.add_argument(f"--{name}", metavar="FILE", help=help_text)
    status.set_defaults(func=_cmd_status)
    for command in (status, *[sub.choices[v] for v in ("judge", "compose")]):
        command.set_defaults(state=None, drivers=None, applied_profile=None, repeat_floor=None, declared_geometry=None)
    for verb, child in sub.choices.items():
        help_from_rows(child, {**_UNCATALOGUED.get(verb, {}), **view_rows(verb, PROG)}, **_EXITS.get(verb, {}))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "judge" and args.vary and (not args.preview or not args.out_dir):
        parser.error("--vary requires --preview and --out-dir")
    if args.command == "judge" and args.out and (not args.preview or args.vary):
        parser.error("--out requires --preview without --vary")
    if args.command in {"judge", "compose"}:
        args.session_dir = args.round
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
