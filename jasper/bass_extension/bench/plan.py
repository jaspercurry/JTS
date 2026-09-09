# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign's inputs, composed from the applied candidate's own field.

One :class:`~jasper.bass_extension.bench.runner.TargetPlan` per rung the
operator asked for, plus the ``measured_context`` the bundle carries. It is
handed the applied snapshot and composes from it; nothing here opens a device,
a socket, or a CamillaDSP connection, so the operator's dry run composes
exactly what the live run will activate.

Two owners, consumed and never re-implemented: the graph a rung plays comes
from :func:`jasper.sound.graph_carrier.recompose_active_baseline_for_bass_extension`
(the one composer, whose own whole-graph proof runs against that rung), and the
authority summary that graph is proved against comes from
:func:`jasper.bass_extension.candidate_field.graph_summary`. The bench never
emits a graph itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from jasper.active_speaker.camilla_yaml import bass_owner_limiter_name
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.bass_extension.candidate_field import (
    BassCandidateFieldError,
    applied_bass_extension_field,
    emitted_rung,
    graph_summary,
    validate_bass_extension_field,
)
from jasper.sound.graph_carrier import (
    CarrierCannotHostEq,
    recompose_active_baseline_for_bass_extension,
)

from .activation import ActivationError, read_configured_clip_limit
from .context import (
    DETECTOR_REFERENCE,
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
    limiter_domain_fingerprint,
)
from .manifest import CampaignManifest
from .runner import BenchRefused, TargetPlan

#: This speaker has applied no bass family, or one this reader will not read.
REFUSE_NO_APPLIED_FAMILY = "bench_no_applied_family"
#: The operator named a rung the applied family does not carry.
REFUSE_TARGET_NOT_IN_FAMILY = "bench_target_not_in_family"
#: The manifest's margin policy is not the one the family was sized under.
REFUSE_MARGIN_MISMATCH = "bench_margin_mismatch"
#: The one composer refused to build the rung's graph.
REFUSE_GRAPH_UNAVAILABLE = "bench_rung_graph_unavailable"
#: The bass owner has no confirmed driver-safety target to admit against.
REFUSE_OWNER_TARGET_UNMAPPED = "bench_owner_target_unmapped"


def target_plans(
    topology: Any,
    applied_profile: Mapping[str, Any] | None,
    *,
    target_ids: Sequence[str],
    current_config_path: str | Path,
    margin_policy_name: str,
) -> tuple[TargetPlan, ...]:
    """One plan per named rung of the applied family, deepest through natural.

    ``current_config_path`` is the selected live graph file: the composer
    rebuilds THIS graph's program overlays and refuses if it cannot reproduce
    them, so the campaign activates the installed program with one rung
    swapped in and nothing else moved.
    """

    family = _applied_family(applied_profile)
    if family["margin_policy_name"] != margin_policy_name:
        raise BenchRefused(
            REFUSE_MARGIN_MISMATCH,
            f"the campaign manifest selects the {margin_policy_name!r} margin "
            f"policy but the applied family was sized under "
            f"{family['margin_policy_name']!r}",
        )
    owner_channels = tuple(int(channel) for channel in family["owner"]["channels"])
    limiter_name = bass_owner_limiter_name(str(family["owner"]["role"]))
    field = applied_bass_extension_field(applied_profile)
    plans: list[TargetPlan] = []
    for target_id in target_ids:
        rung = _rung(family, target_id)
        graph_raw_text = _rung_graph(
            topology,
            applied_profile=applied_profile,
            field=field,
            target_id=target_id,
            current_config_path=current_config_path,
        )
        plans.append(
            TargetPlan(
                target_id=target_id,
                target_fingerprint=json_fingerprint(
                    {"target": rung["target"], "basis": family["basis"]},
                    field_name="bass extension bench target",
                ),
                graph_raw_text=graph_raw_text,
                limiter_name=limiter_name,
                owner_channels=owner_channels,
                profile_summary=graph_summary(field, target_id=target_id),
                baseline_clip_limit_dbfs=_baseline_clip_limit(
                    graph_raw_text, limiter_name, target_id
                ),
                boost_headroom_db=float(rung["target"]["boost_headroom_db"]),
            )
        )
    return tuple(plans)


def bench_role_targets(
    role_targets: Mapping[str, str],
    safety_profile: Mapping[str, Any],
    *,
    owner_role: str,
) -> dict[str, str]:
    """``role_targets`` widened to the bass owner's own driver-safety target.

    ``resolve_conductor_context`` maps only the preset's declared driver roles,
    which never include a local subwoofer, so a sub-owned family would reach
    play-time re-admission with no target for its segment's role and refuse
    there (``program_target_not_mapped``) — after the graph was activated. The
    owner's target is resolved from the confirmed driver-safety profile (the
    same declaration admission reads) here instead, before any audio, and its
    absence is a refusal rather than a late one.
    """

    if owner_role in role_targets:
        return dict(role_targets)
    declared = safety_profile.get("targets")
    matches = [
        str(target.get("target_fingerprint") or "")
        for target in (declared if isinstance(declared, Sequence) else ())
        if isinstance(target, Mapping) and target.get("role") == owner_role
    ]
    matches = [fingerprint for fingerprint in matches if fingerprint]
    if len(matches) != 1:
        raise BenchRefused(
            REFUSE_OWNER_TARGET_UNMAPPED,
            f"the confirmed driver-safety profile declares {len(matches)} "
            f"targets for the {owner_role} the bass family owns, not exactly "
            "one; nothing can be admitted for it",
        )
    return {**role_targets, owner_role: matches[0]}


