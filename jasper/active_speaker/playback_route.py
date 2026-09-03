# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker playback route capability contract.

Active-speaker test/apply audio reaches hardware through a narrower runtime route than the full DAC output topology describes. Route resolution itself (stable ``hw:CARD=`` identity, DAC-agnostic transport plan) lives on :mod:`jasper.output_topology`; this module is a thin reader over :func:`~jasper.output_topology.resolve_output_layout` that adds speaker-group demand accounting and route-fit issues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jasper.audio_hardware.dac import by_id as _dac_by_id
from jasper.log_event import log_event
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    EXPLICIT_SOURCE,
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    OutputLayout,
    OutputTopology,
    SpeakerGroup,
    resolve_output_layout,
)

from ._common import issue as _issue

logger = logging.getLogger(__name__)

# Re-exported: constants moved to jasper.output_topology; kept importable here.
__all__ = [
    "ACTIVE_PLAYBACK_DEVICE_ENV",
    "ACTIVE_PLAYBACK_ROUTE_KIND",
    "EXPLICIT_SOURCE",
    "LOADED_GRAPH_SOURCE",
    "MISSING_SOURCE",
    "OUTPUTD_ACTIVE_LANE_SOURCE",
    "ActiveLaneCapabilityGap",
    "ActivePlaybackRouteCapability",
    "UnrecognizedDacProfile",
    "active_lane_capability_gap",
    "active_playback_route_capability",
    "resolve_active_playback_device",
    "resolve_live_active_endpoint",
]

ACTIVE_PLAYBACK_ROUTE_KIND = "jts_active_speaker_playback_route_capability"

# Witness for :func:`resolve_live_active_endpoint`: the graph itself, distinct
# from ``OUTPUTD_ACTIVE_LANE_SOURCE`` ("marker selects" vs "graph IS on").
LOADED_GRAPH_SOURCE = "loaded_graph"


def _active_main_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    return [
        group
        for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
        and group.kind != "subwoofer"
    ]


def _subwoofer_groups(topology: OutputTopology) -> list[SpeakerGroup]:
    routed = set(topology.routing.subwoofer_group_ids)
    return [
        group
        for group in topology.speaker_groups
        if (
            group.kind == "subwoofer"
            or group.mode == "subwoofer"
            or group.id in routed
        )
    ]


def _highest_assigned_output(groups: list[SpeakerGroup]) -> int | None:
    indexes = [
        channel.physical_output_index
        for group in groups
        for channel in group.channels
        if channel.physical_output_index is not None
    ]
    return max(indexes) if indexes else None


@dataclass(frozen=True)
class ActiveLaneCapabilityGap:
    """A saved layout needs the active outputd lane; this DAC declares none."""

    device_id: str
    device_label: str

    def to_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "device_label": self.device_label}


@dataclass(frozen=True)
class UnrecognizedDacProfile:
    """A saved layout needs the active outputd lane, but ``device_id`` has no registered :class:`~jasper.audio_hardware.dac.DacProfile` to read that capability off. Distinct from :class:`ActiveLaneCapabilityGap`: a gap is proof the DAC cannot drive the layout, this is absence of proof either way."""

    device_id: str

    def to_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id}


def active_lane_capability_gap(
    topology: OutputTopology,
) -> ActiveLaneCapabilityGap | UnrecognizedDacProfile | None:
    """Return why ``topology`` can never reach hardware on this DAC, or None.

    The one predicate for a permanently-undrivable pairing: the layout needs a roleful graph, but the resolved ``DacProfile`` declares no active outputd lane -- the speaker then emits digital silence with every daemon healthy. Only a different layout or hardware fixes it. Three-valued: an unrecognized ``device_id`` returns :class:`UnrecognizedDacProfile` rather than a guessed gap or ``None``; a caller wanting only the definite gap narrows with ``isinstance(gap, ActiveLaneCapabilityGap)``.
    """

    from jasper.active_speaker.runtime_contract import (
        active_topology_requires_roleful_graph,
    )

    if not active_topology_requires_roleful_graph(topology):
        return None
    device_id = topology.hardware.device_id
    profile = _dac_by_id(device_id)
    if profile is None:
        return UnrecognizedDacProfile(device_id=device_id)
    if profile.supports_active_outputd_lane:
        return None
    return ActiveLaneCapabilityGap(
        device_id=device_id,
        # Registry owns the DAC's name; the topology's saved label can be stale.
        device_label=profile.label or topology.hardware.device_label or device_id,
    )


