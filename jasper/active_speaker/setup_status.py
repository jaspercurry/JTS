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
import re
from pathlib import Path
from typing import Any, Mapping

from jasper.output_topology import OutputTopologyError, load_output_topology_strict

from .baseline_profile import (
    baseline_profile_state_path,
    build_baseline_profile_candidate,
    load_applied_baseline_profile_state,
)
from .capture_geometry import comparison_set_valid
from .crossover_preview import load_crossover_preview
from .crossover_contract import (
    automatic_candidate_readiness,
    crossover_snapshot_state,
    legacy_manual_preservation_state,
)
from .design_draft import load_design_draft
from .measurement import load_measurement_state

SETUP_STATUS_KIND = "jts_active_speaker_setup_status"

_CAMILLA_STATEFILE_ENV = "JASPER_CAMILLA_STATEFILE"
_DEFAULT_CAMILLA_STATEFILE = "/var/lib/camilladsp/outputd-statefile.yml"
_STAGED_CONFIG_BASENAMES = {
    "active_speaker_staged_startup.yml",
    "active_speaker_commissioning.yml",
}
_READINESS_DERIVATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    KeyError,
)
_CROSSOVER_SETUP_HREF = "/correction/crossover/"


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _active_group_count(topology: Any) -> int:
    return sum(
        1 for group in getattr(topology, "speaker_groups", ())
        if getattr(group, "mode", "") in {"active_2_way", "active_3_way"}
    )


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
) -> dict[str, Any]:
    """Room-correction prerequisite for an active Layer-A graph.

    Room correction operates on the Layer-A graph that is actually applied. Its
    prerequisite is therefore the immutable, topology-current applied snapshot
    — not whether that crossover was tuned manually or with the microphone.
    Mutable measurements remain quality evidence and observability only.
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
    else:
        reason = None
        detail = f"The applied {tuning_owner} crossover is ready for room correction."

    allowed = reason is None
    return {
        "required": True,
        "status": "ready" if allowed else "incomplete",
        "allowed": allowed,
        "reason": reason,
        "detail": detail,
        "setup_href": _CROSSOVER_SETUP_HREF,
        "applied_profile": {
            "available": isinstance(applied_profile, Mapping),
            "measured_level_match_applied": applied_measured,
            "tuning_owner": tuning_owner or None,
            "snapshot_valid": bool(applied_state["valid"]),
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

    FORWARD-WIRED(active-crossover): acoustic['snr']['worst_relevant']
    ['estimated_snr_db'] has no producer on main yet (lane B); when the
    producing lane lands, verify the real key path matches, drive one
    real-shape (non-fabricated) test through this site, then delete this
    marker.
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
    }


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
        # The "bundle_session_id" half of this check is forward-compatible
        # with SC-4's bundle writer (a later lane): it never sets that key
        # today, so only the comparison-set check is reachable yet.
        # FORWARD-WIRED(active-crossover): bundle_session_id has no producer
        # on main yet (lane D); when the producing lane lands, verify the
        # real key path matches, drive one real-shape (non-fabricated) test
        # through this site, then delete this marker.
        phase = "measuring"
    else:
        phase = "idle"

    session_id = measurements.get("bundle_session_id")
    session_id = str(session_id) if session_id else None

    active_comparison_set = measurements.get("active_comparison_set")
    session_fingerprint = (
        active_comparison_set.get("fingerprint")
        if isinstance(active_comparison_set, Mapping)
        else None
    )

    applied_profile_fingerprint = _mapping(
        (applied_profile or {}).get("source")
    ).get("fingerprint")

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
    """Best-effort active CamillaDSP config path from the outputd statefile."""

    statefile = Path(
        path or os.environ.get(_CAMILLA_STATEFILE_ENV) or _DEFAULT_CAMILLA_STATEFILE
    )
    try:
        text = statefile.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("'\"")


def read_active_speaker_setup_status(
    *,
    active_config_path: str | None = None,
    baseline_state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the authoritative active-speaker setup readiness snapshot.

    For a passive/ordinary speaker, active setup is not required and both
    ``volume_allowed`` and ``grouping_allowed`` are true. For an active speaker,
    the durable baseline profile must be applied and the active CamillaDSP config
    must not be one of the commissioning/staged safety graphs.

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
        # No topology/profile/measurements were readable at all -- commissioning
        # degrades to its own fail-soft idle default, then room_correction_allowed
        # is overwritten below to mirror this branch's own value (design doc
        # "Runtime surface": "room_correction_allowed mirrors the existing
        # acoustic_commissioning.allowed").
        unreadable_commissioning = commissioning_summary(
            None, profile=None, applied_profile=None, measurements=None,
        )
        unreadable_commissioning["room_correction_allowed"] = False
        return {
            "artifact_schema_version": 1,
            "kind": SETUP_STATUS_KIND,
            "active": True,
            "active_group_count": None,
            "status": "unknown",
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "required": True,
                "status": "unknown",
                "allowed": False,
                "reason": "output_topology_unreadable",
                "detail": "Read the output topology before room correction.",
                "setup_href": _CROSSOVER_SETUP_HREF,
            },
            "commissioning": unreadable_commissioning,
            "safety_muted": True,
            "reason": "output_topology_unreadable",
            "detail": "output topology cannot be read safely",
            "active_config_path": active_config_path or None,
            "baseline_profile": None,
            "protected_profile": None,
            "issues": issues,
        }

    active_group_count = _active_group_count(topology)
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
            "active_speaker_commissioning_config_loaded",
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
            "provisional": bool(profile.get("provisional")),
            "revalidation": dict(revalidation),
            "issues": profile_issues,
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
        protected_ready = bool(
            isinstance(protected_profile, Mapping)
            and protected_profile.get("status") == "applied"
            and protected_config_exists
            and protected_topology_current
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
            "topology_current": protected_topology_current,
            "provisional": bool(
                protected_profile.get("provisional")
                if isinstance(protected_profile, Mapping)
                else False
            ),
            "recomposition_snapshot_available": protected_snapshot is not None,
        }

        if not protected_ready and isinstance(protected_profile, Mapping):
            if not protected_config_exists:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_config_missing",
                    "applied active speaker baseline config file is missing",
                ))
            elif not protected_topology_current:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_topology_changed",
                    (
                        "saved output topology no longer matches the applied "
                        "active speaker baseline"
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
    if profile_summary is not None:
        profile_summary["automatic_candidate"] = automatic_candidate

    blocked = any(issue["severity"] == "blocker" for issue in issues)
    reason = issues[0]["code"] if issues else None
    detail = (
        issues[0]["message"]
        if issues
        else "active speaker baseline is applied and output control is ready"
    )
    acoustic_commissioning = _acoustic_commissioning_status(
        topology,
        setup_ready=not blocked,
        profile=profile,
        applied_profile=applied_profile,
        measurements=measurements,
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
        "reason": reason,
        "detail": detail,
        "active_config_path": config_path or None,
        "baseline_profile": profile_summary,
        "protected_profile": protected_profile_summary,
        "applied_crossover": applied_crossover,
        "manual_preservation": manual_preservation,
        "automatic_candidate": automatic_candidate,
        "issues": issues,
    }