def campaign_measured_context(
    applied_profile: Mapping[str, Any] | None,
    plans: Sequence[TargetPlan],
    *,
    manifest: CampaignManifest,
    camilladsp_build_id: str,
    tap_implementation_id: str,
    transparency_policy_fingerprint: str,
    natural_graph_fingerprint: str,
) -> dict[str, Any]:
    """The bundle's ``measured_context``, every field from its own owner.

    ``target_order`` is the campaign's own plan order (deepest through
    natural); the limiter, owner channels and baseline clip limit are the
    plans' — read off the graph that will be activated, not asserted here.
    """

    if not plans:
        raise BenchRefused(
            REFUSE_TARGET_NOT_IN_FAMILY, "the campaign names no target"
        )
    first = plans[0]
    return {
        "target_family_fingerprint": json_fingerprint(
            _applied_family(applied_profile),
            field_name="bass extension family",
        ),
        "target_order": [
            {
                "target_id": plan.target_id,
                "target_fingerprint": plan.target_fingerprint,
            }
            for plan in plans
        ],
        "driver_safety_fingerprint": manifest.driver_safety_fingerprint,
        "margin_policy_fingerprint": manifest.margin_policy_fingerprint,
        "transparency_policy_fingerprint": transparency_policy_fingerprint,
        "natural_graph_fingerprint": natural_graph_fingerprint,
        "baseline_limiter_clip_limit_dbfs": first.baseline_clip_limit_dbfs,
        "limiter_domain_min_dbfs": LIMITER_DOMAIN_MIN_DBFS,
        "limiter_domain_max_dbfs": LIMITER_DOMAIN_MAX_DBFS,
        "limiter_domain_fingerprint": limiter_domain_fingerprint(),
        "camilladsp_build_id": camilladsp_build_id,
        "owner_channels": list(first.owner_channels),
        "sample_rate_hz": _graph_sample_rate_hz(first.graph_raw_text),
        "limiter_name": first.limiter_name,
        "limiter_type": "Limiter",
        "soft_clip": True,
        "tap_implementation_id": tap_implementation_id,
        "detector_reference": DETECTOR_REFERENCE,
    }


def _applied_family(applied_profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """The applied bass family, validated, or a refusal.

    A family this reader would refuse is no family to bench: the emitter would
    refuse the same field a moment later, and a graph is never built from
    evidence that failed its own check.
    """

    field = applied_bass_extension_field(applied_profile)
    if field is None:
        raise BenchRefused(
            REFUSE_NO_APPLIED_FAMILY,
            "this speaker's applied baseline carries no bass-extension family",
        )
    try:
        return validate_bass_extension_field(field)
    except BassCandidateFieldError as exc:
        raise BenchRefused(REFUSE_NO_APPLIED_FAMILY, str(exc)) from exc


def _graph_sample_rate_hz(graph_raw_text: str) -> int:
    """The rate the composed graph runs at, off its own ``devices`` block."""

    devices = _parsed(graph_raw_text, "campaign").get("devices")
    rate = devices.get("samplerate") if isinstance(devices, Mapping) else None
    if type(rate) is not int or rate <= 0:
        raise BenchRefused(
            REFUSE_GRAPH_UNAVAILABLE,
            "the composed graph declares no devices.samplerate",
        )
    return rate


def _rung(family: Mapping[str, Any], target_id: str) -> Mapping[str, Any]:
    rung = emitted_rung(family, target_id)
    if rung is None:
        raise BenchRefused(
            REFUSE_TARGET_NOT_IN_FAMILY,
            f"the applied family carries no {target_id!r} rung",
        )
    return rung


def _rung_graph(
    topology: Any,
    *,
    applied_profile: Mapping[str, Any] | None,
    field: Mapping[str, Any] | None,
    target_id: str,
    current_config_path: str | Path,
) -> str:
    try:
        return recompose_active_baseline_for_bass_extension(
            topology,
            applied_profile=applied_profile,
            desired_profile=field,
            current_config_path=current_config_path,
            bass_target_id=target_id,
        )
    except CarrierCannotHostEq as exc:
        raise BenchRefused(
            REFUSE_GRAPH_UNAVAILABLE, f"{target_id}: {exc}"
        ) from exc


def _baseline_clip_limit(
    graph_raw_text: str, limiter_name: str, target_id: str
) -> float:
    try:
        return read_configured_clip_limit(
            _parsed(graph_raw_text, target_id), limiter_name
        )
    except ActivationError as exc:
        raise BenchRefused(
            REFUSE_GRAPH_UNAVAILABLE, f"{target_id}: {exc}"
        ) from exc


def _parsed(graph_raw_text: str, target_id: str) -> Mapping[str, Any]:
    try:
        parsed = yaml.safe_load(graph_raw_text)
    except yaml.YAMLError as exc:
        raise BenchRefused(
            REFUSE_GRAPH_UNAVAILABLE, f"{target_id}: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise BenchRefused(
            REFUSE_GRAPH_UNAVAILABLE,
            f"{target_id}: the composed graph is not a mapping",
        )
    return parsed
