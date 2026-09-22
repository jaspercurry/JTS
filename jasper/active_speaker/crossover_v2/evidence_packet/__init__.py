# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One round's banked evidence, gathered into one document a reader can answer.

Grades nothing and writes nothing. Its one impurity is reading JSON files
under a directory: no clock, no network, no CamillaDSP handle, no session. It
DERIVES exactly two things — :func:`_cross_seat_sigma_block`'s per-bin spread
across seats, and :func:`_reflections_block`'s tau-to-path-length multiply.

Absence has two never-merged flavours: ``source_absent`` (the artifact was not
handed to this builder) and ``field_null`` (it was, and the field is null).
Redaction is an allowlist that publishes the names it withheld. Operator prose
enters in exactly one block, quarantined and named in ``privacy`` — see
:func:`_operator_notes_block`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.design_draft import design_draft_view
from jasper.audio_measurement.measurement_geometry import load_declared_geometry
from jasper.audio_measurement.program_analysis import ABSOLUTE_NO_CROSSOVER_TOPOLOGY

from ...installation import installation_evidence
from .. import position_cycle
from ..driver_prescription import driver_passbands_from_safety_profile
from ..journey import PHASE_ENTRY_BASELINE
from ..operator_notes import OPERATOR_NOTES_KIND, build_operator_notes
from ..prescription_contract import (
    CONTRACT_COMMAND,
    contract_digests,
    contract_programs,
    prescription_contracts,
)
from ..record_index import Measurement, bundle_measurements
from ..round_inputs import (
    NO_ROUND_ARTIFACTS_REASON,
    STATE_SESSION_UNKNOWN,
    CrossoverEvidencePacketError,
    RoundInputs,
    contract_sources,
    round_artifact_dir,
    state_matches_capture,
)
from .incumbent import (
    STRUCTURAL_HISTORY_AXES,
    _incumbent_block,
    _read_candidate,
    _structural_history_block,
    applied_profile_source,
)
from .offline_reads import (
    CLASSIFICATION_ARTIFACT,
    HARMONICS_ARTIFACT,
    RING_SIDECAR_GLOB,
    _absence,
    _classification_block,
    _copy_allowed,
    _findings_block,
    _harmonics_block,
    _mapping,
    _read_json,
    round_program_dir,
)
from .positions import (
    _POSITIONS_SUBDIR,
    _banked_takes,
    _lateral_poses_block,
    _positions_block,
)
from .readers import (
    PACKET_KIND,
    PACKET_SCHEMA_VERSION,
    PacketSchemaUnsupported,
    _fingerprint,
    packet_driver_passbands_hz,
    packet_feature_classifications,
    packet_incumbent_linearization,
    packet_positional_evidence,
    packet_region_band_hz,
    validate_packet,
)
from .uncertainty import (
    REPEAT_FLOOR_UNMEASURED,
    REPEAT_FLOOR_UNREADABLE,
    REPEAT_FLOOR_UNUSABLE,
    _accuracy_budget_block,
    _capture_snr_block,
    _gate_numbers_reason,
    _reflections_block,
    _repeat_floor_source,
)

__all__ = [
    "CLASSIFICATION_ARTIFACT",
    "HARMONICS_ARTIFACT",
    "NO_CANDIDATE_TAKES",
    "NO_ROUND_ARTIFACTS_REASON",
    "OPERATOR_NOTES_BLOCK",
    "validate_packet",
    "PACKET_KIND",
    "PACKET_SCHEMA_VERSION",
    "RING_SIDECAR_GLOB",
    "CrossoverEvidencePacketError",
    "build_crossover_evidence_packet",
    "packet_driver_passbands_hz",
    "packet_feature_classifications",
    "packet_incumbent_linearization",
    "packet_positional_evidence",
    "packet_region_band_hz",
    "round_artifact_dir",
    "round_program_dir",
    "applied_profile_source",
    "_mapping",
    "_read_candidate",
    "PacketSchemaUnsupported",
    "REPEAT_FLOOR_UNMEASURED",
    "REPEAT_FLOOR_UNREADABLE",
    "REPEAT_FLOOR_UNUSABLE",
    "STRUCTURAL_HISTORY_AXES",
]

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

