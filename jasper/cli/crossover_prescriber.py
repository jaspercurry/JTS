# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Judge and compose prescription documents; serve contracts and read status."""
from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_WRITE_FAILED, answered, failed, read_json_source, read_source_bytes
from .round_views import default_out
from .round_views._common import ARTIFACT_BY_VIEW, context_artifacts
from jasper.active_speaker.candidate_bank import BankedCandidate, CandidateBankRefusal, banked_candidates, find_banked_candidate, publish_authored_candidate
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.crossover_declaration import preset_crossover_geometry
from jasper.active_speaker.crossover_v2.blend_prescription import BlendPrescriptionRefused, prescription_sha256, read_prescription_bytes
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError, build_crossover_evidence_packet, packet_driver_passbands_hz,
    packet_feature_classifications, packet_region_band_hz,
)
from jasper.active_speaker.crossover_v2.prescription_contract import SECTIONS, contract_json, prescription_contracts
from jasper.active_speaker.crossover_v2.prescription_document import (
    PrescriptionDocumentRefused, PrescriptionEvidence, judge_prescription_document, read_prescription_document,
)
from jasper.active_speaker.crossover_v2.room_prescription import ROOM_MEDIAN_UNAVAILABLE
from jasper.active_speaker.crossover_v2.round_inputs import (
    banked_round_of, recent_round_sessions, round_artifact_dir, round_inputs, contract_sources, RoundInputs,
)
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
from jasper.active_speaker.seat_level_reference import seat_level_reference_volume_db
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.audio_measurement.bundles import BundleError
from jasper.identity.reader import CROSSOVER_PAGE_PATH, SPEAKER_SETUP_PAGE_PATH, read_identity, speaker_url
from jasper.output_topology import load_output_topology_strict

PROG = "jasper-crossover-prescriber"
AUTHORITY_TIER = "advisory (judge, contract and status read; compose banks a candidate)"
REASON_UNREADABLE = "evidence_unreadable"
REASON_UNWRITABLE = "output_unwritable"

def _contract_sources(inputs: RoundInputs | None) -> dict[str, Any]:
    if inputs is None:
        return {}
    sources = contract_sources(inputs)
    artifact_dir, _ = round_artifact_dir(inputs.session_dir)
    for name, path in (("draft", inputs.design_draft_path),
                       ("receipt", artifact_dir / "round_receipt.json" if artifact_dir else None)):
        try:
            raw = read_json_source(str(path)) if path is not None else None
        except ValueError:
            raw = None
        sources[name] = raw if isinstance(raw, dict) else {}
    sources["applied_profile"] = (load_applied_baseline_profile_state(inputs.applied_profile_path)
                                  if inputs.applied_profile_path else None)
    return sources


def _document_evidence(args: argparse.Namespace, document: Mapping[str, Any]) -> PrescriptionEvidence:
    inputs = round_inputs(Path(args.round)) if args.round else None
    sources = _contract_sources(inputs)
    sections = document["sections"]
    packet: dict[str, Any] = {}
    sha = ""
    if inputs is not None:
        if sections.get("driver") or sections.get("blend"):
            packet = _load_packet(args, inputs=inputs)
        if sections.get("room"):
            path = default_out(inputs, Path(args.round), ARTIFACT_BY_VIEW["room-median"].artifact)
            try:
                payload = read_source_bytes(str(path))
                sources["room_median"] = json.loads(payload)
            except (OSError, ValueError) as exc:
                raise PrescriptionDocumentRefused(ROOM_MEDIAN_UNAVAILABLE, "room", str(exc)) from exc
            sha = prescription_sha256(payload)
    return PrescriptionEvidence(sources, packet, sha, Path(args.round).name if args.round else "")


