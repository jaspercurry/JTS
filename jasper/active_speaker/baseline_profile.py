# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read the applied active-speaker baseline profile and its candidate identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.dsp_apply import same_config_file
from jasper.output_topology import canonical_fingerprint as _fingerprint

from .camilla_yaml import _branch_context, linearization_headroom_db
from .measurement_programs import PROGRAM_DOCUMENT_ORDER, PURPOSE_SPEAKER
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from .state_paths import baseline_profile_state_path

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"

# Canonical per-parameter provenance vocabulary (SC-3).
PROVENANCE_MANUAL = "manual"
PROVENANCE_MEASURED = "measured"
PROVENANCE_AUTHORED_BY_MODEL = "authored_by_model"
PROVENANCE_SET_BY_USER = "set_by_user"


def applied_bass_extension(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = load_applied_baseline_profile_state() if profile is None else profile
    snapshot = (source or {}).get("recomposition_snapshot") or {}
    raw = snapshot.get("bass_extension") or {}
    return validate_dynamic_bass_descriptor(raw) if raw else {}


def baseline_candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Identify the exact immutable Layer-A candidate, not its cache source."""

    source = candidate.get("source")
    snapshot = candidate.get("recomposition_snapshot")
    hashed_snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else None
    if hashed_snapshot is not None and isinstance(
        hashed_snapshot.get("level_match"), Mapping
    ):
        # Capture recency changes on recompose; graph identity must not.
        # Candidates without this field must keep their stored fingerprint.
        hashed_snapshot["level_match"] = {
            key: value
            for key, value in hashed_snapshot["level_match"].items()
            if key != "newest_capture_at"
        }
    return _fingerprint({
        "artifact_schema_version": candidate.get("artifact_schema_version"),
        "kind": candidate.get("kind"),
        "source_fingerprint": (
            source.get("fingerprint") if isinstance(source, Mapping) else None
        ),
        "recomposition_snapshot": hashed_snapshot,
    })


def _load_saved_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("artifact_schema_version") != SCHEMA_VERSION
        or raw.get("kind") != BASELINE_PROFILE_KIND
    ):
        return None
    return raw


def applied_profile_anchor(
    saved: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Current or retained applied profile behind a mutable candidate state."""
    if not isinstance(saved, Mapping):
        return None
    if saved.get("status") == "applied":
        return saved
    prior = saved.get("applied_recomposition_profile")
    if isinstance(prior, Mapping) and prior.get("status") == "applied":
        return prior
    return None


def _frozen_applied_profile(
    saved: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the applied record fields consumed by runtime readers."""
    applied = applied_profile_anchor(saved)
    if applied is None:
        return None
    snapshot = applied.get("recomposition_snapshot")
    # candidate_fingerprint is derived data, not an authority. Older saved
    # profiles may omit it and a partially written/corrupt profile may carry a
    # value that no longer identifies its immutable snapshot. Always migrate
    # or repair it from the exact source + snapshot content consumers trust.
    candidate_fingerprint = (
        baseline_candidate_fingerprint(applied)
        if isinstance(snapshot, Mapping)
        else None
    )
    return {
        "artifact_schema_version": applied.get("artifact_schema_version"),
        "kind": applied.get("kind"),
        "status": "applied",
        "applied_at": applied.get("applied_at"),
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_artifact_path": applied.get("candidate_artifact_path"),
        "source": dict(applied.get("source") or {}),
        "config": dict(applied.get("config") or {}),
        "corrections": dict(applied.get("corrections") or {}),
        "corrections_source": dict(applied.get("corrections_source") or {}),
        **({"timing": applied["timing"]} if "timing" in applied else {}),
        "gain_provenance": dict(applied.get("gain_provenance") or {}),
        "corrections_provenance": dict(applied.get("corrections_provenance") or {}),
        "level_match": dict(applied.get("level_match") or {}),
        "automatic_candidate": dict(applied.get("automatic_candidate") or {}),
        "linearization": dict(applied.get("linearization") or {}),
        "linearization_outcome": str(applied.get("linearization_outcome") or ""),
        "trim_decision": dict(applied.get("trim_decision") or {}),
        "blend_correction": list(applied.get("blend_correction") or []),
        "room_correction": dict(applied.get("room_correction") or {}),
        "tuning_owner": str(applied.get("tuning_owner") or ""),
        # Quality state belongs to the immutable applied anchor too.  Dropping
        # it here lets an older sensitivity-only profile masquerade as a
        # measured profile on every consumer of the frozen view.
        "provisional": bool(applied.get("provisional")),
        "recomposition_snapshot": dict(snapshot) if isinstance(snapshot, Mapping) else None,
    }


def _profile_branch_context(
    profile: Mapping[str, Any],
) -> dict[str, tuple[Any, float]]:
    """The ``(crossover sections, trim_db)`` context for one profile's charge.

    Rebuilt from the SAME two snapshot fields the graph was emitted from — the
    preset's crossover regions and the per-driver ``corrections`` gains — via
    the emitter's own :func:`~jasper.active_speaker.camilla_yaml._branch_context`,
    so a headroom read back off a profile is evaluated over the chain that
    profile actually carries.

    ``{}`` when either field is missing or unparseable. That is the same
    over-estimating fallback ``linearization_headroom_db`` applies to a role
    absent from a supplied context ("no crossover, no trim"), and it is the
    right direction here: an over-estimated headroom read makes the DECLARED
    apply-boundary offset larger than the graph's, which under-corrects the
    delta probe and leaves the difference visible as ``residual_offset_db``
    rather than hiding it.
    """
    snapshot = profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    corrections = snapshot.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    except (ActiveSpeakerConfigError, TypeError, ValueError):
        return {}
    return _branch_context(preset, corrections)


def profile_program_headroom_db(profile: Mapping[str, Any] | None) -> float:
    """The pre-split common attenuation one profile's linearization costs, dB."""
    linearization = profile_linearization(profile)
    if not linearization:
        return 0.0
    return linearization_headroom_db(
        linearization, branch_context=_profile_branch_context(profile or {}),
    )


def profile_blend_correction(
    profile: Mapping[str, Any] | None,
) -> tuple[Any, ...] | None:
    """Read the snapshot first, then the legacy top-level blend correction.

    None means unknown; () means no correction. Never turn unknown into zero:
    that can double-count correction already present in the measured graph.
    Preserve every entry for blend_filters_from_mapping to validate.
    """

    if not isinstance(profile, Mapping):
        return None
    snapshot = profile.get("recomposition_snapshot")
    raw = (
        snapshot.get("blend_correction") if isinstance(snapshot, Mapping) else None
    )
    if raw is None:
        raw = profile.get("blend_correction")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, Mapping)):
        return None
    return tuple(raw)