#: Verify-claim and state fields the packet carries. ``household_findings`` is
#: NOT among them and never will be: it is household-authored prose, the one
#: privacy-sensitive field in the tree. The operator prose in
#: :data:`OPERATOR_NOTES_BLOCK` is the opposite decision, and the difference is
#: the WRITER: a commissioning declaration about the hardware being graded,
#: not copy a household typed into a correction carve-out.
_STATE_WITHHELD = ("household_findings",)


def _declared_geometry_block(path: Path | None) -> dict[str, Any]:
    """The household's own tape measure, in metres, or WHICH absence this is.

    Read from the path the CALLER resolved — a banked round's frozen sibling,
    or the live speaker's own SSOT file. ``None`` is a round that banked no
    declaration, which is a different fact from one this install could not
    read.
    """

    if path is None:
        return _absence("source_absent", False, "declared_geometry")
    try:
        geometry = load_declared_geometry(path)
    except (OSError, ValueError) as exc:
        return _absence(f"unreadable: {type(exc).__name__}", False, "declared_geometry")
    if geometry is None:
        return _absence("source_absent", False, "declared_geometry")
    return geometry.to_dict()

#: Why there is no block at all: no banked take names a candidate. The
#: ``jasper-measure`` door refuses to bank a variant take without one, so this
#: is a round that cycled no candidates rather than one that lost their labels.
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
        return {
            "available": False,
            **_absence(NO_CANDIDATE_TAKES, False, "banked takes' candidate_id"),
        }
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
        "available": True,
        "candidates": candidates,
        "source": (
            f"{_POSITIONS_SUBDIR}/<take_id>.json candidate_id, selected through "
            "record_index.bundle_measurements"
        ),
        "note": (
            "two candidates measured at different poses are not comparable on "
            "these takes alone; the candidate cycle holds one pose and swaps "
            "the graph under it"
        ),
    }


def _entry_baseline_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """The round's measured "before", read from the take that banked it.

    The receipt names this capture but carries no curve, so this block is the
    durable copy — the flow state file's arrays are rewritten by the next
    persist. With it, ``verification.evaluate_benefit`` can be re-run over a
    banked round by an analysis that did not exist when it was captured.

    A round with no readable take is an ordinary reported absence: retention is
    fail-soft and never costs the household a retake.
    """
    takes = _banked_takes(
        session_dir, rows, PHASE_ENTRY_BASELINE,
        position_cycle.read_entry_baseline_take,
    )
    if not takes:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                f"this round banked no {PHASE_ENTRY_BASELINE} take record under "
                f"{_POSITIONS_SUBDIR}/ — it ran no entry baseline, its capture "
                "was refused, or evidence retention failed at take time"
            ),
        }
    # The last accepted take is the "before": a retake supersedes the attempt
    # it followed. Sorting by take_id orders by index then attempt, because the
    # id is built from both in that order.
    take = max(takes, key=lambda t: str(t.get("artifact_ref") or ""))
    return {
        "available": True,
        **take,
        "n_bins": len(take["freqs_hz"]),
        "n_excluded": sum(1 for flag in take["excluded"] if flag),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json",
        "note": (
            "the summed capture taken at the design-axis mark immediately "
            "before this round's apply. It is the durable copy: the flow state "
            "file holds the same arrays only until the next persist rewrites "
            "them. Comparable to a post-apply capture only when program_id, "
            "reference_mark and graph_fingerprint match on both sides"
        ),
    }


def _region_block(receipt: dict[str, Any], reason: str) -> tuple[dict[str, Any], bool]:
    """The crossover region a proposal must sit inside, and whether it exists.

    ``round_measurements.blend.band_hz`` is the VERIFY absolute claim's own
    band, which decision 10 also makes the region the blend correction is
    solved and graded over, so a prescription is checked against the band the
    deterministic solver was bounded by rather than a second derivation.
    """
    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    band = blend.get("band_hz")
    shape = band is None and blend.get("reason") == ABSOLUTE_NO_CROSSOVER_TOPOLOGY
    band_field = "round_measurements.blend.band_hz"
    absent = _absence(
        ABSOLUTE_NO_CROSSOVER_TOPOLOGY if shape else reason, band is not None, band_field
    )
    if absent:
        return {"available": False, **absent}, shape
    return {
        "available": True,
        "band_hz": band,
        "source": "round_receipt.round_measurements.blend.band_hz",
        "note": (
            "the VERIFY absolute claim's band, which is also the region the "
            "deterministic blend correction is solved and graded over"
        ),
    }, shape