def _cmd_document(args: argparse.Namespace) -> int:
    try:
        try:
            raw = read_prescription_bytes(read_source_bytes(args.document))
        except BlendPrescriptionRefused as exc:
            raise PrescriptionDocumentRefused(exc.reason, None, exc.detail, evidence=exc.evidence) from exc
        document = read_prescription_document(raw)
        if args.base is not None and args.base != document["base"]:
            raise PrescriptionDocumentRefused("composition_base_mismatch", None, "--base and document.base differ")
        root = Path(args.root) if args.root else None
        if document["base"] == "saved":
            saved = candidate_from_applied_profile(load_output_topology_strict(), load_applied_baseline_profile_state() or {})
            base = BankedCandidate(saved, "", "", baseline_profile_state_path())
        else:
            base = find_banked_candidate(document["base"], root=root)
        evidence = _document_evidence(args, document)
        candidate = judge_prescription_document(document, base=base, evidence=evidence)
    except PrescriptionDocumentRefused as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True))
        return EXIT_UNREADABLE if exc.code == REASON_UNREADABLE else EXIT_REFUSED
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        print(json.dumps(PrescriptionDocumentRefused(exc.code, None, exc.detail).to_dict(), sort_keys=True))
        return EXIT_REFUSED
    except (CrossoverEvidencePacketError, OSError, ValueError) as exc:
        print(json.dumps(PrescriptionDocumentRefused(REASON_UNREADABLE, None, str(exc)).to_dict(), sort_keys=True))
        return EXIT_UNREADABLE
    answer = {"ok": True, "code": None, "section": None, "next_action": None, "error": None,
              "candidate_fingerprint": candidate.fingerprint,
              "resolution": candidate.analysis["resolution"], "measurement_status": "unmeasured", "adopted": False}
    if args.command == "judge":
        answer["sections"] = candidate.analysis["evidence"]["prescriptions"]
    else:
        try:
            published = publish_authored_candidate(candidate, root=root)
        except CandidateBankRefusal as exc:
            print(json.dumps(PrescriptionDocumentRefused(exc.code, None, exc.detail).to_dict(), sort_keys=True))
            return EXIT_REFUSED
        except (OSError, BundleError) as exc:
            print(json.dumps(PrescriptionDocumentRefused(REASON_UNWRITABLE, None, str(exc)).to_dict(), sort_keys=True))
            return EXIT_WRITE_FAILED
        answer["out"] = str(published.path)
    return answered(answer)


def _load_packet(args: argparse.Namespace, *, inputs: RoundInputs | None = None) -> dict[str, Any]:
    if args.session_dir is None:
        raise CrossoverEvidencePacketError("name a round directory")
    inputs = inputs or round_inputs(Path(args.session_dir))
    return build_crossover_evidence_packet(
        inputs.session_dir, round_context=inputs,
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



def _cmd_contract(args: argparse.Namespace) -> int:
    try:
        sources = _contract_sources(round_inputs(Path(args.round)) if args.round else None)
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



def _band_phrase(lo: float, hi: float) -> str:
    """One frequency span, spelled the one way this tool spells it."""
    return f"{lo:.1f}-{hi:.1f} Hz"



def _passband_phrase(role: str, lo: float, hi: float) -> str:
    """One role's declared band, to whole hertz.

    A manufacturer figure; a tenth would suggest precision it does not have.
    """
    return f"{role} {lo:.0f}-{hi:.0f} Hz"



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
    return {
        "declared": _declared_section(packet, packet_error),
        "banked": _banked_section(packet, packet_error),
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
            PROG, "contract", "--round", session_dir,
        ]))
    # Status discovers candidates; the LLM chooses a compatible shortlist.
    # Staging every retained artifact would turn discovery into an experiment.
    if len(sections["banked"]["candidates"]) > 1:
        commands.append("jasper-angle-capture plan --help")
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
    """Read retained evidence and candidate status."""
    sections = _status_sections(packet, packet_error)
    context: dict[str, Any] = {
        "latest_agent_note": None, "context_error": None,
    }
    recent = []
    try:
        if session_dir:
            context.update(context_artifacts(round_inputs(Path(session_dir)), Path(session_dir)))
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
        evidence=[args.session_dir] if args.session_dir else [], state=args.state,
    ))



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    contract = sub.add_parser("contract", help="schemas and bounds evaluated on a round")
    contract.add_argument("--round", metavar="DIR")
    contract.add_argument("--section", choices=(*SECTIONS, "all"), default="all")
    contract.add_argument("--out", metavar="FILE")
    contract.set_defaults(func=_cmd_contract)
    for verb in ("judge", "compose"):
        command = sub.add_parser(verb, help="judge every section and preview resolution" if verb == "judge" else "judge, prove and bank one candidate")
        command.add_argument("document", metavar="DOC")
        command.add_argument("--round", dest="round", metavar="DIR")
        if verb == "compose":
            command.add_argument("--base", required=True, metavar="FINGERPRINT|saved")
        else:
            command.set_defaults(base=None)
        command.add_argument("--root", help="candidate bank root")
        command.set_defaults(func=_cmd_document)
    status = sub.add_parser("status", help="read declared, banked and applied state")
    status.add_argument("session_dir", nargs="?")
    for name in ("state", "drivers", "applied-profile", "repeat-floor", "declared-geometry"):
        status.add_argument(f"--{name}")
    status.set_defaults(func=_cmd_status)
    for command in (status, *[sub.choices[v] for v in ("judge", "compose")]):
        command.set_defaults(state=None, drivers=None, applied_profile=None, repeat_floor=None, declared_geometry=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"judge", "compose"}:
        args.session_dir = args.round
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
