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

from pathlib import Path
from typing import Any, Mapping

from jasper.fanin_coupling import RING_PCM_DEVICES, TRANSPORT_RING
from jasper.json_fields import as_mapping
from jasper.output_topology import OutputTopologyError
from jasper.output_topology_store import load_output_topology_strict

from .candidate_bank import load_applied_candidate
from .crossover_contract import (
    crossover_snapshot_state,
    legacy_manual_preservation_state,
)
from .environment import read_camilla_statefile_config_path
from .profile import ActiveSpeakerConfigError
from .setup_readiness import (
    IN_SEQUENCE_CAPTURE_ANCHOR_REASON as IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
    active_group_count,
    readiness_snapshot,
)
from .state_paths import baseline_profile_state_path


# ``ActiveSpeakerConfigError`` is named even though it subclasses ``ValueError``:
# a graph naming a forbidden playback lane makes the emitter refuse, and this
# surface is household-facing, so an indeterminate input must return the
# ``unavailable`` snapshot rather than a traceback. Naming the class keeps that
# legible at the one site where narrowing this tuple would reopen it.
_READINESS_DERIVATION_ERRORS = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    ActiveSpeakerConfigError,
    KeyError,
)
_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)


def _grouped_active_runtime() -> bool:
    """Fresh Active-owned scope fact for both bonded leaders and followers."""

    from jasper.multiroom.config import is_active_member, load_config

    return is_active_member(load_config())


def _idle_commissioning_summary() -> dict[str, Any]:
    return {
        "phase": "idle",
        "applied_profile_fingerprint": None,
        "last_failure_code": None,
        # No topology resolved, so no transport to name (#2412): `null` rather
        # than a guess, since asserting a transport for a box whose route could
        # not be read is the half-fact this key exists to remove.
        "transport": None,
    }


def _commissioning_transport(topology: Any) -> str | None:
    """Which transport this box's commissioning would emit on, or ``None``.

    ONE token with the two ``driver_commission_*`` journal lines
    (:data:`jasper.fanin_coupling.TRANSPORT_RING`), keyed on the same
    :data:`~jasper.fanin_coupling.RING_PCM_DEVICES` membership and reading the
    same chooser (``resolve_active_playback_device``). The shared thing is the
    DERIVATION, not the input: ``prepare`` accepts a caller-supplied
    ``playback_device=`` override, so the agreement is a convention this API
    does not enforce.

    SINGLE-TRANSPORT: this field is ``ring`` or ``None``, ADR-0100 having left
    no second transport. ``None`` when the topology cannot be read or resolves
    to no device — a device string that is not a ring end is reachable only
    through an explicit lab/CI override. Rolefulness is deliberately NOT the
    discriminator: a PASSIVE box resolves the active outputd lane and reports
    ``ring`` exactly as a roleful one does, and the ACTIVE-endpoint marker takes
    no part in the derivation at all.

    The gate is the DAC PROFILE: ``resolve_output_layout`` names the ring when
    the profile declares an active outputd lane. All five registered
    ``DacProfile``s declare one, so the fall-through to no device is unreachable
    from any shipped profile today.

    ``AttributeError`` joins ``_READINESS_DERIVATION_ERRORS`` for this call
    only: ``resolve_output_layout`` walks ``topology.hardware`` unguarded, so a
    ``None`` or duck-typed topology raises a class no sibling derivation does,
    and an observability field must never stop ``/system/snapshot`` answering.
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
) -> dict[str, Any]:
    profile = profile if isinstance(profile, Mapping) else None
    applied_profile = applied_profile if isinstance(applied_profile, Mapping) else None

    # Phase derivation is pinned in priority order: failed, then
    # proposal_ready, else idle.
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
        as_mapping(profile.get("permissions")).get("may_apply")
        or as_mapping(profile.get("permissions")).get("may_compile")
    ):
        phase = "proposal_ready"
    else:
        phase = "idle"

    applied_profile_fingerprint = (applied_profile or {}).get(
        "candidate_fingerprint"
    )

    return {
        "phase": phase,
        "applied_profile_fingerprint": applied_profile_fingerprint,
        "last_failure_code": last_failure_code,
        # A device name without its transport is the half-fact behind #2412.
        "transport": _commissioning_transport(topology),
    }


def commissioning_summary(
    topology: Any,
    *,
    profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Small household/operator commissioning summary for ``/system/snapshot``.

    Pure over ``profile``/``applied_profile`` and fail-soft: any unreadable or
    malformed input degrades to the safest phase (``"idle"``) rather than
    raising. Detailed curves and bundle paths belong to the session report, not
    ``/system/snapshot``.
    """
    try:
        return _derive_commissioning_summary(
            topology,
            profile=profile,
            applied_profile=applied_profile,
        )
    except _READINESS_DERIVATION_ERRORS:
        return _idle_commissioning_summary()


