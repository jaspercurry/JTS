# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Product-level active-speaker setup readiness.

This is the single, household-facing contract for whether an active speaker is
ready for normal output controls and grouping. Lower-level modules still own
their detailed graph/proof work; this module composes their durable artifacts
into the answer that UI, control, and multiroom gates consume.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from jasper.camilla_config_contract import parse_camilla_devices_config
from jasper.fanin_coupling import RING_PCM_DEVICES, TRANSPORT_RING
from jasper.output_topology import OutputTopologyError, load_output_topology_strict

from ._common import (
    BASELINE_TOPOLOGY_CHANGED,
    ROOM_AUTHORITY_RECEIPT_ABSENT,
    ROOM_AUTHORITY_RECEIPT_MALFORMED,
    ROOM_AUTHORITY_RECEIPT_STALE,
    ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
)
from .baseline_profile import (
    active_layer_a_fingerprint,
    baseline_profile_state_path,
    build_baseline_profile_candidate,
    load_applied_baseline_profile_state,
    recompose_applied_baseline_yaml,
)
from .capture_geometry import comparison_set_valid
from .crossover_preview import load_crossover_preview
from .crossover_contract import (
    automatic_candidate_readiness,
    crossover_snapshot_state,
    legacy_manual_preservation_state,
)
from .design_draft import load_design_draft
from .environment import read_camilla_statefile_config_path
from .measurement import load_measurement_state
from .profile import ActiveSpeakerConfigError
from .runtime_contract import (
    CONTRACT_UNCONFIGURED,
    classify_output_contract,
    topology_allows_flat_dac_graph,
)

SETUP_STATUS_KIND = "jts_active_speaker_setup_status"
ROOM_ELIGIBILITY_SCHEMA_VERSION = 1
ROOM_AUTHORITY_PASSIVE_NOT_REQUIRED = "passive_not_required"
ROOM_AUTHORITY_MANUAL_APPLIED_PROFILE = "manual_applied_profile"
ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT = (
    "automatic_commissioning_receipt"
)

_RECEIPT_DETAIL_DEFAULT = (
    "Room correction is running on an automatic crossover that is not "
    "receipt-backed, so this result will not be banked as verified. Finish "
    "commissioning, or apply the current crossover as a manual profile."
)
_RECEIPT_DETAIL = {
    ROOM_AUTHORITY_RECEIPT_ABSENT: _RECEIPT_DETAIL_DEFAULT,
    ROOM_AUTHORITY_RECEIPT_STALE: (
        "The commissioning proof no longer describes this speaker, so room "
        "correction is running on the last-known-good crossover without "
        "banking a verified result. Re-mint it when convenient."
    ),
    ROOM_AUTHORITY_RECEIPT_MALFORMED: (
        "The commissioning proof on disk could not be read, so room "
        "correction is running without banking a verified result. Re-run "
        "commissioning to replace it."
    ),
    ROOM_AUTHORITY_RECEIPT_SUPERSEDED: (
        "This speaker's commissioning proof was minted before a JTS update "
        "that records more about how a proof was taken, so room correction is "
        "running without banking a verified result. Nothing is wrong with the "
        "speaker. Re-run commissioning when convenient."
    ),
}

_STAGED_CONFIG_BASENAMES = {
    "active_speaker_staged_startup.yml",
    "active_speaker_commissioning.yml",
}
IN_SEQUENCE_CAPTURE_ANCHOR_REASON = "active_speaker_commissioning_config_loaded"


def setup_blocked_only_by_in_sequence_anchor(
    status: Mapping[str, Any],
) -> bool:
    """Whether a blocked setup status is the capture sequence's own anchor.

    ``status`` is the composed crossover status payload
    (``correction_crossover_backend.status_payload()``), which carries both
    the ``setup`` artifact this module produces and the backend-composed
    ``capture_entry_pending`` flag.

    PR #1523 keeps the persisted CamillaDSP path anchored on the all-muted
    staged config *between* capture attempts within a single automatic
    measurement sequence (crash-safe posture — a crash/reboot mid-sequence
    comes back muted, never loud; production is restored exactly once at
    sequence end/idle-exit via the capture-entry stash).
    :func:`read_active_speaker_setup_status` sees that staged path and
    correctly reports ``blocked``/``active_speaker_commissioning_config_loaded``
    in isolation — but any readiness gate that requires exact ``"ready"``
    wedges the flow permanently mid-sequence (JTS3 punch #24: the envelope;
    run 10: the level-match and sweep endpoint gates blocked tweeter lock
    2/2 the same way). That state is "anchored mid-sequence by design", not
    "setup unproven".

    The capture-entry stash (``jasper.active_speaker.capture_entry_anchor``)
    is the discriminator rather than a heuristic: its lifecycle *is* the
    sequence boundary — written once when a sequence de-anchors production,
    cleared by every restore path (sequence end, idle-exit, the
    service-start claim). The service-start claim boundary runs that
    restore *before* any envelope or endpoint serves, so a stale stash can
    never make a post-crash, genuinely-unfinished setup read as ready.
    Every other blocked reason, and this reason without a pending stash,
    must gate exactly as a plain blocked status.
    """

    setup = status.get("setup")
    setup = setup if isinstance(setup, Mapping) else {}
    return bool(
        setup.get("status") == "blocked"
        and setup.get("reason") == IN_SEQUENCE_CAPTURE_ANCHOR_REASON
        and status.get("capture_entry_pending") is True
    )
