# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The declared route claim and its post-DSP transport coherence.

A leaf the audio-health composer reads on its slow (60 s) cadence:
:func:`read_route_claim` is the public entry, pairing the runtime plan's
route profile with the same transport-coherence evidence ``jasper-doctor``
reads, so the dashboard and doctor cannot disagree about whether the post-DSP
route is connected.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ._health_fields import _MONITOR_ERRORS
from .. import paths
from ..output_topology import OutputTopologyError
from ..output_topology_store import load_output_topology_strict, load_output_topology

logger = logging.getLogger(__name__)


def _empty_transport() -> dict[str, Any]:
    """Return a fresh "no contradictions" transport state.

    Built per call, never copied from a module constant: ``dict(constant)`` is
    shallow, so every caller would share one ``coherence_errors`` list and a
    single append anywhere would report the box as parked for the lifetime of
    the process.
    """
    return {"coherence_errors": [], "capability_gap": None}


def _transport_state(
    *,
    outputd_env: Mapping[str, str],
    camilla_devices: Mapping[str, Any] | None,
    topology: Any,
) -> dict[str, Any]:
    """Pair the post-DSP transport contradictions with their actionable cause.

    ``transport_coherence_report`` is the single detector — doctor reads the
    same function — so this offers no second opinion about what "disconnected"
    means.  The capability gap says *why* it cannot self-heal when the saved
    layout needs hardware the DAC does not have.

    ``topology`` is an :class:`~jasper.output_topology.OutputTopology`, typed
    loosely because this module imports the topology layer lazily.
    """
    from ..active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        active_lane_capability_gap,
    )
    from ..transport_coherence import transport_coherence_report

    report = transport_coherence_report(
        outputd_env=dict(outputd_env),
        camilla_devices=camilla_devices,
        allow_grouping_capture=True,
    )
    gap = active_lane_capability_gap(topology)
    return {
        "coherence_errors": list(report.errors),
        # An unrecognized DAC profile carries no capability_gap: it is not
        # proof of a gap, only the absence of a profile to check.
        "capability_gap": gap.to_dict() if isinstance(gap, ActiveLaneCapabilityGap) else None,
    }


def _parked_graph_transport() -> dict[str, Any] | None:
    """Transport state for the intentional PARKED graph, or None when absent.

    Feeds :func:`~jasper.control.audio_signal_path._parked_signal` through the same
    ``coherence_errors`` channel the transport detector uses, so the parked
    wording keeps one writer. The capability gap is resolved as
    :func:`_transport_state` resolves it, so a no-active-lane DAC still gets
    that clause after this reason, not instead of it.
    """
    from ..active_speaker.environment import read_camilla_statefile_config_path
    from ..active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        active_lane_capability_gap,
    )
    from ..active_speaker.runtime_contract import (
        active_graph_is_parked,
        parked_muted_exits,
    )

    config_path = read_camilla_statefile_config_path(paths.DEFAULT_CAMILLA_STATEFILE)
    if not active_graph_is_parked(config_path):
        return None
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        # A malformed saved layout must not be reclassified as an empty draft:
        # that would tell a household to choose a new layout while hiding a
        # fault doctor correctly fails on.
        return {
            "coherence_errors": [
                "Saved speaker layout is unavailable or invalid; run jasper-doctor"
            ],
            "capability_gap": None,
        }
    gap = active_lane_capability_gap(topology)
    return {
        "coherence_errors": [
            "CamillaDSP is holding the parked graph, so every output is muted "
            f"({parked_muted_exits(topology)})"
        ],
        "capability_gap": gap.to_dict() if isinstance(gap, ActiveLaneCapabilityGap) else None,
    }


def _read_transport_state(plan: Any) -> dict[str, Any]:
    """Read the transport evidence the coherence detector needs, for ``plan``.

    Reads the evidence doctor reads, so the dashboard and ``jasper-doctor``
    cannot disagree about whether the post-DSP route is connected: both halves
    of the loopback pair (the loaded CamillaDSP graph, and outputd's LIVE
    capture PCM with its env as the fallback), and both statefiles.

    Silent when the loaded graph does not target a registered output endpoint:
    that is "coherence unknown", and doctor skips the same detector on the same
    evidence rather than reporting a contradiction it cannot see both halves of.

    ``plan.route_policy_errors`` is deliberately NOT read instead: that tuple
    mixes these contradictions with USB low-latency route-policy errors, and a
    policy error is not a reason to tell a household its speaker is parked.
    """
    from ..audio_runtime_plan import output_endpoint_evidence_from_statefiles  # lazy: import cost, keeps route assembly off control startup

    evidence = output_endpoint_evidence_from_statefiles(
        paths.DEFAULT_CAMILLA_STATEFILE,
        paths.DEFAULT_CAMILLA2_STATEFILE,
    )
    if evidence.devices is None or not evidence.endpoint_recognized:
        # One unrecognized endpoint is NOT "coherence unknown": the PARKED graph
        # (#2135) writes to a File sink on purpose, because the saved roleful
        # layout has no staged startup graph yet. That IS the parked state, so
        # it must not read as ready just because the graph declines to name an
        # outputd lane.
        return _parked_graph_transport() or _empty_transport()
    # The plan's own merged outputd env (both EnvironmentFile= layers), not a
    # second read of the same two files: this sampler runs every 60 s.
    outputd_env = dict(plan.outputd_env)
    return _transport_state(
        outputd_env=outputd_env,
        camilla_devices=evidence.devices,
        topology=load_output_topology(),
    )


def read_route_claim() -> dict[str, Any]:
    """Read the declared route and its transport coherence.

    Config/statefile work plus one bounded outputd STATUS read rather than a
    live audio probe, so it runs on the slow cadence.
    """
    try:
        from ..audio_runtime_plan import build_audio_runtime_plan_from_system

        plan = build_audio_runtime_plan_from_system()
        profile = plan.route_profile
        # Own try: a transport read that fails must not downgrade the whole
        # route claim to "unavailable" and take the latency card with it.
        try:
            transport = _read_transport_state(plan)
        except _MONITOR_ERRORS:
            logger.debug("audio transport coherence read failed", exc_info=True)
            transport = _empty_transport()
        return {
            "status": "available",
            "route_id": profile.route_id,
            "source_id": profile.source_id,
            "fixed_sample_rate": profile.fixed_sample_rate,
            "low_latency_claim": profile.low_latency_claim,
            "route_config_hash": plan.route_config_hash,
            "transport": transport,
        }
    except _MONITOR_ERRORS:
        logger.debug("audio route claim read failed", exc_info=True)
        return {
            "status": "unavailable",
            "route_id": None,
            "source_id": None,
            "fixed_sample_rate": None,
            "low_latency_claim": False,
            "route_config_hash": None,
            "transport": _empty_transport(),
        }