def profile_linearization(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE reduced linearization, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    linearization = (
        snapshot.get("linearization") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(linearization, Mapping):
        linearization = profile.get("linearization")
    return linearization if isinstance(linearization, Mapping) else {}


def applied_layers(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    return {row.purpose: bool(profile_linearization(profile) if row.purpose == PURPOSE_SPEAKER else
                              snapshot.get(row.candidate_fields[0].name,
                                           profile.get(row.candidate_fields[0].name) if row.profile_fallback else None))
            for row in PROGRAM_DOCUMENT_ORDER}


def applied_layer_names(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    """``applied_layers``, keyed by its packet-facing layer name instead of purpose."""
    layers = applied_layers(profile)
    return {row.applied_name: layers[row.purpose] for row in PROGRAM_DOCUMENT_ORDER}


def profile_driver_corrections(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE ``{role: {gain_db, delay_ms, inverted}}``, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    corrections = (
        snapshot.get("corrections") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(corrections, Mapping):
        corrections = profile.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    if any(
        isinstance(values, Mapping)
        and isinstance(values.get("delay_ms"), bool)
        for values in corrections.values()
    ):
        return {}
    return corrections


def applied_program_level_delta_db(
    previous_profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
) -> float:
    """dB the emitted graph's broadband level moved across one apply (#1811).

    Negative when the apply made the speaker quieter, the ordinary case: the
    applied correction's boost is absorbed as a pre-split common attenuation,
    so the same commanded volume plays quieter the instant the config swaps.

    An input to analysis, never to the speaker's level: the absorption keeps
    the boosted branch at or below unity (``camilla_yaml.program_headroom_db``)
    and compensating at main volume would undo it. The consumer is
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe`, whose
    realized-vs-commanded comparison is not mean-centred. Read from the
    profiles, never assumed; a correction that hands headroom back yields a
    positive number.

    The pre-split term only (#2611): the absorption precedes the branch split,
    so ``predicted_branch_sum`` has no place for it, while the per-branch trims
    are on the commanded axis (:mod:`jasper.active_speaker.crossover_v2.commanded`)
    and counting them here would remove them twice. Room-PEQ and preference-EQ
    headroom are not read: a round that changes either can move level this
    cannot see, which is the remainder the probe's ``residual_offset_db``
    measures (``delta_probe.VERDICT_LEVEL_MISMATCH``).
    """
    return profile_program_headroom_db(previous_profile) - (
        profile_program_headroom_db(applied_profile)
    )


def load_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read one validated baseline artifact without deriving fresh evidence."""
    return _load_saved_state(baseline_profile_state_path(path))


def load_applied_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the applied Layer-A SSOT, even while a new candidate is staged."""
    return _frozen_applied_profile(load_baseline_profile_state(path))


#: :func:`applied_profile_displacement`'s answers. ``""`` is the fourth and it
#: means the record is authoritative — an empty string rather than a word so
#: the check reads as a guard at every call site.
APPLIED_PROFILE_DISPLACED = "applied_profile_displaced"
APPLIED_PROFILE_PATH_UNKNOWN = "applied_profile_path_unknown"
APPLIED_PROFILE_RUNNING_UNKNOWN = "running_config_path_unknown"
#: The record names a config file that is no longer on disk. A fifth answer to
#: the same question, and NOT one of the four above: the record's own
#: ``config.exists`` is frozen at apply time, so only a fresh stat can say it.
APPLIED_PROFILE_CONFIG_MISSING = "applied_profile_config_missing"


def applied_profile_displacement(
    applied: Mapping[str, Any] | None,
    *,
    statefile_path: str | Path | None = None,
) -> str:
    """Is this applied-profile record still what the speaker is PLAYING? (#2537)

    ``""`` when the record's own ``config.path`` is the path CamillaDSP's
    durable statefile selects — the record is authoritative. Otherwise one of
    the three codes above, naming why it cannot be trusted as "the current
    sound".

    An out-of-band reconcile (``reconcile-current-dsp``) can move the running
    graph without touching the record, and the statefile is the one place that
    says so. This is a reader, never a second writer: the record's only writer
    stays :func:`~jasper.active_speaker.baseline_apply.persist_applied_baseline_profile`
    on the apply path, and out-of-band paths stay ignorant of the profile system.

    Fail-soft and reporting, never gating on its own: each caller decides what
    an unknown provenance means for its question. An unreadable statefile is
    :data:`APPLIED_PROFILE_RUNNING_UNKNOWN` ("could not check"), a different
    answer from "checked and it moved".
    """

    from .environment import read_camilla_statefile_config_path

    config = (applied or {}).get("config")
    recorded = str(config.get("path") or "") if isinstance(config, Mapping) else ""
    if not recorded:
        return APPLIED_PROFILE_PATH_UNKNOWN
    running = read_camilla_statefile_config_path(statefile_path)
    if not running:
        return APPLIED_PROFILE_RUNNING_UNKNOWN
    return "" if same_config_file(running, recorded) else APPLIED_PROFILE_DISPLACED