def resolve_active_playback_device(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> tuple[str | None, str]:
    """Return the active-speaker playback PCM."""

    layout = resolve_output_layout(
        topology,
        playback_device=playback_device,
    )
    return layout.playback_device, layout.playback_device_source


def resolve_live_active_endpoint(
    topology: OutputTopology,
) -> tuple[str | None, str]:
    """The playback endpoint this box's active graph is CURRENTLY on.

    ONE derivation for every seam that RE-EMITS or RE-DERIVES an active graph the box already runs -- such a seam must not *choose* an endpoint, only confirm the one the box is on. :func:`resolve_active_playback_device` answers the other question (which endpoint a FRESH emit should target).

    THE GRAPH IS UPSTREAM TRUTH, asked first: the endpoint marker (``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT``) is *derived from* the graph by ``jasper-audio-hardware-reconcile``, so on disagreement the graph is what reconcilers converge toward. Only :data:`~jasper.active_speaker.runtime_contract.OUTPUTD_LEGAL_ENDPOINT_DEVICES` (a ONE-member set since #2285 P2 retired the snd-aloop ACTIVE endpoint) is adopted from the graph; anything else falls through to the chooser below, which already honours an explicit ``JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE`` override. The MARKER answers only when the graph does not (a fresh box has no statefile) -- DEFAULT-SAFE, not fail-loud.

    ``(None, MISSING_SOURCE)`` passes through unchanged, so a caller threading this into ``recompose_applied_baseline_yaml(playback_device=...)`` lands on that function's own snapshot default.

    COST: one statefile read plus one config read, fresh, per call -- uncached by design, because acting on a stale endpoint is the defect this prevents. A caller on a warm path should snapshot the answer itself.
    """

    # Lazy: runtime_contract owns which devices are legal active endpoints.
    from jasper.active_speaker.runtime_contract import OUTPUTD_LEGAL_ENDPOINT_DEVICES
    from jasper.fanin.ring_health import read_loaded_camilla_graph

    graph = read_loaded_camilla_graph()
    device = graph.devices.get("playback_device")
    if isinstance(device, str) and device.strip():
        named = device.strip()
        if named in OUTPUTD_LEGAL_ENDPOINT_DEVICES:
            return named, LOADED_GRAPH_SOURCE
        # Graph names a sink outside the legal set (stale lane, lab PCM, pipe,
        # or the retired aloop endpoint). DEBUG not WARNING: a lab box hits
        # this branch legitimately on every call.
        log_event(
            logger,
            "active_speaker.live_endpoint",
            level=logging.DEBUG,
            result="declined_non_endpoint_device",
            observed=named,
            config=graph.path or "",
            answered_by="playback_route_chooser",
        )
    return resolve_active_playback_device(topology)


@dataclass(frozen=True)
class ActivePlaybackRouteCapability:
    """Current active-speaker runtime route capacity for a saved topology."""

    playback_device: str | None
    playback_device_source: str
    transport_channel_count: int
    required_active_output_count: int
    active_group_count: int
    subwoofer_group_count: int
    subwoofer_supported: bool
    issues: tuple[dict[str, str], ...]

    @property
    def fits_required_outputs(self) -> bool:
        return (
            self.required_active_output_count <= self.transport_channel_count
            if self.transport_channel_count > 0
            else self.required_active_output_count == 0
        )

    @property
    def ready(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ACTIVE_PLAYBACK_ROUTE_KIND,
            "playback_device": self.playback_device,
            "playback_device_source": self.playback_device_source,
            "transport_channel_count": self.transport_channel_count,
            "required_active_output_count": self.required_active_output_count,
            "active_group_count": self.active_group_count,
            "subwoofer_group_count": self.subwoofer_group_count,
            "subwoofer_supported": self.subwoofer_supported,
            "fits_required_outputs": self.fits_required_outputs,
            "ready": self.ready,
            "issues": list(self.issues),
        }


def active_playback_route_capability(
    topology: OutputTopology,
    *,
    playback_device: str | None = None,
) -> ActivePlaybackRouteCapability:
    """Return the active-speaker runtime route capacity.

    Thin reader: route half (device, source, transport width, subwoofer
    support) comes from the resolved ``OutputLayout``; this adds speaker-group
    demand accounting and route-fit issues. Transport width is
    profile-declared, not the DAC's analog output count.
    """

    layout: OutputLayout = resolve_output_layout(
        topology,
        playback_device=playback_device,
    )
    active_groups = _active_main_groups(topology)
    subwoofer_groups = _subwoofer_groups(topology)
    required_groups = active_groups + subwoofer_groups
    highest = _highest_assigned_output(required_groups)
    required_outputs = (highest + 1) if highest is not None else 0

    resolved_device = layout.playback_device
    transport_channels = layout.transport_channel_count

    issues: list[dict[str, str]] = []
    if active_groups and not resolved_device:
        issues.append(_issue(
            "blocker",
            "active_playback_route_unavailable",
            "active-speaker tests need a resolved playback route",
        ))
    if active_groups and resolved_device and transport_channels <= 0:
        issues.append(_issue(
            "blocker",
            "active_playback_route_width_unknown",
            "active-speaker tests need a profile-declared playback route width",
        ))
    if transport_channels and required_outputs > transport_channels:
        issues.append(_issue(
            "blocker",
            "active_playback_route_too_narrow",
            (
                f"This install can drive {transport_channels} active output "
                f"lanes, but this layout uses DAC output {required_outputs}."
            ),
        ))

    return ActivePlaybackRouteCapability(
        playback_device=resolved_device,
        playback_device_source=layout.playback_device_source,
        transport_channel_count=transport_channels,
        required_active_output_count=required_outputs,
        active_group_count=len(active_groups),
        subwoofer_group_count=len(subwoofer_groups),
        subwoofer_supported=layout.subwoofer_supported,
        issues=tuple(issues),
    )
