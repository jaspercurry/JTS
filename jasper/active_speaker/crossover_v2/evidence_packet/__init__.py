# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One round's banked evidence, gathered into one document a reader can answer.

Grades nothing and writes nothing. Its one impurity is reading JSON files:
the round's own, and views filed beside it — the classification and H2/H3
views into :data:`DERIVED_VIEWS`, which the fingerprint skips, and the room
view into ``contracts``, which it covers (a banked round's contracts read
the bank's copy, ADR-0371). No clock, no network, no CamillaDSP handle, no
session.

Absence is a code, never merged with another: ``source_absent`` (the artifact
was not handed to this builder), ``source_unreadable`` (it was, and could not
be read) and ``field_null`` (it was read, and the field is null). The sentence
behind a code rides in ``detail``. Redaction is an allowlist that publishes the names it withheld. Operator prose
enters in exactly one block, quarantined and named in ``privacy`` — see
:func:`_operator_notes_block`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.design_draft import design_draft_view
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
from jasper.audio_measurement.evidence_reasons import EVIDENCE_NOT_BANKED
from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE, load_declared_geometry
from jasper.platform.json_fields import as_mapping

from ...installation import installation_evidence
from ..driver_prescription import driver_passbands_from_safety_profile
from ..operator_notes import OPERATOR_NOTES_KIND, build_operator_notes
from ..prescription_contract import (
    CONTRACT_COMMAND,
    SECTIONS,
    contract_digests,
    contract_programs,
    prescription_contracts,
)
from ..record_index import Measurement, bundle_measurements
from ..round_inputs import (
    NO_ROUND_ARTIFACTS_REASON,
    ROUND_INPUT_ERRORS,
    STATE_SESSION_UNKNOWN,
    CrossoverEvidencePacketError,
    RoundInputs,
    RoundViewsError,
    bank_of,
    banked_packet,
    contract_sources,
    round_artifact_dir,
    round_inputs,
    state_matches_capture,
)
from .incumbent import (
    STRUCTURAL_HISTORY_AXES,
    _incumbent_block,
    _structural_history_block,
    applied_profile_source,
    read_applied_profile,
)
from .offline_reads import (
    CLASSIFICATION_ARTIFACT,
    HARMONICS_ARTIFACT,
    RING_SIDECAR_GLOB,
    _derived_views_block,
    absence,
    copy_allowed,
    read_json,
    round_program_dir,
)
from .positions import (
    POSITIONS_SUBDIR,
    _lateral_poses_block,
)
from .readers import (
    DERIVED_VIEWS,
    PACKET_KIND,
    PACKET_SCHEMA_VERSION,
    _fingerprint,
    fingerprinted,
    packet_driver_passbands_hz,
    packet_feature_classifications,
)
from .uncertainty import (
    REPEAT_FLOOR_UNMEASURED,
    REPEAT_FLOOR_UNREADABLE,
    REPEAT_FLOOR_UNUSABLE,
    _accuracy_budget_block,
    _capture_snr_block,
    _repeat_floor_source,
)

__all__ = [
    "CLASSIFICATION_ARTIFACT",
    "DERIVED_VIEWS",
    "HARMONICS_ARTIFACT",
    "NO_CANDIDATE_TAKES",
    "NO_ROUND_ARTIFACTS_REASON",
    "OPERATOR_NOTES_BLOCK",
    "PACKET_KIND",
    "PACKET_SCHEMA_VERSION",
    "RING_SIDECAR_GLOB",
    "CrossoverEvidencePacketError",
    "EVIDENCE_KEY",
    "EVIDENCE_NOT_BANKED",
    "build_crossover_evidence_packet",
    "build_round_evidence",
    "contract_currency",
    "fingerprinted",
    "round_evidence",
    "packet_driver_passbands_hz",
    "packet_feature_classifications",
    "round_artifact_dir",
    "round_program_dir",
    "applied_profile_source",
    "REPEAT_FLOOR_UNMEASURED",
    "REPEAT_FLOOR_UNREADABLE",
    "REPEAT_FLOOR_UNUSABLE",
    "STRUCTURAL_HISTORY_AXES",
]

#: ``packet.json``'s copy of the packet its bank built, less the derived views
#: and the fingerprint, which ``packet.json`` carries beside it.
EVIDENCE_KEY = "evidence"

#: The one block that carries operator prose. Named in ``privacy`` so the
#: document points at its own quarantine, and asserted to RESOLVE by
#: ``test_the_packet_points_at_its_own_quarantine``.
OPERATOR_NOTES_BLOCK = "operator_notes"

GENERATED_BY = (
    "jasper.active_speaker.crossover_v2.evidence_packet."
    "build_crossover_evidence_packet"
)

#: Bundle identity fields copied verbatim from ``info.json``'s ``fingerprints``
#: block. The mic sub-block is copied whole: it carries a calibration id and a
#: content hash, never a serial (``household_mic`` keeps only a one-way
#: ``serial_hash`` and a last-4 display, and neither is in this tree).
_IDENTITY_FIELDS = (
    "topology_id",
    "topology_fingerprint",
    "output_assignments",
    "graph_fingerprint",
    "mic",
    "build_sha",
)

#: Flow-state fields the packet withholds. ``household_findings`` is
#: household-authored prose, the one privacy-sensitive field in the tree. The
#: operator prose in :data:`OPERATOR_NOTES_BLOCK` is the opposite decision, and
#: the difference is the WRITER: a commissioning declaration about the hardware
#: being graded, not copy a household typed into a correction carve-out.
_STATE_WITHHELD = ("household_findings",)


def _declared_geometry_block(path: Path | None) -> dict[str, Any]:
    """The household's own tape measure, in metres, or WHICH absence this is.

    Read from the path the CALLER resolved — a banked round's frozen sibling,
    or the live speaker's own SSOT file. ``None`` is a round that banked no
    declaration, which is a different fact from one this install could not
    read.
    """

    if path is None:
        return absence("source_absent", False, "declared_geometry")
    try:
        geometry = load_declared_geometry(path)
    except (OSError, ValueError) as exc:
        refused = getattr(exc, "field", None)
        return {**absence(DECLARED_GEOMETRY_UNREADABLE, False, "declared_geometry", f"unreadable: {type(exc).__name__}"),
                **({"refused_field": refused} if refused else {})}
    if geometry is None:
        return absence("source_absent", False, "declared_geometry")
    return geometry.to_dict()

#: No banked take names a candidate. A candidate scope refuses a spec without
#: one (``MeasureSpec``), so the round cycled no candidates; none lost a label.
NO_CANDIDATE_TAKES = "no_candidate_takes"


def _candidates_block(rows: Sequence[Measurement]) -> dict[str, Any]:
    """Which candidates this round played, and at which poses.

    Selected on the take index's ``candidate_id`` column across EVERY phase:
    the engine's own capture record carries a candidate id and no phase at all,
    so a phase-narrowed selection would miss the takes a candidate cycle banks.
    An INVENTORY, not a verdict.
    """
    labelled = [row for row in rows if row.candidate_id]
    if not labelled:
        return absence(NO_CANDIDATE_TAKES, False, "banked takes' candidate_id")
    by_candidate: dict[str, list[Measurement]] = {}
    for row in labelled:
        by_candidate.setdefault(row.candidate_id, []).append(row)
    candidates = []
    for candidate_id in sorted(by_candidate):
        takes = by_candidate[candidate_id]
        poses = sorted(
            {(row.position_deg, row.vertical_deg) for row in takes},
            # A take with no commanded bearing sorts last rather than raising
            # against the ints beside it.
            key=lambda pose: (pose[0] is None, pose[0] or 0, pose[1]),
        )
        candidates.append({
            "candidate_id": candidate_id,
            "n_takes": len(takes),
            "poses": [
                {"position_deg": position_deg, "vertical_deg": vertical_deg}
                for position_deg, vertical_deg in poses
            ],
        })
    return {
        "status": "available",
        "candidates": candidates,
        "source": (
            f"{POSITIONS_SUBDIR}/<take_id>.json candidate_id, selected through "
            "record_index.bundle_measurements"
        ),
        "note": (
            "two candidates measured at different poses are not comparable on "
            "these takes alone; the candidate cycle holds one pose and swaps "
            "the graph under it"
        ),
    }


def _drivers_block(draft: Mapping[str, Any], reason: str, detail: str) -> dict[str, Any]:
    """Each role's own declared band — the bound a per-driver filter sits inside.

    Read from the design draft's computed ``driver_safety_profile`` and
    composed by
    :func:`~.driver_prescription.driver_passbands_from_safety_profile`, so the
    packet reports a band it does not also define.

    Deliberately NOT derived from the crossover:
    ``branch_chain.radiating_band_hz`` is the band this driver is within 3 dB
    of full output over — the bound on a LIFT, narrower than the driver — and
    the whole driver is meant to be correctable.
    """
    profile = as_mapping(design_draft_view(draft).get("driver_safety_profile"))
    passbands = driver_passbands_from_safety_profile(profile)
    absent = absence(reason, bool(passbands), "driver_safety_profile.targets", detail)
    if absent:
        return absent
    return {
        "status": "available",
        "passbands_hz": {
            role: [lo, hi] for role, (lo, hi) in sorted(passbands.items())
        },
        "source": (
            "design_draft.driver_safety_profile.targets[].measurement_band_hz, "
            "floored/capped by that target's own required_protection_filters"
        ),
        "issues": profile.get("issues", []),
        "note": (
            "the driver's published response range narrowed by whatever "
            "protective corners it declares. A per-driver prescription's "
            "filters must sit inside the band of the role they name"
        ),
    }


def _operator_notes_block(draft: Mapping[str, Any], reason: str, detail: str) -> dict[str, Any]:
    """The operator's own words, quarantined from every decision in code."""
    artifact = build_operator_notes(draft)
    absent = absence(
        reason, artifact["status"] == "available", "design_draft.operator_prose", detail
    )
    return {**artifact, **absent} if absent else artifact


def _not_evaluated(
    *,
    state_reason: str,
    applied_profile_reason: str,
    drivers_available: bool,
    lateral_poses_available: bool,
    candidates_available: bool,
    capture_snr_reason: str,
) -> list[dict[str, Any]]:
    """Everything this packet could not answer, and why — one honest list.

    Entries whose absence is a property of the CORPUS are stated whether or not
    this particular session was complete.
    """
    entries: list[dict[str, Any]] = [
        # A property of the CORPUS — nothing in the package ANALYSES an
        # elevation, whoever wrote the artifact — so it belongs here rather
        # than as a per-row flag two producers spell differently. It
        # DISCLOSES and refuses nothing. The claim is about what this packet
        # READS, never about what a round banked.
        {
            "field": "vertical_plane_response",
            "reason": (
                CONTRACT_COMMAND
            ),
        },
    ]
    if not lateral_poses_available:
        # Narrow by construction: a lateral walk banks a signed whole-degree
        # bearing per pose, so the only true claim is about THIS round.
        entries.append({
            "field": "lateral_poses[].position_deg",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if not candidates_available:
        entries.append({
            "field": "candidates",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if capture_snr_reason:
        entries.append({
            "field": "capture_snr",
            "reason": capture_snr_reason,
        })
    if not drivers_available:
        entries.append({
            "field": "drivers.passbands_hz",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if state_reason:
        entries.append({"field": "flow_state", "reason": state_reason})
    if applied_profile_reason:
        entries.append({
            "field": "incumbent",
            "reason": (
                f"{applied_profile_reason}; without the applied-profile SSOT "
                "this packet cannot name the correction the graph is already "
                "carrying, so a per-driver prescription's displacement is "
                "unknown rather than zero"
            ),
        })
    return entries


def build_crossover_evidence_packet(
    session_dir: Path,
    *,
    round_context: RoundInputs | None = None,
    state_path: Path | None = None,
    driver_draft_path: Path | None = None,
    applied_profile_path: Path | None = None,
    repeat_floor_path: Path | None = None,
    declared_geometry_path: Path | None = None,
    statefile_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one round's banked evidence into one versioned document.

    ``session_dir`` is a commissioning bundle: an ``info.json`` beside an
    ``evidence/v1/artifacts/crossover_v2/<capture-session-id>/`` directory
    holding the run manifest and the per-take records.

    Every other path is OPTIONAL and INJECTED rather than resolved here — a
    path resolved here would make a round's answer, and its
    ``packet_fingerprint``, depend on whatever the BUILDING machine happens to
    have. Each absence is reported in the
    ``not_evaluated`` block rather than papered over, and each costs the packet
    something specific:

    * ``state_path`` — the flow state file (``jts_crossover_v2_flow_state``),
      banked outside the bundle; without it, no capture calibration and no
      re-solved trim.
    * ``applied_profile_path`` — the applied-baseline-profile SSOT, which
      answers "what is this speaker playing" for ``incumbent``; without it a
      per-driver prescription's displacement is ``unknown`` rather than
      guessed. See :func:`_incumbent_block` for why the flow state cannot
      stand in for it.
    * ``driver_draft_path`` — the design draft used to compute driver limits;
      without it the per-driver prescription class has no bound to check
      against and refuses by name.
    * ``repeat_floor_path`` — the banked repeat floor; without it the floor
      reads ``unmeasured`` and publishes no thresholds.
    * ``declared_geometry_path`` — the household's declared rig geometry, the
      only viable source for the room's entanglement floor.
    * ``statefile_path`` — a CamillaDSP durable statefile banked alongside
      ``applied_profile_path``; without it ``incumbent.identity``'s
      ``applied_profile_displacement`` (#2537, #3316) is unknown rather than a
      live read of whatever statefile the reading machine happens to have.

    Raises :class:`CrossoverEvidencePacketError` only when ``session_dir`` is
    not a crossover-v2 session bundle at all: a partially banked round is a
    normal thing to want to read.
    """
    if not session_dir.is_dir():
        raise CrossoverEvidencePacketError(f"not a directory: {session_dir}")
    info_raw = _bundle_info(session_dir)
    round_dir, round_reason = round_artifact_dir(session_dir)
    if round_dir is None:
        raise CrossoverEvidencePacketError(f"{round_reason}: {session_dir}")

    inputs = round_context or round_inputs(session_dir)

    state_raw: Any = None
    state_reason = "no flow state file was supplied"
    if state_path is not None:
        state_raw, read_reason, read_detail = read_json(state_path)
        state_reason = read_detail or read_reason
    state = as_mapping(state_raw)
    state_withheld = sorted(key for key in _STATE_WITHHELD if key in state)
    if state and not state_matches_capture(state, round_dir.name):
        state, state_reason = {}, STATE_SESSION_UNKNOWN

    applied_profile, profile_reason, profile_detail = read_applied_profile(applied_profile_path)
    repeat_floor, repeat_floor_reason = _repeat_floor_source(repeat_floor_path)

    draft_raw: Any = None
    draft_reason, draft_detail = "source_absent", "no driver design draft was supplied"
    if driver_draft_path is not None:
        draft_raw, draft_reason, draft_detail = read_json(driver_draft_path)
    drivers = _drivers_block(as_mapping(draft_raw), draft_reason, draft_detail)
    operator_notes = _operator_notes_block(as_mapping(draft_raw), draft_reason, draft_detail)
    take_rows = bundle_measurements(session_dir)
    lateral_poses = _lateral_poses_block(session_dir, take_rows)
    candidates = _candidates_block(take_rows)

    capture_snr = _capture_snr_block(session_dir, take_rows)

    identity, identity_withheld = copy_allowed(
        as_mapping(info_raw.get("fingerprints")), _IDENTITY_FIELDS
    )
    packet: dict[str, Any] = {
        "artifact_schema_version": PACKET_SCHEMA_VERSION,
        "kind": PACKET_KIND,
        "generated_by": GENERATED_BY,
        "privacy": {
            "raw_audio_excluded": True,
            "absolute_paths_excluded": True,
            "household_prose_excluded": True,
            "operator_prose_quarantined_to": OPERATOR_NOTES_BLOCK,
            "operator_prose_kind": OPERATOR_NOTES_KIND,
            "secrets_excluded": True,
            "microphone_serials_excluded": True,
            "withheld_state_fields": state_withheld,
            "note": (
                "captures are referenced by wav_sha256, never by path or "
                "content"
            ),
        },
        "session": {
            **_bundle_session(info_raw),
            "capture_session_id": round_dir.name,
            "declared_geometry": _declared_geometry_block(declared_geometry_path),
            "note": (
                "bundle_session_id and capture_session_id are different id "
                "namespaces; the round artifacts are filed under the capture id"
            ),
        },
        "identity": {
            **identity,
            "placement": as_mapping(info_raw.get("placement")),
            "redacted_fields": identity_withheld,
            "calibration": as_mapping(as_mapping(state.get("evidence")).get("calibration")),
        },
        "incumbent": _incumbent_block(
            applied_profile,
            profile_reason,
            profile_detail,
            state,
            statefile_path,
        ),
        "lateral_poses": lateral_poses,
        "candidates": candidates,
        "capture_snr": capture_snr,
        "accuracy_budget": _accuracy_budget_block(
            round_dir=round_dir,
            repeat_floor=repeat_floor,
            repeat_floor_reason=repeat_floor_reason,
        ),
        "structural_history": _structural_history_block(session_dir),
        "drivers": drivers,
        "operator_notes": operator_notes,
        "installation": installation_evidence(as_mapping(draft_raw)),
        "not_evaluated": _not_evaluated(
            state_reason=state_reason,
            applied_profile_reason=profile_detail or profile_reason,
            drivers_available=drivers["status"] == "available",
            lateral_poses_available=lateral_poses["status"] == "available",
            candidates_available=candidates["status"] == "available",
            capture_snr_reason=str(capture_snr.get("detail") or ""),
        ),
        "contracts": _contract_digests(inputs, round_dir, as_mapping(draft_raw), applied_profile),
        DERIVED_VIEWS: _derived_views_block(inputs),
    }
    packet["packet_fingerprint"] = _fingerprint(packet)
    return packet


def _contract_digests(inputs: RoundInputs, round_dir: Path, draft: Mapping[str, Any],
                      applied_profile: dict[str, Any] | None) -> dict[str, Any]:
    """Each contract section's digest. A section the round's banked candidate refuses is a gap
    with that refusal's code, so the packet still builds from banked inputs (ADR-0371)."""
    receipt = read_json(round_dir / "round_receipt.json")[0]
    sources = {**contract_sources(inputs), "draft": draft, "receipt": as_mapping(receipt),
               "applied_profile": applied_profile or {}}
    programs = contract_programs(sources)
    digests: dict[str, Any] = {}
    for name in (section for section in SECTIONS if section in programs):
        try:
            digests.update(contract_digests(prescription_contracts(programs=(name,), **sources)))
        except MeasuredCrossoverCandidateError as exc:
            digests[name] = absence(exc.code, False, "candidate.json")
    return digests


def contract_currency(inputs: RoundInputs) -> dict[str, Any] | None:
    """Whether the contract digests a banked round's packet stores are the ones its bank's
    inputs give under the code that runs now.

    ``None`` when no bank stored the round's contracts.
    """
    # See ADR-0371
    bank = bank_of(inputs)
    banked = inputs if inputs.banked or bank is None else round_inputs(bank)
    stored = as_mapping(banked_packet(banked).get(EVIDENCE_KEY)).get("contracts")
    round_dir, _ = round_artifact_dir(banked.session_dir)
    if not isinstance(stored, dict) or round_dir is None:
        return None
    try:
        draft = read_json(banked.design_draft_path)[0] if banked.design_draft_path else None
        now = _contract_digests(banked, round_dir, as_mapping(draft), applied_profile_source(banked.applied_profile_path)[0])
    except ROUND_INPUT_ERRORS as exc:
        return {"contract_current": None, "stored": stored, "now": None, "error": str(exc)}
    return {"contract_current": stored == now, "stored": stored, "now": now}


def build_round_evidence(inputs: RoundInputs, *, state_path: Path | None = None) -> dict[str, Any]:
    """The packet built from the round's own inputs, as its bank builds it.

    Never its flow state or CamillaDSP statefile, which a live session rewrites
    as it runs (#3316); ``state_path`` is a ``status`` what-if's.
    """
    return build_crossover_evidence_packet(
        inputs.session_dir, round_context=inputs, state_path=state_path,
        driver_draft_path=inputs.design_draft_path, applied_profile_path=inputs.applied_profile_path,
        repeat_floor_path=inputs.repeat_floor_path, declared_geometry_path=inputs.declared_geometry_path,
    )


def _bundle_info(session_dir: Path) -> dict[str, Any]:
    info_raw, info_reason, info_detail = read_json(session_dir / "info.json")
    if not isinstance(info_raw, dict):
        raise CrossoverEvidencePacketError(f"bundle missing a readable info.json ({info_detail or info_reason}): {session_dir}")
    return info_raw


def _bundle_session(info_raw: Mapping[str, Any]) -> dict[str, Any]:
    """The ``session`` fields the bundle's own ``info.json`` states."""
    return {
        "bundle_session_id": info_raw.get("session_id"),
        "state": info_raw.get("state"),
        "started_at": info_raw.get("started_at"),
    }


def round_evidence(inputs: RoundInputs) -> dict[str, Any]:
    """The round's packet as its bank stored it in ``packet.json``, derived views read now.

    A live session, which no bank holds, is built from its inputs. A banked
    round whose ``packet.json`` holds no :data:`EVIDENCE_KEY` refuses
    :data:`EVIDENCE_NOT_BANKED`.
    """
    # See ADR-0371, ADR-0383
    bank = bank_of(inputs)
    if bank is None:
        return build_round_evidence(inputs)
    stored = banked_packet(inputs)
    evidence = stored.get(EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        raise RoundViewsError(f"{bank}: packet.json holds no {EVIDENCE_KEY}", code=EVIDENCE_NOT_BANKED)
    return {**evidence, DERIVED_VIEWS: _derived_views_block(inputs), "packet_fingerprint": stored.get("packet_fingerprint")}