def active_config_path_from_statefile(
    path: str | Path | None = None,
) -> str:
    """Best-effort active CamillaDSP config path from the outputd statefile.

    Delegates to the canonical ``JASPER_CAMILLA_STATEFILE`` reader,
    :func:`jasper.active_speaker.environment.read_camilla_statefile_config_path`.
    ``""`` (not ``None``) on an unreadable or empty statefile.
    """

    return read_camilla_statefile_config_path(path) or ""


_LAYER_A_DIFFERENCE_LIMIT = 6


def _layer_a_filter_fields(config_text: str) -> dict[str, Any]:
    """Flatten one graph's Layer-A filters to ``<filter>.<parameter>`` values."""

    from .baseline_profile import active_layer_a_projection

    filters = active_layer_a_projection(config_text).get("filters")
    fields: dict[str, Any] = {}
    for name, definition in (
        filters.items() if isinstance(filters, Mapping) else ()
    ):
        body = as_mapping(definition)
        fields[f"{name}.type"] = body.get("type")
        for key, value in as_mapping(body.get("parameters")).items():
            fields[f"{name}.{key}"] = value
    return fields


def _layer_a_differences(
    expected_yaml: str, loaded_yaml: str,
) -> list[dict[str, str]]:
    """Name the Layer-A filter parameters two graphs disagree on, with values.

    The fingerprint pair says THAT the loaded driver-domain graph is not the one
    the applied profile names; an operator acts on WHICH value moved. Bounded,
    so an emitter-wide respelling cannot turn one disclosure into a wall, and
    partial by construction: a graph differing only in routing, mixers or
    devices yields no entries and the fingerprints stand alone.
    """

    expected = _layer_a_filter_fields(expected_yaml)
    loaded = _layer_a_filter_fields(loaded_yaml)
    return [
        {
            "field": field,
            "expected": repr(expected.get(field)),
            "loaded": repr(loaded.get(field)),
        }
        for field in sorted(set(expected) | set(loaded))
        if expected.get(field) != loaded.get(field)
    ][:_LAYER_A_DIFFERENCE_LIMIT]