def _verify_block(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Per-claim verdicts, copied verbatim including their ``not_evaluated``.

    These live only in the flow state, never in the bundle — the receipt's
    ``verification`` block is a different, coarser record.
    """
    verify = state.get("verify")
    absent = _absence(reason, isinstance(verify, dict), "verify")
    if absent:
        return {"available": False, **absent}
    verify = _mapping(verify)
    return {
        "available": True,
        "outcome": verify.get("outcome"),
        "graded_band_hz": verify.get("graded_band_hz"),
        "claims": _mapping(verify.get("claims")),
        "gate": _mapping(verify.get("gate")),
        "delta_probe": _mapping(verify.get("delta_probe")),
    }


def _drivers_block(draft: dict[str, Any], reason: str) -> dict[str, Any]:
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
    profile = _mapping(design_draft_view(draft).get("driver_safety_profile"))
    passbands = driver_passbands_from_safety_profile(profile)
    absent = _absence(reason, bool(passbands), "driver_safety_profile.targets")
    if absent:
        return {"available": False, **absent}
    return {
        "available": True,
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


def _operator_notes_block(draft: dict[str, Any], reason: str) -> dict[str, Any]:
    """The operator's own words, quarantined from every decision in code."""
    artifact = build_operator_notes(draft)
    absent = _absence(
        reason, bool(artifact["available"]), "design_draft.operator_prose"
    )
    return {**artifact, **absent} if absent else artifact


def _not_evaluated(
    *,
    receipt_reason: str,
    cloud_reason: str,
    state_reason: str,
    applied_profile_reason: str,
    classification_available: bool,
    drivers_available: bool,
    lateral_poses_available: bool,
    candidates_available: bool,
    capture_snr_reason: str,
    cross_seat_sigma_reason: str,
    harmonics_reason: str,
    gate_numbers_reason: str,
    reflector_path_reason: str,
    findings: dict[str, Any],
    no_crossover: bool,
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
        # bearing per pose, so the only true claim is about THIS round. It
        # speaks for no cloud seat either — those stamp their own
        # ``position_deg`` — and points at the block that does.
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
    if gate_numbers_reason:
        # Names both numbers rather than only the reflection time: they are
        # banked together by ``spatial.cloud_position_record`` and
        # ``capture_dispatch._gate_record``, and they go missing together.
        entries.append({
            "field": "positions[].gate_reflection_delay_ms",
            "reason": gate_numbers_reason,
        })
    if reflector_path_reason:
        entries.append({
            "field": "reflections.reflector_path_distance_m",
            "reason": reflector_path_reason,
        })
    if capture_snr_reason:
        entries.append({
            "field": "capture_snr",
            "reason": capture_snr_reason,
        })
    if cross_seat_sigma_reason:
        entries.append({
            "field": "positions.cross_seat_sigma",
            "reason": cross_seat_sigma_reason,
        })
    if harmonics_reason:
        entries.append({
            "field": "harmonics",
            "reason": harmonics_reason,
        })
    if not classification_available:
        entries.append({
            "field": "per_bin_minimum_phase_class",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if not drivers_available:
        entries.append({
            "field": "drivers.passbands_hz",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if receipt_reason:
        entries.append({"field": "round_receipt", "reason": receipt_reason})
    if cloud_reason:
        entries.append({"field": "cloud_verify", "reason": cloud_reason})
    if state_reason:
        entries.append({
            "field": "flow_state",
            "reason": f"{state_reason}; per-claim verify verdicts live only here",
        })
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
    summary = _mapping(findings.get("summary"))
    if not any(_mapping(summary.get("finding_count")).values()):
        entries.append({
            "field": "findings",
            "reason": (
                CONTRACT_COMMAND
            ),
        })
    if no_crossover:
        entries.append({"field": "crossover_region.band_hz", "reason": ABSOLUTE_NO_CROSSOVER_TOPOLOGY})
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
    holding the round receipt, the cloud evidence, each phase's finding set
    and the per-position records.

    Every other path is OPTIONAL and INJECTED rather than resolved here — this
    packet is rebuilt by every reader, and a path resolved here would make a
    banked round's answer, and its ``packet_fingerprint``, depend on whatever
    the READING machine happens to have. Each absence is reported in the
    ``not_evaluated`` block rather than papered over, and each costs the packet
    something specific:

    * ``state_path`` — the flow state file (``jts_crossover_v2_flow_state``),
      banked outside the bundle; without it, no per-claim verify verdicts and
      no Fc selection.
    * ``applied_profile_path`` — the applied-baseline-profile SSOT, which
      answers "what is this speaker playing" for ``incumbent``; without it a
      per-driver prescription's displacement is ``unknown`` rather than
      guessed. See :func:`_incumbent_block` for why the flow state cannot
      stand in for it.
    * ``driver_draft_path`` — the design draft used to compute driver limits;
      without it the per-driver prescription class has no bound to check
      against and refuses by name.
    * ``repeat_floor_path`` — the banked repeat floor; without it the floor is
      unmeasured and the two codified assumptions are used, named.
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
    info_raw, info_reason = _read_json(session_dir / "info.json")
    if not isinstance(info_raw, dict):
        raise CrossoverEvidencePacketError(
            f"bundle missing a readable info.json ({info_reason}): {session_dir}"
        )
    round_dir, round_reason = round_artifact_dir(session_dir)
    if round_dir is None:
        raise CrossoverEvidencePacketError(f"{round_reason}: {session_dir}")

    receipt_raw, receipt_reason = _read_json(round_dir / "round_receipt.json")
    cloud_raw, cloud_reason = _read_json(round_dir / "cloud_verify.json")
    classification_raw, classification_reason = _read_json(
        round_dir / CLASSIFICATION_ARTIFACT
    )
    harmonics_raw, harmonics_reason = _read_json(round_dir / HARMONICS_ARTIFACT)
    receipt = _mapping(receipt_raw)
    cloud = _mapping(cloud_raw)
    findings = _findings_block(round_dir, cloud)

    state_raw: Any = None
    state_reason = "no flow state file was supplied"
    if state_path is not None:
        state_raw, read_reason = _read_json(state_path)
        state_reason = read_reason
    state = _mapping(state_raw)
    state_withheld = sorted(key for key in _STATE_WITHHELD if key in state)
    if state and not state_matches_capture(state, round_dir.name):
        state, state_reason = {}, STATE_SESSION_UNKNOWN

    applied_profile, applied_profile_reason = applied_profile_source(
        applied_profile_path
    )
    repeat_floor, repeat_floor_reason = _repeat_floor_source(repeat_floor_path)

    draft_raw: Any = None
    draft_reason = "no driver design draft was supplied"
    if driver_draft_path is not None:
        draft_raw, draft_reason = _read_json(driver_draft_path)
    drivers = _drivers_block(_mapping(draft_raw), draft_reason)
    operator_notes = _operator_notes_block(_mapping(draft_raw), draft_reason)
    classification = _classification_block(classification_raw, classification_reason)
    harmonics = _harmonics_block(harmonics_raw, harmonics_reason)
    take_rows = bundle_measurements(session_dir)
    lateral_poses = _lateral_poses_block(session_dir, take_rows)
    candidates = _candidates_block(take_rows)
    entry_baseline = _entry_baseline_block(session_dir, take_rows)

    capture_snr = _capture_snr_block(session_dir, take_rows)

    identity, identity_withheld = _copy_allowed(
        _mapping(info_raw.get("fingerprints")), _IDENTITY_FIELDS
    )
    spec = _mapping(cloud.get("spec"))
    positions = _positions_block(cloud)
    cross_seat_sigma = _mapping(positions.get("cross_seat_sigma"))
    verify = _verify_block(state, state_reason)
    reflections = _reflections_block(cloud, cloud_reason)
    crossover_region, no_crossover = _region_block(receipt, receipt_reason)
    sources = {**contract_sources(round_context or session_dir), "draft": _mapping(draft_raw),
               "receipt": receipt, "applied_profile": applied_profile or {}}

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
            "bundle_session_id": info_raw.get("session_id"),
            "capture_session_id": round_dir.name,
            "state": info_raw.get("state"),
            "started_at": info_raw.get("started_at"),
            "round_id": receipt.get("round_id"),
            "declared_geometry": _declared_geometry_block(declared_geometry_path),
            "note": (
                "bundle_session_id and capture_session_id are different id "
                "namespaces; the round artifacts are filed under the capture id"
            ),
        },
        "identity": {
            **identity,
            "placement": _mapping(info_raw.get("placement")),
            "redacted_fields": identity_withheld,
            "calibration": _mapping(_mapping(state.get("evidence")).get("calibration")),
        },
        "round": {
            "available": bool(receipt),
            "schema_version": receipt.get("schema_version"),
            "advice": _mapping(receipt.get("advice")),
            "protection": _mapping(receipt.get("protection")),
            "adoption": _mapping(receipt.get("adoption")),
            "verification": _mapping(receipt.get("verification")),
            "round_axes": _mapping(receipt.get("round_axes")),
            "round_measurements": _mapping(receipt.get("round_measurements")),
            "evidence_identities": _mapping(receipt.get("evidence_identities")),
            "proposal_fingerprint": receipt.get("proposal_fingerprint"),
            "proposal_fingerprint_kind": receipt.get("proposal_fingerprint_kind"),
            "entry_graph_fingerprint": receipt.get("entry_graph_fingerprint"),
            "applied_graph_fingerprint": receipt.get("applied_graph_fingerprint"),
            **_absence(receipt_reason, bool(receipt), "round_receipt.json"),
        },
        "crossover_region": crossover_region,
        "incumbent": _incumbent_block(
            receipt,
            receipt_reason,
            applied_profile,
            applied_profile_reason,
            state,
            statefile_path,
        ),
        "flatness": _mapping(cloud.get("flatness")),
        "spec": spec,
        "curve": _mapping(cloud.get("curve")),
        "positions": positions,
        "lateral_poses": lateral_poses,
        "candidates": candidates,
        "entry_baseline": entry_baseline,
        "capture_snr": capture_snr,
        "honesty_mask": {
            "merged_excluded_bands_hz": cloud.get("merged_excluded_bands_hz"),
            "screen_excluded_bands_hz": cloud.get("screen_excluded_bands_hz"),
            "null_registry": _mapping(cloud.get("null_registry")),
            "null_registry_crossover_region": _mapping(
                cloud.get("null_registry_crossover_region")
            ),
            "carve_outs": cloud.get("carve_outs") or [],
            "geometry": _mapping(cloud.get("geometry")),
            "trusted_floor_hz": cloud.get("trusted_floor_hz"),
            "validity_floor_hz": cloud.get("validity_floor_hz"),
            "note": (
                "a bin the merged mask removed is not a bin a prescription may "
                "correct; the mask is the only structural protection against "
                "cutting an interference null"
            ),
        },
        "findings": findings,
        "verify": verify,
        "reflections": reflections,
        "accuracy_budget": _accuracy_budget_block(
            positions=positions,
            reflections=reflections,
            verify=verify,
            round_dir=round_dir,
            repeat_floor=repeat_floor,
            repeat_floor_reason=repeat_floor_reason,
        ),
        "structural_history": _structural_history_block(session_dir),
        "drivers": drivers,
        "operator_notes": operator_notes,
        "installation": installation_evidence(_mapping(draft_raw)),
        "feature_classification": classification,
        "harmonics": harmonics,
        "not_evaluated": _not_evaluated(
            receipt_reason=receipt_reason,
            cloud_reason=cloud_reason,
            state_reason=state_reason,
            applied_profile_reason=applied_profile_reason,
            classification_available=bool(classification.get("available")),
            drivers_available=bool(drivers.get("available")),
            lateral_poses_available=bool(lateral_poses.get("available")),
            candidates_available=bool(candidates.get("available")),
            capture_snr_reason=str(capture_snr.get("reason") or ""),
            cross_seat_sigma_reason=str(cross_seat_sigma.get("reason") or ""),
            harmonics_reason=str(harmonics.get("reason") or ""),
            gate_numbers_reason=_gate_numbers_reason(positions, verify),
            reflector_path_reason=str(reflections.get("reason") or ""),
            findings=findings,
            no_crossover=no_crossover,
        ),
        "contracts": contract_digests(prescription_contracts(programs=contract_programs(sources), **sources)),
    }
    packet["packet_fingerprint"] = _fingerprint(packet)
    return packet