# ``ActiveSpeakerConfigError`` is named even though it subclasses ``ValueError``
# and was therefore already caught. Since ``_applied_layer_a_binding`` began
# passing the COMPARED graph's playback device into the recomposer, that device
# crosses the emitter's own legality guard — a graph naming a forbidden lane
# (``jts_ring_playback``, reachable via a stale statefile or the flat-ring
# cutover class) makes the emitter refuse, and this surface is household-facing:
# an indeterminate input must return the ``unavailable`` snapshot, never a
# traceback. Naming the class keeps that guarantee legible at the one site where
# narrowing this tuple would silently reopen it.
# ``test_a_forbidden_playback_lane_is_unverifiable_never_a_traceback`` pins it.
_READINESS_DERIVATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    ActiveSpeakerConfigError,
    KeyError,
)
_CROSSOVER_SETUP_HREF = "/correction/crossover/"
_ROOMS_SETUP_HREF = "/rooms/"
_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _active_group_count(topology: Any) -> int:
    return sum(
        1 for group in getattr(topology, "speaker_groups", ())
        if getattr(group, "mode", "") in {"active_2_way", "active_3_way"}
    )


def _grouped_active_runtime() -> bool:
    """Fresh Active-owned scope fact for both bonded leaders and followers."""

    from jasper.multiroom.config import is_active_member, load_config

    return is_active_member(load_config())


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a read-only mapping view for optional artifact sections."""
    return value if isinstance(value, Mapping) else {}


def _usable_summed_acoustic(record: Any) -> bool:
    if not isinstance(record, Mapping) or record.get("validated") is not True:
        return False
    acoustic = record.get("acoustic")
    return (
        isinstance(acoustic, Mapping)
        and acoustic.get("verdict") == "blend_ok"
        and record.get("mic_clipping") is not True
        and acoustic.get("mic_clipping") is not True
    )


def _acoustic_commissioning_status(
    topology: Any,
    *,
    setup_ready: bool,
    profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
    measurements: Mapping[str, Any],
    layer_a_binding: Mapping[str, Any],
    receipt_authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Room-correction prerequisite for an active Layer-A graph.

    Room correction operates on the Layer-A graph that is actually applied. An
    immutable, topology-current manual snapshot is sufficient solo-runtime
    operator authority; grouped active remains explicitly unsupported until
    Active can bind its distributed Layer A. An automatic snapshot additionally
    needs Active's strict commissioning receipt. Mutable measurements remain
    quality evidence and observability only and cannot stand in for that receipt.
    """
    summary = _mapping(measurements.get("summary"))
    latest_summed = _mapping(summary.get("latest_summed_validations"))
    required_summed_count = _nonnegative_int(
        summary.get("required_summed_group_count")
    )
    usable_summed = {
        str(group_id): record
        for group_id, record in latest_summed.items()
        if _usable_summed_acoustic(record)
    }
    snapshot = _mapping(
        applied_profile.get("recomposition_snapshot")
        if isinstance(applied_profile, Mapping)
        else None
    )
    level_match = _mapping(snapshot.get("level_match"))
    current_level_match = _mapping(
        profile.get("level_match") if isinstance(profile, Mapping) else None
    )
    incomparable_groups = (
        current_level_match.get("incomparable_groups")
        if isinstance(current_level_match.get("incomparable_groups"), list)
        else []
    )
    current_groups_measured = _nonnegative_int(
        current_level_match.get("groups_measured")
    )
    required_active_groups = _active_group_count(topology)
    excitation_comparable = not incomparable_groups
    current_source = _mapping(
        profile.get("source") if isinstance(profile, Mapping) else None
    )
    applied_state = crossover_snapshot_state(
        applied_profile,
        expected_topology_id=getattr(topology, "topology_id", None),
        expected_topology_fingerprint=str(
            current_source.get("topology_fingerprint") or ""
        ) or None,
    )
    tuning_owner = str(applied_state.get("owner") or "")
    applied_measured = (
        applied_state["valid"]
        and level_match.get("applied") is True
        and _nonnegative_int(level_match.get("groups_measured"))
        >= required_active_groups
    )
    authority: str | None = None
    setup_href = _CROSSOVER_SETUP_HREF
    if not setup_ready:
        reason = "active_speaker_setup_not_ready"
        detail = "Apply the active speaker profile before starting room correction."
    elif not applied_state["valid"]:
        reason = str(applied_state["reason"])
        detail = (
            "Keep the current manual crossover or tune it automatically before "
            "room correction so its applied graph can be saved."
            if reason == "active_applied_profile_snapshot_missing"
            else str(applied_state["detail"])
        )
    elif layer_a_binding.get("status") == "distributed_active_unsupported":
        reason = "active_grouped_room_correction_not_supported"
        detail = (
            "Room correction for a grouped active speaker is not available "
            "yet. Turn grouping off to measure the solo active speaker."
        )
        setup_href = _ROOMS_SETUP_HREF
    elif layer_a_binding.get("matches") is not True:
        reason = (
            "active_applied_profile_graph_mismatch"
            if layer_a_binding.get("status") == "mismatch"
            else "active_applied_profile_graph_unverifiable"
        )
        # Cause-neutral on purpose: the loaded graph can also drift from the
        # applied profile because the EMITTER changed under it (a JTS upgrade
        # that spells a filter differently -- e.g. the PR-L2 shelf-Q fix),
        # not only because someone edited the crossover. Naming the crossover
        # as the thing that changed would send a household looking for an edit
        # nobody made. The remedy is the same either way: re-apply.
        detail = (
            "The sound pipeline loaded on this speaker does not match the "
            "applied manual profile. Apply that crossover again before Room "
            "correction."
            if reason == "active_applied_profile_graph_mismatch"
            else "JTS could not verify the loaded crossover against the applied "
            "profile. Apply the crossover again before Room correction."
        )
    elif tuning_owner == "manual":
        reason = None
        authority = ROOM_AUTHORITY_MANUAL_APPLIED_PROFILE
        detail = f"The applied {tuning_owner} crossover is ready for room correction."
    elif (
        tuning_owner == "automatic"
        and receipt_authority.get("allowed") is True
        and receipt_authority.get("authority") == "automatic_verified_receipt"
    ):
        reason = None
        authority = ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT
        detail = "The verified automatic crossover is ready for room correction."
    else:
        # An automatic applied snapshot remains playback authority, but it is
        # not the receipt-backed commissioning authority Room BANKS.  Do not
        # infer that receipt from mutable measurements or from the graph
        # having been applied successfully; the strict receipt integration is
        # a separate Active-owned authority chain.  Room still runs — this
        # names what may not be claimed, not what is refused (ruling S10).
        reason = (
            str(receipt_authority.get("reason") or ROOM_AUTHORITY_RECEIPT_ABSENT)
            if tuning_owner == "automatic"
            else ROOM_AUTHORITY_RECEIPT_ABSENT
        )
        detail = _RECEIPT_DETAIL.get(reason, _RECEIPT_DETAIL_DEFAULT)

    allowed = reason is None
    return {
        "decision_schema_version": ROOM_ELIGIBILITY_SCHEMA_VERSION,
        "authority": authority,
        # Opaque Active-owned identity for the exact loaded driver-domain
        # graph admitted by this decision. Room may compare this value at its
        # writer boundaries; it must not reconstruct Layer A itself.
        "layer_a_identity": (
            str(layer_a_binding.get("loaded_fingerprint"))
            if allowed and layer_a_binding.get("loaded_fingerprint")
            else None
        ),
        "required": True,
        "status": "ready" if allowed else "incomplete",
        "allowed": allowed,
        "reason": reason,
        "detail": detail,
        "setup_href": setup_href,
        "receipt_fingerprint": (
            receipt_authority.get("receipt_fingerprint")
            if authority == ROOM_AUTHORITY_AUTOMATIC_COMMISSIONING_RECEIPT
            else None
        ),
        "applied_profile": {
            "available": isinstance(applied_profile, Mapping),
            "measured_level_match_applied": applied_measured,
            "tuning_owner": tuning_owner or None,
            "snapshot_valid": bool(applied_state["valid"]),
            "graph_matches_loaded": layer_a_binding.get("matches") is True,
        },
        "drivers": {
            "required_groups": required_active_groups,
            "usable_groups": current_groups_measured,
            "excitation_comparable": excitation_comparable,
        },
        "summed": {
            "required": required_summed_count,
            "usable": len(usable_summed),
        },
    }