def _applied_layer_a_binding(
    topology: Any,
    *,
    applied_profile: Mapping[str, Any] | None,
    active_config_path: str | None,
    active_config_text: str | None,
) -> dict[str, Any]:
    """Compare the compiled applied candidate with the loaded graph."""

    from .baseline_profile import active_layer_a_fingerprint  # lazy: baseline readers import setup status
    from jasper.camilla_config_contract import parse_camilla_devices_config  # lazy: binding reads the loaded graph
    from .candidate_bank import CandidateBankRefusal  # lazy: candidate lookup boundary
    from .candidate_parts import candidate_from_applied_profile  # lazy: baseline readers import setup status
    from .measurement_emit import compile_tuning_graph, load_tuning_declaration  # lazy: graph compilation imports NumPy
    from jasper.sound.settings import saved_sound_layers  # lazy: household EQ imports NumPy

    unavailable = {
        "status": "unverifiable",
        "matches": False,
        "expected_fingerprint": None,
        "loaded_fingerprint": None,
        "differences": [],
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
        # program-domain bake; its driver-domain Layer A lives on the crossover
        # instance. The solo v1 fingerprint cannot bind that distributed graph,
        # so Active emits an explicit unsupported decision instead of a
        # misleading crossover-reapply mismatch.
        if (
            _grouped_active_runtime()
            or f"Source: {_PROGRAM_BAKE_SOURCE}" in loaded_yaml
        ):
            return {
                "status": "distributed_active_unsupported",
                "matches": False,
                "expected_fingerprint": None,
                "loaded_fingerprint": None,
                "differences": [],
            }
        playback_device = parse_camilla_devices_config(loaded_yaml)["playback_device"]
        declaration = load_tuning_declaration(topology, playback_device=playback_device)
        candidate = candidate_from_applied_profile(topology, applied_profile,
            find_candidate=lambda fingerprint: load_applied_candidate(fingerprint, applied_profile=applied_profile))
        preference_filters, trim_db = saved_sound_layers()
        expected_yaml = compile_tuning_graph(declaration, candidate=candidate,
            preference_filters=preference_filters, output_trim_db=trim_db)
        expected = active_layer_a_fingerprint(expected_yaml)
        loaded = active_layer_a_fingerprint(loaded_yaml)
        matches = expected == loaded
        differences = (
            [] if matches else _layer_a_differences(expected_yaml, loaded_yaml)
        )
    except (*_READINESS_DERIVATION_ERRORS, CandidateBankRefusal):
        return unavailable
    return {
        "status": "current" if matches else "mismatch",
        "matches": matches,
        "expected_fingerprint": expected,
        "loaded_fingerprint": loaded,
        "differences": differences,
    }


def read_active_speaker_setup_status(
    *,
    active_config_path: str | None = None,
    active_config_text: str | None = None,
    baseline_state_path: str | Path | None = None,
    include_diagnostics: bool = True,
) -> dict[str, Any]:
    """Read output permission, then optionally add the setup report.

    Volume controls skip diagnostics. Both views use the same fresh applied
    profile and readiness decision; candidate compilation cannot change it.
    """
    topology = None
    applied_profile = None
    read_error = None
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        read_error = str(exc)
    if topology is not None and active_group_count(topology):
        from .baseline_profile import load_applied_baseline_profile_state  # lazy: passive speakers skip baseline imports

        if active_config_path is None:
            active_config_path = active_config_path_from_statefile()
        try:
            applied_profile = load_applied_baseline_profile_state(baseline_state_path)
        except _READINESS_DERIVATION_ERRORS as exc:
            read_error = type(exc).__name__
    status = readiness_snapshot(
        topology, applied_profile=applied_profile,
        active_config_path=active_config_path, read_error=read_error,
    )
    if not include_diagnostics:
        return status

    profile = None
    if topology is not None and status["active"]:
        from .baseline_profile import compile_commissioning_profile  # lazy: import cost — setup diagnostics
        from .design_draft import load_design_draft  # lazy: import cost — setup diagnostics

        try:
            profile = compile_commissioning_profile(
                applied_profile=applied_profile, topology=topology,
                design_draft=load_design_draft(),
                find_candidate=lambda fingerprint: load_applied_candidate(
                    fingerprint, applied_profile=applied_profile or {},
                ),
            )
        except _READINESS_DERIVATION_ERRORS as exc:
            profile = {"status": "unavailable", "issues": [{
                "severity": "warning", "code": "setup_diagnostics_unavailable",
                "message": f"speaker setup diagnostics could not be derived: {type(exc).__name__}",
            }]}
        source = as_mapping(profile.get("source"))
        status["baseline_profile"] = {
            "status": profile.get("status"),
            "path": str(baseline_profile_state_path(baseline_state_path)),
            "config_path": as_mapping(profile.get("config")).get("path"),
            "source_fingerprint": source.get("fingerprint"),
            "candidate_fingerprint": profile.get("candidate_fingerprint"),
            "provisional": bool(profile.get("provisional")),
            "issues": [{
                "severity": str(item.get("severity") or "blocker"),
                "code": str(item.get("code") or "baseline_profile_issue"),
                "message": str(item.get("message") or "active speaker baseline issue"),
            } for item in profile.get("issues", []) if isinstance(item, Mapping)],
            "role": "staging_candidate",
            "live_answer_key": "protected_profile",
            "matches_applied": None if applied_profile is None else bool(
                profile.get("candidate_fingerprint")
                and profile.get("candidate_fingerprint") == applied_profile.get("candidate_fingerprint")
            ),
        }
        status["protected_profile"]["layer_a_binding"] = _applied_layer_a_binding(
            topology, applied_profile=applied_profile,
            active_config_path=active_config_path, active_config_text=active_config_text,
        )
        status["applied_crossover"] = crossover_snapshot_state(
            applied_profile, expected_topology_id=topology.topology_id,
            expected_topology_fingerprint=str(source.get("topology_fingerprint") or "") or None,
        )
        status["manual_preservation"] = legacy_manual_preservation_state(
            applied_profile, current_source_fingerprint=str(source.get("fingerprint") or "") or None,
        )
    status["commissioning"] = commissioning_summary(
        topology, profile=profile, applied_profile=applied_profile,
    )
    return status