def _newest_commissioning_record(
    measurements: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The most recently created driver/summed record, across both maps.

    ``created_at`` is the zero-padded UTC ``_utc_now()`` timestamp everywhere
    it is written (measurement.py), so a plain string comparison sorts
    chronologically.
    """
    if not isinstance(measurements, Mapping):
        return None
    candidates: list[Mapping[str, Any]] = []
    for key in ("latest_by_target", "latest_summed_by_group"):
        bucket = measurements.get(key)
        if isinstance(bucket, Mapping):
            candidates.extend(
                record for record in bucket.values() if isinstance(record, Mapping)
            )
    if not candidates:
        return None
    return max(candidates, key=lambda record: str(record.get("created_at") or ""))


def _last_capture_summary(
    measurements: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The ``{snr_db, verdict, clipping, at}`` view of the newest capture.

    A fixed four-key shape (unlike the top-level commissioning fields, this
    inner block always carries all four keys, ``null`` where unknown) so a
    consumer never has to branch on which keys exist.

    Lane D's stored-ambient SNR producer writes the real
    ``acoustic.snr.worst_relevant.estimated_snr_db`` shape read here.
    """
    record = _newest_commissioning_record(measurements)
    if record is None:
        return None
    acoustic = _mapping(record.get("acoustic"))
    worst_relevant = _mapping(_mapping(acoustic.get("snr")).get("worst_relevant"))
    return {
        "snr_db": worst_relevant.get("estimated_snr_db"),
        "verdict": acoustic.get("verdict"),
        "clipping": record.get("mic_clipping"),
        "at": record.get("created_at"),
    }


def _idle_commissioning_summary() -> dict[str, Any]:
    return {
        "phase": "idle",
        "session_id": None,
        "session_fingerprint": None,
        "applied_profile_fingerprint": None,
        "last_capture": None,
        "last_failure_code": None,
        "room_correction_allowed": False,
        # No topology resolved, so no transport to name (#2412 Wave 4). `null`
        # rather than a guess: this is the fail-soft summary, and asserting a
        # transport for a box whose route could not be read is the half-fact
        # the key exists to remove.
        "transport": None,
    }


def _commissioning_transport(topology: Any) -> str | None:
    """Which transport this box's commissioning would emit on, or ``None``.

    ONE token with the two ``driver_commission_*`` journal lines
    (:data:`jasper.fanin_coupling.TRANSPORT_RING`), keyed on the same
    :data:`~jasper.fanin_coupling.RING_PCM_DEVICES` membership and reading the
    same chooser commissioning itself reads
    (``resolve_active_playback_device``), so the two surfaces cannot disagree
    about a device they both resolve the same way.
    Stated that way on purpose rather than as "they can never differ": the
    shared thing is the DERIVATION, not the input. ``prepare`` accepts a
    caller-supplied ``playback_device=`` override, so a caller that passed one
    could be described by a journal line this function would not reproduce. No
    production caller passes one (call-site audit, PR #2643) — the agreement is
    a convention this API does not enforce.

    ``None`` when the topology **cannot be read** or resolves to **no device**.
    Rolefulness is deliberately NOT the discriminator, and saying it was would
    mislead the reader this field exists to help: a PASSIVE box resolves the
    active outputd lane like any other, and reports ``ring`` exactly as a
    roleful one does.

    **SINGLE-TRANSPORT.** This field is ``ring`` or ``None``. #2285 P2 deleted
    ``resolve_output_layout`` case 2's marker read, so the ACTIVE-endpoint marker
    takes no part in this derivation at all: spied across both the roleful and
    the passive fixture, ``ring_active_endpoint_armed`` is consulted ZERO times
    and both polarities report ``ring``. A device string that is NOT a ring end —
    reachable only through an explicit lab/CI override
    (``resolve_output_layout``'s case 1: the ``playback_device`` argument or
    ``JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE``) — reports ``None``, because
    ADR-0100 left no second transport for it to be labelled as.

    **The gate is the DAC PROFILE, measured rather than assumed.**
    ``resolve_output_layout`` names the ring when the profile declares an active
    outputd lane (``supports_active_outputd_lane`` and
    ``active_outputd_lane_channels``); a box failing that condition falls through
    to ``playback_device=None``, which is the ``no device`` half above. All five
    registered ``DacProfile``s declare such a lane, so that fall-through is not
    reachable from any shipped profile today — the two tests covering it stub the
    collaborator rather than build a lane-less topology. (Two earlier versions of
    this paragraph were false in turn: the first named rolefulness as the consult
    gate, the second kept saying the marker was consulted at all. Each was
    replaced only after measurement — which is why this one states a spy count
    rather than a mechanism.)

    Fail-soft like every other field here: the derivation reaches the topology
    layer, and a box whose route cannot be read reports no transport rather than
    failing ``/state``.

    ``AttributeError`` joins ``_READINESS_DERIVATION_ERRORS`` for this call only.
    The shared tuple is the set the OTHER derivations here can raise; this one
    reaches ``resolve_output_layout``, which walks ``topology.hardware``
    unguarded, so a ``None`` or duck-typed topology — the shape the two
    early-return call sites and every standalone caller pass — raises a class no
    sibling does. Widening the shared tuple would loosen five other derivations
    to buy one; catching it here does not. An observability field must never be
    the reason ``/state`` stops answering.
    """
    from .playback_route import resolve_active_playback_device

    try:
        device, _source = resolve_active_playback_device(topology)
    except (*_READINESS_DERIVATION_ERRORS, AttributeError):
        return None
    return TRANSPORT_RING if device in RING_PCM_DEVICES else None


def _derive_commissioning_summary(
    topology: Any,
    *,
    profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
    measurements: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = profile if isinstance(profile, Mapping) else None
    applied_profile = applied_profile if isinstance(applied_profile, Mapping) else None
    measurements = measurements if isinstance(measurements, Mapping) else {}

    # Phase derivation is pinned in priority order (design doc "Runtime
    # surface" / "Structured events"): failed, then proposal_ready, then
    # measuring, else idle. Each branch is mutually exclusive by construction
    # (elif), so "measuring" is only reached when neither of the first two
    # holds, matching "neither failed nor proposal_ready holds".
    last_failure_code: str | None = None
    if profile is not None and profile.get("status") == "apply_failed":
        phase = "failed"
        for issue_entry in profile.get("issues") or []:
            if (
                isinstance(issue_entry, Mapping)
                and issue_entry.get("severity") == "blocker"
            ):
                code = issue_entry.get("code")
                last_failure_code = str(code) if code else None
                break
    elif profile is not None and bool(
        _mapping(profile.get("permissions")).get("may_apply")
    ):
        phase = "proposal_ready"
    elif comparison_set_valid(measurements.get("active_comparison_set")) or bool(
        measurements.get("bundle_session_id")
    ):
        phase = "measuring"
    else:
        phase = "idle"

    active_comparison_set = measurements.get("active_comparison_set")
    session_id = (
        active_comparison_set.get("bundle_session_id")
        if isinstance(active_comparison_set, Mapping)
        else None
    ) or measurements.get("bundle_session_id")
    session_id = str(session_id) if session_id else None

    session_fingerprint = (
        active_comparison_set.get("fingerprint")
        if isinstance(active_comparison_set, Mapping)
        else None
    )

    applied_profile_fingerprint = (applied_profile or {}).get(
        "candidate_fingerprint"
    )

    # Standalone approximation of "is there a valid applied Layer-A graph the
    # room can correct against" -- read_active_speaker_setup_status overwrites
    # this with the exact acoustic_commissioning.allowed value it already
    # computes from these same inputs plus config-path/topology gating this
    # function does not see, so the wired /state payload always mirrors it
    # exactly; this value is what a caller gets from commissioning_summary
    # standalone (e.g. in a unit test).
    current_source = _mapping(profile.get("source")) if profile is not None else {}
    applied_state = crossover_snapshot_state(
        applied_profile,
        expected_topology_id=getattr(topology, "topology_id", None),
        expected_topology_fingerprint=(
            str(current_source.get("topology_fingerprint") or "") or None
        ),
    )

    return {
        "phase": phase,
        "session_id": session_id,
        "session_fingerprint": session_fingerprint,
        "applied_profile_fingerprint": applied_profile_fingerprint,
        "last_capture": _last_capture_summary(measurements),
        "last_failure_code": last_failure_code,
        "room_correction_allowed": bool(applied_state.get("valid")),
        # `curl /state | jq .` is this campaign's standing probe, and a device
        # name without its transport is the exact half-fact that produced #2412.
        "transport": _commissioning_transport(topology),
    }


def commissioning_summary(
    topology: Any,
    *,
    profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
    measurements: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Small household/operator commissioning summary for ``/state``.

    Pure over ``profile`` / ``applied_profile`` / ``measurements`` -- the same
    objects :func:`read_active_speaker_setup_status` already loads -- and
    fail-soft: any unreadable/malformed input degrades to the safest phase
    (``"idle"``) instead of raising, mirroring
    ``_READINESS_DERIVATION_ERRORS`` above. Detailed curves and bundle paths
    stay out of this block by design; they belong to the session report, not
    ``/state`` (design doc "Runtime surface").
    """
    try:
        return _derive_commissioning_summary(
            topology,
            profile=profile,
            applied_profile=applied_profile,
            measurements=measurements,
        )
    except _READINESS_DERIVATION_ERRORS:
        return _idle_commissioning_summary()


def active_config_path_from_statefile(
    path: str | Path | None = None,
) -> str:
    """Best-effort active CamillaDSP config path from the outputd statefile.

    Delegates to :func:`jasper.active_speaker.environment.read_camilla_statefile_config_path`
    — the canonical ``JASPER_CAMILLA_STATEFILE`` reader — rather than keeping a
    second copy of its env-var name, default path, and ``config_path:`` regex.
    ``""`` (not ``None``) on an unreadable/empty statefile, preserving this
    wrapper's original contract for its caller below.
    """

    return read_camilla_statefile_config_path(path) or ""


def _applied_layer_a_binding(
    topology: Any,
    *,
    applied_profile: Mapping[str, Any] | None,
    active_config_path: str | None,
    active_config_text: str | None,
) -> dict[str, Any]:
    """Bind Active's immutable applied snapshot to the loaded Layer-A graph."""

    unavailable = {
        "status": "unverifiable",
        "matches": False,
        "expected_fingerprint": None,
        "loaded_fingerprint": None,
    }
    if not isinstance(applied_profile, Mapping) or (
        active_config_text is None and not active_config_path
    ):
        return unavailable
    try:
        loaded_yaml = (
            active_config_text
            if active_config_text is not None
            else Path(str(active_config_path)).read_text(encoding="utf-8")
        )
        # A bonded active leader's primary Camilla instance carries only the
        # program-domain File→Snapcast bake; its driver-domain Layer A lives on
        # the crossover instance. The solo v1 fingerprint cannot honestly bind
        # that distributed graph, so Active emits one explicit unsupported
        # decision instead of a misleading crossover-reapply mismatch. A later
        # distributed authority must remain Active-owned and bind both daemons.
        if (
            _grouped_active_runtime()
            or f"Source: {_PROGRAM_BAKE_SOURCE}" in loaded_yaml
        ):
            return {
                "status": "distributed_active_unsupported",
                "matches": False,
                "expected_fingerprint": None,
                "loaded_fingerprint": None,
            }
        # THE TRANSPORT AXIS IS NEUTRALIZED, not compared. This projection binds
        # ``output_devices``, which carries the playback device and the sink's
        # whole CamillaDSP geometry — so an ACTIVE-ring-armed box could never
        # match an expectation built against the device its immutable snapshot
        # recorded, and this check reported ``mismatch``, blocking room
        # correction with "Apply that crossover again" for a transport move
        # nobody asked about (#2339/#2337 family). Layer A is crossover and
        # protection evidence; whether the graph names the RIGHT transport is
        # judged by ``check_fanin_coupling`` and ``ring_edge_width_ready``
        # against the marker and the ring's declaring ends.
        #
        # The endpoint is taken from the graph being COMPARED rather than from
        # the box (the re-emit seams' ``resolve_live_active_endpoint``): this is
        # a two-way comparison of snapshot evidence against a caller-supplied
        # readback, and a third opinion — the statefile — would make a box whose
        # device resolution merely drifted from its snapshot report crossover
        # drift. ``None`` (an unparseable devices block) falls through to the
        # snapshot default, exactly as before.
        loaded_playback = parse_camilla_devices_config(loaded_yaml).get(
            "playback_device"
        )
        expected_yaml, expected_issues = recompose_applied_baseline_yaml(
            topology,
            applied_profile=applied_profile,
            playback_device=loaded_playback or None,
        )
        if expected_yaml is None or expected_issues:
            return unavailable
        expected = active_layer_a_fingerprint(expected_yaml)
        loaded = active_layer_a_fingerprint(loaded_yaml)
    except _READINESS_DERIVATION_ERRORS:
        return unavailable
    matches = expected == loaded
    return {
        "status": "current" if matches else "mismatch",
        "matches": matches,
        "expected_fingerprint": expected,
        "loaded_fingerprint": loaded,
    }


def _blocked_setup_status(
    topology: Any,
    *,
    active_group_count: int | None,
    status: str,
    acoustic_status: str,
    reason: str,
    detail: str,
    room_detail: str,
    setup_href: str,
    active_config_path: str | None,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the one fail-closed setup snapshot shared by blocked inputs."""

    commissioning = commissioning_summary(
        topology, profile=None, applied_profile=None, measurements=None,
    )
    commissioning["room_correction_allowed"] = False
    return {
        "artifact_schema_version": 1,
        "kind": SETUP_STATUS_KIND,
        "active": (
            active_group_count > 0 if active_group_count is not None else None
        ),
        "active_group_count": active_group_count,
        "status": status,
        "configured": False,
        "volume_allowed": False,
        "grouping_allowed": False,
        "room_correction_allowed": False,
        "acoustic_commissioning": {
            "decision_schema_version": ROOM_ELIGIBILITY_SCHEMA_VERSION,
            "authority": None,
            "required": True,
            "status": acoustic_status,
            "allowed": False,
            "reason": reason,
            "detail": room_detail,
            "setup_href": setup_href,
        },
        "commissioning": commissioning,
        "safety_muted": True,
        "reason": reason,
        "detail": detail,
        "active_config_path": active_config_path or None,
        "baseline_profile": None,
        "protected_profile": None,
        "issues": issues,
    }


def read_active_speaker_setup_status(
    *,
    active_config_path: str | None = None,
    active_config_text: str | None = None,
    baseline_state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the authoritative active-speaker setup readiness snapshot.

    For a passive/ordinary speaker, active setup is not required and both
    ``volume_allowed`` and ``grouping_allowed`` are true. For an active speaker,
    the durable baseline profile must be applied and the active CamillaDSP config
    must not be one of the commissioning/staged safety graphs. Room supplies a
    fresh CamillaDSP ``active_raw`` readback as ``active_config_text``; other
    callers retain the durable statefile-path fallback.

    Total + fail-closed for active-output safety: an unreadable topology or
    unreadable baseline profile returns a blocked snapshot instead of silently
    treating the speaker as ready.
    """

    issues: list[dict[str, str]] = []
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        issues.append(_issue(
            "blocker",
            "output_topology_unreadable",
            f"output topology cannot be read safely: {exc}",
        ))
        return _blocked_setup_status(
            None,
            active_group_count=None,
            status="unknown",
            acoustic_status="unknown",
            reason="output_topology_unreadable",
            detail="output topology cannot be read safely",
            room_detail="Read the output topology before room correction.",
            setup_href=_CROSSOVER_SETUP_HREF,
            active_config_path=active_config_path,
            issues=issues,
        )

    output_contract = classify_output_contract(topology)
    active_group_count = _active_group_count(topology)
    if (
        active_group_count == 0
        and not topology_allows_flat_dac_graph(output_contract)
    ):
        unconfigured = output_contract.classification == CONTRACT_UNCONFIGURED
        reason = (
            "output_topology_unconfigured"
            if unconfigured
            else "output_topology_not_ready"
        )
        detail = (
            "choose and save a speaker layout before using audio"
            if unconfigured
            else "choose and save a complete passive mono or stereo layout before using audio"
        )
        room_detail = (
            "Choose and save a speaker layout before room correction."
            if unconfigured
            else "Choose and save a complete passive mono or stereo layout before room correction."
        )
        issue = _issue(
            "blocker",
            reason,
            detail,
        )
        return _blocked_setup_status(
            topology,
            active_group_count=0,
            status="blocked",
            acoustic_status="incomplete",
            reason=reason,
            detail=detail,
            room_detail=room_detail,
            setup_href="/sound/setup/",
            active_config_path=active_config_path,
            issues=[issue, *(dict(item) for item in output_contract.issues)],
        )
    if active_group_count == 0:
        passive_commissioning = commissioning_summary(
            topology, profile=None, applied_profile=None, measurements=None,
        )
        passive_commissioning["room_correction_allowed"] = True
        return {
            "artifact_schema_version": 1,
            "kind": SETUP_STATUS_KIND,
            "active": False,
            "active_group_count": 0,
            "status": "not_active",
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": ROOM_ELIGIBILITY_SCHEMA_VERSION,
                "authority": ROOM_AUTHORITY_PASSIVE_NOT_REQUIRED,
                "required": False,
                "status": "not_required",
                "allowed": True,
                "reason": None,
                "detail": "Passive speakers do not need active-crossover commissioning.",
                "setup_href": None,
            },
            "commissioning": passive_commissioning,
            "safety_muted": False,
            "reason": None,
            "detail": "speaker does not use an active crossover",
            "active_config_path": active_config_path or None,
            "baseline_profile": None,
            "protected_profile": None,
            "issues": [],
        }

    config_path = active_config_path
    if config_path is None:
        config_path = active_config_path_from_statefile()
    config_basename = os.path.basename(config_path or "")
    if not config_path:
        issues.append(_issue(
            "blocker",
            "active_config_path_unknown",
            "current CamillaDSP config path is unavailable",
        ))
    elif config_basename in _STAGED_CONFIG_BASENAMES:
        issues.append(_issue(
            "blocker",
            IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
            "active speaker setup/commissioning graph is loaded",
        ))

    profile_summary: dict[str, Any] | None = None
    protected_profile_summary: dict[str, Any] | None = None
    measurements: Mapping[str, Any] = {}
    applied_profile: Mapping[str, Any] | None = None
    profile: Mapping[str, Any] | None = None
    automatic_profile: Mapping[str, Any] | None = None
    try:
        design_draft = load_design_draft()
        crossover_preview = load_crossover_preview(
            current_design_draft=design_draft,
        )
        measurements = load_measurement_state(topology)
        profile = build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=False,
            state_path=baseline_state_path,
        )
        automatic_profile = build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=False,
            state_path=baseline_state_path,
            tuning_owner="automatic",
        )
        applied_profile = load_applied_baseline_profile_state(baseline_state_path)
    except _READINESS_DERIVATION_ERRORS as exc:
        profile = None
        issues.append(_issue(
            "blocker",
            "active_baseline_profile_unreadable",
            f"active speaker baseline readiness could not be derived: {type(exc).__name__}",
        ))

    if profile is not None:
        raw_config = profile.get("config")
        config: Mapping[str, Any] = (
            raw_config
            if isinstance(raw_config, Mapping)
            else {}
        )
        raw_source = profile.get("source")
        source: Mapping[str, Any] = (
            raw_source
            if isinstance(raw_source, Mapping)
            else {}
        )
        raw_revalidation = profile.get("revalidation")
        revalidation: Mapping[str, Any] = (
            raw_revalidation
            if isinstance(raw_revalidation, Mapping)
            else {"required": False, "status": "not_required"}
        )
        profile_issues = [
            {
                "severity": str(issue.get("severity") or "blocker"),
                "code": str(issue.get("code") or "baseline_profile_issue"),
                "message": str(issue.get("message") or "active speaker baseline issue"),
            }
            for issue in profile.get("issues", [])
            if isinstance(issue, Mapping)
        ]
        profile_summary = {
            "status": profile.get("status"),
            "path": str(baseline_profile_state_path(baseline_state_path)),
            "config_path": config.get("path"),
            "source_fingerprint": source.get("fingerprint"),
            "candidate_fingerprint": profile.get("candidate_fingerprint"),
            "provisional": bool(profile.get("provisional")),
            "revalidation": dict(revalidation),
            "issues": profile_issues,
            # PR-L4 item 8: WHICH question this block answers.
            #
            # `/state.active_speaker_setup` carries two baseline answers, and
            # nothing said they were answering different questions. This one is
            # a freshly RE-DERIVED /sound-page staging candidate — what the
            # household could compile next — and it routinely reads
            # `status: "blocked"` with a different `candidate_fingerprint`
            # while a perfectly good profile is applied and audible. During the
            # 2026-07-27 forensics that pair cost real time: an operator
            # reading `/state` met "blocked" and a fingerprint that matched
            # nothing, sitting beside `protected_profile`'s "ready" and the
            # fingerprint actually running.
            #
            # Both blocks stay — they have different, legitimate readers — but
            # neither may present itself as THE baseline answer any more. The
            # discriminator names the role, points at the block that reports
            # what is audible, and says outright whether the two agree. The
            # live answer is `protected_profile`; this is a proposal.
            "role": "staging_candidate",
            "live_answer_key": "protected_profile",
        }

        # The mutable candidate and the graph that currently protects playback
        # are intentionally different owners. A fresh microphone capture
        # invalidates the candidate fingerprint, but it does not rewrite or
        # weaken the explicitly applied Layer-A graph. Keep normal output and
        # the rest of the crossover journey on that applied anchor while the
        # new candidate advances through driver -> summed -> apply.
        protected_profile = (
            applied_profile
            if isinstance(applied_profile, Mapping)
            else (profile if profile.get("status") == "applied" else None)
        )
        protected_source = _mapping(
            protected_profile.get("source")
            if isinstance(protected_profile, Mapping)
            else None
        )
        protected_config = _mapping(
            protected_profile.get("config")
            if isinstance(protected_profile, Mapping)
            else None
        )
        protected_config_path = str(protected_config.get("path") or "")
        protected_config_exists = bool(
            protected_config_path and Path(protected_config_path).exists()
        )
        protected_topology_fingerprint = str(
            protected_source.get("topology_fingerprint") or ""
        )
        current_topology_fingerprint = str(
            source.get("topology_fingerprint") or ""
        )
        protected_topology_current = not (
            protected_topology_fingerprint
            and current_topology_fingerprint
            and protected_topology_fingerprint != current_topology_fingerprint
        )
        # Topology staleness is deliberately NOT a readiness input (ruling
        # S10, ADR-0019). `topology_config_fingerprint` hashes the whole
        # topology dict bar `pairing_intent`, so display-only strings that
        # reach no clamp and no emitted filter — `SpeakerChannel`'s
        # `human_output_label`, a `SpeakerGroup`'s `label` — rotate it and
        # used to take the box to `blocked`.
        #
        # This comparison is not where a cap is enforced, because it cannot
        # see one: it hashes a dict and reports inequality. The declared facts
        # that DO gate keep their own gates, one step downstream and each
        # reading the field rather than the hash —
        # `evaluate_driver_safety_profile` (`correction_crossover_v2.py`'s
        # session-open gate) and `resolve_driver_excitation_ceilings`, plus
        # the driver-protection clamps. `driver_style` is the worked example
        # of a field that only LOOKS like metadata: it selects the tweeter's
        # `min_highpass_hz` (`driver_protection.py`) and sits in the
        # driver-safety target match (`driver_safety.py`), so editing it still
        # refuses a session — correctly, at the safety gate, with a code.
        protected_ready = bool(
            isinstance(protected_profile, Mapping)
            and protected_profile.get("status") == "applied"
            and protected_config_exists
        )
        protected_snapshot = (
            protected_profile.get("recomposition_snapshot")
            if isinstance(protected_profile, Mapping)
            and isinstance(protected_profile.get("recomposition_snapshot"), Mapping)
            else None
        )
        protected_profile_summary = {
            "available": isinstance(protected_profile, Mapping),
            "status": "ready" if protected_ready else "unavailable",
            "config_path": protected_config_path or None,
            "source_fingerprint": protected_source.get("fingerprint"),
            "candidate_fingerprint": (
                protected_profile.get("candidate_fingerprint")
                if isinstance(protected_profile, Mapping)
                else None
            ),
            "topology_current": protected_topology_current,
            "provisional": bool(
                protected_profile.get("provisional")
                if isinstance(protected_profile, Mapping)
                else False
            ),
            "recomposition_snapshot_available": protected_snapshot is not None,
            # Gauge fix (2026-07-24): WHY Layer-1a driver linearization did
            # or didn't run for the CURRENTLY APPLIED candidate — "" when
            # absent/never evaluated (a pre-gauge-fix profile, or a plain
            # trims-only apply). Read straight off the applied artifact
            # (protected_profile, loaded fresh by
            # load_applied_baseline_profile_state above) rather than the
            # freshly-recomputed `profile` above: `profile` is built with no
            # `measured_candidate` in this function, so it can never carry an
            # honest outcome — only the durable applied artifact can.
            "linearization_outcome": (
                str(protected_profile.get("linearization_outcome") or "")
                if isinstance(protected_profile, Mapping)
                else ""
            ),
            # PR-L4 item 8, the other half of the discriminator: this block is
            # the one that reports what the speaker is ACTUALLY running.
            "role": "applied_profile",
        }
        # ...and whether the two agree, computed once here rather than left for
        # every reader to work out by comparing fingerprints across two blocks
        # with different shapes. `None` = no applied profile to compare against,
        # which is not a disagreement.
        profile_summary["matches_applied"] = (
            None
            if not isinstance(protected_profile, Mapping)
            else bool(
                profile.get("candidate_fingerprint")
                and profile.get("candidate_fingerprint")
                == protected_profile.get("candidate_fingerprint")
            )
        )

        if isinstance(protected_profile, Mapping):
            if not protected_config_exists:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_config_missing",
                    "applied active speaker baseline config file is missing",
                ))
            elif not protected_topology_current:
                issues.append(_issue(
                    "warning",
                    BASELINE_TOPOLOGY_CHANGED,
                    (
                        "topology changed since the applied baseline; re-mint "
                        "when convenient"
                    ),
                ))

        if profile.get("status") != "applied" and not protected_ready:
            profile_blockers = [
                issue for issue in profile_issues
                if issue["severity"] == "blocker"
            ]
            if profile_blockers:
                issues.extend(profile_blockers)
            else:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_profile_not_applied",
                    (
                        "apply the active speaker baseline before normal output "
                        "control or grouping"
                    ),
                ))
        # `config` is the CANDIDATE's, not the applied profile's, and a
        # topology change no longer clears `protected_ready` — so a stale
        # topology now suppresses this arm too. That is the intended reading
        # of last-known-good: what is playing is the applied graph, whose own
        # config file is checked above, and a candidate pointing at a file
        # that was never written is a pending edit rather than a reason to
        # mute the speaker.
        if (
            not protected_ready
            and config.get("path")
            and not Path(str(config.get("path"))).exists()
        ):
            issues.append(_issue(
                "blocker",
                "active_baseline_config_missing",
                "active speaker baseline config file is missing",
            ))

    current_source = _mapping(
        profile.get("source") if isinstance(profile, Mapping) else None
    )
    applied_crossover = crossover_snapshot_state(
        applied_profile,
        expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=str(
            current_source.get("topology_fingerprint") or ""
        ) or None,
    )
    layer_a_binding = _applied_layer_a_binding(
        topology,
        applied_profile=applied_profile,
        active_config_path=config_path,
        active_config_text=active_config_text,
    )
    if protected_profile_summary is not None:
        protected_profile_summary["layer_a_binding"] = layer_a_binding
    manual_preservation = legacy_manual_preservation_state(
        applied_profile,
        current_source_fingerprint=str(current_source.get("fingerprint") or "") or None,
    )
    summary = _mapping(measurements.get("summary"))
    candidate_level_match = _mapping(
        automatic_profile.get("level_match")
        if isinstance(automatic_profile, Mapping)
        else None
    )
    automatic_candidate = (
        dict(automatic_profile["automatic_candidate"])
        if isinstance(automatic_profile, Mapping)
        and isinstance(automatic_profile.get("automatic_candidate"), Mapping)
        else automatic_candidate_readiness(
            required_group_ids=(
                group.id
                for group in topology.speaker_groups
                if group.mode in {"active_2_way", "active_3_way"}
            ),
            level_match=candidate_level_match,
            measurement_summary=summary,
            active_comparison_set=measurements.get("active_comparison_set"),
        )
    )
    automatic_candidate["candidate_fingerprint"] = (
        automatic_profile.get("candidate_fingerprint")
        if isinstance(automatic_profile, Mapping)
        else None
    )
    if profile_summary is not None:
        profile_summary["automatic_candidate"] = automatic_candidate

    # A blocker outranks a notice for the headline no matter which was
    # appended first: the list carries both severities now, and a household
    # told "re-mint when convenient" while the box is actually blocked is
    # being told the wrong thing to do.
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    blocked = bool(blockers)
    headline = blockers or issues
    setup_reason = headline[0]["code"] if headline else None
    detail = (
        headline[0]["message"]
        if headline
        else "active speaker baseline is applied and output control is ready"
    )
    receipt_authority = {
        "allowed": False,
        "authority": "automatic_verified_receipt",
        "reason": ROOM_AUTHORITY_RECEIPT_ABSENT,
        "receipt_fingerprint": None,
    }
    if applied_crossover.get("owner") == "automatic":
        # Manual/passive status stays free of the recorder/analyzer stack.
        from .commissioning_verification import read_commissioning_room_authority

        receipt_authority = read_commissioning_room_authority(topology)
    acoustic_commissioning = _acoustic_commissioning_status(
        topology,
        setup_ready=not blocked,
        profile=profile,
        applied_profile=applied_profile,
        measurements=measurements,
        layer_a_binding=layer_a_binding,
        receipt_authority=receipt_authority,
    )
    commissioning = commissioning_summary(
        topology,
        profile=profile,
        applied_profile=applied_profile,
        measurements=measurements,
    )
    # Mirror the canonical gate exactly rather than trusting
    # commissioning_summary's own standalone approximation (design doc
    # "Runtime surface": "room_correction_allowed mirrors the existing
    # acoustic_commissioning.allowed").
    commissioning["room_correction_allowed"] = acoustic_commissioning["allowed"]
    return {
        "artifact_schema_version": 1,
        "kind": SETUP_STATUS_KIND,
        "active": True,
        "active_group_count": active_group_count,
        "status": "blocked" if blocked else "ready",
        "configured": not blocked,
        "volume_allowed": not blocked,
        "grouping_allowed": not blocked,
        "room_correction_allowed": acoustic_commissioning["allowed"],
        "acoustic_commissioning": acoustic_commissioning,
        "commissioning": commissioning,
        "safety_muted": blocked,
        "reason": setup_reason,
        "detail": detail,
        "active_config_path": config_path or None,
        "baseline_profile": profile_summary,
        "protected_profile": protected_profile_summary,
        "applied_crossover": applied_crossover,
        "manual_preservation": manual_preservation,
        "automatic_candidate": automatic_candidate,
        "issues": issues,
    }
